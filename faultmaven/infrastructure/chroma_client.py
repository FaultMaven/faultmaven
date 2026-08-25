"""ChromaDB client deployment gate.

The vector store has the same deployment split as Redis
(:mod:`faultmaven.infrastructure.redis_client`): standalone runs an in-process
``PersistentClient``; cloud runs a shared ChromaDB server over HTTP. A cloud
process that silently substitutes a ``PersistentClient`` writes vectors into
its own container filesystem — for a web pod that is per-replica search
results, and for a Job (``kb_seed``) it is durable-state corruption: the
Postgres rows land in the shared database while the vectors die with the pod,
leaving a KB that lists fine and searches empty (#901).

This module holds the pieces both client-acquisition paths (the container
provider and ``KnowledgeIngester``) and the deployment coherence gate share,
so they cannot drift apart on what "external ChromaDB is configured" means —
the #881 lesson applied to the vector store.
"""

from __future__ import annotations

import logging
from typing import Any

# The shared predicate lives in the config module (beside coherence check 7)
# so the import stays one-directional: infrastructure → config. Re-exported
# here because this module is the client factories' natural import surface.
from faultmaven.config.deployment_coherence import (
    CHROMA_STORAGE_SYNONYMS,
    DeploymentCoherenceError,
    is_external_chroma_configured,
)

__all__ = [
    "CHROMA_STORAGE_SYNONYMS",
    "CHROMA_TOKEN_AUTH_PROVIDER",
    "ChromaUnavailableError",
    "chroma_token_auth_kwargs",
    "is_external_chroma_configured",
    "local_chroma_or_fail",
]

logger = logging.getLogger(__name__)

# The client-side token auth provider. ``chromadb.auth.token`` was renamed to
# ``chromadb.auth.token_authn`` in ChromaDB 0.5 — below our ``>= 0.5.3`` floor,
# so the old path never imports and must not appear anywhere. It once did, in
# the container provider, guarded by ``except Exception: pass`` — which
# silently produced an unauthenticated client for over a year (#1173).
CHROMA_TOKEN_AUTH_PROVIDER = "chromadb.auth.token_authn.TokenAuthClientProvider"


def chroma_token_auth_kwargs(settings: Any) -> dict[str, str]:
    """Client-side token-auth ``ChromaSettings`` kwargs, or ``{}`` if no token.

    The single place both HTTP client acquisition paths (the container
    provider and ``KnowledgeIngester``) resolve their ChromaDB credential, so
    they cannot drift on either axis again — before #1173 one path read
    ``chromadb_api_key`` with a dead provider path while the other read
    ``chromadb_auth_token`` with the correct one, and neither actually sent a
    token on the deployed configuration. ``CHROMADB_AUTH_TOKEN`` is canonical;
    ``CHROMADB_API_KEY`` is accepted because the deployed secrets carry the
    same value under both names (see faultmaven-enterprise-infra
    ``base/secrets.yaml``).

    No import probe and no ``try`` around this: the provider module exists at
    every supported chromadb version, and if the provider path ever breaks,
    client construction must raise — under cloud that is a boot refusal, which
    is the correct failure direction for "a token is configured but cannot be
    sent" (#1173: both halves failing open is what hid this).
    """
    secret = settings.database.chromadb_auth_token or settings.database.chromadb_api_key
    if not secret:
        return {}
    return {
        "chroma_client_auth_provider": CHROMA_TOKEN_AUTH_PROVIDER,
        "chroma_client_auth_credentials": secret.get_secret_value(),
    }


class ChromaUnavailableError(DeploymentCoherenceError):
    """Raised when the external ChromaDB is unusable on a deployment that requires it.

    A subclass of :class:`DeploymentCoherenceError` — the same boot-refusal
    signal the deployment coherence gate raises, because this is the same
    statement: the running configuration contradicts ``DEPLOYMENT_MODE``.
    """


def local_chroma_or_fail(reason: str, settings: Any) -> None:
    """Allow a local ``PersistentClient``, or refuse when cloud requires the server.

    A ``PersistentClient`` lives in ONE container filesystem. Under
    ``DEPLOYMENT_MODE=cloud`` the vector store is deployment-wide state, so
    substituting a local client silently forks it per-process: a seeding Job
    reports success while the shared server stays empty, and no signal an
    operator has says otherwise. Cloud refuses to run instead. Standalone is
    single-process, where ``PersistentClient`` is the intended backend, so
    callers proceed (with a warning only when an external server was
    configured but unreachable — the caller logs that before calling here).

    Returns normally when the fallback is permitted; raises
    :class:`ChromaUnavailableError` when it is not.
    """
    if settings.is_cloud:
        raise ChromaUnavailableError(
            f"External ChromaDB is required under DEPLOYMENT_MODE=cloud but is "
            f"unusable: {reason}. Refusing to fall back to a local "
            "PersistentClient — vectors would be written into this container's "
            "filesystem and silently diverge from the shared server (rows "
            "without vectors, per-replica search results). Check CHROMADB_URL / "
            "VECTOR_STORAGE_TYPE and connectivity to the ChromaDB service."
        )
