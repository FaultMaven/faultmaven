"""A server fault while verifying a bearer answers 500, never a verdict (#2165).

Each bearer verifier ended in ``except Exception``, which answered an exception
it did not recognise as a verdict on the credential: ``api/v1``'s
``get_current_user_optional`` as "no user" (so ``POST /api/v1/sessions`` minted
an anonymous session, and every ``require_authentication`` route answered 401),
``api/middleware/auth.get_current_user`` (behind ``/admin/*``) as 401. A 401 is
what signs a client out. The exception now reaches the house 500
(``internal_server_error_handler``).

What must not change is pinned beside it: a missing, malformed, expired or
revoked bearer keeps its answer on each path. ``RevocationStateUnknownError``
keeps its 503 on each verifier, pinned in
``tests/unit/modules/auth/test_revocation_fails_closed.py``. The refresh paths
are pinned in ``tests/integration/modules/auth/test_oauth_client_binding.py``.

Driven through the real application, rebuilt (``rebuild_app_under``) and
served without its lifespan, single-tenant unless a test says otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from faultmaven.api.middleware import auth as middleware_auth
from faultmaven.api.v1 import auth_dependencies
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI
from tests.integration._app_rebuild import rebuild_app_under
from tests.utils import (
    InMemoryRevocationStore,
    forge_access_token,
    forge_refresh_token,
    in_memory_session_service,
    request_with_authorization,
    sign_claims_for,
)

pytestmark = [pytest.mark.integration, pytest.mark.security]

MARKER = "verifier-fault-2165"
OPTIONAL = ("POST", "/api/v1/sessions")  # get_current_user_optional (v1)
MANDATORY = ("GET", "/api/v1/sessions")  # require_authentication (v1)
ADMIN = ("GET", "/api/v1/admin/config/status")  # require_platform_admin
ROUTES = {"v1-optional": OPTIONAL, "v1-mandatory": MANDATORY, "admin": ADMIN}


@pytest.fixture(scope="module")
def app():
    return rebuild_app_under({"SKIP_SERVICE_CHECKS": "true"})


@pytest.fixture
def served(app, monkeypatch):
    store = InMemoryRevocationStore()
    service = AuthService(revocation_store=store)
    sessions = in_memory_session_service()

    async def _sessions():
        return sessions

    monkeypatch.setattr(app.state, "auth_service", service, raising=False)
    app.dependency_overrides[get_session_service] = _sessions
    try:
        yield SimpleNamespace(
            client=TestClient(app, raise_server_exceptions=False),
            service=service,
            minted=sessions.session_store.sessions,
        )
    finally:
        app.dependency_overrides.clear()


def _token(service, **overrides):
    return forge_access_token(
        service,
        user_id="user-2165",
        enterprise_id="00000000-0000-0000-0000-000000000002",
        email="user-2165@example.com",
        roles=["user", "platform_admin"],
        **overrides,
    )


def _send(served, route, authorization=None):
    method, path = route
    headers = {} if authorization is None else {"Authorization": authorization}
    return served.client.request(
        method, path, headers=headers, json={} if method == "POST" else None
    )


@contextmanager
def _verifier_fault():
    """Both verification entry points raise an exception nobody maps."""
    with (
        patch.object(
            AuthService,
            "verify_token_with_revocation_check",
            AsyncMock(side_effect=RuntimeError(MARKER)),
        ),
        patch.object(
            AuthService,
            "extract_user_from_token_with_revocation_check",
            AsyncMock(side_effect=RuntimeError(MARKER)),
        ),
    ):
        yield


def _assert_house_500(response):
    assert response.status_code == 500, response.text
    assert response.json()["detail"] == "Internal server error"
    assert "www-authenticate" not in response.headers
    assert MARKER not in response.text


@pytest.mark.parametrize("route", ROUTES.values(), ids=ROUTES.keys())
def test_a_fault_while_verifying_a_bearer_answers_500(served, route):
    """On ``main``: an anonymous 201, a 401, a 401."""
    with _verifier_fault():
        response = _send(served, route, f"Bearer {_token(served.service)}")

    _assert_house_500(response)
    assert served.minted == {}


def test_a_fault_after_verification_answers_500(served, monkeypatch):
    """Raised inside the v1 verifier's ``try`` after the token verified."""

    def _unbound():
        raise RuntimeError(MARKER)

    monkeypatch.setattr(auth_dependencies, "get_current_enterprise_id", _unbound)

    response = _send(served, MANDATORY, f"Bearer {_token(served.service)}")

    _assert_house_500(response)


def test_under_multi_tenant_the_binder_answers_the_fault_first(served, monkeypatch):
    """The global binder verifies the bearer first and never caught it."""
    monkeypatch.setattr(
        "faultmaven.providers.tenancy.factory.requested_tenant_provider",
        lambda: BUILTIN_MULTI,
    )
    with _verifier_fault():
        response = _send(served, MANDATORY, f"Bearer {_token(served.service)}")

    _assert_house_500(response)


async def test_the_middleware_optional_verifier_propagates_the_fault():
    """It has no route; called directly."""
    service = AsyncMock()
    service.extract_user_from_token_with_revocation_check.side_effect = RuntimeError(
        MARKER
    )

    with pytest.raises(RuntimeError, match=MARKER):
        await middleware_auth.get_current_user_optional(
            request_with_authorization("Bearer a.b.c"),
            credentials=None,
            auth_service=service,
        )


def _judged(served, kind):
    """A bearer each verifier judges, by kind; ``None`` sends no header."""
    service = served.service
    if kind == "absent":
        return None
    if kind == "malformed":
        return "Bearer not-a-jwt"
    if kind == "expired":
        return f"Bearer {_token(service, expires_in_minutes=-5)}"
    if kind == "wrong-type":
        token = forge_refresh_token(service, user_id="u", enterprise_id="e")
        return f"Bearer {token}"
    token = _token(service)
    claims = jwt.decode(token, options={"verify_signature": False})
    if kind == "another-key":
        key = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if service._algorithm == "RS256"
            else "another-deployment-secret-0123456789abcdef"
        )
        return f"Bearer {jwt.encode(claims, key, algorithm=service._algorithm)}"
    if kind == "no-sub":
        del claims["sub"]
        return f"Bearer {sign_claims_for(service, claims)}"
    asyncio.run(service.revoke_token(claims["jti"], claims["exp"]))
    return f"Bearer {token}"


JUDGED = [
    "absent",
    "malformed",
    "expired",
    "revoked",
    "wrong-type",
    "another-key",
    "no-sub",
]


@pytest.mark.parametrize("kind", JUDGED)
@pytest.mark.parametrize("route", ROUTES.values(), ids=ROUTES.keys())
def test_a_judged_bearer_keeps_its_verdict(served, route, kind):
    response = _send(served, route, _judged(served, kind))

    if route is OPTIONAL:
        assert response.status_code == 201, response.text
        assert response.json()["user_id"].startswith("user_")
        assert len(served.minted) == 1
    else:
        assert response.status_code == 401, response.text
        assert "Bearer" in response.headers["www-authenticate"]


def _segment(part):
    raw = part if isinstance(part, bytes) else json.dumps(part).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


_CLAIMS = {"sub": "u", "exp": 4102444800, "iat": 0, "jti": "j", "type": "access"}

#: Unsigned shapes from the #2165 design critique's fuzz of ``jwt.decode`` on
#: the pinned PyJWT: every one raised an ``InvalidTokenError`` subclass.
HOSTILE = {
    "one-part": "x",
    "empty-parts": "...",
    "five-parts": "a.b.c.d.e",
    "header-not-json": f"{_segment(b'notjson')}.{_segment(_CLAIMS)}.AAAA",
    "header-deep": f"{_segment(b'[' * 2000)}.{_segment(_CLAIMS)}.AAAA",
    "payload-not-json": f"{_segment({'alg': 'HS256'})}.{_segment(b'zz')}.AAAA",
    "alg-none": f"{_segment({'alg': 'none'})}.{_segment(_CLAIMS)}.",
    "alg-int": f"{_segment({'alg': 5})}.{_segment(_CLAIMS)}.AAAA",
    "alg-list": f"{_segment({'alg': ['HS256']})}.{_segment(_CLAIMS)}.AAAA",
    "crit": f"{_segment({'alg': 'HS256', 'crit': ['x']})}.{_segment(_CLAIMS)}.AAAA",
    "b64-false": (
        f"{_segment({'alg': 'HS256', 'b64': False, 'crit': ['b64']})}"
        f".{_segment(_CLAIMS)}.AAAA"
    ),
    "jwk-header": (
        f"{_segment({'alg': 'HS256', 'jwk': {'kty': 'oct', 'k': 'AA'}})}"
        f".{_segment(_CLAIMS)}.AAAA"
    ),
    "signature-not-b64": f"{_segment({'alg': 'HS256'})}.{_segment(_CLAIMS)}.!!!!",
}


@pytest.mark.parametrize("token", HOSTILE.values(), ids=HOSTILE.keys())
def test_a_hostile_unsigned_bearer_still_answers_401(served, token):
    """Through the real ``AuthService``: an untyped PyJWT failure is a 500 now."""
    response = _send(served, MANDATORY, f"Bearer {token}")

    assert response.status_code == 401, response.text
