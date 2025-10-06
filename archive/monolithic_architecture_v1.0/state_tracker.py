"""Simple heuristic-based diagnostic state tracker.

This is a temporary implementation until function calling is available.
Uses pattern matching and keyword detection to update diagnostic state.
"""

import re
from faultmaven.models import CaseDiagnosticState, UrgencyLevel


# Problem indicators
PROBLEM_KEYWORDS = [
    'error', 'issue', 'problem', 'broken', 'down', 'failing', 'failed',
    'not working', 'crash', 'timeout', 'slow', 'exception', 'bug'
]

# Urgency indicators
CRITICAL_KEYWORDS = ['critical', 'production down', 'outage', 'emergency', 'urgent']
HIGH_KEYWORDS = ['important', 'asap', 'quickly', 'hurry', 'high priority']


def _update_diagnostic_state_heuristic(
    previous_state: CaseDiagnosticState,
    user_query: str,
    llm_answer: str,
    has_suggested_commands: bool
) -> CaseDiagnosticState:
    """Update diagnostic state using heuristics.

    Args:
        previous_state: Current diagnostic state
        user_query: User's query text
        llm_answer: LLM's response
        has_suggested_commands: Whether LLM suggested diagnostic commands

    Returns:
        Updated diagnostic state
    """
    # Create new state as copy
    new_state = CaseDiagnosticState(**previous_state.dict())

    query_lower = user_query.lower()

    # Detect if user has an active problem
    has_problem = any(keyword in query_lower for keyword in PROBLEM_KEYWORDS)

    if has_problem and not previous_state.has_active_problem:
        # New problem detected
        new_state.has_active_problem = True
        new_state.problem_statement = user_query[:200]  # First 200 chars
        new_state.current_phase = 0  # Intake phase

        # Detect urgency
        if any(keyword in query_lower for keyword in CRITICAL_KEYWORDS):
            new_state.urgency_level = UrgencyLevel.CRITICAL
        elif any(keyword in query_lower for keyword in HIGH_KEYWORDS):
            new_state.urgency_level = UrgencyLevel.HIGH
        else:
            new_state.urgency_level = UrgencyLevel.NORMAL

    # If LLM is suggesting diagnostic commands, we're in diagnostic mode
    if has_suggested_commands and new_state.has_active_problem:
        if new_state.current_phase < 1:
            new_state.current_phase = 1  # At least in blast radius phase

    # Simple phase progression based on conversation length
    if new_state.has_active_problem:
        # Count symptoms mentioned
        symptom_patterns = [
            r'error.*?(message|code|stack)',
            r'(pod|container|service).*?(restart|fail|crash)',
            r'(timeout|slow|latency)',
            r'(memory|cpu).*?(high|spike|leak)',
        ]

        new_symptoms = []
        for pattern in symptom_patterns:
            if re.search(pattern, query_lower):
                new_symptoms.append(re.search(pattern, query_lower).group(0))

        # Add unique symptoms
        for symptom in new_symptoms:
            if symptom not in new_state.symptoms:
                new_state.symptoms.append(symptom)

    return new_state
