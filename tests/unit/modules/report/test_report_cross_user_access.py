"""Report routes must deny a same-organization stranger (#1044).

``CaseService.get_case`` collapses two different answers into ``None``: "no such
case" and "you may not see this case" (the owner ∪ shared-to-my-teams gate that
transitively guards reports, exports, analytics and messages). The report routes
used to read that ``None`` as "no case object, so no organization to compare"
and skip the check entirely, falling through to a repository read keyed on
``case_id``/``report_id`` alone. The only surviving boundary was the
organization, so two users in one org could read, edit, delete and re-link each
other's reports — the case narrative, with quoted logs and configs in it.

``validate_organization_access`` was already correct and already tested
(``test_report_org_scope.py``); nothing covered the ``case is None`` branch,
which is why the suite stayed green over it. These tests cover exactly that
branch, and they get their ``None`` from the real ``CaseService`` access-control
path rather than from a stubbed return value, so they fail if the gate itself
is loosened.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from faultmaven.modules.case.domain.models import Case
from faultmaven.modules.case.domain.owned_models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.report.api import routes

SHARED_ORG = "org_beta_group"
VICTIM = "user_victim"
ATTACKER = "user_attacker"
CASE_ID = "case_aaaabbbbcccc"
REPORT_ID = "report_1234"


def _victims_case() -> Case:
    """A case owned by the victim, in the organization both users share."""
    return Case(
        case_id=CASE_ID,
        user_id=VICTIM,
        enterprise_id=SHARED_ORG,
        title="Checkout latency spike",
    )


def _victims_report() -> CaseReport:
    return CaseReport(
        report_id=REPORT_ID,
        case_id=CASE_ID,
        report_type=ReportType.RESOLUTION_SUMMARY,
        title="Resolution summary for checkout latency",
        content="Root cause: connection pool exhaustion. db-primary logs attached.",
        generation_status=ReportStatus.COMPLETED,
        generation_time_ms=1200,
    )


def _case_service_holding(case: Case) -> CaseService:
    """A real CaseService over a repository that holds ``case``.

    Real, not mocked: the ``None`` these tests rely on has to come from the
    access-control branch in ``get_case``, otherwise they only prove that the
    routes propagate a stubbed ``None``. No share repository, so the allowlist
    collapses to owner-only — a case shared to the caller's team is a different
    scenario and is covered separately below.
    """
    repository = MagicMock()
    repository.get = AsyncMock(return_value=case)
    return CaseService(case_repository=repository)


def _attacker() -> MagicMock:
    user = MagicMock()
    user.user_id = ATTACKER
    return user


@pytest.fixture
def case_service() -> CaseService:
    return _case_service_holding(_victims_case())


@pytest.fixture
def case_repository() -> MagicMock:
    """Report reads/writes keyed on id alone — no user or case predicate.

    This mirrors the repository faithfully: ``get_report`` filters on
    ``report_id`` and ``get_reports`` on ``case_id``. If the route does not deny,
    the stranger gets the report back.
    """
    repository = MagicMock()
    repository.get_report = AsyncMock(return_value=_victims_report())
    repository.get_reports = AsyncMock(return_value=[_victims_report()])
    repository.update_report = AsyncMock(return_value=_victims_report())
    repository.delete_report = AsyncMock(return_value=True)
    repository.count_reports = AsyncMock(return_value=0)
    return repository


# ============================================================
# Every endpoint denies the stranger
# ============================================================


@pytest.mark.asyncio
@pytest.mark.security
async def test_get_report_denies_stranger(case_service, case_repository):
    with pytest.raises(HTTPException) as exc:
        await routes.get_report(
            report_id=REPORT_ID,
            current_user=_attacker(),
            tenant_provider=None,
            case_repository=case_repository,
            case_service=case_service,
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.security
async def test_list_reports_for_case_denies_stranger(case_service, case_repository):
    with pytest.raises(HTTPException) as exc:
        await routes.list_reports_for_case(
            case_id=CASE_ID,
            include_history=False,
            report_type=None,
            current_user=_attacker(),
            tenant_provider=None,
            case_repository=case_repository,
            case_service=case_service,
        )

    assert exc.value.status_code == 404
    case_repository.get_reports.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.security
async def test_get_report_versions_denies_stranger(case_service, case_repository):
    with pytest.raises(HTTPException) as exc:
        await routes.get_report_versions(
            report_id=REPORT_ID,
            current_user=_attacker(),
            tenant_provider=None,
            case_repository=case_repository,
            case_service=case_service,
        )

    assert exc.value.status_code == 404
    case_repository.get_reports.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.security
async def test_update_report_denies_stranger(case_service, case_repository):
    """The write must not land — denial has to precede ``update_report``."""
    request = routes.ReportUpdateRequest(content="Vandalised.")

    with pytest.raises(HTTPException) as exc:
        await routes.update_report(
            report_id=REPORT_ID,
            request=request,
            current_user=_attacker(),
            tenant_provider=None,
            case_repository=case_repository,
            case_service=case_service,
        )

    assert exc.value.status_code == 404
    case_repository.update_report.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.security
async def test_delete_report_denies_stranger(case_service, case_repository):
    with pytest.raises(HTTPException) as exc:
        await routes.delete_report(
            report_id=REPORT_ID,
            current_user=_attacker(),
            tenant_provider=None,
            case_repository=case_repository,
            case_service=case_service,
        )

    assert exc.value.status_code == 404
    case_repository.delete_report.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.security
async def test_delete_denies_before_disclosing_report_type(
    case_service, case_repository
):
    """A stranger deleting a runbook gets 404, not the runbook-specific 403.

    The runbook rule answers "what kind of report is this", which is itself
    information about someone else's case. Authorization is ordered first.
    """
    runbook = _victims_report()
    runbook.report_type = ReportType.RUNBOOK
    case_repository.get_report = AsyncMock(return_value=runbook)

    with pytest.raises(HTTPException) as exc:
        await routes.delete_report(
            report_id=REPORT_ID,
            current_user=_attacker(),
            tenant_provider=None,
            case_repository=case_repository,
            case_service=case_service,
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.security
async def test_link_case_denies_stranger(case_service, case_repository):
    with pytest.raises(HTTPException) as exc:
        await routes.link_report_to_case_closure(
            report_id=REPORT_ID,
            request=routes.LinkCaseRequest(),
            current_user=_attacker(),
            tenant_provider=None,
            case_repository=case_repository,
            case_service=case_service,
        )

    assert exc.value.status_code == 404
    case_repository.update_report.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.security
async def test_generate_report_denies_stranger(case_service):
    """/generate already denied; asserted here so the row does not regress."""
    request = MagicMock()
    request.report_types = [ReportType.RESOLUTION_SUMMARY]
    generation_service = MagicMock()
    generation_service.generate_reports = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await routes.generate_report(
            request=request,
            case_id=CASE_ID,
            current_user=_attacker(),
            tenant_provider=None,
            case_service=case_service,
            generation_service=generation_service,
        )

    assert exc.value.status_code == 404
    generation_service.generate_reports.assert_not_awaited()


# ============================================================
# The gate is a gate, not a wall
# ============================================================


@pytest.mark.asyncio
async def test_owner_still_reads_their_report(case_service, case_repository):
    """The owner is unaffected — the fix denies strangers, not everyone."""
    owner = MagicMock()
    owner.user_id = VICTIM

    response = await routes.get_report(
        report_id=REPORT_ID,
        current_user=owner,
        tenant_provider=None,
        case_repository=case_repository,
        case_service=case_service,
    )

    assert response.report_id == REPORT_ID


@pytest.mark.asyncio
async def test_teammate_with_a_share_still_reads_the_report(case_repository):
    """A case shared to the caller's team resolves through the same gate.

    The allowlist is owner ∪ shared-to-my-teams; denying the shared arm would be
    a different bug from the one being fixed.
    """
    repository = MagicMock()
    repository.get = AsyncMock(return_value=_victims_case())
    case_service = CaseService(case_repository=repository)
    case_service._resolve_shared_case_ids = AsyncMock(return_value=[CASE_ID])

    response = await routes.get_report(
        report_id=REPORT_ID,
        current_user=_attacker(),
        tenant_provider=None,
        case_repository=case_repository,
        case_service=case_service,
    )

    assert response.report_id == REPORT_ID


@pytest.mark.asyncio
@pytest.mark.security
async def test_missing_case_service_fails_closed(case_repository):
    """No case service means the gate cannot be evaluated — serve nothing.

    The old code's outer ``if ... and case_service`` skipped the check in this
    situation instead, which is the same fail-open shape one level up.
    """
    with pytest.raises(HTTPException) as exc:
        await routes.get_report(
            report_id=REPORT_ID,
            current_user=_attacker(),
            tenant_provider=None,
            case_repository=case_repository,
            case_service=None,
        )

    assert exc.value.status_code == 503
