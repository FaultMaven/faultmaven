"""Attachment preprocessing helpers: binary detection, the per-turn
preprocessed-attachment record, and the evidence-bearing reroute.

Owns everything about turning a raw upload into what the rest of the
investigation service reasons about: whether its bytes are binary (so the
UTF-8 decode is skipped in favour of a metadata-only placeholder), the
``_PreprocessedAttachment`` record ``_preprocess_attachment`` builds, the
per-attachment dict handed to ``engine.process_turn``, and the processing-mode
reroute a turn carrying evidence forces.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import List, Optional

from faultmaven.core.investigation.coverage_trust import CALLER_DECLARED_COVERAGE_SOURCE
from faultmaven.core.investigation.prompts.context_builder.budget import (
    structural_index_is_searchable,
)
from faultmaven.core.investigation.schemas import Attachment
from faultmaven.core.investigation.turn_pipeline import generate_implicit_query
from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_DEDUP_HITS_TOTAL,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
    _record_mark_linked_failure,
)
from faultmaven.modules.agent.domain.services.query_classifier import (
    ProcessingMode,
    QueryClassification,
)
from faultmaven.modules.case.contracts import Case, CaseState
from faultmaven.modules.case.domain.models.evidence import UploadedFile

logger = logging.getLogger(__name__)

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)


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
    - ``True`` — the check ran over the case's rows and found nothing.
    - ``None`` — *undetermined*: the check could not run because the
      attachment has no content_hash, so nothing here knows. Reading that as ``True`` would report a
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


@dataclass
class _PreprocessedAttachment:
    """Internal result of `_preprocess_attachment`.

    Post-010 strict evidence model: file uploads no longer create an
    Evidence row at intake. This carries the UploadedFile the attachment
    resolved to (with preprocessing artifacts: summary, structural_index,
    data_type, coverage timestamps) plus dedup signals the caller
    needs to populate ``AttachmentResult.duplicate_of``.

    Also carries classification clarification hints when the heuristic
    classifier couldn't confidently classify the attachment — see
    ``_build_classification_clarification``.
    """

    uploaded_file: UploadedFile
    duplicate_of: Optional[str] = None
    duplicate_turn: Optional[int] = None
    # The storage key this attachment's bytes were written under THIS turn —
    # None on a duplicate (nothing was stored) and when no storage service
    # ran. It is how the turn knows which sidecars to flip with
    # ``_mark_turn_uploads_linked`` once its commit has landed (#1878); the
    # row's own ``storage_ref`` cannot tell a fresh store from a reused one.
    newly_stored_ref: Optional[str] = None
    # Did the content-hash check actually run?
    #
    # ``duplicate_of is None`` alone does NOT mean "novel" — it also covers
    # an attachment dedup could not check (no content_hash to match on). Reading absence as novelty reports a confident True for a
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


# Modes a fresh evidence-bearing attachment re-routes to DIRECTED_ANALYSIS
# (#708). TRIAGE and KNOWLEDGE_QUERY were the original two; AGENT_META joined
# in #1328 for the same reason — a question about the assistant delivered
# alongside a new upload must not let the agent skip the upload.
_EVIDENCE_REROUTE_MODES = (
    ProcessingMode.TRIAGE,
    ProcessingMode.KNOWLEDGE_QUERY,
    ProcessingMode.AGENT_META,
)


def _attachment_reroute(
    state: CaseState, mode: ProcessingMode
) -> Optional[ProcessingMode]:
    """Mode a turn that DELIVERS evidence should run in instead of *mode*.

    - INVESTIGATING: TRIAGE / KNOWLEDGE_QUERY / AGENT_META → DIRECTED_ANALYSIS
      (#708): tools are forced so the fresh upload is analysed rather than
      talked past. For AGENT_META that is a deliberate trade: the meta
      question is then answered by the backstop rule under the DA prompt,
      while the upload gets its analysis; the alternative — keeping the meta
      prompt, which tells the model to leave the case untouched — skips the
      upload, which is the very failure #708 closed. The DA system
      instruction's Type D covers the mixed turn explicitly.
    - INQUIRY: AGENT_META → TRIAGE (#1328). The #708 reroute is scoped to
      INVESTIGATING because INQUIRY characterises an upload through the
      structural index rather than forcing analysis, and TRIAGE is the mode
      that does exactly that. Without this, "before we start, what model are
      you?" + dmesg.log renders the meta block ahead of the INQUIRY role and
      the file is never looked at.
    - Anything else: None (no reroute).
    """
    if state == CaseState.INVESTIGATING and mode in _EVIDENCE_REROUTE_MODES:
        return ProcessingMode.DIRECTED_ANALYSIS
    if state == CaseState.INQUIRY and mode == ProcessingMode.AGENT_META:
        return ProcessingMode.TRIAGE
    return None


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


async def _preprocess_attachment(
    file_storage_service,
    preprocessing_service,
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
        ``_PreprocessedAttachment`` wrapping the ``UploadedFile`` this
        attachment resolved to plus optional dedup metadata. Post-010
        strict evidence model: NO Evidence row is created at this
        intake step. On content-hash duplicate within the same
        case, the returned UploadedFile is the existing row and
        ``duplicate_of`` / ``duplicate_turn`` are populated; no
        new UploadedFile is created and no raw file is re-stored.

        Nothing is committed here (#1878). A new row is appended to
        ``case.uploaded_files`` in memory, stamped with ``turn_number``,
        and becomes durable only in the turn's one commit (#1882) — so a
        turn that fails, at its commit or before it, leaves no row, and
        the file is listed, searchable and turn-attributed together or
        not at all. The sidecar is flipped to linked only after that
        commit (``_mark_turn_uploads_linked``).

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
    preprocessing_result = await preprocessing_service.classify_and_extract(
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
    # The lookup is the case's own ``uploaded_files`` and nothing else
    # (#1878). Every committed row reaches the database through the
    # OCC-versioned aggregate ``save()``, and the case is loaded with all of
    # them (neither backend's loader limits the list), so the loaded case
    # holds every row this turn can commit beside. A database row the case
    # does not hold could only be a concurrent turn's commit after this load,
    # and then this turn's own save fails with a version conflict anyway. An
    # earlier attachment of THIS submission is on the list too — appended
    # below — so two identical attachments in one turn are stored once.
    #
    # The earliest match wins: the lowest ``uploaded_at_turn``. (The loaders
    # do not order ``uploaded_files``, so list position means nothing; the
    # turn is what names the original. Keyed on the turn rather than
    # ``uploaded_at`` also because a loaded row and one built this turn need
    # not agree on timezone-awareness, and comparing them would raise.) A
    # same-submission hit carries this turn's number, which is what
    # ``duplicate_turn`` then reports — the turn the original commits with.
    #
    # ``dedup_ran`` separates "checked and found nothing" (novel) from "could
    # not check" (undetermined); without it both look like
    # ``duplicate_of is None`` and a re-submission is reported as new data
    # (#1210 round 2). The one way it cannot run is an attachment with no
    # content hash, and that path logs, because a permanently skipped check
    # means per-case dedup is not working at all.
    existing_file = None
    dedup_ran = bool(preprocessing_result.content_hash)
    if dedup_ran:
        in_case_matches = [
            f
            for f in case.uploaded_files
            if f.content_hash == preprocessing_result.content_hash
        ]
        if in_case_matches:
            existing_file = min(in_case_matches, key=lambda f: f.uploaded_at_turn)
    else:
        logger.warning(
            "No content_hash for '%s' on case %s — per-case dedup could not "
            "run and novelty is UNDETERMINED for this attachment; the turn "
            "is scored conservatively (#1136's upload progress arm will not "
            "arm on it).",
            attachment.filename,
            case.case_id,
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
    if file_storage_service:
        storage_result = await file_storage_service.store_file(
            file_data=attachment.content,
            original_filename=attachment.filename,
            enterprise_id=case.enterprise_id,
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
    # In memory only. The end-of-turn aggregate ``save(case)`` writes this
    # row in the same transaction as the turn's ``current_turn`` and user
    # message (#1878); see the note above the return below.
    case.uploaded_files.append(uploaded_file)

    # Post-010 strict evidence model: write preprocessing artifacts
    # to the UploadedFile row where they semantically belong (they
    # describe the FILE, not any claim about it). NO Evidence row
    # is created at this intake step — Evidence is born only when
    # the LLM extracts a claim-anchored slice via evidence_to_add
    # during INVESTIGATING.
    uploaded_file.summary = preprocessing_result.summary
    uploaded_file.structural_index = preprocessing_result.structural_index
    # The fine-grained ``DataType`` (#583) — see
    # ``_file_row_with_reclassification`` for why, and
    # ``unified_data_type_of`` for how both vocabularies are read.
    uploaded_file.data_type = preprocessing_result.detailed_data_type.value
    uploaded_file.coverage_start_ts = preprocessing_result.coverage_start_ts
    uploaded_file.coverage_end_ts = preprocessing_result.coverage_end_ts
    # WHICH pattern produced that span, carried with it. Consumers state the
    # span as an absolute observation time; this is how they know whether
    # they may. Computed by ``extract_time_range_ts`` and, until #1274, put
    # in a metadata object nobody persisted.
    uploaded_file.coverage_source = preprocessing_result.coverage_source

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
        # Named distinctly from every parsed source: this instant was not
        # read out of the content at all. It is the strongest provenance
        # available — a client that watched the content arrive, validated
        # by ``_parse_observed_at`` — and it is also the one case a
        # metadata blob written during extraction could never express,
        # because it is applied here, afterwards.
        uploaded_file.coverage_source = CALLER_DECLARED_COVERAGE_SOURCE
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

    # No commit here (#1878). The row rides the turn's one commit (#1882) —
    # the same transaction that writes the turn's ``current_turn``, its user
    # message and its reply — so it exists only once the turn that carried it
    # exists. Every file-reading tool (``search_file``, ``read_file``,
    # ``deep_analysis``, ``vectorize_file``) and the file list resolve through
    # ``case.uploaded_files``, so a file is listed, searchable and attributed
    # to a committed turn together, or none of these. A turn that fails at any
    # point, its commit included (a version conflict there), leaves no row:
    # the user is told the turn failed and sends the file again, and that
    # retry is a fresh upload rather than a "duplicate" of its own failed
    # attempt.
    #
    # A scoped commit used to live here (#1013): ``mark_linked`` ran at intake,
    # BEFORE any row existed, so a failed turn left stored bytes exempt from
    # TTL reclaim and referenced by nothing — a permanent orphan — unless the
    # row was committed immediately. It also stamped the row with a turn number
    # the case never committed, which the next committed turn then reused
    # (#1878). ``mark_linked`` now runs after the turn's commit
    # (``_mark_turn_uploads_linked``), and the orphan sweep deletes a blob only
    # when it has no ``uploaded_files`` row AND its sidecar still says
    # ``linked: false`` past the TTL (#1232). So the bytes a failed turn stored
    # are an ordinary orphan the sweep reclaims, and no early commit is needed
    # to keep anything safe. There is no half turn left to commit a row
    # without its reply: the engine's Step-7 save and the deterministic
    # branches' saves are gone, and the turn commits once (#1882).
    return _PreprocessedAttachment(
        uploaded_file=uploaded_file,
        newly_stored_ref=storage_ref,
        dedup_ran=dedup_ran,
        classification_failed=is_classification_failed,
        suggested_types=suggested_types,
        attachment_filename=attachment.filename,
    )


#: The most one ``mark_linked`` call may take after a turn has committed
#: (#1882). The turn's 2xx waits on it, and no deadline covers the
#: post-commit steps, so a storage backend that hangs must not hold the
#: response: a call cut short here is counted ``timed_out`` and left to the
#: orphan sweep, exactly as a call that failed.
MARK_LINKED_TIMEOUT_SECONDS = 5.0


async def _mark_turn_uploads_linked(
    file_storage_service, preprocess_results: List[_PreprocessedAttachment]
) -> None:
    """Flip the sidecar of every blob this turn stored, AFTER its commit.

    Called once the turn's one commit has returned, with the turn's own
    ``preprocess_results`` — the explicit record of which attachments were
    stored this turn (``newly_stored_ref``); a duplicate stored nothing and is
    skipped. Before #1878 this ran at intake, so a turn that failed left a blob
    marked linked with no row behind it.

    Best-effort and never fatal: the turn is already committed. A failure
    leaves ``linked: false`` beside a live row, which the orphan sweep ignores
    because it asks the database first (#1232) — so nothing is lost. It is
    still counted, because the CAUSE is a storage backend erring on a small
    write, and that is worth surfacing on its own. The counter is emitted from
    the API process, which Prometheus scrapes — unlike the sweep's own
    counters, which die with the CronJob pod. Retrying the call (#1232
    direction 3) was deliberately not taken: it would add latency to the
    user-facing turn path to narrow a window that leads nowhere.
    """
    mark_linked = (
        getattr(file_storage_service, "mark_linked", None)
        if file_storage_service
        else None
    )
    if mark_linked is None:
        return
    for result in preprocess_results:
        storage_ref = result.newly_stored_ref
        if not storage_ref:
            continue
        try:
            # Check the result, don't just call it: mark_linked reports
            # failure by returning False rather than raising, so without
            # this neither the warning nor the counter below could fire and
            # the drift would be entirely invisible.
            linked = await asyncio.wait_for(
                mark_linked(storage_ref), timeout=MARK_LINKED_TIMEOUT_SECONDS
            )
            if not linked:
                _record_mark_linked_failure("returned_false")
                logger.warning(
                    "mark_linked returned False for %s (non-fatal; the "
                    "orphan sweep asks the database, so the file is safe "
                    "— but a sidecar write just failed)",
                    storage_ref,
                )
        except asyncio.TimeoutError:
            _record_mark_linked_failure("timed_out")
            logger.warning(
                "mark_linked timed out after %ss for %s (non-fatal; the "
                "orphan sweep asks the database, so the file is safe — but a "
                "sidecar write did not finish)",
                MARK_LINKED_TIMEOUT_SECONDS,
                storage_ref,
            )
        except Exception as e:
            _record_mark_linked_failure("raised")
            logger.warning(
                "mark_linked failed for %s (non-fatal; the orphan sweep "
                "asks the database, so the file is safe — but a sidecar "
                "write just failed): %s",
                storage_ref,
                e,
            )


async def _preprocess_turn_uploads(
    file_storage_service,
    preprocessing_service,
    *,
    case,
    case_id,
    classification,
    next_turn,
    payload,
    processing_mode,
    user_id,
):
    """Preprocess each attachment, re-route the classification for a fresh evidence-bearing upload, and derive the query."""
    uploaded_files_this_turn: List["UploadedFile"] = []
    preprocess_results: List[_PreprocessedAttachment] = []
    if payload.has_attachments:
        for attachment in payload.attachments:
            result = await _preprocess_attachment(
                file_storage_service,
                preprocessing_service,
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
    # (The INQUIRY exception is AGENT_META → TRIAGE, #1328 — see
    # ``_attachment_reroute``.)
    rerouted = _attachment_reroute(case.state, classification.mode)
    if rerouted is not None and _turn_delivers_evidence_bearing_attachment(
        preprocess_results
    ):
        prior_mode = classification.mode.value
        classification = QueryClassification(
            mode=rerouted,
            detected_entities=classification.detected_entities,
            confidence=0.8,
        )
        # ``classification.mode.value`` threads to the engine via
        # intent_data["query_mode"] below; the ``processing_mode`` local
        # is only consumed by preprocessing (already run above), so it
        # is intentionally not reassigned here.
        logger.info(
            "Query re-routed %s→%s on case %s turn %s: "
            "fresh evidence-bearing attachment (#708/#1328)",
            prior_mode,
            rerouted.value.upper(),
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
    return classification, preprocess_results, query, uploaded_files_this_turn
