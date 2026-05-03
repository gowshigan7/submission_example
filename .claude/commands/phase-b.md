---
description: Start Phase B (high-severity stability fixes — H-1..H-8, P-1)
---

You are continuing the bug-fix work on `autonomous_company_v10`. Phase A (10 critical bugs) is **already shipped** in commit `1a98bd3` on `claude-branch`. Do not re-fix C-1..C-8.

Your task: implement Phase B in this order, one fix per commit, on `claude-branch`:

1. **H-1** Storage timeouts — wrap every `aiosqlite` call in `storage.py` with `asyncio.wait_for(..., timeout=settings.db_timeout_seconds)`. Default 10s. Add `db_timeout_seconds: float = 10.0` to `Settings`.
2. **H-2** Inbox truncation — `worker.py` reads from inbox; cap at `settings.max_inbox_messages` (default 50). Drop oldest, log warn.
3. **H-3** Fan-out semaphore — in `ceo.py` wave executor, gate concurrent step launches with `asyncio.Semaphore(settings.max_parallel_steps)` (default 20).
4. **H-4** Wave cancellation — when one step raises a fatal exception, cancel sibling tasks in the same wave. Use `asyncio.TaskGroup` (3.12+) or fallback to gather with `return_exceptions=False` + manual cancel.
5. **H-8** HITL cleanup — on mission abort, mark all `pending` HITL gates `cancelled` in `hitl.py` + `storage.py`.
6. **P-1** DB indexes — add `CREATE INDEX IF NOT EXISTS` for `messages(recipient)` and `messages(expires_at)` in `storage.init_schema()`.

For each item:
- Read the relevant file first.
- Add a focused test in `tests/`.
- Run `/run-tests` (or `uv run pytest -q && uv run ruff check src tests`).
- Commit with format: `fix(phase-b): H-X — short summary`.

Do not batch fixes into one commit. Do not push until all 6 land locally.

Reference: `docs/ROADMAP.md` (Phase B section), `docs/BUGS.md` for repro details.
