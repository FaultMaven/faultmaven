"""Web Search Tool for Investigation DA Loop

Provides web search capabilities within the investigation pipeline's
directed analysis tool loop. Searches trusted technical domains for
error messages, documentation, and solutions.

Provider abstraction supports Google Custom Search and Tavily,
auto-selected based on configured API keys.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)

# Trusted technical domains for domain-filtered search
TRUSTED_DOMAINS: List[str] = [
    "stackoverflow.com",
    "docs.microsoft.com",
    "learn.microsoft.com",
    "docs.aws.amazon.com",
    "kubernetes.io",
    "docs.docker.com",
    "github.com",
    "redis.io",
    "mongodb.com",
    "nginx.org",
    "apache.org",
    "python.org",
    "nodejs.org",
]

# Stage-aware query enrichment terms
_STAGE_ENRICHMENT = {
    "DIAGNOSIS": {
        "error": "root cause possible reasons diagnosis",
        "default": "troubleshoot diagnose analyze",
    },
    "MITIGATION": {
        "error": "temporary fix workaround mitigation",
        "default": "workaround temporary mitigation",
    },
    "TREATMENT": {
        "error": "fix solution resolve remediation",
        "default": "fix solution resolve remediation permanent",
    },
}


@dataclass
class SearchResult:
    """Single search result from a provider."""

    title: str
    url: str
    snippet: str


class SearchProvider(ABC):
    """Abstract search provider interface."""

    @abstractmethod
    async def search(self, query: str, max_results: int) -> List[SearchResult]:
        """Execute a search query and return results."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is configured and available."""


class GoogleCSEProvider(SearchProvider):
    """Google Custom Search Engine provider."""

    def __init__(
        self,
        api_key: str,
        engine_id: str,
        api_endpoint: str = "https://www.googleapis.com/customsearch/v1",
    ):
        self._api_key = api_key
        self._engine_id = engine_id
        self._api_endpoint = api_endpoint

    async def search(self, query: str, max_results: int) -> List[SearchResult]:
        params: Dict[str, str] = {
            "key": self._api_key,
            "cx": self._engine_id,
            "q": query,
            "num": str(min(max_results, 10)),  # Google CSE max is 10
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(self._api_endpoint, params=params)
            response.raise_for_status()
            data = response.json()

        results: List[SearchResult] = []
        for item in data.get("items", []):
            snippet = item.get("snippet", "No description available")
            snippet = snippet.replace("\n", " ").strip()
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            results.append(
                SearchResult(
                    title=item.get("title", "No title"),
                    url=item.get("link", ""),
                    snippet=snippet,
                )
            )
        return results

    def is_available(self) -> bool:
        return bool(self._api_key and self._engine_id)


class TavilyProvider(SearchProvider):
    """Tavily AI search provider."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    async def search(self, query: str, max_results: int) -> List[SearchResult]:
        from tavily import AsyncTavilyClient

        client = AsyncTavilyClient(api_key=self._api_key)
        response = await client.search(
            query=query,
            max_results=max_results,
            search_depth="basic",
            include_domains=TRUSTED_DOMAINS,
        )

        results: List[SearchResult] = []
        for item in response.get("results", []):
            snippet = item.get("content", "No description available")
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."
            results.append(
                SearchResult(
                    title=item.get("title", "No title"),
                    url=item.get("url", ""),
                    snippet=snippet,
                )
            )
        return results

    def is_available(self) -> bool:
        return bool(self._api_key)


class WebSearchTool(AgentTool):
    """Web search tool for the investigation DA loop.

    Searches trusted technical domains for error messages, documentation,
    and solutions. Supports Google CSE and Tavily providers, auto-selected
    based on configured API keys.
    """

    def __init__(
        self,
        settings: Optional[Any] = None,
        provider: Optional[SearchProvider] = None,
        max_results: int = 3,
    ):
        self._max_results = max_results
        self._provider: Optional[SearchProvider] = provider

        if self._provider is None and settings is not None:
            self._provider = self._auto_select_provider(settings)

    @staticmethod
    def _auto_select_provider(settings: Any) -> Optional[SearchProvider]:
        """Auto-select provider based on configured API keys.

        Priority: Tavily (preferred — cleaner content) > Google CSE.
        """
        # Check Tavily first
        tavily_key = settings.knowledge.tavily_api_key
        if tavily_key:
            key_value = (
                tavily_key.get_secret_value()
                if hasattr(tavily_key, "get_secret_value")
                else str(tavily_key)
            )
            if key_value:
                return TavilyProvider(api_key=key_value)

        # Fall back to Google CSE
        google_key = settings.tools.web_search_api_key
        if google_key:
            key_value = (
                google_key.get_secret_value()
                if hasattr(google_key, "get_secret_value")
                else str(google_key)
            )
            engine_id = settings.tools.web_search_engine_id
            if key_value and engine_id:
                return GoogleCSEProvider(
                    api_key=key_value,
                    engine_id=engine_id,
                    api_endpoint=settings.tools.web_search_api_endpoint,
                )

        return None

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search trusted technical websites (Stack Overflow, official docs, GitHub) "
            "for error messages, solutions, and documentation. Use as a supplementary "
            "source when case evidence and knowledge base do not contain the answer. "
            "Provide a focused technical query."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Technical search query. Be specific — include error messages, "
                        "technology names, and version numbers when available."
                    ),
                },
            },
            "required": ["query"],
        }

    def is_available(self) -> bool:
        """Check if a search provider is configured and available."""
        return self._provider is not None and self._provider.is_available()

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute web search within the DA loop."""
        query = params.get("query", "").strip()
        if not query:
            return ToolResult(success=False, data=None, error="No query provided")

        if not self._provider or not self._provider.is_available():
            return ToolResult(
                success=False,
                data=None,
                error="Web search is not available: no search provider configured.",
            )

        try:
            # Enrich query with investigation stage context
            enhanced_query = self._enhance_query_with_context(query, context)

            # For Google CSE, append domain filter to query
            # (Tavily handles this via include_domains parameter)
            search_query = enhanced_query
            if isinstance(self._provider, GoogleCSEProvider):
                site_query = " OR ".join(f"site:{domain}" for domain in TRUSTED_DOMAINS)
                search_query = f"{enhanced_query} ({site_query})"

            results = await self._provider.search(search_query, self._max_results)
            return ToolResult(
                success=True,
                data=self._format_results(results, query),
                error=None,
            )

        except Exception as e:
            # Never interpolate the raw exception. httpx renders
            # HTTPStatusError as "... for url '<full url>'", and the Google CSE
            # URL carries the API key and the investigation's search text as
            # query parameters -- so the 400 this path exists to report was
            # writing both to an ERROR record.
            detail = type(e).__name__
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None:
                detail = f"{detail} (HTTP {status})"
            logger.error("Web search failed: %s", detail)
            return ToolResult(
                success=False,
                data=None,
                error="Web search encountered an error. Try a different query.",
            )

    def _enhance_query_with_context(self, query: str, context: ToolContext) -> str:
        """Enrich query with investigation stage context."""
        stage = context.metadata.get("stage", "")
        if not stage:
            return query

        enrichment = _STAGE_ENRICHMENT.get(stage, {})
        has_error_terms = any(
            term in query.lower() for term in ("error", "exception", "fail", "crash")
        )
        suffix = enrichment.get("error" if has_error_terms else "default", "")

        if suffix:
            return f"{query} {suffix}"
        return query

    @staticmethod
    def _format_results(results: List[SearchResult], original_query: str) -> str:
        """Format search results into a readable string for the LLM."""
        if not results:
            return f"No relevant results found for '{original_query}'."

        lines = [f"Web search results for '{original_query}':"]
        for i, result in enumerate(results, 1):
            lines.append(
                f"\n{i}. **{result.title}**\n"
                f"   URL: {result.url}\n"
                f"   Summary: {result.snippet}"
            )

        lines.append(
            "\n---\n"
            "Note: These are external web results. "
            "Verify solutions against the case evidence before recommending."
        )
        return "\n".join(lines)
