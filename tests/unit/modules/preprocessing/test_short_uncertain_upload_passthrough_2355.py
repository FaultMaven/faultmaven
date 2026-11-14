"""#2355: an uncertain upload is put to the user only where its type matters.

No extractor runs, whatever the type, on an upload under
``MIN_EXTRACTION_LINES`` (the small-file passthrough hands the raw content to
the model) or on a shape-valid page capture at any length. A clarification
card there held the upload out of the turn (a three-line preview, no directed
analysis) over a label that changes nothing the model reads. So a short
uncertain upload no extractor would read passes through, typed
``UNSTRUCTURED_TEXT`` rather than the classifier's guess, with the verdict
recorded as uncertain on the result. Where an extractor would run, the type
picks it, and the upload keeps the placeholder that raises the card.

The agent side keys every card effect (the choices, the "not analyzed yet"
note, the exclusion from directed analysis) on
``extraction_method == "classification_failed"``, so the method is what these
pin.
"""

from __future__ import annotations

import pytest

from faultmaven.core.preprocessing.models import to_unified_data_type
from faultmaven.infrastructure.observability import evidence_metrics
from faultmaven.models.api import DataType, SourceMetadata
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.preprocessing_service import (
    MIN_EXTRACTION_LINES,
    PreprocessingService,
)
from tests.unit.modules.preprocessing.extraction_stubs import CrimeScene
from tests.unit.modules.preprocessing.test_classification_failed_preview import (
    AMBIGUOUS_CSV,
)
from tests.unit.modules.preprocessing.uncertain_classifier import uncertain_classifier

pytestmark = pytest.mark.unit

MINTED_PASTE = "pasted-content-20261113T081500.txt"


def _service(classifier: DataClassifier | None = None) -> PreprocessingService:
    return PreprocessingService(
        classifier=classifier or DataClassifier(),
        logs_extractor=CrimeScene(),
    )


def _lines(n: int) -> str:
    """Content of exactly ``n`` lines by the count the service uses."""
    return "\n".join(f"line {i} of mixed output" for i in range(n))


def _classification_meta(result) -> dict:
    return (result.extraction_metadata or {})["evidence_metadata"]["classification"]


class TestShortUncertainUploadPassesThrough:
    @pytest.mark.asyncio
    async def test_the_real_classifier_s_uncertain_csv_reaches_the_model_raw(self):
        """Unforced: the CSV structural gate is unsure about this export on
        its own (0.55, metrics guess), and at 15 lines it passes through."""
        result = await _service().classify_and_extract(
            content=AMBIGUOUS_CSV, filename="ambiguous-service-data.csv", principal="u1"
        )

        assert result.extraction_method == "raw_passthrough"
        assert result.structural_index == AMBIGUOUS_CSV
        assert "Classification uncertain" not in result.structural_index
        assert result.detailed_data_type == DataType.UNSTRUCTURED_TEXT
        assert result.data_type == to_unified_data_type(DataType.UNSTRUCTURED_TEXT)

    @pytest.mark.asyncio
    async def test_the_verdict_stays_recorded_as_uncertain(self):
        result = await _service().classify_and_extract(
            content=AMBIGUOUS_CSV, filename="ambiguous-service-data.csv", principal="u1"
        )

        meta = _classification_meta(result)
        assert meta["failed"] is True
        assert meta["suggested_types"]

    @pytest.mark.asyncio
    async def test_the_guess_is_not_carried_onto_the_upload(self):
        """The type the classifier could not stand behind is dropped, not
        passed on as the upload's type."""
        result = await _service(
            uncertain_classifier(guess=DataType.STRUCTURED_CONFIG)
        ).classify_and_extract(content=_lines(12), filename="notes.txt", principal="u1")

        assert result.extraction_method == "raw_passthrough"
        assert result.detailed_data_type == DataType.UNSTRUCTURED_TEXT
        extractor = (result.extraction_metadata or {})["evidence_metadata"]["extractor"]
        assert extractor["chosen_type"] == DataType.UNSTRUCTURED_TEXT.value

    @pytest.mark.asyncio
    async def test_a_short_paste_is_not_held_behind_a_placeholder(self):
        content = _lines(30)
        result = await _service(uncertain_classifier()).classify_and_extract(
            content=content, filename=MINTED_PASTE, principal="u1"
        )

        assert result.extraction_method == "raw_passthrough"
        assert result.structural_index == content


class TestTheCutoffIsTheExtractionCutoff:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("line_count", "method"),
        [
            (MIN_EXTRACTION_LINES - 1, "raw_passthrough"),
            (MIN_EXTRACTION_LINES, "classification_failed"),
        ],
    )
    async def test_the_card_starts_where_extraction_starts(self, line_count, method):
        result = await _service(uncertain_classifier()).classify_and_extract(
            content=_lines(line_count), filename=MINTED_PASTE, principal="u1"
        )

        assert result.extraction_method == method

    @pytest.mark.asyncio
    async def test_a_long_uncertain_upload_keeps_its_guess_and_placeholder(self):
        result = await _service(
            uncertain_classifier(guess=DataType.METRICS_AND_PERFORMANCE)
        ).classify_and_extract(
            content=_lines(MIN_EXTRACTION_LINES + 50),
            filename="export.csv",
            principal="u1",
        )

        assert result.extraction_method == "classification_failed"
        assert "Classification uncertain" in result.structural_index
        assert result.detailed_data_type == DataType.METRICS_AND_PERFORMANCE


CAPTURE = "page-capture-20261113T081500.txt"
_CAPTURE_SOURCE = SourceMetadata(source_type="page_capture")


def _capture(n: int, *, shaped: bool = True) -> str:
    head = "[captured_at: 2026-11-13T08:15:00Z]\n## Pod status\n" if shaped else ""
    body_lines = n - (2 if shaped else 0)
    return head + "\n".join(f"row {i}: Running" for i in range(body_lines))


def _shape_invalid_count() -> float:
    if not evidence_metrics.PROMETHEUS_AVAILABLE:
        return 0.0
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value("faultmaven_page_capture_shape_invalid_total")
    return 0.0 if value is None else value


class TestLinesAreCountedAsAnEditorCountsThem:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("line_count", "method"),
        [
            (MIN_EXTRACTION_LINES - 1, "raw_passthrough"),
            (MIN_EXTRACTION_LINES, "classification_failed"),
        ],
    )
    async def test_a_final_newline_is_not_a_line(self, line_count, method):
        """A file saved by an editor ends in a newline; that ends its last
        line, so a 199-line file is still under the cutoff."""
        result = await _service(uncertain_classifier()).classify_and_extract(
            content=_lines(line_count) + "\n", filename="app.out", principal="u1"
        )

        assert result.extraction_method == method


class TestTheEpochPolicyFollowsTheGuess:
    """Retyping must not re-open the bug ``_NO_BARE_EPOCH_TYPES`` closes: a
    config the rules could not confirm reading a large integer as a date."""

    CONFIG = "logging:\n  file:\n    maxBytes: 2147483647\n    backupCount: 5\n"

    @pytest.mark.asyncio
    async def test_an_uncertain_config_guess_keeps_bare_epochs_off(self):
        result = await _service(
            uncertain_classifier(guess=DataType.STRUCTURED_CONFIG)
        ).classify_and_extract(content=self.CONFIG, filename="app.conf", principal="u1")

        assert result.detailed_data_type == DataType.UNSTRUCTURED_TEXT
        assert result.coverage_start_ts is None

    @pytest.mark.asyncio
    async def test_an_uncertain_log_guess_keeps_bare_epochs_on(self):
        """The control: the same bytes under a stream-shaped guess do read the
        integer, so the test above is not passing for want of a parse."""
        result = await _service(
            uncertain_classifier(guess=DataType.LOGS_AND_ERRORS)
        ).classify_and_extract(content=self.CONFIG, filename="app.conf", principal="u1")

        assert result.coverage_start_ts is not None


class TestAPageCaptureIsNeverCarded:
    """Path 3 passes a shape-valid capture through at any length, so its type
    never picks an extractor."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("line_count", [12, MIN_EXTRACTION_LINES + 50])
    async def test_an_uncertain_shaped_capture_passes_through(self, line_count):
        result = await _service(uncertain_classifier()).classify_and_extract(
            content=_capture(line_count),
            filename=CAPTURE,
            source_metadata=_CAPTURE_SOURCE,
            principal="u1",
        )

        assert result.extraction_method == "page_capture_passthrough"
        assert result.detailed_data_type == DataType.UNSTRUCTURED_TEXT

    @pytest.mark.asyncio
    async def test_a_short_capture_failing_its_shape_check_takes_the_small_file_path(
        self,
    ):
        """A capture without the shape signals keeps Path 3's fall-through
        (warning and counter) and then goes the way any short upload goes."""
        before = _shape_invalid_count()

        result = await _service(uncertain_classifier()).classify_and_extract(
            content=_capture(12, shaped=False),
            filename=CAPTURE,
            source_metadata=_CAPTURE_SOURCE,
            principal="u1",
        )

        assert result.extraction_method == "raw_passthrough"
        assert result.detailed_data_type == DataType.UNSTRUCTURED_TEXT
        if evidence_metrics.PROMETHEUS_AVAILABLE:
            assert _shape_invalid_count() == before + 1

    @pytest.mark.asyncio
    async def test_a_long_capture_failing_its_shape_check_is_carded(self):
        """A capture without the shape signals is extracted like any upload,
        so at the cutoff its type matters again."""
        result = await _service(uncertain_classifier()).classify_and_extract(
            content=_capture(MIN_EXTRACTION_LINES + 50, shaped=False),
            filename=CAPTURE,
            source_metadata=_CAPTURE_SOURCE,
            principal="u1",
        )

        assert result.extraction_method == "classification_failed"


@pytest.mark.skipif(
    not evidence_metrics.PROMETHEUS_AVAILABLE,
    reason="prometheus_client not installed; the counter is a no-op",
)
class TestEveryUncertainVerdictIsCounted:
    """Without the card, a short uncertain upload leaves no other trace."""

    @staticmethod
    def _count(outcome: str) -> float:
        from prometheus_client import REGISTRY

        value = REGISTRY.get_sample_value(
            "faultmaven_preprocessing_uncertain_classification_total",
            {"outcome": outcome},
        )
        # A child exists from import only under ENABLE_METRICS=true
        # (``create_children``); otherwise from its first increment.
        return 0.0 if value is None else value

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("line_count", "outcome", "other"),
        [
            (MIN_EXTRACTION_LINES - 1, "passthrough", "clarification"),
            (MIN_EXTRACTION_LINES, "clarification", "passthrough"),
        ],
    )
    async def test_each_outcome_counts_once(self, line_count, outcome, other):
        before, before_other = self._count(outcome), self._count(other)

        await _service(uncertain_classifier()).classify_and_extract(
            content=_lines(line_count), filename=MINTED_PASTE, principal="u1"
        )

        assert self._count(outcome) == before + 1
        assert self._count(other) == before_other

    @pytest.mark.asyncio
    async def test_a_confident_upload_is_not_counted(self):
        before = self._count("passthrough") + self._count("clarification")

        await _service().classify_and_extract(
            content=_lines(5),
            filename="app.log",
            principal="u1",
            user_override=DataType.LOGS_AND_ERRORS,
        )

        assert self._count("passthrough") + self._count("clarification") == before
