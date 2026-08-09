"""A generator validates exactly what it mints, for any configured pair (#938).

``iss`` and ``aud`` are the two claims a deployment is free to name. Before this,
the HS256 generator minted access tokens with its configured pair but validated
against the literals ``"faultmaven"``/``"faultmaven-api"``, and minted refresh
tokens with those literals regardless of configuration. Since ``JWT_ISSUER`` and
``JWT_AUDIENCE`` default to ``"faultmaven-api"``/``"faultmaven-app"``, a
production-wired generator could not validate its own access token.

That was latent rather than live, but by a thread worth naming: the sole caller
of ``validate_access_token`` (``oauth_service``) is handed a generator from
``create_jwt_token_generator``, which returns RS256 *unconditionally*. Its
mode-selecting sibling ``create_signing_token_generator`` returns HS256 under
``AUTH_MODE=local`` — a mode that may be combined with ``OAUTH_ENABLED=true``.
So the latency was a property of which builder the OAuth wiring happened to
call, not of the configuration; point that wiring at the sibling and the dead
path becomes live. Stated here because it is the kind of claim that stays in a
docstring long after it stops being true.

The guarantee under test is a property, not a pair of values: **for either
algorithm, either token kind, and any configured (issuer, audience), the claims
carry that pair, the minting generator accepts its own token, and a decoder
configured with the same pair accepts it too.**

The parameter set deliberately includes the two literals the old code hardcoded.
A test whose configured pair happens to equal the hardcode cannot observe the
defect — that is precisely why the pre-existing HS256 fixtures did not. Here
they are one case among several, and ``deployment-custom`` matches neither the
hardcodes nor the settings defaults, so any reintroduced literal fails.

``test_the_validator_itself_refuses_a_foreign_pair`` is the mutation check
riding along with the suite. The accept assertions are all satisfied by a
validator that verifies nothing, so something has to fail when the checking is
removed; that test presents a correctly-signed token differing only in its pair
and requires the generator's own validator to refuse it. Asserting through
``jwt.decode`` instead — as the test above it does — would measure PyJWT rather
than this module, and a ``verify_aud: False`` here would go unnoticed.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import jwt
import pytest

from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
    RS256JWTTokenGenerator,
    build_hs256_token_generator,
    build_rs256_token_generator,
)
from tests.utils import InMemoryRevocationStore

pytestmark = pytest.mark.asyncio

SECRET = "unit-test-secret-key-please-ignore"
ACCESS_MINUTES = 15
REFRESH_DAYS = 7

#: Configured pairs to sweep. ``settings-defaults`` is what production wires;
#: ``legacy-hardcode`` is what the HS256 paths used to bake in (kept so the
#: fixed code is exercised on it too, not to bless it); ``deployment-custom``
#: shares no value with either, so a reintroduced literal cannot satisfy it.
PAIRS = {
    "settings-defaults": ("faultmaven-api", "faultmaven-app"),
    "legacy-hardcode": ("faultmaven", "faultmaven-api"),
    "deployment-custom": ("acme-idp", "acme-consumers"),
}


class _User:
    """A user object shaped like the ones the mint paths actually receive."""

    user_id = "user-938"
    username = "coherence"
    email = "coherence@local.faultmaven"
    roles = ["user"]
    scopes = ["openid"]
    is_active = True
    deleted_at = None
    organization_id = "11111111-1111-1111-1111-111111111111"
    enterprise_id = "22222222-2222-2222-2222-222222222222"
    account_kind = "human"
    created_at = datetime.now(timezone.utc)


def _rsa_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _build(algorithm, issuer, audience):
    """Return (generator, verification key) wired exactly as the builders do."""
    if algorithm == "HS256":
        generator = HS256JWTTokenGenerator(
            secret_key=SECRET,
            revocation_store=InMemoryRevocationStore(),
            access_token_expire_minutes=ACCESS_MINUTES,
            refresh_token_expire_days=REFRESH_DAYS,
            issuer=issuer,
            audience=audience,
        )
        return generator, SECRET
    private_pem, public_pem = _rsa_pair()
    generator = RS256JWTTokenGenerator(
        private_key=private_pem,
        public_key=public_pem,
        revocation_store=InMemoryRevocationStore(),
        access_token_expire_minutes=ACCESS_MINUTES,
        refresh_token_expire_days=REFRESH_DAYS,
        issuer=issuer,
        audience=audience,
    )
    return generator, public_pem


async def _mint(generator, kind):
    if kind == "access":
        return await generator.generate_access_token(_User())
    return await generator.generate_refresh_token(_User())


async def _validate(generator, kind, token):
    if kind == "access":
        return await generator.validate_access_token(token)
    return await generator.validate_refresh_token(token)


ALGORITHMS = ["HS256", "RS256"]
KINDS = ["access", "refresh"]


@pytest.mark.parametrize("pair_name", list(PAIRS))
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("algorithm", ALGORITHMS)
async def test_claims_carry_the_configured_pair(algorithm, kind, pair_name):
    """Every mint stamps the configured issuer/audience — no literals."""
    issuer, audience = PAIRS[pair_name]
    generator, _ = _build(algorithm, issuer, audience)

    token = await _mint(generator, kind)
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["iss"] == issuer
    assert claims["aud"] == audience


@pytest.mark.parametrize("pair_name", list(PAIRS))
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("algorithm", ALGORITHMS)
async def test_a_generator_validates_its_own_token(algorithm, kind, pair_name):
    """The defect stated directly: mint and validate must agree."""
    issuer, audience = PAIRS[pair_name]
    generator, _ = _build(algorithm, issuer, audience)

    token = await _mint(generator, kind)
    payload = await _validate(generator, kind, token)

    assert payload is not None, (
        f"{algorithm} {kind} token minted with "
        f"iss={issuer!r}/aud={audience!r} was rejected by the generator "
        "that minted it"
    )
    assert payload["sub"] == _User.user_id


@pytest.mark.parametrize("pair_name", list(PAIRS))
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("algorithm", ALGORITHMS)
async def test_an_independent_decoder_with_the_same_pair_accepts(
    algorithm, kind, pair_name
):
    """The other live decoder agrees too.

    ``AuthService.verify_token`` decodes with ``settings.security.jwt_issuer``/
    ``jwt_audience`` and requires those claims present, so a token minted with a
    different pair fails there even when the generator's own validator is happy.
    That is the second direction of the same incoherence, and the reason this
    asserts against a decoder built from the configured pair rather than
    against the generator alone.
    """
    issuer, audience = PAIRS[pair_name]
    generator, key = _build(algorithm, issuer, audience)

    token = await _mint(generator, kind)
    claims = jwt.decode(
        token,
        key,
        algorithms=[algorithm],
        issuer=issuer,
        audience=audience,
        options={"require": ["sub", "iss", "aud", "exp", "iat", "jti"]},
    )

    assert claims["sub"] == _User.user_id


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("algorithm", ALGORITHMS)
async def test_a_wrong_pair_is_rejected_by_a_decoder(algorithm, kind):
    """A mismatched pair fails a decoder configured the standard way."""
    issuer, audience = PAIRS["deployment-custom"]
    generator, key = _build(algorithm, issuer, audience)
    token = await _mint(generator, kind)

    with pytest.raises(jwt.InvalidIssuerError):
        jwt.decode(
            token, key, algorithms=[algorithm], issuer="someone-else", audience=audience
        )

    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(
            token, key, algorithms=[algorithm], issuer=issuer, audience="someone-else"
        )


@pytest.mark.parametrize("wrong", ["issuer", "audience"])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("algorithm", ALGORITHMS)
async def test_the_validator_itself_refuses_a_foreign_pair(algorithm, kind, wrong):
    """The generator's own validator does the checking — not just some decoder.

    The accept assertions above are satisfied by a validator that verifies
    nothing, so on their own they would stay green if ``validate_access_token``
    grew a ``verify_aud: False`` or dropped its ``issuer=`` argument. The
    sibling test one level up cannot see that either: it calls ``jwt.decode``
    directly and so measures PyJWT rather than the code under test.

    This closes that hole. Two generators are built over the **same signing
    key** and differ only in the configured pair, so the token presented here is
    perfectly signed and correctly typed — the pair is the single reason it must
    be refused, and ``None`` can mean nothing else.
    """
    issuer, audience = PAIRS["deployment-custom"]
    ours, key = _build(algorithm, issuer, audience)

    foreign_issuer = "someone-else" if wrong == "issuer" else issuer
    foreign_audience = "someone-else" if wrong == "audience" else audience

    # Same key material, different configured pair.
    if algorithm == "HS256":
        theirs = HS256JWTTokenGenerator(
            secret_key=SECRET,
            revocation_store=InMemoryRevocationStore(),
            access_token_expire_minutes=ACCESS_MINUTES,
            refresh_token_expire_days=REFRESH_DAYS,
            issuer=foreign_issuer,
            audience=foreign_audience,
        )
    else:
        theirs = RS256JWTTokenGenerator(
            private_key=ours.private_key,
            public_key=ours.public_key,
            revocation_store=InMemoryRevocationStore(),
            access_token_expire_minutes=ACCESS_MINUTES,
            refresh_token_expire_days=REFRESH_DAYS,
            issuer=foreign_issuer,
            audience=foreign_audience,
        )

    foreign_token = await _mint(theirs, kind)

    assert await _validate(ours, kind, foreign_token) is None, (
        f"{algorithm} {kind}: a token carrying a foreign {wrong} was accepted — "
        "the validator is not checking the pair it was configured with"
    )


@pytest.mark.parametrize("omitted", ["issuer", "audience", "both"])
@pytest.mark.parametrize("algorithm", ALGORITHMS)
async def test_a_generator_cannot_be_built_without_the_pair(algorithm, omitted):
    """No local default to fall back to — for *either* half of the pair.

    Both generators used to default to ``"faultmaven"``/``"faultmaven-api"``,
    which are not the settings defaults — so a caller that said nothing got a
    generator disagreeing with every other decoder in the deployment. Omission
    now fails at construction instead of at some later validation.

    Each half is omitted separately, not just both together. A default restored
    on ``audience`` alone is the nastier of the two: ``"faultmaven-api"`` is the
    settings default *issuer*, so such a generator mints ``aud="faultmaven-api"``
    while ``AuthService.verify_token`` requires ``"faultmaven-app"`` — half of
    #938 reintroduced, on every request. Asserting only that the error names
    ``issuer`` would not see it, because the both-omitted message names
    ``issuer`` too.
    """
    issuer, audience = PAIRS["deployment-custom"]
    kwargs = {
        "revocation_store": InMemoryRevocationStore(),
        "access_token_expire_minutes": ACCESS_MINUTES,
        "refresh_token_expire_days": REFRESH_DAYS,
    }
    if omitted != "both":
        # Supply the half that is not under test.
        kwargs["audience" if omitted == "issuer" else "issuer"] = (
            audience if omitted == "issuer" else issuer
        )

    # Everything that can raise on its own happens before the raises block, so
    # the only TypeError it can catch is the missing-argument one.
    if algorithm == "HS256":
        construct = lambda: HS256JWTTokenGenerator(  # noqa: E731
            secret_key=SECRET, **kwargs
        )
    else:
        private_pem, public_pem = _rsa_pair()
        construct = lambda: RS256JWTTokenGenerator(  # noqa: E731
            private_key=private_pem, public_key=public_pem, **kwargs
        )

    with pytest.raises(TypeError) as excinfo:
        construct()

    expected = ["issuer", "audience"] if omitted == "both" else [omitted]
    for name in expected:
        assert name in str(excinfo.value), (
            f"{algorithm}: omitting {omitted} raised {excinfo.value!r}, which "
            f"does not name {name!r} — the assertion would not catch a default "
            f"restored on {name} alone"
        )


# =============================================================================
# The settings -> generator seam
# =============================================================================


def _settings_stub(issuer, audience):
    """A settings object shaped like the halves the builders actually read."""
    secret = SimpleNamespace(get_secret_value=lambda: SECRET)
    return SimpleNamespace(
        auth=SimpleNamespace(
            jwt_access_token_expire_minutes=ACCESS_MINUTES,
            jwt_refresh_token_expire_days=REFRESH_DAYS,
        ),
        security=SimpleNamespace(
            jwt_secret_key=secret,
            jwt_issuer=issuer,
            jwt_audience=audience,
        ),
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("algorithm", ALGORITHMS)
async def test_the_builders_wire_the_configured_pair_the_right_way_round(
    algorithm, kind
):
    """The builders read the settings keys the property is stated in terms of.

    Everything above builds generators by hand, so all of it stays green if
    ``build_hs256_token_generator`` passes ``jwt_audience`` as the issuer, or
    reintroduces a literal. That seam is where the configured pair actually
    enters the system, and it was covered only incidentally — by tests about
    token *expiry*, which is not where a reader looks for this.

    The pair here is deliberately asymmetric and shares no value with the
    settings defaults or the old hardcodes, so a swap is a failure rather than
    two names that happen to look interchangeable.
    """
    issuer, audience = "acme-idp", "acme-consumers"
    settings = _settings_stub(issuer, audience)

    if algorithm == "HS256":
        generator = build_hs256_token_generator(settings, InMemoryRevocationStore())
    else:
        private_pem, public_pem = _rsa_pair()
        generator = build_rs256_token_generator(
            settings,
            InMemoryRevocationStore(),
            private_key=private_pem,
            public_key=public_pem,
        )

    token = await _mint(generator, kind)
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["iss"] == issuer, f"{algorithm} {kind}: issuer misrouted"
    assert claims["aud"] == audience, f"{algorithm} {kind}: audience misrouted"
    assert await _validate(generator, kind, token) is not None
