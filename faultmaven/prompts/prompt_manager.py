"""
PromptManager - Centralized Prompt Template Management

This module provides a unified interface for managing all prompt templates,
including system prompts, phase-specific prompts, and few-shot examples.

Design: Object-oriented wrapper around functional prompt modules for better
encapsulation and testability.
"""

from typing import Dict, Any, List, Optional
from enum import Enum

from faultmaven.prompts.system_prompts import (
    get_system_prompt,
    get_tiered_prompt,
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
    get_examples_by_response_type,
    get_examples_by_intent,
    select_intelligent_examples,
    format_intelligent_few_shot_prompt,
)
from faultmaven.prompts.response_prompts import (
    get_response_type_prompt,
    assemble_intelligent_prompt,
)


class PromptTier(str, Enum):
    """Tiered prompt levels for token optimization"""
    MINIMAL = "minimal"      # 30 tokens
    BRIEF = "brief"          # 90 tokens
    STANDARD = "standard"    # 210 tokens


class Phase(str, Enum):
    """SRE troubleshooting phases"""
    BLAST_RADIUS = "blast_radius"
    TIMELINE = "timeline"
    HYPOTHESIS = "hypothesis"
    VALIDATION = "validation"
    SOLUTION = "solution"


class PromptManager:
    """
    Manages prompt templates and generation for FaultMaven AI system.

    This class provides a centralized interface for:
    - System prompts (tiered for token optimization)
    - Phase-specific prompts (5-phase SRE doctrine)
    - Few-shot examples (intelligent selection)
    - Response-type-specific prompts

    Example:
        >>> manager = PromptManager()
        >>> system_prompt = manager.get_system_prompt(tier=PromptTier.BRIEF)
        >>> phase_prompt = manager.get_phase_prompt(
        ...     phase=Phase.BLAST_RADIUS,
        ...     query="My app is down",
        ...     context={"environment": "production"}
        ... )
    """

    def __init__(self):
        """Initialize PromptManager with all template libraries"""
        self.system_prompts = self._load_system_prompts()
        self.phase_prompts = self._load_phase_prompts()
        self.few_shot_library = self._load_few_shot_library()

    def _load_system_prompts(self) -> Dict[str, str]:
        """Load system prompt templates"""
        return {
            "minimal": MINIMAL_PROMPT,
            "brief": BRIEF_PROMPT,
            "standard": STANDARD_PROMPT,
        }

    def _load_phase_prompts(self) -> Dict[str, str]:
        """Load phase-specific prompt templates"""
        return {
            "blast_radius": PHASE_1_BLAST_RADIUS,
            "timeline": PHASE_2_TIMELINE,
            "hypothesis": PHASE_3_HYPOTHESIS,
            "validation": PHASE_4_VALIDATION,
            "solution": PHASE_5_SOLUTION,
        }

    def _load_few_shot_library(self) -> Dict[str, Any]:
        """Load few-shot example library (lazy-loaded from functions)"""
        # Examples are loaded dynamically when needed
        return {}

    # Core API Methods

    def get_system_prompt(
        self,
        tier: PromptTier = PromptTier.STANDARD,
        variant: str = "default"
    ) -> str:
        """
        Get system prompt with specified tier and variant.

        Args:
            tier: Prompt tier (minimal/brief/standard) for token optimization
            variant: Prompt variant (default/concise/detailed)

        Returns:
            System prompt string

        Example:
            >>> manager.get_system_prompt(tier=PromptTier.BRIEF)
            'You are FaultMaven, an expert SRE...'
        """
        if tier == PromptTier.MINIMAL:
            return MINIMAL_PROMPT
        elif tier == PromptTier.BRIEF:
            return BRIEF_PROMPT
        else:
            return STANDARD_PROMPT

    def get_phase_prompt(
        self,
        phase: Phase,
        query: str,
        context: Dict[str, Any]
    ) -> str:
        """
        Generate phase-specific prompt with context injection.

        Args:
            phase: Troubleshooting phase (blast_radius/timeline/etc.)
            query: User's query
            context: Contextual data to inject into prompt

        Returns:
            Formatted phase-specific prompt

        Example:
            >>> manager.get_phase_prompt(
            ...     phase=Phase.BLAST_RADIUS,
            ...     query="App is slow",
            ...     context={"service": "api", "env": "prod"}
            ... )
        """
        return get_phase_prompt(phase.value, query, context)

    def add_few_shot_examples(
        self,
        prompt: str,
        task_type: str,
        num_examples: int = 3
    ) -> str:
        """
        Add few-shot examples to prompt.

        Args:
            prompt: Base prompt to enhance
            task_type: Type of task (classification/troubleshooting/etc.)
            num_examples: Number of examples to include

        Returns:
            Prompt with appended few-shot examples

        Example:
            >>> base_prompt = "Classify this query..."
            >>> enhanced = manager.add_few_shot_examples(
            ...     prompt=base_prompt,
            ...     task_type="classification",
            ...     num_examples=2
            ... )
        """
        examples = select_intelligent_examples(task_type, num_examples)
        return format_intelligent_few_shot_prompt(prompt, examples)

    def get_intelligent_prompt(
        self,
        query: str,
        classification: Dict[str, Any],
        context: Dict[str, Any],
        response_type: Optional[str] = None
    ) -> str:
        """
        Assemble intelligent prompt with all components.

        This is the main prompt assembly method that combines:
        - System prompt (tiered)
        - Classification-specific guidance
        - Context injection
        - Response-type formatting instructions

        Args:
            query: User's query
            classification: Query classification results
            context: Session/user context
            response_type: Desired response type

        Returns:
            Complete assembled prompt

        Example:
            >>> prompt = manager.get_intelligent_prompt(
            ...     query="Why is my database slow?",
            ...     classification={"intent": "troubleshooting", "complexity": "moderate"},
            ...     context={"session_history": [...]},
            ...     response_type="PLAN_PROPOSAL"
            ... )
        """
        return assemble_intelligent_prompt(
            query=query,
            classification=classification,
            context=context,
            response_type=response_type
        )

    def get_response_type_prompt(self, response_type: str) -> str:
        """
        Get response-type-specific formatting instructions.

        Args:
            response_type: ResponseType value (ANSWER/PLAN_PROPOSAL/etc.)

        Returns:
            Formatting instructions for specified response type

        Example:
            >>> instructions = manager.get_response_type_prompt("PLAN_PROPOSAL")
            >>> print(instructions)
            'Format your response as numbered steps...'
        """
        return get_response_type_prompt(response_type)

    # Utility Methods

    def get_token_count_estimate(self, tier: PromptTier) -> int:
        """
        Get estimated token count for prompt tier.

        Args:
            tier: Prompt tier

        Returns:
            Estimated token count

        Example:
            >>> manager.get_token_count_estimate(PromptTier.BRIEF)
            90
        """
        token_counts = {
            PromptTier.MINIMAL: 30,
            PromptTier.BRIEF: 90,
            PromptTier.STANDARD: 210,
        }
        return token_counts[tier]

    def select_tier_by_complexity(self, complexity: str) -> PromptTier:
        """
        Select appropriate prompt tier based on query complexity.

        Args:
            complexity: Query complexity (simple/moderate/complex/expert)

        Returns:
            Recommended prompt tier

        Example:
            >>> manager.select_tier_by_complexity("simple")
            PromptTier.MINIMAL
        """
        complexity_to_tier = {
            "simple": PromptTier.MINIMAL,
            "moderate": PromptTier.BRIEF,
            "complex": PromptTier.STANDARD,
            "expert": PromptTier.STANDARD,
        }
        return complexity_to_tier.get(complexity, PromptTier.STANDARD)

    def get_examples_by_intent(
        self,
        intent: str,
        num_examples: int = 3
    ) -> List[Dict[str, str]]:
        """
        Get few-shot examples for specific intent.

        Args:
            intent: QueryIntent value
            num_examples: Number of examples to retrieve

        Returns:
            List of example dictionaries

        Example:
            >>> examples = manager.get_examples_by_intent("troubleshooting", 2)
            >>> len(examples)
            2
        """
        return get_examples_by_intent(intent, num_examples)

    def get_examples_by_response_type(
        self,
        response_type: str,
        num_examples: int = 3
    ) -> List[Dict[str, str]]:
        """
        Get few-shot examples for specific response type.

        Args:
            response_type: ResponseType value
            num_examples: Number of examples to retrieve

        Returns:
            List of example dictionaries

        Example:
            >>> examples = manager.get_examples_by_response_type("PLAN_PROPOSAL", 2)
            >>> len(examples)
            2
        """
        return get_examples_by_response_type(response_type, num_examples)


# Singleton instance for global access
_prompt_manager_instance: Optional[PromptManager] = None


def get_prompt_manager() -> PromptManager:
    """
    Get singleton PromptManager instance.

    Returns:
        Global PromptManager instance

    Example:
        >>> manager = get_prompt_manager()
        >>> prompt = manager.get_system_prompt()
    """
    global _prompt_manager_instance
    if _prompt_manager_instance is None:
        _prompt_manager_instance = PromptManager()
    return _prompt_manager_instance
