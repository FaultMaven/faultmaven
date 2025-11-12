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


    def inject_v3_context(
        self,
        base_prompt: str,
        investigation_state: Any,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Inject v3.0 context: loop-back, degraded mode, working conclusion, Phase 5 entry

        Args:
            base_prompt: Base phase prompt
            investigation_state: Current investigation state
            context: Optional context dict with phase-specific data

        Returns:
            Enhanced prompt with v3.0 context injected
        """
        from faultmaven.models.investigation import InvestigationPhase

        enhanced_prompt = base_prompt
        current_phase = investigation_state.lifecycle.current_phase
        context = context or {}

        # 1. Loop-back prompt injection (if in loop-back scenario)
        if context.get('is_loop_back'):
            loop_back_prompt = self._get_loopback_prompt(
                investigation_state,
                context.get('loop_back_pattern'),
                context.get('loop_back_count', 0),
            )
            if loop_back_prompt:
                enhanced_prompt = f"{loop_back_prompt}\n\n---\n\n{enhanced_prompt}"

        # 2. Degraded mode prompt injection (overrides engagement mode layer)
        escalation_state = investigation_state.lifecycle.escalation_state
        if escalation_state.operating_in_degraded_mode:
            degraded_prompt = self._get_degraded_mode_prompt(
                escalation_state,
                investigation_state.metadata.current_turn,
            )
            if degraded_prompt:
                enhanced_prompt = f"{degraded_prompt}\n\n---\n\n{enhanced_prompt}"

        # 3. Phase 5 entry mode context injection
        if current_phase == InvestigationPhase.SOLUTION and context.get('phase5_entry_mode'):
            entry_mode_context = self._get_phase5_entry_context(
                context['phase5_entry_mode'],
                investigation_state,
            )
            if entry_mode_context:
                enhanced_prompt = f"{entry_mode_context}\n\n---\n\n{enhanced_prompt}"

        # 4. Working conclusion injection (when confidence < 90%)
        working_conclusion = investigation_state.lifecycle.working_conclusion
        if working_conclusion and working_conclusion.confidence < 0.90:
            wc_context = self._format_working_conclusion(working_conclusion)
            enhanced_prompt = f"{enhanced_prompt}\n\n{wc_context}"

        # 5. Progress summary (every 5 turns)
        if investigation_state.metadata.current_turn % 5 == 0:
            progress_summary = self._format_progress_summary(
                investigation_state.lifecycle.progress_metrics
            )
            enhanced_prompt = f"{enhanced_prompt}\n\n{progress_summary}"

        return enhanced_prompt

    def _get_loopback_prompt(
        self,
        investigation_state: Any,
        pattern: Optional[str],
        loop_count: int,
    ) -> str:
        """Get loop-back prompt based on pattern"""
        from faultmaven.prompts.investigation.loopback_prompts import (
            get_hypothesis_refutation_loopback_prompt,
            get_scope_change_loopback_prompt,
            get_timeline_wrong_loopback_prompt,
        )

        working_conclusion = investigation_state.lifecycle.working_conclusion
        if not working_conclusion:
            return ""

        wc_dict = {
            'statement': working_conclusion.statement,
            'confidence': working_conclusion.confidence,
            'confidence_level': working_conclusion.confidence_level.value,
            'generated_at_turn': working_conclusion.generated_at_turn,
        }

        if pattern == 'hypothesis_refutation':
            return get_hypothesis_refutation_loopback_prompt(
                investigation_state,
                loop_count,
                wc_dict,
                "All hypotheses refuted by validation evidence",
            )
        elif pattern == 'scope_change':
            return get_scope_change_loopback_prompt(
                investigation_state,
                loop_count,
                "Validation revealed broader scope than initially assessed",
            )
        elif pattern == 'timeline_wrong':
            return get_timeline_wrong_loopback_prompt(
                investigation_state,
                loop_count,
                "Evidence contradicts initial timeline assessment",
            )
        return ""

    def _get_degraded_mode_prompt(
        self,
        escalation_state: Any,
        current_turn: int,
    ) -> str:
        """Get degraded mode prompt"""
        from faultmaven.prompts.investigation.degraded_mode_prompts import get_degraded_mode_prompt

        if not escalation_state.degraded_mode_type:
            return ""

        return get_degraded_mode_prompt(
            escalation_state.degraded_mode_type,
            escalation_state.degraded_mode_explanation or "Unknown limitation",
            current_turn,
            escalation_state.entered_degraded_mode_at_turn or current_turn,
        )

    def _get_phase5_entry_context(
        self,
        entry_mode: str,
        investigation_state: Any,
    ) -> str:
        """Get Phase 5 entry mode context"""
        from faultmaven.prompts.investigation.phase5_entry_modes import get_phase5_entry_mode_context

        return get_phase5_entry_mode_context(entry_mode, investigation_state)

    def _format_working_conclusion(self, working_conclusion: Any) -> str:
        """Format working conclusion for prompt injection"""
        return f"""
## Current Working Conclusion (v3.0)

**Statement**: {working_conclusion.statement}
**Confidence**: {working_conclusion.confidence*100:.0f}% ({working_conclusion.confidence_level.value})
**Supporting Evidence**: {working_conclusion.supporting_evidence_count}/{working_conclusion.total_evidence_count}
**Evidence Completeness**: {working_conclusion.evidence_completeness*100:.0f}%

**Caveats**:
{chr(10).join(f"- {c}" for c in working_conclusion.caveats) if working_conclusion.caveats else "- None"}

**Alternative Explanations**:
{chr(10).join(f"- {a}" for a in working_conclusion.alternative_explanations) if working_conclusion.alternative_explanations else "- None"}

**Next Evidence Needed**:
{chr(10).join(f"- {e}" for e in working_conclusion.next_evidence_needed) if working_conclusion.next_evidence_needed else "- None"}

**Can Proceed to Solution**: {"Yes (≥70% confidence)" if working_conclusion.can_proceed_with_solution else "No (<70% confidence)"}
"""

    def _format_progress_summary(self, progress_metrics: Any) -> str:
        """Format progress summary for 5-turn checkpoints"""
        return f"""
## Investigation Progress Summary (Every 5 Turns)

**Evidence Completeness**: {progress_metrics.evidence_completeness*100:.0f}%
**Investigation Momentum**: {progress_metrics.investigation_momentum.value.upper()}
**Turns Since Last Progress**: {progress_metrics.turns_since_last_progress}
**Active Hypotheses**: {progress_metrics.active_hypotheses_count}
**Hypotheses with Sufficient Evidence** (≥70%): {progress_metrics.hypotheses_with_sufficient_evidence}
**Highest Hypothesis Confidence**: {progress_metrics.highest_hypothesis_confidence*100:.0f}%

**Next Steps**:
{chr(10).join(f"- {s}" for s in progress_metrics.next_steps) if progress_metrics.next_steps else "- Continue investigation"}

{f"**Blocked Reasons**:{chr(10)}{chr(10).join(f'- {r}' for r in progress_metrics.blocked_reasons)}" if progress_metrics.blocked_reasons else ""}
"""


# Module-level singleton
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
