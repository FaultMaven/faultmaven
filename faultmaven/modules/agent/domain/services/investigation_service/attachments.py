"""Attachment preprocessing helpers: binary detection, the per-turn
preprocessed-attachment record, and the evidence-bearing reroute.

Owns everything about turning a raw upload into what the rest of the
investigation service reasons about: whether its bytes are binary (so the
UTF-8 decode is skipped in favour of a metadata-only placeholder), the
``_PreprocessedAttachment`` record ``_preprocess_attachment`` builds, the
per-attachment dict handed to ``engine.process_turn``, and the processing-mode
reroute a turn carrying evidence forces.
"""

from dataclasses import dataclass
from typing import List, Optional

from faultmaven.core.investigation.prompts.context_builder.budget import (
    structural_index_is_searchable,
)
from faultmaven.modules.agent.domain.services.query_classifier import (
    ProcessingMode,
)
from faultmaven.modules.case.contracts import (
    CaseState,
)
from faultmaven.modules.case.domain.models.evidence import (
    UploadedFile,
)

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
