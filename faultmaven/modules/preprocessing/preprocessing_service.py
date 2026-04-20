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
import hashlib
import logging
import time
from typing import TYPE_CHECKING, Dict, Optional

from faultmaven.core.preprocessing.models import (
    ExtractionResult,
    PreprocessingResult,
    generate_concise_summary,
    to_unified_data_type,
)
from faultmaven.models.api import DataType, SourceMetadata
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.modules.preprocessing.extractors.protocol import Extractor

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Tier 1 timeout: extractors must complete within this budget.
# On timeout, falls back to TEXT extraction (preview-only).
TIER1_TIMEOUT_SECONDS = 2.0


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

    async def classify_and_extract(
        self,
        content: str,
        filename: str = "pasted_content.txt",
        source_metadata: Optional[SourceMetadata] = None,
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
        - **classification_failed**: heuristics could not reach the
          confidence threshold. Returns a placeholder carrying suggested
          types; the frontend shows a modal and retries with user_override.

        PII redaction is handled at the LLM boundary (MilestoneEngine),
        not here. Structural indexes are stored raw.

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
        )

        detailed_data_type = classification.data_type
        unified_data_type = to_unified_data_type(detailed_data_type)

        logger.info(
            f"classify_and_extract: {filename} → {detailed_data_type.value} "
            f"(unified={unified_data_type.value}, "
            f"source={classification.source}, "
            f"confidence={classification.confidence:.2f})"
        )

        # Path 1: UNANALYZABLE (user opted out — reference only)
        if detailed_data_type == DataType.UNANALYZABLE:
            return self._build_placeholder_result(
                content=content,
                classification=classification,
                detailed_data_type=detailed_data_type,
                unified_data_type=unified_data_type,
                placeholder_text=(
                    f"[File '{filename}' marked as UNANALYZABLE — "
                    f"reference only, no analysis performed]"
                ),
                extraction_method="none",
                start_time=start_time,
            )

        # Path 2: classification_failed (frontend will show modal, retry with user_override)
        if classification.classification_failed:
            logger.warning(
                f"Classification failed for {filename} "
                f"(confidence={classification.confidence:.2f}) — "
                f"requesting user input"
            )
            suggested = ", ".join(
                str(t.value) for t in (classification.suggested_types or [])
            )
            return self._build_placeholder_result(
                content=content,
                classification=classification,
                detailed_data_type=detailed_data_type,
                unified_data_type=unified_data_type,
                placeholder_text=(
                    f"[Classification uncertain for '{filename}' — "
                    f"requesting user input]\nSuggested types: {suggested}"
                ),
                extraction_method="classification_failed",
                start_time=start_time,
            )

        # Path 3: page capture — already structured, pass through with char cap
        if classification.source_type == "page_capture":
            logger.info(
                "classify_and_extract: page_capture detected, "
                "skipping extractor (content already structured)"
            )
            extraction = ExtractionResult(
                method="page_capture_passthrough",
                content=self._fallback_direct_extraction(content),
                metadata={"passthrough": True, "source_type": "page_capture"},
            )
        else:
            # Standard path: dispatch to type-specific extractor with timeout.
            extractor = self.extractors.get(detailed_data_type)
            if extractor:
                extraction = await self._extract_with_timeout(
                    extractor, content, filename
                )
            else:
                logger.warning(
                    f"No extractor for {detailed_data_type.value}, "
                    f"using direct truncation"
                )
                extraction = ExtractionResult(
                    method="direct",
                    content=self._fallback_direct_extraction(content),
                    metadata={"fallback": True},
                )

        return self._build_result(
            content=content,
            extraction=extraction,
            detailed_data_type=detailed_data_type,
            unified_data_type=unified_data_type,
            start_time=start_time,
        )

    def _build_result(
        self,
        content: str,
        extraction: ExtractionResult,
        detailed_data_type: DataType,
        unified_data_type,
        start_time: float,
    ) -> PreprocessingResult:
        """Assemble a PreprocessingResult from an ExtractionResult."""
        processing_time_ms = int((time.time() - start_time) * 1000)
        content_size = len(content.encode("utf-8"))
        index_size = len(extraction.content.encode("utf-8"))
        compression_ratio = index_size / max(content_size, 1)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        return PreprocessingResult(
            data_type=unified_data_type,
            detailed_data_type=detailed_data_type,
            summary=generate_concise_summary(extraction.content),
            structural_index=extraction.content,
            content_ref=None,
            content_size_bytes=content_size,
            content_type="text/plain",
            extraction_method=extraction.method,
            compression_ratio=compression_ratio,
            extraction_metadata=extraction.metadata,
            content_hash=content_hash,
            processing_time_ms=processing_time_ms,
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
            start_time=start_time,
        )

    def _fallback_direct_extraction(self, content: str, max_chars: int = 10000) -> str:
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
    ) -> ExtractionResult:
        """
        Run a Tier 1 extractor with timeout enforcement.

        If the extractor exceeds TIER1_TIMEOUT_SECONDS, cancel it and
        return a TEXT extraction (preview-only) as fallback.

        Design Reference:
            data-preprocessing-design-specification.md Section 4.9
        """
        try:
            result_content = await asyncio.wait_for(
                asyncio.to_thread(extractor.extract, content),
                timeout=TIER1_TIMEOUT_SECONDS,
            )
            return ExtractionResult(
                method=extractor.strategy_name,
                content=result_content,
                metadata={},
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Tier 1 extraction timed out after {TIER1_TIMEOUT_SECONDS}s "
                f"for {filename}. Falling back to TEXT preview."
            )
            # Fallback: truncated preview (always fast)
            return ExtractionResult(
                method="structure_extraction",
                content=self._fallback_direct_extraction(content),
                metadata={"timeout_fallback": True},
            )
        except Exception as e:
            logger.error(
                f"Tier 1 extraction failed for {filename}: {e}. "
                f"Falling back to TEXT preview."
            )
            return ExtractionResult(
                method="structure_extraction",
                content=self._fallback_direct_extraction(content),
                metadata={"error_fallback": True, "error": str(e)[:200]},
            )
