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

from faultmaven.config.deployment_coherence import DeploymentCoherenceError

logger = logging.getLogger(__name__)

# The canonical value is "chromadb" (the default); the legacy spellings are
# accepted as synonyms. Anything else deselects the external server even when
# CHROMADB_URL is set.
CHROMA_STORAGE_SYNONYMS = frozenset({"chromadb", "chroma", "chroma_db", "chroma-db"})


class ChromaUnavailableError(DeploymentCoherenceError):
    """Raised when the external ChromaDB is unusable on a deployment that requires it.

    A subclass of :class:`DeploymentCoherenceError` — the same boot-refusal
    signal the deployment coherence gate raises, because this is the same
    statement: the running configuration contradicts ``DEPLOYMENT_MODE``.
    """


def is_external_chroma_configured(settings: Any) -> bool:
    """Whether the configuration selects the external ChromaDB server.

    True iff ``CHROMADB_URL`` is set and ``VECTOR_STORAGE_TYPE`` is a chromadb
    synonym. The one predicate for that question, shared by the client
    factories and the deployment coherence gate — an inline copy in a caller
    is a copy that can drift from this one.
    """
    db = settings.database
    vector_storage_type = (db.vector_storage_type or "").strip().lower()
    chromadb_url = (db.chromadb_url or "").strip()
    return bool(chromadb_url) and vector_storage_type in CHROMA_STORAGE_SYNONYMS


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
