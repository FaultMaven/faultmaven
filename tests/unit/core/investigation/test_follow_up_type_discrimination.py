"""Follow-up suggestion type-discrimination guardrail.

The discriminator between COOPERATIVE and FREE_SPEECH is AUTHORSHIP of the
message content, not whether the message is a question. Both failure
directions were observed in the field:

- Under-typing (case_a5af93054820): "I have another question" / "How do I..."
  emitted as clickable COOPERATIVE. The click submits the placeholder verbatim
  and the agent must ask for the actual content anyway — one wasted turn.
  Content only the user can author is FREE_SPEECH.
- Over-correction (case_3e8c9eccf2c8): fully-worded specific questions
  ("How do I use the webhook URL?") emitted as non-clickable FREE_SPEECH,
  forcing the user to retype a question the agent already composed. A message
  the agent can fully word is COOPERATIVE — even when it is a question.

These pin the two-test COOPERATIVE gate (COMPLETE + DELIVERABLE), the
authorship discriminator stated in both directions, and that the block
reaches the INQUIRY and INVESTIGATING prompts.

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
        """The observed under-typing failure shapes are named explicitly so
        the model recognizes the class, not just the abstraction."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert '"I have another question"' in block
        assert '"How do I..."' in block

    def test_free_speech_reserved_for_user_authored_content(self):
        """FREE_SPEECH is defined by authorship — content that exists only
        in the user's head — so open invitations have a home type and don't
        default into COOPERATIVE."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert "AUTHORSHIP, not question-ness" in block
        assert (
            '{{"label": "Ask another question", "action_type": "FREE_SPEECH"}}' in block
        )

    def test_composed_questions_are_cooperative(self):
        """The converse rule: a specific, fully-worded question the agent
        composes is a complete payload and must be COOPERATIVE — not demoted
        to FREE_SPEECH for being a question (the over-correction observed in
        case_3e8c9eccf2c8). Pinned via the converse-rule text and the
        question-payload COOPERATIVE example."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert "do NOT demote a suggestion to FREE_SPEECH" in block
        assert '"What does exit code 137 mean?"' in block
        assert '"Can I customize the message format?"' in block

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
        the gate and both directions of the discriminator must be present
        in each assembled template."""
        assert (
            "Before marking a suggestion COOPERATIVE, apply BOTH tests" in template
        ), f"{name} lost the COOPERATIVE two-test gate."
        assert "AUTHORSHIP, not question-ness" in template
        assert "do NOT demote a suggestion to FREE_SPEECH" in template
