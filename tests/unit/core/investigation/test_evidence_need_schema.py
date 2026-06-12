"""Phase 2 schema tests for EvidenceNeedUpdate and SuggestedFollowUp.evidence_need_id.

Pins:

- ``EvidenceNeedUpdate`` validators (create-vs-update semantics, state
  invariants, coercion of ``new_index_N`` from bare integers).
- ``SuggestedFollowUp.evidence_need_id`` field + action-type guard +
  coercion.
- ``evidence_need_updates`` field is present on each non-INQUIRY stage
  state-update class (Diagnosis / Mitigation / Treatment / General) and
  absent from ``InquiryStateUpdate`` (per INV-07).

Run:
    pytest tests/unit/core/investigation/test_evidence_need_schema.py -v
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from faultmaven.core.investigation.schemas import (
    EvidenceNeedUpdate,
    InvestigationResponse_Diagnosis,
    InvestigationResponse_General,
    InvestigationResponse_Mitigation,
    InvestigationResponse_Treatment,
    SuggestedFollowUp,
)
from faultmaven.modules.case.contracts import (
    NeedPriority,
    NeedPurpose,
    NeedState,
)

# ============================================================
# EvidenceNeedUpdate validators
# ============================================================


@pytest.mark.unit
class TestEvidenceNeedUpdateValidators:
    """Pydantic validators on the LLM-emitted schema."""

    def test_minimal_create(self):
        """Create with the minimum required fields."""
        u = EvidenceNeedUpdate(
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="kubectl get pods",
            rationale="confirm symptom is still firing",
        )
        assert u.need_id is None
        assert u.purpose == NeedPurpose.SYMPTOM_VERIFICATION
        # priority is Optional[None] on the schema so an omitted priority on
        # the UPDATE path is distinguishable from an explicit MEDIUM (it would
        # otherwise clobber the stored priority). The MEDIUM default for the
        # CREATE path is applied in the apply-layer (see
        # test_evidence_need_apply_layer.py).
        assert u.priority is None
        assert u.state is None
        assert u.motivating_hypothesis_ids == []
        assert u.fulfilling_evidence_ids == []
        assert u.superseded_reason is None

    def test_bare_fulfill_update_validates(self):
        """A fulfill/state update omits the create-only fields
        (purpose/request_text/rationale/priority). This MUST validate —
        before the fix it raised 'Field required' on those three and the
        turn 500'd. Regression for fix/evidence-need-fulfill-path."""
        u = EvidenceNeedUpdate(
            need_id="eneed_d437986395a0",
            state="fulfilled",
            fulfilling_evidence_ids=["new_index_0"],
        )
        assert u.need_id == "eneed_d437986395a0"
        assert u.purpose is None
        assert u.request_text is None
        assert u.rationale is None
        assert u.priority is None

    def test_create_missing_core_fields_rejected(self):
        """Create path (need_id=None) still requires purpose/request_text/
        rationale — the Optional-ness is for the update path only."""
        with pytest.raises(ValidationError, match="create path"):
            EvidenceNeedUpdate(need_id=None, state="pending")

    def test_create_with_non_pending_status_rejected(self):
        """Create path requires state=None or PENDING."""
        with pytest.raises(ValidationError, match="Cannot create"):
            EvidenceNeedUpdate(
                purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                request_text="x",
                rationale="y",
                state=NeedState.FULFILLED,
                fulfilling_evidence_ids=["ev_abc123def456"],
            )

    def test_create_with_pending_status_accepted(self):
        """state=PENDING is explicit-but-redundant; allowed on create."""
        u = EvidenceNeedUpdate(
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="x",
            rationale="y",
            state=NeedState.PENDING,
        )
        assert u.state == NeedState.PENDING

    def test_superseded_requires_reason(self):
        """SUPERSEDED without superseded_reason is rejected."""
        with pytest.raises(ValidationError, match="superseded_reason"):
            EvidenceNeedUpdate(
                need_id="eneed_abc123def456",
                purpose=NeedPurpose.CAUSAL_VERIFICATION,
                request_text="x",
                rationale="y",
                state=NeedState.SUPERSEDED,
            )

    def test_non_superseded_forbids_reason(self):
        """superseded_reason on non-SUPERSEDED state is rejected."""
        with pytest.raises(ValidationError, match="must be None"):
            EvidenceNeedUpdate(
                need_id="eneed_abc123def456",
                purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                request_text="x",
                rationale="y",
                superseded_reason="no",
            )

    def test_fulfilled_requires_fulfilling_evidence(self):
        """FULFILLED without fulfilling_evidence_ids is rejected."""
        with pytest.raises(ValidationError, match="fulfilling_evidence_id"):
            EvidenceNeedUpdate(
                need_id="eneed_abc123def456",
                purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                request_text="x",
                rationale="y",
                state=NeedState.FULFILLED,
            )

    def test_fulfilled_with_evidence_accepted(self):
        u = EvidenceNeedUpdate(
            need_id="eneed_abc123def456",
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="x",
            rationale="y",
            state=NeedState.FULFILLED,
            fulfilling_evidence_ids=["ev_aaa111bbb222"],
        )
        assert u.state == NeedState.FULFILLED
        assert u.fulfilling_evidence_ids == ["ev_aaa111bbb222"]


# ============================================================
# new_index_N coercion
# ============================================================


@pytest.mark.unit
class TestNewIndexCoercion:
    """Mirrors HypothesisEvidenceLinkToAdd._coerce_bare_int_to_new_index
    (PR #354): bare integers (Gemini function-calling slip-through)
    become ``new_index_N`` strings at schema-validation time."""

    def test_bare_int_in_motivating_list_coerced(self):
        u = EvidenceNeedUpdate(
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="x",
            rationale="y",
            motivating_hypothesis_ids=[0, "hyp_real123abc12", 1],
        )
        assert u.motivating_hypothesis_ids == [
            "new_index_0",
            "hyp_real123abc12",
            "new_index_1",
        ]

    def test_bare_int_in_fulfilling_list_coerced(self):
        u = EvidenceNeedUpdate(
            need_id="eneed_abc123def456",
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="x",
            rationale="y",
            state=NeedState.FULFILLED,
            fulfilling_evidence_ids=[2],
        )
        assert u.fulfilling_evidence_ids == ["new_index_2"]

    def test_bare_int_in_need_id_coerced(self):
        u = EvidenceNeedUpdate(
            need_id=3,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="x",
            rationale="y",
        )
        assert u.need_id == "new_index_3"

    def test_real_string_id_passes_through(self):
        u = EvidenceNeedUpdate(
            need_id="eneed_abc123def456",
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="x",
            rationale="y",
        )
        assert u.need_id == "eneed_abc123def456"

    def test_new_index_string_passes_through(self):
        """Already-formed new_index_N is left alone."""
        u = EvidenceNeedUpdate(
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="x",
            rationale="y",
            motivating_hypothesis_ids=["new_index_4"],
        )
        assert u.motivating_hypothesis_ids == ["new_index_4"]


# ============================================================
# Stage-class hook checks
# ============================================================


@pytest.mark.unit
class TestEvidenceNeedUpdateStageHooks:
    """``evidence_need_updates`` is hooked into each non-INQUIRY stage
    schema (per design §8.6). ``InquiryStateUpdate`` deliberately does
    not carry it (INV-07)."""

    def test_diagnosis_stage_has_field(self):
        fields = InvestigationResponse_Diagnosis.DiagnosisStateUpdate.model_fields
        assert "evidence_need_updates" in fields

    def test_mitigation_stage_has_field(self):
        fields = InvestigationResponse_Mitigation.MitigationStateUpdate.model_fields
        assert "evidence_need_updates" in fields

    def test_treatment_stage_has_field(self):
        fields = InvestigationResponse_Treatment.TreatmentStateUpdate.model_fields
        assert "evidence_need_updates" in fields

    def test_general_stage_has_field(self):
        fields = InvestigationResponse_General.GeneralStateUpdate.model_fields
        assert "evidence_need_updates" in fields

    def test_inquiry_stage_does_not_have_field(self):
        """INV-07: no evidence-side state during INQUIRY."""
        from faultmaven.core.investigation.schemas import InquiryResponse

        fields = InquiryResponse.InquiryStateUpdate.model_fields
        assert "evidence_need_updates" not in fields

    def test_field_default_is_empty_list_not_none(self):
        """Field uses ``Optional[List[T]] = Field(default_factory=list)``
        so LLM-emitted null becomes [], not None."""
        sup = InvestigationResponse_Diagnosis.DiagnosisStateUpdate(milestones=None)
        # default_factory=list means default value is [], not None
        assert sup.evidence_need_updates == []


# ============================================================
# SuggestedFollowUp.evidence_need_id
# ============================================================


@pytest.mark.unit
class TestSuggestedFollowUpEvidenceNeedId:
    """Phase 6 linkage field. Resolution to a real ``eneed_*`` ID
    happens at engine apply-time; the schema accepts the placeholder
    forms and coerces bare ints, mirroring EvidenceNeedUpdate."""

    def test_evidence_action_with_need_id(self):
        s = SuggestedFollowUp(
            label="Share DB metrics",
            action_type="EVIDENCE",
            payload="Please run kubectl top pods",
            evidence_need_id="eneed_abc123def456",
        )
        assert s.evidence_need_id == "eneed_abc123def456"

    def test_bare_int_coerced(self):
        s = SuggestedFollowUp(
            label="Share logs",
            action_type="EVIDENCE",
            payload="Share app.log",
            evidence_need_id=0,
        )
        assert s.evidence_need_id == "new_index_0"

    def test_new_index_passes_through(self):
        s = SuggestedFollowUp(
            label="Share logs",
            action_type="EVIDENCE",
            payload="Share app.log",
            evidence_need_id="new_index_1",
        )
        assert s.evidence_need_id == "new_index_1"

    def test_decide_with_need_id_rejected(self):
        """``evidence_need_id`` on non-EVIDENCE action types is rejected."""
        with pytest.raises(
            ValidationError, match="only permitted with action_type=EVIDENCE"
        ):
            SuggestedFollowUp(
                label="Yes resolve",
                action_type="DECIDE",
                payload="Yes",
                evidence_need_id="eneed_abc123def456",
            )

    def test_free_speech_with_need_id_rejected(self):
        with pytest.raises(
            ValidationError, match="only permitted with action_type=EVIDENCE"
        ):
            SuggestedFollowUp(
                label="What else?",
                action_type="FREE_SPEECH",
                payload="What other data would help?",
                evidence_need_id="eneed_abc123def456",
            )

    def test_evidence_action_without_need_id_ok(self):
        """Backwards-compatible: EVIDENCE suggestions don't have to
        carry a need_id (Phase 5 prompt cutover may have non-linked
        EVIDENCE suggestions in transit)."""
        s = SuggestedFollowUp(
            label="Share any logs",
            action_type="EVIDENCE",
            payload="Share whatever logs are relevant",
        )
        assert s.evidence_need_id is None
