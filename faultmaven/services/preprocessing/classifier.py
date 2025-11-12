"""
Data Classification Service

Fast, rule-based classification (0 LLM calls) with 5-tier prioritization:
1. User override (confidence=1.0)
2. Agent hint (confidence=0.95)
3. Source URL patterns (confidence=0.88-0.94) - for page captures
4. Browser context (confidence=0.85-0.92)
5. Rule-based patterns with file upload boost (confidence=0.60-0.98)

Optimized for /data endpoint (file uploads + page captures).
Copy&paste text is handled by /queries endpoint with separate classification.
"""

import re
from pathlib import Path
from typing import Optional, List, TYPE_CHECKING
from faultmaven.models.api import DataType, ClassificationResult

if TYPE_CHECKING:
    from faultmaven.models.api import SourceMetadata


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
        source_metadata: Optional['SourceMetadata'] = None
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

        # Priority 1: User override (100% confidence)
        if user_override:
            return ClassificationResult(
                data_type=user_override,
                confidence=1.0,
                source="user_override",
                classification_failed=False
            )

        # Priority 2: Agent hint (95% confidence if validated)
        if agent_hint and self._validate_hint(filename, content, agent_hint):
            return ClassificationResult(
                data_type=agent_hint,
                confidence=0.95,
                source="agent_hint",
                classification_failed=False
            )

        # Priority 3: Source URL patterns (page captures - NEW)
        # Higher confidence than browser_context because URL is more specific
        if source_metadata and source_metadata.source_url:
            url_result = self._classify_from_source_url(
                source_metadata.source_url,
                source_metadata.source_type
            )
            if url_result:
                return url_result

        # Priority 4: Browser context (confidence adjusted for ranking)
        if browser_context:
            context_result = self._classify_from_browser_context(browser_context)
            if context_result:
                return context_result

        # Priority 5: Rule-based patterns with source_type boost
        return self._classify_with_rules(filename, content, source_metadata)

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
            return ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']

        if hint == DataType.STRUCTURED_CONFIG:
            # Should have config-like patterns
            ext = Path(filename).suffix.lower()
            if ext in ['.yaml', '.yml', '.json', '.toml', '.ini', '.env']:
                return True
            # Check content for config patterns
            return bool(re.search(r'[a-z_]+\s*[:=]', sample, re.MULTILINE))

        if hint == DataType.SOURCE_CODE:
            # Should have code-like patterns
            ext = Path(filename).suffix.lower()
            code_exts = ['.py', '.js', '.ts', '.java', '.go', '.rs', '.cpp', '.c', '.rb']
            if ext in code_exts:
                return True
            # Check for code patterns
            return bool(re.search(r'(function|def|class|import|package)\s+\w+', sample))

        # Other types: accept hint (can't validate easily)
        return True

    def _classify_from_source_url(
        self,
        source_url: str,
        source_type: str
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
            ('sentry.io', DataType.LOGS_AND_ERRORS, 0.94),
            ('bugsnag.com', DataType.LOGS_AND_ERRORS, 0.94),
            ('rollbar.com', DataType.LOGS_AND_ERRORS, 0.94),
            ('app.datadoghq.com/logs', DataType.LOGS_AND_ERRORS, 0.92),
            ('kibana', DataType.LOGS_AND_ERRORS, 0.92),
            ('splunk.com', DataType.LOGS_AND_ERRORS, 0.92),
            ('logz.io', DataType.LOGS_AND_ERRORS, 0.90),
            ('papertrailapp.com', DataType.LOGS_AND_ERRORS, 0.90),
            ('/logs/', DataType.LOGS_AND_ERRORS, 0.85),  # Generic logs path

            # APM & Metrics platforms
            ('grafana', DataType.METRICS_AND_PERFORMANCE, 0.92),
            ('app.datadoghq.com/apm', DataType.METRICS_AND_PERFORMANCE, 0.92),
            ('app.datadoghq.com/metric', DataType.METRICS_AND_PERFORMANCE, 0.92),
            ('app.datadoghq.com/dashboard', DataType.METRICS_AND_PERFORMANCE, 0.90),
            ('prometheus', DataType.METRICS_AND_PERFORMANCE, 0.92),
            ('newrelic.com', DataType.METRICS_AND_PERFORMANCE, 0.90),
            ('honeycomb.io', DataType.METRICS_AND_PERFORMANCE, 0.90),
            ('jaeger', DataType.METRICS_AND_PERFORMANCE, 0.88),
            ('zipkin', DataType.METRICS_AND_PERFORMANCE, 0.88),
            ('/metrics/', DataType.METRICS_AND_PERFORMANCE, 0.85),  # Generic metrics path
            ('/dashboard/', DataType.METRICS_AND_PERFORMANCE, 0.82),  # Generic dashboard

            # Cloud Platform Consoles
            ('console.aws.amazon.com/cloudwatch', DataType.METRICS_AND_PERFORMANCE, 0.90),
            ('console.cloud.google.com/logs', DataType.LOGS_AND_ERRORS, 0.90),
            ('portal.azure.com', DataType.METRICS_AND_PERFORMANCE, 0.88),

            # Source code platforms
            ('github.com', DataType.SOURCE_CODE, 0.90),
            ('gitlab.com', DataType.SOURCE_CODE, 0.90),
            ('bitbucket.org', DataType.SOURCE_CODE, 0.90),

            # Documentation platforms
            ('readthedocs.io', DataType.UNSTRUCTURED_TEXT, 0.88),
            ('docs.', DataType.UNSTRUCTURED_TEXT, 0.88),
            ('confluence', DataType.UNSTRUCTURED_TEXT, 0.88),
            ('notion.so', DataType.UNSTRUCTURED_TEXT, 0.88),
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
                    classification_failed=False
                )

        return None

    def _classify_from_browser_context(self, context: str) -> Optional[ClassificationResult]:
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
            'sentry': (DataType.LOGS_AND_ERRORS, 0.92),
            'kibana': (DataType.LOGS_AND_ERRORS, 0.90),
            'splunk': (DataType.LOGS_AND_ERRORS, 0.90),
            'grafana': (DataType.METRICS_AND_PERFORMANCE, 0.90),
            'prometheus': (DataType.METRICS_AND_PERFORMANCE, 0.92),
            'datadog': (DataType.METRICS_AND_PERFORMANCE, 0.88),
            'jaeger': (DataType.METRICS_AND_PERFORMANCE, 0.85),
            'zipkin': (DataType.METRICS_AND_PERFORMANCE, 0.85),
        }

        for page_type, (data_type, confidence) in context_mappings.items():
            if page_type in context:
                return ClassificationResult(
                    data_type=data_type,
                    confidence=confidence,
                    source="browser_context",
                    classification_failed=False
                )

        return None

    def _classify_with_rules(
        self,
        filename: str,
        content: str,
        source_metadata: Optional['SourceMetadata'] = None
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
            source_metadata and
            source_metadata.source_type == "file_upload"
        )
        confidence_boost = 0.03 if is_file_upload else 0.0

        # 1. Check for VISUAL_EVIDENCE (highest priority - most specific)
        if ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp']:
            return ClassificationResult(
                data_type=DataType.VISUAL_EVIDENCE,
                confidence=min(0.98 + confidence_boost, 0.99),
                source="rule_based",
                classification_failed=False
            )

        # 2. Check for LOGS_AND_ERRORS (systematic approach)

        # Text-based log patterns
        text_log_patterns = [
            r'\b(error|fatal|critical|panic|exception|traceback)\b',
            r'\[\d{4}-\d{2}-\d{2}',  # Timestamp [2025-10-15
            r'(INFO|WARN|ERROR|DEBUG|TRACE):',  # Log levels
            r'at\s+[\w\.]+\(\w+\.java:\d+\)',  # Java stack trace
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

        text_score = sum(1 for p in text_log_patterns if re.search(p, sample, re.IGNORECASE))
        structured_score = sum(1 for p in structured_log_patterns if re.search(p, sample, re.IGNORECASE))

        # Strong indicator: .log file with any log patterns
        if ext in ['.log', '.txt'] and (text_score >= 2 or structured_score >= 2):
            base_confidence = 0.85 + min(text_score + structured_score, 3) * 0.03  # 0.85-0.94
            return ClassificationResult(
                data_type=DataType.LOGS_AND_ERRORS,
                confidence=min(base_confidence + confidence_boost, 0.98),
                source="rule_based",
                classification_failed=False
            )

        # Strong log patterns regardless of extension
        if text_score >= 3 or structured_score >= 3:
            return ClassificationResult(
                data_type=DataType.LOGS_AND_ERRORS,
                confidence=min(0.88 + confidence_boost, 0.95),
                source="rule_based",
                classification_failed=False
            )

        # 3. Check for METRICS_AND_PERFORMANCE (before STRUCTURED_CONFIG to avoid JSON misclassification)
        metrics_exts = ['.csv', '.tsv']
        metrics_patterns = [
            r'timestamp[,\t]',  # CSV with timestamp column
            r'\btimestamp["\']?\s*[:,]',  # JSON with timestamp field
            r'\w+{[\w="]+}',  # Prometheus format: metric{label="value"}
            r'^\d+\.\d+\s+\d+',  # Unix timestamp + value
            r'(cpu|memory|latency|response_time|throughput|error_rate)',  # Common metric names
            r'\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}',  # ISO timestamp
        ]

        metrics_score = sum(1 for p in metrics_patterns if re.search(p, sample, re.MULTILINE))

        # CSV files with numeric data patterns strongly suggest metrics
        if ext in metrics_exts and metrics_score >= 1:
            return ClassificationResult(
                data_type=DataType.METRICS_AND_PERFORMANCE,
                confidence=0.85,
                source="rule_based",
                classification_failed=False
            )

        # JSON arrays with numeric time-series patterns
        # (must check before STRUCTURED_CONFIG to avoid misclassification)
        if metrics_score >= 3 and re.search(r'\[\s*{', sample):
            return ClassificationResult(
                data_type=DataType.METRICS_AND_PERFORMANCE,
                confidence=0.80,
                source="rule_based",
                classification_failed=False
            )

        # 4. Check for STRUCTURED_CONFIG
        config_exts = ['.yaml', '.yml', '.json', '.toml', '.ini', '.env', '.config']
        config_patterns = [
            r'^[\w_]+\s*[:=]',  # key: value or key=value
            r'^\[[\w\.]+\]',  # [section]
            r'{\s*"[\w_]+":\s*',  # JSON object
        ]

        config_score = sum(1 for p in config_patterns if re.search(p, sample, re.MULTILINE))

        if ext in config_exts:
            return ClassificationResult(
                data_type=DataType.STRUCTURED_CONFIG,
                confidence=min(0.92 + confidence_boost, 0.98),
                source="rule_based",
                classification_failed=False
            )

        if config_score >= 2:
            return ClassificationResult(
                data_type=DataType.STRUCTURED_CONFIG,
                confidence=min(0.75 + confidence_boost, 0.85),
                source="rule_based",
                classification_failed=False
            )

        # 5. Check for SOURCE_CODE
        code_exts = [
            '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', '.cpp', '.c',
            '.h', '.rb', '.php', '.swift', '.kt', '.scala', '.sh', '.bash'
        ]

        code_patterns = [
            r'\b(function|def|class|import|package|interface)\s+\w+',
            r'\b(const|let|var)\s+\w+\s*=',
            r'^\s*(public|private|protected)\s+(class|interface|enum)',
        ]

        code_score = sum(1 for p in code_patterns if re.search(p, sample, re.MULTILINE))

        if ext in code_exts:
            return ClassificationResult(
                data_type=DataType.SOURCE_CODE,
                confidence=min(0.95 + confidence_boost, 0.98),
                source="rule_based",
                classification_failed=False
            )

        if code_score >= 2:
            return ClassificationResult(
                data_type=DataType.SOURCE_CODE,
                confidence=min(0.80 + confidence_boost, 0.90),
                source="rule_based",
                classification_failed=False
            )

        # 6. Check for UNSTRUCTURED_TEXT (markdown, documentation)
        text_exts = ['.md', '.txt', '.rst', '.adoc']
        text_patterns = [
            r'^#{1,6}\s+\w+',  # Markdown headers
            r'^\*\*\w+\*\*',  # Bold text
            r'^\-\s+\w+',  # List items
        ]

        text_score = sum(1 for p in text_patterns if re.search(p, sample, re.MULTILINE))

        if ext in text_exts:
            return ClassificationResult(
                data_type=DataType.UNSTRUCTURED_TEXT,
                confidence=0.88,
                source="rule_based",
                classification_failed=False
            )

        if text_score >= 2:
            return ClassificationResult(
                data_type=DataType.UNSTRUCTURED_TEXT,
                confidence=0.72,
                source="rule_based",
                classification_failed=False
            )

        # 7. Fallback: Low confidence - trigger user modal
        # Default to UNSTRUCTURED_TEXT but request confirmation
        return ClassificationResult(
            data_type=DataType.UNSTRUCTURED_TEXT,
            confidence=0.50,  # Below threshold
            source="rule_based",
            classification_failed=True,  # Trigger user fallback modal
            suggested_types=[
                DataType.LOGS_AND_ERRORS,
                DataType.UNSTRUCTURED_TEXT,
                DataType.STRUCTURED_CONFIG
            ]
        )
