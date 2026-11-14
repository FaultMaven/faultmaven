"""
Preprocessing Service - Tier 0+1 Pipeline Orchestrator

Coordinates the preprocessing pipeline:
- classify_and_extract(): Unified entry point for all attachments
  (optional dedup → classify → extract with timeout → package PreprocessingResult)

Called from _preprocess_attachment() in the unified turn pipeline (Step 1,
before LLM inference).

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md
"""

import asyncio
import dataclasses
import hashlib
import logging
import math
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from faultmaven.core.preprocessing.evidence_metadata import (
    ClassificationMetadata,
    CoverageMetadata,
    EvidenceMetadata,
    ExtractorAttempt,
    ExtractorMetadata,
)
from faultmaven.core.preprocessing.models import (
    ExtractionResult,
    PreprocessingResult,
    generate_concise_summary,
    to_unified_data_type,
)
from faultmaven.infrastructure import worker_process
from faultmaven.infrastructure.index_slot import index_cores
from faultmaven.infrastructure.observability.evidence_metrics import (
    CASE_ENTITIES_OVERFLOW_TOTAL,
    PREPROCESSING_EXTRACTION_YIELD_RATIO,
    PREPROCESSING_UNCERTAIN_CLASSIFICATION_TOTAL,
)
from faultmaven.models.api import DataType, SourceMetadata
from faultmaven.modules.case.contracts import minted_filename_phrase
from faultmaven.modules.preprocessing import extraction_worker
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.entities import (
    extract_entities_for_data_type,
)
from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.modules.preprocessing.extractors.protocol import (
    Extractor,
    ExtractResult,
)
from faultmaven.modules.preprocessing.extractors.sanity_check import (
    SanityResult,
    run_sanity_check,
)
from faultmaven.modules.preprocessing.extractors.utils import (
    MAX_STORED_EXTRACT_CHARS,
    MAX_STRUCTURAL_INDEX_CHARS,
    cut_to_budget,
    extract_time_range_ts,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Tier 1 timeout: ``extract`` alone, in its worker (Principle 14, #2637); past
# it the worker is killed and the answer is TEXT extraction (preview-only). The
# steps around it have bounds of their own, so what falls back is as on a thread.
TIER1_TIMEOUT_SECONDS = 2.0

#: Extractions at once, capped as indexing is; a slot frees once its worker dies.
TIER1_SLOTS = max(2, int(index_cores()))

#: The wait for a slot: the 2 s the deadline once spent on the executor queue.
#: Past it, the preview with ``busy_fallback``: the server was busy.
TIER1_SLOT_WAIT_SECONDS = 2.0

#: Spawn to ``ready``: the interpreter and imports. Over 100 starts, one, four
#: and eight at once: median 125-136 ms, at most 254 ms; about 8x the slowest.
TIER1_START_SECONDS = 2.0

#: Each transfer, the request in and the result out. At the 10 MB upload
#: limit, one shape per extractor: requests 24-65 ms, the largest result 420 ms
#: (error reports, 25.7 MiB). 0.5 s plus 0.2 s a MB: 2.5 s, about 6x the slowest.
TIER1_TRANSFER_BASE_SECONDS = 0.5
TIER1_TRANSFER_SECONDS_PER_MB = 0.2

#: ``RLIMIT_AS``. At the 10 MB limit a worker starts at 23 MiB, and the most
#: any reached within the deadline is 620 MiB (source code, killed while still
#: growing; error reports finish at 278 MiB). Four times the start and the growth
#: a MB: 2,496 MiB at 10 MB. The worker is the OOM killer's first choice.
TIER1_MEMORY_BASE_MIB = 96
TIER1_MEMORY_MIB_PER_MB = 240

#: What the parent reads. Some results grow with their input: at the 10 MB
#: limit, error reports answer 25.7 MiB (2.6 bytes a byte), the visual
#: placeholder 20.1 MiB. 1 MiB for log records plus 6 MiB a MB, over twice that.
TIER1_OUTPUT_BASE_MIB = 1
TIER1_OUTPUT_MIB_PER_MB = 6

#: The worker module. Always this value: tests point it at one of their own.
_EXTRACTION_WORKER = extraction_worker.__name__

_tier1_slots_state: Optional[
    tuple[asyncio.AbstractEventLoop, worker_process.FairSlots]
] = None


def tier1_limits(upload_mb: int) -> tuple[worker_process.Phases, int, int]:
    """Each step's bound, the address-space limit and the output limit in
    bytes, for an upload limit in MB."""
    transfer = TIER1_TRANSFER_BASE_SECONDS + TIER1_TRANSFER_SECONDS_PER_MB * upload_mb
    phases = worker_process.Phases(
        TIER1_START_SECONDS, transfer, TIER1_TIMEOUT_SECONDS, transfer
    )
    memory = TIER1_MEMORY_BASE_MIB + TIER1_MEMORY_MIB_PER_MB * upload_mb
    output = TIER1_OUTPUT_BASE_MIB + TIER1_OUTPUT_MIB_PER_MB * upload_mb
    return phases, memory * 1024 * 1024, output * 1024 * 1024


def _tier1_slots() -> worker_process.FairSlots:
    """The slots, made on the running loop (``worker_process.loop_slots``)."""
    global _tier1_slots_state
    _tier1_slots_state = worker_process.loop_slots(_tier1_slots_state, TIER1_SLOTS)
    return _tier1_slots_state[1]


# Files smaller than this threshold are passed through raw instead of
# running extraction — the raw content is already a better "file extract"
# than any compressed representation would be. A few lines can carry
# megabytes (a one-line JSON export), so the passthrough's size rule is the
# stored-extract bound, ``MAX_STORED_EXTRACT_CHARS`` (Path 4, #2789).
MIN_EXTRACTION_LINES = 200


# Phase 2 — alt-extractor fallback chain used when the classifier's
# own ``suggested_types`` list is empty (which it is for confident
# classifications, since the classifier only populates suggested_types
# on the classification_failed path). The conservative last-resort:
# UNSTRUCTURED_TEXT, whose extractor always produces valid-looking
# output for any text content. If that also fails sanity (rare), the
# retry loop terminates and direct-truncation ships.
# Two-step degrade chain. UNSTRUCTURED_TEXT is the safest pure-text fallback
# (its extractor produces a valid result for any string), DOCUMENTATION is
# tried after that for markdown- or note-shaped content where the headings/
# title heuristics may add value over straight unstructured truncation.
_FALLBACK_ALT_TYPES: tuple[DataType, ...] = (
    DataType.UNSTRUCTURED_TEXT,
    DataType.DOCUMENTATION,
)

# Hard cap on retries per the plan (Phase 2). Total attempts ≤ initial
# + this many alternatives.
_MAX_ALT_RETRIES = 2


def _build_alternative_chain(
    classification,
    already_tried: list[DataType],
    max_alternatives: int,
) -> list[DataType]:
    """Build the ordered list of alternative DataTypes to try.

    Priority:

    1. ``classification.suggested_types`` — populated by the classifier
       when it short-circuited to the classification_failed path.
    2. ``_FALLBACK_ALT_TYPES`` — a conservative last-resort chain used
       when the classifier was confident but sanity failed anyway.

    Already-tried types are excluded. Result is capped at
    ``max_alternatives``.
    """
    already = set(already_tried)
    alternatives: list[DataType] = []

    for suggested in getattr(classification, "suggested_types", None) or []:
        if suggested in already:
            continue
        alternatives.append(suggested)
        already.add(suggested)
        if len(alternatives) >= max_alternatives:
            return alternatives

    for fallback in _FALLBACK_ALT_TYPES:
        if fallback in already:
            continue
        alternatives.append(fallback)
        already.add(fallback)
        if len(alternatives) >= max_alternatives:
            break

    return alternatives


# =============================================================================
# Content preview for the classification_failed placeholder (ISS-030)
# =============================================================================

# Hard caps on the preview block so it never overflows context budgets.
# CSV/TSV: header + up to 2 data rows; other text: up to 3 lines.
# Per-line cell value truncation prevents single-row overflows.
_PREVIEW_MAX_DATA_ROWS_TABULAR = 2
_PREVIEW_MAX_LINES_TEXT = 3
_PREVIEW_MAX_CELL_CHARS = 80
_PREVIEW_MAX_LINE_CHARS = 240


def _truncate(value: str, limit: int) -> str:
    """Trim a string to ``limit`` chars, appending an ellipsis when shortened."""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _build_content_preview(filename: str, content: str) -> Optional[str]:
    """Build a small orienting preview for the classification_failed placeholder.

    The preview is what lets the agent answer "what columns / structure did
    you detect?" with concrete content. Without it the agent only sees the
    placeholder text and the suggested types — no actual file substance.

    For CSV/TSV files: emits an explicit ``Columns:`` line followed by up to
    two sample data rows. For other text inputs: emits a ``Preview:`` block
    with the first 2-3 non-empty lines.

    Returns ``None`` for empty / whitespace-only content (the UNANALYZABLE
    branch handles that case separately and shouldn't carry a preview).
    """
    if not content or not content.strip():
        return None

    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()

    # --- Tabular path: CSV / TSV — surface columns + sample row(s) ---
    if ext in (".csv", ".tsv"):
        delimiter = "\t" if ext == ".tsv" else ","
        # Split by raw newlines but keep only non-empty lines to skip blank
        # leading/trailing rows. Cap the scan to the first ~5KB so massive
        # files don't waste cycles building a preview we'll truncate anyway.
        lines = [line for line in content[:5000].splitlines() if line.strip()]
        if not lines:
            return None
        header_cells = [
            _truncate(c.strip().strip('"'), _PREVIEW_MAX_CELL_CHARS)
            for c in lines[0].split(delimiter)
        ]
        # Drop empty trailing cells from a stray delimiter at end-of-line.
        while header_cells and not header_cells[-1]:
            header_cells.pop()
        if not header_cells:
            return None
        columns_line = "Columns: " + ", ".join(header_cells)
        sample_lines: list[str] = []
        for raw in lines[1 : 1 + _PREVIEW_MAX_DATA_ROWS_TABULAR]:
            cells = [
                _truncate(c.strip().strip('"'), _PREVIEW_MAX_CELL_CHARS)
                for c in raw.split(delimiter)
            ]
            sample_lines.append(_truncate(", ".join(cells), _PREVIEW_MAX_LINE_CHARS))
        block_lines = [columns_line]
        if sample_lines:
            block_lines.append("Sample row(s): " + " | ".join(sample_lines))
        return "\n".join(block_lines)

    # --- Generic text path: first few non-empty lines ---
    text_lines = [
        _truncate(line.rstrip(), _PREVIEW_MAX_LINE_CHARS)
        for line in content.splitlines()
        if line.strip()
    ]
    if not text_lines:
        return None
    preview_lines = text_lines[:_PREVIEW_MAX_LINES_TEXT]
    return "Preview:\n" + "\n".join(preview_lines)


# Data types where a bare integer is not a timestamp. ``epoch_s`` matches
# ``\b([12]\d{9})\b``, so ``maxBytes: 2147483647`` in a config reads as
# 2038-01-19 and a two-line YAML file reports a 29-year coverage span — while
# the comment on the coverage phase below promises configs yield ``(None,
# None)``.
#
# A denylist, not an allowlist: the fix is aimed at the shapes where the
# pattern is provably wrong, and every stream-shaped type (logs, metrics,
# traces, profiles, command output, error reports, and the unstructured pastes
# that are usually log snippets) keeps parsing epoch timestamps exactly as
# before. Narrowing it further would start discarding real timestamps to avoid
# a mistake those types do not make.
_NO_BARE_EPOCH_TYPES = frozenset(
    {
        DataType.STRUCTURED_CONFIG,
        DataType.SOURCE_CODE,
        DataType.VISUAL_EVIDENCE,
        DataType.DOCUMENTATION,
        DataType.UNANALYZABLE,
    }
)


def _cap_stored_extract(extraction: ExtractionResult) -> ExtractionResult:
    """The extraction with what will be stored held to
    ``MAX_STORED_EXTRACT_CHARS`` (#2789).

    Every path's result is assembled through ``_build_result``, which calls
    this, so the bound holds whichever path made the extract: a passthrough's
    raw content, or an extractor's ``file_extract`` and ``search_map`` (not
    every extractor holds to ``MAX_STRUCTURAL_INDEX_CHARS``). A cut part
    keeps its head and tail around a marker; a cut extractor result also
    says so in its ``file_meta`` (``truncated``), which the prompt renders."""
    payload = extraction.metadata.get("extract_result_json")
    if payload:
        if len(payload) <= MAX_STORED_EXTRACT_CHARS:
            return extraction  # both parts fit inside the whole
        result = ExtractResult.from_json(payload)
        file_extract = cut_to_budget(result.file_extract, MAX_STORED_EXTRACT_CHARS)
        search_map = result.search_map and cut_to_budget(
            result.search_map, MAX_STORED_EXTRACT_CHARS
        )
        if (file_extract, search_map) == (result.file_extract, result.search_map):
            return extraction
        cut = dataclasses.replace(
            result,
            file_extract=file_extract,
            search_map=search_map,
            # The extractor's own "truncated" described its output; the
            # stored extract is now cut, whatever it said.
            file_meta={**result.file_meta, "truncated": True},
        )
        return ExtractionResult(
            method=extraction.method,
            content=file_extract,
            metadata={**extraction.metadata, "extract_result_json": cut.to_json()},
        )
    if len(extraction.content) <= MAX_STORED_EXTRACT_CHARS:
        return extraction
    return ExtractionResult(
        method=extraction.method,
        content=cut_to_budget(extraction.content, MAX_STORED_EXTRACT_CHARS),
        metadata=extraction.metadata,
    )


def _line_count(content: str) -> int:
    """Lines as an editor counts them: a final newline ends the last line
    rather than starting another. Decides the small-file passthrough, and
    with it whether an uncertain upload is put to the user (#2355)."""
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


class PreprocessingService:
    """Tier 0+1 pipeline orchestrator for data preprocessing."""

    def __init__(
        self,
        classifier: DataClassifier,
        logs_extractor: LogsAndErrorsExtractor,
        config_extractor: Optional["StructuredConfigExtractor"] = None,  # noqa: F821
        metrics_extractor: Optional[
            "MetricsAndPerformanceExtractor"  # noqa: F821
        ] = None,
        text_extractor: Optional["UnstructuredTextExtractor"] = None,  # noqa: F821
        source_code_extractor: Optional["SourceCodeExtractor"] = None,  # noqa: F821
        visual_extractor: Optional["VisualEvidenceExtractor"] = None,  # noqa: F821
        trace_extractor: Optional["TraceDataExtractor"] = None,  # noqa: F821
        profiling_extractor: Optional["ProfilingDataExtractor"] = None,  # noqa: F821
        error_report_extractor: Optional["ErrorReportExtractor"] = None,  # noqa: F821
        documentation_extractor: Optional[
            "DocumentationExtractor"  # noqa: F821
        ] = None,
        command_output_extractor: Optional[
            "CommandOutputExtractor"  # noqa: F821
        ] = None,
    ):
        """
        Initialize preprocessing service.

        Args:
            classifier: Data classification service
            logs_extractor: LOGS_AND_ERRORS extractor
            config_extractor: STRUCTURED_CONFIG extractor (optional)
            metrics_extractor: METRICS_AND_PERFORMANCE extractor (optional)
            text_extractor: UNSTRUCTURED_TEXT extractor (optional)
            source_code_extractor: SOURCE_CODE extractor (optional)
            visual_extractor: VISUAL_EVIDENCE extractor (optional)
            trace_extractor: TRACE_DATA extractor (optional)
            profiling_extractor: PROFILING_DATA extractor (optional)
            error_report_extractor: ERROR_REPORT extractor (optional)
            documentation_extractor: DOCUMENTATION extractor (optional)
            command_output_extractor: COMMAND_OUTPUT extractor (optional)

        Note:
            PII/secret redaction is applied at the LLM boundary, not here,
            so no sanitizer is needed in this pipeline.
        """
        self.classifier = classifier

        # Extractor registry — unavailable types fall through to direct truncation.
        self.extractors: Dict[DataType, Extractor] = {
            DataType.LOGS_AND_ERRORS: logs_extractor,
        }

        if config_extractor:
            self.extractors[DataType.STRUCTURED_CONFIG] = config_extractor

        if metrics_extractor:
            self.extractors[DataType.METRICS_AND_PERFORMANCE] = metrics_extractor

        if text_extractor:
            self.extractors[DataType.UNSTRUCTURED_TEXT] = text_extractor

        if source_code_extractor:
            self.extractors[DataType.SOURCE_CODE] = source_code_extractor

        if visual_extractor:
            self.extractors[DataType.VISUAL_EVIDENCE] = visual_extractor

        # New extractors
        if trace_extractor:
            self.extractors[DataType.TRACE_DATA] = trace_extractor

        if profiling_extractor:
            self.extractors[DataType.PROFILING_DATA] = profiling_extractor

        if error_report_extractor:
            self.extractors[DataType.ERROR_REPORT] = error_report_extractor

        if documentation_extractor:
            self.extractors[DataType.DOCUMENTATION] = documentation_extractor

        if command_output_extractor:
            self.extractors[DataType.COMMAND_OUTPUT] = command_output_extractor

        # The Tier 1 worker imports each extractor's class by name: refuse here
        # one it cannot import, not as a preview at run time.
        for extractor in filter(None, self.extractors.values()):
            name = f"{type(extractor).__module__}:{type(extractor).__qualname__}"
            if name.startswith("__main__:") or "." in name.partition(":")[2]:
                raise ValueError(f"{name} cannot run in the Tier 1 worker")

        # Phase 4 — entity registry settings (feature flag + hard cap).
        # Read once at construction so per-call overhead is zero; hot-
        # reload in cloud mode flows through the settings singleton at
        # the next service instance.
        from faultmaven.config.settings import get_settings

        self._upload_mb: int = get_settings().upload.max_upload_size_mb
        preprocessing_cfg = get_settings().preprocessing
        self._entity_registry_enabled: bool = bool(
            getattr(preprocessing_cfg, "entity_registry_enabled", False)
        )
        self._entity_registry_cap: int = int(
            getattr(preprocessing_cfg, "entity_registry_cap_per_type", 500)
        )

    async def classify_and_extract(
        self,
        content: str,
        filename: str = "pasted_content.txt",
        source_metadata: Optional[SourceMetadata] = None,
        user_override: Optional[DataType] = None,
        *,
        principal: str,
    ) -> PreprocessingResult:
        """
        Tier 0+1 unified entry point: classify content, run matched extractor, package result.

        Called from `_preprocess_attachment()` in the unified turn pipeline
        for every attachment (file uploads and pasted text).

        Three special paths short-circuit extraction:
        - **Page capture**: content is already structured by the copilot's
          htmlToStructuredText; we pass through with a char cap instead of
          re-parsing with the UnstructuredTextExtractor.
        - **UNANALYZABLE**: user opted out (e.g., via VISUAL_EVIDENCE with
          vision disabled). Returns a reference-only placeholder.
        - **classification_failed** where an extractor will run: the
          classifier could not decide the type, and the type would pick the
          extractor. Returns a placeholder carrying
          suggested types; the turn offers them as clarification choices,
          and a choice re-runs this under ``user_override``. An uncertain
          upload no extractor would read (under ``MIN_EXTRACTION_LINES``, or
          a shape-valid page capture) is not put to the user: it takes its
          passthrough typed UNSTRUCTURED_TEXT (#2355). Every stored extract
          is held to ``MAX_STORED_EXTRACT_CHARS``, head and tail (#2789).

        Phase 1.5 — Reclassification path: the caller can pass
        ``user_override`` (typically from the ``PATCH /evidence/{id}/classification``
        endpoint or the ``reclassify_evidence`` agent tool) to force the
        classifier to return ``source="user_override", confidence=1.0``
        for the given DataType. The rest of the pipeline (extractor
        dispatch, metadata assembly) is unchanged; the resulting
        PreprocessingResult's ``extraction_metadata.evidence_metadata``
        records ``classification.source = "user_override"`` so the
        context builder's low-confidence marker is suppressed on the
        updated evidence row.

        PII redaction is handled at the LLM boundary (MilestoneEngine),
        not here. Structural indexes are stored raw.

        ``principal`` is the user whose turn this is: their extraction waits
        for a worker slot under Principle 14's admission rule (#2645).

        Returns:
            PreprocessingResult with structural_index, summary, and
            extraction metadata. Never raises for extraction failures —
            falls back to direct truncation (see `_extract_with_timeout`).
        """
        start_time = time.time()

        # Tier 0: Classify
        classification = self.classifier.classify(
            filename,
            content,
            source_metadata=source_metadata,
            user_override=user_override,
        )

        detailed_data_type = classification.data_type
        unified_data_type = to_unified_data_type(detailed_data_type)

        logger.info(
            f"classify_and_extract: {filename} → {detailed_data_type.value} "
            f"(unified={unified_data_type.value}, "
            f"source={classification.source}, "
            f"confidence={classification.confidence:.2f})"
        )

        # Path 1: UNANALYZABLE — reference only.
        # Two sub-paths share this branch:
        #   1. Empty content (0-byte file or whitespace-only paste). The
        #      classifier short-circuits here so the user gets a clear
        #      "file is empty" message rather than a misleading
        #      classification_failed modal.
        #   2. User explicitly opted out (e.g. VISUAL_EVIDENCE with vision
        #      disabled). The classifier returns UNANALYZABLE via
        #      user_override.
        if detailed_data_type == DataType.UNANALYZABLE:
            is_empty_content = not content or not content.strip()
            if is_empty_content:
                placeholder_text = (
                    f"[File '{filename}' is empty (0 bytes of analyzable "
                    f"content) — recorded as evidence; nothing to extract]"
                )
            else:
                placeholder_text = (
                    f"[File '{filename}' marked as UNANALYZABLE — "
                    f"reference only, no analysis performed]"
                )
            return self._build_placeholder_result(
                content=content,
                classification=classification,
                detailed_data_type=detailed_data_type,
                unified_data_type=unified_data_type,
                placeholder_text=placeholder_text,
                extraction_method="none",
                start_time=start_time,
            )

        # An uncertain verdict (classification_failed) is put to the user only
        # where an extractor reads the upload, because only there does the
        # type change what the model sees (#2355). The paths below decide
        # that themselves: each passthrough (Path 3, Path 4) takes an
        # uncertain upload as it is, retyped by ``_settle_uncertain``, and
        # the clarification placeholder (Path 2) sits directly before the
        # extractor dispatch, the one place the type is used. A card would
        # otherwise hold the upload out of the turn (a three-line preview,
        # no directed analysis) with analysis two more user turns away.
        triggered_by = "user_override" if user_override is not None else "initial"

        # Path 3: page capture — already structured, pass through with char cap.
        # Validate the structural shape before bypassing the extractor so a
        # mislabeled or maliciously-shaped paste cannot use the page_capture
        # source_type to skip Tier-1 extraction. Real Copilot captures from
        # htmlToStructuredText emit a `[captured_at: …]` preamble or `## `
        # headings (typically both); a capture with neither falls through
        # with a logged warning.
        if classification.source_type == "page_capture":
            has_captured_at = "[captured_at:" in content[:512]
            # A `## ` heading at the start of the content or after a newline.
            has_h2_heading = content.startswith("## ") or "\n## " in content

            if has_captured_at or has_h2_heading:
                logger.info(
                    "classify_and_extract: page_capture detected, "
                    "skipping extractor (content already structured)"
                )
                detailed_data_type, unified_data_type, epoch_policy_type = (
                    self._settle_uncertain(
                        classification, filename, content, detailed_data_type
                    )
                )
                extraction = ExtractionResult(
                    method="page_capture_passthrough",
                    content=self._fallback_direct_extraction(content),
                    metadata={
                        "passthrough": True,
                        "source_type": "page_capture",
                        "shape_signals": {
                            "captured_at_preamble": has_captured_at,
                            "h2_heading": has_h2_heading,
                        },
                    },
                )
                return self._build_result(
                    content=content,
                    extraction=extraction,
                    detailed_data_type=detailed_data_type,
                    unified_data_type=unified_data_type,
                    classification=classification,
                    start_time=start_time,
                    triggered_by=triggered_by,
                    epoch_policy_type=epoch_policy_type,
                )

            # Shape invalid — log and fall through to standard dispatch.
            # Counter for ops visibility: spikes here indicate either a
            # producer-side change in htmlToStructuredText or a misuse of
            # `input_type=page_capture` from a non-Copilot client.
            logger.warning(
                "classify_and_extract: page_capture shape invalid, "
                "falling through to standard extractor dispatch",
                extra={
                    "attachment_filename": filename,
                    "content_prefix": content[:120],
                    "captured_at_preamble": has_captured_at,
                    "h2_heading": has_h2_heading,
                },
            )
            try:
                from faultmaven.infrastructure.observability.evidence_metrics import (
                    PAGE_CAPTURE_SHAPE_INVALID_TOTAL,
                )

                PAGE_CAPTURE_SHAPE_INVALID_TOTAL.inc()
            except ImportError:
                # Counter is optional; don't fail extraction on metrics absence.
                pass

        # Path 4: small-file passthrough — raw content is a better file
        # extract than any compressed representation, and extraction adds
        # no value for files that fit in the LLM context window directly.
        # Its size rule is the stored-extract bound (#2789): past
        # ``MAX_STORED_EXTRACT_CHARS`` a confident upload goes to its
        # extractor, while an uncertain one still passes through, cut by
        # ``_build_result`` like any extract. It is not put to the user, by
        # ruling (owner, 2026-11-14): the card would hold it out of the turn,
        # and the whole upload stays readable by search_file.
        line_count = _line_count(content)
        over_bound = len(content) > MAX_STORED_EXTRACT_CHARS
        if line_count < MIN_EXTRACTION_LINES and (
            not over_bound or classification.classification_failed
        ):
            logger.info(
                "classify_and_extract: %s has %d lines (< %d) and %d chars "
                "(stored bound %d) — raw passthrough, skipping extraction",
                filename,
                line_count,
                MIN_EXTRACTION_LINES,
                len(content),
                MAX_STORED_EXTRACT_CHARS,
            )
            detailed_data_type, unified_data_type, epoch_policy_type = (
                self._settle_uncertain(
                    classification, filename, content, detailed_data_type
                )
            )
            extraction = ExtractionResult(
                method="raw_passthrough",
                content=content,
                metadata={"passthrough": True, "source_type": "small_file"},
            )
            return self._build_result(
                content=content,
                extraction=extraction,
                detailed_data_type=detailed_data_type,
                unified_data_type=unified_data_type,
                classification=classification,
                start_time=start_time,
                triggered_by=triggered_by,
                epoch_policy_type=epoch_policy_type,
            )

        # Path 2: classification_failed where an extractor would read the
        # upload, so the type picks it: put the type to the user. A choice
        # re-runs this method under ``user_override``.
        if classification.classification_failed:
            PREPROCESSING_UNCERTAIN_CLASSIFICATION_TOTAL.labels(
                outcome="clarification"
            ).inc()
            logger.warning(
                f"Classification failed for {filename} "
                f"(confidence={classification.confidence:.2f}) — "
                f"requesting user input"
            )
            suggested = ", ".join(
                str(t.value) for t in (classification.suggested_types or [])
            )
            # ISS-030: surface a small content preview (header + sample row for
            # CSV/TSV; first few lines otherwise) so the agent can describe
            # *what* about the file is ambiguous when the user asks q4-style
            # questions ("what columns/structure was detected?"). Without this,
            # the placeholder text alone has no concrete data for the agent to
            # cite — it can only repeat the suggested types.
            preview_block = _build_content_preview(filename, content)
            # This text is not internal: it lands on ``summary`` and
            # ``structural_index``, so it is rendered into <summary> and
            # <file_extract> for the model AND appended verbatim to
            # ``agent_response`` by ``_handle_file_reclassification``. Naming
            # a minted ``pasted-content-<ts>.txt`` here put it beside a
            # sentence that had just called it "the text you pasted" (#666,
            # #1198 review). ``filename`` itself is untouched -- only how it
            # is SPOKEN changes.
            subject = minted_filename_phrase(filename) or f"'{filename}'"
            placeholder_text = (
                f"[Classification uncertain for {subject} — "
                f"requesting user input]\nSuggested types: {suggested}"
            )
            if preview_block:
                placeholder_text = f"{placeholder_text}\n{preview_block}"
            return self._build_placeholder_result(
                content=content,
                classification=classification,
                detailed_data_type=detailed_data_type,
                unified_data_type=unified_data_type,
                placeholder_text=placeholder_text,
                extraction_method="classification_failed",
                start_time=start_time,
            )

        # Standard path: dispatch to type-specific extractor. When the
        # Phase 2 feature flag is on, the dispatch runs sanity checks
        # and retries alternatives on failure; otherwise it's a single-
        # shot run matching pre-Phase-2 behaviour.
        return await self._dispatch_with_optional_retry(
            content=content,
            filename=filename,
            principal=principal,
            classification=classification,
            initial_data_type=detailed_data_type,
            initial_triggered_by=triggered_by,
            start_time=start_time,
        )

    @staticmethod
    def _settle_uncertain(
        classification, filename: str, content: str, detailed_data_type
    ):
        """The type a passthrough records, and the type whose bare-epoch
        timestamp policy applies to it.

        A confident verdict keeps its type for both. An uncertain one (#2355)
        is not put to the user, because no extractor reads a passthrough: it
        is typed UNSTRUCTURED_TEXT rather than the guess the classifier
        could not stand behind, while the epoch policy stays the guess's, as
        it was under the clarification placeholder (an unconfirmed config
        must not start reading ``maxBytes: 2147483647`` as 2038). The verdict
        stays recorded as uncertain on the result's
        ``evidence_metadata.classification``, which intake does not persist
        (#1710); the counter is the lasting record."""
        if not classification.classification_failed:
            return (
                detailed_data_type,
                to_unified_data_type(detailed_data_type),
                detailed_data_type,
            )
        PREPROCESSING_UNCERTAIN_CLASSIFICATION_TOTAL.labels(outcome="passthrough").inc()
        logger.info(
            "classify_and_extract: %s classification uncertain "
            "(confidence=%.2f, guess=%s, %d lines) and no extractor runs — "
            "passthrough as unstructured_text, no clarification",
            filename,
            classification.confidence,
            detailed_data_type.value,
            _line_count(content),
        )
        return (
            DataType.UNSTRUCTURED_TEXT,
            to_unified_data_type(DataType.UNSTRUCTURED_TEXT),
            detailed_data_type,
        )

    async def _dispatch_with_optional_retry(
        self,
        content: str,
        filename: str,
        classification,
        initial_data_type: DataType,
        initial_triggered_by: str,
        start_time: float,
        principal: str,
    ) -> PreprocessingResult:
        """Run the initial extractor, then retry with alternatives on
        sanity-check failure (Phase 2 feature flag). When the flag is
        off, matches pre-Phase-2 behaviour exactly: one extraction, no
        sanity check, no retry.

        Returns the PreprocessingResult corresponding to whichever
        attempt passed — or the last attempt if all failed, with
        ``metadata.extractor.chosen_type = "direct_fallback"`` so
        observability records the drop-through.
        """
        from faultmaven.config.settings import get_settings

        retry_enabled = get_settings().preprocessing.extractor_retry_enabled

        initial = await self._run_single_dispatch(
            content, filename, initial_data_type, principal
        )

        if not retry_enabled:
            # Phase 0 / Phase 1 behaviour: single-shot, no sanity check.
            return self._build_result(
                content=content,
                extraction=initial,
                detailed_data_type=initial_data_type,
                unified_data_type=to_unified_data_type(initial_data_type),
                classification=classification,
                start_time=start_time,
                triggered_by=initial_triggered_by,
            )

        # Phase 2 retry path. Record each attempt's sanity result so the
        # final ``extractor.attempts`` list tells the full story.
        attempts: list[ExtractorAttempt] = []
        initial_sanity = run_sanity_check(initial_data_type, content, initial.content)
        attempts.append(
            ExtractorAttempt(
                data_type=initial_data_type.value,
                sanity_passed=initial_sanity.passed,
                duration_ms=0,  # filled in by _build_result_with_attempts
                triggered_by=initial_triggered_by,
            )
        )

        if initial_sanity.passed:
            return self._build_result_with_attempts(
                content=content,
                extraction=initial,
                detailed_data_type=initial_data_type,
                unified_data_type=to_unified_data_type(initial_data_type),
                classification=classification,
                start_time=start_time,
                attempts=attempts,
            )

        # Sanity failed. Retry with alternatives, bounded.
        alt_types = _build_alternative_chain(
            classification=classification,
            already_tried=[initial_data_type],
            max_alternatives=_MAX_ALT_RETRIES,
        )

        winning_extraction: Optional[ExtractionResult] = initial
        winning_type: DataType = initial_data_type
        winning_passed = False

        for alt_type in alt_types:
            logger.info(
                "Phase 2 retry: initial extractor for %s failed sanity "
                "(reason=%s); trying %s",
                initial_data_type.value,
                initial_sanity.reason,
                alt_type.value,
            )
            alt_extraction = await self._run_single_dispatch(
                content, filename, alt_type, principal
            )
            alt_sanity = run_sanity_check(alt_type, content, alt_extraction.content)
            attempts.append(
                ExtractorAttempt(
                    data_type=alt_type.value,
                    sanity_passed=alt_sanity.passed,
                    duration_ms=0,
                    triggered_by="sanity_retry",
                )
            )
            if alt_sanity.passed:
                winning_extraction = alt_extraction
                winning_type = alt_type
                winning_passed = True
                break

        if not winning_passed:
            # All candidates failed sanity. Fall back to direct truncation
            # and record the drop-through in evidence metadata so ops can
            # see which content types systematically defeat the sanity
            # checks. ExtractionResult.method is a constrained literal —
            # we reuse ``"direct"`` here and rely on the metadata block
            # carrying the ``chosen_type = "direct_fallback"`` signal.
            logger.warning(
                "Phase 2 retry exhausted for %s: all %d candidates failed "
                "sanity; falling back to direct truncation",
                filename,
                len(attempts),
            )
            winning_extraction = ExtractionResult(
                method="direct",
                content=self._fallback_direct_extraction(content),
                metadata={"fallback": True, "all_retries_failed": True},
            )
            winning_type = DataType.UNSTRUCTURED_TEXT

        return self._build_result_with_attempts(
            content=content,
            extraction=winning_extraction,
            detailed_data_type=winning_type,
            unified_data_type=to_unified_data_type(winning_type),
            classification=classification,
            start_time=start_time,
            attempts=attempts,
            direct_fallback=not winning_passed,
        )

    async def _run_single_dispatch(
        self,
        content: str,
        filename: str,
        data_type: DataType,
        principal: str,
    ) -> ExtractionResult:
        """Run one extractor and return its ExtractionResult.

        Encapsulates the "no extractor registered for this type →
        direct truncation" fallback so the retry loop can treat all
        dispatches uniformly.
        """
        extractor = self.extractors.get(data_type)
        if extractor:
            return await self._extract_with_timeout(
                extractor, content, filename, principal
            )
        logger.warning(f"No extractor for {data_type.value}, using direct truncation")
        return ExtractionResult(
            method="direct",
            content=self._fallback_direct_extraction(content),
            metadata={"fallback": True},
        )

    async def reclassify_evidence(
        self,
        content: str,
        filename: str,
        user_override: DataType,
        previous_metadata: Optional[Dict[str, Any]] = None,
        *,
        principal: str,
    ) -> PreprocessingResult:
        """Re-run the preprocessing pipeline on *content* under a
        user-specified data type.

        Implements Phase 1.5 (see
        ``docs/working/WIP-data-processing-improvement-plan.md``). The
        caller — typically the PATCH ``/classification`` endpoint or the
        ``reclassify_evidence`` agent tool — provides the raw file
        bytes, the chosen data_type, and the existing evidence's
        metadata so we can preserve its ``extractor.attempts`` history.

        The resulting ``PreprocessingResult.extraction_metadata
        ["evidence_metadata"]`` carries:

        - ``classification.source = "user_override"`` and
          ``confidence = 1.0`` (Phase 1's low-confidence marker is
          consequently suppressed on the updated row).
        - ``extractor.chosen_type`` = the requested type's string value.
        - ``extractor.attempts`` = the previous attempts list with the
          new attempt appended. Hard cap: 5 entries; oldest rotate off
          the head.

        Cap chosen to bound row growth on repeated corrections without
        losing the initial-classification record, which is typically
        the interesting one (it's what was wrong).
        """
        result = await self.classify_and_extract(
            content=content,
            filename=filename,
            user_override=user_override,
            principal=principal,
        )
        return self._merge_attempts(result, previous_metadata)

    _MAX_ATTEMPTS_KEPT = 5

    def _merge_attempts(
        self,
        result: PreprocessingResult,
        previous_metadata: Optional[Dict[str, Any]],
    ) -> PreprocessingResult:
        """Prepend previous attempts onto the freshly-written ones.

        Separated from ``reclassify_evidence`` for testability and
        because Phase 2 will reuse it for sanity-check retries.
        """
        if not previous_metadata:
            return result

        prior = previous_metadata.get("extractor", {}).get("attempts", [])
        if not isinstance(prior, list) or not prior:
            return result

        extraction_metadata = dict(result.extraction_metadata or {})
        new_meta = dict(extraction_metadata.get("evidence_metadata") or {})
        extractor_block = dict(new_meta.get("extractor") or {})

        current_attempts = list(extractor_block.get("attempts") or [])
        combined = [dict(a) for a in prior if isinstance(a, dict)] + current_attempts
        # Keep the most recent N entries. When trimming, drop the oldest
        # head so the initial-classification record eventually rolls off
        # but the latest truth is preserved.
        if len(combined) > self._MAX_ATTEMPTS_KEPT:
            combined = combined[-self._MAX_ATTEMPTS_KEPT :]

        extractor_block["attempts"] = combined
        new_meta["extractor"] = extractor_block
        extraction_metadata["evidence_metadata"] = new_meta
        result.extraction_metadata = extraction_metadata
        return result

    def _build_result_with_attempts(
        self,
        content: str,
        extraction: ExtractionResult,
        detailed_data_type: DataType,
        unified_data_type,
        classification,
        start_time: float,
        attempts: list[ExtractorAttempt],
        direct_fallback: bool = False,
    ) -> PreprocessingResult:
        """Variant of ``_build_result`` used by the Phase 2 retry path
        to override the default single-attempt list with the full
        sequence observed during retries.

        When ``direct_fallback`` is True, the metadata records
        ``extractor.chosen_type = "direct_fallback"`` so ops can see
        the retry loop exhausted alternatives.
        """
        result = self._build_result(
            content=content,
            extraction=extraction,
            detailed_data_type=detailed_data_type,
            unified_data_type=unified_data_type,
            classification=classification,
            start_time=start_time,
            triggered_by=attempts[-1].triggered_by if attempts else "initial",
        )

        # Overwrite the single-attempt block that _build_result wrote
        # with the full retry sequence. Also record the direct-fallback
        # signal if applicable.
        extraction_metadata = dict(result.extraction_metadata or {})
        new_meta = dict(extraction_metadata.get("evidence_metadata") or {})
        extractor_block = dict(new_meta.get("extractor") or {})
        extractor_block["attempts"] = [
            a.model_dump(exclude_none=True) for a in attempts
        ]
        if direct_fallback:
            extractor_block["chosen_type"] = "direct_fallback"
        new_meta["extractor"] = extractor_block
        extraction_metadata["evidence_metadata"] = new_meta
        result.extraction_metadata = extraction_metadata
        return result

    def _build_result(
        self,
        content: str,
        extraction: ExtractionResult,
        detailed_data_type: DataType,
        unified_data_type,
        classification,
        start_time: float,
        triggered_by: str = "initial",
        epoch_policy_type: Optional[DataType] = None,
    ) -> PreprocessingResult:
        """Assemble a PreprocessingResult from an ExtractionResult.

        Attaches the ``evidence_metadata`` block under extraction_metadata
        so the calling investigation service can lift it onto the
        Evidence row without re-deriving classifier signals. See
        docs/architecture/data-and-storage/schemas/case-schema.md §4.3
        'evidence.metadata JSON contract'.

        ``triggered_by`` labels the ``ExtractorAttempt`` entry written
        into ``metadata.extractor.attempts``. ``"initial"`` is the
        upload path; ``"user_override"`` is Phase 1.5 reclassification.
        Phase 2 will add ``"sanity_retry"``.
        """
        processing_time_ms = int((time.time() - start_time) * 1000)
        extraction = _cap_stored_extract(extraction)
        content_size = len(content.encode("utf-8"))
        _index_str = (
            extraction.metadata.get("extract_result_json") or extraction.content
        )
        index_size = len(_index_str.encode("utf-8"))
        compression_ratio = index_size / max(content_size, 1)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Phase 2c — extraction yield ratio (index / raw). A drift
        # signal per data_type: when a type's yield baseline shifts
        # abruptly, the content distribution probably changed and the
        # extractor is producing garbage for a new sub-format. Guarded
        # against zero-byte input so the histogram never records a
        # spurious 0/0 = NaN.
        if content_size > 0:
            PREPROCESSING_EXTRACTION_YIELD_RATIO.labels(
                data_type=detailed_data_type.value
            ).observe(index_size / content_size)

        # Phase 3 — case-level timeline. Parse timestamps from the raw
        # content and attach them to the result so the investigation
        # service can lift them onto the Evidence row's
        # coverage_*_ts columns. Best-effort: any parsing failure
        # silently yields (None, None) and the row gets NULL coverage —
        # which matches the valid state for content without parseable
        # timestamps (configs, code, screenshots, short pastes).
        coverage_start_ts = None
        coverage_end_ts = None
        coverage_source: str | None = None
        # ``epoch_policy_type`` is passed only by a passthrough of an
        # uncertain verdict (``_settle_uncertain``, #2355); everywhere else
        # the recorded type decides.
        epoch_policy_type = epoch_policy_type or detailed_data_type
        try:
            coverage_start_ts, coverage_end_ts, coverage_source = extract_time_range_ts(
                content,
                allow_bare_epoch=epoch_policy_type not in _NO_BARE_EPOCH_TYPES,
            )
        except Exception:
            # extract_time_range_ts already swallows per-line parse
            # errors; a top-level exception here would be unusual.
            # Leave both None and carry on.
            logger.debug(
                "extract_time_range_ts failed; coverage timestamps will be NULL"
            )

        # Build the metadata block. Coverage sub-block is populated only
        # when at least the start timestamp was parsed — absent-is-valid
        # per the namespaced contract in case-schema.md §4.3.
        coverage_meta: Optional[CoverageMetadata] = None
        if coverage_start_ts is not None:
            coverage_meta = CoverageMetadata(source=coverage_source)

        evidence_meta = EvidenceMetadata(
            classification=ClassificationMetadata(
                confidence=float(classification.confidence),
                source=str(classification.source),
                failed=bool(classification.classification_failed),
                suggested_types=[
                    t.value for t in (classification.suggested_types or [])
                ],
            ),
            extractor=ExtractorMetadata(
                chosen_type=detailed_data_type.value,
                attempts=[
                    ExtractorAttempt(
                        data_type=detailed_data_type.value,
                        sanity_passed=True,
                        duration_ms=processing_time_ms,
                        triggered_by=triggered_by,
                    )
                ],
            ),
            coverage=coverage_meta,
        )

        merged_metadata: Dict[str, object] = dict(extraction.metadata or {})
        merged_metadata["evidence_metadata"] = evidence_meta.to_storage_dict()

        # Phase 4 — entity registry (feature-flagged). Runs the
        # per-data-type EntityExtractor against the raw content and
        # applies the configured per-(evidence, entity_type) hard cap.
        # Overflow types are recorded so the agent knows the registry
        # is incomplete for those pairs. When the feature is disabled
        # or the data type has no registered extractor, the
        # observation list is empty and no cap logic runs.
        entities_payload: list[Dict[str, Any]] = []
        overflow_types: list[str] = []
        if self._entity_registry_enabled and not merged_metadata.get("placeholder"):
            try:
                observations = extract_entities_for_data_type(
                    detailed_data_type, content
                )
            except Exception as exc:
                # Entity extraction is best-effort — a regex bug must
                # not block evidence persistence. Log and degrade.
                logger.warning(
                    "Entity extraction failed for %s: %s",
                    detailed_data_type.value,
                    exc,
                )
                observations = []

            if observations:
                cap = self._entity_registry_cap
                # Bucket observations by entity_type so the cap is
                # applied per-type (Phase 4 contract). Sort each
                # bucket by mention_count DESC before truncation so
                # the retained rows are the most mentioned.
                by_type: Dict[str, list] = {}
                for obs in observations:
                    by_type.setdefault(obs.entity_type.value, []).append(obs)
                for type_key, bucket in by_type.items():
                    if len(bucket) > cap:
                        overflow_types.append(type_key)
                        # Phase 4 telemetry: one increment per
                        # (evidence, type) overflow event — not per
                        # excess row. Exit-criteria dashboard reads
                        # the ratio of overflow/writes per type.
                        CASE_ENTITIES_OVERFLOW_TOTAL.labels(entity_type=type_key).inc()
                        bucket.sort(key=lambda o: o.mention_count, reverse=True)
                        del bucket[cap:]
                    for obs in bucket:
                        entities_payload.append(
                            {
                                "entity_type": obs.entity_type.value,
                                "entity_value": obs.entity_value,
                                "mention_count": int(obs.mention_count),
                                "in_error_context": bool(obs.in_error_context),
                            }
                        )

        if overflow_types:
            # Stable shape under metadata.entities — Phase 4c tooling
            # reads it to warn the agent that registry lookups for
            # these types may be incomplete.
            stored_meta = merged_metadata["evidence_metadata"]
            if isinstance(stored_meta, dict):
                entities_block = stored_meta.setdefault("entities", {})
                if isinstance(entities_block, dict):
                    entities_block["overflow_types"] = sorted(set(overflow_types))

        return PreprocessingResult(
            data_type=unified_data_type,
            detailed_data_type=detailed_data_type,
            summary=generate_concise_summary(extraction.content),
            structural_index=extraction.metadata.get("extract_result_json")
            or extraction.content,
            content_ref=None,
            content_size_bytes=content_size,
            content_type="text/plain",
            extraction_method=extraction.method,
            compression_ratio=compression_ratio,
            extraction_metadata=merged_metadata,
            content_hash=content_hash,
            processing_time_ms=processing_time_ms,
            coverage_source=coverage_source,
            coverage_start_ts=coverage_start_ts,
            coverage_end_ts=coverage_end_ts,
            entities=entities_payload,
            entity_overflow_types=sorted(set(overflow_types)),
        )

    def _build_placeholder_result(
        self,
        content: str,
        classification,
        detailed_data_type: DataType,
        unified_data_type,
        placeholder_text: str,
        extraction_method: str,
        start_time: float,
    ) -> PreprocessingResult:
        """Assemble a placeholder PreprocessingResult for UNANALYZABLE / classification_failed paths."""
        extraction = ExtractionResult(
            method=extraction_method,
            content=placeholder_text,
            metadata={
                "placeholder": True,
                "classification_source": classification.source,
                "confidence": classification.confidence,
                "suggested_types": [
                    str(t.value) for t in (classification.suggested_types or [])
                ],
            },
        )
        return self._build_result(
            content=content,
            extraction=extraction,
            detailed_data_type=detailed_data_type,
            unified_data_type=unified_data_type,
            classification=classification,
            start_time=start_time,
        )

    def _fallback_direct_extraction(
        self, content: str, max_chars: int = MAX_STRUCTURAL_INDEX_CHARS
    ) -> str:
        """Fallback extraction for types without extractors: truncate to max_chars."""
        if len(content) <= max_chars:
            return content
        return (
            content[:max_chars]
            + f"\n\n... [Truncated {len(content) - max_chars} chars]"
        )

    async def _extract_with_timeout(
        self,
        extractor: object,
        content: str,
        filename: str,
        principal: str,
    ) -> ExtractionResult:
        """
        Run a Tier 1 extractor in a worker process its deadline kills.

        ``extract`` past ``TIER1_TIMEOUT_SECONDS`` is the TEXT preview with
        ``timeout_fallback``; no slot within ``TIER1_SLOT_WAIT_SECONDS``, with
        ``busy_fallback``; any other end without a result, with
        ``error_fallback`` and its reason (Principle 14, #2637).

        Design Reference:
            data-preprocessing-design-specification.md Section 2.4
        """
        cls = type(extractor)
        phases, memory_bytes, output_limit_bytes = tier1_limits(self._upload_mb)

        def preview(**metadata) -> ExtractionResult:
            # Fallback: truncated preview (always fast)
            return ExtractionResult(
                method="structure_extraction",
                content=self._fallback_direct_extraction(content),
                metadata=metadata,
            )

        slots = _tier1_slots()
        try:
            async with asyncio.timeout(TIER1_SLOT_WAIT_SECONDS):
                await slots.acquire(principal)
        except TimeoutError:
            logger.warning(
                f"Tier 1 extraction found no slot free within "
                f"{TIER1_SLOT_WAIT_SECONDS}s for {filename}. "
                f"Falling back to TEXT preview."
            )
            return preview(busy_fallback=True)
        try:
            end = await worker_process.run(
                _EXTRACTION_WORKER,
                {"extractor": f"{cls.__module__}:{cls.__qualname__}"},
                memory_bytes=memory_bytes,
                cpu_seconds=math.ceil(sum(dataclasses.astuple(phases))) + 1,
                output_limit_bytes=output_limit_bytes,
                phases=phases,
                parts=(content,),
            )
            if end.timed_out and end.phase == "work":
                logger.warning(
                    f"Tier 1 extraction timed out after {TIER1_TIMEOUT_SECONDS}s "
                    f"for {filename}. Falling back to TEXT preview."
                )
                return preview(timeout_fallback=True)
            if end.timed_out:
                raise RuntimeError(
                    f"the extraction worker ran past its {end.phase} bound"
                )
            if end.result is None:
                if end.code == worker_process.OUT_OF_MEMORY_EXIT:
                    raise RuntimeError("the extraction worker passed its memory limit")
                raise RuntimeError(
                    end.failure or f"the extraction worker exited {end.code}"
                )
            extract_result_json, file_extract = end.result
        except Exception as e:
            logger.error(
                f"Tier 1 extraction failed for {filename}: {e}. "
                f"Falling back to TEXT preview."
            )
            return preview(error_fallback=True, error=str(e)[:200])
        finally:
            slots.release(principal)
        return ExtractionResult(
            method=extractor.strategy_name,
            # content holds file_extract text for sanity checks;
            # extract_result_json carries the full structured payload.
            content=file_extract,
            metadata={"extract_result_json": extract_result_json},
        )
