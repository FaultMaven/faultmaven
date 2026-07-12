"""Confirm-time resolve-preservation for the CLOSED gate (INV-37, #656).

The resolve gate is protective because RESOLVED asserts a positive
conclusion ("it's fixed"); the close gate is intentionally thin because
CLOSED asserts nothing. The one place the two must stay coupled is the
SUGGEST_RESOLVE pivot: a case that *can* be resolved must never be
terminally recorded as closed-unresolved.

That pivot already fires at proposal time (``assess_closure_readiness``
→ SUGGEST_RESOLVE, wired in the LLM-emit and dropdown paths). This suite
pins the confirm-time guard in ``confirm_pending_transition``: a
qualifying ``causal_absence`` can land AFTER a close was proposed, so the
guard re-checks at the single execution chokepoint immediately before the
close commits. On a hit it replaces the pending close with a RESOLVED
proposal and reports that nothing terminal executed (returns False) — the
caller re-presents the resolve confirmation.

"resolve = close WITH resolution; close = close WITHOUT resolution": it is
always safe to resolve a resolvable case whichever terminal the user asked
for. There is no symmetric counterpart (every case is closable; closing is
always a valid disposition), so the asymmetry stays principled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from faultmaven.core.investigation import terminal_transitions
from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _resolution_confirmation_suggestions,
)
from faultmaven.core.investigation.terminal_transitions import (
    ClosureReadiness,
    assess_closure_readiness,
    confirm_pending_transition,
    propose_transition,
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
    RootCauseConclusion,
    Solution,
    SolutionType,
)

pytestmark = pytest.mark.unit


def _make_investigating_case() -> Case:
    case = Case(
        case_id="case_c0bbeef0a510",
        title="Closed-gate symmetry test",
        state=CaseState.INVESTIGATING,
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
    case.progress.symptom_verified = True
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


def _attach_causal_absence(case: Case) -> None:
    """The RESOLVED proof: the root cause is confirmed eliminated after the
    fix, recorded as a ``causal_absence_evidence`` row."""
    case.evidence.append(
        Evidence(
            summary="Post-fix logs confirm pool exhaustion no longer occurs",
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            collected_at=datetime.now(UTC),
            collected_by="user_test",
            primary_purpose="Confirm root cause eliminated",
            preprocessed_content="no pool exhaustion after fix",
            content_size_bytes=80,
            preprocessing_method="manual",
            source_file_id="file_postfix000001",
            collected_at_turn=3,
        )
    )


def _resolvable_case_with_pending_close() -> Case:
    """A resolution-grade case (root cause + solution + causal_absence) that
    somehow carries a pending CLOSE — the shape the confirm-time guard exists
    to catch (the qualifying causal_absence landed after the close was
    proposed)."""
    case = _make_investigating_case()
    _attach_root_cause(case)
    _attach_solution(case)
    _attach_causal_absence(case)
    # Precondition sanity: this case IS resolvable.
    assert assess_closure_readiness(case).verdict == ClosureReadiness.SUGGEST_RESOLVE
    propose_transition(case, to_state="closed", summary="Closing as unresolved.")
    assert case.pending_transition["to_state"] == "closed"
    return case


# ---------------------------------------------------------------------------
# INV-37 core: a resolvable case never terminally closes.
# ---------------------------------------------------------------------------


def test_confirm_pending_close_pivots_to_resolved_when_resolvable():
    case = _resolvable_case_with_pending_close()

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total") as ctr:
        executed = confirm_pending_transition(case, "user_test")
        ctr.inc.assert_called_once()

    # Nothing terminal committed.
    assert executed is False
    assert case.state == CaseState.INVESTIGATING
    # The pending transition was replaced with a RESOLVED proposal so the
    # caller re-presents the resolve confirmation.
    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "resolved"
    # The pivot stores the canonical SUGGEST_RESOLVE message as the pending's
    # summary — the ONE source of truth the callers re-present (matching the
    # proposal-time pivot), not a re-derived string.
    assert case.pending_transition["summary"] == assess_closure_readiness(case).message


def test_pivot_message_handles_out_of_band_fix_without_record():
    """An out-of-band fix yields a causal_absence with NO root_cause_conclusion
    and NO Solution record — still resolvable. The pivot message must be the
    canonical SUGGEST_RESOLVE prose (which reads gracefully), never a
    self-contradictory 'Root cause: Not yet identified' rendering."""
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    _attach_causal_absence(case)  # no root cause, no solution on record
    assert assess_closure_readiness(case).verdict == ClosureReadiness.SUGGEST_RESOLVE
    propose_transition(case, to_state="closed", summary="Closing as unresolved.")

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total"):
        executed = confirm_pending_transition(case, "user_test")

    assert executed is False
    assert case.pending_transition["to_state"] == "resolved"
    msg = case.pending_transition["summary"]
    assert "qualifies for **resolved**" in msg
    assert "Not yet identified" not in msg


def test_confirm_pending_close_executes_when_not_resolvable():
    """Regression guard: a genuine close (no causal_absence) still closes —
    the pivot only fires for resolvable cases."""
    case = _make_investigating_case()
    _attach_root_cause(case)
    _attach_solution(case)
    # No causal_absence → not resolvable → HAS_SUBSTANCE, not SUGGEST_RESOLVE.
    assert assess_closure_readiness(case).verdict != ClosureReadiness.SUGGEST_RESOLVE
    propose_transition(case, to_state="closed", summary="Closing as stabilized.")

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total") as ctr:
        executed = confirm_pending_transition(case, "user_test")
        ctr.inc.assert_not_called()

    assert executed is True
    assert case.state == CaseState.CLOSED
    assert case.pending_transition is None


def test_confirm_pending_resolve_is_unaffected_by_the_guard():
    """A pending RESOLVED confirm executes normally — the close-scoped guard
    never runs on it."""
    case = _make_investigating_case()
    _attach_root_cause(case)
    _attach_solution(case)
    _attach_causal_absence(case)
    propose_transition(case, to_state="resolved", summary="Resolving.")

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total") as ctr:
        executed = confirm_pending_transition(case, "user_test")
        ctr.inc.assert_not_called()

    assert executed is True
    assert case.state == CaseState.RESOLVED
    assert case.pending_transition is None


async def test_check_automatic_transitions_surfaces_resolve_confirmation_on_pivot():
    """Engine wiring: a bare-'yes' confirm of a pending CLOSE on a resolvable
    case pivots to RESOLVED — the fallback confirm branch must NOT report a
    terminal transition, and must surface the resolve DECIDE pair for the
    re-presented confirmation."""
    case = _resolvable_case_with_pending_close()
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.checkpoint_service = None
    metadata: dict = {}

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total"):
        updated = await eng._check_automatic_transitions(case, metadata, "yes")

    # No terminal transition; the case stays INVESTIGATING with a RESOLVED
    # proposal pending.
    assert updated.state == CaseState.INVESTIGATING
    assert updated.pending_transition["to_state"] == "resolved"
    assert metadata.get("status_transitioned") is not True
    assert metadata.get("close_pivoted_to_resolve") is True
    assert metadata["override_suggestions"] == _resolution_confirmation_suggestions()
    assert metadata["closure_readiness_verdict"] == ClosureReadiness.SUGGEST_RESOLVE


def test_pivot_scoped_to_investigating_never_proposes_invalid_inquiry_edge():
    """The guard is scoped to INVESTIGATING: RESOLVED is not a valid edge from
    INQUIRY, so an INQUIRY→CLOSED confirm must close normally even if a
    (contrived) causal_absence row is present — never pivot to an invalid
    INQUIRY→RESOLVED proposal."""
    case = _make_investigating_case()
    case.atomic_update(state=CaseState.INQUIRY)
    _attach_root_cause(case)
    _attach_solution(case)
    _attach_causal_absence(case)
    propose_transition(case, to_state="closed", summary="Inquiry-only close.")

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total") as ctr:
        executed = confirm_pending_transition(case, "user_test")
        ctr.inc.assert_not_called()

    assert executed is True
    assert case.state == CaseState.CLOSED
    assert case.pending_transition is None
