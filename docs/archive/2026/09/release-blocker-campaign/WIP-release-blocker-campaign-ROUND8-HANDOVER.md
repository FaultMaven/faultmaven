# HANDOVER → ROUND 8 (written 2026-09-02, end of round 7)

You are the PM and QC owner for the next round of FaultMaven's release-blocker campaign.

Main is **`6b3592bf5`** — **re-check `origin/main` immediately before branching, not an hour
earlier.** It moved TWICE during round 7 (a peer workstream landed #1301 and #1302 mid-round) and
only a re-fetch caught it. GitHub builds `refs/pull/N/merge` against the tip; a stale base costs a
full round.

Read `docs/working/WIP-release-blocker-campaign-ROUND7.md` first (gitignored, local, 700 lines).
**Treat every status line as stale.** `gh issue view` is BROKEN on these repos — use
`gh api repos/FaultMaven/faultmaven/issues/<n>`.

## State handed over

Round 7 shipped three merged PRs closing four issues, all reconciled:
| PR | Issues | Merge commit |
|----|--------|--------------|
| #1297 | #1234 | `919d2ab41` |
| #1298 | #942 | `59f0e76e1` |
| #1300 | **#1278 (the last P0)** + #1292 | `bc62febc0` |

**The P0 pool is now EMPTY — 0 open P0s** (verified by client-side enumeration with a label
control). P1 = 14, P2 = 26, P3 = 13. This changes the round's character from blocker triage to P1
debt, and it means priority alone no longer picks the round — ROOT grouping and measured harm do.

Every lane needed exactly TWO rounds. **7 for 7 across rounds 6 and 7**: every lane was fully
CI-green in round one and still shipped a defect `/code-review <pr> high` caught, and most were a
defect in the very guard the PR was written to install.

## FIRST TASK: the cluster is THREE main commits behind and nothing is in flight

```
cluster faultmaven-api  = ghcr.io/faultmaven/faultmaven:sha-4558959   (round-6 base)
both CronJobs           = sha-4558959
origin/main             = 6b3592bf5
image sha-6b3592b       = EXISTS in GHCR (publish-docker success 18:14:57Z)
open infra PRs          = 0   (control: query returned 0 of 0, not an empty-read)
```

So **none of round 7's four fixes are deployed**, and no promotion PR exists. Open one against
`FaultMaven/faultmaven-enterprise-infra` promoting `sha-4558959` → `sha-6b3592b`, get the
Environment approval, and verify the deployment AND both CronJobs afterwards.
**Do NOT hand-edit `kubernetes/apps/faultmaven/overlays/onprem/kustomization.yaml` image tags** —
that is `promote-images.yaml`'s artifact; editing it directly makes the promotion a silent no-op
and looks exactly like broken CI. **CD auto-applies on infra merge.**
Then confirm live: the turn-budget fix (#1300) is the one worth eyeballing, because the cluster
config is `LLM_PROVIDER_TIMEOUT_OVERRIDES gemini=120` / `AGENT_PROVIDER_TIMEOUT_OVERRIDES
gemini=240` — a shape the ladder previously could not fit. `/admin/config/status` now publishes
`llm_retry_ladder_fits_turn_budget`; it should report **False** on that config, with the honest
503 path in effect. **A False there is the fix WORKING, not a regression.**

## HOLD — the held area MOVED at the very end of round 7. Re-derive before assuming.

`6b3592bf5` is **#1302, "turn the KB cause seeder off by default, measured (#1295)"**, merged
17:33:08 by a PEER workstream (12 files, +376/-60).
- **#1295 is STILL OPEN with NO `closed` event.** Its PR title uses `(#1295)` — the same
  non-closing-keyword pattern that stranded #1195 and #1293. Resolve which of the three cases it
  is using `[[open-issue-may-be-a-deliberate-hold]]`: accidental stale-open, deliberate owner hold,
  or referenced-but-unfixed.
- **#1293's premise HAS CHANGED and must be re-measured before it is laned.** With push-seeding
  OFF BY DEFAULT, "16 of 51 labelled-negative pairs admit and 6 seed" is no longer a
  shipped-default harm. Round 7 recorded a cycle (#1295 blocked on #1293's fix; #1293 blocked on an
  owner precision/recall call that "costs 3 correct queries to save 6"). **That cycle may now be
  cut — verify, do not assume.**
- #1288 / #1167 / #1168 remain open. Re-derive whether the hold still applies now that the seeder
  is off; #1167/#1168 are tenant-isolation issues and may never have been in #1295's blast radius.

## CANDIDATES — this is NOT the round. MEASURE FIRST, then bring the owner 2-3 lanes by ROOT.

**Already measured in round 7 (do not re-derive; DO re-verify what you build on):**
- **#947** (P1) — free deletion. PM-verified: **778 passed with the conftest, 778 with it deleted**
  (`-1478/+0`, zero effective consumers in 13,304 collected; every fixture it defines has 0
  external references). Anchors drift −25 (file is 1478 lines, issue says 1503). **Brief it as
  DELETION.** The issue's own first-listed fix direction ("point the import at wherever those
  routers live now") would newly execute ~1160 lines of never-run fixture code for ZERO consumers.
- **#828** (P1, security-correctness) — reproduces (two-process restart probe: both the JTI and the
  user watermark are lost; fakeredis ACKs `SAVE` and persists nothing). **NOT lane-ready — needs an
  owner direction decision.** The revocation check runs during token validation, BEFORE any tenant
  is bound, and `_is_revoked` is FAIL-OPEN by design (`auth_service.py:619-644`). A new table
  following the repo's DEFAULT RLS pattern would read empty on the auth path → **revocation
  silently stops working in Cloud**. Migrations 038/039 both carry
  `# No ENABLE ROW LEVEL SECURITY here, on purpose` for exactly this reason. 031 dropped the old
  table for having NO WRITER, not because durability was rejected — so this is not re-litigating
  031, but 031's principle (replace the store, never add a second) IS binding. ~300-500 LoC + a
  migration.
- **#752** (P1) — reproduces. **`include_deleted` has NO REFERENT**: cases are hard-deleted
  (`models.py:31-33`), `CaseState` has no deleted member, and `MinimalCaseService` "honours" it by
  excluding CLOSED — conflating a disposition with a deletion. Honest fix implements
  `include_terminal`, **REMOVES** `include_deleted`, and fixes the fallback divergence
  (`_container_impl.py:995,:1003`). Blocked on a **cross-repo consumer check** — making
  `include_terminal=False` work drops resolved/closed cases a live endpoint returns today
  (dashboard/copilot).

**Unmeasured P1s worth triaging (round 7 never got to these):**
`#936` fm-reset-kb resolves the Chroma dir from `get_project_root`, not `CHROMADB_KB_PERSIST_DIR`
(a DESTRUCTIVE CLI path bug — likely NOT in #1295's blast radius since it is path resolution, not
retrieval semantics) · `#907` KnowledgeIngester's `CHROMADB_URL` branch sends no auth token ·
`#918` turn-stamp `last_suggestions` so the resolver cannot match choices answered out-of-band ·
`#1170` agent no longer requests jwt-token-decode on the IRSA scenario, primary evidence coverage
80%→60% · `#908` benchmark latency assertions have ~5% headroom against ±30% runner variance ·
`#1206` Cloud dashboard cannot show/change the shipped role pins · `#512` evidence recycling
freshness validator · `#1169` narrow `OAUTH_REDIRECT_URI_PATTERNS` (a listed beta gate).

**A possible ROOT grouping to TEST, not to implement:** #936 and #907 both look like "the KB
infrastructure path takes its configuration from the wrong source." They may be two roots in one
subsystem rather than one root — round 7's #1122/#1272 and #1278/#1292 went opposite ways on
exactly this question, so measure it.

## Check for stale-open issues BEFORE working any of them

Round 7 found THREE and each needed different action — see
`[[open-issue-may-be-a-deliberate-hold]]`. Current suspects:
- **#982** — **ALREADY FIXED, recommend CLOSE.** Landed under two OTHER PR numbers (#997 deleted
  the shadow stack, #1005 removed the residue) and migration `041_drop_agent_executions` dropped
  both tables. Verified with positive controls: `AGENT_LLM_PROVIDER` 0 hits, `create_llm_client` 0,
  `claude-sonnet-4-20250514` 0 in `faultmaven/`, `llm_client.py` absent; control `LLMRouter` = 18.
  The single `AgentOrchestrationService` match is past-tense prose in `modules/agent/README.md`.
  **Not dead code — not code at all.**
- **#1117 / #1118** (P2) — **FIXED, deliberately left open.** PR #1125 (`389d9a58a`, ancestor of
  main) says verbatim `Refs #1117, #1118 (not auto-closing — owner decides).` Mutation-proved
  (3/1455, 24/1455, 24/141). Owner queue item, not a lane.
- **#1295** — see HOLD above.
- **#710** (P3) — still open, premise NARROWED not changed. Leave at P3.

## THE RULE (put in every dispatch brief, verbatim)

Fix the issue at its ROOT, however many rounds it takes. File an issue only for what you uncover
that is genuinely UNRELATED — a different subsystem, a different repo, or a decision the owner must
make — and "different repo" is not an escape hatch. "Found a related defect, filed as #N" is a
prompt to EXTEND the lane, not a deliverable. Lanes report candidates to the PM; the PM decides
what gets filed. Lanes merge nothing.

## How to work

One control agent (you) plus one Opus subagent per lane, each with `isolation: "worktree"` and its
OWN scratchpad subdirectory (they collide silently otherwise). Branch off current `origin/main`.

**BUDGET THE REVIEW ROUND AS WORK — `/code-review <pr> high` on every lane.** 7 for 7 across two
rounds: CI-green in round one, defect caught in review, usually inside the guard the PR installed.
Read every new guard adversarially: what shape satisfies this check while violating its intent —
empty vs disjoint, substring vs token, one element vs many, a floor set below the real population?
Be suspicious of success criteria stated as a count of zero or a single fixture.

**Your own briefs will be wrong.** Mine were refuted EIGHT times in round 7, including a case where
I called a correct number wrong (#1292's "38" — there were two boundaries and the issue's was right
for the metric that matters). **Tell lanes to MEASURE your hints rather than implement them**, and
say so explicitly in the brief.

**Substantiate every review finding by execution against the real head before relaying** — in round
7 a review's headline finding was overstated (it claimed lost retries; measurement showed none) and
I caught it only by replaying the branch's own planner against its own gate. Relay the corrected
version and tell the lane NOT to implement the wrong remedy.

Tests must provably fail before the fix, demonstrated by REVERTING, not asserted. For invariants
that pass on both sides by construction, use a targeted mutation — and confirm the mutation BITES,
because a broken mutation reads as a safe guard (this happened to a lane AND to me in round 7).
Restore from `cp` backups, never `git checkout <path>`. Never `git add -A`. You merge nothing.

## Gates — paste verbatim. THERE ARE FIVE, NOT FOUR.

```
black --check faultmaven/ tests/
ruff check faultmaven/ tests/ --select E9,F63,F7,F82,I
lint-imports --config .importlinter
python scripts/generate_api_docs.py --check
pytest tests/unit/architecture/
```
The fifth is new to this list and it cost round 7 two CI failures: **two independent lanes had all
four of the others clean and CI still failed** on
`tests/unit/architecture/test_architecture_boundaries.py::test_api_layer_boundaries`, an AST scan
over `faultmaven/api/**` stricter than and independent of the `.importlinter` contracts.
`lint-imports` passed throughout in both cases. `test_config_purity.py` and
`test_configuration_compliance.py` in the same directory bite whenever a settings field is added or
constrained.
**Do NOT run isort** — CI doesn't, ruff's `I` disagrees with it, and it will fail CI.
Then `pytest tests/` — CI runs the whole tree, so a unit-only run misses real breaks.

## Traps already paid for

- **A failed probe reads as a pass — in BOTH directions.** Assert a positive control before any
  "none found"/"clean" claim AND before any "caught"/"fixed" claim. In round 7 my scanner probe was
  missing an argument and reported every case as CAUGHT, which would have made me wrongly reject a
  correct finding. `--timeout` is fatal (pytest-timeout is NOT installed: dies instantly, launcher
  exits 0). A wrong path collects 0 and looks green — report COLLECTED COUNTS. A malformed
  `gh --jq` returns empty and reads as an empty pool; a `comm` on unsorted input errors and prints
  nothing, which reads as "no overlap". Both hit me this round.
- **A probe that RUNS CORRECTLY can still be vacuous** — a universal over an empty set is trivially
  true. Distinguish SATISFIED / NOT SATISFIED / COULD NOT ASK.
- **Enumerate the issue pool CLIENT-SIDE** (fetch all open issues, filter labels in code) with a
  label-existence control first. A label-filtered `--jq` query silently returned an empty pool once.
- `nohup ... &` returns a WRAPPER pid that exits immediately while pytest runs under another.
  Confirm liveness against the real pytest PID (`ps -p <pid>`), and read completion off a real
  summary line. `pkill -f` / `pgrep -f` match your own shell — kill by port or PID.
- **Check for CROSS-LANE FILE COLLISIONS before dispatch and again before merge.** Round 7's L1 and
  L2 both modified `admin_config.py` and its test file, and L1 added a FIFTH member to the very
  `features` dict L2 was auditing with an exhaustiveness guard. Compare `pulls/N/files` between
  every pair, with sorted inputs and a positive control.
- A green workflow badge does not mean its steps ran — check step level. The CI/CD Pipeline's build
  job is `push: false`; `publish-docker.yml` publishes, chained by `workflow_run`.
- **A PR's CI being green may be on its PRE-REVIEW head.** Check the head SHA the checks ran
  against before calling a reworked lane green.
- Verify the branch before trusting a failure — the shared checkout moves under you, and
  `/code-review` leaves it on its own branch. The infra repo's checkout usually sits on another
  workstream's branch: use a worktree, never check out there.
- `Closes #<own issue>` only. **Reconcile what closed against what was fixed after every merge**,
  with a control issue that should still be open.
- Cross-repo refs have no safe bare form: always `owner/repo#N`.
- Force the tree in probes: `PYTHONPATH=<worktree>` and assert `faultmaven.__file__` is inside it.

## Box state

Serial full-suite baseline on the round-7 merged tree: **13,373 passed / 0 failed / 119 skipped**
(~36m). Compare whole-suite to whole-suite in the SAME mode, never against "0"; whole-suite xdist
runs flake ~5 here with a SHIFTING set, so compare run to run rather than subtracting a list.
`tests/integration/test_main_app.py::test_application_uses_configuration_defaults` and
`tests/unit/api/test_composition_root.py::test_app_state_has_services_after_startup` are both
ORDER-DEPENDENT — they pass in the full suite and fail standalone. Pre-existing, not regressions.
Under fleet contention (3+ concurrent suites) re-run any timing-shaped failure in isolation before
calling it a regression. `kubectl` works from this box against the on-prem cluster (read-only
introspection is fine). Many stale worktrees are registered and most belong to other workstreams —
**don't prune them.** A PEER WORKSTREAM is active in this repo (it landed #1301 and #1302 during
round 7) — expect main to move under you and expect its pytest processes on the box; leave them
alone.
