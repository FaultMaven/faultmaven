"""Response-Type-Specific Prompts

This module contains prompts tailored to specific ResponseTypes, determining HOW
the agent should respond based on the selected response strategy.

These prompts work alongside the base system prompts to provide specific guidance
for different response scenarios.
"""

from typing import Dict, Any, Optional
from faultmaven.models.api import ResponseType


# Response-type-specific prompt templates
RESPONSE_TYPE_PROMPTS = {
    ResponseType.CLARIFICATION_REQUEST: """Ask 2-3 specific questions about missing info (what/when/where). Explain why needed. Be patient, not interrogative.""",

    ResponseType.PLAN_PROPOSAL: """Provide numbered action steps with exact commands. Include: goal, rationale per step, expected output, verification. Be confident and structured.""",

    ResponseType.ANSWER: """Provide a clear, direct answer to the question. Follow with explanation of reasoning. Include practical examples when helpful. Avoid section headers or formatting - just write naturally.""",

    ResponseType.CONFIRMATION_REQUEST: """State: proposed action, impact, risks, alternatives. Ask clear yes/no question. Be cautious and respectful.""",

    ResponseType.SOLUTION_READY: """Provide: root cause, solution summary, implementation steps, verification, prevention. Be confident and comprehensive.""",

    ResponseType.NEEDS_MORE_DATA: """List specific data/logs needed with exact commands. Explain why needed and how to share safely.""",

    ResponseType.ESCALATION_REQUIRED: """State limitations, summarize attempts, explain why escalating, recommend who/how, provide summary for escalation team. Be honest and supportive.""",
}


# Boundary response templates (special cases)
BOUNDARY_RESPONSE_PROMPTS = {
    "off_topic": """Politely redirect to technical troubleshooting. Mention capabilities: troubleshooting, root cause analysis, config, performance, incidents.""",

    "meta_faultmaven": """Explain: AI troubleshooting assistant using 5-phase SRE methodology. Can analyze logs, perform RCA, propose solutions. Cannot access systems directly or make changes. Ask what they need help with.""",

    "conversation_control": """Acknowledge request. For reset: ask what to help with. For go back: recap previous topic. For skip: ask what's next.""",

    "greeting": """Greet warmly. Introduce as FaultMaven AI troubleshooting assistant. Ask what technical issue they need help with.""",

    "gratitude": """Acknowledge thanks warmly. Offer continued support. Ask if anything else needed.""",
}


def get_response_type_prompt(response_type: ResponseType) -> str:
    """Get the prompt template for a specific ResponseType

    Args:
        response_type: The ResponseType to get prompt for

    Returns:
        Prompt template string
    """
    return RESPONSE_TYPE_PROMPTS.get(
        response_type,
        RESPONSE_TYPE_PROMPTS[ResponseType.CLARIFICATION_REQUEST],  # Default fallback
    )


def get_boundary_prompt(boundary_type: str) -> str:
    """Get boundary response prompt for special cases

    Args:
        boundary_type: Type of boundary (off_topic, meta_faultmaven, etc.)

    Returns:
        Boundary prompt template
    """
    return BOUNDARY_RESPONSE_PROMPTS.get(
        boundary_type,
        BOUNDARY_RESPONSE_PROMPTS["off_topic"],  # Default fallback
    )


def assemble_intelligent_prompt(
    base_system_prompt: str,
    response_type: ResponseType,
    conversation_state: Optional[Dict[str, Any]] = None,
    query_classification: Optional[Dict[str, Any]] = None,
    boundary_type: Optional[str] = None,
) -> str:
    """Assemble complete intelligent prompt with all components

    Args:
        base_system_prompt: Base system prompt (identity + 5-phase doctrine)
        response_type: Selected ResponseType
        conversation_state: Current conversation state
        query_classification: Query classification result
        boundary_type: Boundary type if applicable

    Returns:
        Complete assembled prompt
    """
    prompt_parts = []

    # Part 1: Base system prompt (identity + methodology)
    prompt_parts.append(base_system_prompt)

    # Part 2: Response-type-specific guidance
    if boundary_type:
        # Special boundary case
        boundary_prompt = get_boundary_prompt(boundary_type)
        prompt_parts.append(boundary_prompt)
    else:
        # Normal response type
        response_prompt = get_response_type_prompt(response_type)
        prompt_parts.append(response_prompt)

    # Part 3: High-value context signals only (if critical)
    # Only include conversation warnings if truly needed
    if conversation_state:
        warnings = []

        frustration = conversation_state.get("frustration_score", 0.0)
        if frustration >= 0.7:
            warnings.append("⚠️ User appears frustrated - be extra patient and clear.")

        clarifications = conversation_state.get("clarification_count", 0)
        if clarifications >= 3:  # Raised threshold from 2 to 3
            warnings.append(f"⚠️ Asked {clarifications}x for clarification - make progress or suggest escalation.")

        if warnings:
            prompt_parts.append("\n".join(warnings))

    return "\n\n".join(prompt_parts)
