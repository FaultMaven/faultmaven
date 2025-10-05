"""Context summarization for managing growing conversation history.

Implements Challenge #2 solution: Periodically summarize accumulated state
to prevent context window from growing indefinitely.
"""

from typing import List, Dict, Any
from faultmaven.models import CaseDiagnosticState, CaseMessage


# Summarization thresholds
SUMMARIZE_AFTER_TURNS = 10  # Summarize after this many turns
KEEP_RECENT_MESSAGES = 5    # Keep last N messages verbatim
MAX_SYMPTOMS_BEFORE_CLUSTER = 10  # Cluster symptoms if more than this


async def should_summarize(
    diagnostic_state: CaseDiagnosticState,
    conversation_history: List[CaseMessage]
) -> bool:
    """Check if context should be summarized.
    
    Triggers summarization when:
    - Turn count exceeds threshold
    - Symptoms list is too long
    - Timeline info is growing large
    
    Args:
        diagnostic_state: Current diagnostic state
        conversation_history: Case messages
        
    Returns:
        True if summarization needed
    """
    # Check turn count
    if diagnostic_state.turn_count >= SUMMARIZE_AFTER_TURNS:
        return True
    
    # Check symptoms list length
    if len(diagnostic_state.symptoms) > MAX_SYMPTOMS_BEFORE_CLUSTER:
        return True
    
    # Check conversation history size
    if len(conversation_history) > SUMMARIZE_AFTER_TURNS + 2:
        return True
    
    return False


async def summarize_diagnostic_state(
    diagnostic_state: CaseDiagnosticState,
    llm_client: Any
) -> CaseDiagnosticState:
    """Summarize diagnostic state to reduce context size.
    
    Keeps essential information while reducing verbosity. Expected ~40-60%
    reduction in token usage.
    
    Args:
        diagnostic_state: State to summarize
        llm_client: LLM client for summarization
        
    Returns:
        Summarized diagnostic state
    """
    # Summarize symptoms if too many
    summarized_symptoms = diagnostic_state.symptoms
    if len(diagnostic_state.symptoms) > MAX_SYMPTOMS_BEFORE_CLUSTER:
        summarized_symptoms = await summarize_symptoms(
            diagnostic_state.symptoms,
            llm_client
        )
    
    # Summarize timeline if large
    summarized_timeline = diagnostic_state.timeline_info
    if len(diagnostic_state.timeline_info) > 5:
        summarized_timeline = await summarize_timeline(
            diagnostic_state.timeline_info,
            llm_client
        )
    
    # Keep only high-likelihood hypotheses
    filtered_hypotheses = [
        h for h in diagnostic_state.hypotheses
        if h.get('likelihood') in ['high', 'medium']
    ]
    
    # Create summarized state
    summarized_state = diagnostic_state.copy(deep=True)
    summarized_state.symptoms = summarized_symptoms
    summarized_state.timeline_info = summarized_timeline
    summarized_state.hypotheses = filtered_hypotheses
    
    return summarized_state


async def summarize_symptoms(
    symptoms: List[str],
    llm_client: Any
) -> List[str]:
    """Cluster and summarize symptoms.
    
    Before: ["500 error on /api/users", "500 error on /api/posts", "API latency > 2s", "DB CPU at 100%"]
    After: ["API returning 500 errors across multiple endpoints", "High API latency correlated with DB CPU spike"]
    
    Args:
        symptoms: List of individual symptoms
        llm_client: LLM client
        
    Returns:
        Summarized symptoms list
    """
    if len(symptoms) <= 3:
        return symptoms
    
    summarize_prompt = f"""Cluster these symptoms into 2-4 concise categories:

Symptoms:
{chr(10).join(f'- {s}' for s in symptoms)}

Group related symptoms together and create concise summaries.
Return as a JSON array of strings.

Example:
["API errors affecting multiple endpoints", "Database performance degradation"]
"""
    
    result = await llm_client.complete(
        prompt=summarize_prompt,
        temperature=0.3,
        max_tokens=200
    )
    
    # Parse JSON response (simplified - real implementation needs error handling)
    try:
        import json
        summarized = json.loads(result.content)
        return summarized[:4]  # Max 4 clustered symptoms
    except:
        # Fallback: keep first 4 symptoms
        return symptoms[:4]


async def summarize_timeline(
    timeline_info: Dict[str, Any],
    llm_client: Any
) -> Dict[str, str]:
    """Summarize timeline information, keeping most critical events.
    
    Prioritizes recent changes and triggering events.
    
    Args:
        timeline_info: Timeline dictionary
        llm_client: LLM client
        
    Returns:
        Summarized timeline with top 3-5 entries
    """
    if len(timeline_info) <= 3:
        return timeline_info
    
    # Simple heuristic: keep most recent and deployment-related entries
    # Real implementation would use LLM to rank by importance
    
    priority_keywords = ["deployment", "deploy", "release", "config change", "migration"]
    
    prioritized = {}
    other = {}
    
    for key, value in timeline_info.items():
        if any(keyword in str(value).lower() for keyword in priority_keywords):
            prioritized[key] = value
        else:
            other[key] = value
    
    # Keep top 3 priority + 2 most recent others
    result = prioritized
    remaining = 5 - len(result)
    
    if remaining > 0:
        # Add most recent from 'other'
        other_items = list(other.items())[-remaining:]
        result.update(dict(other_items))
    
    return result


async def summarize_conversation_history(
    messages: List[CaseMessage],
    llm_client: Any,
    keep_recent: int = KEEP_RECENT_MESSAGES
) -> str:
    """Summarize older conversation turns while keeping recent ones verbatim.
    
    Args:
        messages: All case messages
        llm_client: LLM client
        keep_recent: Number of recent messages to keep verbatim
        
    Returns:
        Formatted history with summary + recent messages
    """
    if len(messages) <= keep_recent:
        # No summarization needed
        from .prompt_builder import format_conversation_history
        return format_conversation_history(messages)
    
    # Split into old and recent
    old_messages = messages[:-keep_recent]
    recent_messages = messages[-keep_recent:]
    
    # Summarize old messages
    old_summary = await summarize_old_messages(old_messages, llm_client)
    
    # Format recent messages verbatim
    from .prompt_builder import format_conversation_history
    recent_formatted = format_conversation_history(recent_messages)
    
    # Combine
    return f"[Earlier conversation summary]\n{old_summary}\n\n[Recent messages]\n{recent_formatted}"


async def summarize_old_messages(
    messages: List[CaseMessage],
    llm_client: Any
) -> str:
    """Summarize older conversation messages.
    
    Args:
        messages: Old messages to summarize
        llm_client: LLM client
        
    Returns:
        Summary of old conversation
    """
    # Create condensed version for summarization
    conversation_text = "\n".join([
        f"{'User' if msg.message_type.value == 'user_query' else 'FaultMaven'}: {msg.content}"
        for msg in messages
    ])
    
    summary_prompt = f"""Summarize this troubleshooting conversation in 2-3 sentences, focusing on:
1. What problem was discussed
2. What information was gathered
3. What diagnostic steps were taken

Conversation:
{conversation_text}

Provide a concise summary (50-75 words).
"""
    
    result = await llm_client.complete(
        prompt=summary_prompt,
        temperature=0.3,
        max_tokens=150
    )
    
    return result.content


def estimate_context_savings(
    original_state: CaseDiagnosticState,
    summarized_state: CaseDiagnosticState,
    original_history: List[CaseMessage],
    summarized_history: str
) -> Dict[str, Any]:
    """Estimate token savings from summarization.
    
    Returns:
        Dict with token counts and savings percentage
    """
    from .prompt_builder import estimate_prompt_tokens
    
    # Estimate original tokens
    original_symptoms_tokens = estimate_prompt_tokens(", ".join(original_state.symptoms))
    original_timeline_tokens = estimate_prompt_tokens(str(original_state.timeline_info))
    
    # Estimate summarized tokens
    summarized_symptoms_tokens = estimate_prompt_tokens(", ".join(summarized_state.symptoms))
    summarized_timeline_tokens = estimate_prompt_tokens(str(summarized_state.timeline_info))
    
    # History tokens
    original_history_str = "\n".join([msg.content for msg in original_history])
    original_history_tokens = estimate_prompt_tokens(original_history_str)
    summarized_history_tokens = estimate_prompt_tokens(summarized_history)
    
    original_total = (
        original_symptoms_tokens +
        original_timeline_tokens +
        original_history_tokens
    )
    
    summarized_total = (
        summarized_symptoms_tokens +
        summarized_timeline_tokens +
        summarized_history_tokens
    )
    
    savings_pct = ((original_total - summarized_total) / original_total * 100) if original_total > 0 else 0
    
    return {
        "original_tokens": original_total,
        "summarized_tokens": summarized_total,
        "tokens_saved": original_total - summarized_total,
        "savings_percentage": round(savings_pct, 1)
    }
