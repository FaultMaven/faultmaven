"""Tests for ``assess_closure_readiness`` verdict logic.

Pure-function tests of the readiness assessor. Three verdicts:

- ``SUGGEST_RESOLVE`` — case has root cause + solution; closing would
  discard resolution attribution. Symmetric to
  ``ResolutionReadiness.SUGGEST_CLOSE``.
- ``HAS_SUBSTANCE`` — case has some investigation work worth summarizing
  but is not resolution-grade (e.g., evidence + hypotheses but no
  root cause OR no solution).
- ``TRIVIAL`` — no investigation data on the case.

Engine wiring that consumes these verdicts is tested in
``test_transition_alignment.py`` (dropdown + LLM-emit pivots).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.terminal_transitions import (
    ClosureReadiness,
    assess_closure_readiness,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
)
from faultmaven.modules.case.domain.models import (
    ConfidenceLevel,
    Evidence,
    InvestigationProgress,
    RootCauseConclusion,
    Solution,
    SolutionType,
)


def _make_investigating_case() -> Case:
    """Minimal INVESTIGATING case with empty progress; tests add what they need."""
    case = Case(
        case_id="case_c0bbeef0a510",
        title="Closure readiness test",
        status=CaseStatus.INVESTIGATING,
        user_id="user_test",
        organization_id="org_test",
        description="Test description",
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Test problem",
        ),
    )
    case.progress = InvestigationProgress()
    return case


def _attach_root_cause(case: Case) -> None:
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Connection pool exhaustion under load",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.85,
        mechanism="Pool size capped at 5; traffic spike saturated it",
    )


def _attach_solution(case: Case) -> None:
    case.solutions.append(
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Increase connection pool to 100",
            longterm_fix="Update DestinationRule maxConnections to 100",
        )
    )


def _attach_evidence(case: Case) -> None:
    case.evidence.append(
        Evidence(
            summary="Latency p99 spike to 5.2s confirmed (user description)",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(UTC),
            collected_by="user_test",
            primary_purpose="Symptom verification",
            preprocessed_content="p99=5200ms",
            content_size_bytes=100,
            preprocessing_method="manual",
            collected_at_turn=1,
        )
    )


# ---------------------------------------------------------------------------
# SUGGEST_RESOLVE — case is resolution-grade (root cause + solution present)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSuggestResolveVerdict:
    """``SUGGEST_RESOLVE`` fires when the case has both root cause and
    solution on record — the minimum bar for RESOLVED. Closing would
    discard the resolution attribution; engine should pivot to propose
    RESOLVED instead.
    """

    def test_root_cause_plus_solution_returns_suggest_resolve(self):
        case = _make_investigating_case()
        _attach_root_cause(case)
        _attach_solution(case)

        readiness = assess_closure_readiness(case)

        assert readiness.verdict == ClosureReadiness.SUGGEST_RESOLVE
        # Message names the pivot rationale + presents root cause + solution
        assert "qualifies for **resolved**" in readiness.message
        assert "Connection pool exhaustion" in readiness.message
        assert "Increase connection pool to 100" in readiness.message
        assert "mark it **resolved** instead" in readiness.message

    def test_suggest_resolve_takes_priority_over_has_substance(self):
        """Even when the case has rich substance (evidence + hypotheses),
        if root_cause + solution are present, the pivot fires — the user
        should not be allowed to discard attribution by accident."""
        case = _make_investigating_case()
        _attach_evidence(case)
        _attach_root_cause(case)
        _attach_solution(case)

        readiness = assess_closure_readiness(case)

        assert readiness.verdict == ClosureReadiness.SUGGEST_RESOLVE


# ---------------------------------------------------------------------------
# HAS_SUBSTANCE — investigation produced work but not resolution-grade
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHasSubstanceVerdict:
    """``HAS_SUBSTANCE`` fires when the case has meaningful investigation
    work but is missing one of root cause / solution (so it isn't
    resolution-grade and closing is the right disposition).
    """

    def test_evidence_only_returns_has_substance(self):
        case = _make_investigating_case()
        _attach_evidence(case)

        readiness = assess_closure_readiness(case)

        assert readiness.verdict == ClosureReadiness.HAS_SUBSTANCE
        assert "Evidence collected" in readiness.message

    def test_root_cause_without_solution_returns_has_substance(self):
        """Root cause identified but no solution proposed — not
        resolution-grade. Close path applies; engine summarizes what
        was learned."""
        case = _make_investigating_case()
        _attach_root_cause(case)

        readiness = assess_closure_readiness(case)

        assert readiness.verdict == ClosureReadiness.HAS_SUBSTANCE
        assert "Root cause identified" in readiness.message

    def test_solution_without_root_cause_returns_has_substance(self):
        """Solution proposed but no root cause documented — partially
        complete but not resolution-grade. Close summarizes; resolve
        readiness would route through NEEDS_INFO."""
        case = _make_investigating_case()
        _attach_solution(case)

        readiness = assess_closure_readiness(case)

        assert readiness.verdict == ClosureReadiness.HAS_SUBSTANCE
        assert "Solutions on record" in readiness.message


# ---------------------------------------------------------------------------
# TRIVIAL — no investigation data at all
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTrivialVerdict:
    """``TRIVIAL`` fires when the case has no investigation artifacts of
    any kind — closing a case the agent never really worked on."""

    def test_empty_case_returns_trivial(self):
        case = _make_investigating_case()

        readiness = assess_closure_readiness(case)

        assert readiness.verdict == ClosureReadiness.TRIVIAL
        assert "minimal investigation data" in readiness.message


# ---------------------------------------------------------------------------
# Multi-solution display — title-fallback discrimination
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSolutionTitleFallback:
    """When a case has multiple solutions and some lack titles, the
    user-facing message must keep each entry distinguishable. Earlier
    behavior used the literal string "Untitled" for every titleless
    entry, producing confusing output like
    "Solution: My fix, Untitled, Untitled". The fallback now uses
    "Solution N" (1-indexed) so the user can still tell entries apart.

    Note: ``Solution.title`` is a required ``str`` field (min_length=1),
    so titleless solutions cannot be constructed via the normal Pydantic
    path. The fallback in ``_solution_display_titles`` only fires under
    schema bypass (``model_construct``) or future schema relaxation.
    These tests use ``model_construct`` to exercise the defensive code
    — the indexed fallback is preferable to the literal "Untitled" even
    if the scenario is rare in practice.
    """

    def test_titleless_solutions_use_indexed_fallback_not_literal_untitled(self):
        """SUGGEST_RESOLVE path: case with root cause + multiple solutions
        where some are titleless (via Pydantic bypass). The message must
        not contain the literal "Untitled"; titleless entries render as
        "Solution 2", "Solution 3"."""
        case = _make_investigating_case()
        _attach_root_cause(case)
        # First solution titled normally; next two bypass validation to
        # simulate the schema-bypass / future-relaxation scenario.
        case.solutions.append(
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Bump connection pool to 100",
                longterm_fix="Apply DestinationRule update",
            )
        )
        case.solutions.append(
            Solution.model_construct(
                solution_type=SolutionType.CONFIG_CHANGE,
                title=None,
            )
        )
        case.solutions.append(
            Solution.model_construct(
                solution_type=SolutionType.CONFIG_CHANGE,
                title=None,
            )
        )

        readiness = assess_closure_readiness(case)

        assert readiness.verdict == ClosureReadiness.SUGGEST_RESOLVE
        # Titled solution is preserved.
        assert "Bump connection pool to 100" in readiness.message
        # Titleless solutions render with indexed fallback, not "Untitled".
        assert "Untitled" not in readiness.message
        assert "Solution 2" in readiness.message
        assert "Solution 3" in readiness.message

    def test_titleless_solutions_in_has_substance_path(self):
        """Same fallback applies in the HAS_SUBSTANCE branch (case has
        solution but no root cause) — symmetry across both paths that
        consume solution titles."""
        case = _make_investigating_case()
        # Two solutions, both titleless via bypass. No root cause → HAS_SUBSTANCE.
        case.solutions.append(
            Solution.model_construct(
                solution_type=SolutionType.CONFIG_CHANGE,
                title=None,
            )
        )
        case.solutions.append(
            Solution.model_construct(
                solution_type=SolutionType.CONFIG_CHANGE,
                title=None,
            )
        )

        readiness = assess_closure_readiness(case)

        assert readiness.verdict == ClosureReadiness.HAS_SUBSTANCE
        assert "Untitled" not in readiness.message
        assert "Solution 1" in readiness.message
        assert "Solution 2" in readiness.message
