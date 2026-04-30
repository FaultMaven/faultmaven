"""
Data Classification Service (Tier 0).

Fast, rule-based classification (zero LLM calls) with 5-priority signal
ordering. Priorities are ordered by signal reliability, not by confidence
magnitude:

1. User override              — confidence = 1.0 (source="user_override")
2. Validated agent hint       — confidence = 0.95 (source="agent_hint")
3. Source URL pattern         — confidence = 0.88-0.94 (source="source_url")
4. Browser context            — confidence = 0.85-0.92 (source="browser_context")
5. Rule-based content+ext     — confidence = 0.45-0.98 (source="rule_based"
                                 or "rule_based_best_effort")

Called from `_preprocess_attachment()` in the unified turn pipeline for
every attachment (file uploads and pasted text). Page captures are detected
here via source_metadata and handled with URL-based classification first,
then source_type-aware confidence boosting.

Design Reference:
    docs/architecture/data-processing/data-classification-strategy.md
"""

import functools
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from faultmaven.models.api import ClassificationResult, DataType

if TYPE_CHECKING:
    from faultmaven.models.api import SourceMetadata


# =============================================================================
# Opik telemetry (optional — no-op when Opik is not installed).
# Emits a tracing span per classify() call with tags describing the decision
# so we can measure classifier behavior (source distribution, confidence
# buckets, classification_failed rate) without touching logs.
# =============================================================================

try:
    import opik
    from opik import opik_context

    _OPIK_AVAILABLE = True
except ImportError:
    _OPIK_AVAILABLE = False


def _opik_track_classifier(func: Callable) -> Callable:
    """Wrap classify() with an Opik span and attach classifier-decision tags."""
    if _OPIK_AVAILABLE:
        tracked = opik.track(
            name="tier0_classify", capture_input=False, capture_output=False
        )(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result: ClassificationResult = tracked(*args, **kwargs)
            try:
                if result.confidence >= CONFIDENCE_THRESHOLDS["auto_accept"]:
                    bucket = "high"
                elif (
                    result.confidence >= CONFIDENCE_THRESHOLDS["classification_failed"]
                ):
                    bucket = "medium"
                else:
                    bucket = "low"
                opik_context.update_current_span(
                    tags=[
                        f"source:{result.source}",
                        f"data_type:{result.data_type.value}",
                        f"confidence:{bucket}",
                        f"classification_failed:{result.classification_failed}",
                    ]
                )
            except Exception:
                # Never let telemetry break classification.
                pass
            return result

        return wrapper

    # No-op when Opik isn't installed.
    @functools.wraps(func)
    def passthrough(*args, **kwargs):
        return func(*args, **kwargs)

    return passthrough


# =============================================================================
# Confidence thresholds (load-bearing — drive the classification_failed flag
# and downstream acceptance decisions). Adjusting these shifts the frontier
# between auto-accept and user-modal.
# =============================================================================

CONFIDENCE_THRESHOLDS = {
    # Below this → classification_failed=True → frontend modal for user override.
    "classification_failed": 0.50,
    # At or above this → auto-accept without qualification in downstream UX.
    "auto_accept": 0.85,
}


# File uploads carry a trustworthy extension signal that page captures and
# pasted text do not. Applied as an additive bump on rule-based content scores.
FILE_UPLOAD_CONFIDENCE_BOOST = 0.03


# Page captures get a small specificity bump on top of URL-pattern confidence
# since the URL itself is the strongest available signal.
PAGE_CAPTURE_CONFIDENCE_BOOST = 0.02


def _top_suggested_types(scores: dict, n: int = 3) -> List[DataType]:
    """Top N data types by score, excluding UNANALYZABLE.

    Used on classification_failed paths to surface candidate types for the
    cooperative-clarification UX. Empty scores or all-zero scores return [].
    """
    filtered = [
        (dt, s) for dt, s in scores.items() if dt != DataType.UNANALYZABLE and s > 0
    ]
    if not filtered:
        return []
    filtered.sort(key=lambda kv: -kv[1])
    return [dt for dt, _ in filtered[:n]]


# =============================================================================
# Linux/Unix command-output signatures (Tier 0 command detection).
#
# Each command requires 2+ pattern matches to classify, to limit false positives
# from text that happens to contain a single header-like pattern. Patterns are
# `re.search`-able against a content sample (first 5KB, MULTILINE | IGNORECASE).
#
# Commands route to the detailed DataType whose extractor produces the best
# structural index:
#   - Resource/perf monitoring (top/ps/vmstat/iostat/netstat/free/df/lsof)
#     → COMMAND_OUTPUT (CommandOutputExtractor: parses tabular process/IO data)
#   - Log-oriented (dmesg/journalctl/strace/ltrace)
#     → LOGS_AND_ERRORS (LogsAndErrorsExtractor: timestamp-aware, severity-aware)
#   - Profiling (perf)
#     → PROFILING_DATA (ProfilingDataExtractor: hotspot analysis)
#   - Machine inventory (lscpu)
#     → STRUCTURED_CONFIG (StructuredConfigExtractor: key:value parsing)
# =============================================================================

COMMAND_OUTPUTS: dict = {
    # --- Resource & system info → COMMAND_OUTPUT ---
    "top": {
        "type": DataType.COMMAND_OUTPUT,
        "patterns": [
            r"top\s+-\s+\d{2}:\d{2}:\d{2}\s+up",
            r"Tasks:\s+\d+\s+total,\s+\d+\s+running",
            r"%Cpu\(s\):",
            r"KiB Mem\s*:",
            r"PID\s+USER\s+PR\s+NI\s+VIRT\s+RES",
        ],
        "confidence": 0.95,
    },
    "ps": {
        "type": DataType.COMMAND_OUTPUT,
        "patterns": [
            r"USER\s+PID\s+%CPU\s+%MEM\s+VSZ\s+RSS",
            r"^\s*[\w\-]+\s+\d+\s+[\d\.]+\s+[\d\.]+",
        ],
        "confidence": 0.90,
    },
    "vmstat": {
        "type": DataType.COMMAND_OUTPUT,
        "patterns": [
            r"procs\s+-+memory-+\s+-+swap-+\s+-+io-+",
            r"r\s+b\s+swpd\s+free\s+buff\s+cache",
        ],
        "confidence": 0.95,
    },
    "iostat": {
        "type": DataType.COMMAND_OUTPUT,
        "patterns": [
            r"avg-cpu:\s+%user\s+%nice\s+%system",
            r"Device.*tps.*kB_read/s.*kB_wrtn/s",
        ],
        "confidence": 0.95,
    },
    "netstat": {
        "type": DataType.COMMAND_OUTPUT,
        "patterns": [
            r"Proto\s+Recv-Q\s+Send-Q\s+Local Address\s+Foreign Address",
            r"tcp\s+\d+\s+\d+\s+[\d\.:]+\s+[\d\.:]+",
        ],
        "confidence": 0.90,
    },
    "free": {
        "type": DataType.COMMAND_OUTPUT,
        "patterns": [
            r"total\s+used\s+free\s+shared\s+buff/cache\s+available",
            r"Mem:\s+\d+",
            r"Swap:\s+\d+",
        ],
        "confidence": 0.95,
    },
    "df": {
        "type": DataType.COMMAND_OUTPUT,
        "patterns": [
            r"Filesystem\s+1K-blocks\s+Used\s+Available\s+Use%",
            r"/dev/\w+\s+\d+\s+\d+\s+\d+\s+\d+%",
        ],
        "confidence": 0.95,
    },
    "lsof": {
        "type": DataType.COMMAND_OUTPUT,
        "patterns": [
            r"COMMAND\s+PID\s+USER\s+FD\s+TYPE\s+DEVICE",
            r"^\w+\s+\d+\s+\w+\s+\d+[rwu]\s",
        ],
        "confidence": 0.90,
    },
    # --- Log-oriented command output → LOGS_AND_ERRORS ---
    "dmesg": {
        "type": DataType.LOGS_AND_ERRORS,
        "patterns": [
            r"^\[\s*[\d\.]+\]",
            r"\bkernel:\s",
            r"\bLinux version\s",
        ],
        "confidence": 0.95,
    },
    "journalctl": {
        "type": DataType.LOGS_AND_ERRORS,
        "patterns": [
            r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}",
            r"systemd\[\d+\]:",
            r"\[\d+\]:",
        ],
        "confidence": 0.95,
    },
    "strace": {
        "type": DataType.LOGS_AND_ERRORS,
        "patterns": [
            r"\w+\([^)]*\)\s+=\s+-?\d+",
            r"<\d+\.\d+>",
            r"SIGTERM|SIGKILL|SIGSEGV",
        ],
        "confidence": 0.95,
    },
    "ltrace": {
        "type": DataType.LOGS_AND_ERRORS,
        "patterns": [
            r"\w+\([^)]*\)\s+=\s+\w+",
            r"<\d+\.\d+>",
        ],
        "confidence": 0.90,
    },
    # --- Profiling → PROFILING_DATA ---
    "perf": {
        "type": DataType.PROFILING_DATA,
        "patterns": [
            r"Performance counter stats",
            r"seconds time elapsed",
            r"cycles|instructions",
        ],
        "confidence": 0.95,
    },
    # --- Machine inventory → STRUCTURED_CONFIG ---
    "lscpu": {
        "type": DataType.STRUCTURED_CONFIG,
        "patterns": [
            r"Architecture:\s+\w+",
            r"CPU\(s\):\s+\d+",
            r"Model name:\s+",
        ],
        "confidence": 0.95,
    },
}


class DataClassifier:
    """Fast, rule-based classification with confidence scoring."""

    @_opik_track_classifier
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

        # Empty-content short-circuit: 0-byte uploads (or whitespace-only
        # content) carry no analyzable signal. Routing them through the
        # rule-based fallback would land on a low-confidence
        # UNSTRUCTURED_TEXT classification with classification_failed=True,
        # which surfaces a "we couldn't classify your file" modal — wrong
        # message for a confirmed-empty file. Instead, return UNANALYZABLE
        # so the preprocessing service emits a clear "file is empty" placeholder
        # and the rest of the investigation pipeline proceeds gracefully.
        if not content or not content.strip():
            return ClassificationResult(
                data_type=DataType.UNANALYZABLE,
                confidence=1.0,
                source="rule_based",
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
            # Must be image file — must match rule-path set at line 578
            ext = Path(filename).suffix.lower()
            return ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]

        if hint == DataType.STRUCTURED_CONFIG:
            # Should have config-like patterns — ext set must match rule-path set at line 726
            ext = Path(filename).suffix.lower()
            if ext in [".yaml", ".yml", ".json", ".toml", ".ini", ".env", ".config"]:
                return True
            # Check content for config patterns
            return bool(re.search(r"[a-z_]+\s*[:=]", sample, re.MULTILINE))

        if hint == DataType.SOURCE_CODE:
            # Should have code-like patterns — ext set must match rule-path set at line 754
            ext = Path(filename).suffix.lower()
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
                # Page captures get a small specificity bump — URL is the
                # strongest available signal for that source type.
                if source_type == "page_capture":
                    confidence = min(confidence + PAGE_CAPTURE_CONFIDENCE_BOOST, 0.98)

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
        Rule-based classification (priority 5) — extension + content patterns.

        File uploads receive `FILE_UPLOAD_CONFIDENCE_BOOST` on content-based
        scores, since the extension signal they carry is more reliable than
        what page captures or pasted text provide.

        Args:
            filename: Original filename (extension is a strong hint)
            content: File content (first 5KB is sampled for perf)
            source_metadata: Optional; used to detect file_upload for boost

        Returns:
            ClassificationResult — always populated (worst case, best-effort
            fallback or UNSTRUCTURED_TEXT with classification_failed=True).
        """
        # Sample content (first 5KB for performance)
        sample = content[:5000].lower()
        ext = Path(filename).suffix.lower()

        # Confidence boost for file uploads (extensions are trustworthy).
        is_file_upload = (
            source_metadata and source_metadata.source_type == "file_upload"
        )
        confidence_boost = FILE_UPLOAD_CONFIDENCE_BOOST if is_file_upload else 0.0

        # 1. VISUAL_EVIDENCE — image extensions are definitive.
        if ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]:
            return ClassificationResult(
                data_type=DataType.VISUAL_EVIDENCE,
                confidence=min(0.98 + confidence_boost, 0.99),
                source="rule_based",
                classification_failed=False,
            )

        # 2. TRACE_DATA vs PROFILING_DATA — two disambiguation helpers.
        #    Order matters: try trace first (more specific), then profiling.
        trace_result = self._disambiguate_profiling_vs_trace(sample, confidence_boost)
        if trace_result is not None:
            return trace_result

        # 3. COMMAND_OUTPUT — 13 Linux/Unix commands, 2+ pattern match required.
        command_result = self._detect_command_output(sample, confidence_boost)
        if command_result is not None:
            return command_result

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

        # Documentation-extension files (md/rst/adoc/txt) contain Markdown
        # constructs like [Anchor] and label: that superficially match config
        # patterns. Guard against the false-positive before checking
        # DOCUMENTATION (step 7).
        doc_exts_early = {".md", ".rst", ".adoc", ".txt"}
        if config_score >= 2 and ext not in doc_exts_early:
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

        # UNSTRUCTURED_TEXT: Generic text without clear structure.
        #
        # ISS-023: short, mixed-pattern text files (e.g., a maintenance notice
        # carrying a datetime + URL + email + version tag) genuinely don't
        # have a single dominant type. Rather than asserting UNSTRUCTURED_TEXT
        # at confidence=0.72, detect the ambiguity and lower confidence below
        # the classification_failed threshold so the cooperative-clarification
        # UX surfaces candidate types and asks the user what the file is.
        if ext in text_exts or markdown_score >= 1:
            ambiguity = self._assess_short_text_ambiguity(content, sample)
            if ambiguity is not None:
                return ClassificationResult(
                    data_type=DataType.UNSTRUCTURED_TEXT,
                    confidence=ambiguity["confidence"],
                    source="rule_based_best_effort",
                    classification_failed=True,
                    suggested_types=ambiguity["suggested_types"],
                )
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
                    source="rule_based_best_effort",
                    classification_failed=True,
                    suggested_types=[
                        DataType.METRICS_AND_PERFORMANCE,
                        DataType.STRUCTURED_CONFIG,
                    ],
                )

            return ClassificationResult(
                data_type=DataType.UNSTRUCTURED_TEXT,
                confidence=0.45,
                source="rule_based_best_effort",
                classification_failed=True,
                suggested_types=[
                    DataType.UNSTRUCTURED_TEXT,
                    DataType.DOCUMENTATION,
                ],
            )

        best_type, best_score = max(scores.items(), key=lambda x: x[1])

        if best_score >= 1:
            # Score-based path: surface the top-3 candidates from the scoring
            # pass so the cooperative-clarification UX can offer them as
            # clickable choices.
            scored_suggestions = _top_suggested_types(scores, n=3)
            # Ensure the chosen best_type is first — _top_suggested_types
            # sorts by score which already places it first, but guard against
            # ties.
            if best_type not in scored_suggestions:
                scored_suggestions = [best_type] + scored_suggestions[:2]
            return ClassificationResult(
                data_type=best_type,
                confidence=0.50,
                source="rule_based_best_effort",
                classification_failed=True,
                suggested_types=scored_suggestions,
            )

        # 8. True fallback: nothing matched at all
        return ClassificationResult(
            data_type=DataType.UNSTRUCTURED_TEXT,
            confidence=0.30,
            source="rule_based",
            classification_failed=True,
            suggested_types=[
                DataType.UNSTRUCTURED_TEXT,
                DataType.DOCUMENTATION,
            ],
        )

    # =========================================================================
    # Named disambiguation helpers
    # =========================================================================

    # Short-text ambiguity gate (ISS-023): only fires for files that are too
    # small to provide stable type signals. The maintenance-notice fixture
    # (low-signal-text.txt) is 362 chars / 8 lines.
    _SHORT_TEXT_MAX_CHARS = 1500
    _SHORT_TEXT_MAX_LINES = 25
    # Need at least this many *distinct* type-suggestive pattern categories
    # firing for the file to count as genuinely ambiguous. Two would be too
    # permissive (any prose with a URL would qualify); three matches the
    # design intent of "no single category dominates".
    _AMBIGUITY_MIN_DISTINCT_CATEGORIES = 3

    def _assess_short_text_ambiguity(
        self,
        content: str,
        sample: str,
    ) -> Optional[dict]:
        """Detect short, mixed-pattern .txt files for which UNSTRUCTURED_TEXT
        cannot be asserted with confidence.

        The classifier's default branch returns UNSTRUCTURED_TEXT @ 0.72
        for any .txt file, regardless of how heterogeneous the contents
        are. For a short maintenance notice that mixes a datetime, a URL,
        an email, and a version tag, none of those signals dominates and
        the file resembles UNSTRUCTURED_TEXT and DOCUMENTATION roughly
        equally. Asserting one type at 0.72 prevents the
        classification_failed gate from firing — the agent never sees
        ambiguity and silently proceeds with whichever type was assigned.

        Returns:
            dict with `confidence` (sub-threshold) and `suggested_types`
            list when the file is short AND has signals from at least
            ``_AMBIGUITY_MIN_DISTINCT_CATEGORIES`` distinct type-suggestive
            categories. None when the file is long enough or
            unambiguous enough for the default UNSTRUCTURED_TEXT path.
        """
        if not content:
            return None

        char_count = len(content)
        line_count = content.count("\n") + 1
        if (
            char_count > self._SHORT_TEXT_MAX_CHARS
            and line_count > self._SHORT_TEXT_MAX_LINES
        ):
            return None

        # Type-suggestive pattern categories. Each category fires at most
        # once even if multiple patterns match — we want breadth (how many
        # different types the file resembles) not depth.
        categories: dict[DataType, str] = {}

        # ISO datetime / date in a maintenance window or event header.
        # Suggests a log line or a structured event record.
        if re.search(r"\d{4}-\d{2}-\d{2}[T\s]\d{1,2}:\d{2}", sample) or re.search(
            r"\d{4}-\d{2}-\d{2}\b", sample
        ):
            categories[DataType.LOGS_AND_ERRORS] = "datetime"

        # URL → typical of documentation / runbooks.
        if re.search(r"https?://", sample):
            categories[DataType.DOCUMENTATION] = "url"

        # Email address — communication metadata, often appears in operational
        # docs but doesn't strongly belong to any single data type. Counted as
        # its own category (UNSTRUCTURED_TEXT) so it pushes the breadth count.
        if re.search(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b", sample):
            categories.setdefault(DataType.UNSTRUCTURED_TEXT, "email")

        # Software artifact tag like "v2.3.8" or "tag: v1.0" — suggests
        # release notes / source code context.
        if re.search(r"\bv\d+\.\d+(?:\.\d+)?\b", sample):
            categories[DataType.SOURCE_CODE] = "version_tag"

        # `key: value` lines — common in config files. Require ≥2 distinct
        # keys before counting, so a single "Contact: foo@bar" line doesn't
        # falsely flag it as STRUCTURED_CONFIG.
        kv_keys = set(
            m.group(1).lower()
            for m in re.finditer(r"(?m)^\s*([A-Za-z][\w\-\s]{2,30})\s*:\s*\S", sample)
        )
        if len(kv_keys) >= 2:
            categories[DataType.STRUCTURED_CONFIG] = "key_value_lines"

        # Numeric column / pipe-separated tabular data — would suggest METRICS.
        # Match conservatively: ≥2 rows of comma- or pipe-separated numerics.
        if len(re.findall(r"(?m)^\s*-?\d+(?:\.\d+)?\s*[,|]\s*-?\d+", sample)) >= 2:
            categories[DataType.METRICS_AND_PERFORMANCE] = "numeric_columns"

        if len(categories) < self._AMBIGUITY_MIN_DISTINCT_CATEGORIES:
            return None

        # File is short AND ambiguous. Surface the top two candidates that
        # the cooperative-clarification UX is most likely to be correct
        # about. UNSTRUCTURED_TEXT and DOCUMENTATION are always reasonable
        # defaults for prose-shaped text; emit them in priority order
        # alongside whatever else fired so the agent can offer a small
        # ranked menu.
        priority_order = [
            DataType.UNSTRUCTURED_TEXT,
            DataType.DOCUMENTATION,
            DataType.LOGS_AND_ERRORS,
            DataType.STRUCTURED_CONFIG,
            DataType.SOURCE_CODE,
            DataType.METRICS_AND_PERFORMANCE,
        ]
        suggested = [
            dt
            for dt in priority_order
            if dt in categories or dt == DataType.UNSTRUCTURED_TEXT
        ]
        # Always include DOCUMENTATION as a candidate if a URL is present —
        # it's the standard's expected pairing for runbook-shaped notices.
        if DataType.DOCUMENTATION in categories and (
            DataType.DOCUMENTATION not in suggested
        ):
            suggested.append(DataType.DOCUMENTATION)
        suggested = suggested[:3]

        return {
            # Just below the classification_failed threshold (0.50). Not so
            # low that downstream confidence-boost paths overshoot it back
            # over the line; not so high that a small content tweak pushes
            # the result into auto-accept.
            "confidence": 0.40,
            "suggested_types": suggested,
        }

    def _disambiguate_profiling_vs_trace(
        self, sample: str, confidence_boost: float
    ) -> Optional[ClassificationResult]:
        """
        Disambiguate between TRACE_DATA (distributed traces) and PROFILING_DATA
        (performance profiling output). Returns None if neither matches.

        Rules:
        - Trace wins if ≥2 of 4 trace-specific patterns match. These cover
          OpenTelemetry-style 32-char trace IDs, span IDs, service mesh
          identifiers, and Jaeger-style JSON structure.
        - Otherwise, profiling wins if ≥1 of 4 profiling patterns matches
          (cProfile header, flame graph syntax, call-count summary,
          filename:lineno format).
        """
        trace_patterns = [
            r'(traceId|trace_id)["\s:]+[a-f0-9]{32}',  # OpenTelemetry 32-char hex
            r'(spanId|span_id|parentId)["\s:]+[a-f0-9]{16,}',
            r'"(serviceName|service\.name)"',
            r'"operationName".*"spans":\s*\[',
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

        profiling_patterns = [
            r"\bncalls\s+tottime\s+percall\s+cumtime",  # cProfile header
            r"[\w\.]+(?:;[\w\.]+)+\s+\d+",  # Flame graph: foo;bar;baz 123
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

        return None

    def _detect_command_output(
        self, sample: str, confidence_boost: float
    ) -> Optional[ClassificationResult]:
        """
        Detect Linux/Unix command output using the `COMMAND_OUTPUTS` signatures.

        Each command requires **≥2 pattern matches** to claim the
        classification — this limits false positives from content that
        incidentally contains a header-like string. Commands route to
        the appropriate detailed DataType (see `COMMAND_OUTPUTS` docstring).
        """
        for cmd_name, signature in COMMAND_OUTPUTS.items():
            matched = sum(
                1
                for p in signature["patterns"]
                if re.search(p, sample, re.MULTILINE | re.IGNORECASE)
            )
            if matched >= 2:
                return ClassificationResult(
                    data_type=signature["type"],
                    confidence=min(signature["confidence"] + confidence_boost, 0.99),
                    source="rule_based",
                    classification_failed=False,
                )
        return None
