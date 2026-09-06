"""The recommendation dedup's team arm keys on the ENTERPRISE (ADR-017 D1/D4).

``resolve_shared_kb_ids`` matches the share row's ``enterprise_id`` — that is what
lets one team span two organizations. The report-recommendations route fed it the
caller's ORGANIZATION claim instead, which is a different column with a different
meaning and, in every configuration this campaign ships, a different value: absent
in standalone and in a beta cloud account that is in no organization, and a billing
id where it is present. The team arm was therefore empty on every deployment, and
a runbook a colleague had shared to a common team did not count as a duplicate —
so the dedup recommended generating one that already existed.

Both halves are pinned: the service asks the share table for the enterprise it was
given, and the route gives it the enterprise the request is bound to.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    ProblemVerification,
)
from faultmaven.modules.report.domain.services.report_recommendation_service import (
    ReportRecommendationService,
)

pytestmark = [pytest.mark.unit]

ENTERPRISE = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
BILLING_ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REQUESTER = "user-requester-1"
CASE_ID = "case_aaaabbbbcccc"


@pytest.fixture(autouse=True)
def _bound_enterprise():
    set_current_enterprise_id(ENTERPRISE)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


def _case() -> Case:
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    return Case(
        created_at=opened,
        case_id=CASE_ID,
        user_id="user-owner",
        enterprise_id=ENTERPRISE,
        title="Checkout timeouts",
        description="Checkout orders time out under sustained load.",
        state=CaseState.RESOLVED,
        resolved_at=datetime(2026, 1, 2, tzinfo=UTC),
        closed_at=datetime(2026, 1, 2, tzinfo=UTC),
        problem_verification=ProblemVerification(
            symptom_statement="Timeouts", severity="HIGH"
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Timeouts",
        ),
    )


async def test_the_share_lookup_is_asked_for_the_enterprise_not_the_billing_org():
    """The service passes the enterprise it was handed straight to the share table."""
    share_repository = MagicMock()
    share_repository.list_resource_ids = AsyncMock(return_value=["kb-shared-7"])
    team_service = MagicMock()
    team_service.list_all_user_team_ids = AsyncMock(return_value=["team-1"])
    service = ReportRecommendationService(
        runbook_kb=MagicMock(),
        team_service=team_service,
        share_repository=share_repository,
    )

    scope = await service._resolve_requester_scope(REQUESTER, ENTERPRISE)

    assert (
        share_repository.list_resource_ids.await_args.kwargs["enterprise_id"]
        == ENTERPRISE
    )
    assert {"parent_document_id": {"$in": ["kb-shared-7"]}} in scope["$or"]


async def test_the_route_hands_the_service_the_bound_enterprise():
    """The route's own half: not the organization claim, which names a payer."""
    from faultmaven.modules.case.api import routes as case_routes

    case_service = MagicMock()
    case_service.get_case = AsyncMock(return_value=_case())

    captured = {}

    class _Recording:
        def __init__(self, **_kwargs):
            pass

        async def get_available_report_types(self, case, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                runbook_recommendation=SimpleNamespace(action="generate"),
                available_for_generation=[],
                model_dump=lambda: {},
            )

    request = MagicMock()
    request.app.state.team_service = None
    request.app.state.share_repository = None

    # The caller is in a BILLING organization, and that value must not be what
    # reaches the team arm.
    current_user = SimpleNamespace(
        user_id=REQUESTER, organization_id=BILLING_ORG, enterprise_id=ENTERPRISE
    )

    import faultmaven.modules.report.domain.services.report_recommendation_service as svc

    original = svc.ReportRecommendationService
    svc.ReportRecommendationService = _Recording
    try:
        await case_routes.get_report_recommendations(
            case_id=CASE_ID,
            request=request,
            case_service=case_service,
            current_user=current_user,
            runbook_kb=MagicMock(),
        )
    finally:
        svc.ReportRecommendationService = original

    assert captured.get("requester_enterprise_id") == ENTERPRISE
    assert BILLING_ORG not in captured.values()
