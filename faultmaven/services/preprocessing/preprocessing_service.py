"""
Preprocessing Service - 4-Step Pipeline Orchestrator

Coordinates the 4-step preprocessing pipeline:
1. Classification (rule-based, 0 LLM calls)
2. Type-specific extraction (0-1 LLM calls depending on type)
3. Chunking fallback (0 or N+1 LLM calls, only if needed)
4. Sanitization + packaging

Phase 1: Only LOGS_AND_ERRORS extractor implemented
Phase 2-4: Additional extractors and features
"""

import time
import logging
from typing import Optional
from faultmaven.models.api import (
    DataType,
    PreprocessedData,
    ExtractionMetadata,
    SourceMetadata
)
from faultmaven.services.preprocessing.classifier import DataClassifier
from faultmaven.services.preprocessing.extractors.logs_extractor import LogsAndErrorsExtractor
from faultmaven.infrastructure.security.redaction import DataSanitizer

logger = logging.getLogger(__name__)


class PreprocessingService:
    """4-step pipeline orchestrator for data preprocessing"""

    def __init__(
        self,
        classifier: DataClassifier,
        sanitizer: DataSanitizer,
        logs_extractor: LogsAndErrorsExtractor,
        config_extractor: Optional['StructuredConfigExtractor'] = None,
        metrics_extractor: Optional['MetricsAndPerformanceExtractor'] = None,
        text_extractor: Optional['UnstructuredTextExtractor'] = None,
        source_code_extractor: Optional['SourceCodeExtractor'] = None,
        visual_extractor: Optional['VisualEvidenceExtractor'] = None
    ):
        """
        Initialize preprocessing service

        Args:
            classifier: Data classification service
            sanitizer: PII/secret redaction service
            logs_extractor: LOGS_AND_ERRORS extractor (Phase 1)
            config_extractor: STRUCTURED_CONFIG extractor (Phase 2, optional)
            metrics_extractor: METRICS_AND_PERFORMANCE extractor (Phase 2, optional)
            text_extractor: UNSTRUCTURED_TEXT extractor (Phase 2, optional)
            source_code_extractor: SOURCE_CODE extractor (Phase 2, optional)
            visual_extractor: VISUAL_EVIDENCE extractor (Phase 2 placeholder, Phase 3 full implementation)
        """
        self.classifier = classifier
        self.sanitizer = sanitizer

        # Extractor registry (Phase 1-2: All extractors available)
        self.extractors = {
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

    def preprocess(
        self,
        filename: str,
        content: str,
        agent_hint: Optional[DataType] = None,
        browser_context: Optional[str] = None,
        user_override: Optional[DataType] = None,
        source_metadata: Optional[SourceMetadata] = None
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

        # Step 1: Classification
        classification = self.classifier.classify(
            filename,
            content,
            agent_hint,
            browser_context,
            user_override
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
                time.time() - start_time
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
                time.time() - start_time
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

        # Step 3: Chunking (skip in Phase 1)
        # Phase 4: Implement map-reduce for long UNSTRUCTURED_TEXT

        # Step 4: Sanitization
        logger.info("Applying PII/secret sanitization")
        sanitized = self.sanitizer.sanitize(extracted)

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
                processing_time_ms=processing_time
            ),
            original_size=len(content),
            processed_size=len(sanitized),
            security_flags=security_flags,
            source_metadata=source_metadata
        )

    def _create_unanalyzable_result(
        self,
        filename: str,
        content: str,
        classification,
        source_metadata: Optional[SourceMetadata],
        elapsed_time: float
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
                processing_time_ms=elapsed_time * 1000
            ),
            original_size=len(content),
            processed_size=0,
            security_flags=[],
            source_metadata=source_metadata
        )

    def _create_classification_failed_result(
        self,
        filename: str,
        content: str,
        classification,
        source_metadata: Optional[SourceMetadata],
        elapsed_time: float
    ) -> PreprocessedData:
        """
        Create placeholder result when classification fails

        Frontend will detect this and show user modal for manual selection
        """
        # Include suggested types in content for frontend
        suggested = ", ".join(str(t.value) for t in (classification.suggested_types or []))

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
                processing_time_ms=elapsed_time * 1000
            ),
            original_size=len(content),
            processed_size=0,
            security_flags=[],
            source_metadata=source_metadata
        )

    def _fallback_direct_extraction(self, content: str, max_chars: int = 10000) -> str:
        """
        Fallback extraction for types without extractors (Phase 1)

        Simply truncates content to max_chars
        """
        if len(content) <= max_chars:
            return content

        return content[:max_chars] + f"\n\n... [Truncated {len(content) - max_chars} chars]"
