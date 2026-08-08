#!/bin/bash
#
# FaultMaven — warn when a stamped virtualenv no longer matches its lockfile.
# Called by the post-merge and post-checkout hooks; not a hook itself.
#
# The problem it solves: `requirements/*.txt` are exact pins under version
# control, but a virtualenv is persistent state. `git pull` updates the file and
# never the environment, so every lockfile bump silently widens the gap. CI
# cannot drift — it rebuilds from the lockfile on an empty runner every run — so
# the divergence is invisible until a local result disagrees with CI's, and then
# it looks like a code problem rather than an environment one. A drifted
# interpreter has already made a generated-artifact gate report false staleness.
#
# Design notes:
#  - WARNS, never syncs. A checkout that rewrites the environment underneath a
#    test run in another terminal is worse than the drift it fixes, and several
#    worktrees here share one checkout's venvs.
#  - DISCOVERS stamped venvs (`.venv*/.locksum`) rather than hardcoding names.
#    A contributor who has not run scripts/sync-venv.sh has no stamp and sees
#    nothing at all — this adds no noise to anyone not using it.
#  - Reads stamps from BOTH the main checkout (git-common-dir) and the current
#    tree, because either can hold a venv: worktrees usually share the main
#    checkout's, but sync-venv.sh run inside a worktree creates one there. It
#    always compares against THIS tree's lockfile — the question is whether the
#    environment matches the branch you are now on.
#  - Never fails. Hooks that abort a merge because a helper misbehaved are worse
#    than no hook; every path ends 0.

set -uo pipefail

YELLOW='\033[1;33m'; NC='\033[0m'

# macOS has shasum rather than sha256sum. Nothing here is security-sensitive —
# it is a change detector — but silently degrading to "no check" on a platform
# would be the fail-open shape this hook exists to avoid, so pick explicitly and
# say nothing only when neither tool exists.
if command -v sha256sum >/dev/null 2>&1; then
    _sha256() { sha256sum "$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
    _sha256() { shasum -a 256 "$1" | awk '{print $1}'; }
else
    exit 0
fi

# `x="$(cmd || exit 0)"` would exit the SUBSHELL, not this script — the
# assignment then succeeds with an empty value and everything downstream runs
# against "". Test the values instead.
TREE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
COMMON_DIR="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
[ -n "$TREE_ROOT" ] && [ -n "$COMMON_DIR" ] || exit 0
MAIN_ROOT="$(dirname "$COMMON_DIR")"

# In the ordinary case a worktree's toplevel differs from the main checkout and
# both are searched; in the main checkout itself they are the same path, so it
# is listed once to avoid reporting every venv twice.
ROOTS=("$MAIN_ROOT")
[ "$TREE_ROOT" != "$MAIN_ROOT" ] && ROOTS+=("$TREE_ROOT")

STALE=()
for root in "${ROOTS[@]}"; do
for stamp in "$root"/.venv*/.locksum; do
    # No match leaves the glob literal; the -f test is what skips it.
    [ -f "$stamp" ] || continue

    # A stamp is one `sha256sum` line: "<hash>  requirements/<name>.txt".
    recorded_hash="$(awk 'NR==1 {print $1}' "$stamp")"
    lockfile="$(awk 'NR==1 {print $2}' "$stamp")"
    [ -n "$recorded_hash" ] && [ -n "$lockfile" ] || continue

    # The lockfile may not exist on this branch (or may have been renamed).
    # That is a real mismatch, but not one this hook can describe usefully, so
    # leave it to the sync script rather than guess.
    [ -f "$TREE_ROOT/$lockfile" ] || continue

    [ "$recorded_hash" = "$(_sha256 "$TREE_ROOT/$lockfile")" ] && continue

    # Carry the venv's PATH, not its basename. Two roots are searched, so
    # `.venv-dev` in the main checkout and `.venv-dev` in a worktree would
    # otherwise print as one indistinguishable line — and the fix command below
    # has to name the actual directory to rebuild.
    STALE+=("$(dirname "$stamp")|$lockfile")
done
done

[ ${#STALE[@]} -eq 0 ] && exit 0

printf "${YELLOW}⚠ virtualenv is behind its lockfile${NC}\n" >&2
for entry in "${STALE[@]}"; do
    venv_path="${entry%%|*}"; lock="${entry##*|}"
    printf "    %s was built from a different %s\n" "$venv_path" "$lock" >&2
done

printf "\n  Local results may disagree with CI until you re-sync:\n" >&2
for entry in "${STALE[@]}"; do
    venv_path="${entry%%|*}"; lock="${entry##*|}"
    extra="$(basename "$lock" .txt)"

    # sync-venv.sh defaults its target to .venv-<extra>. Anything else — the
    # shared `.venv`, a second env for the same lockfile — was created with the
    # explicit-path form and must be repeated, or this command would rebuild a
    # DIFFERENT venv, leave the stale one untouched, and warn again forever.
    if [ "$(basename "$venv_path")" = ".venv-${extra}" ]; then
        printf "      ./scripts/sync-venv.sh %s\n" "$extra" >&2
    else
        printf "      ./scripts/sync-venv.sh %s %s\n" "$extra" "$venv_path" >&2
    fi
done
printf "\n" >&2

exit 0
