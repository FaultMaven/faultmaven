"""Phase 2c — extraction-quality feedback signals.

Two metrics:

- ``PREPROCESSING_EXTRACTION_YIELD_RATIO`` — histogram emitted by
  ``PreprocessingService._build_result`` on every extraction.
- ``AGENT_TRIAGE_ESCALATION_TOTAL`` — counter emitted via
  ``record_triage_escalation_if_same_turn`` when a drill-down tool
  targets an evidence delivered in the same turn.

Both are best-effort telemetry: failures must never break the extraction
or tool-call path. Tests pin the trigger conditions and the defensive
behaviour on malformed inputs.
"""

from unittest.mock import MagicMock

import pytest

from faultmaven.infrastructure.observability.evidence_metrics import (
    AGENT_TRIAGE_ESCALATION_TOTAL,
    PREPROCESSING_EXTRACTION_YIELD_RATIO,
    PROMETHEUS_AVAILABLE,
    record_triage_escalation_if_same_turn,
)

# ---------------------------------------------------------------------------
# record_triage_escalation_if_same_turn
# ---------------------------------------------------------------------------


def _evidence_stub(collected_at_turn: int | None, data_type: str = "logs"):
    ev = MagicMock()
    ev.collected_at_turn = collected_at_turn
    ev.data_type = data_type
    return ev


def _case_stub(current_turn: int | None):
    progress = MagicMock()
    progress.current_turn = current_turn
    case = MagicMock()
    case.progress = progress
    return case


class TestTriageEscalationTrigger:
    def test_fires_when_turn_matches(self):
        """Evidence delivered in the current turn → escalation fires."""
        fired = record_triage_escalation_if_same_turn(
            evidence=_evidence_stub(collected_at_turn=5),
            case=_case_stub(current_turn=5),
            tool_name="search_file",
        )
        assert fired is True

    def test_silent_when_turn_mismatch(self):
        """Evidence from an earlier turn is not an in-turn escalation —
        the agent is drilling into historical data, not reacting to
        just-delivered extraction."""
        fired = record_triage_escalation_if_same_turn(
            evidence=_evidence_stub(collected_at_turn=2),
            case=_case_stub(current_turn=5),
            tool_name="search_file",
        )
        assert fired is False

    def test_silent_when_missing_turn_on_evidence(self):
        """Legacy evidence without collected_at_turn can't be classified
        as in-turn — the helper must silently skip rather than guess."""
        fired = record_triage_escalation_if_same_turn(
            evidence=_evidence_stub(collected_at_turn=None),
            case=_case_stub(current_turn=5),
            tool_name="search_file",
        )
        assert fired is False

    def test_silent_when_missing_current_turn_on_case(self):
        """Missing progress.current_turn can't be compared — skip."""
        fired = record_triage_escalation_if_same_turn(
            evidence=_evidence_stub(collected_at_turn=5),
            case=_case_stub(current_turn=None),
            tool_name="search_file",
        )
        assert fired is False

    def test_silent_when_case_is_none(self):
        """No case context (tool invoked outside a turn? defensive) —
        skip rather than raise."""
        fired = record_triage_escalation_if_same_turn(
            evidence=_evidence_stub(collected_at_turn=5),
            case=None,
            tool_name="search_file",
        )
        assert fired is False

    def test_silent_on_broken_inputs(self):
        """Pathological inputs — attribute access fails. Telemetry
        must be swallowed, not raised, so best-effort observability
        never crashes the tool path."""

        class Broken:
            @property
            def collected_at_turn(self):
                raise RuntimeError("broken")

        fired = record_triage_escalation_if_same_turn(
            evidence=Broken(),
            case=_case_stub(current_turn=5),
            tool_name="search_file",
        )
        assert fired is False


# ---------------------------------------------------------------------------
# Yield ratio — smoke-test the histogram is emitted on extraction
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client missing")
class TestYieldRatioEmitted:
    @pytest.mark.asyncio
    async def test_yield_ratio_observes_on_extraction(self):
        """Each call to ``_build_result`` observes the ratio; the
        histogram's sample count for that data_type increases.

        We read the Prometheus metric's internal sample count before
        and after to pin the emission — brittle to prometheus_client
        internals but the only way to verify the Histogram was touched
        without touching the HTTP scrape endpoint.
        """
        from unittest.mock import MagicMock

        from faultmaven.models.api import DataType
        from faultmaven.modules.preprocessing.preprocessing_service import (
            PreprocessingService,
        )

        data_type_label = DataType.LOGS_AND_ERRORS.value
        # Snapshot existing sample count for this label (may be non-zero
        # if other tests ran first in the same process).
        before = _histogram_sample_count(
            PREPROCESSING_EXTRACTION_YIELD_RATIO, data_type=data_type_label
        )

        classifier = MagicMock()
        cls_result = MagicMock()
        cls_result.data_type = DataType.LOGS_AND_ERRORS
        cls_result.confidence = 0.9
        cls_result.source = "rule_based"
        cls_result.source_type = None
        cls_result.classification_failed = False
        cls_result.suggested_types = None
        classifier.classify.return_value = cls_result

        extractor = MagicMock()
        extractor.strategy_name = "crime_scene"
        extractor.llm_calls_used = 0
        extractor.extract.return_value = "extracted summary"

        service = PreprocessingService(classifier=classifier, logs_extractor=extractor)
        await service.classify_and_extract(content="x" * 200)

        after = _histogram_sample_count(
            PREPROCESSING_EXTRACTION_YIELD_RATIO, data_type=data_type_label
        )
        assert after == before + 1


def _histogram_sample_count(histogram, **labels) -> int:
    """Read the internal sample count for a Prometheus histogram with
    the given labels. Returns 0 if the label set hasn't been observed."""
    try:
        child = histogram.labels(**labels)
        # prometheus_client stores samples in _sum and the bucket counters;
        # total observations == _sum._value.get() count path differs per
        # backend. Easier: iterate samples via collect().
        for metric in histogram.collect():
            for sample in metric.samples:
                if sample.name.endswith("_count") and sample.labels.get(
                    "data_type"
                ) == labels.get("data_type"):
                    return int(sample.value)
    except Exception:
        return 0
    return 0
