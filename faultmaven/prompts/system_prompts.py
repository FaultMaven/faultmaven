"""
System Prompts for FaultMaven AI Troubleshooting System

This module contains comprehensive system prompts that define FaultMaven's identity,
methodology, and troubleshooting approach following the five-phase SRE doctrine.
"""

from typing import Dict

# Minimal Core Identity (always included - 30 tokens)
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

# Tier 3: Detailed (for complex cases with explanations - used rarely)
# Kept for backward compatibility but should be rarely used
DETAILED_SYSTEM_PROMPT = STANDARD_PROMPT  # Deprecated, use STANDARD_PROMPT

# PRIMARY_SYSTEM_PROMPT - default (Tier 2 for backward compatibility)
PRIMARY_SYSTEM_PROMPT = STANDARD_PROMPT
CONCISE_SYSTEM_PROMPT = BRIEF_PROMPT  # Tier 1


# Prompt variants registry
SYSTEM_PROMPT_VARIANTS: Dict[str, str] = {
    "default": PRIMARY_SYSTEM_PROMPT,
    "primary": PRIMARY_SYSTEM_PROMPT,
    "concise": CONCISE_SYSTEM_PROMPT,
    "detailed": DETAILED_SYSTEM_PROMPT,
    # New tiered variants
    "minimal": MINIMAL_PROMPT,
    "brief": BRIEF_PROMPT,
    "standard": STANDARD_PROMPT,
}


def get_system_prompt(variant: str = "default", user_expertise: str = "intermediate") -> str:
    """
    Get system prompt based on variant and user expertise level.

    Args:
        variant: Prompt variant ("default", "primary", "concise", "detailed")
        user_expertise: User expertise level ("beginner", "intermediate", "advanced")

    Returns:
        str: System prompt text

    Examples:
        >>> get_system_prompt("default")  # Returns PRIMARY_SYSTEM_PROMPT
        >>> get_system_prompt("concise", "advanced")  # Returns CONCISE_SYSTEM_PROMPT
        >>> get_system_prompt("detailed", "beginner")  # Returns DETAILED_SYSTEM_PROMPT
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
    """Get system prompt with additional context appended (deprecated - use get_tiered_prompt)."""
    base_prompt = get_system_prompt(variant, user_expertise)
    if additional_context:
        return f"{base_prompt}\n\n{additional_context}"
    return base_prompt


def get_tiered_prompt(response_type: str = "ANSWER", complexity: str = "simple") -> str:
    """
    Get optimized system prompt based on response type and complexity.

    This function implements tiered prompt loading for token efficiency:
    - ANSWER/INFO responses: Minimal prompt (30 tokens)
    - Simple troubleshooting: Brief prompt (90 tokens)
    - Moderate/Complex troubleshooting: Standard prompt (210 tokens)

    Args:
        response_type: ResponseType value (ANSWER, PLAN_PROPOSAL, etc.)
        complexity: Query complexity (simple, moderate, complex)

    Returns:
        Optimized system prompt string

    Examples:
        >>> get_tiered_prompt("ANSWER", "simple")  # Returns MINIMAL_PROMPT (30 tokens)
        >>> get_tiered_prompt("PLAN_PROPOSAL", "simple")  # Returns BRIEF_PROMPT (90 tokens)
        >>> get_tiered_prompt("PLAN_PROPOSAL", "complex")  # Returns STANDARD_PROMPT (210 tokens)
    """
    # Minimal prompt for information/explanation requests
    if response_type in ["ANSWER", "INFO", "EXPLANATION"]:
        return MINIMAL_PROMPT

    # Brief prompt for simple troubleshooting
    if complexity == "simple":
        return BRIEF_PROMPT

    # Standard prompt for moderate/complex troubleshooting
    return STANDARD_PROMPT
