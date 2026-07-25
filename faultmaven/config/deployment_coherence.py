"""Deployment coherence gate (ADR-004).

Asserts that the running configuration is coherent with the canonical
``DEPLOYMENT_MODE``. A ``cloud`` deployment MUST present cloud-native identity
(OAuth auth + RS256 keys, WorkOS AuthKit SSO, PostgreSQL, real Redis); otherwise
the app refuses to boot rather than silently running as ``standalone`` on cloud
infrastructure.

Tenancy (``TENANT_PROVIDER`` single/multi) is config-selected in the core
(ADR-010) — both providers are in-core. ``multi`` requires
``DEPLOYMENT_MODE=cloud`` and fails closed outside it
(see ``_check_tenant_provider_coherent``).

This closes the failure mode where a cloud k8s deployment whose Secret carried
``AUTH_MODE=local`` was silently treated as standalone (auth bypassed, DB
overrides skipped, dashboard read-only) — see
``docs/architecture/specifications/llm-configuration-design.md`` (Deployment
Coherence Gate) and ADR-004 in ``faultmaven-doc-internal``.
"""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)


class DeploymentCoherenceError(RuntimeError):
    """Raised at startup when configuration contradicts ``DEPLOYMENT_MODE``."""


def _plain(obj: Any, name: str) -> str:
    """Return a str/SecretStr field's plain value, or '' if unset."""
    val = getattr(obj, name, None)
    if val is None:
        return ""
    getter = getattr(val, "get_secret_value", None)
    return getter() if callable(getter) else str(val)


def _storage_backend_name(settings: Any) -> str:
    """Return the configured storage backend as a plain lowercase name.

    ``StorageBackend`` is a ``(str, Enum)``, so ``str(member)`` yields
    ``'StorageBackend.S3'`` — matching on that is how both storage gates below
    were silently never firing. Read ``.value`` when present, and normalize so
    a plain-string override matches too.
    """
    backend = getattr(getattr(settings, "providers", None), "storage_backend", None)
    if backend is None:
        return ""
    return str(getattr(backend, "value", backend)).strip().lower()


def _check_cloud(settings: Any) -> List[str]:
    """Return the list of reasons the config is NOT a valid cloud deployment."""
    problems: List[str] = []
    auth = settings.auth
    db = settings.database
    security = settings.security

    # 1. Auth must be OAuth (RS256), not the local bypass.
    auth_mode = str(getattr(auth, "auth_mode", "local"))
    if auth_mode != "oauth":
        problems.append(
            f"AUTH_MODE must be 'oauth' for cloud (got '{auth_mode}'). Local auth "
            "bypasses authentication — invalid on a multi-tenant cloud deployment."
        )
    else:
        has_private = bool(_plain(security, "jwt_private_key")) or bool(
            getattr(security, "jwt_private_key_path", None)
        )
        has_public = bool(_plain(security, "jwt_public_key")) or bool(
            getattr(security, "jwt_public_key_path", None)
        )
        if not (has_private and has_public):
            problems.append(
                "OAuth (cloud) requires RS256 keys: set JWT_PRIVATE_KEY(_PATH) and "
                "JWT_PUBLIC_KEY(_PATH)."
            )

    # 2. Database must be PostgreSQL.
    if "postgresql" not in _plain(db, "database_url"):
        problems.append(
            "DATABASE_URL must be PostgreSQL for cloud (got a non-postgresql URL — "
            "likely the SQLite default). SQLite is single-writer and standalone-only."
        )

    # 3. Sessions must be real Redis.
    session_type = str(getattr(db, "session_storage_type", "inmemory") or "inmemory")
    has_redis = bool(getattr(db, "redis_url", None)) or bool(
        getattr(db, "redis_host", None)
    )
    if session_type != "redis" or not has_redis:
        problems.append(
            "Cloud requires real Redis sessions: set SESSION_STORAGE_TYPE=redis with "
            "REDIS_URL or REDIS_HOST. FakeRedis is ephemeral and standalone-only."
        )

    # 4. The hosted identity provider must be configured (ADR-015 D7). Cloud
    # sign-in is WorkOS AuthKit only — without it no user can log in, and the
    # deployment would sit dark while looking healthy. Hard requirement since
    # the WorkOS cutover (it shipped as a warning during rollout).
    missing = [
        env_name
        for env_name, field in (
            ("WORKOS_API_KEY", "workos_api_key"),
            ("WORKOS_CLIENT_ID", "workos_client_id"),
            ("WORKOS_REDIRECT_URI", "workos_redirect_uri"),
        )
        if not _plain(auth, field)
    ]
    if missing:
        problems.append(
            "Cloud requires the WorkOS AuthKit identity provider (ADR-015): set "
            f"{', '.join(missing)}. Without a hosted IdP no user can sign in to "
            "a cloud deployment (see faultmaven-enterprise-infra "
            "docs/operations/workos-sso-setup.md)."
        )

    # 5. Object storage, if selected, must be usable. The container builds the
    # storage backend fail-soft (evidence tools degrade rather than crash the
    # process), so a bucket-less STORAGE_BACKEND=s3 would otherwise boot and
    # only surface when a user tries to upload evidence.
    if _storage_backend_name(settings) == "s3" and not _plain(
        getattr(settings, "evidence_storage", None), "s3_bucket_name"
    ):
        problems.append(
            "STORAGE_BACKEND=s3 requires S3_BUCKET_NAME. Without it the "
            "storage backend cannot be built and evidence upload fails at "
            "request time instead of at boot."
        )

    # Tenancy (TENANT_PROVIDER single/multi) is an INDEPENDENT axis — a cloud
    # deployment may serve a single organization (many users) or many isolated
    # tenants (SaaS). The gate validates cloud-native infra + real auth, not tenancy.
    return problems


def _check_cloud_warnings(settings: Any) -> List[str]:
    """Return non-fatal coherence warnings for a cloud deployment.

    Kept separate from :func:`_check_cloud` because these describe a
    deployment that works but is fragile, not one that is invalid. Promoting
    a warning here to a fatal problem there is a deliberate act — it will
    refuse to boot a deployment that is currently running.
    """
    warnings: List[str] = []

    if _storage_backend_name(settings) == "filesystem":
        warnings.append(
            "STORAGE_BACKEND=filesystem on a cloud deployment. Filesystem "
            "storage is single-node: replicas must share one RWX volume, "
            "making that volume a single point of failure for all evidence "
            "I/O. Set STORAGE_BACKEND=s3 with S3_BUCKET_NAME."
        )

    return warnings


def _check_standalone(settings: Any) -> List[str]:
    """Return non-fatal coherence warnings for a standalone deployment."""
    warnings: List[str] = []
    if str(getattr(settings.auth, "auth_mode", "local")) == "oauth":
        warnings.append(
            "AUTH_MODE=oauth with DEPLOYMENT_MODE=standalone is unusual. OAuth / "
            "multi-user is a cloud capability — set DEPLOYMENT_MODE=cloud, or AUTH_MODE=local."
        )
    return warnings


def _check_tenant_provider_coherent(settings: Any) -> None:
    """Fail closed unless a supported tenant provider is configured.

    Tenancy is config-selected in the core (ADR-010): ``single`` (the Standalone
    default) and ``multi`` are both in-core. ``multi`` requires
    ``DEPLOYMENT_MODE=cloud``: its isolation is PostgreSQL row-level security
    scoped by the per-request organization binding, and the cloud coherence
    checks (PostgreSQL, OAuth/RS256, Redis) are what guarantee that stack —
    ``multi`` outside cloud would run without those guarantees, so it is fatal.
    ``single`` is always valid; an unrecognized provider name is fatal.
    """
    # Lazy import: keep this module importable as early as possible at startup,
    # and reuse the factory's name coercion + constants so the gate and the
    # factory cannot diverge on the provider names or the cloud precondition.
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        BUILTIN_SINGLE,
        MULTI_REQUIRES_CLOUD_MSG,
        coerce_provider_name,
    )

    providers = getattr(settings, "providers", None)
    requested = coerce_provider_name(getattr(providers, "tenant_provider", None))
    if requested == BUILTIN_SINGLE:
        return

    if requested != BUILTIN_MULTI:
        raise DeploymentCoherenceError(
            f"TENANT_PROVIDER='{requested}' is not a recognized provider "
            f"(expected '{BUILTIN_SINGLE}' or '{BUILTIN_MULTI}')."
        )

    if not settings.is_cloud:
        raise DeploymentCoherenceError(MULTI_REQUIRES_CLOUD_MSG)


def validate_deployment_coherence(settings: Any) -> None:
    """Raise :class:`DeploymentCoherenceError` if config contradicts ``DEPLOYMENT_MODE``.

    Cloud incoherence is fatal (fail fast at boot). Standalone incoherence is a
    warning only — the dangerous, asymmetric failure is a cloud deployment
    silently running as standalone, not the reverse. Call once at startup, as
    early as settings are available.
    """
    # Tenancy coherence is always fatal: 'multi' requires DEPLOYMENT_MODE=cloud,
    # and an unrecognized provider name fails closed.
    _check_tenant_provider_coherent(settings)

    if settings.is_cloud:
        problems = _check_cloud(settings)
        if problems:
            joined = "\n  - ".join(problems)
            raise DeploymentCoherenceError(
                "DEPLOYMENT_MODE=cloud is incoherent with the running configuration:\n  - "
                f"{joined}\n"
                "Fix the configuration (see docs/architecture/specifications/"
                "llm-configuration-design.md → Deployment Coherence Gate) or correct "
                "DEPLOYMENT_MODE."
            )
        for warning in _check_cloud_warnings(settings):
            logger.warning("Deployment coherence (cloud): %s", warning)
    else:
        for warning in _check_standalone(settings):
            logger.warning("Deployment coherence (standalone): %s", warning)
