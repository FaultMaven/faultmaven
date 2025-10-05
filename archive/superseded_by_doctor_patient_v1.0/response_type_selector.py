"""Response Type Selector Component - v3.0 Response-Format-Driven Design

This component serves as the bridge between QueryClassification (intent) and
ResponseType (agent response strategy). It determines HOW the agent should respond
based on WHAT the user wants.

v3.0 Design Philosophy (Response-Format-Driven):
- 16 QueryIntent categories → 9 ResponseType formats (1.8:1 ratio)
- Intent taxonomy designed BACKWARD from required output formats
- Each ResponseType has strict structural requirements for frontend parsing
- TROUBLESHOOTING uses workflow state machine for dynamic ResponseType selection

Key Responsibilities:
- Map QueryIntent to appropriate ResponseType (16→9 mapping)
- Consider conversation state and case context
- Handle boundary cases (off-topic, meta, control)
- Apply confidence-based override logic for complex intents
- Support visual response formats (VISUAL_DIAGRAM, COMPARISON_TABLE)

Design Principle:
- Intent = What user wants (from classification engine)
- ResponseType = How agent responds (from this selector)
- Format = Structural requirements for LLM response (for frontend parsing)
"""

import logging
from typing import Dict, Any, Optional
from datetime import datetime

from faultmaven.models.agentic import QueryIntent, QueryClassification
from faultmaven.models.api import ResponseType

logger = logging.getLogger(__name__)


class ResponseTypeSelector:
    """Selects appropriate ResponseType based on classification and context

    v3.0 Response-Format-Driven Design:
    - 16 QueryIntent → 9 ResponseType formats (1.8:1 ratio)
    - Simple answer intents (10) → ANSWER
    - Structured plan intents (3) → PLAN_PROPOSAL
    - Visual response intents (2) → VISUAL_DIAGRAM, COMPARISON_TABLE
    - Diagnostic intent (1) → Dynamic (workflow-driven)
    - Fallback (1) → Conditional
    """

    def __init__(self):
        """Initialize ResponseTypeSelector with v3.0 response-format-driven mapping"""
        self._init_intent_to_response_mapping()
        logger.info("ResponseTypeSelector initialized with v3.0 response-format-driven logic (16→9 mapping)")

    def _init_intent_to_response_mapping(self):
        """Initialize intent-to-response type mapping rules - v3.0 Response-Format-Driven Design"""

        # GROUP 1: SIMPLE ANSWER INTENTS (10) → ResponseType.ANSWER
        # Natural prose response without structured format requirements
        self.simple_answer_intents = {
            QueryIntent.INFORMATION,  # Merged: EXPLANATION, DOCUMENTATION
            QueryIntent.STATUS_CHECK,  # Merged: MONITORING
            QueryIntent.PROCEDURAL,  # How-to and capability questions
            QueryIntent.VALIDATION,  # Hypothetical/confirmation questions
            QueryIntent.BEST_PRACTICES,
            QueryIntent.GREETING,
            QueryIntent.GRATITUDE,
            QueryIntent.OFF_TOPIC,
            QueryIntent.META_FAULTMAVEN,
            QueryIntent.CONVERSATION_CONTROL,
        }

        # GROUP 2: STRUCTURED PLAN INTENTS (3) → ResponseType.PLAN_PROPOSAL
        # Numbered steps with commands and rationale
        self.structured_plan_intents = {
            QueryIntent.CONFIGURATION,
            QueryIntent.OPTIMIZATION,
            QueryIntent.DEPLOYMENT,  # NEW v3.0
        }

        # GROUP 3: VISUAL RESPONSE INTENTS (2) → Specialized ResponseTypes
        # Mermaid diagrams and markdown tables
        self.visual_response_intents = {
            QueryIntent.VISUALIZATION,  # NEW v3.0 → VISUAL_DIAGRAM
            QueryIntent.COMPARISON,  # NEW v3.0 → COMPARISON_TABLE
        }

        # GROUP 4: DIAGNOSTIC INTENT (1) → Dynamic ResponseType (workflow-driven)
        # ResponseType determined by TroubleshootingWorkflowEngine state
        self.diagnostic_intents = {
            QueryIntent.TROUBLESHOOTING,  # Merged: PROBLEM_RESOLUTION, ROOT_CAUSE_ANALYSIS, INCIDENT_RESPONSE
        }

        # Build unified intent_response_mapping (16 intents → 9 formats)
        self.intent_response_mapping = {}

        # Group 1: Simple answer intents → ANSWER (10 intents)
        for intent in self.simple_answer_intents:
            self.intent_response_mapping[intent] = ResponseType.ANSWER

        # Group 2: Structured plan intents → PLAN_PROPOSAL (3 intents)
        for intent in self.structured_plan_intents:
            self.intent_response_mapping[intent] = ResponseType.PLAN_PROPOSAL

        # Group 3: Visual response intents → Specialized formats (2 intents)
        self.intent_response_mapping[QueryIntent.VISUALIZATION] = ResponseType.VISUAL_DIAGRAM
        self.intent_response_mapping[QueryIntent.COMPARISON] = ResponseType.COMPARISON_TABLE

        # Group 4: Diagnostic intent → Dynamic (workflow-driven, 1 intent)
        # TROUBLESHOOTING uses workflow state machine to determine ResponseType
        self.intent_response_mapping[QueryIntent.TROUBLESHOOTING] = None  # Resolved by workflow engine

        # Group 5: Fallback → Conditional (1 intent)
        self.intent_response_mapping[QueryIntent.UNKNOWN] = None  # Resolved in select_response_type()

        # Sentiment-based overrides (unchanged)
        self.sentiment_overrides = {
            "frustration": ResponseType.CLARIFICATION_REQUEST,  # Empathetic clarification
            "confusion": ResponseType.CLARIFICATION_REQUEST,  # Clear explanation needed
            "urgency": ResponseType.PLAN_PROPOSAL,  # Quick action plan
        }

        # Information completeness overrides (unchanged)
        self.completeness_overrides = {
            "low": ResponseType.CLARIFICATION_REQUEST,  # Need more info
            "moderate": None,  # Use default mapping
            "high": None  # Use default mapping
        }

        # Complex intents for confidence override logic (Groups 2, 3, 4)
        # These require high confidence to proceed without clarification
        self.complex_intents = (
            self.structured_plan_intents
            | self.visual_response_intents
            | self.diagnostic_intents
        )

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
            confidence = classification.confidence

            # Step 0: CRITICAL - Low confidence override (< 0.4)
            # Force CLARIFICATION_REQUEST for COMPLEX intents only
            # Simple answer intents (PROCEDURAL, EXPLANATION, etc.) are safe even at low confidence
            if confidence < 0.4 and intent in self.complex_intents:
                logger.warning(
                    f"Low confidence ({confidence:.2f}) + complex intent ({intent.value}) → CLARIFICATION_REQUEST override"
                )
                classification.metadata["confidence_override"] = {
                    "original_confidence": confidence,
                    "original_intent": intent.value,
                    "reason": "Below 0.4 threshold for complex intent - too risky to proceed without clarification"
                }
                return ResponseType.CLARIFICATION_REQUEST
            elif confidence < 0.4:
                # Low confidence but simple intent - log for monitoring but allow to proceed
                logger.info(
                    f"Low confidence ({confidence:.2f}) but simple intent ({intent.value}) - allowing ANSWER"
                )

            # Step 1: Handle UNKNOWN intent with conditional mapping
            if intent == QueryIntent.UNKNOWN:
                # If confidence is reasonable (≥ 0.4 but unknown intent), try simple ANSWER
                # This handles edge cases where patterns didn't match but query is clear
                if confidence >= 0.4:
                    logger.debug(
                        f"UNKNOWN intent with reasonable confidence ({confidence:.2f}) → ANSWER"
                    )
                    return ResponseType.ANSWER
                else:
                    # Very low confidence already handled in Step 0, but belt-and-suspenders
                    logger.debug("UNKNOWN intent with low confidence → CLARIFICATION_REQUEST")
                    return ResponseType.CLARIFICATION_REQUEST

            # Step 2: Get base ResponseType from intent mapping
            base_response_type = self.intent_response_mapping.get(
                intent,
                ResponseType.ANSWER  # Default to simple answer, not clarification
            )

            # Step 3: Check for sentiment-based overrides
            sentiment = classification.metadata.get("sentiment", {}).get("primary_sentiment")
            if sentiment in self.sentiment_overrides:
                override_type = self.sentiment_overrides[sentiment]
                logger.debug(
                    f"Sentiment override: {sentiment} → {override_type.value}"
                )
                return override_type

            # Step 4: Check for completeness-based overrides
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

            # Step 5: Check conversation state for stagnation
            if conversation_state:
                if self._is_conversation_stagnant(conversation_state):
                    logger.debug("Conversation stagnation detected → ESCALATION_REQUIRED")
                    return ResponseType.ESCALATION_REQUIRED

            # Step 6: Check urgency
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

            # Step 7: Special handling for specific intents (already mapped to ANSWER)
            # These are belt-and-suspenders checks since intent_response_mapping already handles them

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

            # Step 8: Return base response type
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

        # Add response-type-specific guidance (v3.0 includes visual formats)
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

        elif response_type == ResponseType.VISUAL_DIAGRAM:
            metadata["guidance"] = "Generate Mermaid diagram with clear structure and labels"
            metadata["tone"] = "visual and structured"
            metadata["format_requirements"] = "Mermaid code block with diagram type (graph, flowchart, etc.)"

        elif response_type == ResponseType.COMPARISON_TABLE:
            metadata["guidance"] = "Create markdown table with clear headers and data rows"
            metadata["tone"] = "analytical and comparative"
            metadata["format_requirements"] = "Markdown table with header row and alignment"

        # Add special handling flags
        if classification.intent == QueryIntent.OFF_TOPIC:
            metadata["special_handling"] = "boundary_response"
            metadata["redirect"] = True

        elif classification.intent == QueryIntent.META_FAULTMAVEN:
            metadata["special_handling"] = "meta_response"
            metadata["include_capabilities"] = True

        elif classification.intent == QueryIntent.VISUALIZATION:
            metadata["special_handling"] = "visual_diagram_generation"
            metadata["validate_mermaid_syntax"] = True

        elif classification.intent == QueryIntent.COMPARISON:
            metadata["special_handling"] = "comparison_table_generation"
            metadata["validate_markdown_table"] = True

        return metadata
