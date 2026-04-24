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

    # Phase 1.5 — user-driven reclassification. Labelled by
    # (from_type, to_type, trigger) so a classifier-bias dashboard can
    # spot systematic (from_type, to_type) pairs. trigger values:
    # ``api`` (direct PATCH), ``agent_tool`` (reclassify_evidence tool).
    EVIDENCE_RECLASSIFICATION_TOTAL = Counter(
        "faultmaven_evidence_reclassification_total",
        "Number of evidence rows whose data_type was changed by a user "
        "override. Emitted by the PATCH "
        "/cases/{id}/evidence/{eid}/classification endpoint and by the "
        "reclassify_evidence agent tool.",
        labelnames=["from_type", "to_type", "trigger"],
    )

    # Phase 2c — extraction-quality feedback signals.
    #
    # Yield ratio: len(structural_index) / len(raw_content), per
    # extraction run, labelled by the extractor's chosen data_type. A
    # baseline forms naturally per type (LOGS ~0.005 for crime-scene
    # extraction, STRUCTURED_CONFIG ~0.3 for parsed config). Abrupt
    # drift — STRUCTURED_CONFIG yield dropping below 0.01 for a week —
    # indicates the data distribution changed and the extractor is
    # producing garbage for a new sub-format. Bucket boundaries cover
    # the full range from near-zero (degenerate) through 1.0
    # (uncompressed passthrough) with denser resolution in the
    # interesting low-compression band.
    PREPROCESSING_EXTRACTION_YIELD_RATIO = Histogram(
        "faultmaven_preprocessing_extraction_yield_ratio",
        "Ratio of structural_index bytes to raw_content bytes per "
        "extraction. Baselines per data_type provide a drift signal.",
        labelnames=["data_type"],
        buckets=(0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0),
    )

    # Triage-to-escalation rate: counts when the agent received a
    # Triage-mode turn with a structural index AND immediately called
    # search_file or deep_analysis on the same evidence (within the
    # same turn's tool-loop). A high ratio of
    # escalations / triage_turns on one data_type indicates the
    # structural index is leaving the agent uninformed — a signal to
    # tune the extractor rather than the classifier. Emitted by the
    # agent tool-loop layer when it observes a tool call on an
    # evidence whose structural_index was just delivered in the
    # current turn's context.
    AGENT_TRIAGE_ESCALATION_TOTAL = Counter(
        "faultmaven_agent_triage_escalation_total",
        "Number of in-turn tool calls (search_file / deep_analysis) "
        "targeting an evidence whose structural_index was delivered "
        "in the same turn. High rate on a data_type → structural "
        "index insufficient for that type.",
        labelnames=["data_type", "tool"],
    )

    # Phase 4 — case-level entity registry overflow. Labeled by
    # ``entity_type`` so the dashboard surfaces which vocabulary
    # members regularly hit the per-(evidence, type) hard cap.
    # Emitted once per (evidence, entity_type) pair that overflows,
    # not per excess row — a single evidence spilling 1000 IPs
    # increments the ``ip`` bucket by 1, not 500.
    # Per Phase 4 plan exit criteria: if any type's overflow rate
    # > 20% of evidence, tune the cap or split the type.
    CASE_ENTITIES_OVERFLOW_TOTAL = Counter(
        "faultmaven_case_entities_overflow_total",
        "Number of (evidence, entity_type) pairs that exceeded the "
        "Phase 4 per-(evidence, type) hard cap and had entity rows "
        "truncated. Labelled by entity_type.",
        labelnames=["entity_type"],
    )
else:
    EVIDENCE_DEDUP_HITS_TOTAL = _NoOpMetric()
    EVIDENCE_ORPHAN_FILES_FOUND_TOTAL = _NoOpMetric()
    EVIDENCE_ORPHAN_FILES_DELETED_TOTAL = _NoOpMetric()
    EVIDENCE_TURN_ASYNC_RETRY_ENQUEUED_TOTAL = _NoOpMetric()
    EVIDENCE_TURN_ASYNC_RETRY_OUTCOME_TOTAL = _NoOpMetric()
    EVIDENCE_TURN_ASYNC_RETRY_LATENCY_SECONDS = _NoOpMetric()
    EVIDENCE_RECLASSIFICATION_TOTAL = _NoOpMetric()
    PREPROCESSING_EXTRACTION_YIELD_RATIO = _NoOpMetric()
    AGENT_TRIAGE_ESCALATION_TOTAL = _NoOpMetric()
    CASE_ENTITIES_OVERFLOW_TOTAL = _NoOpMetric()


__all__ = [
    "EVIDENCE_DEDUP_HITS_TOTAL",
    "EVIDENCE_ORPHAN_FILES_FOUND_TOTAL",
    "EVIDENCE_ORPHAN_FILES_DELETED_TOTAL",
    "EVIDENCE_TURN_ASYNC_RETRY_ENQUEUED_TOTAL",
    "EVIDENCE_TURN_ASYNC_RETRY_OUTCOME_TOTAL",
    "EVIDENCE_TURN_ASYNC_RETRY_LATENCY_SECONDS",
    "EVIDENCE_RECLASSIFICATION_TOTAL",
    "PREPROCESSING_EXTRACTION_YIELD_RATIO",
    "AGENT_TRIAGE_ESCALATION_TOTAL",
    "CASE_ENTITIES_OVERFLOW_TOTAL",
    "PROMETHEUS_AVAILABLE",
    "record_triage_escalation_if_same_turn",
]


def record_triage_escalation_if_same_turn(evidence, case, tool_name: str) -> bool:
    """Emit ``AGENT_TRIAGE_ESCALATION_TOTAL`` when a tool call targets
    an evidence whose structural_index was delivered in the *same* turn.

    "Same turn" = ``evidence.collected_at_turn == case.progress.current_turn``.
    That is the exact definition of "the agent had the structural index
    in its context and immediately needed more" — a triage escalation.

    Returns ``True`` if the counter was incremented, ``False`` otherwise
    (for tests that want to assert the trigger fired). Swallows all
    attribute-access failures so tool paths can't crash on missing
    fields — telemetry is best-effort.
    """
    try:
        collected_at_turn = getattr(evidence, "collected_at_turn", None)
        progress = getattr(case, "progress", None) if case is not None else None
        current_turn = getattr(progress, "current_turn", None) if progress else None
        if collected_at_turn is None or current_turn is None:
            return False
        if collected_at_turn != current_turn:
            return False
        data_type = getattr(evidence, "data_type", None) or "unknown"
        AGENT_TRIAGE_ESCALATION_TOTAL.labels(
            data_type=str(data_type),
            tool=tool_name,
        ).inc()
        return True
    except Exception:
        return False
