"""``POST /cases/{id}/extract-knowledge`` stamps the ACTOR's tenant (#1227).

The route used to pass ``getattr(case, "enterprise_id", "default")`` to
``extract_knowledge_from_case``. Two things were wrong with that, and moving the
store into ``knowledge_suggestions`` turned the second from cosmetic into fatal:

1. **The write side has to agree with the read side.** Every suggestion review
   route — list, get, update, approve, reject, remediate — resolves its tenant
   predicate with ``require_actor_enterprise``. A suggestion stamped with
   anything else is invisible to the reviewer, which is the same
   extract-then-404 shape #1214 fixed, arrived at from the other end.
2. **``"default"`` is not an enterprise id.** It was inert while the store was
   a dict keyed by nothing. ``knowledge_suggestions.enterprise_id`` is a NOT
   NULL foreign key to ``enterprises`` and ``database.py`` sets
   ``PRAGMA foreign_keys=ON``, so the fallback now fails the INSERT — and under
   PostgreSQL RLS it would fail the policy's WITH CHECK too, because the value
   does not match the session's ``app.current_enterprise_id``.
   ``tests/integration/modules/knowledge/test_suggestion_durability_1227.py``
   measures that against a real database; this file pins what the route sends.

These are unit tests over the route: the suggestion service is a double whose
only job is to record which ``enterprise_id`` it was handed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import (
    UNSCOPED_REQUEST_MSG,
    require_authentication,
)
from faultmaven.api.v1.dependencies import (
    get_case_service,
    get_suggestion_service,
)
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.case.api.routes import router as case_router
from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

CASE_ID = "case_aabb11223344"
ACTOR_ENT = "ent-actor-1111"
CASE_ENT = "ent-onthecase-2222"


def _user(enterprise_id) -> DevUser:
    return DevUser(
        user_id="user-admin",
        username="admin",
        email="admin@example.com",
        display_name="Admin",
        created_at=datetime.now(timezone.utc),
        roles=["admin", "platform_admin"],
        enterprise_id=enterprise_id,
    )


class _Case:
    """A case object shaped like the one ``case_service.get_case`` returns."""

    def __init__(self, enterprise_id=CASE_ENT):
        self.case_id = CASE_ID
        self.title = "Connection pool exhaustion"
        self.description = "Prod DB latency spike"
        self.enterprise_id = enterprise_id


class _UnscopedPrincipal:
    """An authenticated principal with no ``enterprise_id`` attribute."""

    user_id = "user-admin"
    username = "admin"


class _CaseWithoutAnEnterprise:
    """The shape the old ``getattr(..., "default")`` fallback existed for."""

    case_id = CASE_ID
    title = "Connection pool exhaustion"
    description = "Prod DB latency spike"


def _suggestion_service_double():
    """Records the enterprise_id it is handed and returns a plausible
    suggestion, so the route reaches its 201 either way."""
    double = MagicMock()

    async def _extract(*, enterprise_id, case_id, extracted_by, **_kwargs):
        return KnowledgeSuggestion(
            suggestion_id="sug_recorded001",
            enterprise_id=enterprise_id,
            case_id=case_id,
            suggested_title="Connection pool exhaustion",
            suggested_content="## Problem\n...",
            extracted_by=extracted_by,
            pii_scan_status=PIIScanStatus.CLEAN,
        )

    double.extract_knowledge_from_case = AsyncMock(side_effect=_extract)
    return double


def _client(user, case=None, suggestion_service=None) -> TestClient:
    app = FastAPI()
    app.include_router(case_router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)

    case_service = MagicMock()
    case_service.get_case = AsyncMock(
        return_value=case if case is not None else _Case()
    )
    app.dependency_overrides[get_case_service] = lambda: case_service
    app.dependency_overrides[get_suggestion_service] = lambda: (
        suggestion_service or _suggestion_service_double()
    )
    app.dependency_overrides[require_authentication] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _stamped_enterprise(suggestion_service) -> str:
    return suggestion_service.extract_knowledge_from_case.await_args.kwargs[
        "enterprise_id"
    ]


class TestTheActorsTenantIsWhatGetsStamped:
    def test_the_suggestion_is_stamped_with_the_actors_enterprise(self):
        """The case here carries a DIFFERENT enterprise_id, so reading it off
        the case and reading it off the actor give different answers — which is
        what makes this test able to fail."""
        service = _suggestion_service_double()
        client = _client(_user(ACTOR_ENT), suggestion_service=service)

        resp = client.post(f"/cases/{CASE_ID}/extract-knowledge", json={})

        assert resp.status_code == 201, resp.text
        assert _stamped_enterprise(service) == ACTOR_ENT
        assert _stamped_enterprise(service) != CASE_ENT

    def test_a_case_object_with_no_enterprise_never_yields_the_default_literal(
        self,
    ):
        """The exact shape the old fallback was written for. ``"default"`` is
        not an enterprise id and the foreign key rejects it; the actor's
        tenant is an answer that exists."""
        service = _suggestion_service_double()
        client = _client(
            _user(ACTOR_ENT),
            case=_CaseWithoutAnEnterprise(),
            suggestion_service=service,
        )

        resp = client.post(f"/cases/{CASE_ID}/extract-knowledge", json={})

        assert resp.status_code == 201, resp.text
        assert _stamped_enterprise(service) == ACTOR_ENT
        assert _stamped_enterprise(service) != "default"

    def test_the_standalone_sentinel_is_stamped_in_a_single_tenant_deployment(self):
        """The common case: one enterprise, and it is a real row that the
        foreign key accepts."""
        service = _suggestion_service_double()
        client = _client(_user(STANDALONE_ENTERPRISE_ID), suggestion_service=service)

        resp = client.post(f"/cases/{CASE_ID}/extract-knowledge", json={})

        assert resp.status_code == 201, resp.text
        assert _stamped_enterprise(service) == STANDALONE_ENTERPRISE_ID


class TestAnUnscopedActorIsRefused:
    """Fail closed. The route previously degraded to a literal; there is
    nowhere to put an extraction for a request that owns no tenant."""

    def test_the_empty_enterprise_answers_403_rather_than_stamping_something(self):
        """``""`` is the value ``api/middleware/tenant_scope`` binds for an
        unauthenticated or invalid-token request. It would pass NOT NULL and,
        on PostgreSQL, even the RLS WITH CHECK (``current_setting`` is ``""``
        too) — and then die on the ``enterprises`` foreign key as an opaque
        IntegrityError several frames later. 403 says the true thing at the
        point it becomes knowable.

        ``None`` is deliberately NOT swept here: ``DevUser.__post_init__``
        replaces it with the Standalone sentinel, so a ``None`` case would
        assert 403 against a user who in fact carries a tenant, and would be a
        test of the dataclass rather than of the route.
        """
        service = _suggestion_service_double()
        client = _client(_user(""), suggestion_service=service)

        resp = client.post(f"/cases/{CASE_ID}/extract-knowledge", json={})

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == UNSCOPED_REQUEST_MSG

    def test_a_user_object_carrying_no_enterprise_attribute_at_all_is_refused(self):
        """``require_actor_enterprise`` reads the attribute defensively, so
        the refusal must hold for a principal shape that simply has no tenant
        field — not only for one whose field is empty."""
        service = _suggestion_service_double()
        client = _client(_UnscopedPrincipal(), suggestion_service=service)

        resp = client.post(f"/cases/{CASE_ID}/extract-knowledge", json={})

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == UNSCOPED_REQUEST_MSG

    def test_nothing_is_extracted_behind_the_403(self):
        """The refusal has to happen BEFORE the generation budget is spent —
        up to four LLM calls on the measured path — not after."""
        service = _suggestion_service_double()
        client = _client(_user(""), suggestion_service=service)

        client.post(f"/cases/{CASE_ID}/extract-knowledge", json={})

        service.extract_knowledge_from_case.assert_not_awaited()


class TestTheSentinelIsNotATenantUnderMulti:
    def test_a_multi_tenant_deployment_refuses_the_standalone_sentinel(self):
        """Under ``TENANT_PROVIDER=multi`` the sentinel identifies the
        single-tenant deployment, not an organization. Stamping it would write
        a suggestion into a pseudo-tenant that owns nothing — and, on
        PostgreSQL, one the session's own RLS policy forbids."""
        from unittest.mock import patch

        from faultmaven.providers.tenancy.factory import BUILTIN_MULTI

        service = _suggestion_service_double()
        client = _client(_user(STANDALONE_ENTERPRISE_ID), suggestion_service=service)

        with patch(
            "faultmaven.providers.tenancy.factory.requested_tenant_provider",
            return_value=BUILTIN_MULTI,
        ):
            resp = client.post(f"/cases/{CASE_ID}/extract-knowledge", json={})

        assert resp.status_code == 403, resp.text
        service.extract_knowledge_from_case.assert_not_awaited()
