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
from typing import Any, Callable, Dict, List, Optional, Sequence

from faultmaven.core.investigation.causal_graph import (
    BLOCK_REASON_COUNT,
    BLOCK_REASON_HEDGED,
    BLOCK_REASON_MIRROR,
    BLOCK_REASON_RESTATEMENT,
    mece_contested_root_ids,
    restatement_held_root_ids,
    root_support_block_reasons,
)
from faultmaven.core.investigation.coverage_trust import is_inferred, is_vouched
from faultmaven.core.investigation.evidence_need_surfacing import (
    is_ask_exhausted,
    select_surfaced_causal_needs,
)
from faultmaven.core.investigation.kb_push import visible_kb_context
from faultmaven.core.investigation.prompts.fence import (
    PromptFence,
    delimiter_overhead_chars,
    render_fenced,
    reseal,
    split_fenced,
    terminate_dangling,
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
    is_server_written_assistant_row,
    is_server_written_user_row,
)
from faultmaven.modules.case.domain.models import CauseState

from .budget import (
    _SECTION_DROPPED_MARKER,
    _SILENT_DROP_MAX_TOKENS,
    EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM,
    EVIDENCE_CONTEXT_MAX_TOTAL_CHARS,
    EVIDENCE_CONTEXT_RECENT_COUNT,
    HISTORY_AGENT_TRUNCATE_THRESHOLD,
    HISTORY_SUMMARY_MAX_TURNS,
    HISTORY_VERBATIM_TURNS,
    KB_MAX_SOLUTION_CHARS,
    SEARCHABLE_STRUCTURAL_INDEX_MIN_CHARS,
    STATE_SUMMARY_DIGEST_CHARS,
    STATE_SUMMARY_MAX_EVIDENCE_DIGESTS,
    STATE_SUMMARY_MAX_HYPOTHESES,
    STATE_SUMMARY_TURN_THRESHOLD,
    TokenBudget,
    _load_context_caps,
    get_token_budget_for_provider,
    structural_index_is_searchable,
)
from .causal_graph_block import _build_causal_graph_block
from .entity_highlights import (
    _ENTITY_HIGHLIGHTS_PREAMBLE,
    _HIGHLIGHT_PER_TYPE_LIMIT,
    _HIGHLIGHT_TYPES,
    EntityHighlightGroup,
    EntityHighlightRow,
    _render_entity_highlights,
    fetch_entity_highlights,
)
from .evidence import (
    _STRUCTURAL_WHITESPACE,
    _TIER_A_DELIMITERS,
    _TIER_A_MARKUP_OVERHEAD_CHARS,
    _TIME_POINT_PATTERN,
    _TIME_RANGE_PATTERNS,
    _attr,
    _build_evidence_context,
    _build_hash_first_seen,
    _coverage_overlaps_window,
    _current_turn_reserve_fraction,
    _effective_evidence_char_budget,
    _evidence_data_turn,
    _evidence_label,
    _evidence_recency_key,
    _extract_time_window_from_query,
    _file_observed_attr,
    _fresh_this_turn_attr,
    _identical_to_prior_attr,
    _label_attr,
    _observed_attr,
    _open_evidence_collected,
    _parse_time_token,
    _render_evidence_block,
    _render_orphan_file_block,
    _render_problem_context,
    _safe_name,
    _score_evidence_for_tier_a,
    _symptom_currency_note,
)
from .evidence_needs import (
    _EXHAUSTED_SECTION_HEADER,
    _build_candidate_solutions_block,
    _build_evidence_needs_block,
    _render_finding_line,
    _render_need_line,
)
from .history import (
    _EVIDENCE_NEEDS_RENDER_CAP,
    _PREVIEW_CAP,
    _PRIORITY_ORDER,
    _REQUEST_TEXT_RENDER_CAP,
    ASIDE_LINE,
    NO_ANSWER_LINE,
    _aside_turns,
    _build_compact_history,
    _build_graduated_history,
    _build_state_summary,
    _build_turn_summary,
    _build_verbatim_history,
    _fence_conversation,
    _preview_turn_from_messages,
    _render_ask_history,
    _truncate_request_text,
)
from .text_shaping import (
    _RERANK_HEADING_PATTERNS,
    _RERANK_STOPWORDS,
    _TRUNCATION_MARKER,
    SanitizedInput,
    _confidence_marker,
    _format_file_meta,
    _parse_extract,
    _rerank_page_capture_sections,
    _smart_truncate_agent_response,
    _split_rerank_sections,
    _trim_to_sentence,
    _trim_to_sentence_end,
    sanitize_user_input,
)

logger = logging.getLogger(__name__)


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


#: Fallback when settings are unavailable. One definition, because the cap is
#: read at two altitudes now: ``build_investigation_context`` applies it before
#: the fence, and ``_allocate_sections`` counts the result.
_USER_MESSAGE_CAP_FALLBACK = 4000


def _user_message_cap() -> int:
    """``prompt_budget.user_message_max_tokens``, or the fallback."""
    try:
        from faultmaven.config.settings import get_settings

        return get_settings().prompt_budget.user_message_max_tokens
    except Exception:
        return _USER_MESSAGE_CAP_FALLBACK


def _shrink_fenced_tail(fenced: str, alloc: int, budget: "TokenBudget") -> str:
    """Shrink a fenced block to ``alloc`` tokens, keeping its BODY's tail.

    The conversation section is the one variable section whose value is at its
    END — the latest turn — so it is sized with ``keep="tail"``. Cutting the
    RENDERED element that way removes its opening delimiter, and below the ~40
    characters of the CLOSING delimiter it removes that too, at which point
    neither is intact and ``reseal`` can only drop the section (#1256 review).
    That drop is the worst possible outcome here: it violates
    ``_truncate_to``'s INV-4 ("never silently dropped") and it takes the most
    recent turn with it, under exactly the budget pressure where continuity
    matters most.

    So the delimiters are reserved FIRST and the remaining allotment buys body.
    Both are then present by construction rather than by repair, and the
    content gets the room the delimiters were otherwise consuming.

    ``terminate_dangling`` is re-applied because the surviving tail is a
    different string from the body the fence terminated. Cutting the HEAD can
    only remove a ``<``, never add one, so this is only ever a no-op or a
    correction — never a new hole.
    """
    parts = split_fenced(fenced)
    if parts is None:  # not a fenced element — size it as an ordinary section
        return budget._truncate_to(fenced, alloc, keep="tail")
    opening, body, closing = parts
    room = alloc - budget.count(opening) - budget.count(closing)
    if room <= _SILENT_DROP_MAX_TOKENS:
        # No room for even a token of body. Emit the same non-silent marker
        # ``_truncate_to`` would, rather than a pair of empty delimiters or
        # nothing at all: it carries no caller bytes and no fenced delimiter,
        # so there is nothing to forge with and nothing to absorb.
        return _SECTION_DROPPED_MARKER
    kept = budget._truncate_to(body, room, keep="tail")
    if not kept:
        return _SECTION_DROPPED_MARKER
    return f"{opening}\n{terminate_dangling(kept)}\n{closing}"


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
    user_message_block: str,
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
        feedback_cap = pb.system_feedback_max_tokens
        journal_cap = pb.journal_max_tokens
        conversation_cap = pb.conversation_history_max_tokens
    except Exception:
        feedback_cap, journal_cap = 1500, 1500
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

    # ``user_message_block`` arrives already capped AND already fenced (the cap
    # runs before the fence in ``build_investigation_context``, or it would cut
    # the rendered element), so it is only counted here.
    capped_feedback = _cap_text_tokens(
        feedback_str, feedback_cap, provider_name, model_name
    )
    ctx["identity"] = _reserve(identity)
    ctx["core_context"] = _reserve(core_context)
    ctx["milestones"] = _reserve(milestones_str)
    ctx["inquiry_state"] = _reserve(inquiry_state_str)
    ctx["pending_action"] = _reserve(pending_action_str)
    ctx["system_feedback"] = _reserve(capped_feedback)
    ctx["user_message"] = _reserve(user_message_block)

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
                rendered = _shrink_fenced_tail(compact_history, alloc, budget)
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
            # A fenced section loses its CLOSING delimiter and its terminator
            # to head truncation, which reopens #1217 downstream of every
            # check that would have caught it — see :func:`reseal`. Re-closing
            # costs a few tokens over ``alloc``; the whole-prompt accountant's
            # margin absorbs that, and the recount below keeps the running
            # total honest.
            rendered = reseal(rendered, text)

        has_content = bool(
            (graduated_history or compact_history)
            if key == "conversation_history"
            else text
        )
        if has_content and not rendered:
            # INV-4, keyed on the OUTCOME (#610): a section that had content
            # and rendered as nothing carries a marker, whatever emptied it.
            # Three things can, and a threshold would have to predict all of
            # them: an allotment of 0 (pass B had nothing left);
            # ``_truncate_to`` below its floor (it cannot fit even its marker
            # at <= 2 tokens, so returns ""); and ``reseal`` refusing a head
            # cut that landed inside a fenced section's OPENING delimiter —
            # which, for a section with a preamble before its fenced element
            # (``entity_highlights``), is every allotment up to the preamble
            # plus the opening tag, dozens of tokens, not two. The sections
            # that reach these states under a small target include
            # ``hypotheses``, ``candidate_solutions`` and
            # ``working_conclusion``, engine state the model is asked to
            # UPDATE, which absent and unmarked read as "none exist".
            #
            # The marker is ``_truncate_to``'s own bare ``[...]``, carrying no
            # caller bytes and no delimiter. Charged to the margin, not to
            # ``remaining``: a section ends up empty only when its allotment
            # could not hold its content, so there is nothing to take the
            # marker's tokens from. 1-2 tokens per section (measured), under
            # 20 across all nine, against ``overhead_margin_tokens`` (256);
            # ``used_tokens`` below counts them, so the running total stays
            # honest.
            rendered = _SECTION_DROPPED_MARKER

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

    # --- 3. No section may end part-way through a tag (#1254, #1256) ---
    #
    # Forgery and absorption are different questions and only the first is
    # about authorship. Every section below is renderer-, engine- or
    # model-authored, so none of them can FORGE a fenced delimiter — but any of
    # them can SWALLOW one. A section ending in ``<uploaded_file file_id="…``
    # with no ``>`` absorbs whatever comes next in the assembled prompt, and
    # after #1256 what comes next may be ``<conversation_history fence="…">``
    # or ``<user_message fence="…">``: the half-written tag then carries the
    # live token, which is exactly the hole #1254 closed on the fallback.
    #
    # Applied to EVERY section, not to the ones that are adjacent today: a
    # section can render empty, which promotes the one before it to adjacent.
    # ``render_fenced``'s own checks cannot cover this — they run on the fenced
    # parts, before the allocator lays the prompt out. Cost is zero except on
    # the shape that is mid-forgery, and a fenced section that survived
    # ``reseal`` ends in its closing delimiter, so this is a no-op there.
    for _key, _text in list(ctx.items()):
        guarded = terminate_dangling(_text)
        if guarded is not _text:
            ctx[_key] = guarded
            budget.used_tokens += budget.count(guarded) - budget.count(_text)

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


def system_feedback_block(
    case: Case, guard: Optional[Callable[[str], str]] = None
) -> str:
    """The previous turn's ``system_feedback`` as a prompt block, or ``""``.

    The one reader of the channel, shared by the main prompt and the minimal
    fallback (``templates._fallback_body``), so a turn that degrades to the
    fallback still delivers the notice (#1688). It reads the LAST record only:
    a turn that built no prompt forwards the notice onto its own record
    (``milestone_engine.record_promptless_turn``), so the last record is the
    one carrying whatever no prompt has rendered yet.

    The heading names where the notice came from. Normally that is the
    previous turn. A forwarded copy belongs to an earlier one, and the
    conversation history above ends on the exchanges that carried it, so
    "previous turn" would point the model at the wrong exchange. The walk
    back finds the nearest record that wrote it, stepping over forwarded copies
    and over any record not carrying it, such as a SKIPPED placeholder that
    ``Case.reconcile_turn_sequence`` backfilled for an interrupted turn.

    ``guard`` wraps the notice text alone; the fallback passes its
    ``_guarded``. Wrapping the finished block instead would put a terminator
    after the block's trailing blank line, on the same line as whatever the
    template renders next.
    """
    last = case.turn_history[-1] if case.turn_history else None
    if last is None or not last.system_feedback:
        return ""
    notice = last.system_feedback
    body = guard(notice) if guard else notice
    if not last.system_feedback_forwarded:
        return f"IMPORTANT - SYSTEM FEEDBACK FROM PREVIOUS TURN:\n{body}\n\n"
    origin = next(
        (
            record
            for record in reversed(case.turn_history[:-1])
            if record.system_feedback == notice and not record.system_feedback_forwarded
        ),
        None,
    )
    source = f"TURN {origin.turn_number}" if origin else "AN EARLIER TURN"
    return (
        f"IMPORTANT - SYSTEM FEEDBACK FROM {source} "
        f"(not shown to you until now):\n{body}\n\n"
    )


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
    entity_highlight_groups: Optional[Sequence["EntityHighlightGroup"]] = None,
    tools_available: bool = False,
) -> Dict[str, str]:
    """
    Gather and format context elements within token budget.

    **One fence token per assembly (#1228, widened in #1256).**
    ``<problem_context>``, ``<entity_highlights>``, ``<evidence_collected>``,
    ``<conversation_history>`` and ``<user_message>`` are the prompt's
    caller-controlled blocks; all five are rendered inside a single
    :func:`~faultmaven.core.investigation.prompts.fence.render_fenced`, so one
    token governs the prompt and one declaration names it. A token per block
    was rejected: it turns the rule the model must follow from one anchor into
    an N-entry token→block binding table, and it lets content in one block
    forge another block's opening tag with a GENUINE token. See :mod:`.fence`.

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
    # Bound and inspect user input (Gap #9). Its BYTES are untouched — the
    # fence below is what makes them structurally inert (#1256).
    sanitized_input = sanitize_user_input(user_message)
    if sanitized_input.warnings:
        logger.warning(
            f"Input sanitization warnings for case {case.case_id}: {', '.join(sanitized_input.warnings)}"
        )
    user_message_safe = sanitized_input.content

    # Which conversation fidelity the fuller slot renders. Resolved here rather
    # than at the section-build site below because the fenced render needs it.
    if use_state_summary is None:
        use_state_summary = case.current_turn > STATE_SUMMARY_TURN_THRESHOLD

    # The user-message cap runs BEFORE the fence, never after: capping a
    # rendered element would cut its closing delimiter and its terminator.
    capped_user = _cap_text_tokens(
        user_message_safe, _user_message_cap(), provider_name, model_name
    )

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

        # The stage is declared once, by the identity block above
        # (``CURRENT_STAGE: {enum}``). It used to be repeated here under a
        # display name, so the model was told the stage was ``DIAGNOSIS`` and
        # then, lower down, that it was ``Investigating`` — one tag name, two
        # vocabularies, reading as self-contradiction. The identity block is a
        # reserved section and is never trimmed, so dropping this loses no
        # stage information on any path (#1075).
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
    # --- ONE fence for the whole assembly (#1228, widened in #1256) ---
    # ``<problem_context>``, ``<entity_highlights>``, ``<evidence_collected>``,
    # ``<conversation_history>`` and ``<user_message>`` are the prompt's
    # caller-controlled blocks. They are rendered together under a single
    # ``render_fenced`` so ONE token is live per prompt: the model gets one
    # anchor to read it from, and a forgery in any of them cannot carry a
    # genuine token borrowed from another. The collision corpus is
    # correspondingly the union of every block's channels, so the token is
    # provably absent from every caller-controlled string in the prompt rather
    # than from one block's own.
    #
    # BOTH conversation fidelities are rendered here, because the allocator
    # picks between them by budget AFTER the fence has been verified; a
    # fidelity rendered outside the fence would be the one channel the corpus
    # never saw.
    #
    # The callable may run more than once (a re-mint re-renders), so it must
    # stay pure — which is why the entity rows are FETCHED by the caller and
    # only FORMATTED here.
    _fenced: Dict[str, str] = {}

    def _render_caller_controlled_blocks(fence: PromptFence) -> str:
        _fenced["core_context"] = _render_problem_context(case, fence)
        _fenced["entity_highlights"] = _render_entity_highlights(
            entity_highlight_groups, fence
        )
        _fenced["evidence"] = _build_evidence_context(
            case,
            processing_mode=processing_mode,
            user_query=user_message_safe,
            provider_name=provider_name,
            model_name=model_name,
            char_budget_override=evidence_char_override,
            tools_available=tools_available,
            fence=fence,
        )
        # Conversation history — two fidelities, both fenced. ``floor`` is the
        # compact one, which always carries the latest turn; ``full`` is the
        # graduated transcript, or the SAME compact string once the case is
        # long enough to have switched to the state summary. Named for what
        # they hold rather than for the fidelity they usually hold: calling the
        # upper rung "graduated" while it holds compact content collapses two
        # rungs of the ladder invisibly. Built once and shared in that case, so
        # a re-mint re-renders it once rather than twice.
        _fenced["conversation_floor"] = _build_compact_history(
            case, user_message_safe, fence
        )
        _fenced["conversation_full"] = (
            _fenced["conversation_floor"]
            if use_state_summary
            else _build_graduated_history(case, fence)
        )
        _fenced["user_message"] = (
            fence.element("user_message", capped_user) if capped_user else ""
        )
        # Checked as one corpus; the parts are what the templates interpolate.
        return "\n\n".join(part for part in _fenced.values() if part)

    render_fenced(_render_caller_controlled_blocks)
    core_context = _fenced["core_context"]
    evidence_str = _fenced["evidence"]

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
        conclusion_str += f"CONFIDENCE: {wc.likelihood * 100:.0f}%\n"
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

    # 6. Conversation History — rendered inside the shared fence above, in two
    # modes:
    # - State Summary (>15 turns): Minimal summary + last turn only
    # - Graduated History (≤15 turns): Recent turns verbatim, older summarized
    # The state-summary mode used to be an inline second copy of
    # ``_build_compact_history``; it is now that function, so there is one
    # place the conversation channel is fenced rather than two (#1256).
    recent_history = _fenced["conversation_full"]

    # 7. Knowledge Base Results
    # Cap individual solution text to prevent a single verbose runbook from
    # consuming the remaining token budget.
    # KB context: combine passed-in results with case-level pre-fetched context.
    #
    # ``case.kb_context`` is the PUSH channel (fm#1360) — runbooks the engine
    # handed the model unasked, via ``MilestoneEngine._prefetch_kb_context``.
    # ``KB_PREFETCH_ENABLED`` governs it, and THIS is the seam where the policy
    # has to bite: the flag's whole claim is that the block is absent from the
    # rendered prompt, and a producer-side guard alone cannot make that true for
    # a case reloaded with context already persisted on it.
    #
    # ``kb_results`` (the parameter) is deliberately NOT gated. It is a
    # caller-supplied channel, not the pre-fetch, and the flag is scoped to the
    # push. It is ``None`` at the only production call site today, so the block
    # below is the pre-fetch and nothing else — but scoping the gate to the
    # field it names keeps that true if a caller ever starts passing results.
    #
    # Read through ``visible_kb_context`` rather than off the case: the same
    # gate has to hold for the turn response's ``sources`` and the ``case_turn``
    # telemetry, and three copies of one predicate is how two of them ended up
    # without it.
    all_kb_results = list(kb_results or [])
    all_kb_results.extend(visible_kb_context(case))

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
            # Several chunks of ONE runbook can now be rendered, and they share
            # its title; the section is what distinguishes them. Absent on an
            # entry written by an older build, which renders as it always did.
            section = res.get("section", "")
            if section:
                title = f"{title} — {section}"
            trigger = res.get("trigger", "")
            trigger_label = f" [matched on {trigger}]" if trigger else ""
            if len(solution) > KB_MAX_SOLUTION_CHARS:
                solution = solution[:KB_MAX_SOLUTION_CHARS] + "... [truncated]"
            if title:
                kb_str += f"MATCH {i + 1}: {title}{trigger_label}\n"
            if summary:
                kb_str += f"  {summary}\n"
            if solution:
                kb_str += f"  SOLUTION: {solution}\n"
            kb_str += "\n"
        kb_str += "</knowledge_context>"

    # 8. System Feedback (Validation errors from previous turn)
    feedback_str = system_feedback_block(case)

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
                        hypothesis_str += f"- [{h.hypothesis_id}] {h.statement} (Confidence: {h.likelihood * 100:.0f}%, State: {h.state.value})\n"
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
                        f"- [{h.hypothesis_id}] {h.statement} "
                        f"(Confidence: {h.likelihood * 100:.0f}%)\n"
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
                hypothesis_str = f"<working_hypotheses>\n- [{best.hypothesis_id}] {best.statement} (Confidence: {best.likelihood * 100:.0f}%, VALIDATED)\n</working_hypotheses>"
            else:
                hypothesis_str = ""

    # 10. INQUIRY State — surfaces an unconfirmed proposed_problem_statement
    # to the LLM with ONE rule, not a fork.
    #
    # There used to be two modes. NOT_YET_CONFIRMED told the LLM "do NOT
    # re-propose it"; HANDSHAKE_DEFERRED, on the single turn after a same-turn
    # guard fire, told it the opposite — "RE-PRESENT the statement verbatim".
    # Both existed because PRESENTING the statement was the LLM's job, so the
    # prompt had to say, turn by turn, whether this was a presenting turn.
    #
    # It is the engine's job now (#1607): on every Gate-1-pending turn the
    # engine composes the standing statement into the reply alongside the
    # confirm/refine pair, so the statement is on screen whenever the user is
    # asked to confirm it — which is what INV-01 required all along and what
    # the prompt could not be relied on to deliver. With presentation
    # guaranteed, the fork collapses: the LLM never presents, and the flag
    # that selected between the two modes is gone with it.
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
                # State fact plus one directive. Deliberately NOT present-tense
                # about the user ("the user has not confirmed") — that is false
                # on the very turn they do, and confirmation detection lives in
                # the static TWO-STEP CONFIRMATION prose plus the
                # user_confirmed_investigation schema field.
                inquiry_state_str += (
                    "ENGINE_PRESENTS_THIS: You proposed this statement on an "
                    "earlier turn (unconfirmed going into this turn). The ENGINE "
                    "appends it to your reply and offers the confirm/refine "
                    "buttons, so do NOT restate it and do NOT ask for "
                    "confirmation yourself — the user would be asked twice. "
                    "Answer the user's current message. If your understanding "
                    "of the problem has CHANGED, write the revised statement "
                    "to proposed_problem_statement; the engine will present the "
                    "new wording, and you must not set "
                    "user_confirmed_investigation on that same turn.\n"
                )
            inquiry_state_str += "</inquiry_state>"

    # Phase 4c — entity highlights block. Rows pre-fetched by the milestone
    # engine from the Phase 4 ``case_entities`` registry, formatted above
    # inside the shared fence. Empty string when the flag is off, the fetch
    # failed, or the case has no extracted entities — safe to always include
    # the key so templates can reference it unconditionally.
    entity_highlights_str = _fenced["entity_highlights"]

    # Evidence-needs Phase 4 — demand-side pool block. Empty string when
    # the pool has no visible needs for this stage (progressive
    # activation; design §10.6).
    evidence_needs_str = _build_evidence_needs_block(case)

    # R9 — candidate-solution priors for LEGACY seeded cases (see
    # ``seeded_provenance``): empty for every case opened after 2026-09-02.
    candidate_solutions_str = _build_candidate_solutions_block(case)

    # =====================================================================
    # Budget allocation
    # =====================================================================
    # Priority-greedy allocator (the token-budget allocation model). Needs both
    # history fidelities so it can pick the one that fits; the compact one is the
    # continuity floor (always carries the latest turn). Both were rendered
    # inside the shared fence above.
    compact_history = _fenced["conversation_floor"]
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
        user_message_block=_fenced["user_message"],
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
