# submission_example

Two Python projects in one repo:

1. **Root** (`main.py`) — minimal FastAPI server wrapping `Qwen/Qwen2.5-Coder-3B-Instruct` for a Polars code-gen `/chat` endpoint. Single-file shim for benchmarking.
2. **`autonomous_company_v10/`** — multi-agent CEO/Worker orchestrator on top of `claude-agent-sdk`. DAG planning, SQLite checkpointing, HITL gates, OpenTelemetry, tools allowlist. Active development.

## Quickstart — autonomous_company_v10

```bash
cd autonomous_company_v10
cp .env.example .env       # set ANTHROPIC_API_KEY
uv sync --extra dev
uv run pytest -q
uv run autonomous-company --help
```

## Quickstart — root FastAPI shim

```bash
uv sync                    # uses root pyproject.toml
uv run uvicorn main:app --reload
# POST /chat with {"message": "...", "tables": {...}}
```

## Where the docs live

| Doc | Purpose |
|---|---|
| `CLAUDE.md` | Memory file auto-loaded by Claude Code agents |
| `docs/ARCHITECTURE.md` | Component diagram, data model, invariants |
| `docs/ROADMAP.md` | Phase A/0/B/C/D/E plan + status |
| `docs/BUGS.md` | Verified bug ledger (C-1..H-15) with repro + fix |
| `autonomous_company_v10/CLAUDE.md` | Subproject-specific memory |

## Branching

- `master` — released code.
- `claude-branch` — active Claude-assisted work. Phase A bug fixes landed at `1a98bd3`. Phase B in progress.

## For Claude Code agents

Slash commands (in `.claude/commands/`):

- `/run-tests` — run pytest + ruff for `autonomous_company_v10`.
- `/phase-b` — start the Phase B fix sequence (H-1 through P-1).
- `/audit-bugs` — read-only audit against the bug ledger.

Permissions are pre-configured in `.claude/settings.json`. Force-pushes, hard-resets, and `.env` reads are denied. Pushes and commits prompt for confirmation.
