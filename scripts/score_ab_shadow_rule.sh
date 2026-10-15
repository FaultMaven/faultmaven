#!/usr/bin/env bash
#
# Score the A/B shadow count rule from the `benchmark-ab` artifacts (#1989).
#
#   scripts/score_ab_shadow_rule.sh YYYY-MM-DD
#
# YYYY-MM-DD is the day the shadow rule merged: only A/B runs created on or
# after it are read. Run it from a FaultMaven clone at an UP-TO-DATE main with
# the dev environment installed (`PYTHON=...` picks the interpreter) and an
# authenticated `gh`: "benchmarked code" is classified against the clone's
# HEAD, so an old checkout scores today's pull requests against yesterday's
# closure. The promotion criterion it scores is in
# docs/development/testing/performance.md.
#
# One row per PULL REQUEST (the independent unit: a pull request's reruns and
# pushes share its code):
#
#   run  pr  touches  count_would_fail  over_count  median  heads  note
#
# * A pull request is red (`count_would_fail` true) if ANY of its evaluated
#   runs was red: a push after review must not erase an earlier false red.
#   `run`, `over_count` and `median` are the newest red run's, else the newest
#   evaluated run's.
# * `heads` is the head SHA (7 characters) of every run the row scored, newest
#   first. `note` says so when the pull request's NEWEST run was not scored (in
#   flight, no artifact, not gated), because `touches` is judged from the pull
#   request's current file list and that run's code is not in the row.
# * `touches` is how many of the pull request's changed files are benchmarked
#   code, the files a real `pytest tests/benchmarks -m benchmark` run imports
#   (tests/wallclock/closure_plugin.py; about 25 s, cached per HEAD sha under
#   ${AB_SCORE_CACHE:-~/.cache/faultmaven/ab-closure}, not cached on a dirty
#   tree). 0 counts toward the criterion.
# * A failed lookup is an `ERROR` row, never a 0, and any ERROR row makes the
#   exit status 1. Runs not yet completed are a named bucket in the summary
#   (`in_progress`), not errors. A bad date or limit exits 2. A summary goes
#   to stderr, so "no runs yet" is a sentence and not an empty screen.
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

[ "${BASH_VERSINFO[0]}" -ge 4 ] || fail "needs bash 4 or newer (associative arrays)"

since="${1:-}"
[ -n "${since}" ] || fail "usage: $0 YYYY-MM-DD (the day the shadow rule merged)"
[[ "${since}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] ||
  fail "'${since}' is not a date; expected YYYY-MM-DD"
[ "$(date -d "${since}" +%F 2>/dev/null)" = "${since}" ] ||
  fail "'${since}' is not a real calendar date"
repo="${AB_SCORE_REPO:-FaultMaven/faultmaven}"
limit="${AB_SCORE_LIMIT:-500}"
[[ "${limit}" =~ ^[1-9][0-9]*$ ]] || fail "AB_SCORE_LIMIT='${limit}' is not a positive integer"
python_bin="${PYTHON:-python}"

root="$(git rev-parse --show-toplevel 2>/dev/null)" ||
  fail "not inside a git clone; run this from a FaultMaven checkout"
[ -f "${root}/tests/wallclock/closure_plugin.py" ] ||
  fail "${root} has no tests/wallclock/closure_plugin.py; not a FaultMaven clone at or after #2014"
cd "${root}"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# The closure: what a real benchmark run imports, cached per HEAD sha.
head_sha="$(git rev-parse HEAD)"
cache_dir="${AB_SCORE_CACHE:-${XDG_CACHE_HOME:-${HOME}/.cache}/faultmaven/ab-closure}"
cache_file="${cache_dir}/${head_sha}"
dirty="$(git status --porcelain --untracked-files=no)"
if [ -z "${dirty}" ] && [ -s "${cache_file}" ]; then
  cp "${cache_file}" "${work}/closure.raw"
else
  echo "collecting the benchmark import closure from a real run (about 25 s; cached per HEAD)..." >&2
  rc=0
  FM_CLOSURE_OUT="${work}/closure.raw" "${python_bin}" -m pytest tests/benchmarks -m benchmark \
    -q --no-cov -p tests.wallclock.closure_plugin > "${work}/pytest.log" 2>&1 || rc=$?
  # 1 = some benchmark failed (the imports were still made); 2+ = collection
  # or internal error, so the import set is not trustworthy.
  if [ "${rc}" -gt 1 ]; then
    tail -n 20 "${work}/pytest.log" >&2
    fail "the benchmark run exited ${rc} (PYTHON=${python_bin}; is the dev environment installed?)"
  fi
  [ -s "${work}/closure.raw" ] ||
    fail "the benchmark run wrote no closure (PYTHON=${python_bin}; see: ${python_bin} -m pytest tests/benchmarks -p tests.wallclock.closure_plugin)"
  if [ -z "${dirty}" ]; then
    mkdir -p "${cache_dir}" && cp "${work}/closure.raw" "${cache_file}"
  fi
fi
sort -u "${work}/closure.raw" > "${work}/closure"

runs="$(gh run list -R "${repo}" --workflow benchmarks.yml --event pull_request \
  --created ">=${since}" --limit "${limit}" --json databaseId,headSha,createdAt,status,conclusion \
  --jq 'sort_by(.createdAt) | reverse | .[] | [.databaseId, .headSha, .status] | @tsv')" ||
  fail "gh run list failed (gh auth status? network? -R ${repo})"

listed=0 in_progress=0 no_artifact=0 not_evaluated=0 evaluated=0 errors=0
declare -A pr_of_sha=()            # head sha -> pull request
declare -A pr_latest_note=()       # pr -> why the newest run was not scored ("" if scored)
declare -A pr_seen=()              # pr -> 1 once its newest run was classified
declare -A pr_red=()               # pr -> 1 once any evaluated run was red
declare -A pr_row_run=() pr_row_over=() pr_row_median=() pr_heads=() pr_any_eval=()
pr_order=()

printf '# run\tpr\ttouches\tcount_would_fail\tover_count\tmedian\theads\tnote\n'

note_latest() { # pr, text ("" = scored): only the pull request's NEWEST run speaks
  if [ -z "${pr_seen[$1]:-}" ]; then
    pr_seen[$1]=1
    pr_latest_note[$1]="$2"
    pr_order+=("$1")
  fi
}

while IFS=$'\t' read -r id sha status; do
  [ -n "${id}" ] || continue
  listed=$((listed + 1))

  pr="${pr_of_sha[${sha}]:-}"
  if [ -z "${pr}" ]; then
    pr="$(gh api "repos/${repo}/commits/${sha}/pulls" --jq '.[0].number // empty' 2>/dev/null)" || pr=""
    [ -z "${pr}" ] || pr_of_sha[${sha}]="${pr}"
  fi

  if [ "${status}" != "completed" ]; then
    # Not finished: nothing to download yet, and not an error.
    in_progress=$((in_progress + 1))
    [ -z "${pr}" ] || note_latest "${pr}" "newest run ${id} (${sha:0:7}) is ${status}, not scored"
    continue
  fi
  if [ -z "${pr}" ]; then
    printf 'ERROR\t%s\tno pull request found for commit %s\n' "${id}" "${sha}"
    errors=$((errors + 1))
    continue
  fi

  dir="$(mktemp -d -p "${work}")"
  if ! err="$(gh run download "${id}" -R "${repo}" --name benchmark-ab --dir "${dir}" 2>&1 > /dev/null)"; then
    case "${err}" in
      *"no valid artifacts"* | *"no artifact"*)
        # `gh` says the first when the run has no artifacts at all (cancelled
        # early), the second when it has others but not this one.
        no_artifact=$((no_artifact + 1))
        note_latest "${pr}" "newest run ${id} (${sha:0:7}) has no benchmark-ab artifact, not scored"
        ;;
      *)
        printf 'ERROR\t%s\tartifact download failed: %s\n' "${id}" "${err}"
        errors=$((errors + 1))
        ;;
    esac
    continue
  fi
  if [ ! -f "${dir}/ab_report.json" ]; then
    no_artifact=$((no_artifact + 1))
    note_latest "${pr}" "newest run ${id} (${sha:0:7}) has no ab_report.json, not scored"
    continue
  fi
  if ! report="$(jq -r 'select(.gated == true and .count_would_fail != null)
      | [.count_would_fail, .over_count, .median] | @tsv' "${dir}/ab_report.json")"; then
    printf 'ERROR\t%s\tab_report.json is not readable\n' "${id}"
    errors=$((errors + 1))
    continue
  fi
  if [ -z "${report}" ]; then
    # Not gated, or a report from before the shadow rule: nothing measured.
    not_evaluated=$((not_evaluated + 1))
    note_latest "${pr}" "newest run ${id} (${sha:0:7}) was not gated or predates the rule, not scored"
    continue
  fi

  IFS=$'\t' read -r would_fail over_count median <<< "${report}"
  evaluated=$((evaluated + 1))
  note_latest "${pr}" ""
  pr_any_eval[${pr}]=1
  pr_heads[${pr}]="${pr_heads[${pr}]:+${pr_heads[${pr}]},}${sha:0:7}"
  # Runs arrive newest first: the first red, and failing that the first
  # evaluated run, represents the pull request.
  if [ "${would_fail}" = "true" ] && [ -z "${pr_red[${pr}]:-}" ]; then
    pr_red[${pr}]=1
    pr_row_run[${pr}]="${id}" pr_row_over[${pr}]="${over_count}" pr_row_median[${pr}]="${median}"
  elif [ -z "${pr_row_run[${pr}]:-}" ]; then
    pr_row_run[${pr}]="${id}" pr_row_over[${pr}]="${over_count}" pr_row_median[${pr}]="${median}"
  fi
done <<< "${runs}"

scored=0 clean=0 false_reds=0 touching=0
for pr in ${pr_order[@]+"${pr_order[@]}"}; do
  [ -n "${pr_any_eval[${pr}]:-}" ] || continue
  if ! files="$(gh api "repos/${repo}/pulls/${pr}/files" --paginate --jq '.[].filename' 2>/dev/null)"; then
    printf 'ERROR\t%s\tcould not list the files of pull request %s\n' "${pr_row_run[${pr}]}" "${pr}"
    errors=$((errors + 1))
    continue
  fi
  touches="$(comm -12 "${work}/closure" <(printf '%s\n' "${files}" | sort -u) | wc -l | tr -d ' ')"
  would_fail=false
  [ -z "${pr_red[${pr}]:-}" ] || would_fail=true
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${pr_row_run[${pr}]}" "${pr}" "${touches}" "${would_fail}" \
    "${pr_row_over[${pr}]}" "${pr_row_median[${pr}]}" "${pr_heads[${pr}]}" "${pr_latest_note[${pr}]}"
  scored=$((scored + 1))
  if [ "${touches}" = "0" ]; then
    if [ "${would_fail}" = "false" ]; then clean=$((clean + 1)); else false_reds=$((false_reds + 1)); fi
  else
    touching=$((touching + 1))
  fi
done

{
  if [ "${listed}" -ge "${limit}" ]; then
    echo "WARNING: the listing hit AB_SCORE_LIMIT=${limit}; older runs since ${since} may be missing. Raise AB_SCORE_LIMIT."
  fi
  echo "listed ${listed} A/B runs since ${since}: ${evaluated} evaluated across ${scored} pull requests, ${in_progress} in_progress (not completed, not scored yet), ${no_artifact} completed with no artifact, ${not_evaluated} not gated or not evaluated, ${errors} errors"
  echo "touching no benchmarked code: ${clean} with count_would_fail false (these count toward the 20), ${false_reds} with it true (false reds: restart the count)"
  echo "touching benchmarked code: ${touching} (read their rows; not evidence either way)"
} >&2
[ "${errors}" -eq 0 ]
