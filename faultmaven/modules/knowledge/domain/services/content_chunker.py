"""Structure-aware content chunking for knowledge base documents.

Splits documents on markdown structural boundaries (headers, horizontal rules)
rather than fixed character counts. Preserves semantic units — a diagnostic step
stays with its conditional, a remediation procedure stays intact.

This is a pure domain component with no infrastructure dependencies.
"""

import re
from typing import List


class ContentChunker:
    """Split document content into semantically meaningful chunks.

    Variable chunk sizes are intentional — a 200-char config parameter
    description is one chunk, a 2500-char procedure section is one chunk.
    The embedding model handles this fine.
    """

    MAX_CHUNK_CHARS = 3000
    MIN_CHUNK_CHARS = 100

    def split(self, content: str) -> List[str]:
        """Split content into chunks using structure-aware boundaries.

        Strategy:
        1. Strip YAML frontmatter
        2. Split on markdown headers / horizontal rules
        3. Fallback: sentence-boundary splitting if no structure detected
        4. Post-process: merge tiny sections, split oversized ones
        """
        stripped = re.sub(
            r"^---\s*\n.*?\n---\s*\n", "", content, count=1, flags=re.DOTALL
        )
        stripped = stripped.strip()

        if not stripped:
            return [content.strip()] if content.strip() else []

        sections = self._split_by_structure(stripped)

        if len(sections) <= 1 and len(stripped) > self.MAX_CHUNK_CHARS:
            sections = self._split_by_sentences(stripped)

        return self._normalize(sections)

    @staticmethod
    def _split_by_structure(content: str) -> List[str]:
        """Split at markdown structural boundaries (headers, horizontal rules)."""
        header_pattern = re.compile(r"\n(?=#{1,4}\s+\S)")
        parts = header_pattern.split(content)
        sections = [p.strip() for p in parts if p.strip()]

        if len(sections) > 1:
            return sections

        hr_pattern = re.compile(r"\n\s*(?:---+|\*\*\*+|___+)\s*\n")
        parts = hr_pattern.split(content)
        sections = [p.strip() for p in parts if p.strip()]
        if len(sections) > 1:
            return sections

        return [content.strip()]

    @staticmethod
    def _split_by_sentences(content: str, max_size: int = 3000) -> List[str]:
        """Fallback: split at line boundaries with max size limit."""
        if len(content) <= max_size:
            return [content]

        chunks = []
        current: List[str] = []
        current_len = 0

        for line in content.split("\n"):
            line_len = len(line) + 1
            if current_len + line_len > max_size and current:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_len
            else:
                current.append(line)
                current_len += line_len

        if current:
            chunks.append("\n".join(current))

        return chunks

    def _normalize(self, sections: List[str]) -> List[str]:
        """Merge tiny sections and split oversized ones."""
        normalized: List[str] = []
        pending = ""

        for section in sections:
            if pending:
                combined = pending + "\n\n" + section
                if len(combined) <= self.MAX_CHUNK_CHARS:
                    pending = combined
                    continue
                else:
                    normalized.append(pending.strip())
                    pending = ""

            if len(section) < self.MIN_CHUNK_CHARS:
                pending = section
            elif len(section) > self.MAX_CHUNK_CHARS:
                sub_chunks = self._split_by_sentences(section, self.MAX_CHUNK_CHARS)
                normalized.extend(sub_chunks)
            else:
                normalized.append(section)

        if pending:
            if normalized:
                last = normalized[-1]
                if len(last) + len(pending) + 2 <= self.MAX_CHUNK_CHARS:
                    normalized[-1] = last + "\n\n" + pending
                else:
                    normalized.append(pending.strip())
            else:
                normalized.append(pending.strip())

        return [c for c in normalized if c.strip()]
