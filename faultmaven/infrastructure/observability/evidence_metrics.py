"""Prometheus metrics for the evidence-handling failure domain.

Scaffolded per evidence-failure-modes.md so subsequent
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
from typing import Optional

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

    # 2026-05-01 review fix: page_capture passthrough shape validation.
    # Increments when an attachment classified as source_type=page_capture
    # lacks the structural-shape signals (no [captured_at:] preamble AND
    # no `\n## ` heading) and falls through to standard extractor dispatch
    # instead of bypassing Tier-1 extraction. A spike here indicates either
    # a producer-side change in Copilot's htmlToStructuredText format, or
    # a non-Copilot client misusing input_type=page_capture.
    PAGE_CAPTURE_SHAPE_INVALID_TOTAL = Counter(
        "faultmaven_page_capture_shape_invalid_total",
        "Number of attachments tagged source_type=page_capture that "
        "failed structural-shape validation and fell through to standard "
        "extractor dispatch. Spikes indicate producer-side format drift "
        "or misuse of the page_capture intake path.",
    )

    # GAP-5 Phase 1 — UploadedFile → Evidence promotion observability.
    #
    # Evidence is born only when the LLM emits evidence_to_add for a file
    # (INVESTIGATING only). A file the LLM reads but never files for stays an
    # orphan indefinitely — no milestone can advance from it. These metrics
    # make that desync visible and establish the baseline the GAP-5 Phase 2/3
    # behavior changes require. Labeled by ``provider`` so promotion
    # reliability can be compared across configured LLM providers.
    EVIDENCE_FILES_PROMOTED_TOTAL = Counter(
        "faultmaven_evidence_files_promoted_total",
        "Number of UploadedFiles promoted to Evidence (a new Evidence row "
        "referencing the file's file_id was created on the current turn). "
        "Labeled by provider.",
        labelnames=["provider"],
    )

    EVIDENCE_ORPHAN_FILES_OBSERVED = Histogram(
        "faultmaven_evidence_orphan_files_observed",
        "Per-turn count of uploaded files NOT yet promoted to Evidence, "
        "observed at the end of each INVESTIGATING turn. Labeled by provider.",
        labelnames=["provider"],
        buckets=(0, 1, 2, 3, 5, 8, 13, 21),
    )

    EVIDENCE_OLDEST_ORPHAN_AGE_TURNS = Histogram(
        "faultmaven_evidence_oldest_orphan_age_turns",
        "Age in turns (current_turn − uploaded_at_turn) of the oldest "
        "unpromoted uploaded file, observed per INVESTIGATING turn. A rising "
        "tail signals chronic orphans. Labeled by provider.",
        labelnames=["provider"],
        buckets=(0, 1, 2, 3, 5, 8, 13, 21, 34),
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
    PAGE_CAPTURE_SHAPE_INVALID_TOTAL = _NoOpMetric()
    EVIDENCE_FILES_PROMOTED_TOTAL = _NoOpMetric()
    EVIDENCE_ORPHAN_FILES_OBSERVED = _NoOpMetric()
    EVIDENCE_OLDEST_ORPHAN_AGE_TURNS = _NoOpMetric()


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
    "PAGE_CAPTURE_SHAPE_INVALID_TOTAL",
    "EVIDENCE_FILES_PROMOTED_TOTAL",
    "EVIDENCE_ORPHAN_FILES_OBSERVED",
    "EVIDENCE_OLDEST_ORPHAN_AGE_TURNS",
    "PROMETHEUS_AVAILABLE",
    "record_triage_escalation_if_same_turn",
    "compute_promotion_snapshot",
    "record_evidence_promotion_metrics",
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
        # Post-redesign: Evidence.data_type was dropped; the equivalent
        # category lives on Evidence.source_type (logs / metrics /
        # configuration / visual / user_description). Use its enum value
        # for the metric label, falling back to "unknown" only when the
        # field is unset (label name preserved for dashboard compatibility).
        source_type = getattr(evidence, "source_type", None)
        data_type = (
            getattr(source_type, "value", source_type) if source_type else "unknown"
        )
        AGENT_TRIAGE_ESCALATION_TOTAL.labels(
            data_type=str(data_type),
            tool=tool_name,
        ).inc()
        return True
    except Exception:
        return False


def compute_promotion_snapshot(case) -> dict:
    """Compute the UploadedFile → Evidence promotion snapshot for a case
    (GAP-5 Phase 1). Pure: reads case state, emits nothing.

    Returns a dict::

        {
          "n_files": int,            # total uploaded files
          "n_promoted": int,         # files referenced by some Evidence.source_file_id
          "n_orphans": int,          # uploaded files with no Evidence yet
          "promoted_this_turn": int, # files first referenced on the current turn
          "oldest_orphan_age_turns": int,  # current_turn − min(orphan uploaded_at_turn); 0 if none
          "orphan_file_ids": list[str],    # ids of the unpromoted files (for Phase 2/3)
        }

    "Promoted" = the file's ``file_id`` appears as some ``Evidence.source_file_id``.
    Best-effort and crash-proof; on any structural surprise it returns a
    zeroed snapshot so callers (telemetry, prompt hints) never fail a turn.
    """
    try:
        uploaded = list(getattr(case, "uploaded_files", None) or [])
        evidence = list(getattr(case, "evidence", None) or [])
        current_turn = int(getattr(case, "current_turn", 0) or 0)

        promoted_ids = {
            ev.source_file_id for ev in evidence if getattr(ev, "source_file_id", None)
        }
        # file_ids referenced by Evidence created on the current turn
        promoted_this_turn_ids = {
            ev.source_file_id
            for ev in evidence
            if getattr(ev, "source_file_id", None)
            and getattr(ev, "collected_at_turn", None) == current_turn
        }

        orphans = [
            uf for uf in uploaded if getattr(uf, "file_id", None) not in promoted_ids
        ]
        orphan_ids = [uf.file_id for uf in orphans if getattr(uf, "file_id", None)]

        oldest_age = 0
        if orphans:
            oldest_upload_turn = min(
                int(getattr(uf, "uploaded_at_turn", current_turn) or current_turn)
                for uf in orphans
            )
            oldest_age = max(0, current_turn - oldest_upload_turn)

        n_files = len(uploaded)
        promoted_files = [
            uf for uf in uploaded if getattr(uf, "file_id", None) in promoted_ids
        ]
        return {
            "n_files": n_files,
            "n_promoted": len(promoted_files),
            "n_orphans": len(orphans),
            "promoted_this_turn": len(
                [
                    uf
                    for uf in uploaded
                    if getattr(uf, "file_id", None) in promoted_this_turn_ids
                ]
            ),
            "oldest_orphan_age_turns": oldest_age,
            "orphan_file_ids": orphan_ids,
        }
    except Exception:
        return {
            "n_files": 0,
            "n_promoted": 0,
            "n_orphans": 0,
            "promoted_this_turn": 0,
            "oldest_orphan_age_turns": 0,
            "orphan_file_ids": [],
        }


def record_evidence_promotion_metrics(
    case, provider_name: Optional[str] = None
) -> dict:
    """Emit GAP-5 Phase 1 promotion metrics + a structured log for a case.

    Computes :func:`compute_promotion_snapshot`, emits the Prometheus metrics
    (labeled by provider), and logs the per-case "N files, M promoted" signal
    at INFO. Returns the snapshot so callers (e.g. the Phase 3 assist trigger)
    can reuse it. No behavior change — observation only. Best-effort.
    """
    snapshot = compute_promotion_snapshot(case)
    provider = (provider_name or "unknown").lower()
    try:
        if snapshot["promoted_this_turn"]:
            EVIDENCE_FILES_PROMOTED_TOTAL.labels(provider=provider).inc(
                snapshot["promoted_this_turn"]
            )
        EVIDENCE_ORPHAN_FILES_OBSERVED.labels(provider=provider).observe(
            snapshot["n_orphans"]
        )
        EVIDENCE_OLDEST_ORPHAN_AGE_TURNS.labels(provider=provider).observe(
            snapshot["oldest_orphan_age_turns"]
        )
        _PROMOTION_LOGGER.info(
            "evidence_promotion_snapshot",
            extra={
                "case_id": getattr(case, "case_id", None),
                "provider": provider,
                "n_files": snapshot["n_files"],
                "n_promoted": snapshot["n_promoted"],
                "n_orphans": snapshot["n_orphans"],
                "promoted_this_turn": snapshot["promoted_this_turn"],
                "oldest_orphan_age_turns": snapshot["oldest_orphan_age_turns"],
            },
        )
    except Exception:  # pragma: no cover - telemetry must never break a turn
        pass
    return snapshot


_PROMOTION_LOGGER = logging.getLogger(__name__)
