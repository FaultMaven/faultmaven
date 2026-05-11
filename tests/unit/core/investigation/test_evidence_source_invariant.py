"""Source-invariant tests for the post-010 strict evidence model.

Migration 010 enforced "every Evidence row has a known source" via:
1. ``evidence_source_invariant`` DB CHECK constraint on the evidence table
2. ``_source_requires_file_unless_user_description`` Pydantic validator on
   the ``Evidence`` domain model
3. ``_source_file_required_unless_user_description`` Pydantic validator on
   the ``EvidenceToAdd`` LLM-output schema

The invariant: ``source_file_id`` is required unless
``source_type == USER_DESCRIPTION`` (the chat-quote case where the LLM
extracted a verbatim system-output snippet from the user's short chat
message rather than a file).

These tests guard against drift by exercising every layer.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from faultmaven.core.investigation.schemas import EvidenceToAdd
from faultmaven.modules.case.domain.models import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
)


@pytest.mark.unit
class TestEvidencePydanticInvariant:
    """Pydantic ``Evidence`` model enforces the source invariant."""

    def test_file_backed_evidence_accepted(self):
        """A LOGS-source-type evidence with source_file_id constructs cleanly."""
        ev = Evidence(
            evidence_id="ev_aaaabbbbcccc",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            primary_purpose="symptom_verified",
            summary="OOM errors at 14:23",
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_aabb12345678",
            collected_by="user_owner",
            collected_at_turn=2,
        )
        assert ev.source_file_id == "file_aabb12345678"
        assert ev.source_type == EvidenceSourceType.LOGS

    def test_chat_extracted_evidence_accepted(self):
        """A USER_DESCRIPTION evidence with no source_file_id is the legal
        carve-out (verbatim system-output quote from the user's chat message).
        """
        ev = Evidence(
            evidence_id="ev_aaaabbbbcccc",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            primary_purpose="symptom_verified",
            summary="User pasted HTTP 503 error in chat",
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            source_file_id=None,
            collected_by="user_owner",
            collected_at_turn=2,
        )
        assert ev.source_file_id is None
        assert ev.source_type == EvidenceSourceType.USER_DESCRIPTION

    def test_logs_without_source_file_id_rejected(self):
        """LOGS source_type + None source_file_id violates the invariant."""
        with pytest.raises(ValidationError) as exc:
            Evidence(
                evidence_id="ev_aaaabbbbcccc",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                primary_purpose="symptom_verified",
                summary="OOM errors",
                source_type=EvidenceSourceType.LOGS,
                source_file_id=None,
                collected_by="user_owner",
                collected_at_turn=2,
            )
        assert "source_file_id is required" in str(exc.value)

    def test_metrics_without_source_file_id_rejected(self):
        """METRICS source_type also requires a source file."""
        with pytest.raises(ValidationError) as exc:
            Evidence(
                evidence_id="ev_aaaabbbbcccc",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                primary_purpose="symptom_verified",
                summary="Latency spike",
                source_type=EvidenceSourceType.METRICS,
                source_file_id=None,
                collected_by="user_owner",
                collected_at_turn=2,
            )
        assert "source_file_id is required" in str(exc.value)

    def test_configuration_without_source_file_id_rejected(self):
        """CONFIGURATION source_type also requires a source file."""
        with pytest.raises(ValidationError) as exc:
            Evidence(
                evidence_id="ev_aaaabbbbcccc",
                category=EvidenceCategory.CAUSAL_EVIDENCE,
                primary_purpose="root_cause_identified",
                summary="Misconfigured connection pool",
                source_type=EvidenceSourceType.CONFIGURATION,
                source_file_id=None,
                collected_by="user_owner",
                collected_at_turn=3,
            )
        assert "source_file_id is required" in str(exc.value)


@pytest.mark.unit
class TestEvidenceToAddInvariant:
    """The LLM-output schema enforces the same rule at the validation
    boundary so the LLM gets a clear error instead of an opaque
    IntegrityError downstream.
    """

    def test_file_backed_evidence_to_add_accepted(self):
        ev = EvidenceToAdd(
            summary="OOM errors at 14:23",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_aabb12345678",
        )
        assert ev.source_file_id == "file_aabb12345678"

    def test_chat_extracted_evidence_to_add_accepted(self):
        ev = EvidenceToAdd(
            summary="User reported HTTP 503",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
        )
        assert ev.source_file_id is None

    def test_logs_without_source_file_id_rejected(self):
        with pytest.raises(ValidationError) as exc:
            EvidenceToAdd(
                summary="OOM errors",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
            )
        msg = str(exc.value)
        assert "source_file_id is required" in msg
        # The error guidance points the LLM at the prompt-context attribute
        assert "file_id" in msg


@pytest.mark.unit
class TestPostDropCategories:
    """The dropped enum values (CONTEXTUAL_EVIDENCE, REJECTED, EvidenceForm)
    must not exist — protects against drift if someone reinstates them.
    """

    def test_evidence_category_has_only_four_values(self):
        """4 categories survive: symptom/causal/mitigation/solution."""
        values = {c.value for c in EvidenceCategory}
        assert values == {
            "symptom_evidence",
            "causal_evidence",
            "mitigation_evidence",
            "solution_evidence",
        }

    def test_no_contextual_evidence_category(self):
        assert not hasattr(EvidenceCategory, "CONTEXTUAL_EVIDENCE")
        with pytest.raises(ValueError):
            EvidenceCategory("contextual_evidence")

    def test_no_rejected_category(self):
        assert not hasattr(EvidenceCategory, "REJECTED")
        with pytest.raises(ValueError):
            EvidenceCategory("rejected")

    def test_no_evidence_form_class(self):
        """The dual-path form discriminator was dropped in migration 010."""
        from faultmaven.modules.case.domain import models

        assert not hasattr(models, "EvidenceForm")

    def test_user_description_source_type_exists(self):
        """The chat-quote source-type marker is the post-010 addition."""
        assert hasattr(EvidenceSourceType, "USER_DESCRIPTION")
        assert EvidenceSourceType.USER_DESCRIPTION.value == "user_description"


@pytest.mark.unit
class TestInquiryResponseDoesNotAcceptEvidence:
    """INQUIRY phase: no evidence creation. The schema field is absent."""

    def test_inquiry_response_has_no_evidence_to_add(self):
        from faultmaven.core.investigation.schemas import InquiryResponse

        inquiry_fields = InquiryResponse.InquiryStateUpdate.model_fields
        assert "evidence_to_add" not in inquiry_fields, (
            "InquiryResponse must not accept evidence_to_add — evidence "
            "creation is gated to INVESTIGATING under the strict model."
        )
