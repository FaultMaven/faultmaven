"""Follow-up suggestion type-discrimination guardrail.

The rules are GENERATIVE: the model decides the type from its intent
BEFORE drafting text — every suggestion is either a GIVE (hand the user
a ready, complete next move → COOPERATIVE) or a GET (obtain input from
the user: data → EVIDENCE, words → FREE_SPEECH). Classifier-style rules
(inspect drafted text, then type it) caused repeated mistyping because
surface form misleads — observed shapes:

- "I have another question" / unfinished "How do I..." emitted clickable
  (case_a5af93054820): a GET (their question) cast as a GIVE.
- Fully-worded answerable questions demoted to non-clickable FREE_SPEECH
  (case_3e8c9eccf2c8): a GIVE mistaken for a GET.
- "Have similar test failures happened before?" emitted clickable
  (case_27e448b278ae): agent-worded, but the ANSWER lives with the user —
  a GET cast as a GIVE; the click submitted a question the agent itself
  could not answer.

These pin the GIVE/GET fork, the litmus, the tie-breaker, the BAD
GET-as-payload contrast set, and that the block reaches both the INQUIRY
and INVESTIGATING prompts.

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


def _flat(text: str) -> str:
    """Collapse whitespace so pins don't depend on line wrapping."""
    return " ".join(text.split())


@pytest.mark.unit
class TestFollowUpTypeDiscrimination:
    def test_give_get_fork_heads_the_block(self):
        """The generative fork — start from intent, GIVE vs GET — must lead
        the rules, with the three types assigned inside it."""
        flat = _flat(_FOLLOW_UP_SUGGESTIONS_BLOCK)
        assert "Start from what YOU WANT" in flat
        assert "GIVE the user a ready next move" in flat
        assert "GET input from the user" in flat
        assert "What you need picks the type" in flat

    def test_litmus_blocks_get_as_clickable(self):
        """The litmus is the single residual check: anything the user must
        add (data or words) makes the suggestion a GET — never
        COOPERATIVE."""
        flat = _flat(_FOLLOW_UP_SUGGESTIONS_BLOCK)
        assert (
            "would acting on this suggestion require the user to add ANYTHING" in flat
        )
        assert "never COOPERATIVE" in flat

    def test_get_miscast_examples_cover_observed_shapes(self):
        """The BAD contrast set pins the three observed GET-as-payload
        shapes: missing question, answer-on-user-side question, missing
        data."""
        flat = _flat(_FOLLOW_UP_SUGGESTIONS_BLOCK)
        assert 'BAD: payload "I have another question"' in flat
        assert 'BAD: payload "Has this happened before?"' in flat
        assert 'BAD: payload "I ran it — here\'s the result"' in flat

    def test_answerable_question_is_a_give(self):
        """A ready-made question the AGENT can answer is a GIVE (clickable)
        — pinned by the example and its annotation, contrasting with the
        FREE_SPEECH open invitation."""
        flat = _flat(_FOLLOW_UP_SUGGESTIONS_BLOCK)
        assert "take up a follow-up question YOU can answer" in flat
        assert '"What does exit code 137 mean?"' in flat
        assert (
            '{{"label": "Ask another question", "action_type": "FREE_SPEECH"}}' in flat
        )

    def test_uncertainty_defaults_to_free_speech(self):
        """Failure costs are asymmetric: a wrongly-clickable suggestion
        submits a broken message as the user; a wrongly-informational one
        only makes the user type. The tie-breaker must point at FREE_SPEECH,
        and the schema default must match it."""
        from faultmaven.core.investigation.schemas import SuggestedFollowUp

        assert "When unsure which type fits, use FREE_SPEECH" in _flat(
            _FOLLOW_UP_SUGGESTIONS_BLOCK
        )
        # Omitted action_type degrades safely to non-clickable: the field
        # defaults to FREE_SPEECH and any stray payload is dropped.
        s = SuggestedFollowUp(label="Do the thing", payload="something")
        assert s.action_type == "FREE_SPEECH"
        assert s.payload is None

    def test_cooperative_payload_completeness_survives_in_mechanics(self):
        """The GIVE consequence at the mechanics layer: payloads stand
        alone and must be actionable by the agent."""
        flat = _flat(_FOLLOW_UP_SUGGESTIONS_BLOCK)
        assert "nothing left for the user to add or edit" in flat
        assert "a message YOU can act on from this case or your own knowledge" in flat

    def test_solution_hold_directive_agrees_with_type_system(self):
        """The DIAGNOSIS solution-hold directive must not prescribe
        preamble COOPERATIVE payloads. It originally hardcoded
        'I have a question about the proposed fix' as query_submit
        (observed wasting a turn in case_b9f8197e1921 — the click
        submitted the preamble and the agent had to ask for the question
        anyway). Both hold moves need user-authored content, so the
        directive must prescribe EVIDENCE (share the fix outcome) +
        FREE_SPEECH (ask about the fix) instead."""
        from faultmaven.core.investigation.prompts.templates import (
            _RCA_DIAGNOSIS_BLOCK,
        )

        block = _RCA_DIAGNOSIS_BLOCK
        assert 'EVIDENCE — "Share the result of the fix"' in block
        assert 'FREE_SPEECH — "Ask about the proposed fix"' in block
        assert "Neither is COOPERATIVE" in block
        # The old prescriptions must not reappear.
        assert 'query_submit: "I have a question about the proposed fix"' not in block
        assert 'query_submit: "I ran the command' not in block

    def test_command_copy_placeholders_remain_legal(self):
        """Scope guard: completeness bites query_submit only. command_copy
        payloads legitimately carry <placeholders> the user edits
        externally (e.g. kubectl logs <pod-name>)."""
        flat = _flat(_FOLLOW_UP_SUGGESTIONS_BLOCK)
        assert "<placeholders>" in flat
        assert "the user edits them externally" in flat

    @pytest.mark.parametrize(
        "template,name",
        [
            (INQUIRY_TEMPLATE, "INQUIRY_TEMPLATE"),
            (INVESTIGATION_BASE, "INVESTIGATION_BASE"),
        ],
    )
    def test_rules_reach_suggestion_generating_prompts(self, template, name):
        """Both suggestion-generating prompts compose the shared block, so
        the GIVE/GET fork and the litmus must be present in each assembled
        template."""
        flat = _flat(template)
        assert (
            "GIVE the user a ready next move" in flat
        ), f"{name} lost the GIVE/GET generative fork."
        assert (
            "would acting on this suggestion require the user to add ANYTHING" in flat
        ), f"{name} lost the litmus."
