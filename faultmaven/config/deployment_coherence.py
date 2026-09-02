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


# The canonical VECTOR_STORAGE_TYPE value is "chromadb" (the default); the
# legacy spellings are accepted as synonyms. Anything else deselects the
# external server even when CHROMADB_URL is set.
CHROMA_STORAGE_SYNONYMS = frozenset({"chromadb", "chroma", "chroma_db", "chroma-db"})


def is_external_chroma_configured(settings: Any) -> bool:
    """Whether the configuration selects the external ChromaDB server.

    True iff ``CHROMADB_URL`` is set and ``VECTOR_STORAGE_TYPE`` is a chromadb
    synonym. The one predicate for that question, shared by the client
    factories (``faultmaven.infrastructure.chroma_client``) and check 7 below —
    an inline copy in a caller is a copy that can drift from this one. It lives
    HERE, not in the infrastructure module, so the dependency stays
    one-directional (infrastructure → config); the reverse import was a
    circular-import architecture violation.
    """
    db = settings.database
    vector_storage_type = (db.vector_storage_type or "").strip().lower()
    chromadb_url = (db.chromadb_url or "").strip()
    return bool(chromadb_url) and vector_storage_type in CHROMA_STORAGE_SYNONYMS


def is_host_only_chroma_configured(settings: Any) -> bool:
    """Whether ``CHROMADB_HOST`` alone selects a remote ChromaDB server.

    The **second** opt-in to a remote store, and the one that is easy to
    forget: ``KnowledgeIngester`` opens an ``HttpClient`` when ``CHROMADB_URL``
    is unset and ``CHROMADB_HOST`` is not ``localhost`` (see
    ``settings.chromadb_host`` — it is the documented knob for a k8s
    deployment). Written here, beside its sibling, so a caller asking "is a
    local directory the store?" cannot answer it by checking only one of the
    two — which is precisely the fm#936 shape, one spelling down.

    Deliberately NOT folded into :func:`is_external_chroma_configured`: that
    predicate has callers (the container's client factory, the coherence gate)
    whose meaning is specifically "``CHROMADB_URL`` selects the server", and
    widening it would silently change what they do. It is also NOT a substitute
    for it: under this opt-in the client factory still hands out a **local**
    ``PersistentClient`` (measured), so only the ingester goes remote and the
    deployment reads its KB from two different stores.

    A BLANK host is not a host. pydantic-settings runs with ``env_ignore_empty``
    off, so ``CHROMADB_HOST=`` — an unpopulated ConfigMap key, a trailing ``=``
    in a .env — arrives as a set field holding ``""``, and a bare ``!=
    "localhost"`` reads that as "a remote server is configured". It is the same
    shape as the blank persist directory (see
    ``faultmaven.bootstrap.data_init.UnusableDataDirError``), and it reached
    further: ``KnowledgeIngester`` would build ``HttpClient(host="", port=8000)``
    off it. Empty means "not configured", which means the embedded client.
    """
    db = settings.database
    host = (db.chromadb_host or "").strip()
    return not (db.chromadb_url or "").strip() and host not in ("", "localhost")


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


def _auth_mode_name(auth: Any) -> str:
    """Return the configured auth mode as a plain lowercase name.

    ``AuthMode`` is a ``(str, Enum)``, so ``str(member)`` yields
    ``'AuthMode.OAUTH'`` — matching on that is how the cloud auth gate refused
    every correctly-configured cloud boot while the standalone warning stayed
    dead (#881). Read ``.value`` when present, and normalize so a plain-string
    override matches too. Both call sites share this one predicate so they
    cannot drift apart again.
    """
    mode = getattr(auth, "auth_mode", None)
    if mode is None:
        return "local"
    return str(getattr(mode, "value", mode)).strip().lower()


def _check_cloud(settings: Any) -> List[str]:
    """Return the list of reasons the config is NOT a valid cloud deployment."""
    problems: List[str] = []
    auth = settings.auth
    db = settings.database
    security = settings.security

    # 1. Auth must be OAuth (RS256), not the local bypass.
    auth_mode = _auth_mode_name(auth)
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

    # 6. No filesystem evidence storage in cloud. Filesystem storage is
    # single-node: replicas must share one RWX volume, making that volume a
    # single point of failure for all evidence I/O. This shipped as a warning
    # while the RWX deployment was still running; promoted to fatal once the
    # object-storage migration completed (infra#127), so the SPOF cannot be
    # reintroduced silently.
    if _storage_backend_name(settings) == "filesystem":
        problems.append(
            "STORAGE_BACKEND=filesystem on a cloud deployment. Filesystem "
            "storage is single-node: replicas must share one RWX volume, "
            "making that volume a single point of failure for all evidence "
            "I/O. Set STORAGE_BACKEND=s3 with S3_BUCKET_NAME."
        )

    # 7. Vectors must be the external ChromaDB server. A local PersistentClient
    # lives in one container filesystem: on a web replica that is per-replica
    # search state, and in a seeding Job it is durable-state corruption — the
    # Postgres rows land in the shared database while the vectors die with the
    # pod (#901). The client factories enforce the same refusal at build time
    # (ChromaUnavailableError) for the reachability case; this check catches
    # the pure-config case at the gate, with a config-level message. The
    # predicate is shared with the client factories (defined above) so the two
    # cannot drift on what "external ChromaDB" means (the #881 lesson).
    if not is_external_chroma_configured(settings):
        problems.append(
            "Cloud requires the external ChromaDB server: set CHROMADB_URL and "
            "VECTOR_STORAGE_TYPE=chromadb. A local PersistentClient writes "
            "vectors into a single container's filesystem — per-replica search "
            "results on web pods, silent vector loss in seeding jobs."
        )

    # Tenancy (TENANT_PROVIDER single/multi) is an INDEPENDENT axis — a cloud
    # deployment may serve a single organization (many users) or many isolated
    # tenants (SaaS). The gate validates cloud-native infra + real auth, not tenancy.
    return problems


def _check_standalone(settings: Any) -> List[str]:
    """Return non-fatal coherence warnings for a standalone deployment."""
    warnings: List[str] = []
    if _auth_mode_name(settings.auth) == "oauth":
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
    else:
        for warning in _check_standalone(settings):
            logger.warning("Deployment coherence (standalone): %s", warning)
