---
description: Run the autonomous_company_v10 test suite (pytest + ruff)
---

Run the full quality gate for the subproject. Output should be the bare results (pass/fail per check); don't narrate.

```bash
cd autonomous_company_v10 && uv run pytest -q && uv run ruff check src tests
```

If any check fails, surface the failing test/file and proposed fix. Do not auto-fix without confirmation unless the failure is a trivial typo or import error.
