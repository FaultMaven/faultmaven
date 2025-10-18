"""Log Preprocessor - Processes application and system logs

Purpose: Transform log files into LLM-ready summaries

This preprocessor reuses the existing LogProcessor for analysis and adds
formatting to generate plain text summaries optimized for LLM consumption.

Key Features:
    - Reuses existing LogProcessor for insight extraction
    - Extracts sample error messages
    - Formats into structured plain text (~8K chars)
    - Detects security issues (PII, secrets in logs)
"""

import logging
import uuid
import time
import re
from typing import Optional, Dict, Any, List

from faultmaven.models.api import DataType, PreprocessedData, SourceMetadata, ExtractionMetadata
from faultmaven.models.interfaces import IPreprocessor
from faultmaven.core.processing.log_analyzer import LogProcessor


class LogPreprocessor(IPreprocessor):
    """
    Preprocessor for application and system logs

    Reuses existing LogProcessor for analysis, adds LLM-optimized formatting.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.log_processor = LogProcessor()  # Reuse existing component

    async def process(
        self,
        content: str,
        filename: str,
        source_metadata: Optional[SourceMetadata] = None
    ) -> PreprocessedData:
        """
        Process log file into LLM-ready summary

        Steps:
            1. Use existing LogProcessor for insight extraction
            2. Extract sample error messages from raw content
            3. Format into structured plain text summary
            4. Return PreprocessedData with summary

        Args:
            content: Raw log file content
            filename: Original filename
            source_metadata: Optional source metadata

        Returns:
            PreprocessedData with LLM-ready summary
        """
        start_time = time.time()

        try:
            # Step 1: Extract insights using existing processor
            self.logger.debug(f"Processing log file: {filename}")
            insights = await self.log_processor.process(content)

            # Step 2: Extract sample error lines from raw content
            error_samples = self._extract_error_samples(content, insights)

            # Step 3: Detect security issues
            security_flags = self._detect_security_issues(content)

            # Step 4: Format into LLM-ready summary
            summary = self._format_log_summary(
                insights=insights,
                error_samples=error_samples,
                filename=filename,
                source_metadata=source_metadata,
                security_flags=security_flags
            )

            # Step 5: Build PreprocessedData with correct structure
            processing_time = (time.time() - start_time) * 1000
            llm_ready_content = summary[:8000]  # Truncate to 8K chars

            return PreprocessedData(
                content=llm_ready_content,
                metadata=ExtractionMetadata(
                    data_type=DataType.LOGS_AND_ERRORS,
                    extraction_strategy="crime_scene",  # Log processor uses pattern-based extraction
                    llm_calls_used=0,  # LogProcessor is rule-based, no LLM calls
                    confidence=0.95,  # High confidence for rule-based log parsing
                    source="rule_based",
                    processing_time_ms=processing_time
                ),
                original_size=len(content),
                processed_size=len(llm_ready_content),
                security_flags=security_flags,
                source_metadata=source_metadata,
                insights=insights  # Preserve structured insights for advanced features
            )

        except Exception as e:
            self.logger.error(f"Log preprocessing failed: {e}", exc_info=True)
            processing_time = (time.time() - start_time) * 1000

            # Return minimal summary on error
            error_content = f"LOG FILE ANALYSIS\n\nERROR: Failed to process log file\n{str(e)[:500]}"
            return PreprocessedData(
                content=error_content,
                metadata=ExtractionMetadata(
                    data_type=DataType.LOGS_AND_ERRORS,
                    extraction_strategy="none",
                    llm_calls_used=0,
                    confidence=0.0,  # Zero confidence on error
                    source="error",
                    processing_time_ms=processing_time
                ),
                original_size=len(content),
                processed_size=len(error_content),
                security_flags=["preprocessing_error"],
                source_metadata=source_metadata,
                insights={"error": str(e)}
            )

    def _extract_error_samples(
        self,
        content: str,
        insights: Dict[str, Any]
    ) -> List[str]:
        """
        Extract sample error messages from raw log content

        Args:
            content: Raw log content
            insights: Insights from LogProcessor

        Returns:
            List of sample error lines (up to 10)
        """
        samples = []
        lines = content.split('\n')

        # Get error patterns from insights
        top_errors = insights.get('top_errors', [])

        # Extract lines containing errors
        for line in lines:
            if any(level in line.upper() for level in ['ERROR', 'FATAL', 'CRITICAL']):
                samples.append(line.strip())

                if len(samples) >= 10:  # Limit to 10 samples
                    break

        return samples

    def _detect_security_issues(self, content: str) -> List[str]:
        """
        Detect potential security issues in log content

        Args:
            content: Raw log content

        Returns:
            List of security flag strings
        """
        flags = []

        # Check for common PII and secrets patterns
        patterns = {
            "api_key_detected": r"(?i)(api[_-]?key|apikey)[:\s=]+['\"]?[A-Za-z0-9]{16,}",
            "password_in_logs": r"(?i)(password|passwd|pwd)[:\s=]+['\"]?[^\s'\"]+",
            "ip_addresses_in_logs": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
            "email_in_logs": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
            "jwt_token_detected": r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
            "aws_key_detected": r"AKIA[0-9A-Z]{16}",
        }

        for flag_name, pattern in patterns.items():
            if re.search(pattern, content):
                flags.append(flag_name)

        return flags

    def _format_log_summary(
        self,
        insights: Dict[str, Any],
        error_samples: List[str],
        filename: str,
        source_metadata: Optional[SourceMetadata],
        security_flags: List[str]
    ) -> str:
        """
        Format insights into LLM-ready plain text summary

        Target: ~8,000 characters
        Structure: Human-readable sections with clear headings

        Args:
            insights: Insights from LogProcessor
            error_samples: Sample error messages
            filename: Original filename
            source_metadata: Optional source metadata
            security_flags: Detected security issues

        Returns:
            Formatted plain text summary
        """
        sections = []

        # Header
        sections.append("LOG FILE ANALYSIS SUMMARY")
        sections.append("=" * 50)
        sections.append("")

        # File information
        sections.append("FILE INFORMATION:")
        sections.append(f"Filename: {filename}")
        sections.append(f"Total entries: {insights.get('total_entries', 0):,}")
        sections.append("")

        # Overview section
        sections.append("OVERVIEW:")

        # Time range
        time_range = insights.get('time_range')
        if time_range:
            sections.append(f"Time range: {time_range.get('start', 'N/A')} to {time_range.get('end', 'N/A')}")
            duration_hours = time_range.get('duration_hours', 0)
            sections.append(f"Duration: {duration_hours:.1f} hours")
        else:
            sections.append("Time range: Not available")

        # Error summary
        error_summary = insights.get('error_summary', {})
        total_errors = error_summary.get('total_errors', 0)
        error_rate = error_summary.get('error_rate', 0)
        sections.append(f"Error count: {total_errors:,} ({error_rate:.2%} error rate)")

        # Log level distribution
        log_levels = insights.get('log_level_distribution', {})
        if log_levels:
            level_str = ", ".join(f"{level}={count}" for level, count in log_levels.items())
            sections.append(f"Log levels: {level_str}")

        sections.append("")

        # Top error patterns
        top_errors = insights.get('top_errors', [])
        if top_errors:
            sections.append("TOP ERROR PATTERNS:")
            for i, error_code in enumerate(top_errors[:10], 1):
                sections.append(f"{i}. {error_code}")
            sections.append("")

        # Anomalies
        anomalies = insights.get('anomalies', [])
        if anomalies:
            sections.append("ANOMALIES DETECTED:")
            for anomaly in anomalies[:5]:  # Top 5 anomalies
                anom_type = anomaly.get('type', 'unknown')
                severity = anomaly.get('severity', 'unknown')
                description = anomaly.get('description', 'No description')
                sections.append(f"• {anom_type}: {description} (severity: {severity})")
            sections.append("")

        # Sample error messages
        if error_samples:
            sections.append("SAMPLE ERROR MESSAGES:")
            for sample in error_samples[:8]:  # Limit to 8 samples for space
                # Truncate very long lines
                truncated = sample[:200] + "..." if len(sample) > 200 else sample
                sections.append(truncated)
            sections.append("")

        # Performance metrics
        perf_metrics = insights.get('performance_metrics')
        if perf_metrics:
            sections.append("PERFORMANCE METRICS:")
            avg_time = perf_metrics.get('avg_response_time_ms', 0)
            p95_time = perf_metrics.get('p95_response_time_ms', 0)
            max_time = perf_metrics.get('max_response_time_ms', 0)
            sections.append(f"Average response time: {avg_time:.2f}ms")
            sections.append(f"P95 response time: {p95_time:.2f}ms")
            sections.append(f"Max response time: {max_time:.2f}ms")
            sections.append("")

        # HTTP status distribution
        http_status_dist = insights.get('http_status_distribution')
        if http_status_dist:
            sections.append("HTTP STATUS DISTRIBUTION:")
            for status_code, count in sorted(http_status_dist.items(), key=lambda x: x[1], reverse=True)[:10]:
                sections.append(f"  {status_code}: {count} requests")
            sections.append("")

        # Contextual analysis (if available)
        contextual = insights.get('contextual_analysis')
        if contextual and contextual.get('contextual_entries', 0) > 0:
            sections.append("CONTEXTUAL ANALYSIS:")
            sections.append(f"Context-relevant entries: {contextual['contextual_entries']}")
            sections.append(f"Contextual percentage: {contextual.get('contextual_percentage', 0):.1f}%")
            if contextual.get('contextual_errors'):
                sections.append(f"Context-relevant errors: {contextual['contextual_errors']}")
            sections.append("")

        # Source information
        if source_metadata:
            sections.append("SOURCE:")
            sections.append(f"Type: {source_metadata.source_type}")
            if source_metadata.source_url:
                sections.append(f"URL: {source_metadata.source_url}")
            if source_metadata.user_description:
                sections.append(f"Description: {source_metadata.user_description}")
            sections.append("")

        # Security warnings (if any)
        if security_flags:
            sections.append("⚠️ SECURITY WARNINGS:")
            sections.append(f"Issues detected: {', '.join(security_flags)}")
            sections.append("")

        return "\n".join(sections)
