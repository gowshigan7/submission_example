from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

import aiosqlite

from .config import Settings
from .models import Agent, AuditEntry, Company, Event, HITLRequest, Message, StepCheckpoint, StepStatus, Task  # noqa: F401


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS companies (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, mission TEXT NOT NULL,
        total_budget_usd REAL NOT NULL, spent_usd REAL NOT NULL DEFAULT 0.0,
        status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS agents (
        id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
        role TEXT NOT NULL, system_prompt TEXT NOT NULL, model TEXT NOT NULL,
        budget_cap_usd REAL NOT NULL DEFAULT 0.50, spent_usd REAL NOT NULL DEFAULT 0.0,
        manager_id TEXT, allowed_tools TEXT NOT NULL DEFAULT '[]', max_turns INTEGER,
        status TEXT NOT NULL DEFAULT 'idle', created_at TEXT NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, company_id TEXT NOT NULL, from_agent_id TEXT NOT NULL,
        to_agent_id TEXT NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'legacy',
        severity TEXT NOT NULL DEFAULT 'info', status TEXT NOT NULL DEFAULT 'pending',
        parent_id TEXT, hop_count INTEGER NOT NULL DEFAULT 0, expires_at TEXT, created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_messages_inbox ON messages(company_id, to_agent_id, status)",
    """
    CREATE TABLE IF NOT EXISTS audit (
        id TEXT PRIMARY KEY, company_id TEXT NOT NULL, agent_id TEXT,
        action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_audit_company ON audit(company_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS step_checkpoints (
        company_id TEXT NOT NULL, step_index INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', output TEXT, attempt INTEGER NOT NULL DEFAULT 0,
        error TEXT, cost_usd REAL NOT NULL DEFAULT 0.0, started_at TEXT, completed_at TEXT,
        PRIMARY KEY (company_id, step_index)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_company ON step_checkpoints(company_id)",
    """
    CREATE TABLE IF NOT EXISTS hitl_requests (
        id TEXT PRIMARY KEY, company_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        prompt TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        response TEXT, created_at TEXT NOT NULL, resolved_at TEXT
    )""",
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL,
        type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_company ON events(company_id, created_at)",
)

_SEVERITY_SORT_KEY: dict[str, int] = {"grave": 0, "blocker": 0, "warning": 1, "notice": 2, "info": 3}


class Storage:
    def __init__(self, settings: Settings) -> None:
        self._path = settings.sqlite_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._migrate()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Storage":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Storage is not connected. Call await storage.connect() first.")
        return self._conn

    @staticmethod
    def _dt_to_iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    async def _migrate(self) -> None:
        conn = self._require_conn()
        async with conn.cursor() as cur:
            await cur.execute("BEGIN")
            try:
                for stmt in _SCHEMA_STATEMENTS:
                    await cur.execute(stmt)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        # Add plan_hash column to existing databases that pre-date this migration.
        # ALTER TABLE fails silently if the column already exists.
        try:
            await conn.execute("ALTER TABLE companies ADD COLUMN plan_hash TEXT")
            await conn.commit()
        except Exception:
            pass

    async def save_company(self, company: Company) -> Company:
        conn = self._require_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO companies (id, name, mission, total_budget_usd, spent_usd, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (company.id, company.name, company.mission, company.total_budget_usd, company.spent_usd, company.status, self._dt_to_iso(company.created_at), self._dt_to_iso(company.updated_at)),
        )
        await conn.commit()
        return company

    async def get_company(self, company_id: str) -> Company | None:
        conn = self._require_conn()
        async with conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Company(id=row["id"], name=row["name"], mission=row["mission"], total_budget_usd=row["total_budget_usd"], spent_usd=row["spent_usd"], status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"])

    async def update_company_status(self, company_id: str, status: str) -> None:
        conn = self._require_conn()
        await conn.execute("UPDATE companies SET status = ?, updated_at = ? WHERE id = ?", (status, self._now(), company_id))
        await conn.commit()

    async def update_company_spent(self, company_id: str, spent_usd: float) -> None:
        conn = self._require_conn()
        await conn.execute("UPDATE companies SET spent_usd = ?, updated_at = ? WHERE id = ?", (spent_usd, self._now(), company_id))
        await conn.commit()

    async def get_plan_hash(self, company_id: str) -> str | None:
        conn = self._require_conn()
        async with conn.execute("SELECT plan_hash FROM companies WHERE id = ?", (company_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return row["plan_hash"]

    async def set_plan_hash(self, company_id: str, plan_hash: str) -> None:
        conn = self._require_conn()
        await conn.execute("UPDATE companies SET plan_hash = ? WHERE id = ?", (plan_hash, company_id))
        await conn.commit()

    async def save_agent(self, agent: Agent) -> Agent:
        conn = self._require_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO agents (id, company_id, role, system_prompt, model, budget_cap_usd, spent_usd, manager_id, allowed_tools, max_turns, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent.id, agent.company_id, agent.role, agent.system_prompt, agent.model, agent.budget_cap_usd, agent.spent_usd, agent.manager_id, json.dumps(agent.allowed_tools), agent.max_turns, agent.status, self._dt_to_iso(agent.created_at)),
        )
        await conn.commit()
        return agent

    async def get_agent(self, agent_id: str) -> Agent | None:
        conn = self._require_conn()
        async with conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_agent(row)

    async def list_agents(self, company_id: str) -> list[Agent]:
        conn = self._require_conn()
        async with conn.execute("SELECT * FROM agents WHERE company_id = ? ORDER BY created_at", (company_id,)) as cur:
            rows = await cur.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def update_agent_spent(self, agent_id: str, spent_usd: float) -> None:
        conn = self._require_conn()
        await conn.execute("UPDATE agents SET spent_usd = ? WHERE id = ?", (spent_usd, agent_id))
        await conn.commit()

    @staticmethod
    def _row_to_agent(row: aiosqlite.Row) -> Agent:
        return Agent(id=row["id"], company_id=row["company_id"], role=row["role"], system_prompt=row["system_prompt"], model=row["model"], budget_cap_usd=row["budget_cap_usd"], spent_usd=row["spent_usd"], manager_id=row["manager_id"], allowed_tools=json.loads(row["allowed_tools"] or "[]"), max_turns=row["max_turns"], status=row["status"], created_at=row["created_at"])

    async def save_message(self, message: Message) -> Message:
        conn = self._require_conn()
        await conn.execute(
            "INSERT INTO messages (id, company_id, from_agent_id, to_agent_id, content, kind, severity, status, parent_id, hop_count, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.id, message.company_id, message.from_agent_id, message.to_agent_id, message.content, message.kind, message.severity, message.status, message.parent_id, message.hop_count, self._dt_to_iso(message.expires_at), self._dt_to_iso(message.created_at)),
        )
        await conn.commit()
        return message

    async def get_messages(self, company_id: str, agent_id: str) -> list[Message]:
        conn = self._require_conn()
        async with conn.execute("SELECT * FROM messages WHERE company_id = ? AND to_agent_id = ? AND status != 'expired' ORDER BY created_at ASC", (company_id, agent_id)) as cur:
            rows = await cur.fetchall()
        return [self._row_to_message(r) for r in rows]

    async def claim_pending_inbox(self, company_id: str, agent_id: str) -> list[Message]:
        conn = self._require_conn()
        async with conn.cursor() as cur:
            await cur.execute("BEGIN IMMEDIATE")
            try:
                await cur.execute("SELECT * FROM messages WHERE company_id = ? AND to_agent_id = ? AND status = 'pending' ORDER BY created_at ASC", (company_id, agent_id))
                rows = await cur.fetchall()
                if rows:
                    ids = [r["id"] for r in rows]
                    placeholders = ",".join("?" for _ in ids)
                    await cur.execute(f"UPDATE messages SET status = 'delivered' WHERE id IN ({placeholders})", ids)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        messages = [self._row_to_message(r) for r in rows]
        for m in messages:
            m.status = "delivered"
        messages.sort(key=lambda m: (_SEVERITY_SORT_KEY.get(m.severity, 4), m.created_at))
        return messages

    async def expire_ttl_messages(self, company_id: str) -> int:
        conn = self._require_conn()
        now_iso = self._now()
        async with conn.execute("UPDATE messages SET status = 'expired' WHERE company_id = ? AND status IN ('pending', 'delivered') AND expires_at IS NOT NULL AND expires_at < ?", (company_id, now_iso)) as cur:
            count = cur.rowcount
        await conn.commit()
        return count if count is not None and count >= 0 else 0

    async def update_message_status(self, message_id: str, status: str) -> None:
        conn = self._require_conn()
        await conn.execute("UPDATE messages SET status = ? WHERE id = ?", (status, message_id))
        await conn.commit()

    @staticmethod
    def _row_to_message(row: aiosqlite.Row) -> Message:
        return Message(id=row["id"], company_id=row["company_id"], from_agent_id=row["from_agent_id"], to_agent_id=row["to_agent_id"], content=row["content"], kind=row["kind"], severity=row["severity"], status=row["status"], parent_id=row["parent_id"], hop_count=row["hop_count"], expires_at=row["expires_at"], created_at=row["created_at"])

    async def save_audit(self, entry: AuditEntry) -> AuditEntry:
        conn = self._require_conn()
        await conn.execute("INSERT INTO audit (id, company_id, agent_id, action, details, created_at) VALUES (?, ?, ?, ?, ?, ?)", (entry.id, entry.company_id, entry.agent_id, entry.action, json.dumps(entry.details), self._dt_to_iso(entry.created_at)))
        await conn.commit()
        return entry

    async def get_audit_log(self, company_id: str, limit: int = 100) -> list[AuditEntry]:
        conn = self._require_conn()
        async with conn.execute("SELECT * FROM audit WHERE company_id = ? ORDER BY created_at DESC LIMIT ?", (company_id, limit)) as cur:
            rows = await cur.fetchall()
        return [AuditEntry(id=r["id"], company_id=r["company_id"], agent_id=r["agent_id"], action=r["action"], details=json.loads(r["details"] or "{}"), created_at=r["created_at"]) for r in rows]

    async def save_checkpoint(self, cp: StepCheckpoint) -> None:
        conn = self._require_conn()
        status = cp.status.value if isinstance(cp.status, StepStatus) else str(cp.status)
        await conn.execute(
            "INSERT OR REPLACE INTO step_checkpoints (company_id, step_index, status, output, attempt, error, cost_usd, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cp.company_id, cp.step_index, status, cp.output, cp.attempt, cp.error, cp.cost_usd, self._dt_to_iso(cp.started_at), self._dt_to_iso(cp.completed_at)),
        )
        await conn.commit()

    async def get_checkpoints(self, company_id: str) -> list[StepCheckpoint]:
        conn = self._require_conn()
        async with conn.execute("SELECT * FROM step_checkpoints WHERE company_id = ? ORDER BY step_index ASC", (company_id,)) as cur:
            rows = await cur.fetchall()
        return [StepCheckpoint(company_id=r["company_id"], step_index=r["step_index"], status=r["status"], output=r["output"], attempt=r["attempt"], error=r["error"], cost_usd=r["cost_usd"], started_at=r["started_at"], completed_at=r["completed_at"]) for r in rows]

    async def update_checkpoint_status(
        self,
        company_id: str,
        step_index: int,
        status: str,
        output: str | None = None,
        error: str | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        """UPSERT a checkpoint row. Safe to call before save_checkpoint (fix C-3)."""
        conn = self._require_conn()
        now = self._now()
        terminal = {StepStatus.COMPLETED.value, StepStatus.FAILED.value, StepStatus.SKIPPED.value}

        if status == StepStatus.RUNNING.value:
            # On insert: record started_at and set attempt=1.
            # On conflict: preserve existing started_at, increment attempt.
            await conn.execute(
                """
                INSERT INTO step_checkpoints
                    (company_id, step_index, status, output, attempt, error, cost_usd, started_at, completed_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, NULL)
                ON CONFLICT(company_id, step_index) DO UPDATE SET
                    status = excluded.status,
                    output = excluded.output,
                    error = excluded.error,
                    cost_usd = excluded.cost_usd,
                    started_at = COALESCE(step_checkpoints.started_at, excluded.started_at),
                    attempt = step_checkpoints.attempt + 1
                """,
                (company_id, step_index, status, output, error, cost_usd, now),
            )
        elif status in terminal:
            await conn.execute(
                """
                INSERT INTO step_checkpoints
                    (company_id, step_index, status, output, attempt, error, cost_usd, started_at, completed_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, NULL, ?)
                ON CONFLICT(company_id, step_index) DO UPDATE SET
                    status = excluded.status,
                    output = excluded.output,
                    error = excluded.error,
                    cost_usd = excluded.cost_usd,
                    completed_at = excluded.completed_at
                """,
                (company_id, step_index, status, output, error, cost_usd, now),
            )
        else:
            await conn.execute(
                """
                INSERT INTO step_checkpoints
                    (company_id, step_index, status, output, attempt, error, cost_usd, started_at, completed_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, NULL, NULL)
                ON CONFLICT(company_id, step_index) DO UPDATE SET
                    status = excluded.status,
                    output = excluded.output,
                    error = excluded.error,
                    cost_usd = excluded.cost_usd
                """,
                (company_id, step_index, status, output, error, cost_usd),
            )
        await conn.commit()

    async def reset_running_checkpoints(self, company_id: str) -> None:
        """Reset RUNNING steps to PENDING so they re-execute cleanly on resume (fix C-4)."""
        conn = self._require_conn()
        await conn.execute(
            "UPDATE step_checkpoints SET status = 'pending' WHERE company_id = ? AND status = 'running'",
            (company_id,),
        )
        await conn.commit()

    async def delete_checkpoints(self, company_id: str) -> None:
        conn = self._require_conn()
        await conn.execute("DELETE FROM step_checkpoints WHERE company_id = ?", (company_id,))
        await conn.commit()

    async def save_hitl_request(self, req: HITLRequest) -> HITLRequest:
        conn = self._require_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO hitl_requests (id, company_id, agent_id, prompt, status, response, created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (req.id, req.company_id, req.agent_id, req.prompt, req.status, req.response, self._dt_to_iso(req.created_at), self._dt_to_iso(req.resolved_at)),
        )
        await conn.commit()
        return req

    async def get_hitl_request(self, request_id: str) -> HITLRequest | None:
        conn = self._require_conn()
        async with conn.execute("SELECT * FROM hitl_requests WHERE id = ?", (request_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_hitl(row)

    async def resolve_hitl_request(self, request_id: str, response: str) -> None:
        conn = self._require_conn()
        await conn.execute("UPDATE hitl_requests SET status = 'resolved', response = ?, resolved_at = ? WHERE id = ?", (response, self._now(), request_id))
        await conn.commit()

    async def get_pending_hitl(self, company_id: str) -> list[HITLRequest]:
        conn = self._require_conn()
        async with conn.execute("SELECT * FROM hitl_requests WHERE company_id = ? AND status = 'pending' ORDER BY created_at ASC", (company_id,)) as cur:
            rows = await cur.fetchall()
        return [self._row_to_hitl(r) for r in rows]

    @staticmethod
    def _row_to_hitl(row: aiosqlite.Row) -> HITLRequest:
        return HITLRequest(id=row["id"], company_id=row["company_id"], agent_id=row["agent_id"], prompt=row["prompt"], status=row["status"], response=row["response"], created_at=row["created_at"], resolved_at=row["resolved_at"])

    async def save_event(self, event: Event) -> str:
        """Persist an event and return its DB-assigned id (fix C-5)."""
        conn = self._require_conn()
        cur = await conn.execute(
            "INSERT INTO events (company_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
            (event.company_id, event.type, json.dumps(event.payload), self._dt_to_iso(event.created_at)),
        )
        rowid = cur.lastrowid
        await conn.commit()
        return str(rowid) if rowid is not None else event.id

    async def get_events(self, company_id: str, since: datetime | None = None) -> list[Event]:
        conn = self._require_conn()
        if since is None:
            query = "SELECT * FROM events WHERE company_id = ? ORDER BY created_at ASC, id ASC"
            params: tuple[Any, ...] = (company_id,)
        else:
            query = "SELECT * FROM events WHERE company_id = ? AND created_at > ? ORDER BY created_at ASC, id ASC"
            params = (company_id, self._dt_to_iso(since))
        async with conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [Event(id=str(r["id"]), company_id=r["company_id"], type=r["type"], payload=json.loads(r["payload"] or "{}"), created_at=r["created_at"]) for r in rows]
