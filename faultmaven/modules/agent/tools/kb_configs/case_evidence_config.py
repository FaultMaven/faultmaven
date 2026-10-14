"""
Case Evidence Store Configuration

KB-specific configuration for case-scoped forensic evidence (logs, configs, metrics, code).
"""

from typing import Optional

from faultmaven.config.settings import get_settings
from faultmaven.modules.agent.tools.kb_config import KBConfig


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

        if "filename" in metadata:
            parts.append(f"Source: {metadata['filename']}")
        if "line_number" in metadata:
            parts.append(f"Line: {metadata['line_number']}")
        if "timestamp" in metadata:
            parts.append(f"Time: {metadata['timestamp']}")

        return ", ".join(parts)

    def extract_source_name(self, metadata: dict) -> str:
        """Extract filename as source"""
        return metadata.get("filename", metadata.get("source_id", "Unknown"))

    def get_citation_format(self) -> str:
        """Cite with line numbers and timestamps"""
        return "line numbers and timestamps"

    def format_response(
        self, answer: str, sources: list, chunk_count: int, confidence: float
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
    def empty_result_message(self) -> str:
        """The empty result for a caller that offers the model no other tool.

        Names no tool: see ``empty_result_for``.
        """
        return self.empty_result_for(search_file_offered=False)

    def empty_result_for(
        self, *, search_file_offered: bool, readable_files: str = ""
    ) -> str:
        """What a search that ran and found nothing tells the model (#1977).

        An empty result means one thing: no file of this case has been indexed
        for semantic search. It does NOT mean the case has no uploads: a file
        under ``vectorization_min_size_bytes`` is never indexed (``vectorize_file``
        refuses it, and the proactive and reactive gates skip it), and a larger
        one is indexed only once something vectorizes it. The old text blamed
        "nothing uploaded yet, or still being indexed", false for a case whose
        files are all small, and the model could repeat it to the user or stop
        looking. The size is read from settings, not written here.

        Names a tool only the turn offers (#1939): ``search_file`` when
        ``search_file_offered``, with ``readable_files`` (the case's files that
        tool reads, ``format_searchable_alternatives``'s list, which starts with
        a space), and no tool otherwise. Nothing here claims the case's files
        are or are not searchable: it says only what the index holds. Reached
        only when the search ran: a failed one raises and never gets here
        (#943).
        """
        min_size = get_settings().agent.vectorization_min_size_bytes
        text = (
            "No file of this case has been indexed for semantic search, so "
            "there was nothing to search. Semantic search covers only files of "
            f"at least {min_size:,} bytes that have been indexed; a smaller "
            "file is never indexed."
        )
        if not search_file_offered:
            return (
                text + " This result does not show whether the case has "
                "uploaded files."
            )
        if not readable_files:
            return text + " This case has no uploaded file to read."
        return (
            text + " This does not mean there is nothing to read: search_file "
            "reads this case's uploaded files, by id." + readable_files
        )

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
