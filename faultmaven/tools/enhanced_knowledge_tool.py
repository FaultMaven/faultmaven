"""Enhanced Knowledge Base Tool with Reasoning Integration

This module provides an enhanced knowledge base tool that integrates with the
reasoning workflows and memory system to provide intelligent, context-aware
knowledge retrieval for agent operations.

Key Features:
- Reasoning-driven query enhancement
- Memory-aware context integration
- Multi-stage knowledge retrieval
- Adaptive search strategies
- Knowledge gap identification
- Proactive knowledge recommendations
"""

import json
import logging
from typing import Any, Dict, List, Optional

from langchain.tools import BaseTool as LangChainBaseTool
from pydantic import PrivateAttr

from faultmaven.models.interfaces import BaseTool as IBaseTool, ToolResult
from faultmaven.services.enhanced_knowledge_service import EnhancedKnowledgeService
from faultmaven.tools.registry import register_tool


@register_tool("enhanced_knowledge_search")
class EnhancedKnowledgeTool(LangChainBaseTool, IBaseTool):
    """Enhanced Knowledge Base Tool with Reasoning and Memory Integration
    
    This tool provides sophisticated knowledge retrieval capabilities that leverage
    reasoning context, memory insights, and adaptive search strategies to find the
    most relevant information for troubleshooting and problem-solving scenarios.
    
    Key Capabilities:
    - Context-aware knowledge search with reasoning integration
    - Memory-enhanced query processing and personalization
    - Multi-modal knowledge retrieval (documents, patterns, insights)
    - Adaptive search strategies based on reasoning type
    - Knowledge gap identification and recommendations
    - Cross-session learning for improved results
    
    Usage:
    - Provide a search query with optional reasoning context
    - Tool automatically enhances query based on memory and context
    - Returns comprehensive results with relevance scoring and insights
    - Identifies knowledge gaps and suggests search improvements
    """
    
    name: str = "enhanced_knowledge_search"
    description: str = """
    Search the knowledge base with advanced reasoning and memory integration.
    
    This tool provides intelligent knowledge retrieval that adapts to your reasoning
    context and learns from previous interactions. It can identify knowledge gaps
    and suggest additional search paths.
    
    Input parameters:
    - query: Your search question or topic (required)
    - reasoning_type: Type of reasoning - diagnostic, analytical, strategic, or creative (optional, default: diagnostic)
    - context: Additional context like urgency, technology stack, environment (optional)
    - session_id: Session identifier for memory context (optional, auto-detected if not provided)
    - depth: Search depth - basic, comprehensive, or exhaustive (optional, default: comprehensive)
    
    Example inputs:
    - Simple: "database connection timeout"
    - With context: {"query": "API rate limiting", "reasoning_type": "diagnostic", "context": {"urgency": "high"}}
    - Comprehensive: {"query": "microservices architecture", "reasoning_type": "strategic", "depth": "exhaustive"}
    """
    
    _enhanced_knowledge_service: EnhancedKnowledgeService = PrivateAttr()
    _logger: logging.Logger = PrivateAttr()

    def __init__(self, enhanced_knowledge_service: EnhancedKnowledgeService):
        """Initialize Enhanced Knowledge Tool
        
        Args:
            enhanced_knowledge_service: Enhanced knowledge service instance
        """
        # Initialize both parent classes properly
        LangChainBaseTool.__init__(self)
        IBaseTool.__init__(self)
        
        self._enhanced_knowledge_service = enhanced_knowledge_service
        self._logger = logging.getLogger(__name__)

    async def _arun(self, query_input: str, **kwargs) -> str:
        """
        Asynchronously search knowledge base with reasoning integration
        
        Args:
            query_input: Either a simple query string or JSON with parameters
            **kwargs: Additional keyword arguments from LangChain
            
        Returns:
            Formatted search results with insights and recommendations
        """
        try:
            # Parse input - handle both simple string and JSON object
            if isinstance(query_input, str):
                if query_input.strip().startswith('{'):
                    # JSON input
                    try:
                        params = json.loads(query_input)
                        query = params.get("query", "")
                        reasoning_type = params.get("reasoning_type", "diagnostic")
                        context = params.get("context", {})
                        session_id = params.get("session_id", "default_session")
                        depth = params.get("depth", "comprehensive")
                    except json.JSONDecodeError:
                        return "Error: Invalid JSON format. Please provide valid JSON or a simple query string."
                else:
                    # Simple string query
                    query = query_input
                    reasoning_type = kwargs.get("reasoning_type", "diagnostic")
                    context = kwargs.get("context", {})
                    session_id = kwargs.get("session_id", "default_session")
                    depth = kwargs.get("depth", "comprehensive")
            else:
                return "Error: Query input must be a string."
            
            if not query or not query.strip():
                return "Error: No query provided. Please specify what you're looking for."
            
            self._logger.info(f"Enhanced knowledge search: {query[:100]}... (type: {reasoning_type})")
            
            # Determine search limit based on depth
            depth_limits = {
                "basic": 5,
                "comprehensive": 10,
                "exhaustive": 15
            }
            limit = depth_limits.get(depth, 10)
            
            # Add reasoning context if available from kwargs
            if "reasoning_context" in kwargs:
                context.update(kwargs["reasoning_context"])
            
            # Perform enhanced search
            search_results = await self._enhanced_knowledge_service.search_with_reasoning_context(
                query=query,
                session_id=session_id,
                reasoning_type=reasoning_type,
                context=context,
                user_profile=kwargs.get("user_profile"),
                limit=limit
            )
            
            # Format results for agent consumption
            formatted_results = self._format_enhanced_results(search_results, depth)
            
            return formatted_results
            
        except Exception as e:
            self._logger.error(f"Enhanced knowledge search failed: {e}")
            return f"Error: Knowledge search failed - {str(e)}. Please try a simpler query or check your input format."

    def _run(self, query_input: str, **kwargs) -> str:
        """
        Synchronous wrapper for the async search
        
        Args:
            query_input: Query string or JSON parameters
            **kwargs: Additional keyword arguments
            
        Returns:
            Formatted search results
        """
        import asyncio
        
        try:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(self._arun(query_input, **kwargs))
        except RuntimeError:
            # If no event loop is running, create a new one
            return asyncio.run(self._arun(query_input, **kwargs))

    def _format_enhanced_results(self, search_results: Dict[str, Any], depth: str) -> str:
        """
        Format enhanced search results for agent consumption
        
        Args:
            search_results: Results from enhanced knowledge service
            depth: Search depth level
            
        Returns:
            Formatted results string
        """
        if not search_results.get("results"):
            return self._format_no_results_response(search_results)
        
        formatted_parts = ["Enhanced Knowledge Search Results:"]
        formatted_parts.append("=" * 50)
        
        # Add search metadata
        formatted_parts.append(f"**Query:** {search_results.get('query', 'Unknown')}")
        formatted_parts.append(f"**Reasoning Type:** {search_results.get('reasoning_type', 'diagnostic')}")
        formatted_parts.append(f"**Enhanced Query:** {search_results.get('enhanced_query', 'N/A')}")
        formatted_parts.append(f"**Search Strategy:** {search_results.get('retrieval_strategy', 'standard')}")
        formatted_parts.append(f"**Confidence Score:** {search_results.get('confidence_score', 0.0):.2f}")
        formatted_parts.append(f"**Contextual Relevance:** {search_results.get('contextual_relevance', 0.0):.2f}")
        formatted_parts.append("")
        
        # Add reasoning insights if available
        reasoning_insights = search_results.get("reasoning_insights", [])
        if reasoning_insights:
            formatted_parts.append("**Query Enhancement Insights:**")
            for insight in reasoning_insights[:3]:  # Top 3 insights
                formatted_parts.append(f"  • {insight}")
            formatted_parts.append("")
        
        # Format search results
        results = search_results.get("results", [])
        formatted_parts.append(f"**Found {len(results)} relevant documents:**")
        formatted_parts.append("")
        
        for i, result in enumerate(results, 1):
            # Basic document information
            formatted_parts.append(f"### Result {i}")
            formatted_parts.append(f"**Relevance Score:** {result.get('relevance_score', 0.0):.2f}")
            
            # Document metadata
            metadata = result.get("metadata", {})
            if metadata.get("title"):
                formatted_parts.append(f"**Title:** {metadata['title']}")
            
            if metadata.get("document_type"):
                formatted_parts.append(f"**Type:** {metadata['document_type']}")
            
            if metadata.get("cluster_topic"):
                formatted_parts.append(f"**Topic:** {metadata['cluster_topic']}")
            
            # Enhancement metadata
            enhancement_metadata = result.get("enhancement_metadata", {})
            if enhancement_metadata.get("reasoning_type"):
                reasoning_score = result.get("diagnostic_relevance") or result.get("analytical_depth") or \
                                result.get("strategic_value") or result.get("innovation_potential")
                if reasoning_score:
                    formatted_parts.append(f"**Reasoning Alignment:** {reasoning_score:.2f}")
            
            # Content
            content = result.get("content", "")
            if len(content) > 300 and depth == "basic":
                content = content[:300] + "..."
            elif len(content) > 500 and depth == "comprehensive":
                content = content[:500] + "..."
            # For exhaustive, show full content
            
            formatted_parts.append(f"**Content:** {content}")
            
            # Additional context if available
            if metadata.get("tags"):
                tags = metadata["tags"]
                if isinstance(tags, list):
                    formatted_parts.append(f"**Tags:** {', '.join(tags)}")
                else:
                    formatted_parts.append(f"**Tags:** {tags}")
            
            if metadata.get("source_url"):
                formatted_parts.append(f"**Source:** {metadata['source_url']}")
            
            formatted_parts.append("")  # Empty line between results
        
        # Add knowledge gaps and recommendations
        knowledge_gaps = search_results.get("knowledge_gaps", [])
        if knowledge_gaps:
            formatted_parts.append("**Knowledge Gaps Identified:**")
            for gap in knowledge_gaps:
                formatted_parts.append(f"  • {gap}")
            formatted_parts.append("")
        
        search_paths = search_results.get("search_expansion_paths", [])
        if search_paths:
            formatted_parts.append("**Suggested Additional Searches:**")
            for path in search_paths:
                formatted_parts.append(f"  • {path}")
            formatted_parts.append("")
        
        # Add performance metrics for comprehensive/exhaustive searches
        if depth in ["comprehensive", "exhaustive"]:
            perf_metrics = search_results.get("performance_metrics", {})
            if perf_metrics:
                formatted_parts.append("**Search Performance:**")
                if perf_metrics.get("search_time_ms"):
                    formatted_parts.append(f"  • Search time: {perf_metrics['search_time_ms']:.1f}ms")
                if perf_metrics.get("memory_insights_used"):
                    formatted_parts.append(f"  • Memory insights used: {perf_metrics['memory_insights_used']}")
                if perf_metrics.get("enhancement_count"):
                    formatted_parts.append(f"  • Query enhancements: {perf_metrics['enhancement_count']}")
                formatted_parts.append("")
        
        # Add curation information
        curation_applied = search_results.get("curation_applied", {})
        if curation_applied.get("curation_applied"):
            formatted_parts.append("**Knowledge Curation Applied:**")
            if curation_applied.get("topic_groups_identified"):
                formatted_parts.append(f"  • Topic groups identified: {curation_applied['topic_groups_identified']}")
            if curation_applied.get("diversity_filtering"):
                formatted_parts.append("  • Diversity filtering applied to reduce redundancy")
            formatted_parts.append("")
        
        return "\n".join(formatted_parts)

    def _format_no_results_response(self, search_results: Dict[str, Any]) -> str:
        """Format response when no results are found"""
        
        formatted_parts = ["No Knowledge Base Results Found"]
        formatted_parts.append("=" * 35)
        
        query = search_results.get("query", "Unknown query")
        formatted_parts.append(f"**Query:** {query}")
        
        enhanced_query = search_results.get("enhanced_query")
        if enhanced_query and enhanced_query != query:
            formatted_parts.append(f"**Enhanced Query:** {enhanced_query}")
        
        formatted_parts.append("")
        formatted_parts.append("**Possible reasons:**")
        formatted_parts.append("  • The knowledge base may not contain information on this topic")
        formatted_parts.append("  • Try using different or more general search terms")
        formatted_parts.append("  • Consider breaking down complex queries into simpler parts")
        
        # Add knowledge gaps if identified
        knowledge_gaps = search_results.get("knowledge_gaps", [])
        if knowledge_gaps:
            formatted_parts.append("")
            formatted_parts.append("**Identified knowledge gaps:**")
            for gap in knowledge_gaps:
                formatted_parts.append(f"  • {gap}")
        
        # Add search suggestions if available
        search_paths = search_results.get("search_expansion_paths", [])
        if search_paths:
            formatted_parts.append("")
            formatted_parts.append("**Suggested alternative searches:**")
            for path in search_paths:
                formatted_parts.append(f"  • {path}")
        
        return "\n".join(formatted_parts)

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """
        Execute the enhanced knowledge search tool using our interface
        
        Args:
            params: Parameters dictionary containing search parameters
            
        Returns:
            ToolResult with success/data/error
        """
        try:
            # Extract parameters
            query = params.get("query", "")
            reasoning_type = params.get("reasoning_type", "diagnostic")
            context = params.get("context", {})
            session_id = params.get("session_id", "default_session")
            depth = params.get("depth", "comprehensive")
            user_profile = params.get("user_profile")
            
            if not query or not query.strip():
                return ToolResult(
                    success=False,
                    data=None,
                    error="No query provided"
                )
            
            # Perform enhanced search
            search_results = await self._enhanced_knowledge_service.search_with_reasoning_context(
                query=query,
                session_id=session_id,
                reasoning_type=reasoning_type,
                context=context,
                user_profile=user_profile,
                limit=10 if depth == "comprehensive" else 15 if depth == "exhaustive" else 5
            )
            
            # Format results
            formatted_results = self._format_enhanced_results(search_results, depth)
            
            return ToolResult(
                success=True,
                data={
                    "formatted_results": formatted_results,
                    "raw_results": search_results,
                    "search_metadata": {
                        "query": query,
                        "reasoning_type": reasoning_type,
                        "depth": depth,
                        "results_count": len(search_results.get("results", [])),
                        "confidence_score": search_results.get("confidence_score", 0.0)
                    }
                },
                error=None
            )
            
        except Exception as e:
            self._logger.error(f"Enhanced knowledge search execution failed: {e}")
            return ToolResult(
                success=False,
                data=None,
                error=str(e)
            )

    def get_schema(self) -> Dict[str, Any]:
        """
        Get the tool schema for our interface compliance
        
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
                        "description": "Search query for the knowledge base"
                    },
                    "reasoning_type": {
                        "type": "string",
                        "enum": ["diagnostic", "analytical", "strategic", "creative"],
                        "description": "Type of reasoning context for search optimization",
                        "default": "diagnostic"
                    },
                    "context": {
                        "type": "object",
                        "description": "Additional context for search enhancement",
                        "properties": {
                            "urgency_level": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "critical"]
                            },
                            "technical_constraints": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "environment": {"type": "string"},
                            "technology_stack": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "service_name": {"type": "string"}
                        }
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session identifier for memory context"
                    },
                    "depth": {
                        "type": "string",
                        "enum": ["basic", "comprehensive", "exhaustive"],
                        "description": "Search depth level",
                        "default": "comprehensive"
                    },
                    "user_profile": {
                        "type": "object",
                        "description": "User profile for personalization"
                    }
                },
                "required": ["query"]
            }
        }

    async def discover_related_knowledge(
        self,
        document_id: str,
        session_id: str = "default_session",
        depth: int = 2
    ) -> str:
        """
        Discover related knowledge for a specific document
        
        Args:
            document_id: Source document ID
            session_id: Session identifier
            depth: Exploration depth
            
        Returns:
            Formatted related knowledge results
        """
        try:
            results = await self._enhanced_knowledge_service.discover_related_knowledge(
                document_id=document_id,
                session_id=session_id,
                exploration_depth=depth
            )
            
            formatted_parts = ["Related Knowledge Discovery:"]
            formatted_parts.append("=" * 35)
            formatted_parts.append(f"**Source Document:** {document_id}")
            
            key_concepts = results.get("key_concepts", [])
            if key_concepts:
                formatted_parts.append(f"**Key Concepts:** {', '.join(key_concepts[:5])}")
            
            related_docs = results.get("related_documents", [])
            if related_docs:
                formatted_parts.append(f"\n**Found {len(related_docs)} related documents:**")
                for i, doc in enumerate(related_docs[:5], 1):
                    formatted_parts.append(f"\n{i}. **Relevance:** {doc.get('relevance_score', 0.0):.2f}")
                    formatted_parts.append(f"   **Connection:** {doc.get('connection_concept', 'Unknown')}")
                    formatted_parts.append(f"   **Content:** {doc.get('content', '')[:200]}...")
            
            knowledge_paths = results.get("knowledge_paths", [])
            if knowledge_paths:
                formatted_parts.append("\n**Knowledge Exploration Paths:**")
                for path in knowledge_paths[:3]:
                    formatted_parts.append(f"  • {path.get('concept')}: {path.get('related_count')} connections")
            
            return "\n".join(formatted_parts)
            
        except Exception as e:
            self._logger.error(f"Knowledge discovery failed: {e}")
            return f"Error: Knowledge discovery failed - {str(e)}"

    async def curate_for_reasoning(
        self,
        reasoning_type: str,
        session_id: str = "default_session",
        topic_focus: Optional[str] = None
    ) -> str:
        """
        Curate knowledge specifically for a reasoning workflow
        
        Args:
            reasoning_type: Type of reasoning workflow
            session_id: Session identifier
            topic_focus: Optional topic focus
            
        Returns:
            Formatted curated knowledge results
        """
        try:
            results = await self._enhanced_knowledge_service.curate_knowledge_for_reasoning(
                reasoning_type=reasoning_type,
                session_id=session_id,
                topic_focus=topic_focus
            )
            
            formatted_parts = [f"Knowledge Curation for {reasoning_type.title()} Reasoning:"]
            formatted_parts.append("=" * 50)
            
            if topic_focus:
                formatted_parts.append(f"**Topic Focus:** {topic_focus}")
            
            curated_content = results.get("curated_content", [])
            if curated_content:
                formatted_parts.append(f"\n**Curated {len(curated_content)} relevant items:**")
                
                for i, item in enumerate(curated_content[:8], 1):
                    formatted_parts.append(f"\n{i}. **Alignment Score:** {item.get('reasoning_alignment_score', 0.0):.2f}")
                    formatted_parts.append(f"   **Concept:** {item.get('concept_alignment', 'General')}")
                    formatted_parts.append(f"   **Content:** {item.get('content', '')[:150]}...")
            
            curation_metadata = results.get("curation_metadata", {})
            if curation_metadata.get("key_concepts"):
                formatted_parts.append(f"\n**Key Concepts Covered:** {', '.join(curation_metadata['key_concepts'])}")
            
            optimization = results.get("reasoning_optimization", {})
            if optimization:
                formatted_parts.append(f"\n**Optimization Metrics:**")
                formatted_parts.append(f"  • Concept coverage: {optimization.get('concept_coverage', 0)} items")
                formatted_parts.append(f"  • Average alignment: {optimization.get('avg_alignment_score', 0.0):.2f}")
            
            return "\n".join(formatted_parts)
            
        except Exception as e:
            self._logger.error(f"Knowledge curation failed: {e}")
            return f"Error: Knowledge curation failed - {str(e)}"


@register_tool("knowledge_discovery")
class KnowledgeDiscoveryTool(LangChainBaseTool, IBaseTool):
    """Knowledge Discovery Tool for exploring related knowledge"""
    
    name: str = "knowledge_discovery"
    description: str = """
    Discover related knowledge and explore knowledge connections.
    
    This tool helps you explore knowledge relationships and discover
    additional relevant information based on a starting document or concept.
    
    Input: Document ID or concept to explore
    """
    
    _enhanced_knowledge_service: EnhancedKnowledgeService = PrivateAttr()
    _logger: logging.Logger = PrivateAttr()

    def __init__(self, enhanced_knowledge_service: EnhancedKnowledgeService):
        LangChainBaseTool.__init__(self)
        IBaseTool.__init__(self)
        self._enhanced_knowledge_service = enhanced_knowledge_service
        self._logger = logging.getLogger(__name__)

    async def _arun(self, input_data: str) -> str:
        """Async knowledge discovery"""
        try:
            # Parse input
            if input_data.startswith('{'):
                params = json.loads(input_data)
                document_id = params.get("document_id", "")
                session_id = params.get("session_id", "default_session")
                depth = params.get("depth", 2)
            else:
                document_id = input_data
                session_id = "default_session"
                depth = 2
            
            if not document_id:
                return "Error: No document ID provided for discovery."
            
            results = await self._enhanced_knowledge_service.discover_related_knowledge(
                document_id=document_id,
                session_id=session_id,
                exploration_depth=depth
            )
            
            # Format results
            formatted_parts = ["Knowledge Discovery Results:"]
            formatted_parts.append("=" * 35)
            
            related_docs = results.get("related_documents", [])
            if related_docs:
                formatted_parts.append(f"Found {len(related_docs)} related documents:")
                for i, doc in enumerate(related_docs[:5], 1):
                    formatted_parts.append(f"\n{i}. Relevance: {doc.get('relevance_score', 0.0):.2f}")
                    formatted_parts.append(f"   Connection: {doc.get('connection_concept', 'Unknown')}")
                    formatted_parts.append(f"   Content: {doc.get('content', '')[:200]}...")
            else:
                formatted_parts.append("No related documents found.")
            
            return "\n".join(formatted_parts)
            
        except Exception as e:
            self._logger.error(f"Knowledge discovery failed: {e}")
            return f"Error: Knowledge discovery failed - {str(e)}"

    def _run(self, input_data: str) -> str:
        """Sync wrapper"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(self._arun(input_data))
        except RuntimeError:
            return asyncio.run(self._arun(input_data))

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Execute knowledge discovery"""
        try:
            document_id = params.get("document_id", "")
            if not document_id:
                return ToolResult(success=False, data=None, error="No document ID provided")
            
            result = await self._arun(document_id)
            return ToolResult(success=True, data=result, error=None)
            
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def get_schema(self) -> Dict[str, Any]:
        """Get tool schema"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "Document ID to explore"
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session identifier"
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Exploration depth",
                        "default": 2
                    }
                },
                "required": ["document_id"]
            }
        }