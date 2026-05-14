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
    InvestigationResponse_Diagnosis,
    InvestigationResponse_General,
    InvestigationResponse_Mitigation,
    InvestigationResponse_Treatment,
)
from faultmaven.modules.case.contracts import TurnOutcome

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
