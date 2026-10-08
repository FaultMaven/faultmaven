"""#1882 part 1: the pieces a turn's single commit is built from.

- ``TurnCommitPlan`` / ``commit_turn_plan`` commit the case with the turn's
  rows in one ``save`` and settle the gates: released on success, cancelled on
  any failure, and settling twice never raises.

The repository half (one transaction, RLS) is pinned in
``tests/unit/modules/case/infrastructure/test_turn_rows_commit_with_case_1882.py``
and its PostgreSQL twin; ``render_reports`` in
``tests/unit/modules/report/test_render_reports_1882.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.turn_commit import (
    TurnCommitPlan,
    commit_turn_plan,
)
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.owned_models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)

pytestmark = [pytest.mark.unit]


def _case(turn: int = 3) -> Case:
    case = Case(
        case_id="case_aabbccddeeff",
        enterprise_id="ent_1882",
        title="Plumbing case",
        state=CaseState.INQUIRY,
    )
    case.current_turn = turn
    return case


def _report(case: Case) -> CaseReport:
    return CaseReport(
        case_id=case.case_id,
        report_type=ReportType.CLOSURE_SUMMARY,
        title="Closure Summary: Plumbing case",
        content="# Closure summary",
        generation_status=ReportStatus.COMPLETED,
        generated_at=datetime.now(timezone.utc).isoformat(),
        generation_time_ms=1,
    )


class TestTurnCommitPlan:
    @pytest.mark.asyncio
    async def test_commit_stores_the_rows_with_the_case_and_releases_the_gates(self):
        repo = InMemoryCaseRepository()
        case = _case()
        plan = TurnCommitPlan()
        report = _report(case)
        plan.reports.append(report)
        gate = plan.gate()

        saved = await commit_turn_plan(repo, case, plan)

        assert saved is case and case.version == 1
        assert await repo.get_report(report.report_id) is report
        assert gate.done() and not gate.cancelled()
        assert gate.result() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            StaleCaseException(
                case_id="case_aabbccddeeff", expected_version=1, actual_version=2
            ),
            RuntimeError("commit failed"),
            asyncio.CancelledError(),
        ],
        ids=["stale", "error", "cancelled"],
    )
    async def test_a_failed_commit_cancels_the_gates_and_reraises(self, error):
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=error)
        plan = TurnCommitPlan()
        gate = plan.gate()

        with pytest.raises(type(error)):
            await commit_turn_plan(repo, _case(), plan)

        assert gate.cancelled()

    @pytest.mark.asyncio
    async def test_commit_hands_save_the_plans_rows(self):
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=lambda case, **_: case)
        case = _case()
        plan = TurnCommitPlan()
        report = _report(case)
        plan.reports.append(report)

        await commit_turn_plan(repo, case, plan)

        repo.save.assert_awaited_once_with(case, reports=(report,), receipt=None)

    @pytest.mark.asyncio
    async def test_settling_twice_never_raises(self):
        plan = TurnCommitPlan()
        released, other = plan.gate(), plan.gate()
        plan.release_gates()
        plan.release_gates()
        plan.cancel_gates()
        assert released.result() is None and other.result() is None

        cancelled_plan = TurnCommitPlan()
        gate = cancelled_plan.gate()
        cancelled_plan.cancel_gates()
        cancelled_plan.cancel_gates()
        cancelled_plan.release_gates()
        assert gate.cancelled()
