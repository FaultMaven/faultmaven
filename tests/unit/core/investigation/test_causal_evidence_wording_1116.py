"""Pin the causal-evidence wording shipped for #1116.

Replaying case_bf484a484a77 turn 9 showed the model filing a ``df -h`` line
at 100% as symptom evidence only: the decision tree's step 2 named a
*change* (deploy, config, code) as the only shape of causal evidence, so a
measured state that IS the hypothesised mechanism was never linked to the
hypothesis. The rule now says a state reading is causal when it is the
condition a hypothesis names, and that a datum which both shows and explains
the problem gets both rows.

These are structural pins, like ``test_investigation_template_acknowledgment_rules``:
the rule lives inside a ~1100-line string constant and nothing else fails when
a reflow or merge drops it. The same file pins the header count the wording
change corrected and the internal-id prohibition it widened.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts.templates import (
    _EVIDENCE_GROUNDING_BLOCK,
    _RCA_DIAGNOSIS_BLOCK,
    INVESTIGATION_BASE,
    SCHEMA_INSTRUCTIONS,
)


@pytest.mark.unit
class TestMeasuredStateIsCausal:
    def test_decision_tree_step_two_admits_a_measured_state(self):
        assert (
            "Does this evidence bear on WHY the problem exists?" in INVESTIGATION_BASE
        )
        assert "OR a measured" in INVESTIGATION_BASE
        assert "STATE that is the mechanism itself" in INVESTIGATION_BASE
        # The concrete shapes the model failed on, so the rule is operational
        # rather than abstract.
        assert "a filesystem at 100%" in INVESTIGATION_BASE
        assert "an exhausted pool" in INVESTIGATION_BASE

    def test_state_reading_is_not_dismissed_as_monitoring_output(self):
        assert 'A state reading is not "just a symptom"' in INVESTIGATION_BASE

    def test_a_datum_that_shows_and_explains_gets_both_rows(self):
        assert "gets BOTH rows" in INVESTIGATION_BASE

    def test_diagnosis_stage_evidence_types_say_the_same(self):
        """The stage block's shorter list must not contradict the tree."""
        assert "a measured state that IS the mechanism" in _RCA_DIAGNOSIS_BLOCK
        assert "(deploy logs, config diffs, code changes)\n" not in _RCA_DIAGNOSIS_BLOCK


@pytest.mark.unit
class TestDecisionTreeCounts:
    """Three numbered steps, four categories. Step 1 used to say 'steps 2-4'."""

    def test_header_counts_four(self):
        assert "DECISION TREE (4 categories)" in INVESTIGATION_BASE

    def test_step_one_continues_to_the_steps_that_exist(self):
        assert "CONTINUE evaluating steps 2-3" in INVESTIGATION_BASE
        assert "steps 2-4" not in INVESTIGATION_BASE
        assert (
            "\n4. "
            not in INVESTIGATION_BASE[
                INVESTIGATION_BASE.find("DECISION TREE") : INVESTIGATION_BASE.find(
                    "CREATING EVIDENCE RECORDS"
                )
            ]
        )


@pytest.mark.unit
class TestInternalIdProhibitionCoversHypothesisIds:
    """The prompt now renders ``[hyp_...]`` ids (so the model can link to a
    standing hypothesis). The prose rule that kept ``ev_`` ids out of
    ``agent_response`` has to name the new ids too, or they are cited back
    at the user (#666 class)."""

    def test_grounding_rule_names_all_three_id_kinds(self):
        assert "NEVER cite internal IDs in agent_response" in _EVIDENCE_GROUNDING_BLOCK
        assert '"ev_a1b2c3d4e5f6"' in _EVIDENCE_GROUNDING_BLOCK
        assert '"hyp_..."' in _EVIDENCE_GROUNDING_BLOCK
        assert '"cn_..."' in _EVIDENCE_GROUNDING_BLOCK

    def test_schema_instructions_extend_the_rule(self):
        assert "hyp_/cn_ id" in SCHEMA_INSTRUCTIONS
