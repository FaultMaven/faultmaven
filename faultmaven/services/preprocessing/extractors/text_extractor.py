"""
UNSTRUCTURED_TEXT Extractor

Intelligently extracts key information from unstructured text (documentation,
markdown, error messages, user descriptions). Uses heuristics and structure
detection - no LLM calls required.
"""

import re
from typing import List, Tuple, Optional


class UnstructuredTextExtractor:
    """Smart extraction from unstructured text (0 LLM calls)"""

    # Output limits
    MAX_OUTPUT_CHARS = 10000  # ~2.5K tokens
    MAX_SECTIONS = 20
    MAX_CODE_BLOCKS = 10
    MAX_ERROR_MESSAGES = 15

    @property
    def strategy_name(self) -> str:
        return "direct"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> str:
        """
        Extract key information from unstructured text

        Strategy:
        1. Detect document structure (markdown, plain text, mixed)
        2. Extract high-value content:
           - Error messages and stack traces
           - Code blocks
           - Headings and their content
           - Lists (especially troubleshooting steps)
        3. Prioritize by relevance (errors > code > structure)
        4. Format for readability
        """
        # Detect structure type
        has_markdown = self._has_markdown_structure(content)

        # Extract high-value elements
        errors = self._extract_error_messages(content)
        code_blocks = self._extract_code_blocks(content)

        if has_markdown:
            sections = self._extract_markdown_sections(content)
        else:
            sections = self._extract_plain_text_sections(content)

        # Build output prioritizing errors and code
        output = self._format_output(errors, code_blocks, sections, content)

        # Safety truncation
        if len(output) > self.MAX_OUTPUT_CHARS:
            output = output[:self.MAX_OUTPUT_CHARS] + "\n\n... [Truncated for length]"

        return output

    def _has_markdown_structure(self, content: str) -> bool:
        """Detect if content uses markdown formatting"""
        markdown_patterns = [
            r'^#{1,6}\s+\w+',  # Headings
            r'```[\w]*\n',  # Code blocks
            r'^\*\s+\w+',  # Unordered lists
            r'^\d+\.\s+\w+',  # Ordered lists
            r'\[.+\]\(.+\)',  # Links
        ]

        matches = sum(1 for p in markdown_patterns if re.search(p, content, re.MULTILINE))
        return matches >= 2

    def _extract_error_messages(self, content: str) -> List[Tuple[str, str]]:
        """
        Extract error messages and stack traces

        Returns: [(error_type, error_content), ...]
        """
        errors = []

        # Pattern 1: Exception stack traces (Python, Java)
        stack_trace_pattern = r'(Traceback \(most recent call last\):.*?)(?=\n\n|\Z)'
        for match in re.finditer(stack_trace_pattern, content, re.DOTALL):
            trace = match.group(1).strip()
            # Limit trace length
            if len(trace) > 500:
                lines = trace.split('\n')
                trace = '\n'.join(lines[:10]) + '\n... [Truncated]'
            errors.append(('Stack Trace', trace))

        # Pattern 2: Error lines with common keywords
        error_keywords = ['error', 'exception', 'fatal', 'critical', 'failed', 'failure']
        lines = content.split('\n')

        for i, line in enumerate(lines):
            line_lower = line.lower()
            if any(kw in line_lower for kw in error_keywords):
                # Extract error with context (±2 lines)
                start = max(0, i - 2)
                end = min(len(lines), i + 3)
                error_context = '\n'.join(lines[start:end])

                if len(error_context) > 300:
                    error_context = error_context[:300] + '...'

                errors.append(('Error Message', error_context))

                if len(errors) >= self.MAX_ERROR_MESSAGES:
                    break

        return errors[:self.MAX_ERROR_MESSAGES]

    def _extract_code_blocks(self, content: str) -> List[Tuple[str, str]]:
        """
        Extract code blocks (markdown fenced or indented)

        Returns: [(language, code), ...]
        """
        code_blocks = []

        # Pattern 1: Markdown fenced code blocks (```language\ncode\n```)
        fenced_pattern = r'```([\w]*)\n(.*?)```'
        for match in re.finditer(fenced_pattern, content, re.DOTALL):
            language = match.group(1) or 'unknown'
            code = match.group(2).strip()

            # Limit code block length
            if len(code) > 500:
                lines = code.split('\n')
                code = '\n'.join(lines[:15]) + '\n... [Truncated]'

            code_blocks.append((language, code))

            if len(code_blocks) >= self.MAX_CODE_BLOCKS:
                break

        # Pattern 2: Indented code blocks (4 spaces or tab)
        if len(code_blocks) < self.MAX_CODE_BLOCKS:
            indented_pattern = r'(?:^    .*$\n)+'
            for match in re.finditer(indented_pattern, content, re.MULTILINE):
                code = match.group(0).strip()
                # Remove indentation
                code = '\n'.join(line[4:] if line.startswith('    ') else line for line in code.split('\n'))

                if len(code) > 500:
                    lines = code.split('\n')
                    code = '\n'.join(lines[:15]) + '\n... [Truncated]'

                code_blocks.append(('indented', code))

                if len(code_blocks) >= self.MAX_CODE_BLOCKS:
                    break

        return code_blocks

    def _extract_markdown_sections(self, content: str) -> List[Tuple[str, str, int]]:
        """
        Extract markdown sections by heading

        Returns: [(heading, content, level), ...]
        """
        sections = []
        lines = content.split('\n')

        current_heading = None
        current_content = []
        current_level = 0

        for line in lines:
            # Check for heading
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)

            if heading_match:
                # Save previous section
                if current_heading:
                    content_text = '\n'.join(current_content).strip()
                    if content_text:
                        sections.append((current_heading, content_text, current_level))

                # Start new section
                current_level = len(heading_match.group(1))
                current_heading = heading_match.group(2).strip()
                current_content = []
            else:
                # Skip code blocks and error messages (already extracted)
                if not line.startswith('```') and not any(kw in line.lower() for kw in ['error', 'exception', 'traceback']):
                    current_content.append(line)

        # Save last section
        if current_heading:
            content_text = '\n'.join(current_content).strip()
            if content_text:
                sections.append((current_heading, content_text, current_level))

        return sections[:self.MAX_SECTIONS]

    def _extract_plain_text_sections(self, content: str) -> List[Tuple[str, str, int]]:
        """
        Extract sections from plain text using paragraph detection

        Returns: [(heading, content, level), ...]
        """
        sections = []

        # Split into paragraphs (separated by blank lines)
        paragraphs = re.split(r'\n\s*\n', content)

        for i, para in enumerate(paragraphs):
            para = para.strip()

            if not para:
                continue

            # Skip if it's an error message or code block (already extracted)
            if any(kw in para.lower() for kw in ['error', 'exception', 'traceback', '    ']):
                continue

            # Use first line as heading if short, otherwise use paragraph number
            lines = para.split('\n')
            if len(lines[0]) < 60:
                heading = lines[0]
                content = '\n'.join(lines[1:]) if len(lines) > 1 else lines[0]
            else:
                heading = f"Section {i+1}"
                content = para

            # Limit content length
            if len(content) > 500:
                content = content[:500] + '...'

            sections.append((heading, content, 1))

            if len(sections) >= self.MAX_SECTIONS:
                break

        return sections

    def _format_output(
        self,
        errors: List[Tuple[str, str]],
        code_blocks: List[Tuple[str, str]],
        sections: List[Tuple[str, str, int]],
        content: str
    ) -> str:
        """Format extracted content for readability"""
        output_lines = []

        # Priority 1: Error messages
        if errors:
            output_lines.append("=== ERROR MESSAGES ===\n")
            for i, (error_type, error_content) in enumerate(errors, 1):
                output_lines.append(f"{i}. {error_type}:")
                output_lines.append(error_content)
                output_lines.append("")

        # Priority 2: Code blocks
        if code_blocks:
            output_lines.append("=== CODE BLOCKS ===\n")
            for i, (language, code) in enumerate(code_blocks, 1):
                output_lines.append(f"{i}. Code ({language}):")
                output_lines.append(code)
                output_lines.append("")

        # Priority 3: Structured content
        if sections:
            output_lines.append("=== DOCUMENT CONTENT ===\n")
            for heading, content, level in sections:
                indent = "  " * (level - 1)
                output_lines.append(f"{indent}## {heading}")
                output_lines.append(content)
                output_lines.append("")

        # If nothing was extracted, return original content (truncated)
        if not errors and not code_blocks and not sections:
            output_lines.append("=== FULL TEXT (No structure detected) ===\n")
            # Return first 5000 chars
            return "\n".join(output_lines) + content[:5000]

        return "\n".join(output_lines)
