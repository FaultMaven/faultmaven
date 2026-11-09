"""Phase 2b — Alt-extractor retry loop in PreprocessingService.

Covers:

- Feature-flag OFF matches pre-Phase-2 behaviour (single dispatch, no
  sanity check, no attempts-block override).
- Feature-flag ON: sanity check runs; passing output returns with a
  single-attempt block (labelled initial).
- Sanity failure triggers retry with alternatives; winning alt yields a
  multi-attempt block with ``triggered_by="sanity_retry"`` on retries.
- Retry budget enforced (total attempts ≤ initial + 2).
- All-fail path lands on direct_fallback and records the drop-through.
- suggested_types from the classifier is consumed first; fallback chain
  kicks in when suggested_types is empty.
"""

from unittest.mock import MagicMock, patch

import pytest

from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.preprocessing_service import (
    _MAX_ALT_RETRIES,
    PreprocessingService,
    _build_alternative_chain,
)
from tests.unit.modules.preprocessing.extraction_stubs import (
    CrimeScene,
    DegenerateMetrics,
    Empty,
    EmptyConfig,
    HealthyMetrics,
    NoErrorsLogs,
    TextFindings,
    UnknownSource,
    marked,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_classifier(
    confidence: float = 0.9,
    data_type: DataType = DataType.METRICS_AND_PERFORMANCE,
    failed: bool = False,
    suggested_types: list[DataType] | None = None,
):
    classifier = MagicMock()
    result = MagicMock()
    result.data_type = data_type
    result.confidence = confidence
    result.source = "rule_based"
    result.source_type = None
    result.classification_failed = failed
    result.suggested_types = suggested_types
    classifier.classify.return_value = result
    return classifier


def _enable_retry(value: bool):
    class _Prep:
        extractor_retry_enabled = value
        confidence_marker_enabled = False
        reclassify_enabled = False

    class _Settings:
        preprocessing = _Prep()

    return patch(
        "faultmaven.config.settings.get_settings",
        return_value=_Settings(),
    )


_LONG_CONTENT = "\n".join(
    f"2024-01-01 INFO metric line {i}" for i in range(250)
)  # above the sanity-check thin-case threshold AND MIN_EXTRACTION_LINES


# ---------------------------------------------------------------------------
# _build_alternative_chain — pure function, no I/O
# ---------------------------------------------------------------------------


class TestBuildAlternativeChain:
    def test_suggested_types_take_priority(self):
        classification = MagicMock(
            suggested_types=[DataType.LOGS_AND_ERRORS, DataType.STRUCTURED_CONFIG]
        )
        chain = _build_alternative_chain(
            classification=classification,
            already_tried=[DataType.METRICS_AND_PERFORMANCE],
            max_alternatives=2,
        )
        assert chain == [DataType.LOGS_AND_ERRORS, DataType.STRUCTURED_CONFIG]

    def test_fallback_chain_used_when_suggested_types_empty(self):
        classification = MagicMock(suggested_types=None)
        chain = _build_alternative_chain(
            classification=classification,
            already_tried=[DataType.METRICS_AND_PERFORMANCE],
            max_alternatives=2,
        )
        # Fallback chain currently has only UNSTRUCTURED_TEXT.
        assert DataType.UNSTRUCTURED_TEXT in chain

    def test_already_tried_types_excluded(self):
        classification = MagicMock(
            suggested_types=[
                DataType.METRICS_AND_PERFORMANCE,
                DataType.LOGS_AND_ERRORS,
            ]
        )
        chain = _build_alternative_chain(
            classification=classification,
            already_tried=[DataType.METRICS_AND_PERFORMANCE],
            max_alternatives=2,
        )
        assert DataType.METRICS_AND_PERFORMANCE not in chain
        assert DataType.LOGS_AND_ERRORS in chain

    def test_respects_max_alternatives_cap(self):
        classification = MagicMock(
            suggested_types=[
                DataType.LOGS_AND_ERRORS,
                DataType.STRUCTURED_CONFIG,
                DataType.SOURCE_CODE,
                DataType.UNSTRUCTURED_TEXT,
            ]
        )
        chain = _build_alternative_chain(
            classification=classification,
            already_tried=[DataType.METRICS_AND_PERFORMANCE],
            max_alternatives=2,
        )
        assert len(chain) == 2


# ---------------------------------------------------------------------------
# Feature-flag OFF — pre-Phase-2 behaviour preserved
# ---------------------------------------------------------------------------


class TestFlagOffBehaviour:
    @pytest.mark.asyncio
    async def test_flag_off_single_dispatch_no_sanity(self):
        """With the flag off, the service must not run sanity checks
        and must produce the same single-attempt metadata shape Phase 1
        tests pinned."""
        classifier = _make_classifier(data_type=DataType.METRICS_AND_PERFORMANCE)
        service = PreprocessingService(
            classifier=classifier,
            logs_extractor=CrimeScene(),
            metrics_extractor=DegenerateMetrics(),
        )
        with _enable_retry(False):
            result = await service.classify_and_extract(content=_LONG_CONTENT)

        attempts = result.extraction_metadata["evidence_metadata"]["extractor"][
            "attempts"
        ]
        # Single attempt, no retries, no sanity-passed field drift.
        assert len(attempts) == 1
        assert attempts[0]["triggered_by"] == "initial"
        # The one attempt is the metrics extractor's.
        assert marked(DegenerateMetrics) in result.structural_index


# ---------------------------------------------------------------------------
# Feature-flag ON — retry path
# ---------------------------------------------------------------------------


class TestFlagOnRetryPath:
    @pytest.mark.asyncio
    async def test_healthy_output_passes_sanity_no_retry(self, spawned):
        """When the initial extractor's output passes sanity, the retry
        loop must not fire — single attempt, labelled initial."""
        classifier = _make_classifier(data_type=DataType.METRICS_AND_PERFORMANCE)

        service = PreprocessingService(
            classifier=classifier,
            logs_extractor=CrimeScene(),
            metrics_extractor=HealthyMetrics(),
            text_extractor=TextFindings(),
        )
        with _enable_retry(True):
            result = await service.classify_and_extract(content=_LONG_CONTENT)

        attempts = result.extraction_metadata["evidence_metadata"]["extractor"][
            "attempts"
        ]
        assert len(attempts) == 1
        assert attempts[0]["triggered_by"] == "initial"
        assert attempts[0]["sanity_passed"] is True
        # The metrics extractor's result stands: the text fallback never ran.
        assert marked(HealthyMetrics) in result.structural_index
        assert len(spawned) == 1

    @pytest.mark.asyncio
    async def test_degenerate_output_triggers_retry_with_fallback(self):
        """Classifier is confident but wrong — metrics extractor
        produces degenerate single-data-point output. suggested_types
        is empty (confident classification). Retry falls through to
        UNSTRUCTURED_TEXT, which always produces valid output."""
        classifier = _make_classifier(
            data_type=DataType.METRICS_AND_PERFORMANCE,
            suggested_types=None,
        )

        # UNSTRUCTURED_TEXT extractor output — permissive default passes.
        service = PreprocessingService(
            classifier=classifier,
            logs_extractor=CrimeScene(),
            metrics_extractor=DegenerateMetrics(),
            text_extractor=TextFindings(),
        )
        with _enable_retry(True):
            result = await service.classify_and_extract(content=_LONG_CONTENT)

        attempts = result.extraction_metadata["evidence_metadata"]["extractor"][
            "attempts"
        ]
        # Initial (metrics, failed) + at least one retry.
        assert len(attempts) >= 2
        assert attempts[0]["data_type"] == DataType.METRICS_AND_PERFORMANCE.value
        assert attempts[0]["sanity_passed"] is False
        # Retry labelled correctly.
        assert attempts[1]["triggered_by"] == "sanity_retry"
        # UNSTRUCTURED_TEXT is in the fallback chain.
        assert attempts[-1]["data_type"] == DataType.UNSTRUCTURED_TEXT.value

    @pytest.mark.asyncio
    async def test_retry_budget_enforced(self):
        """Total attempts must not exceed initial + _MAX_ALT_RETRIES."""
        # All extractors return degenerate output so every sanity check fails.
        # UNSTRUCTURED_TEXT with empty output so its sanity-check fails too.
        classifier = _make_classifier(
            data_type=DataType.METRICS_AND_PERFORMANCE,
            suggested_types=[
                DataType.LOGS_AND_ERRORS,
                DataType.STRUCTURED_CONFIG,
                DataType.SOURCE_CODE,
            ],
        )

        # Logs/config/code extractors all produce output that fails sanity.
        service = PreprocessingService(
            classifier=classifier,
            logs_extractor=NoErrorsLogs(),
            metrics_extractor=DegenerateMetrics(),
            config_extractor=EmptyConfig(),
            source_code_extractor=UnknownSource(),
            text_extractor=Empty(),
        )
        with _enable_retry(True):
            result = await service.classify_and_extract(content=_LONG_CONTENT)

        attempts = result.extraction_metadata["evidence_metadata"]["extractor"][
            "attempts"
        ]
        # Initial + at most _MAX_ALT_RETRIES alternatives.
        assert len(attempts) <= 1 + _MAX_ALT_RETRIES

    @pytest.mark.asyncio
    async def test_all_retries_fail_falls_back_to_direct(self):
        """When every candidate fails sanity, the service lands on
        direct_fallback and records chosen_type so ops can tell.

        The fallback chain is ``_FALLBACK_ALT_TYPES`` =
        ``(UNSTRUCTURED_TEXT, DOCUMENTATION)`` after the LOW-priority
        review fix expanded it from a single-element tuple. Both must
        be wired to fail to exercise the direct_fallback exit.
        """
        classifier = _make_classifier(
            data_type=DataType.METRICS_AND_PERFORMANCE,
            suggested_types=None,
        )

        # The UNSTRUCTURED_TEXT and DOCUMENTATION fallbacks produce empty
        # output, which fails sanity.
        service = PreprocessingService(
            classifier=classifier,
            logs_extractor=CrimeScene(),
            metrics_extractor=DegenerateMetrics(),
            text_extractor=Empty(),
            documentation_extractor=Empty(),
        )
        with _enable_retry(True):
            result = await service.classify_and_extract(content=_LONG_CONTENT)

        extractor_meta = result.extraction_metadata["evidence_metadata"]["extractor"]
        assert extractor_meta["chosen_type"] == "direct_fallback"


# ---------------------------------------------------------------------------
# Thin-case scenario: sparse content must not retry
# ---------------------------------------------------------------------------


class TestThinCaseNoRetry:
    @pytest.mark.asyncio
    async def test_short_content_skips_retry_even_on_degenerate_output(self, spawned):
        """Even if the extractor produces what looks like degenerate
        output, the sanity check's thin-case safeguard passes for short
        content so no retry fires. Critical for the Flexibility Contract
        thin-case scenario — 1-word pastes must not spiral."""
        classifier = _make_classifier(data_type=DataType.METRICS_AND_PERFORMANCE)

        service = PreprocessingService(
            classifier=classifier,
            logs_extractor=CrimeScene(),
            metrics_extractor=DegenerateMetrics(),
            text_extractor=TextFindings(),
        )
        with _enable_retry(True):
            # content < 50 chars triggers the thin-case safeguard.
            result = await service.classify_and_extract(content="short")

        attempts = result.extraction_metadata["evidence_metadata"]["extractor"][
            "attempts"
        ]
        assert len(attempts) == 1
        assert spawned == [], "an extractor ran"
