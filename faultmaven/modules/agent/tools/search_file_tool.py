"""Search File Tool — Mechanical Search over Evidence Files

Provides two search modes over previously uploaded evidence files:

- keyword: Split query into keywords, find matching lines with context
- regex: Treat query as a regex pattern

Historical note: an ``extractor`` mode previously advertised "re-run the
domain extractor with different parameters". No extractor actually
accepted parameters, so every invocation silently degraded to a
defaults-only re-run while returning content already present in the
agent's ``<evidence>`` prompt block. Removed as part of plan
Cross-Cutting Hardening H2 — see
``docs/working/WIP-data-processing-improvement-plan.md``.

Design Reference: docs/architecture/data-processing/README.md
"""

import asyncio
import logging
import math
import re
import signal
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any

from faultmaven.infrastructure import worker_process
from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools import search_file_worker
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext, tool_failure
from faultmaven.modules.agent.tools.search_file_worker import merge_overlapping
from faultmaven.utils.exception_name import exception_log_name
from faultmaven.utils.host_port import HOST_PORT_RE

logger = logging.getLogger(__name__)

# Default search parameters
# Context lines: ±N lines around each match. Keep small — log entries are
# typically self-contained on a single line, and large context windows cause
# the TOOL_RESULT_MAX_CHARS truncation to cut off matches.
#
# Whether that is still true is now measurable rather than assumed (#1088):
# ``faultmaven_tool_result_truncated_total{tool="search_file"}`` over
# ``faultmaven_tool_result_relayed_total{tool="search_file"}`` is this
# constant's clip rate, and ``faultmaven_tool_result_chars{tool="search_file"}``
# is the size distribution it is holding down. Check them before widening this.
DEFAULT_CONTEXT_LINES = 3
DEFAULT_MAX_RESULTS = 20
# Ceiling for LLM-requested max_results (prevents unbounded memory use)
MAX_RESULTS_CEILING = 200

# The regex search runs in a ``search_file_worker`` per search, never here: the
# model writes the pattern (Principle 14, #2686). Measured 2026-11-26 in the
# worker over a 10 MiB file with a pattern matching every line (``""``), the
# costliest legitimate search, whose one merged window is the whole file: for
# excerpts, 140-character ASCII log lines took 0.37-0.40 s and 79 MiB of
# address space (an answer of 10.5 MiB); the same with one emoji, which makes
# every ``str`` of the text 4 bytes a character, 0.76 s and 159 MiB; lines of
# 40 astral characters (2.55 Mi characters) 0.41-0.59 s and 79 MiB (10.4 MiB);
# 8-character lines 2.76-2.79 s and 103 MiB (18.9 MiB); one-letter lines
# 9.8-14.1 s and 207 MiB (53.9 MiB). Count format, at most 6.1 s and 19 MiB.

#: Regex searches at once in each serving process, under ``FairSlots`` keyed by
#: the user who asked (#2645).
SEARCH_SLOTS = 2

#: How long a search waits for a slot before the model is told to retry.
SEARCH_SLOT_WAIT_SECONDS = 10.0

#: The work's CPU-time deadline (``ITIMER_PROF`` in the worker, as the runbook
#: gate's), so a pattern gets the same verdict under any load: 5x the slowest
#: search above but the one-letter lines, which can reach it. The model is told
#: its pattern did not finish within it.
SEARCH_WORK_CPU_SECONDS = 15.0

#: Each step's bound: spawn to ``ready`` (the worker imports ``re``, ``json``
#: and ``worker_process``: 0.07-0.09 s), each transfer (at most 30 MiB of UTF-8
#: at the 10 MB upload limit, 3 bytes a byte of the file), and the work's wall
#: backstop, the CPU deadline at half a core: past it the search is load's,
#: answered as unavailable, not the pattern's.
SEARCH_PHASES = worker_process.Phases(
    start=5.0, request=5.0, work=2 * SEARCH_WORK_CPU_SECONDS, result=5.0
)

#: ``RLIMIT_CPU`` past the CPU deadline: the imports and 1 s of slack, so the
#: deadline's timer ends a search first unless it failed.
SEARCH_CPU_SLACK_SECONDS = 2

#: ``RLIMIT_AS``'s start (the worker's own is under 20 MiB), beside its copies
#: of the text and the answer (``search_memory_limit``), each ``str`` at 4 bytes
#: a character: 188 MiB for the log lines with or without the emoji, 109 MiB
#: for the astral lines, 264 MiB for the 8-character lines and 544 MiB for the
#: one-letter lines, 1.2-2.6x each measurement above.
SEARCH_MEMORY_BASE_MIB = 64

#: What the parent reads past the result's texts: its head (at most
#: ``MAX_RESULTS_CEILING`` results' numbers) and the forwarded log records.
SEARCH_OUTPUT_BASE_BYTES = 1024 * 1024

#: The parent's hold of an answer per byte of its output limit (#3122): the
#: reader's 7, since what ``read_answer`` adds, the head's parse, is at most
#: ``MAX_RESULTS_CEILING`` results' numbers, while the parts, at most 4 bytes a
#: byte, are alive; the texts it re-seats are the parts themselves. No part
#: near the limit holds a lone surrogate, which would make the reader's 8: the
#: file text is decoded with ``errors="replace"``
#: (``_resolve_evidence_content``), and the invalid-pattern message is ``re``'s
#: error text (``search_file_worker._compile``, prefixed in ``regex_lines``),
#: which can carry raw characters of the pattern only in a fragment that does
#: not grow with the pattern; a quote that grows with it, a group name, is
#: ``repr``-escaped, so no lone surrogate. Measured on CPython 3.11.15: 7.0. What
#: ``_regex_in_worker`` does with the answer after its slot's release is
#: outside the term (#3128). An answer ``answer_parts`` cannot write is outside
#: it.
SEARCH_ANSWER_HOLD = worker_process.PHASED_ANSWER_HOLD

_MIB = 1024 * 1024

#: The worker module. Always this value: tests point it at one of their own.
_SEARCH_WORKER = search_file_worker.__name__

_search_slots_state: tuple | None = None


def _prefixes(lines: int) -> int:
    """Every line's number prefix (``"N: "``) and separator, at most."""
    return lines * (len(str(lines)) + 2)


def search_output_limit(text_bytes: int, lines: int) -> int:
    """What a search over ``text_bytes`` of UTF-8 in ``lines`` lines may
    answer: merged windows do not overlap, so each line at most once, with its
    number prefix; and the base."""
    return text_bytes + _prefixes(lines) + SEARCH_OUTPUT_BASE_BYTES


def search_memory_limit(text_bytes: int, chars: int, lines: int) -> int:
    """``RLIMIT_AS`` of a search over ``chars`` characters, ``text_bytes`` of
    UTF-8, in ``lines`` lines. Every ``str`` is sized at 4 bytes a character,
    as one astral character makes it (#3116): the text, held throughout; then
    the most of its UTF-8 as read, the answer's text twice as it is written
    (its writer and the value read out), or once beside the answer's two byte
    copies as it is sent."""
    answer_chars = chars + _prefixes(lines)
    answer_bytes = search_output_limit(text_bytes, lines)
    return (
        SEARCH_MEMORY_BASE_MIB * _MIB
        + 4 * chars
        + max(text_bytes, 2 * 4 * answer_chars, 4 * answer_chars + 2 * answer_bytes)
    )


@dataclass(frozen=True)
class ReadableFile:
    """One file of a case that the file tools can read.

    ``id`` is what the model passes: the ``evidence_id`` of a recorded row, or
    the ``file_id`` of an upload no row records (``recorded`` is False).
    """

    id: str
    filename: str
    recorded: bool


class IdOutcome(Enum):
    """What an id the model passes names in a case (#2375)."""

    NOT_FOUND = "not_found"  # no evidence row and no upload has this id
    ENGINE_INFERENCE = "engine_inference"  # a row with no source file
    MISSING_FILE_RECORD = "missing_file_record"  # a row whose upload is not loaded
    FOUND = "found"


@dataclass(frozen=True)
class ResolvedId:
    """``resolve_case_id``'s answer. ``row`` is the evidence row the id names,
    None for an upload passed by its ``file_id``; ``file`` is the upload, set
    only when FOUND."""

    outcome: IdOutcome
    row: Any = None
    file: Any = None


def resolve_case_id(case: Any, id_: str) -> ResolvedId:
    """Which file an id names in this case: decided here, once, for every file
    tool (#2375).

    An ``evidence_id`` first, then an upload's ``file_id`` (#1964). Whether the
    file can be read is each tool's own test (``has_stored_content`` for the
    readers, the preprocessing artifacts for ``vectorize_file``), so only
    NOT_FOUND may be answered "not found in this case".
    """
    for ev in getattr(case, "evidence", None) or []:
        if getattr(ev, "evidence_id", None) != id_:
            continue
        source_file_id = getattr(ev, "source_file_id", None)
        if source_file_id is None:
            return ResolvedId(IdOutcome.ENGINE_INFERENCE, row=ev)
        file_meta = case.find_uploaded_file(source_file_id)
        if file_meta is None:
            return ResolvedId(IdOutcome.MISSING_FILE_RECORD, row=ev)
        return ResolvedId(IdOutcome.FOUND, row=ev, file=file_meta)
    file_meta = case.find_uploaded_file(id_)
    if file_meta is None:
        return ResolvedId(IdOutcome.NOT_FOUND)
    return ResolvedId(IdOutcome.FOUND, file=file_meta)


def upload_without_its_row(case: Any, file_id: str, verb: str, unrecorded: str) -> str:
    """What a tool that acts on an evidence row answers for an upload's
    ``file_id`` (#2375): the case holds it, so never "not found".

    Names the row that records the upload when one does (first wins), else
    says no row records it yet, followed by the tool's own ``unrecorded``.
    """
    row = next(
        (
            ev
            for ev in getattr(case, "evidence", None) or []
            if getattr(ev, "source_file_id", None) == file_id
        ),
        None,
    )
    if row is not None:
        return (
            f"Upload {file_id} is recorded as evidence {row.evidence_id}: pass "
            f"that evidence_id to {verb} it."
        )
    return f"Upload {file_id} has no evidence row recording it yet{unrecorded}"


def enumerate_readable_files(
    all_evidence: list[Any],
    excluded_evidence_id: str,
    case: Any,
    *,
    excluded_file_id: str | None = None,
) -> list[ReadableFile]:
    """Every file in this case the file tools read: the one source of the list.

    #2009: ``search_file``, ``case_evidence_search`` and ``deep_analysis`` each
    phrase their miss answer from this list, so none can name a file another
    cannot read, or omit one it can.

    Recorded rows come first, by ``evidence_id`` (ISS-025: when the LLM picks a
    non-file-backed evidence_id, an ENGINE_INFERENCE row, the miss should point
    at the actual uploads). Then the uploads no evidence row records, by
    ``file_id``, which ``_resolve_evidence_content`` resolves too (#1964): a
    file uploaded during INQUIRY, a paste no row named, every upload of a case
    closed before any row existed. A terminal turn's prompt renders no ids, so
    this list is how the model learns them, and a file under the vectorization
    size floor is readable no other way. An upload with no stored file is left
    out of both lists, whether or not an evidence row records it: there is
    nothing to read, and naming it would send the model to a "no stored file
    content" miss. ``excluded_file_id`` leaves a file out under every id that
    names it, its rows' and its upload's: the file whose read just failed is
    not offered back under another id (#2359).
    """
    files: list[ReadableFile] = []
    recorded_files = set()
    for ev in all_evidence:
        source_file_id = getattr(ev, "source_file_id", None)
        if source_file_id is not None:
            recorded_files.add(source_file_id)
        ev_id = getattr(ev, "evidence_id", None)
        if not ev_id or ev_id == excluded_evidence_id:
            continue
        if source_file_id is None or source_file_id == excluded_file_id:
            continue
        file_meta = case.find_uploaded_file(source_file_id)
        if file_meta is None or not file_meta.has_stored_content:
            # No stored file (``storage_ref`` is None when the deployment has
            # no file storage): there is nothing to read, so the row is not
            # offered, the same rule the uploads below follow.
            continue
        # display_name, not filename: this goes back to the LLM and a minted
        # pasted-content-<ts>.txt gets cited from there (#666).
        files.append(ReadableFile(ev_id, file_meta.display_name or "(unnamed)", True))

    for upload in getattr(case, "uploaded_files", None) or []:
        file_id = getattr(upload, "file_id", None)
        if (
            not file_id
            or file_id in (excluded_evidence_id, excluded_file_id)
            or file_id in recorded_files
            or not upload.has_stored_content
        ):
            continue
        files.append(ReadableFile(file_id, upload.display_name or "(unnamed)", False))
    return files


def format_searchable_alternatives(
    all_evidence: list[Any],
    excluded_evidence_id: str,
    case: Any,
    *,
    excluded_file_id: str | None = None,
) -> str:
    """Build a redirect hint naming every file in this case this tool reads.

    Phrased for ``search_file`` from ``enumerate_readable_files``. Module-level
    because ``case_evidence_search`` points at the same list when its search
    finds nothing (#1977); that caller excludes no id (``""``). Returns ``""``
    when the case has no file to read.
    """
    files = enumerate_readable_files(
        all_evidence, excluded_evidence_id, case, excluded_file_id=excluded_file_id
    )
    alternatives = [f"{f.id} ({f.filename})" for f in files if f.recorded]
    unrecorded = [f"{f.id} ({f.filename})" for f in files if not f.recorded]

    hint = ""
    if alternatives:
        hint += (
            " Available file-backed evidence in this case: "
            + ", ".join(alternatives)
            + "."
        )
    if unrecorded:
        hint += (
            " Uploaded files with no evidence row (search them by file_id): "
            + ", ".join(unrecorded)
            + "."
        )
    return hint


def _search_error(error: str) -> ToolResult:
    """A regex search the worker did not answer: never a timeout (#3116)."""
    return ToolResult(success=False, data=None, error=error, timed_out=False)


class SearchFileTool(AgentTool):
    """Mechanical search over raw evidence file content.

    Allows the agent to search previously uploaded evidence files using
    keyword matching or regex patterns. Used within Directed Analysis as
    a supplementary tool for exact keyword/regex/timestamp matching.
    """

    def __init__(
        self,
        storage_service: Any = None,
        context_lines: int = DEFAULT_CONTEXT_LINES,
        max_results: int = DEFAULT_MAX_RESULTS,
    ):
        self.storage_service = storage_service
        self.context_lines = context_lines
        self.max_results = max_results

    @property
    def name(self) -> str:
        return "search_file"

    @property
    def description(self) -> str:
        # True on every turn (#1964): a terminal turn's prompt renders no
        # evidence rows and no uploads, so the description promises neither;
        # the ids reach the model through a miss's list of readable files.
        return (
            "Search one of this case's uploaded files for specific information. "
            "Pass the evidence_id of a file-backed evidence row, or the file_id "
            "of an upload; a call with an id that is not on this case returns "
            "the list of the files this tool can read, with their ids. "
            "Two output formats: 'excerpts' (default) returns context windows for "
            "detailed investigation; 'count' returns compact matched lines for "
            "counting and aggregation queries (e.g. 'how many unique IPs?'). "
            "Results carry a 'label' — the file's name, the same label the "
            "context gives it where it lists the file. Cite it verbatim; not "
            "every item is a file the user named."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "evidence_id": {
                    "type": "string",
                    "description": (
                        "The evidence_id of a file-backed evidence row, or the "
                        "file_id of an uploaded file"
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Search query: keywords for keyword mode, "
                        "regex pattern for regex mode"
                    ),
                },
                "search_type": {
                    "type": "string",
                    "enum": ["keyword", "regex"],
                    "description": (
                        "Search mode: 'keyword' splits query into keywords and finds matching lines, "
                        "'regex' treats query as a regex pattern"
                    ),
                },
                "output_format": {
                    "type": "string",
                    "enum": ["excerpts", "count"],
                    "description": (
                        "Output format. 'excerpts' (default): context windows with "
                        "surrounding lines for detailed investigation. "
                        "'count': compact matched lines only (no context) for "
                        "counting and aggregation queries. Use 'count' when you "
                        "need to know how many matches exist or enumerate all values."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        "Maximum number of results to return (up to 200). "
                        "Default: 20 for excerpts, 200 for count mode."
                    ),
                },
            },
            "required": ["evidence_id", "query"],
        }

    async def execute_with_context(
        self,
        params: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute file search."""
        evidence_id = params.get("evidence_id")
        query = params.get("query", "")
        search_type = params.get("search_type", "keyword")
        output_format = params.get("output_format", "excerpts")

        # Resolve effective max_results: LLM override → format default → instance default
        requested_max = params.get("max_results")
        if requested_max is not None:
            # At least 1: a zero or negative cap sliced off the end (#3116).
            effective_max_results = max(1, min(int(requested_max), MAX_RESULTS_CEILING))
        elif output_format == "count":
            # Count mode defaults to ceiling — completeness over detail
            effective_max_results = MAX_RESULTS_CEILING
        else:
            effective_max_results = self.max_results

        if not evidence_id:
            return ToolResult(success=False, data=None, error="evidence_id is required")

        if not query:
            return ToolResult(success=False, data=None, error="query is required")

        try:
            # Resolve evidence content from case.evidence (storage redesign
            # 2026-04 phase 2: standalone evidence path deleted)
            resolved = await self._resolve_evidence_content(
                evidence_id,
                context,
            )
            # ISS-025: helper now returns either a
            # (content, filename, display_name, row) tuple on success (the row
            # is the evidence row, or the upload a file_id names) or a
            # human-readable error string on failure that already enumerates
            # file-backed alternatives the LLM should retry against. Surface
            # that string verbatim so the LLM can redirect.
            #
            # #666: two names, deliberately. ``filename`` is the STORED name
            # and is what the operator log records, so a log line still
            # correlates to a blob on disk and several pastes stay apart in
            # it. ``display_name`` is what goes in the result the LLM reads.
            if isinstance(resolved, str):
                return ToolResult(success=False, data=None, error=resolved)
            if isinstance(resolved, ToolResult):
                return resolved
            content, filename, display_name, evidence_obj = resolved

            # Phase 2c — triage-to-escalation counter. Fires when this
            # tool call targets an evidence whose structural_index was
            # delivered in the same turn (collected_at_turn == current
            # turn). A high rate per data_type signals the extractor's
            # structural index is leaving the agent uninformed.
            try:
                from faultmaven.infrastructure.observability.evidence_metrics import (
                    record_triage_escalation_if_same_turn,
                )

                record_triage_escalation_if_same_turn(
                    evidence=evidence_obj,
                    case=getattr(context, "in_memory_case", None),
                    tool_name="search_file",
                )
            except Exception:
                # Telemetry is best-effort — never fail the tool for it.
                pass

            # The model's pattern runs in its worker (#2686); keyword search, a
            # substring test the system wrote, stays here. Either gives the
            # capped results and their total.
            if search_type == "regex":
                searched = await self._regex_in_worker(
                    content, query, output_format, effective_max_results, context
                )
                if isinstance(searched, ToolResult):
                    return searched
            elif output_format == "count":
                found = self._count_search(content, query)
                searched = found[:effective_max_results], len(found)
            else:
                found = self._keyword_search(content, query)
                searched = found[:effective_max_results], len(found)

            # Route by output format
            if output_format == "count":
                return self._execute_count_format(
                    content,
                    filename,
                    display_name,
                    evidence_id,
                    query,
                    search_type,
                    effective_max_results,
                    searched,
                )

            # --- Excerpts format (default) ---
            results, total_matches = searched
            truncated = total_matches > effective_max_results

            logger.info(
                "search_file: %s (%s), mode=%s, format=excerpts, query_len=%d, "
                "results=%d (total=%d, truncated=%s)",
                evidence_id,
                filename,
                search_type,
                len(query),
                len(results),
                total_matches,
                truncated,
            )

            if not results:
                vocabulary = self._extract_file_vocabulary(content)
                top_terms = (
                    vocabulary.get("patterns", [])
                    + vocabulary.get("frequent_tokens", [])
                )[:10]
                return ToolResult(
                    success=True,
                    data={
                        "evidence_id": evidence_id,
                        "label": display_name,
                        "search_type": search_type,
                        "query": query,
                        "output_format": "excerpts",
                        "results_count": 0,
                        "total_matches": 0,
                        "truncated": False,
                        "results": [],
                        "vocabulary": vocabulary,
                        "suggestion": (
                            f"No matches found. File contains these terms: "
                            f"{', '.join(top_terms)}"
                            if top_terms
                            else "No matches found and no recognizable terms extracted."
                        ),
                    },
                )

            data = {
                "evidence_id": evidence_id,
                "label": display_name,
                "search_type": search_type,
                "query": query,
                "output_format": "excerpts",
                "results_count": len(results),
                "total_matches": total_matches,
                "truncated": truncated,
                "results": results,
            }
            if truncated:
                data["truncation_note"] = (
                    f"Showing {len(results)} of {total_matches} matches. "
                    "Results are capped — use output_format='count' for "
                    "counting/aggregation queries."
                )

            return ToolResult(success=True, data=data)

        except Exception as e:
            logger.exception(
                "search_file failed for %s: %s", evidence_id, exception_log_name(e)
            )
            return tool_failure("Search failed", e)

    def _execute_count_format(
        self,
        content: str,
        filename: str,
        display_name: str,
        evidence_id: str,
        query: str,
        search_type: str,
        effective_max_results: int,
        searched: tuple[list[dict[str, Any]], int],
    ) -> ToolResult:
        """Count format: compact matched lines for counting/aggregation queries.

        Returns matched lines as "LINE: CONTENT" strings — no context windows.
        ~7x more compact than excerpts, fits far more results within the
        milestone engine's TOOL_RESULT_MAX_CHARS budget.

        ``filename`` is the stored name (operator log); ``display_name`` is
        what the LLM-facing result reports (#666). ``searched`` is the capped
        matched lines and their total.
        """
        returned, match_count = searched
        truncated = match_count > effective_max_results

        logger.info(
            "search_file: %s (%s), mode=%s, format=count, query_len=%d, "
            "match_count=%d (returned=%d, truncated=%s)",
            evidence_id,
            filename,
            search_type,
            len(query),
            match_count,
            len(returned),
            truncated,
        )

        if not returned:
            vocabulary = self._extract_file_vocabulary(content)
            top_terms = (
                vocabulary.get("patterns", []) + vocabulary.get("frequent_tokens", [])
            )[:10]
            return ToolResult(
                success=True,
                data={
                    "evidence_id": evidence_id,
                    "label": display_name,
                    "search_type": search_type,
                    "query": query,
                    "output_format": "count",
                    "match_count": 0,
                    "results_count": 0,
                    "truncated": False,
                    "matched_lines": [],
                    "vocabulary": vocabulary,
                    "suggestion": (
                        f"No matches found. File contains these terms: "
                        f"{', '.join(top_terms)}"
                        if top_terms
                        else "No matches found and no recognizable terms extracted."
                    ),
                },
            )

        # Format as compact "LINE: CONTENT" strings
        matched_lines = [f"{m['line']}: {m['content']}" for m in returned]

        data: dict[str, Any] = {
            "evidence_id": evidence_id,
            "label": display_name,
            "search_type": search_type,
            "query": query,
            "output_format": "count",
            "match_count": match_count,
            "results_count": len(returned),
            "truncated": truncated,
            "matched_lines": matched_lines,
        }
        if truncated:
            data["truncation_note"] = (
                f"Showing {len(returned)} of {match_count} matched lines."
            )

        return ToolResult(success=True, data=data)

    def _count_search(self, content: str, query: str) -> list[dict[str, Any]]:
        """Compact keyword search: returns matched lines only, no context
        windows, as {"line": N, "content": "..."} dicts.
        """
        lines = content.split("\n")
        keywords = [kw.lower() for kw in query.split() if len(kw) > 2]
        if not keywords:
            return []
        return [
            {"line": i + 1, "content": line}
            for i, line in enumerate(lines)
            if all(kw in line.lower() for kw in keywords)
        ]

    async def _resolve_evidence_content(
        self,
        evidence_id: str,
        context: ToolContext,
    ) -> tuple[str, str, str, Any] | str | ToolResult:
        """Resolve evidence content from the case-embedded evidence list.

        Storage redesign 2026-04 phase 2 deleted the standalone
        `evidence_artifacts` table and its service. Evidence is case-tied only:
        the unified ingestion pipeline creates Evidence objects embedded in the
        Case (`case.evidence`) with `content_ref` pointing to the stored file.

        Returns:
            On success: (content_str, stored_filename, display_name, row), the
                row being the evidence row, or the upload a ``file_id`` names.
                The two names differ only for pasted/captured content — see
                ``UploadedFile.display_name`` (#666). Logs take the stored
                one, the LLM-facing result takes the display one.
            On failure: a human-readable error string. When other file-backed
            evidence exists in the case, the error enumerates those evidence_ids
            so the LLM can retry against the correct one (ISS-025). A failed
            storage read is a ``tool_failure`` result instead, which records
            whether the read timed out (#2332).
        """
        case = getattr(context, "in_memory_case", None)
        if case is None:
            case_repo = getattr(context, "case_repository", None)
            if case_repo is None:
                logger.warning("search_file: no case_repository on tool context")
                return "Tool context error: no case_repository available."
            case = await case_repo.get(context.case_id)
            if case is None:
                logger.warning("search_file: case %s not found", context.case_id)
                return f"Case {context.case_id} not found."

        all_evidence = getattr(case, "evidence", []) or []

        # An evidence row, or an upload's file_id (#1964): a file uploaded
        # during INQUIRY, before any row exists, a paste no row named, or an
        # upload passed by its file_id although a row records it.
        resolved_id = resolve_case_id(case, evidence_id)
        if resolved_id.outcome is IdOutcome.NOT_FOUND:
            logger.warning(
                "search_file: evidence %s not found in case %s (has %d evidence items)",
                evidence_id,
                context.case_id,
                len(all_evidence),
            )
            alts = format_searchable_alternatives(all_evidence, evidence_id, case)
            return f"Evidence {evidence_id} not found in this case.{alts}"
        if resolved_id.outcome is IdOutcome.ENGINE_INFERENCE:
            # Reject non-file-backed evidence forms BEFORE hitting storage.
            # An engine inference (source_file_id IS NULL,
            # source_type=ENGINE_INFERENCE) has no stored file behind it
            # and cannot be searched. ISS-025: previously the LLM retried
            # these against search_file and treated the failure as "the
            # file is inaccessible", giving up on follow-up questions.
            alts = format_searchable_alternatives(all_evidence, evidence_id, case)
            logger.warning(
                "search_file: evidence %s has no source file (engine inference); "
                "pointing LLM at file-backed alternatives",
                evidence_id,
            )
            return (
                f"Evidence {evidence_id} has no stored file behind it (it's an "
                f"inference the engine recorded). Use a file-backed "
                f'evidence_id (file_backed="true") for this search.{alts}'
            )
        # A matched upload takes the read below as a row does, so a failed read
        # says what failed, never "not found" (#2359). MISSING_FILE_RECORD has
        # no file, and answers as a file with nothing stored.
        file_meta = resolved_id.file
        resolved_row = resolved_id.row if resolved_id.row is not None else file_meta

        # Read raw file via UploadedFile.storage_ref
        if file_meta is None or not file_meta.has_stored_content:
            logger.warning(
                "search_file: evidence %s has no backing file (file not stored)",
                evidence_id,
            )
            alts = format_searchable_alternatives(all_evidence, evidence_id, case)
            return f"Evidence {evidence_id} has no stored file content.{alts}"
        storage_ref = file_meta.storage_ref

        if self.storage_service is not None and hasattr(
            self.storage_service, "retrieve_file"
        ):
            try:
                file_data = await self.storage_service.retrieve_file(storage_ref)
                filename = file_meta.filename or evidence_id
                display_name = file_meta.display_name or evidence_id
                content = file_data.decode("utf-8", errors="replace")
                return content, filename, display_name, resolved_row
            except Exception as e:
                logger.warning(
                    "search_file: storage_service.retrieve_file failed for "
                    "evidence %s (storage_ref=%s): %s",
                    evidence_id,
                    storage_ref,
                    exception_log_name(e),
                )
                alts = format_searchable_alternatives(
                    all_evidence, evidence_id, case, excluded_file_id=file_meta.file_id
                )
                return tool_failure(
                    f"Evidence {evidence_id}: stored file could not be retrieved",
                    e,
                    guidance=alts.strip(),
                )

        logger.warning(
            "search_file: storage_service unavailable; cannot read evidence %s "
            "(storage_ref=%s)",
            evidence_id,
            storage_ref,
        )
        return f"Evidence {evidence_id}: storage service unavailable."

    def _keyword_search(self, content: str, query: str) -> list[dict[str, Any]]:
        """Keyword search: tokenize query, find matching lines, return context windows.

        Two-pass strategy:
        1. Find lines matching ALL keywords (high relevance).
        2. If no all-keyword matches and multiple keywords exist, fall back to
           individual keyword matching (lower cap, marked partial_match).
        """
        lines = content.split("\n")
        keywords = [kw.lower() for kw in query.split() if len(kw) > 2]

        if not keywords:
            return []

        # Pass 1: lines matching ALL keywords
        matches: list[dict] = []
        for i, line in enumerate(lines):
            line_lower = line.lower()
            matched_keywords = [kw for kw in keywords if kw in line_lower]
            if len(matched_keywords) == len(keywords):
                start = max(0, i - self.context_lines)
                end = min(len(lines), i + self.context_lines + 1)
                matches.append(
                    {
                        "excerpt": "\n".join(
                            f"{j + 1}: {lines[j]}" for j in range(start, end)
                        ),
                        "line_start": start + 1,
                        "line_end": end,
                        "matched_keywords": matched_keywords,
                        "relevance": 1.0,
                    }
                )

        merged = merge_overlapping(matches, lines)
        if merged:
            return merged

        # Pass 2: partial match fallback — individual keywords
        if len(keywords) > 1:
            partial_matches: list[dict] = []
            for kw in keywords:
                for i, line in enumerate(lines):
                    if kw in line.lower():
                        start = max(0, i - self.context_lines)
                        end = min(len(lines), i + self.context_lines + 1)
                        partial_matches.append(
                            {
                                "excerpt": "\n".join(
                                    f"{j + 1}: {lines[j]}" for j in range(start, end)
                                ),
                                "line_start": start + 1,
                                "line_end": end,
                                "matched_keywords": [kw],
                                "relevance": 1 / len(keywords),
                                "partial_match": True,
                            }
                        )
            return merge_overlapping(partial_matches, lines)

        return []

    async def _regex_in_worker(
        self,
        content: str,
        pattern: str,
        output_format: str,
        max_results: int,
        context: ToolContext,
    ) -> tuple[list[dict[str, Any]], int] | ToolResult:
        """The model's ``pattern`` over ``content`` in a ``search_file_worker``
        (Principle 14, #2686), under a slot keyed by the user who asked: the
        capped results and their total, or the error the model reads. No error
        is a timeout for the engine (``timed_out`` stays false): a pattern past
        its bound is not answered by indexing the file (#3116)."""
        global _search_slots_state
        _search_slots_state = worker_process.loop_slots(
            _search_slots_state, SEARCH_SLOTS
        )
        slots = _search_slots_state[1]
        slot_key = worker_process.admission_principal(context.user_id)
        try:
            async with asyncio.timeout(SEARCH_SLOT_WAIT_SECONDS):
                await slots.acquire(*slot_key)
        except TimeoutError:
            logger.warning("search_file: no regex search slot freed within the wait")
            return _search_error(
                "Every regex search slot is busy; retry the search shortly, or "
                "use a keyword search."
            )
        lines = content.count("\n") + 1
        text_bytes = len(content.encode("utf-8", "surrogatepass"))
        try:
            end = await worker_process.run(
                _SEARCH_WORKER,
                {
                    "pattern": pattern,
                    "format": output_format,
                    "context_lines": self.context_lines,
                    "max_results": max_results,
                    "work_cpu_seconds": SEARCH_WORK_CPU_SECONDS,
                },
                memory_bytes=search_memory_limit(text_bytes, len(content), lines),
                cpu_seconds=math.ceil(SEARCH_WORK_CPU_SECONDS)
                + SEARCH_CPU_SLACK_SECONDS,
                output_limit_bytes=search_output_limit(text_bytes, lines),
                phases=SEARCH_PHASES,
                parts=(content,),
            )
            if end.result is not None:
                return search_file_worker.read_answer(end.result)
        except worker_process.WorkerOutputExceeded:
            end = worker_process.WorkerEnd(code=worker_process.OUT_OF_MEMORY_EXIT)
        except (worker_process.WorkerFailed, ValueError, KeyError, StopIteration) as e:
            logger.error("search_file: regex worker failed: %s", exception_log_name(e))
            end = worker_process.WorkerEnd()
        finally:
            slots.release(*slot_key)
        if end.code in (-signal.SIGPROF, -signal.SIGXCPU):
            # Its CPU deadline, or RLIMIT_CPU if the timer failed: the
            # pattern's cost alone, whatever the load.
            logger.warning("search_file: a regex search ran past its CPU deadline")
            return _search_error(
                f"The pattern did not finish within {SEARCH_WORK_CPU_SECONDS:g} "
                "seconds of CPU over this file; use a simpler pattern (no nested "
                "quantifiers) or a keyword search."
            )
        if end.code == worker_process.OUT_OF_MEMORY_EXIT:
            logger.warning("search_file: a regex search passed its memory or output")
            return _search_error(
                "The pattern matched more of this file than one search can hold; "
                "use a narrower pattern or a keyword search."
            )
        logger.error(
            "search_file: regex worker gave no answer: ended in %s, exit %s, "
            "timed out %s",
            end.phase,
            end.code,
            end.timed_out,
        )
        return _search_error(
            "Regex search is unavailable right now; use a keyword search."
        )

    # --- Vocabulary extraction (for zero-result recovery) ---

    # The scan reads at most this many characters from the start of the file.
    _VOCAB_SCAN_CHARS = 102_400

    # Compiled once at class level
    _VOCAB_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        ("http_errors", re.compile(r"\b[45]\d{2}\b")),
        (
            "exceptions",
            re.compile(r"\b[A-Z][a-zA-Z]*(?:Error|Exception|Failure|Fault|Timeout)\b"),
        ),
        ("host_port", HOST_PORT_RE),
        ("ip_addresses", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
        ("file_paths", re.compile(r"/[\w/.-]+")),
    ]

    _STOP_WORDS = frozenset(
        {
            "the",
            "and",
            "for",
            "are",
            "but",
            "not",
            "you",
            "all",
            "can",
            "had",
            "her",
            "was",
            "one",
            "our",
            "out",
            "has",
            "have",
            "from",
            "with",
            "they",
            "been",
            "said",
            "each",
            "which",
            "their",
            "will",
            "other",
            "about",
            "many",
            "then",
            "them",
            "these",
            "some",
            "would",
            "make",
            "like",
            "into",
            "time",
            "very",
            "when",
            "come",
            "could",
            "more",
            "than",
            "first",
            "also",
            "its",
            "over",
            "such",
            "after",
            "this",
            "that",
            "what",
            "there",
            "where",
            "just",
            "most",
            "only",
            # Log noise
            "info",
            "debug",
            "warn",
            "warning",
            "error",
            "trace",
            "level",
            "timestamp",
            "date",
            "log",
            "logger",
            "message",
            "msg",
            "null",
            "none",
            "true",
            "false",
            "undefined",
        }
    )

    def _extract_file_vocabulary(self, content: str) -> dict[str, list[str]]:
        """Extract vocabulary from file content for zero-result recovery.

        Three-pass heuristic:
        1. Known patterns (HTTP errors, exceptions, IPs, paths)
        2. Frequent tokens (statistical)
        3. Structural hints (from preprocessed content if available)
        """
        sample = content[: self._VOCAB_SCAN_CHARS]

        result: dict[str, list[str]] = {"patterns": [], "frequent_tokens": []}

        # Pass 1: Known patterns, at most 30; once full, no later pattern runs
        seen: set[str] = set()
        for _label, pattern in self._VOCAB_PATTERNS:
            if len(result["patterns"]) >= 30:
                break
            for match in pattern.finditer(sample):
                val = match.group(0)
                if val not in seen:
                    seen.add(val)
                    result["patterns"].append(val)
                    if len(result["patterns"]) >= 30:
                        break

        # Pass 2: Frequent tokens
        tokens = re.split(r"[\s=:,;|\[\]{}()\"\'+]+", sample)
        counts: Counter[str] = Counter()
        for tok in tokens:
            tok_lower = tok.lower().strip(".-_/")
            if (
                len(tok_lower) > 2
                and not tok_lower.isdigit()
                and tok_lower not in self._STOP_WORDS
                and tok_lower.isalnum()
            ):
                counts[tok_lower] += 1

        # Tokens appearing 2-10 times (not too rare, not too common)
        frequent = [tok for tok, count in counts.most_common(50) if 2 <= count <= 10][
            :20
        ]
        result["frequent_tokens"] = frequent

        return result
