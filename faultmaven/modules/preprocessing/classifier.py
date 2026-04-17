"""
Data Classification Service

Fast, rule-based classification (0 LLM calls) with 5-tier prioritization:
1. User override (confidence=1.0)
2. Agent hint (confidence=0.95)
3. Source URL patterns (confidence=0.88-0.94) - for page captures
4. Browser context (confidence=0.85-0.92)
5. Rule-based patterns with file upload boost (confidence=0.60-0.98)

Called directly from _preprocess_attachment() in the unified turn pipeline
for all attachments (file uploads and pasted data).
"""

import re
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from faultmaven.models.api import ClassificationResult, DataType

if TYPE_CHECKING:
    from faultmaven.models.api import SourceMetadata
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore


class DataClassifier:
    """Fast, rule-based classification with confidence scoring"""

    # Confidence thresholds
    CONFIDENCE_HIGH = 0.90  # >90% = high confidence
    CONFIDENCE_MEDIUM = 0.60  # 60-90% = medium confidence
    CONFIDENCE_LOW_THRESHOLD = 0.60  # <60% = request user input

    def classify(
        self,
        filename: str,
        content: str,
        agent_hint: Optional[DataType] = None,
        browser_context: Optional[str] = None,
        user_override: Optional[DataType] = None,
        source_metadata: Optional["SourceMetadata"] = None,
    ) -> ClassificationResult:
        """
        5-tier classification with confidence scoring

        Priority order (optimized for /data endpoint):
        1. User override (highest - confidence=1.0)
        2. Agent hint (confidence=0.95 if validated)
        3. Source URL patterns (confidence=0.88-0.94) - NEW for page captures
        4. Browser context (confidence=0.85-0.92)
        5. Rule-based patterns with file upload boost (fallback)

        Args:
            filename: Original filename
            content: File content (first 5KB for sampling)
            agent_hint: Expected data type from agent
            browser_context: Page type from browser extension
            user_override: User-selected type (from fallback modal)
            source_metadata: Optional source info (URL, capture time, source type)

        Returns:
            ClassificationResult with data_type, confidence, and source
        """

        # Propagate content origin (page_capture, user_paste, etc.)
        # so downstream components can branch on it.
        _origin = (
            source_metadata.source_type
            if source_metadata and hasattr(source_metadata, "source_type")
            else None
        )

        # Priority 1: User override (100% confidence)
        if user_override:
            return ClassificationResult(
                data_type=user_override,
                confidence=1.0,
                source="user_override",
                classification_failed=False,
                source_type=_origin,
            )

        # Priority 2: Agent hint (95% confidence if validated)
        if agent_hint and self._validate_hint(filename, content, agent_hint):
            return ClassificationResult(
                data_type=agent_hint,
                confidence=0.95,
                source="agent_hint",
                classification_failed=False,
                source_type=_origin,
            )

        # Priority 3: Source URL patterns (page captures - NEW)
        # Higher confidence than browser_context because URL is more specific
        if source_metadata and source_metadata.source_url:
            url_result = self._classify_from_source_url(
                source_metadata.source_url, source_metadata.source_type
            )
            if url_result:
                url_result.source_type = _origin
                return url_result

        # Priority 4: Browser context (confidence adjusted for ranking)
        if browser_context:
            context_result = self._classify_from_browser_context(browser_context)
            if context_result:
                context_result.source_type = _origin
                return context_result

        # Priority 5: Rule-based patterns with source_type boost
        result = self._classify_with_rules(filename, content, source_metadata)
        result.source_type = _origin
        return result

    def _validate_hint(self, filename: str, content: str, hint: DataType) -> bool:
        """
        Validate agent hint against basic heuristics

        Returns False if hint contradicts obvious patterns
        (prevents agent from suggesting wrong type)
        """
        # Sample content for validation (first 5KB)
        sample = content[:5000]

        # Check for obvious contradictions
        if hint == DataType.VISUAL_EVIDENCE:
            # Must be image file
            ext = Path(filename).suffix.lower()
            return ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]

        if hint == DataType.STRUCTURED_CONFIG:
            # Should have config-like patterns
            ext = Path(filename).suffix.lower()
            if ext in [".yaml", ".yml", ".json", ".toml", ".ini", ".env"]:
                return True
            # Check content for config patterns
            return bool(re.search(r"[a-z_]+\s*[:=]", sample, re.MULTILINE))

        if hint == DataType.SOURCE_CODE:
            # Should have code-like patterns
            ext = Path(filename).suffix.lower()
            code_exts = [
                ".py",
                ".js",
                ".ts",
                ".java",
                ".go",
                ".rs",
                ".cpp",
                ".c",
                ".rb",
            ]
            if ext in code_exts:
                return True
            # Check for code patterns
            return bool(re.search(r"(function|def|class|import|package)\s+\w+", sample))

        # Other types: accept hint (can't validate easily)
        return True

    def _classify_from_source_url(
        self, source_url: str, source_type: str
    ) -> Optional[ClassificationResult]:
        """
        Classify based on source URL patterns (Priority 3)

        Higher confidence than browser_context because URL is more specific.
        Handles page captures from monitoring/observability tools.

        Args:
            source_url: URL where content was captured from
            source_type: Type of source ("page_capture", "file_upload", etc.)

        Returns:
            ClassificationResult if URL matches known patterns, None otherwise
        """
        url_lower = source_url.lower()

        # URL pattern mappings: (pattern, data_type, confidence)
        # Confidence: 0.88-0.94 (higher than browser_context due to specificity)
        url_patterns = [
            # Error Tracking & Logs platforms
            ("sentry.io", DataType.LOGS_AND_ERRORS, 0.94),
            ("bugsnag.com", DataType.LOGS_AND_ERRORS, 0.94),
            ("rollbar.com", DataType.LOGS_AND_ERRORS, 0.94),
            ("app.datadoghq.com/logs", DataType.LOGS_AND_ERRORS, 0.92),
            ("kibana", DataType.LOGS_AND_ERRORS, 0.92),
            ("splunk.com", DataType.LOGS_AND_ERRORS, 0.92),
            ("logz.io", DataType.LOGS_AND_ERRORS, 0.90),
            ("papertrailapp.com", DataType.LOGS_AND_ERRORS, 0.90),
            ("/logs/", DataType.LOGS_AND_ERRORS, 0.85),  # Generic logs path
            # APM & Metrics platforms
            ("grafana", DataType.METRICS_AND_PERFORMANCE, 0.92),
            ("app.datadoghq.com/apm", DataType.METRICS_AND_PERFORMANCE, 0.92),
            ("app.datadoghq.com/metric", DataType.METRICS_AND_PERFORMANCE, 0.92),
            ("app.datadoghq.com/dashboard", DataType.METRICS_AND_PERFORMANCE, 0.90),
            ("prometheus", DataType.METRICS_AND_PERFORMANCE, 0.92),
            ("newrelic.com", DataType.METRICS_AND_PERFORMANCE, 0.90),
            ("honeycomb.io", DataType.METRICS_AND_PERFORMANCE, 0.90),
            ("jaeger", DataType.METRICS_AND_PERFORMANCE, 0.88),
            ("zipkin", DataType.METRICS_AND_PERFORMANCE, 0.88),
            # LLM Observability platforms (traces contain embedded prompts — classify as TRACE_DATA
            # so the extractor treats them as diagnostic data, not raw logs or unstructured text)
            ("comet.com/opik", DataType.TRACE_DATA, 0.92),
            ("opik.comet.com", DataType.TRACE_DATA, 0.92),
            ("app.opik.com", DataType.TRACE_DATA, 0.92),
            ("langfuse.com", DataType.TRACE_DATA, 0.90),
            ("smith.langchain.com", DataType.TRACE_DATA, 0.90),
            ("langsmith", DataType.TRACE_DATA, 0.90),
            ("phoenix.arize.com", DataType.TRACE_DATA, 0.90),
            ("wandb.ai", DataType.METRICS_AND_PERFORMANCE, 0.88),
            ("app.helicone.ai", DataType.TRACE_DATA, 0.88),
            (
                "/metrics/",
                DataType.METRICS_AND_PERFORMANCE,
                0.85,
            ),  # Generic metrics path
            (
                "/dashboard/",
                DataType.METRICS_AND_PERFORMANCE,
                0.82,
            ),  # Generic dashboard
            # Cloud Platform Consoles
            (
                "console.aws.amazon.com/cloudwatch",
                DataType.METRICS_AND_PERFORMANCE,
                0.90,
            ),
            ("console.cloud.google.com/logs", DataType.LOGS_AND_ERRORS, 0.90),
            ("portal.azure.com", DataType.METRICS_AND_PERFORMANCE, 0.88),
            # Source code platforms
            ("github.com", DataType.SOURCE_CODE, 0.90),
            ("gitlab.com", DataType.SOURCE_CODE, 0.90),
            ("bitbucket.org", DataType.SOURCE_CODE, 0.90),
            # Documentation platforms
            ("readthedocs.io", DataType.UNSTRUCTURED_TEXT, 0.88),
            ("docs.", DataType.UNSTRUCTURED_TEXT, 0.88),
            ("confluence", DataType.UNSTRUCTURED_TEXT, 0.88),
            ("notion.so", DataType.UNSTRUCTURED_TEXT, 0.88),
        ]

        for pattern, data_type, confidence in url_patterns:
            if pattern in url_lower:
                # Boost confidence slightly for page_capture vs file_upload
                if source_type == "page_capture":
                    confidence = min(confidence + 0.02, 0.98)

                return ClassificationResult(
                    data_type=data_type,
                    confidence=confidence,
                    source="source_url",
                    classification_failed=False,
                )

        return None

    def _classify_from_browser_context(
        self, context: str
    ) -> Optional[ClassificationResult]:
        """
        Classify based on browser page type (Priority 4)

        Supports: grafana, kibana, sentry, datadog, splunk, prometheus

        Note: Confidence lowered from 0.90 to 0.85-0.92 because source_url
        classification (Priority 3) is now more reliable for page captures.
        """
        context = context.lower()

        # Mapping: page_type -> (data_type, confidence)
        # Confidence adjusted: now lower priority than source_url
        context_mappings = {
            "sentry": (DataType.LOGS_AND_ERRORS, 0.92),
            "kibana": (DataType.LOGS_AND_ERRORS, 0.90),
            "splunk": (DataType.LOGS_AND_ERRORS, 0.90),
            "grafana": (DataType.METRICS_AND_PERFORMANCE, 0.90),
            "prometheus": (DataType.METRICS_AND_PERFORMANCE, 0.92),
            "datadog": (DataType.METRICS_AND_PERFORMANCE, 0.88),
            "jaeger": (DataType.METRICS_AND_PERFORMANCE, 0.85),
            "zipkin": (DataType.METRICS_AND_PERFORMANCE, 0.85),
        }

        for page_type, (data_type, confidence) in context_mappings.items():
            if page_type in context:
                return ClassificationResult(
                    data_type=data_type,
                    confidence=confidence,
                    source="browser_context",
                    classification_failed=False,
                )

        return None

    def _classify_with_rules(
        self,
        filename: str,
        content: str,
        source_metadata: Optional["SourceMetadata"] = None,
    ) -> ClassificationResult:
        """
        Rule-based classification with confidence scoring (Priority 5)

        Uses extension + content pattern matching.
        Boosts confidence for file uploads (more reliable than page captures).

        Args:
            filename: Original filename
            content: File content (first 5KB for sampling)
            source_metadata: Optional source info for confidence boosting

        Returns:
            ClassificationResult
        """
        # Sample content (first 5KB for performance)
        sample = content[:5000].lower()
        ext = Path(filename).suffix.lower()

        # Confidence boost for file uploads (file extensions are trustworthy)
        is_file_upload = (
            source_metadata and source_metadata.source_type == "file_upload"
        )
        confidence_boost = 0.03 if is_file_upload else 0.0

        # 1. Check for VISUAL_EVIDENCE (highest priority - most specific)
        if ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]:
            return ClassificationResult(
                data_type=DataType.VISUAL_EVIDENCE,
                confidence=min(0.98 + confidence_boost, 0.99),
                source="rule_based",
                classification_failed=False,
            )

        # 2. Check for TRACE_DATA (distributed traces - very specific patterns)
        trace_patterns = [
            r'(traceId|trace_id)["\s:]+[a-f0-9]{32}',  # OpenTelemetry trace IDs (32-char hex)
            r'(spanId|span_id|parentId)["\s:]+[a-f0-9]{16,}',  # Span IDs
            r'"(serviceName|service\.name)"',  # Service mesh indicators
            r'"operationName".*"spans":\s*\[',  # Jaeger trace structure
        ]

        trace_score = sum(
            1 for p in trace_patterns if re.search(p, sample, re.IGNORECASE)
        )

        if trace_score >= 2:
            return ClassificationResult(
                data_type=DataType.TRACE_DATA,
                confidence=min(0.95 + confidence_boost, 0.99),
                source="rule_based",
                classification_failed=False,
            )

        # 3. Check for PROFILING_DATA (performance profiling - specific headers)
        profiling_patterns = [
            r"\bncalls\s+tottime\s+percall\s+cumtime",  # cProfile header
            r"[\w\.]+(?:;[\w\.]+)+\s+\d+",  # Flame graph format: foo;bar;baz 123
            r"\d+\s+calls?\s+in\s+[\d\.]+\s+seconds",  # Profiling summary
            r"filename:lineno\(function\)",  # cProfile format
        ]

        profiling_score = sum(
            1 for p in profiling_patterns if re.search(p, sample, re.IGNORECASE)
        )

        if profiling_score >= 1:
            return ClassificationResult(
                data_type=DataType.PROFILING_DATA,
                confidence=min(0.92 + confidence_boost, 0.98),
                source="rule_based",
                classification_failed=False,
            )

        # 4. Check for COMMAND_OUTPUT (shell command results - specific formats)
        command_patterns = [
            (r"(top\s+-|Tasks:|%Cpu\(s\)|KiB Mem)", "top"),
            (r"PID\s+USER\s+%CPU\s+%MEM\s+VSZ\s+RSS", "ps"),
            (r"avg-cpu:\s+%user\s+%nice\s+%system", "iostat"),
            (r"Device.*tps.*kB_read/s.*kB_wrtn/s", "iostat"),
            (r"Proto\s+Recv-Q\s+Send-Q\s+Local Address\s+Foreign Address", "netstat"),
            (r"total\s+used\s+free\s+shared\s+buff/cache\s+available", "free"),
            (r"Filesystem\s+1K-blocks\s+Used\s+Available\s+Use%", "df"),
        ]

        for pattern, cmd in command_patterns:
            if re.search(pattern, sample, re.IGNORECASE):
                return ClassificationResult(
                    data_type=DataType.COMMAND_OUTPUT,
                    confidence=min(0.95 + confidence_boost, 0.98),
                    source="rule_based",
                    classification_failed=False,
                )

        # 5. Check for ERROR_REPORT vs LOGS_AND_ERRORS (systematic approach)

        # Text-based log patterns
        text_log_patterns = [
            r"\b(error|fatal|critical|panic|exception|traceback)\b",
            r"\[\d{4}-\d{2}-\d{2}",  # Timestamp [2025-10-15
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\w+",  # Syslog RFC 3164: Jun 14 15:16:01 hostname
            r"\b(INFO|WARN|WARNING|ERROR|DEBUG|TRACE|FATAL|CRITICAL)\b[\s:\]|]",  # Log levels (colon, space, or delimiter)
            r"at\s+[\w\.]+\(\w+\.java:\d+\)",  # Java stack trace
            r'File\s+"[^"]+",\s+line\s+\d+',  # Python stack trace
        ]

        # Structured log patterns (JSON/structured logging)
        structured_log_patterns = [
            r'"timestamp":\s*"\d{4}-\d{2}-\d{2}',  # JSON timestamp field
            r'"level":\s*"(info|warn|error|debug)',  # JSON level field
            r'"event":\s*"[^"]+"',  # JSON event field
            r'"logger":\s*"',  # JSON logger field
            r'"message":\s*"',  # JSON message field
        ]

        text_score = sum(
            1 for p in text_log_patterns if re.search(p, sample, re.IGNORECASE)
        )
        structured_score = sum(
            1 for p in structured_log_patterns if re.search(p, sample, re.IGNORECASE)
        )

        # Check for timestamps to differentiate ERROR_REPORT from LOGS_AND_ERRORS
        timestamp_patterns = [
            r"\[\d{4}-\d{2}-\d{2}",  # [2025-10-15
            r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}",  # ISO timestamp
            r'"timestamp":\s*"\d{4}-\d{2}-\d{2}',  # JSON timestamp
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}",  # Syslog RFC 3164
        ]
        has_timestamps = any(
            re.search(p, sample, re.IGNORECASE) for p in timestamp_patterns
        )

        # Check for stack trace structure
        stack_trace_patterns = [
            r"at\s+[\w\.]+\(\w+\.java:\d+\)",  # Java stack trace
            r'File\s+"[^"]+",\s+line\s+\d+',  # Python stack trace
            r"Traceback \(most recent call last\)",  # Python traceback header
        ]
        has_stack_trace = any(
            re.search(p, sample, re.IGNORECASE) for p in stack_trace_patterns
        )

        # ERROR_REPORT: Has stack trace but NO timestamps (standalone exception)
        if has_stack_trace and not has_timestamps and text_score >= 1:
            return ClassificationResult(
                data_type=DataType.ERROR_REPORT,
                confidence=min(0.92 + confidence_boost, 0.98),
                source="rule_based",
                classification_failed=False,
            )

        # LOGS_AND_ERRORS: Has timestamps AND log patterns (log file)
        # Strong indicator: .log extension is itself strong evidence — lower threshold
        if ext == ".log" and (text_score >= 1 or structured_score >= 1):
            base_confidence = (
                0.88 + min(text_score + structured_score, 3) * 0.03
            )  # 0.88-0.97
            return ClassificationResult(
                data_type=DataType.LOGS_AND_ERRORS,
                confidence=min(base_confidence + confidence_boost, 0.98),
                source="rule_based",
                classification_failed=False,
            )

        # .txt files need stronger evidence (extension is ambiguous)
        if ext == ".txt" and (text_score >= 2 or structured_score >= 2):
            base_confidence = (
                0.85 + min(text_score + structured_score, 3) * 0.03
            )  # 0.85-0.94
            return ClassificationResult(
                data_type=DataType.LOGS_AND_ERRORS,
                confidence=min(base_confidence + confidence_boost, 0.98),
                source="rule_based",
                classification_failed=False,
            )

        # Strong log patterns regardless of extension
        if text_score >= 3 or structured_score >= 3:
            return ClassificationResult(
                data_type=DataType.LOGS_AND_ERRORS,
                confidence=min(0.88 + confidence_boost, 0.95),
                source="rule_based",
                classification_failed=False,
            )

        # 3. Check for METRICS_AND_PERFORMANCE (before STRUCTURED_CONFIG to avoid JSON misclassification)
        metrics_exts = [".csv", ".tsv"]
        metrics_patterns = [
            r"timestamp[,\t]",  # CSV with timestamp column
            r'\btimestamp["\']?\s*[:,]',  # JSON with timestamp field
            r'\w+{[\w="]+}',  # Prometheus format: metric{label="value"}
            r"^\d+\.\d+\s+\d+",  # Unix timestamp + value
            r"\b(cpu|memory|latency|response_time|throughput|error_rate)\b",  # Common metric names
            r"\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}",  # ISO timestamp
            r"\b(date|time|datetime)[,\t]",  # Tabular data with date/time columns
            r"\b(count|total|avg|sum|min|max|mean|median|p\d{2})\b[,\t]",  # Aggregate/stat columns
        ]

        metrics_score = sum(
            1 for p in metrics_patterns if re.search(p, sample, re.MULTILINE)
        )

        # CSV files with numeric data patterns strongly suggest metrics
        if ext in metrics_exts and metrics_score >= 2:
            return ClassificationResult(
                data_type=DataType.METRICS_AND_PERFORMANCE,
                confidence=0.85,
                source="rule_based",
                classification_failed=False,
            )

        # JSON arrays with numeric time-series patterns
        # (must check before STRUCTURED_CONFIG to avoid misclassification)
        if metrics_score >= 3 and re.search(r"\[\s*{", sample):
            return ClassificationResult(
                data_type=DataType.METRICS_AND_PERFORMANCE,
                confidence=0.80,
                source="rule_based",
                classification_failed=False,
            )

        # 4. Check for STRUCTURED_CONFIG
        config_exts = [".yaml", ".yml", ".json", ".toml", ".ini", ".env", ".config"]
        config_patterns = [
            r"^[\w_]+\s*[:=]",  # key: value or key=value
            r"^\[[\w\.]+\]",  # [section]
            r'{\s*"[\w_]+":\s*',  # JSON object
        ]

        config_score = sum(
            1 for p in config_patterns if re.search(p, sample, re.MULTILINE)
        )

        if ext in config_exts:
            return ClassificationResult(
                data_type=DataType.STRUCTURED_CONFIG,
                confidence=min(0.92 + confidence_boost, 0.98),
                source="rule_based",
                classification_failed=False,
            )

        if config_score >= 2:
            return ClassificationResult(
                data_type=DataType.STRUCTURED_CONFIG,
                confidence=min(0.75 + confidence_boost, 0.85),
                source="rule_based",
                classification_failed=False,
            )

        # 5. Check for SOURCE_CODE
        code_exts = [
            ".py",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".java",
            ".go",
            ".rs",
            ".cpp",
            ".c",
            ".h",
            ".rb",
            ".php",
            ".swift",
            ".kt",
            ".scala",
            ".sh",
            ".bash",
        ]

        code_patterns = [
            r"\b(function|def|class|import|package|interface)\s+\w+",
            r"\b(const|let|var)\s+\w+\s*=",
            r"^\s*(public|private|protected)\s+(class|interface|enum)",
        ]

        code_score = sum(1 for p in code_patterns if re.search(p, sample, re.MULTILINE))

        if ext in code_exts:
            return ClassificationResult(
                data_type=DataType.SOURCE_CODE,
                confidence=min(0.95 + confidence_boost, 0.98),
                source="rule_based",
                classification_failed=False,
            )

        if code_score >= 2:
            return ClassificationResult(
                data_type=DataType.SOURCE_CODE,
                confidence=min(0.80 + confidence_boost, 0.90),
                source="rule_based",
                classification_failed=False,
            )

        # 6. Check for WEB_PAGE (HTML content) - before DOCUMENTATION to avoid misclassification
        # Triggers on .html/.htm extensions OR strong HTML structural patterns in content
        html_exts = [".html", ".htm"]
        html_patterns = [
            r"<!doctype\s+html",  # DOCTYPE declaration
            r"<html[\s>]",  # Root element
            r"<head[\s>]",  # Head section
            r"<body[\s>]",  # Body section
            r"<div[\s>]",  # Common container
            r"<script[\s>]",  # Script tag (strong indicator)
            r"<meta\s+",  # Meta tags
            r'<link\s+rel="',  # Stylesheet links
        ]
        html_score = sum(
            1 for p in html_patterns if re.search(p, sample, re.IGNORECASE)
        )

        if ext in html_exts or html_score >= 3:
            return ClassificationResult(
                data_type=DataType.DOCUMENTATION,
                confidence=min(0.88 + confidence_boost, 0.95),
                source="rule_based",
                classification_failed=False,
            )

        # 7. Check for DOCUMENTATION vs UNSTRUCTURED_TEXT
        doc_exts = [".md", ".rst", ".adoc"]
        text_exts = [".txt"]

        markdown_patterns = [
            r"^#{1,6}\s+\w+",  # Markdown headers
            r"^\*\*\w+\*\*",  # Bold text
            r"^\-\s+\w+",  # List items
            r"```[\w]*\n",  # Code blocks
        ]

        # Prose patterns (indicate documentation)
        prose_patterns = [
            r"\b(the|and|for|with|that|this|from|which)\b",  # Common prose words
            r"[A-Z][a-z]+\s+[a-z]+\s+[a-z]+",  # Sentence structure
            r"\.\s+[A-Z]",  # Sentence boundaries
        ]

        markdown_score = sum(
            1 for p in markdown_patterns if re.search(p, sample, re.MULTILINE)
        )
        prose_count = sum(
            len(re.findall(p, sample, re.IGNORECASE)) for p in prose_patterns
        )
        prose_density = prose_count / max(len(sample), 1)

        # DOCUMENTATION: Markdown files or high prose density with structure
        if ext in doc_exts or (markdown_score >= 2 and prose_density > 0.1):
            return ClassificationResult(
                data_type=DataType.DOCUMENTATION,
                confidence=min(0.88 + confidence_boost, 0.95),
                source="rule_based",
                classification_failed=False,
            )

        # UNSTRUCTURED_TEXT: Generic text without clear structure
        if ext in text_exts or markdown_score >= 1:
            return ClassificationResult(
                data_type=DataType.UNSTRUCTURED_TEXT,
                confidence=0.72,
                source="rule_based",
                classification_failed=False,
            )

        # 7. Best-effort fallback: use highest-scoring candidate type (v4.0)
        # Instead of always falling back to UNSTRUCTURED_TEXT, give the
        # best-matching extractor a chance — extractors handle mismatches gracefully.
        scores = {
            DataType.LOGS_AND_ERRORS: text_score + structured_score,
            DataType.METRICS_AND_PERFORMANCE: metrics_score,
            DataType.STRUCTURED_CONFIG: config_score,
            DataType.SOURCE_CODE: code_score,
        }

        # CSV/TSV files that reached best-effort already failed the primary
        # metrics check (metrics_score < 2 at line 478). Vocabulary-based scoring
        # is unreliable for CSV — cell content produces cross-type false positives
        # ("error" → LOGS, "interface" → CODE, "cpu" → METRICS). Instead of
        # letting the generic best-effort pick the noisiest score, use structural
        # analysis per the design spec (has_numeric_columns, has_tabular_structure,
        # has_high_numeric_density):
        #
        # METRICS requires all three:
        #   1. Structural breadth: >= 5 columns (data export, not a lookup table)
        #   2. Data density: >= 10 rows in sample (real dataset, not a stub)
        #   3. Numeric/metric evidence: numeric cells >= 10% OR metrics_score >= 1
        #      (confirms quantitative content, not purely text)
        #
        # Everything else → TEXT (safe fallback, classification_failed=True)
        if ext in metrics_exts:
            first_line = content.split("\n", 1)[0].strip()
            delimiter = "\t" if ext == ".tsv" else ","
            col_count = len(first_line.split(delimiter))

            sample_lines = content[:5000].split("\n")
            data_lines = [l for l in sample_lines[1:] if l.strip()]

            numeric_cells = 0
            total_cells = 0
            for line in data_lines[:20]:
                for cell in line.split(delimiter):
                    cell = cell.strip().strip('"')
                    if cell:
                        total_cells += 1
                        if re.match(r"^-?[\d\.]+$", cell):
                            numeric_cells += 1
            numeric_ratio = numeric_cells / max(total_cells, 1)

            is_structured_data = (
                col_count >= 5
                and len(data_lines) >= 10
                and (numeric_ratio >= 0.1 or metrics_score >= 1)
            )

            if is_structured_data:
                return ClassificationResult(
                    data_type=DataType.METRICS_AND_PERFORMANCE,
                    confidence=0.55,
                    source="rule_based",
                    classification_failed=True,
                )

            return ClassificationResult(
                data_type=DataType.UNSTRUCTURED_TEXT,
                confidence=0.45,
                source="rule_based",
                classification_failed=True,
            )

        best_type, best_score = max(scores.items(), key=lambda x: x[1])

        if best_score >= 1:
            return ClassificationResult(
                data_type=best_type,
                confidence=0.50,
                source="rule_based_best_effort",
                classification_failed=True,
            )

        # 8. True fallback: nothing matched at all
        return ClassificationResult(
            data_type=DataType.UNSTRUCTURED_TEXT,
            confidence=0.30,
            source="rule_based",
            classification_failed=True,
        )
