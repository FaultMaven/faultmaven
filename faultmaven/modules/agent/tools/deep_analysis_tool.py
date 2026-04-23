"""Deep Analysis Tool for Agent Execution

Invokes LLM-interpreted deep analysis on raw evidence files. Primary tool
for Directed Analysis mode — the LLM reads the user's question, searches
the file, and answers.

Design Reference: docs/architecture/data-processing/README.md
"""

import logging
from typing import Any, Dict, Optional

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)


class DeepAnalysisTool(AgentTool):
    """Tool for on-demand deep analysis of raw evidence files.

    Uses the deep analysis backend to drill into raw data (logs, configs,
    etc.) when the structural index doesn't answer the question. Primary
    entry point for Directed Analysis mode.
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
            # Storage redesign 2026-04 phase 2: evidence lives on `case.evidence`
            # (standalone evidence path deleted). Resolve via case_repository.
            case = getattr(context, "in_memory_case", None)
            if case is None and context.case_repository is not None:
                case = await context.case_repository.get(context.case_id)
            if case is None:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Case not found: {context.case_id}",
                )

            evidence = None
            for ev in getattr(case, "evidence", []) or []:
                if getattr(ev, "evidence_id", None) == evidence_id:
                    evidence = ev
                    break
            if evidence is None:
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

            # Phase 2c — triage-to-escalation counter. Mirrors the
            # equivalent wiring in search_file_tool. Fires when this
            # deep analysis targets an evidence delivered in the same
            # turn — signals the extractor's output was insufficient.
            try:
                from faultmaven.infrastructure.observability.evidence_metrics import (
                    record_triage_escalation_if_same_turn,
                )

                record_triage_escalation_if_same_turn(
                    evidence=evidence,
                    case=case,
                    tool_name="deep_analysis",
                )
            except Exception:
                pass

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
