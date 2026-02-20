---
description: Run tests, fix failures, re-run until green, report what changed
allow_all_tools: true
---

# Fix Tests

You run tests, fix what's broken, and report what you changed. No ceremony.

**You must follow all [Agent Principles](../../../.claude/standards/agent-principles.md) and [Testing Standards](../../../.claude/standards/TESTING_STANDARDS.md).**

## Your Task

$ARGUMENTS

## Workflow

1. **Run tests** — Execute the test command (default: `.venv/bin/python -m pytest --tb=short -q` in `faultmaven/`)
2. **If all pass** — Report the count and stop. No report needed when things work.
3. **If failures** — For each failing test:
   - Read the failing test and the code under test
   - Determine root cause (is the test wrong or is the code wrong?)
   - Fix the root cause directly
4. **Re-run** — Run the same test command again
5. **Repeat** steps 3-4 until all tests pass (max 5 iterations)
6. **Report** — One concise summary of what you fixed:

```
Tests: X passed (was Y failures)
Fixed:
- [file:line] what was wrong → what you changed
- [file:line] what was wrong → what you changed
Skipped (not fixable without discussion):
- [file:line] why
```

## Rules

- **Fix code, not tests** — If a test correctly asserts expected behavior and the code is wrong, fix the code. Only fix the test if the test itself has a bug.
- **Don't change test assertions to make them pass** — That defeats the purpose.
- **Stay focused** — Only fix what's failing. Don't refactor, don't improve, don't add tests.
- **Stop after 5 iterations** — If you can't fix it in 5 rounds, report what's left and why.
- **Use the venv** — Always run via `.venv/bin/python -m pytest` in the `faultmaven/` directory.

## Scope Override

If the user specifies a scope, use it:

- `/fix-tests` → run all tests
- `/fix-tests tests/auth/` → run only auth tests
- `/fix-tests tests/case/test_routes.py` → run one file
- `/fix-tests --cov` → run with coverage report appended
