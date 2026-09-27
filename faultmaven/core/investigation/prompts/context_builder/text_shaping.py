import json
import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from faultmaven.core.preprocessing.evidence_metadata import (
    LOW_CONFIDENCE_THRESHOLD,
    EvidenceMetadata,
)

from .budget import HISTORY_AGENT_TRUNCATE_THRESHOLD

logger = logging.getLogger("faultmaven.core.investigation.prompts.context_builder")


_TRUNCATION_MARKER = "[...analysis removed for brevity...]"


def _parse_extract(raw: str) -> tuple[str, str | None, dict]:
    """Parse a structural-index JSON blob into (file_extract, search_map, file_meta).

    Post-010 source: ``uploaded_files.structural_index`` (set by the
    preprocessing pipeline). Pre-010 the same blob lived on
    ``evidence.extract``; the format is unchanged.

    Format is JSON with ``{"v": 1, "file_extract": ..., "search_map": ...,
    "file_meta": ...}`` (see extractors/protocol.py SCHEMA_VERSION). Falls
    back to treating the raw string as file_extract when the input is not
    JSON-shaped.
    """
    if not raw:
        return "", None, {}
    try:
        d = json.loads(raw)
        if isinstance(d, dict) and "file_extract" in d:
            # Coerce each field to the type the renderer will use it as. This
            # blob is NOT always extractor output: preprocessing falls back to
            # ``structural_index = extraction.content`` — the raw upload — when
            # an extractor times out or raises, so an uploaded file whose own
            # bytes are JSON with a ``file_extract`` key lands here with
            # arbitrary types under the other two keys. Un-coerced, a dict
            # ``search_map`` reached ``.strip()`` and a list ``file_meta``
            # reached ``.items()``, and the AttributeError killed prompt
            # assembly for the whole turn — a denial of service costing one
            # upload. A wrong-typed field is rendered as compact JSON rather
            # than dropped, so nothing the extractor did produce is lost.
            fe = d.get("file_extract", "")
            sm = d.get("search_map")
            fm = d.get("file_meta") or {}
            return (
                fe if isinstance(fe, str) else json.dumps(fe, separators=(",", ":")),
                (
                    sm
                    if isinstance(sm, str)
                    else (None if sm is None else json.dumps(sm, separators=(",", ":")))
                ),
                fm if isinstance(fm, dict) else {"file_meta": fm},
            )
    except (json.JSONDecodeError, TypeError):
        pass
    return raw, None, {}


def _format_file_meta(file_meta: dict) -> str:
    """Format file_meta dict as a human-readable k=v string.

    Scalar values are rendered directly; nested dicts/lists use compact JSON
    so the LLM can read them without encountering Python repr artifacts.
    """
    parts = []
    for k, v in file_meta.items():
        if isinstance(v, (dict, list)):
            parts.append(f"{k}={json.dumps(v, separators=(',', ':'))}")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


def _confidence_marker(ev) -> tuple[str, Optional[str]]:
    """Return ``(attr, advisory)`` for the classifier-confidence marker.

    The attr is either ``' confidence="low"'`` or empty; the advisory is
    a one-line note to render inside the evidence's ``<file_extract>``
    block when the marker fires, so the model has an in-prompt cue
    reinforcing the XML attribute.

    Returns empty marker when:

    - the feature flag ``FAULTMAVEN_PREPROCESSING_CONFIDENCE_MARKER`` is off,
    - ``ev.metadata`` is None or missing a ``classification`` block
      (existing evidence predating Phase 1),
    - confidence is above the low-confidence threshold.
    """
    try:
        from faultmaven.config.settings import get_settings

        enabled = get_settings().preprocessing.confidence_marker_enabled
    except Exception:
        enabled = False

    if not enabled:
        return "", None

    metadata = getattr(ev, "metadata", None)
    if not metadata:
        return "", None

    try:
        parsed = EvidenceMetadata.from_storage_dict(metadata)
    except Exception:
        return "", None

    classification = parsed.classification
    if classification is None:
        return "", None

    if classification.confidence >= LOW_CONFIDENCE_THRESHOLD:
        return "", None

    advisory = (
        f"[Classifier confidence: {classification.confidence:.2f} "
        f"(source: {classification.source}). Treat the file extract "
        f"below as tentative — the classifier was unsure about this "
        f"evidence's type, so the extractor may have been wrong.]"
    )
    return ' confidence="low"', advisory


# Stopwords excluded from query-section keyword matching (common English words
# that would cause false-positive matches across unrelated sections).
_RERANK_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "can",
        "could",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "once",
        "and",
        "but",
        "or",
        "nor",
        "not",
        "no",
        "so",
        "if",
        "when",
        "what",
        "which",
        "who",
        "whom",
        "this",
        "that",
        "these",
        "those",
        "am",
        "it",
        "its",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "him",
        "his",
        "she",
        "her",
        "they",
        "them",
        "their",
        "any",
        "all",
        "each",
        "every",
        "how",
        "why",
        "where",
        "there",
        "here",
        "up",
        "down",
        "about",
    }
)


# Heading patterns the rerank tries, in priority order. The primary contract
# is `\n## ` (level-2 markdown headings), which is what the Copilot extension's
# `htmlToStructuredText` emits for `<h2>` elements (see
# faultmaven-copilot/src/lib/utils/html-to-structured-text.ts:171). Fallbacks
# accept other markdown heading levels so a producer-side change to `### ` or
# `#### ` degrades to "less precise reranking" rather than "no reranking".
_RERANK_HEADING_PATTERNS = (
    "\n## ",  # primary: htmlToStructuredText H2 contract
    "\n### ",  # fallback: H3 (htmlToStructuredText H4 → '### ' inverted-indent)
    "\n#### ",  # fallback: H4
)


def _split_rerank_sections(content: str) -> tuple[str, list[str], str]:
    """Find the first heading style that produces >= 2 sections.

    Returns ``(delimiter, sections, preamble)``. If no heading style matches,
    returns ``("", [], content)`` so the caller can no-op gracefully and log
    the format-drift signal.
    """
    for delim in _RERANK_HEADING_PATTERNS:
        parts = content.split(delim)
        if len(parts) > 1:
            return delim, parts[1:], parts[0]
    return "", [], content


def _rerank_page_capture_sections(content: str, query: str) -> str:
    """Rerank page-capture sections by relevance to the user's query.

    **Contract with the producer:** page captures from
    ``htmlToStructuredText`` (faultmaven-copilot) emit ``## `` markdown
    headings to delimit sections (one heading per ``<h2>``). This function
    splits on those headings, scores each section by normalised keyword
    overlap with *query*, and reassembles in descending relevance order so
    the most pertinent panels/messages survive the 4000-char per-item cap
    applied downstream at :data:`EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM`.

    The **preamble** (everything before the first heading) is always pinned
    at position 0 — it contains ``[captured_at: …]`` and the page title,
    which provide essential temporal context.

    Scoring: ``len(query_terms ∩ section_terms) / len(query_terms)``.
    Ties preserve original document order (stable sort).

    **Format-drift handling:** if the primary ``## `` delimiter is absent,
    the function tries ``### `` and ``#### `` before giving up. A complete
    no-split (no markdown headings of any depth, or empty content) is
    logged at INFO level via the structured ``rerank.no_op`` event so
    operators can spot drift between this function and the Copilot
    serializer. The function returns the original content unchanged in
    that case — it does NOT raise.

    Example contract input::

        [captured_at: 2024-03-15T19:50:00Z]
        # Grafana - payments-api / Production Overview

        ## Row 1: Health Overview

        ### Panel: Service Up
        ...

        ## Row 2: Request Volume
        ...
    """
    delim, sections, preamble = _split_rerank_sections(content)
    if not delim:
        # Format drift: no recognized heading delimiter found. Emit a
        # structured log so a Copilot serializer change (or a non-page-
        # capture fuel sneaking through this code path) is visible.
        logger.info(
            "rerank.no_op",
            extra={
                "reason": "no_heading_delimiter",
                "content_length": len(content),
            },
        )
        return content

    if delim != "\n## ":
        # Recoverable drift: primary contract violated, fallback engaged.
        logger.info(
            "rerank.fallback_delimiter",
            extra={
                "primary": "\\n## ",
                "fallback_used": delim.strip(),
                "section_count": len(sections),
            },
        )

    # Tokenise query into meaningful keywords
    query_terms = {
        w
        for w in re.sub(r"[^\w\s]", " ", query.lower()).split()
        if w not in _RERANK_STOPWORDS and len(w) > 1
    }
    if not query_terms:
        return content

    # Score each section by keyword overlap
    scored: list[tuple[int, float, str]] = []
    for idx, section in enumerate(sections):
        section_lower = section.lower()
        section_words = set(re.sub(r"[^\w\s]", " ", section_lower).split())
        overlap = len(query_terms & section_words)
        score = overlap / len(query_terms)
        scored.append((idx, score, section))

    # Stable sort descending by score (preserves original order on ties)
    scored.sort(key=lambda t: -t[1])

    return preamble + delim + delim.join(s[2] for s in scored)


def _smart_truncate_agent_response(
    response: str,
    threshold: int = HISTORY_AGENT_TRUNCATE_THRESHOLD,
) -> str:
    """Truncate agent response preserving narrative structure.

    Preserves the opening (acknowledgment/key insight) and closing
    (question/next action) while replacing the middle analysis blocks
    with a brevity marker. User messages are never passed to this function.

    Strategy:
    1. Under threshold → return as-is
    2. Split on paragraph boundaries (double newline)
    3. Keep first paragraph + last paragraph, replace middle
    4. If first+last still too long, trim at sentence boundaries
    """
    if len(response) <= threshold:
        return response

    paragraphs = [p.strip() for p in response.split("\n\n") if p.strip()]

    if len(paragraphs) >= 3:
        first = paragraphs[0]
        last = paragraphs[-1]
        combined = f"{first}\n\n{_TRUNCATION_MARKER}\n\n{last}"

        # If first+last is still too long, trim each at sentence boundaries
        if len(combined) > threshold * 1.5:
            first = _trim_to_sentence(first, 300)
            last = _trim_to_sentence_end(last, 250)
            combined = f"{first}\n\n{_TRUNCATION_MARKER}\n\n{last}"

        return combined

    if len(paragraphs) == 2:
        # Two paragraphs — keep both but trim if needed
        first = _trim_to_sentence(paragraphs[0], 350)
        last = _trim_to_sentence_end(paragraphs[1], 250)
        return f"{first}\n\n{last}"

    # Single paragraph (no double-newline structure) — sentence-based fallback
    first = _trim_to_sentence(response, 350)
    last = _trim_to_sentence_end(response, 200)
    if first != response:
        return f"{first}\n\n{_TRUNCATION_MARKER}\n\n{last}"
    return response


def _trim_to_sentence(text: str, max_chars: int) -> str:
    """Trim text to the last sentence boundary within max_chars."""
    if len(text) <= max_chars:
        return text
    # Find the last sentence-ending punctuation within limit
    truncated = text[:max_chars]
    for end_char in [". ", ".\n", "? ", "?\n", "! ", "!\n"]:
        last_pos = truncated.rfind(end_char)
        if last_pos > max_chars // 3:  # Don't cut too aggressively
            return truncated[: last_pos + 1]
    # No good sentence boundary — cut at word boundary
    last_space = truncated.rfind(" ")
    if last_space > max_chars // 2:
        return truncated[:last_space] + "..."
    return truncated + "..."


def _trim_to_sentence_end(text: str, max_chars: int) -> str:
    """Keep the last max_chars of text, starting at a sentence boundary."""
    if len(text) <= max_chars:
        return text
    # Find a sentence start near the cut point
    tail = text[-max_chars:]
    for start_marker in [". ", ".\n", "? ", "?\n", "! ", "!\n"]:
        first_pos = tail.find(start_marker)
        if 0 < first_pos < max_chars // 2:
            return tail[first_pos + 2 :]  # Skip the punctuation + space
    # No good sentence boundary — just take the tail
    return "..." + tail.lstrip()


@dataclass
class SanitizedInput:
    """Result of input sanitization with warnings."""

    content: str
    """Sanitized content safe for prompt inclusion"""

    warnings: List[str]
    """Security warnings detected during sanitization"""

    was_modified: bool
    """Whether the input was modified during sanitization"""


def sanitize_user_input(message: str, max_length: int = 10000) -> SanitizedInput:
    """
    Bound and inspect user input for the prompt. Does NOT rewrite its bytes.

    Reference: Prompt Engineering Guide Section 16.2 - Input Sanitization

    **It does not escape angle brackets, and must not (#1256).** It used to,
    and the escape was the main path's only defence against a message forging
    prompt structure. #1228 fenced the other caller-controlled blocks and
    #1242 fenced the fallback's ``<user_message>``; this path is now fenced
    too — ``<user_message>`` and ``<conversation_history>`` carry the
    assembly's token on their delimiters — so the escape had nothing left to
    protect and cost what escaping always costs here: nothing on this path
    decodes, so ``&lt;`` is four literal characters the model reasons about
    and echoes back at the user (the #666 failure mode named in
    :mod:`~faultmaven.core.investigation.prompts.fence`). It also corrupted
    ordinary prose: "consumer lag went from <1000 to >250000" is an inequality,
    not markup, and reached the model as ``&lt;1000``.

    Escaping is still correct where something DOES decode —
    ``causal_map._sanitize_label`` escapes for mermaid, and stays as it is.

    What survives here is everything that does not touch the bytes except to
    bound them: the length cap, and the injection / state-manipulation
    detectors, which only warn.

    Args:
        message: User input message
        max_length: Maximum allowed message length

    Returns:
        SanitizedInput with bounded content and warnings
    """
    warnings = []
    was_modified = False
    sanitized = message

    # 1. Detect prompt injection patterns
    injection_patterns = [
        r"ignore\s+(all\s+)?previous\s+instructions?",
        r"forget\s+(all\s+)?previous\s+instructions?",
        r"you\s+are\s+now\s+",
        r"your\s+new\s+role\s+is",
        r"system\s*:\s*",
        r"<\s*system\s*>",
        r"override\s+instructions?",
        r"disregard\s+(all\s+)?above",
    ]

    for pattern in injection_patterns:
        if re.search(pattern, sanitized, re.IGNORECASE):
            warnings.append(
                f"Potential prompt injection detected: '{pattern}' - keeping for transparency but flagging"
            )
            # Don't modify - log for transparency but allow investigation of injection attempts

    # 2. Limit message length
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]
        was_modified = True
        warnings.append(
            f"Message truncated from {len(message)} to {max_length} characters"
        )

    # 3. Detect state manipulation attempts
    state_manipulation_patterns = [
        r"(milestone|progress|status)\s*=\s*(true|false)",
        r"set\s+(milestone|status|stage)",
        r"mark\s+as\s+(complete|resolved|closed)",
    ]

    for pattern in state_manipulation_patterns:
        if re.search(pattern, sanitized, re.IGNORECASE):
            warnings.append(
                f"Potential state manipulation detected: '{pattern}' - user cannot directly modify state"
            )

    return SanitizedInput(
        content=sanitized,
        warnings=warnings,
        was_modified=was_modified,
    )
