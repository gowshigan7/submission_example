from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from .storage import Storage

log = structlog.get_logger("autonomous_company.events")


@dataclass
class Event:
    type: str
    company_id: str
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EventBus:
    def __init__(self, storage: "Storage | None" = None) -> None:
        self._storage = storage
        self._subscribers: dict[str, list[asyncio.Queue[Event | None]]] = {}
        self._lock = asyncio.Lock()

    async def emit(self, event_type: str, *, company_id: str, payload: dict | None = None) -> None:
        event = Event(type=event_type, company_id=company_id, payload=payload or {})
        if self._storage:
            try:
                from .models import Event as EventModel
                db_event = EventModel(id=event.id, company_id=event.company_id, type=event.type, payload=event.payload, created_at=event.created_at)
                db_id = await self._storage.save_event(db_event)
                # Update event.id to the DB-assigned integer id so that live
                # stream events and replayed events share the same id (fix C-5).
                event.id = db_id
            except Exception as exc:
                log.warning("events.persist_failed", error=str(exc))
        async with self._lock:
            queues = list(self._subscribers.get(company_id, []))
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("events.queue_full", company_id=company_id)

    async def subscribe(self, company_id: str, max_queue_size: int = 500) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=max_queue_size)
        async with self._lock:
            self._subscribers.setdefault(company_id, []).append(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            async with self._lock:
                subs = self._subscribers.get(company_id, [])
                if queue in subs:
                    subs.remove(queue)

    async def complete(self, company_id: str) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(company_id, []))
        for queue in queues:
            await queue.put(None)

    async def replay(self, company_id: str, since: datetime | None = None) -> list[Event]:
        if not self._storage:
            return []
        try:
            db_events = await self._storage.get_events(company_id, since=since)
            return [Event(id=e.id, type=e.type, company_id=e.company_id, payload=e.payload, created_at=e.created_at) for e in db_events]
        except Exception as exc:
            log.warning("events.replay_failed", error=str(exc))
            return []

    async def unsubscribe_all(self, company_id: str) -> None:
        async with self._lock:
            self._subscribers.pop(company_id, None)


def make_emit_fn(bus: EventBus, company_id: str) -> Callable[[str, dict], Awaitable[None]]:
    async def _emit(event_type: str, payload: dict) -> None:
        await bus.emit(event_type, company_id=company_id, payload=payload)
    return _emit


__all__ = ["Event", "EventBus", "make_emit_fn"]
