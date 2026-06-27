"""Tests for the documented-fixes context block (runbook matcher 4b-3).

The matcher stashes a matched runbook's interventions on the chain ROOT node;
``_build_documented_fixes_block`` surfaces them ONLY once that cause is
established (``cause_state == IDENTIFIED``) and its root has VALIDATED. The
matcher never creates Solutions — the LLM proposes the fix and M5 gates it.
"""

from uuid import uuid4

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    _RUNBOOK_INTERVENTIONS_META_KEY,
    KB_MAX_SOLUTION_CHARS,
    _build_documented_fixes_block,
    build_investigation_context,
)
from faultmaven.core.investigation.runbook_cause_matcher import (
    RUNBOOK_INTERVENTIONS_META_KEY,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CauseState,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models import CausalNode, ValidationMethod

pytestmark = pytest.mark.unit

_IV = [{"quadrant": "remediation", "ref": "root", "text": "Fix the hook script."}]


def _case(cause_state: CauseState = CauseState.IDENTIFIED) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="Deploy fails",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Deploy fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="Deploy fails", severity=CaseSeverity.HIGH
        ),
    )
    case.progress.cause_state = cause_state
    return case


def _root(interventions, *, validated: bool = True) -> CausalNode:
    return CausalNode(
        node_id=f"cn_{uuid4().hex[:12]}",
        statement="the root cause",
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED if validated else NodeState.CANDIDATE,
        validation_method=(
            ValidationMethod.EMPIRICAL if validated else ValidationMethod.NONE
        ),
        belief=0.9,
        actionable=True,
        evidence_links=[],
        generated_at_turn=1,
        metadata=(
            {RUNBOOK_INTERVENTIONS_META_KEY: interventions}
            if interventions is not None
            else {}
        ),
    )


def test_meta_key_constant_is_pinned():
    # The prompt module duplicates the literal to avoid importing the matcher;
    # this pins them so they can't drift.
    assert _RUNBOOK_INTERVENTIONS_META_KEY == RUNBOOK_INTERVENTIONS_META_KEY


class TestDocumentedFixesBlock:
    def test_renders_when_identified_and_validated_root_has_fixes(self):
        case = _case(CauseState.IDENTIFIED)
        node = _root(_IV, validated=True)
        case.causal_nodes[node.node_id] = node
        block = _build_documented_fixes_block(case)
        assert "<documented_fixes>" in block
        assert "Fix the hook script." in block
        assert "[remediation]" in block

    def test_empty_when_cause_not_identified(self):
        case = _case(CauseState.CANDIDATES)
        node = _root(_IV, validated=True)
        case.causal_nodes[node.node_id] = node
        assert _build_documented_fixes_block(case) == ""

    def test_empty_when_root_not_validated(self):
        # cause_state IDENTIFIED, but THIS root is a CANDIDATE → its fixes are
        # not surfaced (only a validated root's fixes show).
        case = _case(CauseState.IDENTIFIED)
        node = _root(_IV, validated=False)
        case.causal_nodes[node.node_id] = node
        assert _build_documented_fixes_block(case) == ""

    def test_empty_when_no_interventions(self):
        case = _case(CauseState.IDENTIFIED)
        node = _root(None, validated=True)
        case.causal_nodes[node.node_id] = node
        assert _build_documented_fixes_block(case) == ""

    def test_malformed_interventions_are_skipped(self):
        case = _case(CauseState.IDENTIFIED)
        node = _root(
            ["not-a-dict", {"quadrant": "mitigation", "text": "   "}, _IV[0]],
            validated=True,
        )
        case.causal_nodes[node.node_id] = node
        block = _build_documented_fixes_block(case)
        # Only the one well-formed, non-empty intervention renders.
        assert block.count("  - ") == 1
        assert "Fix the hook script." in block

    def test_long_intervention_text_is_truncated(self):
        case = _case(CauseState.IDENTIFIED)
        node = _root(
            [{"quadrant": "remediation", "ref": "root", "text": "x" * 5000}],
            validated=True,
        )
        case.causal_nodes[node.node_id] = node
        block = _build_documented_fixes_block(case)
        assert "[truncated]" in block
        assert len(block) < 5000  # the 5000-char text was capped

    def test_caps_number_of_interventions(self):
        case = _case(CauseState.IDENTIFIED)
        many = [
            {"quadrant": "mitigation", "ref": "root", "text": f"fix {i}"}
            for i in range(20)
        ]
        node = _root(many, validated=True)
        case.causal_nodes[node.node_id] = node
        block = _build_documented_fixes_block(case)
        assert block.count("  - ") == 8  # _MAX_FIXES_RENDERED


class TestEndToEnd:
    """The block must actually reach the assembled prompt (the kb_results slot),
    not just the private helper."""

    def test_block_appears_in_assembled_prompt_when_identified(self):
        case = _case(CauseState.IDENTIFIED)
        node = _root(_IV, validated=True)
        case.causal_nodes[node.node_id] = node
        ctx = build_investigation_context(case, user_message="what next?")
        assert "<documented_fixes>" in ctx["kb_results"]
        assert "Fix the hook script." in ctx["kb_results"]

    def test_block_absent_from_assembled_prompt_when_not_identified(self):
        case = _case(CauseState.CANDIDATES)
        node = _root(_IV, validated=True)
        case.causal_nodes[node.node_id] = node
        ctx = build_investigation_context(case, user_message="what next?")
        assert "<documented_fixes>" not in ctx["kb_results"]
