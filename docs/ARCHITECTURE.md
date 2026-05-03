# Architecture — autonomous_company_v10

A multi-agent orchestrator on top of `claude-agent-sdk`. CEO plans, Workers execute, Storage persists.

## Component diagram

```
                   ┌────────────────────────────────────────┐
                   │             CLI (cli.py)               │
                   └───────────────────┬────────────────────┘
                                       │
                   ┌───────────────────▼────────────────────┐
                   │          Factory (factory.py)          │  wires everything
                   └───────┬───────────┬────────────┬───────┘
                           │           │            │
              ┌────────────▼──┐  ┌─────▼─────┐  ┌───▼───────┐
              │  CEO (ceo.py) │  │  Storage  │  │  LLM      │
              │  - plan       │◄─┤  (storage │  │  Gateway  │
              │  - DAG        │  │   .py)    │  │  (llm.py) │
              │  - waves      │  └─────▲─────┘  └───▲───────┘
              │  - HITL gate  │        │            │
              │  - resume     │        │            │
              └──────┬────────┘        │            │
                     │                 │            │
              ┌──────▼────────┐        │            │
              │ Worker        │────────┴────────────┘
              │ (worker.py)   │
              │  - claim step │
              │  - run tools  │
              │  - retry      │
              │  - persist CP │
              └──────┬────────┘
                     │
        ┌────────────┼────────────────┐
        │            │                │
   ┌────▼────┐  ┌────▼────┐    ┌──────▼─────────┐
   │ Events  │  │  Comm   │    │ Security Hooks │
   │ (events │  │ (comm   │    │ (security_     │
   │  .py)   │  │  .py)   │    │  hooks.py)     │
   └─────────┘  └─────────┘    └────────────────┘
```

## Lifecycle of a mission

1. **CLI** parses goal/budget/model → calls `factory.build_app(...)` → returns wired `CEO`.
2. **CEO.run(goal)** computes `plan_hash`, persists `Mission`, constructs DAG via `dag.from_steps`, launches the wave loop.
3. **Wave loop:** for each topo-layer:
   - Open `asyncio.TaskGroup` (Phase B target H-3/H-4).
   - For each step in layer: `Worker.execute(step)` under a `Semaphore`.
   - Worker runs the step's prompt through `llm.invoke`, applies allowlisted tools, retries on transient errors, writes a checkpoint.
   - On HITL trigger: pause, persist gate, wait for resume signal.
4. **CEO.resume(mission_id):** re-hydrate cost, validate plan_hash, reset stuck `RUNNING` checkpoints, continue from last completed wave.

## Data model

- `missions` — id, goal, plan_hash, status, total_spent_usd, created_at.
- `checkpoints` — step_id (PK), mission_id, status (PENDING/RUNNING/DONE/FAILED), result_json, attempt_count, idempotency_key.
- `events` — rowid (PK), event_uuid, mission_id, type, payload_json, ts.
- `messages` — rowid (PK), sender, recipient, body, expires_at, hop_count.
- `hitl_gates` — id, mission_id, step_id, status, prompt, response, created_at.

## Critical invariants

1. **Idempotency:** every `Worker.execute(step)` is safe to call N times with the same step_id; uses `idempotency_key`.
2. **Plan-hash purity:** `plan_hash` covers prompt template + tool list + model id. Any change → resume aborts.
3. **Cost monotonicity:** `total_spent_usd` only ever increases; persisted on every LLM call.
4. **Allowlist correctness:** every shell command goes through `security_hooks.check`; multi-command syntax is split before allowlist match.
5. **Checkpoint idempotency:** every status write is an UPSERT, never a bare UPDATE.

## Async model

- All I/O is `async`. Sync code is allowed in pure-CPU helpers (DAG, hashing, sanitization).
- The executor uses `anyio` primitives where they read cleaner than `asyncio`; no thread pools for storage.
- Long-running tools should yield via `await` inside their loop so the wave can be cancelled cleanly.

## Configuration

Single source: `pydantic-settings` in `config.py`. Env file: `.env` (template: `.env.example`). All knobs prefixed `AUTONOMOUS_`. Hardcoded literals are bugs (see C-7).

## Telemetry

- `structlog` for logs (console or JSON via `AUTONOMOUS_LOG_FORMAT`).
- OpenTelemetry traces optional (`AUTONOMOUS_OTEL_ENABLED`). Spans on LLM calls today; full coverage is Phase D.
- Cost attributes attached to LLM spans by `llm.CostCollector`.

## Boundaries

- **Trust boundary 1:** user goal → sanitizer (C-1) → Jinja2 template.
- **Trust boundary 2:** LLM tool-call → `security_hooks` allowlist → shell.
- **Trust boundary 3:** resume input (mission_id) → plan_hash check (H-15) → execution.

Anything that crosses a trust boundary must validate; anything inside trusts the sender.
