"""
Exception Context Extraction for ERROR_REPORT data type

Analyzes standalone exception dumps to extract root cause, relevant stack frames, and fix suggestions.
No LLM calls required - pure stack trace parsing and pattern matching.
"""

import re
from typing import TYPE_CHECKING

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    has_content,
)

if TYPE_CHECKING:
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore


# Block split detection patterns. Each language has a regex that, when
# matched at the start of a line, marks the beginning of a new independent
# error block. Files often pack multiple unrelated errors back-to-back
# (timestamped log dumps, multi-traceback Python output, panic followed by
# panic). Splitting before per-language parsing ensures every top-level
# exception is surfaced — without this the parser conflates frames from
# different errors into one bogus call path and silently drops every
# exception type after the first one matched by ``_parse_exception``.
_BLOCK_START_PATTERNS: dict[str, re.Pattern[str]] = {
    # ISO-like timestamp + ERROR/FATAL/Exception level (Spring, log4j, etc.)
    # Example: "2024-01-15 10:23:47.842 ERROR [order-service-pool-3] ..."
    "java": re.compile(
        r"^\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}:\d{2}[.,]\d+\s+(?:ERROR|FATAL|Exception)"
    ),
    # Python tracebacks always start with this exact header.
    "python": re.compile(r"^Traceback \(most recent call last\):"),
    # Go panic header.
    "go": re.compile(r"^panic:"),
}


class ErrorReportExtractor:
    """Exception context extraction for standalone error reports (0 LLM calls)"""

    # Supported languages and their stack trace patterns
    LANG_PATTERNS = {
        "python": {
            "traceback_header": r"Traceback \(most recent call last\):",
            "stack_frame": r'File "([^"]+)", line (\d+), in (.+)',
            # Allow dotted module prefixes (e.g. ``jinja2.exceptions.TemplateNotFound``,
            # ``httpx.ConnectTimeout``, ``app.exceptions.ReportGenerationError``).
            # The class name itself must end in ``Error`` or ``Exception`` to avoid
            # false positives on ordinary log lines.
            "exception_line": r"^([\w\.]+(?:Error|Exception)): (.+)$",
        },
        "java": {
            "stack_frame": r"at ([\w\.$]+)\(([\w\.]+):(\d+)\)",
            "exception_line": r"^([\w\.]+(?:Error|Exception)): (.+)$",
        },
        "javascript": {
            "stack_frame": r"at (.+) \(([^:]+):(\d+):(\d+)\)",
            "exception_line": r"^(Error|TypeError|ReferenceError|.*Error): (.+)$",
        },
        "go": {
            "stack_frame": r"^\s+([\w\./]+):(\d+)",
            "panic_line": r"^panic: (.+)$",
        },
    }

    @property
    def strategy_name(self) -> str:
        return "exception_context"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> ExtractResult:
        """
        Exception Context Extraction algorithm:
        1. Detect programming language
        2. Split content into independent error blocks
        3. Per block: parse exception type/message, parse stack frames,
           identify root cause (innermost frame)
        4. Combine per-block summaries into a single ``file_extract``
        5. Append fix suggestions (computed once over the full content)
        """
        if not has_content(content):
            return ExtractResult(file_extract=EMPTY_CONTENT_RESPONSE)

        # Detect language
        language = self._detect_language(content)

        # Split into independent error blocks (≥1 always)
        blocks = self._split_into_blocks(content, language)

        # Parse each block independently
        parsed_blocks: list[dict] = []
        for block_text, block_header in blocks:
            exception_type, exception_msg = self._parse_exception(block_text, language)
            stack_frames = self._parse_stack_frames(block_text, language)

            root_cause = None
            if stack_frames:
                root_frame = stack_frames[-1]
                root_cause = root_frame.get("file", root_frame.get("class_method", "?"))

            parsed_blocks.append(
                {
                    "header": block_header,
                    "exception_type": exception_type,
                    "exception_msg": exception_msg,
                    "stack_frames": stack_frames,
                    "root_cause": root_cause,
                }
            )

        # Generate combined summary
        result_text = self._generate_summary(language, parsed_blocks, content)

        # Build file_meta
        exception_types = [
            b["exception_type"]
            for b in parsed_blocks
            if b["exception_type"] not in ("Unknown", None)
        ]
        total_frames = sum(len(b["stack_frames"]) for b in parsed_blocks)
        file_meta: dict = {
            "language": language,
            "exception_count": len(parsed_blocks),
            "exception_types": exception_types,
            # Backward-compat: ``exception_type`` and ``root_cause`` reflect
            # the FIRST block (the one a single-block file would have produced).
            "exception_type": parsed_blocks[0]["exception_type"],
            "stack_frames": total_frames,
            "size_bytes": len(content.encode("utf-8", errors="replace")),
        }
        if parsed_blocks[0]["root_cause"]:
            file_meta["root_cause"] = parsed_blocks[0]["root_cause"]

        return ExtractResult(file_extract=result_text, file_meta=file_meta)

    def _split_into_blocks(
        self, content: str, language: str
    ) -> list[tuple[str, str | None]]:
        """Split ``content`` into independent error blocks.

        A block is the contiguous text spanning from one block-start marker
        (exclusive of the previous one) up to the next marker or end-of-file.
        For each block we also capture an optional header — the block-start
        line itself — so the summary can label sections with their original
        timestamp / context.

        When the language has no block-start pattern, or no marker is found,
        the whole content is returned as a single block. This preserves the
        single-error case unchanged.
        """
        pattern = _BLOCK_START_PATTERNS.get(language)
        if pattern is None:
            return [(content, None)]

        lines = content.split("\n")
        # Indices of lines that mark the start of a new block.
        starts = [i for i, line in enumerate(lines) if pattern.match(line)]

        # Python: a "Traceback (most recent call last):" preceded (within
        # ~3 lines, ignoring blanks) by the chaining marker
        # "During handling of the above exception, another exception
        # occurred:" or "The above exception was the direct cause of the
        # following exception:" is NOT a new top-level block — it is the
        # second half of a chained exception pair belonging to the previous
        # block. Filter those out so the count matches the user's intuition
        # of "distinct top-level exceptions".
        if language == "python" and len(starts) > 1:
            chain_markers = (
                "During handling of the above exception",
                "The above exception was the direct cause",
            )
            filtered: list[int] = []
            for idx in starts:
                # Look back up to 3 non-blank lines for a chain marker.
                is_chained = False
                non_blank_seen = 0
                for back in range(idx - 1, -1, -1):
                    line = lines[back].strip()
                    if not line:
                        continue
                    non_blank_seen += 1
                    if any(marker in line for marker in chain_markers):
                        is_chained = True
                        break
                    if non_blank_seen >= 3:
                        break
                if not is_chained or not filtered:
                    # Always keep the FIRST traceback start, even if its
                    # immediate predecessor matches (defensive).
                    filtered.append(idx)
            starts = filtered

        if not starts:
            return [(content, None)]

        # If the first marker is not at line 0, prepend an "intro" block
        # spanning lines [0:starts[0]] only when it contains an exception
        # itself. Most timestamped logs have nothing meaningful before the
        # first ERROR line, so dropping it keeps the summary clean.
        blocks: list[tuple[str, str | None]] = []
        for idx, start in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
            block_lines = lines[start:end]
            block_text = "\n".join(block_lines).rstrip()
            if not block_text.strip():
                continue
            # Header = the marker line itself, trimmed for display.
            header = block_lines[0].strip() if block_lines else None
            blocks.append((block_text, header))

        # Defensive fallback: if splitting somehow produced nothing
        # (e.g. all blocks were whitespace), return the full content.
        if not blocks:
            return [(content, None)]

        return blocks

    def _detect_language(self, content: str) -> str:
        """Detect programming language from exception format"""
        # Python
        if "Traceback (most recent call last)" in content:
            return "python"

        # Java
        if re.search(r"at [\w\.$]+\([\w\.]+:\d+\)", content):
            return "java"

        # JavaScript
        if re.search(r"at .+ \([^:]+:\d+:\d+\)", content):
            return "javascript"

        # Go
        if "panic:" in content or re.search(r"goroutine \d+", content):
            return "go"

        return "unknown"

    def _parse_exception(self, content: str, language: str) -> tuple[str, str]:
        """Parse exception type and message"""
        if language not in self.LANG_PATTERNS:
            return "Unknown", "Could not parse exception"

        patterns = self.LANG_PATTERNS[language]

        # Python
        if language == "python":
            # Find last line that looks like "ExceptionType: message"
            for line in reversed(content.split("\n")):
                match = re.match(patterns.get("exception_line", ""), line.strip())
                if match:
                    return match.group(1), match.group(2)

        # Java
        elif language == "java":
            for line in content.split("\n"):
                match = re.match(patterns.get("exception_line", ""), line.strip())
                if match:
                    return match.group(1).split(".")[-1], match.group(2)

        # JavaScript
        elif language == "javascript":
            for line in content.split("\n"):
                match = re.match(patterns.get("exception_line", ""), line.strip())
                if match:
                    return match.group(1), match.group(2)

        # Go
        elif language == "go":
            match = re.search(patterns.get("panic_line", ""), content, re.MULTILINE)
            if match:
                return "panic", match.group(1)

        return "Unknown", "Could not parse exception"

    def _parse_stack_frames(self, content: str, language: str) -> list[dict]:
        """Parse stack trace frames"""
        if language not in self.LANG_PATTERNS:
            return []

        patterns = self.LANG_PATTERNS[language]
        frames = []

        # Python
        if language == "python":
            frame_pattern = patterns.get("stack_frame", "")
            for match in re.finditer(frame_pattern, content, re.MULTILINE):
                frames.append(
                    {
                        "file": match.group(1),
                        "line": int(match.group(2)),
                        "function": match.group(3),
                        "is_user_code": self._is_user_code(match.group(1), language),
                    }
                )

        # Java
        elif language == "java":
            frame_pattern = patterns.get("stack_frame", "")
            for match in re.finditer(frame_pattern, content, re.MULTILINE):
                frames.append(
                    {
                        "class_method": match.group(1),
                        "file": match.group(2),
                        "line": int(match.group(3)),
                        "is_user_code": self._is_user_code(match.group(1), language),
                    }
                )

        # JavaScript
        elif language == "javascript":
            frame_pattern = patterns.get("stack_frame", "")
            for match in re.finditer(frame_pattern, content, re.MULTILINE):
                frames.append(
                    {
                        "function": match.group(1),
                        "file": match.group(2),
                        "line": int(match.group(3)),
                        "column": int(match.group(4)),
                        "is_user_code": self._is_user_code(match.group(2), language),
                    }
                )

        # Go
        elif language == "go":
            frame_pattern = patterns.get("stack_frame", "")
            for match in re.finditer(frame_pattern, content, re.MULTILINE):
                frames.append(
                    {
                        "file": match.group(1),
                        "line": int(match.group(2)),
                        "is_user_code": self._is_user_code(match.group(1), language),
                    }
                )

        # Java, JavaScript, and Go list the most recent call FIRST (opposite of Python).
        # Reverse so frames[-1] consistently points to the innermost (root cause) frame.
        if language in ("java", "javascript", "go"):
            frames.reverse()

        return frames

    def _is_user_code(self, location: str, language: str) -> bool:
        """Determine if code is user code vs library/framework code"""
        library_indicators = {
            "python": ["site-packages", "lib/python", "usr/lib", "venv/lib"],
            "java": ["java.", "javax.", "org.springframework", "com.sun"],
            "javascript": ["node_modules", "internal/"],
            "go": ["runtime/", "net/http/", "sync/"],
        }

        indicators = library_indicators.get(language, [])
        return not any(ind in location for ind in indicators)

    def _generate_summary(
        self,
        language: str,
        parsed_blocks: list[dict],
        full_content: str,
    ) -> str:
        """Generate actionable exception summary across all parsed blocks."""
        block_count = len(parsed_blocks)
        noun = "exception" if block_count == 1 else "exceptions"
        lines: list[str] = [
            f"Exception Analysis ({block_count} {noun} detected)",
            "",
        ]

        for idx, block in enumerate(parsed_blocks, start=1):
            # Section heading. For multi-block files include the originating
            # log line (timestamped header) so the agent can disambiguate.
            if block_count > 1:
                if block["header"]:
                    lines.append(f"### Exception {idx} ({block['header']})")
                else:
                    lines.append(f"### Exception {idx}")
                lines.append("")
            lines.append(f"Language: {language.capitalize()}")
            lines.append(f"Exception: {block['exception_type']}")
            lines.append(f"Message: {block['exception_msg']}")
            lines.append("")

            stack_frames = block["stack_frames"]
            if stack_frames:
                root_frame = stack_frames[-1]

                lines.append("🎯 Root Cause:")
                if language == "python":
                    lines.append(
                        f"  - Location: {root_frame['file']}:{root_frame['line']}"
                    )
                    lines.append(f"  - Function: {root_frame['function']}")
                elif language == "java":
                    lines.append(
                        f"  - Location: {root_frame['file']}:{root_frame['line']}"
                    )
                    lines.append(f"  - Method: {root_frame['class_method']}")
                elif language == "javascript":
                    lines.append(
                        f"  - Location: {root_frame['file']}:{root_frame['line']}:{root_frame.get('column', 0)}"
                    )
                    lines.append(
                        f"  - Function: {root_frame.get('function', 'anonymous')}"
                    )

                user_frames = [f for f in stack_frames if f.get("is_user_code", True)]
                if user_frames:
                    lines.append("")
                    lines.append("Call Path (user code only):")
                    for i, frame in enumerate(user_frames, 1):
                        if language == "python":
                            lines.append(
                                f"{i}. {frame['function']} ({frame['file']}:{frame['line']})"
                            )
                        elif language == "java":
                            lines.append(
                                f"{i}. {frame['class_method']} ({frame['file']}:{frame['line']})"
                            )
                        elif language == "javascript":
                            func = frame.get("function", "anonymous")
                            lines.append(
                                f"{i}. {func} ({frame['file']}:{frame['line']})"
                            )

            # Spacer between sections in multi-block output.
            if block_count > 1 and idx < block_count:
                lines.append("")

        # Fix suggestions: computed once over the full content. For
        # multi-block files we feed the FIRST block's exception type/message
        # to keep the heuristic deterministic — but the suggestion list is
        # advisory and the heading hints that all blocks should be reviewed.
        primary = parsed_blocks[0]
        lines.append("")
        lines.append("💡 Likely Fixes:")
        fix_suggestions = self._get_fix_suggestions(
            primary["exception_type"], primary["exception_msg"], full_content
        )
        for suggestion in fix_suggestions:
            lines.append(f"  - {suggestion}")

        return "\n".join(lines)

    def _get_fix_suggestions(
        self, exception_type: str, exception_msg: str, content: str
    ) -> list[str]:
        """Generate fix suggestions based on exception type and message"""
        suggestions = []

        # NullPointerException / AttributeError / TypeError
        if any(
            x in exception_type for x in ["NullPointer", "AttributeError", "TypeError"]
        ):
            if "NoneType" in exception_msg or "None" in exception_msg:
                suggestions.append(
                    "Check for None/null values before accessing attributes or methods"
                )
                suggestions.append("Add null/None checks or use optional chaining")
            else:
                suggestions.append("Verify object initialization before use")
                suggestions.append("Check variable types match expected values")

        # IndexError / ArrayIndexOutOfBounds
        elif any(x in exception_type for x in ["IndexError", "IndexOutOfBounds"]):
            suggestions.append("Verify array/list bounds before accessing elements")
            suggestions.append("Check if collection is empty before indexing")

        # KeyError / NoSuchElementException
        elif any(x in exception_type for x in ["KeyError", "NoSuchElement"]):
            suggestions.append("Verify key exists in dictionary/map before accessing")
            suggestions.append(
                "Use .get() method with default value instead of direct access"
            )

        # Connection / Network errors
        elif any(
            x in exception_type.lower() for x in ["connection", "network", "timeout"]
        ):
            suggestions.append("Check network connectivity and firewall rules")
            suggestions.append("Verify service endpoint is reachable")
            suggestions.append(
                "Consider implementing retry logic with exponential backoff"
            )

        # File not found / IO errors
        elif any(
            x in exception_type
            for x in ["FileNotFound", "IOError", "FileNotFoundException"]
        ):
            suggestions.append("Verify file path exists and is accessible")
            suggestions.append("Check file permissions")

        # Import / Module not found
        elif any(
            x in exception_type
            for x in ["ImportError", "ModuleNotFound", "ClassNotFound"]
        ):
            suggestions.append("Verify dependency is installed")
            suggestions.append("Check import path and module name spelling")

        # Default suggestions
        if not suggestions:
            suggestions.append("Review the exception message for specific details")
            suggestions.append("Check the root cause location in your code")

        return suggestions
