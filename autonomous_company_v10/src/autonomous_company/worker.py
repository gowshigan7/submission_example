"""Worker execution module for autonomous_company_v10.

A :class:`Worker` is responsible for executing a single :class:`StepSpec`
of the CEO's TeamPlan via the Claude Agent SDK. It handles:

* **Resumption** -- skips steps already marked ``COMPLETED`` in storage.
* **A2A context injection** -- formats inbox messages, peer roles, and a
  workspace notice into the user prompt.
* **Security** -- builds pre-tool hooks that consult the
  :class:`SecurityHooks` allow-list.
* **Resilience** -- exponential-backoff retries on transient failures.
* **Persistence** -- updates :class:`StepCheckpoint` rows throughout
  execution so the orchestrator can resume after a crash.
* **Streaming** -- forwards SDK stream events to the SSE ``emit`` callback.
* **Redaction** -- scrubs known secret patterns from any text emitted to
  the UI or logs.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable, TYPE_CHECKING

import structlog

from .a2a import MessageKind, Severity, WorkerReply  # noqa: F401
from .llm import AgentReply, ask
from .models import Agent, BudgetExceeded, StepCheckpoint, StepFailed, StepStatus
from .native_agents import merge_with_runtime_agents
from .telemetry import get_cost_collector, get_tracer

if TYPE_CHECKING:
    from .config import Settings
    from .decisions import StepSpec
    from .security_hooks import SecurityHooks
    from .storage import Storage


__all__ = [
    "Emit",
    "Worker",
    "WORKER_DISALLOWED_TOOLS",
    "redact_payload",
]


Emit = Callable[[str, dict], Awaitable[None]]

log = structlog.get_logger("autonomous_company.worker")


WORKER_DISALLOWED_TOOLS = ["AskUserQuestion"]


_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p), repl)
    for p, repl in [
        (r'(?i)(sk-ant-[A-Za-z0-9\-_]{20,})', '***ANTHROPIC_KEY***'),
        (r'(?i)(sk-[A-Za-z0-9]{20,})', '***OPENAI_KEY***'),
        (r'(AKIA[0-9A-Z]{16})', '***AWS_ACCESS_KEY***'),
        (r'(ghp_[A-Za-z0-9]{36})', '***GITHUB_TOKEN***'),
        (r'(gho_[A-Za-z0-9]{36})', '***GITHUB_OAUTH***'),
        (r'(github_pat_[A-Za-z0-9_]{82})', '***GITHUB_PAT***'),
        (r'(sk_live_[A-Za-z0-9]{24,})', '***STRIPE_LIVE_KEY***'),
        (r'(sk_test_[A-Za-z0-9]{24,})', '***STRIPE_TEST_KEY***'),
        (r'(eyJ[A-Za-z0-9\-_=]+\.eyJ[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_.+/=]*)', '***JWT***'),
        (r'(?i)(Bearer\s+[A-Za-z0-9\-_.~+/]+=*)', '***BEARER_TOKEN***'),
        (r'(xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+)', '***SLACK_BOT_TOKEN***'),
        (r'(xoxa-[0-9]+-[A-Za-z0-9]+)', '***SLACK_APP_TOKEN***'),
        (r'(?i)(discord_token\s*=\s*[A-Za-z0-9\.\-_]{50,})', '***DISCORD_TOKEN***'),
        (r'(AIza[0-9A-Za-z\-_]{35})', '***GOOGLE_API_KEY***'),
        (r'(SG\.[A-Za-z0-9\-_]{22}\.[A-Za-z0-9\-_]{43})', '***SENDGRID_KEY***'),
        (r'(?i)(TWILIO_AUTH_TOKEN\s*[=:]\s*[a-z0-9]{32})', '***TWILIO_AUTH***'),
        (r'(?i)(password\s*[=:]\s*[^\s\'"]{8,})', '***PASSWORD***'),
        (r'(?i)(secret\s*[=:]\s*[^\s\'"]{8,})', '***SECRET***'),
        (r'(?i)(api_key\s*[=:]\s*[^\s\'"]{8,})', '***API_KEY***'),
        (r'(?i)(token\s*[=:]\s*[A-Za-z0-9\-_\.]{20,})', '***TOKEN***'),
    ]
]


def _redact_secrets(line: str) -> str:
    if not isinstance(line, str):
        line = str(line)
    if not line:
        return line
    redacted = line
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_payload(payload: Any) -> Any:
    """Recursively redact secrets from a JSON-serialisable ``payload``."""
    if isinstance(payload, str):
        return _redact_secrets(payload)
    if isinstance(payload, dict):
        return {k: redact_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [redact_payload(v) for v in payload]
    if isinstance(payload, tuple):
        return tuple(redact_payload(v) for v in payload)
    return payload


class Worker:
    """Executes one step of the CEO's TeamPlan using the Claude Agent SDK."""

    def __init__(
        self,
        agent: Agent,
        settings: "Settings",
        storage: "Storage",
        security: "SecurityHooks",
        emit: Emit | None = None,
        workspace_dir: str | None = None,
    ) -> None:
        self.agent = agent
        self.settings = settings
        self.storage = storage
        self.security = security
        self.emit = emit
        self.workspace_dir = workspace_dir

        self._log = log.bind(
            role=agent.role,
            agent_id=agent.id,
            company_id=agent.company_id,
        )

    async def _safe_emit(self, event_type: str, payload: dict) -> None:
        if self.emit is None:
            return
        try:
            await self.emit(event_type, redact_payload(payload))
        except Exception as exc:  # pragma: no cover
            self._log.warning("worker.emit_failed", event_type=event_type, error=repr(exc))

    async def execute(
        self,
        step: "StepSpec",
        *,
        company_id: str,
        step_index: int,
        inbox: list[Any] | None = None,
        peer_roles: list[str] | None = None,
        prior_outputs: dict[int, str] | None = None,
        runtime_agents: dict | None = None,
    ) -> AgentReply:
        """Execute one step. Returns the :class:`AgentReply`."""
        inbox = inbox or []
        peer_roles = peer_roles or []
        prior_outputs = prior_outputs or {}

        # 1. Resume from checkpoint
        existing = await self._lookup_checkpoint(company_id, step_index)
        if existing is not None and existing.status == StepStatus.COMPLETED:
            self._log.info("worker.step_resumed", step_index=step_index, cost_usd=existing.cost_usd)
            await self._safe_emit("step.resumed", {
                "company_id": company_id,
                "step_index": step_index,
                "role": self.agent.role,
                "output_preview": (existing.output or "")[:200],
            })
            return AgentReply(
                text=existing.output or "",
                model=self.agent.model,
                cost_usd=existing.cost_usd,
                stop_reason="resumed",
            )

        # 2. Budget gate
        threshold = self.agent.budget_cap_usd * 0.99
        if self.agent.spent_usd >= threshold:
            self._log.warning("worker.budget_exhausted",
                              spent_usd=self.agent.spent_usd,
                              budget_cap_usd=self.agent.budget_cap_usd)
            raise BudgetExceeded(spent=self.agent.spent_usd, budget=self.agent.budget_cap_usd)

        # 3. Build prompt
        user_prompt = self._build_user_prompt(step, inbox, peer_roles, prior_outputs)

        allowed_tools = [
            t for t in (self.agent.allowed_tools or [])
            if t not in WORKER_DISALLOWED_TOOLS
        ]

        hooks = self._build_hooks()
        agents = merge_with_runtime_agents(runtime_agents)

        await self._safe_emit("step.started", {
            "company_id": company_id,
            "step_index": step_index,
            "role": self.agent.role,
            "instruction_preview": (step.instruction or "")[:200],
        })

        # 4. Retry loop
        tracer = get_tracer("autonomous_company.worker")
        max_retries = max(0, int(self.settings.step_max_retries))
        backoff_base = float(self.settings.step_retry_backoff_base)
        last_exc: BaseException | None = None

        for attempt in range(1, max_retries + 1):
            try:
                await self.storage.update_checkpoint_status(
                    company_id=company_id,
                    step_index=step_index,
                    status=StepStatus.RUNNING.value,
                    output=None,
                    error=None,
                    cost_usd=0.0,
                )
            except Exception as exc:  # pragma: no cover
                self._log.warning("worker.checkpoint_running_failed", error=repr(exc))

            with tracer.start_as_current_span("worker.execute") as span:
                try:
                    span.set_attribute("workflow.company_id", company_id)
                    span.set_attribute("workflow.step_index", step_index)
                    span.set_attribute("workflow.role_name", self.agent.role)
                    span.set_attribute("workflow.attempt", attempt)
                except Exception:  # pragma: no cover
                    pass

                t0 = time.monotonic()
                try:
                    reply = await ask(
                        system_prompt=self.agent.system_prompt,
                        user_prompt=user_prompt,
                        model=self.agent.model or self.settings.worker_model,
                        allowed_tools=allowed_tools,
                        max_turns=self.agent.max_turns,
                        output_schema=step.output_schema,
                        permission_mode=self.settings.worker_permission_mode,
                        cwd=self.workspace_dir,
                        on_message=self._on_sdk_message,
                        on_stderr=self._on_stderr,
                        env=None,
                        hooks=hooks,
                        skills=None,
                        agents=agents,
                        thinking=False,
                        effort=None,
                        settings=self.settings,
                    )
                except BudgetExceeded:
                    raise
                except asyncio.TimeoutError as exc:
                    last_exc = exc
                    self._log.warning("worker.step_timeout", step_index=step_index, attempt=attempt)
                except Exception as exc:
                    last_exc = exc
                    self._log.warning("worker.step_error", step_index=step_index,
                                      attempt=attempt, error=repr(exc))
                else:
                    duration_ms = (time.monotonic() - t0) * 1000.0
                    output_text = _redact_secrets(reply.text or "")
                    reply.text = output_text

                    try:
                        span.set_attribute("worker.cost_usd", reply.cost_usd)
                        span.set_attribute("worker.tokens_in", reply.tokens_in)
                        span.set_attribute("worker.tokens_out", reply.tokens_out)
                        span.set_attribute("worker.duration_ms", duration_ms)
                    except Exception:  # pragma: no cover
                        pass

                    await self._persist_completed(
                        company_id=company_id,
                        step_index=step_index,
                        output=output_text,
                        cost_usd=reply.cost_usd,
                    )

                    self._log.info("worker.step_completed", step_index=step_index,
                                   attempt=attempt, cost_usd=reply.cost_usd, duration_ms=duration_ms)
                    await self._safe_emit("step.completed", {
                        "company_id": company_id,
                        "step_index": step_index,
                        "role": self.agent.role,
                        "cost_usd": reply.cost_usd,
                        "tokens_in": reply.tokens_in,
                        "tokens_out": reply.tokens_out,
                        "output_preview": output_text[:200],
                    })
                    return reply

            if attempt < max_retries:
                delay = min(backoff_base ** attempt, 60.0)
                self._log.info("worker.step_retry", step_index=step_index, attempt=attempt,
                               delay=delay, error=repr(last_exc) if last_exc else None)
                await self._safe_emit("step.retrying", {
                    "company_id": company_id,
                    "step_index": step_index,
                    "role": self.agent.role,
                    "attempt": attempt,
                    "delay": delay,
                    "error": _redact_secrets(repr(last_exc)) if last_exc else "",
                })
                await asyncio.sleep(delay)
                continue

        # 5. Exhausted retries
        assert last_exc is not None
        error_text = _redact_secrets(str(last_exc))
        await self._persist_failed(company_id=company_id, step_index=step_index, error=error_text)
        self._log.error("worker.step_failed", step_index=step_index,
                        attempts=max_retries, error=repr(last_exc))
        await self._safe_emit("step.failed", {
            "company_id": company_id,
            "step_index": step_index,
            "role": self.agent.role,
            "attempts": max_retries,
            "error": error_text,
        })
        raise StepFailed(
            step_index=step_index,
            attempts=max_retries,
            cause=last_exc if isinstance(last_exc, Exception) else Exception(str(last_exc)),
        )

    def _build_user_prompt(
        self,
        step: "StepSpec",
        inbox: list[Any],
        peer_roles: list[str],
        prior_outputs: dict[int, str],
    ) -> str:
        sections: list[str] = []

        if peer_roles or inbox:
            a2a_lines: list[str] = ["--- A2A PROTOCOL ---"]
            if peer_roles:
                peers_str = ", ".join(sorted(peer_roles))
                a2a_lines.append(f"You can send messages to these peers: {peers_str}")
            else:
                a2a_lines.append("You have no peers to message.")
            a2a_lines.append("To send a message, include it in your WorkerReply.outbound list.")

            if inbox:
                a2a_lines.append("")
                a2a_lines.append(f"--- INBOX ({len(inbox)} messages) ---")
                for msg in inbox:
                    msg_id = self._field(msg, "id", default="?")
                    from_role = self._field(msg, "from_role", "from_agent_id", default="unknown")
                    kind = self._field(msg, "kind", default="legacy")
                    severity = self._field(msg, "severity", default="info")
                    content = self._field(msg, "content", default="")
                    a2a_lines.append(
                        f"[MSG-{msg_id}] FROM {from_role} | KIND: {kind} | SEVERITY: {severity}"
                    )
                    a2a_lines.append(str(content))
                    a2a_lines.append("---")
            sections.append("\n".join(a2a_lines))

        if self.workspace_dir:
            sections.append(
                "--- WORKSPACE ---\n"
                f"Your workspace: {self.workspace_dir}\n"
                "All file operations must use paths relative to this directory or absolute paths within it."
            )

        wanted_indices = list(step.include_prior_outputs or [])
        if wanted_indices:
            prior_lines: list[str] = ["--- PRIOR OUTPUTS ---"]
            for idx in wanted_indices:
                output = prior_outputs.get(idx)
                if output is None:
                    prior_lines.append(f"[Step {idx} output]:\n(missing — step did not produce output)")
                else:
                    prior_lines.append(f"[Step {idx} output]:\n{output}")
                prior_lines.append("---")
            sections.append("\n".join(prior_lines))

        sections.append("--- INSTRUCTION ---\n" + (step.instruction or ""))

        return "\n\n".join(sections)

    @staticmethod
    def _field(obj: Any, *names: str, default: Any = None) -> Any:
        if obj is None:
            return default
        for name in names:
            if isinstance(obj, dict):
                if name in obj:
                    return obj[name]
            else:
                value = getattr(obj, name, None)
                if value is not None:
                    return value
        return default

    def _build_hooks(self) -> dict:
        hooks: dict[str, list] = {"pre_tool_use": [], "post_tool_use": []}
        try:
            pre_hook = self.security.build_pre_tool_hook(role=self.agent.role)
            hooks["pre_tool_use"].append(pre_hook)
        except Exception as exc:  # pragma: no cover
            self._log.warning("worker.hook_build_failed", error=repr(exc))
        return hooks

    async def _on_sdk_message(self, msg: Any) -> None:
        try:
            msg_type = type(msg).__name__
            text_preview = ""
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    block_text = getattr(block, "text", None)
                    if isinstance(block_text, str):
                        parts.append(block_text)
                text_preview = "".join(parts)
            elif isinstance(content, str):
                text_preview = content
            else:
                text_attr = getattr(msg, "text", None)
                if isinstance(text_attr, str):
                    text_preview = text_attr

            text_preview = _redact_secrets(text_preview)[:500]
            await self._safe_emit("step.stream", {
                "role": self.agent.role,
                "msg_type": msg_type,
                "text": text_preview,
            })
        except Exception as exc:  # pragma: no cover
            self._log.debug("worker.on_sdk_message_error", error=repr(exc))

    async def _on_stderr(self, line: str) -> None:
        try:
            redacted = _redact_secrets(line)
            self._log.debug("worker.stderr", line=redacted)
            await self._safe_emit("step.debug", {
                "role": self.agent.role,
                "stderr": redacted,
            })
        except Exception as exc:  # pragma: no cover
            self._log.debug("worker.on_stderr_error", error=repr(exc))

    async def _lookup_checkpoint(self, company_id: str, step_index: int) -> StepCheckpoint | None:
        try:
            checkpoints = await self.storage.get_checkpoints(company_id)
        except Exception as exc:  # pragma: no cover
            self._log.warning("worker.checkpoint_lookup_failed", error=repr(exc))
            return None
        for cp in checkpoints:
            if cp.step_index == step_index:
                return cp
        return None

    async def _persist_completed(self, *, company_id: str, step_index: int,
                                  output: str, cost_usd: float) -> None:
        try:
            await self.storage.update_checkpoint_status(
                company_id=company_id,
                step_index=step_index,
                status=StepStatus.COMPLETED.value,
                output=output,
                error=None,
                cost_usd=cost_usd,
            )
        except Exception as exc:  # pragma: no cover
            self._log.warning("worker.checkpoint_completed_failed", error=repr(exc))

    async def _persist_failed(self, *, company_id: str, step_index: int, error: str) -> None:
        try:
            await self.storage.update_checkpoint_status(
                company_id=company_id,
                step_index=step_index,
                status=StepStatus.FAILED.value,
                output=None,
                error=error,
                cost_usd=0.0,
            )
        except Exception as exc:  # pragma: no cover
            self._log.warning("worker.checkpoint_failed_persist_failed", error=repr(exc))
