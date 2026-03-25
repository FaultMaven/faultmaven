"""
Unified Knowledge Base Configuration

KB-specific configuration for the unified KB tool that searches all scopes.
Combines the best of global (educational) and user (procedural) approaches.
"""

from typing import Optional

from faultmaven.modules.agent.tools.kb_config import KBConfig


class UnifiedKBConfig(KBConfig):
    """
    Configuration for the unified Knowledge Base search.

    Searches faultmaven_kb collection with metadata-based scope filtering.
    Results may come from global, personal, or team runbooks.
    """

    def get_collection_name(self, scope_id: Optional[str]) -> str:
        """Single KB collection for all scopes."""
        return "faultmaven_kb"

    def format_chunk_metadata(self, metadata: dict, score: float) -> str:
        """Format with source scope and document info."""
        parts = [f"Score: {score:.2f}"]

        scope = metadata.get("scope", "global")
        parts.append(f"Scope: {scope}")

        if "title" in metadata:
            parts.append(f"Title: {metadata['title']}")
        elif "document_title" in metadata:
            parts.append(f"Doc: {metadata['document_title']}")
        if "category" in metadata:
            parts.append(f"Category: {metadata['category']}")

        return ", ".join(parts)

    def extract_source_name(self, metadata: dict) -> str:
        """Extract document title or article ID as source."""
        if "title" in metadata:
            return metadata["title"]
        if "document_title" in metadata:
            return metadata["document_title"]
        if "kb_article_id" in metadata:
            return metadata["kb_article_id"]
        return "Unknown document"

    def get_citation_format(self) -> str:
        return "document titles"

    def format_response(
        self, answer: str, sources: list, chunk_count: int, confidence: float
    ) -> str:
        """Format with unified citations."""
        response = f"{answer}\n\n"

        if sources:
            response += f"Sources: {', '.join(sources[:5])}"
            if len(sources) > 5:
                response += f" (+{len(sources) - 5} more)"

        return response

    @property
    def requires_scope_id(self) -> bool:
        """No explicit scope_id needed — filtering is automatic."""
        return False

    @property
    def cache_ttl(self) -> int:
        """24-hour cache (KB content is relatively stable)."""
        return 86400

    @property
    def system_prompt(self) -> str:
        return """You are retrieving from the knowledge base, which contains:
- System-wide best practices and documentation
- The user's personal runbooks and procedures
- Team-shared troubleshooting guides

Answer with clarity and precision:
- Provide step-by-step instructions when procedures are available
- Include best practices and common pitfalls
- Reference documents by title for attribution
- Use the appropriate level of detail based on the content source

Be helpful and actionable. Combine general guidance with specific procedures when both are available."""
