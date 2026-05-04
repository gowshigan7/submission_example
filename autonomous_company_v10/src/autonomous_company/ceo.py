from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable, TYPE_CHECKING
import structlog
from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from .decisions import TeamPlan, RoleSpec, StepSpec
from .dag import StepGraph, StepNode, CycleError
from .models import Company, Agent, StepCheckpoint, StepStatus, BudgetExceeded, StepFailed
from .llm import ask, AgentReply
from .comm import send_message, read_inbox, audit, HUMAN_ID, CEO_ID
from .a2a import WorkerReply, CEOReaction, CEODecision, Severity, MessageKind
from .telemetry import get_tracer, get_cost_collector

if TYPE_CHECKING:
    from .config import Settings
    from .storage import Storage
    from .security_hooks import SecurityHooks
    from .hitl import HITL
    from .worker import Worker

Emit = Callable[[str, dict], Awaitable[None]]

log = structlog.get_logger("autonomous_company.ceo")

DEFAULT_CEO_MODEL = "claude-opus-4-7"
DEFAULT_CEO_BUDGET_USD = 2.00
DRAIN_REACTION_MAX_TURNS = 20
MAX_PLAN_HITL_CHAIN = 3
MAX_HITL_CHAIN = 3
SNAPSHOT_MAX_ENTRIES = 500


def _new_id() -> str:
    return uuid.uuid4().hex

def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_company_and_ceo(
    *,
    name: str,
    mission: str,
    total_budget_usd: float = 5.00,
    max_roles: int = 10,
    settings: "Settings | None" = None,
) -> tuple[Company, Agent]:
    """
    Create Company + CEO Agent objects in memory (not yet persisted).
    Caller must persist via storage.save_company() + storage.save_agent().
    """
    model = settings.ceo_model if settings else DEFAULT_CEO_MODEL
    ceo_budget = settings.ceo_budget_usd if settings else DEFAULT_CEO_BUDGET_USD

    now = _now()
    company = Company(
        id=_new_id(),
        name=name,
        mission=mission,
        total_budget_usd=total_budget_usd,
        spent_usd=0.0,
        status="pending",
        created_at=now,
        updated_at=now,
    )

    ceo_agent = Agent(
        id=_new_id(),
        company_id=company.id,
        role="ceo",
        system_prompt="You are the CEO of an autonomous company.",
        model=model,
        budget_cap_usd=ceo_budget,
        spent_usd=0.0,
        allowed_tools=[],
        max_turns=None,
        status="idle",
        created_at=now,
    )

    return company, ceo_agent


async def async_create_company_and_ceo(
    storage: "Storage",
    *,
    name: str,
    mission: str,
    total_budget_usd: float = 5.00,
    max_roles: int = 10,
    settings: "Settings | None" = None,
) -> tuple[Company, Agent]:
    """Async version — saves to DB immediately."""
    company, ceo_agent = create_company_and_ceo(
        name=name,
        mission=mission,
        total_budget_usd=total_budget_usd,
        max_roles=max_roles,
        settings=settings,
    )
    await storage.save_company(company)
    await storage.save_agent(ceo_agent)
    return company, ceo_agent


class CEO:
    """
    The CEO orchestrates multi-agent missions end-to-end.

    Main flow:
      plan() -> TeamPlan  (Claude Opus produces structured plan from Jinja2 prompt)
      orchestrate(plan)   (DAG-based parallel execution with resume support)
    """

    def __init__(
        self,
        storage: "Storage",
        company: Company,
        agent: Agent,
        *,
        workspace: str | Path,
        emit: Emit | None = None,
        settings: "Settings | None" = None,
        security: "SecurityHooks | None" = None,
        hitl: "HITL | None" = None,
        worker_factory: "Callable[[RoleSpec], Worker] | None" = None,
    ) -> None:
        self._storage = storage
        self._company = company
        self._agent = agent
        self._workspace = Path(workspace)
        self._emit = emit or self._noop_emit
        self._settings = settings
        self._security = security
        self._hitl = hitl
        self._worker_factory = worker_factory
        self._total_spent: float = 0.0
        self._step_outputs: dict[int, str] = {}
        self._completed_steps: set[int] = set()
        self._jinja_env = self._build_jinja_env()
        self._snapshot_cache: list[str] | None = None
        self._snapshot_mtime: float = 0.0

    @staticmethod
    async def _noop_emit(event_type: str, payload: dict) -> None:
        pass

    def _build_jinja_env(self) -> Environment:
        prompts_dir = (
            self._settings.prompts_dir if self._settings
            else Path(__file__).parent.parent.parent / "prompts"
        )
        return Environment(
            loader=FileSystemLoader(str(prompts_dir)),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _sanitize_mission(self, text: str) -> str:
        """Escape Jinja2 control syntax in user-provided mission text."""
        return (text
                .replace("{{{", "{ { {")  # must escape triple first
                .replace("{{", "{ {")
                .replace("}}", "} }")
                .replace("{%", "{ %")
                .replace("%}", "% }")
                .replace("{#", "{ #")
                .replace("#}", "# }"))

    def _render_ceo_prompt(
        self,
        *,
        budget_usd: float,
        max_roles: int,
        custom_notes: str = "",
    ) -> str:
        try:
            template = self._jinja_env.get_template("ceo.md.jinja2")
            return template.render(
                mission=self._sanitize_mission(self._company.mission),
                workspace_path=str(self._workspace),
                budget_usd=budget_usd,
                max_roles=max_roles,
                custom_notes=custom_notes,
            )
        except TemplateNotFound:
            log.warning("ceo.prompt_template_missing", fallback=True)
            return self._minimal_fallback_prompt(budget_usd=budget_usd, max_roles=max_roles)

    def _minimal_fallback_prompt(self, *, budget_usd: float, max_roles: int) -> str:
        return (
            f"You are the CEO of an autonomous company. Your mission is:\n\n"
            f"{self._sanitize_mission(self._company.mission)}\n\n"
            f"Workspace: {self._workspace}\n"
            f"Budget: ${budget_usd:.2f}\n"
            f"Max roles: {max_roles}\n\n"
            "Produce a TeamPlan JSON with roles and steps to complete this mission. "
            "Use deps[] to express step dependencies for parallel execution."
        )

    def _check_budget(self) -> None:
        if self._total_spent >= self._company.total_budget_usd:
            raise BudgetExceeded(self._total_spent, self._company.total_budget_usd)

    async def _charge(self, cost_usd: float) -> None:
        self._total_spent += cost_usd
        self._agent.spent_usd += cost_usd
        if self._total_spent >= self._company.total_budget_usd:
            await self._emit("budget.exceeded", {
                "spent": self._total_spent,
                "budget": self._company.total_budget_usd,
            })
            raise BudgetExceeded(self._total_spent, self._company.total_budget_usd)
        await self._storage.update_agent_spent(self._agent.id, self._agent.spent_usd)

    async def plan(self, _clarifications: str | None = None) -> TeamPlan:
        """
        Call Claude Opus to produce a structured TeamPlan.

        1. Render Jinja2 CEO prompt
        2. Build user prompt (mission + optional clarifications)
        3. Call ask() with output_schema=TeamPlan.model_json_schema()
        4. Parse as TeamPlan (validates DAG automatically)
        5. HITL loop if plan.ask_human is set (max max_plan_hitl_chain rounds)
        6. Emit plan.created event
        """
        tracer = get_tracer()
        model = self._settings.ceo_model if self._settings else DEFAULT_CEO_MODEL
        max_roles = self._settings.max_roles if self._settings else 10
        remaining_budget = self._company.total_budget_usd - self._total_spent

        system_prompt = self._render_ceo_prompt(
            budget_usd=remaining_budget,
            max_roles=max_roles,
        )

        user_prompt = self._company.mission
        if _clarifications:
            user_prompt += f"\n\nAdditional context:\n{_clarifications}"

        with tracer.start_as_current_span("ceo.plan") as span:
            span.set_attribute("workflow.company_id", self._company.id)
            span.set_attribute("llm.model", model)

            plan: TeamPlan | None = None
            hitl_rounds = 0
            max_hitl_rounds = (
                self._settings.max_plan_hitl_chain if self._settings else MAX_PLAN_HITL_CHAIN
            )

            while True:
                log.info("ceo.planning", company_id=self._company.id, hitl_round=hitl_rounds)

                reply = await ask(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    model=model,
                    allowed_tools=[],
                    max_turns=1,
                    output_schema=TeamPlan.model_json_schema(),
                    permission_mode="default",
                    cwd=str(self._workspace),
                    settings=self._settings,
                )

                await self._charge(reply.cost_usd)

                raw = reply.structured_output or {}
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except json.JSONDecodeError:
                        raw = {}

                try:
                    plan = TeamPlan.model_validate(raw)
                except Exception as exc:
                    log.error("ceo.plan_parse_failed", error=str(exc), raw=str(raw)[:500])
                    raise ValueError(f"CEO produced invalid TeamPlan: {exc}") from exc

                if plan.ask_human and hitl_rounds < max_hitl_rounds:
                    log.info("ceo.plan_hitl", prompt=plan.ask_human[:100])
                    human_response = await self._ask_human(plan.ask_human)
                    user_prompt += f"\n\nHuman response: {human_response}"
                    hitl_rounds += 1
                    plan = None
                    continue

                break

        await self._emit("plan.created", {
            "roles": len(plan.roles),
            "steps": len(plan.steps),
            "final_output_step": plan.final_output_step,
        })

        log.info("ceo.plan_created", roles=len(plan.roles), steps=len(plan.steps),
                 company_id=self._company.id)

        return plan

    async def orchestrate(self, plan: TeamPlan) -> str:
        """
        Execute the TeamPlan using DAG-based parallel orchestration.

        Supports resuming: completed steps (from prior run) are skipped.
        Uses asyncio.gather for parallel wave execution.
        """
        tracer = get_tracer()

        with tracer.start_as_current_span("ceo.orchestrate") as span:
            span.set_attribute("workflow.company_id", self._company.id)
            span.set_attribute("workflow.steps", len(plan.steps))
            span.set_attribute("workflow.roles", len(plan.roles))

            self._check_budget()

            checkpoints = await self._storage.get_checkpoints(self._company.id)
            self._completed_steps = {
                cp.step_index for cp in checkpoints
                if cp.status == StepStatus.COMPLETED
            }
            for cp in checkpoints:
                if cp.status == StepStatus.COMPLETED and cp.output:
                    self._step_outputs[cp.step_index] = cp.output

            # Re-hydrate total_spent from persisted checkpoints so budget
            # enforcement is correct when resuming after a crash (fix C-2).
            self._total_spent = sum(
                cp.cost_usd for cp in checkpoints
                if cp.status == StepStatus.COMPLETED
            )

            if self._completed_steps:
                log.info("ceo.orchestrate_resuming",
                         completed=len(self._completed_steps),
                         company_id=self._company.id)
                # Reset any RUNNING steps to PENDING so they re-execute
                # cleanly on resume instead of being skipped or double-run (fix C-4).
                await self._storage.reset_running_checkpoints(self._company.id)

            # Detect plan changes between runs so the caller knows that
            # resuming from stale checkpoints may produce wrong outputs (fix H-15).
            plan_hash = hashlib.sha256(plan.model_dump_json().encode()).hexdigest()
            stored_hash = await self._storage.get_plan_hash(self._company.id)
            if stored_hash is None:
                await self._storage.set_plan_hash(self._company.id, plan_hash)
            elif stored_hash != plan_hash:
                log.warning(
                    "ceo.plan_hash_mismatch",
                    company_id=self._company.id,
                    stored=stored_hash[:16],
                    current=plan_hash[:16],
                    advice="Delete checkpoints to start fresh if outputs look wrong.",
                )

            graph = StepGraph(plan.steps)

            workers: dict[str, "Worker"] = {}
            for role_spec in plan.roles:
                worker = self._create_worker(role_spec)
                workers[role_spec.role_name] = worker
                await self._storage.save_agent(worker.agent)

            await self._emit("orchestration.started", {
                "company_id": self._company.id,
                "total_steps": len(plan.steps),
                "waves": len(graph.parallel_groups()),
            })

            await self._storage.update_company_status(self._company.id, "running")

            groups = graph.parallel_groups()
            for wave_idx, wave in enumerate(groups):
                pending = [node for node in wave if node.index not in self._completed_steps]

                if not pending:
                    log.info("ceo.wave_skipped", wave=wave_idx, reason="all_completed")
                    continue

                await self._emit("wave.started", {"wave": wave_idx, "steps": len(pending)})
                log.info("ceo.wave_started", wave=wave_idx, steps=len(pending))

                results = await asyncio.gather(
                    *[self._execute_step(node, plan, workers) for node in pending],
                    return_exceptions=True,
                )

                for node, result in zip(pending, results):
                    if isinstance(result, BudgetExceeded):
                        await self._storage.update_company_status(self._company.id, "failed")
                        await self._emit("mission.failed", {"reason": "budget_exceeded"})
                        raise result
                    if isinstance(result, Exception) and not isinstance(result, StepFailed):
                        log.error("ceo.step_unexpected_error", step=node.index, error=str(result))

                await self._emit("wave.completed", {"wave": wave_idx})

                await self._drain_a2a_backlog(workers, plan)
                await self._react_to_escalations(plan)

            final_idx = plan.final_output_step
            final_output = self._step_outputs.get(final_idx, "")

            await self._storage.update_company_status(self._company.id, "completed")
            await self._emit("mission.completed", {
                "company_id": self._company.id,
                "total_cost_usd": self._total_spent,
                "output_preview": final_output[:200],
            })

            log.info("ceo.orchestration_complete",
                     company_id=self._company.id,
                     total_cost=self._total_spent,
                     steps_completed=len(self._completed_steps) + len(plan.steps))

            return final_output

    def _create_worker(self, role_spec: RoleSpec) -> "Worker":
        from .worker import Worker
        from .models import Agent

        agent = Agent(
            id=_new_id(),
            company_id=self._company.id,
            role=role_spec.role_name,
            system_prompt=role_spec.system_prompt,
            model=role_spec.model,
            budget_cap_usd=role_spec.budget_cap_usd,
            spent_usd=0.0,
            manager_id=self._agent.id,
            allowed_tools=role_spec.allowed_tools,
            max_turns=role_spec.max_turns,
            status="idle",
            created_at=_now(),
        )

        if self._worker_factory:
            return self._worker_factory(role_spec)

        return Worker(
            agent=agent,
            settings=self._settings,
            storage=self._storage,
            security=self._security,
            emit=self._emit,
            workspace_dir=str(self._workspace),
        )

    async def _execute_step(
        self,
        node: StepNode,
        plan: TeamPlan,
        workers: dict[str, "Worker"],
    ) -> None:
        from .condition import evaluate_condition, ConditionError

        step = node.spec
        tracer = get_tracer()

        with tracer.start_as_current_span("ceo._execute_step") as span:
            span.set_attribute("workflow.step_index", node.index)
            span.set_attribute("workflow.role_name", step.to_role)
            span.set_attribute("workflow.company_id", self._company.id)

            if step.condition:
                try:
                    context = {
                        f"step_{i}_done": i in self._completed_steps
                        for i in range(len(plan.steps))
                    }
                    context.update(self._step_outputs)
                    should_run = evaluate_condition(step.condition, context)
                except ConditionError as e:
                    log.warning("ceo.condition_error", step=node.index, error=str(e))
                    should_run = True

                if not should_run:
                    log.info("ceo.step_skipped", step=node.index, condition=step.condition)
                    await self._emit("step.skipped", {
                        "step_index": node.index,
                        "condition": step.condition,
                    })
                    return

            prior_outputs: dict[int, str] = {
                idx: self._step_outputs[idx]
                for idx in step.include_prior_outputs
                if idx in self._step_outputs
            }

            worker = workers.get(step.to_role)
            if not worker:
                raise StepFailed(
                    node.index, 0,
                    ValueError(f"No worker found for role '{step.to_role}'")
                )

            inbox = await read_inbox(
                self._storage,
                company_id=self._company.id,
                agent_id=worker.agent.id,
            )

            peer_roles = [r for r in workers.keys() if r != step.to_role]

            reply = await worker.execute(
                step,
                company_id=self._company.id,
                step_index=node.index,
                inbox=inbox,
                peer_roles=peer_roles,
                prior_outputs=prior_outputs,
            )

            self._step_outputs[node.index] = reply.text
            self._completed_steps.add(node.index)

            await self._charge(reply.cost_usd)

            if reply.structured_output:
                try:
                    worker_reply = WorkerReply.model_validate(reply.structured_output)
                    if worker_reply.outbound:
                        await self._dispatch_outbound(
                            worker_reply.outbound, workers,
                            from_agent_id=worker.agent.id,
                            company_id=self._company.id,
                        )
                    if worker_reply.escalate_to_ceo:
                        await send_message(
                            self._storage,
                            company_id=self._company.id,
                            from_agent_id=worker.agent.id,
                            to_agent_id=self._agent.id,
                            content=worker_reply.escalate_to_ceo,
                            kind=MessageKind.ESCALATION,
                            severity=Severity.GRAVE,
                        )
                    if worker_reply.ask_human:
                        await self._ask_human(worker_reply.ask_human)
                except Exception as e:
                    log.warning("ceo.worker_reply_parse_error", error=str(e), step=node.index)

    async def _dispatch_outbound(
        self,
        outbound_messages: list[Any],
        workers: dict[str, "Worker"],
        from_agent_id: str,
        company_id: str,
    ) -> None:
        for msg in outbound_messages:
            if msg.to == "ceo":
                to_id = self._agent.id
            elif msg.to == "broadcast":
                for worker in workers.values():
                    await send_message(
                        self._storage,
                        company_id=company_id,
                        from_agent_id=from_agent_id,
                        to_agent_id=worker.agent.id,
                        content=msg.content,
                        kind=msg.kind,
                        severity=msg.severity,
                        parent_id=msg.parent_id,
                    )
                continue
            else:
                target = workers.get(msg.to)
                if not target:
                    log.warning("ceo.outbound_unknown_target", target=msg.to)
                    continue
                to_id = target.agent.id

            await send_message(
                self._storage,
                company_id=company_id,
                from_agent_id=from_agent_id,
                to_agent_id=to_id,
                content=msg.content,
                kind=msg.kind,
                severity=msg.severity,
                parent_id=msg.parent_id,
            )

    async def _drain_a2a_backlog(
        self,
        workers: dict[str, "Worker"],
        plan: TeamPlan,
    ) -> None:
        max_turns = (
            self._settings.drain_reaction_max_turns
            if self._settings else DRAIN_REACTION_MAX_TURNS
        )

        for iteration in range(max_turns):
            any_processed = False

            for role_name, worker in workers.items():
                inbox = await read_inbox(
                    self._storage,
                    company_id=self._company.id,
                    agent_id=worker.agent.id,
                    mark_delivered=True,
                )

                if not inbox:
                    continue

                any_processed = True
                log.info("ceo.drain_backlog", role=role_name,
                         messages=len(inbox), iteration=iteration)

                last_step_idx = max(
                    (i for i, s in enumerate(plan.steps) if s.to_role == role_name
                     and i in self._completed_steps),
                    default=None,
                )
                if last_step_idx is None:
                    continue

                node = StepGraph(plan.steps).nodes_by_index().get(last_step_idx)
                if node:
                    await self._execute_step(node, plan, workers)

            if not any_processed:
                break

    async def _react_to_escalations(self, plan: TeamPlan) -> None:
        inbox = await read_inbox(
            self._storage,
            company_id=self._company.id,
            agent_id=self._agent.id,
            mark_delivered=True,
        )

        escalations = [
            m for m in inbox
            if m.severity in (Severity.GRAVE, Severity.BLOCKER)
        ]

        if not escalations:
            return

        log.info("ceo.escalations_received", count=len(escalations))

        escalation_text = "\n\n".join(
            f"[{m.severity.upper()} from {m.from_agent_id}]: {m.content}"
            for m in escalations
        )

        model = self._settings.ceo_model if self._settings else DEFAULT_CEO_MODEL

        reaction_reply = await ask(
            system_prompt=(
                "You are the CEO. A worker has escalated an issue. "
                "Decide how to proceed: continue, abort, modify_next, or ask_human. "
                "Return a CEOReaction JSON."
            ),
            user_prompt=(
                f"Escalations:\n{escalation_text}\n\n"
                f"Mission: {self._company.mission}\n"
                f"Budget remaining: ${self._company.total_budget_usd - self._total_spent:.4f}"
            ),
            model=model,
            allowed_tools=[],
            max_turns=1,
            output_schema=CEOReaction.model_json_schema(),
            settings=self._settings,
        )

        await self._charge(reaction_reply.cost_usd)

        raw = reaction_reply.structured_output or {}
        try:
            reaction = CEOReaction.model_validate(raw)
        except Exception as e:
            log.error("ceo.reaction_parse_failed", error=str(e))
            return

        log.info("ceo.reaction", decision=reaction.decision, rationale=reaction.rationale[:100])
        await self._emit("ceo.reaction", {
            "decision": reaction.decision,
            "rationale": reaction.rationale,
        })

        if reaction.decision == CEODecision.ABORT:
            await self._storage.update_company_status(self._company.id, "aborted")
            raise RuntimeError(f"CEO aborted mission: {reaction.rationale}")

        elif reaction.decision == CEODecision.ASK_HUMAN and reaction.hitl_prompt:
            human_response = await self._ask_human(reaction.hitl_prompt)
            log.info("ceo.hitl_response_received", length=len(human_response))

    async def _ask_human(self, prompt: str) -> str:
        await self._emit("hitl.request", {
            "company_id": self._company.id,
            "prompt": prompt,
        })

        if self._hitl:
            try:
                response = await self._hitl.request(
                    company_id=self._company.id,
                    agent_id=self._agent.id,
                    prompt=prompt,
                )
                return response
            except asyncio.TimeoutError:
                log.warning("ceo.hitl_timeout", prompt=prompt[:100])
                return ""
        else:
            log.warning("ceo.hitl_no_bus", prompt=prompt[:100])
            return ""

    def _workspace_snapshot(self) -> list[str]:
        """List workspace files for context (max SNAPSHOT_MAX_ENTRIES, no symlinks)."""
        try:
            current_mtime = self._workspace.stat().st_mtime
        except OSError:
            return []

        if self._snapshot_cache is not None and current_mtime == self._snapshot_mtime:
            return self._snapshot_cache

        files: list[str] = []
        max_entries = (
            self._settings.snapshot_max_entries
            if self._settings else SNAPSHOT_MAX_ENTRIES
        )

        try:
            for path in sorted(self._workspace.rglob("*")):
                if len(files) >= max_entries:
                    break
                if path.is_symlink():
                    continue
                if path.is_file():
                    try:
                        rel = path.relative_to(self._workspace)
                        files.append(str(rel))
                    except ValueError:
                        pass
        except OSError:
            pass

        self._snapshot_cache = files
        self._snapshot_mtime = current_mtime
        return files

    @property
    def total_spent(self) -> float:
        return self._total_spent

    @property
    def step_outputs(self) -> dict[int, str]:
        return dict(self._step_outputs)
