"""
Web Search Tool for FaultMaven Agent.

This tool provides web search capabilities with domain filtering and
safety controls. It should be used as a last resort when the knowledge
base doesn't have relevant information.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from langchain.tools import BaseTool


class WebSearchTool(BaseTool):
    """
    Web search tool with domain filtering for safe and relevant results.

    This tool searches the public internet for general error messages,
    open-source documentation, or solutions to common software problems
    ONLY if the KnowledgeBaseTool returns no results.
    """

    name: str = "web_search"
    description: str = (
        "Use this tool as a last resort to search the public internet for "
        "general error messages, open-source documentation, or solutions "
        "to common software problems ONLY if the KnowledgeBaseTool "
        "returns no results. Searches are limited to trusted domains "
        "for safety and relevance."
    )

    # Define as private attributes to avoid Pydantic field conflicts
    _api_key: str = ""
    _api_endpoint: str = ""
    _trusted_domains: List[str] = []
    _max_results: int = 3
    _logger: Optional[logging.Logger] = None

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_endpoint: Optional[str] = None,
        trusted_domains: Optional[List[str]] = None,
        max_results: int = 3,
    ):
        """
        Initialize the WebSearchTool.

        Args:
            api_key: API key for search service
            api_endpoint: API endpoint for search service
            trusted_domains: List of trusted domains to search
            max_results: Maximum number of results to return
        """
        super().__init__()

        # Initialize from environment variables if not provided
        self._api_key = api_key or os.getenv("WEB_SEARCH_API_KEY") or ""
        self._api_endpoint = api_endpoint or os.getenv(
            "WEB_SEARCH_API_ENDPOINT"
        ) or "https://www.googleapis.com/customsearch/v1"

        # Default trusted domains for technical documentation
        self._trusted_domains = trusted_domains or [
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

        self._max_results = max_results
        self._logger = logging.getLogger(__name__)

        # Check if API key is available
        if not self._api_key:
            if self._logger:
                self._logger.warning(
                    "No web search API key provided. Web search will be disabled."
                )

    def _run(self, query: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Synchronous run method (not implemented for async tool)."""
        raise NotImplementedError("Use async method _arun instead")

    async def _arun(self, query: str, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Asynchronously perform a web search and return formatted results.

        Args:
            query: Search query string
            context: Optional context for enhanced search

        Returns:
            Formatted search results or error message
        """
        if self._logger:
            self._logger.info(f"Executing web search for: {query}")

        # Check if API key is available
        if not self._api_key:
            return "Web search is not available: No API key configured."

        try:
            # Enhance query with context if available
            enhanced_query = self._enhance_query_with_context(query, context)

            # Construct the search query with trusted domains
            site_query = " OR ".join(
                [f"site:{domain}" for domain in self._trusted_domains]
            )
            full_query = f"{enhanced_query} ({site_query})"

            # Make the API call
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await self._make_search_request(client, full_query)
                results = response.json()

            # Format and return results
            return self._format_search_results(results, query)

        except (httpx.RequestError, httpx.HTTPStatusError, httpx.TimeoutException):
            return "Web search encountered an unexpected error"
        except Exception as e:
            if self._logger:
                self._logger.error(f"Web search failed: {e}")
            return "Web search encountered an unexpected error"

    def _enhance_query_with_context(
        self, query: str, context: Optional[Dict[str, Any]]
    ) -> str:
        """
        Enhance the search query with context information.

        Args:
            query: Original search query
            context: Context information from the agent

        Returns:
            Enhanced search query
        """
        if not context:
            return query

        # Add phase-specific terms
        phase = context.get("phase", "")
        if "error" in query.lower() or "exception" in query.lower():
            if phase == "define_blast_radius":
                query += " impact scope affected systems"
            elif phase == "establish_timeline":
                query += " when started occurred timeline"
            elif phase == "formulate_hypothesis":
                query += " root cause possible reasons"
            elif phase == "validate_hypothesis":
                query += " test validate confirm"
            elif phase == "propose_solution":
                query += " fix solution resolve"

        # Add environment context if available
        if "environment" in context:
            env = context["environment"]
            if env in ["production", "staging", "development"]:
                query += f" {env} environment"

        return query

    async def _make_search_request(
        self, client: httpx.AsyncClient, query: str
    ) -> Optional[httpx.Response]:
        """
        Make the actual search API request.

        Args:
            client: HTTP client
            query: Search query

        Returns:
            Response object or None if failed
        
        Raises:
            httpx.RequestError: Network error during request
            httpx.HTTPStatusError: HTTP error during request
            Exception: Other unexpected errors
        """
        # Using Google Custom Search API as default
        search_engine_id = os.getenv("WEB_SEARCH_ENGINE_ID", "")

        params: Dict[str, str] = {
            "key": self._api_key,
            "cx": search_engine_id,
            "q": query,
            "num": str(self._max_results),
        }

        response = await client.get(self._api_endpoint, params=params)
        response.raise_for_status()
        return response

    def _format_search_results(
        self, results: Dict[str, Any], original_query: str
    ) -> str:
        """
        Format search results into a clean, readable string.

        Args:
            results: Raw search results from API
            original_query: Original search query

        Returns:
            Formatted results string
        """
        if not results or "items" not in results:
            return f"No relevant results found on the web for '{original_query}'."

        items = results.get("items", [])
        if not items:
            return f"No relevant results found on the web for '{original_query}'."

        formatted_results = [f"Web search results for '{original_query}':"]

        for i, item in enumerate(items[: self._max_results], 1):
            title = item.get("title", "No title")
            link = item.get("link", "No link")
            snippet = item.get("snippet", "No description available")

            # Clean up the snippet
            snippet = snippet.replace("\n", " ").strip()
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."

            formatted_results.append(
                f"\n{i}. **{title}**\n" f"   URL: {link}\n" f"   Summary: {snippet}"
            )

        # Add disclaimer
        formatted_results.append(
            "\n---\n"
            "⚠️ **Note**: These are external web results. "
            "Always verify solutions in a test environment before applying to production."
        )

        return "\n".join(formatted_results)

    def is_available(self) -> bool:
        """
        Check if the web search tool is properly configured and available.

        Returns:
            True if the tool can be used, False otherwise
        """
        return bool(self._api_key and self._api_endpoint)

    def get_search_domains(self) -> List[str]:
        """
        Get the list of trusted domains being searched.

        Returns:
            List of trusted domain names
        """
        return self._trusted_domains.copy()

    def add_trusted_domain(self, domain: str) -> None:
        """
        Add a new trusted domain to the search list.

        Args:
            domain: Domain name to add (e.g., 'example.com')
        """
        if domain not in self._trusted_domains:
            self._trusted_domains.append(domain)
            if self._logger:
                self._logger.info(f"Added trusted domain: {domain}")

    def remove_trusted_domain(self, domain: str) -> None:
        """
        Remove a domain from the trusted search list.

        Args:
            domain: Domain name to remove
        """
        if domain in self._trusted_domains:
            self._trusted_domains.remove(domain)
            if self._logger:
                self._logger.info(f"Removed trusted domain: {domain}")
