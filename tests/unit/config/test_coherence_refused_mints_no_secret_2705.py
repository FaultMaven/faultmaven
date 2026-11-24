"""Settings the coherence gate refuses mint no local HS256 secret (#2705).

``build_settings`` gives local auth its secret (``resolve_local_jwt_secret``)
before the deployment coherence gate runs. The gate refuses local auth on a
cloud deployment, and ``TENANT_PROVIDER=multi`` outside cloud, from the
settings alone, so a secret minted there was written for a run that never
starts, onto a volume whose corrected run does not use it. One predicate,
``coherence_refuses_local_auth``, carries both rules to the gate and the mint.
Every settings object here is real, built from a controlled environment.
"""

import os
import stat
from types import SimpleNamespace

import pytest

from faultmaven.config import settings as S
from faultmaven.config.deployment_coherence import (
    DeploymentCoherenceError,
    coherence_refuses_local_auth,
    validate_deployment_coherence,
)
from faultmaven.models.exceptions import ConfigurationError
from tests.utils import delenv_every_spelling

pytestmark = pytest.mark.unit

#: Every name the outcome reads, removed in every letter case (the settings
#: bind them case-insensitively), the nested JSON sections included.
_ISOLATED_NAMES = (
    "AUTH",
    "AUTH_MODE",
    "CONFIG_PRESET",
    "DATABASE",
    "DATABASE_URL",
    "DEPLOYMENT_MODE",
    "JWT_PRIVATE_KEY",
    "JWT_SECRET_FILE",
    "JWT_SECRET_KEY",
    "OAUTH_ENABLED",
    "PROVIDERS",
    "SECURITY",
    "TENANT_PROVIDER",
)

_CLOUD_REFUSAL = "AUTH_MODE must be 'oauth' for cloud"
_MULTI_REFUSAL = "TENANT_PROVIDER='multi' requires DEPLOYMENT_MODE=cloud"


@pytest.fixture
def restored_environ():
    """The process environment, restored whole: a preset writes ``os.environ``
    without ``monkeypatch``, and the mint exports ``JWT_SECRET_KEY``."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def secret_file(restored_environ, monkeypatch, tmp_path):
    """Local auth, no preset, a persistent SQLite file database, and the secret
    file under ``tmp_path``: the arm that mints and persists. Returns the file."""
    delenv_every_spelling(monkeypatch, *_ISOLATED_NAMES)
    path = tmp_path / "data" / ".jwt_secret"
    monkeypatch.setenv("CONFIG_PRESET", "none")
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.setenv("JWT_SECRET_FILE", str(path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/fm.db")
    return path


def _assert_no_secret(settings, secret_file) -> None:
    assert not settings.security.jwt_secret_key
    assert S.env_var_spellings(os.environ, "JWT_SECRET_KEY") == []
    assert not secret_file.parent.exists(), sorted(secret_file.parent.rglob("*"))


@pytest.mark.parametrize(
    "env, refusal",
    [
        pytest.param({"DEPLOYMENT_MODE": "cloud"}, _CLOUD_REFUSAL, id="cloud"),
        pytest.param(
            {"DEPLOYMENT_MODE": "cloud", "TENANT_PROVIDER": "multi"},
            _CLOUD_REFUSAL,
            id="cloud-multi",
        ),
        pytest.param(
            {"TENANT_PROVIDER": "multi"}, _MULTI_REFUSAL, id="multi-standalone"
        ),
        # #2703 made the preset a cloud deployment; it sets no AUTH_MODE.
        pytest.param({"CONFIG_PRESET": "enterprise"}, _CLOUD_REFUSAL, id="enterprise"),
    ],
)
def test_a_refused_local_auth_run_mints_nothing(env, refusal, secret_file, monkeypatch):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    settings = S.build_settings()

    with pytest.raises(DeploymentCoherenceError) as refused:
        validate_deployment_coherence(settings)
    assert refusal in str(refused.value)
    _assert_no_secret(settings, secret_file)


def test_a_secret_file_already_on_a_cloud_volume_is_left_exactly_as_it_was(
    secret_file, monkeypatch
):
    """Not read, not tightened, not deleted: an operator removes it (ruling A)."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    secret_file.parent.mkdir()
    secret_file.write_bytes(b"left-by-an-earlier-refused-boot-2705\n")
    secret_file.chmod(0o644)

    settings = S.build_settings()

    assert not settings.security.jwt_secret_key
    assert S.env_var_spellings(os.environ, "JWT_SECRET_KEY") == []
    assert secret_file.read_bytes() == b"left-by-an-earlier-refused-boot-2705\n"
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o644


def test_an_explicit_key_is_still_judged_under_cloud(secret_file, monkeypatch):
    """The skip comes after the explicit key's judging (#1806)."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv(
        "JWT_SECRET_KEY",
        "-----BEGIN PUBLIC KEY-----\nMIIB2705\n-----END PUBLIC KEY-----",
    )

    with pytest.raises(ConfigurationError, match="PyJWT refuses it as an HMAC secret"):
        S.build_settings()
    assert not secret_file.parent.exists()


def test_cloud_with_local_auth_builds_no_hs256_signer(monkeypatch):
    """What an operator command reaching the signer meets: a refusal, never a
    token signed with a per-process secret."""
    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        SigningKeyUnavailableError,
        build_jwt_token_generator,
    )

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")

    with pytest.raises(SigningKeyUnavailableError):
        build_jwt_token_generator(S.build_settings(), None)


@pytest.mark.parametrize("deployment", ["cloud", "standalone"])
@pytest.mark.parametrize("auth", ["local", "oauth"])
@pytest.mark.parametrize("tenancy", ["single", "multi"])
def test_the_mint_skips_exactly_what_the_gate_refuses_on_these_rules(
    deployment, auth, tenancy, secret_file, monkeypatch
):
    monkeypatch.setenv("DEPLOYMENT_MODE", deployment)
    monkeypatch.setenv("AUTH_MODE", auth)
    monkeypatch.setenv("OAUTH_ENABLED", str(auth == "oauth").lower())
    monkeypatch.setenv("TENANT_PROVIDER", tenancy)

    settings = S.build_settings()
    skipped = coherence_refuses_local_auth(settings)

    assert skipped is (
        (deployment == "cloud" and auth == "local")
        or (deployment == "standalone" and tenancy == "multi")
    )
    if skipped:
        with pytest.raises(DeploymentCoherenceError) as refused:
            validate_deployment_coherence(settings)
        rule = _CLOUD_REFUSAL if deployment == "cloud" else _MULTI_REFUSAL
        assert rule in str(refused.value)
    if auth == "local":
        assert bool(settings.security.jwt_secret_key) is not skipped
        assert secret_file.is_file() is not skipped


@pytest.mark.parametrize(
    "value",
    [None, "single", "multi", "MULTI", S.TenantProvider.SINGLE, S.TenantProvider.MULTI],
)
def test_the_multi_rule_names_the_provider_as_the_factory_does(value):
    from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, coerce_provider_name

    standalone = SimpleNamespace(
        is_cloud=False,
        auth=SimpleNamespace(auth_mode=S.AuthMode.LOCAL),
        providers=SimpleNamespace(tenant_provider=value),
    )

    assert coherence_refuses_local_auth(standalone) is (
        coerce_provider_name(value) == BUILTIN_MULTI
    )
