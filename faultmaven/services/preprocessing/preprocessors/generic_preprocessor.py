"""Generic Preprocessor - Fallback for unsupported data types

Handles OTHER and unsupported data types with basic text processing.
"""

import logging
import uuid
import time
from typing import Optional

from faultmaven.models.api import DataType, PreprocessedData, SourceMetadata, ExtractionMetadata
from faultmaven.models.interfaces import IPreprocessor


class GenericPreprocessor(IPreprocessor):
    """
    Generic preprocessor for unsupported data types

    Provides basic text truncation and summary generation for
    data types without specialized preprocessors.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def process(
        self,
        content: str,
        filename: str,
        source_metadata: Optional[SourceMetadata] = None
    ) -> PreprocessedData:
        """
        Generic preprocessing: truncate and format plainly

        Args:
            content: Raw content
            filename: Original filename
            source_metadata: Optional source metadata

        Returns:
            PreprocessedData with truncated summary
        """
        start_time = time.time()

        # Truncate to 8000 chars
        summary = self._format_generic_summary(content, filename, source_metadata)
        llm_ready_content = summary[:8000]

        processing_time = (time.time() - start_time) * 1000

        return PreprocessedData(
            content=llm_ready_content,
            metadata=ExtractionMetadata(
                data_type=DataType.UNANALYZABLE,
                extraction_strategy="direct",  # Simple truncation
                llm_calls_used=0,
                confidence=0.6,  # Medium confidence for generic processing
                source="fallback",
                processing_time_ms=processing_time
            ),
            original_size=len(content),
            processed_size=len(llm_ready_content),
            security_flags=[],
            source_metadata=source_metadata
        )

    def _format_generic_summary(
        self,
        content: str,
        filename: str,
        source_metadata: Optional[SourceMetadata]
    ) -> str:
        """Format generic summary"""
        sections = []

        sections.append("DATA CONTENT SUMMARY")
        sections.append("=" * 50)
        sections.append("")

        sections.append("FILE INFORMATION:")
        sections.append(f"Filename: {filename}")
        sections.append(f"Size: {len(content):,} bytes")
        sections.append("")

        if source_metadata:
            sections.append("SOURCE:")
            sections.append(f"Type: {source_metadata.source_type}")
            if source_metadata.source_url:
                sections.append(f"URL: {source_metadata.source_url}")
            sections.append("")

        sections.append("CONTENT PREVIEW (first 7000 chars):")
        sections.append(content[:7000])

        if len(content) > 7000:
            sections.append(f"\n... ({len(content) - 7000:,} more bytes)")

        return "\n".join(sections)
