"""KB Tool Adapters for Investigation DA Loop

Wraps DocumentQATool-based KB tools into the AgentTool interface so they
can participate in the investigation pipeline's directed analysis tool loop.

Two adapters:
- KBToolAdapter: unified KB search (all scopes the user can access)
- CaseEvidenceQAAdapter: case-specific evidence forensic search
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)


class KBToolAdapter(AgentTool):
    """Adapter: AnswerFromKB -> AgentTool interface.

    Queries the knowledge base for runbooks, best practices, and documented
    procedures. Automatically filters by the user's accessible scopes
    (global + personal + team).
    """

    def __init__(self, wrapped_tool: Any):
        self._wrapped = wrapped_tool

    @property
    def name(self) -> str:
        return "kb_qa"

    @property
    def description(self) -> str:
        return (
            "Search the knowledge base for runbooks, best practices, and documented "
            "procedures. Returns the most relevant results from all sources you have "
            "access to: global documentation, your personal runbooks, and your team's "
            "shared procedures."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "A focused question about troubleshooting, best practices, "
                        "procedures, or known solutions."
                    ),
                },
            },
            "required": ["question"],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        question = params.get("question", "").strip()
        if not question:
            return ToolResult(success=False, data=None, error="No question provided")

        try:
            result = await self._wrapped._arun(
                question=question,
                user_id=context.user_id,
                team_ids=context.team_ids,
                k=5,
            )
            return ToolResult(success=True, data=result, error=None)
        except Exception as e:
            logger.error(f"KB query failed: {e}")
            return ToolResult(
                success=False,
                data=None,
                error="Knowledge base query failed. The KB may not be populated yet.",
            )


class CaseEvidenceQAAdapter(AgentTool):
    """Adapter: AnswerFromCaseEvidence -> AgentTool interface.

    Semantic search over vectorized case evidence. Available after
    auto-vectorization indexes large evidence files into the vector DB.
    Queries are scoped to the current case.
    """

    def __init__(self, wrapped_tool: Any):
        self._wrapped = wrapped_tool

    @property
    def name(self) -> str:
        return "case_evidence_search"

    @property
    def description(self) -> str:
        return (
            "Semantic search over vectorized case evidence files. Use this tool "
            "when keyword search (search_file) returns no results — this tool "
            "finds content by meaning rather than exact keyword matches. "
            "Only works on files that have been indexed for semantic search."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "A natural language question about the evidence content. "
                        "E.g., 'what tasks is the executor running?' or "
                        "'are there any memory-related operations?'"
                    ),
                },
            },
            "required": ["question"],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        question = params.get("question", "").strip()
        if not question:
            return ToolResult(success=False, data=None, error="No question provided")

        try:
            result = await self._wrapped._arun(
                case_id=context.case_id,
                question=question,
                k=5,
            )
            return ToolResult(success=True, data=result, error=None)
        except Exception as e:
            logger.error(f"Case evidence search failed: {e}")
            return ToolResult(
                success=False,
                data=None,
                error="Case evidence search failed. The evidence may not be vectorized yet.",
            )
