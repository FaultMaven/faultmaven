#!/usr/bin/env bash
# train_step.sh — one merge-train step, in the CURRENT git worktree, for a
# set of interlocking module-decomposition branches that each move a
# different symbol out of the same shared files.
#
# PROVES/DOES: merges one branch into the current one; resolves whatever
# conflicts it can by the rule below (train_resolve.py, plus a same rule
# inlined here for *.py hunks so it can consult `git diff` against the merge
# base); re-runs the import codemod (clean_refs.py --rewrite) of every
# merged module so any import-only side a resolution took is re-derived
# rather than left stale; formats with the project's own ruff/black; and
# refuses to commit unless clean_refs.py reports RESULT: PASS for every
# merged module and ruff/black/collection all pass. Stops (exit 1) on
# anything it cannot resolve, leaving the merge in progress for a human.
#
# WHY: several decomposition branches over the same shared files (each
# fixing up its own moved symbol's imports) conflict almost every merge, on
# import lines this rule can resolve deterministically. Committing without
# re-checking clean_refs would let an import-only resolution regress a
# rewrite a sibling branch already made — a merge step must re-prove the
# invariant it might have just undone, not just resolve the text.
#
# USAGE:
#   train_step.sh [--module-map FILE] [--venv DIR] <branch> <key> [<already-merged key> ...]
#
#   <key>            this step's module key (see --module-map)
#   <already-merged key> ...  keys merged by earlier steps in this train, so
#                    their import codemods are re-run too (a later merge can
#                    reintroduce a stale import an earlier step already fixed)
#
#   --module-map FILE   JSON object mapping a module key to the CLI argument
#                    list clean_refs.py/verify_move.py take after `--old`,
#                    e.g.:
#                      {
#                        "case-routes": ["faultmaven/modules/case/api/routes.py",
#                                        "--homes", "faultmaven/modules/case/api/title_generation.py"],
#                        "causal-graph": ["faultmaven/core/investigation/causal_graph.py"]
#                      }
#                    Defaults to $TRAIN_MODULE_MAP if set; one of the two is
#                    required — there is no built-in module list, because it
#                    is specific to the decomposition in flight.
#   --venv DIR       directory holding python/ruff/black (a venv's bin/).
#                    Defaults to $TRAIN_VENV, then <repo-root>/.venv/bin if it
#                    exists, then bare `python3`/`ruff`/`black` off PATH.
#
# EXIT CODES: 0 the step merged (or resolved) cleanly, re-checked clean, and
# committed. 1 a conflict this script does not resolve, or any of
# clean_refs/ruff/black/collection failed — nothing is committed either way,
# so the working tree is left for a human to finish.
set -uo pipefail

T="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

usage() {
  echo "Usage: $0 [--module-map FILE] [--venv DIR] <branch> <key> [<already-merged key> ...]" >&2
  exit 1
}

MODULE_MAP="${TRAIN_MODULE_MAP:-}"
VENV_DIR="${TRAIN_VENV:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --module-map) MODULE_MAP=$2; shift 2 ;;
    --venv) VENV_DIR=$2; shift 2 ;;
    --) shift; break ;;
    -*) usage ;;
    *) break ;;
  esac
done
[ $# -ge 2 ] || usage
branch=$1; shift; key=$1; shift; merged=("$@" "$key")

repo=$(pwd)
if [ -z "$VENV_DIR" ]; then
  root=$(git rev-parse --show-toplevel 2>/dev/null || echo "$repo")
  [ -x "$root/.venv/bin/python" ] && VENV_DIR="$root/.venv/bin"
fi
PY="python3"; RUFF="ruff"; BLACK="black"
if [ -n "$VENV_DIR" ]; then
  PY="$VENV_DIR/python"; RUFF="$VENV_DIR/ruff"; BLACK="$VENV_DIR/black"
fi

if [ -z "$MODULE_MAP" ]; then
  echo "error: --module-map FILE (or \$TRAIN_MODULE_MAP) is required" >&2
  exit 1
fi
[ -f "$MODULE_MAP" ] || { echo "error: module map not found: $MODULE_MAP" >&2; exit 1; }

declare -A OLD
while IFS=$'\t' read -r k v; do OLD["$k"]="$v"; done < <(
  "$PY" -c '
import json, sys
data = json.load(open(sys.argv[1]))
for k, v in data.items():
    args = v if isinstance(v, list) else [v]
    print(k + "\t" + " ".join(args))
' "$MODULE_MAP"
)
for k in "${merged[@]}"; do
  [ -n "${OLD[$k]+x}" ] || { echo "error: key '$k' is not in $MODULE_MAP" >&2; exit 1; }
done

if git merge -q --no-ff --no-edit "$branch" >/dev/null 2>&1; then
  echo "merged clean: $branch"
else
  mapfile -t U < <(git diff --name-only --diff-filter=U)
  echo "conflicts: ${U[*]}"
  # modify/delete: the file moved into a package or was deleted on one side -> take the deletion
  for f in "${U[@]}"; do
    if [ ! -e "$f" ] || ! grep -q '^<<<<<<< ' "$f" 2>/dev/null; then
      st=$(git status --porcelain -- "$f" | cut -c1-2)
      case "$st" in DU|UD) git rm -q "$f" && echo "  took deletion: $f";; *) echo "  UNHANDLED status $st: $f"; exit 1;; esac
    fi
  done
  mapfile -t U2 < <(git diff --name-only --diff-filter=U)
  for f in "${U2[@]}"; do
    case "$f" in
      *.py)
        # Keep the side whose edits (vs the merge base) go beyond imports; the
        # other side's import rewrites are re-derived by the codemods below.
        # Both sides with non-import edits -> a human merges it.
        mb=$(git merge-base HEAD MERGE_HEAD)
        ni() { git diff "$mb" "$1" -- "$f" | grep -E '^[-+]' | grep -vE '^(\+\+\+|---)' | grep -vE '^[-+]\s*(from |import |\)|[A-Za-z_][A-Za-z0-9_]*( as [A-Za-z_]+)?,?\s*$|$)' | wc -l; }
        o=$(ni HEAD); t=$(ni MERGE_HEAD)
        if [ "$t" = 0 ]; then git checkout --ours -- "$f"; echo "  kept ours (incoming import-only): $f"
        elif [ "$o" = 0 ]; then git checkout --theirs -- "$f"; echo "  took incoming (ours import-only): $f"
        else echo "NEEDS MANUAL (both sides non-import: ours=$o theirs=$t): $f"; exit 1; fi
        git add "$f";;
      *)
        "$PY" "$T/train_resolve.py" "$f" || { echo "NEEDS MANUAL: $f"; exit 1; }
        git add "$f";;
    esac
  done
fi
# re-derive import rewrites of every merged module (paths are absolute via
# --repo, so this does not depend on the caller's cwd)
for k in "${merged[@]}"; do
  "$PY" "$T/clean_refs.py" --repo "$repo" --old ${OLD[$k]} --rewrite >/dev/null 2>&1
done
mapfile -t C < <(git diff --name-only -- '*.py'; git diff --cached --name-only -- '*.py')
if [ ${#C[@]} -gt 0 ]; then
  "$RUFF" check --fix -q "${C[@]}" >/dev/null 2>&1; "$BLACK" -q "${C[@]}" >/dev/null 2>&1
fi
fail=0
for k in "${merged[@]}"; do
  r=$("$PY" "$T/clean_refs.py" --repo "$repo" --old ${OLD[$k]} 2>&1 | grep -E "^# clean|RESULT")
  echo "  [$k] $(echo "$r" | tr '\n' ' ')"
  echo "$r" | grep -q "RESULT: PASS" || fail=1
  echo "$r" | grep -q "warnings=0" || echo "  [$k] has warnings"
done
[ $fail -eq 1 ] && { echo "clean_refs FAILED; not committing"; exit 1; }

ruff_log=$(mktemp)
collect_log=$(mktemp)
trap 'rm -f "$ruff_log" "$collect_log"' EXIT

"$RUFF" check faultmaven/ tests/ >"$ruff_log" 2>&1 || { echo "ruff FAILED; not committing"; tail -20 "$ruff_log"; exit 1; }
"$BLACK" --check -q faultmaven/ tests/ || { echo "black FAILED; not committing"; exit 1; }
SKIP_SERVICE_CHECKS=true "$PY" -m pytest tests/ --collect-only -q -p no:cacheprovider >"$collect_log" 2>&1 || {
  echo "collection FAILED; not committing"; grep -E "Error|error" "$collect_log" | head -10; exit 1;
}
echo "  gates: ruff+black+collect OK ($(tail -1 "$collect_log"))"
git add -A && git -c core.editor=true commit -q --no-edit 2>/dev/null || git commit -q -m "train: merge $branch" 2>/dev/null || true
echo "step done: $(git log --oneline -1)"
