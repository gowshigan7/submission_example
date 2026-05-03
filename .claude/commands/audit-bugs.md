---
description: Re-run the bug audit against the current branch
---

Audit `autonomous_company_v10/` for the bug categories tracked in `docs/BUGS.md`. Do **not** fix anything in this run — produce a report only.

For each category below, scan and report `PASS` (already mitigated) or `FAIL: <file:line>` with one-line evidence:

1. **Jinja2 injection** — any `Template(...).render(unsanitized)` paths in `ceo.py` / `worker.py`.
2. **Resume idempotency** — any `UPDATE checkpoints` without companion `INSERT … ON CONFLICT`. `RUNNING` steps must be reset on startup.
3. **Settings bypass** — any hardcoded numeric literal in `ceo.py`/`worker.py`/`llm.py` that should read from `Settings` (budget, retries, timeouts, max-hops, TTLs).
4. **Cost re-hydration** — `_total_spent` (or equivalent) loaded from DB on resume.
5. **Shell allowlist** — `security_hooks.py` splits on `&&`/`||`/`;`/`|`/backticks before `fnmatch`.
6. **Plan hash** — `storage.py` stores `plan_hash` per mission; `ceo.py` validates on resume.
7. **Event ID** — `save_event` returns the DB rowid, not the input UUID.
8. **DAG cycle path** — `dag._find_cycle` returns the actual cycle, not the visit stack.
9. **Worker attribute** — no `worker._agent` references (should be `worker.agent`).
10. **DB timeouts** (Phase B target) — every `aiosqlite` call inside `asyncio.wait_for`.
11. **Fan-out cap** (Phase B target) — `asyncio.Semaphore` around step launches.

Output: a table with columns `ID | Status | Evidence`. End with the count of `FAIL`s.
