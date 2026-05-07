"""List Evidence Tool for Agent Execution (TASK-015)

This tool allows agents to list all evidence artifacts uploaded
to a case during investigation sessions.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

import logging
from typing import Any, Dict, List

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

        Storage redesign 2026-04 phase 2: evidence is case-tied only and
        accessed via `case.evidence`. The standalone evidence service was
        deleted; this tool reads through `context.case_repository` (with
        `context.in_memory_case` as a turn-scoped optimization).

        Args:
            params: Parameters including optional evidence_type filter and limit
            context: Execution context with case_repository / in_memory_case

        Returns:
            ToolResult with list of evidence metadata or error
        """
        evidence_type_str = params.get("evidence_type")
        limit = params.get("limit", 50)

        try:
            case = context.in_memory_case
            if case is None and context.case_repository is not None:
                case = await context.case_repository.get(context.case_id)
            if case is None:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Case not found: {context.case_id}",
                )

            evidence_list: List[Any] = list(getattr(case, "evidence", []) or [])

            # Optional evidence_type filter — match against the domain Evidence
            # model's `source_type` (or `category`) string value when present.
            if evidence_type_str:
                filtered: List[Any] = []
                for ev in evidence_list:
                    type_attr = getattr(ev, "source_type", None) or getattr(
                        ev, "category", None
                    )
                    type_value = (
                        type_attr.value if hasattr(type_attr, "value") else type_attr
                    )
                    if type_value == evidence_type_str:
                        filtered.append(ev)
                evidence_list = filtered

            # Apply limit (after filtering to keep semantics intuitive)
            if isinstance(limit, int) and limit > 0:
                evidence_list = evidence_list[:limit]

            formatted_evidence = self._format_evidence_list(evidence_list, case)

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
        case: Any,
    ) -> List[Dict[str, Any]]:
        """Format evidence list for LLM consumption.

        Accepts the domain `Evidence` model (which lives on `case.evidence`
        after the storage redesign 2026-04 phase 2). Falls back gracefully for
        fields the domain model does not carry (mime_type, description).

        Args:
            evidence_list: List of domain Evidence records
            case: Case aggregate (used to resolve UploadedFile metadata)

        Returns:
            List of formatted evidence dictionaries
        """
        formatted: List[Dict[str, Any]] = []
        for evidence in evidence_list:
            # Evidence source type / category (either is fine for LLM display)
            type_attr = getattr(evidence, "source_type", None) or getattr(
                evidence, "category", None
            )
            type_value = type_attr.value if hasattr(type_attr, "value") else type_attr

            # Size + filename: walk the FK to UploadedFile via the case
            # aggregate (replaces the dropped denormalized fields on Evidence).
            file_meta = case.find_uploaded_file(
                getattr(evidence, "source_file_id", None)
            )
            size_bytes = int(file_meta.size_bytes if file_meta else 0)
            filename = file_meta.filename if file_meta else None

            # Timestamp: domain Evidence uses collected_at
            collected_at = getattr(evidence, "collected_at", None)
            collected_iso = (
                collected_at.isoformat() if hasattr(collected_at, "isoformat") else None
            )

            formatted.append(
                {
                    "evidence_id": getattr(evidence, "evidence_id", None),
                    "filename": filename,
                    "type": type_value,
                    # mime_type is not on the domain Evidence model; the future
                    # normalized evidence table will reintroduce it (see
                    # deployment-schema-strategy.md §11, Phase 6).
                    "mime_type": None,
                    "file_size_bytes": size_bytes,
                    "file_size_human": self._format_file_size(size_bytes),
                    # Use the evidence summary as a description proxy — the
                    # domain model does not carry a separate description.
                    "description": getattr(evidence, "summary", None),
                    # is_primary is a Phase 6 column; default False for now so
                    # the LLM-facing contract stays stable.
                    "is_primary": bool(getattr(evidence, "is_primary", False)),
                    "created_at": collected_iso,
                }
            )
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
