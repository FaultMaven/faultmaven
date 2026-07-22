"""Tests for the deployment coherence gate (ADR-004).

The gate makes "a cloud deployment silently running as standalone" impossible:
cloud incoherence is fatal, standalone incoherence only warns.
"""

from types import SimpleNamespace

import pytest

from faultmaven.config.deployment_coherence import (
    DeploymentCoherenceError,
    validate_deployment_coherence,
)


def _cloud_ok() -> SimpleNamespace:
    """A fully-coherent cloud settings stand-in."""
    return SimpleNamespace(
        is_cloud=True,
        auth=SimpleNamespace(
            auth_mode="oauth",
            workos_api_key="sk_test_x",
            workos_client_id="client_x",
            workos_redirect_uri="https://api.example.com/api/v1/auth/sso/callback",
        ),
        security=SimpleNamespace(
            jwt_private_key="-----BEGIN PRIVATE KEY-----x",
            jwt_private_key_path=None,
            jwt_public_key="-----BEGIN PUBLIC KEY-----x",
            jwt_public_key_path=None,
        ),
        database=SimpleNamespace(
            database_url="postgresql+asyncpg://u:p@host:5432/db",
            session_storage_type="redis",
            redis_url=None,
            redis_host="faultmaven-redis-master",
        ),
        providers=SimpleNamespace(
            tenant_provider="single"
        ),  # cloud may be single-tenant
    )


def _standalone_ok() -> SimpleNamespace:
    return SimpleNamespace(
        is_cloud=False,
        auth=SimpleNamespace(auth_mode="local"),
        security=SimpleNamespace(),
        database=SimpleNamespace(),
        providers=SimpleNamespace(),
    )


@pytest.mark.unit
def test_standalone_default_passes():
    validate_deployment_coherence(_standalone_ok())  # must not raise


@pytest.mark.unit
def test_cloud_fully_coherent_passes():
    validate_deployment_coherence(_cloud_ok())  # must not raise


@pytest.mark.unit
def test_cloud_with_local_auth_fails():
    s = _cloud_ok()
    s.auth.auth_mode = "local"  # the production incident
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    assert "AUTH_MODE must be 'oauth'" in str(exc.value)


@pytest.mark.unit
def test_cloud_with_sqlite_fails():
    s = _cloud_ok()
    s.database.database_url = "sqlite+aiosqlite:///./data/faultmaven.db"
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    assert "DATABASE_URL must be PostgreSQL" in str(exc.value)


@pytest.mark.unit
def test_cloud_without_redis_fails():
    s = _cloud_ok()
    s.database.session_storage_type = "inmemory"
    s.database.redis_host = None
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    assert "Redis" in str(exc.value)


@pytest.mark.unit
def test_cloud_single_tenant_is_valid():
    # Single-tenant cloud (one org, many users) is valid — tenancy is an
    # independent axis, not part of the cloud-native infra requirement.
    s = _cloud_ok()
    s.providers.tenant_provider = "single"
    validate_deployment_coherence(s)  # must not raise


@pytest.mark.unit
def test_cloud_missing_rs256_keys_fails():
    s = _cloud_ok()
    s.security.jwt_private_key = None
    s.security.jwt_public_key = None
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    assert "RS256 keys" in str(exc.value)


@pytest.mark.unit
def test_cloud_reports_all_problems_at_once():
    s = _cloud_ok()
    s.auth.auth_mode = "local"
    s.database.database_url = "sqlite:///x"
    s.database.session_storage_type = "inmemory"
    s.database.redis_host = None
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    msg = str(exc.value)
    assert "AUTH_MODE" in msg and "DATABASE_URL" in msg and "Redis" in msg


# --- WorkOS AuthKit requirement (ADR-015 D7: hard-fail since cutover) -------


@pytest.mark.unit
@pytest.mark.security
def test_cloud_without_any_workos_config_fails():
    s = _cloud_ok()
    s.auth.workos_api_key = None
    s.auth.workos_client_id = None
    s.auth.workos_redirect_uri = None
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    msg = str(exc.value)
    assert "WorkOS" in msg
    assert (
        "WORKOS_API_KEY" in msg
        and "WORKOS_CLIENT_ID" in msg
        and "WORKOS_REDIRECT_URI" in msg
    )


@pytest.mark.unit
@pytest.mark.security
def test_cloud_with_empty_workos_client_id_fails():
    # The infra ConfigMap ships WORKOS_CLIENT_ID: "" until cutover — an empty
    # string is NOT configured and must name exactly the missing variable.
    s = _cloud_ok()
    s.auth.workos_client_id = ""
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    msg = str(exc.value)
    assert "WORKOS_CLIENT_ID" in msg
    assert "WORKOS_API_KEY" not in msg and "WORKOS_REDIRECT_URI" not in msg


@pytest.mark.unit
def test_cloud_workos_secretstr_api_key_is_unwrapped():
    # The real settings field is a SecretStr — the gate must read its plain
    # value, not truthiness of the wrapper object.
    class _Secret:
        def __init__(self, value: str) -> None:
            self._value = value

        def get_secret_value(self) -> str:
            return self._value

    s = _cloud_ok()
    s.auth.workos_api_key = _Secret("sk_live_x")
    validate_deployment_coherence(s)  # must not raise

    s.auth.workos_api_key = _Secret("")
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    assert "WORKOS_API_KEY" in str(exc.value)


@pytest.mark.unit
def test_standalone_without_workos_does_not_warn_or_raise():
    # WorkOS is a cloud concern only — its absence must not even warn here.
    validate_deployment_coherence(_standalone_ok())


@pytest.mark.unit
def test_standalone_with_oauth_warns_but_does_not_raise():
    s = _standalone_ok()
    s.auth.auth_mode = "oauth"
    # Standalone incoherence is a warning, not fatal — must not raise.
    validate_deployment_coherence(s)


@pytest.mark.unit
def test_settings_is_cloud_property_reads_deployment_mode(monkeypatch):
    from faultmaven.config.settings import FaultMavenSettings

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    cloud = FaultMavenSettings(_env_file=None)
    assert cloud.is_cloud is True
    assert cloud.is_standalone is False

    monkeypatch.setenv("DEPLOYMENT_MODE", "standalone")
    standalone = FaultMavenSettings(_env_file=None)
    assert standalone.is_cloud is False
    assert standalone.is_standalone is True

    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    assert FaultMavenSettings(_env_file=None).is_standalone is True  # default


# --- Tenancy coherence (ADR-010: single/multi both in-core) -----------------


@pytest.mark.unit
@pytest.mark.security
def test_multi_tenant_outside_cloud_fails_closed():
    """TENANT_PROVIDER=multi requires DEPLOYMENT_MODE=cloud — the cloud checks
    (PostgreSQL, OAuth/RS256, Redis) guarantee the stack its RLS isolation
    relies on, so multi outside cloud must refuse to boot."""
    s = _standalone_ok()
    s.providers = SimpleNamespace(tenant_provider="multi")
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    assert "requires DEPLOYMENT_MODE=cloud" in str(exc.value)


@pytest.mark.unit
def test_multi_tenant_passes_gate_under_cloud():
    """Under a fully-coherent cloud configuration, multi passes the gate."""
    s = _cloud_ok()
    s.providers = SimpleNamespace(tenant_provider="multi")
    validate_deployment_coherence(s)  # must not raise


@pytest.mark.unit
def test_single_tenant_always_passes():
    """The built-in single provider is valid in any deployment mode."""
    s = _standalone_ok()
    s.providers = SimpleNamespace(tenant_provider="single")
    validate_deployment_coherence(s)


@pytest.mark.unit
@pytest.mark.security
def test_unknown_provider_fails_closed():
    """An unrecognized TENANT_PROVIDER value -> refuse to boot."""
    s = _standalone_ok()
    s.providers = SimpleNamespace(tenant_provider="bogus")
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    assert "not a recognized provider" in str(exc.value)
