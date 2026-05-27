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
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisStatus,
    InquiryData,
    InvestigationPath,
    NeedPriority,
    NeedPurpose,
    NeedStatus,
    PathSelection,
    UploadedFile,
)

# ============================================================
# Fixtures
# ============================================================


_USE_DEFAULT_PATH = object()


def _make_case(
    *,
    status: CaseStatus = CaseStatus.INVESTIGATING,
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

    if path_selection is _USE_DEFAULT_PATH and status == CaseStatus.INVESTIGATING:
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
        status=status,
        inquiry=inquiry,
        path_selection=path_selection,
    )
    case.current_turn = 5
    if status == CaseStatus.INVESTIGATING:
        case.progress.symptom_verified = symptom_verified
    return case


def _make_hypothesis(
    case: Case,
    *,
    hyp_id: str | None = None,
    status: HypothesisStatus = HypothesisStatus.ACTIVE,
) -> Hypothesis:
    h = Hypothesis(
        hypothesis_id=hyp_id or f"hyp_{uuid4().hex[:12]}",
        statement="Test hypothesis",
        category=HypothesisCategory.DATABASE,
        status=status,
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
        status=None,
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
        assert n.status == NeedStatus.PENDING
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
            status=NeedStatus.SUPERSEDED,
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
                    status=NeedStatus.PENDING,
                )
            ],
            meta,
            case.current_turn,
        )
        # Status stays SUPERSEDED
        assert case.evidence_needs[0].status == NeedStatus.SUPERSEDED
        assert any("resurrection" in r for r in meta.get("validation_repairs", []))

    def test_dangling_hypothesis_id_dropped(self):
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
        engine = _make_engine()
        meta = _empty_metadata()
        engine._apply_evidence_need_updates(
            case,
            [_make_update(purpose=NeedPurpose.CAUSAL_VERIFICATION)],
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
        assert case.evidence_needs[0].status == NeedStatus.SUPERSEDED
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
        assert case.evidence_needs[0].status == NeedStatus.PENDING
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
        assert case.evidence_needs[0].status == NeedStatus.PENDING

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
            status=NeedStatus.FULFILLED,
            created_at_turn=case.current_turn,
        )
        case.evidence_needs.append(need)

        _supersede_needs_on_hypothesis_retirement(
            case, h.hypothesis_id, case.current_turn
        )
        # Status stays FULFILLED; motivators list cleared
        assert case.evidence_needs[0].status == NeedStatus.FULFILLED
        assert case.evidence_needs[0].motivating_hypothesis_ids == []
