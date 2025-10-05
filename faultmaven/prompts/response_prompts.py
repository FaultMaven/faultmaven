"""Response-Type-Specific Prompts

This module contains prompts tailored to specific ResponseTypes, determining HOW
the agent should respond based on the selected response strategy.

These prompts work alongside the base system prompts to provide specific guidance
for different response scenarios.
"""

from typing import Dict, Any, Optional
from faultmaven.models.api import ResponseType
from faultmaven.config.settings import get_settings


# Response-type-specific prompt templates
RESPONSE_TYPE_PROMPTS = {
    ResponseType.CLARIFICATION_REQUEST: """Ask 2-3 specific questions about missing info (what/when/where). Explain why needed. Be patient, not interrogative.""",

    ResponseType.PLAN_PROPOSAL: """Provide numbered action steps with exact commands. Include: goal, rationale per step, expected output, verification. Be confident and structured.""",

    ResponseType.ANSWER: """Provide a clear, direct answer to the question. Follow with explanation of reasoning. Include practical examples when helpful. Avoid section headers or formatting - just write naturally.""",

    ResponseType.CONFIRMATION_REQUEST: """State: proposed action, impact, risks, alternatives. Ask clear yes/no question. Be cautious and respectful.""",

    ResponseType.SOLUTION_READY: """Provide: root cause, solution summary, implementation steps, verification, prevention. Be confident and comprehensive.""",

    ResponseType.NEEDS_MORE_DATA: """List specific data/logs needed with exact commands. Explain why needed and how to share safely.""",

    ResponseType.ESCALATION_REQUIRED: """State limitations, summarize attempts, explain why escalating, recommend who/how, provide summary for escalation team. Be honest and supportive.""",

    ResponseType.VISUAL_DIAGRAM: """Create a Mermaid diagram to visualize the architecture, flow, or system structure. Use appropriate diagram type (graph TD/LR for architecture, flowchart for processes, sequenceDiagram for interactions). Include clear node labels and relationship descriptions. Wrap the diagram in ```mermaid code block. Provide a brief 1-2 sentence explanation before the diagram describing what it shows, and optionally add key insights after.""",

    ResponseType.COMPARISON_TABLE: """Create a markdown comparison table to analyze options, features, or approaches. Use clear column headers (Feature/Aspect, Option A, Option B, etc.). Include relevant comparison dimensions (performance, complexity, use cases, pros/cons). Format as proper markdown table with | delimiters. Provide brief context (1-2 sentences) before the table explaining what's being compared, and add a recommendation or key takeaway after based on the comparison.""",
}


# Note: Boundary response prompts removed - all special case handling now unified
# under ResponseType.ANSWER with context-aware behavior based on query intent


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


# get_boundary_prompt() removed - no longer needed with unified ResponseType.ANSWER


def assemble_intelligent_prompt(
    base_system_prompt: str,
    response_type: ResponseType,
    conversation_state: Optional[Dict[str, Any]] = None,
    query_classification: Optional[Dict[str, Any]] = None,
) -> str:
    """Assemble complete intelligent prompt with all components

    Validates inputs and assembles a complete prompt from base system prompt,
    response-type-specific guidance, and conversation context.

    Args:
        base_system_prompt: Base system prompt (identity + 5-phase doctrine)
        response_type: Selected ResponseType
        conversation_state: Current conversation state
        query_classification: Query classification result (intent used for context-aware ANSWER prompts)

    Returns:
        Complete assembled prompt

    Raises:
        ValueError: If base_system_prompt is empty or response_type is invalid

    Examples:
        >>> prompt = assemble_intelligent_prompt(
        ...     base_system_prompt="You are FaultMaven...",
        ...     response_type=ResponseType.ANSWER
        ... )
    """
    # Validation: Ensure base_system_prompt is not empty
    if not base_system_prompt or not base_system_prompt.strip():
        raise ValueError("base_system_prompt cannot be empty")

    # Validation: Ensure response_type is a ResponseType enum
    if not isinstance(response_type, ResponseType):
        raise ValueError(f"response_type must be ResponseType enum, got {type(response_type)}")

    # Validation: Warn if prompt is very long (may exceed context limits)
    import logging
    logger = logging.getLogger(__name__)

    base_length = len(base_system_prompt)
    if base_length > 2000:  # Characters, roughly 500 tokens
        logger.warning(
            f"Base system prompt is very long ({base_length} chars, ~{base_length//4} tokens). "
            "Consider using a tiered prompt."
        )

    prompt_parts = []

    # Part 1: Base system prompt (identity + methodology)
    prompt_parts.append(base_system_prompt)

    # Part 2: Response-type-specific guidance (now unified - no boundary system)
    # For ANSWER responses with special intents, override with intent-specific prompt
    if response_type == ResponseType.ANSWER and query_classification:
        intent = query_classification.get("intent", "").upper()
        intent_specific_prompts = {
            "GREETING": "Respond warmly to the greeting. Briefly introduce yourself as FaultMaven, an AI troubleshooting assistant. Keep it friendly and concise (2-3 sentences maximum). DO NOT ask technical questions or launch into methodology yet - just acknowledge the greeting warmly.",
            "GRATITUDE": "Acknowledge thanks warmly. Offer continued support. Ask if anything else is needed. Keep it brief and friendly.",
            "OFF_TOPIC": "Politely redirect to technical troubleshooting. Mention your capabilities: troubleshooting, root cause analysis, config issues, performance problems, and incident response. Ask what technical issue you can help with.",
            "META_FAULTMAVEN": "Explain you're an AI troubleshooting assistant using 5-phase SRE methodology. You can analyze logs, perform RCA, and propose solutions, but cannot access systems directly or make changes. Ask what they need help with.",
            "CONVERSATION_CONTROL": "Acknowledge the conversation control request appropriately. For 'reset': ask what to help with. For 'go back': recap previous topic. For 'skip': ask what's next."
        }

        if intent in intent_specific_prompts:
            # Use intent-specific prompt instead of generic ANSWER prompt
            prompt_parts.append(intent_specific_prompts[intent])
        else:
            # Use standard ANSWER prompt for other intents
            response_prompt = get_response_type_prompt(response_type)
            prompt_parts.append(response_prompt)
    else:
        # Non-ANSWER response types use standard prompts
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
        # Use centralized threshold from settings
        settings = get_settings()
        max_clarifications = settings.thresholds.max_clarifications
        if clarifications >= max_clarifications:
            warnings.append(f"⚠️ Asked {clarifications}x for clarification - make progress or suggest escalation.")

        if warnings:
            prompt_parts.append("\n".join(warnings))

    return "\n\n".join(prompt_parts)
