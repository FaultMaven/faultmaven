"""Schema tests for IntentType and QueryIntent — slice 1 of investigation gates.

Covers the two new intent types (PATH_SELECTION, POST_MITIGATION_CHOICE) and
their carrying fields (investigation_path, continue_to_rca), plus the
field-presence validator for each. Existing intent types are smoke-tested
to confirm no regression.
"""

import pytest
from pydantic import ValidationError

from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.case.contracts import CaseState, InvestigationPath


class TestIntentTypeEnum:
    def test_new_intent_types_present(self):
        assert IntentType.PATH_SELECTION.value == "path_selection"
        assert IntentType.POST_MITIGATION_CHOICE.value == "post_mitigation_choice"

    def test_existing_intent_types_unchanged(self):
        """Regression guard: the slice 1 additions don't disturb existing types."""
        expected = {
            "conversation",
            "status_transition",
            "hypothesis_action",
            "evidence_need",
            "confirmation",
            "greeting",
            "path_selection",
            "post_mitigation_choice",
        }
        assert {t.value for t in IntentType} == expected


class TestPathSelectionIntent:
    def test_constructs_with_required_field(self):
        intent = QueryIntent(
            type=IntentType.PATH_SELECTION,
            investigation_path=InvestigationPath.MITIGATION_FIRST,
        )
        assert intent.type == IntentType.PATH_SELECTION
        assert intent.investigation_path == InvestigationPath.MITIGATION_FIRST

    def test_accepts_root_cause(self):
        intent = QueryIntent(
            type=IntentType.PATH_SELECTION,
            investigation_path=InvestigationPath.ROOT_CAUSE,
        )
        assert intent.investigation_path == InvestigationPath.ROOT_CAUSE

    def test_rejects_when_investigation_path_missing(self):
        with pytest.raises(ValidationError) as exc_info:
            QueryIntent(type=IntentType.PATH_SELECTION)
        assert "investigation_path required for path_selection intent" in str(
            exc_info.value
        )


class TestPostMitigationChoiceIntent:
    def test_constructs_with_continue_to_rca_true(self):
        intent = QueryIntent(
            type=IntentType.POST_MITIGATION_CHOICE,
            continue_to_rca=True,
        )
        assert intent.type == IntentType.POST_MITIGATION_CHOICE
        assert intent.continue_to_rca is True

    def test_constructs_with_continue_to_rca_false(self):
        """Even an explicit False is valid — the close branch uses STATUS_TRANSITION,
        but a False value is still a meaningful 'asked but declined to continue' signal.
        """
        intent = QueryIntent(
            type=IntentType.POST_MITIGATION_CHOICE,
            continue_to_rca=False,
        )
        assert intent.continue_to_rca is False

    def test_rejects_when_continue_to_rca_missing(self):
        with pytest.raises(ValidationError) as exc_info:
            QueryIntent(type=IntentType.POST_MITIGATION_CHOICE)
        assert "continue_to_rca required for post_mitigation_choice intent" in str(
            exc_info.value
        )


class TestExistingIntentValidators:
    """Regression guards for the existing intent-type validators."""

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
