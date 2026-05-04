"""LLM gateway for autonomous_company_v10.

This module is the single seam between the platform and the Claude Agent
SDK. Every call into the LLM funnels through :func:`ask`, which:

* wraps the SDK's subprocess-based streaming agent in an asyncio-friendly
  coroutine,
* applies inactivity timeouts via :func:`asyncio.wait_for`,
* retries on transient failures with exponential backoff,
* parses structured output (with a JSON-tail fallback for the well-known
  Opus 4.7 unwrap bug),
* records cost / token / latency metrics through the shared
  :class:`~.telemetry.CostCollector`, and
* emits a single ``llm.ask`` OpenTelemetry span per attempt.

The actual SDK is imported lazily (and defensively) so that test
environments without ``claude-agent-sdk`` installed still import this
module cleanly. Errors are surfaced only when :func:`ask` is invoked.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TYPE_CHECKING

import structlog

from .telemetry import get_cost_collector, get_tracer

if TYPE_CHECKING:
    from .config import Settings


__all__ = [
    "AgentReply",
    "ask",
    "compute_effective_max_turns",
]


log = structlog.get_logger("autonomous_company.llm")


# ---------------------------------------------------------------------- #
# Public dataclass
# ---------------------------------------------------------------------- #


@dataclass
class AgentReply:
    """The normalised result of a single :func:`ask` invocation."""

    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: str = ""
    num_turns: int = 0
    structured_output: Any = None
    session_id: str = ""
    stop_reason: str = ""
    errors: list[str] = field(default_factory=list)
    effective_max_turns: int | None = None


# ---------------------------------------------------------------------- #
# Constants & helpers
# ---------------------------------------------------------------------- #


_MAX_BACKOFF_SEC: float = 30.0
_DEFAULT_INACTIVITY_TIMEOUT: float = 600.0
_DEFAULT_LLM_MAX_RETRIES: int = 2

# Floors for the orchestrator's max_turns so multi-tool workflows have
# enough room to converge before the SDK terminates the conversation.
_NATIVE_MAX_TURNS_FLOOR: int = 12
_CUSTOM_MAX_TURNS_FLOOR: int = 18


def compute_effective_max_turns(
    *,
    max_turns: int | None,
    output_schema: dict | None,
    env: dict,
) -> int | None:
    """Compute the effective ``max_turns`` for an LLM call.

    Rules
    -----
    * ``AUTONOMOUS_MAX_TURNS`` (in ``env``) wins if set to a positive int.
    * Otherwise, take the caller's ``max_turns`` and clamp it up to a
      floor of ``18`` when an ``output_schema`` is required (structured
      output tends to need extra tool turns), or ``12`` for plain text.
    * If the caller passed ``None`` and there is no env override, the
      floor itself is returned so we never hand the SDK a value below
      what real workloads need.
    """
    override = env.get("AUTONOMOUS_MAX_TURNS")
    if override is not None:
        try:
            value = int(override)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value

    floor = _CUSTOM_MAX_TURNS_FLOOR if output_schema else _NATIVE_MAX_TURNS_FLOOR

    if max_turns is None:
        return floor
    if max_turns < floor:
        return floor
    return max_turns


def _extract_last_json_object(text: str) -> dict | None:
    """Return the last balanced ``{...}`` JSON object embedded in ``text``.

    The Claude Agent SDK occasionally fails to populate
    ``ResultMessage.structured_output`` even when the model produced
    valid JSON. We walk the raw text right-to-left, tracking brace
    depth, and attempt :func:`json.loads` on each fully-balanced
    candidate. The first candidate that parses to a ``dict`` wins.
    """
    if not text:
        return None

    n = len(text)
    for end in range(n - 1, -1, -1):
        if text[end] != "}":
            continue
        depth = 0
        in_string = False
        escape = False
        start = -1
        for i in range(end, -1, -1):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start < 0:
            continue
        candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _unwrap_parameter_name(value: Any) -> Any:
    """Strip the Opus 4.7 ``{"$PARAMETER_NAME": {...}}`` wrapper.

    Some Opus 4.7 builds wrap a single-key envelope around structured
    output where the key is the *literal* string ``"$PARAMETER_NAME"``.
    We unwrap exactly one level so downstream consumers see the schema
    they actually defined.
    """
    if isinstance(value, dict) and len(value) == 1:
        only_key = next(iter(value))
        if only_key == "$PARAMETER_NAME":
            return value[only_key]
    return value


def _is_non_retryable(exc: BaseException) -> bool:
    """Return True if ``exc`` should bypass the retry loop entirely."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "budgetexceeded" in name or "budget exceeded" in msg:
        return True
    if "auth" in name and "error" in name:
        return True
    if "unauthorized" in msg or "forbidden" in msg or "invalid api key" in msg:
        return True
    return False


# ---------------------------------------------------------------------- #
# SDK loading
# ---------------------------------------------------------------------- #


def _load_sdk() -> tuple[Any, type[BaseException]]:
    """Import the Claude Agent SDK.

    Tries the modern ``claude_agent_sdk`` namespace first, then a few
    legacy locations. The result is the ``Agent`` (or compatible) class
    plus an exception class to use as a generic retryable error type.
    """
    last_exc: Exception | None = None
    for module_name, agent_attr in (
        ("claude_agent_sdk", "Agent"),
        ("claude_agent_sdk", "ClaudeAgent"),
        ("claude_agent", "Agent"),
        ("claude_code_sdk", "Agent"),
    ):
        try:
            mod = __import__(module_name, fromlist=[agent_attr])
            agent_cls = getattr(mod, agent_attr, None)
            if agent_cls is None:
                continue
            err_cls = getattr(mod, "ClaudeSDKError", Exception)
            return agent_cls, err_cls
        except ImportError as exc:  # pragma: no cover
            last_exc = exc
            continue
    raise ImportError(
        "Could not import the Claude Agent SDK. Install it with: "
        "`pip install claude-agent-sdk`. Last import error: "
        f"{last_exc!r}"
    )


# ---------------------------------------------------------------------- #
# Core SDK call
# ---------------------------------------------------------------------- #


async def _run_sdk_once(
    *,
    agent_cls: Any,
    system_prompt: str,
    user_prompt: str,
    model: str,
    allowed_tools: list[str],
    effective_max_turns: int | None,
    output_schema: dict | None,
    permission_mode: str,
    cwd: str | None,
    on_message: Callable[[Any], Awaitable[None]] | None,
    on_stderr: Callable[[str], Awaitable[None]] | None,
    env: dict[str, str] | None,
    hooks: dict | None,
    skills: list[str] | None,
    agents: dict | None,
    thinking: bool,
    effort: str | None,
) -> AgentReply:
    """Run a single streaming agent session and collect the result."""
    agent_kwargs: dict[str, Any] = {
        "model": model,
        "system_prompt": system_prompt,
        "allowed_tools": allowed_tools,
        "permission_mode": permission_mode,
    }
    if effective_max_turns is not None:
        agent_kwargs["max_turns"] = effective_max_turns
    if output_schema is not None:
        agent_kwargs["output_schema"] = output_schema
    if cwd is not None:
        agent_kwargs["cwd"] = cwd
    if env:
        agent_kwargs["env"] = env
    if hooks:
        agent_kwargs["hooks"] = hooks
    if skills:
        agent_kwargs["skills"] = skills
    if agents:
        agent_kwargs["agents"] = agents
    if thinking:
        agent_kwargs["thinking"] = True
    if effort is not None:
        agent_kwargs["effort"] = effort

    reply = AgentReply(text="", model=model, effective_max_turns=effective_max_turns)
    text_chunks: list[str] = []

    agent = agent_cls(**agent_kwargs)

    stream = None
    if hasattr(agent, "query"):
        stream = agent.query(user_prompt)
    elif hasattr(agent, "run"):
        stream = agent.run(user_prompt)
    elif hasattr(agent, "stream"):
        stream = agent.stream(user_prompt)
    else:
        stream = agent

    async def _drive() -> None:
        async for message in stream:  # type: ignore[union-attr]
            if on_message is not None:
                try:
                    await on_message(message)
                except Exception as cb_exc:  # pragma: no cover
                    log.warning("llm.on_message_error", error=repr(cb_exc))

            chunk_text = _extract_chunk_text(message)
            if chunk_text:
                text_chunks.append(chunk_text)

            if on_stderr is not None:
                err_text = _extract_stderr(message)
                if err_text:
                    try:
                        await on_stderr(err_text)
                    except Exception as cb_exc:  # pragma: no cover
                        log.warning("llm.on_stderr_error", error=repr(cb_exc))

            if _is_result_message(message):
                _populate_terminal(reply, message)

    await _drive()

    if not reply.text:
        reply.text = "".join(text_chunks)

    if output_schema is not None and reply.structured_output is None:
        fallback = _extract_last_json_object(reply.text)
        if fallback is not None:
            reply.structured_output = fallback

    if reply.structured_output is not None:
        reply.structured_output = _unwrap_parameter_name(reply.structured_output)

    return reply


def _extract_chunk_text(message: Any) -> str:
    """Best-effort extraction of textual content from an SDK chunk."""
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "".join(parts)
    if isinstance(content, str):
        return content
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return text
    return ""


def _extract_stderr(message: Any) -> str:
    """Pull stderr text from a stderr-flavoured SDK chunk if applicable."""
    msg_type = type(message).__name__.lower()
    if "stderr" not in msg_type:
        return ""
    text = getattr(message, "text", None) or getattr(message, "content", None)
    if isinstance(text, str):
        return text
    return ""


def _is_result_message(message: Any) -> bool:
    """Return True if ``message`` is the SDK's terminal result frame."""
    return type(message).__name__ in {"ResultMessage", "Result", "FinalMessage"}


def _populate_terminal(reply: AgentReply, message: Any) -> None:
    """Copy terminal metadata out of an SDK result message into ``reply``."""
    usage = getattr(message, "usage", None) or {}
    if isinstance(usage, dict):
        reply.tokens_in = int(
            usage.get("input_tokens")
            or usage.get("tokens_in")
            or 0
        )
        reply.tokens_out = int(
            usage.get("output_tokens")
            or usage.get("tokens_out")
            or 0
        )

    cost = getattr(message, "total_cost_usd", None)
    if cost is None:
        cost = getattr(message, "cost_usd", None)
    if cost is not None:
        try:
            reply.cost_usd = float(cost)
        except (TypeError, ValueError):
            pass

    num_turns = getattr(message, "num_turns", None)
    if isinstance(num_turns, int):
        reply.num_turns = num_turns

    session_id = getattr(message, "session_id", None)
    if isinstance(session_id, str):
        reply.session_id = session_id

    stop_reason = (
        getattr(message, "stop_reason", None)
        or getattr(message, "result", None)
        or ""
    )
    if isinstance(stop_reason, str):
        reply.stop_reason = stop_reason

    structured = getattr(message, "structured_output", None)
    if structured is None:
        structured = getattr(message, "output", None)
    if structured is not None:
        reply.structured_output = structured

    final_text = getattr(message, "result", None) or getattr(
        message, "final_text", None
    )
    if isinstance(final_text, str) and final_text and not reply.text:
        reply.text = final_text


# ---------------------------------------------------------------------- #
# Public ask()
# ---------------------------------------------------------------------- #


async def ask(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    allowed_tools: list[str],
    max_turns: int | None = None,
    output_schema: dict | None = None,
    permission_mode: str = "default",
    cwd: str | None = None,
    on_message: Callable[[Any], Awaitable[None]] | None = None,
    on_stderr: Callable[[str], Awaitable[None]] | None = None,
    env: dict[str, str] | None = None,
    hooks: dict | None = None,
    skills: list[str] | None = None,
    agents: dict | None = None,
    thinking: bool = False,
    effort: str | None = None,
    settings: "Settings | None" = None,
) -> AgentReply:
    """Issue a request to the Claude Agent SDK and return the parsed reply."""
    if settings is not None:
        max_retries = max(0, int(getattr(settings, "llm_max_retries", _DEFAULT_LLM_MAX_RETRIES)))
        inactivity_timeout = float(
            getattr(settings, "inactivity_timeout_sec", _DEFAULT_INACTIVITY_TIMEOUT)
        )
        debug = bool(getattr(settings, "llm_debug", False))
    else:
        max_retries = _DEFAULT_LLM_MAX_RETRIES
        inactivity_timeout = _DEFAULT_INACTIVITY_TIMEOUT
        debug = False

    effective_max_turns = compute_effective_max_turns(
        max_turns=max_turns,
        output_schema=output_schema,
        env=env or os.environ,
    )

    agent_cls, sdk_error_cls = _load_sdk()

    tracer = get_tracer("autonomous_company.llm")
    cost_collector = get_cost_collector()

    has_schema = output_schema is not None
    last_exc: BaseException | None = None
    reply: AgentReply | None = None

    for attempt in range(max_retries + 1):
        log.info(
            "llm.ask_start",
            model=model,
            has_schema=has_schema,
            max_turns=effective_max_turns,
            attempt=attempt,
        )
        t0 = time.monotonic()
        with tracer.start_as_current_span("llm.ask") as span:
            try:
                span.set_attribute("llm.model", model)
                span.set_attribute("llm.attempt", attempt)
                if effective_max_turns is not None:
                    span.set_attribute("llm.max_turns", effective_max_turns)
                span.set_attribute("llm.has_schema", has_schema)
            except Exception:  # pragma: no cover
                pass

            try:
                reply = await asyncio.wait_for(
                    _run_sdk_once(
                        agent_cls=agent_cls,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        model=model,
                        allowed_tools=allowed_tools,
                        effective_max_turns=effective_max_turns,
                        output_schema=output_schema,
                        permission_mode=permission_mode,
                        cwd=cwd,
                        on_message=on_message,
                        on_stderr=on_stderr,
                        env=env,
                        hooks=hooks,
                        skills=skills,
                        agents=agents,
                        thinking=thinking,
                        effort=effort,
                    ),
                    timeout=inactivity_timeout,
                )
                duration_ms = (time.monotonic() - t0) * 1000.0
                try:
                    cost_collector.record(
                        model=model,
                        tokens_in=reply.tokens_in,
                        tokens_out=reply.tokens_out,
                        cost_usd=reply.cost_usd,
                        latency_ms=duration_ms,
                    )
                except Exception:  # pragma: no cover
                    pass
                try:
                    span.set_attribute("llm.tokens_in", reply.tokens_in)
                    span.set_attribute("llm.tokens_out", reply.tokens_out)
                    span.set_attribute("llm.cost_usd", reply.cost_usd)
                    span.set_attribute("llm.duration_ms", duration_ms)
                    if reply.stop_reason:
                        span.set_attribute("llm.stop_reason", reply.stop_reason)
                except Exception:  # pragma: no cover
                    pass

                log.info(
                    "llm.ask_complete",
                    tokens_in=reply.tokens_in,
                    tokens_out=reply.tokens_out,
                    cost_usd=reply.cost_usd,
                    duration_ms=duration_ms,
                    stop_reason=reply.stop_reason,
                    attempt=attempt,
                )
                if debug:
                    log.debug(
                        "llm.ask_debug",
                        model=model,
                        text_len=len(reply.text or ""),
                        structured=reply.structured_output is not None,
                        session_id=reply.session_id,
                    )
                return reply

            except asyncio.TimeoutError as exc:
                last_exc = exc
                log.warning(
                    "llm.ask_timeout",
                    model=model,
                    attempt=attempt,
                    inactivity_timeout=inactivity_timeout,
                )
            except sdk_error_cls as exc:  # type: ignore[misc]
                last_exc = exc
                if _is_non_retryable(exc):
                    log.error("llm.ask_non_retryable", model=model, attempt=attempt, error=repr(exc))
                    raise
                log.warning("llm.ask_sdk_error", model=model, attempt=attempt, error=repr(exc))
            except (ConnectionError, OSError) as exc:
                last_exc = exc
                log.warning("llm.ask_connection_error", model=model, attempt=attempt, error=repr(exc))
            except Exception as exc:
                last_exc = exc
                if _is_non_retryable(exc):
                    log.error("llm.ask_non_retryable", model=model, attempt=attempt, error=repr(exc))
                    raise
                log.warning("llm.ask_unexpected_error", model=model, attempt=attempt, error=repr(exc))

        if attempt < max_retries:
            backoff = min(_MAX_BACKOFF_SEC, 2.0 ** attempt)
            await asyncio.sleep(backoff)

    assert last_exc is not None
    log.error(
        "llm.ask_exhausted",
        model=model,
        attempts=max_retries + 1,
        error=repr(last_exc),
    )
    raise last_exc
