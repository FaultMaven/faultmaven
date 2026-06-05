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
