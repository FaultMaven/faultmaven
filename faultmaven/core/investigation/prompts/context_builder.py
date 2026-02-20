"""Investigation Prompt Context Builder

This module handles gathering and truncating investigation context for LLM prompts,
ensuring we stay within token limits while preserving high-priority information.

Priority:
1. System Prompt & Response Schema (Fixed)
2. Case Definition & Core Identity
3. Recent Conversation History (Last N turns)
4. Knowledge Base Search Results
5. Detailed Evidence Summaries
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

from faultmaven.modules.case.contracts import Case, CaseStatus, InvestigationStage

logger = logging.getLogger(__name__)


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

    # 4. Evidence Summary
    # Note: Evidence IDs removed from LLM context (category-based validation)
    # Evidence is created AFTER LLM response, so IDs don't exist yet
    evidence_str = "<evidence_collected>\n"
    if case.evidence:
        for i, ev in enumerate(case.evidence[-10:]):  # Last 10 evidence items
            evidence_str += f"- [{ev.category.value}] {ev.summary}\n"
    else:
        evidence_str += "No formal evidence collected yet.\n"
    evidence_str += "</evidence_collected>"

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
                pending_action_str = "<pending_action>\n"
                pending_action_str += (
                    f"ACTION_TYPE: {action.action_type.value.upper()}\n"
                )
                pending_action_str += f"DESCRIPTION: {action.description}\n"
                pending_action_str += f"PROPOSED_IN_TURN: {action.proposed_in_turn}\n"
                pending_action_str += (
                    "When the user submits results of executing this action, "
                    "set the corresponding stage-gate milestone in your milestones output.\n"
                )
                pending_action_str += "</pending_action>"
                break

    # 6. Conversation History
    # Gap #8: State Summary + Last Turn pattern for long conversations
    # Use compact state summary for conversations >15 turns to save tokens
    # Auto-enable state summary mode for long conversations
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
            if last_turn.agent_summary:
                summary = last_turn.agent_summary[:200]
                recent_history += f"Agent: {summary}\n"

            recent_history += "</previous_turn>"

        # Current turn (using sanitized input)
        recent_history += "\n\n<current_turn>\n"
        recent_history += f"User: {user_message_safe}\n"
        recent_history += "</current_turn>"

    else:
        # Full conversation history for short conversations (<15 turns)
        recent_history = "<conversation_history>\n"

        # Get recent messages (last 20 messages = ~10 turns)
        recent_messages = case.messages[-20:] if case.messages else []

        if not recent_messages:
            recent_history += "No previous conversation.\n"
        else:
            # Group messages by turn for readability
            current_turn_num = None
            for msg in recent_messages:
                turn_num = msg.get("turn_number", "?")
                role = msg.get("role", "unknown").upper()
                content = msg.get("content", "")

                if not content:
                    continue  # Skip empty messages

                # Add turn header when starting a new turn
                if turn_num != current_turn_num:
                    if current_turn_num is not None:
                        recent_history += "\n"
                    recent_history += f"TURN {turn_num}:\n"
                    current_turn_num = turn_num

                recent_history += f"{role}: {content}\n"

        recent_history += "</conversation_history>"

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
    }

    return ctx
