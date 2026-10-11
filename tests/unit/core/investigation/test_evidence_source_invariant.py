"""Source-invariant tests: evidence is submitted data (#1930).

"Every Evidence row has a known source" is enforced by:
1. the ``evidence_source_invariant`` DB CHECK on the evidence table
   (``tests/integration/test_evidence_source_invariant_db.py``);
2. ``_source_requires_file_unless_engine_inference`` on the ``Evidence``
   domain model;
3. the model-facing ``EvidenceToAdd``: ``source_file_id`` is required, and
   ``source_type`` cannot name the engine's own type.

The invariant: ``source_file_id`` is required unless ``source_type ==
ENGINE_INFERENCE`` (a fact the engine derived; nothing was submitted). What
the user writes in a chat message is a claim and is never a source.

These tests guard against drift by exercising every layer.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from faultmaven.core.investigation.schemas import EvidenceToAdd
from faultmaven.modules.case.domain.models.evidence import (
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

    def test_engine_inference_is_the_one_fileless_row(self):
        """An ENGINE_INFERENCE row with no source_file_id is the one legal
        carve-out: the engine derived it, nothing was submitted."""
        ev = Evidence(
            evidence_id="ev_aaaabbbbcccc",
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            primary_purpose="engine inference: failed-treatment disconfirmation",
            summary="Engine inference (M6), not an observation: ...",
            source_type=EvidenceSourceType.ENGINE_INFERENCE,
            source_file_id=None,
            collected_by="engine",
            collected_at_turn=2,
        )
        assert ev.source_file_id is None
        assert ev.source_type == EvidenceSourceType.ENGINE_INFERENCE

    @pytest.mark.parametrize(
        "source_type",
        [t for t in EvidenceSourceType if t is not EvidenceSourceType.ENGINE_INFERENCE],
    )
    def test_every_other_type_without_a_file_is_rejected(self, source_type):
        """No submitted-data type may stand without its file — the chat-quote
        carve-out is gone (#1930)."""
        with pytest.raises(ValidationError) as exc:
            Evidence(
                evidence_id="ev_aaaabbbbcccc",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                primary_purpose="symptom_verified",
                summary="User pasted HTTP 503 error in chat",
                source_type=source_type,
                source_file_id=None,
                collected_by="user_owner",
                collected_at_turn=2,
            )
        assert "source_file_id is required" in str(exc.value)

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
    """The model-facing schema states the rule (the file id is required) but
    never FAILS a row for it: a row that fails validation is dropped from the
    list and shifts every later ``new_index_N`` ref. A missing or unknown file
    is refused at apply time, in place (``test_evidence_source_guard.py``).
    """

    def test_file_backed_evidence_to_add_accepted(self):
        ev = EvidenceToAdd(
            summary="OOM errors at 14:23",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_aabb12345678",
        )
        assert ev.source_file_id == "file_aabb12345678"

    def test_the_schema_requires_the_file_id(self):
        """Required in the schema the model fills, so a strict provider always
        sends it."""
        schema = EvidenceToAdd.model_json_schema()
        assert "source_file_id" in schema["required"]
        assert "anyOf" not in schema["properties"]["source_file_id"]

    def test_a_null_file_id_parses_as_empty_for_refusal_at_apply(self):
        ev = EvidenceToAdd.model_validate(
            {
                "summary": "User reported HTTP 503",
                "category": "symptom_evidence",
                "source_type": "logs",
                "source_file_id": None,
            }
        )
        assert ev.source_file_id == ""

    def test_the_model_cannot_declare_the_engines_type(self):
        """The vocabulary the model sees omits ENGINE_INFERENCE, and a row
        that names it anyway is read as text (then stands or falls on its
        file)."""
        schema = EvidenceToAdd.model_json_schema()
        ref = schema["properties"]["source_type"]["$ref"].rsplit("/", 1)[-1]
        assert "engine_inference" not in schema["$defs"][ref]["enum"]
        ev = EvidenceToAdd(
            summary="s",
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            source_type="engine_inference",
            source_file_id="file_aabb12345678",
        )
        assert ev.source_type.value == "text"

    def test_the_retired_chat_quote_type_is_refused(self):
        with pytest.raises(ValidationError):
            EvidenceToAdd(
                summary="User reported HTTP 503",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type="user_description",
                source_file_id="file_aabb12345678",
            )


@pytest.mark.unit
class TestPostDropCategories:
    """The dropped enum values (CONTEXTUAL_EVIDENCE, REJECTED, EvidenceForm)
    must not exist — protects against drift if someone reinstates them.
    """

    def test_evidence_category_values(self):
        """The full set of EvidenceCategory values.

        The category set is the presence/absence verification quartet.
        The legacy stage-named ``mitigation_evidence`` / ``solution_evidence``
        were removed in the GAP-5 legacy→absence migration (prompts emit only
        the absence categories; the verification gates are handshake-set and
        the absence rows are consumed by the readiness checks).
        """
        values = {c.value for c in EvidenceCategory}
        assert values == {
            "symptom_evidence",
            "causal_evidence",
            "symptom_absence_evidence",
            "causal_absence_evidence",
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
        from faultmaven.modules.case.domain.models import evidence

        assert not hasattr(evidence, "EvidenceForm")

    def test_the_chat_quote_source_type_is_gone(self):
        """What the user writes in a chat message is never evidence (#1930)."""
        assert not hasattr(EvidenceSourceType, "USER_DESCRIPTION")
        with pytest.raises(ValueError):
            EvidenceSourceType("user_description")
        assert EvidenceSourceType.ENGINE_INFERENCE.value == "engine_inference"


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
