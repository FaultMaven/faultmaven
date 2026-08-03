"""Shared scaffolding for the case-router regression modules.

Requests are driven through the real router over ``httpx.ASGITransport`` on a
single event loop. ``fastapi.testclient.TestClient`` is deliberately not used:
it creates a fresh event loop per request, and async fakeredis-backed
infrastructure elsewhere in this suite then raises "bound to a different event
loop" — a failure mode that surfaces as a confusing unrelated error rather than
as the behaviour under test.

Service fakes here mirror the **real** method signatures rather than accepting
``**kwargs``. A stub that accepts arguments the production class rejects lets a
handler pass a test while failing in production against the real object, so the
fakes are kept signature-faithful on purpose.
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_authentication,
)
from faultmaven.api.v1.dependencies import get_case_service
from faultmaven.modules.case.api.routes import (
    _di_get_case_service_dependency,
    _di_get_session_service_dependency,
)
from faultmaven.modules.case.api.routes import router as case_router


@pytest.fixture
def build_app():
    """Mount the real case router with only the dependencies these routes use.

    ``session`` is what the session service resolves any id to (``None`` models
    an invalid/expired session); ``case`` is what the case service resolves any
    id to (``None`` models an unknown or inaccessible case). ``case_id`` is the
    id returned by ``get_or_create_case_for_session``.
    """

    def _build(*, session=None, case=None, case_id="case-123", case_service=None):
        app = FastAPI()
        app.include_router(case_router, prefix="/api/v1")

        async def _session_service():
            async def get_session(session_id, validate=True):
                return session

            return SimpleNamespace(get_session=get_session)

        async def _case_service():
            if case_service is not None:
                return case_service

            # Signatures mirror CaseService exactly (see module docstring).
            async def get_or_create_case_for_session(
                session_id, user_id=None, force_new=False, title=None
            ):
                return case_id

            async def get_case(case_id, user_id=None):
                return case

            return SimpleNamespace(
                get_or_create_case_for_session=get_or_create_case_for_session,
                get_case=get_case,
            )

        async def _current_user_optional():
            return None

        async def _current_user():
            return SimpleNamespace(user_id="user-1")

        app.dependency_overrides[_di_get_session_service_dependency] = _session_service
        # Routes in this file reach the case service two ways: most via the
        # module's runtime wrapper, list_uploaded_files via the shared
        # dependency directly. Override both so either route shape is covered.
        app.dependency_overrides[_di_get_case_service_dependency] = _case_service
        app.dependency_overrides[get_case_service] = _case_service
        app.dependency_overrides[get_current_user_optional] = _current_user_optional
        app.dependency_overrides[require_authentication] = _current_user
        return app

    return _build


@pytest.fixture
def call_api():
    """Issue one request against an ASGI app and return the response."""

    async def _call(app, method, path, **kwargs):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)

    return _call
