# CLAUDE.md — autonomous_company_v10

Subproject memory. Loaded after the root `CLAUDE.md`.

## Module map

```
src/autonomous_company/
  ceo.py            # CEO orchestrator: plan → DAG → wave-execute → checkpoint
  worker.py         # Worker loop: claim step, run tools, retry, persist
  llm.py            # LLM gateway: claude-agent-sdk + retry + OTEL + cost
  dag.py            # Topological DAG, cycle detection (path-correct since C-8)
  storage.py        # aiosqlite: missions, checkpoints, messages, events (UPSERT)
  events.py         # Event bus, rowid-returning save_event (since C-5)
  hitl.py           # Human-in-the-loop approval gates
  security_hooks.py # Shell command allowlist (token-split since S-2)
  config.py         # pydantic-settings; everything is configurable
  models.py         # Pydantic models (Mission, Step, Checkpoint, Event, …)
  interfaces.py     # Protocol classes for storage/llm/eventbus
  decisions.py      # Decision logging
  comm.py           # Inter-agent messaging
  condition.py      # Step preconditions / when-clauses
  factory.py        # Wiring (dependency injection)
  cli.py            # Typer CLI entrypoint
  a2a.py            # Agent-to-agent stub
  native_agents.py  # Built-in agents
  telemetry.py      # OTEL setup
  __init__.py
prompts/
  ceo.md.jinja2     # CEO system prompt — INPUT IS SANITIZED before render (C-1)
config/
  tools_allowlist.yaml
tests/
  test_config.py test_dag.py test_security_allowlist.py test_telemetry.py
```

## Phase A — what was fixed

10 verified bugs on top of `7718189`, all in commit `1a98bd3`:

| ID | Severity | File | Summary |
|---|---|---|---|
| C-1 | Critical | `ceo.py` | `{# … #}` Jinja2 injection + unsanitized fallback prompt |
| C-2 | Critical | `ceo.py` | `_total_spent` not re-hydrated on resume → silent budget overrun |
| C-3 | Critical | `storage.py` | `update_checkpoint_status` was UPDATE-only → UPSERT (resume was 100% broken) |
| C-4 | Critical | `storage.py` + `ceo.py` | `RUNNING` steps re-ran non-idempotently → `reset_running_checkpoints()` |
| C-5 | Critical | `storage.py` + `events.py` | Event UUID id ignored by DB → `save_event` returns rowid |
| C-7 | Critical | `ceo.py` | `MAX_PLAN_HITL_CHAIN` hardcoded, bypassed `Settings` |
| C-8 | Critical | `dag.py` | `_find_cycle` reported wrong cycle path |
| —  | Critical | `ceo.py` (×5 sites) | `worker._agent` → `worker.agent` AttributeError on every mission |
| S-2 | Sec | `security_hooks.py` | `&&`/`||` bypassed allow-list via `fnmatch` — token-split now |
| H-15 | High | `storage.py` + `ceo.py` | No plan hash → silent wrong output on resume — added plan hash validation |

See `../docs/BUGS.md` for context, repro, and fix diff per bug.

## Phase B — what's next

In priority order (see `../docs/ROADMAP.md` for detail):

- **H-1** Storage timeouts — wrap all DB calls with `asyncio.wait_for(timeout=10s)`
- **H-2** Inbox truncation — cap at `max_inbox_messages=50` in `worker.py`
- **H-3** Fan-out semaphore — `asyncio.Semaphore(max_parallel_steps=20)` in `ceo.py`
- **H-4** Wave cancellation — cancel siblings when one step raises fatal exception
- **H-8** HITL cleanup on mission abort
- **P-1** DB indexes on `messages` and `messages(expires_at)`

## Local conventions

- **Settings is law.** Anything that looks like a magic number (`3`, `5.0`, `3600`) belongs in `Settings`. Audit before introducing new ones.
- **DB calls are async.** Use `aiosqlite` connections via `storage.get_conn()`. Never open raw `sqlite3.connect`.
- **Resume must be idempotent.** Every code path that writes to `checkpoints` must (a) UPSERT, (b) be safe to re-run with same step_id, (c) honor plan_hash.
- **Cost tracking.** Any LLM call goes through `llm.py`'s `CostCollector`. Direct `anthropic` SDK use is forbidden.

## Testing checklist before claiming done

```bash
uv run pytest -q                              # all green
uv run ruff check src tests                   # no new warnings
uv run autonomous-company --help              # CLI still imports
```

If you touched `storage.py`: also run `uv run pytest tests/test_dag.py -q` and add an integration test for the resume path.

If you touched `security_hooks.py`: extend `tests/test_security_allowlist.py` with the new attack vector you defended against.
