"""Partial RSA key configuration must refuse, not fabricate (#853).

`AuthService._load_keys` used to generate a development key pair whenever
*either* half was missing, and `_generate_dev_keys` overwrites **both**. So a
deployment that configured `JWT_PUBLIC_KEY` and forgot `JWT_PRIVATE_KEY` had its
configured public key silently replaced by a random one: the service came up
looking healthy and rejected every token the real signer minted, with a single
log line as the only signal.

`config.deployment_coherence._check_cloud` already refuses this at boot — but
only when `DEPLOYMENT_MODE=cloud`. Every other mode fabricated.

The four states, and why each is pinned:

* private only / public only — refuse, naming the missing half, and **fabricate
  nothing**. The assertions check the key attributes, not just the exception:
  "raises but mutates anyway" is precisely the failure mode here, since the old
  code's whole defect was a write nobody asked for.
* neither — development keys, as before. This is the genuine local path and the
  refusal must not swallow it.
* both — used verbatim, byte for byte.
"""

from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from faultmaven.modules.auth.domain.services.auth_service import (
    AuthService,
    PartialKeyConfigurationError,
)

SECRET = "test-secret-key-0123456789abcdef"  # 32+ bytes: HS256 minimum


def _keypair():
    """An ephemeral pair. Never a committed key."""
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


def _settings(*, private=None, public=None, private_path=None, public_path=None):
    settings = MagicMock()
    settings.auth.auth_mode = "oauth"
    settings.security.jwt_algorithm = "RS256"
    settings.auth.jwt_access_token_expire_minutes = 15
    settings.auth.jwt_refresh_token_expire_days = 7
    settings.security.jwt_issuer = "faultmaven"
    settings.security.jwt_audience = "faultmaven-api"
    settings.security.token_revocation_prefix = "revoked:token:"
    settings.security.jwt_private_key = (
        MagicMock(get_secret_value=MagicMock(return_value=private)) if private else None
    )
    settings.security.jwt_public_key = public
    settings.security.jwt_private_key_path = private_path
    settings.security.jwt_public_key_path = public_path
    settings.security.jwt_secret_key = MagicMock()
    settings.security.jwt_secret_key.get_secret_value.return_value = SECRET
    return settings


def _build(settings):
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=settings,
    ):
        return AuthService()


# ---------------------------------------------------------------------------
# Partial configuration refuses, and writes nothing
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("configured_half", ["private", "public"])
def test_partial_key_configuration_is_refused(configured_half):
    """One half configured => refuse, name the missing half, fabricate nothing."""
    private_pem, public_pem = _keypair()
    if configured_half == "private":
        settings = _settings(private=private_pem)
        expected_missing, expected_env = "public", "JWT_PUBLIC_KEY"
    else:
        settings = _settings(public=public_pem)
        expected_missing, expected_env = "private", "JWT_PRIVATE_KEY"

    with pytest.raises(PartialKeyConfigurationError) as refusal:
        _build(settings)

    message = str(refusal.value)
    assert expected_missing in message, message
    assert expected_env in message, message
    # The refusal must say what it declined to do, or the operator reads it as
    # a generic misconfiguration and looks in the wrong place.
    assert "development" in message.lower(), message


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("configured_half", ["private", "public"])
def test_a_refused_load_fabricates_no_key_material(configured_half):
    """The assertion the exception alone does not make.

    The defect was a *write*: generation replaced both halves. A service that
    raises after fabricating would satisfy the test above and still have
    clobbered the configured key on the instance the caller may yet hold. So
    inspect the attributes, not the control flow.
    """
    private_pem, public_pem = _keypair()
    settings = _settings(
        private=private_pem if configured_half == "private" else None,
        public=public_pem if configured_half == "public" else None,
    )

    service = AuthService.__new__(AuthService)  # no __init__: nothing loaded yet
    service._settings = settings
    service._revocation_store = None
    service._private_key = None
    service._public_key = None

    with pytest.raises(PartialKeyConfigurationError):
        service._load_keys()

    if configured_half == "private":
        assert service._private_key == private_pem, "configured key was mutated"
        assert service._public_key is None, "a public key was fabricated"
    else:
        assert service._public_key == public_pem, "configured key was mutated"
        assert service._private_key is None, "a private key was fabricated"


@pytest.mark.unit
@pytest.mark.security
def test_a_named_but_missing_key_file_is_configured_and_broken():
    """A path that does not resolve is not "unconfigured".

    Treating it as unconfigured is what let a typo in `JWT_PUBLIC_KEY_PATH` fall
    through to fabrication with only a warning.
    """
    private_pem, _ = _keypair()
    settings = _settings(
        private=private_pem, public_path="/nonexistent/definitely-not-here.pub"
    )

    with pytest.raises(PartialKeyConfigurationError):
        _build(settings)


# ---------------------------------------------------------------------------
# The two states that must keep working
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_configuration_at_all_still_generates_development_keys():
    """The genuine local path, unchanged.

    Nothing declared — no string, no path, no constructor argument — selects
    development keys deliberately, and the refusal must not swallow that.
    """
    service = _build(_settings())

    assert service._private_key is not None
    assert service._public_key is not None
    assert "BEGIN PRIVATE KEY" in service._private_key
    assert "BEGIN PUBLIC KEY" in service._public_key


@pytest.mark.unit
@pytest.mark.security
def test_a_complete_pair_is_used_verbatim():
    """Both halves configured => neither is touched."""
    private_pem, public_pem = _keypair()

    service = _build(_settings(private=private_pem, public=public_pem))

    assert service._private_key == private_pem
    assert service._public_key == public_pem


@pytest.mark.unit
@pytest.mark.security
def test_constructor_supplied_pair_is_used_verbatim():
    """The container passes keys positionally; that path must not regress."""
    private_pem, public_pem = _keypair()

    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=_settings(),
    ):
        service = AuthService(private_key=private_pem, public_key=public_pem)

    assert service._private_key == private_pem
    assert service._public_key == public_pem


# ---------------------------------------------------------------------------
# The refusal must reach the operator
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_the_container_factory_does_not_degrade_on_a_partial_pair():
    """`create_auth_service` wraps construction in `except Exception` and returns
    a fallback service. That handler must not absorb this refusal: the fallback
    builds the same service from the same settings, so it cannot succeed where
    this failed — it would only re-raise from inside a handler and bury the
    cause under a chained traceback.
    """
    from faultmaven.container.providers.services import create_auth_service

    private_pem, _ = _keypair()
    settings = _settings(private=private_pem)

    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=settings,
    ):
        with pytest.raises(PartialKeyConfigurationError):
            create_auth_service(None, settings)


@pytest.mark.unit
@pytest.mark.security
def test_the_refusal_survives_the_lenient_composition_path():
    """`compose_application` re-raises `RuntimeError` in every deployment mode and
    degrades on anything else, so the lineage is load-bearing, not decorative.
    """
    assert issubclass(PartialKeyConfigurationError, RuntimeError)
