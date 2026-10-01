"""Schema tests for IntentType and QueryIntent.

Covers the IntentType enum membership and the per-intent field-presence
validators.

NOTE (investigation-flow redesign): the PATH_SELECTION and
POST_MITIGATION_CHOICE intents (and their carrying fields
investigation_path / continue_to_rca) were removed with the path fork.
"""

import pytest
from pydantic import ValidationError

from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.case.contracts import CaseState


class TestIntentTypeEnum:
    def test_intent_types_present(self):
        """The current intent-type set. PATH_SELECTION /
        POST_MITIGATION_CHOICE were removed with the path fork."""
        expected = {
            "conversation",
            "status_transition",
            "hypothesis_action",
            "evidence_need",
            "confirmation",
            "greeting",
            "file_reclassification",
        }
        assert {t.value for t in IntentType} == expected

    def test_path_intents_are_gone(self):
        """Regression guard: the removed path-fork intents must not return."""
        assert not hasattr(IntentType, "PATH_SELECTION")
        assert not hasattr(IntentType, "POST_MITIGATION_CHOICE")


class TestExistingIntentValidators:
    """Regression guards for the intent-type validators."""

    def test_conversation_needs_no_extra_fields(self):
        QueryIntent(type=IntentType.CONVERSATION)  # should not raise

    def test_status_transition_still_requires_to_status(self):
        with pytest.raises(ValidationError):
            QueryIntent(type=IntentType.STATUS_TRANSITION)

    def test_status_transition_constructs_with_to_status(self):
        intent = QueryIntent(
            type=IntentType.STATUS_TRANSITION,
            to_state=CaseState.RESOLVED,
        )
        assert intent.to_state == CaseState.RESOLVED

    def test_confirmation_still_requires_confirmation_value(self):
        with pytest.raises(ValidationError):
            QueryIntent(type=IntentType.CONFIRMATION)

    def test_hypothesis_action_still_requires_id_and_action(self):
        with pytest.raises(ValidationError):
            QueryIntent(type=IntentType.HYPOTHESIS_ACTION)

    def test_evidence_need_still_requires_evidence_need_id(self):
        with pytest.raises(ValidationError):
            QueryIntent(type=IntentType.EVIDENCE_NEED)


class TestConfirmationNamesItsOffer:
    """K14 (#1812): a confirmation card names the offer it presents, and a
    client forwards the card's intent verbatim, so ``QueryIntent`` must keep the
    key. A field the model does not declare is dropped silently (it has no
    ``extra`` config), which is why the key needs one."""

    def test_proposal_id_round_trips(self):
        intent = QueryIntent(
            type=IntentType.CONFIRMATION,
            confirmation_value=True,
            proposal_id="2026-09-30T11:57:20.123456+00:00",
        )
        again = QueryIntent.model_validate_json(intent.model_dump_json())
        assert again.proposal_id == "2026-09-30T11:57:20.123456+00:00"

    def test_a_card_intent_forwarded_as_slack_does_keeps_the_key(self):
        """Slack sends ``{**intent, "user_confirmed": True}``; the route pops
        ``type`` and builds the model from the rest."""
        card = {
            "type": "confirmation",
            "confirmation_value": False,
            "proposal_id": "gate1:0123456789abcdef",
            "user_confirmed": True,
        }
        data = {k: v for k, v in card.items() if k != "type"}
        intent = QueryIntent(type=IntentType.CONFIRMATION, **data)
        assert intent.proposal_id == "gate1:0123456789abcdef"
        assert intent.confirmation_value is False

    def test_the_key_is_optional(self):
        """A card rendered before the keys shipped still validates; the engine
        refuses its click as untargeted rather than the route refusing it."""
        intent = QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=True)
        assert intent.proposal_id is None
