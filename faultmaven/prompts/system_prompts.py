"""
System Prompts for FaultMaven AI Troubleshooting System

This module contains comprehensive system prompts that define FaultMaven's identity,
methodology, and troubleshooting approach following the five-phase SRE doctrine.
"""

from typing import Dict

# Neutral Identity (for non-troubleshooting interactions - 10 tokens)
NEUTRAL_IDENTITY = """You are FaultMaven, an AI assistant."""

# Minimal Core Identity (for troubleshooting - 30 tokens)
CORE_IDENTITY = """You are FaultMaven, an expert SRE. Provide clear, actionable troubleshooting guidance."""

# Brief Methodology (for simple troubleshooting - 60 tokens)
BRIEF_METHODOLOGY = """For troubleshooting: 1) Scope impact 2) Timeline 3) Hypotheses 4) Validate 5) Solution."""

# Detailed Methodology (for complex troubleshooting - 180 tokens)
DETAILED_METHODOLOGY = """For complex troubleshooting, follow 5 phases:
1. Define Blast Radius - scope, impact, affected systems, when started
2. Establish Timeline - last known good, recent changes, correlated events
3. Formulate Hypotheses - potential causes ranked by likelihood
4. Validate - test with logs, metrics, config checks
5. Propose Solution - immediate fix, root cause, verification, prevention"""

# Tiered System Prompts - Conditional Loading

# Tier 0: Minimal (for ANSWER responses - 30 tokens)
MINIMAL_PROMPT = CORE_IDENTITY

# Tier 1: Brief (for simple troubleshooting - 90 tokens)
BRIEF_PROMPT = CORE_IDENTITY + "\n\n" + BRIEF_METHODOLOGY

# Tier 2: Standard (for moderate troubleshooting - 210 tokens)
STANDARD_PROMPT = CORE_IDENTITY + "\n\n" + DETAILED_METHODOLOGY

# PRIMARY_SYSTEM_PROMPT - default (Tier 2 for backward compatibility)
PRIMARY_SYSTEM_PROMPT = STANDARD_PROMPT
CONCISE_SYSTEM_PROMPT = BRIEF_PROMPT  # Tier 1


# Prompt variants registry
SYSTEM_PROMPT_VARIANTS: Dict[str, str] = {
    "default": PRIMARY_SYSTEM_PROMPT,
    "primary": PRIMARY_SYSTEM_PROMPT,
    "concise": CONCISE_SYSTEM_PROMPT,
    "detailed": STANDARD_PROMPT,  # Use STANDARD_PROMPT for detailed
    # New tiered variants
    "minimal": MINIMAL_PROMPT,
    "brief": BRIEF_PROMPT,
    "standard": STANDARD_PROMPT,
}


def get_system_prompt(variant: str = "default", user_expertise: str = "intermediate") -> str:
    """Get system prompt based on variant and user expertise level

    Selects appropriate system prompt variant based on explicit variant choice
    or automatically based on user expertise level.

    Args:
        variant: Prompt variant ("default", "primary", "concise", "detailed", "minimal", "brief", "standard")
        user_expertise: User expertise level ("beginner", "intermediate", "advanced")

    Returns:
        System prompt text

    Examples:
        >>> get_system_prompt("default")
        'You are FaultMaven, an expert SRE...'
        >>> get_system_prompt("concise", "advanced")
        'You are FaultMaven, an expert SRE...For troubleshooting...'
        >>> get_system_prompt("minimal")
        'You are FaultMaven, an expert SRE. Provide clear, actionable troubleshooting guidance.'

    Deprecated:
        Use get_tiered_prompt() for automatic tier selection based on response type
    """
    # Auto-select variant based on expertise if using default
    if variant == "default":
        if user_expertise == "beginner":
            variant = "detailed"
        elif user_expertise == "advanced":
            variant = "concise"
        else:  # intermediate
            variant = "primary"

    return SYSTEM_PROMPT_VARIANTS.get(variant, PRIMARY_SYSTEM_PROMPT)


def get_system_prompt_with_context(
    variant: str = "default",
    user_expertise: str = "intermediate",
    additional_context: str = ""
) -> str:
    """Get system prompt with additional context appended

    Args:
        variant: Prompt variant ("default", "primary", "concise", "detailed")
        user_expertise: User expertise level ("beginner", "intermediate", "advanced")
        additional_context: Additional context to append

    Returns:
        System prompt with context appended

    Deprecated:
        Use get_tiered_prompt() for automatic tier selection based on response type
    """
    base_prompt = get_system_prompt(variant, user_expertise)
    if additional_context:
        return f"{base_prompt}\n\n{additional_context}"
    return base_prompt


def get_tiered_prompt(
    response_type: str = "ANSWER",
    complexity: str = "simple",
    intent: str = None
) -> str:
    """Get optimized system prompt based on response type, complexity, and intent

    Implements tiered prompt loading for token efficiency (81% reduction):
    - Non-troubleshooting intents: Neutral identity (10 tokens)
    - ANSWER/INFO responses: Minimal prompt (30 tokens)
    - Simple troubleshooting: Brief prompt (90 tokens)
    - Moderate/Complex troubleshooting: Standard prompt (210 tokens)

    Args:
        response_type: ResponseType value (ANSWER, PLAN_PROPOSAL, etc.)
        complexity: Query complexity (simple, moderate, complex)
        intent: Query intent (GREETING, GRATITUDE, OFF_TOPIC, etc.) - optional

    Returns:
        Optimized system prompt string

    Examples:
        >>> get_tiered_prompt("ANSWER", "simple", "GREETING")
        'You are FaultMaven, an AI assistant.'  # NEUTRAL_IDENTITY (10 tokens)
        >>> get_tiered_prompt("ANSWER", "simple")
        'You are FaultMaven, an expert SRE...'  # MINIMAL_PROMPT (30 tokens)
        >>> get_tiered_prompt("PLAN_PROPOSAL", "simple")
        'You are FaultMaven...For troubleshooting...'  # BRIEF_PROMPT (90 tokens)
    """
    # Neutral identity for non-troubleshooting intents
    NON_TROUBLESHOOTING_INTENTS = [
        "GREETING", "GRATITUDE", "OFF_TOPIC",
        "META_FAULTMAVEN", "CONVERSATION_CONTROL"
    ]

    if intent and intent.upper() in NON_TROUBLESHOOTING_INTENTS:
        return NEUTRAL_IDENTITY

    # Minimal prompt for information/explanation requests
    if response_type in ["ANSWER", "INFO", "EXPLANATION"]:
        return MINIMAL_PROMPT

    # Brief prompt for simple troubleshooting
    if complexity == "simple":
        return BRIEF_PROMPT

    # Standard prompt for moderate/complex troubleshooting
    return STANDARD_PROMPT
