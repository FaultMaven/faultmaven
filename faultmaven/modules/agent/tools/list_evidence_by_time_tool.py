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
from datetime import datetime, timezone
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
        results.extend(
            _format_unpromoted_files(context.in_memory_case, start_ts, end_ts)
        )
        # One ordering across both kinds — the note below promises it, and a
        # merged list sorted only within each half would quietly break that.
        results.sort(key=_coverage_sort_key)
        return ToolResult(
            success=True,
            data={
                "case_id": context.case_id,
                "start_ts": start_raw,
                "end_ts": end_raw,
                "count": len(results),
                "evidence": results,
                "note": (
                    "Ordered by coverage_start_ts ascending. Timeless items "
                    "(NULL coverage) are excluded. Rows carry either "
                    "evidence_id or file_id — a file_id row is an upload not "
                    "yet recorded as evidence, addressable by search_file just "
                    "the same. coverage_source says what produced the span: "
                    "caller_declared and the dated formats are stated instants, "
                    "while epoch_s/epoch_ms are bare-integer regex hits and "
                    "syslog_bsd_noyear carries a year the parser invented — "
                    "weigh those accordingly. A null coverage_source means the "
                    "provenance was never recorded, so the span is unverified: "
                    "treat it as unknown, not as confirmed."
                ),
            },
        )


def _coverage_sort_key(row: Dict[str, Any]) -> datetime:
    """Order both row kinds by when their content starts.

    Parsed rather than string-compared: PostgreSQL returns TZ-aware bounds and
    SQLite naive ones, so ``"2026-08-30T11:38:37"`` and
    ``"2026-08-30T11:38:37+00:00"`` are the same instant with different
    lengths, and lexical order puts them in the wrong sequence the moment a
    deployment holds both shapes. Naive is read as UTC — the same wire contract
    ``_parse_iso_bound`` documents. Unparseable or missing sorts first, where a
    row with no known start belongs.
    """
    raw = row.get("coverage_start_ts")
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_unpromoted_files(case: Any, start, end) -> List[Dict[str, Any]]:
    """Dated files that have no Evidence row yet, in the same window.

    INV-07 forbids Evidence creation during INQUIRY, so a forwarded alert is
    an ``<uploaded_file>`` for its whole first turn — and it is precisely the
    kind of thing "what was observed when" is asking about. Answering with
    silence, while the prompt shows that same file carrying
    ``observed_through``, teaches the model to distrust the attribute: tool
    output reads as the authoritative check.

    Keyed by ``file_id`` rather than ``evidence_id`` because that is what the
    row IS, and the sibling tools already accept either form (``search_file``
    resolves a ``file_id`` from an ``<uploaded_file>`` tag or an ``id`` from an
    ``<evidence>`` tag).
    """
    if case is None or not getattr(case, "uploaded_files", None):
        return []
    promoted = {
        str(ev.source_file_id)
        for ev in getattr(case, "evidence", None) or []
        if getattr(ev, "source_file_id", None) is not None
    }
    rows: List[Dict[str, Any]] = []
    for uf in case.uploaded_files:
        if uf.file_id is None or str(uf.file_id) in promoted:
            continue
        f_start = getattr(uf, "coverage_start_ts", None)
        f_end = getattr(uf, "coverage_end_ts", None)
        if f_start is None or f_end is None:
            continue  # timeless, same exclusion the repository query applies
        if start is not None and f_end < start:
            continue
        if end is not None and f_start > end:
            continue
        rows.append(
            {
                "file_id": uf.file_id,
                "label": uf.display_name,
                "data_type": uf.data_type,
                "coverage_start_ts": f_start.isoformat(),
                "coverage_end_ts": f_end.isoformat(),
                "coverage_source": getattr(uf, "coverage_source", None),
                "summary": uf.summary,
            }
        )
    return rows


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
                "label": filename,
                "data_type": source_type.value if source_type else None,
                "coverage_start_ts": start.isoformat() if start else None,
                "coverage_end_ts": end.isoformat() if end else None,
                "coverage_source": getattr(ev, "coverage_source", None),
                "summary": getattr(ev, "summary", None),
            }
        )
    return formatted
