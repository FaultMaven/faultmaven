"""Stages 0-5 of a document conversion, over the caller's file (#2622).

``prepare`` runs in the preparation worker (``preparation_worker``), a process
its parent kills at the deadline (Principle 14). It imports the parsers, the
secret patterns, token counting and frontmatter, and nothing else, and it
takes and returns plain data: ``document_preprocessor`` builds the result
models, and Stage 6 (triage) runs there, on the loop.

A refusal carries its code and its sentence, never the document's text.
"""

import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from faultmaven.infrastructure.security.secret_patterns import (
    SECRET_PATTERNS,
    RedactionSpan,
    redact_spans,
)
from faultmaven.modules.knowledge.domain.services.document_parser import (
    DocumentParser,
    DocumentTooComplex,
    ParsedDocument,
)
from faultmaven.modules.knowledge.domain.services.runbook_grammar import code_spans
from faultmaven.utils.frontmatter import match_frontmatter
from faultmaven.utils.token_estimation import _get_tiktoken_encoder

MAX_TOKEN_LIMIT = 30_000
MIN_TEXT_LENGTH = 200
TRIAGE_SAMPLE_TOKENS = 2000

#: How many skipped page numbers a sentence names before "and N more".
_PAGES_NAMED = 10


def _page_list(pages: tuple[int, ...]) -> str:
    """The first ``_PAGES_NAMED`` page numbers, then how many more."""
    named = ", ".join(map(str, pages[:_PAGES_NAMED]))
    more = len(pages) - _PAGES_NAMED
    return f"{named} and {more} more" if more > 0 else named


def _refusal(code: str, reason: str) -> Dict[str, Any]:
    """A refusal: a ``ConversionErrorCode`` value and its sentence."""
    return {"refused": code, "reason": reason}


def _refused_pages_left_too_little(parsed: ParsedDocument) -> Dict[str, Any]:
    """The length or technical-content gate refused what the refused pages left:
    the refused pages are the cause, not the document's length or subject."""
    return _refusal(
        "DOCUMENT_TOO_COMPLEX",
        f"The PDF reader's limits refused page(s) {_page_list(parsed.skipped_pages)}"
        f" of {parsed.page_count}, and the pages it could read are not enough to "
        "convert. Export it to text (TXT or Markdown), or split it into smaller "
        "documents, and convert that instead.",
    )


def _get_encoder():
    """tiktoken's ``cl100k_base`` (a GPT-4 / Claude approximation), from the
    loader every tiktoken count shares, or None when it cannot be loaded."""
    return _get_tiktoken_encoder("gpt-4")


#: The most characters one tiktoken call encodes. A call returns every token
#: of its text at once, about 40 bytes each, so one call over the whole of a
#: large text ran out of the worker's memory: 20 MB of text extracted from a
#: 10 MB HTML upload is 29 million tokens (#2622).
_COUNT_CHUNK_CHARS = 1 << 20


def _chunks(text: str):
    """``text`` in pieces of at most ``_COUNT_CHUNK_CHARS``, each ending after
    a newline that a non-space character follows. cl100k's pre-tokenizer
    always splits there, so the pieces' tokens are the whole's. A window with
    no such newline is cut at its limit, where the count can be off by one or
    two tokens."""
    start = 0
    while len(text) - start > _COUNT_CHUNK_CHARS:
        end = start + _COUNT_CHUNK_CHARS
        cut = text.rfind("\n", start, end - 1)
        while cut >= start and text[cut + 1].isspace():
            cut = text.rfind("\n", start, cut)
        end = cut + 1 if cut >= start else end
        yield text[start:end]
        start = end
    yield text[start:]


def _measure(text: str) -> tuple[int, str]:
    """The token count, and the triage sample: the first
    ``TRIAGE_SAMPLE_TOKENS`` tokens, from one encoding of the text, a chunk
    at a time. A special token's spelling (``<|endoftext|>``) is counted as
    the text it is."""
    encoder = _get_encoder()
    if encoder is None:
        return len(text) // 4, text[: TRIAGE_SAMPLE_TOKENS * 4]
    count, head = 0, []
    for chunk in _chunks(text):
        tokens = encoder.encode(chunk, disallowed_special=())
        count += len(tokens)
        head += tokens[: TRIAGE_SAMPLE_TOKENS + 1 - len(head)]
    if count > TRIAGE_SAMPLE_TOKENS:
        return count, encoder.decode(head[:TRIAGE_SAMPLE_TOKENS])
    return count, text


def count_tokens(text: str) -> int:
    """Count tokens with tiktoken, or four characters a token without it.

    Without the encoding the size gate runs on the same heuristic as every
    other count in that state, rather than failing the upload. It understates
    dense text, so a CJK document over the limit can pass the gate; rejecting
    ordinary English documents by an overstated count would be the worse
    failure for a gate whose limit the conversion enforces again.
    """
    return _measure(text)[0]


# =============================================================================
# Content Cleanup Patterns
# =============================================================================

# Patterns to strip from extracted text
_CLEANUP_PATTERNS = [
    # Repeated headers/footers (page numbers, copyright)
    (r"(?m)^(?:Page \d+(?:\s+of\s+\d+)?|©.*?\d{4}.*?)$", ""),
    # Cookie/privacy banners
    (
        r"(?i)(?:we use cookies|cookie policy|privacy policy|accept all cookies)[^\n]*\n?",
        "",
    ),
    # Marketing/sales boilerplate
    (
        r"(?i)(?:try our enterprise|upgrade to pro|start your free trial|contact sales)[^\n]*\n?",
        "",
    ),
    # Table of contents entries with page numbers
    (r"(?m)^.{3,60}\.{3,}\s*\d+\s*$", ""),
    # "Last updated by..." revision metadata
    (r"(?i)(?:last (?:updated|modified|edited) (?:by|on))[^\n]*\n?", ""),
    # Excessive blank lines (3+ → 2)
    (r"\n{3,}", "\n\n"),
]


def cleanup_text(text: str) -> str:
    """Stage 2: Remove boilerplate and noise from extracted text."""
    for pattern, replacement in _CLEANUP_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text.strip()


# =============================================================================
# Secret redaction (the sanitizer's regex layer)
# =============================================================================


def redact_sensitive_content(text: str) -> tuple[str, Dict[str, Any]]:
    """Stage 3: Detect and redact secrets before the text reaches a model.

    Returns the text and the fields of a ``RedactionReport``.

    Detection is the sanitizer's own secret pattern set (``SECRET_PATTERNS``,
    #1980): a document is scanned for exactly the secrets case content is.
    The sanitizer's network-address patterns (internal IPs, MACs) are not
    applied here: they are identifiers, not secrets, and on this path they
    are the model-boundary redaction's, under ``SANITIZE_PII`` (#1901). The
    replacement differs too, and deliberately. This stage runs on every
    conversion, whatever ``SANITIZE_PII`` says, and what it writes is meant
    to be read: ``[REDACTED:<type>]`` tells the model, and then the runbook's
    author, that a value of that kind was removed and must be filled in
    (document-to-runbook-conversion.md §2.4), and ``redaction_report`` counts
    the markers by type for the user. A keyed ``<TYPE_digest>`` pseudonym
    answers a different need — matching one value across a case — and is
    what the model-boundary redaction adds under ``SANITIZE_PII``.
    """
    counts: dict[str, int] = {}
    first_start: dict[str, int] = {}

    def _marker(span: RedactionSpan, _value: str) -> str:
        redaction_type = span.entity_type.lower()
        counts[redaction_type] = counts.get(redaction_type, 0) + 1
        first_start.setdefault(redaction_type, span.start)
        return f"[REDACTED:{redaction_type}]"

    redacted = redact_spans(text, SECRET_PATTERNS, _marker)

    def _found_in(start: int) -> str:
        if "```" in text[max(0, start - 50) : start]:
            return "Found in code blocks"
        return "Found in text content"

    redactions = [
        {
            "type": redaction_type,
            "count": count,
            "context": _found_in(first_start[redaction_type]),
        }
        for redaction_type, count in counts.items()
    ]
    total = sum(counts.values())

    warning = None
    if total > 0:
        warning = (
            f"{total} sensitive items were redacted. "
            "Review generated runbooks for [REDACTED] placeholders."
        )

    return redacted, {
        "redactions": redactions,
        "warning": warning,
        "total_redacted": total,
    }


# =============================================================================
# Source Metadata Extraction
# =============================================================================


def extract_source_metadata(
    file_path: Path,
    content_type: str,
    extracted_text: str,
    page_count: Optional[int],
) -> Dict[str, Any]:
    """Stage 5: Capture provenance metadata from source file. A PDF's page
    count is the extraction's, so the file is not opened a second time."""
    stat = file_path.stat()
    word_count = len(extracted_text.split())

    metadata = {
        "original_filename": file_path.name,
        "file_size_bytes": stat.st_size,
        "content_type": content_type,
        "word_count": word_count,
    }

    if page_count is not None:
        metadata["page_count"] = page_count

    return metadata


# =============================================================================
# File Integrity Validation
# =============================================================================


def validate_file_integrity(file_path: Path, content_type: str) -> Optional[str]:
    """Check file is non-zero and magic bytes match claimed type.

    Returns error message if invalid, None if OK.
    """
    if not file_path.exists():
        return "File does not exist"

    size = file_path.stat().st_size
    if size == 0:
        return "File is empty (0 bytes)"

    # Check magic bytes for binary formats
    if content_type in (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        try:
            with open(file_path, "rb") as f:
                header = f.read(8)
            if content_type == "application/pdf" and not header.startswith(b"%PDF"):
                return "File claims to be PDF but does not have PDF signature. The file may be renamed or corrupt."
            if "openxml" in content_type and not header.startswith(b"PK\x03\x04"):
                return "File claims to be DOCX but does not have ZIP/DOCX signature. The file may be renamed or corrupt."
        except MemoryError:
            raise  # the worker's declared exit, never a corrupt file (#2622)
        except Exception:
            return "Cannot read file header — file may be corrupt"

    return None


# =============================================================================
# Heuristic Technical Content Pre-Check (no LLM needed)
# =============================================================================

# Signals that indicate technical/troubleshooting content
_TECHNICAL_SIGNALS = [
    r"(?i)\b(?:error|exception|failed|failure|timeout|refused|denied)\b",
    r"(?i)\b(?:stack\s*trace|traceback|segfault|core\s*dump|panic)\b",
    r"(?i)\b(?:HTTP\s*[45]\d{2}|status\s*code\s*[45])",
    r"```",  # code blocks
    r"(?i)\b(?:kubectl|docker|systemctl|journalctl|grep|curl|wget|ssh)\b",
    r"(?i)\b(?:restart|rollback|failover|recovery|remediat|mitigat|diagnos)\b",
    r"\b(?:ERROR|WARN|FATAL|CRITICAL|DEBUG)\b",  # log levels
    r"(?:postgres|mysql|redis|nginx|apache|kafka|elasticsearch)://",
    r"(?i)\b(?:OOM|CPU|memory|disk|latency|throughput|connection)\s+(?:limit|usage|exceeded|full)\b",
]


def check_technical_content(text: str) -> tuple[bool, int]:
    """Fast heuristic check for technical signals in text.

    Returns (has_signals, signal_count).
    """
    count = 0
    for pattern in _TECHNICAL_SIGNALS:
        matches = re.findall(pattern, text[:10000])  # Check first 10K chars for speed
        count += len(matches)
    return count >= 2, count


# =============================================================================
# Existing Runbook Detection
# =============================================================================

_RUNBOOK_FRONTMATTER_FIELDS = {
    "id",
    "domain",
    "service",
    "symptom_class",
    "severity",
    "status",
}

# 4 of 6 is "runbook-shaped frontmatter". Named rather than inlined so
# ``test_document_preprocessor_existing_runbook`` can assert the threshold is
# actually reachable — see ``_RUNBOOK_FRONTMATTER_FIELDS`` against
# ``RunbookValidator.REQUIRED_METADATA``. If a schema change made three of these
# six optional, every valid runbook would fall under the threshold and this gate
# would disarm with no test failing.
_RUNBOOK_FRONTMATTER_MIN_FIELDS = 4

# The two sections that make a document a FaultMaven runbook rather than some
# other technical document, checked alongside the frontmatter above.
#
# The frontmatter test ALONE is not a safe basis for a refusal. Its threshold is
# "4 of 6", and four of those six field names are generic: an incident-report or
# ticket export carrying ``id`` / ``service`` / ``severity`` / ``status`` meets it
# without being a runbook — and an incident report is precisely the kind of
# document this pipeline exists to convert (``ANALYSIS_SYSTEM_PROMPT`` names
# ``incident_report`` as a source type). While the detection only raised a
# warning, a false positive cost nothing; as the basis of a 422 it would
# refuse legitimate work, so the body shape is required too.
#
# ``## Symptom Recognition`` + ``## Causes`` are the pair that carries the cause
# model this refusal is about — the runbook states one failure surface and
# enumerates its causes underneath — and nothing outside a FaultMaven runbook
# writes both. Their spelling is the validator's, not a second copy: they are
# asserted to be a subset of ``RunbookValidator``'s ``REQUIRED_SECTIONS`` by
# ``test_document_preprocessor_existing_runbook.py``, so renaming a section there
# fails that test rather than silently disarming this gate.
_RUNBOOK_BODY_SECTIONS = ("Symptom Recognition", "Causes")

# Exact-anchored ``^## Section$``, the same anchoring ``RunbookValidator``
# ``_validate_structure`` uses. A prefix match would accept ``## Causes of the
# outage`` in an ordinary postmortem.
_RUNBOOK_BODY_SECTION_RES = tuple(
    re.compile(rf"^##[ \t]+{re.escape(section)}[ \t]*$", re.MULTILINE)
    for section in _RUNBOOK_BODY_SECTIONS
)

# Code is masked before the section search, because a postmortem that QUOTES the
# runbook it followed is a document this pipeline exists to convert, and a fenced
# quote of ``## Symptom Recognition`` + ``## Causes`` would otherwise satisfy the
# body half and hard-refuse it (422). This is where the gate deliberately parts
# company with ``RunbookValidator._validate_structure``, which does NOT mask: the
# validator is already looking at something claiming to be a runbook, so a fenced
# heading costs it nothing, while here it costs a legitimate conversion.
#
# Via ``runbook_grammar.mask_code`` rather than a local regex. A private
# ``re.compile(r"```.*?```", re.DOTALL)`` shipped here first and was wrong three
# ways, each of which either refused a legitimate document or disarmed the gate
# on a real runbook: it did not know ``~~~`` fences; it paired backtick runs
# positionally, so an unclosed fence swallowed the real headings after it; and
# replacing the span with newlines dropped the characters before its first and
# after its last, splicing WITHIN a line and manufacturing a ``## Causes`` the
# source never had. The shared helper answers all three because it is the same
# ``_protected_spans`` the comment mask uses, and it is length-preserving.

# Bytes before the opening ``---`` that must not disarm the gate. The
# frontmatter anchor is ``re.match``, so it binds at offset 0: a UTF-8 BOM (the
# "UTF-8 with BOM" default of several Windows editors, preserved verbatim by
# ``Path.read_text(encoding="utf-8")``) or one blank line ahead of the
# delimiter made ``detect_existing_runbook`` answer False for a byte-identical
# runbook. Detection runs BEFORE ``cleanup_text``, which is the only thing that
# would otherwise have removed them, so #1375 still reproduced on any runbook
# saved by such an editor.
_LEADING_NOISE = "\ufeff \t\r\n"


def classify_runbook_shape(text: str) -> tuple[bool, bool]:
    """Return ``(is_runbook, is_near_runbook)`` from ONE pass over the document.

    ``prepare`` needs both answers and they share every expensive step: the
    leading-noise strip, the YAML parse, and the code-span scan over a document
    that may run to ``MAX_TOKEN_LIMIT`` (30K tokens) — the size check happens
    later, so this stage sees the document at full size. Asking
    ``detect_existing_runbook`` and then ``is_near_runbook`` did all of it
    twice.

    The two public predicates below delegate here rather than the other way
    round, so the shape rule is stated once and the surface the tests drive is
    the surface production uses.
    """
    fields = _runbook_frontmatter_fields(text)
    if len(fields) < _RUNBOOK_FRONTMATTER_MIN_FIELDS:
        return False, False
    # Only now is the body scan worth paying for.
    if _has_runbook_body(text):
        return True, False
    return False, "symptom_class" in fields


def detect_existing_runbook(text: str) -> bool:
    """Is this document already a FaultMaven runbook?

    True only when BOTH hold: the document opens with runbook frontmatter, and
    its body carries the canonical section skeleton OUTSIDE any fenced block.
    See the constants above for why neither half decides it alone.
    """
    return _has_runbook_frontmatter(text) and _has_runbook_body(text)


def _runbook_frontmatter_fields(text: str) -> frozenset:
    """Which of ``_RUNBOOK_FRONTMATTER_FIELDS`` the opening frontmatter carries.

    Returns the matched field NAMES rather than a bool, because the two callers
    need different questions answered from one parse: the gate asks "are there
    at least four", the near-runbook warning asks "is ``symptom_class`` among
    them". Empty when the document does not open with parseable frontmatter.

    Sole owner of the ``_LEADING_NOISE`` strip, and the only reader for which
    offset 0 is load-bearing — the section search is ``MULTILINE`` and
    indifferent to what precedes the first delimiter. A second copy in the
    caller made the guard untestable: removing either left the other, so a
    mutation that should have restored the BOM bug killed no test.
    """
    text = text.lstrip(_LEADING_NOISE)

    match = match_frontmatter(text)
    if not match:
        return frozenset()

    try:
        import yaml

        metadata = yaml.safe_load(match.group(1))
        if not isinstance(metadata, dict):
            return frozenset()
        return frozenset(metadata.keys()) & _RUNBOOK_FRONTMATTER_FIELDS
    except MemoryError:
        raise  # the worker's declared exit, never "not a runbook" (#2622)
    except Exception:
        return frozenset()


def _has_runbook_frontmatter(text: str) -> bool:
    """Runbook-SHAPED frontmatter: at least the threshold many fields."""
    return len(_runbook_frontmatter_fields(text)) >= _RUNBOOK_FRONTMATTER_MIN_FIELDS


def is_near_runbook(text: str) -> bool:
    """Ours by frontmatter, but the body is not the canonical skeleton.

    Deliberately NARROWER than the gate's own frontmatter half. The threshold is
    four of six, and four of those six are generic enough that an incident
    report carrying ``id``/``service``/``severity``/``status`` meets it — so
    warning on the frontmatter half alone would fire on precisely the document
    class this pipeline exists to convert, which is the same false-positive
    argument that made the body half mandatory for the 422.

    ``symptom_class`` is the discriminator: it is the one field in the set that
    is FaultMaven's own controlled vocabulary rather than a word every tracker
    also uses, it is in ``REQUIRED_METADATA`` so every runbook we produce or
    accept carries it, and no ticket export does. What is left is the case worth
    a warning and nothing else: a document that IS one of ours whose body the
    gate no longer recognises — sections renamed locally, heading levels
    flattened by a round trip through another format.
    """
    return classify_runbook_shape(text)[1]


def _has_runbook_body(text: str) -> bool:
    """Does the body carry the canonical section skeleton, outside code?

    Matches on the RAW text and discards any hit that starts inside a code span,
    rather than matching on a masked copy. A mask must pick a fill character,
    and the fill can satisfy the pattern being tested: blanking to spaces turns
    ``##`` + an inline span + `` Causes`` into ``##             Causes``, which
    ``^##[ \t]+Causes[ \t]*$`` accepts — a heading the mask invented. Offsets
    have no fill to be confused by.
    """
    spans = code_spans(text)

    def outside_code(match) -> bool:
        return not any(start <= match.start() < end for start, end in spans)

    return all(
        any(outside_code(m) for m in pattern.finditer(text))
        for pattern in _RUNBOOK_BODY_SECTION_RES
    )


# =============================================================================
# Stages 0-5
# =============================================================================


def prepare(
    file_path: Path, content_type: str, enter: Callable[[int], object]
) -> Dict[str, Any]:
    """Stages 0-5 over the caller's file. Returns a refusal (``refused``, a
    ``ConversionErrorCode`` value, and its ``reason``) or what Stage 6 needs.
    ``enter(n)`` is called as each stage starts: 0 to 5, and ``"1b"`` once the
    parse is done and the runbook-shape check starts."""
    enter(0)
    warnings: list[str] = []

    # Stage 0: File integrity
    integrity_error = validate_file_integrity(file_path, content_type)
    if integrity_error:
        empty = "empty" in integrity_error.lower()
        return _refusal("FILE_EMPTY" if empty else "FILE_CORRUPT", integrity_error)

    # Stage 1: Format extraction
    enter(1)
    try:
        parsed = DocumentParser().extract_document_text(file_path, content_type)
    except DocumentTooComplex as e:
        return _refusal("DOCUMENT_TOO_COMPLEX", str(e))
    except ValueError as e:
        error_msg = str(e)
        if "encoding" in error_msg.lower() or "decode" in error_msg.lower():
            return _refusal("ENCODING_ERROR", error_msg)
        if "unsupported" in error_msg.lower():
            return _refusal("UNSUPPORTED_FORMAT", error_msg)
        return _refusal("FILE_CORRUPT", error_msg)
    extracted_text = parsed.text
    if parsed.skipped_pages:
        warnings.append(
            "The PDF reader's limits refused page(s) "
            f"{_page_list(parsed.skipped_pages)} of {parsed.page_count}, so "
            "their text was skipped. Check the generated runbook against "
            "those pages of the source."
        )

    # Stage 1b: Existing runbook detection — a REFUSAL, not a warning.
    #
    # Converting a runbook is a category error, and the warning this
    # replaces both under-stated the outcome and bound nothing. It said the
    # conversion "may produce a duplicate" (one); what it actually produces
    # is one runbook per ``### Cause`` subsection of the source, because the
    # analysis pass's definition of a failure mode ("different symptoms OR
    # different resolutions") is satisfied by each Cause separately — a
    # Cause carries its own Statement, Indicators and Interventions. That
    # inverts the content model, which is explicit that one runbook is one
    # failure mode and that several ``### Cause N`` subsections within it are
    # expected and correct (runbook-content-architecture.md §2). Measured on
    # the shipped pack: 7 of 9 runbooks fed back analysed into exactly
    # ``causes - 1`` failure modes, up to 4 drafts from one runbook (#1375).
    #
    # Even a conversion that returned a single mode would be lossy: the
    # runbook is re-derived by an LLM under RUNBOOK_MAX_TOKENS (4096, ~16K
    # chars) from sources that routinely run to 34K chars, and ``status``,
    # ``verified_by`` and ``version`` reset to draft/""/1.0.0 — so whatever
    # verification the source carried is discarded. Nothing about the output
    # is better than the input that was already in hand.
    #
    # Refused HERE, before cleanup and before either LLM call, so the
    # refusal costs nothing. ``ALREADY_A_RUNBOOK`` already had its 422 in
    # ``conversion_routes`` and its user-facing copy in the Dashboard; this
    # is the raise site they were waiting for.
    enter("1b")
    is_runbook, near_runbook = classify_runbook_shape(extracted_text)
    if is_runbook:
        return _refusal(
            "ALREADY_A_RUNBOOK",
            "This document is already a FaultMaven runbook. Converting it "
            "would re-derive a new runbook from its prose — splitting its "
            "causes into separate runbooks and resetting its verification "
            "status — rather than adding the runbook you already have. Add "
            "it to the knowledge base directly instead of converting it.",
        )

    # A NEAR-runbook keeps the advisory the refusal above replaces: one of
    # ours whose body the gate no longer recognises still converts, and
    # fragmentation is still the likely outcome, so the user is told why
    # rather than left with silence. See ``is_near_runbook`` for why this is
    # narrower than the gate's own frontmatter half.
    if near_runbook:
        warnings.append(
            "This document carries FaultMaven runbook frontmatter but not the "
            "expected section structure. Conversion will re-derive runbooks "
            "from its prose, which may split one runbook's causes into "
            "several. If it is already a runbook, add it to the knowledge "
            "base directly instead of converting it."
        )

    # Stage 2: Content cleanup
    enter(2)
    extracted_text = cleanup_text(extracted_text)

    # Check minimum length
    if len(extracted_text.strip()) < MIN_TEXT_LENGTH:
        if parsed.skipped_pages:
            return _refused_pages_left_too_little(parsed)
        return _refusal(
            "DOCUMENT_TOO_SHORT",
            f"Document contains too little text ({len(extracted_text)} characters, "
            f"minimum: {MIN_TEXT_LENGTH}). The source lacks sufficient content "
            "for runbook conversion.",
        )

    # Stage 2b: Heuristic technical content check (before LLM calls)
    has_technical, signal_count = check_technical_content(extracted_text)
    if not has_technical:
        if parsed.skipped_pages:
            return _refused_pages_left_too_little(parsed)
        return _refusal(
            "NO_TECHNICAL_CONTENT",
            "This document does not contain recognizable technical content "
            "(no error messages, log patterns, commands, or diagnostic procedures found). "
            "Runbook conversion requires troubleshooting-related source material.",
        )

    # Stage 3: Sensitive content scan
    enter(3)
    extracted_text, redaction_report = redact_sensitive_content(extracted_text)
    if redaction_report["total_redacted"] > 0:
        warnings.append(
            f"{redaction_report['total_redacted']} sensitive items were redacted. "
            "Redacted commands may be incomplete in the generated runbook. "
            "Review before verifying."
        )

    # Stage 4: Size check (hard limit)
    enter(4)
    token_count, triage_sample = _measure(extracted_text)
    if token_count > MAX_TOKEN_LIMIT:
        return _refusal(
            "DOCUMENT_TOO_LONG",
            f"Document contains {token_count:,} tokens (limit: {MAX_TOKEN_LIMIT:,}). "
            "Please split the document into smaller, focused chapters "
            "and convert each one separately.",
        )

    # Stage 5: Source metadata extraction
    enter(5)
    source_metadata = extract_source_metadata(
        file_path, content_type, extracted_text, parsed.page_count
    )
    source_metadata["token_count"] = token_count
    return {
        "extracted_text": extracted_text,
        "source_metadata": source_metadata,
        "redaction_report": redaction_report,
        "token_count": token_count,
        "warnings": warnings,
        "triage_sample": triage_sample,
    }
