"""Tests for the deployment coherence gate (ADR-004).

The gate makes "a cloud deployment silently running as standalone" impossible:
cloud incoherence is fatal, standalone incoherence only warns.
"""

from enum import Enum
from types import SimpleNamespace

import pytest

from faultmaven.config.deployment_coherence import (
    DeploymentCoherenceError,
    validate_deployment_coherence,
)
from faultmaven.config.settings import AuthMode, StorageBackend


def _cloud_ok() -> SimpleNamespace:
    """A fully-coherent cloud settings stand-in.

    ``auth_mode`` is the REAL ``AuthMode`` enum, because that is what pydantic
    hands the gate at boot. Feeding a bare ``"oauth"`` string here is what let
    #881 hide: the gate compared ``str(member)`` (``'AuthMode.OAUTH'``) and so
    refused every real cloud boot while this suite stayed green.
    """
    return SimpleNamespace(
        is_cloud=True,
        auth=SimpleNamespace(
            auth_mode=AuthMode.OAUTH,
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
        auth=SimpleNamespace(auth_mode=AuthMode.LOCAL),
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
    s.auth.auth_mode = AuthMode.LOCAL  # the production incident
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
    s.auth.auth_mode = AuthMode.LOCAL
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
def test_standalone_with_oauth_warns_but_does_not_raise(caplog):
    s = _standalone_ok()
    s.auth.auth_mode = AuthMode.OAUTH
    # Standalone incoherence is a warning, not fatal — must not raise.
    with caplog.at_level("WARNING"):
        validate_deployment_coherence(s)

    # ...but it MUST actually warn. Under `str(member)` this branch was dead
    # code (#881): the test passed by never asserting the warning existed.
    assert any("AUTH_MODE=oauth" in r.getMessage() for r in caplog.records)


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


@pytest.mark.unit
@pytest.mark.security
def test_cloud_with_filesystem_storage_fails_closed():
    """Filesystem storage in cloud must refuse to boot.

    Cloud replicas sharing one RWX volume make that volume a single point of
    failure for all evidence I/O (#689). This shipped as a warning while the
    RWX deployment was still running; with the object-storage migration done
    (infra#127) it is fatal, so the SPOF cannot be reintroduced silently.
    """
    s = _cloud_ok()
    s.providers.storage_backend = StorageBackend.FILESYSTEM

    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    assert "STORAGE_BACKEND=filesystem" in str(exc.value)


@pytest.mark.unit
def test_cloud_with_object_storage_does_not_warn(caplog):
    s = _cloud_ok()
    s.providers.storage_backend = StorageBackend.S3
    s.evidence_storage = SimpleNamespace(s3_bucket_name="faultmaven-evidence")

    with caplog.at_level("WARNING"):
        validate_deployment_coherence(s)

    assert not any("STORAGE_BACKEND" in r.message for r in caplog.records)


@pytest.mark.unit
def test_cloud_s3_without_bucket_fails():
    """STORAGE_BACKEND=s3 with no bucket must not boot.

    The container builds storage fail-soft, so without this gate a bucket-less
    S3 config boots healthy and only fails when a user uploads evidence.
    """
    s = _cloud_ok()
    s.providers.storage_backend = StorageBackend.S3
    s.evidence_storage = SimpleNamespace(s3_bucket_name=None)

    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    assert "S3_BUCKET_NAME" in str(exc.value)


@pytest.mark.unit
def test_cloud_s3_with_bucket_passes():
    s = _cloud_ok()
    s.providers.storage_backend = StorageBackend.S3
    s.evidence_storage = SimpleNamespace(s3_bucket_name="faultmaven-evidence")

    validate_deployment_coherence(s)  # must not raise


@pytest.mark.unit
def test_storage_backend_name_reads_the_real_enum():
    """Guard the stringification that made both storage gates inert.

    `StorageBackend` is a `(str, Enum)`, so `str(member)` is
    'StorageBackend.S3' — not 's3'. Both gates originally matched on that and
    silently never fired, while tests that hand-wrote a lowercase string
    passed. This asserts against the real enum members.
    """
    from faultmaven.config.deployment_coherence import _storage_backend_name

    for member, expected in (
        (StorageBackend.S3, "s3"),
        (StorageBackend.FILESYSTEM, "filesystem"),
    ):
        s = SimpleNamespace(providers=SimpleNamespace(storage_backend=member))
        assert _storage_backend_name(s) == expected

    # A plain-string override (env/test config) must resolve identically.
    plain = SimpleNamespace(providers=SimpleNamespace(storage_backend="S3"))
    assert _storage_backend_name(plain) == "s3"


# --- AUTH_MODE must be read as the enum's value, not str(member) (#881) ------


@pytest.mark.unit
def test_auth_mode_name_reads_the_real_enum():
    """`AuthMode` is a `(str, Enum)`: `str(member)` is 'AuthMode.OAUTH', not
    'oauth'. The gate compared on that, so the cloud auth check refused every
    correct cloud boot and the standalone warning was dead code (#881)."""
    from faultmaven.config.deployment_coherence import _auth_mode_name

    for member, expected in (
        (AuthMode.OAUTH, "oauth"),
        (AuthMode.LOCAL, "local"),
    ):
        assert _auth_mode_name(SimpleNamespace(auth_mode=member)) == expected

    # Plain-string and absent-attribute configs must resolve identically.
    assert _auth_mode_name(SimpleNamespace(auth_mode="OAuth")) == "oauth"
    assert _auth_mode_name(SimpleNamespace()) == "local"


@pytest.mark.unit
def test_pydantic_really_hands_the_gate_an_authmode_member():
    """Anchor the stand-in to reality: the settings field is typed `AuthMode`,
    so a coherent cloud boot passes the gate an ENUM MEMBER — which is exactly
    the input the old `str()` comparison could never satisfy."""
    from faultmaven.config.settings import AuthSettings

    parsed = AuthSettings(auth_mode="oauth", oauth_enabled=True, _env_file=None)
    # NOT `is AuthMode.OAUTH`: other tests in the full suite reload
    # faultmaven.config.settings, so this member and the imported class can be
    # duplicate enum objects — identity is a module-lifecycle detail, not the
    # property under test. What matters: pydantic yields a MEMBER (not a raw
    # string), whose str() is the trap and whose value-equality is the fix.
    assert isinstance(parsed.auth_mode, Enum)
    assert type(parsed.auth_mode) is not str
    assert str(parsed.auth_mode) != "oauth"  # the trap, pinned
    assert parsed.auth_mode == "oauth"  # str-mixin equality is the fix


@pytest.mark.unit
def test_cloud_with_authmode_enum_raises_no_auth_mode_problem():
    """A coherent cloud config carrying the real enum must produce NO AUTH_MODE
    complaint. This is the #881 regression: the staging flip rehearsal booted
    with AUTH_MODE=oauth parsed into `AuthMode.OAUTH` and was refused."""
    from faultmaven.config.deployment_coherence import _check_cloud

    s = _cloud_ok()
    s.auth.auth_mode = AuthMode.OAUTH
    assert not [p for p in _check_cloud(s) if "AUTH_MODE" in p]

    # And the whole gate must accept it.
    validate_deployment_coherence(s)  # must not raise


@pytest.mark.unit
def test_cloud_auth_mode_gate_accepts_enum_and_string_alike():
    """Both the parsed enum and a raw 'oauth' string are valid cloud auth; only
    local is refused. Sweep the input space rather than one instance."""
    from faultmaven.config.deployment_coherence import _check_cloud

    for accepted in (AuthMode.OAUTH, "oauth"):
        s = _cloud_ok()
        s.auth.auth_mode = accepted
        assert not [
            p for p in _check_cloud(s) if "AUTH_MODE" in p
        ], f"{accepted!r} must be accepted as cloud auth"

    for refused in (AuthMode.LOCAL, "local"):
        s = _cloud_ok()
        s.auth.auth_mode = refused
        problems = [p for p in _check_cloud(s) if "AUTH_MODE" in p]
        assert problems, f"{refused!r} must be refused as cloud auth"
        # The operator-facing message must name the mode readably — never
        # 'AuthMode.LOCAL' leaking the Python repr into a boot error.
        assert "got 'local'" in problems[0]


@pytest.mark.unit
def test_cloud_with_authmode_local_enum_fails_closed():
    """The dangerous direction: local auth on cloud must still refuse to boot
    when it arrives as the real enum."""
    s = _cloud_ok()
    s.auth.auth_mode = AuthMode.LOCAL
    with pytest.raises(DeploymentCoherenceError) as exc:
        validate_deployment_coherence(s)
    assert "AUTH_MODE must be 'oauth'" in str(exc.value)
