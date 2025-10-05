"""State extraction from LLM responses using function calling.

Implements Challenge #1 solution: Use function calling instead of raw JSON
for reliable, type-safe state updates.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime

from faultmaven.models import CaseDiagnosticState, UrgencyLevel


# Function schema for LLM function calling
UPDATE_DIAGNOSTIC_STATE_FUNCTION = {
    "name": "update_diagnostic_state",
    "description": "Update the diagnostic state based on conversation analysis",
    "parameters": {
        "type": "object",
        "properties": {
            "has_active_problem": {
                "type": "boolean",
                "description": "Whether conversation indicates active technical problem"
            },
            "problem_statement": {
                "type": "string",
                "description": "Concise one-sentence problem statement (empty if no problem)"
            },
            "current_phase": {
                "type": "integer",
                "enum": [0, 1, 2, 3, 4, 5],
                "description": "Current diagnostic phase (0-5)"
            },
            "phase_advancement_reason": {
                "type": "string",
                "description": "Why this phase? What criteria were met to advance or stay?"
            },
            "urgency_level": {
                "type": "string",
                "enum": ["normal", "high", "critical"],
                "description": "Problem urgency level"
            },
            "new_symptoms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Newly identified symptoms from this turn"
            },
            "timeline_updates": {
                "type": "object",
                "description": "New timeline information (key-value pairs)",
                "additionalProperties": {"type": "string"}
            },
            "new_hypotheses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "hypothesis": {"type": "string"},
                        "evidence": {"type": "string"},
                        "likelihood": {
                            "type": "string",
                            "enum": ["high", "medium", "low"]
                        }
                    },
                    "required": ["hypothesis", "likelihood"]
                },
                "description": "New root cause hypotheses proposed"
            },
            "new_tests_performed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New diagnostic tests/validations performed"
            },
            "root_cause": {
                "type": "string",
                "description": "Root cause if identified (empty if not yet identified)"
            },
            "solution_proposed": {
                "type": "boolean",
                "description": "Was a solution proposed in this response?"
            },
            "solution_text": {
                "type": "string",
                "description": "Solution summary (empty if not proposed)"
            }
        },
        "required": [
            "has_active_problem",
            "problem_statement",
            "current_phase",
            "phase_advancement_reason"
        ]
    }
}


# Goal-oriented phase assessment prompt
PHASE_ASSESSMENT_PROMPT = """Analyze this troubleshooting conversation and determine diagnostic state.

User Query: {user_query}
Assistant Response: {llm_response}

Previous State:
- Has active problem: {has_active_problem}
- Current phase: Phase {current_phase} ({phase_name})
- Problem: {problem_statement}
- Urgency: {urgency_level}

CRITICAL: Assess phase progression based on SUBSTANCE, not conversation flow.

Phase Advancement Criteria:
- Phase 0 → 1: Problem clearly defined and scoped
- Phase 1 → 2: Blast radius/impact sufficiently understood (who/what affected, severity known)
- Phase 2 → 3: Timeline established (when started, what changed, triggers identified)
- Phase 3 → 4: At least 2-3 ranked hypotheses formulated with evidence
- Phase 4 → 5: Root cause validated with high confidence
- Phase 5 → Done: Solution proposed and user ready to implement

Questions to Answer:
1. Is the current phase ({phase_name}) complete based on criteria above?
2. What information is still MISSING for this phase to be complete?
3. If phase should advance, what specific criteria were MET?
4. If staying in same phase, what information is NEEDED to progress?

Urgency Detection:
- CRITICAL: Words like "down", "outage", "production", "data loss", "emergency"
- HIGH: Words like "urgent", "asap", "quickly", "impacting users"
- NORMAL: Standard troubleshooting pace

Call update_diagnostic_state() with your analysis.
"""


PHASE_NAMES = {
    0: "Intake",
    1: "Blast Radius",
    2: "Timeline",
    3: "Hypotheses",
    4: "Validation",
    5: "Solution"
}


async def extract_diagnostic_state_updates(
    llm_response: str,
    user_query: str,
    previous_state: CaseDiagnosticState,
    llm_client: Any  # Type: ILLMProvider with function calling support
) -> CaseDiagnosticState:
    """Extract diagnostic state updates from LLM response using function calling.
    
    Uses function calling to ensure type-safe, validated state updates.
    Implements goal-oriented phase assessment to prevent rigid progression.
    
    Args:
        llm_response: The LLM's response to user query
        user_query: The user's original query
        previous_state: Current diagnostic state
        llm_client: LLM client with function calling capability
        
    Returns:
        Updated diagnostic state
        
    Examples:
        >>> # Simulated usage (actual usage requires LLM client)
        >>> state = CaseDiagnosticState(
        ...     has_active_problem=True,
        ...     problem_statement="API slow",
        ...     current_phase=0
        ... )
        >>> # updated_state = await extract_diagnostic_state_updates(
        >>> #     llm_response="Let's check what's affected...",
        >>> #     user_query="My API is slow",
        >>> #     previous_state=state,
        >>> #     llm_client=llm
        >>> # )
    """
    # Build goal-oriented assessment prompt
    assessment_prompt = PHASE_ASSESSMENT_PROMPT.format(
        user_query=user_query,
        llm_response=llm_response,
        has_active_problem=previous_state.has_active_problem,
        current_phase=previous_state.current_phase,
        phase_name=PHASE_NAMES[previous_state.current_phase],
        problem_statement=previous_state.problem_statement or "None",
        urgency_level=previous_state.urgency_level.value
    )
    
    # Call LLM with function calling (forces structured output)
    function_call_result = await llm_client.complete_with_functions(
        prompt=assessment_prompt,
        functions=[UPDATE_DIAGNOSTIC_STATE_FUNCTION],
        function_call={"name": "update_diagnostic_state"}  # Force this function
    )
    
    # Extract validated arguments from function call
    state_updates = function_call_result.function_call.arguments
    
    # Apply updates to create new state
    updated_state = apply_state_updates(previous_state, state_updates)
    
    return updated_state


def apply_state_updates(
    previous_state: CaseDiagnosticState,
    updates: Dict[str, Any]
) -> CaseDiagnosticState:
    """Apply state updates from function call to create new state.
    
    Intelligently merges new information with existing state.
    
    Args:
        previous_state: Current state
        updates: Updates from function call
        
    Returns:
        New updated state
    """
    # Start with previous state values
    new_state_data = previous_state.dict()
    
    # Update basic fields
    new_state_data["has_active_problem"] = updates.get(
        "has_active_problem",
        previous_state.has_active_problem
    )
    
    new_state_data["problem_statement"] = updates.get(
        "problem_statement",
        previous_state.problem_statement
    )
    
    new_state_data["current_phase"] = updates.get(
        "current_phase",
        previous_state.current_phase
    )
    
    # Set urgency level
    urgency_str = updates.get("urgency_level", previous_state.urgency_level.value)
    new_state_data["urgency_level"] = UrgencyLevel(urgency_str)
    
    # Merge symptoms (add new, keep existing)
    new_symptoms = updates.get("new_symptoms", [])
    existing_symptoms = previous_state.symptoms
    all_symptoms = list(set(existing_symptoms + new_symptoms))
    new_state_data["symptoms"] = all_symptoms
    
    # Merge timeline info
    timeline_updates = updates.get("timeline_updates", {})
    merged_timeline = {**previous_state.timeline_info, **timeline_updates}
    new_state_data["timeline_info"] = merged_timeline
    
    # Merge hypotheses (add new, keep existing)
    new_hypotheses = updates.get("new_hypotheses", [])
    existing_hypotheses = previous_state.hypotheses
    all_hypotheses = existing_hypotheses + new_hypotheses
    new_state_data["hypotheses"] = all_hypotheses
    
    # Merge tests performed
    new_tests = updates.get("new_tests_performed", [])
    existing_tests = previous_state.tests_performed
    all_tests = existing_tests + new_tests
    new_state_data["tests_performed"] = all_tests
    
    # Update root cause
    if updates.get("root_cause"):
        new_state_data["root_cause"] = updates["root_cause"]
    
    # Update solution
    if updates.get("solution_proposed"):
        new_state_data["solution_proposed"] = True
        new_state_data["solution_text"] = updates.get("solution_text", "")
    
    # Update timestamp
    new_state_data["last_updated"] = datetime.utcnow()
    new_state_data["turn_count"] = previous_state.turn_count + 1
    
    # Set problem_started_at if this is first time problem identified
    if updates.get("has_active_problem") and not previous_state.has_active_problem:
        new_state_data["problem_started_at"] = datetime.utcnow()
    
    # Create new state instance
    return CaseDiagnosticState(**new_state_data)


def get_state_extraction_function_schema() -> Dict[str, Any]:
    """Get the function schema for state extraction.
    
    Returns:
        Function schema dict for LLM function calling
    """
    return UPDATE_DIAGNOSTIC_STATE_FUNCTION
