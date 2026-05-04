# CLAUDE.md — Repo Memory

This file is auto-loaded by Claude Code. Keep it short, factual, and link out for depth.

## What this repo is

Two Python projects living side by side:

1. **Root (`main.py`)** — minimal FastAPI server wrapping `Qwen/Qwen2.5-Coder-3B-Instruct` for a Polars code-gen `/chat` endpoint. Single-file shim.
2. **`autonomous_company_v10/`** — the real work. A multi-agent CEO/Worker orchestration system built on `claude-agent-sdk`, with DAG planning, SQLite checkpointing, HITL gates, OpenTelemetry, and a tools allowlist. **~4k LOC, 25 modules.** This is what bug-fix and roadmap work targets.

## Active branch

`claude-branch` (not `master`). All Phase A bug fixes landed on `1a98bd3`. Continue Phase B work on this branch unless told otherwise.

## Where to look first

| Task | Read |
|---|---|
| High-level design | `docs/ARCHITECTURE.md` |
| What's been fixed / what's next | `docs/ROADMAP.md` |
| Bug ledger (C-1 … H-15) | `docs/BUGS.md` |
| Subproject conventions | `autonomous_company_v10/CLAUDE.md` |
| Frameworks comparison (27 SOTA) | `docs/SOTA_COMPARISON.md` (TBD, see ROADMAP §Phase 0) |

## Commands

All commands run from `autonomous_company_v10/` unless noted.

```bash
# Install (uv preferred, pip fallback)
cd autonomous_company_v10 && uv sync --extra dev
# Test
uv run pytest -q
# Lint
uv run ruff check src tests
# CLI
uv run autonomous-company --help
```

Root FastAPI server (rarely touched):

```bash
uv run uvicorn main:app --reload
```

## Conventions

- **Python 3.12+**, type-annotated, async-first (`anyio` / `asyncio`).
- **Settings** flow through `pydantic-settings` in `config.py`. Never hardcode budgets, retries, timeouts — read from `Settings`.
- **DB writes** go through `storage.py`. Use `UPSERT` patterns; do not write raw `UPDATE` for checkpoints (regression risk — see C-3).
- **Prompts** are Jinja2 templates in `prompts/`. **Always sanitize** untrusted user input before it reaches a template (see C-1).
- **Shell commands** must pass `security_hooks.py` allowlist checks. `&&`/`||`/`;`/backticks are split-and-validated per token (see S-2).
- **Tests** are async-marked via `asyncio_mode = "auto"` (already in `pyproject.toml`); just write `async def test_…`.

## Doing-the-task rules

- One todo `in_progress` at a time. Mark complete the moment a task lands.
- Don't add error handling, fallbacks, or feature flags for cases that can't happen.
- Default to no comments. Only add WHY comments for non-obvious invariants.
- Edits over rewrites. Touch only the lines the task requires.
- Before claiming a fix done: run `uv run pytest -q` and confirm green.

## Pointers (for fast nav)

- CEO orchestration: `autonomous_company_v10/src/autonomous_company/ceo.py`
- Worker loop: `…/worker.py`
- LLM gateway (retry + OTEL + cost): `…/llm.py`
- DAG (cycle detect, topo): `…/dag.py`
- Checkpoints (UPSERT, plan-hash): `…/storage.py`
- Allowlist: `…/security_hooks.py` + `config/tools_allowlist.yaml`
- HITL gates: `…/hitl.py`
- Events bus: `…/events.py`

## What NOT to do

- Don't push to `master` from automation. Phase work goes to `claude-branch` (or whatever feature branch the session opens).
- Don't `git reset --hard` or force-push without explicit user OK.
- Don't introduce new top-level packages — extend `autonomous_company_v10/src/autonomous_company/` instead.
- Don't widen `tools_allowlist.yaml` without a security note in the commit.
