"""KB Tool Adapters for Investigation DA Loop

Wraps existing DocumentQATool-based KB tools (AnswerFromGlobalKB,
AnswerFromUserKB) into the AgentTool interface so they can participate
in the investigation pipeline's directed analysis tool loop.

Adapter pattern: translates the interface without modifying the
working vector search + LLM synthesis logic in DocumentQATool.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)


class GlobalKBToolAdapter(AgentTool):
    """Adapter: AnswerFromGlobalKB -> AgentTool interface.

    Queries the system-wide knowledge base for general troubleshooting
    guidance, best practices, and documented solutions. No user scoping.
    """

    def __init__(self, wrapped_tool: Any):
        """
        Args:
            wrapped_tool: AnswerFromGlobalKB instance
        """
        self._wrapped = wrapped_tool

    @property
    def name(self) -> str:
        return "global_kb_qa"

    @property
    def description(self) -> str:
        return (
            "Query the system-wide knowledge base for general troubleshooting guidance, "
            "best practices, and documented solutions to known problems. Use when you need "
            "industry-standard approaches or common solutions (e.g. 'standard approach for "
            "diagnosing memory leaks', 'common causes of API timeouts')."
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
                        "or known solutions."
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
            result = await self._wrapped._arun(question=question, k=5)
            return ToolResult(success=True, data=result, error=None)
        except Exception as e:
            logger.error(f"Global KB query failed: {e}")
            return ToolResult(
                success=False,
                data=None,
                error="Knowledge base query failed. The KB may not be populated yet.",
            )


class UserKBToolAdapter(AgentTool):
    """Adapter: AnswerFromUserKB -> AgentTool interface.

    Queries the user's personal knowledge base for their documented
    runbooks, procedures, and best practices. Scoped to the requesting user.
    """

    def __init__(self, wrapped_tool: Any):
        """
        Args:
            wrapped_tool: AnswerFromUserKB instance
        """
        self._wrapped = wrapped_tool

    @property
    def name(self) -> str:
        return "user_kb_qa"

    @property
    def description(self) -> str:
        return (
            "Query the user's personal knowledge base for their documented runbooks, "
            "procedures, and best practices. Use when the user references 'my runbook' "
            "or 'my procedure', or when looking for the user's standard approach to "
            "common issues."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "A focused question about the user's documented procedures, "
                        "runbooks, or best practices."
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
            # AnswerFromUserKB._arun expects (user_id, question, k)
            result = await self._wrapped._arun(
                user_id=context.user_id,
                question=question,
                k=5,
            )
            return ToolResult(success=True, data=result, error=None)
        except Exception as e:
            logger.error(f"User KB query failed: {e}")
            return ToolResult(
                success=False,
                data=None,
                error="User knowledge base query failed. Your KB may not be populated yet.",
            )
