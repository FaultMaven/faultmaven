"""Two rules that used to have no single home, and now do.

**A code is redeemable once.** Claiming was a read-then-write split across two
repository calls, so two concurrent redemptions could both observe the code
unused and both mint a token pair — the replay RFC 6749 §4.1.2 requires the
server to prevent. It is now one atomic call the store arbitrates.

**A deactivated account holds no live credential.** That rule was written six
times across five modules in three spellings, and the two paths that had no copy
(`POST /auth/login`, `POST /auth/register`) would happily mint for a deactivated
user. It now lives in one predicate, enforced at the one chokepoint every mint
path funnels through.

Both are tested as *properties of the system* rather than of the call sites that
happen to exist today: the deactivation tests sweep every mint entry point by
construction, so a new one added later is covered without anyone remembering to
come back here.
"""

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.config.settings import AuthSettings
from faultmaven.exceptions import AuthorizationError, InactiveAccountError
from faultmaven.models.exceptions import InvalidGrantError
from faultmaven.modules.auth.contracts import OAuthAuthorizationDTO, OAuthCodeDTO
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
    RS256JWTTokenGenerator,
    account_may_hold_credentials,
)
from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl
from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
    InMemoryOAuthCodeRepository,
    RedisOAuthCodeRepository,
)
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
)

#: The configured pair, as production wires it (JWT_ISSUER/JWT_AUDIENCE
#: defaults). Deliberately not the literals the HS256 paths once hardcoded:
#: a fixture that matched those could not observe #938.
ISSUER = "faultmaven-api"
AUDIENCE = "faultmaven-app"
SECRET = "test-secret-key-for-hs256-signing-only"
REDIRECT = "chrome-extension://abc123/callback.html"


def _pkce():
    import base64
    import hashlib
    import secrets

    verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
        .decode("utf-8")
        .rstrip("=")
    )
    return verifier, challenge


def _user(**overrides):
    fields = dict(
        user_id="user_123",
        username="testuser",
        email="testuser@acme.example",
        display_name="Test User",
        created_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return DevUser(**fields)


def _generator():
    fakeredis = pytest.importorskip("fakeredis")
    return HS256JWTTokenGenerator(
        secret_key=SECRET,
        revocation_store=RedisTokenRevocationStore(
            fakeredis.aioredis.FakeRedis(decode_responses=True)
        ),
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


def _service(code_repository, user=None):
    users = AsyncMock()
    users.get = AsyncMock(return_value=user if user is not None else _user())
    return OAuthServiceImpl(
        code_repository=code_repository,
        user_repository=users,
        token_generator=_generator(),
        settings=AuthSettings(
            oauth_allowed_clients=["faultmaven-copilot"],
            oauth_redirect_uri_patterns=[
                r"^chrome-extension://[a-z0-9]+/callback\.html$"
            ],
        ),
    )


def _authorization_request(challenge):
    return OAuthAuthorizationDTO(
        client_id="faultmaven-copilot",
        redirect_uri=REDIRECT,
        state="state_abc",
        code_challenge=challenge,
        code_challenge_method="S256",
        scope="openid profile email",
    )


def _repositories():
    """Every wired implementation, so the guarantee is not per-backend."""
    fakeredis = pytest.importorskip("fakeredis")
    return [
        ("in_memory", InMemoryOAuthCodeRepository()),
        (
            "redis",
            RedisOAuthCodeRepository(
                fakeredis.aioredis.FakeRedis(decode_responses=True)
            ),
        ),
    ]


# =============================================================================
# A code is redeemable exactly once
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [2, 8])
async def test_concurrent_redemption_mints_exactly_one_token_pair(attempts):
    """The defect: N concurrent redemptions used to yield N token pairs.

    Run against every wired repository, and with more than two racers.

    Honest about what each arm proves. The **Redis** arm is the real race:
    fakeredis awaits yield, so all N racers reach the claim and a non-atomic
    implementation loses reliably — verified by porting this test to the
    pre-fix code, where it reports "minted 8 token pairs". The **in-memory**
    arm does NOT race: with no await between the check and the set the
    coroutines serialize, so 7 of 8 losers die at the fast-path `used` check
    and never reach `claim_code`. It is a contract check there (exactly one
    success), not a concurrency proof — and a no-yield read-modify-write is
    genuinely unraceable under one event loop, so there is no fault for it to
    miss. Insert one await into that critical section, however, and this test
    does catch it.
    """
    for name, repository in _repositories():
        verifier, challenge = _pkce()
        service = _service(repository)
        code = await service.create_authorization_code(
            "user_123", _authorization_request(challenge)
        )

        results = await asyncio.gather(
            *[
                service.exchange_code_for_token(
                    code=code, code_verifier=verifier, redirect_uri=REDIRECT
                )
                for _ in range(attempts)
            ],
            return_exceptions=True,
        )

        minted = [r for r in results if not isinstance(r, Exception)]
        refused = [r for r in results if isinstance(r, InvalidGrantError)]

        assert len(minted) == 1, f"{name}: minted {len(minted)} token pairs"
        assert len(refused) == attempts - 1, f"{name}: {refused}"
        assert all(r.error_code == "CODE_ALREADY_USED" for r in refused), name


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_claim_is_won_by_exactly_one_caller_at_the_repository():
    """The same property at the layer that owns it, without the service around it."""
    for name, repository in _repositories():
        await repository.save_code(
            OAuthCodeDTO(
                code="code_1",
                user_id="user_123",
                redirect_uri=REDIRECT,
                code_challenge="challenge",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )

        won = await asyncio.gather(*[repository.claim_code("code_1") for _ in range(8)])

        assert sum(1 for w in won if w) == 1, f"{name}: {won}"
        # And it stays claimed afterwards.
        assert await repository.claim_code("code_1") is False, name


@pytest.mark.unit
@pytest.mark.asyncio
async def test_claiming_an_unknown_or_expired_code_is_refused_not_granted():
    """Fail closed: nothing to claim must never read as a successful claim."""
    for name, repository in _repositories():
        assert await repository.claim_code("never-existed") is False, name

        await repository.save_code(
            OAuthCodeDTO(
                code="expired",
                user_id="user_123",
                redirect_uri=REDIRECT,
                code_challenge="challenge",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        assert await repository.claim_code("expired") is False, name


# =============================================================================
# A transient failure must not burn the holder's code
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_transient_user_store_failure_leaves_the_code_redeemable():
    """The second defect: the claim was spent before the fallible work.

    `DatabaseUserStore.get_user` swallows exceptions and returns None, so a
    database blip arrives here as USER_NOT_FOUND — indistinguishable from a
    permanent condition. With the claim spent first, the retry got
    CODE_ALREADY_USED and the holder had to restart the whole OAuth dance.
    """
    for name, repository in _repositories():
        verifier, challenge = _pkce()
        service = _service(repository)
        code = await service.create_authorization_code(
            "user_123", _authorization_request(challenge)
        )

        # First attempt: the user store blips.
        service.user_repository.get = AsyncMock(return_value=None)
        with pytest.raises(InvalidGrantError) as blip:
            await service.exchange_code_for_token(
                code=code, code_verifier=verifier, redirect_uri=REDIRECT
            )
        assert blip.value.error_code == "USER_NOT_FOUND", name

        # It recovers, and the holder's code is still good.
        service.user_repository.get = AsyncMock(return_value=_user())
        tokens = await service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )
        assert tokens.access_token, name


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_stolen_code_without_the_verifier_still_cannot_be_burned():
    """Moving the claim later must not weaken this.

    Validation stays in front of the claim, so someone holding a leaked code but
    no PKCE verifier cannot spend it to deny the legitimate holder.
    """
    for name, repository in _repositories():
        verifier, challenge = _pkce()
        service = _service(repository)
        code = await service.create_authorization_code(
            "user_123", _authorization_request(challenge)
        )

        wrong_verifier, _ = _pkce()
        with pytest.raises(InvalidGrantError) as attack:
            await service.exchange_code_for_token(
                code=code, code_verifier=wrong_verifier, redirect_uri=REDIRECT
            )
        assert attack.value.error_code == "PKCE_VERIFICATION_FAILED", name

        # The real holder is unaffected.
        tokens = await service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )
        assert tokens.access_token, name


# =============================================================================
# A deactivated account holds no live credential — at the chokepoint
# =============================================================================

MINT_METHODS = ["generate_access_token", "generate_refresh_token"]

#: BOTH signing implementations, because the sweep is only as wide as this list.
#: An earlier version swept HS256 only — so both RS256 gates could be deleted
#: with the whole suite green, on the algorithm ``AUTH_MODE=oauth`` uses, i.e.
#: the cloud deployment this rule most matters for. The gap was invisible
#: precisely because the sweep *looked* exhaustive.
GENERATORS = ["hs256", "rs256"]


def _generator_named(name):
    fakeredis = pytest.importorskip("fakeredis")
    store = RedisTokenRevocationStore(
        fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    if name == "hs256":
        return HS256JWTTokenGenerator(
            secret_key=SECRET,
            revocation_store=store,
            access_token_expire_minutes=15,
            refresh_token_expire_days=7,
            issuer=ISSUER,
            audience=AUDIENCE,
        )

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return RS256JWTTokenGenerator(
        private_key=key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        public_key=key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
        revocation_store=store,
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("generator_name", GENERATORS)
@pytest.mark.parametrize("method", MINT_METHODS)
async def test_no_token_of_any_kind_is_minted_for_a_deactivated_account(
    method, generator_name
):
    """Swept over the whole mint surface: both algorithms, both token kinds.

    This is the property the six scattered copies were each trying to express.
    Enforcing it at the chokepoint means a mint path added tomorrow inherits it
    without its author knowing the rule exists — but only for paths that go
    through ``IJWTTokenGenerator``. See ``account_may_hold_credentials`` for the
    surface that is deliberately outside this guarantee.
    """
    generator = _generator_named(generator_name)
    with pytest.raises(InactiveAccountError):
        await getattr(generator, method)(_user(is_active=False))


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("generator_name", GENERATORS)
@pytest.mark.parametrize("method", MINT_METHODS)
async def test_an_active_account_is_unaffected(method, generator_name):
    """The negative control: the gate must not refuse everyone."""
    generator = _generator_named(generator_name)
    assert await getattr(generator, method)(_user(is_active=True))


@pytest.mark.unit
@pytest.mark.security
def test_the_refusal_is_an_authorization_failure_not_a_server_fault():
    """Answered as 403 by MRO, so a caller that does not translate it is safe.

    Asserting the subclass relationship rather than the handler registry: it is
    the relationship Starlette actually resolves on, and it is what stops an
    untranslated raise from reaching a client as a 500.
    """
    assert issubclass(InactiveAccountError, AuthorizationError)


@pytest.mark.unit
@pytest.mark.security
def test_the_predicate_refuses_when_the_flag_is_absent():
    """Absence must refuse, and the control uses a REAL type that has no flag.

    An earlier version permitted absence and asserted it with a bare local
    class, so it could not have noticed that `AuthenticatedUser` — the auth
    module's own request-path type, carrying user_id/organization_id/roles — has
    no `is_active` at all. That type is one refactor from a mint call, where
    permit-on-absence would have signed for an account nobody checked.

    Using `AuthenticatedUser` here rather than a stand-in is the point: it is
    the object that actually exists and actually lacks the field.
    """
    from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

    token_derived = AuthenticatedUser(
        user_id="user_123",
        organization_id="org_acme",
        email="testuser@acme.example",
        roles=["user"],
        permissions=[],
    )
    assert not hasattr(token_derived, "is_active")
    assert account_may_hold_credentials(token_derived) is False

    assert account_may_hold_credentials(_user(is_active=True)) is True
    assert account_may_hold_credentials(_user(is_active=False)) is False


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_the_oauth_exchange_still_reports_its_own_protocol_error():
    """The chokepoint must not flatten protocol vocabulary.

    The OAuth leg keeps its own check so a client sees InvalidGrantError with
    USER_INACTIVE, not a bare authorization failure. The chokepoint is the
    backstop for paths that forget, not a replacement for speaking OAuth.
    """
    repository = InMemoryOAuthCodeRepository()
    verifier, challenge = _pkce()
    service = _service(repository, user=_user(is_active=False))
    code = await service.create_authorization_code(
        "user_123", _authorization_request(challenge)
    )

    with pytest.raises(InvalidGrantError) as refusal:
        await service.exchange_code_for_token(
            code=code, code_verifier=verifier, redirect_uri=REDIRECT
        )
    assert refusal.value.error_code == "USER_INACTIVE"


# =============================================================================
# The chokepoint is the only mint (#853)
# =============================================================================

#: The parallel mint path `AuthService` used to carry. Each took a `user_id` and
#: an `organization_id` **string** and signed the latter verbatim, so a caller
#: could put any value in the `organization_id` claim — bypassing
#: `resolve_organization_claim`, the guard every real mint funnels through
#: (#850). Its only caller passed `organization_id or "org-default"`: a truthy,
#: non-sentinel, fabricated tenant that both layers of the #850 fix would have
#: waved through. Nothing routed to it, so it was removed rather than gated.
REMOVED_AUTH_SERVICE_MINTS = [
    "generate_access_token",
    "generate_refresh_token",
    "generate_token_pair",
]


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("method", REMOVED_AUTH_SERVICE_MINTS)
def test_auth_service_carries_no_mint_of_its_own(method):
    """A tombstone, not a behavioural test: there is nothing left to exercise.

    The names are cheap to reintroduce — `AuthService` is handed to the request
    path everywhere, so a future change needing a token has it in reach, and a
    three-line helper here would sign claims no guard inspects. Every mint goes
    through `IJWTTokenGenerator`, which signs from a *user object* and resolves
    the org claim; a new one added here instead must fail this test.

    Note the same three names exist on `IJWTTokenGenerator` and are the live,
    guarded path. This asserts only that `AuthService` does not carry them.
    """
    from faultmaven.modules.auth.domain.services.auth_service import AuthService

    assert not hasattr(AuthService, method), (
        f"AuthService.{method} is back. Mint through IJWTTokenGenerator instead: "
        "it takes the account, refuses a deactivated one, and resolves the "
        "organization claim rather than signing a caller-supplied string (#853)."
    )


# =============================================================================
# Findings from code review on PR #957
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
def test_the_refusal_does_not_disclose_the_account_id():
    """`POST /auth/login` reaches this while the caller is still anonymous.

    The 403 handler echoes `str(exc)` to the client, so an id interpolated into
    the message hands an unauthenticated caller the internal UUID of an account
    it just probed. The id belongs in the log line, which this asserts is still
    where it goes.
    """
    from faultmaven.modules.auth.domain.services import jwt_token_generator

    with pytest.raises(InactiveAccountError) as refusal:
        jwt_token_generator._refuse_if_deactivated(_user(is_active=False), "access")

    assert "user_123" not in str(refusal.value)


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_failed_used_flag_write_does_not_undo_a_won_claim():
    """The post-claim write is best-effort in fact, not only in intent.

    The claim is already won when it runs. Letting it raise turns a successful
    redemption into a 500, and the client's retry then loses the claim it
    already holds — the burned code the "claim last" ordering exists to prevent.
    """
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repository = RedisOAuthCodeRepository(client)

    await repository.save_code(
        OAuthCodeDTO(
            code="code_flag",
            user_id="user_123",
            redirect_uri=REDIRECT,
            code_challenge="challenge",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )

    async def _explode(*_args, **_kwargs):
        raise ConnectionError("redis went away mid-claim")

    client.setex = _explode

    assert await repository.claim_code("code_flag") is True
    # And single-use still holds, because the claim key is the gate.
    assert await repository.claim_code("code_flag") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_losing_a_claim_is_recorded_as_a_failed_exchange():
    """A concurrent replay is the branch this method now detects — count it.

    Deliberately NOT driven by `asyncio.gather`. The early `used` check records
    an identical `CODE_ALREADY_USED` metric, and once the winner flips the flag
    the losers reach that branch instead — so a racing test cannot tell which
    branch reported, and passes with the new call deleted. (It did: mutation
    testing caught this assertion being dead.)

    So the repository here reports the code as never-used while refusing every
    claim after the first. That is only reachable through the claim-loss branch,
    which is exactly what needs pinning.
    """
    from unittest.mock import patch

    repository = InMemoryOAuthCodeRepository()
    verifier, challenge = _pkce()
    service = _service(repository)
    code = await service.create_authorization_code(
        "user_123", _authorization_request(challenge)
    )

    # First redemption wins normally.
    assert await service.exchange_code_for_token(
        code=code, code_verifier=verifier, redirect_uri=REDIRECT
    )

    # Now replay the state a loser sees: the fast-path check still says
    # "unused", so only the atomic claim can refuse.
    stored = await repository.get_code(code)
    repository.get_code = AsyncMock(
        return_value=dataclasses.replace(stored, used=False)
    )

    with patch(
        "faultmaven.modules.auth.domain.services.oauth_service.oauth_metrics"
    ) as metrics:
        with pytest.raises(InvalidGrantError) as loss:
            await service.exchange_code_for_token(
                code=code, code_verifier=verifier, redirect_uri=REDIRECT
            )

    assert loss.value.error_code == "CODE_ALREADY_USED"
    failed = [
        call
        for call in metrics.record_token_exchange.call_args_list
        if call.kwargs.get("success") is False
        and call.kwargs.get("error_code") == "CODE_ALREADY_USED"
    ]
    assert len(failed) == 1, metrics.record_token_exchange.call_args_list
    assert failed[0].kwargs.get("duration_seconds") is not None


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_crafted_code_cannot_reach_the_claim_key():
    """The claim key must not live where `get_code` will parse it.

    The code arrives as unvalidated free text on an unauthenticated endpoint.
    While the claim key was `<code-key>:claimed`, a client that had completed one
    exchange could ask for `"<its own code>:claimed"`, land on a payload of `"1"`,
    and turn its 401 into an unhandled AttributeError — a remotely triggerable
    500. Separate prefixes make the collision unconstructible.
    """
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repository = RedisOAuthCodeRepository(client)

    await repository.save_code(
        OAuthCodeDTO(
            code="ABC",
            user_id="user_123",
            redirect_uri=REDIRECT,
            code_challenge="challenge",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    assert await repository.claim_code("ABC") is True

    # No key the code namespace can name is the claim key.
    keys = sorted(await client.keys("*"))
    assert not any(k.startswith("oauth:code:") and k.endswith(":claimed") for k in keys)

    # And the crafted lookup is simply "no such code", not an exception.
    assert await repository.get_code("ABC:claimed") is None
    # Single-use is unaffected by the move.
    assert await repository.claim_code("ABC") is False


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_non_object_payload_reads_as_no_such_code():
    """Independent of the prefix fix, and deliberately so.

    Two guards, because they fail for different reasons: the prefix stops this
    input being reachable, this stops any non-object payload — whatever wrote
    it — becoming a 500 instead of a 401.
    """
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repository = RedisOAuthCodeRepository(client)

    # Two distinct failure modes, and an isinstance check only covers the first:
    # valid JSON that is not an object, and text that is not JSON at all — the
    # latter raises inside json.loads, so the result is never inspected.
    not_an_object = ("1", '"a string"', "null", "[1, 2]", "true")
    not_even_json = ("", "1: claimed", "{oops", "\x00binary")
    for payload in not_an_object + not_even_json:
        await client.setex("oauth:code:weird", 600, payload)
        assert await repository.get_code("weird") is None, payload


@pytest.mark.unit
@pytest.mark.security
def test_the_refusal_really_answers_403_through_the_registered_handlers():
    """Exercise the mapping, do not merely assert the class relationship.

    The `issubclass` check above states the intent; this proves the app's real
    handler registry plus Starlette's MRO resolution actually turn the refusal
    into a 403. Those are different claims — a registry change could break the
    second while the first still passes.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from faultmaven.api.exception_handlers import get_exception_handlers

    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)

    @app.get("/mint")
    async def _mint():
        raise InactiveAccountError("This account is deactivated")

    response = TestClient(app, raise_server_exceptions=False).get("/mint")
    assert response.status_code == 403
    assert "user_123" not in response.text


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_deleting_a_user_revokes_their_outstanding_tokens_first():
    """Deletion must not leave live access tokens behind.

    Deleting removes the account but not the credentials issued from it:
    refresh stops (the user is gone) while outstanding ACCESS tokens keep
    authenticating for the rest of their TTL, because the request path never
    reloads the user. Deactivation — the weaker operation — already wrote a
    revocation watermark, so deleting a compromised account left it usable
    longer than merely disabling it.

    Order is asserted, not just the call: revoking after a successful delete
    would be unrecoverable if the revoke then failed.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from faultmaven.modules.auth.api import auth as auth_routes

    order = []
    user_store = AsyncMock()
    user_store.get_user_by_username = AsyncMock(
        return_value=SimpleNamespace(user_id="user_123", username="doomed")
    )

    async def _delete(_uid):
        order.append("delete")
        return True

    async def _revoke(_uid):
        order.append("revoke")
        return datetime.now(timezone.utc)

    user_store.delete_user = _delete
    auth_service = MagicMock()
    auth_service.revoke_user_tokens = _revoke

    request = MagicMock()
    request.app.state.auth_service = auth_service

    with patch.object(
        auth_routes, "get_user_store", AsyncMock(return_value=user_store)
    ):
        result = await auth_routes.delete_user(
            username="doomed", request=request, _=None
        )

    assert result["user_id"] == "user_123"
    assert order == ["revoke", "delete"], order


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_failed_revocation_blocks_the_delete():
    """If the credentials cannot be killed, the account must stay to try again.

    Deleting anyway would strand live tokens for an account that can no longer
    be looked up — nothing left to revoke them by.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from faultmaven.exceptions import ServiceError
    from faultmaven.modules.auth.api import auth as auth_routes

    deleted = []
    user_store = AsyncMock()
    user_store.get_user_by_username = AsyncMock(
        return_value=SimpleNamespace(user_id="user_123", username="doomed")
    )
    user_store.delete_user = AsyncMock(side_effect=lambda uid: deleted.append(uid))

    auth_service = MagicMock()
    auth_service.revoke_user_tokens = AsyncMock(
        side_effect=ServiceError("revocation store down")
    )
    request = MagicMock()
    request.app.state.auth_service = auth_service

    with patch.object(
        auth_routes, "get_user_store", AsyncMock(return_value=user_store)
    ):
        with pytest.raises(Exception):
            await auth_routes.delete_user(username="doomed", request=request, _=None)

    assert deleted == [], "the account was deleted despite revocation failing"
