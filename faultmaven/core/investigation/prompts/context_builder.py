"""Investigation Prompt Context Builder

This module handles gathering and truncating investigation context for LLM prompts,
ensuring we stay within token limits while preserving high-priority information.

Priority:
1. System Prompt & Response Schema (Fixed)
2. Case Definition & Core Identity
3. Recent Conversation History (Last N turns)
4. Knowledge Base Search Results
5. Evidence Context (Sliding Window: Tier A structural index + Tier B/C summaries)
6. Older Conversation History (Truncated)

Gap #6: Token Budget Dynamic Loading
- Provider-specific token limits (Claude: 200K, GPT-4: 128K, etc.)
- Reference: Prompt Engineering Guide Section 11.3

Gap #9: Input Sanitization
- Prompt injection pattern detection
- XML tag escaping
- Message length limits
- Reference: Prompt Engineering Guide Section 16.2
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional

from faultmaven.core.investigation.causal_graph import (
    BLOCK_REASON_COUNT,
    BLOCK_REASON_HEDGED,
    BLOCK_REASON_MIRROR,
    mece_contested_root_ids,
    root_support_block_reasons,
)
from faultmaven.core.investigation.evidence_need_surfacing import (
    select_surfaced_causal_needs,
)
from faultmaven.core.preprocessing.evidence_metadata import (
    LOW_CONFIDENCE_THRESHOLD,
    EvidenceMetadata,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    EntityType,
    EvidenceCategory,
    InvestigationActionType,
    InvestigationStage,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
)
from faultmaven.modules.case.domain.models import CauseState


# =============================================================================
# Evidence Context Sliding Window Configuration
# =============================================================================
# These constants are tunable via InvestigationContextSettings (see
# faultmaven.config.settings). The module-level names are preserved so
# importing tests / call-sites continue to work; they pull the live values
# from settings at import time. To change at runtime, set the env vars
# EVIDENCE_CONTEXT_RECENT_COUNT / EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM /
# EVIDENCE_CONTEXT_MAX_TOTAL_CHARS and restart.
def _load_context_caps() -> tuple[int, int, int]:
    """Return (recent_count, max_chars_per_item, max_total_chars).

    Imported here (not at module top) to keep the import graph cheap and
    to avoid forcing a settings load in every test that imports a single
    helper from this module.
    """
    try:
        from faultmaven.config.settings import get_settings

        s = get_settings().investigation_context
        return s.recent_count, s.max_chars_per_item, s.max_total_chars
    except Exception:
        # Settings may not be available in some test contexts; fall back
        # to the documented defaults.
        return 3, 4000, 16000


(
    EVIDENCE_CONTEXT_RECENT_COUNT,
    EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM,
    EVIDENCE_CONTEXT_MAX_TOTAL_CHARS,
) = _load_context_caps()

# =============================================================================
# Graduated Conversation History Configuration
# =============================================================================
# Recent turns: full user messages + smart-truncated agent responses
HISTORY_VERBATIM_TURNS = 3
# Older turns: one-line summaries from TurnProgress metadata
HISTORY_SUMMARY_MAX_TURNS = 7
# Agent response character count before smart-truncation kicks in
HISTORY_AGENT_TRUNCATE_THRESHOLD = 600

# =============================================================================
# State Summary Configuration
# =============================================================================
# Turn threshold at which graduated history is replaced with compact state summary
STATE_SUMMARY_TURN_THRESHOLD = 15
# Max evidence digest items in state summary
STATE_SUMMARY_MAX_EVIDENCE_DIGESTS = 8
# Max chars per evidence digest entry
STATE_SUMMARY_DIGEST_CHARS = 180
# Max chars per KB solution in context (prevents verbose runbooks from consuming budget)
KB_MAX_SOLUTION_CHARS = 800

# Min structural-index length for an uploaded file to count as a searchable
# target. This is the single source of truth: the context builder renders a
# file as ``<uploaded_file searchable="true">`` only above this length, and
# the engine's force_tools guards (``_has_searchable_material`` /
# ``_turn_delivers_evidence_bearing_attachment``) use the same rule — they
# must agree, or a forced Directed-Analysis turn could have no rendered search
# target and the tool loop would crash for lack of one (#708).
SEARCHABLE_STRUCTURAL_INDEX_MIN_CHARS = 10


def structural_index_is_searchable(structural_index: Optional[str]) -> bool:
    """True when an uploaded file's structural index carries enough content to
    be a search target. Shared by the context builder's ``searchable`` render
    and the engine's force_tools guards so the threshold stays in lockstep."""
    return bool(structural_index) and (
        len(structural_index) > SEARCHABLE_STRUCTURAL_INDEX_MIN_CHARS
    )


logger = logging.getLogger(__name__)

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
            return (
                d.get("file_extract", ""),
                d.get("search_map"),
                d.get("file_meta") or {},
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
                "first_120_chars": content[:120],
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
    Sanitize user input for prompt injection patterns.

    Reference: Prompt Engineering Guide Section 16.2 - Input Sanitization

    Args:
        message: User input message
        max_length: Maximum allowed message length

    Returns:
        SanitizedInput with sanitized content and warnings
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

    # 2. Escape XML-like tags to prevent structure manipulation
    # Preserve structure by escaping < and >
    original_length = len(sanitized)
    sanitized = sanitized.replace("<", "&lt;").replace(">", "&gt;")
    if len(sanitized) != original_length:
        was_modified = True
        warnings.append("XML-like tags escaped to prevent structure manipulation")

    # 3. Limit message length
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]
        was_modified = True
        warnings.append(
            f"Message truncated from {len(message)} to {max_length} characters"
        )

    # 4. Detect state manipulation attempts
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


def get_token_budget_for_provider(
    provider_name: str, model_name: Optional[str] = None
) -> int:
    """
    Get provider-specific token budget for prompts.

    Reference: Prompt Engineering Guide Section 11.3 - Provider-Specific Limits

    A conservative per-provider budget table. The main prompt-assembly path does
    NOT size against this — it uses
    ``model_context.resolve_model_budget(...).prompt_target`` (the flat
    ``PROMPT_TARGET_TOKENS``) directly. This function survives as a fallback for
    the two spots that need a per-provider number without a resolved budget: the
    evidence char-cap in ``_effective_evidence_char_budget`` when no explicit
    override is passed, and the ``max_tokens is None`` default in
    ``build_investigation_context`` (direct callers / tests that omit a budget).

    Args:
        provider_name: Provider name (e.g., "anthropic", "openai", "fireworks")
        model_name: Optional specific model name for fine-grained limits

    Returns:
        Recommended prompt token budget (conservative to leave room for response)
    """
    # Provider-specific prompt budgets (conservative to allow response tokens)
    # Based on total context windows minus expected response size

    # Default to conservative 8K if provider unknown
    default_budget = 8000

    provider_lower = provider_name.lower() if provider_name else ""
    model_lower = model_name.lower() if model_name else ""

    # Anthropic Claude (200K context window)
    if "anthropic" in provider_lower or "claude" in model_lower:
        if "sonnet" in model_lower or "opus" in model_lower:
            return 12000  # 12K prompt budget for 200K context
        return 10000  # Conservative for other Claude models

    # OpenAI GPT-4 (128K context window)
    elif "openai" in provider_lower or "gpt-4" in model_lower:
        if "turbo" in model_lower or "gpt-4o" in model_lower:
            return 10000  # 10K prompt budget for 128K context
        return 8000  # Conservative for older GPT-4

    # Google Gemini (1M+ context window)
    elif "google" in provider_lower or "gemini" in model_lower:
        return 15000  # 15K prompt budget for massive context

    # Meta Llama (128K context window)
    elif "meta" in provider_lower or "llama" in model_lower:
        return 8000  # 8K prompt budget for 128K context

    # Fireworks AI (context varies by model)
    elif "fireworks" in provider_lower:
        if "llama-3.3" in model_lower:
            return 8000
        return 6000  # Conservative for other models

    # Cohere (4K-128K depending on model)
    elif "cohere" in provider_lower:
        return 6000  # Conservative

    # Default fallback
    logger.debug(
        f"Unknown provider '{provider_name}' with model '{model_name}', "
        f"using default budget of {default_budget} tokens"
    )
    return default_budget


class TokenBudget:
    """Running token budget shared across prompt sections (GAP-2/GAP-4).

    Token-native: each section is measured with the provider/model tokenizer
    via :func:`faultmaven.utils.token_estimation.estimate_tokens` rather than
    the old 4-chars≈1-token character heuristic. When no provider is supplied
    (internal callers / tests) it degrades to the character fallback that
    ``estimate_tokens`` already provides, so behavior is unchanged for those
    paths.

    The single instance threaded through ``build_investigation_context`` makes
    this the accountant for the *sum* of the dynamic sections: ``use()``
    deducts from one shared pool, so later (lower-priority) sections are
    trimmed once the budget is spent. Sections are fed in priority order by the
    caller (see ``build_investigation_context``), so trimming hits the
    lowest-value content first.
    """

    def __init__(
        self,
        limit_tokens: int = 8000,
        *,
        provider_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        self.limit_tokens = limit_tokens
        self._limit_units = limit_tokens
        self.used_tokens = 0
        self._provider = provider_name
        self._model = model_name

    def count(self, text: str) -> int:
        """Size of *text* in tokens."""
        if not text:
            return 0
        from faultmaven.utils.token_estimation import estimate_tokens

        return estimate_tokens(
            text, provider=self._provider or "local", model=self._model
        )

    def _truncate_to(self, text: str, token_limit: int, keep: str = "head") -> str:
        """Truncate *text* to ~``token_limit`` tokens with a marker.

        ``keep="head"`` keeps the start (default); ``keep="tail"`` keeps the end
        (used for conversation history, whose most-recent turns are at the end).
        Never returns "" for non-empty input above a 2-token floor — it always
        leaves at least a bare ``[...]`` marker, so a section is never *silently*
        dropped (INV-4). Does not mutate ``used_tokens``.
        """
        if not text or token_limit <= 2:
            return ""
        marker = (
            "\n[...truncated...]"
            if token_limit < 30
            else "\n[... Content truncated due to context limit ...]"
        )
        marker_tokens = self.count(marker)
        # Only room for (about) the marker → emit a minimal non-silent trace.
        if token_limit <= marker_tokens + 1:
            return "[...]"

        # keep="tail" drops the OLDEST (leading) content, so the marker goes at
        # the FRONT; keep="head" drops trailing content, marker at the end.
        def _compose(slice_text: str) -> str:
            return marker + slice_text if keep == "tail" else slice_text + marker

        def _slice(n: int) -> str:
            return text[-n:] if keep == "tail" else text[:n]

        char_budget = max(20, (token_limit - marker_tokens) * 4)
        truncated = _slice(char_budget)
        while truncated and self.count(_compose(truncated)) > token_limit:
            char_budget = int(char_budget * 0.85)
            truncated = _slice(char_budget)
        return _compose(truncated) if truncated else "[...]"

    def use(self, text: str, cap: Optional[int] = None) -> str:
        """Admit *text* against the shared budget, optionally capped.

        ``cap`` is a per-section token ceiling (priority-greedy allocation): the
        section may take at most ``min(remaining_global, cap)`` tokens. Without a
        cap it may take all remaining budget. Over-limit content is truncated
        with a marker (never silently dropped — see INV-4).
        """
        if not text:
            return text
        allowance = self._limit_units - self.used_tokens
        if cap is not None:
            allowance = min(allowance, cap)
        if allowance <= 0:
            return ""
        tokens = self.count(text)
        if tokens <= allowance:
            self.used_tokens += tokens
            return text
        result = self._truncate_to(text, allowance)
        if result:
            self.used_tokens += self.count(result)
        return result


def _build_state_summary(case: Case) -> str:
    """
    Build compact state summary for long conversations (Gap #8: Section 11.5).

    Instead of full conversation history (~2000 tokens), provides compact summary (~200 tokens).

    Args:
        case: Current case

    Returns:
        Formatted state summary
    """
    # Problem description
    problem_desc = case.description[:100] if case.description else "Not specified"
    if case.problem_verification and case.problem_verification.symptom_statement:
        problem_desc = case.problem_verification.symptom_statement[:100]

    # Current stage
    stage = "INQUIRY"
    if case.state.value == "investigating" and case.current_stage:
        stage = case.current_stage.value.upper()
    elif case.state.value in ["resolved", "closed"]:
        stage = case.state.value.upper()

    # Verification status
    verified_items = []
    if case.progress:
        p = case.progress
        if p.symptom_verified:
            verified_items.append("symptom")

    verified = ", ".join(verified_items) if verified_items else "none"

    # Active hypotheses — include top 3 so the agent retains awareness of
    # competing theories when the full hypothesis block is absent.
    active_h = [
        h for h in case.hypotheses.values() if h.state.value in ["active", "validated"]
    ]
    if active_h:
        sorted_h = sorted(active_h, key=lambda h: h.likelihood, reverse=True)
        hypothesis_lines = []
        for h in sorted_h[:3]:
            status_tag = " [VALIDATED]" if h.state.value == "validated" else ""
            hypothesis_lines.append(
                f"  - {h.statement[:100]} ({h.likelihood*100:.0f}%{status_tag})"
            )
        hypothesis_str = "\n".join(hypothesis_lines)
    else:
        hypothesis_str = "  None yet"

    # Evidence count + digest of diagnostic findings
    evidence_count = len(case.evidence)
    evidence_str = (
        f"{evidence_count} artifacts analyzed"
        if evidence_count > 0
        else "No evidence collected"
    )

    # Compact digest: retain key findings from diagnostic evidence so the agent
    # can still cite specifics after Tier A window eviction. Includes config/code
    # evidence which often contains root-cause clues.
    evidence_digests = []
    for ev in case.evidence:
        dt = ev.source_type.value.lower()
        if (
            "log" in dt
            or "metric" in dt
            or "trace" in dt
            or "error_report" in dt
            or "config" in dt
            or "code" in dt
        ) and ev.summary:
            evidence_digests.append(
                f"[{ev.source_type.value}] {ev.summary[:STATE_SUMMARY_DIGEST_CHARS]}"
            )
    evidence_digest = (
        "; ".join(evidence_digests[:STATE_SUMMARY_MAX_EVIDENCE_DIGESTS])
        if evidence_digests
        else ""
    )

    # Turn metrics
    turns_total = case.current_turn
    turns_since_progress = case.turns_without_progress

    summary = f"""<state_summary>
Investigation: {problem_desc}
Stage: {stage}
Verified: {verified}
Active Hypotheses:
{hypothesis_str}
Evidence: {evidence_str}
Turns: {turns_total} total, {turns_since_progress} since last progress
</state_summary>"""

    # Append evidence digest outside the compact summary block so it doesn't
    # inflate the base summary for cases with no diagnostic evidence
    if evidence_digest:
        summary += f"\n<evidence_digest>\n{evidence_digest}\n</evidence_digest>"

    return summary


_TIME_RANGE_PATTERNS: List[re.Pattern[str]] = [
    # "between 14:30 and 14:45" / "from 14:30 to 14:45"
    re.compile(
        r"\b(?:between|from)\s+"
        r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?)"
        r"\s+(?:and|to|-)\s+"
        r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?)\b",
        re.IGNORECASE,
    ),
    # "2026-04-23T14:00 to 2026-04-23T15:00" — ISO-8601 range
    re.compile(
        r"(?P<start>\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?)"
        r"\s+(?:and|to|-)\s+"
        r"(?P<end>\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?)"
    ),
]

# "at 14:30" / "around 14:30" — single-point queries. Matched separately
# so the window collapses to (ts, ts) rather than erroring.
_TIME_POINT_PATTERN = re.compile(
    r"\b(?:at|around|near)\s+"
    r"(?P<ts>\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?)\b",
    re.IGNORECASE,
)


def _parse_time_token(token: str, reference_date: datetime) -> Optional[datetime]:
    """Parse ``14:30`` / ``14:30:00`` (relative to *reference_date*) or a
    full ISO timestamp. Returns naive UTC-equivalent datetimes so the
    caller can compare uniformly against stored coverage timestamps
    (SQLite stores naive; Postgres TZ-aware — the rerank uses only
    equality-ish ordering, not subtraction, so mixing is tolerable).
    """
    # ISO-8601 with date component.
    if "-" in token:
        try:
            return datetime.fromisoformat(token.replace(" ", "T"))
        except ValueError:
            return None
    # HH:MM[:SS] — anchor to reference_date.
    parts = token.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        return None
    return datetime.combine(reference_date.date(), time(h, m, s))


def _extract_time_window_from_query(
    user_query: str, reference: Optional[datetime] = None
) -> Optional[tuple[datetime, datetime]]:
    """Parse a simple time range out of the user's turn text.

    Supports the common phrasings:

    - ``between 14:30 and 14:45`` / ``from 14:30 to 14:45``
    - ``at 14:30`` / ``around 14:30`` (collapses to a point)
    - ISO-8601 ranges and points

    Returns ``(start, end)`` datetimes when a range is recognised,
    ``(ts, ts)`` for a point query, or ``None`` when nothing matches.
    ``reference`` anchors bare HH:MM tokens to a date; defaults to
    ``datetime.now()`` when omitted.
    """
    if not user_query:
        return None
    ref = reference or datetime.now()

    for pattern in _TIME_RANGE_PATTERNS:
        match = pattern.search(user_query)
        if match:
            start = _parse_time_token(match.group("start"), ref)
            end = _parse_time_token(match.group("end"), ref)
            if start is not None and end is not None:
                return (start, end) if start <= end else (end, start)

    point_match = _TIME_POINT_PATTERN.search(user_query)
    if point_match:
        ts = _parse_time_token(point_match.group("ts"), ref)
        if ts is not None:
            return ts, ts

    return None


def _coverage_overlaps_window(ev, window: tuple[datetime, datetime]) -> bool:
    """Return True when the evidence's coverage span intersects the
    window. NULL coverage → False (timeless evidence isn't time-
    windowable, consistent with the repository query's semantics)."""
    start_ts = getattr(ev, "coverage_start_ts", None)
    end_ts = getattr(ev, "coverage_end_ts", None)
    if start_ts is None or end_ts is None:
        return False
    window_start, window_end = window
    # Naive / aware mismatch: drop tzinfo on the stored side for the
    # comparison. This sacrifices cross-timezone correctness but the
    # rerank is a ranking nudge, not a correctness-critical filter.
    if start_ts.tzinfo is not None:
        start_ts = start_ts.replace(tzinfo=None)
    if end_ts.tzinfo is not None:
        end_ts = end_ts.replace(tzinfo=None)
    return start_ts <= window_end and end_ts >= window_start


def _score_evidence_for_tier_a(
    ev, case, time_window: Optional[tuple[datetime, datetime]] = None
) -> float:
    """
    Score evidence for Tier A promotion. Higher score = more likely to get
    full structural index in the LLM context.

    Scoring weights:
    - Data type priority (+2 logs/metrics, +1 config/code, 0 text): diagnostic
      evidence should always beat READMEs and CITATIONs.
    - Hypothesis linkage (+3): evidence backing an active/validated hypothesis
      is the most valuable context the agent can have.
    - Has structural content (+1): evidence whose source file carries
      a rich structural_index (preprocessing output) benefits more from
      Tier A than items with minimal extraction.
    - Coverage match (+4): Phase 3c — evidence whose coverage_*_ts
      intersects a time window mentioned in the current user turn.
      The +4 weight intentionally exceeds the data-type bonus so a
      time-matched config can outrank a non-matching log; the rerank
      treats the time window as the strongest available signal when
      the user has explicitly mentioned one.
    - Recency (0.0-1.0): tiebreaker. Normalized against case.current_turn so
      it never outweighs type or hypothesis bonuses.
    """
    score = 0.0

    # Recency: 0.0 to 1.0, tiebreaker only
    current_turn = max(case.current_turn, 1)
    score += ev.collected_at_turn / current_turn

    # Data type priority: diagnostic evidence over text. Substring-match
    # over the source_type value tolerates both detailed
    # (logs_and_errors, metrics_and_performance) and unified (logs,
    # metrics) forms without enumerating each.
    dt = ev.source_type.value.lower()
    if "log" in dt or "metric" in dt or "trace" in dt or "error_report" in dt:
        score += 2
    elif "config" in dt or "code" in dt or "command" in dt or "profil" in dt:
        score += 1

    # Hypothesis linkage: evidence linked to active/validated hypotheses
    # with a supportive stance (supports/strongly_supports)
    for h in case.hypotheses.values():
        if h.state.value not in ("active", "validated"):
            continue
        link = next(
            (l for l in h.evidence_links if l.evidence_id == ev.evidence_id),
            None,
        )
        if link is not None and link.stance.value in ("supports", "strongly_supports"):
            score += 3
            break

    # Structural content richness: evidence whose backing file carries a
    # rich structural_index benefits more from Tier A. Post-010 the
    # structural_index lives on the source UploadedFile (not on ev.extract,
    # which is just an optional verbatim quote and is typically small).
    file_meta = case.find_uploaded_file(getattr(ev, "source_file_id", None))
    if (
        file_meta is not None
        and file_meta.structural_index
        and len(file_meta.structural_index) > 200
    ):
        score += 1

    # Phase 3c — time-window coverage match. Only fires when the
    # caller supplied a parsed window (flag must be on for that to
    # happen). Weight exceeds data-type bonus so the rerank
    # meaningfully surfaces the matching evidence.
    if time_window is not None and _coverage_overlaps_window(ev, time_window):
        score += 4

    # Pre-mitigation evidence up-weight. After a mitigation verifies
    # (``progress.mitigation.completed_at_turn`` is set), evidence
    # collected before the mitigation boundary is the RCA-relevant window
    # because telemetry collected post-mitigation typically shows a
    # stabilized system that no longer exhibits the root cause's signature.
    # +5 weight matches/exceeds the time-window bonus so pre-mitigation
    # diagnostic evidence outranks post-mitigation noise during RCA. Only
    # fires when ``mitigation.completed_at_turn`` is set and the current
    # turn is past that boundary — outside that window this is a no-op.
    mitigation = case.progress.mitigation if case.progress else None
    if (
        mitigation is not None
        and mitigation.completed_at_turn is not None
        and case.current_turn > mitigation.completed_at_turn
        and ev.collected_at_turn <= mitigation.completed_at_turn
    ):
        score += 5

    return score


# Pasted content gets an auto-generated timestamped filename at ingestion
# (see UploadedFile.upload_source). The filename itself carries no semantic
# signal, so when the LLM cites it ("see pasted-content-20260524T043237.txt")
# the reference is information-free. _displayable_filename synthesizes a
# label from data_type + summary head for these cases.
_PASTED_CONTENT_FILENAME = re.compile(r"^pasted-content-\d{8}T\d{6}Z?\.txt$")


def _displayable_filename(uf) -> str:
    """Return a human-readable filename label for an uploaded file.

    For pasted content with an auto-generated timestamped filename, synthesize
    a semantic label from the file's ``data_type`` and ``summary`` head (both
    populated by the Tier 0/1 preprocessor at ingestion). For real filenames,
    return them unchanged.
    """
    if uf is None or not uf.filename:
        return ""
    if not _PASTED_CONTENT_FILENAME.match(uf.filename):
        return uf.filename
    dtype = (uf.data_type or "data").replace("_", " ")
    summary = (uf.summary or "").strip()
    if summary:
        # First sentence (up to 60 chars) — keeps the label citation-friendly.
        snippet = summary.split(".")[0][:60].strip()
        return f"{dtype}: {snippet}"
    return f"{dtype} (pasted)"


def _build_hash_first_seen(case) -> Dict[str, int]:
    """Map each ``content_hash`` to the earliest turn it appeared on.

    Used to detect identical re-uploads — when the same byte-equal file is
    submitted across multiple turns, the second-and-later occurrences carry
    an ``identical_to_prior_upload_at_turn`` attribute pointing at the first
    occurrence. Returns an empty dict if the case has no uploaded files or
    no hashes are populated.
    """
    seen: Dict[str, int] = {}
    if not hasattr(case, "uploaded_files") or not case.uploaded_files:
        return seen
    for uf in case.uploaded_files:
        if not uf.content_hash or uf.uploaded_at_turn is None:
            continue
        existing = seen.get(uf.content_hash)
        if existing is None or uf.uploaded_at_turn < existing:
            seen[uf.content_hash] = uf.uploaded_at_turn
    return seen


def _identical_to_prior_attr(uf, hash_first_seen: Dict[str, int]) -> str:
    """XML attribute marking a re-uploaded byte-identical file.

    Returns ``' identical_to_prior_upload_at_turn="N"'`` when this file's
    ``content_hash`` was first seen on an earlier turn (N), empty otherwise.
    The first occurrence of any hash never carries the marker — only
    subsequent identical re-uploads do. Gives the LLM a precise signal
    that the user re-submitted the same content, useful for noticing
    e.g. "the same config has been submitted three times — the apply
    isn't taking effect".
    """
    if uf is None or not uf.content_hash or uf.uploaded_at_turn is None:
        return ""
    first = hash_first_seen.get(uf.content_hash)
    if first is None or first >= uf.uploaded_at_turn:
        return ""
    return f' identical_to_prior_upload_at_turn="{first}"'


def _fresh_this_turn_attr(item_turn: Optional[int], current_turn: int) -> str:
    """XML attribute marker for items collected/uploaded this turn.

    Returns ``' fresh_this_turn="true"'`` when the item's turn matches the
    current turn, empty string otherwise. The asymmetric encoding (attribute
    present only for fresh items) keeps the prior-context items visually
    quieter in long evidence blocks and gives the LLM a positional signal
    to distinguish data the user just provided from data being re-cited
    from history.
    """
    if item_turn is None:
        return ""
    if item_turn == current_turn:
        return ' fresh_this_turn="true"'
    return ""


def _symptom_currency_note(case, indicator: str) -> str:
    """Qualify the ``symptom_verified`` indicator with how current it is.

    A bare ``- symptom_verified`` states a conclusion while withholding
    everything needed to weigh it: what established the problem, when it was
    observed, and whether that still speaks to now. Read as settled fact, it
    sends the investigation looking for a cause of something that may have
    stopped. The flag is unchanged — this only stops it being reported as more
    than it is.

    Empty for every other indicator, and for cases where currency does not
    arise (see ``assess_symptom_currency``).
    """
    if indicator != "symptom_verified":
        return ""

    from faultmaven.core.investigation.symptom_currency import (
        SymptomCurrency,
        assess_symptom_currency,
        newest_symptom_observation,
    )

    currency = assess_symptom_currency(case)
    if currency == SymptomCurrency.NOT_APPLICABLE:
        return ""
    if currency == SymptomCurrency.UNDATED:
        return (
            " — the evidence behind this carries no observation time, so how "
            "recently the problem was seen is UNKNOWN (not confirmed recent)"
        )

    observed = newest_symptom_observation(case)
    stamp = observed.isoformat() if observed else "unknown"
    if currency == SymptomCurrency.CURRENT:
        return f" — symptom last observed {stamp}"
    return (
        f" — symptom last observed {stamp}, and this case records the problem "
        "as ONGOING. That the problem EXISTED is established; that it is "
        "STILL HAPPENING is not. Confirm it is still occurring before treating "
        "the cause as the open question, and say so plainly if it is not"
    )


def _observed_attr(ev) -> str:
    """XML attributes for WHEN the evidence's content was observed.

    Distinct from ``fresh_this_turn``, which is about when the AGENT saw the
    row — a two-hour-old alert pasted this turn is ``fresh_this_turn="true"``
    and two hours stale at the same time. Reading turn-recency as currency is
    exactly the confusion this attribute exists to break, so both are rendered
    and they answer different questions.

    ``age`` is precomputed rather than left as timestamp arithmetic for the
    model: the staleness judgement should not depend on it doing date math
    correctly under load. Emitted only when the coverage span is known —
    absence means unknown, never fresh, and the model must not read a missing
    attribute as an assurance.
    """
    end_ts = getattr(ev, "coverage_end_ts", None)
    if end_ts is None:
        return ""
    if end_ts.tzinfo is None:
        end_ts = end_ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - end_ts
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 0:
        # Future coverage: a clock skew or a mis-parsed year. Show the instant
        # and withhold the age rather than printing a negative one.
        return f' observed_through="{end_ts.isoformat()}"'
    if total_minutes < 60:
        age = f"{total_minutes}m"
    elif total_minutes < 60 * 24:
        age = f"{total_minutes // 60}h"
    else:
        age = f"{total_minutes // (60 * 24)}d"
    return f' observed_through="{end_ts.isoformat()}" age="{age}"'


def _evidence_label(ev, file_lookup: dict, case=None) -> str:
    """Build a short user-facing label for evidence.

    Used in the XML ``label`` attribute so the LLM can reference evidence
    by a human-readable name (e.g., "nginx-error.log") instead of the
    internal ``ev_`` ID.  The label is chosen from the best available
    source in priority order: filename → source_type → fallback.

    When ``case`` is provided and the evidence's source file is pasted
    content with a timestamped auto-filename, the label is synthesized
    from the file's ``data_type`` and ``summary`` (see
    ``_displayable_filename``).
    """
    # 1. Filename from uploaded files lookup (synthesized label for pasted
    #    content when case is available)
    if ev.source_file_id and str(ev.source_file_id) in file_lookup:
        if case is not None:
            uf = case.find_uploaded_file(ev.source_file_id)
            if uf is not None:
                return _displayable_filename(uf)
        return file_lookup[str(ev.source_file_id)]
    # 2. Source type as readable label (handles USER_DESCRIPTION for
    #    chat-extracted evidence and LOGS / METRICS / etc. for files
    #    whose filename wasn't in the lookup)
    if ev.source_type:
        return ev.source_type.value.replace("_", " ")
    return "uploaded data"


def _effective_evidence_char_budget(
    provider_name: Optional[str], model_name: Optional[str]
) -> int:
    """Effective char budget for the ``<evidence_collected>`` block.

    Model-aware when the active provider is known: a fraction
    (``evidence_budget_fraction``) of the provider's whole-prompt **token**
    budget (:func:`get_token_budget_for_provider`), converted to chars via the
    4-chars≈1-token approximation used by :class:`TokenBudget`. This lets the
    evidence cap scale with the model's context window (e.g. ~36K chars on a
    Gemini-class model) instead of one fixed constant.

    When ``provider_name`` is absent (tests, internal callers), falls back to
    the module-level :data:`EVIDENCE_CONTEXT_MAX_TOTAL_CHARS` — read live so
    tests that monkeypatch it still drive behavior. Floored at two per-item
    caps so the current-turn floor always has room for at least one full item.
    """
    if not provider_name:
        return EVIDENCE_CONTEXT_MAX_TOTAL_CHARS
    try:
        from faultmaven.config.settings import get_settings

        fraction = get_settings().investigation_context.evidence_budget_fraction
    except Exception:
        fraction = 0.6
    prompt_tokens = get_token_budget_for_provider(provider_name, model_name)
    model_aware = int(prompt_tokens * fraction * 4)
    return max(2 * EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM, model_aware)


def _current_turn_reserve_fraction() -> float:
    """Fraction of the evidence budget reserved for current-turn items."""
    try:
        from faultmaven.config.settings import get_settings

        return get_settings().investigation_context.current_turn_reserve_fraction
    except Exception:
        return 0.5


def _render_orphan_file_block(
    uf,
    hash_first_seen: dict,
    current_turn: int,
    summary_only: bool = False,
    elide_extract: bool = False,
) -> str:
    """Render one orphan ``UploadedFile`` as an ``<uploaded_file>`` block.

    Shared by the current-turn floor and the historical Tier-D fill so both
    paths produce byte-identical markup. The structural index is split into
    ``file_extract`` (per-item capped, with a search_file truncation pointer),
    ``search_map``, and ``file_meta``.

    ``summary_only=True`` emits just the opening/closing tag (id, filename,
    data_type, freshness, ``searchable``) without the ``file_extract`` body —
    the graceful-degradation render used when a current-turn file can't fit its
    full structural index within the reserve. The file stays present and
    addressable (the LLM can still ``search_file`` it by ``file_id``) instead of
    vanishing (INV-EC-1 / INV-EC-3).

    ``elide_extract=True`` (directed-analysis index+stub) drops only the
    ``file_extract`` body but KEEPS ``search_map`` + ``file_meta`` — the same
    render Evidence-backed Tier-A items get in that mode, so a not-yet-promoted
    orphan gets identical navigation hints (search_map), not a bare stub.
    """
    file_extract, search_map, file_meta = _parse_extract(uf.structural_index or "")
    file_id_attr = f' file_id="{uf.file_id}"' if uf.file_id else ""
    filename_attr = f' filename="{uf.filename}"' if uf.filename else ""
    data_type_attr = f' data_type="{uf.data_type}"' if uf.data_type else ""
    fresh_attr = _fresh_this_turn_attr(uf.uploaded_at_turn, current_turn)
    duplicate_attr = _identical_to_prior_attr(uf, hash_first_seen)
    entry = (
        f"  <uploaded_file{file_id_attr}{filename_attr}"
        f"{data_type_attr}{fresh_attr}{duplicate_attr}"
        f' searchable="true">\n'
    )
    if summary_only:
        # Degraded render: file present + addressable, full index omitted.
        entry += (
            "    <file_extract>[Full content omitted to fit budget; "
            "use search_file with the file_id above to read it.]</file_extract>\n"
        )
        entry += "  </uploaded_file>\n"
        return entry
    if elide_extract and file_extract.strip():
        # DA index+stub: drop the extract body, keep search_map (below). Same
        # marker Evidence-backed items get, so navigation parity holds.
        entry += (
            '    <file_extract role="orientation" elided="directed_analysis">\n'
            "[Structural index elided in directed-analysis mode — call "
            "search_file with the file_id above to read specifics from the "
            "raw file.]\n"
            "    </file_extract>\n"
        )
    elif file_extract.strip():
        truncation_note = ""
        if len(file_extract) > EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM:
            remaining_chars = len(file_extract) - EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM
            file_extract = file_extract[:EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM]
            truncation_note = (
                f"\n[TRUNCATED: {remaining_chars:,} more characters not shown. "
                "Use search_file with the file_id above for specific lookups.]"
            )
        entry += "    <file_extract>\n"
        if uf.filename:
            entry += f"[Source: {uf.filename}]\n"
        entry += file_extract
        entry += truncation_note
        entry += "\n    </file_extract>\n"
    if search_map and search_map.strip():
        entry += f"    <search_map>\n{search_map}\n    </search_map>\n"
    if file_meta:
        meta_lines = _format_file_meta(file_meta)
        entry += f"    <file_meta>{meta_lines}</file_meta>\n"
    entry += "  </uploaded_file>\n"
    return entry


def _build_evidence_context(
    case: Case,
    processing_mode: Optional[str] = None,
    user_query: str = "",
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    char_budget_override: Optional[int] = None,
    tools_available: bool = False,
) -> str:
    """
    Build the evidence context section using a three-tier sliding window.

    This replaces the simple last-10 evidence list with a tiered system that
    includes structural indexes for recent data evidence, fixing the
    "I don't have access to file content" bug.

    Tier A: Top N file-backed evidence items (``source_file_id IS NOT NULL``)
            by relevance score → include the file's structural_index from
            uploaded_files, capped per item. Scored by data type, hypothesis
            linkage, content richness, and recency.
    Tier B: Remaining file-backed evidence → summary only.
    Tier C: Chat-extracted evidence (``source_file_id IS NULL``,
            ``source_type=USER_DESCRIPTION``) → summary only, always.

    Token budget: ~4000 tokens dedicated. Worst case: 3 Tier A items x 4000
    chars = 12,000 chars (~3000 tokens).
    """
    # Build the hash → first-seen-turn map once for the whole render. Used
    # to surface ``identical_to_prior_upload_at_turn`` on re-uploaded
    # byte-identical files. Cheap (single pass over uploaded_files) and
    # shared across the INQUIRY fallback, Tier A/B file-backed evidence,
    # and Tier D orphan rendering below.
    hash_first_seen = _build_hash_first_seen(case)

    if not case.evidence:
        # Post-010 strict evidence model: during INQUIRY, files are stored on
        # uploaded_files (not promoted to Evidence until INVESTIGATING). Surface
        # structural_index here so the INQUIRY template's <file_extract> reference
        # resolves and the agent can characterize the file on the first turn.
        if hasattr(case, "uploaded_files") and case.uploaded_files:
            files_with_content = [
                uf
                for uf in case.uploaded_files
                if structural_index_is_searchable(uf.structural_index)
            ]
            if files_with_content:
                result = "<evidence_collected>\n"
                for uf in files_with_content:
                    file_extract, search_map, file_meta = _parse_extract(
                        uf.structural_index or ""
                    )
                    # Emit file_id under the same attribute name used on
                    # <evidence file_id="..."> so the source_file_id rule is
                    # phase-uniform. The LLM passes this value into
                    # search_file's evidence_id parameter (the tool accepts
                    # either an ev_xxx or a file_xxx during INQUIRY).
                    file_id_attr = f' file_id="{uf.file_id}"' if uf.file_id else ""
                    filename_attr = f' filename="{uf.filename}"' if uf.filename else ""
                    data_type_attr = (
                        f' data_type="{uf.data_type}"' if uf.data_type else ""
                    )
                    fresh_attr = _fresh_this_turn_attr(
                        uf.uploaded_at_turn, case.current_turn
                    )
                    duplicate_attr = _identical_to_prior_attr(uf, hash_first_seen)
                    result += (
                        f"  <uploaded_file{file_id_attr}{filename_attr}"
                        f"{data_type_attr}{fresh_attr}{duplicate_attr}"
                        f' searchable="true">\n'
                    )
                    if file_extract.strip():
                        truncation_note = ""
                        if len(file_extract) > EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM:
                            remaining_chars = (
                                len(file_extract) - EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM
                            )
                            file_extract = file_extract[
                                :EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM
                            ]
                            truncation_note = (
                                f"\n[TRUNCATED: {remaining_chars:,} more characters not shown. "
                                "Use search_file with the file_id above for specific lookups.]"
                            )
                        result += "    <file_extract>\n"
                        if uf.filename:
                            result += f"[Source: {uf.filename}]\n"
                        result += file_extract
                        result += truncation_note
                        result += "\n    </file_extract>\n"
                    if search_map and search_map.strip():
                        result += f"    <search_map>\n{search_map}\n    </search_map>\n"
                    if file_meta:
                        meta_lines = _format_file_meta(file_meta)
                        result += f"    <file_meta>{meta_lines}</file_meta>\n"
                    result += "  </uploaded_file>\n"
                result += "</evidence_collected>"
                return result
        return (
            "<evidence_collected>\n"
            "No formal evidence collected yet.\n"
            "</evidence_collected>"
        )

    # Build filename lookup from uploaded files
    file_lookup = {}
    if hasattr(case, "uploaded_files") and case.uploaded_files:
        for uf in case.uploaded_files:
            if uf.file_id and uf.filename:
                file_lookup[str(uf.file_id)] = uf.filename

    # Separate evidence by source for tiered treatment. Post-010: the
    # ``form`` column was dropped; source_file_id IS NOT NULL is the
    # marker for file-backed evidence. The strict source-invariant
    # CHECK ensures chat-extracted rows have source_type=USER_DESCRIPTION.
    data_evidence = []  # source_file_id IS NOT NULL
    text_evidence = []  # source_file_id IS NULL (chat-extracted)
    for ev in case.evidence:
        if ev.source_file_id is not None:
            data_evidence.append(ev)
        else:
            text_evidence.append(ev)

    # Phase 3c — extract a time window from the user's turn when the
    # feature flag is on. The parsed window feeds into the Tier A
    # scoring so evidence whose coverage intersects can outrank items
    # that would otherwise score higher on data-type or recency.
    time_window = None
    try:
        from faultmaven.config.settings import get_settings

        if get_settings().preprocessing.timeline_rerank_enabled:
            time_window = _extract_time_window_from_query(user_query)
    except Exception:
        # Settings path missing or unparseable — skip the rerank. The
        # base ranking is still valid.
        time_window = None

    # Select Tier A by relevance score (not FIFO). Logs/metrics with hypothesis
    # linkage beat READMEs/CITATIONs regardless of upload order.
    scored = sorted(
        data_evidence,
        key=lambda ev: _score_evidence_for_tier_a(ev, case, time_window=time_window),
        reverse=True,
    )
    current_turn = getattr(case, "current_turn", 0) or 0
    tier_a_set = set(id(ev) for ev in scored[:EVIDENCE_CONTEXT_RECENT_COUNT])
    # Current-turn floor (INV-EC-1): evidence created THIS turn is the highest-
    # signal context for the turn's task. Force it into Tier A regardless of the
    # recent_count cap or relevance score so it always gets a full render.
    if current_turn > 0:
        for ev in data_evidence:
            if ev.collected_at_turn == current_turn:
                tier_a_set.add(id(ev))

    # Order Tier A current-turn-first so the budget downgrade below only ever
    # hits older evidence, never the file the user just provided.
    def _current_turn_first(ev) -> int:
        return 0 if (current_turn > 0 and ev.collected_at_turn == current_turn) else 1

    tier_a = sorted(
        (ev for ev in data_evidence if id(ev) in tier_a_set), key=_current_turn_first
    )
    tier_b = [ev for ev in data_evidence if id(ev) not in tier_a_set]

    # Model-aware budget; falls back to the module-level char cap when the
    # provider is unknown (read live so test monkeypatching still drives it).
    # Under the allocator, the caller passes the evidence section's actual
    # allotment (char_budget_override) so the block sizes itself to what it will
    # be granted — not the full model budget — avoiding the double-budget where
    # evidence self-sizes large and is then re-truncated, and so the current-turn
    # floor below is computed against the real allotment (INV-1).
    effective_total_chars = (
        char_budget_override
        if char_budget_override is not None
        else _effective_evidence_char_budget(provider_name, model_name)
    )
    current_turn_floor_chars = max(
        EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM,
        int(effective_total_chars * _current_turn_reserve_fraction()),
    )

    # Directed-analysis index+stub: in DA turns, historical evidence carries only
    # its addressable stub + search_map, not the large <file_extract> body — the
    # agent fetches specifics via search_file. Validated (A/B + eval, no conclusion
    # regression), so it is the standing behavior rather than a flag. Gated on TWO
    # conditions, both required:
    #   1. this is a directed_analysis turn, AND
    #   2. tools_available — search_file will ACTUALLY run this turn. Without (2)
    #      a tool-less / tool-incapable turn would be stranded with a stub that
    #      points at a tool it cannot call (NO INCORRECT CONCLUSION). "directed_
    #      analysis" is the classifier's ambiguous default and does NOT by itself
    #      imply tool calling works, so tool-availability must be checked here.
    # The current-turn upload always keeps its full extract (freshness / INV-EC-1).
    da_index_only = processing_mode == "directed_analysis" and tools_available

    result = "<evidence_collected>\n"
    total_chars = 0
    # INV-4: count evidence items skipped for budget (Tiers B/C/D) so their
    # omission is never silent — a marker is emitted before the closing tag.
    n_omitted = 0

    # File ids already backed by an Evidence row — computed once and reused by
    # both the current-turn floor and the historical Tier-D fill (they used to
    # recompute this identical set independently).
    referenced_file_ids = {
        str(ev.source_file_id) for ev in case.evidence if ev.source_file_id is not None
    }

    # === Current-turn orphan-file floor (INV-EC-1) ===
    # Files uploaded THIS turn with no Evidence row yet are the exact blind spot
    # that made the agent read a stale file: they used to land in the historical
    # Tier-D fill, after older evidence had consumed the budget, and get dropped.
    # Render them FIRST from a reserved slice. Every current-turn orphan is
    # ALWAYS rendered and ALWAYS marked handled (so Tier D neither re-renders nor
    # drops it): in full while the reserve has room (the first one is guaranteed
    # full even if it alone exceeds the reserve), otherwise as a summary stub
    # that keeps the file present and search_file-addressable (INV-EC-1/EC-3).
    handled_file_ids: set[str] = set()
    if current_turn > 0 and getattr(case, "uploaded_files", None):
        current_turn_orphans = [
            uf
            for uf in case.uploaded_files
            if uf.file_id is not None
            and uf.uploaded_at_turn == current_turn
            and str(uf.file_id) not in referenced_file_ids
            and structural_index_is_searchable(uf.structural_index)
        ]
        for uf in current_turn_orphans:
            full_entry = _render_orphan_file_block(uf, hash_first_seen, current_turn)
            # First item renders full unconditionally; later items render full
            # only while within the reserve, else degrade to a summary stub.
            # Never dropped — current-turn uploads are always present.
            if total_chars == 0 or (
                total_chars + len(full_entry) <= current_turn_floor_chars
            ):
                result += full_entry
                total_chars += len(full_entry)
            else:
                summary_entry = _render_orphan_file_block(
                    uf, hash_first_seen, current_turn, summary_only=True
                )
                result += summary_entry
                total_chars += len(summary_entry)
            handled_file_ids.add(str(uf.file_id))

    # Tier A: Recent data evidence with structural index
    for ev in tier_a:
        # Post-010: the structural index (file_extract + search_map +
        # file_meta JSON) lives on uploaded_files.structural_index, not
        # on ev.extract. The Evidence row carries an optional verbatim
        # quote in ev.extract instead. We render the structural index
        # first (orientation content) and append the quote (if any) as
        # the LLM's claim-supporting snippet.
        ev_file_meta = case.find_uploaded_file(ev.source_file_id)
        structural_index_raw = (
            ev_file_meta.structural_index if ev_file_meta is not None else ""
        ) or ""
        file_extract, search_map, file_meta = _parse_extract(structural_index_raw)

        # Rerank page capture sections by query relevance before truncation
        # so the most pertinent panels/messages survive the per-item char cap.
        is_page_capture = (
            ev_file_meta is not None and ev_file_meta.upload_source == "page_capture"
        )
        if user_query and is_page_capture:
            file_extract = _rerank_page_capture_sections(file_extract, user_query)

        truncated = False

        is_current_turn_ev = current_turn > 0 and ev.collected_at_turn == current_turn
        # In DA index-only mode, HISTORICAL evidence drops its file_extract body
        # (stub + search_map only). The current-turn upload keeps its extract.
        suppress_extract = da_index_only and not is_current_turn_ev

        # Per-item cap applies to file_extract (the orientation content)
        if (
            not suppress_extract
            and len(file_extract) > EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM
        ):
            remaining_chars = len(file_extract) - EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM
            file_extract = file_extract[:EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM]
            truncated = True

        # Total budget cap. Current-turn evidence is prioritized but BOUNDED:
        # it skips the downgrade only while the current-turn reserve still has
        # room (so a fresh item always wins a full render), not unconditionally —
        # otherwise N current-turn evidence rows could each render in full with
        # no cap and blow the whole evidence budget. Once the reserve is spent,
        # current-turn evidence degrades to a Tier-B summary like everything else.
        # A suppressed extract contributes no extract bytes to the estimate.
        entry_estimate = (
            (0 if suppress_extract else len(file_extract))
            + len(ev.summary or "")
            + len(ev.extract or "")
            + 200
        )  # overhead for XML tags
        within_reserve = total_chars < current_turn_floor_chars
        exempt_from_downgrade = is_current_turn_ev and within_reserve
        if (
            not exempt_from_downgrade
            and total_chars + entry_estimate > effective_total_chars
        ):
            # Downgrade remaining Tier A to Tier B (summary only)
            tier_b.append(ev)
            continue

        data_type_attr = (
            f' data_type="{ev.source_type.value}"' if ev.source_type else ""
        )
        label = _evidence_label(ev, file_lookup, case)
        label_attr = f' label="{label}"'
        filename_attr = ""
        file_id_attr = ""
        if ev.source_file_id and str(ev.source_file_id) in file_lookup:
            filename_attr = f' filename="{file_lookup[str(ev.source_file_id)]}"'
            file_id_attr = f' file_id="{ev.source_file_id}"'
        # Post-010: file-backed evidence has source_file_id set and a
        # raw file behind it. ``searchable`` advertises that the search/
        # deep_analysis tools can operate on this row's source file.
        is_searchable = ev.source_file_id is not None and ev_file_meta is not None
        searchable_attr = ' searchable="true"' if is_searchable else ""
        confidence_attr, confidence_advisory = _confidence_marker(ev)
        fresh_attr = _fresh_this_turn_attr(ev.collected_at_turn, case.current_turn)
        observed_attr = _observed_attr(ev)
        duplicate_attr = _identical_to_prior_attr(ev_file_meta, hash_first_seen)
        result += f'  <evidence id="{ev.evidence_id}"{label_attr}{file_id_attr}{data_type_attr}{filename_attr}{searchable_attr}{confidence_attr}{fresh_attr}{observed_attr}{duplicate_attr}>\n'
        result += f"    <summary>{ev.summary}</summary>\n"
        if file_extract.strip() and suppress_extract:
            # DA index-only: elide the extract body, keep the file addressable.
            # The <search_map> below and the evidence id/file_id on the tag give
            # the agent everything it needs to search_file for specifics (INV-4:
            # the elision is marked, never silent).
            result += (
                '    <file_extract role="orientation" elided="directed_analysis">\n'
                "[Structural index elided in directed-analysis mode — call "
                "search_file with the evidence id above to read specifics from "
                "the raw file.]\n"
                "    </file_extract>\n"
            )
        elif file_extract.strip():
            role_attr = (
                ' role="orientation"' if processing_mode == "directed_analysis" else ""
            )
            result += f"    <file_extract{role_attr}>\n"
            # Content-level source attribution: reinforces the XML attribute
            # so the LLM sees which file this content belongs to while reading
            # through multi-evidence blocks, not just in the enclosing tag.
            if ev.source_file_id and str(ev.source_file_id) in file_lookup:
                result += f"[Source: {file_lookup[str(ev.source_file_id)]}]\n"
            if confidence_advisory:
                result += f"{confidence_advisory}\n"
            result += file_extract
            if truncated:
                result += f"\n[TRUNCATED: {remaining_chars:,} more characters not shown. Use search_file with the evidence id above to search for specific content in the raw file.]"
            result += "\n    </file_extract>\n"
        # Post-010: surface the agent's verbatim quote (when present) as a
        # distinct claim-supporting snippet, separate from the file's
        # structural index above.
        if ev.extract and ev.extract.strip():
            result += f"    <verbatim_quote>{ev.extract.strip()}</verbatim_quote>\n"
        if search_map and search_map.strip():
            result += f"    <search_map>\n{search_map}\n    </search_map>\n"
        if file_meta:
            meta_lines = _format_file_meta(file_meta)
            result += f"    <file_meta>{meta_lines}</file_meta>\n"
        result += "  </evidence>\n"
        total_chars += entry_estimate

    # Tier B: Older data evidence (summary only)
    for ev in tier_b:
        label = _evidence_label(ev, file_lookup, case)
        label_attr = f' label="{label}"'
        filename_attr = ""
        file_id_attr = ""
        if ev.source_file_id and str(ev.source_file_id) in file_lookup:
            filename_attr = f' filename="{file_lookup[str(ev.source_file_id)]}"'
            file_id_attr = f' file_id="{ev.source_file_id}"'
        ev_file_meta = case.find_uploaded_file(ev.source_file_id)
        is_searchable = ev.source_file_id is not None and ev_file_meta is not None
        searchable_attr = ' searchable="true"' if is_searchable else ""
        confidence_attr, _ = _confidence_marker(ev)
        fresh_attr = _fresh_this_turn_attr(ev.collected_at_turn, case.current_turn)
        observed_attr = _observed_attr(ev)
        duplicate_attr = _identical_to_prior_attr(ev_file_meta, hash_first_seen)
        entry = f'  <evidence id="{ev.evidence_id}"{label_attr}{file_id_attr}{filename_attr}{searchable_attr}{confidence_attr}{fresh_attr}{observed_attr}{duplicate_attr}>'
        entry += f"<summary>{ev.summary}</summary></evidence>\n"
        # Skip (not break) over-budget summaries so a single large item never
        # drops every lower-ranked item behind it (INV-EC-2).
        if total_chars + len(entry) > effective_total_chars:
            n_omitted += 1
            continue
        result += entry
        total_chars += len(entry)

    # Tier C: chat-extracted evidence (never searchable — has no source
    # file). source_file_id IS NULL here per the new source-invariant.
    # Include the verbatim_quote when present: for chat-extracted
    # evidence it carries the actual system-output slice the user typed
    # in (the summary alone would lose that detail).
    # INV-4: the 5-most-recent cap drops OLDER chat evidence — count it so the
    # <evidence_omitted> marker reflects the omission (these have no
    # source_file_id, so the marker signals it, since search_file can't reach
    # them).
    n_omitted += max(0, len(text_evidence) - 5)
    for ev in text_evidence[-5:]:  # Cap at 5 most recent items
        label = _evidence_label(ev, file_lookup, case)
        label_attr = f' label="{label}"'
        fresh_attr = _fresh_this_turn_attr(ev.collected_at_turn, case.current_turn)
        observed_attr = _observed_attr(ev)
        quote_block = ""
        if ev.extract and ev.extract.strip():
            quote_block = f"<verbatim_quote>{ev.extract.strip()}</verbatim_quote>"
        entry = (
            f'  <evidence id="{ev.evidence_id}"{label_attr}{fresh_attr}{observed_attr}>'
            f"<summary>{ev.summary}</summary>{quote_block}</evidence>\n"
        )
        if total_chars + len(entry) > effective_total_chars:
            n_omitted += 1
            continue
        result += entry
        total_chars += len(entry)

    # Tier D — pending uploads not yet promoted to Evidence.
    #
    # Without this, files uploaded after the first Evidence row exists
    # become invisible to the LLM in the loops above (which enumerate
    # only Evidence rows). The LLM then can't emit ``evidence_to_add``
    # for the new file because it has no content to react to — the
    # chicken-and-egg that surfaces as "I don't have direct access to
    # the file contents". Same rendering as the INQUIRY-phase fallback
    # at the top of this function; the INQUIRY path was already correct
    # for the empty-Evidence case, this section generalizes it to the
    # non-empty case.
    #
    # Current-turn orphans are already rendered in the floor above (and tracked
    # in handled_file_ids); this section renders the remaining (historical)
    # orphans on the budget that survives the floor + Tiers A–C. Uses the
    # referenced_file_ids set computed once above.
    if hasattr(case, "uploaded_files") and case.uploaded_files:
        # Iterate newest-first so newer orphans are attempted before older ones.
        orphan_files = sorted(
            (
                uf
                for uf in case.uploaded_files
                if uf.file_id is not None
                and str(uf.file_id) not in referenced_file_ids
                and str(uf.file_id) not in handled_file_ids
                and structural_index_is_searchable(uf.structural_index)
            ),
            key=lambda uf: (uf.uploaded_at_turn or 0, str(uf.file_id)),
            reverse=True,
        )
        for uf in orphan_files:
            # DA index-only elides HISTORICAL evidence extracts (these orphans are
            # all historical — current-turn ones went through the floor above), so
            # a not-yet-promoted file is treated the same as an Evidence-backed one
            # (stub only, addressable via search_file) instead of dumped in full.
            entry = _render_orphan_file_block(
                uf, hash_first_seen, current_turn, elide_extract=da_index_only
            )
            # Greedy newest-first fill with skip-not-break (INV-EC-2): one large
            # orphan never drops every smaller orphan behind it. Note this is a
            # greedy fit, not a strict newest-wins policy — a large newer orphan
            # may be skipped while a smaller older one fits. Current-turn files
            # are never affected (handled by the floor above).
            if total_chars + len(entry) > effective_total_chars:
                n_omitted += 1
                continue
            result += entry
            total_chars += len(entry)

    # INV-4: never drop evidence silently. If any item was skipped for budget,
    # say so — the agent can then ask for it or search_file rather than assume
    # the shown set is exhaustive.
    if n_omitted:
        result += (
            f'  <evidence_omitted count="{n_omitted}" '
            f'reason="prompt_budget" note="More evidence exists but did not fit '
            f'this turn; use search_file / list_evidence to reach it." />\n'
        )

    result += "</evidence_collected>"
    return result


def _build_turn_summary(turn) -> str:
    """Build a compact summary from a TurnProgress record.

    Format: TURN {n}: {user_summary} → {structural_metadata} | Agent: {response_summary}

    Includes both structural metadata (milestones, evidence counts) AND the
    agent_response_summary so the LLM knows WHAT was analyzed, not just counts.
    """
    parts = []

    # Milestones completed this turn
    if turn.milestones_completed:
        parts.append(", ".join(turn.milestones_completed))

    # Evidence and hypothesis counts
    if turn.evidence_added:
        parts.append(f"{len(turn.evidence_added)} evidence added")
    if turn.hypotheses_generated:
        parts.append(f"{len(turn.hypotheses_generated)} hypotheses proposed")
    if turn.hypotheses_validated:
        parts.append(f"{len(turn.hypotheses_validated)} hypotheses validated")
    if turn.solutions_proposed:
        parts.append(f"{len(turn.solutions_proposed)} solutions proposed")

    outcome_desc = ", ".join(parts) if parts else ""

    # Always include agent_response_summary when available — this tells the
    # LLM what it actually analyzed/concluded, not just structural counts.
    # Without this, summarized turns lose critical detail like "analyzed
    # nova-api logs, found VM lifecycle events" → just "1 evidence added".
    agent_part = ""
    if turn.agent_response_summary:
        agent_part = f" | Agent: {turn.agent_response_summary[:200]}"
    elif not outcome_desc:
        # No structural metadata AND no agent summary — use outcome as fallback
        if turn.outcome:
            outcome_desc = str(turn.outcome.value)
        else:
            outcome_desc = "conversation"

    user_part = turn.user_message_summary or "User message"
    if outcome_desc:
        return f"TURN {turn.turn_number}: {user_part} → {outcome_desc}{agent_part}"
    return f"TURN {turn.turn_number}: {user_part}{agent_part}"


def _build_graduated_history(case: Case) -> str:
    """Build graduated conversation history: recent turns verbatim, older summarized.

    Recent turns (last HISTORY_VERBATIM_TURNS): full user messages + smart-truncated
    agent responses. Older turns: one-line summaries from TurnProgress metadata.

    Falls back to verbatim-only if turn_history is unavailable.
    """
    messages = case.messages or []
    turn_records = case.turn_history or []

    if not messages:
        return (
            "<conversation_history>\nNo previous conversation.\n</conversation_history>"
        )

    # Determine the turn number boundary between "earlier" and "recent"
    # Get all unique turn numbers from messages, sorted
    all_turn_nums = sorted(
        {m.get("turn_number", 0) for m in messages if m.get("turn_number")}
    )

    if len(all_turn_nums) <= HISTORY_VERBATIM_TURNS:
        # Few enough turns — all verbatim, no summarization needed
        return _build_verbatim_history(messages)

    # Split: recent turns get verbatim, older turns get summarized
    recent_turn_nums = set(all_turn_nums[-HISTORY_VERBATIM_TURNS:])
    earlier_turn_nums = all_turn_nums[:-HISTORY_VERBATIM_TURNS]

    result = "<conversation_history>\n"

    # --- EARLIER TURNS (summarized from TurnProgress) ---
    if earlier_turn_nums and turn_records:
        # Index turn_records by turn_number for quick lookup
        turn_index = {t.turn_number: t for t in turn_records}
        summary_turns = earlier_turn_nums[-HISTORY_SUMMARY_MAX_TURNS:]

        result += "EARLIER TURNS:\n"
        for turn_num in summary_turns:
            if turn_num in turn_index:
                result += _build_turn_summary(turn_index[turn_num]) + "\n"
            else:
                # Turn record missing — minimal fallback from messages
                user_msgs = [
                    m
                    for m in messages
                    if m.get("turn_number") == turn_num and m.get("role") == "user"
                ]
                user_preview = (
                    user_msgs[0].get("content", "")[:100] if user_msgs else "..."
                )
                result += f"TURN {turn_num}: {user_preview}\n"
        result += "\n"

    elif earlier_turn_nums:
        # No turn_records available — summarize from messages directly
        result += "EARLIER TURNS:\n"
        summary_turns = earlier_turn_nums[-HISTORY_SUMMARY_MAX_TURNS:]
        for turn_num in summary_turns:
            user_msgs = [
                m
                for m in messages
                if m.get("turn_number") == turn_num and m.get("role") == "user"
            ]
            user_preview = user_msgs[0].get("content", "")[:150] if user_msgs else "..."
            result += f"TURN {turn_num}: {user_preview}\n"
        result += "\n"

    # --- RECENT TURNS (verbatim with smart agent truncation) ---
    result += "RECENT TURNS:\n"
    current_turn_num = None
    for msg in messages:
        turn_num = msg.get("turn_number")
        if turn_num not in recent_turn_nums:
            continue

        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if not content:
            continue

        if turn_num != current_turn_num:
            if current_turn_num is not None:
                result += "\n"
            result += f"TURN {turn_num}:\n"
            current_turn_num = turn_num

        if role == "ASSISTANT":
            content = _smart_truncate_agent_response(content)

        result += f"{role}: {content}\n"

    result += "</conversation_history>"
    return result


def _build_verbatim_history(messages: list) -> str:
    """Build full verbatim history for short conversations (≤3 turns)."""
    result = "<conversation_history>\n"
    current_turn_num = None

    for msg in messages[-20:]:
        turn_num = msg.get("turn_number", "?")
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if not content:
            continue

        if turn_num != current_turn_num:
            if current_turn_num is not None:
                result += "\n"
            result += f"TURN {turn_num}:\n"
            current_turn_num = turn_num

        result += f"{role}: {content}\n"

    result += "</conversation_history>"
    return result


# =============================================================================
# Evidence-needs block (Phase 4 of evidence-needs rollout)
# =============================================================================

# Cap on rendered needs per section to keep token cost bounded. Each
# rendered need is ~80 chars of header (capped via
# ``_REQUEST_TEXT_RENDER_CAP``) + ~80 chars of motivator line.
# Single-section case (DIAGNOSIS, or MITIGATION/TREATMENT with one of
# outstanding/re-verification empty): ~600 tokens worst case at 15
# needs. Both-sections case (MITIGATION/TREATMENT with both populated):
# up to 30 needs total, ~1200 tokens worst case. Typical cases stay
# well under either bound because the LLM emits short request_text.
_EVIDENCE_NEEDS_RENDER_CAP = 15

# Per-need truncation cap for ``request_text``. The model attribute is
# capped at 500 chars by the schema, but in the rendered block we keep
# things scannable. The full text is preserved in the DB and surfaces
# via the EVIDENCE-suggestion side (Phase 6).
_REQUEST_TEXT_RENDER_CAP = 120

# Priority sort key — HIGH first so the LLM sees the urgent demand
# without scrolling. Keyed by enum member rather than ``.value`` so a
# future ``NeedPriority`` addition raises ``KeyError`` here instead of
# silently sinking the new bucket to the bottom of the list.
_PRIORITY_ORDER: dict[NeedPriority, int] = {
    NeedPriority.HIGH: 0,
    NeedPriority.MEDIUM: 1,
    NeedPriority.LOW: 2,
}


def _truncate_request_text(text: str) -> str:
    """Truncate ``request_text`` for rendering only — full text stays in
    the DB. Adds a single-char ellipsis when truncation actually
    occurs so the LLM knows the surfaced line is partial."""
    if len(text) <= _REQUEST_TEXT_RENDER_CAP:
        return text
    return text[: _REQUEST_TEXT_RENDER_CAP - 1].rstrip() + "…"


def _render_need_line(need) -> tuple[str, str]:
    """Render one need into (header_line, motivator_line)."""
    purpose_label = (
        "SYMPTOM" if need.purpose == NeedPurpose.SYMPTOM_VERIFICATION else "CAUSAL"
    )
    status_suffix = (
        f", {need.state.value.upper()}" if need.state != NeedState.PENDING else ""
    )
    header = (
        f"  - [{need.need_id}] {_truncate_request_text(need.request_text)} "
        f"({purpose_label}, {need.priority.value.upper()}{status_suffix})"
    )
    if need.purpose == NeedPurpose.SYMPTOM_VERIFICATION:
        motivator_line = "      motivated_by: problem_statement"
    else:
        ids = need.motivating_hypothesis_ids
        motivator_line = (
            f"      motivated_by: [{', '.join(ids)}]"
            if ids
            else "      motivated_by: []"
        )
    return header, motivator_line


def _render_finding_line(ev) -> str:
    """Render one confirmed presence-evidence row as a re-check line.

    The re-verification checklist (MITIGATION/TREATMENT) is anchored on
    the confirmed ``symptom_evidence`` / ``causal_evidence`` rows — the
    canonical record of what was established — NOT on FULFILLED needs.
    Needs are demand-side and gap-conditional (created only when the
    verifying data wasn't already in hand), so a need-anchored checklist
    silently omits every symptom/cause that was confirmed from
    already-available data. Evidence rows exist for every confirmed
    finding, so this checklist is complete.
    """
    label = "SYMPTOM" if ev.category == EvidenceCategory.SYMPTOM_EVIDENCE else "CAUSE"
    summary = ev.summary or ev.extract or "(no summary)"
    return (
        f"  - [{ev.evidence_id}] {_truncate_request_text(summary)} "
        f"({label}, established turn {ev.collected_at_turn})"
    )


def _build_evidence_needs_block(case: Case) -> str:
    """Render the ``<evidence_needs>`` context block.

    Returns ``""`` (progressive activation, design §10.6) when:

    - The pool is empty.
    - All needs are filtered out by status (no PENDING/PARTIALLY_MET
      in DIAGNOSIS; no PENDING/PARTIALLY_MET/FULFILLED in
      MITIGATION/TREATMENT).
    - The case is not INVESTIGATING (terminal/inquiry have their own
      surfaces).

    Filtering rules (design §8.4):

    - Default (DIAGNOSIS stage): render PENDING + PARTIALLY_MET needs as
      "outstanding needs" — data to look for during upload review.
      FULFILLED and SUPERSEDED are excluded to save tokens.
    - MITIGATION / TREATMENT: render two clearly-labelled sections —
      outstanding needs as above, plus a "re-verification checklist"
      built from the confirmed presence-evidence rows
      (``symptom_evidence`` / ``causal_evidence``) so the agent can
      confirm the fix held by re-checking the data that established each
      symptom/cause. The checklist is anchored on evidence rows, NOT on
      FULFILLED needs: needs are gap-conditional (created only when the
      verifying data wasn't already in hand), so a need-anchored
      checklist omits every finding confirmed from already-available
      data — the common case. Either section is omitted if empty.

    Output shape (DIAGNOSIS):

        <evidence_needs>
        Unexpected findings outside the entries below are equally
        important and may lead to new hypotheses or revised needs.

        Outstanding needs (data to look for in uploads):

          - [eneed_001] Response time metrics (SYMPTOM, HIGH)
              motivated_by: problem_statement
          - [eneed_003] DB connection pool metrics (CAUSAL, HIGH)
              motivated_by: [hyp_001, hyp_003]
        </evidence_needs>

    Output shape (MITIGATION/TREATMENT, both sections populated):

        <evidence_needs>
        Unexpected findings outside the entries below are equally
        important and may lead to new hypotheses or revised needs.

        Outstanding needs (data to look for in uploads):

          - [eneed_004] App connection timeout logs (CAUSAL, MEDIUM)
              motivated_by: [hyp_001]

        Re-verification checklist (confirmed findings — re-check each to
        confirm the fix held; emit *_absence_evidence when a signature is gone):

          - [ev_abc123] API p99 latency 8.9s during incident (SYMPTOM, established turn 5)
          - [ev_def456] audit_events Seq Scan, no index on created_at (CAUSE, established turn 8)
        </evidence_needs>
    """
    if case.state != CaseState.INVESTIGATING:
        return ""
    # NOTE: do NOT early-return on an empty `evidence_needs` pool. The
    # MITIGATION/TREATMENT re-verification checklist is sourced from
    # presence-evidence rows, which exist even when no need was ever
    # created (the common gap-free case). The `not outstanding and not
    # re_verification` check below is the correct emptiness gate.

    in_post_diagnosis = case.current_stage in (
        InvestigationStage.MITIGATION,
        InvestigationStage.TREATMENT,
    )

    outstanding_all = [n for n in case.evidence_needs if n.is_outstanding]
    # Surface-cap the causal asks (engine-differential + LLM-emitted causal) to the
    # rotating top-≤N (select_surfaced_causal_needs) so a broad retrieval-seeded
    # differential can't flood the user; SYMPTOM needs are unaffected. All needs stay
    # PENDING — this only bounds what is SHOWN, and rotates under non-progress so no
    # answerable ask is permanently hidden (#604).
    _surfaced_causal_ids = {n.need_id for n in select_surfaced_causal_needs(case)}
    # Causal needs the surface cap held back this turn. Counted into the overflow
    # notice below so the "…and N more not shown" signal reflects the TRUE hidden
    # demand — otherwise these vanish from `outstanding` and the LLM is told the ask
    # list is near-complete while live discriminators are withheld (anti-anchoring §6.1).
    # UNOBTAINABLE needs are excluded: they were deliberately dropped from the
    # surfaced set (declared un-gettable), so counting them as "more not shown"
    # would nudge the model to chase data it already declared a wall on.
    hidden_causal = sum(
        1
        for n in outstanding_all
        if n.purpose == NeedPurpose.CAUSAL_VERIFICATION
        and n.need_id not in _surfaced_causal_ids
        and n.obtainability != NeedObtainability.UNOBTAINABLE
    )
    outstanding = [
        n
        for n in outstanding_all
        if n.purpose != NeedPurpose.CAUSAL_VERIFICATION
        or n.need_id in _surfaced_causal_ids
    ]
    # Re-verification checklist is anchored on confirmed presence-evidence
    # rows (symptom/causal), NOT FULFILLED needs. Evidence rows exist for
    # every confirmed finding; FULFILLED needs are gap-conditional and
    # gap-rare, so a need-anchored checklist silently omits findings
    # confirmed from already-available data. See _render_finding_line.
    re_verification = (
        [
            ev
            for ev in case.evidence
            if ev.category
            in (EvidenceCategory.SYMPTOM_EVIDENCE, EvidenceCategory.CAUSAL_EVIDENCE)
        ]
        if in_post_diagnosis
        else []
    )

    if not outstanding and not re_verification:
        return ""

    # Re-verification findings: chronological by the turn they were established.
    re_verification.sort(key=lambda ev: ev.collected_at_turn)

    # Render-cap budget: up to _EVIDENCE_NEEDS_RENDER_CAP entries per section so neither
    # starves the other — generous enough that real cases never hit the cap.
    #
    # The surface cap already chose ≤ _SURFACED_CAUSAL_CAP causal asks to show; those are
    # RESERVED a render slot. Otherwise the priority sort — causal needs are MEDIUM,
    # symptom needs HIGH — could push all of them past _EVIDENCE_NEEDS_RENDER_CAP behind a
    # wall of HIGH symptom needs, silently dropping the asks the surface cap just picked.
    # Only out_rest needs priority-sorting (it feeds the render-cap slice); the reserved
    # causal needs are kept regardless of priority, and out_rendered is sorted once at the
    # end for display (stable sort preserves the repo's created_at/need_id tie order).
    out_reserved = [n for n in outstanding if n.need_id in _surfaced_causal_ids]
    out_rest = [n for n in outstanding if n.need_id not in _surfaced_causal_ids]
    out_rest.sort(key=lambda n: _PRIORITY_ORDER[n.priority])
    out_rendered = (
        out_reserved
        + out_rest[: max(0, _EVIDENCE_NEEDS_RENDER_CAP - len(out_reserved))]
    )
    out_rendered.sort(key=lambda n: _PRIORITY_ORDER[n.priority])
    # Overflow = needs dropped by the render cap PLUS causal needs the surface cap
    # held back (`hidden_causal`), so the notice never under-reports the hidden demand.
    out_overflow = (len(outstanding) - len(out_rendered)) + hidden_causal
    reverif_rendered = re_verification[:_EVIDENCE_NEEDS_RENDER_CAP]
    reverif_overflow = len(re_verification) - len(reverif_rendered)

    lines: list[str] = ["<evidence_needs>"]
    # Anti-anchoring framing — design §6.1. Emitted once at the block
    # opening regardless of which sections fire so the LLM never treats
    # the list as exhaustive, including during re-verification (where
    # evidence that the fix introduced a new problem is exactly the
    # kind of finding this sentence keeps in view).
    lines.append(
        "Unexpected findings outside the entries below are equally important "
        "and may lead to new hypotheses or revised needs."
    )
    lines.append("")

    if outstanding:
        lines.append("Outstanding needs (data to look for in uploads):")
        lines.append("")
        for need in out_rendered:
            header, motivator_line = _render_need_line(need)
            lines.append(header)
            lines.append(motivator_line)
        if out_overflow > 0:
            lines.append("")
            lines.append(
                f"  …and {out_overflow} more outstanding need(s) not shown "
                f"(cap reached)."
            )

    if re_verification:
        if outstanding:
            lines.append("")
        lines.append("Re-verification checklist (confirmed findings — re-check each to")
        lines.append(
            "confirm the fix held; emit *_absence_evidence when a signature is gone):"
        )
        lines.append("")
        for ev in reverif_rendered:
            lines.append(_render_finding_line(ev))
        if reverif_overflow > 0:
            lines.append("")
            lines.append(
                f"  …and {reverif_overflow} more re-verification need(s) not "
                f"shown (cap reached)."
            )

    lines.append("</evidence_needs>")
    return "\n".join(lines)


def _build_candidate_solutions_block(case: Case) -> str:
    """Render the ``<candidate_solutions>`` block (R9).

    When a runbook-seeded cause has been *confirmed* (its root counterfactually
    validated), surface that runbook's structured ``interventions`` as CANDIDATE
    fixes so the LLM proposes them via ``solutions_to_add`` — with the
    intervention quadrant carried through — instead of re-deriving the fix from
    prose. A *prior, not a directive*: each candidate still requires the user to
    accept and verify, and the M5 gate is unchanged.

    Returns ``""`` unless the case is INVESTIGATING and a confirmed seeded cause
    carries captured interventions (``confirmed_cause_interventions``). Because
    interventions are captured only when the seeder runs (behind the
    ``FAULTMAVEN_KB_CAUSE_SEEDER`` flag), this block is inert when the flag is off —
    an empty section, exactly like every other optional prompt block on a turn that
    has nothing to show for it.
    """
    if case.state != CaseState.INVESTIGATING:
        return ""

    # Provenance-read helper lives in the seeder module (not a safety mechanism;
    # see the provenance-blindness invariant). Local import mirrors the module's
    # other lazy kb_cause_seeder uses and avoids an import cycle.
    from faultmaven.core.investigation.kb_cause_seeder import (
        confirmed_cause_interventions,
    )

    interventions = confirmed_cause_interventions(case)
    if not interventions:
        return ""

    lines = [
        "<candidate_solutions>",
        "The confirmed root cause was seeded from a runbook that documents these",
        "interventions. They are CANDIDATE fixes for the established cause — a",
        "prior, not a directive. Weigh each against the case evidence; each still",
        "requires the user to accept and verify (the solution gate is unchanged).",
        "When you propose one via solutions_to_add, set `quadrant` to the listed",
        "quadrant so the fix is recorded against the right causal rung.",
        "",
    ]
    for iv in interventions:
        quadrant = iv.get("quadrant") or "?"
        text = " ".join((iv.get("text") or "").split())
        if len(text) > 300:
            text = text[:297] + "..."
        lines.append(f"- [{quadrant}] {text}")
    lines.append("</candidate_solutions>")
    return "\n".join(lines)


def _build_compact_history(case: Case, user_message_safe: str) -> str:
    """State-summary + previous-turn + current-turn (the low-fidelity history).

    Extracted so the allocator can choose between this and the fuller graduated
    history by budget pressure. Crucially, this *always* includes the current
    turn (and the previous turn when available), so even at the lowest fidelity
    conversational continuity is preserved — this is what lets the allocator
    guarantee continuity via the conversation section's floor instead of a
    separately-reserved last exchange.
    """
    recent_history = _build_state_summary(case)
    if case.turn_history:
        last_turn = case.turn_history[-1]
        recent_history += "\n\n<previous_turn>\n"
        if last_turn.evidence_added:
            recent_history += (
                f"User provided: {len(last_turn.evidence_added)} evidence artifacts\n"
            )
        if last_turn.agent_response_summary:
            recent_history += f"Agent: {last_turn.agent_response_summary[:200]}\n"
        recent_history += "</previous_turn>"
    recent_history += "\n\n<current_turn>\n"
    recent_history += f"User: {user_message_safe}\n"
    recent_history += "</current_turn>"
    return recent_history


# =============================================================================
# Priority-greedy budget allocator (the token-budget allocation model).
# See docs/architecture/investigation-engine/prompt-token-budget-allocation.md
# =============================================================================
def _cap_text_tokens(
    text: str,
    max_tokens: int,
    provider_name: Optional[str],
    model_name: Optional[str],
) -> str:
    """Bound a reserved item to ``max_tokens`` (truncate with a marker)."""
    if not text:
        return text
    tb = TokenBudget(max_tokens, provider_name=provider_name, model_name=model_name)
    return tb.use(text)


def _allocate_sections(
    *,
    budget: "TokenBudget",
    case: Case,
    provider_name: Optional[str],
    model_name: Optional[str],
    # reserved (bounded, never trimmed)
    identity: str,
    core_context: str,
    milestones_str: str,
    inquiry_state_str: str,
    pending_action_str: str,
    user_message_safe: str,
    feedback_str: str,
    # variable sections (priority order is fixed below)
    evidence_str: str,
    graduated_history: str,
    compact_history: str,
    journal_str: str,
    conclusion_str: str,
    kb_str: str,
    hypothesis_str: str,
    evidence_needs_str: str,
    entity_highlights_str: str,
    candidate_solutions_str: str,
) -> Dict[str, str]:
    """Priority-greedy allocation of ``budget`` across the prompt sections.

    Reserve first (bounded), then two passes over the variable sections in
    strict priority order: pass A grants each its floor, pass B grows each up to
    its cap with the remaining budget (sequential, not proportional). Continuity
    is guaranteed by the conversation floor (its lowest fidelity, the compact
    history, always carries the latest turn); INV-1 (current-turn upload) is
    guaranteed by evidence's floor + its internal current-turn render.
    """
    from faultmaven.config.settings import get_settings

    try:
        pb = get_settings().prompt_budget
        user_cap = pb.user_message_max_tokens
        feedback_cap = pb.system_feedback_max_tokens
        journal_cap = pb.journal_max_tokens
        conversation_cap = pb.conversation_history_max_tokens
    except Exception:
        user_cap, feedback_cap, journal_cap = 4000, 1500, 1500
        conversation_cap = 8000

    try:
        evidence_fraction = (
            get_settings().investigation_context.evidence_budget_fraction
        )
    except Exception:
        evidence_fraction = 0.6

    ctx: Dict[str, str] = {}

    # --- 1. Reserve (bounded, always present, counted first) ---
    def _reserve(text: str) -> str:
        if text:
            budget.used_tokens += budget.count(text)
        return text

    capped_user = _cap_text_tokens(
        user_message_safe, user_cap, provider_name, model_name
    )
    capped_feedback = _cap_text_tokens(
        feedback_str, feedback_cap, provider_name, model_name
    )
    ctx["identity"] = _reserve(identity)
    ctx["core_context"] = _reserve(core_context)
    ctx["milestones"] = _reserve(milestones_str)
    ctx["inquiry_state"] = _reserve(inquiry_state_str)
    ctx["pending_action"] = _reserve(pending_action_str)
    ctx["system_feedback"] = _reserve(capped_feedback)
    ctx["user_message"] = _reserve(capped_user)

    reserve_tokens = budget.used_tokens
    section_budget = max(0, budget.limit_tokens - reserve_tokens)

    # --- 2. Section sizes, measured ONCE and reused (no re-tokenization) ---
    # Conversation has two fidelities: the fuller graduated history, and the
    # compact one (which ALWAYS carries the latest turn). Both are sized here;
    # the renderer in pass B picks the largest that fits its allotment and, when
    # it must truncate, keeps the TAIL so the most-recent turns survive.
    graduated_tokens = budget.count(graduated_history)
    compact_tokens = budget.count(compact_history)
    evidence_cap = int(section_budget * evidence_fraction)
    evidence_tokens = budget.count(evidence_str) if evidence_str else 0
    # Evidence floor: guarantee room for at least the current-turn render so
    # INV-1 (a fresh upload's addressable stub always survives) holds in the
    # normal path. Granted FIRST in pass A (evidence is priority #1), ahead of
    # the conversation continuity floor — INV-1 outranks continuity, which
    # degrades gracefully (and the starvation fallback backstops the extreme
    # case). Must NOT be capped by leaving room for compact_history, or it
    # collapses to 0 when the conversation floor is large and the current-turn
    # upload is dropped.
    evidence_floor = min(evidence_tokens, EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM // 4)

    # (key, text, size, floor, cap) in PRIORITY order. Conversation uses the
    # graduated size for sizing; its floor is the compact size (continuity).
    variable = [
        (
            "evidence",
            evidence_str,
            evidence_tokens,
            evidence_floor,
            max(evidence_cap, evidence_floor),
        ),
        (
            "conversation_history",
            graduated_history,
            graduated_tokens,
            min(compact_tokens, section_budget),
            # Bounded cap (§5.1): must not default to the whole section_budget or
            # verbose history starves the journal/KB/hypotheses below it. Kept at
            # least as large as the continuity floor so the compact-history floor
            # is never capped below itself.
            max(
                min(compact_tokens, section_budget),
                min(section_budget, conversation_cap),
            ),
        ),
        (
            "investigation_journal",
            journal_str,
            budget.count(journal_str),
            0,
            journal_cap,
        ),
        (
            "working_conclusion",
            conclusion_str,
            budget.count(conclusion_str),
            0,
            section_budget,
        ),
        ("kb_results", kb_str, budget.count(kb_str), 0, section_budget),
        ("hypotheses", hypothesis_str, budget.count(hypothesis_str), 0, section_budget),
        (
            "candidate_solutions",
            candidate_solutions_str,
            budget.count(candidate_solutions_str),
            0,
            section_budget,
        ),
        (
            "evidence_needs",
            evidence_needs_str,
            budget.count(evidence_needs_str),
            0,
            section_budget,
        ),
        (
            "entity_highlights",
            entity_highlights_str,
            budget.count(entity_highlights_str),
            0,
            section_budget,
        ),
    ]

    # Pass A — pre-reserve floors (highest priority first, while budget remains)
    reserved_floor: Dict[str, int] = {}
    remaining = section_budget
    for key, _text, size, floor, _cap in variable:
        grant = min(floor, size, remaining)
        reserved_floor[key] = grant
        remaining -= grant

    # Pass B — strict-priority sequential greedy fill up to each cap
    for key, text, size, _floor, cap in variable:
        want = min(size, cap)
        floor_grant = reserved_floor[key]
        take_extra = min(max(0, want - floor_grant), remaining)
        alloc = floor_grant + take_extra
        remaining -= take_extra

        if key == "conversation_history":
            # Continuity: pick the largest fidelity that fits; if even compact
            # must be cut, keep the TAIL (latest turns are at the end).
            if alloc <= 0:
                rendered = ""
            elif alloc >= graduated_tokens:
                rendered = graduated_history
            elif alloc >= compact_tokens:
                rendered = compact_history
            else:
                rendered = budget._truncate_to(compact_history, alloc, keep="tail")
        elif not text or alloc <= 0:
            rendered = ""
        elif size <= alloc:
            rendered = text
        else:
            # The journal is recency-ordered (oldest anchors first, newest last)
            # and is anti-amnesia memory: under hard truncation keep the TAIL so
            # the most-recent decisions/findings/blockers survive — dropping the
            # newest (the default keep="head") is exactly wrong here. The other
            # variable sections (KB, hypotheses) are rank-ordered best-first, so
            # keep="head" is correct for them.
            keep = "tail" if key == "investigation_journal" else "head"
            rendered = budget._truncate_to(text, alloc, keep=keep)

        ctx[key] = rendered
        # Reuse the known size when the section was admitted whole (no recount).
        if rendered is text:
            used = size
        elif rendered:
            used = budget.count(rendered)
        else:
            used = 0
        budget.used_tokens += used
        # Reclaim any allotment the section didn't consume (e.g. conversation
        # downgraded to a smaller fidelity than its graduated-sized alloc) so it
        # flows down to lower-priority sections instead of being stranded.
        if used < alloc:
            remaining += alloc - used

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "prompt_allocation_v2",
            extra={
                "case_id": case.case_id,
                "provider": provider_name,
                "model": model_name,
                "resolved_limit_tokens": budget.limit_tokens,
                "reserve_tokens": reserve_tokens,
                "section_budget_tokens": section_budget,
                "used_tokens": budget.used_tokens,
            },
        )
    return ctx


def _build_causal_graph_block(case: Case) -> str:
    """Render the causal graph (hypotheses ARE chains, methodology M3).

    Render the chain STRUCTURE with node ids — not flat statements — so the LLM
    EXTENDS the existing graph: it references an existing node's ``cn_...`` id
    (in produces / root_node_ref / node_evidence_links) instead of re-stating a
    cause as a fresh duplicate node. That cross-turn re-emission is what
    fragments grounding across duplicate roots and stalls cause_state at UNKNOWN
    (the node-identity loop was previously open: the engine assigned ids but
    never rendered them back, so the LLM could not reference them). REFUTED
    hypotheses keep their refutation_reason inline (anti-amnesia, Rule 8:
    prevents re-proposing a rejected theory); the pair-integrity invariant
    guarantees it is non-empty when state=REFUTED.

    Returns an empty string when the case has no active hypotheses and no
    causal nodes (nothing to render yet).
    """

    def _stmt(s: str) -> str:
        s = " ".join((s or "").split())
        return s if len(s) <= 140 else s[:137] + "..."

    nodes = case.causal_nodes or {}
    active_h = [h for h in case.hypotheses.values() if h.state.value != "retired"]
    if not (active_h or nodes):
        return ""

    # §7.1/INV-29 elicitation: a ROOT held from VALIDATED only by the
    # causal-grounding bar gets its REASON-SPECIFIC recovery action rendered
    # inline — without it the model sees a bare [root/inconclusive],
    # re-records the same datum (which the independence mirror collapses) or
    # re-hedges the same link, and stalls.
    block_reasons = root_support_block_reasons(case)
    recovery_notes = {
        BLOCK_REASON_COUNT: (
            " — needs a SECOND INDEPENDENT causal observation to validate "
            "(re-recording the same datum does not count)"
        ),
        BLOCK_REASON_MIRROR: (
            " — needs a SECOND INDEPENDENT causal observation to validate "
            "(re-recording the same datum does not count)"
        ),
        BLOCK_REASON_HEDGED: (
            " — its causal support is self-hedged (stance_confidence below "
            "0.6); record a CONFIDENT causal observation to ground it"
        ),
    }

    # §7.1.2 MECE arbitration: contested roots (several simultaneously-
    # validated, mutually-exclusive causes) get the discrimination ask
    # rendered inline — without it the model sees several [root/validated]
    # lines, reads the cause as settled, and never runs the test that
    # separates them. Rendered through the SAME per-node reason → note maps
    # as the §7.1 recovery notes; the overlay order makes the precedence
    # explicit (a contested VALIDATED root shows the discrimination ask even
    # if a future block reason ever annotates validated roots too).
    _MECE_CONTESTED = "mece_contested"
    recovery_notes[_MECE_CONTESTED] = (
        " — one of several simultaneously-validated MUTUALLY-EXCLUSIVE roots: "
        "cause identification is HELD until discriminating evidence refutes "
        "the alternatives (at most one can be the real cause)"
    )
    node_reasons = {
        **block_reasons,
        **dict.fromkeys(mece_contested_root_ids(case), _MECE_CONTESTED),
    }

    def _node_line(indent: str, n) -> str:
        note = recovery_notes.get(node_reasons.get(n.node_id), "")
        return (
            f"{indent}{n.node_id} [{n.node_type.value}/{n.node_state.value}] "
            f"{_stmt(n.statement)}{note}"
        )

    on_path: set[str] = set()
    lines = [
        "<causal_graph>",
        "Chains built so far (D = the problem). REFERENCE these cn_... ids when "
        "extending — attach evidence or new rungs to an existing node rather "
        "than re-stating a cause already present as a new node.",
    ]
    for h in active_h:
        lines.append(
            f"- {_stmt(h.statement)} "
            f"(Confidence: {h.likelihood*100:.0f}%, State: {h.state.value})"
        )
        chain_ids = h.path or ([h.root_node_id] if h.root_node_id else [])
        for nid in chain_ids:
            n = nodes.get(nid)
            if n is None or n.node_type.value == "problem":
                continue
            on_path.add(nid)
            lines.append(_node_line("    ", n))
        if h.state.value == "refuted" and h.refutation_reason:
            lines.append(f"    Refuted because: {_stmt(h.refutation_reason)}")
    # Standalone nodes on no hypothesis path — surface their ids so the LLM
    # attaches/extends them instead of re-emitting the same cause.
    orphans = [
        n
        for nid, n in nodes.items()
        if nid not in on_path and n.node_type.value != "problem"
    ]
    if orphans:
        lines.append(
            "  Unattached causes already in the graph — reference these ids, "
            "do not re-emit:"
        )
        for n in orphans:
            lines.append(_node_line("    ", n))
    lines.append("</causal_graph>")
    return "\n".join(lines)


def build_investigation_context(
    case: Case,
    user_message: str,
    kb_results: Optional[List[Dict[str, Any]]] = None,
    max_tokens: Optional[int] = None,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    use_state_summary: Optional[bool] = None,
    enable_stage_specific_loading: bool = True,
    processing_mode: Optional[str] = None,
    entity_highlights: Optional[str] = None,
    tools_available: bool = False,
) -> Dict[str, str]:
    """
    Gather and format context elements within token budget.

    Gap #10: Stage-Specific Context Loading
    - Skip irrelevant sections based on investigation stage
    - Reference: Prompt Engineering Guide Section 11.4

    Args:
        case: Current case
        user_message: User's message this turn
        kb_results: Optional knowledge base search results
        max_tokens: Optional explicit token limit (overrides provider-based calculation)
        provider_name: LLM provider name for dynamic budget calculation
        model_name: LLM model name for fine-grained budget calculation
        use_state_summary: Optional flag to use compact state summary instead of full history
                          (auto-enabled for conversations >15 turns)
        enable_stage_specific_loading: Enable stage-specific context optimization (default: True)

    Returns:
        Dictionary of formatted context sections
    """
    # Sanitize user input (Gap #9: Input Sanitization)
    sanitized_input = sanitize_user_input(user_message)
    if sanitized_input.warnings:
        logger.warning(
            f"Input sanitization warnings for case {case.case_id}: {', '.join(sanitized_input.warnings)}"
        )
    user_message_safe = sanitized_input.content

    # Determine token budget (Gap #6: Provider-Specific Limits)
    if max_tokens is None:
        if provider_name:
            max_tokens = get_token_budget_for_provider(provider_name, model_name)
            logger.debug(
                f"Using provider-specific budget: {max_tokens} tokens "
                f"(provider={provider_name}, model={model_name})"
            )
        else:
            max_tokens = 8000  # Default fallback
            logger.debug("Using default budget: 8000 tokens (no provider specified)")

    budget = TokenBudget(
        max_tokens,
        provider_name=provider_name,
        model_name=model_name,
    )

    # 1. Identity & Status (Gap #8: XML tags for better LLM attention)
    #
    # CURRENT_TIME anchors every other timestamp in this prompt. Without it the
    # model has no way to tell a live reading from a stale one — an alert
    # stamped 19:36 is just a number, and "is this still happening?" is not a
    # question it can even ask. It cannot be inferred from the conversation
    # (the model's own sense of "now" is its training cutoff), so it has to be
    # stated. Whether the model may TRUST a symptom is decided by the engine's
    # gates; this only gives it the arithmetic to reason about age at all.
    identity = f"<case_identity>\n"
    identity += f"CURRENT_TIME: {datetime.now(timezone.utc).isoformat()}\n"
    identity += f"CASE_ID: {case.case_id}\n"
    identity += f"STATE: {case.state.value.upper()}\n"
    if case.state == CaseState.INVESTIGATING and case.current_stage:
        identity += f"CURRENT_STAGE: {case.current_stage.value.upper()}\n"
    identity += "</case_identity>"

    # 2. Case Core Context
    core_context = f"<problem_context>\n"
    core_context += f"TITLE: {case.title}\n"
    core_context += f"DESCRIPTION: {case.description}\n"
    if case.problem_verification:
        pv = case.problem_verification
        core_context += f"SYMPTOM_STATEMENT: {pv.symptom_statement}\n"
        if pv.severity:
            core_context += f"SEVERITY: {pv.severity}\n"
        if pv.temporal_state:
            core_context += f"TEMPORAL_STATE: {pv.temporal_state.value}\n"
    core_context += "</problem_context>"

    # 3. Milestone Status (separated into stage-gate and progress indicators)
    milestones_str = ""
    if case.state == CaseState.INVESTIGATING:
        p = case.progress

        # Stage-gate milestones (drive transitions). Post-redesign the
        # mitigation gates live on the mitigation record, not progress
        # booleans; derive the same telemetry symbols from it.
        _stab = p.mitigation
        stage_gates = {
            "mitigation_accepted": bool(_stab is not None and _stab.accepted),
            "mitigation_verified": bool(_stab is not None and _stab.verified),
            "solution_accepted": p.solution_accepted,
            "solution_verified": p.solution_verified,
        }
        active_gates = [k for k, v in stage_gates.items() if v]

        # Progress indicators (LLM context)
        indicators = {
            "symptom_verified": p.symptom_verified,
            "root_cause_identified": p.cause_state == CauseState.IDENTIFIED,
            "solution_proposed": p.solution_proposed,
        }
        active_indicators = [k for k, v in indicators.items() if v]

        milestones_str = f"<current_stage>{p.stage_display_name}</current_stage>\n"
        if active_gates:
            milestones_str += "<stage_gate_milestones>\n"
            for g in active_gates:
                milestones_str += f"- {g}\n"
            milestones_str += "</stage_gate_milestones>\n"
        if active_indicators:
            milestones_str += "<progress_indicators>\n"
            for ind in active_indicators:
                milestones_str += f"- {ind}{_symptom_currency_note(case, ind)}\n"
            milestones_str += "</progress_indicators>"
        else:
            milestones_str += "<progress_indicators>None yet</progress_indicators>"

    # 4. Evidence Context (Sliding Window)
    # Three-tier system: Tier A (recent data with structural index),
    # Tier B (older data, summary only), Tier C (user text, summary only).
    # Fixes "I don't have access to file content" bug by including
    # structural indexes in the LLM context for recent evidence.
    # Under the allocator, size the evidence block to its actual allotment
    # (≈ evidence_fraction of the section budget) rather than the full model
    # budget — see _build_evidence_context (avoids the double-budget).
    evidence_char_override = None
    if max_tokens:
        try:
            from faultmaven.config.settings import get_settings

            _frac = get_settings().investigation_context.evidence_budget_fraction
        except Exception:
            _frac = 0.6
        evidence_char_override = max(
            2 * EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM, int(max_tokens * _frac * 4)
        )
    evidence_str = _build_evidence_context(
        case,
        processing_mode=processing_mode,
        user_query=user_message_safe,
        provider_name=provider_name,
        model_name=model_name,
        char_budget_override=evidence_char_override,
        tools_available=tools_available,
    )

    # 5. Causal graph (hypotheses ARE chains, methodology M3).
    # Rendered by _build_causal_graph_block — see its docstring for the
    # node-identity-loop rationale (render ids back so the LLM extends rather
    # than re-emits). The stage-specific loading below may later REPLACE this
    # with a condensed <working_hypotheses> block on non-DIAGNOSIS stages.
    hypothesis_str = _build_causal_graph_block(case)

    # 5a. Investigation Journal (durable long-term memory)
    # Compact, append-only record of key findings, decisions, and context.
    # Always included in full — ~5 KB for a 50-turn investigation.
    journal_str = ""
    if case.investigation_journal:
        journal_str = "<investigation_journal>\n"
        for entry in case.investigation_journal:
            tag = entry.entry_type.upper()
            journal_str += f"[T{entry.turn}] {tag}: {entry.content}\n"
        journal_str += "</investigation_journal>"

    # 5b. Working Conclusion (durable case-level understanding)
    # Persists across turns even after evidence structural indexes are evicted
    # from the Tier A window, ensuring the agent retains its accumulated findings.
    conclusion_str = ""
    if case.working_conclusion:
        wc = case.working_conclusion
        conclusion_str = "<working_conclusion>\n"
        conclusion_str += f"STATEMENT: {wc.statement}\n"
        conclusion_str += f"CONFIDENCE: {wc.likelihood*100:.0f}%\n"
        conclusion_str += f"REASONING: {wc.reasoning[:1000]}\n"
        if wc.supporting_evidence_ids:
            conclusion_str += f"EVIDENCE: {', '.join(wc.supporting_evidence_ids)}\n"
        # §7.1.2 coherence: the working conclusion is the max-likelihood pick
        # over STANDING hypotheses — on a MECE-contested case that is one of
        # several simultaneously-validated exclusive causes, and rendering it
        # unqualified beside the graph block's discrimination ask invites the
        # model to anchor on the pick instead of running the separating test.
        if getattr(case.progress, "cause_identification_contested", False):
            conclusion_str += (
                "NOTE: this is ONE of several simultaneously-validated "
                "mutually-exclusive candidate causes (see the causal graph) — "
                "cause identification is HELD; gather DISCRIMINATING evidence "
                "before treating this statement as the cause.\n"
            )
        conclusion_str += "</working_conclusion>"

    # 5c. Pending ProposedAction (Framework §4.1: LLM needs this to detect compliance)
    # Selection (INV-33): prefer the newest COMPLIANCE-BEARING pending action (a
    # SOLUTION or MITIGATION carries a MILESTONE_TO_SET and drives a stage
    # transition) over a bare DIAGNOSTIC ask. Since the zone-exit de-absolutization
    # lets the model raise a parallel diagnostic while a fix is still pending, a
    # plain newest-pending pick would let that diagnostic mask the fix's
    # solution_accepted cue and stall the TREATMENT transition on the post-fix
    # reply. A DIAGNOSTIC (no compliance gate) surfaces only when nothing
    # compliance-bearing stands pending.
    pending_action_str = ""
    if case.proposed_actions:
        pending = [a for a in case.proposed_actions if a.state == "pending"]
        compliance_bearing = [
            a
            for a in pending
            if a.action_type
            in (InvestigationActionType.SOLUTION, InvestigationActionType.MITIGATION)
        ]
        action = (compliance_bearing or pending)[-1] if pending else None
        if action is not None:
            action_type_upper = action.action_type.value.upper()
            pending_action_str = "<pending_action>\n"
            pending_action_str += f"ACTION_TYPE: {action_type_upper}\n"
            pending_action_str += f"DESCRIPTION: {action.description}\n"
            if action.commands:
                pending_action_str += "COMMANDS:\n"
                for cmd in action.commands:
                    pending_action_str += f"  - {cmd}\n"
            pending_action_str += f"PROPOSED_IN_TURN: {action.proposed_in_turn}\n"
            # Map action type → milestone so the LLM knows exactly what to set
            if action_type_upper == "MITIGATION":
                pending_action_str += (
                    "MILESTONE_TO_SET: mitigation_accepted (set True when user "
                    "submits results of executing this mitigation)\n"
                )
            elif action_type_upper == "SOLUTION":
                pending_action_str += (
                    "MILESTONE_TO_SET: solution_accepted (set True when user "
                    "submits results of executing this solution)\n"
                )
            # Surface engine-issued downgrade reason if present so the
            # LLM understands why its intent was rewritten and can
            # recover on this turn (e.g. by gathering the missing
            # evidence and re-proposing).
            if getattr(action, "downgrade_reason", None):
                pending_action_str += f"ENGINE_NOTE: {action.downgrade_reason}\n"
            pending_action_str += "</pending_action>"

    # 6. Conversation History
    # Two modes:
    # - State Summary (>15 turns): Minimal summary + last turn only
    # - Graduated History (≤15 turns): Recent turns verbatim, older summarized
    if use_state_summary is None:
        use_state_summary = case.current_turn > STATE_SUMMARY_TURN_THRESHOLD

    if use_state_summary:
        # State Summary + Last Turn pattern (~200 tokens vs ~2000)
        recent_history = _build_state_summary(case)

        # Add previous turn context if available
        if case.turn_history:
            last_turn = case.turn_history[-1]
            recent_history += "\n\n<previous_turn>\n"

            # What user provided
            if last_turn.evidence_added:
                recent_history += f"User provided: {len(last_turn.evidence_added)} evidence artifacts\n"

            # What agent requested or concluded
            if last_turn.agent_response_summary:
                summary = last_turn.agent_response_summary[:200]
                recent_history += f"Agent: {summary}\n"

            recent_history += "</previous_turn>"

        # Current turn (using sanitized input)
        recent_history += "\n\n<current_turn>\n"
        recent_history += f"User: {user_message_safe}\n"
        recent_history += "</current_turn>"

    else:
        # Graduated history: recent turns verbatim, older turns summarized
        recent_history = _build_graduated_history(case)

    # 7. Knowledge Base Results
    # Cap individual solution text to prevent a single verbose runbook from
    # consuming the remaining token budget.
    # KB context: combine passed-in results with case-level pre-fetched context
    all_kb_results = list(kb_results or [])
    if case.kb_context:
        all_kb_results.extend(case.kb_context)

    kb_str = ""
    if all_kb_results:
        kb_str = (
            "<knowledge_context>\n"
            "The following runbooks matched the investigation symptoms or root cause. "
            "These are suggestions — do not force these solutions if the evidence "
            "points to a different root cause.\n\n"
        )
        for i, res in enumerate(all_kb_results[:5]):  # Top 5
            summary = res.get("summary", "")
            solution = res.get("solution", "")
            title = res.get("title", "")
            trigger = res.get("trigger", "")
            trigger_label = f" [matched on {trigger}]" if trigger else ""
            if len(solution) > KB_MAX_SOLUTION_CHARS:
                solution = solution[:KB_MAX_SOLUTION_CHARS] + "... [truncated]"
            if title:
                kb_str += f"MATCH {i+1}: {title}{trigger_label}\n"
            if summary:
                kb_str += f"  {summary}\n"
            if solution:
                kb_str += f"  SOLUTION: {solution}\n"
            kb_str += "\n"
        kb_str += "</knowledge_context>"

    # 8. System Feedback (Validation errors from previous turn)
    feedback_str = ""
    if case.turn_history:
        last_turn = case.turn_history[-1]
        if last_turn.system_feedback:
            feedback_str = f"IMPORTANT - SYSTEM FEEDBACK FROM PREVIOUS TURN:\n{last_turn.system_feedback}\n\n"

    # 9. Stage-Specific Context Loading (Gap #10: Section 11.4)
    # Optimize context by condensing hypothesis details during stages where
    # diagnosis is complete. Frees budget for action-focused context.
    # Uses its own query against case.hypotheses rather than the active_h
    # variable from section 5 (which contains all non-retired hypotheses).
    if enable_stage_specific_loading and case.state == CaseState.INVESTIGATING:
        stage = case.current_stage or InvestigationStage.DIAGNOSIS

        if stage == InvestigationStage.DIAGNOSIS:
            # During long DIAGNOSIS investigations (state summary mode), condense
            # to top 3 hypotheses — the full block would duplicate the state summary.
            if use_state_summary:
                active_validated = [
                    h
                    for h in case.hypotheses.values()
                    if h.state.value in ("active", "validated")
                ]
                if active_validated:
                    top_3 = sorted(
                        active_validated, key=lambda h: h.likelihood, reverse=True
                    )[:3]
                    hypothesis_str = "<working_hypotheses>\n"
                    for h in top_3:
                        hypothesis_str += f"- {h.statement} (Confidence: {h.likelihood*100:.0f}%, State: {h.state.value})\n"
                    hypothesis_str += "</working_hypotheses>"

        elif stage == InvestigationStage.MITIGATION:
            logger.debug("Stage-specific loading: MITIGATION - condensing hypotheses")
            active_validated = [
                h
                for h in case.hypotheses.values()
                if h.state.value in ("active", "validated")
            ]
            if active_validated:
                hypothesis_str = "<working_hypotheses>\n"
                for h in active_validated:
                    hypothesis_str += (
                        f"- {h.statement} (Confidence: {h.likelihood*100:.0f}%)\n"
                    )
                hypothesis_str += "</working_hypotheses>"
            else:
                hypothesis_str = ""

        elif stage == InvestigationStage.TREATMENT:
            logger.debug("Stage-specific loading: TREATMENT - condensing hypotheses")
            validated = [
                h for h in case.hypotheses.values() if h.state.value == "validated"
            ]
            if validated:
                best = max(validated, key=lambda h: h.likelihood)
                hypothesis_str = f"<working_hypotheses>\n- {best.statement} (Confidence: {best.likelihood*100:.0f}%, VALIDATED)\n</working_hypotheses>"
            else:
                hypothesis_str = ""

    # 10. INQUIRY State — surfaces an unconfirmed proposed_problem_statement
    # to the LLM in one of two modes:
    #   NOT_YET_CONFIRMED — default; instructs the LLM not to re-propose the
    #     same statement and to focus on the user's current message.
    #   HANDSHAKE_DEFERRED — fires only on the turn immediately following a
    #     same-turn-confirmation guard fire (see INV-01); instructs the LLM
    #     to re-present the statement and ask for confirmation explicitly.
    # The two modes are mutually exclusive and gated on
    # case.inquiry.handshake_deferred_at_turn (set by _apply_inquiry_updates
    # in milestone_engine when the guard rejects a same-turn collapse).
    inquiry_state_str = ""
    if case.state == CaseState.INQUIRY and case.inquiry:
        inq = case.inquiry
        if inq.proposed_problem_statement and inq.proposed_problem_statement.strip():
            inquiry_state_str = "<inquiry_state>\n"
            inquiry_state_str += (
                f"PROPOSED_PROBLEM_STATEMENT: {inq.proposed_problem_statement}\n"
            )
            inquiry_state_str += f"CONFIRMED: {inq.problem_statement_confirmed}\n"
            if not inq.problem_statement_confirmed:
                handshake_deferred = (
                    inq.handshake_deferred_at_turn is not None
                    and inq.handshake_deferred_at_turn == case.current_turn - 1
                )
                if handshake_deferred:
                    # Previous turn: LLM emitted user_confirmed_investigation=True
                    # the same turn it first wrote proposed_problem_statement.
                    # The engine deferred the transition to preserve the User-
                    # Agent Handshake. This turn, the LLM MUST re-present the
                    # statement and ask for confirmation — overrides the
                    # default NOT_YET_CONFIRMED "don't re-propose" rule.
                    # Note: the engine deterministically attaches the canonical
                    # DECIDE confirmation pair on this turn (see
                    # _investigation_confirmation_suggestions in milestone_engine),
                    # so the prompt does not prescribe exact suggestion labels.
                    inquiry_state_str += (
                        "HANDSHAKE_DEFERRED: On the previous turn you set "
                        "user_confirmed_investigation=True the same turn you "
                        "first wrote proposed_problem_statement. The engine "
                        "deferred the transition because the user must see "
                        "the statement before confirming. This turn, RE-"
                        "PRESENT the statement verbatim — e.g. 'I want to "
                        "make sure I understand: <statement>. Is that "
                        "accurate?' Do NOT set user_confirmed_investigation"
                        "=True this turn — the user has not yet seen the "
                        "statement.\n"
                    )
                else:
                    # State fact, not a live directive: describe the case as it
                    # ENTERED this turn (proposed on an earlier turn, unconfirmed),
                    # never assert present-tense "the user has not confirmed" — that
                    # is false on the very turn the user confirms, and confirmation
                    # detection lives in the static TWO-STEP CONFIRMATION prose +
                    # the user_confirmed_investigation schema field. This block only
                    # suppresses re-proposing; it adds no confirmation directive.
                    inquiry_state_str += (
                        "NOT_YET_CONFIRMED: You already proposed this problem statement "
                        "on an earlier turn (it was unconfirmed going into this turn). "
                        "Do NOT re-propose it — respond to the user's current message.\n"
                    )
            inquiry_state_str += "</inquiry_state>"

    # Phase 4c — entity highlights block. Pre-fetched by the milestone
    # engine from the Phase 4 ``case_entities`` registry. Empty string
    # when the flag is off, the fetch failed, or the case has no
    # extracted entities — safe to always include the key so templates
    # can reference it unconditionally.
    entity_highlights_str = entity_highlights or ""

    # Evidence-needs Phase 4 — demand-side pool block. Empty string when
    # the pool has no visible needs for this stage (progressive
    # activation; design §10.6).
    evidence_needs_str = _build_evidence_needs_block(case)

    # R9 — candidate-solution priors: a confirmed runbook-seeded cause's
    # structured interventions, surfaced at the SOLUTION stage so the LLM proposes
    # them (quadrant-carrying) rather than re-deriving the fix from prose. Empty
    # when the seeder flag is off or no seeded cause is confirmed.
    candidate_solutions_str = _build_candidate_solutions_block(case)

    # =====================================================================
    # Budget allocation
    # =====================================================================
    # Priority-greedy allocator (the token-budget allocation model). Needs both
    # history fidelities so it can pick the one that fits; the compact one is the
    # continuity floor (always carries the latest turn).
    compact_history = _build_compact_history(case, user_message_safe)
    ctx = _allocate_sections(
        budget=budget,
        case=case,
        provider_name=provider_name,
        model_name=model_name,
        identity=identity,
        core_context=core_context,
        milestones_str=milestones_str,
        inquiry_state_str=inquiry_state_str,
        pending_action_str=pending_action_str,
        user_message_safe=user_message_safe,
        feedback_str=feedback_str,
        evidence_str=evidence_str,
        graduated_history=recent_history,
        compact_history=compact_history,
        journal_str=journal_str,
        conclusion_str=conclusion_str,
        kb_str=kb_str,
        hypothesis_str=hypothesis_str,
        evidence_needs_str=evidence_needs_str,
        entity_highlights_str=entity_highlights_str,
        candidate_solutions_str=candidate_solutions_str,
    )
    return ctx


# =============================================================================
# Phase 4c — entity-highlights pre-fetcher
# =============================================================================

# Entity types surfaced in the auto-injection block. These are the
# signals that most often drive hypothesis formation and refinement.
# Types not listed here (path, device, metric_name) are still usable
# via the ``find_entity`` / ``list_top_entities`` tools — they're just
# not part of the always-on highlights.
_HIGHLIGHT_TYPES: tuple[EntityType, ...] = (
    EntityType.IP,
    EntityType.HOSTNAME,
    EntityType.USER,
    EntityType.SERVICE,
)
# Per-type limit. Small enough that four types fit comfortably in a
# few hundred tokens; large enough to surface the shape of the data
# without dumping everything. The agent can go deeper via the tools.
_HIGHLIGHT_PER_TYPE_LIMIT = 5


async def fetch_entity_highlights(
    case_repository: Any,
    case_id: str,
    *,
    per_type_limit: int = _HIGHLIGHT_PER_TYPE_LIMIT,
) -> str:
    """Return a compact ``<entity_highlights>`` block or ``""``.

    Queries ``CaseRepository.list_top_entities`` for each
    investigative-signal type (IP, hostname, user, service) and formats
    the results as a tight XML block the template can drop in directly.
    Empty string when:

    - the repository is ``None`` or lacks ``list_top_entities`` (test
      doubles without the full surface),
    - every type returned zero rows,
    - or the query raised (failures are logged and degraded).

    Callers (milestone engine) gate on ``FAULTMAVEN_ENTITY_REGISTRY``
    before calling; this helper is also safe to call with the flag off,
    because there'll be no entities to surface.
    """
    if case_repository is None:
        return ""
    query = getattr(case_repository, "list_top_entities", None)
    if query is None:
        return ""

    sections: list[str] = []
    for entity_type in _HIGHLIGHT_TYPES:
        try:
            rows = await query(
                case_id=case_id,
                entity_type=entity_type,
                limit=per_type_limit,
            )
        except Exception as exc:
            logger.warning(
                "fetch_entity_highlights failed for %s on case %s: %s",
                entity_type.value,
                case_id,
                exc,
            )
            continue
        if not rows:
            continue
        body_lines = []
        for row in rows:
            marker = " (error)" if getattr(row, "in_error_context", False) else ""
            body_lines.append(f"  - {row.entity_value} ×{row.mention_count}{marker}")
        sections.append(f"{entity_type.value}:\n" + "\n".join(body_lines))

    if not sections:
        return ""

    return (
        "<entity_highlights>\n"
        "Top entities extracted from this case's evidence "
        "(aggregated mention_count across artifacts). Use find_entity "
        "to locate a value's origin evidence, or list_top_entities for "
        "types not shown here.\n\n" + "\n\n".join(sections) + "\n</entity_highlights>"
    )
