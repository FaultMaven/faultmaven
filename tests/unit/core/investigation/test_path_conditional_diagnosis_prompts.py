"""Path-conditional DIAGNOSIS-prompt assembly tests.

Pins the structural fix for the Run 26 deletion-confusion loop: the
hypothesis-creation mandate must appear ONLY in ``_RCA_DIAGNOSIS_BLOCK``,
never in the pre-mitigation MITIGATION_FIRST prompt.

The block isolation is what eliminates the conflicting-signal problem.
If a future maintainer reintroduces the hypothesis mandate into a
shared sub-block or into ``_SYMPTOM_VALIDATION_BLOCK``, these tests
fail.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts.templates import (
    _GATE3_PENDING_BLOCK,
    _HYPOTHESIS_EVIDENCE_ORDERING_BLOCK,
    _POST_MITIGATION_RCA_PREFIX,
    _RCA_DIAGNOSIS_BLOCK,
    _SYMPTOM_VALIDATION_BLOCK,
    _select_diagnosis_block,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    InquiryData,
    InvestigationPath,
    PathSelection,
    ProblemVerification,
)

# ---------------------------------------------------------------------------
# Block isolation — load-bearing invariant for PR #2
# ---------------------------------------------------------------------------


class TestBlockIsolation:
    """The hypothesis-creation mandate must be physically contained in
    ``_RCA_DIAGNOSIS_BLOCK`` and absent from every other diagnosis-stage
    block. Failure here means the conflicting-signal problem the
    refactor exists to eliminate could re-emerge.
    """

    def test_hypothesis_mandate_present_in_rca_block(self):
        assert "HYPOTHESIS-EVIDENCE ORDERING" in _RCA_DIAGNOSIS_BLOCK
        assert "hypotheses_to_add" in _RCA_DIAGNOSIS_BLOCK

    def test_hypothesis_mandate_absent_from_symptom_validation_block(self):
        assert "HYPOTHESIS-EVIDENCE ORDERING" not in _SYMPTOM_VALIDATION_BLOCK
        # The symptom-validation block must additionally and explicitly
        # FORBID structured hypothesis emission — not merely fail to
        # mandate it. Normalize whitespace so the assertion survives
        # prompt re-wrapping.
        normalized = " ".join(_SYMPTOM_VALIDATION_BLOCK.split())
        assert "DO NOT emit ``hypotheses_to_add``" in normalized

    def test_hypothesis_mandate_absent_from_gate3_pending_block(self):
        assert "HYPOTHESIS-EVIDENCE ORDERING" not in _GATE3_PENDING_BLOCK
        # Gate 3 PENDING must also explicitly forbid hypothesis emission;
        # the engine rejects RCA-side milestones at this state (INV-21),
        # so the prompt must align. Normalize whitespace so the assertion
        # survives prompt re-wrapping.
        normalized = " ".join(_GATE3_PENDING_BLOCK.split())
        assert "DO NOT emit ``hypotheses_to_add``" in normalized

    def test_causal_evidence_classification_forbidden_in_symptom_validation(self):
        """Causal evidence presupposes a hypothesis (INV-17). The
        symptom-validation block bans hypothesis emission, so it must
        also explicitly ban causal_evidence classification — otherwise
        the LLM could classify causal evidence with no hypothesis to
        attach, violating INV-17.
        """
        normalized = " ".join(_SYMPTOM_VALIDATION_BLOCK.split())
        assert "causal_evidence" in normalized
        assert "DO NOT classify any evidence as ``causal_evidence``" in normalized

    def test_hypothesis_ordering_block_is_the_canonical_text(self):
        """The standalone constant must contain the canonical phrasing,
        so a single edit propagates everywhere it's composed.
        """
        assert "HYPOTHESIS-EVIDENCE ORDERING" in _HYPOTHESIS_EVIDENCE_ORDERING_BLOCK
        assert "Never skip step 1" in _HYPOTHESIS_EVIDENCE_ORDERING_BLOCK

    def test_rca_block_includes_hypothesis_ordering_via_composition(self):
        """``_RCA_DIAGNOSIS_BLOCK`` is composed from named sub-blocks
        including ``_HYPOTHESIS_EVIDENCE_ORDERING_BLOCK``. Pin that the
        composed product contains the canonical sub-block text verbatim.
        """
        assert _HYPOTHESIS_EVIDENCE_ORDERING_BLOCK.strip() in _RCA_DIAGNOSIS_BLOCK


# ---------------------------------------------------------------------------
# Shared sub-block composition — both blocks pull from the same vocabulary
# ---------------------------------------------------------------------------


class TestSharedSubBlockComposition:
    """Both top-level blocks compose from the shared sub-blocks
    (DIAGNOSIS_ZONES, EVIDENCE_REQUEST_FORMAT, URGENCY_RECOGNITION).
    Edits to a sub-block must propagate to both consumers — pinning
    co-occurrence here catches regressions.
    """

    def test_zones_preamble_in_both_blocks(self):
        assert "DIAGNOSIS ZONES" in _SYMPTOM_VALIDATION_BLOCK
        assert "DIAGNOSIS ZONES" in _RCA_DIAGNOSIS_BLOCK

    def test_evidence_request_format_in_both_blocks(self):
        assert "EVIDENCE REQUESTS" in _SYMPTOM_VALIDATION_BLOCK
        assert "EVIDENCE REQUESTS" in _RCA_DIAGNOSIS_BLOCK
        for block in (_SYMPTOM_VALIDATION_BLOCK, _RCA_DIAGNOSIS_BLOCK):
            assert "**What**" in block
            assert "**Where**" in block
            assert "**When**" in block

    def test_urgency_recognition_in_both_blocks(self):
        assert "URGENCY RECOGNITION" in _SYMPTOM_VALIDATION_BLOCK
        assert "URGENCY RECOGNITION" in _RCA_DIAGNOSIS_BLOCK


# ---------------------------------------------------------------------------
# _select_diagnosis_block dispatch — pins the routing for each case shape
# ---------------------------------------------------------------------------


def _make_case(
    *,
    path: InvestigationPath | None = InvestigationPath.ROOT_CAUSE,
    mitigation_completed_at_turn: int | None = None,
    rca_after_mitigation_confirmed: bool = False,
) -> Case:
    """Build an INVESTIGATING-stage case with controllable path_selection."""
    path_selection: PathSelection | None
    if path is None:
        path_selection = None
    else:
        alternate = (
            InvestigationPath.MITIGATION_FIRST
            if path == InvestigationPath.ROOT_CAUSE
            else InvestigationPath.ROOT_CAUSE
        )
        path_selection = PathSelection(
            path=path,
            auto_selected=True,
            rationale="test fixture",
            alternate_path=alternate,
            selected_by="test-user",
            mitigation_completed_at_turn=mitigation_completed_at_turn,
            rca_after_mitigation_confirmed=rca_after_mitigation_confirmed,
        )

    case = Case(
        case_id="case_abcdef012345",
        title="Test case",
        status=CaseStatus.INVESTIGATING,
        user_id="test-user",
        organization_id="test-org",
        description="Production API returning 503s",
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Production API returning 503s",
        ),
        problem_verification=ProblemVerification(
            symptom_statement="503s on /api/checkout",
            severity="HIGH",
        ),
    )
    # path_selection may be None (defensive branch) — assign via setattr to
    # bypass the validator that would otherwise reject an INVESTIGATING case
    # without path_selection.
    if path_selection is not None:
        case.path_selection = path_selection
    else:
        object.__setattr__(case, "path_selection", None)
    return case


class TestDispatcher:
    """``_select_diagnosis_block`` returns the right block for each case
    shape. The dispatch is the cleavage point where path-state turns
    into prompt-content selection.
    """

    def test_root_cause_returns_rca_block(self):
        case = _make_case(path=InvestigationPath.ROOT_CAUSE)
        result = _select_diagnosis_block(case)
        # Composed: focus_emphasis + _RCA_DIAGNOSIS_BLOCK
        assert _RCA_DIAGNOSIS_BLOCK in result
        # The RCA block carries the hypothesis mandate; pre-mitigation
        # text must not be in the rendered prompt.
        assert "HYPOTHESIS-EVIDENCE ORDERING" in result
        assert "PRE-MITIGATION SYMPTOM VALIDATION" not in result

    def test_mitigation_first_pre_mitigation_returns_symptom_block(self):
        case = _make_case(
            path=InvestigationPath.MITIGATION_FIRST,
            mitigation_completed_at_turn=None,
        )
        result = _select_diagnosis_block(case)
        assert result == _SYMPTOM_VALIDATION_BLOCK
        # Defensive: the hypothesis mandate must NOT be reachable in
        # the rendered prompt for this case.
        assert "HYPOTHESIS-EVIDENCE ORDERING" not in result
        assert "PRE-MITIGATION SYMPTOM VALIDATION" in result

    def test_mitigation_first_gate3_pending_returns_gate3_block(self):
        case = _make_case(
            path=InvestigationPath.MITIGATION_FIRST,
            mitigation_completed_at_turn=7,
            rca_after_mitigation_confirmed=False,
        )
        result = _select_diagnosis_block(case)
        # Gate 3 PENDING is self-contained — no RCA block underneath.
        assert "GATE 3 PENDING" in result
        assert "turn 7" in result
        assert _RCA_DIAGNOSIS_BLOCK not in result
        # And no hypothesis mandate by composition.
        assert "HYPOTHESIS-EVIDENCE ORDERING" not in result

    def test_mitigation_first_post_gate3_returns_rca_with_post_mitigation_prefix(
        self,
    ):
        case = _make_case(
            path=InvestigationPath.MITIGATION_FIRST,
            mitigation_completed_at_turn=7,
            rca_after_mitigation_confirmed=True,
        )
        result = _select_diagnosis_block(case)
        # POST-MITIGATION RCA prefix is present and parameterized
        assert "POST-MITIGATION RCA" in result
        assert "turn 7" in result
        # Full RCA block is included — hypothesis mandate now in scope
        assert _RCA_DIAGNOSIS_BLOCK in result
        assert "HYPOTHESIS-EVIDENCE ORDERING" in result

    def test_path_selection_none_falls_back_to_symptom_validation(self):
        """Post-INV-19 an INVESTIGATING case always has path_selection,
        but the dispatcher must still degrade safely if state is
        somehow broken — falling back to the strictest block
        (symptom validation, no hypothesis emission).
        """
        case = _make_case(path=None)
        result = _select_diagnosis_block(case)
        assert result == _SYMPTOM_VALIDATION_BLOCK
        assert "HYPOTHESIS-EVIDENCE ORDERING" not in result


# ---------------------------------------------------------------------------
# end-to-end via get_prompt_for_case — proves the dispatcher reaches the
# rendered prompt for both branches.
# ---------------------------------------------------------------------------


class TestGetPromptForCaseRoutesPathConditionally:
    """``get_prompt_for_case`` is the public entry point. These tests
    ensure that the path-conditional dispatch is reached at the
    surface — guarding against a future refactor that bypasses
    ``_select_diagnosis_block``.
    """

    @pytest.mark.parametrize(
        "path,mitigation_completed_at_turn,rca_after_mitigation_confirmed,expected_marker,banned_marker",
        [
            (
                InvestigationPath.ROOT_CAUSE,
                None,
                False,
                "HYPOTHESIS-EVIDENCE ORDERING",
                "PRE-MITIGATION SYMPTOM VALIDATION",
            ),
            (
                InvestigationPath.MITIGATION_FIRST,
                None,
                False,
                "PRE-MITIGATION SYMPTOM VALIDATION",
                "HYPOTHESIS-EVIDENCE ORDERING",
            ),
            (
                InvestigationPath.MITIGATION_FIRST,
                7,
                False,
                "GATE 3 PENDING",
                "HYPOTHESIS-EVIDENCE ORDERING",
            ),
            (
                InvestigationPath.MITIGATION_FIRST,
                7,
                True,
                "POST-MITIGATION RCA",
                "PRE-MITIGATION SYMPTOM VALIDATION",
            ),
        ],
    )
    def test_path_conditional_routing_reaches_rendered_prompt(
        self,
        path,
        mitigation_completed_at_turn,
        rca_after_mitigation_confirmed,
        expected_marker,
        banned_marker,
    ):
        from faultmaven.core.investigation.prompts.templates import (
            get_prompt_for_case,
        )

        case = _make_case(
            path=path,
            mitigation_completed_at_turn=mitigation_completed_at_turn,
            rca_after_mitigation_confirmed=rca_after_mitigation_confirmed,
        )
        prompt = get_prompt_for_case(case, user_message="What's happening?")
        assert expected_marker in prompt
        assert banned_marker not in prompt
