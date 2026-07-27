"""R4 (#770): global-tier authoring gate on the DB/disk-derived publish paths.

The ``convert`` / ``runbooks/create`` / upload / suggestion-approve routes gate
global authoring at the API layer (covered by ``test_platform_tier_gate.py``).
This module covers the three points whose scope is only known after a lookup and
were the open R4 holes in single-tenant / cloud-single:

* the shared domain policy (:mod:`faultmaven.modules.knowledge.domain.global_authoring`);
* ``ConversionService.verify_draft`` — publishing a global draft into the KB;
* the ``verify_draft`` / ``verify-batch`` / ``scan`` routes thread ``is_platform_admin``.

``scan``'s per-file skip behaviour is covered end-to-end in
``tests/integration/modules/knowledge/test_scan_for_runbooks.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import AuthorizationError, NotFoundError
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.domain import global_authoring
from faultmaven.modules.knowledge.domain.global_authoring import (
    GLOBAL_AUTHORING_ADMIN_MSG,
    GLOBAL_AUTHORING_MULTI_MSG,
    ensure_global_authoring_allowed,
    is_global_authoring_allowed,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

# ---------------------------------------------------------------------------
# Tenant-provider patch helpers (the domain policy reads its OWN import)
# ---------------------------------------------------------------------------


def _patch_provider(monkeypatch, value):
    monkeypatch.setattr(global_authoring, "requested_tenant_provider", lambda: value)


# ===========================================================================
# Domain policy
# ===========================================================================


class TestGlobalAuthoringPolicy:
    def test_single_tenant_admin_allowed(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        ensure_global_authoring_allowed(is_platform_admin=True)  # no raise
        assert is_global_authoring_allowed(is_platform_admin=True) is True

    def test_single_tenant_non_admin_refused(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        with pytest.raises(AuthorizationError) as exc:
            ensure_global_authoring_allowed(is_platform_admin=False)
        assert str(exc.value) == GLOBAL_AUTHORING_ADMIN_MSG
        assert is_global_authoring_allowed(is_platform_admin=False) is False

    def test_multi_tenant_refused_even_for_admin(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_MULTI)
        with pytest.raises(AuthorizationError) as exc:
            ensure_global_authoring_allowed(is_platform_admin=True)
        assert str(exc.value) == GLOBAL_AUTHORING_MULTI_MSG
        assert is_global_authoring_allowed(is_platform_admin=True) is False


# ===========================================================================
# Service: verify_draft global-publish gate
# ===========================================================================


def _make_session_factory(job=None, draft=None):
    """execute(stmt).scalar_one_or_none() → ``job`` first call, ``draft`` next."""
    calls = {"n": 0}

    async def _execute(_stmt):
        calls["n"] += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = job if calls["n"] == 1 else draft
        return result

    session = AsyncMock()
    session.execute = _execute

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    return _Factory()


def _service(job=None, draft=None) -> ConversionService:
    return ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=_make_session_factory(job=job, draft=draft),
        knowledge_service=None,
    )


def _job(scope: str):
    j = MagicMock()
    j.user_id = "user_x"
    j.scope = scope
    return j


@pytest.mark.unit
@pytest.mark.asyncio
class TestVerifyDraftGlobalGate:
    async def test_global_non_admin_single_tenant_refused(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _service(job=_job("global"), draft=None)
        with pytest.raises(AuthorizationError) as exc:
            await service.verify_draft(
                conversion_id="c",
                draft_id="d",
                user_id="user_x",
                username="alice",
                is_platform_admin=False,
            )
        assert str(exc.value) == GLOBAL_AUTHORING_ADMIN_MSG

    async def test_global_under_multi_refused_even_for_admin(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_MULTI)
        service = _service(job=_job("global"), draft=None)
        with pytest.raises(AuthorizationError) as exc:
            await service.verify_draft(
                conversion_id="c",
                draft_id="d",
                user_id="user_x",
                username="alice",
                is_platform_admin=True,
            )
        assert str(exc.value) == GLOBAL_AUTHORING_MULTI_MSG

    async def test_global_admin_single_tenant_passes_gate(self, monkeypatch):
        # Admin single-tenant clears the gate → flow proceeds to the draft
        # lookup (draft=None here), so we reach NotFoundError(draft), NOT
        # AuthorizationError. Proves the gate did not block a permitted caller.
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _service(job=_job("global"), draft=None)
        with pytest.raises(NotFoundError) as exc:
            await service.verify_draft(
                conversion_id="c",
                draft_id="d",
                user_id="user_x",
                username="alice",
                is_platform_admin=True,
            )
        assert exc.value.resource_type == "draft"

    async def test_non_global_scope_never_gated(self, monkeypatch):
        # A personal draft is not platform-tier authoring — the gate is skipped
        # regardless of is_platform_admin / tenant provider (reach the draft lookup).
        _patch_provider(monkeypatch, BUILTIN_MULTI)
        service = _service(job=_job("personal"), draft=None)
        with pytest.raises(NotFoundError) as exc:
            await service.verify_draft(
                conversion_id="c",
                draft_id="d",
                user_id="user_x",
                username="alice",
                is_platform_admin=False,
            )
        assert exc.value.resource_type == "draft"


@pytest.mark.unit
@pytest.mark.asyncio
class TestVerifyBatchInheritsGate:
    async def test_batch_records_forbidden_without_publishing(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _service(job=_job("global"), draft=None)
        result = await service.verify_batch(
            draft_refs=[("c", "d")],
            user_id="user_x",
            username="alice",
            is_platform_admin=False,
        )
        assert result["forbidden"] == 1
        assert result["verified"] == 0
        assert result["results"][0]["status"] == "forbidden"
        assert result["results"][0]["knowledge_item_id"] is None


# ===========================================================================
# Service: update_draft global-edit gate (#785)
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestUpdateDraftGlobalGate:
    """Editing a global draft pre-verification is platform-corpus authoring:
    same policy as verify_draft, applied once the job's scope is loaded and
    regardless of job ownership (a "system"-owned scan draft included)."""

    def _system_job(self, scope: str):
        j = MagicMock()
        j.user_id = "system"
        j.scope = scope
        return j

    async def test_global_system_job_non_admin_refused(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _service(job=self._system_job("global"), draft=None)
        with pytest.raises(AuthorizationError) as exc:
            await service.update_draft(
                conversion_id="c",
                draft_id="d",
                user_id="user_x",
                content="new content",
                is_platform_admin=False,
            )
        assert str(exc.value) == GLOBAL_AUTHORING_ADMIN_MSG

    async def test_global_under_multi_refused_even_for_admin(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_MULTI)
        service = _service(job=self._system_job("global"), draft=None)
        with pytest.raises(AuthorizationError) as exc:
            await service.update_draft(
                conversion_id="c",
                draft_id="d",
                user_id="user_x",
                content="new content",
                is_platform_admin=True,
            )
        assert str(exc.value) == GLOBAL_AUTHORING_MULTI_MSG

    async def test_global_admin_single_tenant_passes_gate(self, monkeypatch):
        # Admin clears the gate → flow proceeds to the draft lookup
        # (draft=None here) and returns None. Proves the gate did not block
        # a permitted caller.
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _service(job=self._system_job("global"), draft=None)
        result = await service.update_draft(
            conversion_id="c",
            draft_id="d",
            user_id="user_x",
            content="new content",
            is_platform_admin=True,
        )
        assert result is None

    async def test_personal_scope_never_gated(self, monkeypatch):
        _patch_provider(monkeypatch, BUILTIN_MULTI)
        service = _service(job=_job("personal"), draft=None)
        result = await service.update_draft(
            conversion_id="c",
            draft_id="d",
            user_id="user_x",
            content="new content",
            is_platform_admin=False,
        )
        assert result is None

    async def test_default_is_fail_closed(self, monkeypatch):
        # Omitting the flag must never publish-enable: default False refuses.
        _patch_provider(monkeypatch, BUILTIN_SINGLE)
        service = _service(job=self._system_job("global"), draft=None)
        with pytest.raises(AuthorizationError):
            await service.update_draft(
                conversion_id="c",
                draft_id="d",
                user_id="user_x",
                content="new content",
            )


# ===========================================================================
# Route wiring: is_platform_admin derived from the caller's roles
# ===========================================================================


def _user(*, roles) -> DevUser:
    return DevUser(
        user_id="u1",
        username="u1",
        email="u1@example.com",
        display_name="U1",
        created_at=datetime.now(timezone.utc),
        roles=roles,
    )


class TestRouteThreadsIsAdmin:
    """The routes must pass is_platform_admin = ('platform_admin' in roles) to the service."""

    def _client(self, service, user):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from faultmaven.api.v1.auth_dependencies import require_authentication
        from faultmaven.modules.knowledge.api.conversion_routes import (
            _get_conversion_service,
        )
        from faultmaven.modules.knowledge.api.conversion_routes import (
            router as conversion_router,
        )

        app = FastAPI()
        app.include_router(conversion_router)
        app.dependency_overrides[_get_conversion_service] = lambda: service
        app.dependency_overrides[require_authentication] = lambda: user
        return TestClient(app)

    def test_verify_route_passes_admin_true(self):
        service = MagicMock()
        service.verify_draft = AsyncMock(
            return_value=MagicMock(model_dump=lambda: {"ok": True})
        )
        client = self._client(service, _user(roles=["admin", "platform_admin"]))
        resp = client.post("/knowledge/conversions/c1/drafts/d1/verify")
        assert resp.status_code == 200
        assert service.verify_draft.await_args.kwargs["is_platform_admin"] is True

    def test_verify_route_passes_admin_false_for_member(self):
        service = MagicMock()
        service.verify_draft = AsyncMock(
            return_value=MagicMock(model_dump=lambda: {"ok": True})
        )
        client = self._client(service, _user(roles=["user"]))
        resp = client.post("/knowledge/conversions/c1/drafts/d1/verify")
        assert resp.status_code == 200
        assert service.verify_draft.await_args.kwargs["is_platform_admin"] is False

    def test_scan_route_passes_admin_flag(self):
        service = MagicMock()
        service.scan_for_runbooks = AsyncMock(return_value={"discovered": 0})
        client = self._client(service, _user(roles=["user"]))
        resp = client.post("/knowledge/scan")
        assert resp.status_code == 200
        assert service.scan_for_runbooks.await_args.kwargs["is_platform_admin"] is False

    def test_update_draft_route_passes_admin_flag(self):
        service = MagicMock()
        service.update_draft = AsyncMock(
            return_value=MagicMock(model_dump=lambda: {"ok": True})
        )
        client = self._client(service, _user(roles=["admin", "platform_admin"]))
        resp = client.put(
            "/knowledge/conversions/c1/drafts/d1",
            json={"content": "x" * 120},
        )
        assert resp.status_code == 200
        assert service.update_draft.await_args.kwargs["is_platform_admin"] is True

    def test_update_draft_route_passes_admin_false_for_member(self):
        service = MagicMock()
        service.update_draft = AsyncMock(
            return_value=MagicMock(model_dump=lambda: {"ok": True})
        )
        client = self._client(service, _user(roles=["user"]))
        resp = client.put(
            "/knowledge/conversions/c1/drafts/d1",
            json={"content": "x" * 120},
        )
        assert resp.status_code == 200
        assert service.update_draft.await_args.kwargs["is_platform_admin"] is False

    def test_verify_batch_route_passes_admin_flag(self):
        service = MagicMock()
        service.verify_batch = AsyncMock(return_value={"total": 1})
        client = self._client(service, _user(roles=["admin", "platform_admin"]))
        resp = client.post(
            "/knowledge/drafts/verify-batch",
            json={"draft_ids": [{"conversion_id": "c1", "draft_id": "d1"}]},
        )
        assert resp.status_code == 200
        assert service.verify_batch.await_args.kwargs["is_platform_admin"] is True
