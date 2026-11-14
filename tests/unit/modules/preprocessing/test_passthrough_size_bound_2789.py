"""#2789: what is stored as an upload's extract is bounded, whichever path made it.

Path 4 stored a few-line upload's raw content as its extract, sized by lines
alone, so a one-line, multi-MB export was copied onto its row whole and read
back on every later turn. And not every extractor holds to its own output
budget (a 150-line, 63K log came back as a 106K index). One bound,
``MAX_STORED_EXTRACT_CHARS``, now holds for every stored ``file_extract`` and
``search_map``, enforced where every result is assembled (``_build_result``),
by a head-and-tail cut that keeps what the prompt shows. Path 4's size rule is
the same bound (owner, 2026-11-14). Under ``MIN_EXTRACTION_LINES`` and past
it:

- a confident upload goes to its type's extractor, like any larger upload;
- an uncertain one still passes through with no card, cut like any extract.
"""

from __future__ import annotations

import json

import pytest

from faultmaven.infrastructure.observability import evidence_metrics
from faultmaven.models.api import DataType, SourceMetadata
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.extractors import (
    LogsAndErrorsExtractor,
    StructuredConfigExtractor,
)
from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    MAX_STORED_EXTRACT_CHARS,
    cut_to_budget,
)
from faultmaven.modules.preprocessing.preprocessing_service import (
    MIN_EXTRACTION_LINES,
    PreprocessingService,
    _line_count,
)
from tests.unit.modules.preprocessing.uncertain_classifier import uncertain_classifier

pytestmark = pytest.mark.unit

MINTED_PASTE = "pasted-content-20261114T081500.txt"
PASTE = SourceMetadata(source_type="text_paste")

#: What the prompt shows of an extract (``EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM``
#: at its default): a cut must leave it as it was.
PROMPT_VIEW = 4000


def _service(classifier: DataClassifier | None = None) -> PreprocessingService:
    return PreprocessingService(
        classifier=classifier or DataClassifier(),
        logs_extractor=LogsAndErrorsExtractor(),
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


def _log(lines: int, width: int) -> str:
    """A log of ``lines`` lines of about ``width`` characters each."""
    head = "2026-11-14T08:15:{s:02d}Z ERROR [api] request_id={i} upstream timeout "
    return "".join(
        head.format(s=i % 60, i=i) + "k=v " * ((width - 70) // 4) + "\n"
        for i in range(lines)
    )


def _log_of(chars: int) -> str:
    """Log lines cut to exactly ``chars`` characters, far fewer than
    ``MIN_EXTRACTION_LINES`` lines."""
    text = _log(chars // 400 + 1, 500)[:chars]
    assert _line_count(text) < MIN_EXTRACTION_LINES
    return text


#: Past the bound on one line each, an order below the upload limit.
EXPORT = _json_export(1000)
LOG_EVENTS = _json_log_events(700)


def _classification(result) -> dict:
    return (result.extraction_metadata or {})["evidence_metadata"]["classification"]


def _stored(result) -> ExtractResult:
    """The stored extract. An extractor's result is its ``ExtractResult`` as
    JSON; a passthrough, or the preview a loaded runner's busy or timeout
    fallback stores, is plain text."""
    try:
        payload = json.loads(result.structural_index)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and "file_extract" in payload:
        return ExtractResult.from_json(result.structural_index)
    return ExtractResult(file_extract=result.structural_index)


@pytest.mark.parametrize("content", [EXPORT, LOG_EVENTS], ids=["export", "events"])
def test_each_example_is_one_line_and_past_the_bound(content):
    """The premise: only a size rule can tell these uploads from small ones."""
    assert _line_count(content) == 1
    assert len(content) > MAX_STORED_EXTRACT_CHARS


class TestCutToBudget:
    def test_text_that_fits_is_unchanged(self):
        assert cut_to_budget("x" * 100, 100) == "x" * 100

    def test_a_cut_fills_the_budget_with_head_and_tail(self):
        text = "".join(f"row {i:06d}\n" for i in range(20000))
        cut = cut_to_budget(text, MAX_STORED_EXTRACT_CHARS)
        assert MAX_STORED_EXTRACT_CHARS - 10 <= len(cut) <= MAX_STORED_EXTRACT_CHARS
        assert cut.startswith(text[:PROMPT_VIEW])
        assert cut.endswith("row 019999\n")
        assert "search_file reads the whole upload" in cut

    def test_a_budget_smaller_than_the_marker_keeps_only_the_head(self):
        assert cut_to_budget("z" * 1000, 50) == "z" * 50

    def test_one_character_over_loses_only_what_the_marker_needs(self):
        text = "y" * (MAX_STORED_EXTRACT_CHARS + 1)
        cut = cut_to_budget(text, MAX_STORED_EXTRACT_CHARS)
        assert cut.count("y") > MAX_STORED_EXTRACT_CHARS - 100


class TestEveryStoredExtractIsBounded:
    @pytest.mark.asyncio
    async def test_a_large_confident_log_under_the_line_cutoff_is_extracted_and_cut(
        self,
    ):
        """The logs extractor keeps such a log whole and more; the bound holds
        anyway, and the head the prompt shows is the extractor's own."""
        content = _log(150, 600)
        assert len(content) > MAX_STORED_EXTRACT_CHARS
        alone = LogsAndErrorsExtractor().extract(content)
        assert len(alone.file_extract) > MAX_STORED_EXTRACT_CHARS, "premise"

        result = await _service().classify_and_extract(
            content=content, filename="api.log", principal="u1"
        )

        stored = _stored(result)
        assert result.extraction_method not in (
            "raw_passthrough",
            "classification_failed",
        )
        assert len(stored.file_extract) <= MAX_STORED_EXTRACT_CHARS
        # On a loaded runner the extractor can run past its deadline; the
        # bound holds for the stored preview too, and the checks of the
        # extractor's own extract apply when it finished.
        if result.extraction_method == "crime_scene":
            assert stored.file_extract.startswith(alone.file_extract[:PROMPT_VIEW])
            assert stored.file_meta["truncated"] is True

    @pytest.mark.asyncio
    async def test_an_upload_past_the_line_cutoff_is_bounded_too(self):
        """The bound is not Path 4's: a 300-line log's extract is held to it."""
        content = _log(300, 600)

        result = await _service().classify_and_extract(
            content=content,
            filename="api.log",
            principal="u1",
            user_override=DataType.LOGS_AND_ERRORS,
        )

        stored = _stored(result)
        assert len(stored.file_extract) <= MAX_STORED_EXTRACT_CHARS
        assert len(stored.search_map or "") <= MAX_STORED_EXTRACT_CHARS

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
    @pytest.mark.parametrize(
        ("chars", "extracted"),
        [(MAX_STORED_EXTRACT_CHARS, False), (MAX_STORED_EXTRACT_CHARS + 1, True)],
    )
    async def test_extraction_starts_one_past_the_bound(self, chars, extracted):
        content = _log_of(chars)
        result = await _service().classify_and_extract(
            content=content,
            filename="api.log",
            principal="u1",
            user_override=DataType.LOGS_AND_ERRORS,
        )

        # Past the passthrough and not carded. Which extract arrives (the
        # extractor's, or the busy/timeout preview on a loaded runner) is
        # the worker's business, not this rule's.
        if extracted:
            assert result.extraction_method not in (
                "raw_passthrough",
                "classification_failed",
            )
        else:
            assert result.extraction_method == "raw_passthrough"
            assert result.structural_index == content


class TestALargeUncertainUploadIsCutNotCarded:
    @pytest.mark.asyncio
    async def test_a_real_classifier_s_uncertain_paste_passes_through_cut(self):
        """Unforced: no paste rule types the log events, and the paste is not
        held behind a card; it is stored head and tail, the prompt's view
        unchanged."""
        result = await _service().classify_and_extract(
            content=LOG_EVENTS,
            filename=MINTED_PASTE,
            source_metadata=PASTE,
            principal="u1",
        )

        assert _classification(result)["failed"] is True
        assert result.extraction_method == "raw_passthrough"
        assert result.detailed_data_type == DataType.UNSTRUCTURED_TEXT
        assert len(result.structural_index) <= MAX_STORED_EXTRACT_CHARS
        assert result.structural_index.startswith(LOG_EVENTS[:PROMPT_VIEW])
        assert result.structural_index.endswith(LOG_EVENTS[-1000:])
        assert "search_file reads the whole upload" in result.structural_index

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("chars", "cut"),
        [(MAX_STORED_EXTRACT_CHARS, False), (MAX_STORED_EXTRACT_CHARS + 1, True)],
    )
    async def test_the_cut_starts_one_past_the_bound(self, chars, cut):
        content = _log_of(chars)
        result = await _service(uncertain_classifier()).classify_and_extract(
            content=content, filename=MINTED_PASTE, principal="u1"
        )

        assert result.extraction_method == "raw_passthrough"
        assert (result.structural_index != content) is cut
        assert len(result.structural_index) <= MAX_STORED_EXTRACT_CHARS

    @pytest.mark.asyncio
    async def test_a_paste_the_size_of_the_largest_real_ones_is_kept_whole(self):
        """The largest rendered rig pastes under 200 lines run to about 53K:
        under the bound, so they keep the whole passthrough they had."""
        content = _log_of(53_500)
        result = await _service(uncertain_classifier()).classify_and_extract(
            content=content, filename=MINTED_PASTE, principal="u1"
        )

        assert result.extraction_method == "raw_passthrough"
        assert result.structural_index == content

    @pytest.mark.asyncio
    async def test_the_line_cutoff_still_cards(self):
        """The size rule adds to the line count; it does not replace it: at
        ``MIN_EXTRACTION_LINES`` an uncertain upload is carded at any size."""
        content = "\n".join("ok" for _ in range(MIN_EXTRACTION_LINES))

        result = await _service(uncertain_classifier()).classify_and_extract(
            content=content, filename=MINTED_PASTE, principal="u1"
        )

        assert result.extraction_method == "classification_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content", [_json_export(3), _json_log_events(3)], ids=["export", "events"]
)
async def test_the_same_shapes_small_still_pass_through_whole(content):
    """The control: each shape under the bound keeps the whole passthrough,
    so the tests above are not passing because pastes stopped passing
    through or are always cut."""
    result = await _service().classify_and_extract(
        content=content,
        filename=MINTED_PASTE,
        source_metadata=PASTE,
        principal="u1",
    )

    assert result.extraction_method == "raw_passthrough"
    assert result.structural_index == content
    assert "characters not shown here" not in result.structural_index


@pytest.mark.skipif(
    not evidence_metrics.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed; the counter is a no-op",
)
@pytest.mark.asyncio
async def test_a_large_uncertain_upload_counts_as_a_passthrough():
    from prometheus_client import REGISTRY

    def count(outcome: str) -> float:
        value = REGISTRY.get_sample_value(
            "faultmaven_preprocessing_uncertain_classification_total",
            {"outcome": outcome},
        )
        return 0.0 if value is None else value

    before = count("passthrough"), count("clarification")

    await _service(uncertain_classifier()).classify_and_extract(
        content=_log_of(MAX_STORED_EXTRACT_CHARS + 1),
        filename=MINTED_PASTE,
        principal="u1",
    )

    assert (count("passthrough"), count("clarification")) == (
        before[0] + 1,
        before[1],
    )
