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


def _format_searchable_alternatives(
    all_evidence: list[Any],
    excluded_evidence_id: str,
    case: Any,
) -> str:
    """Build a redirect hint listing file-backed evidence_ids in this case.

    ISS-025: when the LLM picks a non-file-backed evidence_id (e.g. a
    chat-extracted USER_DESCRIPTION row), the tool error should point
    it at the actual searchable upload(s) so the next iteration
    succeeds.
    """
    alternatives = []
    for ev in all_evidence:
        ev_id = getattr(ev, "evidence_id", None)
        if not ev_id or ev_id == excluded_evidence_id:
            continue
        if getattr(ev, "source_file_id", None) is None:
            continue
        file_meta = case.find_uploaded_file(getattr(ev, "source_file_id", None))
        if file_meta is None:
            continue
        # display_name, not filename: this hint goes back to the LLM and
        # a minted pasted-content-<ts>.txt gets cited from there (#666).
        filename = file_meta.display_name or "(unnamed)"
        alternatives.append(f"{ev_id} ({filename})")

    if not alternatives:
        return ""
    return (
        " Available file-backed evidence in this case: " + ", ".join(alternatives) + "."
    )


class DeepAnalysisTool(AgentTool):
    """Tool for on-demand deep analysis of raw evidence files.

    Uses the deep analysis backend to drill into raw data (logs, configs,
    etc.) when the structural index doesn't answer the question. Primary
    entry point for Directed Analysis mode.
    """

    def __init__(self, tier2_service: Any):
        """Initialize with a Tier 2 analysis service.

        Args:
            tier2_service: ITier2SearchService implementation (or None if disabled)
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
            "error patterns). Pass the evidence_id from an <evidence> element OR the "
            "file_id from an <uploaded_file> element (a file uploaded this turn that has "
            "no Evidence row yet), plus a focused query."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "evidence_id": {
                    "type": "string",
                    "description": (
                        "The id to analyze: an evidence id (ev_…) from an "
                        "<evidence> element, or a file id (file_…) from an "
                        "<uploaded_file> element for a file uploaded this turn "
                        "that has no Evidence row yet."
                    ),
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

            from faultmaven.core.preprocessing.models import (
                AnalysisContext,
                UnifiedDataType,
            )

            _TYPE_MAP = {
                "logs": UnifiedDataType.LOGS,
                "metrics": UnifiedDataType.METRICS,
                "configuration": UnifiedDataType.CONFIGURATION,
                "code": UnifiedDataType.CODE,
            }

            all_evidence = getattr(case, "evidence", []) or []
            evidence = None
            for ev in all_evidence:
                if getattr(ev, "evidence_id", None) == evidence_id:
                    evidence = ev
                    break

            file_ref = None
            data_type = UnifiedDataType.TEXT  # default

            if evidence is not None:
                # Reject non-file-backed evidence BEFORE hitting storage.
                # Post-010: chat-extracted evidence has source_file_id IS
                # NULL and cannot be deep-analyzed (no stored bytes to
                # analyze). Without this guard the LLM would pass those
                # evidence_ids to deep_analysis, get an opaque storage
                # error, and give up on follow-up questions.
                if getattr(evidence, "source_file_id", None) is None:
                    alts = _format_searchable_alternatives(
                        all_evidence, evidence_id, case
                    )
                    return ToolResult(
                        success=False,
                        data=None,
                        error=(
                            f"Evidence {evidence_id} has no stored file behind it "
                            f"(it's a verbatim quote from the user's chat message) "
                            f"and cannot be analyzed. Use a file-backed evidence_id "
                            f'(searchable="true") for deep_analysis.{alts}'
                        ),
                    )

                # Get file reference (storage_ref via UploadedFile)
                file_meta = case.find_uploaded_file(evidence.source_file_id)
                file_ref = file_meta.storage_ref if file_meta else None
                if not file_ref:
                    alts = _format_searchable_alternatives(
                        all_evidence, evidence_id, case
                    )
                    return ToolResult(
                        success=False,
                        data=None,
                        error=(
                            f"Evidence {evidence_id} has no file reference for "
                            f"deep analysis.{alts}"
                        ),
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

                # Determine data type from evidence category
                category = getattr(evidence, "category", None)
                if category:
                    cat_value = (
                        category.value if hasattr(category, "value") else str(category)
                    )
                    data_type = _TYPE_MAP.get(cat_value.lower(), UnifiedDataType.TEXT)
            else:
                # Dual-resolution: the id may be a bare ``file_…`` for a file
                # uploaded this turn that has no Evidence row yet. Mirrors
                # search_file's file-id fallback so a just-uploaded file is
                # analyzable before the LLM creates Evidence for it — without
                # this, the LLM reuses the nearest existing evidence_id and
                # analyzes the WRONG file (the bug this fallback closes).
                file_meta = case.find_uploaded_file(evidence_id)
                if file_meta is None or not getattr(file_meta, "storage_ref", None):
                    alts = _format_searchable_alternatives(
                        all_evidence, evidence_id, case
                    )
                    return ToolResult(
                        success=False,
                        data=None,
                        error=f"Evidence {evidence_id} not found in this case.{alts}",
                    )
                file_ref = file_meta.storage_ref
                ft = getattr(file_meta, "data_type", None)
                if ft:
                    data_type = _TYPE_MAP.get(str(ft).lower(), UnifiedDataType.TEXT)
                # NOTE: the triage-to-escalation metric
                # (record_triage_escalation_if_same_turn) is intentionally not
                # emitted here. That helper is Evidence-keyed (it inspects the
                # Evidence row's collected_at_turn); on this branch there is no
                # Evidence row yet. A same-turn file_id deep-analysis is arguably
                # also an escalation — wiring file-keyed escalation telemetry is
                # tracked separately, not bundled into this fix.

            analysis_context = AnalysisContext(
                case_id=context.case_id,
            )

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
