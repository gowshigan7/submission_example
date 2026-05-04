# Roadmap — autonomous_company_v10

Living document. Source of truth for what's done, what's next, and why.

| Phase | Status | Goal |
|---|---|---|
| **Phase A** — Critical bugs | ✅ Done (`1a98bd3`) | Stop the bleeding: 10 verified critical bugs |
| **Phase 0** — Repo org for Claude Code | 🟡 In progress | Make the repo navigable by Claude Code agents |
| **Phase B** — High-severity stability | ⏳ Next | Storage timeouts, fan-out cap, wave cancellation |
| **Phase C** — Performance | ⏳ Planned | Indexes, batching, hot-path profiling |
| **Phase D** — Observability | ⏳ Planned | OTEL spans on every async hop, cost dashboards |
| **Phase E** — SOTA alignment | ⏳ Planned | Match patterns from 27-framework comparison |

---

## Phase A — Critical bugs (DONE)

Single commit `1a98bd3` on top of `7718189`. Ten fixes, all with regression tests.

See `docs/BUGS.md` for the per-bug repro + fix diff. Summary:

- **C-1** Jinja2 `{# … #}` injection + unsanitized fallback prompt → input sanitization layer in `ceo.py`.
- **C-2** `_total_spent` not re-hydrated on resume → re-read from DB in `ceo._resume_mission`.
- **C-3** `update_checkpoint_status` was UPDATE-only → UPSERT in `storage.py` (resume was 100% broken).
- **C-4** `RUNNING` steps re-ran non-idempotently → `reset_running_checkpoints()` on mission start.
- **C-5** Event UUID `id` ignored by DB → `save_event` returns the DB rowid.
- **C-7** `MAX_PLAN_HITL_CHAIN` hardcoded → reads from `Settings`.
- **C-8** `dag._find_cycle` reported wrong cycle path → fixed via parent-pointer back-walk.
- **(unnumbered)** `worker._agent` → `worker.agent` at 5 sites in `ceo.py`.
- **S-2** `&&`/`||` bypassed allowlist via `fnmatch` → token-split in `security_hooks.py`.
- **H-15** No plan hash → silent wrong output on resume → plan hash stored + validated.

---

## Phase 0 — Repo organization for Claude Code

**Why:** Future sessions (this one and beyond) navigate the repo through CLAUDE.md, slash commands, and a clean `docs/` tree. Without it, every session re-derives context from scratch and burns tokens.

**Done in this phase:**

- [x] Root `CLAUDE.md` — navigation hub, build/test commands, conventions, "what NOT to do".
- [x] `autonomous_company_v10/CLAUDE.md` — subproject memory: module map, Phase A bug summary, local conventions, test checklist.
- [x] `.claude/settings.json` — permissions allowlist (pytest, ruff, uv, safe git), deny-list (force-push, hard-reset, .env reads), ask-list (push, commit, dependency changes).
- [x] `.claude/commands/run-tests.md` — `/run-tests` slash command (pytest + ruff in one shot).
- [x] `.claude/commands/phase-b.md` — `/phase-b` kickoff command with the ordered task list and per-fix protocol.
- [x] `.claude/commands/audit-bugs.md` — `/audit-bugs` re-runs the bug audit (read-only, produces a report).
- [x] `docs/ARCHITECTURE.md` — module map and data flow.
- [x] `docs/BUGS.md` — full C-1..H-15 ledger with repro + fix references.
- [x] `docs/ROADMAP.md` — this file.
- [x] `README.md` — replace empty file with project overview + quickstart.

**Still TODO:**

- [ ] `docs/SOTA_COMPARISON.md` — port the 27-framework comparison table (CrewAI, LangGraph, AutoGen, Letta, Inngest, Temporal, Restate, Hatchet, Trigger.dev, Prefect, Dagster, Airflow, Argo, Flyte, Step Functions, …) with axes: durable execution, HITL, cost tracking, OTEL, multi-tenancy, plan-hash equivalence.
- [ ] CI workflow `.github/workflows/test.yml` — run pytest + ruff on every push to `claude-branch` so Claude can read CI results via PR events.
- [ ] `docs/CONTRIBUTING.md` — short doc for human reviewers: branch model, commit format, what Claude is and isn't allowed to do here.
- [ ] Pre-commit hook (optional) — block direct edits to `master`.

**Acceptance criteria:** A fresh Claude Code session can answer "what should I work on next?" without reading any source file — only `CLAUDE.md` and `docs/`.

---

## Phase B — High-severity stability

Run via `/phase-b`. One commit per fix on `claude-branch`. All fixes need a regression test.

| ID | File | Fix |
|---|---|---|
| H-1 | `storage.py` | Wrap every `aiosqlite` call in `asyncio.wait_for(timeout=settings.db_timeout_seconds)`. Default `10.0`s. |
| H-2 | `worker.py` | Cap inbox at `settings.max_inbox_messages` (default 50). Drop oldest with WARN log. |
| H-3 | `ceo.py` | Wave executor gates concurrent step launches with `asyncio.Semaphore(settings.max_parallel_steps)` (default 20). |
| H-4 | `ceo.py` | Wave cancellation: on fatal exception, cancel sibling tasks in same wave (`asyncio.TaskGroup`). |
| H-8 | `hitl.py` + `storage.py` | On mission abort, mark all `pending` HITL gates `cancelled`. |
| P-1 | `storage.py` | `CREATE INDEX IF NOT EXISTS` for `messages(recipient)` and `messages(expires_at)`. |

**Acceptance criteria:** No silent budget overrun, no unbounded fan-out, no orphaned HITL gates, p99 mission resume <100ms.

---

## Phase C — Performance

After Phase B, profile the hot path (likely `ceo._execute_wave` + `storage.save_event`).

- [ ] Bench: 100-step DAG mission end-to-end timing baseline.
- [ ] Batch event writes (`executemany`) when ≥5 events queued.
- [ ] Connection pool for aiosqlite (currently re-opens per call?).
- [ ] Profile-guided index additions on `events`, `checkpoints`.
- [ ] `EXPLAIN QUERY PLAN` audit of every query in `storage.py`.

**Acceptance criteria:** 2× throughput on the 100-step bench vs. post-Phase-B baseline.

---

## Phase D — Observability

OTEL is wired but underused.

- [ ] Span around every `await` in `ceo.py` and `worker.py`.
- [ ] Cost attributes (`llm.tokens.input`, `llm.tokens.output`, `llm.cost_usd`) on every LLM span.
- [ ] Mission-level trace: parent span = mission, children = waves, grandchildren = steps.
- [ ] Structured-log → trace correlation via `trace_id` in `structlog` context.
- [ ] Optional Prometheus `/metrics` endpoint behind `settings.metrics_enabled`.

**Acceptance criteria:** A failed mission can be diagnosed end-to-end from a single trace ID without reading logs.

---

## Phase E — SOTA alignment

Cross-reference `docs/SOTA_COMPARISON.md` (Phase 0 deliverable) and pick the highest-leverage adoptions:

- [ ] **Durable execution** (Temporal/Restate/Inngest pattern): make every step's side-effect signature stable + replay-deterministic.
- [ ] **Compensating transactions** (saga pattern): each tool call defines its inverse.
- [ ] **Subscription-based HITL** (LangGraph interrupt-style): clients subscribe to gate events instead of polling.
- [ ] **Agent memory** (Letta/MemGPT pattern): long-term memory layer on top of `comm.py`.
- [ ] **Plan diffing** (Dagster asset-graph pattern): visualize what the plan-hash check actually changed between runs.

**Acceptance criteria:** Each adoption ships behind a feature flag with A/B benchmarks.

---

## Open questions

- Should Phase B ship as one PR or six? (Currently planned: six commits, one PR.)
- Do we want a CI gate on bug-audit (`/audit-bugs`) before merge?
- `claude-branch` vs `claude/fix-critical-bugs-*` naming — pick one and stick.
