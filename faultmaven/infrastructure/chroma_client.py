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
import string
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

# chromadb's own rule (``token_authn._check_token`` at the 1.5.8 pin) —
# mirrored here so a bad credential fails before a client exists, not inside
# the callers' connection-failure handling. See chroma_token_auth_kwargs.
_VALID_TOKEN_CHARS = frozenset(
    string.digits + string.ascii_letters + string.punctuation
)


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

    The token is validated HERE, not left to ``TokenAuthClientProvider``
    inside ``HttpClient(...)``: the provider's ``ValueError`` fires inside the
    callers' connection-failure handling, where standalone answers it with a
    silent ``PersistentClient`` fallback (the populated external store swapped
    for an empty local one) and cloud with a boot refusal whose headline
    blames connectivity. Raising before any client is constructed keeps a
    malformed credential loud and named on both paths. Surrounding whitespace
    is stripped first — the deployed secret is minted with shell tooling, and
    a trailing newline should not become a fleet-wide CrashLoop — but any
    interior violation of chromadb's own character rule (ASCII letters,
    digits, punctuation; ``token_authn._check_token``) is refused.
    """
    auth_token = settings.database.chromadb_auth_token
    api_key = settings.database.chromadb_api_key
    if (
        auth_token
        and api_key
        and auth_token.get_secret_value() != api_key.get_secret_value()
    ):
        # Precedence is silent otherwise, and the loser is the spelling most
        # of the docs mention — an operator who rotated CHROMADB_API_KEY
        # would send the stale token with no signal until enforcement exists.
        logger.warning(
            "CHROMADB_AUTH_TOKEN and CHROMADB_API_KEY are both set and "
            "differ; using CHROMADB_AUTH_TOKEN. The deployed secrets are "
            "expected to carry the same value under both names."
        )
    secret = auth_token or api_key
    if not secret:
        return {}
    source = "CHROMADB_AUTH_TOKEN" if auth_token else "CHROMADB_API_KEY"
    token = secret.get_secret_value().strip()
    if not token or not all(c in _VALID_TOKEN_CHARS for c in token):
        raise ValueError(
            f"{source} is set but is not a usable ChromaDB token: it must be "
            "non-empty and contain only ASCII letters, digits, and "
            "punctuation. Fix the credential; an unauthenticated client is "
            "not an acceptable fallback (#1173)."
        )
    return {
        "chroma_client_auth_provider": CHROMA_TOKEN_AUTH_PROVIDER,
        "chroma_client_auth_credentials": token,
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
