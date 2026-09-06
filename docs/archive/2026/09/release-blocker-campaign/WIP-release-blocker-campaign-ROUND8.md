# Release-blocker campaign — ROUND 8 (dispatched 2026-09-02)

> Every status line here is stale on your next read. Re-verify with `gh api`
> (`gh issue view` is BROKEN on these repos).

## Base
Main moved TWICE more than the round-7 handover recorded. Handover said `6b3592bf5`;
actual tip at dispatch was **`a99b1fbe1`**. The peer workstream landed:
- `2780ad684` = #1304 (closes #1303)
- `a99b1fbe1` = #1307, **removes the KB cause seeder** (#1295 step 4b part 1)
and has **PR #1309 OPEN** (head `aa43650dc`, step 4b part 2, 26 files, −609 lines in
`knowledge_service.py`). Expect main to keep moving.

Gates on `a99b1fbe1`, PM-verified in a tree-asserted worktree: black **1178 unchanged**,
ruff **passed**, import-linter **13 kept / 0 broken**, `generate_api_docs.py --check` **matches**,
`pytest tests/unit/architecture/` **131 passed / 1 skipped**.

## FIRST TASK — CLOSED
`FaultMaven/faultmaven-enterprise-infra#299` merged 2026-09-02T20:00:41Z. CD run **success**
(`Deploy to on-prem: success`); `deployment "faultmaven-api" successfully rolled out`.
Deployment **and both CronJobs** (`case-cleanup`, `storage-cleanup`) on `sha-a99b1fb`.

**Target changed from the planned `sha-6b3592b` to `sha-a99b1fb`.** `promote-images.yaml` reads
`available-images/staging/faultmaven.yaml`, which had already advanced (19:53:10Z). Reaching
`sha-6b3592b` would have required hand-editing the queue — the same class of mechanism
subversion as editing the overlay. Promoted `faultmaven` ONLY; the queue also held an
unrequested slack-agent bump (`sha-7c7112f` → `sha-8ddc897`) deliberately left alone.
Both PR workflows arrived `action_required` (unlike #298) and needed approving first.

Live verification, on the shipped image against the real ConfigMap (admin endpoint is 401
without a token, so the route's own function was evaluated in-pod):
```
LadderPlan(attempts=1, paid_attempts=3, afforded_seconds=120.0,
           full_ladder_seconds=374.0, fits=False)
```
`admin_config.py:806` is `enabled=plan.fits`, so `/admin/config/status` publishes
`llm_retry_ladder_fits_turn_budget: False`. `374.0` == round 7's predicted `3*120+14`.
**False is the fix working.**

## METHODOLOGICAL CORRECTION — the stale-open detector is weak
The PM ran a sweep over 400 commit SUBJECTS, got clean controls, and reported "no new
accidental stale-opens". **That claim was wrong.** #907 was fixed by PR #1182, whose subject
names **#1173** and whose full message and PR body mention #907 **zero times**. Re-running the
sweep over commit BODIES, the positive control FAILED — #907 is invisible to any
message-based sweep. #982 is the same shape (fixed under #997/#1005).
**Collateral fixes leave no textual trace. Only executing an issue's claim against current
main finds them. Treat the stale-open population as larger than known.**

## MEASUREMENT ROUND (3 agents, worktree-isolated, before any lane was dispatched)
All findings below re-verified by the PM by execution, with positive controls.

### Held area — RE-DERIVED after the seeder removal
- **#1293 — FIXED BY REMOVAL. CLOSE.** Every referent gone: `kb_cause_seeder.py`,
  `kb_grounding.py`, `tests/eval/kb_cause_seeder/`, the `kb_grounding_1285` fixture.
  `_tokenize(metadata["title"])` = **0 hits**; control `_tokenize(` = **3**.
  **The #1295 <-> #1293 cycle is CUT** — and it was already cut before this round: #1302
  replaced the confounded `kb_cause_seed_ungrounded_total` counter with an on/off outcome A/B
  that #1293 cannot confound.
- **#1295 — a FOURTH outcome: decision MADE and ENACTED.** Not stale-open, not
  referenced-but-unfixed. Push removed (#1307), pull retained (`_prefetch_kb_context` survives).
  Decided from #1302's A/B (resolved 3/6 OFF vs 2/6 ON; root cause identified 5/6 OFF vs 3/6 ON).
  **But on a SUBSTITUTED INSTRUMENT** — the counter #1295 prescribed was deleted unread.
  No step 4c exists. CLOSE on #1309 merge, if the owner ratifies the substitution.
- **#1288 — STILL LIVE, and never about the seeder.** #1295's body miscategorises it;
  #1288's own body says it is unreachable from retrieval-scoring work. → LANE 3.
- **#1167 / #1168 — STILL LIVE, and NEVER in #1295's blast radius** (their timelines contain
  zero references to it). 34 collected / 34 passed — and the tests *assert the permissive
  behaviour* (`# does not raise`, `assert CHUNK_B_TEAM in got`). `build_kb_scope_filter` has
  exactly three arms, no `organization_id`. **Not lane-ready — owner picks the option.**
  Sequence any lane AFTER #1309 (2 files overlap, at zero lines).
- **#982 — CLOSE.** Migration 041 confirmed in-chain; no ORM models remain.
  (PM's own grep hit `AgentExecutionState`, an unrelated Pydantic DTO — substring vs token.)
- **#1117 / #1118 — deliberate hold, engineering complete.** Owner decides.

### #936 / #907 — TWO ROOTS AT ONE SEAM, and one is already fixed
- **#907 — DOES NOT REPRODUCE. CLOSE.** Fixed by PR #1182 (2026-08-25). Main sends
  `TokenAuthClientProvider` + credentials on the `CHROMADB_URL` branch; the positive control at
  `434ec7f96^` reproduces the authless branch exactly. Two `HttpClient` sites, both via the
  shared resolver. Zero lane.
- **#936 — REPRODUCES, and its own premise is FALSE.** → LANE 1.
The PM's "one root, one lane" hypothesis is **refuted by execution**: main carries #907's fix
AND #936 still reproduces on the same tree.

### #918 / #1170 — BOTH evaporated as lanes
- **#918 — ~2 of 3 exposures ALREADY FIXED and unlinked.**
  `core/investigation/suggestion_liveness.py` (added by `cead40fd5` / PR #1263, 2026-08-29;
  refined by #1267) implements #918's proposal and names fm#918 **5 times**. #918's timeline
  cross-references only **#916** — nobody closed the loop. Only exposure 3 remains
  (a classifier-minted **Gate-1** confirmation not facing the INV-26 substance test), and that
  is a **scope decision #721 already made once in the other direction**.
  → **OWNER DECISION**, not a lane. Link #1263/#1267 to it.
- **#1170 — DOES NOT REPRODUCE. CLOSE.** The metric no longer exists. `fm-sre-simulator`
  PR **#48** (merged 2026-08-27, *"fix(metric): score an evidence request, not a mention of
  one"*) closed its #47, titled *"primary_evidence_coverage credits narration as a request:
  jwt-token-decode scores on a phrase this scenario's own evidence text supplies"* — precisely
  the defect #1170's number rests on. Under the corrected metric the alleged drop **inverts
  run-for-run**; both eras are {0.6, 0.8}. `jwt-token-decode` was **never served** in either
  cited era, so the issue's premise ("rests on three primary items instead of four") is false —
  it rested on three in both. Positive control confirmed the probe CAN credit the item.
  Residual, NOT this issue: the agent asks for the decoded JWT in **1 of 17** runs, steady
  across both eras — a fresh product question with a measured baseline, not a regression.

## LANES DISPATCHED — 3, each worktree + own scratchpad, all off `a99b1fbe1`
Cross-lane and peer-PR collision checked with `LC_ALL=C` sort and a POSITIVE CONTROL
(injecting `kb_init.py` into lane 1 correctly reported it).
Lane1 vs Lane2 vs Lane3: all NO OVERLAP. vs #1309: L1 NO, L2 NO, **L3 OVERLAPS**
`knowledge_service.py` (hunks disjoint: #1309 stops ~2037, resumes 3090; L3 targets 2653/2712/2839).

### L1 — #936  ROOT: a maintenance/bootstrap path resolves a data location from a source the running server does not use
Branch `fix/936-resolve-kb-paths-from-the-settings-the-server-uses`. `Closes #936`.
**Population is 3, not 1**: `cli/reset_kb.py:116-117` (the filed instance, two mechanisms);
`bootstrap/data_init.py:73-82` `ensure_data_directories()` — **named by no issue, and it
MANUFACTURES the decoy that suppresses #936's own warning**; `config/settings.py:1921`
`chroma_persist_directory` — orphan setting, zero consumers.
**LIVE on the cluster with ZERO overrides** (PM-verified on the running `sha-a99b1fb` pod):
`/app/data/chroma-kb` EMPTY, `/app/alembic.ini`+`pyproject.toml` present so `get_project_root()`
= `/app`, real store **73,244,672 bytes** on `faultmaven-chromadb-0`, and
`CHROMADB_KB_PERSIST_DIR`/`PROJECT_ROOT`/`TENANT_PROVIDER` all UNSET.
`fm-reset-kb --yes` today drops every `knowledge_items` row, wipes an empty decoy, exits **0**.
**The issue's own suggested fix is MEASURED INSUFFICIENT** — it fixes the override variant and
leaves the production-reachable one intact. Correct shape already exists in the same directory
(`cli/wipe_deployment.py:547-577`, `bootstrap/kb_init.py:112-120`).
Hazard: site 3 touches `settings.py` -> config-purity + compliance gates.
Riders in #936's body (README wording drift; BYPASSRLS role guard in `jobs/run.py`) are
report-only, NOT absorbed.

### L2 — #947  ROOT: a fixture module no test consumes is not a harness
Branch `fix/947-delete-the-unreachable-api-conftest`. `Closes #947`. Expect `-1478/+0`.
PM re-verified on `a99b1fbe1`: `tests/unit/api/` = **872 passed / 1 skipped WITH the conftest
and 872/1 WITHOUT**, 873 collected both ways, 21s faster without.
**Briefed as DELETION** — the issue's own first-listed fix would newly execute ~1160 lines of
never-run fixture code for ZERO consumers. Burden of the lane is the consumer proof
(fixtures resolve by NAME, not import) + a fixture-SHADOWING check against parent conftests.

### L3 — #1288  ROOT: a published API contract claims a capability the implementation lacks
Branch `fix/1288-two-knowledge-endpoints-claim-more-than-they-do`. `Closes #1288`.
`knowledge_service.py:2653` ships `# (In production, this would use embeddings)` under
`if self._vector_store:`; `fulltext_search_documents:2839` scores **title only**;
`KnowledgeItemRepository.search_by_text` has no caller (a same-named method on
`RunbookKnowledgeBase` is the decoy — positive control required).
Drift is published in the **CI-gated** `openapi.json`. Central judgement is per endpoint:
raise the implementation or lower the claim. Guard must DISCRIMINATE lexical from semantic
(a rewording test proves nothing). Must rebase onto main before push (#1309 shifts every line).

## OWNER QUEUE
1. **CLOSE #1293** — fixed by removal, PM-verified with controls.
2. **CLOSE #982** — fixed by #997 + #1005 + migration 041.
3. **CLOSE #907** — fixed by PR #1182, invisible to message sweeps.
4. **CLOSE #1170** — does not reproduce; `fm-sre-simulator` #48 fixed the metric.
5. **CLOSE #1295 on #1309 merge** — ratify the instrument substitution (A/B outcome instead of
   the prescribed counter, which was deleted unread).
6. **DECIDE #1167 + #1168 together** — the two strongest remaining P1s. #1167 needs option
   1/2/3 (only 3 closes it, at a signature change across every KB read path); #1168 needs a
   backfill migration + a global-tier decision. Downstream consumer is #1252 (beta gate).
   Sequence after #1309.
7. **DECIDE #918** — mostly delivered by #1263/#1267. Link them. One question: should a
   classifier-minted **Gate-1** confirmation face the INV-26 substance test? #721 decided the
   opposite once, deliberately.
8. **#1117 / #1118** — engineering complete, deliberate hold. Close or keep as the umbrella
   for #1116.
9. **#828** — carried. Reproduces; NOT lane-ready. RLS hazard: revocation runs before any
   tenant is bound and `_is_revoked` is fail-open, so a table following the DEFAULT RLS pattern
   would silently disable revocation in Cloud (038/039 carry the counter-precedent verbatim).
10. **#752** — carried. `include_deleted` has no referent. Blocked on a cross-repo consumer check.
11. **CLAUDE.md is stale** (L53, L864, L866): says the `agent_executions`/`agent_tool_calls`
    tables, ORM models and contract methods "remain" and that dropping them "needs a migration
    and is a separate decision". **Migration 041 dropped them.** PM-verified.
12. Beta gates #1251/#1252/#1016/#1169, `FaultMaven/faultmaven-slack-agent#25` — unchanged.

## FILING CANDIDATES (PM decides; nothing filed)
- PR #1307's body justifies keeping the eval corpora with "(fm#1272 is still open)" — **#1272
  closed 2026-08-31**, ~2 days before the PR was authored. Corpora still worth keeping; the
  stated reason is stale.
- ~2 present-tense comments still name the removed seeder after #1309
  (`utils/serialization.py:88`, `knowledge_vector_store.py:1093`).
- `scripts/auth/README.md:190` "Granting operator roles" vs `cli/promote_platform_admin.py:100`
  printing "Granted operator roles".
- BYPASSRLS role guard checks RLS-exemption but not write privilege (`faultmaven/jobs/run.py`) —
  different subsystem, unmeasured.
- Mis-marked sync tests: `@pytest.mark.asyncio` on non-async defs,
  `tests/unit/infrastructure/llm/providers/test_openai_messages.py:467,:544` (carried).
- The agent requests the decoded JWT in 1/17 simulator runs, steady across eras.

## PM CORRECTIONS TO ITS OWN BRIEFS
- **The stale-open sweep was under-powered** (see above). The headline self-correction of the round.
- Hypothesised **#936 + #907 = one root, one lane**. **Refuted by execution** — #907 was already
  fixed on the same tree where #936 still reproduces.
- Hypothesised **#918 lane-ready, #1170 a provable regression**. **Both refuted** — #918 is
  ~2/3 already fixed and unlinked; #1170 is entirely a harness artifact fixed upstream.
- Carried the handover's base `6b3592bf5`. **Wrong by two commits** at dispatch time.
- Carried the handover's promotion target `sha-6b3592b`. **Superseded** — the queue had moved.
- The `comm`-on-unsorted-input trap fired on TWO of three measurement agents. It is not optional.

## Box state
Serial full-suite baseline on the round-7 merged tree: **13,373 passed / 0 failed / 119 skipped**
(~36m). Compare whole-suite to whole-suite in the SAME mode, never against "0"; xdist whole-suite
runs flake ~5 with a SHIFTING set. `tests/integration/test_main_app.py::
test_application_uses_configuration_defaults` and `tests/unit/api/test_composition_root.py::
test_app_state_has_services_after_startup` are ORDER-DEPENDENT — pass in the full suite, fail
standalone; pre-existing. A PEER WORKSTREAM is active in this repo — leave its processes and
worktrees alone; don't prune stale worktrees.

---

# ROUND 8 OUTCOMES (appended as they landed)

## Base moved AGAIN during the round
`4da25e38c` = #1309 (#1295 step 4b part 2) merged 21:16:50Z, mid-lane. All three lanes
rebased. Peer workstream then promoted the cluster to `sha-4da25e3` at 21:56 — so the cluster
is on current main and #1307+#1309 are deployed. Peer PRs #1313/#1314/#1315 also opened;
collision-checked against all three lanes, no overlap (12 pairs, positive control fired).

## Test-count arithmetic for this round (needed for every whole-suite comparison)
| tree | collected |
|---|---|
| `a99b1fbe1` | 13,347 |
| `4da25e38c` (current main) | **13,226** — #1309 deleted **121** tests |
| PR #1310 (#1288) | 13,229 (+3) |
| PR #1311 (#947) | 13,251 (+25) |
| PR #1312 (#936) | 13,257 (+31) |
**Two lanes compared against `a99b1fbe1` and reported a phantom loss.** Compare against the
CURRENT base or the deletion shows up as your regression.

## NEW TRAP — `base.sha` is not the merge-base
The GitHub API's `pulls/N.base.sha` reported `4da25e38c` for a branch whose merge-base was
`a99b1fbe1` and which did NOT contain #1309. It reports the base BRANCH TIP. `MERGEABLE` +
green CI did not catch it either — CI ran on the stale tree.
**Only `git merge-base --is-ancestor <main> <head>` answers "is this branch current".**

## LANE OUTCOMES — 9 for 9 on the review round
Every lane CI-green in round one; every one shipped a defect `/code-review high` caught.

### L3 — #1288 → PR #1310  DONE, PM-verified
Round 2 head `2588654a3`, 15/15 green, +1325/-626, 19 files.
**Review found a SECURITY REGRESSION the PR introduced**, PM-verified end to end: base
line 2314 was `"content": ""`; the PR returned a real body excerpt on a route using
`get_current_user_optional` that publishes NO `security` in openapi, while
`GET /documents/{id}` requires `HTTPBearer`. `_inventory_visibility_clause`'s own docstring:
*"an anonymous caller sees global content only"*.
**The lane then found what changed the remedy**: `POST /knowledge/search` was ALREADY
anonymous and ALREADY returned `content[:200]` on base — so suppressing only on its own route
would have created a NEW divergence between the two arms. It applied one rule in one place
(*body text goes only to a caller who could have read the document directly*), closing its own
regression AND the pre-existing one. **The PM's framing would have produced a worse fix.**
PM-verified on the head: `kb_c` (its own NO_MATCH fixture) 0.1667 → **0.0** on every query;
control `zzzqqq` → 0.0; `ENOSPC` → 0.3 on the right doc only;
`_may_read_document_content(None)` → False, authed → True.
It also caught a test of its own that **encoded the vulnerability** (service fixture with
`user_id=None` asserting it received content).

### L2 — #947 → PR #1311  DONE pending final review
Head `d0c80188a`, merge-base `4da25e38c`, 15/15 green, +496/-1480, 3 files, **+25 exactly**.
**Best-executed lane of the campaign.** Ran `/code-review high` on itself, found **13 issues
ALL inside the guard it had just added**, and **WITHDREW the entire name-binding half** rather
than patching it — "not soundly decidable from an AST, and every failure mode was a false
positive", i.e. worse than no gate. Four were false positives that would redden CI on correct
code (`if TYPE_CHECKING`, `for`/`with`/`match`/walrus bindings, import inside a `def`,
re-raising handler).
Consumer proof (the strongest seen here): AST name-scan over **all 731 test files** (fixtures
resolve by NAME — an import-graph search proves nothing) → 0 external requests for all ten
fixtures; controls `sample_case` 179, `mock_case_service` 47, fabricated name 0. Plus pytest's
own `--fixtures-per-test` map over all 873 tests **byte-identical before/after**, naming the
file 0 times. Guard's "0 violations" is NOT a vacuous zero — it reports 6 modules that DO
resolve as its positive control, with floors (726 files ≥ 600, 6 resolved ≥ 3).
Refuted the issue itself: the import #947 says "resolves" does not, and the file's OTHER
guarded import was dead too — the whole 1187-line body always ran on fallbacks.

### L1 — #936 → PR #1312  REWORKING
Head `347186bb4`, 15/15 green, +1374/-83, 12 files. Ran `/code-review high` on its own first
cut and acted on it (found `CHROMADB_HOST` as a SECOND remote opt-in; caught a blank-value
`rmtree`-the-cwd hazard **it had introduced**; rejected the PM's fact 5).
**Review found the lane's OWN ROOT, in its own error handler.** PM-verified by execution
driving the lane's own fixtures with `rmtree` patched to raise:
```
EXIT CODE = 0 | SQL DELETES = 1 | STORE STILL EXISTS = True | 'DIVERGE' printed = True
```
`reset_kb.py:381-391` — the `except OSError` branch prints DIVERGE and **falls through with no
return**, reaching `return 0` at :421. The documented runbook
`fm-reset-kb --yes && kubectl scale ...` then proceeds onto the un-wiped store. The module's
exit contract (0/1/2) has **no code for "the destructive half ran and the recoverable half did
not"**. Blocking.
Second, PM-verified: **blank `CHROMADB_HOST=` reads as REMOTE** (`("" or "").strip() !=
"localhost"`), so an unpopulated ConfigMap key bricks `fm-reset-kb` on an embedded deployment
with the message *"CHROMADB_HOST= is configured, so this deployment's KB vectors live on a
remote ChromaDB server"*. The same `env_ignore_empty` shape the PR fixes for the persist dir,
one knob over — and the same predicate drives the ingester → `HttpClient(host='', port=8000)`.

## PM CORRECTIONS TO ITS OWN CLAIMS (round 8)
- **`TENANT_PROVIDER` is `multi` on the cluster, not unset.** It lives in the
  `faultmaven-secrets` SECRET; the PM's probe read only the ConfigMap and reported `<UNSET>`.
  So `fm-reset-kb` REFUSES on this cluster (#770) and **#936's harm is masked here** — the
  reachable population is a SINGLE-TENANT deployment on external ChromaDB. Mechanism live,
  harm not live on this cluster. Read the running container's env, not the ConfigMap.
- **The stale-open sweep was under-powered** (subjects, then bodies; #907's fix mentions it
  nowhere). Only execution finds collateral fixes.
- "33 collected across the 4 relevant files" → actually **13** (or 17 with a fourth file).
- The config-purity hazard "17 collected" → the whole `tests/unit/architecture/` dir is **132**.

## LIVE FINDING FOR THE OWNER — unauthenticated KB body exposure on the public API
Verified against production (single benign query, read-only):
`POST https://api.faultmaven.ai/api/v1/knowledge/search` with NO auth → **200**, returning
200-char body excerpts of global runbooks. Present in the deployed image
(`knowledge_service.py:2791`, route uses `get_current_user_optional`). **Pre-existing, not
introduced this round.**
Calibration: what leaks TODAY is the shipped KB pack (repo-public runbooks), so practical
impact is low. The MECHANISM is the issue — `global` is the platform tier visible to every
tenant, so any operator-authored global runbook is exposed the same way, while
`GET /documents/{id}` requires `HTTPBearer` for the same text. PR #1310 closes it.
**Owner decision:** if global runbook bodies are meant to be public, the coherent remedy is
relaxing `GET /documents/{id}`, not two search endpoints leaking around it.

---

# ROUND 8 FINAL — all three lanes complete, PM-verified, none merged

Main moved a FOURTH time: `fd1a389ea` (#1316, peer). All three PRs are 1 commit behind it with
**zero file overlap** (checked, control fired 3/3), all `mergeable=true`, all blocked only on
the required-review gate. No rebase forced — churning three lanes for a disjoint 3-file peer
commit buys nothing.

| PR | Issue | Head | CI | Δ tests vs `4da25e38c` (13,226) |
|----|-------|------|----|-----|
| #1310 | #1288 | `2588654a3` | 15/15 | 13,229 (**+3**) |
| #1311 | #947 | `485dd4bd9` | 15/15 | 13,267 (**+41**) |
| #1312 | #936 | `09737eedf` | 15/15 | 13,263 (**+37**) |

## The review round: 9 for 9, and every defect was inside the guard the PR installed
- **#1310** introduced an anonymous body-excerpt exposure — then, chasing it, found the
  PRE-EXISTING one on `/knowledge/search` that changed the correct remedy. PM's framing would
  have produced a WORSE fix (a new divergence between the two arms).
- **#1312** violated its own thesis in its own error handler: `exit 0` after diverging.
  PM-verified before (`EXIT CODE = 0 | DELETES = 1 | STORE EXISTS = True`) and after
  (`EXIT CODE = 3`, and the misleading "Next step: restart the API server" line unreachable).
  The lane found it was worse than reported — that restart instruction was the LAST thing
  printed. Chose **3, not 2**: 2 means re-running fixes it; this needs a human first.
- **#1311**'s guard had a FALSE NEGATIVE on the very class it was built to catch (a class body
  in a guarded try executes at definition time; `ast.ClassDef` was in the skip set) AND a FALSE
  POSITIVE (first-matching-handler ignored). PM-verified both, before and after:
  ClassDef 0→1 violations, re-raise-then-broad 1→0, control steady at 1.

## Judgement calls worth keeping
- **#1311 withdrew half its guard in round 1** (name binding: "not soundly decidable from an
  AST, every failure mode a false positive — worse than no gate") and then **fixed rather than
  narrowed in round 2**, on the correct distinction: construct detection IS decidable
  (first-matching-handler is documented, class bodies execute at definition time,
  `TYPE_CHECKING` is False at runtime). Those were implementation errors, not undecidability.
- It built an **execution oracle** (a `sys.meta_path` hook recording whether the absent import
  was actually attempted), because its first harness inferred "swallowed" from a clean `exec`
  and **mis-scored two rows**. 5 disagreements → 0 across 13 shapes.
- It caught a bug in its own fix by probe, not review: `issubclass(ImportError,
  ModuleNotFoundError)` is **False** — the subclass relation was inverted, and an absent module
  raises the narrower `ModuleNotFoundError`.
- It resolved the duplication through the repo's own documented convention
  (`tests/error_text_ast.py`: "the analysis lives here so hardening it hardens every guard at
  once"). New `tests/import_guard_ast.py` is imported by BOTH guards; running the two old
  copies over the same inputs **disagreed on 5 shapes**, proving the predicted divergence.
- **#1312 removed `is_remote_chroma_configured`** rather than adopting it — zero production
  callers, and `reset_kb` needs the two answers kept APART, which was the whole of finding 3.

## Numbers corrected during the round
- `resolved >= 3` floor → pinned `== 6`; the old floor stayed GREEN at populations 5, 4 and 3.
- The guard's "151 first-party guarded imports" → PM/review measured **108**, and after the
  correctness fixes it is **103**. The 151 came from the withdrawn first draft.
- Probe vacuity: `assert all("no such module" in v)` could not fail (one `append`, literal
  always present). Probes now assert the LINE NUMBER — reverting it kills **15**.

## OWNER QUEUE — final
1. **Merge #1310, #1311, #1312** (or review). All green, all mergeable, none merged.
2. **CLOSE #1293, #982, #907, #1170.** All verified. #907 and #982 were fixed collaterally
   under other PR numbers and are invisible to any message-based sweep.
3. **CLOSE #1295** once satisfied the decision may rest on the A/B outcome rather than the
   `kb_cause_seed_ungrounded_total` counter it prescribed (deleted unread).
4. **DECIDE #1167 + #1168 together** — strongest remaining P1s, live, never held by #1295.
5. **DECIDE #918** — mostly delivered by #1263/#1267 (which never cross-referenced it). One
   question: should a classifier-minted Gate-1 confirmation face the INV-26 substance test?
6. **DECIDE the unauthenticated KB body exposure** — live on the public API, pre-existing,
   closed by #1310. If global runbook bodies are meant to be public, the coherent remedy is
   relaxing `GET /documents/{id}`, not two search endpoints leaking around it.
7. **`fm-reset-kb` now has NO supported reset for external ChromaDB** — it refuses instead of
   corrupting (strictly better), but a single-tenant on-prem deployment has no path. Adding a
   destructive network capability to that CLI is an owner call.
8. **CLAUDE.md stale** — migration 041 dropped `agent_executions`/`agent_tool_calls`; the file
   still says they "remain" and that dropping them "needs a migration and is a separate
   decision" (L53, L864, L866).
9. Carried: **#828** (RLS hazard — a table following the default pattern silently disables
   revocation in Cloud), **#752** (blocked on a cross-repo consumer check), #1117/#1118,
   beta gates.

## FILING CANDIDATES (nothing filed)
Container vs ingester disagree on where the KB lives under `CHROMADB_HOST` alone (now
load-bearing in two comments and a doc paragraph) · `scripts/check_config_compliance.py` is
broken on main (`ModuleNotFoundError: tests.architecture`) · `CHROMADB_COLLECTION` is a knob the
KB writer ignores · `tags` diverges between `/knowledge/search`'s two arms on base · `category`
accepted and ignored by `/knowledge/search` · snippet route reads the document twice, second
read UNSCOPED · `rbac.md` omits `POST /documents/search` · `tests/conftest.py` has **26 of 31
fixtures with zero consumers** (same root as #947 at much larger scale; `reset_container` is
among the dead AND is the worked example in `CLAUDE.md:749` and `tests/README.md:191`) ·
`tests/unit/infrastructure/shims/conftest.py` is a pure no-op · `context_builder.py:1261` has
`\b` in a non-raw docstring (SyntaxWarning in 3.12) · `faultmaven/models/__init__.py.bak` is a
committed backup · #936 riders (a) BYPASSRLS guard checks no write privilege, (b) README
wording · PR #1307's body cites "#1272 is still open" — it closed 2 days earlier · agent
requests the decoded JWT in 1/17 simulator runs.
