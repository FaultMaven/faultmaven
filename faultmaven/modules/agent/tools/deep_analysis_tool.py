"""Deep Analysis Tool for Agent Execution

Invokes Tier 2 deep analysis on raw evidence files when Tier 0+1
structural indexes are insufficient to answer the agent's question.

Design Reference:
- docs/architecture/data-processing/data-preprocessing-design-specification.md Section 9.3
"""

import logging
from typing import Any, Dict, Optional

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)


class DeepAnalysisTool(AgentTool):
    """Tool for on-demand deep analysis of raw evidence files.

    Uses Tier 2 preprocessing to drill into raw data (logs, configs, etc.)
    when the structural index from Tier 0+1 doesn't answer the question.
    """

    def __init__(self, tier2_service: Any):
        """Initialize with a Tier 2 analysis service.

        Args:
            tier2_service: ITier2AnalysisService implementation (or None if disabled)
        """
        self.tier2_service = tier2_service

    @property
    def name(self) -> str:
        return "deep_analysis"

    @property
    def description(self) -> str:
        return (
            "Perform deep analysis on a raw evidence file to answer a specific question. "
            "Use this when the structural summary from evidence is insufficient and you "
            "need to drill into the raw data (e.g., specific log lines, config values, "
            "error patterns). Requires an evidence_id and a focused query."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "evidence_id": {
                    "type": "string",
                    "description": "The ID of the evidence artifact to analyze",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "A focused question about the file contents. "
                        "Be specific (e.g., 'What errors appear between 14:00 and 14:30?')"
                    ),
                },
            },
            "required": ["evidence_id", "query"],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute deep analysis on a file."""
        if not self.tier2_service:
            return ToolResult(
                success=False,
                data=None,
                error="Deep analysis is not available (Tier 2 backend is disabled).",
            )

        evidence_id = params.get("evidence_id", "")
        query = params.get("query", "")

        if not evidence_id or not query:
            return ToolResult(
                success=False,
                data=None,
                error="Both evidence_id and query are required.",
            )

        try:
            # Get evidence to find the file reference
            evidence_service = context.evidence_service
            if not evidence_service:
                return ToolResult(
                    success=False,
                    data=None,
                    error="Evidence service not available in context.",
                )

            evidence = await evidence_service.get_evidence(
                evidence_id, context.organization_id
            )
            if not evidence:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Evidence {evidence_id} not found.",
                )

            # Get file reference (content_ref from preprocessing)
            file_ref = getattr(evidence, "content_ref", None) or getattr(
                evidence, "file_path", None
            )
            if not file_ref:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Evidence {evidence_id} has no file reference for deep analysis.",
                )

            # Build analysis context
            from faultmaven.core.preprocessing.models import (
                AnalysisContext,
                UnifiedDataType,
            )

            analysis_context = AnalysisContext(
                case_id=context.case_id,
            )

            # Determine data type from evidence category
            data_type = UnifiedDataType.TEXT  # default
            category = getattr(evidence, "category", None)
            if category:
                cat_value = (
                    category.value if hasattr(category, "value") else str(category)
                )
                type_map = {
                    "logs": UnifiedDataType.LOGS,
                    "metrics": UnifiedDataType.METRICS,
                    "configuration": UnifiedDataType.CONFIGURATION,
                    "code": UnifiedDataType.CODE,
                }
                data_type = type_map.get(cat_value.lower(), UnifiedDataType.TEXT)

            # Invoke Tier 2
            result = await self.tier2_service.analyze(
                file_ref=file_ref,
                query=query,
                context=analysis_context,
                data_type=data_type,
            )

            # Format result for agent consumption
            excerpts_text = ""
            if result.excerpts:
                for i, excerpt in enumerate(result.excerpts, 1):
                    line_info = ""
                    if excerpt.line_start is not None:
                        line_info = f" (lines {excerpt.line_start}-{excerpt.line_end})"
                    excerpts_text += (
                        f"\n--- Excerpt {i}{line_info} ---\n{excerpt.content}\n"
                    )

            return ToolResult(
                success=True,
                data={
                    "answer": result.answer,
                    "excerpts": excerpts_text,
                    "confidence": result.confidence,
                    "backend": result.backend_used,
                },
            )

        except Exception as e:
            logger.warning(
                f"Deep analysis failed for evidence {evidence_id}: {e}",
                exc_info=True,
                extra={"case_id": context.case_id, "evidence_id": evidence_id},
            )
            return ToolResult(
                success=False,
                data=None,
                error=f"Deep analysis failed: {str(e)[:200]}",
            )
