"""Phase 3 tests: engine apply-layer for ``evidence_need_updates``.

Mirrors the pattern in
``tests/unit/core/investigation/test_path_conditional_emission_backstop.py``:

- ``TestEvidenceNeedUpdateApplyLayer`` — create/update mechanics,
  same-turn ``new_index_N`` resolution, FK validation, immutable-purpose
  rule, SUPERSEDED-is-terminal.
- ``TestCausalNeedRejectionInRestrictedStates`` — path-conditional
  rejection in all three restricted states, symptom-purpose exempt.
- ``TestNeedSupersessionOnHypothesisRetirement`` — deterministic engine
  rule across the four retirement sites (post-hoc snapshot-diff in
  ``_process_turn_impl``).
- ``TestNeedFulfillmentJunctionApply`` — fulfilling_evidence_ids
  resolution + dangling-reference handling.

Run:
    pytest tests/unit/core/investigation/test_evidence_need_apply_layer.py -v
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _supersede_needs_on_hypothesis_retirement,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    InvestigationPath,
    NeedPriority,
    NeedPurpose,
    NeedState,
    PathSelection,
    UploadedFile,
)

# ============================================================
# Fixtures
# ============================================================


_USE_DEFAULT_PATH = object()


def _make_case(
    *,
    state: CaseState = CaseState.INVESTIGATING,
    path_selection=_USE_DEFAULT_PATH,
    symptom_verified: bool = True,
) -> Case:
    """Build a minimal Case in INVESTIGATING with optional path commit.

    Default (no ``path_selection`` kwarg) is post-Gate-2 ROOT_CAUSE
    path — the unrestricted state. Pass ``path_selection=None``
    explicitly for pre_path_investigating. Pass an actual
    ``PathSelection`` to test other restricted states.
    """
    inquiry = InquiryData()
    inquiry.proposed_problem_statement = "Test problem"
    inquiry.problem_statement_confirmed = True
    inquiry.decided_to_investigate = True

    if path_selection is _USE_DEFAULT_PATH and state == CaseState.INVESTIGATING:
        path_selection = PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=False,
            rationale="test fixture: unrestricted state",
            selected_by="user_test",
        )
    elif path_selection is _USE_DEFAULT_PATH:
        # Non-INVESTIGATING case: leave path_selection=None
        path_selection = None

    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_test",
        organization_id="org_test",
        title="Test case",
        description="Test problem",
        state=state,
        inquiry=inquiry,
        path_selection=path_selection,
    )
    case.current_turn = 5
    if state == CaseState.INVESTIGATING:
        case.progress.symptom_verified = symptom_verified
    return case


def _make_hypothesis(
    case: Case,
    *,
    hyp_id: str | None = None,
    state: HypothesisState = HypothesisState.ACTIVE,
) -> Hypothesis:
    h = Hypothesis(
        hypothesis_id=hyp_id or f"hyp_{uuid4().hex[:12]}",
        statement="Test hypothesis",
        category=HypothesisCategory.DATABASE,
        state=state,
        likelihood=0.6,
        initial_likelihood=0.6,
        generated_at_turn=case.current_turn,
        last_updated_turn=case.current_turn,
        last_progress_at_turn=case.current_turn,
        iterations_without_progress=0,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        rationale="test",
    )
    case.hypotheses[h.hypothesis_id] = h
    return h


def _make_evidence(case: Case, *, ev_id: str | None = None) -> Evidence:
    ev = Evidence(
        evidence_id=ev_id or f"ev_{uuid4().hex[:12]}",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="symptom_verified",
        summary="test symptom",
        extract="error log line",
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_aabb12345678",
        collected_by="user_test",
        collected_at_turn=case.current_turn,
    )
    case.evidence.append(ev)
    return ev


def _make_update(**overrides):
    """Build a SimpleNamespace mimicking EvidenceNeedUpdate for the
    apply-layer (the layer only reads attributes, no instance check)."""
    defaults = dict(
        need_id=None,
        purpose=NeedPurpose.SYMPTOM_VERIFICATION,
        request_text="kubectl get pods",
        rationale="confirm symptom",
        priority=NeedPriority.MEDIUM,
        state=None,
        motivating_hypothesis_ids=[],
        fulfilling_evidence_ids=[],
        superseded_reason=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_engine() -> MilestoneEngine:
    """Bare engine instance — only the apply method is exercised, so
    most dependencies don't need wiring."""
    # MilestoneEngine.__init__ takes many args; use object.__new__ to
    # bypass for unit tests of pure-Python methods.
    eng = MilestoneEngine.__new__(MilestoneEngine)
    return eng


def _empty_metadata() -> dict:
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "evidence_needs_updated": [],
        "progress_made": False,
        "status_transitioned": False,
    }


# ============================================================
# Apply-layer mechanics
# ============================================================


@pytest.mark.unit
class TestEvidenceNeedUpdateApplyLayer:
    """Create/update mechanics, FK validation, immutable-purpose rule,
    SUPERSEDED-is-terminal."""

    def test_create_new_need_with_minimal_fields(self):
        case = _make_case()
        engine = _make_engine()
        meta = _empty_metadata()

        engine._apply_evidence_need_updates(
            case=case,
            updates_list=[_make_update()],
            metadata=meta,
            current_turn=case.current_turn,
        )

        assert len(case.evidence_needs) == 1
        n = case.evidence_needs[0]
        assert n.purpose == NeedPurpose.SYMPTOM_VERIFICATION
        assert n.state == NeedState.PENDING
        assert n.motivating_hypothesis_ids == []
        assert n.fulfilling_evidence_ids == []
        # Metadata tracking — for Phase 6 _resolve_id_ref on suggestions
        assert n.need_id in meta["evidence_needs_updated"]

    def test_create_new_need_propagates_created_at_turn(self):
        case = _make_case()
        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case, [_make_update()], meta, case.current_turn
        )
        assert case.evidence_needs[0].created_at_turn == case.current_turn

    def test_update_existing_need_merges_motivators(self):
        case = _make_case()
        h1 = _make_hypothesis(case)
        h2 = _make_hypothesis(case)
        existing = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="initial",
            rationale="initial",
            motivating_hypothesis_ids=[h1.hypothesis_id],
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(existing)

        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    need_id=existing.need_id,
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    request_text="updated",
                    rationale="updated rationale",
                    motivating_hypothesis_ids=[h2.hypothesis_id],
                )
            ],
            meta,
            case.current_turn,
        )

        assert sorted(case.evidence_needs[0].motivating_hypothesis_ids) == sorted(
            [h1.hypothesis_id, h2.hypothesis_id]
        )
        assert case.evidence_needs[0].request_text == "updated"

    def test_update_immutable_purpose_ignored(self):
        case = _make_case()
        existing = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="x",
            rationale="y",
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(existing)

        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    need_id=existing.need_id,
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    request_text="x",
                    rationale="y",
                )
            ],
            meta,
            case.current_turn,
        )
        # Purpose unchanged
        assert case.evidence_needs[0].purpose == NeedPurpose.SYMPTOM_VERIFICATION
        assert any("purpose-change" in r for r in meta.get("validation_repairs", []))

    def test_superseded_is_terminal(self):
        case = _make_case()
        h1 = _make_hypothesis(case)
        existing = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="x",
            rationale="y",
            motivating_hypothesis_ids=[h1.hypothesis_id],
            state=NeedState.SUPERSEDED,
            superseded_reason="prior reason",
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(existing)

        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    need_id=existing.need_id,
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    request_text="x",
                    rationale="y",
                    state=NeedState.PENDING,
                )
            ],
            meta,
            case.current_turn,
        )
        # Status stays SUPERSEDED
        assert case.evidence_needs[0].state == NeedState.SUPERSEDED
        assert any("resurrection" in r for r in meta.get("validation_repairs", []))

    def test_dangling_hypothesis_id_dropped_on_symptom_need(self):
        """Dangling hypothesis IDs are dropped with a validation_repair
        note. Tested on a SYMPTOM need (where empty motivators are
        the normal shape) — causal needs with all-dangling motivators
        are handled by the orphan-reject path below."""
        case = _make_case()
        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                    motivating_hypothesis_ids=["hyp_doesnotexist"],
                )
            ],
            meta,
            case.current_turn,
        )
        # Need created, but with empty motivators (dangling dropped)
        assert len(case.evidence_needs) == 1
        assert case.evidence_needs[0].motivating_hypothesis_ids == []
        assert any(
            "dangling hypothesis ID" in r for r in meta.get("validation_repairs", [])
        )

    def test_dangling_evidence_id_dropped(self):
        case = _make_case()
        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    fulfilling_evidence_ids=["ev_doesnotexist"],
                )
            ],
            meta,
            case.current_turn,
        )
        # Need created without dangling ev_id; cannot mark FULFILLED
        assert case.evidence_needs[0].fulfilling_evidence_ids == []
        assert any(
            "dangling evidence ID" in r for r in meta.get("validation_repairs", [])
        )


# ============================================================
# new_index_N resolution
# ============================================================


@pytest.mark.unit
class TestNewIndexResolution:
    """Same-turn ID resolution via metadata-stored lists +
    in-loop need-to-need refs."""

    def test_motivating_hypothesis_ids_resolved_against_metadata(self):
        case = _make_case()
        h_new = _make_hypothesis(case, hyp_id="hyp_aaaa11112222")
        engine = _make_engine()
        meta = _empty_metadata()
        meta["hypotheses_generated"] = [h_new.hypothesis_id]

        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    motivating_hypothesis_ids=["new_index_0"],
                )
            ],
            meta,
            case.current_turn,
        )
        assert case.evidence_needs[0].motivating_hypothesis_ids == [h_new.hypothesis_id]

    def test_fulfilling_evidence_ids_resolved_against_metadata(self):
        case = _make_case()
        ev_new = _make_evidence(case, ev_id="ev_bbbb22223333")
        engine = _make_engine()
        meta = _empty_metadata()
        meta["evidence_added"] = [ev_new.evidence_id]

        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                    fulfilling_evidence_ids=["new_index_0"],
                )
            ],
            meta,
            case.current_turn,
        )
        assert case.evidence_needs[0].fulfilling_evidence_ids == [ev_new.evidence_id]

    def test_same_turn_need_id_reference(self):
        """Second update can reference the first via ``new_index_0``."""
        case = _make_case()
        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [
                # Create
                _make_update(
                    purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                    request_text="first need",
                    rationale="r1",
                ),
                # Update the just-created one (references via new_index_0)
                _make_update(
                    need_id="new_index_0",
                    purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                    request_text="updated text",
                    rationale="r2",
                ),
            ],
            meta,
            case.current_turn,
        )
        assert len(case.evidence_needs) == 1
        assert case.evidence_needs[0].request_text == "updated text"
        assert case.evidence_needs[0].rationale == "r2"


# ============================================================
# Path-conditional rejection
# ============================================================


@pytest.mark.unit
class TestCausalNeedRejectionInRestrictedStates:
    """Mirror ``TestCausalEvidenceRejection`` — causal-purpose need
    updates are rejected in the three restricted states; symptom-purpose
    is always allowed."""

    def test_reject_in_pre_path_investigating(self):
        """Pre-path: ``path_selection is None`` and symptom_verified True."""
        # Build a case in pre_path_investigating state (path=None,
        # symptom_verified=True)
        case = _make_case(symptom_verified=True, path_selection=None)
        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [_make_update(purpose=NeedPurpose.CAUSAL_VERIFICATION)],
            meta,
            case.current_turn,
        )
        assert case.evidence_needs == []
        assert any(
            "Rejected causal-purpose" in r for r in meta.get("validation_repairs", [])
        )
        assert "PATH-CONDITIONAL EMISSION ERROR" in (meta.get("system_feedback") or "")

    def test_reject_in_pre_mitigation_mitigation_first(self):
        ps = PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=False,
            rationale="test fixture",
            selected_by="user_test",
            # mitigation_completed_at_turn=None means pre-mitigation state
        )
        case = _make_case(path_selection=ps)
        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [_make_update(purpose=NeedPurpose.CAUSAL_VERIFICATION)],
            meta,
            case.current_turn,
        )
        assert case.evidence_needs == []
        assert any(
            "Rejected causal-purpose" in r for r in meta.get("validation_repairs", [])
        )

    def test_symptom_purpose_allowed_in_pre_path(self):
        """Symptom-validation work is the EXPECTED activity in
        pre_path_investigating — symptom needs flow normally."""
        case = _make_case(symptom_verified=True, path_selection=None)
        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [_make_update(purpose=NeedPurpose.SYMPTOM_VERIFICATION)],
            meta,
            case.current_turn,
        )
        assert len(case.evidence_needs) == 1
        assert case.evidence_needs[0].purpose == NeedPurpose.SYMPTOM_VERIFICATION

    def test_causal_allowed_in_root_cause_path(self):
        """Default fixture is ROOT_CAUSE path — unrestricted."""
        case = _make_case()  # default = ROOT_CAUSE path
        h = _make_hypothesis(case)
        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    motivating_hypothesis_ids=[h.hypothesis_id],
                )
            ],
            meta,
            case.current_turn,
        )
        assert len(case.evidence_needs) == 1
        assert case.evidence_needs[0].purpose == NeedPurpose.CAUSAL_VERIFICATION


# ============================================================
# Hypothesis-retirement supersession
# ============================================================


@pytest.mark.unit
class TestNeedSupersessionOnHypothesisRetirement:
    """Deterministic engine rule (design §7.4)."""

    def test_supersedes_causal_need_when_sole_motivator_retires(self):
        case = _make_case()
        h = _make_hypothesis(case)
        need = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="x",
            rationale="y",
            motivating_hypothesis_ids=[h.hypothesis_id],
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(need)

        count = _supersede_needs_on_hypothesis_retirement(
            case, h.hypothesis_id, case.current_turn
        )
        assert count == 1
        assert case.evidence_needs[0].state == NeedState.SUPERSEDED
        assert (
            case.evidence_needs[0].superseded_reason
            == "all motivating hypotheses retired"
        )
        assert case.evidence_needs[0].motivating_hypothesis_ids == []

    def test_survives_when_multiple_motivators_partial_retirement(self):
        case = _make_case()
        h1 = _make_hypothesis(case)
        h2 = _make_hypothesis(case)
        need = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="x",
            rationale="y",
            motivating_hypothesis_ids=[h1.hypothesis_id, h2.hypothesis_id],
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(need)

        _supersede_needs_on_hypothesis_retirement(
            case, h1.hypothesis_id, case.current_turn
        )
        # Status unchanged (h2 still motivates)
        assert case.evidence_needs[0].state == NeedState.PENDING
        assert case.evidence_needs[0].motivating_hypothesis_ids == [h2.hypothesis_id]

    def test_symptom_needs_exempt_from_supersession(self):
        """Symptom needs have empty motivating lists by design (motivated
        by problem statement) — never get auto-superseded by hypothesis
        retirement."""
        case = _make_case()
        h = _make_hypothesis(case)
        need = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="x",
            rationale="y",
            motivating_hypothesis_ids=[],
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(need)

        _supersede_needs_on_hypothesis_retirement(
            case, h.hypothesis_id, case.current_turn
        )
        assert case.evidence_needs[0].state == NeedState.PENDING

    def test_fulfilled_need_not_superseded_even_with_empty_motivators(self):
        """FULFILLED needs stay FULFILLED as audit trail of what was
        collected, even if their motivating hypothesis retires."""
        case = _make_case()
        ev = _make_evidence(case)
        h = _make_hypothesis(case)
        need = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="x",
            rationale="y",
            motivating_hypothesis_ids=[h.hypothesis_id],
            fulfilling_evidence_ids=[ev.evidence_id],
            state=NeedState.FULFILLED,
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(need)

        _supersede_needs_on_hypothesis_retirement(
            case, h.hypothesis_id, case.current_turn
        )
        # Status stays FULFILLED; motivators list cleared
        assert case.evidence_needs[0].state == NeedState.FULFILLED
        assert case.evidence_needs[0].motivating_hypothesis_ids == []


# ============================================================
# FULFILLED-with-all-dangling-evidence demotion (post-review fix #1)
# ============================================================


@pytest.mark.unit
class TestFulfilledDemotionOnEmptyFulfillments:
    """When all referenced ``fulfilling_evidence_ids`` are dropped as
    dangling, FULFILLED state is demoted to PARTIALLY_MET to keep the
    persisted ``EvidenceNeed`` consistent with its model invariant
    (FULFILLED requires non-empty fulfillment list)."""

    def test_create_with_fulfilled_and_all_dangling_evidence_demotes(self):
        case = _make_case()
        engine = _make_engine()
        meta = _empty_metadata()

        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    state=NeedState.FULFILLED,
                    fulfilling_evidence_ids=["ev_doesnotexist"],
                )
            ],
            meta,
            case.current_turn,
        )

        assert len(case.evidence_needs) == 1
        assert case.evidence_needs[0].state == NeedState.PARTIALLY_MET
        assert case.evidence_needs[0].fulfilling_evidence_ids == []
        assert any(
            "Demoted FULFILLED→PARTIALLY_MET" in r
            for r in meta.get("validation_repairs", [])
        )

    def test_update_to_fulfilled_with_all_dangling_evidence_demotes(self):
        case = _make_case()
        existing = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="x",
            rationale="y",
            state=NeedState.PENDING,
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(existing)
        engine = _make_engine()
        meta = _empty_metadata()

        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    need_id=existing.need_id,
                    state=NeedState.FULFILLED,
                    fulfilling_evidence_ids=["ev_doesnotexist"],
                )
            ],
            meta,
            case.current_turn,
        )

        assert case.evidence_needs[0].state == NeedState.PARTIALLY_MET
        assert case.evidence_needs[0].fulfilling_evidence_ids == []
        assert any(
            "Demoted FULFILLED→PARTIALLY_MET" in r
            for r in meta.get("validation_repairs", [])
        )

    def test_update_to_fulfilled_with_prior_fulfillment_succeeds(self):
        """Demotion should *not* fire when the post-merge fulfillment
        list is non-empty (a prior fulfillment carries the need)."""
        case = _make_case()
        ev_prior = _make_evidence(case)
        existing = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="x",
            rationale="y",
            state=NeedState.PARTIALLY_MET,
            fulfilling_evidence_ids=[ev_prior.evidence_id],
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(existing)
        engine = _make_engine()
        meta = _empty_metadata()

        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    need_id=existing.need_id,
                    state=NeedState.FULFILLED,
                    fulfilling_evidence_ids=["ev_doesnotexist"],
                )
            ],
            meta,
            case.current_turn,
        )

        # Prior fulfillment survives; new dangling ID dropped; no demotion.
        assert case.evidence_needs[0].state == NeedState.FULFILLED
        assert case.evidence_needs[0].fulfilling_evidence_ids == [ev_prior.evidence_id]


# ============================================================
# Prior-turn retired motivator filtering (post-review fix #2)
# ============================================================


@pytest.mark.unit
class TestPriorTurnRetiredMotivatorFiltered:
    """A causal need motivated by an already-RETIRED hypothesis would
    survive the end-of-turn snapshot-diff (which only fires for
    this-turn retirements). The apply-layer drops RETIRED IDs at
    create/update time to keep the pool clean."""

    def test_retired_motivator_dropped_on_create(self):
        case = _make_case()
        h_retired = _make_hypothesis(case, state=HypothesisState.RETIRED)
        h_active = _make_hypothesis(case, state=HypothesisState.ACTIVE)
        engine = _make_engine()
        meta = _empty_metadata()

        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    motivating_hypothesis_ids=[
                        h_retired.hypothesis_id,
                        h_active.hypothesis_id,
                    ],
                )
            ],
            meta,
            case.current_turn,
        )

        assert case.evidence_needs[0].motivating_hypothesis_ids == [
            h_active.hypothesis_id
        ]
        assert any(
            "retired hypothesis ID" in r for r in meta.get("validation_repairs", [])
        )

    def test_all_retired_motivators_rejects_create(self):
        """When every motivator filters away (all-retired, or all-
        dangling, or originally empty), a causal-purpose create is
        rejected outright. Persisting an orphan causal need would
        bypass §7.4 supersession entirely — the snapshot-diff only
        cleans up needs whose motivator retired *this turn*, so a
        need born empty would live forever."""
        case = _make_case()
        h_retired = _make_hypothesis(case, state=HypothesisState.RETIRED)
        engine = _make_engine()
        meta = _empty_metadata()

        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    motivating_hypothesis_ids=[h_retired.hypothesis_id],
                )
            ],
            meta,
            case.current_turn,
        )

        assert case.evidence_needs == []
        assert any(
            "Rejected causal-purpose evidence_need create" in r
            for r in meta.get("validation_repairs", [])
        )

    def test_all_dangling_motivators_rejects_causal_create(self):
        """All-dangling motivator references hit the same orphan-reject
        path as all-retired (same downstream symptom: causal need with
        no valid motivating hypothesis)."""
        case = _make_case()
        engine = _make_engine()
        meta = _empty_metadata()

        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    motivating_hypothesis_ids=["hyp_doesnotexist"],
                )
            ],
            meta,
            case.current_turn,
        )

        assert case.evidence_needs == []
        assert any(
            "Rejected causal-purpose evidence_need create" in r
            for r in meta.get("validation_repairs", [])
        )

    def test_causal_create_without_motivators_rejected(self):
        """Same orphan-reject path covers the case where the LLM
        omits motivators entirely on a causal need. Per design §5.2
        causal needs are *motivated by hypotheses*; absent motivators
        is malformed regardless of whether the field was empty or
        filtered to empty."""
        case = _make_case()
        engine = _make_engine()
        meta = _empty_metadata()

        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    motivating_hypothesis_ids=[],
                )
            ],
            meta,
            case.current_turn,
        )

        assert case.evidence_needs == []

    def test_retired_motivator_dropped_on_update(self):
        case = _make_case()
        h_retired = _make_hypothesis(case, state=HypothesisState.RETIRED)
        existing = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="x",
            rationale="y",
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(existing)
        engine = _make_engine()
        meta = _empty_metadata()

        engine._apply_evidence_need_updates(
            case,
            [
                _make_update(
                    need_id=existing.need_id,
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    motivating_hypothesis_ids=[h_retired.hypothesis_id],
                )
            ],
            meta,
            case.current_turn,
        )

        assert case.evidence_needs[0].motivating_hypothesis_ids == []


# ============================================================
# Snapshot-diff bookend integration (post-review fix #6.3 lite)
# ============================================================


@pytest.mark.unit
class TestSnapshotDiffBookendIntegration:
    """Direct exercise of the snapshot/diff logic used in
    ``_process_turn_impl``. Pins the integration contract without
    requiring an LLM stub or the full turn pipeline: if the pre-turn
    snapshot or post-turn diff is refactored away, this fails.

    The integration shape under test:
        pre  = {h_id : h.state == RETIRED} captured before update apply
        post = same set computed after update apply
        newly_retired = post - pre
        for each in newly_retired: _supersede_needs_on_hypothesis_retirement(...)
    """

    def test_diff_identifies_newly_retired_and_supersedes(self):
        case = _make_case()
        # A causal need motivated by h_will_retire; the snapshot taken
        # before retirement does NOT include h_will_retire in the
        # retired set, so the post-update diff will flag it.
        h_will_retire = _make_hypothesis(case, state=HypothesisState.ACTIVE)
        need = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="x",
            rationale="y",
            motivating_hypothesis_ids=[h_will_retire.hypothesis_id],
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(need)

        # Pre-turn snapshot — empty (h_will_retire is ACTIVE).
        pre_retired = {
            h_id
            for h_id, h in case.hypotheses.items()
            if h.state == HypothesisState.RETIRED
        }

        # Simulate the engine flipping the hypothesis during this turn.
        case.hypotheses[h_will_retire.hypothesis_id].state = HypothesisState.RETIRED

        # Post-turn diff — the integration logic exactly as wired in
        # _process_turn_impl.
        newly_retired = {
            h_id
            for h_id, h in case.hypotheses.items()
            if h.state == HypothesisState.RETIRED
        } - pre_retired

        assert newly_retired == {h_will_retire.hypothesis_id}

        for retired_id in newly_retired:
            _supersede_needs_on_hypothesis_retirement(
                case, retired_id, case.current_turn
            )

        # Need was superseded because its sole motivator retired this turn.
        assert case.evidence_needs[0].state == NeedState.SUPERSEDED

    def test_diff_skips_prior_turn_retirements(self):
        """A hypothesis that was already RETIRED in the pre-turn
        snapshot does not appear in newly_retired and is not handed to
        the supersession helper a second time."""
        case = _make_case()
        h_already_retired = _make_hypothesis(case, state=HypothesisState.RETIRED)

        pre_retired = {
            h_id
            for h_id, h in case.hypotheses.items()
            if h.state == HypothesisState.RETIRED
        }
        # No mutation this turn.
        post_retired = {
            h_id
            for h_id, h in case.hypotheses.items()
            if h.state == HypothesisState.RETIRED
        }
        newly_retired = post_retired - pre_retired

        assert h_already_retired.hypothesis_id in pre_retired
        assert newly_retired == set()


def _process_response_metadata() -> dict:
    """Metadata dict matching the one ``_process_response_structured``
    actually builds and threads into ``_apply_investigation_updates``.

    Crucially this does NOT contain ``evidence_needs_updated`` — the
    production dict omits it (only the parallel ``_process_turn_impl``
    dict seeds it). The apply-layer must seed the key itself; relying on
    the caller raised ``KeyError`` and 500'd the turn (eval Run 33,
    case_815d9ac01ec1).
    """
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "progress_made": False,
        "status_transitioned": False,
    }


@pytest.mark.unit
class TestApplyLayerSeedsMetadataKey:
    """Regression: the apply-layer must not assume the caller seeded
    ``metadata["evidence_needs_updated"]``.

    ``_process_response_structured`` builds its own metadata dict without
    that key, so a bare ``metadata["evidence_needs_updated"].append(...)``
    raised ``KeyError`` on the FIRST need created or updated in a turn —
    500'ing the entire turn. This is the demand-side create path the eval
    expected to be "inert"; it was actually crashing.
    """

    def test_create_does_not_keyerror_without_seeded_key(self):
        case = _make_case()
        engine = _make_engine()
        meta = _process_response_metadata()
        assert "evidence_needs_updated" not in meta  # production shape

        # Must not raise KeyError.
        engine._apply_evidence_need_updates(
            case=case,
            updates_list=[_make_update()],
            metadata=meta,
            current_turn=case.current_turn,
        )

        assert len(case.evidence_needs) == 1
        assert meta["evidence_needs_updated"] == [case.evidence_needs[0].need_id]

    def test_update_existing_need_does_not_keyerror(self):
        case = _make_case()
        engine = _make_engine()
        # Seed an existing need to exercise the UPDATE append site (6621).
        existing = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="existing need",
            rationale="seeded",
            priority=NeedPriority.MEDIUM,
            state=NeedState.PENDING,
            created_at_turn=1,
        )
        case.evidence_needs.append(existing)

        meta = _process_response_metadata()
        engine._apply_evidence_need_updates(
            case=case,
            updates_list=[
                _make_update(
                    need_id=existing.need_id,
                    state=NeedState.PARTIALLY_MET,
                )
            ],
            metadata=meta,
            current_turn=case.current_turn,
        )

        assert existing.state == NeedState.PARTIALLY_MET
        assert existing.need_id in meta["evidence_needs_updated"]


# ============================================================
# Regression: bare fulfill/state update (create-only fields omitted)
# ============================================================


@pytest.mark.unit
class TestFulfillUpdateOmittedCreateOnlyFields:
    """Regression for the fulfill-path crash (fix/evidence-need-fulfill-path).

    A validated ``EvidenceNeedUpdate`` on a fulfill/state update carries
    ``None`` for the create-only fields (``purpose`` / ``request_text`` /
    ``rationale`` / ``priority``) — the LLM omits them because they are
    immutable on the update path. Before the fix the apply-layer:
      - crashed with ``None.value`` at the immutable-purpose guard, and
      - clobbered ``request_text`` / ``rationale`` with None (silent
        corruption) and downgraded ``priority`` to the field default.

    These tests drive the bare fulfill THROUGH ``_apply_evidence_need_updates``
    (not just schema validation) and assert the existing need's create-only
    fields are preserved and state flips to FULFILLED.
    """

    def _pending_causal_need(self, case) -> EvidenceNeed:
        need = EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="EXPLAIN ANALYZE output for the audit_events query",
            rationale="confirm the unindexed Seq Scan is holding pool connections",
            priority=NeedPriority.HIGH,
            state=NeedState.PENDING,
            motivating_hypothesis_ids=[],
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(need)
        return need

    def test_bare_fulfill_preserves_create_only_fields_and_fulfills(self):
        case = _make_case()
        ev = _make_evidence(case)
        need = self._pending_causal_need(case)

        engine = _make_engine()
        meta = _empty_metadata()
        # Exactly what a validated EvidenceNeedUpdate carries on a fulfill:
        # need_id + state + fulfilling_evidence_ids; everything else None.
        engine._apply_evidence_need_updates(
            case=case,
            updates_list=[
                _make_update(
                    need_id=need.need_id,
                    purpose=None,
                    request_text=None,
                    rationale=None,
                    priority=None,
                    state=NeedState.FULFILLED,
                    fulfilling_evidence_ids=[ev.evidence_id],
                )
            ],
            metadata=meta,
            current_turn=case.current_turn,
        )

        updated = case.evidence_needs[0]
        # state flipped, evidence linked
        assert updated.state == NeedState.FULFILLED
        assert ev.evidence_id in updated.fulfilling_evidence_ids
        # create-only fields PRESERVED (not nulled / not downgraded)
        assert (
            updated.request_text == "EXPLAIN ANALYZE output for the audit_events query"
        )
        assert (
            updated.rationale
            == "confirm the unindexed Seq Scan is holding pool connections"
        )
        assert updated.priority == NeedPriority.HIGH
        assert updated.purpose == NeedPurpose.CAUSAL_VERIFICATION
        # No spurious "purpose-change" repair: purpose was omitted, not flipped
        assert not any(
            "purpose-change" in r for r in meta.get("validation_repairs", [])
        )

    def test_bare_partial_met_update_preserves_fields(self):
        case = _make_case()
        ev = _make_evidence(case)
        need = self._pending_causal_need(case)

        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case=case,
            updates_list=[
                _make_update(
                    need_id=need.need_id,
                    purpose=None,
                    request_text=None,
                    rationale=None,
                    priority=None,
                    state=NeedState.PARTIALLY_MET,
                    fulfilling_evidence_ids=[ev.evidence_id],
                )
            ],
            metadata=meta,
            current_turn=case.current_turn,
        )

        updated = case.evidence_needs[0]
        assert updated.state == NeedState.PARTIALLY_MET
        assert updated.priority == NeedPriority.HIGH
        assert updated.request_text.startswith("EXPLAIN ANALYZE")

    def test_create_with_none_priority_defaults_to_medium(self):
        """priority is Optional[None] on the schema; the apply-layer create
        path applies the MEDIUM default (the default moved off the field so
        an omitted priority on update wouldn't clobber the stored value)."""
        case = _make_case()
        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case=case,
            updates_list=[_make_update(priority=None)],
            metadata=meta,
            current_turn=case.current_turn,
        )
        assert case.evidence_needs[0].priority == NeedPriority.MEDIUM

    def test_update_can_still_revise_fields_when_provided(self):
        """The None-guard is 'omit means leave unchanged' — a value
        explicitly supplied on update still revises (design §7.2)."""
        case = _make_case()
        need = self._pending_causal_need(case)

        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case=case,
            updates_list=[
                _make_update(
                    need_id=need.need_id,
                    purpose=None,
                    request_text="revised: also capture pg_stat_statements",
                    rationale=None,
                    priority=NeedPriority.MEDIUM,
                    state=None,
                )
            ],
            metadata=meta,
            current_turn=case.current_turn,
        )

        updated = case.evidence_needs[0]
        assert updated.request_text == "revised: also capture pg_stat_statements"
        # rationale omitted -> preserved; priority supplied -> revised
        assert (
            updated.rationale
            == "confirm the unindexed Seq Scan is holding pool connections"
        )
        assert updated.priority == NeedPriority.MEDIUM

    def test_empty_string_revision_is_ignored(self):
        """An explicit ``request_text=""`` / ``rationale=""`` on update is
        treated as 'leave unchanged' (truthiness guard), not a blank-out.
        These fields are min_length=1 on the domain model, so a "" would
        otherwise corrupt the need and crash on the next repo round-trip."""
        case = _make_case()
        need = self._pending_causal_need(case)

        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case=case,
            updates_list=[
                _make_update(
                    need_id=need.need_id,
                    purpose=None,
                    request_text="",
                    rationale="",
                    priority=None,
                    state=None,
                )
            ],
            metadata=meta,
            current_turn=case.current_turn,
        )

        updated = case.evidence_needs[0]
        assert (
            updated.request_text == "EXPLAIN ANALYZE output for the audit_events query"
        )
        assert (
            updated.rationale
            == "confirm the unindexed Seq Scan is holding pool connections"
        )
