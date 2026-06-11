"""Follow-up suggestion type-discrimination guardrail.

A COOPERATIVE query_submit payload is sent verbatim when clicked — the user
cannot edit it first. A payload whose real content must come from the user
("I have another question", "How do I...") therefore wastes a turn: the click
submits an information-free message and the agent has to ask for the actual
content anyway (observed in case_a5af93054820). Such invitations are
FREE_SPEECH by definition — the user speaks in their own words.

These pin the two-test COOPERATIVE gate (COMPLETE + DELIVERABLE) and the
broadened FREE_SPEECH definition in ``_FOLLOW_UP_SUGGESTIONS_BLOCK``, and that
both reach the INQUIRY and INVESTIGATING prompts.

Run:
    pytest tests/unit/core/investigation/test_follow_up_type_discrimination.py -v
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts.templates import (
    _FOLLOW_UP_SUGGESTIONS_BLOCK,
    INQUIRY_TEMPLATE,
    INVESTIGATION_BASE,
)


@pytest.mark.unit
class TestFollowUpTypeDiscrimination:
    def test_cooperative_gate_requires_complete_payload(self):
        """The COOPERATIVE gate must include the completeness test: a
        query_submit payload submits verbatim and must be the user's
        complete message."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert "COMPLETE" in block
        assert "complete message" in block
        assert "NOT COOPERATIVE" in block

    def test_placeholder_payloads_named_as_counterexamples(self):
        """The observed failure shapes are named explicitly so the model
        recognizes the class, not just the abstraction."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert '"I have another question"' in block
        assert '"How do I..."' in block

    def test_free_speech_covers_user_questions(self):
        """FREE_SPEECH must cover the user's next question — not only
        knowledge/judgment/observations — so open invitations have a home
        type and don't default into COOPERATIVE."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert "their next question" in block
        assert (
            '{{"label": "Ask another question", "action_type": "FREE_SPEECH"}}' in block
        )

    def test_command_copy_placeholders_remain_legal(self):
        """Scope guard: the completeness test bites query_submit only.
        command_copy payloads legitimately carry <placeholders> the user
        edits in their terminal (e.g. kubectl logs <pod-name>)."""
        assert "command_copy payloads MAY contain <placeholders>" in (
            _FOLLOW_UP_SUGGESTIONS_BLOCK
        )

    @pytest.mark.parametrize(
        "template,name",
        [
            (INQUIRY_TEMPLATE, "INQUIRY_TEMPLATE"),
            (INVESTIGATION_BASE, "INVESTIGATION_BASE"),
        ],
    )
    def test_gate_reaches_suggestion_generating_prompts(self, template, name):
        """Both suggestion-generating prompts compose the shared block, so
        the gate must be present in each assembled template."""
        assert (
            "Before marking a suggestion COOPERATIVE, apply BOTH tests" in template
        ), f"{name} lost the COOPERATIVE two-test gate."
        assert "their next question" in template
