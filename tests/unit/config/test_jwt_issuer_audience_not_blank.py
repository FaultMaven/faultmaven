"""A blank issuer/audience is refused at startup, not at first login (#938).

Since #938 these two settings are load-bearing: every mint stamps them, every
decode checks them, and no hardcoded fallback remains. Pydantic accepts
``JWT_AUDIENCE=`` from the environment as the empty string without complaint, so
before this validator a deployment booted clean and failed every authentication
afterwards.

**The two fields are not symmetric at runtime, and this suite states which is
which so no reader has to guess.** PyJWT treats a falsy ``aud`` *in the payload*
as absent and raises ``MissingRequiredClaimError``, so a blank audience makes a
generator reject the tokens it just minted. A blank *issuer* compares equal to
itself and is silently functional. ``test_only_a_blank_audience_would_have_
broken_auth`` pins that asymmetry against PyJWT directly, so the validator's
docstring cannot quietly drift into claiming both fields break auth.

Both are refused regardless — a blank issuer is unintended in every case, and a
rule holding for one half of a pair is one an operator misremembers.
"""

import os

import jwt
import pytest
from pydantic import ValidationError

from faultmaven.config.settings import SecuritySettings

#: Values a human would read as "blank". PyJWT does not strip, so `" "` is
#: truthy to it — a bare falsiness check in the validator would admit it.
BLANK = ["", " ", "   ", "\t", "\n"]

FIELDS = ["jwt_issuer", "jwt_audience"]

#: Every environment name that binds these fields. ``SecuritySettings`` uses an
#: empty ``env_prefix``, so a plain ``JWT_ISSUER`` exported in a developer's or
#: CI shell reaches them — and pydantic-settings binds case-insensitively.
BINDING_ENV_NAMES = ["JWT_ISSUER", "JWT_AUDIENCE"]


@pytest.fixture(autouse=True)
def _no_ambient_pair(monkeypatch):
    """No ambient configuration decides the outcome of these tests.

    Every assertion here is about what the *code* does with a given value, so an
    exported ``JWT_ISSUER`` — which reaches these fields, ``.env`` included,
    since dotenv loads into ``os.environ`` — would otherwise silently substitute
    a different input and fail tests that have nothing to do with it.
    """
    for name in list(os.environ):
        if name.upper() in BINDING_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("blank", BLANK)
@pytest.mark.parametrize("field", FIELDS)
def test_a_blank_value_is_refused_at_construction(field, blank):
    """Each field, each spelling of blank — refused before anything boots."""
    with pytest.raises(ValidationError) as excinfo:
        SecuritySettings(**{field: blank})

    message = str(excinfo.value)
    assert field.upper() in message, (
        f"{field}={blank!r} was refused, but the error does not name "
        f"{field.upper()}, so an operator cannot tell which setting to fix: "
        f"{message}"
    )


@pytest.mark.parametrize("field", FIELDS)
def test_a_non_blank_value_is_accepted(field):
    """The validator narrows; it does not reject ordinary configuration."""
    settings = SecuritySettings(**{field: "acme-idp"})
    assert getattr(settings, field) == "acme-idp"


def test_the_defaults_survive_the_validator():
    """The shipped defaults are themselves validated, and pass.

    The fields declare ``validate_default=True`` precisely so this is a real
    assertion. Pydantic skips validators on unset fields by default, so without
    it this would construct the model, read two constants back, and prove only
    that they are spelled as written — a blank default would sail past the check
    written to prevent a blank value.
    """
    settings = SecuritySettings()

    assert settings.jwt_issuer == "faultmaven"
    assert settings.jwt_audience == "faultmaven-api"

    # The declaration is asserted directly because a blank *default* is not
    # reachable from a test: overriding it in a subclass would restate
    # ``validate_default`` and so could not detect its removal from the real
    # field. Without this, dropping the flag regresses silently — the values
    # above are non-blank, so they pass whether or not anything checked them.
    for field in FIELDS:
        assert SecuritySettings.model_fields[field].validate_default is True, (
            f"{field} no longer validates its default, so the blank-value rule "
            "does not cover the shipped configuration"
        )


@pytest.mark.parametrize(
    "padded,expected",
    [
        (" faultmaven-api", "faultmaven-api"),
        ("faultmaven-api ", "faultmaven-api"),
        ("\tfaultmaven-api\n", "faultmaven-api"),
    ],
)
@pytest.mark.parametrize("field", FIELDS)
def test_surrounding_whitespace_is_stripped_from_the_stored_value(
    field, padded, expected
):
    """The deployment uses the trimmed value, not the raw one.

    PyJWT compares ``iss`` by equality and matches ``aud`` exactly, so a stored
    ``"faultmaven-api "`` is a *different* audience that every token then
    carries. It is self-consistent, so nothing fails — until the space is
    removed and every token in circulation stops verifying. Neither a Kubernetes
    ConfigMap value nor a Compose ``environment:`` entry trims, and the space is
    invisible in both.
    """
    settings = SecuritySettings(**{field: padded})

    assert getattr(settings, field) == expected


@pytest.mark.parametrize(
    "issuer,audience,expect_broken",
    [
        ("faultmaven", "", True),  # blank audience: MissingRequiredClaim
        ("", "faultmaven-api", False),  # blank issuer: silently functional
    ],
)
def test_only_a_blank_audience_would_have_broken_auth(issuer, audience, expect_broken):
    """Pin the asymmetry the validator's docstring describes.

    This deliberately bypasses the validator and asks PyJWT directly, because
    the claim being checked is about PyJWT's behaviour, not FaultMaven's. If a
    future PyJWT starts rejecting a blank issuer too, this fails and the
    validator's prose gets corrected — rather than the prose staying wrong and
    nobody noticing, which is how the #938 docstrings went stale in the first
    place.
    """
    claims = {
        "sub": "u",
        "iss": issuer,
        "aud": audience,
        "jti": "j",
        "iat": 1700000000,
        "exp": 4102444800,
    }
    token = jwt.encode(claims, "secret-key-for-this-unit-test-only", algorithm="HS256")

    try:
        jwt.decode(
            token,
            "secret-key-for-this-unit-test-only",
            algorithms=["HS256"],
            issuer=issuer,
            audience=audience,
        )
        broken = False
    except jwt.InvalidTokenError:
        broken = True

    assert broken is expect_broken
