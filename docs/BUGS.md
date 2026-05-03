# BUGS — autonomous_company_v10

Tracking ledger for verified bugs. Severities: **C** = Critical (data loss / silent wrong output / security), **H** = High (stability / correctness under load), **S** = Security, **P** = Performance.

Phase A bugs are all **fixed** in commit `1a98bd3` on `claude-branch`. Phase B bugs are open.

---

## Phase A — Fixed in 1a98bd3

### C-1: Jinja2 `{# … #}` injection + unsanitized fallback prompt
- **File:** `src/autonomous_company/ceo.py`
- **Repro:** Submit a goal containing `{# … #}` Jinja2 comment markers; they render as literal text but bypass any prefix/suffix wrapping in the template. Worse: when the primary prompt path fails, the fallback path concatenated raw user input into the system prompt.
- **Fix:** Sanitize untrusted input before render (strip `{%`, `{{`, `{#` and their close tags). Fallback path now goes through the same sanitizer.
- **Test:** add a fuzz case to `tests/` covering each Jinja2 delimiter pair.

### C-2: `_total_spent` not re-hydrated on resume — silent budget overrun
- **File:** `src/autonomous_company/ceo.py`
- **Repro:** Start a mission, kill it after spending $X, resume. The cost meter starts at $0, so the $X is lost and the budget cap is silently bypassed.
- **Fix:** `_resume_mission` now re-reads cumulative cost from `events` (or a dedicated `mission_cost` column).

### C-3: `update_checkpoint_status` was UPDATE-only — resume was 100% broken
- **File:** `src/autonomous_company/storage.py`
- **Repro:** First-time write of a checkpoint goes through an UPDATE that affects 0 rows. On resume, no checkpoint is found, mission re-plans from scratch.
- **Fix:** Replace UPDATE with `INSERT … ON CONFLICT(step_id) DO UPDATE SET status=excluded.status, …` (UPSERT).

### C-4: `RUNNING` steps re-ran non-idempotently on resume
- **File:** `src/autonomous_company/storage.py` + `ceo.py`
- **Repro:** Step crashes mid-execution leaving status=`RUNNING`. On resume, the orchestrator launches it again without checking idempotency, double-billing and double-effecting.
- **Fix:** New `reset_running_checkpoints(mission_id)` called on mission start; flips `RUNNING` → `PENDING` so the retry-with-idempotency-key path fires.

### C-5: Event UUID `id` ignored by DB — event ordering broken
- **File:** `src/autonomous_company/storage.py` + `events.py`
- **Repro:** `save_event` accepted a UUID `id` but the DB schema used `INTEGER PRIMARY KEY AUTOINCREMENT`, so the UUID was discarded. Downstream consumers ordering by `id` got DB rowids, not the UUIDs they thought they had.
- **Fix:** `save_event` now returns the DB rowid; consumers updated. UUID is kept as `event_uuid` column for cross-system correlation.

### C-7: `MAX_PLAN_HITL_CHAIN` hardcoded — bypassed Settings
- **File:** `src/autonomous_company/ceo.py`
- **Repro:** Operators trying to tune the HITL chain depth via env var saw no effect.
- **Fix:** Read from `Settings.max_plan_hitl_chain`.

### C-8: `dag._find_cycle` reported wrong cycle path
- **File:** `src/autonomous_company/dag.py`
- **Repro:** Build a DAG with a cycle A→B→C→A and an irrelevant tail X→Y→C. `_find_cycle` returned the visit stack `[X, Y, C, A, B]` instead of the cycle `[A, B, C]`.
- **Fix:** Track parent pointers; on back-edge detection, walk parents from the back-edge target to the source to extract the actual cycle.

### `worker._agent` → `worker.agent` (5 sites)
- **File:** `src/autonomous_company/ceo.py`
- **Repro:** Every mission failed with `AttributeError: 'Worker' object has no attribute '_agent'` because the public attr is `agent`.
- **Fix:** Rename all 5 references.

### S-2: `&&` / `||` bypassed allowlist via `fnmatch`
- **File:** `src/autonomous_company/security_hooks.py`
- **Repro:** Allowlist had `ls *`. Attacker submits `ls ; rm -rf /`. `fnmatch("ls ; rm -rf /", "ls *")` returns True because `*` matches everything. Same for `ls && evil`, `ls || evil`, `ls | evil`, `ls $(evil)`, `` ls `evil` ``.
- **Fix:** Tokenize on `&&`, `||`, `;`, `|`, command-substitution and backticks; allowlist-check each subcommand independently. Reject if any sub fails.
- **Test:** `tests/test_security_allowlist.py` covers each metacharacter class.

### H-15: No plan hash — silent wrong output on resume
- **File:** `src/autonomous_company/storage.py` + `ceo.py`
- **Repro:** Edit the prompt template. Resume an in-flight mission. The new template is used for remaining steps; results are mixed-output but the mission appears to "complete normally".
- **Fix:** Hash the canonicalized plan (template + tool list + model id) on mission start; persist to `missions.plan_hash`; on resume, recompute and abort with `PlanHashMismatchError` if changed.

---

## Phase B — Open

### H-1: Storage timeouts
- **File:** `src/autonomous_company/storage.py`
- **Risk:** A wedged DB lock blocks the whole orchestrator forever; missions hang silently.
- **Fix:** `asyncio.wait_for(conn.execute(...), timeout=settings.db_timeout_seconds)` on every call. Default 10s. On timeout, raise `StorageTimeoutError` (new) and let the caller decide.

### H-2: Inbox truncation
- **File:** `src/autonomous_company/worker.py`
- **Risk:** A worker that never drains its inbox grows it unboundedly until SQLite OOMs.
- **Fix:** `settings.max_inbox_messages` (default 50). On overflow, drop oldest with WARN log. Optional: also enforce per-fetch limit.

### H-3: Fan-out semaphore
- **File:** `src/autonomous_company/ceo.py`
- **Risk:** A 1000-step parallel wave launches 1000 tasks, exhausting LLM rate limits and DB connections.
- **Fix:** `asyncio.Semaphore(settings.max_parallel_steps)` (default 20). Wrap each `_execute_step` launch.

### H-4: Wave cancellation on fatal
- **File:** `src/autonomous_company/ceo.py`
- **Risk:** One step dies; siblings keep running, burning budget on work that can't be used.
- **Fix:** Use `asyncio.TaskGroup` so any unhandled exception cancels siblings. Distinguish "fatal" (TaskGroup) from "retryable" (caught inside step).

### H-8: HITL cleanup on mission abort
- **File:** `src/autonomous_company/hitl.py` + `storage.py`
- **Risk:** Aborting a mission leaves `pending` gates that block future missions or confuse operators.
- **Fix:** On abort, `UPDATE hitl_gates SET status='cancelled' WHERE mission_id=? AND status='pending'`.

### P-1: DB indexes
- **File:** `src/autonomous_company/storage.py`
- **Risk:** Linear scans on `messages(recipient)` and TTL sweeps on `messages(expires_at)` get expensive at >10k messages.
- **Fix:** `CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient); CREATE INDEX IF NOT EXISTS idx_messages_expires ON messages(expires_at)` in `init_schema`.

---

## Bug intake template

When you find a new bug, append:

```
### <ID>: <one-line summary>
- **File:** path:line
- **Repro:** minimal steps that trigger the bug
- **Fix:** approach
- **Test:** location of regression test
```

ID convention: `C-<n>` critical, `H-<n>` high, `S-<n>` security, `P-<n>` perf, `M-<n>` minor. Increment from the highest existing.
