"""
Case Evidence Store Configuration

KB-specific configuration for case-scoped forensic evidence (logs, configs, metrics, code).
"""

from typing import Optional
from faultmaven.tools.kb_config import KBConfig


class CaseEvidenceConfig(KBConfig):
    """
    Configuration for Case Evidence Store.

    Characteristics:
    - Scoped to specific case (requires case_id)
    - Forensic focus: line numbers, timestamps, verbatim error messages
    - Short cache TTL (1 hour - case session duration)
    - Collection per case: case_{case_id}
    """

    def get_collection_name(self, scope_id: Optional[str]) -> str:
        """Get case-scoped collection name"""
        if not scope_id:
            raise ValueError("case_id required for case evidence queries")
        return f"case_{scope_id}"

    def format_chunk_metadata(self, metadata: dict, score: float) -> str:
        """Format with forensic precision: filenames, line numbers, timestamps"""
        parts = [f"Score: {score:.2f}"]

        if 'filename' in metadata:
            parts.append(f"Source: {metadata['filename']}")
        if 'line_number' in metadata:
            parts.append(f"Line: {metadata['line_number']}")
        if 'timestamp' in metadata:
            parts.append(f"Time: {metadata['timestamp']}")

        return ', '.join(parts)

    def extract_source_name(self, metadata: dict) -> str:
        """Extract filename as source"""
        return metadata.get('filename', metadata.get('source_id', 'Unknown'))

    def get_citation_format(self) -> str:
        """Cite with line numbers and timestamps"""
        return "line numbers and timestamps"

    def format_response(
        self,
        answer: str,
        sources: list,
        chunk_count: int,
        confidence: float
    ) -> str:
        """Format with file citations and relevance metrics"""
        response = f"{answer}\n\n"

        if sources:
            response += f"📎 Sources: {', '.join(sources[:3])}"
            if len(sources) > 3:
                response += f" (+{len(sources) - 3} more)"
            response += f" ({chunk_count} chunks, {confidence:.0%} relevance)"

        return response

    @property
    def requires_scope_id(self) -> bool:
        """Requires case_id"""
        return True

    @property
    def cache_ttl(self) -> int:
        """1 hour cache (case session duration)"""
        return 3600

    @property
    def system_prompt(self) -> str:
        """Forensic analysis system prompt"""
        return """You are analyzing uploaded case evidence (logs, configs, metrics, code).

Answer factually with forensic precision:
- Cite exact line numbers and timestamps when available
- Include error codes and messages verbatim
- Preserve chronological order for events
- Distinguish between ERROR, WARN, INFO severity levels
- Note file names and locations explicitly

Be precise and detailed. This is forensic evidence analysis for troubleshooting."""
