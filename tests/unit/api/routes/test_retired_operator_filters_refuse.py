"""The two renamed operator filters REFUSE the old name rather than ignoring it.

ADR-017 renamed a query parameter on each of the two break-glass surfaces.
FastAPI drops an undeclared query parameter silently, so a client still sending
the old name would receive a **200 with every row in it** — an unfiltered answer
presented as a filtered one, with nothing in the response saying so. On a
governance surface that is the wrong direction: the caller asked "who reached
THAT tenant's data" and would be handed every tenant's.

That is worse than a 422, so it is a 422. The old parameter is declared solely so
it can be refused, kept out of the published schema, and never served.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from faultmaven.api.routes.admin_cases import list_operator_access_audit
from faultmaven.api.routes.admin_grants import list_grants

pytestmark = [pytest.mark.unit, pytest.mark.security]

ENTERPRISE = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def _grant_repo() -> MagicMock:
    repo = MagicMock()
    repo.list_grants = AsyncMock(return_value=([], 0))
    return repo


def _audit_repo() -> MagicMock:
    repo = MagicMock()
    repo.list_access = AsyncMock(return_value=([], 0))
    return repo


async def test_the_grant_listing_refuses_the_retired_organization_filter():
    repo = _grant_repo()

    with pytest.raises(HTTPException) as exc:
        await list_grants(
            current_user=MagicMock(),
            grant_repo=repo,
            organization_id=ENTERPRISE,
        )

    assert exc.value.status_code == 422
    assert "enterprise_id" in str(exc.value.detail)
    repo.list_grants.assert_not_awaited(), (
        "the query ran with the filter dropped, which answers with every grant"
    )


async def test_the_grant_listing_still_filters_on_the_new_name():
    """The control: the refusal must be about the old name, not about filtering."""
    repo = _grant_repo()

    await list_grants(
        current_user=MagicMock(),
        grant_repo=repo,
        enterprise_id=ENTERPRISE,
        # Every parameter is explicit: called directly rather than through
        # FastAPI, an omitted one keeps its ``Query`` default OBJECT rather than
        # becoming its value — which the refusal reads as "the caller sent it",
        # and which the paging arithmetic cannot add up.
        organization_id=None,
        operator_user_id=None,
        case_id=None,
        live_only=False,
        limit=100,
        offset=0,
    )

    assert repo.list_grants.await_args.kwargs["target_enterprise_id"] == ENTERPRISE


async def test_the_audit_trail_refuses_the_retired_target_filter():
    repo = _audit_repo()

    with pytest.raises(HTTPException) as exc:
        await list_operator_access_audit(
            current_user=MagicMock(),
            audit_repo=repo,
            target_organization_id=ENTERPRISE,
        )

    assert exc.value.status_code == 422
    assert "target_enterprise_id" in str(exc.value.detail)
    repo.list_access.assert_not_awaited()


async def test_the_audit_trail_still_filters_on_the_new_name():
    repo = _audit_repo()

    await list_operator_access_audit(
        current_user=MagicMock(),
        audit_repo=repo,
        target_enterprise_id=ENTERPRISE,
        target_organization_id=None,
        operator_user_id=None,
        target_case_id=None,
        action=None,
        grant_id=None,
        limit=100,
        offset=0,
    )

    assert repo.list_access.await_args.kwargs["target_enterprise_id"] == ENTERPRISE


def test_the_refused_names_are_not_published():
    """A declared-only-to-refuse parameter must not read as a supported filter."""
    import json
    from pathlib import Path

    spec = json.loads(
        Path("docs/reference/api/openapi.json").read_text(encoding="utf-8")
    )
    for path, retired in (
        ("/api/v1/admin/grants", "organization_id"),
        ("/api/v1/admin/audit/operator-access", "target_organization_id"),
    ):
        operation = spec["paths"][path]["get"]
        names = {p["name"] for p in operation.get("parameters", [])}
        assert retired not in names, (
            f"{path} publishes {retired!r}, so a generated client would offer a "
            "filter this endpoint only ever refuses"
        )
