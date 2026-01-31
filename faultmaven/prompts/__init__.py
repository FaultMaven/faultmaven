"""
FaultMaven Prompt Engineering Module

This module contains prompt templates and system prompts for the FaultMaven AI troubleshooting system.

Components:
- system_prompts: Tiered system prompts (MINIMAL, BRIEF, STANDARD)
- response_prompts: Response-type-specific prompts
- few_shot_examples: Pattern templates
"""

from faultmaven.prompts.few_shot_examples import (
    format_intelligent_few_shot_prompt,
    format_pattern_prompt,
    get_examples_by_intent,
    get_examples_by_response_type,
    get_pattern,
    get_response_pattern,
    select_intelligent_examples,
)
from faultmaven.prompts.response_prompts import (
    RESPONSE_TYPE_PROMPTS,
    assemble_intelligent_prompt,
    get_response_type_prompt,
)
from faultmaven.prompts.system_prompts import (
    BRIEF_PROMPT,
    CONCISE_SYSTEM_PROMPT,
    MINIMAL_PROMPT,
    NEUTRAL_IDENTITY,
    PRIMARY_SYSTEM_PROMPT,
    STANDARD_PROMPT,
    get_system_prompt,
    get_tiered_prompt,
)

__all__ = [
    # System prompts
    "get_system_prompt",
    "get_tiered_prompt",
    "PRIMARY_SYSTEM_PROMPT",
    "CONCISE_SYSTEM_PROMPT",
    "NEUTRAL_IDENTITY",
    "MINIMAL_PROMPT",
    "BRIEF_PROMPT",
    "STANDARD_PROMPT",
    # Pattern templates
    "get_pattern",
    "get_response_pattern",
    "format_pattern_prompt",
    # Pattern selection
    "get_examples_by_response_type",
    "get_examples_by_intent",
    "select_intelligent_examples",
    "format_intelligent_few_shot_prompt",
    # Response-type prompts
    "get_response_type_prompt",
    "assemble_intelligent_prompt",
    "RESPONSE_TYPE_PROMPTS",
]
