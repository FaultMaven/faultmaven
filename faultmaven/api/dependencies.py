"""API Dependencies Module

The names of the runtime backends ``GET /admin/config/status`` reports, decided
by the infrastructure predicates that own each question.

The request-scoped service factory and every dependency built on it were
removed in #1935, together with the case-nested session routes that were their
only consumer.
"""

from typing import Any, Optional

from faultmaven.infrastructure.chroma_client import chroma_client_kind
from faultmaven.infrastructure.persistence.database import active_database_backend
from faultmaven.infrastructure.redis_client import is_fakeredis

# This module used to re-export ``get_session_id`` (and import
# ``get_session_service`` without using it) from ``api.v1.dependencies``, to give
# "a canonical import path for all API dependencies". ``get_session_id`` was
# deleted in #1554 — nothing depended on it, and what it did was turn a
# caller-supplied ``X-Session-Id`` into a session id, which is the shape #1461
# ruled out. Nothing re-exported remains, so the section is gone rather than
# emptied: a second import path for a dependency is how two accessors for one
# slot start, and the modules that define these own them.


__all__ = [
    "database_backend_name",
    "session_storage_backend_name",
    "vector_storage_backend_name",
]


# ============================================================
# Runtime Backends
# ============================================================
#
# What ``GET /admin/config/status`` reports as the database, session and vector
# backends. Each is read from the live object the process serves with and
# decided by the infrastructure predicate that owns the question; this module
# only names the answers, because routes may not import the infrastructure layer
# (``tests/unit/architecture/test_architecture_boundaries.py``) and this is the
# route-facing seam that may.

NOT_INITIALIZED = "not initialized"


def database_backend_name() -> str:
    """The dialect of the engine this process built, e.g. ``"postgresql"`` or
    ``"sqlite"``; ``"not initialized"`` before one exists."""
    return active_database_backend() or NOT_INITIALIZED


def session_storage_backend_name(redis_client: Any) -> str:
    """``"redis"`` or ``"fakeredis (inmemory)"`` for the container's Redis client
    — the one the session store is built with; ``"not initialized"`` for
    ``None``."""
    if redis_client is None:
        return NOT_INITIALIZED
    return "fakeredis (inmemory)" if is_fakeredis(redis_client) else "redis"


def vector_storage_backend_name(kb_client: Any, evidence_client: Any) -> str:
    """What the KB and evidence ChromaDB clients the container built talk to.

    ``"chromadb (server)"`` when both address a ChromaDB server,
    ``"chromadb (persistent, split: kb + evidence)"`` when both are local trees,
    ``"disabled"`` when neither was built (``SKIP_SERVICE_CHECKS`` on
    standalone), and a per-client breakdown when they differ — which the
    standalone fallback can produce if the server drops between the two
    constructions.
    """

    kb, evidence = chroma_client_kind(kb_client), chroma_client_kind(evidence_client)
    if kb is None and evidence is None:
        return "disabled"
    if kb == evidence == "server":
        return "chromadb (server)"
    if kb == evidence == "persistent":
        return "chromadb (persistent, split: kb + evidence)"
    return f"chromadb (kb: {kb or 'disabled'}, evidence: {evidence or 'disabled'})"
