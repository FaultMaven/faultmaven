"""Investigation Service - Manages milestone-based troubleshooting workflow

Purpose: Orchestrate investigation turns and milestone progress tracking

This service wraps the MilestoneEngine and provides:
- Access control for investigations
- Case retrieval and persistence
- Turn creation and processing
- Progress tracking and reporting
- Integration with session management
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from faultmaven.core.investigation.case_telemetry import (
    TELEMETRY_HANDOFF_KEY,
    TurnPath,
    collect_progress_arms,
    emit_case_turn,
)
from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    check_if_progress_made,
)
from faultmaven.core.investigation.prompts.context_builder import (
    structural_index_is_searchable,
)
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.investigation.suggestion_liveness import (
    CLARIFICATION_SPAN_CAP,
    OFFERED_DATA_TYPE_KEY,
    OFFERED_TURN_KEY,
    entry_file_id,
    entry_match_keys,
    file_data_types,
    is_clarification_entry,
    live_suggestions,
)
from faultmaven.core.investigation.turn_pipeline import (
    generate_implicit_query,
    submitted_name,
)
from faultmaven.core.investigation.turn_uploads import report_turn_uploads
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
    ValidationException,
)
from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_DEDUP_HITS_TOTAL,
    EVIDENCE_RECLASSIFICATION_TOTAL,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.api import DataType
from faultmaven.models.api_models import (
    AttachmentResult,
    IntentType,
    ProgressTransparencyInfo,
    QueryIntent,
    SuggestedActionResponse,
    TurnResponse,
)

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    TurnOutcome,
    TurnProgress,
    VerificationStatus,
)
from faultmaven.modules.case.contracts import ICaseRepository as CaseRepository
from faultmaven.modules.case.domain.models import (
    Evidence,
    EvidenceSourceType,
    UploadedFile,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


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

    * ``progress_made`` comes from :func:`check_if_progress_made`, the same
      predicate the engine's own deterministic branches use, NOT a hardcoded
      ``False``. The turns route accepts an intent alongside files, so a
      clarification click can carry a genuinely novel upload — and
      ``_finish_deterministic_turn`` is explicit that such an upload counts.
      Hardcoding False would report an inert turn on one where the user supplied
      new data, and leave the stall counter climbing through it.
    * the summaries carry the real text. ``_build_graduated_history`` renders
      from the record when one exists and falls back to the message text only
      when it is MISSING, so an empty record does not leave the text alone — it
      REPLACES it with "User message → conversation".
    * ``system_feedback`` is FORWARDED from the previous turn. It is read off
      ``turn_history[-1]`` and is meant for the next prompt; these routes build
      no prompt, so they have not consumed it. Dropping it would silently
      swallow a reasoning-validation error whenever a greeting landed between
      two engine turns.
    """
    if case.turn_history and case.turn_history[-1].turn_number == case.current_turn:
        return

    previous = case.turn_history[-1] if case.turn_history else None
    progress_made = check_if_progress_made(metadata)
    case.turn_history.append(
        TurnProgress(
            turn_number=case.current_turn,
            timestamp=datetime.now(timezone.utc),
            milestones_completed=list(metadata.get("milestones_completed") or []),
            evidence_added=[],
            hypotheses_generated=[],
            hypotheses_validated=[],
            solutions_proposed=[],
            progress_made=progress_made,
            outcome=metadata.get("outcome") or TurnOutcome.CONVERSATION,
            user_message_summary=_summarize_for_history(user_message, 200),
            agent_response_summary=_summarize_for_history(agent_response, 500),
            system_feedback=(previous.system_feedback if previous else None),
        )
    )
    # One-directional, matching ``_finish_deterministic_turn``: progress RESETS
    # the stall counter and nothing here ever increments it.
    if progress_made:
        case.turns_without_progress = 0


def _summarize_for_history(text: str, max_length: int) -> str:
    """Bound a message for the turn record, as the engine's own recorder does."""
    text = text or ""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


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


def _infer_source_type(data_type: DataType) -> EvidenceSourceType:
    return _DATA_TYPE_TO_SOURCE_TYPE.get(data_type, EvidenceSourceType.TEXT)


def _file_row_with_reclassification(
    file_meta: "UploadedFile",
    preprocessing_result,
    new_source_type: EvidenceSourceType,
) -> "UploadedFile":
    """UploadedFile row updated with re-extracted preprocessing artifacts.

    Post-010 routing: data_type / summary / structural_index describe the
    FILE and live on ``uploaded_files``; Evidence rows carry only the
    LLM-authored claim. Shared by both reclassification paths.
    """
    return file_meta.model_copy(
        update={
            "data_type": new_source_type.value,
            "summary": preprocessing_result.summary,
            "structural_index": preprocessing_result.structural_index,
        },
        deep=True,
    )


# Filename extensions and MIME prefixes for content known to be binary.
# Decoding such content as UTF-8 with errors="replace" produces a string of
# replacement chars that destroys the original bytes for any downstream
# multimodal/binary-aware extractor (Phase 3+). When detected, we skip the
# destructive decode and pass a metadata-only placeholder string to the
# classifier; the original bytes remain accessible via the storage layer.
_BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".tiff",
        ".ico",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".7z",
        ".rar",
        ".mp4",
        ".mov",
        ".avi",
        ".webm",
        ".mp3",
        ".wav",
        ".flac",
        ".bin",
        ".exe",
        ".dll",
        ".so",
    }
)
_BINARY_MIME_PREFIXES = (
    "image/",
    "video/",
    "audio/",
    "application/pdf",
    "application/zip",
    "application/x-",
    # application/octet-stream intentionally excluded: it is a generic client
    # fallback ("I don't know the type"), not a declarative binary signal.
    # Clients that know the type (browsers, SDK) send specific MIME types.
    # Clients that don't (curl, programmatic uploaders) send octet-stream for
    # text files too. Ambiguous cases are resolved by Layer 3 byte sniffing.
)

# Scan at most this many bytes when sniffing content for binary signals.
# 8 KB is enough to catch any real binary format's magic bytes and provides
# a statistically reliable non-printable ratio sample.
_SNIFF_SAMPLE = 8192


def _sniff_binary(content: bytes) -> bool:
    """Return True when raw bytes look like binary data.

    Uses two heuristics in priority order:
    1. Null byte (\\x00): text files in any encoding never contain null bytes.
       A single null in the sample is a definitive binary signal — the same
       heuristic used by git, grep -I, and the POSIX `file` command.
    2. Non-printable character ratio: catches binary files that happen to lack
       null bytes in the first 8 KB (rare, but possible with some encodings or
       encrypted payloads). Threshold of 30 % matches the `file` command's
       default heuristic for "binary" classification.
    """
    sample = content[:_SNIFF_SAMPLE]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    non_text = sum(1 for b in sample if b < 0x09 or (0x0E <= b <= 0x1F) or b == 0x7F)
    return (non_text / len(sample)) > 0.30


def _is_binary_content(
    filename: Optional[str],
    content_type: Optional[str],
    content: Optional[bytes] = None,
) -> bool:
    """Return True when filename, MIME type, or byte content signals binary.

    Three-layer detection in priority order:

    Layer 1 — Filename extension: definitive for known binary formats
    (.png, .pdf, .zip, .exe …). Fast path, no I/O.

    Layer 2 — MIME type: definitive only for protocol-level binary signals
    (image/*, video/*, audio/*, application/pdf …). application/octet-stream
    is excluded because it is a generic client fallback, not a binary signal.

    Layer 3 — Byte sniffing: resolves ambiguous MIME types (octet-stream or
    absent) by inspecting the actual bytes. Uses null-byte presence and
    non-printable character ratio — the same heuristics used by git and the
    POSIX `file` command. Only applied when content is provided.
    """
    fname = (filename or "").lower()
    if any(fname.endswith(ext) for ext in _BINARY_EXTENSIONS):
        return True

    ctype = (content_type or "").lower()
    if any(ctype.startswith(prefix) for prefix in _BINARY_MIME_PREFIXES):
        return True

    # MIME is ambiguous (octet-stream or absent) — sniff bytes if available.
    if content is not None and (ctype == "application/octet-stream" or not ctype):
        return _sniff_binary(content)

    return False


def _binary_placeholder(
    filename: Optional[str], content_type: Optional[str], size_bytes: int
) -> str:
    """Metadata-only string fed to the classifier for binary content.

    The original bytes remain in attachment.content / storage; this string
    only exists so the classifier has something to route on. Including the
    filename and content_type lets the rule-based classifier still pick
    VISUAL_EVIDENCE via filename-extension matching.
    """
    size_kb = size_bytes / 1024
    return (
        f"[binary attachment: filename={filename or 'unknown'}, "
        f"content_type={content_type or 'unknown'}, "
        f"size={size_kb:.1f}KB]"
    )


# DataType string value → human-friendly phrasing for cooperative clarification
# suggestions. Users see `label` on the suggestion card; `long` goes into the
# pre-composed query_submit payload. UNANALYZABLE is intentionally omitted —
# not a choice users can meaningfully select. UNSTRUCTURED_TEXT is handled as
# the "Something else" fallback separately to avoid duplicate suggestions.
_CLARIFICATION_FRIENDLY_NAMES: Dict[str, Dict[str, str]] = {
    "logs_and_errors": {"label": "Application logs", "long": "application logs"},
    "error_report": {
        "label": "Error report",
        "long": "an error report or stack trace",
    },
    "trace_data": {"label": "Trace data", "long": "trace data"},
    "metrics_and_performance": {
        "label": "Metrics",
        "long": "metrics or performance data",
    },
    "profiling_data": {"label": "Profiling data", "long": "profiling data"},
    "command_output": {"label": "Command output", "long": "command output"},
    "structured_config": {"label": "Configuration", "long": "configuration"},
    "source_code": {"label": "Source code", "long": "source code"},
    "documentation": {"label": "Documentation", "long": "documentation or notes"},
    "visual_evidence": {"label": "Screenshot", "long": "a screenshot or image"},
}

# Fallback phrasing used for the "Something else" option and applied when the
# classifier's only suggested type is UNSTRUCTURED_TEXT.
_CLARIFICATION_FALLBACK_LABEL = "Something else"
_CLARIFICATION_FALLBACK_LONG = "unstructured text"

# Choice seeds for paste-provenance clarifications. Pasted text in an incident
# thread is overwhelmingly command output or logs (product guidance: command
# output, regardless of content, is usually pasted) — and a paste that reached
# clarification has, by definition, weak content signals, so the provenance
# prior outranks the classifier's sub-threshold guesses. Seeds go first; the
# classifier's own suggestions fill the remaining slots.
_PASTE_CLARIFICATION_SEEDS = ["command_output", "logs_and_errors"]


def _engine_attachment_metadata(result: "_PreprocessedAttachment") -> dict:
    """The per-attachment dict handed to ``engine.process_turn``.

    The file facts are sourced entirely from the ``UploadedFile`` row, which is
    the record of what was submitted. ``source_type`` used to be taken from the
    shape of the SUBMITTED filename instead — ``"paste" if
    att.filename.startswith("pasted-content-") else "file_upload"`` — so a page
    capture, minted as ``page-capture-<ts>.txt``, reached the engine tagged
    ``file_upload`` and the engine could not tell a captured page from a chosen
    file (#1201). ``uf.input_origin`` is that fact derived once, with the
    precedence every other consumer uses.

    ``is_novel`` is the other fact the engine cannot recover for itself: did
    this turn bring data the case did not already hold? It is **tri-state**,
    because the honest answer has three values:

    - ``False`` — the content-hash dedup short-circuit fired, so the case
      demonstrably already held these bytes.
    - ``True`` — the lookup ran and found nothing.
    - ``None`` — *undetermined*: the lookup never ran (no content_hash, or it
      raised), so nothing here knows. Reading that as ``True`` would report a
      brand-new file for a byte-identical re-submission and arm #1136's
      progress arm on it — #1210 inverted, in the aggressive direction. The
      engine treats ``None`` conservatively and says so in the log.

    The engine used to re-derive novelty from the case aggregate
    (``file_id not in {f.file_id for f in case.uploaded_files}``), but
    ``_preprocess_attachment`` appends the authoritative row to that same
    aggregate BEFORE ``process_turn`` is called and there is no reload in
    between — so the id was always already known, and #1136's stall-net arm for
    uploads was dead on every turn (#1210). Deriving it here, once, from the
    field that owns it is the same lesson #1201 pinned: one derivation, not two.
    """
    uf = result.uploaded_file
    if result.duplicate_of is not None:
        is_novel: Optional[bool] = False
    elif result.dedup_ran:
        is_novel = True
    else:
        is_novel = None
    return {
        "file_id": uf.file_id,
        "filename": uf.filename,
        "data_type": uf.data_type or "",
        "size": uf.size_bytes,
        "source_type": uf.input_origin,
        "summary": uf.summary or "",
        "storage_ref": uf.storage_ref,
        "is_novel": is_novel,
    }


def _is_paste_upload(target: "_PreprocessedAttachment") -> bool:
    """True when the clarification target was pasted TEXT, not a chosen file.

    Deliberately paste-only, not "paste or capture": its second caller seeds
    the clarification choices with ``_PASTE_CLARIFICATION_SEEDS`` (command
    output / logs), which is a prior about war-room pastes and would be wrong
    for a captured web page. Detection itself lives on ``UploadedFile`` —
    provenance tag first, minted-filename shape as the fallback for rows
    whose tag predates the current values.
    """
    return target.uploaded_file.is_pasted


def _clarification_subject(target: "_PreprocessedAttachment") -> str:
    """How the clarification copy names the thing being classified.

    A minted name ("pasted-content-…", "page-capture-…") refers to the
    transport, not anything the user recognizes — call it what they did.
    Real files keep their filename.

    The capture arm is not hypothetical: classification_failed is decided
    BEFORE the page_capture passthrough in ``classify_and_extract``, so a
    capture the classifier is unsure about lands here, and without this it
    read ``the file you shared ("page-capture-20260709T105531.txt")`` —
    #666 on the Copilot's own channel, with no LLM in the loop (#1198
    review).
    """
    phrase = target.uploaded_file.submission_phrase
    if phrase is not None:
        return phrase
    filename = target.attachment_filename or "the uploaded file"
    return f'the file you shared ("{filename}")'


def _upload_subject(uf) -> str:
    """How agent copy names an ``UploadedFile`` back to the user.

    Same rule as ``_clarification_subject``, reached from the other side:
    that one starts from a preprocessing result, this one from the stored
    file row. Sentence register, so no turn number — see
    ``UploadedFile.submission_phrase``.
    """
    if uf is None or not getattr(uf, "filename", None):
        return "the uploaded file"
    return uf.submission_phrase or f'"{uf.filename}"'


# Longest filename fragment a choice label will carry. Long enough to keep
# real names recognisable, short enough that a pathological one cannot
# dominate the resolver's choice list.
_LABEL_QUALIFIER_MAX_CHARS = 48


def _sanitize_label_fragment(text: str) -> str:
    """Flatten caller-supplied text to one bounded, single-line fragment.

    A choice ``label`` is not display-only. It is persisted in
    ``last_suggestions`` and rendered verbatim into
    ``IntentResolver._build_prompt`` as a line in a NUMBERED CHOICE LIST —
    the prompt whose answer selects which offered intent fires. A filename
    carrying a newline therefore injects new lines into that list: a forged
    ``7. Yes, close the case`` reshapes the menu the classifier picks from,
    and because the same list also carries the engine's own follow-up
    intents (confirmation, status transitions), the steer is not confined
    to reclassification.

    Escaping is the wrong tool and #1216's lesson says why: nothing on this
    path DECODES, so ``&#10;`` would reach the model as those five literal
    characters and the label would read as garbage while still not being a
    newline the model treats as structure. The fix is at mint time — strip
    the characters that carry structure, collapse the whitespace, and bound
    the length — which is also what keeps the label a button.

    Every non-printable character becomes a space rather than being
    deleted — that covers newline, CR, tab and the rest of C0/C1, and also
    the format category, so a bidi override cannot reorder the label
    either. Deleting them instead would glue the surrounding tokens
    together and make a mangled name harder to recognise, not safer. Runs
    of whitespace then collapse to one space, and truncation is marked with
    an ellipsis so a clipped name does not read as the whole name.
    """
    flattened = "".join(ch if ch.isprintable() else " " for ch in text)
    collapsed = " ".join(flattened.split())
    if len(collapsed) > _LABEL_QUALIFIER_MAX_CHARS:
        return collapsed[: _LABEL_QUALIFIER_MAX_CHARS - 1].rstrip() + "…"
    return collapsed


def _clarification_label_qualifier(target: "_PreprocessedAttachment") -> str:
    """Short name telling one failed attachment's choices from another's.

    The *button* register — third and shortest of the three ways this
    codebase names an attachment back to the user, beside
    ``UploadedFile.display_name`` (citable, carries a turn number) and
    ``UploadedFile.submission_phrase`` (sentence, carries a clause). A button
    has room for neither, so this is the bare noun. Derived from the same
    provenance properties as its two siblings; keep the wording in step.

    The paste and capture arms are our own fixed wording. The third is the
    user's filename, and it goes through ``_sanitize_label_fragment``
    because a label reaches the intent resolver's prompt — see there. A
    name that sanitises away to nothing falls back to the generic phrase
    rather than an empty parenthetical.

    Not unique on its own, and no longer asked to be. Two pastes are both
    "pasted text" and two uploads of one filename are both that filename;
    nothing available here separates them (``submission_phrase`` is our own
    fixed wording, the minted ``pasted-content-<ts>.txt`` is a transport name
    the user never saw, and ``uploaded_at_turn`` is not unique per turn —
    #1264). Round one of #1245 appended the turn number here and rested the
    wrong-file guarantee on it; that guarantee now lives in
    ``_admit_clarification_entries``, which simply does not offer two
    attachments a typed answer could not tell apart. This is left to do the
    job a button can actually do: help the reader see which attachment a card
    is about.
    """
    uf = target.uploaded_file
    if uf.is_page_capture:
        base = "captured page"
    elif uf.is_pasted:
        base = "pasted text"
    else:
        raw = target.attachment_filename or uf.filename or ""
        base = _sanitize_label_fragment(raw) or "the uploaded file"
    return base


def _reclassification_intent(file_id: str, dt_value: str) -> Dict[str, Any]:
    """The engine-owned intent a clarification choice carries."""
    return {
        "type": IntentType.FILE_RECLASSIFICATION.value,
        "file_id": file_id,
        "data_type": dt_value,
    }


def _clarification_suggestions_for_failed(
    failed: List["_PreprocessedAttachment"],
) -> List[SuggestedActionResponse]:
    """Emit DECIDE suggestions for EVERY attachment that hit
    classification_failed.

    Takes the ALREADY-FILTERED list, so this and the narration note cannot
    disagree about which attachments failed — see
    ``_build_classification_clarification``, the one place the filter runs.

    One set of choices per failed attachment: up to 3 type-specific plus a
    "Something else" fallback each, and at least the fallback for any
    attachment that failed.

    **Every** failure, not just the first. The emitter used to clarify
    ``failed[0]`` and justify it from the per-turn file limit — "the limit is
    1, so we expect at most one classification_failed result". The premise
    was false: the limit is on ``files`` ALONE (``maxItems: 1``), and
    ``pasted_content`` is a separate form field that legitimately rides
    alongside a file as a second attachment. The paste/capture arm reaches
    ``classification_failed`` on its own — see ``_clarification_subject`` —
    so a turn where both fell below threshold left the second attachment
    with no choices, no ``file_reclassification`` intent and no recovery
    path, silently misclassified (#1222). Do not reintroduce a
    count-bounded shortcut here: the recovery path has to exist for whatever
    the route lets through, not for what one field's cap implies.

    Choice sources, per attachment: for a chosen file, the classifier's
    ``suggested_types``; for pasted text, the war-room seeds (command output
    / logs) come first — see ``_PASTE_CLARIFICATION_SEEDS`` — then the
    classifier's suggestions fill the remaining slots. Dedup and the
    three-choice budget are per attachment, so one attachment's choices
    never consume another's. The copy names the subject the way the user
    knows it (``_clarification_subject``).

    Every label carries the attachment's short name
    (``_clarification_label_qualifier``) — "Documentation (mystery.txt)",
    "Documentation (pasted text)" — because two cards both reading
    "Documentation" are indistinguishable on screen and in the resolver's
    numbered choice list.

    UNCONDITIONALLY, which is a change from the "only when more than one
    attachment failed this turn" rule #1236 shipped and #1245 round one
    widened to "more than one is on offer". Both were premised on the label
    being needed only to separate cards from EACH OTHER. The real hazard is
    the bare label as a standing generic: a question now outlives its turn,
    so "Documentation" minted on a lone turn 1 stays matchable on turn 5,
    and a user typing that shorthand while looking at turn 5's qualified
    cards resolved onto turn 1's file — oldest-wins, against every other
    ordering rule in this seam. A label that always names its subject has no
    generic form to be captured by.

    Qualifiers are NOT relied on to be unique — see
    ``_clarification_label_qualifier`` and ``_admit_clarification_entries``.

    A turn mints at most ONE synthetic
    name (#1198): ``pasted_content`` is a single form field, so a turn
    carries one paste or one capture, never two, and everything else is a
    user-chosen filename. Note this is a property of the *names*, not a
    count: two attachments could only share a qualifier by sharing a
    filename, and then ``payload`` collides too — such a pair is
    indistinguishable in any wording, so nothing is lost by not guarding it.

    Each suggestion carries an engine-owned ``file_reclassification`` intent
    (file_id + target DataType) so any client that forwards suggestion intent
    on click — the cross-client contract — resolves the choice through the
    structured reclassification handler, never as a free-text turn the LLM
    might act on literally (e.g. by deep-analyzing the file instead of
    re-labeling it). The ``payload`` remains the human-readable record of the
    choice; intent routing takes precedence over it server-side.

    Returns an empty list when no classification failure occurred this turn.
    """
    if not failed:
        return []

    suggestions: List[SuggestedActionResponse] = []

    for target in failed:
        subject = _clarification_subject(target)
        file_id = target.uploaded_file.file_id
        suffix = f" ({_clarification_label_qualifier(target)})"
        candidates = list(target.suggested_types or [])
        if _is_paste_upload(target):
            candidates = _PASTE_CLARIFICATION_SEEDS + candidates

        seen: set = set()
        emitted = 0

        for dt_value in candidates:
            if dt_value in seen:
                continue
            seen.add(dt_value)
            # unstructured_text collapses into the fallback — don't surface twice
            if dt_value == "unstructured_text":
                continue
            friendly = _CLARIFICATION_FRIENDLY_NAMES.get(dt_value)
            if friendly is None:
                continue
            suggestions.append(
                SuggestedActionResponse(
                    label=f'{friendly["label"]}{suffix}',
                    type="DECIDE",
                    payload=f'Treat {subject} as {friendly["long"]}.',
                    body=f'Treat as {friendly["long"]}.',
                    intent=_reclassification_intent(file_id, dt_value),
                )
            )
            emitted += 1
            if emitted >= 3:
                break

        # Always include the "Something else" fallback — last position for
        # this attachment.
        suggestions.append(
            SuggestedActionResponse(
                label=f"{_CLARIFICATION_FALLBACK_LABEL}{suffix}",
                type="DECIDE",
                payload=f"Treat {subject} as {_CLARIFICATION_FALLBACK_LONG}.",
                body=f"Treat as {_CLARIFICATION_FALLBACK_LONG}.",
                intent=_reclassification_intent(file_id, "unstructured_text"),
            )
        )

    return suggestions


def _clarification_note_for_failed(
    failed: List["_PreprocessedAttachment"],
) -> Optional[str]:
    """The narration bridge for this turn's clarification choices, or None.

    Takes the ALREADY-FILTERED list for the reason given on
    ``_clarification_suggestions_for_failed``: the note names exactly the
    attachments the choices target, by construction rather than by prose.

    The clarification suggestions are engine-emitted, so the LLM's own
    response usually says nothing about the content it couldn't classify —
    without this note the choices ("Treat as documentation.") read as
    disconnected nonsense under an unrelated investigation reply. Appended
    deterministically so every client gets the same context, phrased in the
    user's terms (a paste is "the text you pasted", never its synthetic
    snippet name).

    Names **every** failed attachment, for the same reason the emitter
    clarifies every one: the note used to pick the first via ``next(...)``,
    so a paste+file turn where both failed offered choices for two things
    while naming one (#1222). Wording is unchanged for the single-failure
    case, which is every turn carrying only one attachment.
    """
    subjects = [_clarification_subject(r) for r in failed]
    if not subjects:
        return None
    if len(subjects) == 1:
        named, pronoun = subjects[0], "it"
    else:
        named = f"{', '.join(subjects[:-1])} or {subjects[-1]}"
        pronoun = "them"
    return (
        f"\n\nOne more thing — I couldn't confidently classify "
        f"{named}, so I haven't analyzed {pronoun} yet. "
        f"How should I treat {pronoun}?"
    )


def _build_classification_clarification(
    preprocess_results: List["_PreprocessedAttachment"],
) -> Tuple[List[SuggestedActionResponse], Optional[str]]:
    """This turn's clarification choices and the note that introduces them.

    The ONE place ``classification_failed`` is filtered. Both halves are
    built from the same list object, so the note cannot name a different
    set of attachments than the choices target — an invariant that was
    prose (two call sites each re-deriving the filter) until it was made
    structural here. Nothing else should re-derive it.

    Takes nothing but this turn's results. Round one of #1245 threaded the
    carried attachments in here so the qualifier could be decided over the
    whole on-offer span; that coupling is gone with the qualifier's
    correctness role — the emitter describes THIS turn, and whether two
    attachments can be told apart is settled later, on the set that is
    actually stored.
    """
    failed = [r for r in preprocess_results if r.classification_failed]
    return (
        _clarification_suggestions_for_failed(failed),
        _clarification_note_for_failed(failed),
    )


# ============================================================
# Clarification set assembly (#1245, fm#918)
# ============================================================
#
# The liveness RULE — which stored entries may still answer a typed message —
# lives beside its consumer in ``core/investigation/suggestion_liveness.py``.
# What lives here is the ASSEMBLY: which of this turn's choices and which
# still-open earlier ones end up in the set that rule is applied to.
#
# Round one of #1245 tried to keep that set unambiguous by MINTING UNIQUE
# TEXT — a qualifier appended to each label, decided from how many
# attachments failed this turn and discriminated by the turn number. Review
# found three independent ways that guarantee fails, and they are worth
# stating because each is a trap the next person will re-lay:
#
#   - It covered ``label`` but not ``payload``, and ``_exact_match`` tests
#     PAYLOAD FIRST. Two pastes produce byte-identical payloads
#     ("Treat the text you pasted as documentation."), so the older card's
#     own wording resolved onto the newer file.
#   - It discriminated by ``uploaded_at_turn``, which is not unique per turn:
#     the persisted counter stands still across a SERVICE-dispatched turn
#     (#1264), so two attachments can carry the same number.
#   - It decided qualification BEFORE the span cap ran, so the set the
#     uniqueness claim was argued over is not the set that gets stored.
#
# So uniqueness is no longer a property of the wording. It is a property of
# ADMISSION: an attachment joins the on-offer set only if none of its
# matchable strings already belongs to an admitted one
# (``_admit_clarification_entries``), decided on the final set, using the
# matcher's own normalisation, with no clock involved. The qualifier is now
# unconditional and exists for the READER's benefit — telling two cards apart
# on screen — not to carry a correctness guarantee it cannot keep.
#
# ``IntentResolver`` holds the third line: a typed string that would resolve
# two ways resolves to neither. Admission should make that unreachable for
# clarifications; the guard is what makes it true regardless.


def _carry_forward_unresolved_clarifications(
    previous_suggestions: Optional[List[Dict[str, Any]]],
    case: "Case",
    resolved_file_id: Optional[str],
    *,
    as_of_turn: int,
) -> List[Dict[str, Any]]:
    """Clarification choices for attachments this turn left open.

    ``last_suggestions`` is rebuilt from scratch every turn, so before #1222
    the whole list collapsed the moment a turn produced no clarification of
    its own. With one failed attachment that cost nothing — the only pending
    question had just been answered. Once the emitter clarifies EVERY failure,
    answering one question deleted the others: the paste's four choices
    vanished from server-side memory the moment the user resolved the file.
    #1222 fixed that for the turn that RESOLVES an attachment.

    It stayed broken for the turn that IGNORES the question (#1245) — the far
    commoner shape, and identical for one attachment or two. The previous
    scoping (``resolved_file_id is None`` → carry nothing) was justified as
    self-limiting, "the carried set only ever shrinks, one file per
    reclassification". That was false: the turns route accepts an intent
    ALONGSIDE ``files`` and ``pasted_content``, so a reclassification turn that
    also uploads two failing attachments carries one file out and mints two in,
    growing the span by one every turn without limit.

    So the scoping is gone in both directions: the carry runs on every turn,
    and what bounds it is the liveness rule plus ``_admit_clarification_entries``
    (applied by the caller, over the whole assembled set). An answered question
    is still dropped — ``resolved_file_id`` names it exactly, and the referent
    check in ``suggestion_is_live`` catches the same thing arriving from
    anywhere else.

    Only clarification choices are carried. An engine follow-up was about the
    turn that produced it and does not outlive it; that is the
    ``FOLLOW_UP_CARRY_TURNS`` window, enforced by the shared liveness rule
    rather than by this filter, so both sites agree.
    """
    return [
        entry
        for entry in live_suggestions(previous_suggestions, case, as_of_turn=as_of_turn)
        if is_clarification_entry(entry) and entry_file_id(entry) != resolved_file_id
    ]


def _admit_clarification_entries(
    entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """The clarification entries that may be on offer together.

    Two rules, both applied per ATTACHMENT and both newest-first:

    **Unambiguous.** An attachment is admitted only if none of its matchable
    strings (``entry_match_keys`` — payload and label, folded the way the
    matcher folds them) already belongs to an admitted attachment. This is
    where the wrong-file guarantee actually lives. Two pastes read "the text
    you pasted" in every payload and no wording available to us separates
    them: ``submission_phrase`` is ours by design, the filename is a minted
    transport name the user never saw, and the turn number is not unique
    (#1264). Rather than offer two indistinguishable menus and let the matcher
    pick, the older one is not offered at all — which is also where it was
    before #1245, so nothing regresses.

    **Bounded.** At most ``CLARIFICATION_SPAN_CAP`` attachments, so the
    resolver's choice list cannot grow with user behaviour.

    Order is list position, newest first, and NOT ``offered_turn``. Sorting by
    the stamp looks more robust and is strictly worse: two turns can share a
    stamp (#1264), so it supplies no ordering exactly when a tie-break matters,
    while position is well-defined always. The list arrives newest-first by
    construction — this turn's choices are prepended, carried ones keep their
    relative order — and ``_stored_suggestions`` is the single place that
    builds it.

    Whole attachments are admitted or dropped, never split. Keeping two of an
    attachment's four choices leaves a menu that looks complete and silently no
    longer offers "documentation" — a wrong answer is worse than a missing
    question.
    """
    admitted: Dict[str, None] = {}
    claimed: Set[str] = set()
    # Refusal has to be remembered, not re-decided per entry. An attachment
    # turned away on its first choice would otherwise be let in on a later one
    # whose wording happens not to collide — readmitting it with a menu missing
    # exactly the options that clashed, which is the partial menu this refuses
    # to build. Pinned by
    # ``test_a_collision_refuses_the_whole_attachment_not_one_choice``.
    rejected: Set[str] = set()

    for entry in entries:
        file_id = entry_file_id(entry)
        if file_id is None or file_id in rejected:
            continue
        if file_id in admitted:
            # Another choice for an attachment already in: claim its wording
            # too, so a later attachment colliding with THIS card is refused
            # as surely as one colliding with the card that got it admitted.
            claimed |= entry_match_keys(entry)
            continue
        if len(admitted) >= CLARIFICATION_SPAN_CAP:
            # Full. Nothing further can be admitted, and ``claimed`` exists
            # only to refuse admissions, so the rest of the list is work with
            # no consequence — the final filter drops it either way.
            break
        keys = entry_match_keys(entry)
        if keys & claimed:
            # An already-admitted attachment answers to this wording; a second
            # one behind the same strings could only be reached by guessing.
            rejected.add(file_id)
            continue
        admitted[file_id] = None
        claimed |= keys

    return [e for e in entries if entry_file_id(e) in admitted]


def _stored_suggestions(
    *,
    case: "Case",
    clarification: List[SuggestedActionResponse],
    carried: List[Dict[str, Any]],
    follow_ups: List[Dict[str, Any]],
    offered_turn: int,
    as_of_turn: int,
) -> List[Dict[str, Any]]:
    """The ``last_suggestions`` value to persist at the end of a turn.

    Order is load-bearing twice over: ``IntentResolver._exact_match`` returns
    the FIRST match, and ``_admit_clarification_entries`` reads position as its
    ordering. Newest first — this turn's choices, then the carried ones
    oldest-last, then the engine's follow-ups.

    Follow-ups are stamped too. They are not carried (their window is one
    turn), but the stamp is what makes them EXPIRE rather than linger when no
    turn rewrites the list — fm#918's mid-turn-save exposure is exactly a
    follow-up outliving the state it was about.

    Everything assembled is then put through the liveness rule at
    ``as_of_turn``, the number the NEXT read will use. Filtering the fresh
    entries too is not belt-and-braces: a turn can end TERMINAL while carrying
    a ``classification_failed`` attachment, and the guard on
    ``suggestion_is_live`` already says a clarification is dead on a closed
    case (the handler answers 422). Storing them anyway made this function's
    own contract — what is stored is what the next read accepts — false for
    exactly the set where it mattered.
    """
    types = file_data_types(case)
    fresh = [
        {
            "label": s.label,
            "action_type": s.type,
            "payload": s.payload,
            "body": s.body,
            "intent": s.intent,
            OFFERED_TURN_KEY: offered_turn,
            OFFERED_DATA_TYPE_KEY: types.get((s.intent or {}).get("file_id")),
        }
        for s in clarification
    ]
    # Files this turn built fresh choices for win: the same attachment must
    # never appear twice, and the surviving wording has to be the one the user
    # was just shown.
    fresh_ids = {(s.intent or {}).get("file_id") for s in clarification}
    assembled = _admit_clarification_entries(
        fresh + [e for e in carried if entry_file_id(e) not in fresh_ids]
    ) + [{**f, OFFERED_TURN_KEY: offered_turn} for f in follow_ups if f.get("intent")]
    return live_suggestions(assembled, case, as_of_turn=as_of_turn)


@dataclass
class _PreprocessedAttachment:
    """Internal result of `_preprocess_attachment`.

    Post-010 strict evidence model: file uploads no longer create an
    Evidence row at intake. This carries the UploadedFile that was
    persisted (with preprocessing artifacts: summary, structural_index,
    data_type, coverage timestamps) plus dedup signals the caller
    needs to populate ``AttachmentResult.duplicate_of``.

    Also carries classification clarification hints when the heuristic
    classifier couldn't confidently classify the attachment — see
    ``_build_classification_clarification``.
    """

    uploaded_file: UploadedFile
    duplicate_of: Optional[str] = None
    duplicate_turn: Optional[int] = None
    # Did the content-hash lookup actually execute and return an answer?
    #
    # ``duplicate_of is None`` alone does NOT mean "novel" — it also covers
    # every case where dedup never ran (no content_hash to match on, or the
    # lookup raised). Reading absence as novelty reports a confident True for a
    # byte-identical re-submission, which ARMS #1136's progress arm and resets
    # ``turns_without_progress`` — the inverse of #1210, in the aggressive
    # direction. Defaults False so any construction site that does not
    # positively establish the answer is treated as undetermined.
    dedup_ran: bool = False
    # Classification clarification — populated only when the preprocessing
    # result had extraction_method="classification_failed". Contains 0–3
    # DataType enum values (as strings) suggested by the classifier for
    # cooperative-clarification UX. Empty/None when classification succeeded.
    classification_failed: bool = False
    suggested_types: Optional[List[str]] = None
    attachment_filename: Optional[str] = None


def _turn_delivers_evidence_bearing_attachment(
    preprocess_results: List["_PreprocessedAttachment"],
) -> bool:
    """True when this turn carried an attachment that was successfully
    classified and extracted into non-trivial content.

    Such a turn must drive Directed Analysis even under a generic cover
    message ("here's the logs"): ``classify_query`` only sees the message
    string, so the entity-free cover note routes to TRIAGE and lets the
    agent skip evidence analysis (#708). The strong signals live in the
    file, which the preprocessor already characterized. Excludes
    ``classification_failed`` uploads (awaiting user clarification) and
    empty/unanalyzable placeholders (no structural content to search); the
    searchability test is the context builder's own
    ``structural_index_is_searchable`` so this stays in lockstep with the
    ``searchable="true"`` render.
    """
    for r in preprocess_results:
        if r.classification_failed:
            continue
        if structural_index_is_searchable(r.uploaded_file.structural_index):
            return True
    return False


# ============================================================
# Intent dispatch
# ============================================================
#
# The IntentType enum is the contract between the API layer and the
# investigation pipeline. Each value must have a defined route here, or
# the system can't promise it can handle requests carrying that intent.
# Historically the dispatch lived as a scattered ``if / elif / else: raise``
# chain in ``process_turn``; new enum values could be added (slice 1 of the
# investigation-gates work did exactly this) without updating the dispatch,
# and the gap surfaced only at runtime as a 500.
#
# The dispatch table below is the single source of truth. The boot check
# in ``InvestigationService.__init__`` validates completeness against the
# IntentType enum — a new enum value without an entry here fails service
# construction, which fails app startup and CI.


class _IntentDispatchKind(str, Enum):
    """How an intent reaches its handler.

    SERVICE — a method on InvestigationService (special-cased: no LLM call,
              or pre-LLM bookkeeping).
    ENGINE  — delegated to ``engine.process_turn`` with intent_type +
              intent_data threaded through; the engine dispatches
              internally to a per-intent handler.
    NOT_IMPLEMENTED — the enum value exists in the API contract but the
              system does not yet handle it. Runtime requests raise
              ValidationException (422) with a clear "not implemented"
              message. Use this for known gaps; remove the enum value if
              the gap is permanent.
    """

    SERVICE = "service"
    ENGINE = "engine"
    NOT_IMPLEMENTED = "not_implemented"


_INTENT_DISPATCH: Dict[IntentType, _IntentDispatchKind] = {
    IntentType.STATUS_TRANSITION: _IntentDispatchKind.SERVICE,
    IntentType.CONFIRMATION: _IntentDispatchKind.SERVICE,
    IntentType.HYPOTHESIS_ACTION: _IntentDispatchKind.SERVICE,
    IntentType.GREETING: _IntentDispatchKind.SERVICE,
    # FILE_RECLASSIFICATION resolves a classification_failed upload: the
    # clarification DECIDE suggestions carry this intent (file_id + target
    # DataType) and the handler re-runs preprocessing mechanically — no LLM
    # call, so it can never mistake the choice for an analysis request.
    IntentType.FILE_RECLASSIFICATION: _IntentDispatchKind.SERVICE,
    IntentType.CONVERSATION: _IntentDispatchKind.ENGINE,
    # EVIDENCE_NEED (renamed from EVIDENCE_REQUEST in Phase 2 of the
    # evidence-needs redesign) is in the IntentType enum and has a
    # QueryIntent validator requiring evidence_need_id, but no handler
    # is wired yet — the pool model surfaces needs through EVIDENCE-type
    # suggestions and the LLM matches uploads to needs at file-
    # processing time, so a user-initiated intent isn't needed for the
    # MVP. Stays NOT_IMPLEMENTED until a frontend feature specifically
    # requires it (e.g., a "tell me more about this need" button). See
    # docs/architecture/investigation-engine/evidence-needs-design.md §9.3.
    IntentType.EVIDENCE_NEED: _IntentDispatchKind.NOT_IMPLEMENTED,
}


def _validate_intent_dispatch_completeness() -> None:
    """Validate that every ``IntentType`` enum value has a dispatch route.

    Raises RuntimeError at service construction time (and therefore at app
    startup and CI) if the dispatch table is incomplete. This converts the
    silent-runtime-500 failure mode of the prior elif chain into a
    fail-fast contract: a new enum value cannot ship without a dispatch
    decision (service handler, engine handler, or explicit not-implemented).
    """
    defined = set(IntentType)
    routed = set(_INTENT_DISPATCH.keys())
    missing = defined - routed
    extra = routed - defined
    if missing or extra:
        parts = []
        if missing:
            parts.append(
                f"IntentType values without a dispatch entry: "
                f"{sorted(v.value for v in missing)}"
            )
        if extra:
            parts.append(
                f"_INTENT_DISPATCH entries that are not IntentType values: "
                f"{sorted(v.value for v in extra)}"
            )
        raise RuntimeError(
            "InvestigationService intent dispatch is incomplete. "
            + " ".join(parts)
            + " Update _INTENT_DISPATCH in investigation_service.py or the "
            "IntentType enum so the two agree."
        )


class InvestigationService:
    """
    Service for managing investigation turns and milestone progress.

    Coordinates between:
    - MilestoneEngine (core investigation logic)
    - CaseRepository (persistence)
    - Access control (user permissions)
    """

    def __init__(
        self,
        milestone_engine: MilestoneEngine,
        case_repository: CaseRepository,
        preprocessing_service=None,
        file_storage_service=None,
    ):
        """
        Initialize investigation service.

        Args:
            milestone_engine: Core investigation engine with LLM integration
            case_repository: Case persistence layer
            preprocessing_service: Classification and extraction pipeline
            file_storage_service: Raw file storage (local/S3)
        """
        self.engine = milestone_engine
        self.repository = case_repository
        self.preprocessing_service = preprocessing_service
        self.file_storage_service = file_storage_service
        self.intent_resolver = IntentResolver(milestone_engine.llm_provider)
        # Fail-fast: refuse to construct if the intent dispatch table is
        # missing any IntentType value (or vice-versa). The system cannot
        # honor the API contract if it can't route every advertised intent.
        _validate_intent_dispatch_completeness()

    @trace("investigation_service_process_turn")
    async def process_turn(
        self, case_id: str, user_id: str, payload: TurnPayload
    ) -> TurnResponse:
        """
        Process a user turn through the two-step pipeline.

        Step 1: Preprocess any attachments (classify + extract, before LLM).
        Step 2: LLM inference with query + evidence context.

        Args:
            case_id: Case identifier
            user_id: User making the request
            payload: Turn payload with optional query and/or attachments

        Returns:
            TurnResponse with agent response, milestones, progress, and attachment results

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
            ServiceException: If turn processing fails
        """
        # #1142: bound before the try so the error path can tell "the case was
        # never loaded" from "a turn was consumed and then failed", and so a
        # failure AFTER the success row is emitted does not produce a second row
        # for the same turn.
        case = None
        turn_consumed = False
        turn_row_emitted = False

        def _emit_error_row() -> None:
            """One row for a turn that was consumed and then failed (#1142).

            Carries the volume facts off ``payload`` — they are known whatever
            failed, and omitting them reports the user as having gone silent on
            a turn they pasted 4 KB into, which is the mirror image of the
            misattribution the ``error`` label exists to prevent.
            """
            if turn_consumed and not turn_row_emitted and case is not None:
                emit_case_turn(
                    case,
                    path=TurnPath.ERROR,
                    user_message_chars=len(payload.query or ""),
                    attachment_count=len(payload.attachments or []),
                )

        try:
            # 1. Retrieve case and verify access
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            if case.user_id != user_id:
                logger.warning(
                    f"User {user_id} denied access to case {case_id} (owner: {case.user_id})"
                )
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            next_turn = case.current_turn + 1

            # ── STEP 1: PRE-LLM DATA INGESTION ──
            # Classify query for scenario-driven processing mode
            from faultmaven.modules.agent.domain.services.query_classifier import (
                ProcessingMode,
                QueryClassification,
                classify_query,
            )

            classification = classify_query(
                payload.query or "",
                has_attachments=payload.has_attachments,
            )
            processing_mode = classification.mode.value

            # Post-010 strict evidence model: preprocessing creates only
            # UploadedFile rows (no auto-Evidence). Evidence is born
            # later when the LLM emits ``evidence_to_add`` during
            # INVESTIGATING. Track the UploadedFiles created this turn
            # so the implicit-query helper can describe what the user
            # submitted; ``case.uploaded_files`` already had each row
            # appended inside ``_preprocess_attachment``.
            uploaded_files_this_turn: List["UploadedFile"] = []
            preprocess_results: List[_PreprocessedAttachment] = []
            if payload.has_attachments:
                for attachment in payload.attachments:
                    result = await self._preprocess_attachment(
                        case,
                        attachment,
                        user_id,
                        next_turn,
                        processing_mode=processing_mode,
                    )
                    preprocess_results.append(result)
                    uploaded_files_this_turn.append(result.uploaded_file)

            # #708: a fresh evidence-bearing upload must drive Directed
            # Analysis even when the accompanying message is a generic cover
            # note. classify_query only sees the message text, so a cover note
            # ("here's the logs") with no inline entities routes to TRIAGE —
            # and a knowledge-phrased cover ("what causes connection resets?")
            # routes to KNOWLEDGE_QUERY — either of which lets the agent skip
            # the freshly uploaded evidence. Re-route both to DA using the
            # attachment signal the preprocessor already produced. This is
            # channel-agnostic (Copilot pasted-content and Slack file uploads
            # flow through the same path) and composes with the Slack agent's
            # message_to_text alert-flattening, which already carries alert
            # entities in the query text. query_mode threads to the engine and
            # drives force_tools (tool_choice=required); DA subsumes triage.
            #
            # Scoped to INVESTIGATING: on INQUIRY the goal is to frame the
            # problem, and a fresh upload is characterized via the structural
            # index, not forced into directed analysis before the problem
            # statement is confirmed. (Terminal turns never reach the engine's
            # generation path — they short-circuit to _process_terminal_turn.)
            _reroute_modes = (ProcessingMode.TRIAGE, ProcessingMode.KNOWLEDGE_QUERY)
            if (
                case.state == CaseState.INVESTIGATING
                and classification.mode in _reroute_modes
                and _turn_delivers_evidence_bearing_attachment(preprocess_results)
            ):
                prior_mode = classification.mode.value
                classification = QueryClassification(
                    mode=ProcessingMode.DIRECTED_ANALYSIS,
                    detected_entities=classification.detected_entities,
                    confidence=0.8,
                )
                # ``classification.mode.value`` threads to the engine via
                # intent_data["query_mode"] below; the ``processing_mode`` local
                # is only consumed by preprocessing (already run above), so it
                # is intentionally not reassigned here.
                logger.info(
                    "Query re-routed %s→DIRECTED_ANALYSIS on case %s turn %s: "
                    "fresh evidence-bearing attachment (#708)",
                    prior_mode,
                    case_id,
                    next_turn,
                )

            # Determine query (explicit or implicit)
            query = payload.query
            if not payload.has_query and payload.has_attachments:
                query = generate_implicit_query(
                    uploaded_files_this_turn,
                    [a.filename for a in payload.attachments],
                )

            # 2. Build user message and update case in-memory (NOT persisted yet).
            #    What the deferral actually buys: nothing is committed BEFORE the
            #    LLM runs, so a turn that fails in the LLM call leaves no orphaned
            #    user message and no inflated turn count, and the client can retry
            #    the same turn. That is the whole of it.
            #
            #    ⚠️ It does NOT make the turn atomic, and it does NOT commit the
            #    user message and the agent's reply together. Two earlier versions
            #    of this comment claimed one or the other; both were false, so
            #    check this against the code before trusting it:
            #
            #      - On an engine-routed turn ``MilestoneEngine`` saves the case
            #        UNCONDITIONALLY at its Step 7 (``milestone_engine.py``, in
            #        ``_process_turn_impl``) — before returning, and therefore
            #        before the agent reply is appended by step 4 below. The user
            #        message is durable at that point and the reply is not. A
            #        failure in the window between them (reverse-redaction,
            #        clarification building, response assembly) leaves exactly the
            #        orphaned-user-message + inflated-turn state this comment used
            #        to promise was impossible.
            #      - The deterministic and terminal branches commit the same
            #        ``case`` object earlier still, at their own ``save(case)``
            #        sites.
            #
            #    So: an LLM failure commits nothing; a post-LLM failure can commit
            #    a half turn. Do not reason about this path as all-or-nothing.
            from uuid import uuid4

            intent = payload.intent
            intent_type = intent.type if intent else IntentType.CONVERSATION

            user_message_obj = {
                "message_id": f"msg_{uuid4().hex[:12]}",
                "turn_number": next_turn,
                "role": "user",
                "message_type": "user_query",
                "content": query or "",
                "created_at": to_json_compatible(datetime.now(timezone.utc)),
                "author_id": user_id,
                "token_count": None,
                "metadata": {
                    "has_attachments": payload.has_attachments,
                    "attachment_count": len(payload.attachments),
                    "intent_type": intent_type.value,
                    "intent_metadata": (
                        intent.model_dump(exclude_unset=True, exclude={"type"})
                        if intent
                        else {}
                    ),
                },
            }
            case.messages.append(user_message_obj)
            case.message_count += 1
            case.current_turn = next_turn
            # #1142: this assignment is what "a turn was consumed" MEANS, and it
            # is the reason the telemetry row is emitted from this method rather
            # than from the engine — several routes below consume a turn number
            # without reaching ``MilestoneEngine.process_turn`` at all. Read
            # terminality here, before dispatch: the engine's terminal
            # short-circuit returns before any turn bookkeeping, so afterwards
            # nothing distinguishes it from a generation turn that did nothing.
            was_terminal = case.is_terminal
            # Gates the error-path row. ``case is not None`` is NOT the same
            # question: a failure between the load and this line (an extractor
            # or storage error inside attachment preprocessing, say) leaves a
            # bound ``case`` whose ``current_turn`` is still the PREVIOUS turn's
            # — a row emitted there would collide with that turn's real row on
            # the documented (case_id, turn) dedup key, and on turn 1 would
            # invent a row for turn 0.
            turn_consumed = True

            # ── STEP 2: LLM INFERENCE ──
            # Heuristic check for greetings if intent is CONVERSATION (default)
            #
            # Never on a turn that carried an attachment (#1229). The heuristic
            # reads the message text alone, so "hi" plus a genuinely new log
            # matched ``^(hi|hello|...)$`` and routed to ``_handle_greeting`` —
            # which answers from a static string, never calls the engine, and
            # therefore reported no upload, armed no progress arm, and left the
            # two #1224 degradation warnings unreachable. The row was already
            # committed and dedup-classified by then; only the engine was not
            # told. A turn that delivers data is not a greeting, whatever the
            # covering text says — the same judgement #708 applies one block
            # below when it re-routes a generic cover note to Directed
            # Analysis. Any attachment disqualifies, not just a novel one: a
            # re-submission still belongs on the path that knows what to do
            # with a duplicate.
            if (
                intent_type == IntentType.CONVERSATION
                and query
                and not payload.has_attachments
            ):
                heuristic_intent = self._detect_intent_heuristic(query)
                if heuristic_intent:
                    intent_type = heuristic_intent
                    logger.info(
                        f"Heuristic detected intent {intent_type.value} for message: '{query}'"
                    )

            # Intent resolution: match typed text against the choices still
            # on offer. Only runs when no structured intent was sent and the
            # case has live intent-bearing suggestions.
            #
            # ``live_suggestions``, not the raw field: the row is rewritten
            # only on this method's success path, so what is stored is not by
            # itself evidence that a turn put it there for now (fm#918).
            # ``case.current_turn`` is already this turn's number here (set
            # just above with the user message), so an entry offered on the
            # immediately preceding turn ages to 1.
            #
            # Computed inside the guard, not above it: the cheap conditions
            # reject the great majority of turns, and this walks every stored
            # entry and indexes every uploaded file to answer a question those
            # turns never ask.
            if (
                intent_type == IntentType.CONVERSATION
                and query
                and not payload.has_attachments
                and case.last_suggestions
            ):
                on_offer = live_suggestions(
                    case.last_suggestions, case, as_of_turn=case.current_turn
                )
                resolved_intent = (
                    await self.intent_resolver.resolve(
                        user_message=query,
                        last_suggestions=on_offer,
                    )
                    if on_offer
                    else None
                )
                if resolved_intent:
                    try:
                        resolved_qi = QueryIntent(**resolved_intent)
                        if self._minted_intent_swallows_terminal_consent(
                            case, resolved_qi, query
                        ):
                            # INV-26 guard (#721): the resolver's classifier
                            # tier matched substantive typed text ("yes but
                            # what about the replication lag?") to a
                            # suggestion whose intent would confirm the
                            # pending TERMINAL transition. Substantive input
                            # is never consent to an irreversible action —
                            # drop the minted intent so the message flows
                            # through the pending-gate escape lane as a
                            # normal turn (the engine withdraws the proposal
                            # and processes the message; it can re-propose
                            # from fresher state).
                            logger.info(
                                "Discarded classifier-minted intent "
                                f"{resolved_qi.type.value} for case "
                                f"{case.case_id}: substantive reply must not "
                                "confirm a pending terminal transition "
                                "(INV-26, #721)"
                            )
                        else:
                            intent = resolved_qi
                            intent_type = resolved_qi.type
                            logger.info(
                                f"Intent resolved from suggestions: {intent_type.value} "
                                f"for message: '{query[:50]}...'"
                            )
                    except Exception:
                        logger.warning(
                            "Failed to parse resolved intent, "
                            "falling back to conversation",
                            exc_info=True,
                        )

            # Dispatch on the boot-validated routing table. ``intent_type``
            # is guaranteed to be present in ``_INTENT_DISPATCH`` because
            # _validate_intent_dispatch_completeness ran at service
            # construction and would have refused to start otherwise.
            dispatch_kind = _INTENT_DISPATCH[intent_type]

            # Attachment metadata for the engine. Post-010: uploads create only
            # an UploadedFile (no auto-Evidence), so the file facts are sourced
            # directly from those rows — the #1201 fix: the row is the record of
            # what was submitted, its filename is not.
            #
            # Walks ``preprocess_results`` rather than ``uploaded_files_this_turn``
            # (the same set, in the same order — one result per attachment, and
            # that list is built from ``result.uploaded_file``) because the
            # result also carries ``duplicate_of``, the only place novelty is
            # still knowable by the time the engine runs (#1210).
            #
            # Built HERE, above the dispatch, rather than inside the ENGINE
            # branch: the SERVICE-routed handlers below delegate to the very
            # same ``engine.process_turn`` and used to hand it
            # ``attachments=None`` even though ``_preprocess_attachment`` had
            # already run and committed a row for every attachment on the turn.
            # An upload riding a suggestion-chip intent was persisted and
            # dedup-classified, and the engine was told nothing arrived (#1229).
            attachment_metadata = [
                _engine_attachment_metadata(res) for res in preprocess_results
            ]

            if dispatch_kind == _IntentDispatchKind.NOT_IMPLEMENTED:
                # Intent value is defined in the IntentType enum (API
                # contract) but no handler exists in this build. Surface as
                # 422 with a clear message rather than a 500 — this is a
                # contract gap, not a server failure.
                raise ValidationException(
                    f"Intent type '{intent_type.value}' is defined in the "
                    "API but not implemented in this build. Either drop the "
                    "enum value or add a handler in investigation_service.",
                    {"intent_type": intent_type.value},
                )

            if dispatch_kind == _IntentDispatchKind.SERVICE:
                # Service-level handlers — special-cased because each does
                # pre-LLM work (no LLM call, state mutation only, etc.)
                # and the handler signatures vary.
                if intent_type == IntentType.STATUS_TRANSITION:
                    result = await self._handle_status_transition(
                        case=case,
                        user_message=query or "",
                        from_state=intent.from_state if intent else None,
                        to_state=intent.to_state if intent else None,
                        user_confirmed=(
                            (intent.user_confirmed or False) if intent else False
                        ),
                        user_id=user_id,
                        attachments=attachment_metadata or None,
                    )
                elif intent_type == IntentType.CONFIRMATION:
                    result = await self._handle_confirmation(
                        case=case,
                        user_message=query or "",
                        confirmation_value=(
                            intent.confirmation_value if intent else None
                        ),
                        user_id=user_id,
                        attachments=attachment_metadata or None,
                    )
                elif intent_type == IntentType.HYPOTHESIS_ACTION:
                    result = await self._handle_hypothesis_action(
                        case=case,
                        user_message=query or "",
                        hypothesis_id=intent.hypothesis_id if intent else None,
                        action=intent.action if intent else None,
                        user_id=user_id,
                        attachments=attachment_metadata or None,
                    )
                elif intent_type == IntentType.GREETING:
                    result = await self._handle_greeting(
                        case=case, attachments=attachment_metadata or None
                    )
                elif intent_type == IntentType.FILE_RECLASSIFICATION:
                    result = await self._handle_file_reclassification(
                        case=case,
                        file_id=intent.file_id if intent else None,
                        data_type_value=intent.data_type if intent else None,
                        attachments=attachment_metadata or None,
                    )
                else:
                    # Dispatch table claims SERVICE but there's no handler.
                    # This is a developer error (added entry but not a
                    # method); a 500 is correct here — the user did
                    # nothing wrong.
                    raise ServiceException(
                        f"Internal: SERVICE-routed intent "
                        f"'{intent_type.value}' has no handler method. "
                        "Update the if/elif chain in process_turn."
                    )
            elif dispatch_kind == _IntentDispatchKind.ENGINE:
                # Engine-routed: thread intent_type + intent_data through
                # to ``engine.process_turn``, which dispatches internally
                # to the per-intent handler in milestone_engine.
                #
                # DA evidence search is handled inside MilestoneEngine's
                # tool loop. The same LLM that tracks hypotheses searches
                # evidence directly during generation — no pre-fetch or
                # separate gathering step needed.

                result = await self.engine.process_turn(
                    case=case,
                    user_message=query or "",
                    attachments=attachment_metadata or None,
                    intent_type=intent_type.value,
                    intent_data={
                        **(intent.model_dump(exclude_unset=True) if intent else {}),
                        "query_mode": classification.mode.value,
                    },
                    # The turn's authenticated principal. Kept out of
                    # ``intent_data`` deliberately: that dict is built from the
                    # client-supplied intent payload, and the KB read allowlist
                    # must not be keyed on anything a client can set.
                    user_id=user_id,
                )
            else:
                # Defensive: _IntentDispatchKind only has three values
                # and NOT_IMPLEMENTED / SERVICE / ENGINE are all handled
                # above. A new dispatch kind without a corresponding
                # branch would land here — developer error, 500.
                raise ServiceException(
                    f"Internal: Unknown dispatch kind '{dispatch_kind}' "
                    f"for intent '{intent_type.value}'."
                )

            # 3. Processing succeeded — extract updated case
            updated_case = result["case_updated"]
            agent_response_text = result["agent_response"]

            # 3a. #1264: every consumed turn gets a ``turn_history`` entry.
            #
            # Both repositories persist ``Case.effective_current_turn`` — the
            # last recorded turn number — rather than the in-flight
            # ``current_turn``. That is #500's prevention half, and it is still
            # right: it stops the stored counter running ahead of the history,
            # which is what let one interrupted turn permanently wedge a case.
            # But it means a route that consumes a turn number WITHOUT recording
            # one freezes the persisted counter. ``process_turn`` reloads the
            # case every request and derives ``next_turn`` from that column, so
            # the very next turn re-derives the number just used — no process
            # boundary required. Measured on the corpus: 7 cases carry a
            # ``(case_id, turn_number)`` pair with two user messages, and one
            # resolved case has THREE user turns all stamped turn 9.
            #
            # The rule this restores is one the engine already states: its
            # deterministic branches record a TurnProgress because "a
            # deterministic branch still consumes a turn number"
            # (``_finish_deterministic_turn``). So ``turn_history`` is already a
            # record of CONSUMED turns rather than of engine turns, and the
            # routes that skip it — greeting, file reclassification, and the
            # terminal short-circuit — are the ones that were missed, not a
            # different kind of turn.
            #
            # Placed at the chokepoint rather than at those three sites for the
            # reason the telemetry emission is here too: this method is where a
            # turn number is consumed, so a backstop here cannot be missed by a
            # route added later. It is a no-op on every path that already
            # recorded, which is the overwhelming majority.
            _backfill_consumed_turn(
                updated_case,
                user_message=query or "",
                agent_response=agent_response_text,
                metadata=result.get("metadata") or {},
            )

            # Placed HERE, not beside the save: ``next_read_turn`` below is
            # ``effective_current_turn + 1``, and every consumer of the turn
            # clock on this path reads it after this point. Recording the turn
            # after them would leave them reading a counter that is one behind
            # for this turn — which is the same off-by-one this issue is about,
            # just relocated. Caught by #1263's window test, which stopped
            # closing its recovery window.

            # #1142: lift the engine's progress-arm reading out of the returned
            # metadata BEFORE step 4 persists that dict onto the assistant
            # ``case_messages`` row. Popped rather than copied: the row is
            # readable through the transcript API, and this is monitoring data
            # collected like logging data, not part of the product surface.
            turn_telemetry = (result.get("metadata") or {}).pop(
                TELEMETRY_HANDOFF_KEY, None
            ) or {}

            # Reverse-substitute PII placeholders so user sees real values.
            # The LLM worked with redacted content; the user should not.
            redaction_ctx = result.get("redaction_ctx")
            if redaction_ctx:
                agent_response_text = redaction_ctx.reverse(agent_response_text)

            # 3b. Store suggestions with intent metadata for next turn's
            #      intent resolver (bounded choice matching). Clarification
            #      suggestions (classification_failed this turn) are built
            #      here — before the save — so a user who *types* a choice
            #      ("application logs") instead of clicking resolves to the
            #      same file_reclassification intent as a click.
            #
            # Read the carry off ``updated_case``: the reclassification
            # handler ``model_copy``s the case, so this is still the PREVIOUS
            # turn's list at this point.
            #
            # ``next_read_turn`` is the number the NEXT turn's adoption site
            # will compute, and BOTH sides of the seam are filtered at it, so
            # what is stored is exactly what the next read accepts. It is
            # ``effective_current_turn + 1``, not ``current_turn + 1``. Since
            # #1264 those agree on every route — the backfill above guarantees
            # this turn is recorded before the counter is read — but deriving
            # from the persisted clock keeps the seam correct BY CONSTRUCTION
            # rather than by the two happening to match. If a route ever stops
            # recording again, that shows up as a clock bug, not as silently
            # dropped clarification questions.
            # Filtering at the wrong one is not a rounding error — it ages
            # every entry an extra turn after every clarification click and
            # permanently drops questions the reader would still have taken.
            resolved_file_id = (
                (result.get("metadata") or {})
                .get("file_reclassified", {})
                .get("file_id")
            )
            next_read_turn = updated_case.effective_current_turn + 1
            carried_entries = _carry_forward_unresolved_clarifications(
                updated_case.last_suggestions,
                updated_case,
                resolved_file_id,
                as_of_turn=next_read_turn,
            )

            # Choices and the note that introduces them come back together
            # from one filter pass, so the note cannot name a different set
            # of attachments than the choices target.
            clarification, clarification_note = _build_classification_clarification(
                preprocess_results
            )

            raw_follow_ups = result.get("suggested_follow_ups", [])
            stored = _stored_suggestions(
                case=updated_case,
                clarification=clarification,
                carried=carried_entries,
                follow_ups=raw_follow_ups,
                offered_turn=updated_case.current_turn,
                as_of_turn=next_read_turn,
            )
            updated_case.last_suggestions = stored or None

            # The CARDS are derived from what survived storage, so one rule
            # decides both. A turn can deliver an unclassifiable attachment
            # AND close the case; ``_handle_file_reclassification`` refuses on
            # a terminal case, so each card would be a button that answers 422
            # while the typed route is silently dropped by the liveness rule —
            # and "How should I treat it?" is not a question a closed case is
            # asking. Special-casing ``is_terminal`` here instead would put the
            # same judgement in two places and, worse, make the filter inside
            # ``_stored_suggestions`` unreachable: an invariant nothing can
            # break is an invariant nothing is checking.
            offered_ids = {entry_file_id(e) for e in stored}
            clarification = [
                s
                for s in clarification
                if (s.intent or {}).get("file_id") in offered_ids
            ]
            if not clarification:
                clarification_note = None
            if clarification_note:
                agent_response_text += clarification_note

            # 4. Append the agent response and save.
            #    ⚠️ This is NOT an atomic commit of both messages, though it used
            #    to say so ("commits both messages together, guaranteeing no
            #    half-completed turns"). On an engine-routed turn the engine has
            #    ALREADY committed the user message at its Step 7 save, so by the
            #    time control reaches here a half-completed turn is exactly what
            #    is in the database, and this save completes it rather than
            #    preventing it.
            #
            #    It IS the single commit for both only when no engine save
            #    intervened — GREETING and FILE_RECLASSIFICATION. The other three
            #    SERVICE intents (STATUS_TRANSITION, CONFIRMATION,
            #    HYPOTHESIS_ACTION) delegate to ``engine.process_turn`` from their
            #    handlers, so they hit Step 7 just like an engine-routed turn.
            #    "Service-dispatched" is NOT a synonym for "no engine save".
            #    See the STEP-2 comment for the full ordering.
            agent_message = {
                "message_id": f"msg_{uuid4().hex[:12]}",
                "turn_number": updated_case.current_turn,
                "role": "assistant",
                "message_type": "agent_response",
                "content": agent_response_text,
                "created_at": to_json_compatible(datetime.now(timezone.utc)),
                "author_id": None,
                "token_count": None,
                "metadata": result.get("metadata", {}),
            }
            updated_case.messages.append(agent_message)
            updated_case.message_count += 1
            await self.repository.save(updated_case)

            # 4b. #1142: one row per consumed turn, on every route. Emitted
            # AFTER the save so the counter, the case state and both ledgers are
            # the settled post-turn values — the pre-existing
            # ``grounding_assessment`` trace reports from inside response
            # application and therefore carries the PREVIOUS turn's
            # ``turns_without_progress``.
            #
            # The route is taken from the engine's handoff when there is one.
            # The fallbacks are not cosmetic: GREETING and FILE_RECLASSIFICATION
            # are answered here without ever calling the engine, and a terminal
            # case short-circuits inside it, so all three would otherwise be
            # stream GAPS — and a gap silently shortens every streak a consumer
            # computes, making a correct handshake read as an engine-dry run.
            turn_metadata = result.get("metadata") or {}
            # No handoff means the route never reached the engine's progress
            # decision — but it still reported its uploads, because every route
            # runs ``report_turn_uploads``. Reading the arms off the returned
            # metadata rather than defaulting to all-zero is what keeps
            # ``user_supplied_new`` true on a turn where the user DID upload
            # (a file riding a clarification click, or arriving on a closed
            # case). All-zero there would print "the user supplied nothing" on
            # exactly the engine-dry-user-supplying turn this stream exists to
            # surface.
            turn_arms = turn_telemetry.get("arms") or collect_progress_arms(
                turn_metadata
            )
            if turn_telemetry.get("path"):
                turn_path = turn_telemetry["path"]
            elif intent_type == IntentType.GREETING:
                turn_path = TurnPath.GREETING
            elif intent_type == IntentType.FILE_RECLASSIFICATION:
                turn_path = TurnPath.RECLASSIFICATION
            elif was_terminal:
                turn_path = TurnPath.TERMINAL
            else:
                turn_path = TurnPath.LLM
            emit_case_turn(
                updated_case,
                path=turn_path,
                arms=turn_arms,
                gate_name=turn_telemetry.get("gate_name"),
                progress_made=bool(turn_metadata.get("progress_made", False)),
                outcome=turn_metadata.get("outcome") or turn_telemetry.get("outcome"),
                validation_repairs=int(turn_telemetry.get("validation_repairs", 0)),
                repair_pattern=turn_telemetry.get("repair_pattern"),
                # ``payload.query``, not ``query``: on an attachment-only turn
                # ``query`` has been replaced by ``generate_implicit_query``'s
                # engine-composed sentence, and reporting its length would say
                # the user wrote a paragraph on a turn they typed nothing. This
                # field's whole job is telling "user went silent" apart from
                # "user wrote a paragraph that produced nothing".
                user_message_chars=len(payload.query or ""),
                attachment_count=len(attachment_metadata or []),
            )
            turn_row_emitted = True

            # 5. Build TurnResponse
            suggested_actions = [
                SuggestedActionResponse(
                    label=f["label"],
                    type=f["action_type"],
                    payload=f.get("payload"),
                    body=f.get("body"),
                    hints=f.get("hints"),
                    intent=f.get("intent"),
                )
                for f in raw_follow_ups
            ]

            # Prepend classification-clarification suggestions (built at 3b)
            # when this turn's attachment hit classification_failed.
            # User-in-the-loop guidance takes priority over generic
            # follow-up suggestions from the engine.
            if clarification:
                suggested_actions = clarification + suggested_actions

            # Read-time assurance grade for narration-only clients (#572/INV-28):
            # present whenever the case has stated a root cause, recomputed from
            # the causal graph so a resolution turn (which never recomputes the
            # persisted progress field) still carries the true grade beside the
            # cause claim the LLM wrote into agent_response.
            turn_cause_assurance = None
            turn_cause_overclaim = None
            if updated_case.root_cause_conclusion is not None:
                from faultmaven.core.investigation.cause_assurance import (
                    conclusion_overclaims,
                    grade_cause_assurance,
                )

                _grade = grade_cause_assurance(updated_case)
                turn_cause_assurance = _grade.value
                turn_cause_overclaim = conclusion_overclaims(
                    updated_case.root_cause_conclusion, _grade
                )

            response = TurnResponse(
                agent_response=agent_response_text,
                turn_number=updated_case.current_turn,
                milestones_completed=result.get("metadata", {}).get(
                    "milestones_completed", []
                ),
                case_state=updated_case.state,
                progress_made=result.get("metadata", {}).get("progress_made", False),
                attachments_processed=[
                    AttachmentResult(
                        file_id=res.uploaded_file.file_id,
                        # The chip the Copilot renders on the very turn
                        # the user pasted — #666's most immediate surface.
                        # ``file_id`` is the documented handle the frontend
                        # references an attachment by, so this field is
                        # display-only.
                        #
                        # ``submitted_name``, not ``uploaded_file.display_name``:
                        # dedup matches on content_hash ALONE, so the row can
                        # be one the user named differently on an earlier turn,
                        # and naming the chip from it reports a filename they
                        # never sent.
                        filename=submitted_name(att.filename, res.uploaded_file),
                        source_type=res.uploaded_file.data_type or "",
                        file_size=res.uploaded_file.size_bytes,
                        processing_status=(
                            "duplicate" if res.duplicate_of else "completed"
                        ),
                        uploaded_at=datetime.now(timezone.utc).isoformat(),
                        upload_source=res.uploaded_file.upload_source,
                        duplicate_of=res.duplicate_of,
                        duplicate_turn=res.duplicate_turn,
                    )
                    for att, res in zip(payload.attachments, preprocess_results)
                ],
                suggested_actions=suggested_actions,
                progress_transparency=self._build_progress_transparency(
                    result.get("metadata", {}), updated_case
                ),
                cause_assurance=turn_cause_assurance,
                cause_overclaim=turn_cause_overclaim,
            )

            logger.info(
                f"Processed turn {response.turn_number} for case {case_id}, "
                f"status={response.case_state}, milestones={len(response.milestones_completed)}, "
                f"attachments={len(uploaded_files_this_turn)}, messages={updated_case.message_count}"
            )

            return response

        except (
            NotFoundError,
            PermissionDeniedException,
            StaleCaseException,
            ValidationException,
        ):
            # NotFoundError → 404, PermissionDeniedException → 403,
            # StaleCaseException → 409, ValidationException → 422.
            # All must pass through unwrapped so the FastAPI exception
            # handlers can map them to the correct HTTP status; wrapping
            # them in ServiceException would mask the contract error as 500.
            #
            # #1142: these get a row too when the turn was already consumed.
            # StaleCaseException is the case that matters — on an engine-routed
            # turn the engine has ALREADY committed the incremented
            # ``current_turn`` at its own save, so an OCC conflict on the
            # service's save leaves a durably consumed turn with no row, and a
            # gap shortens every streak computed over the stream.
            _emit_error_row()
            raise
        except Exception as e:
            # #1142: the turn number was consumed at STEP 1 and the request then
            # failed, so without a row here the stream shows a gap on exactly
            # the turns where something went wrong. The point of labelling it is
            # attribution: a provider outage or a tool-loop failure must not read
            # as an idle engine. ``case`` may be unbound if the failure preceded
            # the load, and the case may never have been saved at this turn
            # number, so a consumer dedups on (case_id, turn) preferring the
            # non-error row.
            _emit_error_row()
            logger.error(f"Failed to process turn for case {case_id}: {e}")
            # Preserve a typed error_code (e.g. QUOTA_EXHAUSTED billing) through
            # the wrap so the route handler can map it to a precise HTTP status
            # instead of a generic 500.
            raise ServiceException(
                f"Turn processing failed: {str(e)}",
                details={"error_code": getattr(e, "error_code", None)},
            ) from e

    @trace("investigation_service_get_progress")
    async def get_progress(self, case_id: str, user_id: str) -> Dict[str, Any]:
        """
        Get current investigation progress.

        Args:
            case_id: Case identifier
            user_id: User making the request

        Returns:
            Progress summary with:
            - case_id, status, current_stage
            - milestones_completed, pending_milestones
            - current_turn

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            # Check permissions
            if case.user_id != user_id:
                logger.warning(
                    f"User {user_id} denied access to case {case_id} (owner: {case.user_id})"
                )
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # Return progress summary
            return {
                "case_id": case.case_id,
                "state": case.state.value,
                "current_stage": (
                    case.current_stage.value if case.current_stage else None
                ),
                "milestones_completed": case.progress.completed_milestones,
                "pending_milestones": case.progress.pending_milestones,
                "current_turn": case.current_turn,
            }

        except (NotFoundError, PermissionDeniedException):
            raise
        except Exception as e:
            logger.error(f"Failed to get progress for case {case_id}: {e}")
            raise ServiceException(f"Progress retrieval failed: {str(e)}") from e

    # ============================================================
    # Attachment Preprocessing
    # ============================================================

    async def _preprocess_attachment(
        self,
        case: "Case",
        attachment: Attachment,
        user_id: str,
        turn_number: int,
        processing_mode: str = "triage",
    ) -> _PreprocessedAttachment:
        """Preprocess a single attachment through classification and extraction.

        Args:
            case: Case entity (for case_id context)
            attachment: Raw attachment from turn payload
            user_id: User who submitted the attachment
            turn_number: Current turn number
            processing_mode: Processing mode from query classification
                (triage or directed_analysis)

        Returns:
            ``_PreprocessedAttachment`` wrapping the persisted
            ``UploadedFile`` plus optional dedup metadata. Post-010
            strict evidence model: NO Evidence row is created at this
            intake step. On content-hash duplicate within the same
            case, the returned UploadedFile is the existing row and
            ``duplicate_of`` / ``duplicate_turn`` are populated; no
            new UploadedFile is created and no raw file is re-stored.

        Raises:
            ServiceException: If preprocessing or storage fails
        """
        from uuid import uuid4

        # Skip destructive UTF-8 decode for known-binary content (images,
        # PDFs, video, etc.). The classifier still sees a metadata string
        # (filename, MIME, size) so it can route to VISUAL_EVIDENCE; the
        # raw bytes are preserved in attachment.content / file storage for
        # multimodal/binary-aware extractors downstream.
        if _is_binary_content(
            attachment.filename, attachment.content_type, attachment.content
        ):
            content = _binary_placeholder(
                attachment.filename,
                attachment.content_type,
                len(attachment.content),
            )
            logger.info(
                "binary attachment: skipping UTF-8 decode",
                extra={
                    "attachment_filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "size_bytes": len(attachment.content),
                },
            )
        else:
            content = attachment.content.decode("utf-8", errors="replace")

        # Convert dict source_metadata to SourceMetadata for classifier compatibility
        source_meta = None
        if attachment.source_metadata:
            from faultmaven.models.api import SourceMetadata

            source_meta = SourceMetadata(**attachment.source_metadata)

        # Classify and extract structural index.
        # COLD START ORIENTATION: This extractor pass runs for EVERY file
        # regardless of processing mode. In Triage mode, the structural index
        # IS the user-facing answer. In Directed Analysis mode, it serves as
        # internal orientation — a map of the file's contents (time range,
        # services, error distribution) so the DA's LLM can formulate targeted
        # search strategies instead of searching blind. Do NOT skip this step
        # for DA-mode files.
        preprocessing_result = await self.preprocessing_service.classify_and_extract(
            content=content,
            filename=attachment.filename,
            source_metadata=source_meta,
        )

        # Per-case content-hash dedup short-circuit. Post-010: dedup is
        # a file-level concern (uploaded_files), since uploads no longer
        # create an Evidence row at intake. An attachment whose
        # content_hash already exists on this case returns the existing
        # UploadedFile instead of creating a new one. No raw file
        # re-storage either — storage already has the bytes.
        #
        # ``dedup_ran`` records whether the lookup actually produced an answer.
        # It is what separates "ran and found nothing" (novel) from "never ran"
        # (undetermined) downstream; without it both look like
        # ``duplicate_of is None`` and a re-submission is reported as new data
        # (#1210 round 2). Both skip paths log, because a permanently skipped
        # lookup means per-case dedup is not working at all.
        existing_file = None
        dedup_ran = False
        if not preprocessing_result.content_hash:
            logger.warning(
                "No content_hash for '%s' on case %s — per-case dedup could not "
                "run and novelty is UNDETERMINED for this attachment; the turn "
                "is scored conservatively (#1136's upload progress arm will not "
                "arm on it).",
                attachment.filename,
                case.case_id,
            )
        else:
            try:
                existing_file = (
                    await self.repository.find_uploaded_file_by_content_hash(
                        case.case_id, preprocessing_result.content_hash
                    )
                )
                dedup_ran = True
            except AttributeError as e:
                # Two very different things land here: a repository that does
                # not implement the lookup at all (test doubles), and a real
                # implementation raising AttributeError from inside its own
                # body. Neither can be told apart from the outside, and in both
                # the answer is the same — dedup did not run — so this stays a
                # degradation rather than a failure. It is no longer SILENT:
                # swallowing it and reporting the attachment novel is how a
                # broken repository would quietly re-arm the stall net.
                logger.warning(
                    "Per-case dedup lookup unavailable on %s for case %s (%s) — "
                    "novelty is UNDETERMINED for '%s'; the turn is scored "
                    "conservatively and duplicate uploads will not be detected.",
                    type(self.repository).__name__,
                    case.case_id,
                    e,
                    attachment.filename,
                )
        if existing_file is not None:
            logger.info(
                "Duplicate upload detected: file '%s' matches %s (turn %s) "
                "in case %s — reusing existing UploadedFile",
                attachment.filename,
                existing_file.file_id,
                existing_file.uploaded_at_turn,
                case.case_id,
            )
            EVIDENCE_DEDUP_HITS_TOTAL.inc()
            return _PreprocessedAttachment(
                uploaded_file=existing_file,
                duplicate_of=existing_file.file_id,
                duplicate_turn=existing_file.uploaded_at_turn,
                dedup_ran=True,
            )

        # Post-010 strict evidence model: file upload creates only an
        # UploadedFile row (with preprocessing artifacts attached).
        # 1. Store raw content; storage_result.storage_key becomes the
        #    UploadedFile.storage_ref the backend uses to retrieve.
        # 2. Construct UploadedFile carrying file-level metadata
        #    (filename, size, hash, mime, upload provenance).
        # 3. Attach the preprocessing artifacts (summary,
        #    structural_index, data_type, coverage timestamps) — these
        #    describe the file, not any claim about it.
        # 4. No Evidence row is created here; Evidence is born only
        #    when the LLM emits evidence_to_add during INVESTIGATING.
        upload_source = "file_upload"
        if attachment.source_metadata:
            upload_source = attachment.source_metadata.get("source_type", "file_upload")

        storage_ref: Optional[str] = None
        if self.file_storage_service:
            storage_result = await self.file_storage_service.store_file(
                file_data=attachment.content,
                original_filename=attachment.filename,
                organization_id=getattr(case, "organization_id", "default"),
                case_id=case.case_id,
                mime_type=attachment.content_type,
            )
            storage_ref = storage_result.get("storage_key")

        uploaded_file = UploadedFile(
            file_id=f"file_{uuid4().hex[:12]}",
            filename=attachment.filename,
            size_bytes=len(attachment.content),
            content_type=attachment.content_type,
            content_hash=preprocessing_result.content_hash,
            uploaded_at_turn=turn_number,
            uploaded_at=datetime.now(UTC),
            uploaded_by=user_id,
            upload_source=upload_source,
            storage_ref=storage_ref,
        )
        case.uploaded_files.append(uploaded_file)

        # Best-effort sidecar "linked" flag for orphan cleanup. Skipped
        # when storage_ref is None (no storage service or store_file
        # returned nothing); storage services without mark_linked (test
        # doubles, minimal stubs) are handled gracefully.
        mark_linked = (
            getattr(self.file_storage_service, "mark_linked", None)
            if self.file_storage_service
            else None
        )
        if mark_linked is not None and storage_ref:
            try:
                # Check the result, don't just call it: mark_linked reports
                # failure by returning False rather than raising, so without
                # this the warning below could never fire and an at-risk file
                # would be reclaimed at TTL with no operator signal.
                if not await mark_linked(storage_ref):
                    logger.warning(
                        "mark_linked returned False for %s (non-fatal, file "
                        "stays as orphan candidate until TTL)",
                        storage_ref,
                    )
            except Exception as e:
                logger.warning(
                    "mark_linked failed for %s (non-fatal, file stays as "
                    "orphan candidate until TTL): %s",
                    storage_ref,
                    e,
                )

        # Post-010 strict evidence model: write preprocessing artifacts
        # to the UploadedFile row where they semantically belong (they
        # describe the FILE, not any claim about it). NO Evidence row
        # is created at this intake step — Evidence is born only when
        # the LLM extracts a claim-anchored slice via evidence_to_add
        # during INVESTIGATING.
        uploaded_file.summary = preprocessing_result.summary
        uploaded_file.structural_index = preprocessing_result.structural_index
        uploaded_file.data_type = _infer_source_type(
            preprocessing_result.detailed_data_type
        ).value
        uploaded_file.coverage_start_ts = preprocessing_result.coverage_start_ts
        uploaded_file.coverage_end_ts = preprocessing_result.coverage_end_ts

        # Fall back to the caller's declared observation time when the content
        # carries no parseable timestamps of its own. Alert notifications are
        # the motivating case: an Alertmanager Slack message is one prose line
        # with no embedded timestamp, so the extractor finds nothing and the
        # file's coverage is NULL — leaving ingestion time as the only temporal
        # signal anywhere on the evidence, which reads a two-hour-old alert as
        # current.
        #
        # Parsed content ALWAYS wins: it describes what the data actually
        # spans, while `observed_at` is only the caller's statement about when
        # it saw the content. Both-or-neither, never a half-open span — a start
        # without an end would make the row look like it covers up to now.
        if (
            attachment.observed_at is not None
            and uploaded_file.coverage_start_ts is None
            and uploaded_file.coverage_end_ts is None
        ):
            uploaded_file.coverage_start_ts = attachment.observed_at
            uploaded_file.coverage_end_ts = attachment.observed_at
            logger.info(
                "Seeded coverage for %s from caller-declared observed_at %s "
                "(content had no parseable timestamps)",
                uploaded_file.file_id,
                attachment.observed_at.isoformat(),
            )

        # Preprocessor diagnostics (classifier confidence, extractor
        # attempts, entity overflow markers) have no claim-anchored
        # Evidence to land on at intake; their natural home is
        # ``uploaded_files.metadata`` (JSON blob). Tracked as a follow-up
        # — no currently-shipping feature regresses.

        # ``case_entities`` population is deferred. Entities should either
        # anchor to the UploadedFile (schema change) or be populated lazily
        # when the LLM creates ``evidence_to_add`` rows referencing this
        # file. The data is still in ``preprocessing_result.entities`` for
        # any reader that wants it.

        # Surface classification clarification hints when the heuristic
        # classifier produced a low-confidence result. Suggested types are
        # propagated by PreprocessingService via extraction_metadata as a
        # list of DataType string values.
        is_classification_failed = (
            preprocessing_result.extraction_method == "classification_failed"
        )
        suggested_types: Optional[List[str]] = None
        if is_classification_failed:
            suggested_types = (
                preprocessing_result.extraction_metadata.get("suggested_types") or []
            )

        # Commit the row NOW, on its own, rather than letting it ride along on
        # the end-of-turn ``save(case)``.
        #
        # An upload is a user-initiated fact: the bytes are already in storage
        # (``store_file`` above), and whether this turn's LLM later succeeds has
        # no bearing on whether the user uploaded the file. When the row waited
        # for the aggregate save, a turn that raised left the bytes stored with
        # nothing referencing them — and ``mark_linked`` had already exempted
        # them from TTL reclaim, so the orphan was permanent rather than
        # self-clearing. The retry then stored a second copy, because
        # ``find_uploaded_file_by_content_hash`` cannot dedup against a row that
        # was never written.
        #
        # Committed here, at the end, so the row carries its preprocessing
        # artifacts and seeded coverage rather than a bare stub. Scoped rather
        # than ``save(case)`` because the aggregate save commits the whole case,
        # and mid-turn that would make the half-built turn durable — the very
        # thing deferring the save exists to avoid. The underlying
        # ``_upsert_uploaded_files`` is purely additive, so the end-of-turn
        # aggregate save re-upserts this row rather than removing it.
        add_uploaded_file = getattr(self.repository, "add_uploaded_file", None)
        if add_uploaded_file is not None:
            try:
                await add_uploaded_file(
                    case.case_id,
                    uploaded_file,
                    getattr(case, "organization_id", "default"),
                )
            except Exception as e:
                # Degrade to the previous behaviour (the row rides the
                # end-of-turn save) rather than failing the upload outright —
                # but say so. Silence here would turn a durability regression
                # into an invisible one.
                logger.warning(
                    "Scoped commit of uploaded_file %s on case %s failed: %s. "
                    "The row now depends on the end-of-turn save; if this turn "
                    "fails, the stored bytes are orphaned.",
                    uploaded_file.file_id,
                    case.case_id,
                    e,
                )
        else:
            # WARNING, not DEBUG. `add_uploaded_file` is an @abstractmethod on
            # CaseRepository and a member of the ICaseRepository Protocol, so in
            # production this branch is unreachable — reaching it means either a
            # test double or that the contract method was renamed without
            # updating this call site. Both revert every upload to the orphaning
            # behaviour this code exists to prevent, which is not a debug-level
            # event. (`test_service_calls_the_contract_method_name` pins the
            # name against a silent rename.)
            logger.warning(
                "Repository %s has no add_uploaded_file — uploads fall back to "
                "the end-of-turn save and are orphaned if the turn fails. "
                "uploaded_file=%s",
                type(self.repository).__name__,
                uploaded_file.file_id,
            )

        return _PreprocessedAttachment(
            uploaded_file=uploaded_file,
            dedup_ran=dedup_ran,
            classification_failed=is_classification_failed,
            suggested_types=suggested_types,
            attachment_filename=attachment.filename,
        )

    # ============================================================
    # Intent-Based Query Handlers
    # ============================================================

    async def _handle_status_transition(
        self,
        case: "Case",
        user_message: str,
        from_state: Optional[str],
        to_state: Optional[str],
        user_confirmed: bool,
        user_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Handle status transition intent with validation.

        Args:
            case: Case entity
            user_message: User's message explaining the transition
            from_state: Expected current status
            to_state: Requested new status
            user_confirmed: Whether user confirmed the transition
            user_id: Authenticated principal for the turn (keys the agent's
                KB read allowlist)
            attachments: The turn's engine attachment metadata. Passed through
                verbatim — this handler used to hardcode ``None`` while
                ``_preprocess_attachment`` had already committed a row for every
                attachment, so an upload riding a dropdown/chip intent was
                invisible to the engine (#1229).

        Returns:
            Result dict with agent response and updated case
        """
        logger.info(
            f"Processing status transition: {from_state} → {to_state} "
            f"(confirmed={user_confirmed}) for case {case.case_id}"
        )

        # Validate transition request
        if not to_state:
            raise ValidationException(
                "to_state is required for status_transition intent",
                {"field": "to_state"},
            )

        # Delegate to milestone engine with structured intent
        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=attachments,
            intent_type="status_transition",
            intent_data={
                "from_state": from_state,
                "to_state": to_state,
                "user_confirmed": user_confirmed,
            },
            user_id=user_id,
        )

        return result

    @staticmethod
    def _minted_intent_swallows_terminal_consent(
        case: "Case", minted: QueryIntent, user_message: str
    ) -> bool:
        """INV-26 guard for resolver-minted intents (#721).

        True when adopting ``minted`` would let a SUBSTANTIVE typed message
        confirm the case's pending TERMINAL transition. The IntentResolver's
        classifier tier semantically matches typed text against the previous
        turn's DECIDE suggestions and can mint ``confirmation``/
        ``status_transition`` intents — but the engine treats those intents
        as deterministic consent (the DECIDE-click path) and consults them
        BEFORE its INV-26 bare-token guards. A click IS deterministic
        consent; an inference from typed text is not. So a minted intent
        that would confirm a pending RESOLVED/CLOSED must pass the same
        substance test the typed-confirmation matcher applies
        (``is_substantive_reply`` — shared single source of truth): "yes but
        what about the replication lag?" is substantive input, never consent
        to an irreversible transition.

        Only confirm-shaped mints over a pending terminal transition are
        guarded. Declines, mints with no pending transition (e.g. Gate 1
        problem-statement confirmation), and contradicting status
        transitions (which merely cancel the pending) adopt as before —
        none of them can execute a terminal transition.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            is_substantive_reply,
        )

        pending = getattr(case, "pending_transition", None)
        if not pending:
            return False

        confirms_pending = (
            minted.type == IntentType.CONFIRMATION and minted.confirmation_value is True
        ) or (
            minted.type == IntentType.STATUS_TRANSITION
            and minted.to_state is not None
            and minted.to_state.value == pending.get("to_state")
        )
        return confirms_pending and is_substantive_reply(user_message)

    async def _handle_confirmation(
        self,
        case: "Case",
        user_message: str,
        confirmation_value: Optional[bool],
        user_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Handle yes/no confirmation intent.

        Args:
            case: Case entity
            user_message: User's confirmation message
            confirmation_value: True for yes, False for no
            user_id: Authenticated principal for the turn (keys the agent's
                KB read allowlist)
            attachments: The turn's engine attachment metadata (see
                ``_handle_status_transition``; #1229).

        Returns:
            Result dict with agent response and updated case
        """
        logger.info(
            f"Processing confirmation: {confirmation_value} for case {case.case_id}"
        )

        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=attachments,
            intent_type="confirmation",
            intent_data={"value": confirmation_value},
            user_id=user_id,
        )

        return result

    async def _handle_hypothesis_action(
        self,
        case: "Case",
        user_message: str,
        hypothesis_id: Optional[str],
        action: Optional[str],
        user_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Handle hypothesis action intent (validate/refute/retire).

        Args:
            case: Case entity
            user_message: User's message about the hypothesis
            hypothesis_id: Target hypothesis ID
            action: Action to perform
            user_id: Authenticated principal for the turn (keys the agent's
                KB read allowlist)
            attachments: The turn's engine attachment metadata (see
                ``_handle_status_transition``; #1229).

        Returns:
            Result dict with agent response and updated case
        """
        logger.info(
            f"Processing hypothesis action: {action} on {hypothesis_id} for case {case.case_id}"
        )

        if not hypothesis_id or not action:
            raise ValidationException(
                "hypothesis_id and action required for hypothesis_action intent",
                {"field": "hypothesis_id" if not hypothesis_id else "action"},
            )

        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=attachments,
            intent_type="hypothesis_action",
            intent_data={"hypothesis_id": hypothesis_id, "action": action},
            user_id=user_id,
        )

        return result

    def _build_progress_transparency(
        self, metadata: Dict[str, Any], case: "Case"
    ) -> Optional[ProgressTransparencyInfo]:
        """Build ProgressTransparencyInfo from turn metadata.

        ``verification_status`` carries the engine's persisted assessment for
        the turn (the grounding × progress join) so the frontend can surface the
        honest partial outcome — e.g. ``insufficient_evidence`` — alongside the
        stalled-milestone info.

        Emitted when transparent mode is active (stalled-milestone surfacing)
        **or** when the status is one of the honest-partial readings. The latter
        is decoupled from ``progress_transparent`` on purpose: a declared data
        wall reaches ``INSUFFICIENT_EVIDENCE`` *before* the time-stall thresholds
        that drive transparent mode, so gating the status on that flag would hide
        the very outcome the frontend needs to show. ``active`` still reflects
        transparent mode only.
        """
        verification_status = None
        cause_assurance = None
        if case.progress:
            if case.progress.verification_status:
                verification_status = case.progress.verification_status.value
            if case.progress.cause_assurance:
                cause_assurance = case.progress.cause_assurance.value

        transparent = bool(metadata.get("progress_transparent"))
        # Every engine-driven honest-partial reading is surfaced independently of
        # transparent mode, for the same reason: each can be reached on a turn
        # that never activates it, and gating on the flag would hide the outcome
        # the frontend exists to show. ``INSUFFICIENT_EVIDENCE`` via the declared
        # data wall (which fires before the time thresholds); ``TREATMENT_BLOCKED``
        # (#1136) because a case parked on an unapplied fix is conversational —
        # transparent mode counts investigative turns, so a fix-blocked stall can
        # sit in that cell for turns on end without ever tripping it;
        # ``RESTATEMENT_HELD`` (#1195) because it is carved OUT of
        # ``INSUFFICIENT_EVIDENCE`` — omitting it would silence, in this channel,
        # exactly the cases that channel used to (wrongly) report, which is the
        # suppression-without-replacement failure that fix exists to avoid.
        surface_honest_partial = verification_status in (
            VerificationStatus.INSUFFICIENT_EVIDENCE.value,
            VerificationStatus.TREATMENT_BLOCKED.value,
            VerificationStatus.RESTATEMENT_HELD.value,
        )
        if not transparent and not surface_honest_partial:
            return None

        return ProgressTransparencyInfo(
            active=transparent,
            pending_milestone=metadata.get("pending_milestone"),
            milestone_description=metadata.get("milestone_description"),
            repair_type=metadata.get("stagnation_type"),
            verification_status=verification_status,
            cause_assurance=cause_assurance,
        )

    async def _handle_file_reclassification(
        self,
        case: "Case",
        file_id: Optional[str],
        data_type_value: Optional[str],
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Resolve a classification_failed upload by reclassifying its file.

        Engine-owned resolution for the classification-clarification
        suggestions (see ``_build_classification_clarification``):
        the user's click/typed choice arrives as a ``file_reclassification``
        intent carrying the UploadedFile ID and the target DataType. The
        handler re-runs preprocessing under ``user_override`` and updates the
        file's artifacts — mechanically, with no LLM call, so the choice can
        never be misread as an analysis request.

        Post-010: preprocessing artifacts (data_type, summary,
        structural_index) land on the UploadedFile. Any Evidence rows already
        backed by this file get their ``source_type`` re-aligned; usually
        there are none at clarification time (Evidence is born later, during
        INVESTIGATING).

        Returns:
            Result dict with deterministic agent response and updated case.

        Raises:
            ValidationException: terminal case, missing/unknown intent
                fields, or the file has no stored raw bytes to re-extract
                (→ 422).
            NotFoundError: file_id not in this case, or the stored blob is
                gone from storage (→ 404).
            ServiceException: storage/preprocessing service unavailable
                (→ 500).
        """
        # Terminal guard. The other SERVICE intents inherit terminal
        # protection by delegating to engine.process_turn (which
        # short-circuits terminal cases to Q&A); this handler never reaches
        # the engine, so it must refuse mutation itself — a stale
        # clarification button or a direct POST must not rewrite a closed
        # case's files/evidence.
        if case.is_terminal:
            raise ValidationException(
                "Cannot reclassify files on a closed case — the "
                "investigation is terminal; only questions about the case "
                "are accepted.",
                {"case_state": case.state.value},
            )
        if not file_id or not data_type_value:
            raise ValidationException(
                "file_id and data_type required for file_reclassification intent",
                {"field": "file_id" if not file_id else "data_type"},
            )
        try:
            data_type = DataType(data_type_value)
        except ValueError:
            valid = ", ".join(t.value for t in DataType)
            raise ValidationException(
                f"Unknown data_type '{data_type_value}'. Valid: {valid}",
                {"field": "data_type"},
            )

        logger.info(
            f"Processing file reclassification: {file_id} → {data_type.value} "
            f"for case {case.case_id}"
        )

        file_index = next(
            (
                i
                for i, uf in enumerate(case.uploaded_files or [])
                if uf.file_id == file_id
            ),
            None,
        )
        if file_index is None:
            raise NotFoundError("UploadedFile", file_id)
        file_meta = case.uploaded_files[file_index]

        if not file_meta.storage_ref:
            raise ValidationException(
                f"Uploaded file {file_id} has no stored raw content — "
                "reclassification requires re-running the extractor over "
                "the original bytes.",
                {"field": "file_id"},
            )
        # NotFoundError from storage (blob missing) passes through process_turn
        # unwrapped → 404, never a 5xx on a clicked suggestion.
        preprocessing_result, new_source_type = await self._reextract_under_override(
            file_meta, data_type
        )
        previous_type = file_meta.data_type or "unknown"

        new_files_list = list(case.uploaded_files)
        new_files_list[file_index] = _file_row_with_reclassification(
            file_meta, preprocessing_result, new_source_type
        )

        # Re-align Evidence rows already backed by this file (claim content —
        # the LLM-authored summary/extract — stays untouched).
        new_evidence_list = list(case.evidence or [])
        for i, ev in enumerate(new_evidence_list):
            if ev.source_file_id == file_id:
                new_evidence_list[i] = ev.model_copy(
                    update={"source_type": new_source_type}, deep=True
                )

        # Shallow copy with the replaced collections. ``messages`` gets a
        # fresh list because process_turn appends the agent message to the
        # returned case; every other field is only ever reassigned, never
        # mutated in place, so sharing by reference is safe — and skips
        # deep-copying the whole case (messages, hypotheses, causal graph)
        # on a mechanical click path.
        updated_case = case.model_copy(
            update={
                "uploaded_files": new_files_list,
                "evidence": new_evidence_list,
                "messages": list(case.messages),
            }
        )

        EVIDENCE_RECLASSIFICATION_TOTAL.labels(
            from_type=str(previous_type),
            to_type=preprocessing_result.data_type.value,
            trigger="clarification",
        ).inc()

        subject = _upload_subject(file_meta)
        friendly = _CLARIFICATION_FRIENDLY_NAMES.get(data_type.value, {}).get(
            "long"
        ) or data_type.value.replace("_", " ")
        agent_response = f"Got it — I've recorded {subject} as {friendly}."
        if preprocessing_result.summary:
            agent_response += f"\n\n{preprocessing_result.summary}"

        return {
            "agent_response": agent_response,
            "suggested_follow_ups": [
                {
                    "label": "Analyze it now",
                    "action_type": "DECIDE",
                    # The identifier, not ``subject``: this payload is
                    # replayed as a standalone turn, where "the text you
                    # pasted" has no antecedent. The sentence above it is in
                    # conversation and keeps the prose form.
                    "payload": f'Analyze "{file_meta.display_name}".',
                    "body": "Run the analysis with the corrected classification.",
                }
            ],
            "case_updated": updated_case,
            "metadata": {
                "progress_made": False,
                "milestones_completed": [],
                "file_reclassified": {
                    "file_id": file_id,
                    "from_type": str(previous_type),
                    "to_type": new_source_type.value,
                },
                **report_turn_uploads(case.case_id, case.current_turn, attachments),
            },
        }

    async def _handle_greeting(
        self,
        case: "Case",
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Handle greeting intent without LLM.

        Args:
            case: Case entity
            attachments: The turn's engine attachment metadata (#1229).
                Normally empty here — the heuristic that mints this intent is
                now barred from firing on a turn that carried an attachment,
                and an explicit client-sent GREETING with a file is the only
                way to arrive with one. Reported anyway, because "normally
                empty" is not "provably empty" and a dropped signal is exactly
                what #1229 is about.

        Returns:
            Result dict with static agent response and updated case
        """
        logger.info(f"Processing greeting for case {case.case_id}")

        # Static response (saving tokens and latency)
        agent_response = (
            "Hello! I'm FaultMaven, your AI-powered troubleshooting copilot. "
            "I can help you diagnose issues, analyze logs, and verify solutions. "
            "Please describe the problem you're observing."
        )

        # No engine call needed - manually construct result
        return {
            "agent_response": agent_response,
            "suggested_follow_ups": [
                {
                    "label": "Describe your issue",
                    "action_type": "FREE_SPEECH",
                    "hints": [
                        "symptoms",
                        "error messages",
                        "timeline",
                        "affected services",
                    ],
                },
                {
                    "label": "Share error logs from the affected service",
                    "action_type": "EVIDENCE",
                    "body": "Error logs will help identify the root cause faster.",
                },
            ],
            "case_updated": case,
            "metadata": {
                # These two handlers never reach the engine, so they report the
                # turn's uploads but do not write engine-owned state: no
                # progress flag, no ``turns_without_progress`` touch. That is
                # self-consistent — the flag says False and the counter is
                # unchanged, which agree — where a True flag beside an untouched
                # counter would be the disagreement #1229 exists to remove.
                # ``_check_if_progress_made`` is the sole writer of that
                # counter and it lives in the engine; a service-side second
                # writer is the two-derivations shape, not a fix for it.
                "progress_made": False,
                "milestones_completed": [],
                **report_turn_uploads(case.case_id, case.current_turn, attachments),
            },
        }

    def _detect_intent_heuristic(self, message: str) -> Optional[IntentType]:
        """Detect intent from message content using simple heuristics.

        Args:
            message: User message text

        Returns:
            Detected IntentType or None
        """
        clean_msg = message.strip().lower()

        # Greeting patterns (case-insensitive)
        # Matches: "Hi", "Hello", "Hi FaultMaven", "Greetings", "Help"
        # Does NOT match: "Hi, the db is down", "Hello, I have an error"
        greeting_pattern = r"^(hi|hello|hey|greetings|help)( faultmaven)?[\.!]*$"

        if re.match(greeting_pattern, clean_msg):
            return IntentType.GREETING

        return None

    @trace("investigation_service_transition_to_investigating")
    async def transition_to_investigating(
        self, case_id: str, user_id: str, confirmed_description: str
    ) -> Case:
        """
        Transition case from INQUIRY to INVESTIGATING.

        Called when user confirms the problem statement during inquiry phase.

        Args:
            case_id: Case identifier
            user_id: User making the request
            confirmed_description: Confirmed problem description

        Returns:
            Updated case

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
            ServiceException: If transition fails or invalid state
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            # Check permissions
            if case.user_id != user_id:
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # Validate current status
            if case.state != CaseState.INQUIRY:
                raise ServiceException(
                    f"Cannot transition to INVESTIGATING: case is in {case.state.value} status"
                )

            # Ensure inquiry data is properly set for INVESTIGATING transition
            if not case.inquiry.proposed_problem_statement:
                # Use confirmed_description as the problem statement
                case.inquiry.proposed_problem_statement = confirmed_description

            if not case.inquiry.problem_statement_confirmed:
                case.inquiry.problem_statement_confirmed = True
                case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)

            if not case.inquiry.decided_to_investigate:
                case.inquiry.decided_to_investigate = True
                case.inquiry.decision_made_at = datetime.now(timezone.utc)

            # Update case
            case.description = confirmed_description
            case.state = CaseState.INVESTIGATING

            # Save
            updated_case = await self.repository.save(case)

            logger.info(
                f"Transitioned case {case_id} to INVESTIGATING with description: "
                f"{confirmed_description[:100]}..."
            )

            return updated_case

        except (NotFoundError, PermissionDeniedException, ServiceException):
            raise
        except Exception as e:
            logger.error(f"Failed to transition case {case_id} to INVESTIGATING: {e}")
            raise ServiceException(f"Status transition failed: {str(e)}") from e

    @trace("investigation_service_reclassify_evidence")
    async def reclassify_evidence(
        self,
        case_id: str,
        evidence_id: str,
        user_id: str,
        data_type: DataType,
        trigger: str = "api",
    ) -> Evidence:
        """Re-run preprocessing on the file behind an existing evidence row
        under a user-specified data type.

        Phase 1.5 — implements the "escape hatch" for confident
        misclassification. The caller (PATCH endpoint or
        ``reclassify_evidence`` agent tool) provides the new data type;
        this method fetches the stored raw bytes, re-runs extraction
        under ``user_override=data_type``, and updates the **backing
        UploadedFile**'s preprocessing artifacts (``data_type``,
        ``summary``, ``structural_index``) — these live with the file,
        not on Evidence. The Evidence row's ``source_type`` is
        re-aligned so it stays consistent with the file's new
        classification, but the LLM-authored ``summary`` and ``extract``
        fields on Evidence are left untouched (they are claim content,
        not preprocessing output).

        Args:
            case_id: Case owning the evidence.
            evidence_id: Evidence being reclassified.
            user_id: User making the request (authorisation check).
            data_type: Target data type (DataType enum value).
            trigger: Where the request came from — ``api`` (direct
                PATCH) or ``agent_tool`` (reclassify_evidence tool).
                Labels the observability counter.

        Returns:
            The updated Evidence row with the re-aligned
            ``source_type``. The structural_index / summary / data_type
            updates land on the backing UploadedFile in the same case.

        Raises:
            NotFoundError: case or evidence not found. Mapped to HTTP 404
                by the global exception handler.
            AuthorizationError: user does not own the case. Mapped to
                HTTP 403.
            ConflictError: evidence has no backing file —
                reclassification requires stored raw bytes to re-extract.
                Mapped to HTTP 409 with
                ``conflict_reason="no_backing_file"`` in the body so
                callers can branch programmatically.
            ServiceException: any other failure (storage fetch,
                preprocessing). Mapped to HTTP 500.
        """
        case = await self.repository.get(case_id)
        if not case:
            raise NotFoundError("Case", case_id)
        if case.user_id != user_id:
            raise AuthorizationError(
                f"User {user_id} not authorized for case {case_id}"
            )

        evidence_index: Optional[int] = None
        for i, ev in enumerate(case.evidence or []):
            if ev.evidence_id == evidence_id:
                evidence_index = i
                break
        if evidence_index is None:
            raise NotFoundError("Evidence", evidence_id)

        evidence = case.evidence[evidence_index]
        file_meta = case.find_uploaded_file(evidence.source_file_id)
        storage_ref = file_meta.storage_ref if file_meta else None
        if not storage_ref:
            raise ConflictError(
                f"Evidence {evidence_id} has no stored raw file — "
                "reclassification requires re-running the extractor "
                "over the original content, which is not available for "
                "evidence that was created without file storage.",
                resource_type="evidence",
                resource_id=evidence_id,
                conflict_reason="no_backing_file",
            )
        # storage_ref non-None (checked above) implies file_meta is present.
        preprocessing_result, new_source_type = await self._reextract_under_override(
            file_meta, data_type, previous_metadata=evidence.metadata
        )

        # Lift the updated evidence_metadata block from the result.
        pp_metadata = preprocessing_result.extraction_metadata
        new_evidence_metadata: Optional[Dict[str, Any]] = None
        if isinstance(pp_metadata, dict):
            candidate = pp_metadata.get("evidence_metadata")
            if isinstance(candidate, dict):
                new_evidence_metadata = candidate

        previous_type = evidence.source_type.value
        new_type = preprocessing_result.data_type.value

        # Post-010 routing: preprocessing artifacts (data_type, summary,
        # structural_index) describe the FILE and land on
        # ``uploaded_files``. Evidence carries the LLM's claim — we only
        # re-align ``source_type`` so the agent sees consistent data on
        # the next turn. The LLM-authored ``summary`` and ``extract``
        # fields on Evidence are left untouched: they are claim content,
        # not preprocessing output.
        file_index = next(
            (
                i
                for i, uf in enumerate(case.uploaded_files or [])
                if uf.file_id == evidence.source_file_id
            ),
            None,
        )
        new_files_list = list(case.uploaded_files or [])
        if file_index is not None:
            new_files_list[file_index] = _file_row_with_reclassification(
                file_meta, preprocessing_result, new_source_type
            )

        updated_evidence = evidence.model_copy(
            update={
                "source_type": new_source_type,
                "metadata": new_evidence_metadata,
            },
            deep=True,
        )
        new_evidence_list = list(case.evidence)
        new_evidence_list[evidence_index] = updated_evidence
        updated_case = case.model_copy(
            update={
                "evidence": new_evidence_list,
                "uploaded_files": new_files_list,
            },
            deep=True,
        )

        await self.repository.save(updated_case)

        EVIDENCE_RECLASSIFICATION_TOTAL.labels(
            from_type=str(previous_type or "unknown"),
            to_type=new_type,
            trigger=trigger,
        ).inc()

        logger.info(
            "Reclassified evidence %s in case %s: %s -> %s (trigger=%s)",
            evidence_id,
            case_id,
            previous_type,
            new_type,
            trigger,
        )

        return updated_evidence

    async def _reextract_under_override(
        self,
        file_meta: "UploadedFile",
        data_type: DataType,
        previous_metadata: Optional[Dict[str, Any]] = None,
    ):
        """Retrieve the stored raw bytes behind *file_meta* and re-run
        preprocessing under ``user_override=data_type``.

        Shared mechanics of both reclassification paths — the PATCH /
        agent-tool ``reclassify_evidence`` and the clarification-intent
        ``_handle_file_reclassification``. Callers own target lookup,
        authorization, terminal/conflict policy, persistence, and
        response shape.

        Returns:
            ``(preprocessing_result, new_source_type)`` where the source
            type is inferred from the result's fine-grained
            ``detailed_data_type`` (the coarse UnifiedDataType in
            ``data_type`` never matches the source-type map's keys).

        Raises:
            ServiceException: storage/preprocessing service unavailable.
            NotFoundError: stored blob missing from storage.
        """
        if not self.file_storage_service:
            raise ServiceException(
                "File storage service unavailable; cannot re-extract"
            )
        if not self.preprocessing_service:
            raise ServiceException(
                "Preprocessing service unavailable; cannot reclassify"
            )

        # Fetch raw bytes + decode. Storage returns bytes; extractors
        # operate on strings (UTF-8 is the convention per the upload path).
        # Skip the destructive decode for binary content (see
        # _is_binary_content).
        raw_bytes = await self.file_storage_service.retrieve_file(file_meta.storage_ref)
        filename = file_meta.filename or "the uploaded file"
        if _is_binary_content(filename, file_meta.content_type, raw_bytes):
            content = _binary_placeholder(
                filename, file_meta.content_type, len(raw_bytes)
            )
            logger.info(
                "binary content: skipping UTF-8 decode on reclassify",
                extra={"filename": filename, "size_bytes": len(raw_bytes)},
            )
        else:
            content = raw_bytes.decode("utf-8", errors="replace")

        preprocessing_result = await self.preprocessing_service.reclassify_evidence(
            content=content,
            filename=filename,
            user_override=data_type,
            previous_metadata=previous_metadata,
        )
        return preprocessing_result, _infer_source_type(
            preprocessing_result.detailed_data_type
        )
