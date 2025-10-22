"""
User Knowledge Base Configuration

KB-specific configuration for user-scoped personal runbooks and procedures.
"""

from typing import Optional
from faultmaven.tools.kb_config import KBConfig


class UserKBConfig(KBConfig):
    """
    Configuration for User Knowledge Base.

    Characteristics:
    - Scoped to specific user (requires user_id)
    - Procedural focus: step-by-step instructions, user's documented practices
    - Medium cache TTL (24 hours - runbooks change infrequently)
    - Collection per user: user_{user_id}_kb
    """

    def get_collection_name(self, scope_id: Optional[str]) -> str:
        """Get user-scoped collection name"""
        if not scope_id:
            raise ValueError("user_id required for user KB queries")
        return f"user_{scope_id}_kb"

    def format_chunk_metadata(self, metadata: dict, score: float) -> str:
        """Format with procedural context: document titles, categories"""
        parts = [f"Score: {score:.2f}"]

        if 'document_title' in metadata:
            parts.append(f"Doc: {metadata['document_title']}")
        if 'category' in metadata:
            parts.append(f"Category: {metadata['category']}")
        if 'tags' in metadata and metadata['tags']:
            # tags is a list
            tags_str = ', '.join(metadata['tags'][:2])  # Show first 2 tags
            parts.append(f"Tags: {tags_str}")

        return ', '.join(parts)

    def extract_source_name(self, metadata: dict) -> str:
        """Extract document title as source"""
        return metadata.get('document_title', 'Unknown document')

    def get_citation_format(self) -> str:
        """Cite with document titles and sections"""
        return "document titles and sections"

    def format_response(
        self,
        answer: str,
        sources: list,
        chunk_count: int,
        confidence: float
    ) -> str:
        """Format with runbook citations"""
        response = f"{answer}\n\n"

        if sources:
            response += f"📚 From your runbooks: {', '.join(sources)}"

        return response

    @property
    def requires_scope_id(self) -> bool:
        """Requires user_id"""
        return True

    @property
    def cache_ttl(self) -> int:
        """24 hours cache (runbooks stable)"""
        return 86400

    @property
    def system_prompt(self) -> str:
        """Procedural knowledge system prompt"""
        return """You are retrieving from the user's personal runbooks and procedures.

Answer with procedural clarity:
- Provide step-by-step instructions when procedures are described
- Reference the user's documented procedures by title
- Use the user's terminology and naming conventions
- Include decision points and troubleshooting flows from their docs
- Preserve the user's preferred formats and structures

Be helpful and procedural. This is the user's documented knowledge and best practices."""
