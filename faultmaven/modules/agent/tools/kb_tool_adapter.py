"""KB Tool Adapters for Investigation DA Loop

Wraps DocumentQATool-based KB tools into the AgentTool interface so they
can participate in the investigation pipeline's directed analysis tool loop.

Two adapters:
- KBToolAdapter: unified KB search (every scope the case may draw on)
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
    procedures, under the case's scope filter from ``ToolContext`` (the case's
    audience, #1919). Never under anything the model supplies: the only
    parameter is the question.
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
            "procedures. Returns the most relevant results from every source this "
            "case may draw on: global documentation, plus the personal and team "
            "runbooks the case's readers can see."
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

        if context.kb_scope_filter is None:
            # No scope was resolved for this case. Searching anyway would need a
            # scope invented here, and the only safe one to invent is narrower
            # than the case's — the same silent narrowing the model would then
            # read as "the KB holds nothing on this". Refuse, and say so.
            return ToolResult(
                success=False,
                data=None,
                error=(
                    "Knowledge base query refused: no retrieval scope was "
                    "resolved for this case. This is a retrieval failure, not a "
                    "statement about the knowledge base's contents — draw no "
                    "conclusion about what it holds."
                ),
            )

        try:
            result = await self._wrapped._arun(
                question=question,
                scope_filter=context.kb_scope_filter,
                k=5,
            )
            return ToolResult(success=True, data=result, error=None)
        except Exception as e:
            logger.error(f"KB query failed: {e}")
            # Report the failure, and ONLY the failure. The previous text
            # ("The KB may not be populated yet") guessed at a cause the
            # adapter cannot know, turning an infrastructure fault into a
            # claim about the KB's contents — the model would then reason
            # from "the KB is empty" (#943). A failed query establishes
            # nothing about what the knowledge base holds.
            return ToolResult(
                success=False,
                data=None,
                error=(
                    f"Knowledge base query failed: {e}. This is a retrieval "
                    f"failure, not a statement about the knowledge base's "
                    f"contents — draw no conclusion about what it holds."
                ),
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
            # Same rule as KBToolAdapter above: a failed search says nothing
            # about whether evidence exists. "may not be vectorized yet" was a
            # guess that reads as a finding about the case (#943).
            return ToolResult(
                success=False,
                data=None,
                error=(
                    f"Case evidence search failed: {e}. This is a retrieval "
                    f"failure, not a statement about what evidence exists — "
                    f"draw no conclusion about the case's evidence."
                ),
            )
