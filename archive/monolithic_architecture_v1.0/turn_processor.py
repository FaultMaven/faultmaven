"""Turn-by-turn processing for doctor/patient architecture.

Orchestrates the complete flow: prompt building → LLM call → state extraction.
"""

from typing import Tuple, Optional, Any
from datetime import datetime

from faultmaven.models import (
    Case,
    CaseDiagnosticState,
    LLMResponse,
    CaseMessage,
    MessageType
)
from faultmaven.prompts.doctor_patient import PromptVersion
from .prompt_builder import build_diagnostic_prompt, estimate_prompt_tokens
from .state_tracker import _update_diagnostic_state_heuristic


async def process_turn(
    user_query: str,
    case: Case,
    llm_client: Any,  # Type: ILLMProvider
    prompt_version: Optional[PromptVersion] = None,
    session_id: Optional[str] = None
) -> Tuple[LLMResponse, CaseDiagnosticState]:
    """Process one conversation turn with doctor/patient model.
    
    Complete workflow:
    1. Build diagnostic prompt with full context
    2. Call powerful LLM with structured output (LLMResponse)
    3. Extract state updates using function calling
    4. Update case with new message and state
    
    Args:
        user_query: User's question/statement
        case: Current case with diagnostic state and history
        llm_client: LLM provider client
        prompt_version: Which prompt version to use (None = use from settings)
        session_id: Optional session ID for message tracking
        
    Returns:
        Tuple of (LLM response with guidance, Updated diagnostic state)
        
    Examples:
        >>> # Simulated usage
        >>> case = Case(
        ...     case_id="case123",
        ...     title="API Issues",
        ...     diagnostic_state=CaseDiagnosticState()
        ... )
        >>> # response, new_state = await process_turn(
        >>> #     user_query="My API is returning 500 errors",
        >>> #     case=case,
        >>> #     llm_client=llm,
        >>> #     prompt_version=PromptVersion.STANDARD
        >>> # )
    """
    from faultmaven.config.settings import FaultMavenSettings
    
    # Get prompt version from settings if not specified
    if prompt_version is None:
        settings = FaultMavenSettings()
        prompt_version = PromptVersion(settings.prompts.doctor_patient_version)
    
    # Get current diagnostic state
    diagnostic_state = case.diagnostic_state
    
    # Build complete prompt with diagnostic context
    complete_prompt = build_diagnostic_prompt(
        user_query=user_query,
        diagnostic_state=diagnostic_state,
        conversation_history=case.messages,
        prompt_version=prompt_version
    )
    
    # Estimate tokens for monitoring
    prompt_tokens = estimate_prompt_tokens(complete_prompt)
    
    # Import function schema
    from .function_schemas import get_function_schemas

    # Call LLM with function calling for diagnostic state updates
    llm_raw_response = await llm_client.route(
        prompt=complete_prompt,
        temperature=0.7,
        max_tokens=1500,
        tools=get_function_schemas(),
        tool_choice="auto"  # Let LLM decide when to call function
    )

    # Extract text content from LLM response
    answer_text = llm_raw_response.content if hasattr(llm_raw_response, 'content') else str(llm_raw_response)

    # Parse JSON response from LLM
    import json
    import re
    import logging

    logger = logging.getLogger(__name__)

    parsed_data = {}  # Initialize outside try block for scope
    try:
        # Extract JSON from response (handle markdown code blocks)
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', answer_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try parsing entire response as JSON
            json_str = answer_text.strip()

        parsed_data = json.loads(json_str)

        # Create LLMResponse from parsed JSON
        from faultmaven.models.doctor_patient import SuggestedAction, CommandSuggestion, ActionType, CommandSafety

        llm_response = LLMResponse(
            answer=parsed_data.get("answer", answer_text),
            clarifying_questions=parsed_data.get("clarifying_questions", []),
            suggested_actions=[
                SuggestedAction(
                    label=action.get("label", ""),
                    type=ActionType(action.get("type", "question_template")),
                    payload=action.get("payload", ""),
                    icon=action.get("icon")
                )
                for action in parsed_data.get("suggested_actions", [])
            ],
            suggested_commands=[
                CommandSuggestion(
                    command=cmd.get("command", ""),
                    description=cmd.get("description", ""),
                    why=cmd.get("why", ""),
                    safety=CommandSafety(cmd.get("safety", "safe"))
                )
                for cmd in parsed_data.get("suggested_commands", [])
            ],
            command_validation=None  # TODO: Parse if present
        )
        logger.info(f"✅ Successfully parsed structured LLM response with {len(llm_response.suggested_actions)} actions, {len(llm_response.suggested_commands)} commands")
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        # Fallback to simple response if JSON parsing fails
        logger.warning(f"Failed to parse LLM JSON response: {e}. Using fallback with plain text.")

        llm_response = LLMResponse(
            answer=answer_text,
            clarifying_questions=[],
            suggested_actions=[],
            suggested_commands=[],
            command_validation=None
        )

    # Extract state updates from function calling (preferred) or JSON (fallback)
    if llm_raw_response.tool_calls:
        # Function calling approach (99.5% reliable)
        from .function_schemas import extract_diagnostic_state_from_function_call

        tool_call = llm_raw_response.tool_calls[0]  # Use first tool call
        state_updates = extract_diagnostic_state_from_function_call(tool_call.function)

        # Merge updates into current state (delta update)
        updated_state_dict = diagnostic_state.dict()
        updated_state_dict.update(state_updates)

        # Handle special field conversions
        if "urgency_level" in state_updates:
            from faultmaven.models import UrgencyLevel
            updated_state_dict["urgency_level"] = UrgencyLevel(state_updates["urgency_level"])

        updated_state = CaseDiagnosticState(**updated_state_dict)
        logger.info(f"✅ Applied function call state updates: {list(state_updates.keys())}")

    elif parsed_data and parsed_data.get("diagnostic_state_updates"):
        # JSON fallback approach (98% reliable)
        state_updates = parsed_data["diagnostic_state_updates"]

        # Merge updates into current state (delta update)
        updated_state_dict = diagnostic_state.dict()
        updated_state_dict.update(state_updates)

        # Handle special field conversions
        if "urgency_level" in state_updates:
            from faultmaven.models import UrgencyLevel
            updated_state_dict["urgency_level"] = UrgencyLevel(state_updates["urgency_level"])

        updated_state = CaseDiagnosticState(**updated_state_dict)
        logger.info(f"✅ Applied JSON state updates: {list(state_updates.keys())}")

    else:
        # Heuristic fallback (70-80% reliable)
        updated_state = _update_diagnostic_state_heuristic(
            previous_state=diagnostic_state,
            user_query=user_query,
            llm_answer=llm_response.answer,
            has_suggested_commands=len(llm_response.suggested_commands) > 0
        )
        logger.info("⚠️ Using heuristic state tracking (no function call or JSON updates)")
    
    # Add user message to case
    user_message = CaseMessage(
        case_id=case.case_id,
        session_id=session_id,
        message_type=MessageType.USER_QUERY,
        content=user_query,
        timestamp=datetime.utcnow()
    )
    case.add_message(user_message)
    
    # Add assistant response to case
    assistant_message = CaseMessage(
        case_id=case.case_id,
        session_id=session_id,
        message_type=MessageType.AGENT_RESPONSE,
        content=llm_response.answer,
        timestamp=datetime.utcnow(),
        metadata={
            "has_guidance": llm_response.has_guidance(),
            "is_diagnostic_mode": llm_response.is_diagnostic_mode(),
            "action_count": llm_response.get_action_count(),
            "command_count": llm_response.get_command_count(),
            "command_validation": llm_response.command_validation is not None
        }
    )
    case.add_message(assistant_message)
    
    # Update case diagnostic state
    case.diagnostic_state = updated_state
    case.updated_at = datetime.utcnow()
    
    return llm_response, updated_state


async def detect_case_closure(
    user_query: str,
    llm_response: LLMResponse,
    diagnostic_state: CaseDiagnosticState
) -> Tuple[bool, str]:
    """Detect if case should be closed.
    
    Three types of closure:
    1. Explicit: User says "thanks, that fixed it" or "problem solved"
    2. Implicit: User asks for summary ("what did we do?")
    3. New issue: User starts discussing different problem
    
    Args:
        user_query: User's latest query
        llm_response: LLM's response
        diagnostic_state: Current diagnostic state
        
    Returns:
        Tuple of (should_close, closure_type)
        closure_type: "explicit", "implicit_summary", "new_issue", or ""
    """
    user_lower = user_query.lower()
    
    # Explicit closure patterns
    explicit_patterns = [
        "thanks", "thank you", "that fixed it", "that worked",
        "problem solved", "all good", "issue resolved",
        "that did it", "perfect", "exactly what i needed"
    ]
    
    if any(pattern in user_lower for pattern in explicit_patterns):
        if diagnostic_state.solution_proposed:
            return True, "explicit"
    
    # Implicit closure (summary request)
    summary_patterns = [
        "what did we do", "summarize", "recap",
        "what was the root cause", "how did we fix",
        "what steps did we take"
    ]
    
    if any(pattern in user_lower for pattern in summary_patterns):
        if diagnostic_state.current_phase >= 4:  # Validation or Solution phase
            return True, "implicit_summary"
    
    # New issue detection (topic shift while current problem unresolved)
    if diagnostic_state.has_active_problem and not diagnostic_state.case_resolved:
        # Check if user is asking about completely different topic
        # This would require more sophisticated NLP - simplified here
        new_issue_indicators = [
            "different issue", "another problem", "new issue",
            "switch topics", "different topic"
        ]
        
        if any(indicator in user_lower for indicator in new_issue_indicators):
            return True, "new_issue"
    
    return False, ""


async def generate_closure_summary(
    case: Case,
    diagnostic_state: CaseDiagnosticState,
    llm_client: Any
) -> str:
    """Generate case resolution summary when closing.
    
    Creates comprehensive summary of the troubleshooting process.
    
    Args:
        case: The case to summarize
        diagnostic_state: Final diagnostic state
        llm_client: LLM client for summary generation
        
    Returns:
        Summary text
    """
    summary_prompt = f"""Generate a case resolution summary for this troubleshooting session.

Problem: {diagnostic_state.problem_statement}
Symptoms: {', '.join(diagnostic_state.symptoms) if diagnostic_state.symptoms else 'None'}
Root Cause: {diagnostic_state.root_cause or 'Not identified'}
Solution: {diagnostic_state.solution_text or 'Not proposed'}
Tests Performed: {', '.join(diagnostic_state.tests_performed) if diagnostic_state.tests_performed else 'None'}

Create a structured summary with these sections:
1. **Problem Statement**: What was the issue?
2. **Impact**: Who/what was affected?
3. **Investigation Timeline**: Key steps in diagnosis
4. **Root Cause**: What caused the problem?
5. **Solution Implemented**: How it was fixed
6. **Prevention**: How to prevent recurrence
7. **Key Learnings**: Important insights

Keep it concise but comprehensive (200-300 words).
"""
    
    summary = await llm_client.complete(
        prompt=summary_prompt,
        temperature=0.5,
        max_tokens=500
    )
    
    return summary.content
