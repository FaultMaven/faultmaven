# Release-blocker campaign — ROUND 6 (dispatched 2026-08-31)

> Every status line here is stale on your next read. Re-verify with `gh api`
> (`gh issue view` is broken on these repos).

## Base
Lanes branch off **`915b8d1c9`** (#1279, "a known observation time answers the timing question").
Main moved from `dbadfbdbe` → `915b8d1c9` DURING planning — caught by re-fetching immediately
before dispatch. #1279 touches only `templates.py` + one test; no lane collision, but it moved
the `root_node_ref` mandate anchor **1625 → 1634** (R3's brief was corrected).

Gates on `915b8d1c9`: black clean, ruff clean, **13 import-linter contracts kept**.
Full-suite baseline on `dbadfbdbe` (one commit earlier): **12,869 passed / 0 failed / 122 skipped /
1 deselected / 1 xfailed, 30m31s** — run **SERIALLY** (`-p no:randomly`, no xdist). Lanes must
compare whole-suite to whole-suite in the SAME mode, never against "0"; historical note says xdist
whole-suite runs flake ~5 here with a shifting set.

## Pool reconciliation
P0 open = 6. #1278 excluded by owner.

**#1195 is ALREADY FIXED — recommend CLOSE, not a lane.** PR #1199 (`68e6d2dbd`, merged
2026-08-27 05:28 UTC) is an ancestor of main. It was never closed because the PR title used
`(#1195)`, which is not a GitHub closing keyword — the timeline carries no `closed` event — and
today's P0 label sweep pulled it back into the pool. Both deliverables shipped: the carve-out at
the join (`assess_verification_status` → `VerificationStatus.RESTATEMENT_HELD`, 2 sites) and the
replacement affordance (`_restatement_held_suggestions` / `_restatement_held_pending`). 44 pins pass.
**Mutation-proved load-bearing:** in a separate worktree with PYTHONPATH forced and
`faultmaven.__file__` asserted inside it, reverting both carve-out returns to
`INSUFFICIENT_EVIDENCE` → **7 failed, 37 passed**, including the exact pin the issue demanded
(`test_insufficient_evidence_handoff_is_not_pending_while_a_root_is_held`).
Note `case_4d1b45632f27` (closed `insufficient_evidence`, NULL RCC) was created 2026-08-27 03:37 UTC,
~1h51m BEFORE #1199's commit — it predates the fix, it did not survive it.

## Hypotheses the owner asked to test — BOTH came back negative
1. **"#1122 and #1195 may be one lane."** REFUTED, by a route neither predicted: #1195 is done.
2. **"#1232 is the cleanest standalone lane."** REFUTED: it is cross-repo, deployment-coupled, and
   carries a mirror-image hazard the issue never mentions (see R2).

## LANES DISPATCHED — 4, each in its own worktree + scratchpad, all off `915b8d1c9`

### R1 — #1270  ROOT: progress is scored before every arm AND EVERY WRITER has run
Anchors: score `:5963`, unconditional assign `:5964`, transition check `:5968`, arm written
`:12102`/`:12144`, counter re-read `:6022`, turn_record persists the STALE value `:6081`,
predicate `:3841` with the `status_transitioned` arm at `:3898`.
**Corpus (dev DB, 253 cases / 2129 turns): 158 of 170 transition turns mis-scored (92.9%)**, every
visible arm empty on all 158. The 12 exceptions are fully explained — **12/12 carried an upload that
turn** (`novel_files_uploaded` fired independently) vs 6/158 of the failures. Current, not
historical: Jun 95/7, Jul 44/5, **Aug 19/0**. 110/253 cases carry an inflated `turns_without_progress`.
Detector rests on a code fact: `next_steps == ["Confirm problem statement and decide to investigate"]`
is emitted **iff** `state == INQUIRY`, and `progress_metrics` is computed AFTER `:5968` — so it
witnesses post-transition state while `progress_made` carries the pre-transition score.
**Charter widened past the issue** (Fable): the invariant is about WRITERS, not just arms.
Verified second writer — `:850` sets `metadata["progress_made"] = True` in the stage-gate compliance
path and `:5964` **clobbers it unconditionally**; `check_if_progress_made` never reads the key.

### R2 — #1232  ROOT: reclamation decides from a cache instead of the authority
Sweep is ARMED (`ORPHAN_CLEANUP_ENABLED: "true"`, `DRY_RUN: "false"`, infra `base/configmap.yaml:125-126`).
Issue locators STALE: real chain is `modules/agent/.../investigation_service.py:2179-2231`.
`uploaded_files` is RLS-tenanted and **FAIL-CLOSED** → unbound org yields zero rows → everything
looks unreferenced. Bypass = `faultmaven_maintenance` BYPASSRLS via `--cross-tenant-maintenance`;
`case_cleanup` is the exact template. `storage_ref` UNINDEXED → one SELECT into a set. `run.py:463`
already passes `container`; `storage_cleanup.run()` ignores it → no runner plumbing.
**HAZARD (PM-verified independently): 160 of 850 sweep candidates have NO `uploaded_files` row and
ALL 160 say `linked=true`.** The tidier rewrite ("DB is authority, delete what it doesn't reference")
deletes 160 objects on first armed run. Fix must be strictly ADDITIVE and fail closed.
*A data-loss change wearing a data-safety label.*
**Both merge orderings fail closed AND page** (`jobs/run.py` raises `JobTenantScopeError` for
flag-without-scope and scope-without-flag). Lane must check whether the CronJob image tag is pinned
(if so, one atomic infra PR; if not, pre-announce the window). **CD auto-applies on infra merge.**
Pre-armed-run gate is a 4-item checklist incl. the empty-referenced-set guard and the
referenced-fraction measurement of the 146 prod objects (an R2 EXIT CRITERION, not an owner punt).

### R3 — #1122  ROOT: novelty diluted by hypotheses that are not evidence of restatement
Reproduces byte-for-byte. Population: **5 of 29 would-validate roots held**, 5 cases.
`case_4d1b45632f27` = harm end-to-end (PM-verified: `closure_reason='closed_insufficient_evidence'`,
`root_cause_conclusion=None`).
**REFUTED, do not re-attempt:** (a) no threshold fix exists (0.30/0.15/0.12 hold+block; 0.11
releases+breaks #656); (b) the owner's option (b) fires **0/9** on real refusals (mutual Jaccard
0.075–0.276 vs a 0.8 bar) — refusals are ROOT SCARCITY, not duplication; (c) keying on
`seeded_from_runbook` is forbidden — that fixes #1272's symptom inside #1122's frame and leaves the
3 non-seeded holds held.
**CHARTER CORRECTED (Fable, verified):** both pinned fixtures are the **unattached** shape —
`_case()` builds hypotheses with no `root_node_id` — while all 5 real holds are **attached siblings
rooted elsewhere**. As originally written the charter could go green while releasing 0 real holds.
Added a **population release criterion** (distilled attached-sibling fixture from a real hold;
≥3 non-seeded holds must release).
**#1137 history added:** the `:249` docstring says *"An earlier cut of fm#1137 shipped one and this
fixture is what caught it"* — this flip was attempted and reverted. Lane has explicit permission to
return "lexical separators exhausted + sweep evidence" as a COMPLETE round-1 deliverable.
Unswept space noted: the prior sweep varied the novelty bar only; incident containment 0.889 vs
#656's 0.667, so a high one-way-containment bar was never tried.
**PM methodological error corrected:** I had used dev-DB absence to downweight the unattached shape
in R3 while calling dev-DB absence inconclusive for #1272. Same fact, two standards. The incident
case is not in the dev DB either. The pin stays.

### K — #1272  ROOT: the KB relevance score is unsound, and the gate can't tell a weak best from a real match
**Was going to be a read-only audit + a held fix lane. Owner and Fable both rejected that** —
re-verifying defects already found and deferring the fix is shrinkage wearing a rigorous hat, and
both arms land in the same ~20 lines of `_prefetch_kb_context`, so splitting violates same-seam.
**The issue's framing is contradicted by measurement and my numbers win:**
- NOT about Kubernetes: delete all 41 k8s+cloud runbooks and the target still ranks 37/50, 41/50.
- Domain gating impossible: `Kubernetes Node NotReady` and `Linux Disk Full` are BOTH `domain: compute`;
  within a perfect `service: linux` gate the target is **4th of 4** on 2 of 3 queries.
- "Not a content gap" is FALSE: `qemu` 0/91 runbooks, `libvirt` 0/91, `enospc` 3/91, `pid` 42/91.
- #710/`_compute_metadata_score` is NOT on the affected paths (`_rerank` runs only inside
  `hybrid_search`; sole caller `document_qa_tool.py:442`).
- **Lowering the floor is the WRONG LEVER:** `KB_PREFETCH_FETCH_LIMIT = 10` fetches the top-10
  CHUNKS before the floor applies; target chunk ranks 369/1297, 420/1297, 36/1297 — outside the
  window on all three. `KB_CONTEXT_MAX_ENTRIES = 3` then renders top-3 by the same score.
Defects to fix: `_rerank` discards the `final_score` it sorted by (returned `score` = raw cosine);
the `max(0.0,...)` clamp makes draft/stale/**deprecated** all 0.0 so deprecated is never demoted;
no IDF (`pid` counts as much as `enospc`); 35% of the weight budget inert (metadata 0.0 for ALL
1297 chunks, freshness spans 0.0215) and cosine's 0.40 weight buys 0.088 influence vs term-overlap's
0.25 buying 0.25 — **term overlap has ~2.8x the real influence**; IDENTIFIER_PATTERNS miss lowercase
technical nouns. Doc drift: CLAUDE.md claims "vector + BM25" (code says "Not BM25") and "HNSW cosine"
(no `hnsw:space` → L2 default).
Pass conditions: Q3 absent → **rank 1** (hybrid already achieves this, unwired); **Q2 seeds 0**
(today 8/91 clear the floor, ALL 8 wrong); Q1 stays 0. Query expansion IS in scope (ENOSPC verbatim
control hit rank 1/1297). Regression sweep MANDATORY. Recall side (chunking, indexing, and why the
literal title `"Linux Disk Full"` ranks only 3rd/91) investigated inside this lane.

## PM ACTIONS TAKEN
- **infra#268 corrected by COMMENT, not body edit** — it is a MERGED PR, so its body is the
  historical record. Clause 1 (missing `linked` key → orphan) TRUE; clause 2 (failed sidecar write →
  deletable) FALSE — `survey_sidecars` never enumerates a bare object, so it accumulates.
  https://github.com/FaultMaven/faultmaven-enterprise-infra/pull/268#issuecomment-5474850233
  This closes the second of the two artifacts #1232 named (the ConfigMap was already fixed by infra#275).

## OWNER QUEUE
- **Close #1195** as fixed by #1199 (evidence above). Held for the owner — not closed by the PM.
- Beta gates #1251/#1252/#1016/#1169/`FaultMaven/faultmaven-slack-agent#25` — unchanged.
- **KB content authoring**: `qemu`/`libvirt` in 0/91 runbooks. No ranking change reaches those
  queries; query expansion is lane K's, but writing virtualization coverage is a
  `faultmaven-kb-toolkit` call. NOTE this contradicts #1272's own "not a content gap" text.
- **#1232's 160 unreferenced objects**: whether to reclaim them is a data decision.

## THE RULE (in every brief, verbatim)
Fix at the ROOT, however many rounds. File only what is genuinely UNRELATED — a different
subsystem, a different repo, or a decision the owner must make. "Different repo" is NOT an escape
hatch. "Found a related defect, filed as #N" is a prompt to EXTEND the lane. Lanes report
candidates to the PM; the PM decides what gets filed.

---

# LANE OUTCOMES (live — re-verify, do not trust)

## R1 — #1270 — COMPLETE, PR #1280, CI 15/15 green (head `ca95a46a1`)
Branch `fix/1270-score-progress-after-every-arm-writer` off `915b8d1c9`. Body is `Closes #1270` only.
Re-score placed immediately after `_check_automatic_transitions` returns (NOT by moving the block);
both readings go through a new module-level **`score_progress`** which is MONOTONE, making the
`:850` stage-gate clobber unreachable rather than merely improbable.
**Lane extended twice on the same root** (correctly, not ticketed): the three engine-bypassing
routes scored in `_backfill_consumed_turn` but never wrote the reading back; and of the two
terminal-confirm branches only one passed `status_transitioned=True` — the other wrote it onto an
outer dict it never returns, so the arm was dead on arrival.
Fails-before: reverting all three fixes reds **8 of 20**, each on the verdict not a denominator.
Arm-genericity PROVED BY MUTATION: planting a different arm (`hypotheses_generated`) after the
final score on the fixed tree reds the ordering guard.
Corpus, denominator reported both sides (170 found / 170 replayed): **0 True → 170 True**.
Downstream: non-zero `turns_without_progress` 110 → 92; cases ever ≥3 (LOW) 72 → 62; ever ≥5 28 → 26.
Counter model positive-controlled against the persisted column: exact on 228/228.
Suite: **12,890 passed / 0 failed**, run TWICE on different heads (33m39s, 32m57s). Liveness read
off the real pytest PID, never the bash wrapper. Both harnesses exit 2 `COULD NOT ASK` on an empty
corpus rather than printing an unsupported zero.
PM-verified independently: the dead-arm claim, the momentum impact, PR mergeable, 15/15 success.

## R2 — #1232 — PRs open (#1281 + infra#283 draft); review round found DATA LOSS, now fixed
`/code-review 1281 high` returned 15 findings. PM substantiated the top five by execution.
**F1 (blocking, fixed):** the fail-closed guard was `if candidates and not referenced_refs` — only
TOTALLY empty. A NON-EMPTY set DISJOINT from the candidates passed it, and every candidate then
scored unreferenced → past-TTL unlinked objects deleted. Reachable: `knowledge_service.py:2179`
writes `storage_ref=str(file_path)` and `conversion_service.py:1516` writes `retained_path` —
absolute paths that can never match a backend key.
**PM independently mutation-verified the fix**: reverting `candidate_set & referenced_set` to the
emptiness test reds **6 tests across all three call sites**; the mutated run's own log shows
`deleted=1` with `referenced_refs_count=2`, i.e. the defect reproducing.
**F2 solved WITH F1, not against it:** dry runs always proceed (they delete nothing and their
counters are the diagnostic); live runs refuse unless `--allow-disjoint-reference-set`. So the
mandatory canary completes and a genuinely-empty install is not deadlocked.
**F3 extended, and the lane found a THIRD copy neither PM nor review named:**
`faultmaven/infrastructure/tasks/case_cleanup.py`, the APScheduler in-process path, unguarded on a
6-HOUR UNATTENDED TIMER — PM-verified as real on base main (0 guards). Guard lifted into the shared
`faultmaven/jobs/reference_set.py` rather than bolted onto one job.
The lane's own `UNRELATED_REFS` fixture WAS the F1 shape — a non-empty set chosen to dodge the
emptiness guard; six existing tests failed the moment the guard became correct.
F4 confirmed: deleting the two published operator signals previously redded NOTHING.
F14 declined as MUST-NOT-FIX (filtering to case-bound rows shrinks the protected set = a deletion
wearing a tidy-up label; `case_id` is nullable). F7: added DISTINCT, argued IN PLACE against the
`LIMIT` a perf reviewer would reach for, since a dropped row becomes a file believed unreferenced.
Infra#283 PM-verified: explicit ⛔ merge-blocking box + draft; ConfigMap diff adds ONLY comments
(filtered the diff — no key/value changed), so the render really is byte-identical.
Outstanding: full serial suite on `a0e1c9d65` (13,054 collected) + CI step-level confirmation.

## R3 — #1122 — running.  ## K — #1272 — running.

## FILED BY PM THIS ROUND
- **#1284** — `hypotheses_validated` is a progress arm with NO WRITER; five consumers read a
  permanently-empty list. Non-empty on **0 of 2,129** turns (control: siblings 537/255/211/176).
  `_calculate_momentum` sums it into `total_progress`, so HIGH/MODERATE bands run on 2 of 3
  declared inputs and their thresholds were calibrated with one summand silently zero.
  Owner decision (give it a writer = the scoring policy #1136 narrowed on purpose; retire it =
  touches arm set, persisted schema, five readers). #1142 checked, NOT a duplicate.
  R1's weaker second observation (INQUIRY path writes no arms at all) folded in as context.

## PM REVIEW ROUNDS
- #1281: done, 15 findings, F1 was data loss. #1280: `/code-review 1280 high` RUNNING.

---

# ROUND 6 CLOSED — all four lanes complete, all 15/15 green, all mergeable
`origin/main` still `915b8d1c9` (no drift during the round).

| PR | Lane | Head | Checks |
|----|------|------|--------|
| #1280 | R1 — #1270 | `622292808` | 15/15 |
| #1281 | R2 — #1232 | `a0e1c9d65` | 15/15 |
| #1282 | K  — #1272 | `585e3884a` | 15/15 |
| #1283 | R3 — #1122 | `e7d8ff7b3` | 15/15 |
| infra#283 | R2 | `8e4b073d7` | draft, green, ⛔ merge-blocked by design |

## EVERY LANE NEEDED A SECOND ROUND — 4 for 4
Each was fully CI-green in round one and still shipped a defect `/code-review <pr> high` caught.
THREE of the four were a defect in the very guard the PR was written to install:
- **R2**: fail-closed guard tested EMPTINESS not OVERLAP -> a non-empty set disjoint from the
  candidates deleted live files. PM-verified: `deleted=1` with `referenced_refs_count=2`.
- **R3**: #656 REGRESSION — the one boundary it was forbidden to cross. PM-verified main HELD /
  branch RELEASED with a clean-anchor control passing on both.
- **K**: grounding gate defeated by SUBSTRING match (`'pod' ⊂ 'podman'`), and its docstring argued
  the chosen direction avoided the very case it produced. PM-verified on #1272's own query.
- **R1**: guard RED ON CORRECT CODE (monotone short-circuit) + `or {}` reintroducing the fixed bug
  at a second seam. PM-verified by function-level reproduction with a positive control.

## PM VERIFICATION PERFORMED (not relayed)
- R2: ran the real 850-object corpus through the new chain (WOULD_DELETE=0); mutated the overlap
  guard back to emptiness -> **6 tests red across all three call sites**.
- R1: re-ran the gutted-body mutant that PASSED in round 1 -> now **reds 5** incl.
  `test_the_predicate_is_the_engine_s_own`.
- R3: re-ran the regression probe -> HELD, control HELD; disabled the principal-source arm ->
  **3 contaminated-anchor pins red AND the probe flips back to RELEASED**.
- K: re-ran the identity-term probe -> servicenow 6→0, portal 1→0, podman 4→1 (legit plural fold);
  then resolved the QEMU apparent-discrepancy by measuring the floor: **5 chunks clear 0.5, ZERO of
  them identity-grounded** -> nothing seeds. K's table was a post-floor count, mine predicate-level;
  both correct, and K disclosed the predicate residue in prose rather than hiding it.

## CORRECTIONS THE PM MADE TO LANE REPORTS
- R1's candidate cited `:5703`; that line passes `progress_made=False`. The bare site is **`:5735`**
  (the propose-and-await-confirmation dropdown), and it is ONE site — `:5297`/`:5622` pass genuine
  derived arms via `**confirmed_transition_arms(...)`. Recorded on #1284 with the correction.
- My own error, corrected by R3: I used dev-DB absence to downweight the unattached shape while
  calling dev-DB absence inconclusive for #1272. Same fact, two standards.
- My population criterion was under-specified: a single-element fixture makes the attribution test
  a TAUTOLOGY. R3 replaced it with `case_c26d7905f26d` (multi-element).
- My suggested "unswept gap" (high one-way containment) was measured and REJECTED by R3.
- Both commensurability remedies I put in K's brief (RRF, min-max) MEASURABLY LOSE (Q3 rank 1→9, 1→8).
- **`case_4d1b45632f27` is `mece_contested` on BOTH trees** (R3's measurement, not independently
  reproduced by PM): its NULL conclusion is a contest between two OTHER roots, so the restatement
  hold was never what bound it. This corrects the PM's earlier statement to the owner.

## FILED BY PM
- **#1284** — `hypotheses_validated` progress arm has no writer (0 of 2,129 turns; siblings
  537/255/211/176). Plus a comment adding the `:5735` second instance. Owner decision.

## OWNER QUEUE
1. **Close #1195** — fixed by #1199, never closed (PR title used `(#1195)`, not a closing keyword).
   Evidence: ancestor check, 44 pins pass, mutation reds 7 incl. the exact pin the issue demanded.
2. **#1284** — scoring policy: give the dead arm a writer, retire it, or require a co-firing arm.
3. **MERGE ORDER for R2**: merge #1281 → bump `images[0].newTag` INSIDE infra#283 to that merge
   commit's `sha-` → merge infra#283. Zero window. Both other orderings fail closed AND page.
   CD auto-applies on infra merge.
4. Beta gates #1251/#1252/#1016/#1169/`FaultMaven/faultmaven-slack-agent#25` — unchanged.
5. **KB content**: `qemu`/`libvirt` in 0/91 runbooks (kb-toolkit). Contradicts #1272's own
   "not a content gap" text.
6. **Runbook titles absent from embedded text** — 16/1297 chunks, 15/91 chunk-0s (PM-verified).
   Needs a chunker change + pack rebuild in kb-toolkit.
7. **#1232's 160 unreferenced objects** — reclaim or not; a data decision.
8. **Sweep counters structurally unscrapable** — `evidence_orphan_file_rate_high` cannot fire.
9. **Query expansion route** for #1272 (curated lexicon vs LLM call on the prefetch path).
10. **Standing hazard**: 6 of 7 remaining §7.1 holds are #661 contaminated anchors; any future
    change treating "the anchors" as inert problem framing breaks the same way. Pinned 3x now.
