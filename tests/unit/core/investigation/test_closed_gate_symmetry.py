"""Confirm-time resolve-preservation for the CLOSED gate (INV-37, #656).

The resolve gate is protective because RESOLVED asserts a positive
conclusion ("it's fixed"); the close gate is intentionally thin because
CLOSED asserts nothing. The one place the two must stay coupled is the
SUGGEST_RESOLVE pivot: a case that *can* be resolved must never be
terminally recorded as closed-unresolved.

That pivot already fires at proposal time (``assess_closure_readiness``
→ SUGGEST_RESOLVE, wired in the LLM-emit and dropdown paths). This suite
pins the confirm-time guard in ``confirm_open_offer``: a qualifying
``causal_absence`` can land AFTER a close was offered, so the guard re-checks
at the single execution chokepoint immediately before the close commits. On a
hit it answers the close ``pivoted``, opens a RESOLVED offer in its place and
reports that nothing terminal executed (returns False) — the caller
re-presents the resolve confirmation.

"resolve = close WITH resolution; close = close WITHOUT resolution": it is
always safe to resolve a resolvable case whichever terminal the user asked
for. There is no symmetric counterpart (every case is closable; closing is
always a valid disposition), so the asymmetry stays principled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation import terminal_transitions
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.turn_commit import TurnCommitPlan
from faultmaven.core.investigation.offer_projection import project_offers
from faultmaven.core.investigation.terminal_transitions import (
    ClosureReadiness,
    assess_closure_readiness,
    confirm_open_offer,
)
from faultmaven.modules.case.contracts import (
    AnswerKind,
    AnswerVia,
    Case,
    CaseState,
    EvidenceCategory,
    EvidenceSourceType,
    ProblemStatus,
)
from faultmaven.modules.case.domain.models.conclusion import (
    ConfidenceLevel,
    RootCauseConclusion,
)
from faultmaven.modules.case.domain.models.evidence import Evidence
from faultmaven.modules.case.domain.models.solution import Solution, SolutionType
from tests.case_builders import investigating_case, with_open_offer

pytestmark = pytest.mark.unit


def _make_investigating_case() -> Case:
    return investigating_case(
        case_id="case_c0bbeef0a510",
        title="Closed-gate symmetry test",
        user_id="user_test",
        enterprise_id="org_test",
        description="Test description",
        statement="Test problem",
    )


def _attach_root_cause(case: Case) -> None:
    case.progress.problem_status = ProblemStatus.VERIFIED
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


def _attach_confirmation(case: Case) -> None:
    """The RESOLVED proof, gone ⇒ gone: the root cause confirmed eliminated
    after the fix (a ``causal_absence_evidence`` row) and the reported
    symptom confirmed gone with it (a ``symptom_absence_evidence`` row at
    the same turn, #1906)."""
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
    case.evidence.append(
        Evidence(
            summary="Post-fix check: the reported symptom no longer occurs",
            category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            collected_at=datetime.now(UTC),
            collected_by="user_test",
            primary_purpose="Confirm the reported symptom is gone",
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
    _attach_confirmation(case)
    # Precondition sanity: this case IS resolvable.
    assert assess_closure_readiness(case).verdict == ClosureReadiness.SUGGEST_RESOLVE
    with_open_offer(case, kind="close", summary="Closing as unresolved.")
    return case


def _confirm(case: Case) -> bool:
    return confirm_open_offer(case, case.effective_driver_id, via=AnswerVia.CLICK)


# ---------------------------------------------------------------------------
# INV-37 core: a resolvable case never terminally closes.
# ---------------------------------------------------------------------------


def test_confirm_pending_close_pivots_to_resolved_when_resolvable():
    case = _resolvable_case_with_pending_close()

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total") as ctr:
        executed = _confirm(case)
        ctr.inc.assert_called_once()

    # Nothing terminal committed.
    assert executed is False
    assert case.state == CaseState.INVESTIGATING
    # The close was answered ``pivoted`` and a RESOLVED offer opened in its
    # place, so the caller re-presents the resolve confirmation.
    assert [a.answer for a in case.offer_record.answers] == [AnswerKind.PIVOTED]
    assert case.offer_record.open is not None
    assert case.offer_record.open.kind == "resolve"
    assert case.offer_record.open.pivoted_from_close
    # The pivot stores the canonical SUGGEST_RESOLVE message as the pending's
    # summary — the ONE source of truth the callers re-present (matching the
    # proposal-time pivot), not a re-derived string.
    assert case.offer_record.open.summary == assess_closure_readiness(case).message


def test_pivot_message_handles_out_of_band_fix_without_record():
    """An out-of-band fix yields a causal_absence with NO root_cause_conclusion
    and NO Solution record — still resolvable. The pivot message must be the
    canonical SUGGEST_RESOLVE prose (which reads gracefully), never a
    self-contradictory 'Root cause: Not yet identified' rendering."""
    case = _make_investigating_case()
    case.progress.problem_status = ProblemStatus.VERIFIED
    _attach_confirmation(case)  # no root cause, no solution on record
    assert assess_closure_readiness(case).verdict == ClosureReadiness.SUGGEST_RESOLVE
    with_open_offer(case, kind="close", summary="Closing as unresolved.")

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total"):
        executed = _confirm(case)

    assert executed is False
    assert case.offer_record.open.kind == "resolve"
    msg = case.offer_record.open.summary
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
    with_open_offer(case, kind="close", summary="Closing as stabilized.")

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total") as ctr:
        executed = _confirm(case)
        ctr.inc.assert_not_called()

    assert executed is True
    assert case.state == CaseState.CLOSED
    assert case.offer_record.open is None


def test_confirm_pending_resolve_is_unaffected_by_the_guard():
    """A pending RESOLVED confirm executes normally — the close-scoped guard
    never runs on it."""
    case = _make_investigating_case()
    _attach_root_cause(case)
    _attach_solution(case)
    _attach_confirmation(case)
    with_open_offer(case, kind="resolve", summary="Resolving.")

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total") as ctr:
        executed = _confirm(case)
        ctr.inc.assert_not_called()

    assert executed is True
    assert case.state == CaseState.RESOLVED
    assert case.offer_record.open is None


async def test_a_bare_yes_on_the_close_surfaces_the_resolve_confirmation():
    """Engine wiring: a bare "yes" to an open CLOSE on a resolvable case is
    answered at the engine's gate (0b) and pivots to RESOLVED — the turn
    reports no terminal transition, never reaches the model, and surfaces the
    resolve DECIDE pair for the re-presented confirmation."""
    case = _resolvable_case_with_pending_close()
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(return_value=None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        side_effect=AssertionError("the gate answers a bare yes")
    )
    case.current_turn += 1

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total"):
        result = await engine.process_turn(
            case=case, user_message="yes", redaction_ctx=None
        )

    # No terminal transition; the case stays INVESTIGATING with a RESOLVED
    # offer open.
    updated = result["case_updated"]
    assert updated.state == CaseState.INVESTIGATING
    assert updated.offer_record.open.kind == "resolve"
    assert result["metadata"].get("status_transitioned") is not True
    assert result["agent_response"] == updated.offer_record.open.summary
    assert result["suggested_follow_ups"] == project_offers(updated).pair
    assert [card["label"] for card in result["suggested_follow_ups"]][0] == (
        "Yes, mark as resolved"
    )


def test_pivot_scoped_to_investigating_never_proposes_invalid_inquiry_edge():
    """The guard is scoped to INVESTIGATING: RESOLVED is not a valid edge from
    INQUIRY, so an INQUIRY→CLOSED confirm must close normally even if a
    (contrived) causal_absence row is present — never pivot to an invalid
    INQUIRY→RESOLVED proposal."""
    case = _make_investigating_case()
    case.atomic_update(state=CaseState.INQUIRY)
    _attach_root_cause(case)
    _attach_solution(case)
    _attach_confirmation(case)
    with_open_offer(case, kind="close", summary="Inquiry-only close.")

    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total") as ctr:
        executed = _confirm(case)
        ctr.inc.assert_not_called()

    assert executed is True
    assert case.state == CaseState.CLOSED
    assert case.offer_record.open is None
