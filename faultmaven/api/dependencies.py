"""API Dependencies Module

The names of the runtime backends ``GET /admin/config/status`` reports, decided
by the infrastructure predicates that own each question, and the database
bounds it reports beside them; the jobs' memory caps beside the container's
limit (``container_memory``, #2921), from the worker pools' own constants
(``document_preprocessor``, ``preprocessing_service``, ``runbook_gate``,
``embedding_guard``, ``index_slot``, ``draft_content``), imported where they
are read; and the LLM usage ledger's read side (``GET /admin/llm/usage`` and
``features.llm_usage_metering``).

The request-scoped service factory and every dependency built on it were
removed in #1935, together with the case-nested session routes that were their
only consumer.
"""

from typing import Any, Optional

from faultmaven.api.models import (
    ContainerMemoryStatus,
    DatabaseTimeoutsStatus,
    FeatureStatus,
    KnowledgeIndexingStatus,
    TraceBackendStatus,
)
from faultmaven.infrastructure import index_slot
from faultmaven.infrastructure.chroma_client import chroma_client_kind
from faultmaven.infrastructure.persistence.database import (
    active_database_backend,
    active_database_bounds,
)
from faultmaven.infrastructure.redis_client import is_fakeredis
from faultmaven.models.exceptions import IndexAdmissionRefused

# This module used to re-export ``get_session_id`` (and import
# ``get_session_service`` without using it) from ``api.v1.dependencies``, to give
# "a canonical import path for all API dependencies". ``get_session_id`` was
# deleted in #1554 — nothing depended on it, and what it did was turn a
# caller-supplied ``X-Session-Id`` into a session id, which is the shape #1461
# ruled out. Nothing re-exported remains, so the section is gone rather than
# emptied: a second import path for a dependency is how two accessors for one
# slot start, and the modules that define these own them.


__all__ = [
    "container_memory_status",
    "database_backend_name",
    "database_timeouts_status",
    "llm_usage_metering_status",
    "llm_usage_report",
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


def database_timeouts_status(
    revocation_read_timeout_seconds: Optional[float],
) -> DatabaseTimeoutsStatus:
    """The bounds the database engine was built with, beside the revocation read's (#2280).

    The four PostgreSQL bounds are ``None`` before the engine exists and on a
    SQLite engine, where none applies. The revocation read's deadline is the
    caller's to read off the store it holds.
    """
    bounds = active_database_bounds()

    def seconds(milliseconds: Optional[int]) -> Optional[float]:
        return milliseconds / 1000 if milliseconds is not None else None

    return DatabaseTimeoutsStatus(
        command_timeout_seconds=bounds.command_timeout_seconds if bounds else None,
        connect_timeout_seconds=bounds.connect_timeout_seconds if bounds else None,
        statement_timeout_seconds=(
            seconds(bounds.statement_timeout_ms) if bounds else None
        ),
        client_connection_check_interval_seconds=(
            seconds(bounds.client_connection_check_interval_ms) if bounds else None
        ),
        revocation_read_timeout_seconds=revocation_read_timeout_seconds,
    )


def knowledge_indexing_status(window_seconds: int) -> KnowledgeIndexingStatus:
    """The KB indexing window beside this process's slot (#2055): what it
    holds, how long the running job has run, and its host lock."""
    slot = index_slot.index_admission
    return KnowledgeIndexingStatus(
        window_seconds=window_seconds,
        queued=slot.queued(),
        encoding_for_seconds=slot.encoding_for_seconds(),
        host_lock_available=slot.host_lock_available,
        waiting_for_host_lock=slot.waiting_for_host_lock,
    )


_MIB = 1024 * 1024


def container_memory_status(settings: Any) -> ContainerMemoryStatus:
    """The worst-overlap formula's bounded terms, beside the cgroup's limit.

    Principle 14, *Container memory*: per serving process, every pool's slots
    at their ``RLIMIT_AS`` plus the buffers the process holds per job, Tier 1's
    input at its worst (3 *U*); the KB encode once per container. *U* is in
    MiB, as the body-size limit counts it. Tier 1's slots are read as in force.
    """
    from faultmaven.config.worker_stores import serving_processes
    from faultmaven.infrastructure import embedding_guard as count
    from faultmaven.infrastructure.model_cache import cgroup_memory
    from faultmaven.modules.knowledge.domain.services import (
        document_preprocessor as prep,
    )
    from faultmaven.modules.knowledge.domain.services import runbook_gate as gate
    from faultmaven.modules.knowledge.draft_content import DRAFT_CONTENT_MAX_CHARS
    from faultmaven.modules.preprocessing import preprocessing_service as tier1

    w = serving_processes(settings)
    u_mb = int(settings.upload.max_upload_size_mb)
    u = u_mb * _MIB
    slots = tier1.TIER1_SLOTS
    _, tier1_memory, tier1_output = tier1.tier1_limits(u_mb)
    pools = w * (
        prep.PREPARATION_SLOTS * prep.preparation_memory_limit(u_mb)
        + slots * tier1_memory
        + gate.GATE_SLOTS * gate.GATE_MEMORY_BYTES
        + count.TOKEN_COUNT_SLOTS * count.TOKEN_COUNT_MEMORY_BYTES
    )
    gate_texts_in = 2 * 2 * DRAFT_CONTENT_MAX_CHARS * 4
    buffers = w * (
        prep.PREPARATION_SLOTS * prep.PREPARATION_OUTPUT_LIMIT_BYTES
        + slots * (2 * 3 * u + tier1_output)
        + gate.GATE_SLOTS * (gate_texts_in + gate.GATE_OUTPUT_LIMIT_BYTES)
        + count.TOKEN_COUNT_SLOTS * (2 * u + _MIB)
    )
    encode = index_slot.ENCODE_MEMORY_BYTES
    caps = pools + buffers + encode
    supervised = w > 1 or bool(getattr(settings.server, "reload", False))
    cgroup = cgroup_memory()
    limit = cgroup.memory_max_bytes
    return ContainerMemoryStatus(
        job_caps_bytes=caps,
        pools_bytes=pools,
        buffers_bytes=buffers,
        encode_bytes=encode,
        encode_without_host_lock_bytes=w * encode,
        serving_processes=w,
        upload_limit_mb=u_mb,
        tier1_slots=slots,
        unbounded_terms=[
            "api",
            *(["supervisor"] if supervised else []),
            "encode_buffers",
        ],
        memory_max_bytes=limit,
        oom_group=cgroup.oom_group,
        cgroup_source=cgroup.source,
        headroom_bytes=None if limit is None else limit - caps,
        job_caps_exceed_limit=None if limit is None else caps > limit,
    )


def llm_tracing_is_effective() -> bool:
    """Will a traced call record a span: the record ``opik_sdk.configure``
    wrote, and the SDK's switch."""
    from faultmaven.infrastructure.observability import opik_sdk

    return opik_sdk.tracing_is_effective()


def trace_backend_status() -> TraceBackendStatus:
    """The trace SDK's policy from that record, and the state of a client the
    process already built, never building one (#2616 part c, #2573). Blocking
    (the parked count is a SQLite read): run it off the event loop."""
    from faultmaven.infrastructure.observability import opik_sdk

    record = opik_sdk.backend()
    server_connection, parked_messages = opik_sdk.connection_state()
    return TraceBackendStatus(
        sentry_reporting=record.sentry_reporting,
        capture_content=record.capture_content,
        project=record.project,
        ping_interval_s=record.ping_interval_s,
        server_connection=server_connection,
        parked_messages=parked_messages,
    )


def indexing_busy_refusal(reason: str) -> IndexAdmissionRefused:
    """A ``KNOWLEDGE_INDEXING_BUSY`` refusal of ``reason``, its wait read from
    this process's queue as it stands (#2055)."""
    slot = index_slot.index_admission
    return index_slot.refusal(reason, wait=slot.retry_after_seconds(reason))


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


# ============================================================
# The LLM usage ledger (#640, #1764)
# ============================================================


async def llm_usage_report(**kwargs: Any) -> dict:
    """The bound enterprise's usage report, read from the ledger
    (``read_usage_report``'s keywords).

    Raises:
        UsageStoreUnavailable: the store could not be read.
    """
    from faultmaven.infrastructure.llm.usage_report import read_usage_report

    return await read_usage_report(**kwargs)


async def llm_usage_metering_status() -> FeatureStatus:
    """``features["llm_usage_metering"]``: is LLM spend being persisted (#1764)?

    The effect, not a setting: the installed ledger must be the database one
    (``isinstance``, as ``token_revocation_durable`` decides), AND a one-row
    read of its tables must succeed now. The probe catches its own failure and
    publishes the exception type only, so it cannot fail this response.
    """
    from faultmaven.infrastructure.llm.usage_ledger import (
        SqlUsageLedger,
        get_installed_usage_ledger,
    )
    from faultmaven.infrastructure.llm.usage_report import probe_usage_store

    ledger = get_installed_usage_ledger()
    fault = await probe_usage_store() if isinstance(ledger, SqlUsageLedger) else None
    if ledger is None:
        detail = (
            "No usage ledger is installed, so LLM spend is not persisted: the "
            "composition root did not run."
        )
    elif not isinstance(ledger, SqlUsageLedger):
        detail = (
            f"The installed usage ledger is {type(ledger).__name__}, not the "
            "database ledger, so LLM spend is not persisted."
        )
    elif fault is not None:
        detail = (
            f"The usage tables cannot be read ({fault}); see the server log. "
            "GET /admin/llm/usage answers 503 until they can."
        )
    else:
        detail = (
            "The database usage ledger is installed and llm_usage_daily and "
            "llm_turn_spend can be read. Calls whose write failed are not in "
            "them: llm_usage_unpersisted_calls_total{reason} counts those."
        )
    return FeatureStatus(
        enabled=isinstance(ledger, SqlUsageLedger) and fault is None,
        description=detail,
        config_hint=(
            "True when the database usage ledger is installed AND its tables "
            "can be read; it does not say every call was written. Every "
            "deployment that boots the API installs it; "
            "False with the tables unreadable means the database is down or "
            "was not migrated to head."
        ),
    )
