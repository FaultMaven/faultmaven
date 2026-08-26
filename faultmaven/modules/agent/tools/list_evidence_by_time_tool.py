"""List Evidence By Time Tool — Phase 3b.

Exposes the ``case_repository.list_evidence_by_time_window`` query as
an agent tool. The LLM calls this when the user's turn references a
time window ("what happened between 14:30 and 14:45?") and the agent
needs to narrow the evidence set to artifacts whose *content* covers
that span — as opposed to all evidence in the case.

Covers evidence with non-NULL
``coverage_start_ts``/``coverage_end_ts`` only. Timeless evidence
(configs, code, screenshots, short pastes) is excluded by the
repository query; use ``list_evidence`` without bounds for the
unfiltered view.

Design Reference:
    docs/working/WIP-data-processing-improvement-plan.md Phase 3.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)


def _parse_iso_bound(
    value: Optional[str], bound_name: str
) -> tuple[Optional[datetime], Optional[str]]:
    """Parse an ISO-8601 string into a datetime.

    Returns ``(datetime, None)`` on success, ``(None, error_message)``
    on parse failure. The caller surfaces the error message to the
    agent as a ToolResult error.
    """
    if value is None or value == "":
        return None, None
    try:
        # fromisoformat accepts both "2026-04-23T14:00:00" and
        # "2026-04-23T14:00:00+00:00"; naive datetimes are treated as
        # UTC-equivalent by the downstream comparison (stored bounds
        # are always TZ-aware under PostgreSQL; SQLite is naive).
        return datetime.fromisoformat(value), None
    except (ValueError, TypeError) as e:
        return (
            None,
            (
                f"Invalid ISO-8601 timestamp for {bound_name}: {value!r}. "
                f"Expected format: YYYY-MM-DDTHH:MM:SS[+00:00]. Error: {e}"
            ),
        )


class ListEvidenceByTimeTool(AgentTool):
    """Return evidence whose coverage overlaps a time window.

    Use this when the user's turn references a specific time span
    ("what happened between 14:30 and 14:45?"). The response includes
    evidence items whose extracted timestamps overlap the window,
    ordered by their coverage_start_ts. Timeless evidence is excluded
    — use ``list_evidence`` for the unfiltered view.
    """

    def __init__(self, case_repository: Any = None):
        self.case_repository = case_repository

    @property
    def name(self) -> str:
        return "list_evidence_by_time"

    @property
    def description(self) -> str:
        return (
            "List evidence whose content covers a specified time window. "
            "Use when the user asks about a specific time range "
            "('between 14:30 and 14:45', 'during the outage window'). "
            "Returns evidence ordered by coverage start time. Timeless "
            "evidence (configs, code, screenshots, short pastes) is "
            "excluded — use list_evidence for the full set. Either "
            "bound may be omitted; both None returns all evidence with "
            "non-NULL coverage."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "start_ts": {
                    "type": "string",
                    "description": (
                        "ISO-8601 lower bound of the time window "
                        "(inclusive), e.g. '2026-04-23T14:30:00'. "
                        "Omit for 'everything up to end_ts'."
                    ),
                },
                "end_ts": {
                    "type": "string",
                    "description": (
                        "ISO-8601 upper bound of the time window "
                        "(inclusive), e.g. '2026-04-23T14:45:00'. "
                        "Omit for 'everything from start_ts onward'."
                    ),
                },
            },
            "required": [],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Run the time-window query and return a compact evidence list."""
        if self.case_repository is None:
            return ToolResult(
                success=False,
                data=None,
                error=(
                    "Case repository not wired; time-window evidence "
                    "queries are unavailable on this deployment."
                ),
            )

        start_raw = params.get("start_ts")
        end_raw = params.get("end_ts")

        start_ts, err = _parse_iso_bound(start_raw, "start_ts")
        if err:
            return ToolResult(success=False, data=None, error=err)
        end_ts, err = _parse_iso_bound(end_raw, "end_ts")
        if err:
            return ToolResult(success=False, data=None, error=err)

        try:
            evidence_list = await self.case_repository.list_evidence_by_time_window(
                case_id=context.case_id,
                start=start_ts,
                end=end_ts,
            )
        except Exception as e:
            logger.exception(
                "list_evidence_by_time_window failed for case %s: %s",
                context.case_id,
                e,
            )
            return ToolResult(
                success=False,
                data=None,
                error=f"Time-window query failed: {e}",
            )

        results = _format_evidence_summaries(evidence_list, context.in_memory_case)
        return ToolResult(
            success=True,
            data={
                "case_id": context.case_id,
                "start_ts": start_raw,
                "end_ts": end_raw,
                "count": len(results),
                "evidence": results,
                "note": (
                    "Evidence ordered by coverage_start_ts ascending. "
                    "Timeless evidence (NULL coverage) is excluded."
                ),
            },
        )


def _format_evidence_summaries(
    evidence_list: List[Any], case: Any = None
) -> List[Dict[str, Any]]:
    """Flatten Evidence objects to agent-friendly dicts.

    Includes the fields the LLM typically needs to decide whether to
    drill in: id, filename, data_type, coverage span, and the summary.

    The ``case`` parameter is used to resolve UploadedFile metadata
    (filename) via ``case.find_uploaded_file(ev.source_file_id)``. May
    be None if the caller doesn't have a case in scope (filename will
    be None in that case).
    """
    formatted: List[Dict[str, Any]] = []
    for ev in evidence_list:
        start = getattr(ev, "coverage_start_ts", None)
        end = getattr(ev, "coverage_end_ts", None)
        file_meta = (
            case.find_uploaded_file(getattr(ev, "source_file_id", None))
            if case is not None
            else None
        )
        # display_name, not filename — see list_evidence_tool (#666).
        filename = file_meta.display_name if file_meta else None
        source_type = getattr(ev, "source_type", None)
        formatted.append(
            {
                "evidence_id": getattr(ev, "evidence_id", None),
                "filename": filename,
                "data_type": source_type.value if source_type else None,
                "coverage_start_ts": start.isoformat() if start else None,
                "coverage_end_ts": end.isoformat() if end else None,
                "summary": getattr(ev, "summary", None),
            }
        )
    return formatted
