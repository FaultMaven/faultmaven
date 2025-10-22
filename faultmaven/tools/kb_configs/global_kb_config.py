"""
Global Knowledge Base Configuration

KB-specific configuration for system-wide documentation and best practices.
"""

from typing import Optional
from faultmaven.tools.kb_config import KBConfig


class GlobalKBConfig(KBConfig):
    """
    Configuration for Global Knowledge Base.

    Characteristics:
    - System-wide scope (no scoping parameter)
    - Educational focus: best practices, multiple approaches, gotchas
    - Long cache TTL (7 days - system KB changes rarely)
    - Single collection: global_kb
    """

    def get_collection_name(self, scope_id: Optional[str]) -> str:
        """Get global collection name (no scoping)"""
        return "global_kb"

    def format_chunk_metadata(self, metadata: dict, score: float) -> str:
        """Format with educational context: article IDs, titles, categories"""
        parts = [f"Score: {score:.2f}"]

        if 'kb_article_id' in metadata:
            parts.append(f"Article: {metadata['kb_article_id']}")
        if 'title' in metadata:
            parts.append(f"Title: {metadata['title']}")
        if 'category' in metadata:
            parts.append(f"Category: {metadata['category']}")

        return ', '.join(parts)

    def extract_source_name(self, metadata: dict) -> str:
        """Extract article ID or title as source"""
        if 'kb_article_id' in metadata:
            return metadata['kb_article_id']
        return metadata.get('title', 'Unknown article')

    def get_citation_format(self) -> str:
        """Cite with article IDs and titles"""
        return "article IDs and titles"

    def format_response(
        self,
        answer: str,
        sources: list,
        chunk_count: int,
        confidence: float
    ) -> str:
        """Format with KB article citations"""
        response = f"{answer}\n\n"

        if sources:
            response += f"📖 Knowledge Base: {', '.join(sources[:5])}"  # Show more for global
            if len(sources) > 5:
                response += f" (+{len(sources) - 5} more)"

        return response

    @property
    def requires_scope_id(self) -> bool:
        """No scoping required (global access)"""
        return False

    @property
    def cache_ttl(self) -> int:
        """7 days cache (system KB changes rarely)"""
        return 604800

    @property
    def system_prompt(self) -> str:
        """General best practices system prompt"""
        return """You are retrieving from the system-wide knowledge base.

Answer with general best practices:
- Provide industry-standard approaches and methodologies
- Include multiple options when applicable
- Reference official documentation and standards
- Cover common pitfalls and gotchas
- Explain the reasoning behind recommendations

Be comprehensive and educational. This is general troubleshooting guidance applicable across cases."""
