"""knowledge_base.py

Purpose: Enhanced RAG lookup tool for knowledge base with contextual search

Requirements:
--------------------------------------------------------------------------------
• Create BaseTool subclass with context-aware search
• Query ChromaDB vector store with metadata filtering
• Return document chunks with metadata and expanded queries

Key Components:
--------------------------------------------------------------------------------
  class KnowledgeBaseTool(BaseTool): ...
  async def _arun(query: str, context: dict)

Technology Stack:
--------------------------------------------------------------------------------
ChromaDB, LangChain Tools

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import json
import logging
from typing import Any, Dict, List, Optional

from langchain.tools import BaseTool as LangChainBaseTool
from langchain_core.tools import Tool
from pydantic import PrivateAttr

from faultmaven.core.knowledge.ingestion import KnowledgeIngester
from faultmaven.models.interfaces import BaseTool as IBaseTool, ToolResult


class KnowledgeBaseTool(LangChainBaseTool, IBaseTool):
    """Enhanced RAG tool for querying the knowledge base with contextual search"""

    name: str = "knowledge_base_search"
    description: str = """
    Search the knowledge base for relevant troubleshooting information, 
    documentation, and guides. Use this tool when you need to find 
    specific information about errors, solutions, or procedures.
    
    Input should be a search query describing what you're looking for.
    Context can be provided as a dictionary to enhance search specificity.
    """
    _logger: logging.Logger = PrivateAttr()
    _knowledge_ingester: KnowledgeIngester = PrivateAttr()

    def __init__(self, knowledge_ingester: KnowledgeIngester):
        super().__init__()
        self._logger = logging.getLogger(__name__)
        self._knowledge_ingester = knowledge_ingester

    async def _arun(self, query: str, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Asynchronously search the knowledge base with contextual enhancement

        Args:
            query: Search query
            context: Optional context to enhance search specificity

        Returns:
            Formatted search results with source citations
        """
        try:
            self._logger.info(f"Searching knowledge base for: {query}")
            if not query or not query.strip():
                return "No query provided."

            # Expand query using context
            expanded_query = self._expand_query_with_context(query, context or {})

            # Extract metadata filters from context
            metadata_filters = self._extract_metadata_filters(context or {})

            # Perform enhanced search
            results = await self._knowledge_ingester.search(
                query=expanded_query, n_results=5, filter_metadata=metadata_filters
            )

            if not results:
                return "No relevant information found in the knowledge base."

            formatted_results = self._format_results(
                results, expanded_query, metadata_filters
            )
            return formatted_results

        except Exception as e:
            self._logger.error(f"Knowledge base search failed: {e}")
            return f"Error searching knowledge base: {str(e)}"

    def _expand_query_with_context(self, query: str, context: Dict[str, Any]) -> str:
        """
        Expand the search query using contextual information

        Args:
            query: Original search query
            context: Context dictionary containing relevant information

        Returns:
            Expanded query string
        """
        # Start with the original query
        expanded_parts = [query]

        # Add service names if available
        if context.get("service_name"):
            expanded_parts.append(f"service:{context['service_name']}")

        # Add error codes or types
        if context.get("error_code"):
            expanded_parts.append(f"error:{context['error_code']}")

        if context.get("error_type"):
            expanded_parts.append(f"error_type:{context['error_type']}")

        # Add environment information
        if context.get("environment"):
            expanded_parts.append(f"environment:{context['environment']}")

        # Add technology stack information
        if context.get("technology"):
            expanded_parts.append(f"technology:{context['technology']}")

        # Add phase information for targeted search
        if context.get("phase"):
            phase_keywords = {
                "define_blast_radius": "impact scope assessment",
                "establish_timeline": "timeline changes deployment",
                "formulate_hypothesis": "root cause analysis",
                "validate_hypothesis": "testing validation",
                "propose_solution": "solution fix resolution",
            }
            phase_keyword = phase_keywords.get(context["phase"], "")
            if phase_keyword:
                expanded_parts.append(phase_keyword)

        # Add recent findings or symptoms
        if context.get("symptoms"):
            symptoms = context["symptoms"]
            if isinstance(symptoms, list):
                expanded_parts.extend(symptoms)
            else:
                expanded_parts.append(str(symptoms))

        # Add component or system information
        if context.get("component"):
            expanded_parts.append(f"component:{context['component']}")

        expanded_query = " ".join(expanded_parts)

        if expanded_query != query:
            self._logger.info(f"Expanded query from '{query}' to '{expanded_query}'")

        return expanded_query

    def _extract_metadata_filters(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract metadata filters from context for ChromaDB filtering

        Args:
            context: Context dictionary

        Returns:
            Dictionary of metadata filters
        """
        filters = {}

        # Service name filter
        if context.get("service_name"):
            filters["service_name"] = context["service_name"]

        # Document type filter based on phase
        if context.get("phase"):
            phase_to_doc_types = {
                "define_blast_radius": ["impact_assessment", "system_overview"],
                "establish_timeline": ["deployment_guide", "change_log"],
                "formulate_hypothesis": ["troubleshooting_guide", "error_catalog"],
                "validate_hypothesis": ["testing_guide", "diagnostic_procedures"],
                "propose_solution": ["solution_guide", "fix_procedures"],
            }
            doc_types = phase_to_doc_types.get(context["phase"], [])
            if doc_types:
                filters["document_type"] = doc_types

        # Technology filter
        if context.get("technology"):
            filters["technology"] = context["technology"]

        # Environment filter
        if context.get("environment"):
            filters["environment"] = context["environment"]

        # Severity filter
        if context.get("severity"):
            filters["severity"] = context["severity"]

        # Tag filters
        if context.get("tags"):
            tags = context["tags"]
            if isinstance(tags, list):
                filters["tags"] = tags
            else:
                filters["tags"] = [tags]

        if filters:
            self._logger.info(f"Applying metadata filters: {filters}")

        return filters

    def _run(self, query: str, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Synchronous wrapper for the async search

        Args:
            query: Search query
            context: Optional context dictionary

        Returns:
            Search results
        """
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(self._arun(query, context))
        except RuntimeError:
            # If no event loop is running, create a new one
            return asyncio.run(self._arun(query, context))

    def _format_results(
        self,
        results: List[Dict[str, Any]],
        expanded_query: str,
        metadata_filters: Dict[str, Any],
    ) -> str:
        """
        Format search results with source citations and context information

        Args:
            results: List of search results from ChromaDB
            expanded_query: The expanded search query used
            metadata_filters: Metadata filters applied

        Returns:
            Formatted string with results and citations
        """
        if not results:
            return "No results found."

        formatted_parts = ["Knowledge Base Search Results:"]

        # Add search context information
        if expanded_query:
            formatted_parts.append(f"**Search Query:** {expanded_query}")

        if metadata_filters:
            filter_desc = ", ".join([f"{k}: {v}" for k, v in metadata_filters.items()])
            formatted_parts.append(f"**Applied Filters:** {filter_desc}")

        formatted_parts.append("")  # Empty line

        for i, result in enumerate(results, 1):
            document = result["document"]
            metadata = result["metadata"]
            relevance_score = result.get("relevance_score", 0.0)

            # Format the result
            formatted_parts.append(f"\n{i}. **Relevance: {relevance_score:.2f}**")

            # Source information
            source = metadata.get("source", metadata.get("title", "Unknown"))
            formatted_parts.append(f"**Source:** {source}")

            # Document type
            if metadata.get("document_type"):
                formatted_parts.append(f"**Type:** {metadata['document_type']}")

            # Service/Component information
            if metadata.get("service_name"):
                formatted_parts.append(f"**Service:** {metadata['service_name']}")

            if metadata.get("component"):
                formatted_parts.append(f"**Component:** {metadata['component']}")

            # Environment information
            if metadata.get("environment"):
                formatted_parts.append(f"**Environment:** {metadata['environment']}")

            # Technology information
            if metadata.get("technology"):
                formatted_parts.append(f"**Technology:** {metadata['technology']}")

            # Tags
            if metadata.get("tags"):
                tags = metadata["tags"]
                if isinstance(tags, str):
                    tag_list = tags.split(",")
                else:
                    tag_list = tags

                if tag_list and tag_list[0]:  # Check if tags are not empty
                    formatted_parts.append(f"**Tags:** {', '.join(tag_list)}")

            # Source URL
            if metadata.get("source_url"):
                formatted_parts.append(f"**URL:** {metadata['source_url']}")

            # Add the document content (truncated if too long)
            content = document.strip()
            if len(content) > 500:
                content = content[:500] + "..."

            formatted_parts.append(f"**Content:** {content}")

            # Add chunk information
            chunk_index = metadata.get("chunk_index", 0) + 1
            total_chunks = metadata.get("total_chunks", 1)
            chunk_info = f"(Chunk {chunk_index} of {total_chunks})"
            formatted_parts.append(f"**Location:** {chunk_info}")

            # Add confidence indicator
            if relevance_score > 0.8:
                formatted_parts.append("**Confidence:** High")
            elif relevance_score > 0.6:
                formatted_parts.append("**Confidence:** Medium")
            else:
                formatted_parts.append("**Confidence:** Low")

        return "\n".join(formatted_parts)

    def search(self, query: str, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Public method for direct search access

        Args:
            query: Search query
            context: Optional context dictionary

        Returns:
            Search results
        """
        return self._run(query, context)

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """
        Execute the knowledge base search tool using our interface.
        
        Args:
            params: Parameters dictionary containing 'query' and optional 'context'
            
        Returns:
            ToolResult with success/data/error
        """
        try:
            query = params.get('query', '')
            context = params.get('context')
            
            if not query or not query.strip():
                return ToolResult(
                    success=False,
                    data=None,
                    error="No query provided"
                )
            
            # Call existing LangChain method
            result = await self._arun(query, context)
            
            return ToolResult(
                success=True,
                data=result,
                error=None
            )
        except Exception as e:
            self._logger.error(f"Knowledge base search execution failed: {e}")
            return ToolResult(
                success=False,
                data=None,
                error=str(e)
            )
    
    def get_schema(self) -> Dict[str, Any]:
        """
        Get the tool schema for our interface compliance.
        
        Returns:
            Tool schema dictionary
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for the knowledge base",
                    },
                    "context": {
                        "type": "object",
                        "description": "Optional context to enhance search specificity",
                        "properties": {
                            "service_name": {"type": "string"},
                            "error_code": {"type": "string"},
                            "error_type": {"type": "string"},
                            "environment": {"type": "string"},
                            "technology": {"type": "string"},
                            "phase": {"type": "string"},
                            "symptoms": {"type": "array", "items": {"type": "string"}},
                            "component": {"type": "string"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "required": ["query"],
            },
        }
    
    def get_tool_schema(self) -> Dict[str, Any]:
        """
        Get the tool schema for LangChain integration (legacy compatibility).

        Returns:
            Tool schema dictionary
        """
        schema = self.get_schema()
        # Convert our schema format to LangChain format
        return {
            "name": schema["name"],
            "description": schema["description"],
            "args_schema": schema["parameters"],
        }


def create_knowledge_base_tool(knowledge_ingester: KnowledgeIngester) -> Tool:
    """
    Factory function to create a LangChain Tool from KnowledgeBaseTool

    Args:
        knowledge_ingester: KnowledgeIngester instance

    Returns:
        LangChain Tool instance
    """
    kb_tool = KnowledgeBaseTool(knowledge_ingester)

    return Tool(
        name=kb_tool.name,
        description=kb_tool.description,
        func=kb_tool._run,
        coroutine=kb_tool._arun,
    )


class KnowledgeBaseFilteredTool(LangChainBaseTool, IBaseTool):
    """RAG tool with advanced filtering capabilities"""

    name: str = "knowledge_base_filtered_search"
    description: str = """
    Search the knowledge base with advanced filtering options (e.g., by type, tag, or source).
    Input should be a JSON object with a 'query' field and optional filter fields.
    """
    _logger: logging.Logger = PrivateAttr()
    _knowledge_ingester: KnowledgeIngester = PrivateAttr()

    def __init__(self, knowledge_ingester: KnowledgeIngester):
        super().__init__()
        self._logger = logging.getLogger(__name__)
        self._knowledge_ingester = knowledge_ingester

    async def _arun(self, query_json: str) -> str:
        """
        Asynchronously search with advanced filtering

        Args:
            query_json: JSON string with query and filter parameters

        Returns:
            Formatted search results
        """
        try:
            # Parse JSON input
            query_data = json.loads(query_json)
            query = query_data.get("query", "")
            filters = query_data.get("filters", {})

            if not query:
                return "No query provided in the JSON input."

            # Use the enhanced search
            kb_tool = KnowledgeBaseTool(self._knowledge_ingester)
            return await kb_tool._arun(query, filters)

        except json.JSONDecodeError:
            return "Invalid JSON format. Please provide a valid JSON object with 'query' field."
        except Exception as e:
            self._logger.error(f"Filtered search failed: {e}")
            return f"Error in filtered search: {str(e)}"

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """
        Execute the filtered knowledge base search tool using our interface.
        
        Args:
            params: Parameters dictionary containing 'query_json'
            
        Returns:
            ToolResult with success/data/error
        """
        try:
            query_json = params.get('query_json', '')
            
            if not query_json:
                return ToolResult(
                    success=False,
                    data=None,
                    error="No query_json provided"
                )
            
            # Call existing LangChain method
            result = await self._arun(query_json)
            
            return ToolResult(
                success=True,
                data=result,
                error=None
            )
        except Exception as e:
            self._logger.error(f"Filtered knowledge base search execution failed: {e}")
            return ToolResult(
                success=False,
                data=None,
                error=str(e)
            )
    
    def get_schema(self) -> Dict[str, Any]:
        """
        Get the tool schema for our interface compliance.
        
        Returns:
            Tool schema dictionary
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query_json": {
                        "type": "string",
                        "description": "JSON string with query and filter parameters",
                    },
                },
                "required": ["query_json"],
            },
        }

    def _run(self, query_json: str) -> str:
        """
        Synchronous wrapper for the async search

        Args:
            query_json: JSON string with query and filter parameters

        Returns:
            Search results
        """
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(self._arun(query_json))
        except RuntimeError:
            # If no event loop is running, create a new one
            return asyncio.run(self._arun(query_json))

    def _format_filtered_results(
        self, results: List[Dict[str, Any]], filters: Dict[str, Any]
    ) -> str:
        """
        Format filtered search results

        Args:
            results: Search results
            filters: Applied filters

        Returns:
            Formatted results string
        """
        if not results:
            return "No results found with the specified filters."

        formatted_parts = ["Filtered Knowledge Base Search Results:"]

        if filters:
            filter_desc = ", ".join([f"{k}: {v}" for k, v in filters.items()])
            formatted_parts.append(f"**Applied Filters:** {filter_desc}")

        formatted_parts.append("")  # Empty line

        for i, result in enumerate(results, 1):
            document = result["document"]
            metadata = result["metadata"]
            relevance_score = result.get("relevance_score", 0.0)

            formatted_parts.append(f"\n{i}. **Relevance: {relevance_score:.2f}**")
            formatted_parts.append(f"**Source:** {metadata.get('source', 'Unknown')}")

            # Show which filters matched
            matched_filters = []
            for key, value in filters.items():
                if key in metadata and metadata[key] == value:
                    matched_filters.append(f"{key}: {value}")

            if matched_filters:
                formatted_parts.append(
                    f"**Matched Filters:** {', '.join(matched_filters)}"
                )

            # Add content (truncated)
            content = document.strip()
            if len(content) > 400:
                content = content[:400] + "..."

            formatted_parts.append(f"**Content:** {content}")

        return "\n".join(formatted_parts)
