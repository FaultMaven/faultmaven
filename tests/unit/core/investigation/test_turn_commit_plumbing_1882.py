"""#1882 part 1: the pieces a turn's single commit is built from.

- ``CheckpointService.capture`` snapshots without touching storage, and its id
  tells two sites in one turn apart while a site that fires twice collides.
- ``TurnCommitPlan`` / ``commit_turn_plan`` commit the case with the turn's
  rows in one ``save`` and settle the gates: released on success, cancelled on
  any failure, and settling twice never raises.

The repository half (one transaction, loud collisions, RLS) is pinned in
``tests/unit/modules/case/infrastructure/test_turn_rows_commit_with_case_1882.py``
and its PostgreSQL twin; ``render_reports`` in
``tests/unit/modules/report/test_render_reports_1882.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.checkpoint_service import (
    CheckpointService,
    checkpoint_id_for,
)
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


def _transition(case: Case, to_state: str):
    return CheckpointService.capture(
        case,
        trigger="pre_case_action",
        metadata={"from_state": case.state.value, "to_state": to_state},
    )


class TestCapture:
    def test_capture_touches_no_storage(self):
        repo = MagicMock()
        service = CheckpointService(repo)
        case = _case()

        checkpoint = service.capture(case, "pre_case_action", {"to_state": "closed"})

        assert repo.mock_calls == []
        assert checkpoint.case_id == case.case_id
        assert checkpoint.turn_number == 3
        assert checkpoint.trigger == "pre_case_action"
        assert checkpoint.metadata == {"to_state": "closed"}
        assert checkpoint.case_snapshot["case_id"] == case.case_id
        assert len(checkpoint.snapshot_hash) == 64

    def test_two_sites_in_one_turn_get_two_ids(self):
        """Gate 1 (to investigating), a terminal confirm (to closed or
        resolved) and a statement revision (an action, no target state) never
        share an id, so no two of them can collide in one commit."""
        case = _case()
        ids = {
            _transition(case, "investigating").checkpoint_id,
            _transition(case, "closed").checkpoint_id,
            _transition(case, "resolved").checkpoint_id,
            CheckpointService.capture(
                case, "pre_case_action", {"action": "problem_statement_revised"}
            ).checkpoint_id,
        }
        assert len(ids) == 4

    def test_one_site_firing_twice_in_a_turn_collides(self):
        case = _case()
        assert (
            _transition(case, "closed").checkpoint_id
            == _transition(case, "closed").checkpoint_id
        )

    def test_the_turn_and_the_case_are_in_the_id(self):
        a = checkpoint_id_for("case_aabbccddeeff", 3, "pre_case_action", "closed")
        assert a != checkpoint_id_for(
            "case_aabbccddeeff", 4, "pre_case_action", "closed"
        )
        assert a != checkpoint_id_for(
            "case_ffeeddccbbaa", 3, "pre_case_action", "closed"
        )
        assert a != checkpoint_id_for("case_aabbccddeeff", 3, "turn_complete", "closed")

    def test_the_id_fits_the_column(self):
        """``case_checkpoints.checkpoint_id`` is VARCHAR(36); PostgreSQL
        refuses a longer value (pinned against a real PG in
        ``test_turn_rows_commit_with_case_postgres_1882.py``)."""
        assert len(_transition(_case(turn=99999), "investigating").checkpoint_id) <= 36

    @pytest.mark.asyncio
    async def test_create_checkpoint_is_capture_plus_its_own_write(self):
        repo = InMemoryCaseRepository()
        case = _case()
        created = await CheckpointService(repo).create_checkpoint(
            case, "pre_case_action", {"to_state": "closed"}
        )
        assert created is not None
        assert created.checkpoint_id == _transition(case, "closed").checkpoint_id
        assert await repo.get_checkpoint(created.checkpoint_id) is created

    @pytest.mark.asyncio
    async def test_create_checkpoint_still_logs_and_drops_a_failed_write(self):
        repo = MagicMock()
        repo.create_checkpoint = AsyncMock(side_effect=RuntimeError("db down"))
        assert await CheckpointService(repo).create_checkpoint(_case()) is None


class TestTurnCommitPlan:
    @pytest.mark.asyncio
    async def test_commit_stores_the_rows_with_the_case_and_releases_the_gates(self):
        repo = InMemoryCaseRepository()
        case = _case()
        plan = TurnCommitPlan()
        report = _report(case)
        plan.reports.append(report)
        assert plan.add_checkpoint(_transition(case, "closed"))
        gate = plan.gate()

        saved = await commit_turn_plan(repo, case, plan)

        assert saved is case and case.version == 1
        assert await repo.get_report(report.report_id) is report
        assert len(await repo.get_checkpoints(case.case_id)) == 1
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
        report, checkpoint = _report(case), _transition(case, "closed")
        plan.reports.append(report)
        plan.add_checkpoint(checkpoint)

        await commit_turn_plan(repo, case, plan)

        repo.save.assert_awaited_once_with(
            case, reports=(report,), checkpoints=(checkpoint,)
        )

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

    def test_a_repeated_checkpoint_is_kept_once(self):
        case = _case()
        plan = TurnCommitPlan()
        first = _transition(case, "closed")
        assert plan.add_checkpoint(first) is True
        assert plan.add_checkpoint(_transition(case, "closed")) is False
        assert plan.add_checkpoint(_transition(case, "investigating")) is True
        assert plan.checkpoints[0] is first
        assert len(plan.checkpoints) == 2
