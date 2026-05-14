"""Phase 1 — evidence.metadata contract + classifier confidence surfacing.

Covers:
- The EvidenceMetadata pydantic model round-trips through storage dicts
  with absent keys remaining absent.
- PreprocessingService writes the `evidence_metadata` block into
  PreprocessingResult.extraction_metadata, with the classifier's
  confidence / source / failed / suggested_types preserved.
- PreprocessingResult round-trip is backward-compatible — a consumer
  that does not know about `evidence_metadata` still works.
"""

from unittest.mock import MagicMock

import pytest

from faultmaven.core.preprocessing.evidence_metadata import (
    LOW_CONFIDENCE_THRESHOLD,
    ClassificationMetadata,
    EvidenceMetadata,
    ExtractorMetadata,
)
from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.preprocessing_service import PreprocessingService

# ---------------------------------------------------------------------------
# Pydantic model shape — the namespaced, additive contract
# ---------------------------------------------------------------------------


class TestEvidenceMetadataContract:
    def test_empty_metadata_round_trips_as_empty_dict(self):
        """A metadata instance with no sub-blocks serializes to {} —
        existing evidence predating Phase 1 reads as this shape."""
        meta = EvidenceMetadata()
        assert meta.to_storage_dict() == {}

    def test_classification_only_does_not_bleed_into_other_keys(self):
        """Writing one namespace leaves the others absent, preserving
        the ownership contract in case-schema.md §4.3."""
        meta = EvidenceMetadata(
            classification=ClassificationMetadata(
                confidence=0.42,
                source="rule_based",
                failed=False,
                suggested_types=["logs"],
            )
        )
        stored = meta.to_storage_dict()
        assert set(stored.keys()) == {"classification"}
        assert "extractor" not in stored
        assert "entities" not in stored
        assert "coverage" not in stored

    def test_from_storage_tolerates_none(self):
        """A NULL metadata column must deserialize to a default instance,
        not raise."""
        meta = EvidenceMetadata.from_storage_dict(None)
        assert meta.classification is None
        assert meta.extractor is None

    def test_from_storage_tolerates_empty_dict(self):
        """Empty dict is the persisted shape when nothing has been
        written yet (SQLite column default is ``'{}'``)."""
        meta = EvidenceMetadata.from_storage_dict({})
        assert meta.classification is None

    def test_from_storage_tolerates_unknown_extra_keys(self):
        """Forward-compatibility: a future phase that adds a new
        top-level key must not break today's reader."""
        meta = EvidenceMetadata.from_storage_dict(
            {
                "classification": {
                    "confidence": 0.9,
                    "source": "rule_based",
                    "failed": False,
                    "suggested_types": [],
                },
                "future_phase_key": {"anything": True},
            }
        )
        assert meta.classification is not None
        assert meta.classification.confidence == 0.9

    def test_confidence_bounds_enforced(self):
        """Classifier confidence is clipped to [0, 1] by the pydantic
        validator so garbage values don't pollute downstream logic."""
        with pytest.raises(Exception):
            ClassificationMetadata(confidence=2.0, source="rule_based", failed=False)

    def test_low_confidence_threshold_sits_between_gates(self):
        """Sanity-pin the threshold against the known classifier
        thresholds (auto_accept=0.85, classification_failed=0.50) so
        someone can't silently tune the marker out of the "classified
        but shaky" band."""
        assert 0.50 < LOW_CONFIDENCE_THRESHOLD < 0.85


# ---------------------------------------------------------------------------
# PreprocessingService writes the metadata contract into its result
# ---------------------------------------------------------------------------


def _make_classifier(
    confidence: float,
    source: str = "rule_based",
    failed: bool = False,
    suggested=None,
):
    classifier = MagicMock()
    result = MagicMock()
    result.data_type = DataType.LOGS_AND_ERRORS
    result.confidence = confidence
    result.source = source
    result.source_type = None
    result.classification_failed = failed
    # suggested_types is a list of DataType-like objects with .value
    if suggested:
        stub = [MagicMock(value=s) for s in suggested]
        result.suggested_types = stub
    else:
        result.suggested_types = None
    classifier.classify.return_value = result
    return classifier


def _make_extractor():
    from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult

    extractor = MagicMock()
    extractor.strategy_name = "crime_scene"
    extractor.llm_calls_used = 0
    extractor.extract.return_value = ExtractResult(
        file_extract="=== Crime Scene ===\nError at line 42",
        search_map="",
        file_meta={},
    )
    return extractor


class TestPreprocessingServiceWritesMetadata:
    @pytest.mark.asyncio
    async def test_high_confidence_writes_classification_block(self):
        classifier = _make_classifier(confidence=0.92)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        result = await service.classify_and_extract(content="some log data")

        ev_meta = result.extraction_metadata["evidence_metadata"]
        assert ev_meta["classification"]["confidence"] == pytest.approx(0.92)
        assert ev_meta["classification"]["source"] == "rule_based"
        assert ev_meta["classification"]["failed"] is False

    @pytest.mark.asyncio
    async def test_low_confidence_is_preserved_in_metadata(self):
        """A low-confidence classification is what the context builder's
        marker reads. Pin that the value survives serialization."""
        classifier = _make_classifier(confidence=0.4)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        result = await service.classify_and_extract(content="some shaky data")

        ev_meta = result.extraction_metadata["evidence_metadata"]
        assert ev_meta["classification"]["confidence"] == pytest.approx(0.4)
        # The downstream marker decision uses LOW_CONFIDENCE_THRESHOLD.
        assert ev_meta["classification"]["confidence"] < LOW_CONFIDENCE_THRESHOLD

    @pytest.mark.asyncio
    async def test_extractor_chosen_type_recorded(self):
        classifier = _make_classifier(confidence=0.9)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        result = await service.classify_and_extract(content="data")

        ev_meta = result.extraction_metadata["evidence_metadata"]
        assert ev_meta["extractor"]["chosen_type"] == DataType.LOGS_AND_ERRORS.value

    @pytest.mark.asyncio
    async def test_classification_failed_path_still_writes_metadata(self):
        """classification_failed short-circuits to a placeholder result.
        The metadata block must still carry the classifier's signals so
        the agent can see what was uncertain."""
        classifier = _make_classifier(
            confidence=0.35,
            failed=True,
            suggested=["logs_and_errors", "command_output"],
        )
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        result = await service.classify_and_extract(content="ambiguous content")

        ev_meta = result.extraction_metadata["evidence_metadata"]
        assert ev_meta["classification"]["failed"] is True
        assert "logs_and_errors" in ev_meta["classification"]["suggested_types"]

    @pytest.mark.asyncio
    async def test_absent_extractor_metadata_keys_stay_absent(self):
        """Phase 1 writes `classification` and `extractor`. Phase 4's
        `entities` and Phase 3's `coverage` keys must NOT appear — the
        ownership contract depends on writers touching only their own
        namespace."""
        classifier = _make_classifier(confidence=0.9)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        result = await service.classify_and_extract(content="data")

        ev_meta = result.extraction_metadata["evidence_metadata"]
        assert "entities" not in ev_meta
        assert "coverage" not in ev_meta


# ---------------------------------------------------------------------------
# Phase 1.5 — reclassify_evidence wrapper + attempts merging
# ---------------------------------------------------------------------------


class TestReclassifyEvidence:
    """Reclassification re-runs the pipeline under a ``user_override``
    and preserves the prior ``extractor.attempts`` history so the row
    keeps its classification trail."""

    @pytest.mark.asyncio
    async def test_initial_upload_records_attempt_as_initial(self):
        """No ``user_override`` means the upload path wrote the first
        attempt. It must be labelled ``initial``, not ``user_override``."""
        classifier = _make_classifier(confidence=0.9)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        result = await service.classify_and_extract(content="data")

        attempts = result.extraction_metadata["evidence_metadata"]["extractor"][
            "attempts"
        ]
        assert len(attempts) == 1
        assert attempts[0]["triggered_by"] == "initial"
        assert attempts[0]["data_type"] == DataType.LOGS_AND_ERRORS.value

    @pytest.mark.asyncio
    async def test_reclassify_records_attempt_as_user_override(self):
        """``reclassify_evidence`` must mark the new attempt's
        ``triggered_by`` as ``user_override`` so observability can
        distinguish user-driven corrections from upload-path runs."""
        classifier = _make_classifier(confidence=0.9)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        result = await service.reclassify_evidence(
            content="data",
            filename="evidence.log",
            user_override=DataType.LOGS_AND_ERRORS,
            previous_metadata=None,
        )

        attempts = result.extraction_metadata["evidence_metadata"]["extractor"][
            "attempts"
        ]
        assert attempts[-1]["triggered_by"] == "user_override"

    @pytest.mark.asyncio
    async def test_reclassify_preserves_previous_attempts(self):
        """History is the point. An evidence that was classified as
        METRICS on upload, then reclassified as LOGS by the user, must
        carry both attempts — the initial bad call is what made the
        correction necessary and is the interesting one to debug."""
        classifier = _make_classifier(confidence=0.9)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        previous_metadata = {
            "classification": {
                "confidence": 0.88,
                "source": "rule_based",
                "failed": False,
                "suggested_types": [],
            },
            "extractor": {
                "chosen_type": DataType.METRICS_AND_PERFORMANCE.value,
                "attempts": [
                    {
                        "data_type": DataType.METRICS_AND_PERFORMANCE.value,
                        "sanity_passed": True,
                        "duration_ms": 12,
                        "triggered_by": "initial",
                    }
                ],
            },
        }
        result = await service.reclassify_evidence(
            content="data",
            filename="evidence.log",
            user_override=DataType.LOGS_AND_ERRORS,
            previous_metadata=previous_metadata,
        )

        attempts = result.extraction_metadata["evidence_metadata"]["extractor"][
            "attempts"
        ]
        assert len(attempts) == 2
        assert attempts[0]["data_type"] == DataType.METRICS_AND_PERFORMANCE.value
        assert attempts[0]["triggered_by"] == "initial"
        assert attempts[1]["data_type"] == DataType.LOGS_AND_ERRORS.value
        assert attempts[1]["triggered_by"] == "user_override"

    @pytest.mark.asyncio
    async def test_reclassify_caps_attempts_at_five_most_recent(self):
        """Ten prior attempts + this one → six would exceed the cap.
        The oldest rotate off the head so the latest truth survives
        and the row can't grow unbounded under pathological re-correction."""
        classifier = _make_classifier(confidence=0.9)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        previous_metadata = {
            "extractor": {
                "chosen_type": DataType.METRICS_AND_PERFORMANCE.value,
                "attempts": [
                    {
                        "data_type": DataType.METRICS_AND_PERFORMANCE.value,
                        "sanity_passed": True,
                        "duration_ms": i,
                        "triggered_by": f"step_{i}",
                    }
                    for i in range(10)
                ],
            },
        }
        result = await service.reclassify_evidence(
            content="data",
            filename="evidence.log",
            user_override=DataType.LOGS_AND_ERRORS,
            previous_metadata=previous_metadata,
        )

        attempts = result.extraction_metadata["evidence_metadata"]["extractor"][
            "attempts"
        ]
        assert len(attempts) == 5
        # Latest attempt is the user_override one — must survive.
        assert attempts[-1]["triggered_by"] == "user_override"
        # Oldest head entries rolled off; the 4 most-recent prior survive.
        assert attempts[0]["triggered_by"] == "step_6"

    @pytest.mark.asyncio
    async def test_reclassify_confidence_is_1_and_source_is_user_override(self):
        """Phase 1's low-confidence marker must be suppressed on the
        updated row. That hinges on ``classification.source`` being
        ``user_override`` and ``confidence`` being 1.0."""
        classifier = _make_classifier(confidence=0.9)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )

        # The real DataClassifier writes source="user_override", confidence=1.0
        # on the user_override priority path. Our mock classifier returns
        # whatever we configured; the _make_classifier helper doesn't model
        # that branch. Patch in what the real classifier does.
        classifier.classify.return_value.confidence = 1.0
        classifier.classify.return_value.source = "user_override"

        result = await service.reclassify_evidence(
            content="data",
            filename="evidence.log",
            user_override=DataType.LOGS_AND_ERRORS,
        )

        cls = result.extraction_metadata["evidence_metadata"]["classification"]
        assert cls["source"] == "user_override"
        assert cls["confidence"] == 1.0
