"""Turn-record bookkeeping for routes the milestone engine never sees.

Owns the backstop that gives a SERVICE-dispatched turn (greeting,
file-reclassification, out-of-band) the same ``TurnProgress`` accounting the
engine's own Step 6 would have written, the KB pre-fetch citation rendering,
and the ``UploadedFile.data_type`` <-> ``EvidenceSourceType`` fold shared by
the published attachment result and the reclassification metric's label.
"""

from typing import Any

from faultmaven.core.investigation.kb_push import TURN_METADATA_KB_PROMPTED
from faultmaven.core.investigation.milestone_engine.progress import (
    record_promptless_turn,
    score_progress,
    summarize_for_turn_record,
)
from faultmaven.core.preprocessing.models import unified_data_type_of
from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_MARK_LINKED_FAILURES_TOTAL,
)
from faultmaven.models.api import DataType, Source, SourceType
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    MESSAGE_METADATA_KB_SOURCES,
    Case,
    TurnOutcome,
)
from faultmaven.modules.case.domain.models.evidence import (
    EvidenceSourceType,
    UploadedFile,
)

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)


def _record_mark_linked_failure(outcome: str) -> None:
    """Count a sidecar mark_linked failure without ever failing the upload.

    The metric is an observability side-channel on an already-persisted upload;
    a broken/absent Prometheus registry must not turn a successful turn into a
    500. Same swallow-everything posture the sweep's counters use.
    """
    try:
        EVIDENCE_MARK_LINKED_FAILURES_TOTAL.labels(outcome=outcome).inc()
    except Exception:  # pragma: no cover - metrics must never break a turn
        pass


def _backfill_consumed_turn(
    case: "Case",
    *,
    user_message: str,
    agent_response: str,
    metadata: dict[str, Any],
) -> None:
    """Record a ``TurnProgress`` for a consumed turn that recorded none (#1264).

    A no-op whenever the turn already has an entry, which is every route that
    reaches the milestone engine's turn bookkeeping. It fires for the three that
    do not: ``GREETING`` and ``FILE_RECLASSIFICATION`` (answered in this service,
    never calling the engine) and the engine's terminal short-circuit, which
    returns before Step 6.

    Keyed on the LAST entry's number rather than on membership, because
    ``turn_history`` is maintained strictly consecutive by
    ``Case.reconcile_turn_sequence`` — so "the tail is not this turn" is exactly
    "this turn was not recorded", and the check stays O(1) on a list that grows
    with the case.

    **The record is a real one, not a placeholder.** ``turn_history`` is not
    only a counter: ``prompts/context_builder`` renders it as the prompt's
    EARLIER TURNS block and reads ``[-1].system_feedback``, and
    ``working_conclusion_generator`` / ``progress_monitor`` window it for
    momentum and loop detection. A minimal entry is therefore not "honest but
    small" — it actively destroys what those readers need:

    * ``progress_made`` comes from :func:`score_progress` — the same predicate
      the engine's own deterministic branches use, applied with the same
      monotone write, NOT a hardcoded ``False``. The turns route accepts an
      intent alongside files, so a clarification click can carry a genuinely
      novel upload — and ``_finish_deterministic_turn`` is explicit that such an
      upload counts. Hardcoding False would report an inert turn on one where
      the user supplied new data, and leave the stall counter climbing through
      it.

      The score is written BACK onto *metadata* (#1270), not merely consumed
      here. All three backstopped routes hand over a dict carrying a hardcoded
      ``progress_made: False`` beside the turn's upload keys, and that same dict
      is what the #1142 telemetry row, ``TurnResponse.progress_made`` and the
      persisted assistant ``case_messages`` row all read. Scoring without
      writing back left them reporting ``progress_made: false`` beside
      ``arms.novel_files_uploaded: 1`` and ``turns_without_progress: 0`` — one
      turn, one decision, three surfaces disagreeing. (The GREETING handler's
      "the flag says False and the counter is unchanged, which agree" was true
      when written; #1264 made this function touch the counter and left the flag
      behind.) The early return above is what keeps the write off every route
      that reached the engine's Step 6, where ``progress_made`` is already
      authoritative.
    * the summaries carry the real text. ``_build_graduated_history`` renders
      from the record when one exists and falls back to the message text only
      when it is MISSING, so an empty record does not leave the text alone — it
      REPLACES it with "User message → conversation".
    * ``system_feedback`` is FORWARDED from the previous turn. It is read off
      ``turn_history[-1]`` and is meant for the next prompt; these routes build
      no prompt, so they have not consumed it. Dropping it would silently
      swallow a reasoning-validation error whenever a greeting landed between
      two engine turns. The rule lives in :func:`record_promptless_turn`, the
      builder this shares with the engine's deterministic branches, so it
      cannot hold in one copy and not the other (#1688).
    """
    if case.turn_history and case.turn_history[-1].turn_number == case.current_turn:
        return

    record_promptless_turn(
        case,
        user_message=user_message,
        agent_response=agent_response,
        progress_made=score_progress(metadata),
        milestones_completed=metadata.get("milestones_completed"),
        outcome=metadata.get("outcome") or TurnOutcome.CONVERSATION,
        # #1451: the engine's terminal short-circuit reports a placeholder it
        # synthesized on the metadata; a blank answer is about to be replaced
        # by this service's own backstop marker. Either way the summary is not
        # something the agent said.
        agent_response_synthesized=(
            bool(metadata.get(MESSAGE_METADATA_AGENT_SYNTHESIZED))
            or not (agent_response or "").strip()
        ),
    )


def _record_composed_reply(case: "Case", agent_response: str) -> None:
    """Re-derive this turn's record when the service composed its reply onto
    a blank answer (#1660).

    The clarification note is appended AFTER the turn is recorded — by the
    engine's Step 6, or by :func:`_backfill_consumed_turn` above — and both
    record a blank answer as unanswered (``agent_response_synthesized``). The
    note then makes the reply non-blank, so the persistence backstop never
    fires and the row goes out unflagged. One turn, two verdicts: the EARLIER
    TURNS summary said "no answer" while the RECENT window quoted the note.

    Settled the way the engine settles its own composition (#1442): prose
    composed onto a missing answer is a real reply, not a placeholder. The note
    is the question the user was actually shown, and the next turn needs it —
    "treat it as application logs" answers it. So the record follows the row,
    and is re-derived from the text rather than carried over, exactly as the
    engine re-records after composing a gate notice.

    Only called for a blank base the engine did not flag. A placeholder the
    engine synthesized is not blank; that row stays flagged and so does its
    record.
    """
    if not case.turn_history or case.turn_history[-1].turn_number != case.current_turn:
        return
    # ``TurnProgress`` is frozen: replace the record, never mutate it.
    case.turn_history[-1] = case.turn_history[-1].model_copy(
        update={
            "agent_response_summary": summarize_for_turn_record(
                agent_response.strip(), 500
            ),
            "agent_response_synthesized": not agent_response.strip(),
        }
    )


_DATA_TYPE_TO_SOURCE_TYPE: dict[DataType, EvidenceSourceType] = {
    DataType.LOGS_AND_ERRORS: EvidenceSourceType.LOGS,
    DataType.ERROR_REPORT: EvidenceSourceType.LOGS,
    DataType.COMMAND_OUTPUT: EvidenceSourceType.LOGS,
    DataType.TRACE_DATA: EvidenceSourceType.LOGS,
    DataType.METRICS_AND_PERFORMANCE: EvidenceSourceType.METRICS,
    DataType.PROFILING_DATA: EvidenceSourceType.METRICS,
    DataType.STRUCTURED_CONFIG: EvidenceSourceType.CONFIGURATION,
    DataType.SOURCE_CODE: EvidenceSourceType.CODE,
    DataType.DOCUMENTATION: EvidenceSourceType.TEXT,
    DataType.UNSTRUCTURED_TEXT: EvidenceSourceType.TEXT,
    DataType.VISUAL_EVIDENCE: EvidenceSourceType.IMAGE,
    DataType.UNANALYZABLE: EvidenceSourceType.TEXT,
}


def _kb_sources(entries: list[dict]) -> list[Source]:
    """Render pre-fetched runbook entries as citable ``Source`` entries.

    ``entries`` is what a turn's prompt rendered, captured by the engine before
    generation (``TURN_METADATA_KB_PROMPTED``, selected by
    ``prompt_kb_entries``, which applies the push gate of fm#1360). Never
    ``case.kb_context`` read after the turn: a pre-fetch fired while the
    response is applied writes context the answer never saw, and citing a
    runbook the model was not shown is worse than citing none.

    ``Source`` was never constructed anywhere before fm#1361, which is why the
    Copilot's citation components were unreachable. Only ``knowledge_base`` is
    emitted here; the published enum admits five more values (see the 3.3.0
    entry in ``api/contract_version.py``).

    ``content`` carries the matched EXCERPT rather than the title: the client
    shows a content preview and reads the title from ``metadata``.
    ``confidence`` is the retrieval score, on the same cosine scale the
    pre-fetch floors with.

    Defensive about entry shape because ``kb_context`` round-trips through a
    JSON blob: a row written by an older build is a plain dict of whatever it
    happened to hold, and a citation list must never be the thing that fails a
    turn that otherwise succeeded.
    """
    sources: list[Source] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        metadata = {
            "document_id": entry.get("parent_document_id"),
            "title": entry.get("title"),
            # Which pre-fetch fired — "symptom" (the problem statement) or
            # "root_cause" (remediation). A reader measuring retrieval quality
            # needs to know which query produced the hit.
            "trigger": entry.get("trigger"),
        }
        score = entry.get("score")
        sources.append(
            Source(
                type=SourceType.KNOWLEDGE_BASE,
                content=str(entry.get("summary") or entry.get("title") or ""),
                confidence=float(score) if isinstance(score, (int, float)) else None,
                metadata=metadata,
            )
        )
    return sources


def _source_key(source: dict) -> tuple[str, str]:
    """One runbook excerpt: its document and the matched text."""
    metadata = source.get("metadata") or {}
    return (str(metadata.get("document_id") or ""), str(source.get("content") or ""))


def _record_turn_kb_sources(turn_meta: dict, case: Any) -> None:
    """Build this turn's ``sources`` from what its prompt rendered, for the row.

    Takes the engine's raw capture (``TURN_METADATA_KB_PROMPTED``) out of
    ``turn_meta`` and writes the published form under
    ``MESSAGE_METADATA_KB_SOURCES``, which the assistant row persists and the
    turn response reads. A turn whose prompt carried no KB context (no engine
    run, the push off, nothing fetched) records nothing.

    ``new_this_turn`` is decided against the most recent EARLIER assistant row
    that recorded sources, not against a turn number: the context stands in
    every prompt until a pre-fetch replaces it, and a re-fetch can return the
    same runbooks. Comparing persisted rows keeps the answer right across a
    turn that failed before its row was written, a retried turn, and a
    renumbered history, none of which a turn-number stamp survives. Call it
    BEFORE the turn's own row is appended.
    """
    sources = _kb_sources(turn_meta.pop(TURN_METADATA_KB_PROMPTED, None) or [])
    if not sources:
        return
    earlier: set[tuple[str, str]] = set()
    for row in reversed(getattr(case, "messages", None) or []):
        recorded = (row.get("metadata") or {}).get(MESSAGE_METADATA_KB_SOURCES)
        if row.get("role") == "assistant" and recorded:
            earlier = {_source_key(s) for s in recorded if isinstance(s, dict)}
            break
    published = []
    for source in sources:
        row_source = source.model_dump(mode="json")
        source.new_this_turn = _source_key(row_source) not in earlier
        published.append(source.model_dump(mode="json"))
    turn_meta[MESSAGE_METADATA_KB_SOURCES] = published


def _infer_source_type(data_type: DataType) -> EvidenceSourceType:
    return _DATA_TYPE_TO_SOURCE_TYPE.get(data_type, EvidenceSourceType.TEXT)


def _published_source_type(uploaded_file: "UploadedFile") -> str:
    """``AttachmentResult.source_type`` for *uploaded_file*: the 6-valued string.

    ``UploadedFile.data_type`` holds the fine-grained ``DataType`` on rows
    written since #583 and the 6-valued string on rows written before; the
    API field is documented as the 6-valued vocabulary, so it is folded here
    rather than changing the published contract. An unrecognised value is
    passed through rather than erased — it is what the row says.

    No coercion of a non-string: the field is ``Optional[str]``, its writers
    store ``DataType.value`` off a required field, and the repositories load a
    string column, so none reaches here in production. The only way one
    does is a test double whose preprocessing result lacks a real
    ``detailed_data_type`` — and failing ``AttachmentResult`` validation
    loudly is the right outcome for that, not a ``str()`` of a Mock.
    """
    stored = uploaded_file.data_type
    folded = unified_data_type_of(stored)
    return folded.value if folded else (stored or "")
