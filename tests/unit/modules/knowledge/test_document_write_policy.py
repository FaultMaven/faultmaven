"""#834: ownership-aware write policy for published KB documents.

``PUT``/``DELETE /knowledge/documents/{id}`` were unconditionally
operator-only, so the owner of a personal runbook could not edit or delete it.
The policy (:mod:`faultmaven.modules.knowledge.domain.document_write`) now
decides per scope: global → the global-tier authoring policy; personal/team →
the owner, or the single-tenant platform operator (no operator override under
multi — that is the break-glass path, never standing access).

Covers the full decision space of the policy (property sweep over
scope × ownership × role × tenant provider) and the route wiring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import AuthorizationError
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.domain import global_authoring
from faultmaven.modules.knowledge.domain.document_write import (
    ensure_document_write_allowed,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE


def _patch_provider(monkeypatch, value):
    monkeypatch.setattr(global_authoring, "requested_tenant_provider", lambda: value)


# ===========================================================================
# Domain policy — full decision-space sweep
# ===========================================================================

ALLOWED = "allowed"
REFUSED = "refused"

# (scope, owner_matches, is_platform_admin, tenant_provider) → verdict
POLICY_CASES = [
    # global scope delegates to the global-tier policy: single-tenant admin
    # only, never under multi. Ownership is irrelevant for global rows.
    ("global", False, True, BUILTIN_SINGLE, ALLOWED),
    ("global", False, False, BUILTIN_SINGLE, REFUSED),
    ("global", True, False, BUILTIN_SINGLE, REFUSED),
    ("global", False, True, BUILTIN_MULTI, REFUSED),
    ("global", True, True, BUILTIN_MULTI, REFUSED),
    # personal: the owner always may; a non-owner only as the single-tenant
    # platform operator; under multi there is NO operator override.
    ("personal", True, False, BUILTIN_SINGLE, ALLOWED),
    ("personal", True, False, BUILTIN_MULTI, ALLOWED),
    ("personal", False, False, BUILTIN_SINGLE, REFUSED),
    ("personal", False, True, BUILTIN_SINGLE, ALLOWED),
    ("personal", False, True, BUILTIN_MULTI, REFUSED),
    # team mirrors personal: shares grant read visibility, not write.
    ("team", True, False, BUILTIN_SINGLE, ALLOWED),
    ("team", True, False, BUILTIN_MULTI, ALLOWED),
    ("team", False, False, BUILTIN_SINGLE, REFUSED),
    ("team", False, True, BUILTIN_SINGLE, ALLOWED),
    ("team", False, True, BUILTIN_MULTI, REFUSED),
]


@pytest.mark.unit
class TestDocumentWritePolicy:
    @pytest.mark.parametrize(
        "scope,owner_matches,is_admin,provider,verdict", POLICY_CASES
    )
    def test_decision_space(
        self, monkeypatch, scope, owner_matches, is_admin, provider, verdict
    ):
        _patch_provider(monkeypatch, provider)
        kwargs = dict(
            scope=scope,
            owner_id="u1" if owner_matches else "someone_else",
            actor_user_id="u1",
            is_platform_admin=is_admin,
        )
        if verdict == ALLOWED:
            ensure_document_write_allowed(**kwargs)  # no raise
        else:
            with pytest.raises(AuthorizationError):
                ensure_document_write_allowed(**kwargs)

    @pytest.mark.parametrize("provider", [BUILTIN_SINGLE, BUILTIN_MULTI])
    def test_ownerless_non_global_document_is_operator_only(
        self, monkeypatch, provider
    ):
        # owner_id NULL (owner account deleted): nobody matches the owner arm,
        # so only the single-tenant operator may write.
        _patch_provider(monkeypatch, provider)
        kwargs = dict(
            scope="personal",
            owner_id=None,
            actor_user_id="u1",
            is_platform_admin=False,
        )
        with pytest.raises(AuthorizationError):
            ensure_document_write_allowed(**kwargs)


# ===========================================================================
# Route wiring: PUT / DELETE /knowledge/documents/{id}
# ===========================================================================


def _user(*, user_id="u1", roles) -> DevUser:
    return DevUser(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        created_at=datetime.now(timezone.utc),
        roles=roles,
    )


def _doc(*, scope, owner_id):
    return {
        "document_id": "doc1",
        "title": "T",
        "content": "C",
        "document_type": "runbook",
        "tags": [],
        "scope": scope,
        "owner_id": owner_id,
        "source_url": None,
        "created_at": "",
        "updated_at": "",
        "metadata": {},
    }


def _client(knowledge_service, user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from faultmaven.api.exception_handlers import get_exception_handlers
    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.knowledge.api.routes import (
        get_knowledge_service,
        router,
    )

    app = FastAPI()
    app.include_router(router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[get_knowledge_service] = lambda: knowledge_service
    app.dependency_overrides[require_authentication] = lambda: user
    return TestClient(app)


def _knowledge_service(doc):
    service = MagicMock()
    service.get_document = AsyncMock(return_value=doc)
    service.update_document_metadata = AsyncMock(return_value={"document_id": "doc1"})
    service.delete_document = AsyncMock(return_value={"success": True})
    return service


@pytest.mark.unit
class TestDocumentWriteRoutes:
    def test_owner_updates_own_personal_document(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _knowledge_service(_doc(scope="personal", owner_id="u1"))
        client = _client(service, _user(user_id="u1", roles=["user"]))
        resp = client.put("/knowledge/documents/doc1", json={"title": "New"})
        assert resp.status_code == 200
        service.update_document_metadata.assert_awaited_once()

    def test_owner_deletes_own_personal_document(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _knowledge_service(_doc(scope="personal", owner_id="u1"))
        client = _client(service, _user(user_id="u1", roles=["user"]))
        resp = client.delete("/knowledge/documents/doc1")
        assert resp.status_code == 204
        service.delete_document.assert_awaited_once()

    def test_non_owner_cannot_write_personal_document(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _knowledge_service(_doc(scope="personal", owner_id="someone_else"))
        client = _client(service, _user(user_id="u1", roles=["user"]))
        assert (
            client.put("/knowledge/documents/doc1", json={"title": "X"}).status_code
            == 403
        )
        assert client.delete("/knowledge/documents/doc1").status_code == 403
        service.update_document_metadata.assert_not_awaited()
        service.delete_document.assert_not_awaited()

    def test_platform_admin_writes_any_document_single_tenant(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _knowledge_service(_doc(scope="global", owner_id=None))
        client = _client(
            service, _user(user_id="op", roles=["admin", "platform_admin"])
        )
        assert (
            client.put("/knowledge/documents/doc1", json={"title": "X"}).status_code
            == 200
        )
        assert client.delete("/knowledge/documents/doc1").status_code == 204

    def test_non_admin_cannot_write_global_document(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _knowledge_service(_doc(scope="global", owner_id="u1"))
        client = _client(service, _user(user_id="u1", roles=["user"]))
        assert (
            client.put("/knowledge/documents/doc1", json={"title": "X"}).status_code
            == 403
        )
        assert client.delete("/knowledge/documents/doc1").status_code == 403

    def test_global_write_refused_under_multi_even_for_admin(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_MULTI)
        service = _knowledge_service(_doc(scope="global", owner_id=None))
        client = _client(
            service, _user(user_id="op", roles=["admin", "platform_admin"])
        )
        assert (
            client.put("/knowledge/documents/doc1", json={"title": "X"}).status_code
            == 403
        )
        assert client.delete("/knowledge/documents/doc1").status_code == 403

    def test_missing_document_404s_before_authorization(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _knowledge_service(None)
        client = _client(service, _user(user_id="u1", roles=["user"]))
        assert (
            client.put("/knowledge/documents/doc1", json={"title": "X"}).status_code
            == 404
        )
        assert client.delete("/knowledge/documents/doc1").status_code == 404
