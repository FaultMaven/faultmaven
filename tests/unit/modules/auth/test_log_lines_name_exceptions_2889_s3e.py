"""The auth area's log lines name an exception by its class and code, never its
text (#2889 S3e).

Principles §7, "What a log line may hold", rule 1: "A message that interpolates
an exception's text, a payload, or a user-supplied value is not metadata. A
first-party exception contributes its class and its ``error_code`` ..., not its
message." Stage S3 of #2886.

Each case drives one site with :data:`MARKER` in the exception's message,
captures every record at DEBUG, and renders each through the real chain's
formatter (``build_log_formatter``). The marker is on no rendered line, and the
class name (with its code, where first-party) is on the site's line. How each
site is driven is stated on its case. What the caller is answered (a return
value, a raised error, a status and body) is asserted beside it, unchanged.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import fakeredis.aioredis as fakeredis_aio
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import oauth_protocol_error_handler
from faultmaven.exceptions import UserLookupFailed
from faultmaven.infrastructure.auth.user_store import RedisUserStore
from faultmaven.infrastructure.persistence.user_repository import InMemoryUserRepository
from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
from faultmaven.models.exceptions import InvalidGrantError, OAuthProtocolError
from faultmaven.modules.auth.api.auth import _resolve_organization_summary
from faultmaven.modules.auth.api.oauth import get_oauth_service, router
from faultmaven.modules.auth.api.rate_limiting import require_oauth_rate_limit_token
from faultmaven.modules.auth.domain.services import auth_service as auth_service_module
from faultmaven.modules.auth.domain.services import jwt_token_generator
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    RevocationStateUnknownError,
)
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
)
from faultmaven.modules.auth.domain.services.user_service import UserService
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)
from tests.unit.modules.auth.test_revocation_fails_closed import (
    _auth_service,
    _RaisingStore,
    _token,
)
from tests.utils import InMemoryRevocationStore

pytestmark = [pytest.mark.unit, pytest.mark.security]


# ── modules/auth/domain/services/jwt_token_generator.py ───────────────────


async def test_an_invalid_access_token_is_named_by_class(lines):
    """``HS256JWTTokenGenerator.validate_access_token``, driven with
    ``jwt.decode`` patched to raise. The ``error`` extra holds the class, not
    the text, and the token is still refused."""
    generator = HS256JWTTokenGenerator(
        secret_key="s3e-unit-test-secret-key-please-ignore",
        revocation_store=InMemoryRevocationStore(),
        access_token_expire_minutes=60,
        refresh_token_expire_days=7,
        issuer="faultmaven",
        audience="faultmaven-api",
    )
    with patch.object(
        jwt_token_generator.jwt,
        "decode",
        side_effect=jwt.InvalidSignatureError(MARKER),
    ):
        assert await generator.validate_access_token("a.b.c") is None
    refused = _named(lines(), "JWT validation failed: invalid access token")
    assert refused["error"] == "InvalidSignatureError"


# ── modules/auth/api/oauth.py ─────────────────────────────────────────────


def test_an_invalid_grant_is_named_by_class_and_code(lines):
    """Driven through ``POST /auth/oauth/token`` on a scratch app, the OAuth
    service stubbed to refuse the code exchange. The client's RFC 6749 §5.2
    body is unchanged: it still carries ``str(e)``, the code and the grant
    error's curated message."""
    message = f"PKCE verification failed {MARKER}"
    service = MagicMock(
        exchange_code_for_token=AsyncMock(
            side_effect=InvalidGrantError(
                message, error_code="PKCE_VERIFICATION_FAILED"
            )
        )
    )
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(OAuthProtocolError, oauth_protocol_error_handler)
    app.dependency_overrides[get_oauth_service] = lambda: service
    app.dependency_overrides[require_oauth_rate_limit_token] = lambda: None

    response = TestClient(app).post(  # a scratch app: no lifespan to run
        "/auth/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "redirect_uri": "https://client.example/cb",
            "code_verifier": "v" * 43,
            "client_id": "client-1",
        },
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_grant",
        "error_description": f"[PKCE_VERIFICATION_FAILED] {message}",
    }
    _named(lines(), "Invalid grant", "InvalidGrantError[PKCE_VERIFICATION_FAILED]")


# ── modules/auth/domain/services/auth_service.py ──────────────────────────


async def test_an_unknown_revocation_state_names_the_cause(lines):
    """Driven through ``verify_token_with_revocation_check``, the revocation
    store raising. The raised error's ``kind`` and the counter's label are the
    cause's class, as before."""
    service = _auth_service(_RaisingStore(ConnectionError(f"store down {MARKER}")))
    token = _token(service)
    with patch.object(auth_service_module, "revocation_state_unknown_total") as counter:
        with pytest.raises(RevocationStateUnknownError) as raised:
            await service.verify_token_with_revocation_check(token)
    assert raised.value.kind == "ConnectionError"
    counter.labels.assert_called_once_with(kind="ConnectionError")
    _named(
        lines(),
        "Revocation state unknown, refusing the request:",
        "Revocation state unknown, refusing the request: ConnectionError",
    )


# ── infrastructure/auth/user_store.py ─────────────────────────────────────


async def test_a_failed_redis_get_is_named_by_class(lines):
    """Driven through ``RedisUserStore.get_user``, its Redis client's ``get``
    raising. The lookup still fails as ``UserLookupFailed``."""
    redis = MagicMock(get=AsyncMock(side_effect=ConnectionError(MARKER)))
    with pytest.raises(UserLookupFailed):
        await RedisUserStore(redis).get_user("u-1")
    _named(lines(), "Redis GET failed for key", "ConnectionError")


# ── modules/auth/infrastructure/stores/redis_session_store.py ─────────────


async def test_an_unreadable_session_is_named_by_class(lines):
    """Driven through ``RedisSessionStore.list_sessions`` on FakeRedis, one
    indexed record's ``created_at`` holding the marker, so the parse raises a
    ``ValueError`` that quotes it. The record is skipped, as before."""
    redis = fakeredis_aio.FakeRedis(
        server=fakeredis.FakeServer(), decode_responses=True
    )
    try:
        store = RedisSessionStore(redis, session_ttl_seconds=1800)
        record = {
            "session_id": "bad-1",
            "user_id": "user-a",
            "created_at": MARKER,
            "last_activity": MARKER,
        }
        await redis.set("session:bad-1", json.dumps(record), ex=60)
        await redis.sadd("sessions:user:user-a", "bad-1")
        assert await store.list_sessions("user-a") == []
    finally:
        await redis.aclose()
    _named(lines(), "Skipping unreadable session of user user-a", "ValueError")


# ── modules/auth/domain/services/user_service.py ──────────────────────────


def _user_service(**overrides) -> UserService:
    with patch(
        "faultmaven.modules.auth.domain.services.user_service.get_settings",
        return_value=MagicMock(),
    ):
        return UserService(
            user_repo=InMemoryUserRepository(),
            auth_service=MagicMock(),
            **overrides,
        )


async def test_a_failed_role_change_audit_is_named_by_class(lines):
    """``_audit_role_change``, called with an audit sink whose write raises.
    The role and both ids stay on the line, and the change is not failed."""
    sink = MagicMock(log_event=AsyncMock(side_effect=RuntimeError(MARKER)))
    service = _user_service(audit_log=sink)
    await service._audit_role_change(
        user_id="u-7",
        enterprise_id="ent-1",
        admin_user_id="admin-3",
        event_type="role_assigned",
        role="org_admin",
        resulting_roles=["user", "org_admin"],
    )
    _named(
        lines(),
        "role_change_audit_failed",
        "the 'org_admin' change to user u-7 by admin-3 IS in force but was not "
        "recorded: RuntimeError",
    )


async def test_a_refused_reset_is_named_by_class(lines):
    """Driven through ``reset_password``, the signer's verification raising.
    The ``Password reset refused`` line's ``error`` field holds the class, and
    the caller still gets the one uniform refusal."""
    signer = MagicMock(
        verify_password_reset_token=AsyncMock(side_effect=jwt.InvalidTokenError(MARKER))
    )
    service = _user_service(token_generator=signer)
    with pytest.raises(AuthenticationError):
        await service.reset_password("a.b.c", "N3w-P@ssw0rd-long")
    refused = _named(lines(), "Password reset refused")
    assert refused["error"] == "InvalidTokenError"
    assert refused["refusal_reason"] == "token_unverifiable"


# ── infrastructure/security/case_redaction.py ─────────────────────────────


async def test_a_failed_registry_load_is_named_by_class(lines):
    """Driven through ``CaseRedactionContext.load``, its Redis client's
    ``hgetall`` raising. The context still loads, in memory only."""
    redis = MagicMock(hgetall=AsyncMock(side_effect=ConnectionError(MARKER)))
    context = CaseRedactionContext("case-1", MagicMock(), redis_client=redis)
    await context.load()
    assert context._loaded
    _named(
        lines(),
        "Failed to load redaction registry",
        "case-1: ConnectionError. Using in-memory only.",
    )


# ── modules/auth/api/auth.py ──────────────────────────────────────────────


class _User:
    organization_id = "9f2b1c7e-1f3a-4c5d-8e91-0a2b3c4d5e6f"


async def test_a_failed_profile_organization_lookup_is_named_by_class(lines):
    """``_resolve_organization_summary`` (``GET /auth/me``'s organization
    field), called with a repository whose read raises. The field degrades to
    absent and the ``error`` extra holds the class."""
    repo = MagicMock(get_organization=AsyncMock(side_effect=RuntimeError(MARKER)))
    with patch(
        "faultmaven.modules.auth.api.auth.usable_tenant_id",
        return_value=_User.organization_id,
    ):
        assert await _resolve_organization_summary(_User(), repo) is None
    failed = _named(lines(), "Could not resolve organization for profile")
    assert failed["error"] == "RuntimeError"
