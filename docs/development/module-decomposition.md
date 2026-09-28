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

Without re-exports, each decomposition PR rewrites the callers of its own
module, and those callers live in the other PRs' files. So the PRs overlap in
import statements, and they merge **in a fixed order**. Put the modules others
import first and the module that imports all the others last. After each
merge, **sync the next PR with `main` before merging it, even when GitHub
reports no conflict.** Files a PR *adds* were never seen by the codemods of
the PRs merged before it, so a textually clean merge can still import a
removed path.

A sync is one `train_step.sh` run in the PR's worktree:

```bash
TRAIN_MODULE_MAP=module-map.json TRAIN_BASE=<commit the wave branched from> \
    scripts/refactor/train_step.sh origin/main <this key> <merged key> ...
```

- **`.py` conflicts are resolved per file.** Diff each side against the merge
  base. Keep the side whose edits go beyond imports. The other side's import
  rewrites are re-derived when the step re-runs `clean_refs.py --rewrite` for
  every merged module. If both sides made non-import edits, the step stops for
  a human. Do not resolve `.py` hunks line by line: a line union broke the
  syntax of a parenthesized import, and taking one side's hunk whole dropped a
  module-alias import the codemods cannot re-derive.
- **Docs conflicts** go to `train_resolve.py`. It takes each line from
  whichever side changed it, or merges per character when the edits don't
  overlap.
- **Pin `--base` / `TRAIN_BASE`** to the commit the wave branched from. After
  the first merge, `origin/main` no longer holds the original module file that
  `clean_refs.py` reads.
- The step refuses to commit unless `clean_refs.py` passes for every merged
  module, and `ruff` (read its exit code; `ruff ... | tail` reports tail's),
  `black --check` and test collection all pass. After it, run the PR's own
  tests, re-run `verify_move.py --clean`, push, and wait for every required
  check (`ci_required.sh`) before merging.
- A resolution can drop a hand edit that no import gate sees. On #1707, taking
  one side of a test file removed a guard widening, and the guard kept passing
  while reading an empty `__init__.py`. Before merging the last PR, build the
  whole sequence in a scratch worktree and run the full suite on it.

## 9. Decomposing a class (extracting its methods)

Sections 1–8 move whole top-level statements. A class whose size is its own methods needs a different move: its methods leave the class. Choose each method's new home by what it depends on:

- **Module function.** Use this when the method reads no instance state, or reads dependencies the owner can pass as values **at call time**: `self._fn(a)` becomes `_fn(self.dep, a)`. The function receives the owner's one binding on every call, so a test that replaces `owner.dep` still reaches it. Dependency parameters drop leading underscores. An owner **property**, such as a data directory that tests patch on the class, is a dependency too: the owner evaluates it at each call.
- **Collaborator class.** Use this only when the group holds mutable state, or is called from more than one module (a patched function would then have several reader namespaces), or is a natural object seam. The owner builds it in `__init__` and holds it as a public attribute. A collaborator method called from outside the collaborator loses its leading underscore.
- **Never a mixin.** It splits the file, not the class.

A collaborator must never snapshot a value that its owner or a sibling also reads. If it did, `owner.x = stub` would reach half the code. When several objects read the same dependencies, give them one **shared holder**: `holderize.py` moves the owner's `self.X` attributes into a dataclass the owner builds as `self.deps`, and every reader uses `self.deps.X`. The owner must also receive every dependency through its constructor: a dependency assigned after construction never reaches a collaborator that was already built (#1722).

The tools, in order:

```bash
python scripts/refactor/holderize.py --repo WT --source path/engine.py --class Engine \
    --module path/dependencies.py --state _locks --apply           # only if a holder is needed
python scripts/refactor/extract_members.py --repo WT --spec spec.json            # dry run: homes, deps, ERRORS
python scripts/refactor/extract_members.py --repo WT --spec spec.json --apply --report r.json
python scripts/refactor/verify_extract.py --base BASE_WT --head WT --spec spec.json
python scripts/refactor/audit_seams.py --repo WT --spec spec.json --report r.json [--rewrite]
```

- **`extract_members.py`** rewrites the owner and writes the new modules. Its dry run refuses a boundary that is wrong: a moved member that calls back into the owner, a dependency that would shadow a local, a module-level name with no home, a collaborator cycle, or a collaborator reading an owner property. Fix the spec, not the output. It copies the owner's whole import block into each new module. A group with `"append": true` adds to a module that already exists: its imports merge into that module's import header, a name the module already imports is not imported again, and a name it already binds from a different source is refused. The project's lint gate does not enforce F401, so prune with `ruff check --fix --extend-select F401 <written files>`: `--extend-select` adds the rule to the configured set, which the same run also applies (import sorting included), where `--select` would replace it.
- **`verify_extract.py`** is independent of the codemod. It canonicalises the base and head bodies (member calls, dependency reads, constants) and requires an equal AST for every method. It checks the dependency arguments at each rewritten call site, the public-name rule, the owner's collaborator construction, and the owned-state initialisers.
- **`audit_seams.py`** finds the references a move leaves stale or vacuous:
  - `obj._moved(...)`;
  - `patch.object(obj, "_moved")`;
  - `obj._moved = stub`, which silently creates an attribute nothing reads;
  - a dependency swap the owner no longer honours;
  - a `__new__`-built owner that now lacks its collaborators.

  `--rewrite` fixes the mechanical cases. A name that another class still defines (a sibling repository, say) is reported AMBIGUOUS and never rewritten.
- **`route_table.py`,** for moves of route handlers, proves the served route table is unchanged, in order.

**Delete a delegate shim rather than moving it.** A method whose body only calls a module function, kept "because callers and tests target it", is a compatibility shim. Point its callers at the function.

## 10. Splitting a long function body

Sections 1–9 move whole definitions. A method whose size is its own body needs Extract Method: runs of its statements become named **phases**. A wrong cut changes behaviour without changing any statement: an input bound on only some paths, an output left unbound on one, a read that now resolves to a global. So every cut is checked statically, and the whole split is proven by re-inlining.

The phase rules:

- **Two shapes.** A phase is a run of consecutive statements of one statement list, at any nesting depth.
  - **STRAIGHT** contains no `return`. The owner calls `a, b = [await] self._phase(x=x, ...)`, and the phase ends with `return a, b`.
  - **TAIL** is a suffix of its statement list on which every path ends in `return` or `raise`. The owner calls `return [await] self._phase(...)` where the suffix began.

  There is no reply-or-continue protocol. A block that sometimes returns and sometimes falls through keeps its dispatch (the `if` tests and the fall-through code) in the owner; only its terminating branches become TAIL phases.
- **Inputs** are the names the run reads before binding them; an augmented assignment `x += a` reads `x`. Each must be bound on every path into the phase. Parameter names equal argument names.
- **Outputs** are the names the run assigns that are read after it. Each must be assigned on every fall-through path of the phase, or already bound on entry, in which case it is also an input so that its entry value survives the paths that do not re-bind it. An `except ... as e` name is never an output.
- **What stays in the owner.** A phase never assigns a name that an `except`/`finally` handler or a closure outside it reads. Also refused inside a phase: `nonlocal`, `global`, `yield`, `locals()`, `vars()`, `del`, a `break` or `continue` that escapes it, and a read of a name bound only later in the function.

The phase call replaces its statements in place, inside the same `try`, so exceptions propagate unchanged.

The tools, in order:

```bash
python scripts/refactor/phase_flow.py --file F --func Class.method --outline [--depth N]
python scripts/refactor/phase_flow.py --file F --func Class.method --phase NAME:FIRST-LAST [...]
python scripts/refactor/extract_phase.py --file F --func Class.method --phase NAME:FIRST-LAST [...] --apply
python scripts/refactor/verify_inline.py --base BASE_FILE --head HEAD_FILE --func Class.method
```

- **`phase_flow.py`** prints the method's statement ranges (`--outline`), then reports each candidate phase's shape, inputs and outputs. It refuses any phase the rules above reject, and overlapping phases.
- **`extract_phase.py`** is step A. Each phase becomes a private method of the same class with keyword-only parameters, its body moved verbatim, and the owner gets the call. It uses `phase_flow.py`'s analysis, so it refuses the same phases. Run black, then replace each placeholder docstring with one line saying what the phase does.
- **`verify_inline.py`** is the step-A proof, and is independent of `extract_phase.py`. It substitutes every phase body back at its call site and requires the base method's AST exactly. It then checks the call contract (arguments equal parameters, call targets equal returned outputs, a call is awaited exactly when its method is async, a TAIL call is returned), and the bindings: a base local that a phase reads before binding must be a parameter, a STRAIGHT phase's returned names must be bound on every path or be parameters, and no base local resolves as a global. Those binding checks catch splits whose re-inline is AST-equal but which raise at run time.

**Step B** moves the phase methods out as module functions with §9's tools. A phase that calls back into the owner stays a method: `extract_members.py` refuses to move it. Phases are functions, not collaborators, because each has one caller and holds no state. Step B also changes the `logger` name on records emitted from a moved phase to the new module's `__name__`; check that nothing keys on the old name.

**Source-reading tests.** A test that reads `inspect.getsource(<owner>)` and asserts that a call is present, or that one call precedes another, finds the call moved into a phase. Point a presence check at the phase that now holds the call. For an order check across phases, never concatenate sources, which changes the order: rebuild the owner by re-inlining every phase at its call site, and assert on that. Prove each re-pointed test with a planted failure, as in §5.

## Checking CI before building on a SHA

```bash
scripts/refactor/ci_required.sh <sha> [--repo OWNER/NAME] [--contexts "..."]
```

Prints the latest conclusion of every required context and a single
GREEN/PENDING/RED verdict (exit 0/2/1) — never GREEN while a context is
absent, queued or still running, which is the failure mode of grepping
`gh pr checks` output for the word "fail" while a required check simply
hasn't been scheduled yet.
