"""Prometheus metrics for the evidence-handling failure domain.

Scaffolded per PLAN-evidence-failure-modes-implementation.md §M2 so subsequent
plans can emit into stable metric names:

- ``evidence_dedup_hits_total`` — emitted now (PLAN-content-hash-deduplication
  already landed).
- ``evidence_orphan_files_*`` — emitted when M1 (orphan cleanup) lands.
- ``evidence_turn_async_retry_*`` — emitted when PLAN-async-turn-retry lands.

Metric names follow the existing ``faultmaven_`` prefix convention from
``tracing.py`` so they appear in the same namespace when scraped.

Alert rules that consume these metrics live in the Grafana/Prometheus config
(out of this repository). See ``docs/operations/observability/evidence-metrics.md``
for the canonical alert definitions.
"""

import logging

try:
    from prometheus_client import Counter, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logging.warning("Prometheus client not available — evidence metrics are no-ops")


class _NoOpMetric:
    """Fallback used when prometheus_client is not installed.

    Mirrors the subset of the Counter/Histogram API the emitters use so callers
    don't need PROMETHEUS_AVAILABLE guards at every call site.
    """

    def labels(self, *args, **kwargs) -> "_NoOpMetric":
        return self

    def inc(self, amount: float = 1.0) -> None:
        pass

    def observe(self, amount: float) -> None:
        pass


if PROMETHEUS_AVAILABLE:
    EVIDENCE_DEDUP_HITS_TOTAL = Counter(
        "faultmaven_evidence_dedup_hits_total",
        "Number of attachments that matched an existing content_hash on the "
        "same case and short-circuited through the dedup path (no new Evidence "
        "row, no raw-file re-storage).",
    )

    EVIDENCE_ORPHAN_FILES_FOUND_TOTAL = Counter(
        "faultmaven_evidence_orphan_files_found_total",
        "Number of stored files found to be orphaned (past TTL with "
        "linked=False) during an orphan-cleanup sweep. Emitted by the M1 "
        "orphan cleanup job.",
    )

    EVIDENCE_ORPHAN_FILES_DELETED_TOTAL = Counter(
        "faultmaven_evidence_orphan_files_deleted_total",
        "Number of orphaned files successfully deleted from storage during an "
        "orphan-cleanup sweep. Emitted by the M1 orphan cleanup job.",
    )

    EVIDENCE_TURN_ASYNC_RETRY_ENQUEUED_TOTAL = Counter(
        "faultmaven_evidence_turn_async_retry_enqueued_total",
        "Number of turns whose LLM call hit a transient failure and were "
        "enqueued for async retry. Labeled by failure reason. Emitted by the "
        "PLAN-async-turn-retry implementation.",
        labelnames=["reason"],
    )

    EVIDENCE_TURN_ASYNC_RETRY_OUTCOME_TOTAL = Counter(
        "faultmaven_evidence_turn_async_retry_outcome_total",
        "Terminal outcome of async turn retries. Labeled by outcome "
        "(success | failure | timeout | superseded | cancelled). Emitted by "
        "the PLAN-async-turn-retry implementation.",
        labelnames=["outcome"],
    )

    EVIDENCE_TURN_ASYNC_RETRY_LATENCY_SECONDS = Histogram(
        "faultmaven_evidence_turn_async_retry_latency_seconds",
        "Wall-clock seconds from first retry enqueue to terminal outcome, "
        "covering all attempts. Emitted by the PLAN-async-turn-retry "
        "implementation.",
    )
else:
    EVIDENCE_DEDUP_HITS_TOTAL = _NoOpMetric()
    EVIDENCE_ORPHAN_FILES_FOUND_TOTAL = _NoOpMetric()
    EVIDENCE_ORPHAN_FILES_DELETED_TOTAL = _NoOpMetric()
    EVIDENCE_TURN_ASYNC_RETRY_ENQUEUED_TOTAL = _NoOpMetric()
    EVIDENCE_TURN_ASYNC_RETRY_OUTCOME_TOTAL = _NoOpMetric()
    EVIDENCE_TURN_ASYNC_RETRY_LATENCY_SECONDS = _NoOpMetric()


__all__ = [
    "EVIDENCE_DEDUP_HITS_TOTAL",
    "EVIDENCE_ORPHAN_FILES_FOUND_TOTAL",
    "EVIDENCE_ORPHAN_FILES_DELETED_TOTAL",
    "EVIDENCE_TURN_ASYNC_RETRY_ENQUEUED_TOTAL",
    "EVIDENCE_TURN_ASYNC_RETRY_OUTCOME_TOTAL",
    "EVIDENCE_TURN_ASYNC_RETRY_LATENCY_SECONDS",
    "PROMETHEUS_AVAILABLE",
]
