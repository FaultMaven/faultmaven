"""
FaultMaven Prompt Engineering Module

This module contains all prompt templates, system prompts, phase-specific prompts,
and pattern templates for the FaultMaven AI troubleshooting system.

Components:
- prompt_manager: Unified OOP interface for all prompt operations (RECOMMENDED)
- system_prompts: Tiered system prompts (MINIMAL, BRIEF, STANDARD)
- phase_prompts: Phase-specific prompt templates for each troubleshooting phase
- few_shot_examples: Compact pattern templates (Phase 3 optimized)
- response_prompts: Response-type-specific prompts

Optimization Results (Phase 0):
- Tiered system prompts: 30/90/210 tokens (vs 2,000 tokens before)
- Pattern templates: ~100-200 tokens (vs 1,500 tokens before)
- Total reduction: 81% tokens saved per request

Usage:
    >>> from faultmaven.prompts import get_prompt_manager
    >>> manager = get_prompt_manager()
    >>> prompt = manager.get_system_prompt(tier=PromptTier.BRIEF)
"""

from faultmaven.prompts.system_prompts import (
    get_system_prompt,
    get_tiered_prompt,  # Phase 2: Tiered prompt loading
    PRIMARY_SYSTEM_PROMPT,
    CONCISE_SYSTEM_PROMPT,
    # Tiered prompts (Phase 2)
    NEUTRAL_IDENTITY,
    MINIMAL_PROMPT,
    BRIEF_PROMPT,
    STANDARD_PROMPT,
)

from faultmaven.prompts.phase_prompts import (
    get_phase_prompt,
    PHASE_1_BLAST_RADIUS,
    PHASE_2_TIMELINE,
    PHASE_3_HYPOTHESIS,
    PHASE_4_VALIDATION,
    PHASE_5_SOLUTION,
)

from faultmaven.prompts.few_shot_examples import (
    # Phase 3: Pattern-based retrieval (optimized)
    get_pattern,
    get_response_pattern,
    format_pattern_prompt,
    # Task 2: Enhanced pattern selection
    get_examples_by_response_type,
    get_examples_by_intent,
    select_intelligent_examples,
    format_intelligent_few_shot_prompt,
)

from faultmaven.prompts.response_prompts import (
    get_response_type_prompt,
    assemble_intelligent_prompt,
    RESPONSE_TYPE_PROMPTS,
)

from faultmaven.prompts.prompt_manager import (
    PromptManager,
    get_prompt_manager,
    PromptTier,
    Phase,
)

__all__ = [
    # PromptManager (OOP interface - RECOMMENDED)
    "PromptManager",
    "get_prompt_manager",
    "PromptTier",
    "Phase",
    # System prompts
    "get_system_prompt",
    "get_tiered_prompt",
    "PRIMARY_SYSTEM_PROMPT",
    "CONCISE_SYSTEM_PROMPT",
    "NEUTRAL_IDENTITY",
    "MINIMAL_PROMPT",
    "BRIEF_PROMPT",
    "STANDARD_PROMPT",
    # Phase prompts
    "get_phase_prompt",
    "PHASE_1_BLAST_RADIUS",
    "PHASE_2_TIMELINE",
    "PHASE_3_HYPOTHESIS",
    "PHASE_4_VALIDATION",
    "PHASE_5_SOLUTION",
    # Phase 3: Pattern templates (optimized)
    "get_pattern",
    "get_response_pattern",
    "format_pattern_prompt",
    # Task 2: Enhanced pattern selection
    "get_examples_by_response_type",
    "get_examples_by_intent",
    "select_intelligent_examples",
    "format_intelligent_few_shot_prompt",
    # Response-type prompts
    "get_response_type_prompt",
    "assemble_intelligent_prompt",
    "RESPONSE_TYPE_PROMPTS",
]
