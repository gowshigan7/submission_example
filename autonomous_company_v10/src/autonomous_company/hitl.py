from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from .models import HITLRequest

if TYPE_CHECKING:
    from .config import Settings
    from .storage import Storage

__all__ = ["HITL"]

log = structlog.get_logger("autonomous_company.hitl")

_FILE_DROP_POLL_INTERVAL_SEC: float = 2.0
_MAX_REQUEST_ID_LEN: int = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)

def _new_id() -> str:
    return uuid.uuid4().hex


class HITL:
    def __init__(self, settings: "Settings", storage: "Storage") -> None:
        self._settings = settings
        self._storage = storage
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def request(self, company_id: str, agent_id: str, prompt: str, hitl_dir: Path | None = None, timeout_sec: float = 300.0) -> str:
        request_id = _new_id()
        if len(request_id) > _MAX_REQUEST_ID_LEN:
            request_id = request_id[:_MAX_REQUEST_ID_LEN]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        record = HITLRequest(id=request_id, company_id=company_id, agent_id=agent_id, prompt=prompt, status="pending", response=None, created_at=_now(), resolved_at=None)
        async with self._lock:
            self._pending[request_id] = future
            await self._persist(record)
            if hitl_dir is not None:
                watcher = asyncio.create_task(self._watch_file_drop(request_id=request_id, hitl_dir=hitl_dir, company_id=company_id), name=f"hitl-watch-{request_id}")
                self._watchers[request_id] = watcher
        log.info("hitl.request_opened", company_id=company_id, agent_id=agent_id, request_id=request_id, timeout_sec=timeout_sec)
        try:
            response = await asyncio.wait_for(future, timeout=timeout_sec)
        except asyncio.TimeoutError:
            log.warning("hitl.request_timeout", company_id=company_id, request_id=request_id)
            await self._finalize(request_id=request_id, company_id=company_id, status="expired", response=None)
            raise
        except asyncio.CancelledError:
            log.warning("hitl.request_cancelled", company_id=company_id, request_id=request_id)
            await self._finalize(request_id=request_id, company_id=company_id, status="cancelled", response=None)
            raise
        else:
            log.info("hitl.request_resolved", company_id=company_id, request_id=request_id)
            return response
        finally:
            await self._cleanup_watcher(request_id)

    async def resolve(self, company_id: str, request_id: str, response: str) -> bool:
        async with self._lock:
            future = self._pending.get(request_id)
            if future is None or future.done():
                return False
            future.set_result(response)
            self._pending.pop(request_id, None)
        await self._update_persisted(request_id=request_id, company_id=company_id, status="resolved", response=response)
        log.info("hitl.resolved", company_id=company_id, request_id=request_id)
        return True

    resolve_from_rest = resolve

    async def cancel(self, company_id: str, request_id: str) -> None:
        async with self._lock:
            future = self._pending.pop(request_id, None)
            if future is not None and not future.done():
                future.cancel()
        await self._cleanup_watcher(request_id)
        await self._update_persisted(request_id=request_id, company_id=company_id, status="cancelled", response=None)

    async def get_pending(self, company_id: str) -> list[HITLRequest]:
        get_pending_for_company = getattr(self._storage, "get_pending_hitl_requests", None)
        if callable(get_pending_for_company):
            requests = await get_pending_for_company(company_id)
        else:
            list_all = getattr(self._storage, "list_hitl_requests", None)
            requests = await list_all(company_id) if callable(list_all) else []
        return [r for r in requests if getattr(r, "status", "pending") == "pending"]

    async def _watch_file_drop(self, request_id: str, hitl_dir: Path, company_id: str) -> None:
        if len(request_id) > _MAX_REQUEST_ID_LEN:
            return
        try:
            hitl_dir = hitl_dir.resolve()
        except Exception:
            return
        reply_path = hitl_dir / f"{request_id}.reply.md"
        while True:
            future = self._pending.get(request_id)
            if future is None or future.done():
                return
            try:
                await asyncio.sleep(_FILE_DROP_POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                raise
            try:
                future = self._pending.get(request_id)
                if future is None or future.done():
                    return
                if not reply_path.exists():
                    continue
                resolved_path = reply_path.resolve()
                try:
                    if not resolved_path.is_relative_to(hitl_dir):
                        return
                except AttributeError:
                    if not str(resolved_path).startswith(str(hitl_dir)):
                        return
                if not resolved_path.is_file():
                    continue
                try:
                    text = resolved_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                await self.resolve(company_id=company_id, request_id=request_id, response=text)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def _persist(self, record: HITLRequest) -> None:
        save = getattr(self._storage, "save_hitl_request", None) or getattr(self._storage, "save_hitl", None)
        if not callable(save):
            return
        try:
            await save(record)
        except Exception:
            pass

    async def _update_persisted(self, *, request_id: str, company_id: str, status: str, response: str | None) -> None:
        update = getattr(self._storage, "update_hitl_status", None) or getattr(self._storage, "update_hitl_request", None)
        if not callable(update):
            return
        try:
            await update(company_id=company_id, request_id=request_id, status=status, response=response, resolved_at=_now())
        except TypeError:
            try:
                await update(company_id, request_id, status, response)
            except Exception:
                pass
        except Exception:
            pass

    async def _finalize(self, *, request_id: str, company_id: str, status: str, response: str | None) -> None:
        async with self._lock:
            self._pending.pop(request_id, None)
        await self._cleanup_watcher(request_id)
        await self._update_persisted(request_id=request_id, company_id=company_id, status=status, response=response)

    async def _cleanup_watcher(self, request_id: str) -> None:
        async with self._lock:
            watcher = self._watchers.pop(request_id, None)
        if watcher is not None and not watcher.done():
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
