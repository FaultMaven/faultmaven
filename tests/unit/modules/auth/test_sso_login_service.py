"""Unit tests for the SSO hosted-login orchestration service (ADR-015, PR 2b).

The service is the security surface of the cloud login flow, so these tests
pin its guarantees: state is single-use CSRF, ``return_to`` is same-origin
path-only, every callback failure maps to a sanitized error slug (never IdP
text), the completion code is single-use, and exchange failures are uniform
(None) regardless of cause. The ephemeral store is real (FakeRedis-backed) so
single-use semantics are exercised end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import fakeredis.aioredis as fakeredis
import pytest

from faultmaven.modules.auth.contracts import ISSOIdentityProvider, SSOIdentity
from faultmaven.modules.auth.domain.services.sso_login_service import (
    ERROR_ACCESS_DENIED,
    ERROR_EXCHANGE_FAILED,
    ERROR_FAILED,
    ERROR_STATE_INVALID,
    ERROR_USER_INACTIVE,
    ERROR_USER_UNKNOWN,
    SSOLoginService,
    sanitize_return_to,
)
from faultmaven.modules.auth.exceptions import SSOAuthenticationError
from faultmaven.modules.auth.infrastructure.stores.sso_ephemeral_store import (
    SSOEphemeralStore,
)

DASHBOARD_URL = "https://app.faultmaven.test"

IDENTITY = SSOIdentity(
    provider="workos",
    provider_user_id="user_wos_123",
    email="alex@example.com",
    email_verified=True,
    display_name="Alex Example",
)


def make_user(user_id="u-1", is_active=True):
    return SimpleNamespace(
        user_id=user_id,
        username="alex",
        email="alex@example.com",
        display_name="Alex Example",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_active=is_active,
        roles=["user"],
    )


class FakeProvider(ISSOIdentityProvider):
    """Programmable ISSOIdentityProvider: returns an identity or raises."""

    def __init__(self, identity=IDENTITY, fail=False):
        self.identity = identity
        self.fail = fail
        self.exchanged_codes: list[str] = []

    @property
    def provider_name(self) -> str:
        return "workos"

    def build_authorization_url(self, *, state: str) -> str:
        return f"https://authkit.test/authorize?state={state}"

    def exchange_code(self, code: str) -> SSOIdentity:
        self.exchanged_codes.append(code)
        if self.fail:
            # Mimics the adapter: vendor detail stays out of the message.
            raise SSOAuthenticationError("SSO code exchange failed")
        return self.identity


class FakeUserRepository:
    def __init__(self, users_by_subject=None, users_by_id=None):
        self.users_by_subject = users_by_subject or {}
        self.users_by_id = users_by_id or {}

    async def get_by_sso(self, provider, provider_id):
        return self.users_by_subject.get((provider, provider_id))

    async def get(self, user_id):
        return self.users_by_id.get(user_id)


class FakeTokenGenerator:
    async def generate_access_token(self, user):
        return f"access-{user.user_id}"

    async def generate_refresh_token(self, user):
        return f"refresh-{user.user_id}"


class FakeSessionService:
    def __init__(self):
        self.created = []

    async def create_session(self, user_id, client_id=None, metadata=None):
        self.created.append({"user_id": user_id, "metadata": metadata or {}})
        return SimpleNamespace(session_id=f"sess-{user_id}"), False


@pytest.fixture
def store():
    return SSOEphemeralStore(fakeredis.FakeRedis(decode_responses=True))


def build_service(
    store,
    *,
    provider=None,
    users_by_subject=None,
    users_by_id=None,
    session_service=None,
    dashboard_url=DASHBOARD_URL,
):
    return SSOLoginService(
        identity_provider=provider or FakeProvider(),
        ephemeral_store=store,
        user_repository=FakeUserRepository(users_by_subject, users_by_id),
        token_generator=FakeTokenGenerator(),
        session_service=session_service or FakeSessionService(),
        dashboard_url=dashboard_url,
        access_token_expires_in=3600,
    )


def redirect_params(url: str) -> dict:
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == DASHBOARD_URL
    assert parts.path == "/auth/sso/callback"
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


def state_from(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


# =============================================================================
# return_to sanitization
# =============================================================================


@pytest.mark.parametrize(
    "value",
    [
        "/cases",
        "/cases/abc-123?tab=report",
        "/",
    ],
)
def test_sanitize_return_to_accepts_same_origin_paths(value):
    assert sanitize_return_to(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://evil.test/phish",
        "//evil.test/phish",
        "/\\evil.test",
        "\\evil",
        "cases",  # relative, not rooted
        "/cases\r\nSet-Cookie: x=y",  # header/control injection
        "/cases with space",
        "/" + "a" * 600,  # over length cap
    ],
)
def test_sanitize_return_to_rejects_unsafe_values(value):
    assert sanitize_return_to(value) is None


# =============================================================================
# begin_login
# =============================================================================


async def test_begin_login_returns_idp_url_with_stored_state(store):
    service = build_service(store)
    url = await service.begin_login("/cases")
    assert url.startswith("https://authkit.test/authorize?state=")
    payload = await store.consume_state(state_from(url))
    assert payload == {"return_to": "/cases"}


async def test_begin_login_drops_unsafe_return_to(store):
    service = build_service(store)
    url = await service.begin_login("https://evil.test/phish")
    assert await store.consume_state(state_from(url)) == {}


async def test_begin_login_states_are_unique(store):
    service = build_service(store)
    first = await service.begin_login(None)
    second = await service.begin_login(None)
    assert state_from(first) != state_from(second)


# =============================================================================
# complete_callback
# =============================================================================


async def test_callback_happy_path_issues_completion_code(store):
    user = make_user()
    service = build_service(store, users_by_subject={("workos", "user_wos_123"): user})
    login_url = await service.begin_login("/cases")

    redirect = await service.complete_callback(
        code="authkit-code", state=state_from(login_url), error=None
    )

    params = redirect_params(redirect)
    assert params["return_to"] == "/cases"
    assert "error" not in params
    # The completion code is real and single-use in the store.
    assert await store.consume_login(params["code"]) == {"user_id": "u-1"}
    assert await store.consume_login(params["code"]) is None


async def test_callback_without_state_is_rejected(store):
    service = build_service(store)
    redirect = await service.complete_callback(code="c", state=None, error=None)
    assert redirect_params(redirect) == {"error": ERROR_STATE_INVALID}


async def test_callback_with_unknown_state_is_rejected(store):
    service = build_service(store)
    redirect = await service.complete_callback(
        code="c", state="never-issued", error=None
    )
    assert redirect_params(redirect) == {"error": ERROR_STATE_INVALID}


async def test_callback_state_is_single_use(store):
    user = make_user()
    service = build_service(store, users_by_subject={("workos", "user_wos_123"): user})
    state = state_from(await service.begin_login(None))
    first = await service.complete_callback(code="c", state=state, error=None)
    assert "code" in redirect_params(first)

    replay = await service.complete_callback(code="c", state=state, error=None)
    assert redirect_params(replay) == {"error": ERROR_STATE_INVALID}


async def test_callback_maps_idp_access_denied(store):
    service = build_service(store)
    state = state_from(await service.begin_login("/cases"))
    redirect = await service.complete_callback(
        code=None, state=state, error="access_denied"
    )
    params = redirect_params(redirect)
    assert params["error"] == ERROR_ACCESS_DENIED
    assert params["return_to"] == "/cases"


async def test_callback_maps_other_idp_errors_to_generic_slug(store):
    service = build_service(store)
    state = state_from(await service.begin_login(None))
    redirect = await service.complete_callback(
        code=None, state=state, error="server_error: upstream SAML assertion invalid"
    )
    params = redirect_params(redirect)
    assert params["error"] == ERROR_FAILED
    # Raw IdP error text must never be echoed to the browser.
    assert "SAML" not in redirect


async def test_callback_idp_error_still_consumes_state(store):
    service = build_service(store)
    state = state_from(await service.begin_login(None))
    await service.complete_callback(code=None, state=state, error="access_denied")
    replay = await service.complete_callback(code="c", state=state, error=None)
    assert redirect_params(replay) == {"error": ERROR_STATE_INVALID}


async def test_callback_missing_code_is_generic_failure(store):
    service = build_service(store)
    state = state_from(await service.begin_login(None))
    redirect = await service.complete_callback(code=None, state=state, error=None)
    assert redirect_params(redirect)["error"] == ERROR_FAILED


async def test_callback_exchange_failure_maps_to_slug(store):
    service = build_service(store, provider=FakeProvider(fail=True))
    state = state_from(await service.begin_login(None))
    redirect = await service.complete_callback(code="bad", state=state, error=None)
    assert redirect_params(redirect)["error"] == ERROR_EXCHANGE_FAILED


async def test_callback_unknown_subject_maps_to_slug(store):
    # No users registered: strict match-by-subject finds nothing (JIT is a
    # later phase).
    service = build_service(store)
    state = state_from(await service.begin_login(None))
    redirect = await service.complete_callback(code="c", state=state, error=None)
    assert redirect_params(redirect)["error"] == ERROR_USER_UNKNOWN


async def test_callback_inactive_user_maps_to_slug(store):
    user = make_user(is_active=False)
    service = build_service(store, users_by_subject={("workos", "user_wos_123"): user})
    state = state_from(await service.begin_login(None))
    redirect = await service.complete_callback(code="c", state=state, error=None)
    assert redirect_params(redirect)["error"] == ERROR_USER_INACTIVE


async def test_callback_handles_dashboard_url_trailing_slash(store):
    service = build_service(store, dashboard_url=DASHBOARD_URL + "/")
    redirect = await service.complete_callback(code=None, state=None, error=None)
    assert redirect.startswith(f"{DASHBOARD_URL}/auth/sso/callback?")


# =============================================================================
# exchange
# =============================================================================


async def _login_and_get_code(service, store):
    state = state_from(await service.begin_login(None))
    redirect = await service.complete_callback(code="c", state=state, error=None)
    return redirect_params(redirect)["code"]


async def test_exchange_happy_path_mints_session(store):
    user = make_user()
    sessions = FakeSessionService()
    service = build_service(
        store,
        users_by_subject={("workos", "user_wos_123"): user},
        users_by_id={"u-1": user},
        session_service=sessions,
    )
    code = await _login_and_get_code(service, store)

    result = await service.exchange(code)

    assert result is not None
    assert result.access_token == "access-u-1"
    assert result.refresh_token == "refresh-u-1"
    assert result.expires_in == 3600
    assert result.session_id == "sess-u-1"
    assert result.user is user
    assert sessions.created[0]["metadata"]["login_method"] == "sso"
    assert sessions.created[0]["metadata"]["sso_provider"] == "workos"


async def test_exchange_unknown_code_returns_none(store):
    service = build_service(store)
    assert await service.exchange("never-issued") is None


async def test_exchange_code_is_single_use(store):
    user = make_user()
    service = build_service(
        store,
        users_by_subject={("workos", "user_wos_123"): user},
        users_by_id={"u-1": user},
    )
    code = await _login_and_get_code(service, store)
    assert await service.exchange(code) is not None
    assert await service.exchange(code) is None


async def test_exchange_fails_when_user_deleted_after_callback(store):
    user = make_user()
    # Present at callback time (by subject), gone at exchange time (by id).
    service = build_service(
        store, users_by_subject={("workos", "user_wos_123"): user}, users_by_id={}
    )
    code = await _login_and_get_code(service, store)
    assert await service.exchange(code) is None


async def test_exchange_fails_when_user_deactivated_after_callback(store):
    active = make_user()
    deactivated = make_user(is_active=False)
    service = build_service(
        store,
        users_by_subject={("workos", "user_wos_123"): active},
        users_by_id={"u-1": deactivated},
    )
    code = await _login_and_get_code(service, store)
    assert await service.exchange(code) is None
