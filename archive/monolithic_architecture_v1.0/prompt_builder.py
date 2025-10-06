"""Prompt builder for doctor/patient architecture.

Formats diagnostic state and conversation history into context for the LLM.
"""

from typing import List, Dict, Any
from faultmaven.models import CaseDiagnosticState, CaseMessage, UrgencyLevel
from faultmaven.prompts.doctor_patient import get_doctor_patient_prompt, PromptVersion


PHASE_NAMES = {
    0: "Intake (Problem Identification)",
    1: "Blast Radius (Impact Assessment)",
    2: "Timeline (Change Analysis)",
    3: "Hypotheses (Root Cause Theories)",
    4: "Validation (Testing)",
    5: "Solution (Resolution)"
}


def format_diagnostic_state(state: CaseDiagnosticState) -> str:
    """Format diagnostic state for LLM context.
    
    Creates a concise summary of the current diagnostic state that the LLM
    can use to maintain continuity across turns.
    
    Args:
        state: Current diagnostic state
        
    Returns:
        Formatted state summary
        
    Examples:
        >>> state = CaseDiagnosticState(
        ...     has_active_problem=True,
        ...     problem_statement="API returning 500 errors",
        ...     current_phase=1,
        ...     symptoms=["500 errors", "high latency"]
        ... )
        >>> print(format_diagnostic_state(state))
        Active Problem: API returning 500 errors
        Current Phase: Phase 1 - Blast Radius (Impact Assessment)
        Urgency: NORMAL
        Symptoms Identified: 500 errors, high latency
    """
    if not state.has_active_problem:
        return "No active problem - user is in informational/exploratory mode"
    
    lines = [
        f"Active Problem: {state.problem_statement}",
        f"Current Phase: Phase {state.current_phase} - {PHASE_NAMES[state.current_phase]}",
        f"Urgency: {state.urgency_level.value.upper()}"
    ]
    
    if state.symptoms:
        lines.append(f"Symptoms Identified: {', '.join(state.symptoms)}")
    
    if state.timeline_info:
        timeline_summary = []
        for key, value in state.timeline_info.items():
            timeline_summary.append(f"{key}: {value}")
        if timeline_summary:
            lines.append(f"Timeline Info: {'; '.join(timeline_summary)}")
    
    if state.hypotheses:
        hypotheses_list = []
        for h in state.hypotheses:
            likelihood = h.get('likelihood', 'unknown')
            hypothesis_text = h.get('hypothesis', 'Unknown hypothesis')
            hypotheses_list.append(f"[{likelihood}] {hypothesis_text}")
        if hypotheses_list:
            lines.append(f"Working Hypotheses: {'; '.join(hypotheses_list)}")
    
    if state.tests_performed:
        lines.append(f"Tests Performed: {', '.join(state.tests_performed)}")
    
    if state.root_cause:
        lines.append(f"Root Cause Identified: {state.root_cause}")
    
    if state.solution_proposed:
        lines.append(f"Solution Proposed: {state.solution_text}")
    
    return "\n".join(lines)


def format_conversation_history(
    messages: List[CaseMessage],
    max_messages: int = 5,
    max_tokens: int = 500
) -> str:
    """Format recent conversation history for LLM context.
    
    Includes the most recent messages up to token limit. Older messages
    should be summarized separately (see context_summarizer.py).
    
    Args:
        messages: List of case messages
        max_messages: Maximum number of messages to include
        max_tokens: Rough token budget (1 token ≈ 4 chars)
        
    Returns:
        Formatted conversation history
    """
    if not messages:
        return "No previous conversation"
    
    # Sort by timestamp, most recent last
    sorted_messages = sorted(messages, key=lambda m: m.timestamp)
    recent_messages = sorted_messages[-max_messages:]
    
    formatted = []
    total_chars = 0
    max_chars = max_tokens * 4  # Rough estimation
    
    for msg in recent_messages:
        role = "User" if msg.message_type.value == "user_query" else "FaultMaven"
        line = f"{role}: {msg.content}"
        
        if total_chars + len(line) > max_chars:
            break
        
        formatted.append(line)
        total_chars += len(line)
    
    return "\n".join(formatted)


def build_diagnostic_prompt(
    user_query: str,
    diagnostic_state: CaseDiagnosticState,
    conversation_history: List[CaseMessage],
    prompt_version: PromptVersion = PromptVersion.STANDARD
) -> str:
    """Build complete diagnostic prompt for LLM.
    
    Combines the system prompt template with current diagnostic state,
    conversation history, and user query.
    
    Args:
        user_query: Current user question/statement
        diagnostic_state: Current diagnostic state
        conversation_history: Recent case messages
        prompt_version: Which prompt version to use
        
    Returns:
        Complete formatted prompt ready for LLM
        
    Examples:
        >>> from faultmaven.models import CaseDiagnosticState
        >>> state = CaseDiagnosticState(has_active_problem=False)
        >>> prompt = build_diagnostic_prompt(
        ...     user_query="What's the difference between Redis and Memcached?",
        ...     diagnostic_state=state,
        ...     conversation_history=[],
        ...     prompt_version=PromptVersion.STANDARD
        ... )
        >>> "Redis" in prompt and "user_query" in prompt
        True
    """
    # Get the appropriate prompt template
    prompt_template = get_doctor_patient_prompt(prompt_version)
    
    # Format diagnostic state context
    state_context = format_diagnostic_state(diagnostic_state)
    
    # Format conversation history
    history_context = format_conversation_history(conversation_history)
    
    # Fill in template placeholders
    complete_prompt = prompt_template.format(
        diagnostic_state_context=state_context,
        conversation_history=history_context,
        user_query=user_query
    )
    
    return complete_prompt


def estimate_prompt_tokens(prompt: str) -> int:
    """Estimate token count for a prompt.
    
    Uses rough heuristic: 1 token ≈ 3.7 characters for English text.
    
    Args:
        prompt: The prompt text
        
    Returns:
        Estimated token count
    """
    return int(len(prompt) / 3.7)
