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

import fakeredis
import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_authentication,
)
from faultmaven.exceptions import AuthorizationError, CaseNotDriverError, NotFoundError
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    _di_get_session_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router as case_router


def _owner_or_shared_case_service(
    *,
    owner: str,
    shared_to: tuple[str, ...] = (),
    case_id: str = "case-123",
    driver: str | None = None,
    **members,
):
    """A case service whose ``get_case`` answers ``CaseService``'s resolver.

    One resolver, shared by every module that asserts a case gate: it encodes
    ADR-013 §D4 and ADR-020 D2, and two hand-written copies of it drift
    independently — a change to the rule has to land in both, and the module
    whose copy was missed keeps passing against the old one.

    - read: ``owner`` (the creator) ∪ ``shared_to``;
    - ``driver_only``: the effective driver (``driver``, default the creator),
      who must also read the case;
    - ``creator_only``: the creator.

    It HONOURS both flags rather than omitting them, so a handler narrowed to
    a write arm asserts "the reader was refused" instead of surfacing as an
    incidental TypeError -> 500.

    ``members`` are attached as-is, for whatever else the route under test
    calls.
    """
    service = SimpleNamespace(**members)
    effective_driver = driver or owner

    async def require_case(
        requested_id, user_id=None, *, driver_only=False, creator_only=False
    ):
        # ``CaseService.require_case``'s verdicts (#2806): unreadable is the
        # case's NotFoundError, a reader refused a driver write is
        # CaseNotDriverError, a reader refused a creator write is 403.
        if requested_id != case_id:
            raise NotFoundError("Case", requested_id)
        if user_id:
            if not (user_id == owner or user_id in shared_to):
                raise NotFoundError("Case", requested_id)
            if driver_only and user_id != effective_driver:
                raise CaseNotDriverError()
            if creator_only and user_id != owner:
                raise AuthorizationError("Only the case's creator may do this")
        return SimpleNamespace(
            case_id=case_id,
            user_id=owner,
            driver_id=driver,
            effective_driver_id=effective_driver,
        )

    async def get_case(
        requested_id, user_id=None, *, driver_only=False, creator_only=False
    ):
        try:
            return await require_case(
                requested_id,
                user_id,
                driver_only=driver_only,
                creator_only=creator_only,
            )
        except (NotFoundError, AuthorizationError):
            return None

    service.get_case = get_case
    service.require_case = require_case
    return service


@pytest.fixture
def owner_or_shared_case_service():
    """The shared resolver, as a fixture (this directory is not a package)."""
    return _owner_or_shared_case_service


@pytest.fixture
def build_app():
    """Mount the real case router with only the dependencies these routes use.

    ``session`` is what the session service resolves any id to (``None`` models
    an invalid/expired session); ``case`` is what the case service resolves any
    id to (``None`` models an unknown or inaccessible case). ``case_service``
    and ``session_service`` replace the default fakes outright, for a module
    asserting on a failure inside the service.
    """

    def _build(*, session=None, case=None, case_service=None, session_service=None):
        app = FastAPI()
        # The app's own handlers, so a DOMAIN exception reaching a route here
        # is mapped the way production maps it. Without them a `NotFoundError`
        # escaped the test client as a raw exception, and a route that relies
        # on the mapping for its 404 (the session resume, #1398) could not be
        # asserted against the status a client would actually see.
        for exc_type, handler in get_exception_handlers().items():
            app.add_exception_handler(exc_type, handler)
        app.include_router(case_router, prefix="/api/v1")

        async def _session_service():
            if session_service is not None:
                return session_service

            async def get_session(session_id, validate=True):
                # The real service RAISES when it cannot answer (e.g.
                # `ServiceException("Session store not configured")`) rather
                # than returning None, so an Exception passed as `session`
                # models that arm — a caller that only ever returns None
                # cannot exercise the route's unevaluable-gate path.
                if isinstance(session, Exception):
                    raise session
                return session

            return SimpleNamespace(get_session=get_session)

        async def _case_service():
            if case_service is not None:
                return case_service

            # Signatures mirror CaseService exactly (see module docstring).
            async def get_case(
                case_id, user_id=None, *, driver_only=False, creator_only=False
            ):
                # The write flags are on the real signature and routes under
                # this scaffolding pass them. Omitting it here raised TypeError
                # inside the handler, which its bare `except` turned into a
                # 500 — so a gate test would assert "not 200" and pass for the
                # wrong reason, which is the failure this fake exists to avoid.
                return case

            async def require_case(
                case_id, user_id=None, *, driver_only=False, creator_only=False
            ):
                # ``case`` None models an unknown or unreadable case: the
                # case's NotFoundError, as the real one raises (#2806).
                if case is None:
                    raise NotFoundError("Case", case_id)
                return case

            return SimpleNamespace(get_case=get_case, require_case=require_case)

        async def _current_user_optional():
            return None

        async def _current_user():
            return SimpleNamespace(user_id="user-1")

        app.dependency_overrides[_di_get_session_service_dependency] = _session_service
        app.dependency_overrides[_di_get_case_service_dependency] = _case_service
        app.dependency_overrides[get_current_user_optional] = _current_user_optional
        app.dependency_overrides[require_authentication] = _current_user
        return app

    return _build


@pytest.fixture
async def unreachable_session_service():
    """The real session service over a real ``RedisSessionStore`` whose FakeRedis
    server emulates a lost connection: what a configured, unreachable store
    raises, end to end (#2985)."""
    server = fakeredis.FakeServer()
    server.connected = False
    redis = fakeredis_aio.FakeRedis(server=server, decode_responses=True)
    yield AuthSessionService(
        session_store=RedisSessionStore(redis, session_ttl_seconds=60)
    )
    await redis.aclose()


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
