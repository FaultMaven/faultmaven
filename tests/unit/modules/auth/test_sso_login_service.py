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

import asyncio
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


class FakeAuditRepository:
    """Records IAuditRepository.log_event calls; optionally explodes."""

    def __init__(self, fail=False):
        self.fail = fail
        self.entries: list[dict] = []

    async def log_event(self, **kwargs):
        if self.fail:
            raise RuntimeError("audit store down")
        self.entries.append(kwargs)
        return True


class FakeTokenGenerator:
    async def generate_access_token(self, user, *, state_read_at):
        return f"access-{user.user_id}"

    async def generate_refresh_token(self, user, *, state_read_at):
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
    audit=None,
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
        audit_log=audit,
    )


def redirect_params(url: str) -> dict:
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == DASHBOARD_URL
    assert parts.path == "/auth/sso/callback"
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


async def callback(
    service,
    *,
    code="authkit-code",
    state,
    error=None,
    browser=...,
    client_ip=None,
    user_agent=None,
):
    """Invoke complete_callback; browser cookie defaults to matching the state."""
    browser_state = state if browser is ... else browser
    return await service.complete_callback(
        code=code,
        state=state,
        error=error,
        browser_state=browser_state,
        client_ip=client_ip,
        user_agent=user_agent,
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
    payload = await store.consume_login(params["code"])
    assert payload.pop("state_read_at") > 0  # epoch seconds (#831)
    assert payload == {"user_id": "u-1"}
    assert await store.consume_login(params["code"]) is None


async def test_login_payload_capture_precedes_the_callback_reads(store):
    """#831 produce side: the carried ``state_read_at`` is the callback's
    ENTRY capture, taken before the org/user reads — not a stamp taken when
    the payload is built. A revoke-all landing during those reads must
    postdate the carried basis, or the exchange would mint a surviving pair
    from pre-revocation state. RED if the capture moves after the reads (or
    into the payload construction): the carried instant then lands after the
    read finished, and the assertion inverts."""
    user = make_user()

    read_finished: dict[str, datetime] = {}

    class SlowFirstReadStore(SSOEphemeralStore):
        """Delay the CALLBACK's first read — ``consume_state`` — not a later
        one: a capture regressed to after the state consume (but before the
        org/user reads) must also go RED, since the org resolution between
        them is exactly the leg-1 state the carry protects."""

        async def consume_state(self, state):
            await asyncio.sleep(0.3)
            read_finished["at"] = datetime.now(UTC)
            return await super().consume_state(state)

    slow_store = SlowFirstReadStore(store._redis)
    service = build_service(
        slow_store,
        repo=FakeUserRepository(users_by_subject={("workos", "user_wos_123"): user}),
    )
    start = await service.begin_login(None)

    redirect = await callback(service, state=start.state)

    params = redirect_params(redirect)
    payload = await slow_store.consume_login(params["code"])
    carried = datetime.fromtimestamp(payload["state_read_at"], UTC)
    assert carried < read_finished["at"]


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
    payload = await store.consume_login(params["code"])
    assert payload.pop("state_read_at") > 0  # epoch seconds (#831)
    assert payload == {"user_id": user.user_id}
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
    payload = await store.consume_login(params["code"])
    assert payload.pop("state_read_at") > 0  # epoch seconds (#831)
    assert payload == {"user_id": "u-won"}


# =============================================================================
# complete_callback — JIT audit trail (ADR-015 PR 7)
# =============================================================================


async def test_jit_creation_writes_audit_entry(store):
    repo = FakeUserRepository()
    audit = FakeAuditRepository()
    service = build_service(store, repo=repo, audit=audit)
    start = await service.begin_login(None)

    redirect = await callback(
        service,
        state=start.state,
        client_ip="203.0.113.7",
        user_agent="Mozilla/5.0 (test)",
    )

    assert "code" in redirect_params(redirect)
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    user = repo.created[0]
    assert entry["user_id"] == user.user_id
    assert entry["event_type"].value == "account_created"
    assert entry["event_category"].value == "authentication"
    assert entry["resource_type"] == "user"
    assert entry["resource_id"] == user.user_id
    assert entry["details"] == {
        "provider": "workos",
        "method": "sso_jit",
        "username": "alex",
    }
    assert entry["ip_address"] == "203.0.113.7"
    assert entry["user_agent"] == "Mozilla/5.0 (test)"
    assert entry["success"] is True


async def test_jit_audit_entry_never_carries_email_or_subject(store):
    # The audit details must not duplicate the email or IdP subject — the user
    # row already holds the email under its own lifecycle (same posture as the
    # log stream).
    audit = FakeAuditRepository()
    service = build_service(store, repo=FakeUserRepository(), audit=audit)
    start = await service.begin_login(None)
    await callback(service, state=start.state)

    blob = str(audit.entries[0]["details"])
    assert "alex@example.com" not in blob
    assert "user_wos_123" not in blob


async def test_returning_subject_writes_no_audit_entry(store):
    # Only JIT creation is audited (ADR-015 PR 7 scope) — a routine returning
    # login must not add an account_created row.
    existing = make_user()
    audit = FakeAuditRepository()
    service = build_service(
        store, users_by_subject={("workos", "user_wos_123"): existing}, audit=audit
    )
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    assert "code" in redirect_params(redirect)
    assert audit.entries == []


async def test_lost_create_race_writes_no_audit_entry(store):
    # The concurrent winner's callback audits the creation; the loser logs in
    # with the winner's row and must not duplicate the entry.
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

    audit = FakeAuditRepository()
    service = build_service(store, repo=RacingRepository(), audit=audit)
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    assert "code" in redirect_params(redirect)
    assert audit.entries == []


async def test_audit_write_failure_does_not_fail_the_login(store):
    # Fail-open: the account exists and the login is legitimate — an audit
    # store outage must not turn the first sign-in into an error.
    repo = FakeUserRepository()
    audit = FakeAuditRepository(fail=True)
    service = build_service(store, repo=repo, audit=audit)
    start = await service.begin_login(None)

    redirect = await callback(service, state=start.state)

    params = redirect_params(redirect)
    assert "error" not in params
    assert len(repo.created) == 1
    payload = await store.consume_login(params["code"])
    assert payload.pop("state_read_at") > 0  # epoch seconds (#831)
    assert payload == {"user_id": repo.created[0].user_id}


async def test_no_audit_repository_is_fine(store):
    # audit_log is optional — the default wiring in existing unit setups (and
    # any composition without a DB) must provision without it.
    repo = FakeUserRepository()
    service = build_service(store, repo=repo)
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    assert "code" in redirect_params(redirect)
    assert len(repo.created) == 1


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


async def test_profile_sync_ignores_malformed_idp_email(store):
    # The sync path assigns onto an existing model (no validate_assignment),
    # so it must run the same email validation the create path gets from
    # model construction — a malformed IdP email is never persisted.
    identity = SSOIdentity(
        provider="workos",
        provider_user_id="user_wos_123",
        email="not-an-email",
        email_verified=True,
    )
    user = make_user()
    repo = FakeUserRepository(users_by_subject={("workos", "user_wos_123"): user})
    service = build_service(store, provider=FakeProvider(identity=identity), repo=repo)
    start = await service.begin_login(None)
    redirect = await callback(service, state=start.state)

    assert "code" in redirect_params(redirect)
    assert user.email == "alex@example.com"


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


# --------------------------------------------------------------------------- #
# Single-logout: the IdP session id must survive the two-leg login
# --------------------------------------------------------------------------- #
#
# The session id is known only on the CALLBACK leg, but is needed by the
# EXCHANGE leg that answers the client. It rides the completion code, exactly as
# the resolved organization does. If that hand-off breaks, logout silently stops
# ending the IdP session and the regression is invisible until someone tries to
# switch accounts.

IDENTITY_WITH_SESSION = SSOIdentity(
    provider="workos",
    provider_user_id="user_wos_123",
    email="alex@example.com",
    email_verified=True,
    display_name="Alex Example",
    provider_session_id="session_01HSID",
)


class LogoutCapableProvider(FakeProvider):
    """A provider that offers single-logout, and records what it was asked."""

    def __init__(self, identity=IDENTITY_WITH_SESSION, raises=None):
        super().__init__(identity=identity)
        self.raises = raises
        self.logout_calls: list[str] = []
        self.return_tos: list[str | None] = []

    def build_logout_url(
        self, *, provider_session_id: str, return_to: str | None = None
    ) -> str | None:
        self.logout_calls.append(provider_session_id)
        self.return_tos.append(return_to)
        if self.raises is not None:
            raise self.raises
        return f"https://authkit.test/logout?session={provider_session_id}"


async def _exchange_with(store, provider):
    user = make_user()
    service = build_service(
        store,
        provider=provider,
        users_by_subject={("workos", "user_wos_123"): user},
        users_by_id={"u-1": user},
    )
    code = await _login_and_get_code(service, store)
    return await service.exchange(code)


async def test_exchange_returns_the_idp_logout_url(store):
    """The whole point: the client is told where to end the IdP session."""
    provider = LogoutCapableProvider()

    result = await _exchange_with(store, provider)

    assert result is not None
    assert result.idp_logout_url == "https://authkit.test/logout?session=session_01HSID"
    # Built from the id captured on the CALLBACK leg — proving the hand-off.
    assert provider.logout_calls == ["session_01HSID"]
    # The destination is named, not inherited from the IdP's default Logout URI
    # — a dashboard setting this deployment cannot see, so an unset or stale one
    # would land the user somewhere arbitrary right after signing out.
    assert provider.return_tos == [DASHBOARD_URL]


async def test_exchange_returns_no_logout_url_without_a_provider_session(store):
    """A provider that exposes no session id yields no logout URL, and the login
    is otherwise untouched — this is the pre-existing behaviour, not a failure."""
    provider = LogoutCapableProvider(identity=IDENTITY)  # no provider_session_id

    result = await _exchange_with(store, provider)

    assert result is not None
    assert result.access_token  # the login still works
    assert result.idp_logout_url is None
    # Never asked: there is no session to end.
    assert provider.logout_calls == []


async def test_exchange_survives_a_provider_that_raises_building_the_url(store):
    """A broken logout link must not cost a working sign-in.

    ``exchange`` runs after the user is already authenticated; raising here
    would turn a successful login into a 401 for the sake of a cosmetic field.
    """
    provider = LogoutCapableProvider(raises=RuntimeError("idp unreachable"))

    result = await _exchange_with(store, provider)

    assert result is not None
    assert result.access_token
    assert result.idp_logout_url is None
