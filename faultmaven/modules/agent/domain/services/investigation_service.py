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
from typing import Any, Dict, List, Optional

from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.prompts.context_builder import (
    structural_index_is_searchable,
)
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.investigation.turn_pipeline import (
    generate_implicit_query,
    submitted_name,
)
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
from faultmaven.modules.case.contracts import Case, CaseState, VerificationStatus
from faultmaven.modules.case.contracts import ICaseRepository as CaseRepository
from faultmaven.modules.case.domain.models import (
    Evidence,
    EvidenceSourceType,
    UploadedFile,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)

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


def _clarification_label_qualifier(target: "_PreprocessedAttachment") -> str:
    """Short name telling one failed attachment's choices from another's.

    The *button* register — third and shortest of the three ways this
    codebase names an attachment back to the user, beside
    ``UploadedFile.display_name`` (citable, carries a turn number) and
    ``UploadedFile.submission_phrase`` (sentence, carries a clause). A button
    has room for neither, so this is the bare noun. Derived from the same
    provenance properties as its two siblings; keep the wording in step.
    """
    uf = target.uploaded_file
    if uf.is_page_capture:
        return "captured page"
    if uf.is_pasted:
        return "pasted text"
    return target.attachment_filename or uf.filename or "the uploaded file"


def _reclassification_intent(file_id: str, dt_value: str) -> Dict[str, Any]:
    """The engine-owned intent a clarification choice carries."""
    return {
        "type": IntentType.FILE_RECLASSIFICATION.value,
        "file_id": file_id,
        "data_type": dt_value,
    }


def _build_classification_clarification_suggestions(
    preprocess_results: List["_PreprocessedAttachment"],
) -> List[SuggestedActionResponse]:
    """Emit DECIDE suggestions for EVERY attachment that hit
    classification_failed.

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

    Labels carry the attachment's short name
    (``_clarification_label_qualifier``) **only** when more than one
    attachment failed: two cards both reading "Documentation" are
    indistinguishable on screen, and ``IntentResolver._exact_match`` matches
    a typed label against the choices in order — so it would resolve an
    answer meant for the paste onto the file, turning a missing option into
    a wrong action. A single failure keeps the bare label it has always had.
    The qualifiers are distinct because a turn mints at most ONE synthetic
    name (#1198): ``pasted_content`` is a single form field, so a turn
    carries one paste or one capture, never two, and everything else is a
    user-chosen filename.

    Each suggestion carries an engine-owned ``file_reclassification`` intent
    (file_id + target DataType) so any client that forwards suggestion intent
    on click — the cross-client contract — resolves the choice through the
    structured reclassification handler, never as a free-text turn the LLM
    might act on literally (e.g. by deep-analyzing the file instead of
    re-labeling it). The ``payload`` remains the human-readable record of the
    choice; intent routing takes precedence over it server-side.

    Returns an empty list when no classification failure occurred this turn.
    """
    failed = [r for r in preprocess_results if r.classification_failed]
    if not failed:
        return []

    qualify = len(failed) > 1
    suggestions: List[SuggestedActionResponse] = []

    for target in failed:
        subject = _clarification_subject(target)
        file_id = target.uploaded_file.file_id
        suffix = f" ({_clarification_label_qualifier(target)})" if qualify else ""
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


def _classification_clarification_note(
    preprocess_results: List["_PreprocessedAttachment"],
) -> Optional[str]:
    """The narration bridge for this turn's clarification choices, or None.

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
    subjects = [
        _clarification_subject(r) for r in preprocess_results if r.classification_failed
    ]
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
    ``_build_classification_clarification_suggestions``.
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

            # ── STEP 2: LLM INFERENCE ──
            # Heuristic check for greetings if intent is CONVERSATION (default)
            if intent_type == IntentType.CONVERSATION and query:
                heuristic_intent = self._detect_intent_heuristic(query)
                if heuristic_intent:
                    intent_type = heuristic_intent
                    logger.info(
                        f"Heuristic detected intent {intent_type.value} for message: '{query}'"
                    )

            # Intent resolution: match typed text against last turn's suggestions.
            # Only runs when no structured intent and the case has suggestions
            # with intent metadata from the previous turn.
            if (
                intent_type == IntentType.CONVERSATION
                and query
                and not payload.has_attachments
                and case.last_suggestions
            ):
                resolved_intent = await self.intent_resolver.resolve(
                    user_message=query,
                    last_suggestions=case.last_suggestions,
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
                    )
                elif intent_type == IntentType.CONFIRMATION:
                    result = await self._handle_confirmation(
                        case=case,
                        user_message=query or "",
                        confirmation_value=(
                            intent.confirmation_value if intent else None
                        ),
                        user_id=user_id,
                    )
                elif intent_type == IntentType.HYPOTHESIS_ACTION:
                    result = await self._handle_hypothesis_action(
                        case=case,
                        user_message=query or "",
                        hypothesis_id=intent.hypothesis_id if intent else None,
                        action=intent.action if intent else None,
                        user_id=user_id,
                    )
                elif intent_type == IntentType.GREETING:
                    result = await self._handle_greeting(case=case)
                elif intent_type == IntentType.FILE_RECLASSIFICATION:
                    result = await self._handle_file_reclassification(
                        case=case,
                        file_id=intent.file_id if intent else None,
                        data_type_value=intent.data_type if intent else None,
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
                # Build attachment metadata for the engine. Post-010:
                # uploads create only an UploadedFile (no auto-Evidence),
                # so the file facts are sourced directly from those rows —
                # the #1201 fix: the row is the record of what was submitted,
                # its filename is not.
                #
                # Walks ``preprocess_results`` rather than
                # ``uploaded_files_this_turn`` (the same set, in the same
                # order — one result per attachment, and the list above is
                # built from ``result.uploaded_file``) because the result also
                # carries ``duplicate_of``, the only place novelty is still
                # knowable by the time the engine runs (#1210).
                attachment_metadata = [
                    _engine_attachment_metadata(res) for res in preprocess_results
                ]
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
            clarification = _build_classification_clarification_suggestions(
                preprocess_results
            )
            # Narration bridge — see ``_classification_clarification_note``.
            # Derived from the same predicate as the suggestions above, so
            # the note names exactly the attachments the choices target.
            clarification_note = _classification_clarification_note(preprocess_results)
            if clarification_note:
                agent_response_text += clarification_note
            raw_follow_ups = result.get("suggested_follow_ups", [])
            updated_case.last_suggestions = (
                [
                    {
                        "label": s.label,
                        "action_type": s.type,
                        "payload": s.payload,
                        "body": s.body,
                        "intent": s.intent,
                    }
                    for s in clarification
                ]
                + [s for s in raw_follow_ups if s.get("intent")]
            ) or None

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
            raise
        except Exception as e:
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
            attachments=None,
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
    ) -> Dict[str, Any]:
        """Handle yes/no confirmation intent.

        Args:
            case: Case entity
            user_message: User's confirmation message
            confirmation_value: True for yes, False for no
            user_id: Authenticated principal for the turn (keys the agent's
                KB read allowlist)

        Returns:
            Result dict with agent response and updated case
        """
        logger.info(
            f"Processing confirmation: {confirmation_value} for case {case.case_id}"
        )

        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=None,
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
    ) -> Dict[str, Any]:
        """Handle hypothesis action intent (validate/refute/retire).

        Args:
            case: Case entity
            user_message: User's message about the hypothesis
            hypothesis_id: Target hypothesis ID
            action: Action to perform
            user_id: Authenticated principal for the turn (keys the agent's
                KB read allowlist)

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
            attachments=None,
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
    ) -> Dict[str, Any]:
        """Resolve a classification_failed upload by reclassifying its file.

        Engine-owned resolution for the classification-clarification
        suggestions (see ``_build_classification_clarification_suggestions``):
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
            },
        }

    async def _handle_greeting(self, case: "Case") -> Dict[str, Any]:
        """Handle greeting intent without LLM.

        Args:
            case: Case entity

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
                "progress_made": False,
                "milestones_completed": [],
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
