"""Unit tests for the SSO hosted-login orchestration service (ADR-015, PR 2b).

The service is the security surface of the cloud login flow, so these tests
pin its guarantees: state is single-use AND browser-bound (login-CSRF defense),
``return_to`` is same-origin path-only, every callback failure — including
infrastructure exceptions — maps to a sanitized error slug (never IdP text,
never a raised error), the completion code is single-use, and exchange
failures are uniform (None) regardless of cause. JIT provisioning (ADR-015
D4/D5) is strict subject-match-or-create: no email linking, no admin grant.
The ephemeral store is real (FakeRedis-backed) so single-use semantics are
exercised end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    SSOLoginService,
    derive_username,
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


def make_user(user_id="u-1", is_active=True, deleted_at=None, **overrides):
    user = SimpleNamespace(
        user_id=user_id,
        username="alex",
        email="alex@example.com",
        display_name="Alex Example",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_active=is_active,
        is_email_verified=True,
        email_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_login_at=None,
        deleted_at=deleted_at,
        roles=["user"],
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


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
    """Duck-typed user repository with JIT-provisioning support.

    ``users_by_subject``/``users_by_id`` mirror the lookup indexes the service
    uses; ``existing`` holds pre-existing unlinked accounts that only the
    email/username uniqueness scans should see. ``created``/``updated`` record
    the writes for assertions.
    """

    def __init__(self, users_by_subject=None, users_by_id=None, existing=None):
        self.users_by_subject = users_by_subject or {}
        self.users_by_id = users_by_id or {}
        self.existing = list(existing or [])
        self.created = []
        self.updated = []

    def _all_users(self):
        seen = {}
        for user in (
            list(self.users_by_subject.values())
            + list(self.users_by_id.values())
            + self.existing
            + self.created
        ):
            seen[id(user)] = user
        return list(seen.values())

    async def get_by_sso(self, provider, provider_id):
        return self.users_by_subject.get((provider, provider_id))

    async def get(self, user_id):
        return self.users_by_id.get(user_id)

    async def get_by_username(self, username):
        for user in self._all_users():
            if user.username.lower() == username.lower():
                return user
        return None

    async def get_by_email(self, email):
        for user in self._all_users():
            if user.email.lower() == email.lower():
                return user
        return None

    async def create(self, user):
        from faultmaven.exceptions import ConflictError

        if await self.get_by_email(user.email):
            raise ConflictError("Email already registered")
        if await self.get_by_username(user.username):
            raise ConflictError("Username already taken")
        self.created.append(user)
        self.users_by_subject[(user.sso_provider, user.sso_provider_id)] = user
        self.users_by_id[user.user_id] = user
        return user

    async def update(self, user):
        self.updated.append(user)
        return user


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
    repo=None,
    session_service=None,
    dashboard_url=DASHBOARD_URL,
):
    return SSOLoginService(
        identity_provider=provider or FakeProvider(),
        ephemeral_store=store,
        user_repository=(
            repo
            if repo is not None
            else FakeUserRepository(users_by_subject, users_by_id)
        ),
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


async def callback(service, *, code="authkit-code", state, error=None, browser=...):
    """Invoke complete_callback; browser cookie defaults to matching the state."""
    browser_state = state if browser is ... else browser
    return await service.complete_callback(
        code=code, state=state, error=error, browser_state=browser_state
    )


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
    start = await service.begin_login("/cases")
    assert start.authorization_url == (
        f"https://authkit.test/authorize?state={start.state}"
    )
    assert await store.consume_state(start.state) == {"return_to": "/cases"}


async def test_begin_login_drops_unsafe_return_to(store):
    service = build_service(store)
    start = await service.begin_login("https://evil.test/phish")
    assert await store.consume_state(start.state) == {}


async def test_begin_login_states_are_unique(store):
    service = build_service(store)
    first = await service.begin_login(None)
    second = await service.begin_login(None)
    assert first.state != second.state


# =============================================================================
# complete_callback — state + browser binding
# =============================================================================


async def test_callback_happy_path_issues_completion_code(store):
    user = make_user()
    service = build_service(store, users_by_subject={("workos", "user_wos_123"): user})
    start = await service.begin_login("/cases")

    redirect = await callback(service, state=start.state)

    params = redirect_params(redirect)
    assert params["return_to"] == "/cases"
    assert "error" not in params
    # The completion code is real and single-use in the store.
    assert await store.consume_login(params["code"]) == {"user_id": "u-1"}
    assert await store.consume_login(params["code"]) is None


async def test_callback_without_state_is_rejected(store):
    service = build_service(store)
    redirect = await callback(service, state=None, browser=None)
    assert redirect_params(redirect) == {"error": ERROR_STATE_INVALID}


async def test_callback_with_unknown_state_is_rejected(store):
    service = build_service(store)
    redirect = await callback(service, state="never-issued")
    assert redirect_params(redirect) == {"error": ERROR_STATE_INVALID}


async def test_callback_with_oversized_state_is_rejected(store):
    service = build_service(store)
    redirect = await callback(service, state="s" * 4096)
    assert redirect_params(redirect) == {"error": ERROR_STATE_INVALID}


async def test_callback_state_is_single_use(store):
    user = make_user()
    service = build_service(store, users_by_subject={("workos", "user_wos_123"): user})
    start = await service.begin_login(None)
    first = await callback(service, state=start.state)
    assert "code" in redirect_params(first)

    replay = await callback(service, state=start.state)
    assert redirect_params(replay) == {"error": ERROR_STATE_INVALID}


async def test_callback_without_browser_cookie_is_rejected(store):
    # Login-CSRF: a callback URL replayed in a browser that did not start the
    # flow carries no state cookie — reject, and burn the state.
    user = make_user()
    service = build_service(store, users_by_subject={("workos", "user_wos_123"): user})
    start = await service.begin_login(None)

    redirect = await callback(service, state=start.state, browser=None)
    assert redirect_params(redirect) == {"error": ERROR_STATE_INVALID}
    # The state was consumed by the rejected attempt — the URL is dead now.
    replay = await callback(service, state=start.state)
    assert redirect_params(replay) == {"error": ERROR_STATE_INVALID}


async def test_callback_with_mismatched_browser_cookie_is_rejected(store):
    user = make_user()
    service = build_service(store, users_by_subject={("workos", "user_wos_123"): user})
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state, browser="other-state")
    assert redirect_params(redirect) == {"error": ERROR_STATE_INVALID}


# =============================================================================
# complete_callback — IdP errors, exchange, user resolution
# =============================================================================


async def test_callback_maps_idp_access_denied(store):
    service = build_service(store)
    start = await service.begin_login("/cases")
    redirect = await callback(
        service, code=None, state=start.state, error="access_denied"
    )
    params = redirect_params(redirect)
    assert params["error"] == ERROR_ACCESS_DENIED
    assert params["return_to"] == "/cases"


async def test_callback_maps_other_idp_errors_to_generic_slug(store):
    service = build_service(store)
    start = await service.begin_login(None)
    redirect = await callback(
        service,
        code=None,
        state=start.state,
        error="server_error: upstream SAML assertion invalid",
    )
    params = redirect_params(redirect)
    assert params["error"] == ERROR_FAILED
    # Raw IdP error text must never be echoed to the browser.
    assert "SAML" not in redirect


async def test_callback_idp_error_still_consumes_state(store):
    service = build_service(store)
    start = await service.begin_login(None)
    await callback(service, code=None, state=start.state, error="access_denied")
    replay = await callback(service, state=start.state)
    assert redirect_params(replay) == {"error": ERROR_STATE_INVALID}


async def test_callback_missing_code_is_generic_failure(store):
    service = build_service(store)
    start = await service.begin_login(None)
    redirect = await callback(service, code=None, state=start.state)
    assert redirect_params(redirect)["error"] == ERROR_FAILED


async def test_callback_oversized_code_is_generic_failure(store):
    provider = FakeProvider()
    service = build_service(store, provider=provider)
    start = await service.begin_login(None)
    redirect = await callback(service, code="c" * 4096, state=start.state)
    assert redirect_params(redirect)["error"] == ERROR_FAILED
    # The garbage code was never shipped to the IdP.
    assert provider.exchanged_codes == []


async def test_callback_exchange_failure_maps_to_slug(store):
    service = build_service(store, provider=FakeProvider(fail=True))
    start = await service.begin_login(None)
    redirect = await callback(service, code="bad", state=start.state)
    assert redirect_params(redirect)["error"] == ERROR_EXCHANGE_FAILED


async def test_callback_inactive_user_maps_to_slug(store):
    user = make_user(is_active=False)
    service = build_service(store, users_by_subject={("workos", "user_wos_123"): user})
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)
    assert redirect_params(redirect)["error"] == ERROR_USER_INACTIVE


async def test_callback_soft_deleted_user_maps_to_slug(store):
    user = make_user(deleted_at=datetime(2026, 6, 1, tzinfo=UTC))
    service = build_service(store, users_by_subject={("workos", "user_wos_123"): user})
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)
    assert redirect_params(redirect)["error"] == ERROR_USER_INACTIVE


# =============================================================================
# complete_callback — JIT provisioning (ADR-015 D4/D5)
# =============================================================================


def test_derive_username_sanitizes_local_part():
    assert derive_username("Alex@example.com") == "alex"
    assert derive_username("a.b-c_d@example.com") == "a.b-c_d"
    assert derive_username("Alex.O'Neil+dev@Example.com") == "alex.oneildev"
    assert derive_username("..alex..@example.com") == "alex"
    assert derive_username("++++@example.com") == "user"
    assert derive_username("a" * 200 + "@example.com") == "a" * 64


async def test_callback_unknown_subject_provisions_user(store):
    repo = FakeUserRepository()
    service = build_service(store, repo=repo)
    start = await service.begin_login(None)

    redirect = await callback(service, state=start.state)

    params = redirect_params(redirect)
    assert "error" not in params
    assert len(repo.created) == 1
    user = repo.created[0]
    assert user.username == "alex"
    assert user.email == "alex@example.com"
    assert user.display_name == "Alex Example"
    assert user.hashed_password is None
    assert user.sso_provider == "workos"
    assert user.sso_provider_id == "user_wos_123"
    assert user.is_active is True
    assert user.is_email_verified is True
    assert user.email_verified_at is not None
    assert user.last_login_at is not None
    # Never admin (D5): every JIT user gets exactly the base role.
    assert user.roles == ["user"]
    # The completion code points at the new user; no redundant profile sync.
    assert await store.consume_login(params["code"]) == {"user_id": user.user_id}
    assert repo.updated == []


async def test_jit_user_reflects_unverified_email(store):
    identity = SSOIdentity(
        provider="workos",
        provider_user_id="user_wos_999",
        email="sam@example.com",
        email_verified=False,
    )
    repo = FakeUserRepository()
    service = build_service(store, provider=FakeProvider(identity=identity), repo=repo)
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    assert "code" in redirect_params(redirect)
    user = repo.created[0]
    assert user.is_email_verified is False
    assert user.email_verified_at is None
    # Display name falls back to the derived username when the IdP has none.
    assert user.display_name == "sam"


async def test_jit_username_collision_gets_numeric_suffix(store):
    taken = make_user(user_id="u-old", email="other@example.com")  # username "alex"
    repo = FakeUserRepository(existing=[taken])
    service = build_service(store, repo=repo)
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    assert "code" in redirect_params(redirect)
    assert repo.created[0].username == "alex-2"


async def test_jit_refuses_email_owned_by_unlinked_account(store):
    # ADR-015 D4: never link by email — an existing unlinked account owning
    # the identity's email fails the login and provisions nothing.
    unlinked = make_user(user_id="u-old", username="someone-else")
    repo = FakeUserRepository(existing=[unlinked])
    service = build_service(store, repo=repo)
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    assert redirect_params(redirect)["error"] == ERROR_FAILED
    assert repo.created == []


@pytest.mark.parametrize("email", ["", "not-an-email", "a" * 300 + "@example.com"])
async def test_jit_refuses_unusable_email(store, email):
    identity = SSOIdentity(
        provider="workos",
        provider_user_id="user_wos_999",
        email=email,
        email_verified=True,
    )
    repo = FakeUserRepository()
    service = build_service(store, provider=FakeProvider(identity=identity), repo=repo)
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    assert redirect_params(redirect)["error"] == ERROR_FAILED
    assert repo.created == []


async def test_jit_create_race_falls_back_to_concurrent_row(store):
    # Two concurrent first logins for the same subject: the loser's create
    # conflicts, but the subject now resolves — log in with that row.
    winner = make_user(user_id="u-won")

    class RacingRepository(FakeUserRepository):
        def __init__(self):
            super().__init__()
            self.sso_calls = 0

        async def get_by_sso(self, provider, provider_id):
            self.sso_calls += 1
            return None if self.sso_calls == 1 else winner

        async def create(self, user):
            from faultmaven.exceptions import ConflictError

            raise ConflictError("Email already registered")

    service = build_service(store, repo=RacingRepository())
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    params = redirect_params(redirect)
    assert await store.consume_login(params["code"]) == {"user_id": "u-won"}


# =============================================================================
# complete_callback — returning-subject profile sync (ADR-015 D4)
# =============================================================================


async def test_callback_syncs_profile_on_returning_subject(store):
    user = make_user(
        email="old@example.com",
        display_name="Old Name",
        is_email_verified=False,
        email_verified_at=None,
    )
    repo = FakeUserRepository(users_by_subject={("workos", "user_wos_123"): user})
    service = build_service(store, repo=repo)
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    assert "code" in redirect_params(redirect)
    assert repo.updated == [user]
    assert user.email == "alex@example.com"
    assert user.display_name == "Alex Example"
    assert user.is_email_verified is True
    assert user.email_verified_at is not None
    assert user.last_login_at is not None
    assert repo.created == []


async def test_callback_profile_sync_conflict_does_not_fail_login(store):
    user = make_user(email="old@example.com")

    class ConflictingRepository(FakeUserRepository):
        async def update(self, user):
            from faultmaven.exceptions import ConflictError

            raise ConflictError("Email already in use")

    repo = ConflictingRepository(users_by_subject={("workos", "user_wos_123"): user})
    service = build_service(store, repo=repo)
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    assert "code" in redirect_params(redirect)


async def test_callback_handles_dashboard_url_trailing_slash(store):
    service = build_service(store, dashboard_url=DASHBOARD_URL + "/")
    redirect = await callback(service, state=None, browser=None)
    assert redirect.startswith(f"{DASHBOARD_URL}/auth/sso/callback?")


# =============================================================================
# complete_callback — infrastructure failures still redirect
# =============================================================================


class ExplodingRepository(FakeUserRepository):
    async def get_by_sso(self, provider, provider_id):
        raise RuntimeError("database connection lost")


async def test_callback_survives_repository_failure(store):
    service = SSOLoginService(
        identity_provider=FakeProvider(),
        ephemeral_store=store,
        user_repository=ExplodingRepository(),
        token_generator=FakeTokenGenerator(),
        session_service=FakeSessionService(),
        dashboard_url=DASHBOARD_URL,
        access_token_expires_in=3600,
    )
    start = await service.begin_login(None)
    # Browser is mid-redirect: infra failure must land on the dashboard, not
    # raise into a 500 page — and must not leak the failure detail.
    redirect = await callback(service, state=start.state)
    assert redirect_params(redirect)["error"] == ERROR_FAILED
    assert "database" not in redirect


async def test_callback_survives_store_failure():
    class ExplodingStore:
        async def consume_state(self, state):
            raise ConnectionError("redis down")

    service = SSOLoginService(
        identity_provider=FakeProvider(),
        ephemeral_store=ExplodingStore(),
        user_repository=FakeUserRepository(),
        token_generator=FakeTokenGenerator(),
        session_service=FakeSessionService(),
        dashboard_url=DASHBOARD_URL,
        access_token_expires_in=3600,
    )
    redirect = await callback(service, state="some-state")
    assert redirect_params(redirect)["error"] == ERROR_FAILED


# =============================================================================
# exchange
# =============================================================================


async def _login_and_get_code(service, store):
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)
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


async def test_exchange_fails_when_user_soft_deleted_after_callback(store):
    active = make_user()
    deleted = make_user(deleted_at=datetime(2026, 6, 1, tzinfo=UTC))
    service = build_service(
        store,
        users_by_subject={("workos", "user_wos_123"): active},
        users_by_id={"u-1": deleted},
    )
    code = await _login_and_get_code(service, store)
    assert await service.exchange(code) is None
