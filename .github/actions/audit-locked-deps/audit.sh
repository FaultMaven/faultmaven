#!/bin/bash
# The fail-closed audit of core's locked dependencies, run by the
# audit-locked-deps action: Security Scanning on every pull request and merge
# group (ci-cd.yml), and the daily Scheduled Dependency Audit of main
# (scheduled-audit.yml, #2218). One copy, so both gates read the same ignores.
#
# Fail-closed dependency audit (#969). pip-audit exits non-zero on any
# finding — no report parser, no `|| true` — and audits the declared
# locked dependency graph (requirements/*.txt: a superset of what any
# artifact installs; the Dockerfile strips GPU pins, so an advisory can
# fire on an audited-but-unshipped package — anchor those to a tracking
# issue), not the runner's venv.
#
# Ignore policy: every --ignore-vuln below MUST cite an entry in
# docs/operations/security/vulnerability-exceptions.md or a tracking
# issue; an ignore without a documented anchor should be rejected in
# review. Remove the ignore when its entry's escalation trigger fires.
#
# One invocation over every lockfile. The three files pin identical
# versions for every package they share, so nothing needs a per-file
# exception and pip-audit's single merged resolution is exact. If a
# future lockfile has to sit on a vulnerable pin the others have moved
# off, split the run back out per scope rather than widening an ignore
# to all three — a global ignore would hide the regression elsewhere.
IGNORES=(
  # chromadb pre-auth code injection — no patched release exists;
  # unreachable in standalone, contained in cloud. pip-audit prints
  # this one under its PYSEC alias (PYSEC-2026-311); it matches
  # aliases, so the GHSA id below is what suppresses it — do not
  # "correct" the id to whatever the log shows.
  # → vulnerability-exceptions.md "chromadb"  (fm#902 is closed; the
  #   entry, not the issue, is this flag's anchor — same as the three
  #   below, so all four cite the same place)
  --ignore-vuln GHSA-f4j7-r4q5-qw2c
  # Three further chromadb server advisories, published 2026-06-12
  # and ingested into the audit feed on 2026-08-24: authenticated
  # code injection via UPDATE_COLLECTION (GHSA-36p7, Critical) and
  # two cross-tenant authorization failures in the server's authz
  # layer (GHSA-2wm9, GHSA-xph7, High). Same containment as above —
  # the vulnerable HTTP server never starts in standalone, and in
  # cloud it sits behind TWO independent, live-verified controls
  # (2026-08-25, infra#253 / fm#1173): the NetworkPolicy pair
  # (ClusterIP only, ingress restricted to the auth proxy, which
  # admits four first-party pods; egress default-denied) and the
  # token-enforcing chromadb-auth-proxy — required because the
  # ChromaDB 1.x server itself has no auth to enable. The two
  # authorization CVEs stay moot: the proxy's single-token gate
  # creates one all-access principal, no ChromaDB tenants or RBAC —
  # tenancy is enforced above ChromaDB and never delegated to it.
  # The exception entry is the authoritative containment record.
  # Reported under CVE aliases (CVE-2026-45833 / -45830 / -45831);
  # as above, ignore by GHSA id — do not "correct" these to the ids
  # the log prints.
  # → vulnerability-exceptions.md "chromadb"
  --ignore-vuln GHSA-36p7-vc44-83pf
  --ignore-vuln GHSA-2wm9-hf6c-p5cr
  --ignore-vuln GHSA-xph7-9rjv-w5fr
  # lupa sandbox escape — no upstream fix (2.7 and 2.8 are build
  # and packaging releases); Lua bodies are module string constants,
  # no user input reaches them. PRE-ARMED, NOT LOAD-BEARING: the
  # advisory range stops at 2.6 while we pin 2.8, so pip-audit has
  # nothing to report and this flag currently suppresses nothing.
  # Keep it anyway — if the range is corrected to cover 2.8, this
  # must not red the gate on a risk already accepted. Do not delete
  # it for being inert; see the doc section on why the scanners are
  # quiet (which is also why lupa has no Dependabot alert to watch).
  # → vulnerability-exceptions.md "lupa"
  --ignore-vuln GHSA-69v7-xpr6-6gjm
)
# Glob-built so a future lockfile cannot silently escape the audit.
REQS=()
for f in requirements/*.txt; do
  # An unmatched glob leaves the literal pattern as one iteration.
  # With a single list that would make REQS non-empty and the guard
  # below silent, so the -f test is what keeps it able to fire. (The
  # two-list version this replaced happened to survive that case for
  # an unrelated reason: the literal fell into the non-cloud arm, so
  # the CLOUD side was empty and its guard tripped. Neither version
  # was ever fail-open — this one just does not depend on the
  # accident.)
  [ -f "$f" ] || continue
  REQS+=(-r "$f")
done
# Fail closed on an empty list: pip-audit with no -r audits the
# runner's venv and would report a cheerful green having checked
# nothing.
if [ ${#REQS[@]} -eq 0 ]; then
  echo "::error::no lockfiles matched requirements/*.txt — pip-audit would audit the runner venv instead"
  exit 1
fi
# The OSV service, not pip-audit's default PyPI feed. PyPI's feed is a lagging,
# incomplete copy of OSV's records, for GHSA-* and PYSEC-* ids alike:
# GHSA-cx6f-vjhg-g3cc (virtualenv) was still missing from it about 20 hours
# after Dependabot raised it, and mako 1.3.12 lacked both GHSA-5639 and
# PYSEC-2026-4243 (#2124). OSV had both, and it still reports PYSEC-2026-4322
# (brotli, #2216), which reached the PyPI feed first. OSV returns a GHSA and a
# PYSEC record per advisory and pip-audit 2.10.1 keeps both, so its "ignored"
# and "Found" counts double-count; the ignores still suppress each accepted
# advisory under every id.
# One bounded retry: a transient OSV error is otherwise indistinguishable from
# a finding. Real findings fail twice.
pip-audit --vulnerability-service osv --no-deps "${REQS[@]}" "${IGNORES[@]}" || {
  echo "pip-audit failed — retrying once in case of transient service error"
  sleep 30
  pip-audit --vulnerability-service osv --no-deps "${REQS[@]}" "${IGNORES[@]}"
}
