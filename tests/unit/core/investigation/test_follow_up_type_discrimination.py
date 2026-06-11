"""Follow-up suggestion type-discrimination guardrail.

The type system has one selection rule: WHO AUTHORS THE CONTENT of the
user's next move (agent → COOPERATIVE, environment → EVIDENCE, user →
FREE_SPEECH). Both failure directions were observed in the field:

- Under-typing (case_a5af93054820): placeholder payloads ("I have another
  question", an unfinished "How do I...") emitted as clickable COOPERATIVE.
  The click submits the placeholder verbatim and the agent must ask for the
  actual content anyway — one wasted turn.
- Over-correction (case_3e8c9eccf2c8): fully-worded, directly answerable
  questions ("How do I use the webhook URL?") emitted as non-clickable
  FREE_SPEECH, forcing the user to retype questions the agent had already
  composed.

These pin the authorship triage, the query_submit completeness and
deliverability rules, and the contrast example pair (a complete question is
COOPERATIVE; an open invitation is FREE_SPEECH) in
``_FOLLOW_UP_SUGGESTIONS_BLOCK``, and that the block reaches both the
INQUIRY and INVESTIGATING prompts.

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
    def test_authorship_is_the_type_selection_rule(self):
        """The single triage rule — who must author the move's content —
        must head the block, with all three arms."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert "who must AUTHOR that move's content" in block
        assert "YOU can write the user's complete message or command" in block
        assert "ENVIRONMENT must supply data" in block
        assert "only the USER can write it" in block

    def test_query_submit_payload_must_be_complete(self):
        """A query_submit payload submits verbatim, so it must stand alone.
        Placeholder/template payloads (the under-typing failure) are
        excluded and redirected to FREE_SPEECH."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert "stand alone" in block
        assert "nothing left for the user to add or edit" in block
        assert "a template or preamble that still needs the user's words" in block

    def test_composed_questions_are_cooperative(self):
        """A fully-worded question the agent composes is a complete payload
        (the over-correction failure demoted these to FREE_SPEECH). Pinned
        by the qualifier sentence and the question-payload example, which
        contrasts with the FREE_SPEECH open invitation."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert "A fully-worded question" in block
        assert '"What does exit code 137 mean?"' in block
        assert (
            '{{"label": "Ask another question", "action_type": "FREE_SPEECH"}}' in block
        )

    def test_undeliverable_payloads_route_to_evidence(self):
        """COOPERATIVE must not promise answers the agent can't deliver —
        when answering needs data the case doesn't have, the type is
        EVIDENCE."""
        assert "if answering would need data you don't have, use EVIDENCE" in (
            _FOLLOW_UP_SUGGESTIONS_BLOCK
        )

    def test_command_copy_placeholders_remain_legal(self):
        """Scope guard: completeness bites query_submit only. command_copy
        payloads legitimately carry <placeholders> the user edits
        externally (e.g. kubectl logs <pod-name>)."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert "<placeholders>" in block
        assert "the user edits them externally" in block

    @pytest.mark.parametrize(
        "template,name",
        [
            (INQUIRY_TEMPLATE, "INQUIRY_TEMPLATE"),
            (INVESTIGATION_BASE, "INVESTIGATION_BASE"),
        ],
    )
    def test_rules_reach_suggestion_generating_prompts(self, template, name):
        """Both suggestion-generating prompts compose the shared block, so
        the triage rule and the completeness rule must be present in each
        assembled template."""
        assert (
            "who must AUTHOR that move's content" in template
        ), f"{name} lost the authorship triage rule."
        assert "nothing left for the user to add or edit" in template
