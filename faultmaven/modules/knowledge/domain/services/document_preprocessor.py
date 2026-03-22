"""6-stage document preprocessing pipeline for conversion.

Stages:
1. Format extraction (via DocumentParser)
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


class DocumentPreprocessor:
    """Orchestrates the 6-stage preprocessing pipeline."""

    def __init__(self, llm_router=None, settings=None):
        self._parser = DocumentParser()
        self._llm_router = llm_router
        self._settings = settings

    async def preprocess(
        self,
        file_path: Path,
        content_type: str,
    ) -> PreprocessingResult:
        """Run the full preprocessing pipeline.

        Returns PreprocessingResult with extracted text, metadata, and triage results.
        """
        warnings: list[str] = []

        # Stage 1: Format extraction
        try:
            extracted_text = self._parser.parse(file_path, content_type)
        except ValueError as e:
            return PreprocessingResult(
                extracted_text="",
                source_metadata={},
                is_rejected=True,
                rejection_reason=str(e),
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
            )

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
