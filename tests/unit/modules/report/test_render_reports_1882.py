"""``render_reports`` renders without writing; ``generate_reports`` is it plus a write (#1882).

A turn will carry its terminal summary to the turn's own commit, so it needs
the render without the ``add_report``. Rows it holds but has not committed
(``pending``) count against the regeneration cap and towards the next version
exactly as committed rows do — otherwise a turn holding one uncommitted
regeneration could render a third version past ``MAX_REGENERATIONS``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from faultmaven.exceptions import ValidationException
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import InquiryData
from faultmaven.modules.case.domain.owned_models.report import ReportType
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)

pytestmark = [pytest.mark.unit]

CLOSURE = ReportType.CLOSURE_SUMMARY
MAX = ReportGenerationService.MAX_REGENERATIONS


def _closed_case() -> Case:
    case = Case(
        case_id="case_0123456789ab",
        enterprise_id="ent_1882",
        title="Render case",
        description="Queries time out",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True, proposed_problem_statement="Timeouts"
        ),
    )
    object.__setattr__(case, "state", CaseState.CLOSED)
    object.__setattr__(case, "closed_at", datetime.now(timezone.utc))
    return case


@pytest.fixture
def repo():
    return InMemoryCaseRepository()


@pytest.fixture
def service(repo):
    # A lock manager the render must never take: any call on it is recorded.
    return ReportGenerationService(case_repository=repo, lock_manager=MagicMock())


@pytest.mark.asyncio
async def test_render_writes_nothing_and_takes_no_lock(service, repo):
    case = _closed_case()

    reports = await service.render_reports(case, [CLOSURE])

    assert [r.report_type for r in reports] == [CLOSURE]
    assert reports[0].version == 1
    assert await repo.count_reports(case.case_id) == 0
    assert service.lock_manager.mock_calls == []


@pytest.mark.asyncio
async def test_pending_counts_towards_the_version(service):
    reports = await service.render_reports(_closed_case(), [CLOSURE], pending=1)
    assert reports[0].version == 2


@pytest.mark.asyncio
async def test_pending_counts_against_the_cap(service, repo):
    case = _closed_case()
    (stored,) = await service.render_reports(case, [CLOSURE])
    await repo.add_report(stored)

    # One committed + one the caller holds uncommitted = the cap.
    with pytest.raises(ValidationException) as exc:
        await service.render_reports(case, [CLOSURE], pending=MAX - 1)
    assert str(exc.value) == "regeneration_limit_exceeded"

    # Control: with nothing held, the same case still has a slot.
    (again,) = await service.render_reports(case, [CLOSURE])
    assert again.version == 2


@pytest.mark.asyncio
async def test_a_type_repeated_in_one_call_gets_consecutive_versions(service):
    first, second = await service.render_reports(_closed_case(), [CLOSURE, CLOSURE])
    assert (first.version, second.version) == (1, 2)


@pytest.mark.asyncio
async def test_render_refuses_what_generate_refuses(service):
    with pytest.raises(ValidationException) as exc:
        await service.render_reports(_closed_case(), [ReportType.RUNBOOK])
    assert str(exc.value) == "invalid_report_type"

    open_case = _closed_case()
    object.__setattr__(open_case, "state", CaseState.INVESTIGATING)
    with pytest.raises(ValidationException) as exc:
        await service.render_reports(open_case, [CLOSURE])
    assert str(exc.value) == "invalid_case_state"


@pytest.mark.asyncio
async def test_generate_is_render_plus_a_write_up_to_the_cap(repo):
    service = ReportGenerationService(case_repository=repo)
    case = _closed_case()

    first = await service.generate_reports(case, [CLOSURE])
    assert [r.version for r in first.reports] == [1]
    assert first.remaining_regenerations == MAX - 1
    assert await repo.count_reports(case.case_id, CLOSURE) == 1

    second = await service.generate_reports(case, [CLOSURE])
    assert [r.version for r in second.reports] == [2]
    assert second.remaining_regenerations == 0
    assert await repo.count_reports(case.case_id, CLOSURE) == 2

    with pytest.raises(ValidationException) as exc:
        await service.generate_reports(case, [CLOSURE])
    assert str(exc.value) == "regeneration_limit_exceeded"
    assert await repo.count_reports(case.case_id, CLOSURE) == 2
