from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

from .a2a import DEFAULT_TTL_SEC, MAX_HOP_COUNT, MessageKind, Severity
from .models import AuditEntry, Message
from .a2a import MessageStatus  # noqa: F401

if TYPE_CHECKING:
    from .config import Settings
    from .storage import Storage

__all__ = ["HUMAN_ID", "CEO_ID", "send_message", "read_inbox", "expire_ttl", "expire_ttl_with_audit", "audit"]

HUMAN_ID = "human"
CEO_ID = "ceo"

_KINDS_REQUIRING_TTL: frozenset[MessageKind] = frozenset({MessageKind.REQUEST, MessageKind.HITL_REQUEST, MessageKind.ESCALATION, MessageKind.BROADCAST})
_SEVERITY_RANK: dict[str, int] = {"blocker": 5, "grave": 4, "warning": 3, "notice": 2, "info": 1}

log = structlog.get_logger("autonomous_company.comm")


def _now() -> datetime:
    return datetime.now(timezone.utc)

def _new_id() -> str:
    return uuid.uuid4().hex

def _resolve_max_hop_count(settings: "Settings | None") -> int:
    if settings is None:
        return MAX_HOP_COUNT
    value = getattr(settings, "max_hop_count", None)
    if isinstance(value, int) and value > 0:
        return value
    return MAX_HOP_COUNT

def _resolve_default_ttl(settings: "Settings | None") -> int:
    if settings is None:
        return DEFAULT_TTL_SEC
    value = getattr(settings, "default_ttl_sec", None)
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_TTL_SEC

def _severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(severity, 0)


async def send_message(storage: "Storage", *, company_id: str, from_agent_id: str, to_agent_id: str, content: str, kind: MessageKind = MessageKind.LEGACY, severity: Severity = Severity.INFO, parent_id: str | None = None, ttl_seconds: int | None = None, hop_count: int = 0, settings: "Settings | None" = None) -> Message | None:
    max_hops = _resolve_max_hop_count(settings)
    if hop_count >= max_hops:
        log.warning("comm.message_hop_exceeded", company_id=company_id, hop_count=hop_count, max_hops=max_hops)
        return None
    now = _now()
    if ttl_seconds is None and kind in _KINDS_REQUIRING_TTL:
        ttl_seconds = _resolve_default_ttl(settings)
    expires_at: datetime | None = None
    if ttl_seconds is not None and ttl_seconds > 0:
        expires_at = now + timedelta(seconds=int(ttl_seconds))
    msg = Message(id=_new_id(), company_id=company_id, from_agent_id=from_agent_id, to_agent_id=to_agent_id, content=content, kind=str(kind), severity=str(severity), status=MessageStatus.PENDING.value, parent_id=parent_id, hop_count=hop_count, expires_at=expires_at, created_at=now)
    await storage.save_message(msg)
    log.info("comm.message_sent", company_id=company_id, message_id=msg.id, from_agent_id=from_agent_id, to_agent_id=to_agent_id)
    return msg


async def read_inbox(storage: "Storage", *, company_id: str, agent_id: str, mark_delivered: bool = True) -> list[Message]:
    await expire_ttl(storage, company_id=company_id)
    if mark_delivered:
        messages = await storage.claim_pending_inbox(company_id, agent_id)
    else:
        all_messages = await storage.get_messages(company_id, agent_id)
        messages = [m for m in all_messages if getattr(m, "status", None) in (MessageStatus.PENDING.value, MessageStatus.DELIVERED.value)]
    now = _now()
    fresh: list[Message] = []
    for m in messages:
        expires_at = getattr(m, "expires_at", None)
        if expires_at is not None and expires_at <= now:
            continue
        fresh.append(m)
    fresh.sort(key=lambda m: (-_severity_rank(getattr(m, "severity", "info")), getattr(m, "created_at", now)))
    return fresh


async def expire_ttl(storage: "Storage", *, company_id: str) -> int:
    expired = await _collect_and_expire(storage, company_id=company_id, agent_id=None)
    return len(expired)


async def expire_ttl_with_audit(storage: "Storage", *, company_id: str, agent_id: str) -> list[Message]:
    return await _collect_and_expire(storage, company_id=company_id, agent_id=agent_id)


async def _collect_and_expire(storage: "Storage", *, company_id: str, agent_id: str | None) -> list[Message]:
    now = _now()
    expired: list[Message] = []
    if agent_id is not None:
        candidates = await storage.get_messages(company_id, agent_id)
    else:
        agents = await storage.list_agents(company_id)
        seen_ids: set[str] = set()
        candidates = []
        for ag in agents:
            ag_id = getattr(ag, "id", None)
            if not ag_id:
                continue
            for msg in await storage.get_messages(company_id, ag_id):
                msg_id = getattr(msg, "id", None)
                if msg_id and msg_id in seen_ids:
                    continue
                if msg_id:
                    seen_ids.add(msg_id)
                candidates.append(msg)
    for msg in candidates:
        status = getattr(msg, "status", None)
        if status == MessageStatus.EXPIRED.value:
            continue
        if status in (MessageStatus.READ.value, MessageStatus.ANSWERED.value):
            continue
        expires_at = getattr(msg, "expires_at", None)
        if expires_at is None or expires_at > now:
            continue
        try:
            msg.status = MessageStatus.EXPIRED.value
        except Exception:
            pass
        await storage.save_message(msg)
        expired.append(msg)
    return expired


async def audit(storage: "Storage", *, company_id: str, action: str, agent_id: str | None = None, **details: object) -> AuditEntry:
    entry = AuditEntry(id=_new_id(), company_id=company_id, agent_id=agent_id, action=action, details=dict(details), created_at=_now())
    await storage.save_audit(entry)
    return entry
