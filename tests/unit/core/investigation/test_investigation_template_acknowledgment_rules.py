"""Structural regression tests for INVESTIGATION_BASE acknowledgment rules.

Pin the load-bearing acknowledgment rules so a future "prompt cleanup" PR
doesn't silently remove the guardrails that defend against specific failure
modes observed in eval runs.

These tests don't validate prompt EFFECTIVENESS — that's an eval concern.
They validate that the structural pieces the design depends on are PRESENT.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts.templates import INVESTIGATION_BASE


@pytest.mark.unit
class TestAcknowledgmentRulesPresent:
    """Two paired rules govern how the agent handles user claims about
    submitted data. Both are load-bearing:

    - ACKNOWLEDGE CORRECTIONS: agent must update its working model when
      the user corrects a prior claim or notes a step was already tried.
    - VERIFY BEFORE ACKNOWLEDGING "ALREADY PROVIDED": agent must check
      <evidence_collected> before agreeing with a user's claim that data
      was already submitted, to defend against false-acknowledgment
      stranding observed at Run 29 T8 (case_c855827d82d6).
    """

    def test_acknowledge_corrections_rule_present(self):
        assert "ACKNOWLEDGE CORRECTIONS" in INVESTIGATION_BASE

    def test_verify_before_acknowledging_rule_present(self):
        """Run 29 T8: persona claimed 'I already sent you that data' after
        an earlier 500 had blocked the actual upload. Agent reflexively
        agreed ('You are correct, you have already provided...') without
        scanning <evidence_collected>, stranding the investigation for
        one turn until the persona happened to send the missing file.

        The rule must instruct the agent to verify against
        <evidence_collected> BEFORE agreeing, and to name the missing
        artifact if no match exists."""
        assert 'VERIFY BEFORE ACKNOWLEDGING "ALREADY PROVIDED"' in INVESTIGATION_BASE
        # The rule must reference the evidence inventory the LLM has access
        # to — without this anchor, "verify" has no operational meaning.
        assert "<evidence_collected>" in INVESTIGATION_BASE
        # The rule must require naming the missing artifact, not just
        # acknowledging the gap abstractly. "I see X but not Y" is the
        # operational shape that produces a useful next turn.
        assert "name what's missing" in INVESTIGATION_BASE

    def test_verify_rule_paired_with_acknowledge_corrections(self):
        """The two rules are paired and should appear together so future
        editors recognize them as a unit. Order matters: ACKNOWLEDGE
        comes first (the general rule), VERIFY follows (the exception
        that prevents the general rule from being weaponized by false
        claims)."""
        ack_idx = INVESTIGATION_BASE.find("ACKNOWLEDGE CORRECTIONS")
        verify_idx = INVESTIGATION_BASE.find(
            'VERIFY BEFORE ACKNOWLEDGING "ALREADY PROVIDED"'
        )
        assert ack_idx >= 0 and verify_idx >= 0
        assert verify_idx > ack_idx, (
            "VERIFY rule should follow ACKNOWLEDGE CORRECTIONS rule so "
            "they read as a paired unit (general + exception)."
        )
        # The two rules should be within a few hundred chars — same
        # bulleted list. If they drift apart, the pairing is lost.
        assert verify_idx - ack_idx < 800, (
            "ACKNOWLEDGE and VERIFY rules should remain adjacent bullets. "
            "If they drift apart, the pairing intent is obscured."
        )


@pytest.mark.unit
class TestSymmetricEvidenceGapRules:
    """Two symmetric rules cover the two failure modes around evidence gaps:

    - User implies NEW data but no fresh attachment → ask for the file
      (existing rule, line ~996)
    - User claims ALREADY-PROVIDED data but no match in inventory → name
      what's missing (new rule from this PR)

    Both should be present so the agent has guidance in both directions.
    """

    def test_implies_new_data_but_missing_rule_present(self):
        # The existing inverse-direction rule.
        assert 'fresh_this_turn="true"' in INVESTIGATION_BASE

    def test_claims_already_provided_but_missing_rule_present(self):
        # The new same-direction rule.
        assert 'VERIFY BEFORE ACKNOWLEDGING "ALREADY PROVIDED"' in INVESTIGATION_BASE
