"""Follow-up suggestion type-discrimination guardrail.

The rules are GENERATIVE: the model decides the type from its intent
BEFORE drafting text — the four wire types ARE the intents: DECIDE (user
accepts a pre-written message; clickable, click sends), RUN (user
executes a composed command; clickable, click copies), EVIDENCE (user
supplies environment data; informational), FREE_SPEECH (user supplies
their own words; informational). Classifier-style rules
(inspect drafted text, then type it) caused repeated mistyping because
surface form misleads — observed shapes:

- "I have another question" / unfinished "How do I..." emitted clickable
  (case_a5af93054820): a content-GET (their question) cast as clickable.
- Fully-worded answerable questions demoted to non-clickable FREE_SPEECH
  (case_3e8c9eccf2c8): a DECIDE move mistaken for a GET.
- "Have similar test failures happened before?" emitted clickable
  (case_27e448b278ae): agent-worded, but the ANSWER lives with the user —
  a GET cast as clickable; the click submitted a question the agent
  itself could not answer.

These pin the intent lanes, the litmus, the tie-breaker, the BAD
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
    def test_intent_lanes_head_the_block(self):
        """The generative rules — start from intent — must lead the block,
        with three lanes each binding intent to type AND encoding:
        DECIDE (clickable, sends), RUN (pasteable, copies), GET CONTENT
        (informational)."""
        flat = _flat(_FOLLOW_UP_SUGGESTIONS_BLOCK)
        assert "Start from what YOU WANT" in flat
        assert "the TYPE names your intent, and the encoding follows from it" in flat
        assert "1. DECIDE — you expect a decision or answer FROM the user" in flat
        assert "you pre-compose it for them" in flat
        assert "DECIDE (clickable — click sends)" in flat
        assert "2. RUN — you want the user to execute an exact command" in flat
        assert "RUN (pasteable — click copies)" in flat
        assert "3. GET CONTENT" in flat
        assert "what you need picks the type" in flat

    def test_run_lane_owns_command_copy_and_output_return_trip(self):
        """RUN is its own intent lane, not buried under DECIDE: command
        execution happens externally, and the output coming back is a
        separate EVIDENCE ask — never folded into the command suggestion
        (the 'I ran it — here's the result' failure shape)."""
        flat = _flat(_FOLLOW_UP_SUGGESTIONS_BLOCK)
        assert "RUN (pasteable — click copies)" in flat
        assert "That return trip is a separate EVIDENCE ask" in flat

    def test_litmus_blocks_get_as_clickable(self):
        """The litmus is the single residual check: any CONTENT the user
        must supply (data or words, beyond the click itself) makes the
        suggestion a GET — never clickable."""
        flat = _flat(_FOLLOW_UP_SUGGESTIONS_BLOCK)
        assert (
            "beyond the click (send or copy), must the user supply any CONTENT" in flat
        )
        assert "never DECIDE or RUN" in flat

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
        assert "a ready-made question YOU can answer" in flat
        assert (
            '"action_type": "DECIDE", "payload": "What does exit code 137 mean?"'
            in flat
        )
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
        preamble clickable payloads. It originally hardcoded
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
        assert "Neither is clickable (DECIDE/RUN)" in block
        # The old prescriptions must not reappear.
        assert 'query_submit: "I have a question about the proposed fix"' not in block
        assert 'query_submit: "I ran the command' not in block

    def test_command_copy_placeholders_remain_legal(self):
        """Scope guard: completeness bites query_submit only. command_copy
        payloads legitimately carry <placeholders> the user edits
        externally (e.g. kubectl logs <pod-name>)."""
        flat = _flat(_FOLLOW_UP_SUGGESTIONS_BLOCK)
        assert "<placeholders>" in flat
        assert "the user edits them in their terminal" in flat

    @pytest.mark.parametrize(
        "template,name",
        [
            (INQUIRY_TEMPLATE, "INQUIRY_TEMPLATE"),
            (INVESTIGATION_BASE, "INVESTIGATION_BASE"),
        ],
    )
    def test_rules_reach_suggestion_generating_prompts(self, template, name):
        """Both suggestion-generating prompts compose the shared block, so
        the intent lanes and the litmus must be present in each assembled
        template."""
        flat = _flat(template)
        assert (
            "the TYPE names your intent, and the encoding follows from it" in flat
        ), f"{name} lost the intent-lane generative rules."
        assert (
            "beyond the click (send or copy), must the user supply any CONTENT" in flat
        ), f"{name} lost the litmus."
