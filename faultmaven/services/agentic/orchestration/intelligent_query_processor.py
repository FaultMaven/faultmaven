"""Intelligent Query Processor - Orchestration Layer

This module provides a clean orchestration layer for intelligent query processing,
separating the complexity of classification, state management, and prompt assembly
from the main AgentService.

Design Principles:
- Clean separation of concerns
- Graceful fallback on errors
- Feature flag support for safe rollout
- Comprehensive logging at decision points
"""

import logging
from typing import Dict, Any, Optional, Tuple
from dataclasses import asdict
from collections import defaultdict

from faultmaven.models.agentic import QueryIntent, QueryClassification
from faultmaven.models.api import ResponseType
from faultmaven.services.agentic.engines.classification_engine import QueryClassificationEngine
from faultmaven.services.agentic.orchestration.response_type_selector import ResponseTypeSelector
from faultmaven.services.agentic.management.conversation_state_manager import (
    ConversationStateManager,
    ConversationState,
)
from faultmaven.prompts import (
    get_system_prompt,
    get_tiered_prompt,  # Phase 2: Optimized tiered prompts
    assemble_intelligent_prompt,
    format_intelligent_few_shot_prompt,  # Task 2: Enhanced few-shot (deprecated)
    format_pattern_prompt,  # Phase 3: Optimized pattern templates
)
from faultmaven.config.settings import get_settings

logger = logging.getLogger(__name__)


class TokenUsageTracker:
    """Track token usage for optimization monitoring"""

    def __init__(self):
        """Initialize token usage tracker"""
        self.usage_by_response_type = defaultdict(list)
        self.usage_by_complexity = defaultdict(list)
        self.total_requests = 0
        self.total_tokens = 0

    def record_usage(
        self,
        prompt_tokens: int,
        response_type: str,
        complexity: str,
        breakdown: Optional[Dict[str, int]] = None
    ):
        """Record token usage for a request

        Args:
            prompt_tokens: Total prompt tokens
            response_type: Response type identifier
            complexity: Query complexity level
            breakdown: Optional breakdown by component
        """
        self.total_requests += 1
        self.total_tokens += prompt_tokens

        self.usage_by_response_type[response_type].append(prompt_tokens)
        self.usage_by_complexity[complexity].append(prompt_tokens)

        # Log detailed usage
        logger.info(
            f"Token usage recorded: {prompt_tokens} tokens "
            f"(response_type={response_type}, complexity={complexity})",
            extra={
                "token_usage": {
                    "total_tokens": prompt_tokens,
                    "response_type": response_type,
                    "complexity": complexity,
                    "breakdown": breakdown or {},
                }
            }
        )

    def get_statistics(self) -> Dict[str, Any]:
        """Get usage statistics

        Returns:
            Dictionary with usage statistics
        """
        def _calc_avg(values):
            return sum(values) // len(values) if values else 0

        stats = {
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "average_tokens": self.total_tokens // self.total_requests if self.total_requests else 0,
            "by_response_type": {},
            "by_complexity": {}
        }

        for response_type, tokens in self.usage_by_response_type.items():
            stats["by_response_type"][response_type] = {
                "count": len(tokens),
                "average": _calc_avg(tokens),
                "min": min(tokens) if tokens else 0,
                "max": max(tokens) if tokens else 0,
            }

        for complexity, tokens in self.usage_by_complexity.items():
            stats["by_complexity"][complexity] = {
                "count": len(tokens),
                "average": _calc_avg(tokens),
                "min": min(tokens) if tokens else 0,
                "max": max(tokens) if tokens else 0,
            }

        return stats


class IntelligentQueryProcessor:
    """Orchestrates intelligent query processing with fallback handling"""

    def __init__(
        self,
        classification_engine: QueryClassificationEngine,
        response_selector: ResponseTypeSelector,
    ):
        """Initialize intelligent query processor

        Args:
            classification_engine: Query classification engine
            response_selector: Response type selector
        """
        self.classification_engine = classification_engine
        self.response_selector = response_selector
        self.state_manager = ConversationStateManager()  # Manages its own Redis connection
        self.token_tracker = TokenUsageTracker()  # Track token usage for optimization monitoring

        # Feature flag from settings system (proper configuration management)
        settings = get_settings()
        self.enabled = settings.features.enable_intelligent_prompts

        logger.info(
            f"IntelligentQueryProcessor initialized (enabled={self.enabled}, "
            f"token_tracking=enabled)"
        )

    async def process_query(
        self,
        sanitized_query: str,
        session_id: str,
        case_id: str,
        conversation_context: str = "",
    ) -> Tuple[str, ResponseType, Dict[str, Any]]:
        """Process query with intelligent classification and prompt assembly

        Args:
            sanitized_query: Sanitized user query
            session_id: Session identifier
            case_id: Case identifier
            conversation_context: Conversation history context

        Returns:
            Tuple of (assembled_prompt, response_type, metadata)
        """
        if not self.enabled:
            # Feature flag disabled - use legacy processing
            logger.debug("Intelligent prompts disabled via feature flag")
            return await self._legacy_processing(sanitized_query, conversation_context)

        try:
            # Step 1: Classify query with error fallback
            classification = await self._classify_with_fallback(
                sanitized_query, session_id, case_id
            )

            # Step 2: Get conversation state with error fallback
            conv_state = await self._get_state_with_fallback(session_id, case_id)

            # Step 3: Select response type with error fallback
            response_type = await self._select_response_type_with_fallback(
                classification, conv_state, case_id
            )

            # Step 4: Assemble intelligent prompt with error fallback
            prompt = await self._assemble_prompt_with_fallback(
                classification, response_type, conv_state, sanitized_query, conversation_context
            )

            # Step 5: Record turn (fire and forget - don't block on errors)
            await self._record_turn_safe(
                session_id, case_id, classification, response_type
            )

            # Step 6: Build metadata
            metadata = {
                "processing_mode": "intelligent",
                "classification": {
                    "intent": classification.intent.value,
                    "confidence": classification.confidence,
                    "complexity": classification.complexity,
                    "domain": classification.domain,
                },
                "response_type": response_type.value,
                "conversation_state": {
                    "phase": conv_state.current_phase,
                    "turn": conv_state.total_turns,
                    "frustration_score": conv_state.frustration_score,
                },
            }

            logger.info(
                f"Intelligent processing complete: intent={classification.intent.value}, "
                f"response_type={response_type.value}, phase={conv_state.current_phase}"
            )

            return prompt, response_type, metadata

        except Exception as e:
            # Comprehensive error fallback
            logger.error(
                f"Intelligent processing failed, falling back to legacy: {e}",
                exc_info=True,
            )
            return await self._legacy_processing(sanitized_query, conversation_context)

    async def _classify_with_fallback(
        self, query: str, session_id: str, case_id: str
    ) -> QueryClassification:
        """Classify query with fallback on error"""
        try:
            # Step 1: Basic classification
            classification = await self.classification_engine.classify_query(
                query, context={"session_id": session_id, "case_id": case_id}
            )

            # Step 2: Enhance with sentiment
            try:
                sentiment = self.classification_engine.detect_sentiment(query)
                classification.metadata["sentiment"] = sentiment
            except Exception as e:
                logger.warning(f"Sentiment detection failed: {e}")
                classification.metadata["sentiment"] = {
                    "primary_sentiment": "neutral",
                    "sentiment_detected": False,
                }

            # Step 3: Enhance with info completeness
            try:
                info_completeness = (
                    self.classification_engine.assess_information_completeness(query)
                )
                classification.metadata["info_completeness"] = info_completeness
            except Exception as e:
                logger.warning(f"Completeness assessment failed: {e}")
                classification.metadata["info_completeness"] = {
                    "completeness_level": "moderate",
                    "needs_clarification": False,
                }

            return classification

        except Exception as e:
            logger.error(f"Classification failed, using fallback: {e}")
            # Return minimal fallback classification
            return QueryClassification(
                query=query,
                normalized_query=query.lower(),
                intent=QueryIntent.UNKNOWN,
                confidence=0.3,
                complexity="moderate",
                domain="general",
                urgency="medium",
                entities=[],
                context={},
                classification_method="fallback",
                processing_recommendations={},
                metadata={"error": str(e)},
            )

    async def _get_state_with_fallback(
        self, session_id: str, case_id: str
    ) -> ConversationState:
        """Get conversation state with fallback on error"""
        try:
            return await self.state_manager.get_state(session_id, case_id)
        except Exception as e:
            logger.warning(f"State retrieval failed, using empty state: {e}")
            # Return empty state on error
            return ConversationState(session_id=session_id, case_id=case_id)

    async def _select_response_type_with_fallback(
        self,
        classification: QueryClassification,
        conv_state: ConversationState,
        case_id: str,
    ) -> ResponseType:
        """Select response type with fallback on error"""
        try:
            return self.response_selector.select_response_type(
                classification=classification,
                conversation_state=asdict(conv_state),
                case_context={"case_id": case_id},
            )
        except Exception as e:
            logger.error(f"Response type selection failed, using default: {e}")
            # Default to CLARIFICATION_REQUEST on error
            return ResponseType.CLARIFICATION_REQUEST

    async def _assemble_prompt_with_fallback(
        self,
        classification: QueryClassification,
        response_type: ResponseType,
        conv_state: ConversationState,
        sanitized_query: str,
        conversation_context: str,
    ) -> str:
        """Assemble prompt with fallback on error"""
        try:
            # Phase 2: Get optimized tiered system prompt (30/90/210 tokens vs 2,000)
            base_prompt = get_tiered_prompt(
                response_type=response_type.value,
                complexity=classification.complexity
            )

            # Log token optimization
            estimated_tokens = len(base_prompt) // 4
            logger.debug(
                f"Tiered prompt selected: {estimated_tokens} tokens "
                f"(response_type={response_type.value}, complexity={classification.complexity})"
            )

            # Determine boundary type if applicable
            boundary_type = None
            if classification.intent in [
                QueryIntent.OFF_TOPIC,
                QueryIntent.META_FAULTMAVEN,
                QueryIntent.GREETING,
                QueryIntent.GRATITUDE,
                QueryIntent.CONVERSATION_CONTROL,
            ]:
                boundary_type = classification.intent.value

            # Convert to dict if needed
            conv_state_dict = asdict(conv_state) if hasattr(conv_state, '__dataclass_fields__') else conv_state
            classification_dict = classification.dict() if hasattr(classification, 'dict') else asdict(classification)

            # Assemble intelligent prompt with optimized base
            prompt = assemble_intelligent_prompt(
                base_system_prompt=base_prompt,
                response_type=response_type,
                conversation_state=conv_state_dict,
                query_classification=classification_dict,
                boundary_type=boundary_type,
            )

            # Phase 3: Add optimized pattern templates as SYSTEM INSTRUCTIONS (100-200 tokens vs 1,500)
            # Wrapped to prevent LLM from echoing them to user
            pattern_section = format_pattern_prompt(
                response_type=response_type,
                domain=classification.domain
            )
            pattern_tokens = 0
            if pattern_section:
                pattern_tokens = len(pattern_section) // 4
                logger.debug(f"Pattern template added: {pattern_tokens} tokens (domain={classification.domain})")
                # CRITICAL: Wrap patterns in clear instruction boundary to prevent echo
                prompt += f"\n\n===== SYSTEM INSTRUCTIONS (DO NOT DISPLAY TO USER) =====\n{pattern_section}\n===== END SYSTEM INSTRUCTIONS =====\n"

            # Add conversation context AFTER instructions (user-facing content)
            context_tokens = 0
            if conversation_context:
                context_tokens = len(conversation_context) // 4
                prompt += f"\n\n## Conversation History\n{conversation_context}"

            # Add current query (user-facing content)
            query_tokens = len(sanitized_query) // 4
            prompt += f"\n\n---\n\n## Current User Query\n\n{sanitized_query}"

            # Calculate total and track usage
            total_tokens = len(prompt) // 4

            # Detailed breakdown for monitoring
            breakdown = {
                "base_system_prompt": estimated_tokens,
                "response_prompt": 25,  # Compressed response-type prompt
                "pattern_template": pattern_tokens,
                "conversation_context": context_tokens,
                "user_query": query_tokens,
                "total": total_tokens,
            }

            # Track token usage
            self.token_tracker.record_usage(
                prompt_tokens=total_tokens,
                response_type=response_type.value,
                complexity=classification.complexity,
                breakdown=breakdown
            )

            logger.info(
                f"Prompt assembled: ~{total_tokens} tokens "
                f"(optimized from ~6,000 baseline, {int((1 - total_tokens/6000) * 100)}% reduction)",
                extra={"token_breakdown": breakdown}
            )

            return prompt

        except Exception as e:
            logger.error(f"Prompt assembly failed, using legacy: {e}")
            # Fallback to simple prompt
            return self._build_legacy_prompt(sanitized_query, conversation_context)

    async def _record_turn_safe(
        self,
        session_id: str,
        case_id: str,
        classification: QueryClassification,
        response_type: ResponseType,
    ):
        """Record turn safely (don't fail on error)"""
        try:
            # Convert to dict if needed
            classification_dict = classification.dict() if hasattr(classification, 'dict') else asdict(classification)

            await self.state_manager.record_turn(
                session_id=session_id,
                query_classification=classification_dict,
                response_type=response_type.value,
                case_id=case_id,
            )
        except Exception as e:
            # Log but don't fail - state tracking is non-critical
            logger.warning(f"Failed to record conversation turn: {e}")

    async def _legacy_processing(
        self, sanitized_query: str, conversation_context: str
    ) -> Tuple[str, ResponseType, Dict[str, Any]]:
        """Legacy processing (simple prompt without intelligence)"""
        prompt = self._build_legacy_prompt(sanitized_query, conversation_context)

        metadata = {
            "processing_mode": "legacy",
            "reason": "feature_flag_disabled_or_error",
        }

        # Default response type
        response_type = ResponseType.ANSWER

        return prompt, response_type, metadata

    def _build_legacy_prompt(
        self, sanitized_query: str, conversation_context: str
    ) -> str:
        """Build simple legacy prompt (still uses optimized tiered prompts)"""
        parts = []

        # Use optimized tiered prompt even in legacy mode (default to STANDARD)
        base_prompt = get_tiered_prompt(
            response_type="ANSWER",
            complexity="moderate"
        )
        parts.append(base_prompt)

        # Conversation context
        if conversation_context:
            parts.append(f"\nConversation History:\n{conversation_context}")

        # Current query
        parts.append(f"\n\n---\n\n## Current User Query\n\n{sanitized_query}")

        # Simple instruction
        parts.append(
            """
## Your Task
Analyze the user's query and provide helpful troubleshooting assistance.
"""
        )

        return "\n\n".join(parts)

    def get_token_statistics(self) -> Dict[str, Any]:
        """Get token usage statistics

        Returns:
            Dictionary with detailed token usage statistics

        Example:
            >>> stats = processor.get_token_statistics()
            >>> print(f"Average tokens: {stats['average_tokens']}")
            >>> print(f"Total requests: {stats['total_requests']}")
        """
        stats = self.token_tracker.get_statistics()

        # Add optimization metrics
        if stats["total_requests"] > 0:
            baseline_tokens = 6000  # Pre-optimization baseline
            actual_average = stats["average_tokens"]
            savings_pct = int((1 - actual_average / baseline_tokens) * 100)

            stats["optimization_metrics"] = {
                "baseline_tokens": baseline_tokens,
                "actual_average": actual_average,
                "savings_percentage": savings_pct,
                "total_tokens_saved": (baseline_tokens - actual_average) * stats["total_requests"],
            }

        return stats
