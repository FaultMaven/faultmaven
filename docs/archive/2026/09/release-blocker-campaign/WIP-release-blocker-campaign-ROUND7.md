# Release-blocker campaign — ROUND 7 (dispatched 2026-09-02)

> Every status line here is stale on your next read. Re-verify with `gh api`
> (`gh issue view` is BROKEN on these repos).

## Base
Lanes branch off **`4558959c1`** (#1296, the #1293 measurement PR). Re-fetched at dispatch;
main did NOT move during planning this round.

Gates on `4558959c1`, PM-verified in a dedicated worktree with `faultmaven.__file__` asserted:
black **1174 files unchanged**, ruff **passed**, import-linter **13 kept / 0 broken**,
`generate_api_docs.py --check` **matches**. Serial full-suite baseline (round-6 merge):
**13,023 passed / 0 failed / 122 skipped, ~29m**. Compare whole-suite to whole-suite in the
SAME mode, never against "0".

## Carried-over first task — CLOSED
infra#298 (`sha-18d85e4` -> `sha-4558959`) merged 2026-09-02T06:17:01Z. Cluster verified:
`faultmaven-api` (both pods) **and** both CronJobs (`case-cleanup`, `storage-cleanup`) on
`sha-4558959`. Note: the two clean orphan-sweep nights ran on `sha-18d85e4`; the first armed
run on the new image is the 03:00 after dispatch.

## Pool reconciliation — the handover shortlist was 6 of 18
PM re-enumerated ALL 62 open issues and filtered **client-side** (no `--jq` label query — that
is how the earlier empty read happened), with a label-existence control first.
**1 P0 + 17 P1 = 18.** Newly surfaced and material: **#947** (dead api conftest), **#982**
(agent LLM shadow stack), #936, #907, #1170, #918, #908, #512, #1206.

## HOLD — #1295 blast radius
#1295's own text: *"The tactical defects in the gate are tracked separately (#1293, #1288)."*
#1284 appears nowhere in it (it is the `hypotheses_validated` progress arm — held on the
round-6 scoring-policy decision, a different axis). Hold list: **#1293, #1288, #1167, #1168.**

**⚠ #1293 does not belong on that list, and #1295 is what says so.** #1295: *"Fix #1293 before
treating the counter as a decision input, or the data will argue for keeping a path that is
mostly admitting noise."* #1293 is the PREREQUISITE, not the downstream. Holding it deadlocks
the decision it is held for.
PM checked whether #1293 was a #1195-class stale-open: it has the SHAPE (PR #1296 titled
`(#1293)`, no closing keyword, **no `closed` event** in its timeline) but is genuinely
UNFIXED. #1296 changed no behaviour — 6 of 7 files were tests/eval/deps; the sole product-code
change is a **22-line docstring** recording that rarity was measured and REFUTED as a remedy.
#1293's real blocker is one it names itself: the measured fix *"costs 3 correct queries to save
6"* — a precision/recall product decision. **#1295 is blocked on #1293; #1293 is blocked on the
owner. Only the owner can cut the cycle.** Third path that prejudges nothing: build the
purpose-built labelled set (#1293 says the #1291 fixture is the wrong instrument — 60 of its
113 pairs are authored paraphrases). kb-toolkit-shaped, still inside the held area.

## MEASUREMENT ROUND (3 agents, worktree-isolated, before any lane was dispatched)

### Stale-open audit — #1117 / #1118 / #710
**#1117 + #1118: FIXED. Recommend CLOSE.** Both shipped in PR **#1125** (merge `389d9a58a`,
2026-08-20, PM-verified ancestor of main). Open **on purpose** — PR body says verbatim
`Refs #1117, #1118 (not auto-closing — owner decides).` NOT the #1195 accident.
Mutation-proved, not asserted: router drops `reasoning_intent` -> **3 failed / 1455 collected**;
per-provider translation neutered -> **24 failed / 1455**; `min_output_tokens` neutered inside
`route()` -> **24 failed / 141**. Includes #1117's item-4 ask verbatim
(`test_reasoning_call_with_floor_cannot_return_starved_max_tokens_stop`), which is NOT
tautological — it drives a real `LLMRouter` and is bracketed by two negative controls
(cut-but-above-floor -> returned; below-floor-but-clean-STOP -> returned).
Residual: `min_output_tokens` never wired into Anthropic's `budget_tokens < max_tokens`
partition. PR #1125 named that deferral and assigned it to **#1116** (open, owns it).
`ANTHROPIC_THINKING_MODE` defaults `off`, Anthropic is not the shipped default, and the
router's post-call check still raises — an extra raise+retry, not a starvation hole. No lane.
**#710: STILL OPEN, premise NARROWED not changed. Leave P3, no lane.** KB-area, held anyway.

### #1278 / #1292 / #982
**ONE ROOT** (not two meeting at a seam). See LANE 1.
**#982: ALREADY FIXED — stale-open, recommend CLOSE.** Landed under two OTHER PR numbers
(#997 deleted the shadow stack, #1005 removed the residue), which is why it never closed.
Migration `041_drop_agent_executions` dropped both tables. PM-verified with positive controls:
`AGENT_LLM_PROVIDER` 0, `create_llm_client` 0, `claude-sonnet-4-20250514` 0 in `faultmaven/`,
`llm_client.py` absent; control `LLMRouter` = 18 hits. The single `AgentOrchestrationService`
match is past-tense prose in `modules/agent/README.md`. **Not dead code — not code at all.**

### #1234 / #828 / #752 / #942 / #947 — all five REPRODUCE
See LANES 2 and 3 for the two taken. Queue dispositions below for the rest.

## LANES DISPATCHED — 3, each worktree + own scratchpad, all off `4558959c1`

### L1 — #1278 + #1292  ROOT: a bounded operation must not begin a step it cannot finish inside its own bound
Branch `fix/1278-budget-the-retry-ladder-against-the-turn-deadline`. Both `Closes`.
Ladder: one `with_retry` site (`milestone_engine.py:9610`); `with_retry`
`llm_error_handler.py:699-735`; `RetryConfig` `:208-215` (`max_retries=3, base=2.0, exp=2.0,
cap=30.0`, **hardcoded, no env knob**); backoff **2+4+8 = 14s**; breaker `router.py:238-239`
threshold 3. **Cost = `3T + 14`.** Deadline `A` = `AgentSettings.agent_request_timeout`
(`settings.py:2625`) applied at `routes.py:3005`. The two settings are in DIFFERENT Pydantic
classes (`LLMSettings.request_timeout` `settings.py:448` has **no range constraint at all**).
**#1292's ARITHMETIC IS WRONG:** it says `3T+2+4` and proposes `T <= 38` safe. The third
backoff IS spent. Real boundary at A=120 is **35** (35->119 FITS, 36->122 BREACHES,
38->128 BREACHES). A lane writing the issue's "38 passes/40 fails" matrix **pins a breaching
config as safe**. Brief carries the corrected table.
**#1278's OWN FIX DIRECTION 3 IS A PROVEN NO-OP:** tripwire on `registry.py:820` — mutation-
bites control fires under forced UNHEALTHY+breaker-disabled; real outage shape is byte-
identical to baseline and never reaches it. Breaker (3) and health record (3) cross at the
same failure; breaker is checked one layer earlier. **Direction 4 already shipped**
(`exception_handlers.py:135,190-191` returns 503 + `Retry-After: 30`).
**THE SEAM:** mid-ladder cancellation kills the sequence before enough failures accumulate to
open the breaker. Control (ladder finishes): turn1 23.04s->503 opens, turns 2-3 0.00s->503.
Treatment (cut): 4.0s->504 x3 before opening.
**PRODUCTION IS IN THE BREACHING CONFIG** (kubectl-verified): gemini LLM=120 / AGENT=240 ->
`3*120+14 = 374s` vs 240s, **1 failure/turn**, breaker needs 3 -> **3 full 240s opaque 504s
(12 min)** before fast-fail. Harm is OUTAGE-CONDITIONAL, not currently firing.
**Owner decided: NO ConfigMap lever this round** — the lane must make the ladder correct at ANY
config, which is strictly stronger than tuning two numbers into agreement.
**#1290 dependence proved by mutation:** reverting its two decision points collapses the hang
ladder to 1 attempt = T, which fits. NOT a #1290 regression — a coupling #1290 made reachable.
Fix must NOT un-declare the timeout as retryable (restores #1287's bug).
Stale locators corrected in brief: `llm_error_handler.py:202-205` -> `208-215`;
`registry.py:781-787` -> the reasoned-about lines moved to `819-820`.

### L2 — #1234  ROOT: status publishes configured intent where its own field contract promises effective reality
Branch `fix/1234-status-reports-effective-not-intended`. `Closes #1234`.
Reproduces (PM-verified, opik genuinely absent here): config on + SDK absent -> `enabled=True`
while `OPIK_AVAILABLE=False` and `health_check()` says `degraded`.
Anchors `admin_config.py:528`, `:540-541`; `tracing.py:70,:231,:422-423`; contract
`api/models.py:482` `enabled: "Feature is active and usable"`.
**THE ISSUE'S OWN PREMISE IS FALSE** (PM-verified): it claims every other entry echoes config.
`admin_config.py:523-525` already conjoins (`enable_web_search and has_tavily`) and `:590`
`suggestion_store_worker_safe` is an explicit RUNTIME probe (added in #1227). `llm_tracing` is
the **outlier** -> a bug, not a design call.
**WIDENED to the whole `features` dict as a POPULATION** — 2 known-correct controls +
1 known-broken makes the pin discriminating rather than tautological. Count floor required.
Hazards briefed: (1) conjoining inside `enabled` needs NO schema change; adding a field or
changing `enabled`'s meaning pulls in `api-contract-drift` + doc regeneration; (2) do not tidy
`has_api_key` here; (3) **this box has no opik so a one-directional pin is green for the wrong
reason** — SDK-present arm must monkeypatch `tracing.OPIK_AVAILABLE`.

### L3 — #942 (+ extension)  ROOT: the harness may substitute what is ABSENT, never what is PRESENT and under test
Branch `fix/942-harness-substitutes-what-it-claims-to-test`. `Closes #942`.
Arm 1 (as filed): 8 stand-ins armed with `ValueError` on `find_spec` (control: hand-built
ModuleType raises). Fix takes armed **8 -> 0**. Blast radius measured: **1 failed / 2688
collected** — `test_no_eager_embedding_load.py:439` (`__spec__ is None  # the trap`).
25 `sys.modules` write sites, **not the 14 the issue states**.
Urgency corrected honestly: production exposure **nil** — zero direct `find_spec` calls in
`faultmaven/` outside the central helper, which catches `ValueError` and is AST-guarded.
Arm 2 (**the extension, same seam**): `tests/conftest.py:328,340,353,592,598,604` replace
THREE REAL product modules with `SimpleNamespace` — `core.processing.log_analyzer`,
`observability.alerting`, `observability.apm_metrics`. `log_analyzer.LogProcessor` resolves to
a `Mock`. All three exist on disk with production importers, and
`tests/infrastructure/test_observability_core.py:209-216` already hand-deletes the stub to
reach the real class — the "worked around locally, trap still armed" pattern #942 was filed
about. **Arm-2 blast radius UNMEASURED — lane must measure and REPORT BEFORE committing;
explicit authority to narrow to arm 1, but not silently.**
Also on the seam, to measure: `pypdf` stubbed at `:543` though really installed (**6.16.2**) —
sharpened by main's own pypdf security bump, so the suite cannot currently be evidence the
bumped path works; and stand-ins under packages that do not exist (`faultmaven.tools.web_search`
`:598`, `faultmaven.core.knowledge.ingestion` `:592`).
**Hazards briefed:** (1) repairing the trap makes `test_the_conftest_stand_in_still_reads_as_
obtainable` pass via a DIFFERENT path — green while no longer testing what it was written for;
must preserve coverage, not delete the line; (2) guard must NOT be a `sys.modules`-wide sweep —
27 entries are spec-less and 8 legitimately so; enumerate + count floor (precedent
`_MIN_FILES_EXPECTED = 200` at `test_optional_dependency_detection.py:35`).
**#947 and `tests/unit/api/conftest.py` EXPLICITLY OUT OF SCOPE** (different root).

## OWNER QUEUE
1. **Close #1117 + #1118** — fixed by #1125, deliberately left open ("owner decides").
2. **Close #982** — fixed by #997 + #1005 + migration 041. Stale-open.
3. **#1293 / #1295 CYCLE** — #1295 needs #1293 fixed; #1293 needs a precision/recall decision.
   Only the owner can cut it. Third path: build the purpose-built labelled set.
4. **#828** — reproduces (two-process restart probe: both JTI and user watermark lost;
   fakeredis ACKs `SAVE` and persists nothing). **NOT lane-ready — needs a direction decision.**
   HAZARD: the revocation check runs during token validation, BEFORE any tenant is bound, and
   `_is_revoked` is **fail-open by design** (`auth_service.py:619-644`). A new table following
   the DEFAULT pattern (RLS + `_tenant_isolation`, as 018/023/028/030/041 all do) would read
   empty on the auth path -> **revocation silently stops working in Cloud**. Precedent exists
   and the repo knows it: 038 and 039 both carry `# No ENABLE ROW LEVEL SECURITY here, on
   purpose` for unauthenticated readers. *A security fix that makes security worse, and it is
   the default thing to do.* Also: 031 dropped the table for having **no writer**, not because
   durability was rejected — so this is NOT re-litigating 031, but 031's principle against a
   second persistence backend IS binding (replace the store, never add one). Separately, the
   watermark-TTL-from-current-config issue on the thread survives any durability fix.
   Size: ~300-500 LoC + migration.
5. **#947** — free deletion. PM-verified independently: **778 passed with the conftest, 778
   passed with it deleted** (`-1478/+0`, zero effective consumers in 13,304 collected; every
   fixture it defines has 0 external references). Anchors drift -25 (file is 1478 lines, issue
   says 1503; `except ImportError` at 1262 not 1287). **Brief it as DELETION** — the issue's
   own first-listed fix direction ("point the import at wherever those routers live now") would
   newly execute ~1160 lines of never-run fixture code for ZERO consumers.
6. **#752** — reproduces. `include_deleted` **has no referent**: cases are hard-deleted
   (`models.py:31-33`), `CaseState` has no deleted member, and `MinimalCaseService` "honours" it
   by excluding CLOSED — conflating a disposition with a deletion. Honest fix implements
   `include_terminal`, **REMOVES** `include_deleted`, fixes the fallback divergence
   (`_container_impl.py:995,:1003`). Existing test already passes both no-op params
   (`test_list_session_cases_pagination.py:67-68`). **Blocked on a cross-repo consumer check** —
   making `include_terminal=False` work removes resolved/closed cases a live endpoint returns
   today (dashboard/copilot).
7. **CLAUDE.md stale**: says `agent_executions`/`agent_tool_calls` "remain… Dropping them needs
   a migration and is a separate decision." **Migration 041 dropped both.**
8. Beta gates #1251/#1252/#1016/#1169/`FaultMaven/faultmaven-slack-agent#25` — unchanged.
9. Carried from round 6, unfiled: orphan-sweep Prometheus counters structurally unscrapable so
   `evidence_orphan_file_rate_high` cannot fire (infra, independent, fileable now); duplicate
   ROOT nodes in chain ingestion; runbook titles absent from embedded text (16/1297 — HELD);
   `POST /knowledge/search` still pure-vector (HELD). `FaultMaven/faultmaven-enterprise-infra#294` open.

## FILING CANDIDATES FROM MEASUREMENT (PM decides; nothing filed yet)
- `local` provider ignores `LLM_REQUEST_TIMEOUT` — `PROVIDER_SCHEMA["local"]["timeout"]=60`
  hardcoded (`registry.py:154`) and preferred over `timeout_for_provider()` (`:467`), so the
  project's own documented `{"ollama": 600}` example never reaches the aiohttp client.
- `.env.example:406` documents `LLM_REQUEST_TIMEOUT` as having no enforced range beside
  `{"fireworks":180,"ollama":600}` examples that breach a 120s ceiling 3-15x.
- `derive_kb_context_metadata` never sets `domain` (`agent/tools/base.py:59-61`), so the entire
  `ctx_domain` branch of `_compute_metadata_score` is unreachable in production (HELD area).
- A chunk with NO `status` outranks all 182 shipped `draft` runbooks on the metadata signal
  (0.2308 vs 0.1538) — an unfrontmattered upload gets a structural edge (HELD area).
- Mis-marked sync tests: `@pytest.mark.asyncio` on non-async defs at
  `tests/unit/infrastructure/llm/providers/test_openai_messages.py:467,:544`.

## PM CORRECTIONS TO ITS OWN BRIEFS (all three measurement agents refuted something)
- Carried round 6's "`_rerank` runs only inside `hybrid_search`, sole caller
  `document_qa_tool.py:442`" forward. **WRONG: `hybrid_search` has TWO callers** —
  `knowledge_service.py:669` (seeding) is the second, so `_rerank`/`_compute_metadata_score` DO
  run on the seeding path. What is single-sited is `context_metadata`.
- "#710's premise changed once hybrid was wired into seeding" — wrong direction. Hybrid IS
  wired in; the block simply never executes there. NARROWED, not changed.
- Hypothesised #982 shared a root with #1278 ("a configured value the executing path never
  reads"). **Refuted on both halves:** #982's config no longer exists, and #1278's health signal
  IS read (`should_attempt()` in `_get_routing_order()`) — it just cannot bite before the breaker.
- Hypothesised #942 + #947 were one lane by root. **Refuted:** different files, different
  mechanisms (missing dunder vs swallowed exception), blast radii 13,304 vs 0. But the instinct
  was right elsewhere — #942's true same-seam neighbour is the module shadowing INSIDE
  `tests/conftest.py`.
- Worried that repairing #947's fixture could turn a green suite red. **Number is zero.**
- Undercounted `reasoning_intent` occurrences by 4-6x.
- Said the P0 was "live in production right now". More precisely: the MISCONFIGURATION is live;
  the harm is OUTAGE-CONDITIONAL.

## STANDING METHOD RULES (in every brief)
Fails-before by REVERTING, not asserting. Targeted mutation for invariants true on both sides,
and the mutation must BITE. Read every new guard ADVERSARIALLY — what shape satisfies the check
while violating its intent (empty vs disjoint, substring vs token, one element vs many)?
Never a success criterion stated as a count of zero or a single fixture. SATISFIED / NOT
SATISFIED / COULD NOT ASK. Positive control before any "clean" claim. Never `--timeout`
(pytest-timeout NOT installed — dies instantly, exits 0). Report COLLECTED COUNT. Real pytest
PID, not the `nohup` wrapper. `PYTHONPATH` + assert `faultmaven.__file__`. Never `git add -A`.
Restore from `cp` backups, never `git checkout <path>`. Do NOT run isort.

---

# LANE OUTCOMES (live — re-verify, do not trust)

## L3 — #942 — COMPLETE, PR #1298, CI 15/15 green (head `bfdd3624e`)
Branch `fix/942-harness-substitutes-what-it-claims-to-test`. Body `Closes #942` only.
Base `main` at `4558959c1`, mergeable, **checks verified at STEP level** (not badge): 15 success.
4 files, **+636/-104**: `tests/conftest.py` (+104/-90), `tests/unit/test_harness_stand_ins.py`
(NEW, +492), plus repairs to `test_observability_core.py` and `test_no_eager_embedding_load.py`.
Shape: all 12 remaining substitutions routed through ONE helper `_install_stand_in`, which
attaches a real `ModuleSpec`, **refuses first-party names outright**, and records what it
substituted in `HARNESS_STAND_INS`. Seven bad stubs DELETED (6 `faultmaven.*` + `pypdf`), each
replaced by a comment saying why.

**ARM-2 BLAST RADIUS = ZERO, three independent ways** (narrowing authority not needed):
- Targeted A/B, 6,877 selected (every production importer of the shadowed modules + pypdf
  consumers): BASE 6800P/77S, FIX identical, 0 new.
- Whole-suite A/B same box/mode: BASE 12F/13166P, FIX 12F/**13182P** — 0 new, 0 fixed, same 12
  both sides. `13182-13166 = +16` = exactly the 16 new tests. The 12 are the agent's rsync copy
  method (excluded `.git`/`.gitignore`); positive control: those 41 tests pass in the real worktree.
- CI whole tree x2: Test Cloud **13,158 passed / 0 failed**, Test Standalone **13,118 / 0**.

**12 mutations, every one bites.** Spec-stripping reds 2/16; re-shadowing `log_analyzer` reds
4/16 **plus** `test_data_processing_has_tracing`; re-stubbing pypdf reds 2/16; a MISLABELLED spec
(resolves but lies) reds 1/16; registry-stops-recording reds 1/16. It also mutated **its own
guard**: neutering the AST scanner is caught ONLY by its positive control — the scan itself stays
green, which is why the control exists.

**PM's hazard 1 was WRONG, and the lane refuted it correctly (PM re-verified in source).**
I claimed the repaired test would "pass via the `find_spec` path instead". It cannot:
`optional_dependency.py` does `imported = sys.modules.get(name); if imported is not None: return
module_is_usable(...)` — it returns BEFORE `find_spec`, and the docstring says so explicitly
(*"``sys.modules`` is consulted FIRST … find_spec RAISES ValueError for a name in sys.modules
whose __spec__ is None"*). Coverage was never at risk from the spec; only the ASSERTION encoding
it was. Lane pinned the branch DIRECTLY (booby-trapped `find_spec`) and proved it beats the naive
fix: under a combined mutation, "just delete the assertion" -> **1 passed** (coverage silently
lost) vs shipped repair -> **1 failed**.

**Lane found a defect in ITS OWN guard and removed it** — `_MIN_STAND_INS = 10` sat beside a
required-set of 10 names, so the floor was logically implied by another assertion in the same
test and could NEVER fire. Exactly the "floor below the real population" failure the brief
warned about. Surviving floors both bind, both mutation-proven: `_MIN_CONFTESTS_SCANNED = 8`
(12 measured, mis-resolution lands at 0-1) and `_MIN_FIRST_PARTY_EXAMINED = 60` (87 measured).

**Further refutations of the brief (measured):** issue says 14 stand-in sites, there are **25**;
armed set is **11 not 8** (`SimpleNamespace` stubs have no `__spec__` ATTRIBUTE at all, so they
raise too); arm 2 is **6 shadowed modules not 3** — `apm_integration` was missing from the PM's
list. The brief's urgency correction (production exposure nil) was CONFIRMED and stated in the PR.

**L3 candidates for PM to rule on (lane filed nothing):**
1. `ctypes` substituted on healthy boxes — conftest guards on not-yet-*imported*, not
   not-*installed*; registry proves it fires here. Exempt under the shipped invariant
   (deliberate third-party substitution) and product code never imports it. Left alone.
2. `sklearn` really installed and stubbed — now MORE relevant: un-shadowed `log_analyzer`
   imports `IsolationForest` and gets a `Mock`.
3. Dead fail-open `except Exception: WebSearchTool = Mock` with ZERO readers. Different
   mechanism (no `sys.modules` write) so out of this PR's invariant.
4. A second dead `del sys.modules[...]` in `test_observability_core.py::test_knowledge_base_has_
   tracing`, for a name the conftest never stubbed — dead before AND after; lane left it rather
   than manufacture a causal story. (Correct call.)

`tests/unit/api/conftest.py` / #947 untouched, as instructed.
**PM review round `/code-review 1298 high`: RUNNING** (adjudicate against head `bfdd3624e`).

## L2 — #1234 — PR #1297, CI 15/15 green (head `6c61c7eaf`), REVIEW ROUND 1 DONE — 2 BLOCKING
Branch `fix/1234-status-reports-effective-not-intended`. `Closes #1234`. 2 files, +636/-21
(`admin_config.py` +151/-21, `test_admin_config_endpoints.py` NEW +485).
Lane's own result: population enumerated (4 features), **3 of 4 were lying** — `llm_tracing`
false-positive (#1234), `first_party_consent_skip` false-positive, `web_search` FALSE-NEGATIVE
(off on a working Google-CSE deployment), `suggestion_store_worker_safe` correct (kept as
control). Revert of `admin_config.py` reds **9 of 52**. Six mutations, all bit. Lane found and
fixed a defect in its own sweep pre-push (hardcoded `parametrize` list beside the registry).
Local serial suite **13,200 passed / 0 failed / 119 skipped**.
**Lane refuted the PM's brief 3 ways:** (1) `web_search` was NOT a "known-correct control" — it
is a liar; PM and issue were each half right and both missed Google CSE. (2) `ENABLE_WEB_SEARCH`
has **no production reader at all** (PM independently confirmed on main: only `settings.py:1860`
+ the endpoint). (3) **THE FOUR GATES ARE NOT SUFFICIENT** — `tests/unit/architecture/
test_architecture_boundaries.py::test_api_layer_boundaries` is a FIFTH guard `lint-imports` does
not cover; all four gates were clean and CI still failed. **Propagated to L1 mid-flight.**

### `/code-review 1297 high` — 12 findings. PM SUBSTANTIATED THE TOP 3 BY EXECUTION.
- **BLOCKING 1 (PM-confirmed by source): the fix does NOT fix #1234 where the SDK IS present.**
  `_llm_tracing_is_effective` = `opik_enabled and OPIK_AVAILABLE` — **2 of 3 gates**.
  `tracing.py:450-458`: with neither `opik_use_local` nor `opik_url_override` set,
  `init_opik_tracing` calls `_disable_sdk_tracing()` and returns *"Opik enabled but no URL
  configured … Tracing will be disabled."* So on the `[cloud]` extra with `OPIK_ENABLED=true`
  and no URL, the predicate says True and no span is recorded — **#1234 verbatim, surviving its
  own fix**. The suite PINS it: `_scenario_llm_tracing` leaves URL unset and asserts True.
- **BLOCKING 2 (PM-confirmed, SHARPER than the review): the population sweep cannot
  discriminate for half its members.** PM applied the pure-settings mutations:
  `TestEveryFeatureReportsEffectNotIntent` = **9 passed / 0 failed under BOTH**. Consent-skip
  mutation IS caught — but by a DIFFERENT targeted test
  (`test_consent_skip_reports_inactive_when_a_required_half_is_missing[oauth-disabled]`).
  **Web-search mutation alone is caught by NOTHING: 52 passed in-file, 930 passed / 1 skipped
  across `tests/unit/api` + `tests/unit/architecture`.** Positive control: consent-skip mutation
  does red in the same harness, so the instrument is live. Mechanism: only 2 of 4 members
  withhold a non-knob capability; for the other 2 the "reality" parameter is another setting, so
  the sweep degenerates to "a conjunction of settings is a conjunction."
- **DECISION relayed:** dropping `ENABLE_WEB_SEARCH` leaves it with ZERO readers, so
  `ENABLE_WEB_SEARCH=false` now reports `enabled: true` — a REGRESSION vs main, while
  `.env.example:265`, `quickstart.md:364` and CLAUDE.md still advertise it. Lane must wire it or
  de-document it in this PR.
- 9 more relayed unsubstantiated for the lane to judge (consent-skip omits `oauth_allowed_clients`
  / `oauth_redirect_uri_patterns` / `oauth_require_consent`; `has_api_key` conjoins
  `WEB_SEARCH_ENGINE_ID`; SecretStr vs `str(key)` divergence; settings-recompute vs composed
  object; asymmetric exception handling -> 500 for the whole endpoint;
  `test_config_hint_names_the_install…` pins nothing (reverting to main's hint still passes —
  "cloud" satisfied by "Opik cloud key"); `has_api_key` from `opik_use_local` alone; 4 copies of
  the SecretStr idiom).
**L2 RESUMED for round 2.**

### `/code-review 1298 high` — 13 findings. PM SUBSTANTIATED THE TOP 2 BY EXECUTION.
- **BLOCKING 1 (PM-confirmed with a LIVE CONTROL): the AST scanner misses 6 reachable shapes.**
  Both positive controls CAUGHT; **6 of 6 non-control shapes MISSED** — local-helper indirection
  (**the shape the helper itself has**), `from sys import modules`, `import sys as s`,
  `match`/`case` (`_walk` never visits `node.cases`), `setdefault` in an `if` header
  (`_check_calls` only runs on Assign/AnnAssign/AugAssign/Expr), tuple target. Levels 2 and 3
  do not cover it (both iterate `HARNESS_STAND_INS`, helper-installed by construction; runtime
  sweep scoped to `faultmaven.*`), so a spec-less THIRD-PARTY stand-in re-arms the #942 root
  invisibly. The control at line 209 pins `lines == {3,4,6,7,8}`, so adding a `match` case reds
  the CONTROL not the scanner.
  *PM METHOD NOTE: the PM's first probe was broken (missing `label` arg) and read every case as
  CAUGHT — a failed probe reads as a pass in BOTH directions. Caught only by re-checking the
  signature.*
- **BLOCKING 2 (PM-confirmed by execution): the guard file LEAKS a stand-in into `sys.modules`.**
  Appending `assert "l3_probe_third_party" not in sys.modules` -> **1 failed, 16 passed**.
  `monkeypatch.delitem(..., raising=False)` on an ABSENT key records no undo entry, so the module
  created at line 285 survives the session. The comment at 279-281 asserts the opposite — in a
  file whose whole subject is that the harness must not leave stand-ins in `sys.modules`.
- **HIGH (relayed, PM judges it the most consequential):** removing the observability stubs makes
  `alert_manager` / `metrics_collector` / `apm_integration` REAL MUTABLE SINGLETONS written by
  `PerformanceMiddleware` on EVERY request, with no reset fixture. The lane's A/B was clean only
  because **no test asserts on `/metrics/*` today** — so the zero-delta is real but does not bound
  the risk. The conftest comment claims "no threads, no I/O, no asyncio tasks"; both `__init__`s
  call `get_settings()` at import and `_schedule_notification` does `asyncio.create_task` with an
  `asyncio.run` fallback that raises inside a running loop.
- Also relayed: `read_text()` with no encoding dies under POSIX locale; a SECOND dead
  `del sys.modules[...]` at `test_observability_core.py:218-232`; **two PRODUCTION docstrings now
  false** (`optional_dependency.py:102`, `model_cache.py:47-48` both cite the doubles as
  spec-less) while the diff touches no production file; early-return skips spec+registry;
  `_REQUIRED_STAND_INS` vs conditional install; `__spec__` mutation aliasing; in-process
  `sys.modules` assertion where `test_import_isolation.py` already ships a subprocess probe;
  ~120 lines of now-unreachable ctypes blocks; two micro-cleanups.
**L3 RESUMED for round 2.**

## L3 ROUND 2 — PM-VERIFIED FIXED (head `b649bc8f5`, CI 15/15)
PM re-ran ITS OWN probes unchanged against the new head:
| probe | round 1 | round 2 |
|---|---|---|
| 6 scanner evasion shapes | **6 of 6 MISSED** | **0 of 6 missed** |
| 2 negative controls (one READS `sys.modules`) | — | **0 false positives** |
| external leak assertion | **1 failed / 16 passed** | **34 passed** |
Negative controls added deliberately: a repaired scanner that flagged EVERYTHING would also show
"0 missed", so the fix had to be shown not to have traded a blind spot for noise.
**Lane REFUTED the ctypes finding and PM CONFIRMED the refutation is right — the review was
INVERTED.** Probe: `_ctypes` is REAL (lib-dynload `.so`), NOT in the registry; `ctypes` IS in the
registry. So the block that fires is the one with the WRONG guard (`if "ctypes" not in
sys.modules` = not-yet-*imported*, not not-*installed*), and the correctly-guarded
`try/except ImportError` blocks are the dead ones. Deleting "the later ones" would have removed
the CORRECT fallbacks and kept the incorrect one. Lane also declined to un-fake stdlib `ctypes`
inside this PR (affects numpy/protobuf/chromadb) and escalated instead — right on both counts.
Lane also refuted the early-return finding (present name is either the real module or an earlier
stand-in; both already carry a spec).
F6 accepted in full: autouse reset fixture AND the corrected comment, field list pinned by a test.
8 mutations, all bite. **Lane disclosed a BROKEN mutation of its own** (G showed "NO REDS"; an
inline comment had swallowed a closing paren) — the mirror of the PM's own broken probe this
session. A broken mutation reads as a safe guard.
Honest COULD NOT ASK: the `LC_ALL=C` encoding repro could not be reproduced (box coerces UTF-8);
fix applied on its own merits.
Whole tree **13,212 passed / 119 skipped / 0 failed**, reconciled `13182 + 12 artifacts + 17 new`.
*PM note: a `sqlite3` row in the PM's ctypes probe showed `__file__=None` — that was ABSENT from
`sys.modules`, not stubbed (`getattr(None,...)` returns None). Not a finding; not reported.*

## L1 — #1278 + #1292 — COMPLETE, PR #1300, CI **16/16** (head `966df1a31`)
15 files, **+1608/-44**. `Closes #1278` + `Closes #1292`. Base main, mergeable.
New `core/investigation/turn_budget.py` carries the deadline in a **ContextVar**, bound in
`routes.py` at the same site that applies the turn-wide `wait_for`. `with_retry` (a) CLAMPS every
attempt to what remains — unconditional at any config — and (b) REFUSES a retry whose backoff +
another attempt of the worst cost **observed in that ladder** will not fit (observed, not
configured `T`, so the handler never re-derives the router's resolution and a fast-failing
provider keeps every retry). Early stop -> `TURN_BUDGET_EXHAUSTED` -> 503 + `Retry-After: 30`.
**Unbound context = unbounded**, so jobs/CLI/direct-call tests are untouched.
Revert/mutation: 186 collected, 186 pass; R1 4 · R2 1 · R3 2 · R4 7 · M1 5 · M2 8 · M3 10 · M4 13
· M5 3 · M6 2 · M7 1. R2 and R3 red DIFFERENT tests — neither half redundant.
Whole tree **13,246 passed / 0 failed / 119 skipped** (34m31s). All five gates.

### PM CORRECTIONS FROM L1 — both PM errors, both verified
1. **The PM's ROOT STATEMENT was wrong on the breaker** (and it was in the PR body). "Lets the
   breaker latch on turn 1" holds only where the budget affords 3 attempts. At the cluster shape
   it affords **ONE** — the PM's own production note said "1 failure per turn, breaker needs 3"
   and the PM did not carry it through. What actually changes at T=120/A=240: each of those three
   turns goes **240s opaque 504 -> 120s honest 503**. Corrected in the PR body.
2. **The PM OVER-CORRECTED #1292's arithmetic.** PM-verified table:
   | T | full 3T+14 | fits | paid 3T+6 | all 3 paid fit |
   |---|---|---|---|---|
   | 35 | 119 | YES | 111 | YES |
   | 36 | 122 | no | 114 | YES |
   | 38 | 128 | no | 120 | YES |
   | 39 | 131 | no | 123 | no |
   **TWO boundaries.** 35/36 = config coherence (whole ladder fits). 37/38 = a PAID ATTEMPT is
   lost — the one a user feels, because the trailing 8s backoff precedes an attempt the OPEN
   BREAKER REFUSES ANYWAY. Calling #1292's "38" simply *wrong* overstated it; it was right for
   the metric that matters. Lane pins BOTH, at real values, against a no-clock planner, plus a
   population invariant and a guard that the table still spans both verdicts and >1 attempt count
   ("a parametrised table whose rows all agree is a single fixture in disguise").
3. **#1278's direction 2 (health as trigger) is UNNECESSARY** — measured: 2, 3, 5, 10, 20 and 50
   ladders in one turn all cost **3** provider calls on budget alone; health never consulted.
4. **The four gates were insufficient — L1 hit it INDEPENDENTLY before the PM's warning arrived.**
   `test_api_layer_boundaries` failed on `admin_config.py` importing core+infrastructure; fixed by
   moving composition to `config/retry_budget.py`. `lint-imports` passed throughout.
**Three defects in its OWN guard, found pre-ship** (as predicted): (a) reserve was a DEFAULT
ARGUMENT, so a scaled test silently measured a zero budget and refused every attempt — which
*looks* like the guard working; (b) the no-budget exit returned the previous `RETRY` result whose
`error_code` is None, falling through to a bare **500** — worse than the 504 it replaced (M7 pins
it); (c) after widening a timing margin R2 killed nothing, so it rebuilt a discriminating test
where the un-gated ladder is cancelled ASLEEP in a 2s backoff.
L1 candidates (unrelated, not filed): `generate_with_truncation_retry` is a SECOND unbudgeted
in-turn retry (worst case 2T; at cluster shape 2x120 = the whole turn) — per-consumer design call,
3 of 5 call sites are outside a turn, **owner decision**; `docs/architecture/investigation-engine/
error-handling-and-recovery.md` documents an `LLMErrorHandler` that no longer exists (and the
handler's docstring names it as its Design Reference); `modules/case/api/routes.py` carries 30
F401 unused imports (CI's ruff selection excludes F401).

## ⚠ CROSS-LANE COLLISION — #1300 and #1297 (PM-detected, both lanes informed)
Both modify **`faultmaven/api/routes/admin_config.py`** AND
**`tests/unit/api/test_admin_config_endpoints.py`**. #1300 adds a **FIFTH** entry
(`llm_retry_ladder_fits_turn_budget`) to the very `features` dict #1297 audits as a POPULATION
with an exhaustiveness guard — and one of L2's own round-1 mutations was "5th unclassified
feature -> red". **That guard firing is CORRECT.** PM told L2 to keep it STRICT (weakening it to
tolerate unknowns converts the population rule into an opt-in list) and that the new field needs a
THIRD taxonomy slot: its subject genuinely IS the configuration, so reading it from settings is
reading reality, not the anti-pattern.
**PM RECOMMENDS MERGE ORDER: #1300 FIRST, then #1297** (P0, finished; L2 was open for edits).

## L2 ROUND 2 — PM-VERIFIED FIXED (head `8f72ede80`, CI 15/15)
**Blocking 1 fixed at the ROOT, not by adding a third conjunct** — "re-deriving is what caused
it", so the answer is no longer derived at the endpoint: `init_opik_tracing` RECORDS whether it
reached a configured backend, and `tracing_is_effective()` combines that with the SDK's live
`is_tracing_active()`. `_llm_tracing_is_effective()` now takes **no arguments at all**.
Measured: no-URL True->**False**, `OPIK_TRACK_DISABLE` True->**False**, config raised
True->**False**, working backend True->True. Added `test_tracing_is_effective.py` (8 tests)
driving the REAL function, closing the gap that endpoint tests monkeypatching the flag could not.
**Blocking 2 fixed — PM VERIFIED WITH ITS OWN UNCHANGED MUTATION.** Round 1: the pure-settings
web_search mutation was caught by **NOTHING in 930 tests**. Round 2, same mutation:
**5 failed / 62 passed**, including
`TestEveryFeatureReportsEffectNotIntent::test_configured_but_capability_absent_reports_disabled[web_search]`
— **the sweep itself now catches it.** Each member now withholds a real runtime fact (recorded
tracing outcome, composed tool on `app.state`, mounted OAuth authorize route, composed repository).
Lane then found **its own anti-vacuity guard could be vacuous** (a constant stand-in would satisfy
`test_a_pure_settings_implementation_fails_this_sweep`) and pinned each stand-in to report False
on unconfigured settings too.
**F5: lane WIRED `ENABLE_WEB_SEARCH`** rather than de-documenting — `false` had still handed the
model a tool that sends investigation text to a third party. Honoured at the REGISTRY (the
decision the knob makes), default True so no unset deployment changes.
Rejected 2, both stated in code and PR: redirect-pattern containment (regex-admits-regex is
UNDECIDABLE — limit named in the docstring; the decidable clients half IS checked) and
`has_api_key` from `OPIK_USE_LOCAL` (self-hosted needs no key, so the field correctly says no
credential is owed; "is it working" is `enabled`, now correct).
Cross-lane: lane SIMULATED the fifth entry — guard reds with an actionable message naming the key
and both registration paths. Added the missing taxonomy: TWO ways to comply — runtime-verified,
and **configuration-derived** (subject IS the config; owes a True-AND-False pair, since its
failure mode is being a CONSTANT, not echoing an instruction). *"The distinction is what the field
CLAIMS, not where its bytes come from."*
Fails-before: reverting all four source files reds **22 failed + 8 errors of 74**.
Whole suite **13,226 passed / 0 failed / 119 skipped**.

## ROUND 7 STATUS AT THIS POINT — 3 PRs open, all mergeable, origin/main still `4558959c1`
| PR | Lane | Head | Checks | Review |
|----|------|------|--------|--------|
| #1300 | L1 — #1278+#1292 | `966df1a31` | **16/16** | `/code-review 1300 high` RUNNING |
| #1297 | L2 — #1234 | `8f72ede80` | 15/15 | round 1 done, 2 blocking FIXED + PM-verified |
| #1298 | L3 — #942 | `b649bc8f5` | 15/15 | round 1 done, 2 blocking FIXED + PM-verified |

### `/code-review 1300 high` — 13 findings. PM SUBSTANTIATED THE TOP 2 BY EXECUTION.
- **F5 CONFIRMED (PM-verified empirically) — the clamp stops the BREAKER RECORDING.**
  Clamp is `await asyncio.wait_for(operation(), timeout=spendable)` (`llm_error_handler.py:833`).
  When it fires before the provider's own timeout the inner coroutine gets `CancelledError`.
  PM probe on this box: `CancelledError is Exception subclass? False`; outer `with_retry` catches
  `TimeoutError` (classification still runs — good); **base_client handlers that ran: NONE**.
  `record_failure` is reachable ONLY from `except asyncio.TimeoutError` (`:442`->`:453`) and
  `except Exception as call_error` (`:477`->`:519/:554`); grep for `CancelledError` across
  `llm_error_handler.py` + `base_client.py` + `turn_budget.py` = **0 hits**.
  **Reachability is NARROWER than the review implies (PM-derived):** on attempt 2+ the gate has
  already required `spendable >= backoff + worst_observed`, so after the backoff
  `spendable >= worst_observed ≈ T` and the provider's own timeout still fires first. The clamp
  preempts on the **FIRST** attempt, when `A - reserve < T` — i.e. exactly `T=180/A=120`, **a row
  in L1's own population table**. On those configs the breaker records nothing on ANY turn, never
  opens, and #1278's "the same full-budget failure repeats on every turn" persists indefinitely.
  **So the budget half of #1278 is fixed and the breaker half is not, on T>A configs.**
- **F1 OVERSTATED — PM REFUTED THE REVIEW and told the lane NOT to implement its suggestion.**
  Review claimed the gate loses attempts and breaks the retry policy on the shipped default. PM
  replayed L1's own `worst_case_ladder_plan` against its own `can_afford_next_attempt`:
  | T/A | PLANNER | RUNTIME |
  |---|---|---|
  | 30/120 shipped default | attempts=3 paid=3 full=104 fits=True | refused retry **4** at 96s; **paid=3** |
  | 35/120 coherence bound | attempts=3 paid=3 full=119 fits=True | refused retry **4** at 111s; **paid=3** |
  | 120/240 live cluster | attempts=1 paid=3 full=374 fits=False | refused retry 2 at 120s; paid=1 |
  **NO paid attempt is lost anywhere; planner and runtime AGREE on attempt counts in all three
  shapes.** The refused iteration is the breaker-refused one, and declining to sleep 8s before an
  attempt that returns instantly is arguably correct.
  The REAL residue is smaller: on a `fits=True` config a hung provider terminates with
  `TURN_BUDGET_EXHAUSTED` when the ladder completed all useful work and the breaker had opened —
  and that code's documented remediation (`exceptions.py:203`) sends the operator to
  `/admin/config/status`, which says `enabled: true`. A **LABELLING** defect (probably should be
  `PROVIDER_CIRCUIT_OPEN`), not a lost-retries defect. PM explicitly told L1 **not** to restructure
  the gate to model the breaker's state — that is the coupling L1 deliberately avoided when it
  refuted direction 2.
- Prioritised to the lane, unsubstantiated: **only ONE call site is budgeted** (`intent_resolver.
  route()` and `document_qa_tool.route()` also run inside `bind_turn_deadline` but reach the
  provider via `_resolve_timeout()` alone; with `{"ollama":600}` vs A=120 a hang burns the whole
  turn and returns the exact 504 at `routes.py:3046` the PR replaces — review's altitude fix is to
  clamp inside `LLMRouter._resolve_timeout`); and **`.env.example:407` + `CLAUDE.md:411` state the
  cost as `3xLLM_REQUEST_TIMEOUT+14` when the attempt cost is `max(override, base)`** — understating
  104s vs 374s on the cluster shape, directly above the `# LLM_REQUEST_TIMEOUT=30` line.
- Nine more relayed for the lane's judgement (budget gate shared with the
  `OutputTruncationError(cap_reached=False)` branch may skip the #662 COMPRESS_MEMORY degrade;
  planner models ONE ladder while a turn runs several; `describe_retry_ladder_budget` inside the
  endpoint's blanket try/except 500s the WHOLE status response; `wait_for` wraps in
  `ensure_future` on 3.11 but awaits inline on 3.12+ so ContextVars stop propagating and
  cancellation shape differs between the project minimum and CI; `resolve_chat_provider_name`
  placement; `resolve_request_timeout` re-reads env despite the `validation_alias`; the new
  `features` entry inverts the map's semantics; handler-per-backoff; a dead `inf` branch).
**L1 RESUMED for round 2.**

---

# ROUND 7 — TWO LANES MERGED, ONE READY

**MERGE ORDER INVERTED vs the PM's recommendation.** The owner merged **#1297 at 11:33:36**
(`919d2ab41`) and **#1298 at 11:34:00** (`59f0e76e1`) while L1 was in round 2. L1 detected this
itself and synced onto the new main rather than reporting a conflict.
`origin/main` also took `4db2a1c9d` (#1301, a peer workstream's idempotency fix) mid-round —
PM verified **zero file overlap** with all three lanes, using a positive control after a first
`comm` errored on sort order and returned a misleading "(none)".

**RECONCILED (what closed vs what was fixed):** #1234 closed 11:33:37 by #1297 ✓ · #942 closed
11:34:02 by #1298 ✓ · #1278 + #1292 correctly STILL OPEN (#1300 unmerged) ✓ · #1299 closed by the
peer #1301 ✓ · control #828 still open ✓. **Nothing closed unintentionally.**

## L1 ROUND 2 — PM-VERIFIED (head `a1f0c4471`, CI **16/16**, mergeable, contains all 3 merged commits)
**F5 fixed by moving the clamp, NOT by catching `CancelledError`** — the lane declined to record a
failure "from a handler that never observed one" and took the second option: the clamp now applies
to the timeout the call is GIVEN, in `LLMRouter._resolve_timeout` (`clamp_to_turn_budget`), so the
failure expires inside `call_external`, raises `ExternalCallTimeout`, and records.
**PM verified the MECHANISM directly, not the lane's table** — on the exact F5 scenario:
```
T=180 A=120:  clamp_to_turn_budget(180) = 119.00s   <- timeout the CALL gets
              backstop_turn_budget()    = 119.75s   <- with_retry's outer wait_for
              backstop expires LATER? True -> call times out first, breaker RECORDS
```
Reserves: `TURN_BUDGET_RESERVE_SECONDS = 1.0`, `TURN_BUDGET_BACKSTOP_RESERVE_SECONDS = 0.25`.
The ordering is what makes the recording path win, and it is asserted (M8 backstop-wins reds 14).
**The all-call-sites finding was folded in and it was NOT extra work — it is the same fix.**
`_resolve_timeout` has exactly one consumer, so clamping there also bounds `intent_resolver` and
`document_qa_tool`. `with_retry` keeps only a backstop for providers reached without the router.
**PM's `PROVIDER_CIRCUIT_OPEN` suggestion REJECTED, correctly** — claiming that code from the gate
would assert unverified breaker state and collide with the genuine path (which reports it because
it ASKED the breaker and got `CircuitBreakerError`). Verdict now turns on
`retry_count + 1 < max_retries` — structural, no breaker knowledge: 3 of 3 paid -> `RETRY_EXHAUSTED`
(the provider failed); fewer -> `TURN_BUDGET_EXHAUSTED` (the config; status page has something to
say). Lane then caught its own residue: the MESSAGE still said "the budget is spent" while
reporting `RETRY_EXHAUSTED` — the same misdirection in prose. Fixed.
**Lane found a weakness in its OWN mutation:** R5 originally reded only 1 because the breaker test
called the clamp directly, leaving the "router narrows -> narrowed call records" seam unpinned.
Added `test_the_router_applies_the_SHARED_clamp_not_a_local_min`; R5 now reds 2.
Mutations (244 collected): R5 2 · M8 14 · M9 2 · M10 10 · M11 12 · M12 1 · M14 1, plus the round-1
set re-run green. Baseline and restored both 244/244.
**Category-2 registry: #1297 NAMED the category but SHIPPED NO REGISTRY — PM verified**
(`CONFIGURATION_DERIVED_SCENARIOS`: **0** occurrences on merged `919d2ab41`, **6** on #1300).
L1 implemented it as specified, sweep widened to the union (asserted disjoint), pair proof in ONE
test because split across two either half passes alone against a constant. That file: **72 passed**.
Also fixed: truncation retry reports `TOKEN_LIMIT` so #662's degrade still runs; `.env.example` +
`CLAUDE.md` now name the per-provider override (they understated the cluster shape ~3x);
`fits` documents its one-ladder scope; handler hoisted; dead `inf` branch removed.
Rejected with reasons: blanket try/except (pre-existing + uniform; a targeted catch restores the
silent-absence mode the field exists to remove); `wait_for` 3.11/3.12 ContextVars (inert — every
write is at turn/request scope, outside the LLM call, and the primary clamp is now the provider's
own `wait_for` which predates this PR); `os.getenv` re-read (deliberate, mirrors
`LLMRouter.__init__` so the report cannot disagree with the router; pinned by a test);
`resolve_chat_provider_name` onto `LLMSettings` (reasonable, but it is the router's own resolution
rule shared to stop drift — candidate, not this lane).
Full suite on the merged tree: **13,373 passed / 0 failed / 119 skipped** (35m49s).
**PM re-ran all five gates on `a1f0c4471`:** black 1183 unchanged · ruff passed · import-linter
**13 kept / 0 broken** · API reference matches · `tests/unit/architecture/` **131 passed / 1 skipped**.

## FINAL ROUND 7 STATE
| PR | Issue | Head | Checks | State |
|----|-------|------|--------|-------|
| #1297 | #1234 | `8f72ede80` | 15/15 | **MERGED** `919d2ab41` |
| #1298 | #942 | `b649bc8f5` | 15/15 | **MERGED** `59f0e76e1` |
| #1300 | #1278 + #1292 | `a1f0c4471` | **16/16** | OPEN, mergeable, READY — owner merges |

## PM BRIEFS REFUTED BY LANES THIS ROUND (running tally: 8)
1. `_rerank` sole-caller claim (carried from round 6) — `hybrid_search` has TWO callers.
2. #710's premise "changed" — it NARROWED.
3. #982 shares a root with #1278 — refuted on both halves.
4. #942 + #947 are one lane by root — refuted; the true neighbour was inside `tests/conftest.py`.
5. `web_search` is a "known-correct control" — it was a LIAR (false-negative).
6. The four gates are sufficient — a FIFTH (`test_api_layer_boundaries`) exists; two lanes hit it.
7. The root statement's "breaker latches on turn 1" — config-dependent; at cluster shape it
   affords ONE attempt. What changes is 240s opaque 504 -> 120s honest 503.
8. "#1292's arithmetic is wrong" — OVER-CORRECTED. TWO boundaries: 35/36 config coherence,
   37/38 attempt lost. #1292's 38 was right for the metric that matters.
Plus: the PM's `PROVIDER_CIRCUIT_OPEN` suggestion was rejected with a better answer, and the
review's own F1 headline was refuted by PM measurement before the lane acted on it.

---

# ROUND 7 CLOSED — 3 of 3 lanes merged, 4 issues closed, reconciled
`origin/main` = `6b3592bf5`.
| PR | Issues | Merge commit | Merged |
|----|--------|--------------|--------|
| #1297 | #1234 | `919d2ab41` | 11:33:36Z |
| #1298 | #942 | `59f0e76e1` | 11:34:00Z |
| #1300 | #1278 (the only P0) + #1292 | `bc62febc0` | 17:32:04Z |
RECONCILED: #1234 closed 11:33:37 · #942 closed 11:34:02 · #1278 closed 17:32:05 ·
#1292 closed 17:32:06. Controls #828/#752/#1293/#1295 all still OPEN. **Nothing closed
unintentionally.** Every lane needed exactly TWO rounds; every lane was CI-green in round 1 and
still shipped a defect `/code-review <pr> high` caught — **7 for 7 across rounds 6 and 7.**

## ⚠ THE HELD AREA MOVED — first thing for the next round to re-verify
`6b3592bf5` = **#1302, "feat(kb): turn the KB cause seeder off by default, measured (#1295)"**,
merged 17:33:08 by a PEER workstream (12 files, +376/-60). Consequences:
- **#1295 is STILL OPEN with NO `closed` event** (timeline: cross-referenced, commented,
  cross-referenced, referenced). Its PR title uses `(#1295)` — **the same non-closing-keyword
  pattern as #1195 and #1293.** Determine whether that is deliberate (a decision issue tracking
  more than #1302 shipped) or the accident, using [[open-issue-may-be-a-deliberate-hold]].
- **#1293's premise has changed and MUST be re-measured before it is laned.** With push-seeding
  OFF BY DEFAULT, "16 of 51 labelled-negative pairs admit and 6 seed" is no longer a
  shipped-default harm. The PM/owner cycle recorded above (#1295 blocked on #1293, #1293 blocked
  on an owner precision/recall call) may now be CUT — but verify, do not assume.
- #1288 / #1167 / #1168 remain open; re-derive whether the hold still applies.

## OWNER QUEUE CARRIED TO ROUND 8
1. **Close #1117 + #1118** — fixed by #1125, deliberately left open (`Refs … not auto-closing —
   owner decides`). Mutation-proved: 3/1455, 24/1455, 24/141.
2. **Close #982** — fixed by #997 + #1005 + migration 041. Not dead code; not code at all.
3. **#828** (P1, security-correctness) — reproduces. NOT lane-ready: needs a direction decision.
   The revocation check runs BEFORE any tenant is bound and `_is_revoked` is FAIL-OPEN, so a table
   following the default RLS pattern would silently disable revocation in Cloud. 031's principle
   (replace the store, never add a second) is binding; 031 itself is not.
4. **#752** (P1) — reproduces, but `include_deleted` has NO REFERENT (cases are hard-deleted) so
   the honest fix REMOVES a shipped API param, and `include_terminal=False` drops cases a live
   endpoint returns today. Blocked on a dashboard/copilot consumer check.
5. **#947** — free deletion, PM-verified 778 passed both with and without (`-1478/+0`, zero
   effective consumers in 13,304). Brief it as DELETION, never "repair".
6. **CLAUDE.md stale**: `agent_executions`/`agent_tool_calls` described as remaining; migration
   041 dropped both.
7. Filing candidates from measurement: `local` provider ignores `LLM_REQUEST_TIMEOUT`
   (`PROVIDER_SCHEMA["local"]["timeout"]=60` at `registry.py:154`, preferred over
   `timeout_for_provider()` at `:467`); `generate_with_truncation_retry` is a second in-turn retry
   whose per-consumer refusal policy is an owner call; `docs/architecture/investigation-engine/
   error-handling-and-recovery.md` documents an `LLMErrorHandler` that no longer exists;
   `modules/case/api/routes.py` carries 30 F401s (CI's ruff selection excludes F401);
   mis-marked `@pytest.mark.asyncio` on sync defs at `test_openai_messages.py:467,:544`.
8. Beta gates #1251/#1252/#1016/#1169/`FaultMaven/faultmaven-slack-agent#25` — unchanged.
9. Round-6 carryover: orphan-sweep Prometheus counters structurally unscrapable
   (`evidence_orphan_file_rate_high` cannot fire); duplicate ROOT nodes in chain ingestion;
   `FaultMaven/faultmaven-enterprise-infra#294` open.

## STANDING NOTE FOR ROUND 8 — THE GATE LIST IS FIVE, NOT FOUR
`black --check` / `ruff --select E9,F63,F7,F82,I` / `lint-imports` /
`generate_api_docs.py --check` **plus `pytest tests/unit/architecture/`**. Two independent lanes
had all four clean and CI still failed on `test_api_layer_boundaries`, which `lint-imports` does
not cover. Recorded in [[lint-the-paths-ci-lints]].
