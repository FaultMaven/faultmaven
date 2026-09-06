# Review: PR #993 — observation-time provenance & closure_reason cleanup

**Diff range:** PR #993 (base: main, head: fix/observation-time-provenance)
**Skills loaded:** architecture, investigation-framework
**Files assessed:** 15 non-test files (docs + code) + 12 test files spot-checked

## Violations

- **docs/architecture/investigation-engine/investigation-data-models.md:914** — §2.4 "Diagnostic Feasibility Advisory Signal" says the terminal state for `rca_infeasible=True` cases is `CLOSED(solution_deferred)`. This contradicts both the new `derive_closure_reason` (which returns `closed_rca_infeasible` for any `rca_infeasible=True` + rationale case, at higher precedence than `solution_deferred`) and this PR's own doc changes elsewhere (framework-md line 702, data-models-md line 100). The correct label is `closed_rca_infeasible`.

- **docs/architecture/investigation-engine/investigation-lifecycle-logic.md:1826-1832** — The `derive_closure_reason` narrative is stale and internally contradictory. It claims the function returns `closed_insufficient_evidence` "when the case is closed from INVESTIGATING while in the `INSUFFICIENT_EVIDENCE` verification-status cell" — but the code (`terminal_transitions.py:446-450`) explicitly no longer keys on that cell, with a "This no longer keys on…" note explaining why. The next clause ("otherwise `closed_insufficient_evidence` — `mitigation_sufficient` reason was folded into the latter") is also wrong: `mitigation_sufficient` was un-folded by this PR, and the paragraph fails to mention `solution_deferred` and `closed_rca_infeasible` as intermediate branches.

- **docs/architecture/investigation-engine/investigation-lifecycle-logic.md:1824** — Enumeration of CLOSED closure_reason values omits `solution_deferred`. Lists only four (`inquiry_only`, `closed_rca_infeasible`, `mitigation_sufficient`, `closed_insufficient_evidence`) while `VALID_CLOSURE_REASONS` and `derive_closure_reason` both include five in this PR.

- **docs/architecture/investigation-engine/investigation-lifecycle-logic.md:1443** — §2.4.4 states `closure_reason = "mitigation_sufficient"` for the `rca_infeasible=True` path. Same defect as data-models.md:914 — this path now derives to `closed_rca_infeasible` because `_rca_declared_infeasible` is checked before `_mitigation_verified` in `derive_closure_reason`.

- **docs/architecture/investigation-engine/investigation-lifecycle-logic.md:693-694** — Comment inside `force_close_investigation` example says "closes as `mitigation_sufficient`" and then "the former `mitigation_sufficient` reason was folded in" in the same paragraph. Mutually contradictory.

- **docs/architecture/investigation-engine/investigation-data-models.md:102-104** — Same stale-fold contradiction: "a case stabilized by a verified mitigation closes as 'mitigation_sufficient'" immediately followed by "the former 'mitigation_sufficient' reason was folded in".

- **docs/architecture/investigation-engine/insufficient-evidence-handling.md:103** — File not touched by this PR, but the derivation description is now false: it claims "`derive_closure_reason` returns a distinct `closed_insufficient_evidence` reason when a case is closed from the `INSUFFICIENT_EVIDENCE` cell". Under this PR, `closed_insufficient_evidence` is the default fallthrough for any close from INVESTIGATING that isn't `solution_deferred` / `closed_rca_infeasible` / `mitigation_sufficient`; the cell no longer participates. The doc is authoritative for the insufficient-evidence design and cannot be silently out of sync with the shipped derivation.

## Concerns

- **docs/architecture/investigation-engine/two-dimensional-hypothesis-methodology.md:866** — Says the RCA-infeasible ladder cap "closes (`mitigation_sufficient`) on symptom absence". The table 12 lines lower (line 885) correctly shows `closed_rca_infeasible / mitigation_sufficient` for the same quadrant. Within a single doc the two locations should agree.

- **faultmaven/infrastructure/observability/funnel_metrics.py:145** — Inline comment "the effort query admits only the two effort-bearing close reasons" was accurate before this PR (2 reasons) but now the query admits four. Comment drift; the code below is correct.

- **faultmaven/core/investigation/terminal_transitions.py:411-467** — `derive_closure_reason` precedence (`solution_deferred > closed_rca_infeasible > mitigation_sufficient > closed_insufficient_evidence`) is load-bearing and documented at length in the docstring and in `case/domain/models.py`, but nothing outside the function pins the order — a future refactor reordering the `if` chain would silently change every stabilized case's reason. Consider extracting the order to a tested list of `(predicate, reason)` pairs. Not a violation.

- **faultmaven/core/investigation/symptom_currency.py:77** — `STALE_AFTER = timedelta(minutes=30)` is defined at module scope. Fine, but the design docs never name this constant or the threshold — the "when is a symptom stale?" answer lives only in code. A design-doc drift check would not surface a change to this number. Consider a one-line reference from `evidence-driven-investigation-framework.md` or `investigation-data-models.md` back to `symptom_currency.STALE_AFTER`.

- **faultmaven/core/investigation/symptom_currency.py:143-170** — `assess_symptom_currency` returns `NOT_APPLICABLE` for any `case.state != INVESTIGATING`. This is documented and matches the module docstring's scoping. Confirm the LLM cannot reach the retraction path from INQUIRY-shaped prompts (not fully traced) — if it can, the "unknown vs stale" scoping flips inside INQUIRY.

- **faultmaven/core/investigation/milestone_engine.py:944-1010** — `_apply_symptom_retraction` is module-private and the test suite imports it directly. Testing private helpers directly is an anti-pattern this codebase generally avoids — the same helper is not covered end-to-end via `_apply_investigation_updates`, so seam behavior (ordering vs the milestone loop, interaction with cause backstop legs) rests on integration coverage elsewhere.

- **faultmaven/modules/case/api/routes.py:154-186** — `resolve_paste_source_meta` is now module-scope (no leading underscore) for test import. If it's meant only for the test suite, a leading underscore would match the codebase's convention (see the neighbouring `_parse_observed_at`).

- **faultmaven/modules/case/api/routes.py:189-228** — `_parse_observed_at` silently swallows both malformed input and future timestamps by returning `None`. Given how load-bearing observation-time is now (drives `_symptom_currency_note` and the whole Zone-2 anchoring behavior), consider surfacing "observed_at was invalid" into turn metadata so a Slack-agent misconfiguration is diagnosable without log grepping. Not a violation.

- **docs/architecture/investigation-engine/investigation-lifecycle-logic.md:1990** — §4.6 "Abandoned/Escalated" says "a stabilized case closes as `mitigation_sufficient`, one that established nothing as `closed_insufficient_evidence`" — silently omits both `solution_deferred` and `closed_rca_infeasible`, which are two other reasons a §4.6 close can produce.

- **tests/unit/core/investigation/test_transition_alignment.py:698** — Renamed test `test_other_investigating_status_yields_insufficient_evidence` sets `verification_status = VerificationStatus.OPEN` and asserts `closed_insufficient_evidence`. Under the new derivation, `verification_status` is irrelevant — that field is never read by `derive_closure_reason`. The test passes because `closed_insufficient_evidence` is the default fallthrough, but the setup is misleading (the same anti-pattern was flagged and rewritten in `test_case_service.py`).

- Module boundaries all clean. `symptom_currency.py` imports `Case`/`CaseState`/`EvidenceCategory` from `faultmaven.modules.case.contracts` (correct). `terminal_transitions.py` newly imports `SolutionFeasible` from `case.contracts`. `report_generation_service.py` imports through `case.contracts`. `case/api/routes.py` imports its own module's domain/infrastructure (allowed). `investigation_service.py` (agent) imports `Attachment` from `core.investigation.schemas` (correct — Attachment is engine-owned). No cross-module infrastructure imports introduced.

- Observability changes (`funnel_metrics.py`) are in the right layer. Metric names unchanged; only the label domain expanded. Bounded-label seeding preserved so vanished series reset to zero — good.

## Overall Assessment

**Revise before ship.** The code changes (closure_reason unfolding, `symptom_currency` module, `_apply_symptom_retraction`, evidence coverage inheritance, `observed_at` provenance) are coherent, well-documented at the code layer, and respect module boundaries. Observability additions are correctly placed and named. Where this PR falls short is doc-code alignment: the same lifecycle document being modified in this PR carries at least four stale statements about `derive_closure_reason` that this PR's own code makes false, plus a doc-level contradiction between §2.4.4 (says `mitigation_sufficient` for rca-infeasible) and the new derivation precedence (produces `closed_rca_infeasible` for rca-infeasible). `insufficient-evidence-handling.md:103` is untouched by this PR but is now materially inconsistent with the shipped semantics — the PR description promises "make every closure_reason a reason", yet the doc that established `closed_insufficient_evidence` as a first-class reason still describes the pre-PR wiring. Fix the six lifecycle-doc lines identified above and update `insufficient-evidence-handling.md:103`, and this ships cleanly.
