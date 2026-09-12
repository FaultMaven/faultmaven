"""6-stage document preprocessing pipeline for conversion.

Stages:
1. Format extraction (via DocumentParser)
1b. Already-a-runbook gate: refuse a document that is already a FaultMaven
    runbook (ALREADY_A_RUNBOOK), or warn when only its frontmatter matches
2. Content cleanup (strip boilerplate)
3. Sensitive content scan (PII redaction)
4. Size check (hard limit at 30K tokens)
5. Source metadata extraction
6. Content triage (LLM classifier)
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import tiktoken

from faultmaven.modules.knowledge.domain.models.conversion import (
    ConversionErrorCode,
    PreprocessingResult,
    RedactionEntry,
    RedactionReport,
    TriageResult,
)
from faultmaven.modules.knowledge.domain.services.document_parser import (
    DocumentParser,
)

logger = logging.getLogger(__name__)

MAX_TOKEN_LIMIT = 30_000
MIN_TEXT_LENGTH = 200
TRIAGE_SAMPLE_TOKENS = 2000

# tiktoken encoder for token counting (cl100k_base covers GPT-4 / Claude approximation)
_ENCODER = None


def _get_encoder():
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


def count_tokens(text: str) -> int:
    """Count tokens using tiktoken (universal approximation)."""
    return len(_get_encoder().encode(text))


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
# PII Redaction (regex layer — Presidio optional)
# =============================================================================

_REDACTION_PATTERNS = [
    (
        "api_key",
        r"(?i)(?:api[_-]?key|apikey)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-]{20,})['\"]?",
    ),
    ("aws_key", r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    ("jwt", r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+"),
    ("private_key", r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"),
    (
        "db_connection_string",
        r"(?i)(?:postgres(?:ql)?|mysql|mongodb|redis)://[^\s'\"]+",
    ),
    ("password", r"(?i)(?:password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\"]{6,}['\"]?"),
]


def redact_sensitive_content(text: str) -> tuple[str, RedactionReport]:
    """Stage 3: Detect and redact sensitive content before LLM."""
    redactions: list[RedactionEntry] = []
    total = 0

    for redaction_type, pattern in _REDACTION_PATTERNS:
        matches = list(re.finditer(pattern, text))
        if matches:
            count = len(matches)
            total += count
            # Determine context from first match location
            first_match = matches[0]
            start = max(0, first_match.start() - 30)
            end = min(len(text), first_match.end() + 30)
            context_snippet = text[start:end]

            # Determine where it was found
            if "```" in text[max(0, first_match.start() - 50) : first_match.start()]:
                context = "Found in code blocks"
            else:
                context = "Found in text content"

            redactions.append(
                RedactionEntry(type=redaction_type, count=count, context=context)
            )
            text = re.sub(pattern, f"[REDACTED:{redaction_type}]", text)

    warning = None
    if total > 0:
        warning = (
            f"{total} sensitive items were redacted. "
            "Review generated runbooks for [REDACTED] placeholders."
        )

    return text, RedactionReport(
        redactions=redactions, warning=warning, total_redacted=total
    )


# =============================================================================
# Source Metadata Extraction
# =============================================================================


def extract_source_metadata(
    file_path: Path, content_type: str, extracted_text: str
) -> Dict[str, Any]:
    """Stage 5: Capture provenance metadata from source file."""
    stat = file_path.stat()
    word_count = len(extracted_text.split())

    metadata = {
        "original_filename": file_path.name,
        "file_size_bytes": stat.st_size,
        "content_type": content_type,
        "word_count": word_count,
    }

    # Page count for PDFs
    if content_type == "application/pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(file_path))
            metadata["page_count"] = len(reader.pages)
        except Exception:
            pass

    return metadata


# =============================================================================
# Content Triage Prompt
# =============================================================================

TRIAGE_SYSTEM_PROMPT = """You are a document classifier. Determine if the provided text excerpt contains troubleshooting content.

Troubleshooting content includes: diagnostic procedures, error resolution steps, incident response procedures, runbooks, postmortems, or vendor troubleshooting guides.

Non-troubleshooting content includes: pure architecture docs, marketing material, API reference docs without error handling, meeting notes, project plans.

Respond with JSON:
{"is_actionable": true/false, "confidence": 0.0-1.0, "reason": "brief explanation"}"""


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

# Fenced blocks are masked before the section search, because a postmortem that
# QUOTES the runbook it followed is a document this pipeline exists to convert,
# and a fenced quote of ``## Symptom Recognition`` + ``## Causes`` would
# otherwise satisfy the body half and hard-refuse it (422). Note this is where
# the gate deliberately parts company with ``RunbookValidator._validate_structure``,
# which does NOT mask: the validator is already looking at something claiming to
# be a runbook, so a fenced heading costs it nothing, while here it costs a
# legitimate conversion. ``_flag_malformed_cause_headings`` masks for the same
# reason this does.
#
# Masked to BLANK LINES rather than deleted. Deleting splices the lines either
# side of the fence together, which can manufacture a line that starts with
# ``##`` where the source had none — the same splice hazard #1241 hit when it
# deleted comments instead of masking them.
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Bytes before the opening ``---`` that must not disarm the gate. The
# frontmatter anchor is ``re.match``, so it binds at offset 0: a UTF-8 BOM (the
# "UTF-8 with BOM" default of several Windows editors, preserved verbatim by
# ``Path.read_text(encoding="utf-8")``) or one blank line ahead of the
# delimiter made ``detect_existing_runbook`` answer False for a byte-identical
# runbook. Detection runs BEFORE ``cleanup_text``, which is the only thing that
# would otherwise have removed them, so #1375 still reproduced on any runbook
# saved by such an editor.
_LEADING_NOISE = "\ufeff \t\r\n"


def detect_existing_runbook(text: str) -> bool:
    """Is this document already a FaultMaven runbook?

    True only when BOTH hold: the document opens with runbook frontmatter, and
    its body carries the canonical section skeleton OUTSIDE any fenced block.
    See the constants above for why neither half decides it alone.
    """
    return _has_runbook_frontmatter(text) and has_runbook_body(text)


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

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
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

    Deliberately NARROWER than ``_has_runbook_frontmatter``. The threshold is
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
    fields = _runbook_frontmatter_fields(text)
    return (
        len(fields) >= _RUNBOOK_FRONTMATTER_MIN_FIELDS
        and "symptom_class" in fields
        and not has_runbook_body(text)
    )


def has_runbook_body(text: str) -> bool:
    """Does the body carry the canonical section skeleton, outside code fences?"""
    unfenced = _CODE_FENCE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return all(pattern.search(unfenced) for pattern in _RUNBOOK_BODY_SECTION_RES)


# =============================================================================
# DocumentPreprocessor
# =============================================================================


class DocumentPreprocessor:
    """Orchestrates the preprocessing pipeline."""

    def __init__(self, llm_router=None, settings=None):
        self._parser = DocumentParser()
        self._llm_router = llm_router
        self._settings = settings

    async def preprocess(
        self,
        file_path: Path,
        content_type: str,
    ) -> PreprocessingResult:
        """Run the full preprocessing pipeline."""
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
            extracted_text = self._parser.parse(file_path, content_type)
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
        if detect_existing_runbook(extracted_text):
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
        if is_near_runbook(extracted_text):
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
            file_path, content_type, extracted_text
        )
        source_metadata["token_count"] = token_count

        # Stage 6: Content triage (classifier LLM)
        triage_result = await self._run_content_triage(extracted_text)
        if triage_result:
            if not triage_result.is_actionable and triage_result.confidence > 0.8:
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
            elif not triage_result.is_actionable and triage_result.confidence <= 0.8:
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
        """Stage 6: Send first 2K tokens to classifier to determine if actionable."""
        if not self._llm_router or not self._settings:
            return None

        try:
            # Only send the first 2K tokens
            encoder = _get_encoder()
            tokens = encoder.encode(text)
            if len(tokens) > TRIAGE_SAMPLE_TOKENS:
                sample_text = encoder.decode(tokens[:TRIAGE_SAMPLE_TOKENS])
            else:
                sample_text = text

            classifier_model = self._settings.llm.get_classifier_model()

            response = await self._llm_router.route(
                messages=[
                    {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Classify this document excerpt:\n\n{sample_text}",
                    },
                ],
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

            import json

            result = json.loads(response.content)
            return TriageResult(
                is_actionable=result.get("is_actionable", True),
                confidence=float(result.get("confidence", 0.5)),
                reason=result.get("reason", ""),
            )
        except Exception as e:
            logger.warning(f"Content triage failed, proceeding anyway: {e}")
            return None
