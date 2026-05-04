from __future__ import annotations
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
import structlog

from .models import Company, Agent
from .storage import Storage
from .security_hooks import SecurityHooks
from .hitl import HITL
from .events import EventBus, make_emit_fn
from .worker import Worker
from .ceo import CEO, async_create_company_and_ceo

if TYPE_CHECKING:
    from .config import Settings
    from .decisions import RoleSpec

log = structlog.get_logger("autonomous_company.factory")


async def build_mission_stack(
    *,
    mission: str,
    name: str | None = None,
    settings: "Settings",
    total_budget_usd: float | None = None,
    max_roles: int | None = None,
    workspace_dir: Path | None = None,
    company_id: str | None = None,
) -> tuple[Storage, EventBus, CEO]:
    budget = total_budget_usd or settings.default_budget_usd
    roles = max_roles or settings.max_roles
    cid = company_id or uuid.uuid4().hex
    workspace = workspace_dir or (settings.companies_root / cid)
    workspace.mkdir(parents=True, exist_ok=True)
    storage = Storage(settings)
    await storage.connect()
    event_bus = EventBus(storage=storage)
    emit = make_emit_fn(event_bus, cid)
    security = SecurityHooks(settings)
    hitl = HITL(settings, storage)
    company, ceo_agent = await async_create_company_and_ceo(
        storage, name=name or f"company_{cid[:8]}", mission=mission,
        total_budget_usd=budget, max_roles=roles, settings=settings,
    )

    def worker_factory(role_spec: "RoleSpec") -> Worker:
        agent = Agent(
            id=uuid.uuid4().hex,
            company_id=company.id,
            role=role_spec.role_name,
            system_prompt=role_spec.system_prompt,
            model=role_spec.model,
            budget_cap_usd=role_spec.budget_cap_usd,
            allowed_tools=role_spec.allowed_tools,
            max_turns=role_spec.max_turns,
            created_at=datetime.now(timezone.utc),
        )
        return Worker(agent=agent, settings=settings, storage=storage, security=security, emit=emit, workspace_dir=str(workspace))

    ceo = CEO(storage=storage, company=company, agent=ceo_agent, workspace=workspace, emit=emit, settings=settings, security=security, hitl=hitl, worker_factory=worker_factory)
    log.info("mission_stack.built", company_id=company.id, mission=mission[:80])
    return storage, event_bus, ceo


@asynccontextmanager
async def mission_context(*, mission: str, settings: "Settings", **kwargs) -> AsyncIterator[tuple[Storage, EventBus, CEO]]:
    storage, event_bus, ceo = await build_mission_stack(mission=mission, settings=settings, **kwargs)
    try:
        yield storage, event_bus, ceo
    finally:
        await storage.close()
