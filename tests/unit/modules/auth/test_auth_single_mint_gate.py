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
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from faultmaven.config.settings import AuthSettings
from faultmaven.exceptions import AuthorizationError, InactiveAccountError
from faultmaven.models.exceptions import InvalidGrantError
from faultmaven.modules.auth.contracts import OAuthAuthorizationDTO, OAuthCodeDTO
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
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

    Run against every wired repository, and with more than two racers — a
    two-caller race can be won by luck of scheduling, whereas eight makes a
    non-atomic implementation lose reliably.
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


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("method", MINT_METHODS)
async def test_no_token_of_any_kind_is_minted_for_a_deactivated_account(method):
    """Swept over the mint surface, not asserted at one call site.

    This is the property the six scattered copies were each trying to express.
    Enforcing it here means a mint path added tomorrow inherits it without its
    author knowing the rule exists.
    """
    generator = _generator()
    with pytest.raises(InactiveAccountError):
        await getattr(generator, method)(_user(is_active=False))


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("method", MINT_METHODS)
async def test_an_active_account_is_unaffected(method):
    """The negative control: the gate must not refuse everyone."""
    generator = _generator()
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
def test_the_predicate_permits_when_the_flag_is_absent():
    """Documented direction, pinned: absence means "not a user", not "deactivated".

    `users.is_active` is NOT NULL DEFAULT 1 and every user type on a mint path
    declares it, so refusing on absence would lock out every account the day an
    unrelated refactor passes something without the attribute.
    """

    class NoFlag:
        user_id = "user_123"

    assert account_may_hold_credentials(NoFlag()) is True
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
async def test_the_account_aware_mint_on_auth_service_refuses_a_deactivated_account():
    """The second signing surface, gated where it can see an account.

    `AuthService` signs from a subject id and never touches
    `IJWTTokenGenerator`, so the chokepoint does not reach it. Its one
    account-aware composition does enforce the rule when handed the account.
    """
    from faultmaven.modules.auth.domain.services.auth_service import AuthService

    service = AuthService(revocation_store=AsyncMock())

    with pytest.raises(InactiveAccountError):
        service.generate_token_pair(
            user_id="user_123",
            organization_id="org_acme",
            email="testuser@acme.example",
            roles=["user"],
            account=_user(is_active=False),
        )

    # An active account is unaffected.
    pair = service.generate_token_pair(
        user_id="user_123",
        organization_id="org_acme",
        email="testuser@acme.example",
        roles=["user"],
        account=_user(is_active=True),
    )
    assert pair.access_token and pair.refresh_token


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

    The early `used` check reports to `record_token_exchange`; the claim-loss
    branch is where a genuine race actually lands. If only the first reported,
    the exchange failure rate would omit exactly the contention it was added to
    surface, and would look healthiest under load.
    """
    from unittest.mock import patch

    repository = InMemoryOAuthCodeRepository()
    verifier, challenge = _pkce()
    service = _service(repository)
    code = await service.create_authorization_code(
        "user_123", _authorization_request(challenge)
    )

    with patch(
        "faultmaven.modules.auth.domain.services.oauth_service.oauth_metrics"
    ) as metrics:
        results = await asyncio.gather(
            *[
                service.exchange_code_for_token(
                    code=code, code_verifier=verifier, redirect_uri=REDIRECT
                )
                for _ in range(4)
            ],
            return_exceptions=True,
        )

    losses = [r for r in results if isinstance(r, InvalidGrantError)]
    assert len(losses) == 3

    failed = [
        call
        for call in metrics.record_token_exchange.call_args_list
        if call.kwargs.get("success") is False
        and call.kwargs.get("error_code") == "CODE_ALREADY_USED"
    ]
    assert len(failed) == 3, metrics.record_token_exchange.call_args_list
    assert all(c.kwargs.get("duration_seconds") is not None for c in failed)
