"""List Evidence Tool for Agent Execution (TASK-015)

This tool allows agents to list all evidence artifacts uploaded
to a case during investigation sessions.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

import logging
from typing import Any, Dict, List, Optional

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext, tool_registry

logger = logging.getLogger(__name__)


class ListEvidenceTool(AgentTool):
    """Tool for listing evidence artifacts for a case.

    Allows the agent to discover what evidence files have been
    uploaded for the current case, including metadata like
    file type, size, and description.
    """

    @property
    def name(self) -> str:
        return "list_evidence"

    @property
    def description(self) -> str:
        return (
            "List all evidence artifacts uploaded for the current case. "
            "Returns metadata about each file including ID, filename, type, "
            "size, and description. Use the evidence_id with read_file to "
            "access file contents."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "evidence_type": {
                    "type": "string",
                    "description": (
                        "Optional filter by evidence type. "
                        "Options: screenshot, log_file, config_file, "
                        "code_snippet, stack_trace, metrics_data, "
                        "network_capture, database_dump, other"
                    ),
                    "enum": [
                        "screenshot",
                        "log_file",
                        "config_file",
                        "code_snippet",
                        "stack_trace",
                        "metrics_data",
                        "network_capture",
                        "database_dump",
                        "other",
                    ],
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 50)",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": [],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """List evidence artifacts for the current case.

        Args:
            params: Parameters including optional evidence_type filter and limit
            context: Execution context with evidence service

        Returns:
            ToolResult with list of evidence metadata or error
        """
        evidence_type_str = params.get("evidence_type")
        limit = params.get("limit", 50)

        if not context.evidence_service:
            return ToolResult(
                success=False,
                data=None,
                error="Evidence service not available",
            )

        try:
            # Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
            from faultmaven.modules.evidence.contracts import EvidenceArtifactType

            # Parse evidence type filter if provided
            evidence_type = None
            if evidence_type_str:
                try:
                    evidence_type = EvidenceArtifactType(evidence_type_str)
                except ValueError:
                    return ToolResult(
                        success=False,
                        data=None,
                        error=f"Invalid evidence_type: {evidence_type_str}",
                    )

            # List evidence for the case
            evidence_list = await context.evidence_service.list_evidence_by_case(
                case_id=context.case_id,
                organization_id=context.organization_id,
                evidence_type=evidence_type,
                limit=limit,
            )

            # Format evidence for response
            formatted_evidence = self._format_evidence_list(evidence_list)

            logger.info(
                f"Listed {len(evidence_list)} evidence items for case {context.case_id} "
                f"in execution {context.execution_id}"
            )

            return ToolResult(
                success=True,
                data={
                    "evidence": formatted_evidence,
                    "total_count": len(evidence_list),
                    "case_id": context.case_id,
                },
            )

        except Exception as e:
            logger.exception(f"Failed to list evidence for case {context.case_id}: {e}")
            return ToolResult(
                success=False,
                data=None,
                error=f"Failed to list evidence: {str(e)}",
            )

    def _format_evidence_list(
        self,
        evidence_list: List[Any],
    ) -> List[Dict[str, Any]]:
        """Format evidence list for LLM consumption.

        Args:
            evidence_list: List of evidence artifacts

        Returns:
            List of formatted evidence dictionaries
        """
        formatted = []
        for evidence in evidence_list:
            formatted.append({
                "evidence_id": evidence.evidence_id,
                "filename": evidence.original_filename,
                "type": evidence.evidence_type.value if hasattr(evidence.evidence_type, "value") else str(evidence.evidence_type),
                "mime_type": evidence.mime_type,
                "file_size_bytes": evidence.file_size,
                "file_size_human": self._format_file_size(evidence.file_size),
                "description": evidence.description,
                "is_primary": evidence.is_primary,
                "created_at": (
                    evidence.created_at.isoformat()
                    if evidence.created_at
                    else None
                ),
            })
        return formatted

    def _format_file_size(self, size_bytes: int) -> str:
        """Format file size in human-readable format.

        Args:
            size_bytes: File size in bytes

        Returns:
            Human-readable size string
        """
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


class SearchKnowledgeTool(AgentTool):
    """Tool for searching the knowledge base (RAG).

    Allows the agent to search the knowledge base for relevant
    information to help with troubleshooting.

    Note: This is a placeholder implementation. Full RAG integration
    will be implemented in future tasks.
    """

    @property
    def name(self) -> str:
        return "search_knowledge"

    @property
    def description(self) -> str:
        return (
            "Search the knowledge base for relevant troubleshooting information, "
            "documentation, and previous solutions. Use natural language queries "
            "to find related information."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query in natural language",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 5)",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Search the knowledge base.

        Note: This is a placeholder. RAG integration will be added in future tasks.

        Args:
            params: Parameters including query and limit
            context: Execution context

        Returns:
            ToolResult with search results or placeholder message
        """
        query = params.get("query", "")
        limit = params.get("limit", 5)

        if not query:
            return ToolResult(
                success=False,
                data=None,
                error="query parameter is required",
            )

        # Placeholder implementation
        logger.info(
            f"Knowledge search requested: '{query}' (limit: {limit}) "
            f"for execution {context.execution_id}"
        )

        return ToolResult(
            success=False,
            data=None,
            error=(
                "Knowledge base search is not yet implemented. "
                "This feature will be available in a future release. "
                "Please use the available evidence files for investigation."
            ),
        )


# Register tools with the agent tool registry
def register_list_evidence_tool() -> None:
    """Register the list_evidence tool with the agent tool registry."""
    try:
        tool_registry.register(ListEvidenceTool())
    except ValueError:
        # Already registered
        pass


def register_search_knowledge_tool() -> None:
    """Register the search_knowledge tool with the agent tool registry."""
    try:
        tool_registry.register(SearchKnowledgeTool())
    except ValueError:
        # Already registered
        pass


def register_all_agent_tools() -> None:
    """Register all agent tools with the agent tool registry."""
    from faultmaven.modules.agent.tools.read_file_tool import register_read_file_tool

    register_read_file_tool()
    register_list_evidence_tool()
    register_search_knowledge_tool()
