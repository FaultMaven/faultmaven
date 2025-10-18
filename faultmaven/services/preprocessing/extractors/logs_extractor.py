"""
Crime Scene Extraction for LOGS_AND_ERRORS data type

Implements severity-based error detection with adaptive context extraction.
No LLM calls required - pure keyword-based extraction.
"""

import re
from typing import List, Dict, Optional, Tuple


class LogsAndErrorsExtractor:
    """Crime Scene Extraction for logs and error reports (0 LLM calls)"""

    # Severity weights for error prioritization
    SEVERITY_WEIGHTS = {
        'FATAL': 100,
        'CRITICAL': 90,
        'panic': 90,  # Go panic
        'ERROR': 50,
        'WARN': 10,
        'WARNING': 10,
    }

    # Configuration constants
    MAX_SNIPPET_LINES = 500  # Safety limit
    SINGLE_ERROR_CONTEXT_LINES = 200  # ±200 lines around single error
    MULTIPLE_CRIMES_CONTEXT_LINES = 100  # ±100 lines around first + last
    TAIL_EXTRACTION_LINES = 500  # Last 500 lines if no errors
    ERROR_BURST_WINDOW = 50  # Lines to check for clustering
    ERROR_BURST_THRESHOLD = 10  # Min errors to trigger burst mode

    @property
    def strategy_name(self) -> str:
        return "crime_scene"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> str:
        """
        Crime Scene Extraction algorithm:
        1. Find all errors with severity tracking
        2. Prioritize highest-severity error
        3. Detect multiple crime scenes or error bursts
        4. Extract context with adaptive sizing
        5. Safety check: truncate if exceeds limit
        """
        lines = content.split('\n')

        # 1. Find all errors with severity
        errors = self._find_all_errors_with_severity(lines)

        if not errors:
            # No errors found - extract tail
            return self._extract_tail(lines)

        # 2. Find highest-severity error
        primary_error = max(errors, key=lambda e: e['severity'])

        # 3. Check for multiple high-severity errors (ERROR level or higher)
        high_severity = [e for e in errors if e['severity'] >= self.SEVERITY_WEIGHTS['ERROR']]

        if len(high_severity) > 1:
            # Multiple crime scenes: first + last
            return self._extract_multiple_crime_scenes(
                lines,
                high_severity[0],
                high_severity[-1]
            )

        # 4. Check for error burst around primary error
        burst_window = self._detect_error_burst(lines, primary_error['line_idx'])

        if burst_window:
            return self._extract_burst_context(lines, burst_window, primary_error)
        else:
            return self._extract_single_error_context(lines, primary_error)

    def _find_all_errors_with_severity(self, lines: List[str]) -> List[Dict]:
        """
        Scan all lines for error keywords and track severity

        Returns:
            List of dicts with {line_idx, line_text, severity, keyword}
        """
        errors = []

        for idx, line in enumerate(lines):
            # Check each severity keyword
            for keyword, severity in self.SEVERITY_WEIGHTS.items():
                # Case-insensitive match with word boundary
                pattern = rf'\b{re.escape(keyword)}\b'
                if re.search(pattern, line, re.IGNORECASE):
                    errors.append({
                        'line_idx': idx,
                        'line_text': line,
                        'severity': severity,
                        'keyword': keyword
                    })
                    break  # Only count first match per line

        return errors

    def _detect_error_burst(
        self,
        lines: List[str],
        error_idx: int,
        window: Optional[int] = None
    ) -> Optional[Tuple[int, int]]:
        """
        Detect error burst (multiple errors clustered together)

        Args:
            lines: All log lines
            error_idx: Index of primary error
            window: Window size (default: ERROR_BURST_WINDOW)

        Returns:
            (burst_start, burst_end) if burst detected, else None
        """
        if window is None:
            window = self.ERROR_BURST_WINDOW

        # Check ±window lines for error density
        start = max(0, error_idx - window)
        end = min(len(lines), error_idx + window)

        burst_errors = []
        for idx in range(start, end):
            line = lines[idx]
            # Check for any error keyword
            for keyword in self.SEVERITY_WEIGHTS.keys():
                pattern = rf'\b{re.escape(keyword)}\b'
                if re.search(pattern, line, re.IGNORECASE):
                    burst_errors.append(idx)
                    break

        # If >threshold errors in window, it's a burst
        if len(burst_errors) >= self.ERROR_BURST_THRESHOLD:
            return (min(burst_errors), max(burst_errors))

        return None

    def _extract_single_error_context(self, lines: List[str], error: Dict) -> str:
        """
        Extract ±200 lines around a single error

        Args:
            lines: All log lines
            error: Error dict with line_idx

        Returns:
            Formatted snippet with context
        """
        error_idx = error['line_idx']
        context = self.SINGLE_ERROR_CONTEXT_LINES

        start = max(0, error_idx - context)
        end = min(len(lines), error_idx + context + 1)

        snippet = lines[start:end]

        # Safety check
        snippet = self._truncate_if_needed(snippet, error_idx - start)

        return self._format_snippet(snippet, f"Single {error['keyword']} at line {error_idx + 1}")

    def _extract_multiple_crime_scenes(
        self,
        lines: List[str],
        first_error: Dict,
        last_error: Dict
    ) -> str:
        """
        Extract first + last crime scenes
        Captures error onset + current state

        Args:
            lines: All log lines
            first_error: First high-severity error
            last_error: Last high-severity error

        Returns:
            Combined snippet with both scenes
        """
        context = self.MULTIPLE_CRIMES_CONTEXT_LINES

        # Extract around first error (onset)
        first_start = max(0, first_error['line_idx'] - context)
        first_end = min(len(lines), first_error['line_idx'] + context + 1)
        first_snippet = lines[first_start:first_end]

        # Extract around last error (current state)
        last_start = max(0, last_error['line_idx'] - context)
        last_end = min(len(lines), last_error['line_idx'] + context + 1)
        last_snippet = lines[last_start:last_end]

        # Combine snippets with separator
        combined = (
            first_snippet +
            ["\n... [Multiple errors occurred between crime scenes] ...\n"] +
            last_snippet
        )

        # Safety check
        combined = self._truncate_if_needed(combined, len(first_snippet))

        error_count = last_error['line_idx'] - first_error['line_idx']
        return self._format_snippet(
            combined,
            f"Multiple crime scenes: First {first_error['keyword']} at line {first_error['line_idx'] + 1}, "
            f"Last {last_error['keyword']} at line {last_error['line_idx'] + 1} ({error_count} lines apart)"
        )

    def _extract_burst_context(
        self,
        lines: List[str],
        burst_window: Tuple[int, int],
        primary_error: Dict
    ) -> str:
        """
        Extract error burst with expanded window

        Args:
            lines: All log lines
            burst_window: (start_idx, end_idx) of burst
            primary_error: Primary error dict

        Returns:
            Formatted snippet covering full burst
        """
        burst_start, burst_end = burst_window

        # Add padding around burst
        padding = 50
        start = max(0, burst_start - padding)
        end = min(len(lines), burst_end + padding + 1)

        snippet = lines[start:end]

        # Safety check
        snippet = self._truncate_if_needed(snippet, primary_error['line_idx'] - start)

        burst_size = burst_end - burst_start + 1
        return self._format_snippet(
            snippet,
            f"Error burst detected: {burst_size} lines with {primary_error['keyword']} storm"
        )

    def _extract_tail(self, lines: List[str]) -> str:
        """
        Fallback: Extract last N lines if no errors found

        Args:
            lines: All log lines

        Returns:
            Formatted tail snippet
        """
        tail_lines = self.TAIL_EXTRACTION_LINES
        start = max(0, len(lines) - tail_lines)
        snippet = lines[start:]

        return self._format_snippet(
            snippet,
            f"No errors detected - showing last {len(snippet)} lines"
        )

    def _truncate_if_needed(
        self,
        snippet: List[str],
        error_offset: int
    ) -> List[str]:
        """
        Safety check: Truncate snippet if exceeds MAX_SNIPPET_LINES

        Strategy: Keep lines around error, truncate from middle

        Args:
            snippet: Lines to check
            error_offset: Offset of error within snippet

        Returns:
            Potentially truncated snippet
        """
        if len(snippet) <= self.MAX_SNIPPET_LINES:
            return snippet

        # Truncate from middle, keeping start and end around error
        keep_before = 200
        keep_after = 200

        return (
            snippet[:keep_before] +
            [f"\n... [Truncated {len(snippet) - keep_before - keep_after} lines for size] ...\n"] +
            snippet[-keep_after:]
        )

    def _format_snippet(self, lines: List[str], header: str) -> str:
        """
        Format extracted lines with header

        Args:
            lines: Lines to format
            header: Description header

        Returns:
            Formatted string ready for LLM
        """
        formatted = [
            "=" * 60,
            f"CRIME SCENE EXTRACTION: {header}",
            "=" * 60,
            "",
            *lines,
            "",
            "=" * 60,
        ]

        return "\n".join(formatted)
