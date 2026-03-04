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

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    EvidenceForm,
    InvestigationStage,
)

# =============================================================================
# Evidence Context Sliding Window Configuration
# =============================================================================
# How many recent data evidence items get full structural_index (Tier A)
EVIDENCE_CONTEXT_RECENT_COUNT = 3
# Max chars per Tier A evidence item's structural_index
EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM = 4000
# Max total chars for the entire evidence context section
EVIDENCE_CONTEXT_MAX_TOTAL_CHARS = 16000

# =============================================================================
# Graduated Conversation History Configuration
# =============================================================================
# Recent turns: full user messages + smart-truncated agent responses
HISTORY_VERBATIM_TURNS = 3
# Older turns: one-line summaries from TurnProgress metadata
HISTORY_SUMMARY_MAX_TURNS = 7
# Agent response character count before smart-truncation kicks in
HISTORY_AGENT_TRUNCATE_THRESHOLD = 600

logger = logging.getLogger(__name__)

_TRUNCATION_MARKER = "[...analysis removed for brevity...]"


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
    """Simple character-based token approximation (1 token ~= 4 chars)"""

    def __init__(self, limit_tokens: int = 8000):
        self.limit_chars = limit_tokens * 4
        self.used_chars = 0

    def has_budget(self, text: str) -> bool:
        return self.used_chars + len(text) <= self.limit_chars

    def use(self, text: str) -> str:
        if self.has_budget(text):
            self.used_chars += len(text)
            return text
        else:
            # Truncate if partially fits
            remaining = self.limit_chars - self.used_chars
            if remaining > 100:
                truncated = (
                    text[: remaining - 50]
                    + "\n[... Content truncated due to context limit ...]"
                )
                self.used_chars = self.limit_chars
                return truncated
            return ""


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
    if case.status.value == "investigating" and case.current_stage:
        stage = case.current_stage.value.upper()
    elif case.status.value in ["resolved", "closed"]:
        stage = case.status.value.upper()

    # Verification status
    verified_items = []
    if case.progress:
        p = case.progress
        if p.symptom_verified:
            verified_items.append("symptom")
        if p.scope_assessed:
            verified_items.append("scope")
        if p.timeline_established:
            verified_items.append("timeline")
        if p.changes_identified:
            verified_items.append("changes")

    verified = ", ".join(verified_items) if verified_items else "none"

    # Active hypothesis
    active_h = [
        h for h in case.hypotheses.values() if h.status.value in ["active", "validated"]
    ]
    if active_h:
        best_h = max(active_h, key=lambda h: h.likelihood)
        hypothesis_str = (
            f"{best_h.statement[:80]} ({best_h.likelihood*100:.0f}% confidence)"
        )
    else:
        hypothesis_str = "None yet"

    # Evidence count
    evidence_count = len(case.evidence)
    evidence_str = (
        f"{evidence_count} artifacts analyzed"
        if evidence_count > 0
        else "No evidence collected"
    )

    # Turn metrics
    turns_total = case.current_turn
    turns_since_progress = case.turns_without_progress

    summary = f"""<state_summary>
Investigation: {problem_desc}
Stage: {stage}
Verified: {verified}
Active Hypothesis: {hypothesis_str}
Evidence: {evidence_str}
Turns: {turns_total} total, {turns_since_progress} since last progress
</state_summary>"""

    return summary


def _build_evidence_context(case: Case) -> str:
    """
    Build the evidence context section using a three-tier sliding window.

    This replaces the simple last-10 evidence list with a tiered system that
    includes structural indexes for recent data evidence, fixing the
    "I don't have access to file content" bug.

    Tier A: Last N data evidence items (form=DOCUMENT or SUBMITTED_DATA)
            → Include preprocessed_content (structural index), capped per item.
    Tier B: Older data evidence → summary only.
    Tier C: USER_TEXT evidence → summary only, always.

    Token budget: ~4000 tokens dedicated. Worst case: 3 Tier A items x 4000
    chars = 12,000 chars (~3000 tokens).
    """
    if not case.evidence:
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

    # Separate evidence by form for tiered treatment
    data_evidence = []  # DOCUMENT or SUBMITTED_DATA
    text_evidence = []  # USER_TEXT
    for ev in case.evidence:
        if ev.form in (EvidenceForm.DOCUMENT, EvidenceForm.SUBMITTED_DATA):
            data_evidence.append(ev)
        else:
            text_evidence.append(ev)

    # Split data evidence into recent (Tier A) and older (Tier B)
    tier_a = data_evidence[-EVIDENCE_CONTEXT_RECENT_COUNT:]
    tier_b = (
        data_evidence[:-EVIDENCE_CONTEXT_RECENT_COUNT]
        if len(data_evidence) > EVIDENCE_CONTEXT_RECENT_COUNT
        else []
    )

    result = "<evidence_collected>\n"
    total_chars = 0

    # Tier A: Recent data evidence with structural index
    for ev in tier_a:
        structural_index = ev.preprocessed_content or ""
        truncated = False

        # Per-item cap
        if len(structural_index) > EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM:
            remaining_chars = (
                len(structural_index) - EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM
            )
            structural_index = structural_index[:EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM]
            truncated = True

        # Total budget cap
        entry_estimate = (
            len(structural_index) + len(ev.summary or "") + 200
        )  # overhead for XML tags
        if total_chars + entry_estimate > EVIDENCE_CONTEXT_MAX_TOTAL_CHARS:
            # Downgrade remaining Tier A to Tier B (summary only)
            tier_b.append(ev)
            continue

        data_type_attr = f' data_type="{ev.data_type}"' if ev.data_type else ""
        filename_attr = ""
        if ev.source_file_id and str(ev.source_file_id) in file_lookup:
            filename_attr = f' filename="{file_lookup[str(ev.source_file_id)]}"'
        result += f'  <evidence id="{ev.evidence_id}" form="{ev.form.value}"{data_type_attr}{filename_attr}>\n'
        result += f"    <summary>{ev.summary}</summary>\n"
        if structural_index.strip():
            result += "    <structural_index>\n"
            result += structural_index
            if truncated:
                result += f"\n[TRUNCATED: {remaining_chars:,} more characters. Use search_file or read_file to query the full content.]"
            result += "\n    </structural_index>\n"
        result += "  </evidence>\n"
        total_chars += entry_estimate

    # Tier B: Older data evidence (summary only)
    for ev in tier_b:
        filename_attr = ""
        if ev.source_file_id and str(ev.source_file_id) in file_lookup:
            filename_attr = f' filename="{file_lookup[str(ev.source_file_id)]}"'
        entry = (
            f'  <evidence id="{ev.evidence_id}" form="{ev.form.value}"{filename_attr}>'
        )
        entry += f"<summary>{ev.summary}</summary></evidence>\n"
        if total_chars + len(entry) > EVIDENCE_CONTEXT_MAX_TOTAL_CHARS:
            break
        result += entry
        total_chars += len(entry)

    # Tier C: USER_TEXT evidence (summary only, always)
    for ev in text_evidence[-5:]:  # Cap at 5 most recent text items
        filename_attr = ""
        if ev.source_file_id and str(ev.source_file_id) in file_lookup:
            filename_attr = f' filename="{file_lookup[str(ev.source_file_id)]}"'
        entry = (
            f'  <evidence id="{ev.evidence_id}" form="{ev.form.value}"{filename_attr}>'
        )
        entry += f"<summary>{ev.summary}</summary></evidence>\n"
        if total_chars + len(entry) > EVIDENCE_CONTEXT_MAX_TOTAL_CHARS:
            break
        result += entry
        total_chars += len(entry)

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


def build_investigation_context(
    case: Case,
    user_message: str,
    kb_results: Optional[List[Dict[str, Any]]] = None,
    max_tokens: Optional[int] = None,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    use_state_summary: Optional[bool] = None,
    enable_stage_specific_loading: bool = True,
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

    budget = TokenBudget(max_tokens)

    # 1. Identity & Status (Gap #8: XML tags for better LLM attention)
    identity = f"<case_identity>\n"
    identity += f"CASE_ID: {case.case_id}\n"
    identity += f"STATUS: {case.status.value.upper()}\n"
    if case.status == CaseStatus.INVESTIGATING and case.current_stage:
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
    if case.status == CaseStatus.INVESTIGATING:
        p = case.progress

        # Stage-gate milestones (drive transitions)
        stage_gates = {
            "mitigation_accepted": p.mitigation_accepted,
            "mitigation_verified": p.mitigation_verified,
            "solution_accepted": p.solution_accepted,
            "solution_verified": p.solution_verified,
        }
        active_gates = [k for k, v in stage_gates.items() if v]

        # Progress indicators (LLM context)
        indicators = {
            "symptom_verified": p.symptom_verified,
            "scope_assessed": p.scope_assessed,
            "timeline_established": p.timeline_established,
            "changes_identified": p.changes_identified,
            "root_cause_identified": p.root_cause_identified,
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
                milestones_str += f"- {ind}\n"
            milestones_str += "</progress_indicators>"
        else:
            milestones_str += "<progress_indicators>None yet</progress_indicators>"

    # 4. Evidence Context (Sliding Window)
    # Three-tier system: Tier A (recent data with structural index),
    # Tier B (older data, summary only), Tier C (user text, summary only).
    # Fixes "I don't have access to file content" bug by including
    # structural indexes in the LLM context for recent evidence.
    evidence_str = _build_evidence_context(case)

    # 5. Hypothesis Summary
    hypothesis_str = ""
    active_h = [h for h in case.hypotheses.values() if h.status.value != "retired"]
    if active_h:
        hypothesis_str = "<working_hypotheses>\n"
        for h in active_h:
            hypothesis_str += f"- {h.statement} (Confidence: {h.likelihood*100:.0f}%, Status: {h.status.value})\n"
        hypothesis_str += "</working_hypotheses>"

    # 5b. Pending ProposedAction (Framework §4.1: LLM needs this to detect compliance)
    pending_action_str = ""
    if case.proposed_actions:
        for action in reversed(case.proposed_actions):
            if action.status == "pending":
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
                pending_action_str += "</pending_action>"
                break

    # 6. Conversation History
    # Two modes:
    # - State Summary (>15 turns): Minimal summary + last turn only
    # - Graduated History (≤15 turns): Recent turns verbatim, older summarized
    if use_state_summary is None:
        use_state_summary = case.current_turn > 15

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
    kb_str = ""
    if kb_results:
        kb_str = "<knowledge_base_matches>\n"
        for i, res in enumerate(kb_results[:3]):  # Top 3
            kb_str += f"MATCH {i+1} ({res.get('type')}): {res.get('summary')}\n"
            kb_str += f"SOLUTION: {res.get('solution')}\n\n"
        kb_str += "</knowledge_base_matches>"

    # 8. System Feedback (Validation errors from previous turn)
    feedback_str = ""
    if case.turn_history:
        last_turn = case.turn_history[-1]
        if last_turn.system_feedback:
            feedback_str = f"IMPORTANT - SYSTEM FEEDBACK FROM PREVIOUS TURN:\n{last_turn.system_feedback}\n\n"

    # 9. Stage-Specific Context Loading (Gap #10: Section 11.4)
    # Optimize context by skipping irrelevant sections based on investigation stage
    if enable_stage_specific_loading and case.status == CaseStatus.INVESTIGATING:
        stage = case.current_stage or InvestigationStage.DIAGNOSIS

        if stage == InvestigationStage.DIAGNOSIS:
            # DIAGNOSIS covers symptom verification, hypothesis work, and solution proposal
            logger.debug(
                f"Stage-specific loading: DIAGNOSIS - full context for diagnosis"
            )
            # All context sections are relevant during diagnosis

        elif stage == InvestigationStage.MITIGATION:
            # MITIGATION: Focus on the proposed mitigation and verification
            logger.debug(
                f"Stage-specific loading: MITIGATION - focusing on mitigation verification"
            )
            # Hypotheses are less important during mitigation

        elif stage == InvestigationStage.TREATMENT:
            # TREATMENT: Focus on solution verification, extended diagnosis if fix fails
            logger.debug(
                f"Stage-specific loading: TREATMENT - focusing on solution verification"
            )
            # All context sections relevant (may need extended diagnosis)

    # 10. INQUIRY State (prevents blind re-proposal of already-proposed problem statements)
    inquiry_state_str = ""
    if case.status == CaseStatus.INQUIRY and case.inquiry:
        inq = case.inquiry
        if inq.proposed_problem_statement and inq.proposed_problem_statement.strip():
            inquiry_state_str = "<inquiry_state>\n"
            inquiry_state_str += (
                f"PROPOSED_PROBLEM_STATEMENT: {inq.proposed_problem_statement}\n"
            )
            inquiry_state_str += f"CONFIRMED: {inq.problem_statement_confirmed}\n"
            if not inq.problem_statement_confirmed:
                inquiry_state_str += (
                    "AWAITING_CONFIRMATION: You already proposed this problem statement. "
                    "If the user's message shows engagement with the problem (uploading data, "
                    "asking about the issue, referencing the problem, or any affirmative response), "
                    "treat it as implicit confirmation and set user_confirmed_investigation=True. "
                    "Do NOT re-ask for confirmation if you already asked in a previous turn.\n"
                )
            inquiry_state_str += "</inquiry_state>"

    # Assembly with budget check
    ctx = {
        "identity": budget.use(identity),
        "core_context": budget.use(core_context),
        "milestones": budget.use(milestones_str),
        "evidence": budget.use(evidence_str),
        "hypotheses": budget.use(hypothesis_str),
        "pending_action": budget.use(pending_action_str),
        "kb_results": budget.use(kb_str),
        "system_feedback": feedback_str,  # Prioritize feedback
        "conversation_history": budget.use(recent_history),
        "user_message": user_message_safe,  # Sanitized user message always included
        "inquiry_state": budget.use(inquiry_state_str),
    }

    return ctx
