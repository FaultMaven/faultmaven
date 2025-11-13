"""
Documentation Structure Extraction for DOCUMENTATION data type

Extracts structured information from runbooks, wikis, and technical documentation.
Focuses on extracting procedures, troubleshooting steps, and configuration references.
No LLM calls required - pure markdown/text parsing.
"""

import re
from typing import List, Dict, Optional, Tuple


class DocumentationExtractor:
    """Documentation structure extraction for runbooks and wikis (0 LLM calls)"""

    @property
    def strategy_name(self) -> str:
        return "documentation_structure"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> str:
        """
        Documentation Structure Extraction algorithm:
        1. Detect documentation format (Markdown, reStructuredText, plain text)
        2. Extract document title
        3. Extract section headings and hierarchy
        4. Identify key sections (troubleshooting, procedures, config)
        5. Extract code blocks and commands
        6. Generate structured summary
        """
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

        # Generate summary
        return self._generate_summary(
            title,
            sections,
            code_blocks,
            troubleshooting_sections,
            procedure_sections,
            config_sections
        )

    def _is_markdown(self, content: str) -> bool:
        """Detect if content is Markdown format"""
        markdown_indicators = [
            r'^#{1,6}\s+\w+',  # Headers
            r'```[\w]*\n',  # Code blocks
            r'^\*\*\w+\*\*',  # Bold
            r'^\[.+\]\(.+\)',  # Links
        ]

        return sum(
            1 for pattern in markdown_indicators
            if re.search(pattern, content, re.MULTILINE)
        ) >= 2

    def _extract_title(self, content: str, is_markdown: bool) -> str:
        """Extract document title"""
        lines = content.split('\n')

        if is_markdown:
            # Look for # Title (H1)
            for line in lines[:10]:  # Check first 10 lines
                if line.startswith('# '):
                    return line[2:].strip()

        # Fallback: first non-empty line
        for line in lines[:5]:
            if line.strip():
                return line.strip()[:100]  # Limit length

        return "Untitled Document"

    def _extract_sections(self, content: str, is_markdown: bool) -> List[Dict]:
        """Extract section headings and their content"""
        sections = []

        if is_markdown:
            # Match Markdown headers: # H1, ## H2, ### H3, etc.
            header_pattern = r'^(#{1,6})\s+(.+)$'

            lines = content.split('\n')
            current_section = None
            section_content = []

            for i, line in enumerate(lines):
                match = re.match(header_pattern, line)

                if match:
                    # Save previous section
                    if current_section:
                        current_section['content'] = '\n'.join(section_content).strip()
                        sections.append(current_section)

                    # Start new section
                    level = len(match.group(1))
                    title = match.group(2).strip()

                    current_section = {
                        'level': level,
                        'title': title,
                        'line_num': i + 1,
                        'content': ''
                    }
                    section_content = []
                elif current_section:
                    section_content.append(line)

            # Save last section
            if current_section:
                current_section['content'] = '\n'.join(section_content).strip()
                sections.append(current_section)

        else:
            # Plain text: look for underlined headers
            lines = content.split('\n')
            for i in range(len(lines) - 1):
                # Check if next line is all ==== or ----
                if re.match(r'^[=\-]{3,}$', lines[i + 1].strip()):
                    sections.append({
                        'level': 1 if '=' in lines[i + 1] else 2,
                        'title': lines[i].strip(),
                        'line_num': i + 1,
                        'content': ''
                    })

        return sections

    def _extract_code_blocks(self, content: str, is_markdown: bool) -> List[Dict]:
        """Extract code blocks and inline commands"""
        code_blocks = []

        if is_markdown:
            # Extract fenced code blocks: ```language\ncode\n```
            pattern = r'```([\w]*)\n([\s\S]*?)```'

            for match in re.finditer(pattern, content):
                language = match.group(1) or 'text'
                code = match.group(2).strip()

                code_blocks.append({
                    'language': language,
                    'code': code,
                    'type': 'fenced'
                })

        # Extract inline code commands (backtick-wrapped or indented)
        inline_pattern = r'`([^`]+)`'
        for match in re.finditer(inline_pattern, content):
            command = match.group(1).strip()

            # Only include if it looks like a command
            if self._looks_like_command(command):
                code_blocks.append({
                    'language': 'shell',
                    'code': command,
                    'type': 'inline'
                })

        return code_blocks

    def _looks_like_command(self, text: str) -> bool:
        """Check if text looks like a shell command"""
        command_indicators = [
            'kubectl', 'docker', 'systemctl', 'journalctl', 'tail', 'grep', 'curl', 'wget',
            'ssh', 'scp', 'ps', 'top', 'netstat', 'ifconfig', 'ping', 'traceroute',
            'git', 'npm', 'pip', 'mvn', 'gradle', 'cargo', 'go'
        ]

        return any(text.lower().startswith(cmd) for cmd in command_indicators)

    def _find_troubleshooting_sections(self, sections: List[Dict]) -> List[Dict]:
        """Identify sections related to troubleshooting"""
        keywords = ['troubleshoot', 'debug', 'error', 'problem', 'issue', 'diagnos', 'fix', 'resolve']

        return [
            section for section in sections
            if any(keyword in section['title'].lower() for keyword in keywords)
        ]

    def _find_procedure_sections(self, sections: List[Dict]) -> List[Dict]:
        """Identify sections containing procedures"""
        keywords = ['how to', 'procedure', 'step', 'install', 'setup', 'configure', 'deploy', 'guide']

        return [
            section for section in sections
            if any(keyword in section['title'].lower() for keyword in keywords)
        ]

    def _find_config_sections(self, sections: List[Dict]) -> List[Dict]:
        """Identify sections related to configuration"""
        keywords = ['config', 'setting', 'parameter', 'environment', 'variable', 'option']

        return [
            section for section in sections
            if any(keyword in section['title'].lower() for keyword in keywords)
        ]

    def _generate_summary(
        self,
        title: str,
        sections: List[Dict],
        code_blocks: List[Dict],
        troubleshooting_sections: List[Dict],
        procedure_sections: List[Dict],
        config_sections: List[Dict]
    ) -> str:
        """Generate structured documentation summary"""
        lines = [
            f"Documentation: {title}",
            "",
            f"📄 Document Overview:",
            f"  - Total sections: {len(sections)}",
            f"  - Code blocks: {len(code_blocks)}",
            ""
        ]

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
            shell_commands = [cb for cb in code_blocks if cb['language'] in ['shell', 'bash', 'sh', 'text']]
            if shell_commands:
                lines.append("💻 Key Commands:")
                for cmd_block in shell_commands[:10]:  # First 10 commands
                    code = cmd_block['code']
                    # Truncate long commands
                    if len(code) > 80:
                        code = code[:77] + '...'
                    lines.append(f"  $ {code}")
                lines.append("")

        # Full table of contents
        if sections:
            lines.append("📑 Table of Contents:")
            for section in sections:
                indent = "  " * (section['level'] - 1)
                lines.append(f"{indent}- {section['title']}")

        return "\n".join(lines)
