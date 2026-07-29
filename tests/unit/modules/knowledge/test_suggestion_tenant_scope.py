"""Suggestion routes resolve ids inside the actor's tenant (#876).

All six suggestion routes are gated on ``platform_admin``. That role says what
an operator may *do*, never *whose* data they may see: without a tenant
predicate every id-addressed route reached a bare store lookup, and four of them
are writes. These tests pin the predicate on all of them, and pin that an
out-of-scope id answers **404** — the same answer as an absent id, so the status
code is not an existence oracle.

Companion rule: ``docs/architecture/security/rbac.md`` — "Tenant-Scoped
Resolution".
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.v1.auth_dependencies import (
    UNSCOPED_REQUEST_MSG,
    require_platform_admin,
)
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.api.routes import get_suggestion_service
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

ORG_A = "org-alpha-11111111"
ORG_B = "org-beta-22222222"

SUG_A = "sug_ownedbyorga"
SUG_B = "sug_ownedbyorgb"


def _suggestion(suggestion_id: str, organization_id: str) -> KnowledgeSuggestion:
    """A review-ready suggestion. Identical across tenants but for the org, so a
    404 can only come from the tenant predicate and not from some other guard
    (unscanned PII, wrong status) firing first."""
    return KnowledgeSuggestion(
        suggestion_id=suggestion_id,
        organization_id=organization_id,
        case_id="case-1",
        status=SuggestionStatus.PENDING_REVIEW,
        suggested_title="Connection pool exhaustion",
        suggested_content="## Problem\n...",
        suggested_type="troubleshooting_guide",
        extracted_by="u-extractor",
        extracted_at=datetime.now(timezone.utc),
        pii_scan_status=PIIScanStatus.CLEAN,
        source_case_title="Connection pool exhaustion",
    )


def _service() -> SuggestionService:
    """A REAL service holding one suggestion per tenant.

    Both rows are review-ready, so the org-B row would be returned/acted on by
    every route but for the tenant predicate.
    """
    service = SuggestionService()
    service._suggestions_store[SUG_A] = _suggestion(SUG_A, ORG_A)
    service._suggestions_store[SUG_B] = _suggestion(SUG_B, ORG_B)
    return service


def _admin(organization_id) -> DevUser:
    return DevUser(
        user_id="user-admin",
        username="admin",
        email="admin@example.com",
        display_name="Admin",
        created_at=datetime.now(timezone.utc),
        roles=["admin", "platform_admin"],
        organization_id=organization_id,
    )


def _client(service, user) -> TestClient:
    from faultmaven.api.exception_handlers import get_exception_handlers

    app = FastAPI()
    app.include_router(knowledge_router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[get_suggestion_service] = lambda: service
    app.dependency_overrides[require_platform_admin] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _call(client, suggestion_id: str):
    """Every id-addressed suggestion route, as (name, response) pairs."""
    return [
        ("get", client.get(f"/knowledge/suggestions/{suggestion_id}")),
        ("update", client.put(f"/knowledge/suggestions/{suggestion_id}", json={})),
        (
            "approve",
            client.post(f"/knowledge/suggestions/{suggestion_id}/approve", json={}),
        ),
        (
            "reject",
            client.post(
                f"/knowledge/suggestions/{suggestion_id}/reject",
                json={"rejection_reason": "no"},
            ),
        ),
        (
            "remediate",
            client.post(f"/knowledge/suggestions/{suggestion_id}/remediate-pii"),
        ),
    ]


ROUTES = ["get", "update", "approve", "reject", "remediate"]

_SINGLE = patch(
    "faultmaven.providers.tenancy.factory.requested_tenant_provider",
    return_value=BUILTIN_SINGLE,
)
_MULTI = patch(
    "faultmaven.providers.tenancy.factory.requested_tenant_provider",
    return_value=BUILTIN_MULTI,
)


# =============================================================================
# Cross-tenant: 404, never 403, never 200 — on every id-addressed route
# =============================================================================


@pytest.mark.security
@pytest.mark.parametrize("actor_org,foreign_id", [(ORG_A, SUG_B), (ORG_B, SUG_A)])
def test_another_tenants_suggestion_is_404_on_every_id_route(actor_org, foreign_id):
    """Swept in both directions so neither tenant is privileged by accident."""
    service = _service()
    client = _client(service, _admin(actor_org))

    for name, resp in _call(client, foreign_id):
        assert resp.status_code == 404, f"{name} answered {resp.status_code}"
        assert resp.json()["detail"] == "Suggestion not found"

    # And nothing was mutated behind the 404s.
    foreign = service._suggestions_store[foreign_id]
    assert foreign.status is SuggestionStatus.PENDING_REVIEW
    assert foreign.reviewed_by is None
    assert foreign.pii_remediated_by is None


@pytest.mark.security
@pytest.mark.parametrize("route", ROUTES)
def test_an_absent_id_answers_exactly_as_a_foreign_one(route):
    """If "absent" and "someone else's" differed, the status code would leak
    whether the id exists."""
    client = _client(_service(), _admin(ORG_A))
    absent = dict(_call(client, "sug_doesnotexist0"))[route]
    foreign = dict(_call(_client(_service(), _admin(ORG_A)), SUG_B))[route]
    assert absent.status_code == foreign.status_code == 404
    assert absent.json()["detail"] == foreign.json()["detail"]


@pytest.mark.security
@pytest.mark.parametrize("actor_org,own_id", [(ORG_A, SUG_A), (ORG_B, SUG_B)])
def test_the_owning_tenant_still_reaches_its_own_suggestion(actor_org, own_id):
    """The positive arm — without it the 404 sweep above would pass on a route
    that simply refuses everything."""
    with _SINGLE:
        client = _client(_service(), _admin(actor_org))
        resp = client.get(f"/knowledge/suggestions/{own_id}")
        assert resp.status_code == 200
        assert resp.json()["suggestion_id"] == own_id

        resp = client.put(f"/knowledge/suggestions/{own_id}", json={"title": "new"})
        assert resp.status_code == 200

        # 409 (not 404): the tenant predicate passed and the domain's own
        # "nothing to remediate, this scan came back CLEAN" guard fired. Reaching
        # a domain conflict is proof the id resolved.
        resp = client.post(f"/knowledge/suggestions/{own_id}/remediate-pii")
        assert resp.status_code == 409

        resp = client.post(
            f"/knowledge/suggestions/{own_id}/reject",
            json={"rejection_reason": "not reusable"},
        )
        assert resp.status_code == 200


@pytest.mark.security
def test_approve_reaches_the_owning_tenants_suggestion():
    with _SINGLE:
        service = _service()
        client = _client(service, _admin(ORG_A))
        resp = client.post(f"/knowledge/suggestions/{SUG_A}/approve", json={})
    assert resp.status_code == 201
    assert service._suggestions_store[SUG_A].status is SuggestionStatus.APPROVED


# =============================================================================
# The list route is scoped too — the same hole, one route over
# =============================================================================


@pytest.mark.security
@pytest.mark.parametrize("actor_org,expected", [(ORG_A, [SUG_A]), (ORG_B, [SUG_B])])
def test_list_returns_only_the_actors_tenant(actor_org, expected):
    client = _client(_service(), _admin(actor_org))
    resp = client.get("/knowledge/suggestions")
    assert resp.status_code == 200
    assert [s["suggestion_id"] for s in resp.json()["suggestions"]] == expected
    assert resp.json()["total_count"] == 1


# =============================================================================
# Fail closed: no tenant is a refusal, never a widened query
# =============================================================================


@pytest.mark.security
@pytest.mark.parametrize("route", ROUTES)
def test_an_org_less_actor_is_refused_on_every_id_route(route):
    client = _client(_service(), _admin(""))
    resp = dict(_call(client, SUG_A))[route]
    assert resp.status_code == 403
    assert resp.json()["detail"] == UNSCOPED_REQUEST_MSG


@pytest.mark.security
def test_an_org_less_actor_is_refused_on_the_list_route():
    client = _client(_service(), _admin(""))
    resp = client.get("/knowledge/suggestions")
    assert resp.status_code == 403
    assert resp.json()["detail"] == UNSCOPED_REQUEST_MSG


@pytest.mark.security
@pytest.mark.parametrize("route", ROUTES)
def test_the_standalone_sentinel_is_not_a_tenant_under_multi(route):
    """Under ``TENANT_PROVIDER=multi`` the sentinel identifies the single-tenant
    deployment, not an organization — accepting it would scope an operator to a
    pseudo-tenant that owns nothing and bypass the real one."""
    from faultmaven.config.constants import STANDALONE_ORG_ID

    client = _client(_service(), _admin(STANDALONE_ORG_ID))
    with _MULTI:
        resp = dict(_call(client, SUG_A))[route]
    assert resp.status_code == 403
    assert resp.json()["detail"] == UNSCOPED_REQUEST_MSG


@pytest.mark.security
def test_the_standalone_sentinel_is_a_tenant_under_single():
    """The same id is the legitimate tenant in a Standalone deployment."""
    from faultmaven.config.constants import STANDALONE_ORG_ID

    service = SuggestionService()
    service._suggestions_store[SUG_A] = _suggestion(SUG_A, STANDALONE_ORG_ID)
    client = _client(service, _admin(STANDALONE_ORG_ID))
    with _SINGLE:
        resp = client.get(f"/knowledge/suggestions/{SUG_A}")
    assert resp.status_code == 200


# =============================================================================
# Service seam
# =============================================================================


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor_org,wanted,expected",
    [
        (ORG_A, SUG_A, SUG_A),
        (ORG_B, SUG_B, SUG_B),
        (ORG_A, SUG_B, None),
        (ORG_B, SUG_A, None),
        ("org-3", SUG_A, None),
    ],
)
async def test_get_suggestion_visible_sweeps_the_tenant_grid(
    actor_org, wanted, expected
):
    service = _service()
    got = await service.get_suggestion_visible(wanted, organization_id=actor_org)
    assert (got.suggestion_id if got else None) == expected


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("falsy_org", ["", None])
async def test_get_suggestion_visible_fails_closed_without_a_tenant(falsy_org):
    service = _service()
    assert (
        await service.get_suggestion_visible(SUG_A, organization_id=falsy_org) is None
    )


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row_org,actor_org", [("", ""), (None, None), ("", None), (None, "")]
)
async def test_a_tenantless_actor_cannot_match_a_tenantless_row(row_org, actor_org):
    """Fail-closed must not degenerate into "no org equals no org".

    Equality alone would let an org-less actor read an org-less row: both sides
    are falsy, so the comparison agrees and the guard reads as satisfied. The
    explicit falsy check is what keeps such a row unreachable rather than
    universally readable.

    ``KnowledgeSuggestion.__post_init__`` refuses to construct an org-less
    suggestion, so this row is mutated into existence after construction — the
    store is a plain mapping, and this is the state the equality check alone
    would mishandle.
    """
    orphan = _suggestion("sug_orphan", ORG_A)
    orphan.organization_id = row_org
    service = SuggestionService()
    service._suggestions_store["sug_orphan"] = orphan
    got = await service.get_suggestion_visible("sug_orphan", organization_id=actor_org)
    assert got is None


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("falsy_org", ["", None])
async def test_list_suggestions_fails_closed_without_a_tenant(falsy_org):
    result = await _service().list_suggestions(organization_id=falsy_org)
    assert result["suggestions"] == []
    assert result["total_count"] == 0


@pytest.mark.security
@pytest.mark.asyncio
async def test_the_unscoped_load_is_still_available_to_trusted_callers():
    """``get_suggestion`` stays unscoped on purpose — extraction has no actor to
    scope by. The split is the point; collapsing them would break that path."""
    service = _service()
    assert (await service.get_suggestion(SUG_B)).organization_id == ORG_B
