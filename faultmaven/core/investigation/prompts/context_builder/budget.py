import logging
from typing import Optional

logger = logging.getLogger("faultmaven.core.investigation.prompts.context_builder")


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
# Max hypotheses listed in the state summary. Every active/validated hypothesis
# is listed (with its id) rather than the top 3: past the summary threshold the
# summary is the only place a lower-ranked hypothesis's ``hyp_`` id appears, and
# without it the model cannot link causal evidence to that hypothesis (#1116).
STATE_SUMMARY_MAX_HYPOTHESES = 10
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


#: Allotments at or below this many tokens cannot hold even the truncation
#: marker, so ``TokenBudget._truncate_to`` returns "" for them and
#: ``_shrink_fenced_tail`` returns :data:`_SECTION_DROPPED_MARKER`. The
#: allocator does NOT key INV-4 on this number: it marks any non-empty section
#: that rendered as "" for whatever reason, because a fenced section can also
#: be emptied well above it (#610).
_SILENT_DROP_MAX_TOKENS = 2

#: The one marker for "content existed here and none of it fit". Emitted by
#: ``_truncate_to`` when only the marker fits, by ``_shrink_fenced_tail`` when
#: the conversation's delimiters leave no room for body, and by the allocator
#: for any non-empty section that would otherwise render as "" (#610).
_SECTION_DROPPED_MARKER = "[...]"


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
        dropped (INV-4). At or below the floor it returns "", because a limit
        that small cannot hold even the marker; the allocator, which is the
        caller that can reach that floor, marks any section that renders as ""
        and charges the marker to the margin (#610). Does not mutate
        ``used_tokens``.
        """
        if not text or token_limit <= _SILENT_DROP_MAX_TOKENS:
            return ""
        marker = (
            "\n[...truncated...]"
            if token_limit < 30
            else "\n[... Content truncated due to context limit ...]"
        )
        marker_tokens = self.count(marker)
        # Only room for (about) the marker → emit a minimal non-silent trace.
        if token_limit <= marker_tokens + 1:
            return _SECTION_DROPPED_MARKER

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
        return _compose(truncated) if truncated else _SECTION_DROPPED_MARKER

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
