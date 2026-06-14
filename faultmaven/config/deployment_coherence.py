"""Deployment coherence gate (ADR-004).

Asserts that the running configuration is coherent with the canonical
``DEPLOYMENT_MODE``. A ``cloud`` deployment MUST present cloud identity (OAuth
auth + RS256 keys, PostgreSQL, real Redis, multi-tenant); otherwise the app
refuses to boot rather than silently running as ``standalone`` on cloud
infrastructure.

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


def _check_cloud(settings: Any) -> List[str]:
    """Return the list of reasons the config is NOT a valid cloud deployment."""
    problems: List[str] = []
    auth = settings.auth
    db = settings.database
    security = settings.security
    providers = settings.providers

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

    # 4. Tenancy must be multi.
    tenant = str(getattr(providers, "tenant_provider", "single"))
    if tenant != "multi":
        problems.append(
            f"TENANT_PROVIDER must be 'multi' for cloud (got '{tenant}'). "
            "Single-tenant provides no isolation between organizations."
        )
    return problems


def _check_standalone(settings: Any) -> List[str]:
    """Return non-fatal coherence warnings for a standalone deployment."""
    warnings: List[str] = []
    if str(getattr(settings.auth, "auth_mode", "local")) == "oauth":
        warnings.append(
            "AUTH_MODE=oauth with DEPLOYMENT_MODE=standalone is unusual. OAuth / "
            "multi-user is a cloud capability — set DEPLOYMENT_MODE=cloud, or AUTH_MODE=local."
        )
    return warnings


def validate_deployment_coherence(settings: Any) -> None:
    """Raise :class:`DeploymentCoherenceError` if config contradicts ``DEPLOYMENT_MODE``.

    Cloud incoherence is fatal (fail fast at boot). Standalone incoherence is a
    warning only — the dangerous, asymmetric failure is a cloud deployment
    silently running as standalone, not the reverse. Call once at startup, as
    early as settings are available.
    """
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
    else:
        for warning in _check_standalone(settings):
            logger.warning("Deployment coherence (standalone): %s", warning)
