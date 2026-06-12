"""Unit tests for the defensive validators on ``core/investigation/schemas.py``.

These tests pin two specific defenses against LLM-output drift:

1. ``*StateUpdate.outcome`` is ``Optional[TurnOutcome]`` with a default of
   ``CONVERSATION``. The server recomputes the outcome from actual state
   changes via ``determine_turn_outcome()``, so an LLM omission must not
   raise — it should degrade to the default and let the recompute take
   over.

2. ``BaseInteractionResponse.suggested_follow_ups`` carries a
   ``mode="before"`` field validator that decodes a JSON-encoded string
   back into a list. Some providers (notably Fireworks/DeepSeek V3) return
   the field as a string rather than a parsed array; the validator
   prevents that from failing the whole turn.

Reference: ``error-handling-and-recovery.md §3.4`` (Defensive Schema
Coercion).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from faultmaven.core.investigation.schemas import (
    HypothesisEvidenceLinkToAdd,
    InvestigationResponse_Diagnosis,
    InvestigationResponse_General,
    InvestigationResponse_Mitigation,
    InvestigationResponse_Treatment,
    SuggestedFollowUp,
)
from faultmaven.modules.case.contracts import EvidenceStance, TurnOutcome

# ============================================================
# Helpers
# ============================================================


# Pairs (Response class, state_updates key) so the parametrised tests can
# iterate every schema that carries the Optional ``outcome`` field.
RESPONSE_CASES = [
    (InvestigationResponse_Diagnosis, "DiagnosisStateUpdate"),
    (InvestigationResponse_Mitigation, "MitigationStateUpdate"),
    (InvestigationResponse_Treatment, "TreatmentStateUpdate"),
    (InvestigationResponse_General, "GeneralStateUpdate"),
]


def _minimal_payload(extra_state: dict | None = None) -> dict:
    """Build the smallest LLM payload that satisfies the response schema.

    ``state_updates`` is required as a nested object on every response; the
    inner shape uses Optional defaults so an empty dict validates.
    """
    return {
        "agent_response": "ok",
        "state_updates": extra_state or {},
    }


# ============================================================
# Outcome — Optional with default=CONVERSATION
# ============================================================


@pytest.mark.unit
class TestOutcomeOptional:
    """``state_updates.outcome`` must accept absence without raising."""

    @pytest.mark.parametrize("response_cls,_state_name", RESPONSE_CASES)
    def test_outcome_omitted_defaults_to_conversation(self, response_cls, _state_name):
        """LLM omits ``outcome`` entirely → defaults to ``CONVERSATION``."""
        parsed = response_cls.model_validate(_minimal_payload())
        assert parsed.state_updates.outcome == TurnOutcome.CONVERSATION

    @pytest.mark.parametrize("response_cls,_state_name", RESPONSE_CASES)
    def test_outcome_explicit_value_preserved(self, response_cls, _state_name):
        """LLM-provided outcome value still flows through (server may override
        downstream, but the schema preserves what the LLM said)."""
        payload = _minimal_payload({"outcome": "milestone_completed"})
        parsed = response_cls.model_validate(payload)
        assert parsed.state_updates.outcome == TurnOutcome.MILESTONE_COMPLETED

    @pytest.mark.parametrize("response_cls,_state_name", RESPONSE_CASES)
    def test_outcome_invalid_enum_still_rejected(self, response_cls, _state_name):
        """Optional-with-default does NOT mean "anything goes" — an invalid
        enum value is still a validation error. Defensive coercion is for
        the omission case only."""
        payload = _minimal_payload({"outcome": "not_a_real_outcome"})
        with pytest.raises(ValidationError):
            response_cls.model_validate(payload)


# ============================================================
# suggested_follow_ups — JSON-string coercion
# ============================================================


@pytest.mark.unit
class TestSuggestedFollowUpsCoercion:
    """The ``mode="before"`` validator parses JSON strings into lists.

    Tested on ``InvestigationResponse_General`` since the validator lives
    on ``BaseInteractionResponse`` — every concrete response class
    inherits it.
    """

    def test_list_passes_through_unchanged(self):
        suggestion = {
            "label": "Anything else to share?",
            "action_type": "FREE_SPEECH",
            "payload": "What other context might be relevant?",
        }
        payload = _minimal_payload()
        payload["suggested_follow_ups"] = [suggestion]
        parsed = InvestigationResponse_General.model_validate(payload)
        assert parsed.suggested_follow_ups is not None
        assert len(parsed.suggested_follow_ups) == 1
        assert parsed.suggested_follow_ups[0].label == "Anything else to share?"
        assert parsed.suggested_follow_ups[0].action_type == "FREE_SPEECH"

    def test_json_string_is_decoded_into_list(self):
        """The whole field arrived as a JSON-encoded string — Fireworks/DeepSeek
        sometimes returns this shape when the outer JSON is mis-serialised."""
        suggestions_str = json.dumps(
            [
                {
                    "label": "What logs are available?",
                    "action_type": "FREE_SPEECH",
                    "payload": "List the log sources you can pull from.",
                }
            ]
        )
        payload = _minimal_payload()
        payload["suggested_follow_ups"] = suggestions_str
        parsed = InvestigationResponse_General.model_validate(payload)
        assert parsed.suggested_follow_ups is not None
        assert len(parsed.suggested_follow_ups) == 1
        assert parsed.suggested_follow_ups[0].label == "What logs are available?"

    def test_unparseable_string_collapses_to_none(self):
        """Malformed JSON string → field becomes None (Optional). Suggestions
        are advisory UI affordances; a bad payload must not fail the turn."""
        payload = _minimal_payload()
        payload["suggested_follow_ups"] = "this is not json {{ broken"
        parsed = InvestigationResponse_General.model_validate(payload)
        assert parsed.suggested_follow_ups is None

    def test_omission_is_none_by_default(self):
        """Baseline: field is Optional with default=None — absence is fine."""
        parsed = InvestigationResponse_General.model_validate(_minimal_payload())
        assert parsed.suggested_follow_ups is None


class TestSuggestedFollowUpPayloadScope:
    """``payload`` is meaningful only for the clickable types (DECIDE — the
    message a click sends; RUN — the command a click copies). The
    ``_enforce_payload_scope`` validator requires it there and drops it for
    EVIDENCE/FREE_SPEECH so a stray agent-voiced question/description never
    lingers on a field the user never sees.
    """

    def test_free_speech_payload_is_dropped(self):
        """Even if the LLM stuffs a question into payload (the item-1 bug),
        it is nulled — FREE_SPEECH carries everything in label + hints."""
        f = SuggestedFollowUp(
            label="Share what I'm seeing in my environment",
            action_type="FREE_SPEECH",
            payload="Is this happening in your environment?",
            hints=["symptoms", "timeline"],
        )
        assert f.payload is None
        assert f.hints == ["symptoms", "timeline"]

    def test_evidence_payload_is_dropped(self):
        f = SuggestedFollowUp(
            label="Share the auth-service error logs",
            action_type="EVIDENCE",
            payload="Application error logs from the affected service",
            body="Helps pinpoint the failing component.",
        )
        assert f.payload is None
        assert f.body == "Helps pinpoint the failing component."

    def test_run_keeps_command_payload(self):
        f = SuggestedFollowUp(
            label="Get pod logs",
            action_type="RUN",
            payload="kubectl logs <pod> --tail=100",
        )
        assert f.payload == "kubectl logs <pod> --tail=100"
        assert f.action_type == "RUN"

    def test_decide_keeps_payload(self):
        f = SuggestedFollowUp(
            label="Validate the config hypothesis",
            action_type="DECIDE",
            payload="Let's validate the config change hypothesis",
        )
        assert f.payload == "Let's validate the config change hypothesis"
        assert f.action_type == "DECIDE"

    def test_decide_with_command_payload_coerced_to_run(self):
        """Encoding safety net: a DECIDE whose payload is a shell command
        would SUBMIT the command as a chat message on click — the validator
        coerces it to RUN so the click copies instead."""
        f = SuggestedFollowUp(
            label="Get pod logs",
            action_type="DECIDE",
            payload="kubectl logs <pod> --tail=100",
        )
        assert f.action_type == "RUN"
        assert f.payload == "kubectl logs <pod> --tail=100"

    def test_clickable_without_payload_raises(self):
        """DECIDE/RUN are defined by the send/copy text — they cannot exist
        without one."""
        with pytest.raises(ValidationError):
            SuggestedFollowUp(label="Do the thing", action_type="DECIDE")
        with pytest.raises(ValidationError):
            SuggestedFollowUp(label="Run the check", action_type="RUN")


# ============================================================
# HypothesisEvidenceLinkToAdd — int-where-string coercion
# (Variant E in the shape-failures backlog; observed on Gemini 2.5 Pro
# function-calling at turn 12 of case_8a8ca15a4f03, 2026-05-24)
# ============================================================


def _link_payload(**overrides):
    """Minimum payload for a HypothesisEvidenceLinkToAdd row."""
    base = {
        "hypothesis_id_ref": "hyp_aaaaaaaaaaaa",
        "evidence_id_ref": "ev_bbbbbbbbbbbb",
        "stance": EvidenceStance.SUPPORTS,
        "reasoning": "ev shows hypothesis matches symptom",
    }
    base.update(overrides)
    return base


@pytest.mark.unit
class TestHypothesisEvidenceLinkIntCoercion:
    """Bare integers on ``hypothesis_id_ref`` / ``evidence_id_ref`` get
    coerced to ``new_index_N`` form. Production observation: Gemini 2.5
    Pro's function-calling tool spec lets ``hypothesis_id_ref: 1``
    through despite the schema declaring ``str``. Pydantic would
    otherwise raise and 500 the whole turn."""

    def test_existing_string_id_passes_through(self):
        link = HypothesisEvidenceLinkToAdd.model_validate(_link_payload())
        assert link.hypothesis_id_ref == "hyp_aaaaaaaaaaaa"
        assert link.evidence_id_ref == "ev_bbbbbbbbbbbb"

    def test_new_index_string_passes_through(self):
        link = HypothesisEvidenceLinkToAdd.model_validate(
            _link_payload(
                hypothesis_id_ref="new_index_2",
                evidence_id_ref="new_index_3",
            )
        )
        assert link.hypothesis_id_ref == "new_index_2"
        assert link.evidence_id_ref == "new_index_3"

    def test_bare_int_on_hypothesis_ref_coerced(self):
        """Production failure: LLM emitted ``1`` instead of ``new_index_1``."""
        link = HypothesisEvidenceLinkToAdd.model_validate(
            _link_payload(hypothesis_id_ref=1)
        )
        assert link.hypothesis_id_ref == "new_index_1"

    def test_bare_int_on_evidence_ref_coerced(self):
        link = HypothesisEvidenceLinkToAdd.model_validate(
            _link_payload(evidence_id_ref=2)
        )
        assert link.evidence_id_ref == "new_index_2"

    def test_both_refs_coerced_independently(self):
        link = HypothesisEvidenceLinkToAdd.model_validate(
            _link_payload(hypothesis_id_ref=1, evidence_id_ref=7)
        )
        assert link.hypothesis_id_ref == "new_index_1"
        assert link.evidence_id_ref == "new_index_7"

    def test_bool_is_not_coerced(self):
        """``isinstance(True, int)`` is True in Python — must not coerce
        booleans to ``new_index_True``. Pydantic should raise instead."""
        with pytest.raises(ValidationError):
            HypothesisEvidenceLinkToAdd.model_validate(
                _link_payload(hypothesis_id_ref=True)
            )

    def test_float_not_coerced(self):
        """Only bare ``int`` is rescued. Floats remain a type error so the
        validator doesn't accidentally swallow malformed numeric inputs."""
        with pytest.raises(ValidationError):
            HypothesisEvidenceLinkToAdd.model_validate(
                _link_payload(hypothesis_id_ref=1.5)
            )
