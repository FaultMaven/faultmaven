"""Tests for ``disposition_eligibility`` — the denormalized read view that
gates Resolve / Close UI affordances on actual case content.

Covers four concerns:

1. **Pure-function derivation** (``derive_disposition_eligibility``):
   maps case state → per-disposition eligibility verdict, state-aware.

2. **Repository chokepoint (P3)**: ``CaseRepository.save()`` refreshes
   the persisted column from the helper at every save — no per-mutation
   site burden.

3. **UI adapter exposure**: all three ``CaseUIResponse_*`` variants
   surface the field so the frontend can render dropdown eligibility.

4. **Lifecycle integration**: a case walking INQUIRY → INVESTIGATING →
   (add root cause + solution) carries the correct eligibility through
   each transition.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.terminal_transitions import (
    DISPOSITION_ELIGIBILITY_NEEDS_INFO,
    DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
    DISPOSITION_ELIGIBILITY_READY,
    DISPOSITION_ELIGIBILITY_SUGGESTS_ALTERNATIVE,
    derive_disposition_eligibility,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
)
from faultmaven.modules.case.domain.models import (
    ConfidenceLevel,
    Evidence,
    InvestigationProgress,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    SolutionType,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_inquiry_case() -> Case:
    return Case(
        case_id="case_de1100000001",
        title="Eligibility test",
        state=CaseState.INQUIRY,
        user_id="user_test",
        organization_id="org_test",
        description="",
        inquiry=InquiryData(),
    )


def _make_investigating_case() -> Case:
    case = Case(
        case_id="case_de1100000002",
        title="Eligibility test (investigating)",
        state=CaseState.INVESTIGATING,
        user_id="user_test",
        organization_id="org_test",
        description="Test problem",
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Test problem",
        ),
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom", severity="HIGH"
        ),
    )
    case.progress = InvestigationProgress()
    return case


def _attach_root_cause(case: Case) -> None:
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Test root cause",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.85,
        mechanism="Test mechanism",
    )


def _attach_solution(case: Case) -> None:
    case.solutions.append(
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Test solution",
            longterm_fix="Apply fix",
        )
    )


def _attach_evidence(case: Case) -> None:
    case.evidence.append(
        Evidence(
            summary="Test symptom evidence",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(UTC),
            collected_by="user_test",
            primary_purpose="Symptom verification",
            preprocessed_content="symptom observed",
            content_size_bytes=100,
            preprocessing_method="manual",
            collected_at_turn=1,
        )
    )


# ---------------------------------------------------------------------------
# 1. Pure-function derivation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeriveDispositionEligibility:
    """``derive_disposition_eligibility`` produces the right
    per-disposition verdict for each case shape. Status-aware: INQUIRY
    can only close, INVESTIGATING reads readiness verdicts, terminal
    is not_eligible for everything.
    """

    def test_inquiry_returns_close_only(self):
        """INQUIRY: only CLOSED is a valid edge. Resolved must be
        not_eligible regardless of any other state."""
        case = _make_inquiry_case()
        result = derive_disposition_eligibility(case)
        assert result == {
            "resolved": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
            "closed": DISPOSITION_ELIGIBILITY_READY,
        }

    def test_investigating_empty_case_is_close_only(self):
        """INVESTIGATING + thin case: resolution-readiness verdict is
        SUGGEST_CLOSE → resolved is not_eligible; close remains ready."""
        case = _make_investigating_case()
        result = derive_disposition_eligibility(case)
        assert result["resolved"] == DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE
        assert result["closed"] == DISPOSITION_ELIGIBILITY_READY

    def test_investigating_partial_returns_needs_info_for_resolved(self):
        """INVESTIGATING + evidence + root_cause but no solution:
        resolution-readiness = NEEDS_INFO → resolved eligibility =
        needs_info; close stays ready (HAS_SUBSTANCE branch)."""
        case = _make_investigating_case()
        _attach_evidence(case)
        _attach_root_cause(case)
        # No solution attached → NEEDS_INFO for resolved
        result = derive_disposition_eligibility(case)
        assert result["resolved"] == DISPOSITION_ELIGIBILITY_NEEDS_INFO
        assert result["closed"] == DISPOSITION_ELIGIBILITY_READY

    def test_investigating_resolution_ready_returns_ready_for_resolved(self):
        """INVESTIGATING + root cause + solution: resolution-readiness =
        READY → resolved eligibility = ready. Close eligibility flips
        to suggests_alternative (SUGGEST_RESOLVE warns that closing
        would discard attribution and recommends resolving instead —
        distinct from needs_info which asks the user to add data)."""
        case = _make_investigating_case()
        _attach_root_cause(case)
        _attach_solution(case)
        result = derive_disposition_eligibility(case)
        assert result["resolved"] == DISPOSITION_ELIGIBILITY_READY
        assert result["closed"] == DISPOSITION_ELIGIBILITY_SUGGESTS_ALTERNATIVE

    def test_terminal_resolved_is_not_eligible_for_anything(self):
        """RESOLVED case is terminal — no further dispositions."""
        case = _make_investigating_case()
        _attach_root_cause(case)
        _attach_solution(case)
        # Bypass Pydantic's state-transition validators by direct attribute write.
        # (Validators require resolved_at + closed_at + closure_reason for RESOLVED;
        # we're only testing the derive helper's state branch here.)
        object.__setattr__(case, "state", CaseState.RESOLVED)
        result = derive_disposition_eligibility(case)
        assert result == {
            "resolved": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
            "closed": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
        }

    def test_terminal_closed_is_not_eligible_for_anything(self):
        """CLOSED case is terminal — no further dispositions."""
        case = _make_inquiry_case()
        object.__setattr__(case, "state", CaseState.CLOSED)
        result = derive_disposition_eligibility(case)
        assert result == {
            "resolved": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
            "closed": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
        }


# ---------------------------------------------------------------------------
# 2. Repository chokepoint (P3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestRepositorySaveChokepoint:
    """``CaseRepository.save()`` is the single site that refreshes
    ``case.disposition_eligibility`` from the derive helper. Every save
    path triggers it; app code never authors the field directly. This is
    the P3 maintenance pattern.

    These tests use the in-memory repository — the chokepoint contract
    is the same across SQLite / PostgreSQL / InMemory by design (the
    repo abstract base + parallel implementations all call the same
    derive helper in their save methods).
    """

    async def test_save_populates_eligibility_when_none(self):
        """New case starts with disposition_eligibility=None; first
        save derives + persists the correct value."""
        repo = InMemoryCaseRepository()
        case = _make_inquiry_case()
        assert case.disposition_eligibility is None

        saved = await repo.save(case)

        assert saved.disposition_eligibility == {
            "resolved": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
            "closed": DISPOSITION_ELIGIBILITY_READY,
        }

    async def test_save_refreshes_on_subsequent_writes(self):
        """The chokepoint runs on EVERY save — including subsequent
        writes that change inputs. No per-mutation-site update burden."""
        repo = InMemoryCaseRepository()
        case = _make_investigating_case()
        await repo.save(case)
        assert (
            case.disposition_eligibility["resolved"]
            == DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE
        )

        # Now attach root cause + solution. Just calling save() again
        # is enough — the derive helper at the chokepoint picks up the
        # new content automatically.
        _attach_root_cause(case)
        _attach_solution(case)
        await repo.save(case)

        assert case.disposition_eligibility["resolved"] == DISPOSITION_ELIGIBILITY_READY
        # And close flips to suggests_alternative (SUGGEST_RESOLVE
        # warning — distinct from needs_info; the user is asked to
        # consider resolving, not to add data).
        assert (
            case.disposition_eligibility["closed"]
            == DISPOSITION_ELIGIBILITY_SUGGESTS_ALTERNATIVE
        )

    async def test_eligibility_overwritten_even_if_caller_set_a_stale_value(self):
        """Caller-authored values are NOT trusted. The chokepoint
        unconditionally derives and overwrites, so a stale value (e.g.,
        from a deserialization or a confused caller) cannot persist."""
        repo = InMemoryCaseRepository()
        case = _make_inquiry_case()
        # Caller sets a wrong value.
        case.disposition_eligibility = {
            "resolved": DISPOSITION_ELIGIBILITY_READY,  # wrong — inquiry can't resolve
            "closed": DISPOSITION_ELIGIBILITY_READY,
        }

        await repo.save(case)

        # Chokepoint overwrote with the truth.
        assert (
            case.disposition_eligibility["resolved"]
            == DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE
        )


# ---------------------------------------------------------------------------
# 3. UI adapter exposure
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUIAdapterExposesEligibility:
    """All three ``CaseUIResponse_*`` variants surface
    ``disposition_eligibility`` so the frontend can gate dropdown
    affordances per case state. The adapter passes through whatever the
    derive helper persisted to the column — no recomputation here."""

    def test_inquiry_response_includes_eligibility(self):
        from faultmaven.modules.case.domain.services.case_ui_adapter import (
            transform_case_for_ui,
        )

        case = _make_inquiry_case()
        case.disposition_eligibility = {
            "resolved": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
            "closed": DISPOSITION_ELIGIBILITY_READY,
        }

        ui = transform_case_for_ui(case)

        assert ui.disposition_eligibility == case.disposition_eligibility

    def test_investigating_response_includes_eligibility(self):
        from faultmaven.modules.case.domain.services.case_ui_adapter import (
            transform_case_for_ui,
        )

        case = _make_investigating_case()
        case.disposition_eligibility = {
            "resolved": DISPOSITION_ELIGIBILITY_READY,
            "closed": DISPOSITION_ELIGIBILITY_SUGGESTS_ALTERNATIVE,
        }

        ui = transform_case_for_ui(case)

        assert ui.disposition_eligibility == case.disposition_eligibility


# ---------------------------------------------------------------------------
# 4. Lifecycle integration — eligibility tracks through transitions
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestLifecycleIntegration:
    """End-to-end pin: a case walking INQUIRY → INVESTIGATING → (root
    cause + solution attached) carries the right eligibility through
    each save, with no per-mutation-site touch in the lifecycle code.
    """

    async def test_eligibility_walks_correctly_through_lifecycle(self):
        repo = InMemoryCaseRepository()

        # Stage 1: brand-new INQUIRY case
        case = _make_inquiry_case()
        await repo.save(case)
        assert case.disposition_eligibility == {
            "resolved": DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE,
            "closed": DISPOSITION_ELIGIBILITY_READY,
        }, "INQUIRY: close-only at the start"

        # Stage 2: transition to INVESTIGATING. Still thin — resolved
        # not_eligible, close ready.
        from faultmaven.modules.case.contracts import InvestigationProgress

        case.inquiry.problem_statement_confirmed = True
        case.inquiry.decided_to_investigate = True
        case.inquiry.proposed_problem_statement = "Problem"
        case.description = "Problem"
        case.state = CaseState.INVESTIGATING
        case.progress = InvestigationProgress()
        case.problem_verification = ProblemVerification(
            symptom_statement="Symptom", severity="HIGH"
        )
        await repo.save(case)
        assert (
            case.disposition_eligibility["resolved"]
            == DISPOSITION_ELIGIBILITY_NOT_ELIGIBLE
        )
        assert case.disposition_eligibility["closed"] == DISPOSITION_ELIGIBILITY_READY

        # Stage 3: attach root cause + solution → resolution-grade.
        _attach_root_cause(case)
        _attach_solution(case)
        await repo.save(case)
        assert case.disposition_eligibility["resolved"] == DISPOSITION_ELIGIBILITY_READY
        # Close warns now — SUGGEST_RESOLVE → suggests_alternative.
        assert (
            case.disposition_eligibility["closed"]
            == DISPOSITION_ELIGIBILITY_SUGGESTS_ALTERNATIVE
        )


# ---------------------------------------------------------------------------
# 5. needs_info vs suggests_alternative — disjoint semantics pin
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEligibilityValuesAreSemanticallyDisjoint:
    """``needs_info`` (add data) and ``suggests_alternative`` (consider
    the other action) are distinct UX patterns — frontend renders them
    differently. A previous design merged them into a single
    ``needs_info`` value for "backend compactness"; this test pins the
    split so a future refactor can't silently re-merge them and
    re-overload one label across two different UX patterns.
    """

    def test_needs_info_and_suggests_alternative_are_distinct_constants(self):
        assert (
            DISPOSITION_ELIGIBILITY_NEEDS_INFO
            != DISPOSITION_ELIGIBILITY_SUGGESTS_ALTERNATIVE
        )

    def test_partial_resolution_uses_needs_info_not_alternative(self):
        """Partial-resolution case: user must ADD data (root cause /
        solution) — that's the ``needs_info`` semantic, not
        ``suggests_alternative``."""
        case = _make_investigating_case()
        _attach_evidence(case)
        _attach_root_cause(case)
        # No solution → NEEDS_INFO from assess_resolution_readiness.

        result = derive_disposition_eligibility(case)

        assert result["resolved"] == DISPOSITION_ELIGIBILITY_NEEDS_INFO
        assert result["resolved"] != DISPOSITION_ELIGIBILITY_SUGGESTS_ALTERNATIVE

    def test_resolution_grade_close_uses_suggests_alternative_not_needs_info(self):
        """Resolution-grade case clicked-to-close: user should be told
        to CONSIDER the alternative (resolve) — that's the
        ``suggests_alternative`` semantic, not ``needs_info``. No data
        is missing; the disposition itself is what's questioned."""
        case = _make_investigating_case()
        _attach_root_cause(case)
        _attach_solution(case)

        result = derive_disposition_eligibility(case)

        assert result["closed"] == DISPOSITION_ELIGIBILITY_SUGGESTS_ALTERNATIVE
        assert result["closed"] != DISPOSITION_ELIGIBILITY_NEEDS_INFO
