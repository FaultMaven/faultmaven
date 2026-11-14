"""#2789: the small-file passthrough is small by size as well as by lines.

Path 4 hands the raw content to the model as the upload's extract. Sized by
lines alone, a few-line upload of megabytes (a one-line JSON export, a
minified bundle) was stored whole as its ``structural_index``, though the
prompt shows at most ``EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM`` of it. The raw
content stands in for an extract, so it may not exceed the most any extract
holds, ``MAX_STRUCTURAL_INDEX_CHARS``. Past that it reaches the extractor
dispatch like any larger upload: a confident type goes to its extractor, an
uncertain one to the clarification card (owner, 2026-11-14), because the card
sits directly before the dispatch (#2355).
"""

from __future__ import annotations

import json

import pytest

from faultmaven.infrastructure.observability import evidence_metrics
from faultmaven.models.api import DataType, SourceMetadata
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.extractors import StructuredConfigExtractor
from faultmaven.modules.preprocessing.extractors.utils import (
    MAX_STRUCTURAL_INDEX_CHARS,
)
from faultmaven.modules.preprocessing.preprocessing_service import (
    MIN_EXTRACTION_LINES,
    PreprocessingService,
    _line_count,
)
from tests.unit.modules.preprocessing.extraction_stubs import CrimeScene
from tests.unit.modules.preprocessing.uncertain_classifier import uncertain_classifier

pytestmark = pytest.mark.unit

MINTED_PASTE = "pasted-content-20261114T081500.txt"
PASTE = SourceMetadata(source_type="text_paste")


def _service(classifier: DataClassifier | None = None) -> PreprocessingService:
    return PreprocessingService(
        classifier=classifier or DataClassifier(),
        logs_extractor=CrimeScene(),
        config_extractor=StructuredConfigExtractor(),
    )


def _json_export(items: int) -> str:
    """A one-line JSON export, as ``kubectl get -o json | jq -c`` or an API
    dump writes it: one line, however many objects."""
    return json.dumps(
        {
            "kind": "PodList",
            "items": [
                {
                    "name": f"api-{i}",
                    "status": "Running",
                    "restarts": i % 4,
                    "labels": {"app": "api", "tier": "backend"},
                }
                for i in range(items)
            ],
        }
    )


def _json_log_events(events: int) -> str:
    """A one-line JSON array of log events, as a log API's export returns
    it. A JSON log document is not config (#2361), and no paste rule types
    it with confidence."""
    return json.dumps(
        [
            {
                "ts": f"2026-11-14T08:{i // 60 % 60:02d}:{i % 60:02d}Z",
                "level": "error" if i % 7 == 0 else "info",
                "svc": "api",
                "msg": "upstream connect error or disconnect/reset before headers",
            }
            for i in range(events)
        ]
    )


#: About 50 KB on one line each: five times the bound, an order below the
#: upload limit, so an extractor finishes well inside its deadline on any
#: runner.
EXPORT = _json_export(600)
LOG_EVENTS = _json_log_events(400)


def _log_of(chars: int) -> str:
    """Log lines of a fixed width, cut to exactly ``chars`` characters and
    far fewer than ``MIN_EXTRACTION_LINES`` lines."""
    line = "2026-11-14T08:15:00Z ERROR api upstream timeout " + "x" * 450 + "\n"
    text = (line * (chars // len(line) + 1))[:chars]
    assert _line_count(text) < MIN_EXTRACTION_LINES
    return text


class TestAFewLinesOfManyBytesIsNotPassedThrough:
    @pytest.mark.parametrize("content", [EXPORT, LOG_EVENTS])
    def test_each_example_is_one_line_and_past_the_bound(self, content):
        """The premise: only the size bound can tell these uploads from small
        ones."""
        assert _line_count(content) == 1
        assert len(content) > 4 * MAX_STRUCTURAL_INDEX_CHARS

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("filename", "source"),
        [("pods.json", None), (MINTED_PASTE, PASTE)],
        ids=["file", "paste"],
    )
    async def test_a_confident_export_goes_to_its_extractor(self, filename, source):
        """A config parser reads the export, so it is config whether uploaded
        or pasted (#2361), and the config extractor reads it."""
        result = await _service().classify_and_extract(
            content=EXPORT, filename=filename, source_metadata=source, principal="u1"
        )

        assert result.detailed_data_type == DataType.STRUCTURED_CONFIG
        assert result.extraction_method not in (
            "raw_passthrough",
            "classification_failed",
        )
        assert len(result.structural_index) < len(EXPORT) / 2

    @pytest.mark.asyncio
    async def test_an_uncertain_paste_of_log_events_is_carded(self):
        """No rule types the paste, and an extractor would read it, so its
        type is put to the user, with a preview in place of the blob."""
        result = await _service().classify_and_extract(
            content=LOG_EVENTS,
            filename=MINTED_PASTE,
            source_metadata=PASTE,
            principal="u1",
        )

        assert result.extraction_method == "classification_failed"
        assert "Classification uncertain" in result.structural_index
        assert len(result.structural_index) < MAX_STRUCTURAL_INDEX_CHARS

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content", [_json_export(3), _json_log_events(3)], ids=["export", "events"]
    )
    async def test_the_same_shapes_small_still_pass_through_raw(self, content):
        """The control: each shape at a small size keeps the passthrough, so
        the tests above are not passing because pastes stopped passing
        through."""
        result = await _service().classify_and_extract(
            content=content,
            filename=MINTED_PASTE,
            source_metadata=PASTE,
            principal="u1",
        )

        assert result.extraction_method == "raw_passthrough"
        assert result.structural_index == content


class TestTheBoundIsTheExtractBudget:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("chars", "method"),
        [
            (MAX_STRUCTURAL_INDEX_CHARS, "raw_passthrough"),
            (MAX_STRUCTURAL_INDEX_CHARS + 1, "classification_failed"),
        ],
    )
    async def test_an_uncertain_upload_is_carded_one_past_the_bound(
        self, chars, method
    ):
        content = _log_of(chars)
        result = await _service(uncertain_classifier()).classify_and_extract(
            content=content, filename=MINTED_PASTE, principal="u1"
        )

        assert result.extraction_method == method

    @pytest.mark.asyncio
    async def test_a_confident_log_one_past_the_bound_is_extracted(self):
        result = await _service().classify_and_extract(
            content=_log_of(MAX_STRUCTURAL_INDEX_CHARS + 1),
            filename="api.log",
            principal="u1",
            user_override=DataType.LOGS_AND_ERRORS,
        )

        # Past the passthrough and not carded. Which extract arrives (the
        # extractor's, or the busy/timeout preview on a loaded runner) is
        # the worker's business, not this bound's.
        assert result.extraction_method not in (
            "raw_passthrough",
            "classification_failed",
        )

    @pytest.mark.asyncio
    async def test_the_line_bound_still_holds_under_the_size_bound(self):
        """The size bound adds to the line count; it does not replace it."""
        content = "\n".join("ok" for _ in range(MIN_EXTRACTION_LINES))
        assert len(content) < MAX_STRUCTURAL_INDEX_CHARS

        result = await _service(uncertain_classifier()).classify_and_extract(
            content=content, filename=MINTED_PASTE, principal="u1"
        )

        assert result.extraction_method == "classification_failed"


@pytest.mark.skipif(
    not evidence_metrics.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed; the counter is a no-op",
)
@pytest.mark.asyncio
async def test_a_large_uncertain_upload_counts_as_a_clarification():
    from prometheus_client import REGISTRY

    def count(outcome: str) -> float:
        value = REGISTRY.get_sample_value(
            "faultmaven_preprocessing_uncertain_classification_total",
            {"outcome": outcome},
        )
        return 0.0 if value is None else value

    before = count("clarification"), count("passthrough")

    await _service(uncertain_classifier()).classify_and_extract(
        content=_log_of(MAX_STRUCTURAL_INDEX_CHARS + 1),
        filename=MINTED_PASTE,
        principal="u1",
    )

    assert (count("clarification"), count("passthrough")) == (
        before[0] + 1,
        before[1],
    )
