"""Response Type Selector Component

This component serves as the bridge between QueryClassification (intent) and
ResponseType (agent response strategy). It determines HOW the agent should respond
based on WHAT the user wants.

Key Responsibilities:
- Map QueryIntent to appropriate ResponseType
- Consider conversation state and case context
- Handle boundary cases (off-topic, meta, control)
- Apply rule-based ResponseType selection logic

Design Principle:
- Intent = What user wants (from classification)
- ResponseType = How agent responds (from this selector)
"""

import logging
from typing import Dict, Any, Optional
from datetime import datetime

from faultmaven.models.agentic import QueryIntent, QueryClassification
from faultmaven.models.api import ResponseType

logger = logging.getLogger(__name__)


class ResponseTypeSelector:
    """Selects appropriate ResponseType based on classification and context"""

    def __init__(self):
        """Initialize ResponseTypeSelector with rule-based mapping"""
        self._init_intent_to_response_mapping()
        logger.info("ResponseTypeSelector initialized with rule-based logic")

    def _init_intent_to_response_mapping(self):
        """Initialize intent-to-response type mapping rules"""

        # Primary mapping: Intent → Default ResponseType
        self.intent_response_mapping = {
            # Technical troubleshooting intents
            QueryIntent.TROUBLESHOOTING: ResponseType.CLARIFICATION_REQUEST,  # Usually need more info
            QueryIntent.PROBLEM_RESOLUTION: ResponseType.PLAN_PROPOSAL,  # Propose solution steps
            QueryIntent.ROOT_CAUSE_ANALYSIS: ResponseType.PLAN_PROPOSAL,  # Investigation plan
            QueryIntent.INCIDENT_RESPONSE: ResponseType.PLAN_PROPOSAL,  # Response plan

            # Informational intents
            QueryIntent.EXPLANATION: ResponseType.ANSWER,  # Direct explanation
            QueryIntent.INFORMATION: ResponseType.ANSWER,  # Information retrieval
            QueryIntent.DOCUMENTATION: ResponseType.ANSWER,  # Doc reference

            # Status and verification intents
            QueryIntent.STATUS_CHECK: ResponseType.ANSWER,  # Status info
            QueryIntent.MONITORING: ResponseType.ANSWER,  # Monitoring data

            # Configuration intents
            QueryIntent.CONFIGURATION: ResponseType.PLAN_PROPOSAL,  # Config steps
            QueryIntent.OPTIMIZATION: ResponseType.PLAN_PROPOSAL,  # Optimization plan
            QueryIntent.BEST_PRACTICES: ResponseType.ANSWER,  # Best practice info

            # Conversation intelligence intents
            QueryIntent.OFF_TOPIC: ResponseType.ANSWER,  # Polite boundary response
            QueryIntent.META_FAULTMAVEN: ResponseType.ANSWER,  # About FaultMaven
            QueryIntent.CONVERSATION_CONTROL: ResponseType.ANSWER,  # Acknowledge control
            QueryIntent.GREETING: ResponseType.ANSWER,  # Greeting response
            QueryIntent.GRATITUDE: ResponseType.ANSWER,  # Acknowledgment

            # Fallback
            QueryIntent.UNKNOWN: ResponseType.CLARIFICATION_REQUEST  # Ask for clarification
        }

        # Sentiment-based overrides
        self.sentiment_overrides = {
            "frustration": ResponseType.CLARIFICATION_REQUEST,  # Empathetic clarification
            "confusion": ResponseType.CLARIFICATION_REQUEST,  # Clear explanation needed
            "urgency": ResponseType.PLAN_PROPOSAL,  # Quick action plan
        }

        # Information completeness overrides
        self.completeness_overrides = {
            "low": ResponseType.CLARIFICATION_REQUEST,  # Need more info
            "moderate": None,  # Use default mapping
            "high": None  # Use default mapping
        }

    def select_response_type(
        self,
        classification: QueryClassification,
        conversation_state: Optional[Dict[str, Any]] = None,
        case_context: Optional[Dict[str, Any]] = None
    ) -> ResponseType:
        """Select appropriate ResponseType based on classification and context

        Args:
            classification: QueryClassification result from engine
            conversation_state: Current conversation state (optional)
            case_context: Case-specific context (optional)

        Returns:
            ResponseType: Selected response strategy
        """
        try:
            intent = classification.intent

            # Step 1: Get base ResponseType from intent mapping
            base_response_type = self.intent_response_mapping.get(
                intent,
                ResponseType.CLARIFICATION_REQUEST  # Fallback
            )

            # Step 2: Check for sentiment-based overrides
            sentiment = classification.metadata.get("sentiment", {}).get("primary_sentiment")
            if sentiment in self.sentiment_overrides:
                override_type = self.sentiment_overrides[sentiment]
                logger.debug(
                    f"Sentiment override: {sentiment} → {override_type.value}"
                )
                return override_type

            # Step 3: Check for completeness-based overrides
            completeness = classification.metadata.get("info_completeness", {}).get(
                "completeness_level"
            )
            if completeness in self.completeness_overrides:
                override_type = self.completeness_overrides[completeness]
                if override_type:
                    logger.debug(
                        f"Completeness override: {completeness} → {override_type.value}"
                    )
                    return override_type

            # Step 4: Check conversation state for stagnation
            if conversation_state:
                if self._is_conversation_stagnant(conversation_state):
                    logger.debug("Conversation stagnation detected → ESCALATION_REQUIRED")
                    return ResponseType.ESCALATION_REQUIRED

            # Step 5: Check urgency
            if classification.urgency == "critical":
                # Critical issues need immediate action plan
                if intent not in [
                    QueryIntent.OFF_TOPIC,
                    QueryIntent.META_FAULTMAVEN,
                    QueryIntent.GREETING,
                    QueryIntent.GRATITUDE
                ]:
                    logger.debug(f"Critical urgency → PLAN_PROPOSAL")
                    return ResponseType.PLAN_PROPOSAL

            # Step 6: Special handling for specific intents

            # Off-topic: Always polite boundary
            if intent == QueryIntent.OFF_TOPIC:
                logger.debug("Off-topic query → ANSWER (boundary response)")
                return ResponseType.ANSWER

            # Meta questions: Always informative answer
            if intent == QueryIntent.META_FAULTMAVEN:
                logger.debug("Meta question → ANSWER (about FaultMaven)")
                return ResponseType.ANSWER

            # Greetings/Gratitude: Simple acknowledgment
            if intent in [QueryIntent.GREETING, QueryIntent.GRATITUDE]:
                logger.debug(f"{intent.value} → ANSWER (acknowledgment)")
                return ResponseType.ANSWER

            # Conversation control: Acknowledge and reset
            if intent == QueryIntent.CONVERSATION_CONTROL:
                logger.debug("Conversation control → ANSWER (acknowledge)")
                return ResponseType.ANSWER

            # Step 7: Return base response type
            logger.debug(
                f"Intent {intent.value} → {base_response_type.value} (base mapping)"
            )
            return base_response_type

        except Exception as e:
            logger.error(f"ResponseType selection failed: {e}")
            # Safe fallback
            return ResponseType.CLARIFICATION_REQUEST

    def _is_conversation_stagnant(self, conversation_state: Dict[str, Any]) -> bool:
        """Detect if conversation is stuck in a loop or making no progress

        Args:
            conversation_state: Current conversation state

        Returns:
            bool: True if conversation appears stagnant
        """
        try:
            # Check for repeated clarification requests
            clarification_count = conversation_state.get("clarification_count", 0)
            if clarification_count >= 3:
                logger.warning(
                    f"Stagnation: {clarification_count} clarification requests"
                )
                return True

            # Check for same phase for too long
            current_phase = conversation_state.get("current_phase")
            phase_duration = conversation_state.get("phase_duration_turns", 0)
            if current_phase and phase_duration >= 5:
                logger.warning(
                    f"Stagnation: Stuck in {current_phase} for {phase_duration} turns"
                )
                return True

            # Check for circular topic switching
            topic_history = conversation_state.get("topic_history", [])
            if len(topic_history) >= 4:
                # Check if last 4 topics show a pattern (A→B→A→B)
                if (
                    topic_history[-4] == topic_history[-2]
                    and topic_history[-3] == topic_history[-1]
                ):
                    logger.warning(f"Stagnation: Circular topic switching detected")
                    return True

            return False

        except Exception as e:
            logger.error(f"Stagnation detection failed: {e}")
            return False

    def get_response_type_metadata(
        self, response_type: ResponseType, classification: QueryClassification
    ) -> Dict[str, Any]:
        """Get metadata about the selected response type

        Args:
            response_type: Selected ResponseType
            classification: Original QueryClassification

        Returns:
            Dictionary with response type metadata
        """
        metadata = {
            "response_type": response_type.value,
            "intent": classification.intent.value,
            "selection_timestamp": datetime.utcnow().isoformat(),
            "confidence": classification.confidence,
        }

        # Add response-type-specific guidance
        if response_type == ResponseType.CLARIFICATION_REQUEST:
            metadata["guidance"] = "Ask specific questions to gather missing information"
            metadata["tone"] = "helpful and patient"

        elif response_type == ResponseType.PLAN_PROPOSAL:
            metadata["guidance"] = "Present clear, actionable steps with rationale"
            metadata["tone"] = "confident and structured"

        elif response_type == ResponseType.ANSWER:
            metadata["guidance"] = "Provide direct, comprehensive information"
            metadata["tone"] = "informative and clear"

        elif response_type == ResponseType.ESCALATION_REQUIRED:
            metadata["guidance"] = "Acknowledge limitations and suggest escalation"
            metadata["tone"] = "honest and supportive"

        # Add special handling flags
        if classification.intent == QueryIntent.OFF_TOPIC:
            metadata["special_handling"] = "boundary_response"
            metadata["redirect"] = True

        elif classification.intent == QueryIntent.META_FAULTMAVEN:
            metadata["special_handling"] = "meta_response"
            metadata["include_capabilities"] = True

        return metadata
