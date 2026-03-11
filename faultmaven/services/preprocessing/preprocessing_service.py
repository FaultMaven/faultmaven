"""
Preprocessing Service - Tier 0+1 Pipeline Orchestrator

Coordinates the preprocessing pipeline:
- classify_and_extract(): Unified entry point for all attachments
  (classify → extract with timeout → sanitize → package PreprocessingResult)
- process_upload(): File upload entry point (file validation → classify → extract
  with timeout → sanitize → store raw file → package PreprocessingResult)

Called from _preprocess_attachment() in the unified turn pipeline (Step 1,
before LLM inference).

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md
"""

import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional

from faultmaven.core.preprocessing.models import (
    DuplicateFileError,
    ExtractionResult,
    FileInfo,
    FileTooLargeError,
    PreprocessingResult,
    SanitizationResult,
    generate_concise_summary,
    to_unified_data_type,
)
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.models.api import (
    DataType,
    ExtractionMetadata,
    PreprocessedData,
    SourceMetadata,
)
from faultmaven.services.preprocessing.classifier import DataClassifier
from faultmaven.services.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.services.preprocessing.extractors.protocol import Extractor

if TYPE_CHECKING:
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore

logger = logging.getLogger(__name__)

# Tier 1 timeout: extractors must complete within this budget.
# On timeout, falls back to TEXT extraction (preview-only).
TIER1_TIMEOUT_SECONDS = 2.0

# Default maximum upload size (10 MB)
DEFAULT_MAX_UPLOAD_SIZE = 10 * 1024 * 1024


class PreprocessingService:
    """4-step pipeline orchestrator for data preprocessing"""

    def __init__(
        self,
        classifier: DataClassifier,
        sanitizer: DataSanitizer,
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
        chunking_service: Optional["ChunkingService"] = None,  # noqa: F821
        chunk_trigger_tokens: int = 8000,
    ):
        """
        Initialize preprocessing service

        Args:
            classifier: Data classification service
            sanitizer: PII/secret redaction service
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
            chunking_service: ChunkingService for large documents (optional)
            chunk_trigger_tokens: Token threshold to trigger chunking (default 8000)
        """
        self.classifier = classifier
        self.sanitizer = sanitizer
        self.chunking_service = chunking_service
        self.chunk_trigger_tokens = chunk_trigger_tokens

        # Extractor registry - all 11 data types
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

    async def preprocess(
        self,
        filename: str,
        content: str,
        agent_hint: Optional[DataType] = None,
        browser_context: Optional[str] = None,
        user_override: Optional[DataType] = None,
        source_metadata: Optional[SourceMetadata] = None,
    ) -> PreprocessedData:
        """
        4-step preprocessing pipeline

        Args:
            filename: Original filename
            content: Raw file content
            agent_hint: Expected data type from agent (Phase 3)
            browser_context: Page type from browser (Phase 3)
            user_override: User-selected type from modal (Phase 3)
            source_metadata: Optional source information

        Returns:
            PreprocessedData with extracted content and metadata

        Raises:
            ValueError: If file is UNANALYZABLE and no extractors available
        """
        start_time = time.time()

        logger.info(
            f"Starting preprocessing pipeline for {filename} "
            f"(size={len(content)} bytes, hint={agent_hint})"
        )

        # Step 1: Classification (with source_metadata for URL-based classification)
        classification = self.classifier.classify(
            filename,
            content,
            agent_hint,
            browser_context,
            user_override,
            source_metadata,  # Pass for URL pattern matching and file upload boost
        )

        logger.info(
            f"Classification: {classification.data_type} "
            f"(confidence={classification.confidence:.2f}, source={classification.source})"
        )

        # Handle UNANALYZABLE
        if classification.data_type == DataType.UNANALYZABLE:
            return self._create_unanalyzable_result(
                filename,
                content,
                classification,
                source_metadata,
                time.time() - start_time,
            )

        # Handle classification_failed (trigger user modal)
        if classification.classification_failed:
            logger.warning(
                f"Classification failed for {filename} "
                f"(confidence={classification.confidence:.2f}) - requesting user input"
            )
            # Return placeholder with classification_failed flag
            # Frontend will show modal and retry with user_override
            return self._create_classification_failed_result(
                filename,
                content,
                classification,
                source_metadata,
                time.time() - start_time,
            )

        # Step 2: Type-specific extraction
        extractor = self.extractors.get(classification.data_type)

        if not extractor:
            # Fallback for Phase 1: types not yet implemented
            logger.warning(
                f"No extractor for {classification.data_type} - using direct truncation fallback"
            )
            extracted = self._fallback_direct_extraction(content)
            strategy = "direct"
            llm_calls = 0
        else:
            logger.info(f"Using {extractor.strategy_name} extraction strategy")
            extracted = extractor.extract(content)
            strategy = extractor.strategy_name
            llm_calls = extractor.llm_calls_used

        # Step 3: Chunking (Phase 4 - Map-Reduce for long documents)
        token_count = self._estimate_tokens(extracted)

        if token_count > self.chunk_trigger_tokens and self.chunking_service:
            logger.info(
                f"Document exceeds {self.chunk_trigger_tokens} tokens ({token_count} tokens) "
                f"- triggering map-reduce chunking"
            )

            try:
                # Use ChunkingService for intelligent map-reduce processing
                extracted = await self.chunking_service.process_long_text(
                    content=extracted,
                    data_type=classification.data_type,
                    filename=filename,
                )

                strategy = "map_reduce"
                # Estimate LLM calls: N chunks + 1 synthesis
                chunk_count = (token_count // 4000) + 1
                llm_calls += chunk_count + 1

                logger.info(
                    f"Map-reduce chunking complete: {token_count} tokens → "
                    f"{self._estimate_tokens(extracted)} tokens "
                    f"({chunk_count + 1} LLM calls)"
                )
            except Exception as e:
                logger.error(f"Chunking failed: {e}. Falling back to truncation.")
                # Fallback to truncation if chunking fails
                if len(extracted) > 10000:
                    extracted = (
                        extracted[:10000]
                        + "\n\n... [Chunking failed, content truncated]"
                    )

        elif token_count > self.chunk_trigger_tokens and not self.chunking_service:
            logger.warning(
                f"Document exceeds {self.chunk_trigger_tokens} tokens ({token_count} tokens) "
                f"but ChunkingService not available - will truncate instead"
            )

        # PII redaction is handled at the LLM boundary (MilestoneEngine),
        # not at extraction time. Structural indices are stored raw.
        sanitized = extracted

        # Check for security issues
        security_flags = []
        if sanitized != extracted:
            security_flags.append("pii_redacted")

        # Package result
        processing_time = (time.time() - start_time) * 1000

        logger.info(
            f"Preprocessing complete: {len(content)} bytes -> {len(sanitized)} chars "
            f"in {processing_time:.1f}ms (LLM calls: {llm_calls})"
        )

        return PreprocessedData(
            content=sanitized,
            metadata=ExtractionMetadata(
                data_type=classification.data_type,
                extraction_strategy=strategy,
                llm_calls_used=llm_calls,
                confidence=classification.confidence,
                source=classification.source,
                processing_time_ms=processing_time,
            ),
            original_size=len(content),
            processed_size=len(sanitized),
            security_flags=security_flags,
            source_metadata=source_metadata,
        )

    def _create_unanalyzable_result(
        self,
        filename: str,
        content: str,
        classification,
        source_metadata: Optional[SourceMetadata],
        elapsed_time: float,
    ) -> PreprocessedData:
        """
        Create result for UNANALYZABLE files (user opted out)

        Returns minimal PreprocessedData indicating file is reference-only
        """
        return PreprocessedData(
            content=f"[File '{filename}' marked as UNANALYZABLE - reference only, no analysis performed]",
            metadata=ExtractionMetadata(
                data_type=DataType.UNANALYZABLE,
                extraction_strategy="none",
                llm_calls_used=0,
                confidence=classification.confidence,
                source=classification.source,
                processing_time_ms=elapsed_time * 1000,
            ),
            original_size=len(content),
            processed_size=0,
            security_flags=[],
            source_metadata=source_metadata,
        )

    def _create_classification_failed_result(
        self,
        filename: str,
        content: str,
        classification,
        source_metadata: Optional[SourceMetadata],
        elapsed_time: float,
    ) -> PreprocessedData:
        """
        Create placeholder result when classification fails

        Frontend will detect this and show user modal for manual selection
        """
        # Include suggested types in content for frontend
        suggested = ", ".join(
            str(t.value) for t in (classification.suggested_types or [])
        )

        return PreprocessedData(
            content=(
                f"[Classification uncertain for '{filename}' - requesting user input]\n"
                f"Suggested types: {suggested}"
            ),
            metadata=ExtractionMetadata(
                data_type=classification.data_type,
                extraction_strategy="classification_failed",
                llm_calls_used=0,
                confidence=classification.confidence,
                source=classification.source,
                processing_time_ms=elapsed_time * 1000,
            ),
            original_size=len(content),
            processed_size=0,
            security_flags=[],
            source_metadata=source_metadata,
        )

    def _fallback_direct_extraction(self, content: str, max_chars: int = 10000) -> str:
        """
        Fallback extraction for types without extractors (Phase 1)

        Simply truncates content to max_chars
        """
        if len(content) <= max_chars:
            return content

        return (
            content[:max_chars]
            + f"\n\n... [Truncated {len(content) - max_chars} chars]"
        )

    def _estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for text

        Uses simple heuristic: 1 token ≈ 4 characters
        This is conservative for English text
        """
        return len(text) // 4

    # =========================================================================
    # Design v3.0: process_upload() — Tier 0+1 entry point
    # =========================================================================

    async def process_upload(
        self,
        raw_content: bytes,
        filename: str,
        content_type: str,
        case_id: str,
        source_metadata: Optional[SourceMetadata] = None,
        max_upload_size: int = DEFAULT_MAX_UPLOAD_SIZE,
        storage_service: Optional[object] = None,
        evidence_repo: Optional[object] = None,
    ) -> PreprocessingResult:
        """
        Main Tier 0+1 entry point per design specification v3.0.

        Guarantees:
        - Completes in <2s (timeout-enforced extraction with fallback)
        - Never fails due to extraction errors (fallback chain)
        - Raises only FileTooLargeError or DuplicateFileError

        Steps:
        1. Validate file size
        2. Compute content_hash (SHA-256 of raw bytes) for deduplication
        3. Check for duplicates (early exit)
        4. Tier 0: Classify data type
        5. Tier 1: Extract structural index (timeout-enforced)
        6. Sanitize PII/secrets
        7. Store raw file (if storage service available)
        8. Package PreprocessingResult

        Design Reference:
            data-preprocessing-design-specification.md Section 9.2
        """
        start_time = time.time()

        # Step 0: Validate file size
        if len(raw_content) > max_upload_size:
            raise FileTooLargeError(len(raw_content), max_upload_size)

        # Step 1: Compute content_hash early for deduplication
        content_hash = hashlib.sha256(raw_content).hexdigest()

        # Step 2: Check for duplicate (early exit before extraction work)
        if evidence_repo is not None:
            try:
                existing = await evidence_repo.find_by_content_hash(
                    case_id, content_hash
                )
                if existing:
                    raise DuplicateFileError(existing.evidence_id, content_hash)
            except AttributeError:
                # Repo doesn't support find_by_content_hash yet
                logger.debug("Evidence repo does not support find_by_content_hash")

        # Tier 0: Classify
        content_str = raw_content.decode("utf-8", errors="replace")
        classification = self.classifier.classify(
            filename,
            content_str,
            source_metadata=source_metadata,
        )

        detailed_data_type = classification.data_type
        unified_data_type = to_unified_data_type(detailed_data_type)

        logger.info(
            f"process_upload: {filename} → {detailed_data_type.value} "
            f"(unified={unified_data_type.value}, "
            f"confidence={classification.confidence:.2f})"
        )

        # Tier 1: Extract structural index with timeout enforcement
        extractor = self.extractors.get(detailed_data_type)

        if extractor:
            extraction = await self._extract_with_timeout(
                extractor, content_str, filename
            )
        else:
            # Fallback for types without a dedicated extractor
            logger.warning(
                f"No extractor for {detailed_data_type.value}, "
                f"using direct truncation"
            )
            extraction = ExtractionResult(
                method="direct",
                content=self._fallback_direct_extraction(content_str),
                metadata={"fallback": True},
            )

        # PII redaction is handled at the LLM boundary (MilestoneEngine),
        # not at extraction time. Structural indices are stored raw.
        sanitized_content = extraction.content
        sanitization_applied = False
        redactions_count = 0

        # Store raw file (if storage service is available)
        content_ref = None
        if storage_service is not None:
            try:
                content_ref = await storage_service.store_raw_file(
                    content=raw_content,
                    case_id=case_id,
                    filename=filename,
                    content_type=content_type,
                )
            except Exception as e:
                logger.warning(f"Failed to store raw file: {e}")

        # Package result
        processing_time_ms = int((time.time() - start_time) * 1000)
        raw_size = len(raw_content)
        index_size = len(sanitized_content.encode("utf-8"))
        compression_ratio = index_size / max(raw_size, 1)

        return PreprocessingResult(
            data_type=unified_data_type,
            detailed_data_type=detailed_data_type,
            summary=generate_concise_summary(sanitized_content),
            structural_index=sanitized_content,
            content_ref=content_ref,
            content_size_bytes=raw_size,
            content_type=content_type,
            extraction_method=extraction.method,
            compression_ratio=compression_ratio,
            extraction_metadata=extraction.metadata,
            content_hash=content_hash,
            sanitization_applied=sanitization_applied,
            redactions_count=redactions_count,
            processing_time_ms=processing_time_ms,
        )

    async def classify_and_extract(
        self,
        content: str,
        filename: str = "pasted_content.txt",
        source_metadata: Optional[SourceMetadata] = None,
    ) -> PreprocessingResult:
        """
        Classify content and run the matched extractor.

        Called from _preprocess_attachment() for all attachments in the unified
        turn pipeline. Same Tier 0+1 pipeline as process_upload() but without
        file validation, deduplication, raw file storage, or bytes handling.

        Returns:
            PreprocessingResult with structural_index, summary, and extraction metadata.
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
            f"confidence={classification.confidence:.2f})"
        )

        # Tier 1: Extract structural index with timeout enforcement
        extractor = self.extractors.get(detailed_data_type)

        if extractor:
            extraction = await self._extract_with_timeout(extractor, content, filename)
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

        # PII redaction is handled at the LLM boundary (MilestoneEngine),
        # not at extraction time. Structural indices are stored raw.
        sanitized_content = extraction.content
        sanitization_applied = False
        redactions_count = 0

        # Compute content hash from text (not raw bytes like process_upload)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Package result
        processing_time_ms = int((time.time() - start_time) * 1000)
        content_size = len(content.encode("utf-8"))
        index_size = len(sanitized_content.encode("utf-8"))
        compression_ratio = index_size / max(content_size, 1)

        return PreprocessingResult(
            data_type=unified_data_type,
            detailed_data_type=detailed_data_type,
            summary=generate_concise_summary(sanitized_content),
            structural_index=sanitized_content,
            content_ref=None,
            content_size_bytes=content_size,
            content_type="text/plain",
            extraction_method=extraction.method,
            compression_ratio=compression_ratio,
            extraction_metadata=extraction.metadata,
            content_hash=content_hash,
            sanitization_applied=sanitization_applied,
            redactions_count=redactions_count,
            processing_time_ms=processing_time_ms,
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
