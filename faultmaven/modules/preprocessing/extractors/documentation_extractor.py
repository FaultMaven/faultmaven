"""
Documentation Structure Extraction for DOCUMENTATION data type

Extracts structured information from runbooks, wikis, and technical documentation.
Focuses on extracting procedures, troubleshooting steps, and configuration references.
No LLM calls required - pure markdown/text parsing.
"""

import re

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    MAX_STRUCTURAL_INDEX_CHARS,
    has_content,
    truncate_output,
)


class DocumentationExtractor:
    """Documentation structure extraction for runbooks and wikis (0 LLM calls)"""

    @property
    def strategy_name(self) -> str:
        return "documentation_structure"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> ExtractResult:
        """
        Documentation Structure Extraction algorithm:
        1. Detect documentation format (Markdown, reStructuredText, plain text)
        2. Extract document title
        3. Extract section headings and hierarchy
        4. Identify key sections (troubleshooting, procedures, config)
        5. Extract code blocks and commands
        6. Generate structured summary
        """
        content = content.lstrip("\ufeff")
        if len(content) > 50_000_000:
            return ExtractResult(
                file_extract="[File exceeds 50MB maximum size limit for extraction]"
            )

        if not has_content(content):
            return ExtractResult(file_extract=EMPTY_CONTENT_RESPONSE)

        # Detect format
        is_markdown = self._is_markdown(content)

        # Extract document structure
        title = self._extract_title(content, is_markdown)
        sections = self._extract_sections(content, is_markdown)
        code_blocks = self._extract_code_blocks(content, is_markdown)

        # Identify key sections
        troubleshooting_sections = self._find_troubleshooting_sections(sections)
        procedure_sections = self._find_procedure_sections(sections)
        config_sections = self._find_config_sections(sections)

        # Lead paragraph — README-style metadata that titles often miss
        # (project description, version label, scope statement).
        lead_paragraph = self._extract_lead_paragraph(content, is_markdown)

        # Generate summary
        result = self._generate_summary(
            title,
            sections,
            code_blocks,
            troubleshooting_sections,
            procedure_sections,
            config_sections,
            lead_paragraph=lead_paragraph,
        )

        # Count commands in code blocks
        commands_count = sum(
            1
            for cb in code_blocks
            if cb["type"] == "inline" and self._looks_like_command(cb["code"])
        )

        file_meta: dict = {
            "format": "markdown" if is_markdown else "plain",
            "sections": len(sections),
            "code_blocks": len(code_blocks),
            "commands": commands_count,
            "size_bytes": len(content.encode("utf-8", errors="replace")),
        }
        if not sections:
            file_meta["empty_toc"] = True
        return ExtractResult(file_extract=result, file_meta=file_meta)

    def _is_markdown(self, content: str) -> bool:
        """Detect if content is Markdown format"""
        markdown_indicators = [
            r"^#{1,6}\s+\w+",  # Headers
            r"```[\w]*\n",  # Code blocks
            r"^\*\*\w+\*\*",  # Bold
            r"^\[.+\]\(.+\)",  # Links
        ]

        return (
            sum(
                1
                for pattern in markdown_indicators
                if re.search(pattern, content, re.MULTILINE)
            )
            >= 1
        )

    # Truncation cap for title strings — long enough to keep typical
    # README titles intact (incl. trailing badge links), short enough to
    # keep the FILE SUMMARY first line scannable.
    _TITLE_MAX_LEN = 250

    def _extract_title(self, content: str, is_markdown: bool) -> str:
        """Extract document title.

        Handles three header styles:
          - ATX H1:    "# Title"
          - Setext H1: "Title\n=====" (title underlined with `=`)
          - Setext H2: "Title\n-----" (title underlined with `-`)
                      — used as a fallback when no H1 is found
        Falls through to the first non-empty line for plain-text files.
        """
        lines = content.split("\n")

        if is_markdown:
            # ATX-style H1 first (most common).
            for line in lines[:10]:
                if line.startswith("# "):
                    return line[2:].strip()[: self._TITLE_MAX_LEN]
            # Setext-style: a non-empty line followed by `===` or `---`.
            # README files commonly use this form (e.g. NAB README).
            for i in range(min(len(lines), 10) - 1):
                title_line = lines[i].strip()
                underline = lines[i + 1].strip()
                if not title_line:
                    continue
                if len(underline) >= 3 and (
                    set(underline) == {"="} or set(underline) == {"-"}
                ):
                    return title_line[: self._TITLE_MAX_LEN]

        # Fallback: first non-empty line.
        for line in lines[:5]:
            if line.strip():
                return line.strip()[: self._TITLE_MAX_LEN]

        return "Untitled Document"

    def _extract_lead_paragraph(self, content: str, is_markdown: bool) -> str | None:
        """Return the first prose paragraph after the title.

        README-style documents put critical identifying metadata in the
        lead paragraph (project description, version label, scope) — not
        the title. Surfacing this paragraph in the FILE SUMMARY lets the
        agent answer "what is this about?" with the metadata-level facts
        (e.g. "v1.1") that the title alone doesn't expose.

        Heuristic: skip leading whitespace, the title line, any Setext
        underline, and any ATX header that follows. Take the first
        contiguous block of non-empty prose lines, capped to ~600 chars.
        """
        lines = content.split("\n")
        # Skip up through the title region.
        i = 0
        n = len(lines)
        # Skip leading blank lines.
        while i < n and not lines[i].strip():
            i += 1
        if i >= n:
            return None
        # If this line is an ATX header, advance past it.
        if is_markdown and lines[i].startswith("# "):
            i += 1
        # If this line is a Setext title (next line is === or ---), skip both.
        elif (
            is_markdown
            and i + 1 < n
            and lines[i].strip()
            and lines[i + 1].strip()
            and (
                set(lines[i + 1].strip()) == {"="} or set(lines[i + 1].strip()) == {"-"}
            )
            and len(lines[i + 1].strip()) >= 3
        ):
            i += 2
        else:
            # Plain text — first prose line IS the title, skip it.
            i += 1
        # Skip blank lines between title and lead paragraph.
        while i < n and not lines[i].strip():
            i += 1
        # Skip any subheader (## Section).
        if is_markdown and i < n and lines[i].lstrip().startswith("#"):
            return None
        # Collect the first contiguous prose block.
        para: list[str] = []
        while i < n and lines[i].strip():
            para.append(lines[i].strip())
            i += 1
        if not para:
            return None
        joined = " ".join(para)
        if len(joined) > 600:
            joined = joined[:597] + "..."
        return joined

    def _extract_sections(self, content: str, is_markdown: bool) -> list[dict]:
        """Extract section headings and their content"""
        sections = []

        if is_markdown:
            # Match Markdown headers: # H1, ## H2, ### H3, etc.
            header_pattern = r"^(#{1,6})\s+(.+)$"

            lines = content.split("\n")
            current_section = None
            section_content = []

            for i, line in enumerate(lines):
                match = re.match(header_pattern, line)

                if match:
                    # Save previous section
                    if current_section:
                        current_section["content"] = "\n".join(section_content).strip()
                        sections.append(current_section)

                    # Start new section
                    level = len(match.group(1))
                    title = match.group(2).strip()

                    current_section = {
                        "level": level,
                        "title": title,
                        "line_num": i + 1,
                        "content": "",
                    }
                    section_content = []
                elif current_section:
                    section_content.append(line)

            # Save last section
            if current_section:
                current_section["content"] = "\n".join(section_content).strip()
                sections.append(current_section)

        else:
            # Plain text: look for underlined headers
            lines = content.split("\n")
            for i in range(len(lines) - 1):
                # Check if next line is all ==== or ----
                if re.match(r"^[=\-]{3,}$", lines[i + 1].strip()):
                    sections.append(
                        {
                            "level": 1 if "=" in lines[i + 1] else 2,
                            "title": lines[i].strip(),
                            "line_num": i + 1,
                            "content": "",
                        }
                    )

        return sections

    def _extract_code_blocks(self, content: str, is_markdown: bool) -> list[dict]:
        """Extract code blocks and inline commands"""
        code_blocks = []

        if is_markdown:
            # Extract fenced code blocks: ```language\ncode\n```
            pattern = r"```([\w]*)\n([\s\S]*?)```"

            for match in re.finditer(pattern, content):
                language = match.group(1) or "text"
                code = match.group(2).strip()

                code_blocks.append(
                    {"language": language, "code": code, "type": "fenced"}
                )

        # Extract inline code commands (backtick-wrapped or indented)
        inline_pattern = r"`([^`]+)`"
        for match in re.finditer(inline_pattern, content):
            command = match.group(1).strip()

            # Only include if it looks like a command
            if self._looks_like_command(command):
                code_blocks.append(
                    {"language": "shell", "code": command, "type": "inline"}
                )

        return code_blocks

    # Shell commands recognised as commands regardless of inline length.
    # Matched as whole whitespace-separated tokens so that short but
    # legitimate entries like ``top``, ``ps``, ``ping`` are not dropped
    # by a length gate.
    _COMMAND_INDICATORS = (
        "kubectl",
        "docker",
        "systemctl",
        "journalctl",
        "tail",
        "grep",
        "curl",
        "wget",
        "ssh",
        "scp",
        "ps",
        "top",
        "netstat",
        "ifconfig",
        "ping",
        "traceroute",
        "git",
        "npm",
        "pip",
        "mvn",
        "gradle",
        "cargo",
        "go",
    )

    def _looks_like_command(self, text: str) -> bool:
        """Check if text looks like a shell command.

        Detection is token-based rather than substring-based: each
        whitespace-separated token (lower-cased) is compared for
        exact equality against a known-commands list. This supersedes
        the previous ``cmd in text.lower()`` test, which had two
        structural flaws:

        * ``go`` matched inside ``google.com`` and ``scargo``; ``pip``
          matched inside ``zipper``, etc. — unbounded false positives.
        * A ``len(text) <= 5`` gate ran before the indicator check, so
          legitimate short entries like ``top``, ``ps``, ``df -h``,
          ``free``, ``ping`` were silently rejected.

        Iterating the tokens preserves the ``sudo kubectl ...`` style
        (``kubectl`` is a whole token) while rejecting ``google.com``
        (no token equals ``go``).
        """
        stripped = text.strip()
        if not stripped:
            return False

        tokens = {t.lower() for t in stripped.split()}
        return any(cmd in tokens for cmd in self._COMMAND_INDICATORS)

    def _find_troubleshooting_sections(self, sections: list[dict]) -> list[dict]:
        """Identify sections related to troubleshooting"""
        keywords = [
            "troubleshoot",
            "debug",
            "error",
            "problem",
            "issue",
            "diagnos",
            "fix",
            "resolve",
        ]

        return [
            section
            for section in sections
            if any(keyword in section["title"].lower() for keyword in keywords)
        ]

    def _find_procedure_sections(self, sections: list[dict]) -> list[dict]:
        """Identify sections containing procedures"""
        keywords = [
            "how to",
            "procedure",
            "step",
            "install",
            "setup",
            "configure",
            "deploy",
            "guide",
        ]

        return [
            section
            for section in sections
            if any(keyword in section["title"].lower() for keyword in keywords)
        ]

    def _find_config_sections(self, sections: list[dict]) -> list[dict]:
        """Identify sections related to configuration"""
        keywords = [
            "config",
            "setting",
            "parameter",
            "environment",
            "variable",
            "option",
        ]

        return [
            section
            for section in sections
            if any(keyword in section["title"].lower() for keyword in keywords)
        ]

    def _generate_summary(
        self,
        title: str,
        sections: list[dict],
        code_blocks: list[dict],
        troubleshooting_sections: list[dict],
        procedure_sections: list[dict],
        config_sections: list[dict],
        lead_paragraph: str | None = None,
    ) -> str:
        """Generate structured documentation summary"""
        lines = [
            f"Documentation: {title}",
            "",
        ]
        if lead_paragraph:
            lines.append(f"Abstract: {lead_paragraph}")
            lines.append("")
        lines.extend(
            [
                f"📄 Document Overview:",
                f"  - Total sections: {len(sections)}",
                f"  - Code blocks: {len(code_blocks)}",
                "",
            ]
        )

        # Troubleshooting sections
        if troubleshooting_sections:
            lines.append("🔧 Troubleshooting Sections:")
            for section in troubleshooting_sections[:5]:  # Top 5
                lines.append(f"  - {section['title']}")
            lines.append("")

        # Procedure sections
        if procedure_sections:
            lines.append("📋 Procedures:")
            for section in procedure_sections[:5]:  # Top 5
                lines.append(f"  - {section['title']}")
            lines.append("")

        # Configuration sections
        if config_sections:
            lines.append("⚙️  Configuration:")
            for section in config_sections[:5]:  # Top 5
                lines.append(f"  - {section['title']}")
            lines.append("")

        # Code blocks / Commands
        if code_blocks:
            shell_commands = [
                cb
                for cb in code_blocks
                if cb["language"] in ["shell", "bash", "sh", "text"]
            ]
            if shell_commands:
                lines.append("💻 Key Commands:")
                for cmd_block in shell_commands[:10]:  # First 10 commands
                    code = cmd_block["code"]
                    # Truncate long commands
                    if len(code) > 80:
                        code = code[:77] + "..."
                    lines.append(f"  $ {code}")
                lines.append("")

        # Full table of contents
        if sections:
            lines.append("📑 Table of Contents:")
            for section in sections:
                indent = "  " * (section["level"] - 1)
                lines.append(f"{indent}- {section['title']}")

        # Actual section content — allows the agent to answer questions about
        # what the document contains (tables, descriptions, references, etc.)
        # without an LLM summarisation step.
        content_lines: list[str] = []
        for section in sections:
            body = section.get("content", "").strip()
            if not body:
                continue
            indent = "  " * (section["level"] - 1)
            content_lines.append(f"\n{indent}## {section['title']}")
            # Limit each section to 2000 chars to keep budget for all sections
            if len(body) > 2000:
                body = body[:2000] + " ..."
            content_lines.append(body)

        if content_lines:
            lines.append("")
            lines.append("=== DOCUMENT CONTENT ===")
            lines.extend(content_lines)

        return truncate_output("\n".join(lines), MAX_STRUCTURAL_INDEX_CHARS)
