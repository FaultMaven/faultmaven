"""Error Preprocessor - Processes stack traces and error reports

Purpose: Parse and format stack traces from multiple programming languages

Supports:
    - Python: Traceback format
    - Java: Exception stack traces
    - JavaScript/Node.js: Error stack traces
    - Go: Panic traces

Key Features:
    - Language detection from patterns
    - Stack trace parsing (exception type, message, frames)
    - Root cause identification
    - Plain text formatting optimized for LLMs
"""

import logging
import uuid
import time
import re
from typing import Optional, Dict, Any, List, Tuple

from faultmaven.models.api import DataType, PreprocessedData, SourceMetadata, ExtractionMetadata
from faultmaven.models.interfaces import IPreprocessor


class ErrorPreprocessor(IPreprocessor):
    """
    Preprocessor for stack traces and error reports

    Parses error reports from multiple languages and formats them
    into LLM-ready summaries with clear structure.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)

        # Language detection patterns
        self.language_patterns = {
            "python": [
                r"Traceback \(most recent call last\):",
                r'File "[^"]+", line \d+',
                r"^\s+File\s+",
            ],
            "java": [
                r"Exception in thread",
                r"at [\w.$]+\(",
                r"Caused by:",
                r"\.java:\d+\)",
            ],
            "javascript": [
                r"Error:",
                r"at .+? \(.+?:\d+:\d+\)",
                r"at Object\.",
                r"at Module\.",
            ],
            "go": [
                r"panic:",
                r"goroutine \d+",
                r"^\s+[a-z]+/[a-z]+.*\.go:\d+",
            ],
        }

    async def process(
        self,
        content: str,
        filename: str,
        source_metadata: Optional[SourceMetadata] = None
    ) -> PreprocessedData:
        """
        Process error report into LLM-ready summary

        Steps:
            1. Detect programming language
            2. Parse stack trace structure
            3. Extract exception type, message, root cause
            4. Build call chain summary
            5. Format into plain text

        Args:
            content: Raw error report content
            filename: Original filename
            source_metadata: Optional source metadata

        Returns:
            PreprocessedData with formatted error summary
        """
        start_time = time.time()

        try:
            # Step 1: Detect language
            language = self._detect_language(content)
            self.logger.debug(f"Detected language: {language}")

            # Step 2: Parse stack trace
            parsed = self._parse_stack_trace(content, language)

            # Step 3: Format summary
            summary = self._format_error_summary(
                parsed=parsed,
                filename=filename,
                source_metadata=source_metadata
            )

            # Step 4: Build PreprocessedData with correct structure
            processing_time = (time.time() - start_time) * 1000
            llm_ready_content = summary[:5000]  # 5K chars for errors (more focused)

            return PreprocessedData(
                content=llm_ready_content,
                metadata=ExtractionMetadata(
                    data_type=DataType.LOGS_AND_ERRORS,
                    extraction_strategy="ast_parse",  # Stack trace parsing
                    llm_calls_used=0,  # Rule-based parsing, no LLM calls
                    confidence=0.9 if parsed.get('exception_type') != 'Unknown' else 0.5,
                    source="rule_based",
                    processing_time_ms=processing_time
                ),
                original_size=len(content),
                processed_size=len(llm_ready_content),
                security_flags=[],
                source_metadata=source_metadata,
                insights=parsed  # Preserve parsed stack trace structure
            )

        except Exception as e:
            self.logger.error(f"Error preprocessing failed: {e}", exc_info=True)
            processing_time = (time.time() - start_time) * 1000

            # Return minimal summary on error
            error_content = f"ERROR REPORT ANALYSIS\n\nERROR: Failed to parse error report\n{str(e)[:500]}"
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

    def _detect_language(self, content: str) -> str:
        """
        Detect programming language from stack trace patterns

        Args:
            content: Raw error content

        Returns:
            Detected language string ('python', 'java', 'javascript', 'go', 'unknown')
        """
        scores = {lang: 0 for lang in self.language_patterns.keys()}

        for language, patterns in self.language_patterns.items():
            for pattern in patterns:
                if re.search(pattern, content, re.MULTILINE | re.IGNORECASE):
                    scores[language] += 1

        # Return language with highest score
        if max(scores.values()) > 0:
            return max(scores, key=scores.get)

        return "unknown"

    def _parse_stack_trace(
        self,
        content: str,
        language: str
    ) -> Dict[str, Any]:
        """
        Parse stack trace based on detected language

        Args:
            content: Raw error content
            language: Detected language

        Returns:
            Dictionary with parsed error information
        """
        if language == "python":
            return self._parse_python_traceback(content)
        elif language == "java":
            return self._parse_java_exception(content)
        elif language == "javascript":
            return self._parse_javascript_error(content)
        elif language == "go":
            return self._parse_go_panic(content)
        else:
            return self._parse_generic_error(content)

    def _parse_python_traceback(self, content: str) -> Dict[str, Any]:
        """Parse Python traceback format"""
        parsed = {
            "language": "python",
            "exception_type": "Unknown",
            "message": "",
            "frames": [],
            "root_cause": None,
        }

        lines = content.split('\n')

        # Extract exception type and message (usually last line)
        for line in reversed(lines):
            if ':' in line and not line.strip().startswith('File'):
                parts = line.split(':', 1)
                if len(parts) == 2:
                    parsed["exception_type"] = parts[0].strip()
                    parsed["message"] = parts[1].strip()
                    break

        # Extract stack frames
        for i, line in enumerate(lines):
            # Python stack frame: File "path", line 123, in function_name
            match = re.match(r'\s*File "([^"]+)", line (\d+)(?:, in (.+))?', line)
            if match:
                file_path, line_num, func_name = match.groups()
                frame_info = f'File "{file_path}", line {line_num}'
                if func_name:
                    frame_info += f", in {func_name}"

                # Get the code line if available (next line)
                if i + 1 < len(lines):
                    code_line = lines[i + 1].strip()
                    if code_line:
                        frame_info += f"\n    {code_line}"

                parsed["frames"].append(frame_info)

        # Root cause is typically the last frame
        if parsed["frames"]:
            parsed["root_cause"] = parsed["frames"][-1].split('\n')[0]

        return parsed

    def _parse_java_exception(self, content: str) -> Dict[str, Any]:
        """Parse Java exception format"""
        parsed = {
            "language": "java",
            "exception_type": "Unknown",
            "message": "",
            "frames": [],
            "root_cause": None,
            "caused_by": []
        }

        lines = content.split('\n')

        # Extract exception type and message (first non-empty line)
        for line in lines:
            if line.strip() and ':' in line:
                parts = line.split(':', 1)
                if 'Exception' in parts[0] or 'Error' in parts[0]:
                    parsed["exception_type"] = parts[0].strip()
                    if len(parts) > 1:
                        parsed["message"] = parts[1].strip()
                    break

        # Extract stack frames
        for line in lines:
            # Java frame: at com.example.Class.method(File.java:123)
            match = re.match(r'\s*at\s+([\w.$<>]+)\(([^)]+)\)', line)
            if match:
                method_path, location = match.groups()
                parsed["frames"].append(f"at {method_path}({location})")

            # Caused by chain
            if line.strip().startswith('Caused by:'):
                parsed["caused_by"].append(line.strip())

        # Root cause is first frame
        if parsed["frames"]:
            parsed["root_cause"] = parsed["frames"][0]

        return parsed

    def _parse_javascript_error(self, content: str) -> Dict[str, Any]:
        """Parse JavaScript/Node.js error format"""
        parsed = {
            "language": "javascript",
            "exception_type": "Error",
            "message": "",
            "frames": [],
            "root_cause": None,
        }

        lines = content.split('\n')

        # Extract error type and message (first line)
        if lines:
            first_line = lines[0].strip()
            if ':' in first_line:
                parts = first_line.split(':', 1)
                parsed["exception_type"] = parts[0].strip()
                if len(parts) > 1:
                    parsed["message"] = parts[1].strip()

        # Extract stack frames
        for line in lines:
            # JS frame: at Function.name (file.js:123:45)
            match = re.match(r'\s*at\s+(.+?)\s+\((.+?):(\d+):(\d+)\)', line)
            if match:
                func_name, file_path, line_num, col_num = match.groups()
                parsed["frames"].append(f"at {func_name} ({file_path}:{line_num}:{col_num})")
            else:
                # Alternative format: at file.js:123:45
                match = re.match(r'\s*at\s+(.+?):(\d+):(\d+)', line)
                if match:
                    file_path, line_num, col_num = match.groups()
                    parsed["frames"].append(f"at {file_path}:{line_num}:{col_num}")

        # Root cause is first frame
        if parsed["frames"]:
            parsed["root_cause"] = parsed["frames"][0]

        return parsed

    def _parse_go_panic(self, content: str) -> Dict[str, Any]:
        """Parse Go panic format"""
        parsed = {
            "language": "go",
            "exception_type": "panic",
            "message": "",
            "frames": [],
            "root_cause": None,
            "goroutine": None
        }

        lines = content.split('\n')

        # Extract panic message
        for line in lines:
            if line.strip().startswith('panic:'):
                parsed["message"] = line.replace('panic:', '').strip()
                break

        # Extract goroutine info
        for line in lines:
            match = re.match(r'goroutine (\d+)', line)
            if match:
                parsed["goroutine"] = match.group(1)
                break

        # Extract stack frames (Go format: package/path.function)
        for i, line in enumerate(lines):
            if re.match(r'^\s+[a-z]+/[a-z]+.*\.go:\d+', line):
                frame = line.strip()
                # Get function name from previous line if available
                if i > 0:
                    func_line = lines[i - 1].strip()
                    if func_line and not func_line.startswith('panic:'):
                        frame = f"{func_line}\n    {frame}"
                parsed["frames"].append(frame)

        # Root cause is first frame
        if parsed["frames"]:
            parsed["root_cause"] = parsed["frames"][0].split('\n')[0]

        return parsed

    def _parse_generic_error(self, content: str) -> Dict[str, Any]:
        """Parse unknown/generic error format"""
        parsed = {
            "language": "unknown",
            "exception_type": "Error",
            "message": content.split('\n')[0] if content else "No message",
            "frames": [],
            "root_cause": None,
        }

        # Try to extract anything that looks like a stack frame
        lines = content.split('\n')
        for line in lines[:20]:  # Limit to first 20 lines
            if any(indicator in line.lower() for indicator in ['at ', 'file ', 'line ', ':']):
                parsed["frames"].append(line.strip())

        return parsed

    def _format_error_summary(
        self,
        parsed: Dict[str, Any],
        filename: str,
        source_metadata: Optional[SourceMetadata]
    ) -> str:
        """
        Format parsed error into LLM-ready summary

        Args:
            parsed: Parsed error information
            filename: Original filename
            source_metadata: Optional source metadata

        Returns:
            Formatted plain text summary
        """
        sections = []

        sections.append("ERROR REPORT ANALYSIS")
        sections.append("=" * 50)
        sections.append("")

        # File information
        sections.append("FILE INFORMATION:")
        sections.append(f"Filename: {filename}")
        sections.append("")

        # Exception details
        sections.append("EXCEPTION:")
        sections.append(f"Type: {parsed.get('exception_type', 'Unknown')}")
        sections.append(f"Message: {parsed.get('message', 'No message')}")
        sections.append(f"Language: {parsed.get('language', 'unknown')}")
        sections.append("")

        # Goroutine info for Go
        if parsed.get('goroutine'):
            sections.append(f"Goroutine: {parsed['goroutine']}")
            sections.append("")

        # Root cause
        root_cause = parsed.get('root_cause')
        if root_cause:
            sections.append("ROOT CAUSE:")
            sections.append(root_cause)
            sections.append("")

        # Call stack
        frames = parsed.get('frames', [])
        if frames:
            sections.append(f"CALL STACK (top {min(15, len(frames))} frames):")
            for i, frame in enumerate(frames[:15], 1):
                # Indent multi-line frames
                frame_lines = frame.split('\n')
                sections.append(f"{i}. {frame_lines[0]}")
                for line in frame_lines[1:]:
                    sections.append(f"   {line}")
            if len(frames) > 15:
                sections.append(f"... and {len(frames) - 15} more frames")
            sections.append("")

        # Caused by chain (Java)
        caused_by = parsed.get('caused_by', [])
        if caused_by:
            sections.append("CAUSED BY CHAIN:")
            for cause in caused_by[:5]:  # Limit to 5
                sections.append(f"• {cause}")
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

        return "\n".join(sections)
