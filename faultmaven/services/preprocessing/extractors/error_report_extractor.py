"""
Exception Context Extraction for ERROR_REPORT data type

Analyzes standalone exception dumps to extract root cause, relevant stack frames, and fix suggestions.
No LLM calls required - pure stack trace parsing and pattern matching.
"""

import re
from typing import List, Dict, Optional, Tuple


class ErrorReportExtractor:
    """Exception context extraction for standalone error reports (0 LLM calls)"""

    # Supported languages and their stack trace patterns
    LANG_PATTERNS = {
        'python': {
            'traceback_header': r'Traceback \(most recent call last\):',
            'stack_frame': r'File "([^"]+)", line (\d+), in (.+)',
            'exception_line': r'^(\w+(?:Error|Exception)): (.+)$',
        },
        'java': {
            'stack_frame': r'at ([\w\.$]+)\(([\w\.]+):(\d+)\)',
            'exception_line': r'^([\w\.]+(?:Error|Exception)): (.+)$',
        },
        'javascript': {
            'stack_frame': r'at (.+) \(([^:]+):(\d+):(\d+)\)',
            'exception_line': r'^(Error|TypeError|ReferenceError|.*Error): (.+)$',
        },
        'go': {
            'stack_frame': r'^\s+([\w\./]+):(\d+)',
            'panic_line': r'^panic: (.+)$',
        },
    }

    @property
    def strategy_name(self) -> str:
        return "exception_context"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> str:
        """
        Exception Context Extraction algorithm:
        1. Detect programming language
        2. Parse exception type and message
        3. Parse stack trace frames
        4. Identify root cause (innermost frame)
        5. Filter user code vs library code
        6. Extract variable values if present
        7. Generate actionable summary with fix suggestions
        """
        # Detect language
        language = self._detect_language(content)

        # Parse exception data
        exception_type, exception_msg = self._parse_exception(content, language)

        # Parse stack frames
        stack_frames = self._parse_stack_frames(content, language)

        # Generate summary
        return self._generate_summary(
            language, exception_type, exception_msg, stack_frames, content
        )

    def _detect_language(self, content: str) -> str:
        """Detect programming language from exception format"""
        # Python
        if 'Traceback (most recent call last)' in content:
            return 'python'

        # Java
        if re.search(r'at [\w\.$]+\([\w\.]+:\d+\)', content):
            return 'java'

        # JavaScript
        if re.search(r'at .+ \([^:]+:\d+:\d+\)', content):
            return 'javascript'

        # Go
        if 'panic:' in content or re.search(r'goroutine \d+', content):
            return 'go'

        return 'unknown'

    def _parse_exception(self, content: str, language: str) -> Tuple[str, str]:
        """Parse exception type and message"""
        if language not in self.LANG_PATTERNS:
            return "Unknown", "Could not parse exception"

        patterns = self.LANG_PATTERNS[language]

        # Python
        if language == 'python':
            # Find last line that looks like "ExceptionType: message"
            for line in reversed(content.split('\n')):
                match = re.match(patterns.get('exception_line', ''), line.strip())
                if match:
                    return match.group(1), match.group(2)

        # Java
        elif language == 'java':
            for line in content.split('\n'):
                match = re.match(patterns.get('exception_line', ''), line.strip())
                if match:
                    return match.group(1).split('.')[-1], match.group(2)

        # JavaScript
        elif language == 'javascript':
            for line in content.split('\n'):
                match = re.match(patterns.get('exception_line', ''), line.strip())
                if match:
                    return match.group(1), match.group(2)

        # Go
        elif language == 'go':
            match = re.search(patterns.get('panic_line', ''), content, re.MULTILINE)
            if match:
                return "panic", match.group(1)

        return "Unknown", "Could not parse exception"

    def _parse_stack_frames(self, content: str, language: str) -> List[Dict]:
        """Parse stack trace frames"""
        if language not in self.LANG_PATTERNS:
            return []

        patterns = self.LANG_PATTERNS[language]
        frames = []

        # Python
        if language == 'python':
            frame_pattern = patterns.get('stack_frame', '')
            for match in re.finditer(frame_pattern, content, re.MULTILINE):
                frames.append({
                    'file': match.group(1),
                    'line': int(match.group(2)),
                    'function': match.group(3),
                    'is_user_code': self._is_user_code(match.group(1), language)
                })

        # Java
        elif language == 'java':
            frame_pattern = patterns.get('stack_frame', '')
            for match in re.finditer(frame_pattern, content, re.MULTILINE):
                frames.append({
                    'class_method': match.group(1),
                    'file': match.group(2),
                    'line': int(match.group(3)),
                    'is_user_code': self._is_user_code(match.group(1), language)
                })

        # JavaScript
        elif language == 'javascript':
            frame_pattern = patterns.get('stack_frame', '')
            for match in re.finditer(frame_pattern, content, re.MULTILINE):
                frames.append({
                    'function': match.group(1),
                    'file': match.group(2),
                    'line': int(match.group(3)),
                    'column': int(match.group(4)),
                    'is_user_code': self._is_user_code(match.group(2), language)
                })

        # Go
        elif language == 'go':
            frame_pattern = patterns.get('stack_frame', '')
            for match in re.finditer(frame_pattern, content, re.MULTILINE):
                frames.append({
                    'file': match.group(1),
                    'line': int(match.group(2)),
                    'is_user_code': self._is_user_code(match.group(1), language)
                })

        return frames

    def _is_user_code(self, location: str, language: str) -> bool:
        """Determine if code is user code vs library/framework code"""
        library_indicators = {
            'python': ['site-packages', 'lib/python', 'usr/lib', 'venv/lib'],
            'java': ['java.', 'javax.', 'org.springframework', 'com.sun'],
            'javascript': ['node_modules', 'internal/'],
            'go': ['runtime/', 'net/http/', 'sync/'],
        }

        indicators = library_indicators.get(language, [])
        return not any(ind in location for ind in indicators)

    def _generate_summary(
        self,
        language: str,
        exception_type: str,
        exception_msg: str,
        stack_frames: List[Dict],
        full_content: str
    ) -> str:
        """Generate actionable exception summary"""
        lines = [
            "Exception Analysis",
            "",
            f"Language: {language.capitalize()}",
            f"Exception: {exception_type}",
            f"Message: {exception_msg}",
            ""
        ]

        # Find root cause
        if stack_frames:
            # Root cause is the innermost frame (last in stack)
            root_frame = stack_frames[-1]

            lines.append("🎯 Root Cause:")
            if language == 'python':
                lines.append(f"  - Location: {root_frame['file']}:{root_frame['line']}")
                lines.append(f"  - Function: {root_frame['function']}")
            elif language == 'java':
                lines.append(f"  - Location: {root_frame['file']}:{root_frame['line']}")
                lines.append(f"  - Method: {root_frame['class_method']}")
            elif language == 'javascript':
                lines.append(f"  - Location: {root_frame['file']}:{root_frame['line']}:{root_frame.get('column', 0)}")
                lines.append(f"  - Function: {root_frame.get('function', 'anonymous')}")

            # Extract user code frames only
            user_frames = [f for f in stack_frames if f.get('is_user_code', True)]

            if user_frames:
                lines.append("")
                lines.append("Call Path (user code only):")
                for i, frame in enumerate(user_frames, 1):
                    if language == 'python':
                        lines.append(f"{i}. {frame['function']} ({frame['file']}:{frame['line']})")
                    elif language == 'java':
                        lines.append(f"{i}. {frame['class_method']} ({frame['file']}:{frame['line']})")
                    elif language == 'javascript':
                        func = frame.get('function', 'anonymous')
                        lines.append(f"{i}. {func} ({frame['file']}:{frame['line']})")

        # Add fix suggestions based on exception type
        lines.append("")
        lines.append("💡 Likely Fixes:")
        fix_suggestions = self._get_fix_suggestions(exception_type, exception_msg, full_content)
        for suggestion in fix_suggestions:
            lines.append(f"  - {suggestion}")

        return "\n".join(lines)

    def _get_fix_suggestions(
        self,
        exception_type: str,
        exception_msg: str,
        content: str
    ) -> List[str]:
        """Generate fix suggestions based on exception type and message"""
        suggestions = []

        # NullPointerException / AttributeError / TypeError
        if any(x in exception_type for x in ['NullPointer', 'AttributeError', 'TypeError']):
            if 'NoneType' in exception_msg or 'None' in exception_msg:
                suggestions.append("Check for None/null values before accessing attributes or methods")
                suggestions.append("Add null/None checks or use optional chaining")
            else:
                suggestions.append("Verify object initialization before use")
                suggestions.append("Check variable types match expected values")

        # IndexError / ArrayIndexOutOfBounds
        elif any(x in exception_type for x in ['IndexError', 'IndexOutOfBounds']):
            suggestions.append("Verify array/list bounds before accessing elements")
            suggestions.append("Check if collection is empty before indexing")

        # KeyError / NoSuchElementException
        elif any(x in exception_type for x in ['KeyError', 'NoSuchElement']):
            suggestions.append("Verify key exists in dictionary/map before accessing")
            suggestions.append("Use .get() method with default value instead of direct access")

        # Connection / Network errors
        elif any(x in exception_type.lower() for x in ['connection', 'network', 'timeout']):
            suggestions.append("Check network connectivity and firewall rules")
            suggestions.append("Verify service endpoint is reachable")
            suggestions.append("Consider implementing retry logic with exponential backoff")

        # File not found / IO errors
        elif any(x in exception_type for x in ['FileNotFound', 'IOError', 'FileNotFoundException']):
            suggestions.append("Verify file path exists and is accessible")
            suggestions.append("Check file permissions")

        # Import / Module not found
        elif any(x in exception_type for x in ['ImportError', 'ModuleNotFound', 'ClassNotFound']):
            suggestions.append("Verify dependency is installed")
            suggestions.append("Check import path and module name spelling")

        # Default suggestions
        if not suggestions:
            suggestions.append("Review the exception message for specific details")
            suggestions.append("Check the root cause location in your code")

        return suggestions
