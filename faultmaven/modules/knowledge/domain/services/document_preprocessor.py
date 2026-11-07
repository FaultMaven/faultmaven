"""6-stage document preprocessing pipeline for conversion.

Stages:
1. Format extraction (via DocumentParser)
1b. Already-a-runbook gate: refuse a document that is already a FaultMaven
    runbook (ALREADY_A_RUNBOOK). A document with runbook frontmatter whose body
    is not the canonical skeleton is WARNED about instead, and only when that
    frontmatter carries `symptom_class` — see ``is_near_runbook`` for why the
    frontmatter alone is too generic to warn on
2. Content cleanup (strip boilerplate)
3. Sensitive content scan (PII redaction)
4. Size check (hard limit at 30K tokens)
5. Source metadata extraction
6. Content triage (LLM classifier)
"""

import asyncio
import contextvars
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, NamedTuple, Optional

from faultmaven.core.investigation.confidence_repair import (
    ConfidenceAction,
    ConfidenceRepair,
    classify,
    short_repr,
)
from faultmaven.core.investigation.confidence_repair import (
    count as count_confidence_repair,
)
from faultmaven.exceptions import RawModelContentRefused
from faultmaven.infrastructure.llm.dispatch import (
    route_model_call,
)
from faultmaven.infrastructure.llm.json_response import loads_llm_json
from faultmaven.infrastructure.security.model_content import model_boundary_redacted
from faultmaven.infrastructure.security.redaction import (
    SECRET_PATTERNS,
    RedactionSpan,
    redact_spans,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    ConversionErrorCode,
    PreprocessingResult,
    RedactionEntry,
    RedactionReport,
    TriageResult,
)
from faultmaven.modules.knowledge.domain.services.document_parser import (
    DocumentParser,
    DocumentTooComplex,
    ParsedDocument,
)
from faultmaven.modules.knowledge.domain.services.runbook_grammar import code_spans
from faultmaven.utils.frontmatter import match_frontmatter
from faultmaven.utils.token_estimation import _get_tiktoken_encoder

logger = logging.getLogger(__name__)

MAX_TOKEN_LIMIT = 30_000
MIN_TEXT_LENGTH = 200
TRIAGE_SAMPLE_TOKENS = 2000

#: How long a request waits for a worker, and then for its Stages 0-5, each
#: from its own start (#2153). The route has no other limit until the ingress's
#: 660 s, so both waits and the model calls after them fit, and a legitimate
#: dense PDF (10.7 s measured for one page) has room. Work a caller abandons is
#: bounded by the check between pages, not by this.
EXTRACTION_TIMEOUT_SECONDS = 120

#: ``Retry-After`` on ``CONVERSION_BUSY``. Every worker held when a request gives
#: up has ended, or been abandoned at a page boundary, within one bound of it.
CONVERSION_BUSY_RETRY_AFTER_SECONDS = EXTRACTION_TIMEOUT_SECONDS

#: Where Stages 0-5 run: CPU over the caller's file, off the event loop (#1417's
#: class, #2153). Not a semaphore: ``async with sem: await wait_for(to_thread())``
#: frees the slot on timeout while the thread runs on, bounding nothing. A pool
#: worker stays taken until its work ends, so at most two run, and a queued one
#: whose caller gave up is cancelled and never runs. A thread cannot be killed,
#: so a started worker whose caller gave up checks between PDF pages and stops
#: after its current one; process exit waits for that page. The route unlinking
#: the temp file meanwhile is safe on Linux: an open file outlives its name, and
#: a worker not yet reading it fails with a result nobody reads.
_EXTRACTION_POOL = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="document-extract"
)

#: How many skipped page numbers a sentence names before "and N more".
_PAGES_NAMED = 10


def _page_list(pages: tuple[int, ...]) -> str:
    """The first ``_PAGES_NAMED`` page numbers, then how many more."""
    named = ", ".join(map(str, pages[:_PAGES_NAMED]))
    more = len(pages) - _PAGES_NAMED
    return f"{named} and {more} more" if more > 0 else named


def _refusal(reason: str, error_code: ConversionErrorCode) -> PreprocessingResult:
    return PreprocessingResult(
        extracted_text="",
        source_metadata={},
        is_rejected=True,
        rejection_reason=reason,
        error_code=error_code,
    )


def _refused_pages_left_too_little(parsed: ParsedDocument) -> PreprocessingResult:
    """The length or technical-content gate refused what the refused pages left:
    the refused pages are the cause, not the document's length or subject."""
    return _refusal(
        f"The PDF reader's limits refused page(s) {_page_list(parsed.skipped_pages)}"
        f" of {parsed.page_count}, and the pages it could read are not enough to "
        "convert. Export it to text (TXT or Markdown), or split it into smaller "
        "documents, and convert that instead.",
        ConversionErrorCode.DOCUMENT_TOO_COMPLEX,
    )


def _resolve(started: "asyncio.Future[None]") -> None:
    """Mark a request's worker started, unless the request stopped waiting."""
    if not started.done():
        started.set_result(None)


def _get_encoder():
    """tiktoken's ``cl100k_base`` (a GPT-4 / Claude approximation), from the
    loader every tiktoken count shares, or None when it cannot be loaded."""
    return _get_tiktoken_encoder("gpt-4")


def count_tokens(text: str) -> int:
    """Count tokens with tiktoken, or four characters a token without it.

    Without the encoding the size gate runs on the same heuristic as every
    other count in that state, rather than failing the upload. It understates
    dense text, so a CJK document over the limit can pass the gate; rejecting
    ordinary English documents by an overstated count would be the worse
    failure for a gate whose limit the conversion enforces again.
    """
    encoder = _get_encoder()
    if encoder is None:
        return len(text) // 4
    return len(encoder.encode(text))


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


def redact_sensitive_content(text: str) -> tuple[str, RedactionReport]:
    """Stage 3: Detect and redact secrets before the text reaches a model.

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
        RedactionEntry(
            type=redaction_type,
            count=count,
            context=_found_in(first_start[redaction_type]),
        )
        for redaction_type, count in counts.items()
    ]
    total = sum(counts.values())

    warning = None
    if total > 0:
        warning = (
            f"{total} sensitive items were redacted. "
            "Review generated runbooks for [REDACTED] placeholders."
        )

    return redacted, RedactionReport(
        redactions=redactions, warning=warning, total_redacted=total
    )


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
# Content Triage Prompt
# =============================================================================

TRIAGE_SYSTEM_PROMPT = """You are a document classifier. Determine if the provided text excerpt contains troubleshooting content.

Troubleshooting content includes: diagnostic procedures, error resolution steps, incident response procedures, runbooks, postmortems, or vendor troubleshooting guides.

Non-troubleshooting content includes: pure architecture docs, marketing material, API reference docs without error handling, meeting notes, project plans.

Respond with JSON:
{"is_actionable": true/false, "confidence": 0.0-1.0, "reason": "brief explanation"}"""

#: A "not actionable" verdict above this is a hard reject (``NOT_ACTIONABLE``);
#: at or below it the document converts with a warning.
TRIAGE_HARD_REJECT_CONFIDENCE = 0.8

#: The confidence of a verdict that states none usable. It must stay at or below
#: the threshold above, so a verdict without a usable confidence can only warn.
TRIAGE_DEFAULT_CONFIDENCE = 0.5


def _triage_confidence(raw: Any) -> float:
    """Read the classifier's ``confidence`` with #1502's rule (fm#1672).

    Nothing holds the classifier to ``0.0-1.0``, and a model that answers ``50``
    meaning 50% used to clear the hard-reject threshold. ``classify`` is the one
    implementation of the rule: a value in ``(1, 100]`` is a percentage and is
    rescaled, a ``bool`` is coerced to ``1.0``/``0.0``, and anything else
    (negative, above 100, NaN, infinity, not a number) is treated as absent and
    takes :data:`TRIAGE_DEFAULT_CONFIDENCE` — the advisory path, never the hard
    reject. A missing or ``null`` confidence is absence the model chose, not a
    repair, and is not counted. Every repair is counted on
    ``faultmaven_schema_field_repairs_total`` and logged with the raw value.
    """
    if raw is None:
        return TRIAGE_DEFAULT_CONFIDENCE
    kind, value = classify(raw)
    if kind == "conforming":
        return value
    if kind in ("rescaled", "coerced"):
        action = ConfidenceAction(kind)
    else:
        action, value = ConfidenceAction.DEFAULTED, TRIAGE_DEFAULT_CONFIDENCE
    repair = ConfidenceRepair(
        schema="TriageResult",
        field="confidence",
        action=action,
        raw=raw,
        value=value,
    )
    count_confidence_repair(repair)
    logger.warning(
        "Content triage confidence repaired: %s",
        repair.note(),
        extra={"action": action.value, "raw": short_repr(raw), "value": value},
    )
    return value


# =============================================================================
# DocumentPreprocessor
# =============================================================================


# =============================================================================
# File Integrity Validation
# =============================================================================

# Magic bytes for supported formats
_FILE_SIGNATURES = {
    b"%PDF": "application/pdf",
    b"PK\x03\x04": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


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
        except Exception:
            return "Cannot read file header — file may be corrupt"

    return None


# =============================================================================
# Encoding Detection for Text Files
# =============================================================================


def read_text_with_fallback(file_path: Path) -> str:
    """Read text file with encoding fallback: UTF-8 → Latin-1 → error."""
    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        pass

    try:
        return file_path.read_text(encoding="latin-1")
    except UnicodeDecodeError:
        pass

    raise ValueError(
        "Cannot decode file — it is not valid UTF-8 or Latin-1. "
        "Please re-save the file as UTF-8 and try again."
    )


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

    ``preprocess`` needs both answers and they share every expensive step: the
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
# DocumentPreprocessor
# =============================================================================


class _Prepared(NamedTuple):
    """Stages 0-5's output, handed from the extraction pool to Stage 6."""

    extracted_text: str
    source_metadata: Dict[str, Any]
    redaction_report: RedactionReport
    token_count: int
    warnings: list[str]


class DocumentPreprocessor:
    """Orchestrates the preprocessing pipeline."""

    def __init__(self, llm_router=None, settings=None, sanitizer=None):
        self._parser = DocumentParser()
        self._llm_router = llm_router
        self._settings = settings
        # The DI sanitizer the conversion service is handed: what the triage
        # call sends is redacted with it at this layer (#1901), not left to
        # whichever router is configured.
        self._sanitizer = sanitizer

    def _prepare(
        self,
        file_path: Path,
        content_type: str,
        on_start: Optional[Callable[[], object]] = None,
        abandoned: Optional[threading.Event] = None,
    ) -> PreprocessingResult | _Prepared:
        """Stages 0-5, synchronous CPU over the caller's file: ``preprocess``
        runs this in ``_EXTRACTION_POOL``, never on the event loop. Returns the
        rejection, or what Stage 6 needs. ``on_start`` tells the caller this
        request holds a worker; ``abandoned`` is set once it stops waiting."""
        if on_start is not None:
            on_start()
        warnings: list[str] = []

        # Stage 0: File integrity
        integrity_error = validate_file_integrity(file_path, content_type)
        if integrity_error:
            error_code = (
                ConversionErrorCode.FILE_EMPTY
                if "empty" in integrity_error.lower()
                else ConversionErrorCode.FILE_CORRUPT
            )
            return PreprocessingResult(
                extracted_text="",
                source_metadata={},
                is_rejected=True,
                rejection_reason=integrity_error,
                error_code=error_code,
            )

        # Stage 1: Format extraction
        try:
            parsed = self._parser.extract_document_text(
                file_path, content_type, abandoned
            )
        except DocumentTooComplex as e:
            return _refusal(str(e), ConversionErrorCode.DOCUMENT_TOO_COMPLEX)
        except ValueError as e:
            error_msg = str(e)
            if "encoding" in error_msg.lower() or "decode" in error_msg.lower():
                error_code = ConversionErrorCode.ENCODING_ERROR
            elif "unsupported" in error_msg.lower():
                error_code = ConversionErrorCode.UNSUPPORTED_FORMAT
            else:
                error_code = ConversionErrorCode.FILE_CORRUPT
            return PreprocessingResult(
                extracted_text="",
                source_metadata={},
                is_rejected=True,
                rejection_reason=error_msg,
                error_code=error_code,
            )
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
        is_runbook, near_runbook = classify_runbook_shape(extracted_text)
        if is_runbook:
            return PreprocessingResult(
                extracted_text="",
                source_metadata={},
                is_rejected=True,
                rejection_reason=(
                    "This document is already a FaultMaven runbook. Converting it "
                    "would re-derive a new runbook from its prose — splitting its "
                    "causes into separate runbooks and resetting its verification "
                    "status — rather than adding the runbook you already have. Add "
                    "it to the knowledge base directly instead of converting it."
                ),
                error_code=ConversionErrorCode.ALREADY_A_RUNBOOK,
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
        extracted_text = cleanup_text(extracted_text)

        # Check minimum length
        if len(extracted_text.strip()) < MIN_TEXT_LENGTH:
            if parsed.skipped_pages:
                return _refused_pages_left_too_little(parsed)
            return PreprocessingResult(
                extracted_text=extracted_text,
                source_metadata={},
                is_rejected=True,
                rejection_reason=(
                    f"Document contains too little text ({len(extracted_text)} characters, "
                    f"minimum: {MIN_TEXT_LENGTH}). The source lacks sufficient content "
                    "for runbook conversion."
                ),
                error_code=ConversionErrorCode.DOCUMENT_TOO_SHORT,
            )

        # Stage 2b: Heuristic technical content check (before LLM calls)
        has_technical, signal_count = check_technical_content(extracted_text)
        if not has_technical:
            if parsed.skipped_pages:
                return _refused_pages_left_too_little(parsed)
            return PreprocessingResult(
                extracted_text=extracted_text,
                source_metadata={},
                is_rejected=True,
                rejection_reason=(
                    "This document does not contain recognizable technical content "
                    "(no error messages, log patterns, commands, or diagnostic procedures found). "
                    "Runbook conversion requires troubleshooting-related source material."
                ),
                error_code=ConversionErrorCode.NO_TECHNICAL_CONTENT,
            )

        # Stage 3: Sensitive content scan
        extracted_text, redaction_report = redact_sensitive_content(extracted_text)
        if redaction_report.total_redacted > 0:
            warnings.append(
                f"{redaction_report.total_redacted} sensitive items were redacted. "
                "Redacted commands may be incomplete in the generated runbook. "
                "Review before verifying."
            )

        # Stage 4: Size check (hard limit)
        token_count = count_tokens(extracted_text)
        if token_count > MAX_TOKEN_LIMIT:
            return PreprocessingResult(
                extracted_text=extracted_text,
                source_metadata={},
                token_count=token_count,
                is_rejected=True,
                rejection_reason=(
                    f"Document contains {token_count:,} tokens (limit: {MAX_TOKEN_LIMIT:,}). "
                    "Please split the document into smaller, focused chapters "
                    "and convert each one separately."
                ),
                error_code=ConversionErrorCode.DOCUMENT_TOO_LONG,
            )

        # Stage 5: Source metadata extraction
        source_metadata = extract_source_metadata(
            file_path, content_type, extracted_text, parsed.page_count
        )
        source_metadata["token_count"] = token_count
        return _Prepared(
            extracted_text, source_metadata, redaction_report, token_count, warnings
        )

    async def preprocess(
        self, file_path: Path, content_type: str
    ) -> PreprocessingResult:
        """Run the full preprocessing pipeline: Stages 0-5 in the extraction
        pool, then Stage 6 on the loop.

        Two waits, each up to ``EXTRACTION_TIMEOUT_SECONDS``: for a worker, then
        for its run, timed from when it started. A request that gets no worker
        is ``CONVERSION_BUSY``, and its queued work is cancelled; one whose run
        outlasts the bound is refused, and its worker stops at the next page.
        Each bound is scoped to its own wait, so a ``TimeoutError`` raised in
        the worker propagates as the error it is.
        """
        loop = asyncio.get_running_loop()
        started, abandoned = loop.create_future(), threading.Event()
        # A copied context, as ``to_thread`` makes and ``submit`` does not:
        # pypdf's limits, the tenant and the request id are context variables.
        job = _EXTRACTION_POOL.submit(
            contextvars.copy_context().run,
            self._prepare,
            file_path,
            content_type,
            partial(loop.call_soon_threadsafe, _resolve, started),
            abandoned,
        )
        try:
            try:
                async with asyncio.timeout(EXTRACTION_TIMEOUT_SECONDS):
                    await started
            except TimeoutError:
                logger.warning(
                    "conversion of %s: no extraction worker free within %s s",
                    file_path.name,
                    EXTRACTION_TIMEOUT_SECONDS,
                )
                return _refusal(
                    "The server is busy converting other documents, so this one "
                    "could not start. Try again in "
                    f"{CONVERSION_BUSY_RETRY_AFTER_SECONDS} seconds.",
                    ConversionErrorCode.CONVERSION_BUSY,
                )
            try:
                async with asyncio.timeout(EXTRACTION_TIMEOUT_SECONDS) as running:
                    prepared = await asyncio.wrap_future(job)
            except TimeoutError:
                if not running.expired():
                    raise
                logger.warning(
                    "conversion of %s: extraction ran past %s s; refused",
                    file_path.name,
                    EXTRACTION_TIMEOUT_SECONDS,
                )
                return _refusal(
                    "Reading this document took longer than the "
                    f"{EXTRACTION_TIMEOUT_SECONDS}-second limit, so it was not "
                    "converted. Export it to text (TXT or Markdown), or split it "
                    "into smaller documents, and convert that instead.",
                    ConversionErrorCode.DOCUMENT_TOO_COMPLEX,
                )
        finally:
            # Queued: cancelled, so it never runs. Running: it stops after its
            # current page. Finished: neither does anything.
            abandoned.set()
            job.cancel()
        if isinstance(prepared, PreprocessingResult):
            return prepared
        extracted_text, source_metadata, redaction_report, token_count, warnings = (
            prepared
        )

        # Stage 6: Content triage (classifier LLM)
        triage_result = await self._run_content_triage(extracted_text)
        if triage_result:
            if (
                not triage_result.is_actionable
                and triage_result.confidence > TRIAGE_HARD_REJECT_CONFIDENCE
            ):
                return PreprocessingResult(
                    extracted_text=extracted_text,
                    source_metadata=source_metadata,
                    redaction_report=redaction_report,
                    triage_result=triage_result,
                    token_count=token_count,
                    is_rejected=True,
                    rejection_reason=(
                        "This document does not appear to contain troubleshooting content. "
                        "The conversion pipeline produces runbooks from diagnostic procedures, "
                        "incident reports, vendor troubleshooting guides, or postmortems."
                    ),
                    error_code=ConversionErrorCode.NOT_ACTIONABLE,
                )
            elif (
                not triage_result.is_actionable
                and triage_result.confidence <= TRIAGE_HARD_REJECT_CONFIDENCE
            ):
                warnings.append(
                    "This document may not contain sufficient troubleshooting content. "
                    "Generated runbooks may have incomplete sections."
                )

        return PreprocessingResult(
            extracted_text=extracted_text,
            source_metadata=source_metadata,
            redaction_report=redaction_report,
            triage_result=triage_result,
            warnings=warnings,
            token_count=token_count,
        )

    async def _run_content_triage(self, text: str) -> Optional[TriageResult]:
        """Stage 6: Send first 2K tokens to classifier to determine if actionable.

        Triage is advisory and fails OPEN — a document that cannot be
        classified is still converted — with one exception: a redaction that
        is required and cannot run. That is not a triage failure but a refusal
        to send, so it is raised between the two fail-open guards below, and a
        ``RedactionUnavailableError`` stops the conversion with nothing sent
        (#1901), as it stops an investigation turn.
        """
        if not self._llm_router or not self._settings:
            return None

        try:
            # Only send the first 2K tokens
            encoder = _get_encoder()
            if encoder is None:
                sample_text = text[: TRIAGE_SAMPLE_TOKENS * 4]
            else:
                tokens = encoder.encode(text)
                if len(tokens) > TRIAGE_SAMPLE_TOKENS:
                    sample_text = encoder.decode(tokens[:TRIAGE_SAMPLE_TOKENS])
                else:
                    sample_text = text

            classifier_model = self._settings.llm.get_classifier_model()
        except RawModelContentRefused:
            # A refusal is a programming error, never a fallback (#1998).
            raise
        except Exception as e:
            logger.warning(f"Content triage failed, proceeding anyway: {e}")
            return None

        messages = await model_boundary_redacted(
            [
                {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Classify this document excerpt:\n\n{sample_text}",
                },
            ],
            self._sanitizer,
        )

        try:
            # A knowledge-base document: no case to tag, so its trace carries
            # the enterprise alone (#2102, #2575).
            response = await route_model_call(
                self._llm_router,
                messages=messages,
                model=classifier_model,
                max_tokens=256,
                temperature=0.1,
                response_format={"type": "json_object"},
                # Land on CLASSIFIER_PROVIDER when one is set — the model
                # alone doesn't route; see intent_resolver for the same
                # pattern. The classifier ships PINNED, so this kwarg is
                # normally present and a router used here must accept it.
                **(
                    {"provider_override": override}
                    if (
                        override := self._settings.llm.explicit_role_provider(
                            "classifier"
                        )
                    )
                    else {}
                ),
            )

            if response.is_truncated:
                # Fail-open is right here — triage is advisory and a document
                # that cannot be classified should still be converted — but the
                # reason must not be silent. A 256-token budget for a three-field
                # JSON verdict should never run out, so a cut here means the
                # model is writing prose instead of the verdict, and that is
                # worth seeing rather than swallowing as a parse error (#1094).
                logger.warning(
                    "Content triage response truncated at the 256-token cap; "
                    "proceeding without a triage verdict"
                )
                return None

            # Fence-tolerant, for the reason in `loads_llm_json`. This site
            # fails QUIETLY — the exception handler below returns None and the
            # document proceeds with no triage verdict — so a provider without
            # a JSON mode silently disabled triage rather than reporting it.
            result = loads_llm_json(response.content, strict=True)
            return TriageResult(
                is_actionable=result.get("is_actionable", True),
                confidence=_triage_confidence(result.get("confidence")),
                reason=result.get("reason", ""),
            )
        except RawModelContentRefused:
            # A refusal is a programming error, never a fallback (#1998).
            raise
        except Exception as e:
            logger.warning(f"Content triage failed, proceeding anyway: {e}")
            return None
