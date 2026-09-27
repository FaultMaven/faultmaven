# Module Decomposition Workflow

How to split an oversized module in this codebase without leaving a facade
behind. The rule the whole workflow serves is stated once, in
[`CLAUDE.md`, Architecture](../../CLAUDE.md#architecture): **no backward
compatibility — the system is pre-user.** A decomposition builds the clean
end state directly: one canonical import path per symbol (the module that
defines it), no facades, re-exports, shims or deprecated aliases. This
document does not restate that rule; it is the checklist for satisfying it
mechanically instead of by review alone.

The tools below live in `scripts/refactor/`; each script's own docstring
covers its usage and exit codes in full (`--help`, or read the top of the
file). Run everything with the project's own toolchain
(`pip install -e ".[dev]"`, or `.venv/bin/python` if you keep a venv) — not
whatever `python`/`ruff`/`black` happen to resolve on `PATH`.

## 1. Plan the split

Before moving a single line, sketch the grouping — which top-level
statements of the old module go to which new submodule — as line ranges, and
check it closes:

```bash
python scripts/refactor/plan_split.py old_module.py grouping.json [baseline_audit.json]
```

`grouping.json` maps a submodule name to the old module's 1-indexed,
inclusive line ranges. The report shows, per group: its size, which other
groups' names it needs (a group needing the `__init__` remainder is a cycle
through the facade you're trying to delete — illegal by construction), and
group-to-group cycles. Catching a cycle here is nearly free; catching it
after the code has moved costs a second decomposition.

## 2. Move byte-for-byte

Move the code with no edits — not even a reformat — then prove it:

```bash
python scripts/refactor/verify_move.py --repo <worktree> --base origin/main \
    --old path/to/old_module.py --clean
```

This diffs every top-level statement of the old module against the new
package's inventory of the same kind: MISSING (dropped), DUPLICATED (pasted
twice), CHANGED (edited in transit) all fail the run; ADDED is printed for a
human to read but never fails it. `--clean` also requires every logger to be
`getLogger(__name__)`. A body that differs only in a re-pointed
function-local import reads as IMPORT-ONLY and does not fail — that's an
expected caller update, not a text change.

Do not fix findings by hand-editing the new files to make the diff pass;
fix the actual move so the statement really is what it was.

## 3. Rewrite callers, then remove the facade

```bash
python scripts/refactor/clean_refs.py --repo <worktree> --old path/to/old_module.py --rewrite
```

This finds every stale reference to the old path — a leftover
`from old_module import X` where `X` is defined in a submodule, an aliased
`import old_module as m` then `m.X`, a patch string built from the old path,
an `__init__.py` re-export, an unused "kept for parity" import, a literal
logger name — and `--rewrite` fixes the plain `from D import ...` cases by
splitting the import across the modules that actually define each name. Run
`ruff check --fix` and `black` on whatever it touched afterward (the codemod
does not reformat). Re-run without `--rewrite` until it reports
`RESULT: PASS`; the WARN-DOC / WARN-DOC-DOTTED lines are for updating docs
and diagrams by hand, and never fail the run.

Only once this passes does the old file/facade actually go away. A package's
`__init__.py` keeps a docstring and genuinely package-level code — nothing
else.

## 4. Check globals

`verify_move` proves the moved text is identical; it cannot prove the moved
code still means the same thing. A submodule that imports a different
object under an identical name (`import datetime` vs `from datetime import
datetime`, or a same-named class from elsewhere) changes behavior with
zero-diff text, and nothing else in this workflow catches it — including a
read confined to a class body or a lambda, not just an ordinary function.

```bash
python scripts/refactor/check_globals.py --repo <head worktree> --base <base worktree> \
    --old path/to/old_module.py
```

`--base` here is a worktree (a real checkout of the pre-split revision), not
a git ref — the tool execs the old file directly to get the same objects the
new code should still be resolving to.

## 5. Audit patches, and adjudicate FLAGs with the poison plugin

A test's `patch("pkg.mod.name")` replaces one binding. If the code path
under test moved into a submodule that imports `name` into its own globals,
the facade still has `name` — the patch applies without error — but nothing
under test reads it through `pkg.mod` any more. The test keeps passing
throughout, having proved nothing.

```bash
python scripts/refactor/audit_patches.py --repo <worktree> --module pkg.mod --json out.json [--compare base.json]
```

Run it before and after the split and `--compare`: only verdicts that got
*worse* (OK/CLASS_ATTR/DICT/MODULE → FLAG → INERT → MISSING) are this
decomposition's to fix. A FLAG or INERT verdict is a hypothesis about the
static reader graph, not a proof about what a real test executes — adjudicate
it dynamically before treating it as a real defect:

```bash
PYTHONPATH=scripts/refactor POISON_TARGET="pkg.mod.name" POISON_KIND=async \
    pytest -p poison_plugin path/to/test_file.py
```

**Always pair this with a positive control**: one run where the target is
known to be reached (that run must fail on "POISON reached: ..."), one run
for the case the audit flagged (this run's own pass/fail is the answer). An
assertion that has never been observed to fail proves nothing about the case
it was meant to catch.

## 6. Re-prove the source-reading guards

Architecture and lint guards that walk the source tree (`lint-imports`,
`ruff check`, any `tests/unit/architecture/` test that scans files with
`ast`) must be re-run after the move, not trusted from before it — a guard
that reads paths or module names is exactly the kind of check a decomposition
can quietly slip past by relocating the thing it was watching.

## 7. The combined-tree check

A PR's own CI ran against a base that predates every sibling decomposition
landing beside it. `git diff`/`merge-tree` prove the *text* merges; they do
not prove a repo-wide guard added by one PR still passes against the tree
every PR together produces. Before treating a stack of decomposition PRs as
mergeable in any order, build the combined tree (merge them into a scratch
branch) and re-run the full gate list — `ruff`, `black --check`,
`lint-imports`, `generate_api_docs.py --check`, and the test suite — on that
tree, not on each PR's own base.

## 8. Merging interlocking PRs in order

Several decomposition branches that touch the same shared files (each fixing
up imports for its own moved symbol) conflict on nearly every merge, almost
always on import lines only. `train_resolve.py` (and `train_step.sh`, which
wraps it with the re-check below) resolve a hunk only when the outcome is
mechanically re-derivable afterward, per this rule:

- one side changed *only* import lines relative to the merge base → take the
  other side (its import rewrites are re-derived by re-running `clean_refs.py
  --rewrite` on every merged module afterward);
- both sides changed only import lines → union-merge (keep everything either
  side added, drop everything either side removed — a module-alias import is
  not something the codemod can re-derive on its own, so neither addition may
  be dropped);
- a `.md` hunk → take each line from whichever side changed it, or merge
  character-by-character when the edits don't overlap;
- anything else, on either side → left with conflict markers in place, for a
  human. Never guessed at.

```bash
TRAIN_MODULE_MAP=module-map.json TRAIN_VENV=.venv/bin \
    scripts/refactor/train_step.sh <branch> <key> [<already-merged key> ...]
```

`module-map.json` maps each module key to the CLI arguments `clean_refs.py`
takes after `--old` (see the script's own docstring for the exact shape). A
step re-runs the import codemod and `clean_refs.py`'s check for every module
merged so far — not just the one this step is merging — because a later
merge can reintroduce a stale import an earlier step already fixed. It
refuses to commit unless `clean_refs.py` reports `RESULT: PASS` for every one
of them, plus `ruff`, `black --check` and test collection all pass.

## Checking CI before building on a SHA

```bash
scripts/refactor/ci_required.sh <sha> [--repo OWNER/NAME] [--contexts "..."]
```

Prints the latest conclusion of every required context and a single
GREEN/PENDING/RED verdict (exit 0/2/1) — never GREEN while a context is
absent, queued or still running, which is the failure mode of grepping
`gh pr checks` output for the word "fail" while a required check simply
hasn't been scheduled yet.
