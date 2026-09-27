#!/usr/bin/env bash
# ci_required.sh — the latest conclusion of every required CI context for a commit.
#
# PROVES: whether a SHA is actually safe to build on top of — every status
# check a merge rule requires has RUN and SUCCEEDED, not merely "nothing red
# showing yet". `gh pr checks` and the GitHub UI both go quiet while a
# required check is still queued or hasn't been scheduled, which reads
# identically to "passing" to a script that only greps for failure text.
# This instead prints one line per required context (its latest conclusion,
# by started_at, so a re-run supersedes a stale failure) and a single
# GREEN/PENDING/RED verdict that is never GREEN while a context is absent,
# queued, in_progress, pending, waiting or requested.
#
# USAGE:
#   ci_required.sh <sha> [--repo OWNER/NAME] [--contexts "Name1,Name2,..."]
#
#   --repo      defaults to `gh repo view --json nameWithOwner`, i.e. the repo
#               of the current working directory's checkout.
#   --contexts  comma-separated required context names. Defaults to the
#               contexts documented below, which were correct for this repo's
#               rulesets at the time this script was written; a ruleset can
#               change independently of this file, so pass --contexts (or set
#               CI_REQUIRED_CONTEXTS) rather than trust the default once it is
#               stale. To read the live list yourself:
#                 gh api "repos/<owner>/<repo>/rulesets" --jq '.[].id' | \
#                   xargs -I{} gh api "repos/<owner>/<repo>/rulesets/{}" \
#                     --jq '.rules[]? | select(.type=="required_status_checks")
#                           | .parameters.required_status_checks[]?.context'
#
# EXIT CODES: 0 verdict GREEN, 1 verdict RED, 2 verdict PENDING (a poll loop
# should keep waiting), 3 usage error (no <sha>, or --repo could not be
# determined).
set -uo pipefail

DEFAULT_CONTEXTS="Test Cloud,Test Standalone,Environment Smoke Check,Code Quality Checks,Test PostgreSQL Integration,Architecture Boundary Check,API Contract Drift Check,Test Packaging Configuration,build,Security Scanning,CodeQL"

usage() {
  echo "Usage: $0 <sha> [--repo OWNER/NAME] [--contexts \"Name1,Name2,...\"]" >&2
  exit 3
}

[ $# -ge 1 ] || usage
sha=$1; shift
repo=""
contexts_csv="${CI_REQUIRED_CONTEXTS:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo=$2; shift 2 ;;
    --contexts) contexts_csv=$2; shift 2 ;;
    *) usage ;;
  esac
done

if [ -z "$repo" ]; then
  repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) || {
    echo "error: could not determine the repo (pass --repo OWNER/NAME)" >&2
    exit 3
  }
fi
[ -n "$contexts_csv" ] || contexts_csv=$DEFAULT_CONTEXTS
IFS=',' read -r -a REQ <<< "$contexts_csv"

json=$(gh api "repos/$repo/commits/$sha/check-runs?per_page=100" --paginate --jq '.check_runs[] | {name, status, conclusion, started_at}' 2>/dev/null)
verdict=GREEN
for c in "${REQ[@]}"; do
  st=$(echo "$json" | jq -rs --arg n "$c" '[.[] | select(.name==$n)] | sort_by(.started_at) | last | if . == null then "absent" else (.conclusion // .status) end')
  printf "  %-30s %s\n" "$c" "$st"
  case "$st" in
    success|skipped|neutral) ;;
    absent|queued|in_progress|pending|waiting|requested) [ "$verdict" = GREEN ] && verdict=PENDING ;;
    *) verdict=RED ;;
  esac
done
echo "VERDICT $sha ($repo): $verdict"
case "$verdict" in
  GREEN) exit 0 ;;
  RED) exit 1 ;;
  *) exit 2 ;;
esac
