#!/usr/bin/env bash
#
# Score the A/B shadow count rule from the `benchmark-ab` artifacts (#1989).
#
#   scripts/score_ab_shadow_rule.sh YYYY-MM-DD
#
# YYYY-MM-DD is the day the shadow rule merged: only A/B runs created on or
# after it are read. Run it from a FaultMaven clone with the dev environment
# installed (`PYTHON=...` picks the interpreter) and an authenticated `gh`.
# The promotion criterion it scores is in
# docs/development/testing/performance.md.
#
# One row per PULL REQUEST, its latest run that evaluated the rule (the
# independent unit: a pull request's reruns and pushes share its code):
#
#   run  pr  touches  count_would_fail  over_count  median
#
# `touches` is how many of the pull request's changed files are benchmarked
# code (python -m tests.wallclock.benchmarked_files); 0 counts toward the
# criterion. A failed lookup is an `ERROR` row, never a 0, and any ERROR row
# makes the exit status 1. A summary goes to stderr, so "no runs yet" is a
# sentence and not an empty screen.
#
# ‼ `pipefail` is not decoration: without it a failed `gh` feeding a filter
# reports the filter's status, which is how the first version of this scorer
# printed touches=0 for a merged pull request whose lookup had failed.
set -euo pipefail
export LC_ALL=C

fail() {
  echo "ERROR: $*" >&2
  exit 2
}

since="${1:-}"
[ -n "${since}" ] || fail "usage: $0 YYYY-MM-DD (the day the shadow rule merged)"
repo="${AB_SCORE_REPO:-FaultMaven/faultmaven}"
limit="${AB_SCORE_LIMIT:-500}"
python_bin="${PYTHON:-python}"

root="$(git rev-parse --show-toplevel 2>/dev/null)" ||
  fail "not inside a git clone; run this from a FaultMaven checkout"
[ -f "${root}/tests/wallclock/benchmarked_files.py" ] ||
  fail "${root} has no tests/wallclock/benchmarked_files.py; not a FaultMaven clone at or after the shadow rule"
cd "${root}"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

"${python_bin}" -m tests.wallclock.benchmarked_files > "${work}/closure.raw" ||
  fail "tests.wallclock.benchmarked_files failed (PYTHON=${python_bin}; is the dev environment installed?)"
sort -u "${work}/closure.raw" > "${work}/closure"
[ -s "${work}/closure" ] || fail "tests.wallclock.benchmarked_files printed nothing"

runs="$(gh run list -R "${repo}" --workflow benchmarks.yml --event pull_request \
  --created ">=${since}" --limit "${limit}" --json databaseId,headSha,createdAt \
  --jq 'sort_by(.createdAt) | reverse | .[] | [.databaseId, .headSha] | @tsv')" ||
  fail "gh run list failed (gh auth status? network? -R ${repo})"

listed=0 scored=0 skipped=0 errors=0 clean=0 false_reds=0 touching=0
seen=" "

printf '# run\tpr\ttouches\tcount_would_fail\tover_count\tmedian\n'
while IFS=$'\t' read -r id sha; do
  [ -n "${id}" ] || continue
  listed=$((listed + 1))

  pr="$(gh api "repos/${repo}/commits/${sha}/pulls" --jq '.[0].number // empty' 2>/dev/null)" || pr=""
  if [ -z "${pr}" ]; then
    printf 'ERROR\t%s\tno pull request found for commit %s\n' "${id}" "${sha}"
    errors=$((errors + 1))
    continue
  fi
  case "${seen}" in *" ${pr} "*)
    skipped=$((skipped + 1))
    continue
    ;;
  esac

  dir="$(mktemp -d -p "${work}")"
  if ! err="$(gh run download "${id}" -R "${repo}" --name benchmark-ab --dir "${dir}" 2>&1 > /dev/null)"; then
    case "${err}" in
      *"no artifact"*)
        skipped=$((skipped + 1))
        ;;
      *)
        printf 'ERROR\t%s\tartifact download failed: %s\n' "${id}" "${err}"
        errors=$((errors + 1))
        ;;
    esac
    continue
  fi
  if [ ! -f "${dir}/ab_report.json" ]; then
    skipped=$((skipped + 1))
    continue
  fi
  if ! evaluated="$(jq -r 'select(.gated == true and .count_would_fail != null)
      | [.count_would_fail, .over_count, .median] | @tsv' "${dir}/ab_report.json")"; then
    printf 'ERROR\t%s\tab_report.json is not readable\n' "${id}"
    errors=$((errors + 1))
    continue
  fi
  if [ -z "${evaluated}" ]; then
    # Not gated, or a report from before the shadow rule: nothing measured.
    skipped=$((skipped + 1))
    continue
  fi

  if ! files="$(gh api "repos/${repo}/pulls/${pr}/files" --paginate --jq '.[].filename' 2>/dev/null)"; then
    printf 'ERROR\t%s\tcould not list the files of pull request %s\n' "${id}" "${pr}"
    errors=$((errors + 1))
    continue
  fi
  touches="$(comm -12 "${work}/closure" <(printf '%s\n' "${files}" | sort -u) | wc -l | tr -d ' ')"

  IFS=$'\t' read -r would_fail over_count median <<< "${evaluated}"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "${id}" "${pr}" "${touches}" "${would_fail}" "${over_count}" "${median}"
  seen="${seen}${pr} "
  scored=$((scored + 1))
  if [ "${touches}" = "0" ]; then
    if [ "${would_fail}" = "false" ]; then clean=$((clean + 1)); else false_reds=$((false_reds + 1)); fi
  else
    touching=$((touching + 1))
  fi
done <<< "${runs}"

{
  echo "listed ${listed} A/B runs since ${since}; scored ${scored} pull requests (${skipped} runs skipped: older run of a scored pull request, no artifact, not gated or not evaluated), ${errors} errors"
  echo "touching no benchmarked code: ${clean} with count_would_fail false (these count toward the 20), ${false_reds} with it true (false reds: restart the count)"
  echo "touching benchmarked code: ${touching} (read their rows; not evidence either way)"
} >&2
[ "${errors}" -eq 0 ]
