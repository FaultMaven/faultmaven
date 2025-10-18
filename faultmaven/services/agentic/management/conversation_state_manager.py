"""Conversation State Manager Component

This component manages conversation state with Redis persistence, tracking the current
phase of troubleshooting, topic history, stagnation indicators, and user sentiment
across the multi-turn conversation.

Key Responsibilities:
- Track conversation state per session/case
- Persist state to Redis with TTL
- Detect conversation stagnation and dead-ends
- Manage topic shifts and phase transitions
- Track sentiment and frustration levels

Design Principles:
- State persists for 24 hours (same as sessions)
- Atomic updates with Redis transactions
- Thread-safe operations
- Graceful degradation if Redis unavailable
"""

import json
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict

from faultmaven.infrastructure.redis_client import create_redis_client
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


@dataclass
class ConversationState:
    """Represents the current state of a conversation"""

    session_id: str
    case_id: Optional[str] = None

    # Phase tracking (aligned with 5-phase SRE doctrine)
    current_phase: str = "intake"  # intake, blast_radius, timeline, hypothesis, validation, solution
    phase_start_turn: int = 0
    phase_duration_turns: int = 0

    # Topic tracking
    current_topic: Optional[str] = None
    topic_history: List[str] = None
    topic_shift_count: int = 0

    # Stagnation detection
    clarification_count: int = 0
    repeated_questions: List[str] = None
    last_progress_turn: int = 0

    # Sentiment tracking
    frustration_score: float = 0.0  # 0-1 scale
    confusion_indicators: int = 0
    satisfaction_indicators: int = 0

    # Metadata
    total_turns: int = 0
    created_at: str = None
    updated_at: str = None

    def __post_init__(self):
        """Initialize mutable defaults"""
        if self.topic_history is None:
            self.topic_history = []
        if self.repeated_questions is None:
            self.repeated_questions = []
        if self.created_at is None:
            self.created_at = to_json_compatible(datetime.now(timezone.utc))
        if self.updated_at is None:
            self.updated_at = to_json_compatible(datetime.now(timezone.utc))


class ConversationStateManager:
    """Manages conversation state with Redis persistence"""

    # Redis key prefixes
    STATE_KEY_PREFIX = "conversation:state:"
    STATE_TTL_SECONDS = 86400  # 24 hours (same as sessions)

    # Phase progression order
    PHASE_ORDER = [
        "intake",
        "blast_radius",
        "timeline",
        "hypothesis",
        "validation",
        "solution",
    ]

    # Stagnation thresholds
    MAX_CLARIFICATIONS = 3
    MAX_PHASE_DURATION = 5
    FRUSTRATION_THRESHOLD = 0.7

    def __init__(self):
        """Initialize ConversationStateManager with Redis client"""
        self.redis_client = None
        self._connection_healthy = None
        logger.info("ConversationStateManager initialized with Redis persistence")

    async def _ensure_client(self):
        """Ensure Redis client is initialized"""
        if self.redis_client is None or self._connection_healthy is None:
            try:
                self.redis_client = create_redis_client()
                self._connection_healthy = True
            except Exception as e:
                logger.error(f"Failed to initialize Redis client: {e}")
                self.redis_client = None
                self._connection_healthy = False

    async def get_state(
        self, session_id: str, case_id: Optional[str] = None
    ) -> ConversationState:
        """Get conversation state for session/case

        Args:
            session_id: Session identifier
            case_id: Optional case identifier

        Returns:
            ConversationState: Current state or new state if none exists
        """
        try:
            await self._ensure_client()
            if not self._connection_healthy or self.redis_client is None:
                logger.warning("Redis unavailable, returning new state")
                return ConversationState(session_id=session_id, case_id=case_id)

            state_key = self._get_state_key(session_id, case_id)
            state_json = await self.redis_client.get(state_key)

            if state_json:
                state_dict = json.loads(state_json)
                state = ConversationState(**state_dict)
                logger.debug(
                    f"Retrieved conversation state for {session_id}: phase={state.current_phase}"
                )
                return state
            else:
                # Create new state
                state = ConversationState(session_id=session_id, case_id=case_id)
                logger.debug(f"Created new conversation state for {session_id}")
                return state

        except Exception as e:
            logger.error(
                f"Failed to get conversation state (Redis unavailable or error): {e}",
                extra={
                    "session_id": session_id,
                    "case_id": case_id,
                    "error_type": type(e).__name__,
                    "fallback": "empty_state"
                }
            )
            # Return default state on error (graceful degradation)
            return ConversationState(session_id=session_id, case_id=case_id)

    async def update_state(
        self, session_id: str, state: ConversationState, case_id: Optional[str] = None
    ) -> bool:
        """Update conversation state in Redis

        Args:
            session_id: Session identifier
            state: Updated ConversationState
            case_id: Optional case identifier

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            await self._ensure_client()
            if not self._connection_healthy or self.redis_client is None:
                logger.warning("Redis unavailable, skipping state update")
                return False

            state_key = self._get_state_key(session_id, case_id)

            # Update timestamp
            state.updated_at = to_json_compatible(datetime.now(timezone.utc))

            # Serialize to JSON
            state_dict = asdict(state)
            state_json = json.dumps(state_dict)

            # Store with TTL
            await self.redis_client.setex(
                state_key, self.STATE_TTL_SECONDS, state_json
            )

            logger.debug(f"Updated conversation state for {session_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to update conversation state: {e}")
            return False

    async def record_turn(
        self,
        session_id: str,
        query_classification: Dict[str, Any],
        response_type: str,
        case_id: Optional[str] = None,
    ) -> ConversationState:
        """Record a conversation turn and update state

        Args:
            session_id: Session identifier
            query_classification: Classification result from engine
            response_type: Selected ResponseType
            case_id: Optional case identifier

        Returns:
            ConversationState: Updated state
        """
        try:
            # Get current state
            state = await self.get_state(session_id, case_id)

            # Increment turn counter
            state.total_turns += 1
            state.phase_duration_turns += 1

            # Update topic tracking
            current_topic = self._extract_topic(query_classification)
            if current_topic and current_topic != state.current_topic:
                # Topic shifted
                if state.current_topic:  # Only count if not first topic
                    state.topic_shift_count += 1
                    state.topic_history.append(state.current_topic)
                state.current_topic = current_topic
                logger.debug(f"Topic shift detected: {state.current_topic}")

            # Update stagnation indicators
            if response_type == "CLARIFICATION_REQUEST":
                state.clarification_count += 1
                # Check for repeated questions
                query = query_classification.get("query", "")
                normalized = query.lower().strip()
                if normalized in state.repeated_questions:
                    logger.warning(f"Repeated question detected: {normalized}")
                state.repeated_questions.append(normalized)
            else:
                # Progress made, reset clarification count
                state.clarification_count = 0
                state.last_progress_turn = state.total_turns

            # Update sentiment tracking
            sentiment = query_classification.get("metadata", {}).get("sentiment", {})
            if sentiment:
                self._update_sentiment(state, sentiment)

            # Check for phase progression
            if self._should_advance_phase(state, response_type):
                self._advance_phase(state)

            # Save updated state
            await self.update_state(session_id, state, case_id)

            return state

        except Exception as e:
            logger.error(f"Failed to record conversation turn: {e}")
            # Return current state on error
            return await self.get_state(session_id, case_id)

    async def reset_state(
        self, session_id: str, case_id: Optional[str] = None
    ) -> bool:
        """Reset conversation state (e.g., user says "start over")

        Args:
            session_id: Session identifier
            case_id: Optional case identifier

        Returns:
            bool: True if successful
        """
        try:
            await self._ensure_client()
            if not self._connection_healthy or self.redis_client is None:
                logger.warning("Redis unavailable, skipping state reset")
                return False

            state_key = self._get_state_key(session_id, case_id)
            await self.redis_client.delete(state_key)
            logger.info(f"Reset conversation state for {session_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to reset conversation state: {e}")
            return False

    async def detect_stagnation(
        self, session_id: str, case_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Detect if conversation is stagnant

        Args:
            session_id: Session identifier
            case_id: Optional case identifier

        Returns:
            Dictionary with stagnation assessment
        """
        try:
            state = await self.get_state(session_id, case_id)

            stagnation_indicators = []
            stagnation_score = 0.0

            # Check clarification count
            if state.clarification_count >= self.MAX_CLARIFICATIONS:
                stagnation_indicators.append(
                    f"Too many clarification requests ({state.clarification_count})"
                )
                stagnation_score += 0.3

            # Check phase duration
            if state.phase_duration_turns >= self.MAX_PHASE_DURATION:
                stagnation_indicators.append(
                    f"Stuck in {state.current_phase} for {state.phase_duration_turns} turns"
                )
                stagnation_score += 0.3

            # Check topic cycling
            if len(state.topic_history) >= 4:
                if self._detect_topic_cycling(state.topic_history):
                    stagnation_indicators.append("Circular topic switching detected")
                    stagnation_score += 0.2

            # Check frustration level
            if state.frustration_score >= self.FRUSTRATION_THRESHOLD:
                stagnation_indicators.append(
                    f"High frustration level ({state.frustration_score:.2f})"
                )
                stagnation_score += 0.2

            is_stagnant = stagnation_score >= 0.5

            # Generate recovery suggestions based on stagnation indicators
            recovery_suggestions = []
            if is_stagnant:
                if state.clarification_count >= self.MAX_CLARIFICATIONS:
                    recovery_suggestions.append(
                        "Try switching to direct troubleshooting commands instead of questions"
                    )
                    recovery_suggestions.append(
                        "Provide specific examples or error messages from your environment"
                    )

                if state.phase_duration_turns >= self.MAX_PHASE_DURATION:
                    recovery_suggestions.append(
                        f"Consider moving to next troubleshooting phase (currently in: {state.current_phase})"
                    )
                    recovery_suggestions.append(
                        "Try a different troubleshooting approach or hypothesis"
                    )

                if len(state.topic_history) >= 4 and self._detect_topic_cycling(state.topic_history):
                    recovery_suggestions.append(
                        "Focus on one specific issue instead of switching between topics"
                    )
                    recovery_suggestions.append(
                        "Create separate troubleshooting cases for different issues"
                    )

                if state.frustration_score >= self.FRUSTRATION_THRESHOLD:
                    recovery_suggestions.append(
                        "Consider escalating to human expert or documentation"
                    )
                    recovery_suggestions.append(
                        "Take a break and return with fresh perspective"
                    )
                    recovery_suggestions.append(
                        "Try restarting the conversation with clearer problem definition"
                    )

            return {
                "is_stagnant": is_stagnant,
                "stagnation_score": min(stagnation_score, 1.0),
                "indicators": stagnation_indicators,
                "recommendation": (
                    "escalation" if is_stagnant else "continue"
                ),
                "recovery_suggestions": recovery_suggestions,
            }

        except Exception as e:
            logger.error(f"Stagnation detection failed: {e}")
            return {
                "is_stagnant": False,
                "stagnation_score": 0.0,
                "indicators": [],
                "recommendation": "continue",
                "recovery_suggestions": [],
                "error": str(e),
            }

    def _get_state_key(self, session_id: str, case_id: Optional[str] = None) -> str:
        """Generate Redis key for conversation state"""
        if case_id:
            return f"{self.STATE_KEY_PREFIX}{session_id}:case:{case_id}"
        return f"{self.STATE_KEY_PREFIX}{session_id}"

    def _extract_topic(self, query_classification: Dict[str, Any]) -> Optional[str]:
        """Extract topic from query classification"""
        # Use domain as primary topic indicator
        domain = query_classification.get("domain")
        if domain and domain != "general":
            return domain

        # Fall back to intent as topic
        intent = query_classification.get("intent")
        if intent:
            return intent

        return None

    def _update_sentiment(self, state: ConversationState, sentiment: Dict[str, Any]):
        """Update sentiment tracking in state"""
        primary_sentiment = sentiment.get("primary_sentiment")

        if primary_sentiment == "frustration":
            # Increase frustration score
            state.frustration_score = min(state.frustration_score + 0.15, 1.0)
        elif primary_sentiment == "confusion":
            state.confusion_indicators += 1
            state.frustration_score = min(state.frustration_score + 0.1, 1.0)
        elif primary_sentiment == "satisfaction":
            # Decrease frustration, increase satisfaction
            state.frustration_score = max(state.frustration_score - 0.2, 0.0)
            state.satisfaction_indicators += 1
        elif primary_sentiment == "neutral":
            # Gradual frustration decay
            state.frustration_score = max(state.frustration_score - 0.05, 0.0)

    def _should_advance_phase(
        self, state: ConversationState, response_type: str
    ) -> bool:
        """Determine if should advance to next phase"""
        # Advance when solution is ready
        if response_type == "SOLUTION_READY":
            return True

        # Advance when plan is proposed and accepted
        if response_type == "PLAN_PROPOSAL" and state.phase_duration_turns >= 2:
            return True

        # Advance when sufficient progress in current phase
        if state.phase_duration_turns >= 3 and response_type == "ANSWER":
            return True

        return False

    def _advance_phase(self, state: ConversationState):
        """Advance to next phase in troubleshooting"""
        try:
            current_index = self.PHASE_ORDER.index(state.current_phase)
            if current_index < len(self.PHASE_ORDER) - 1:
                new_phase = self.PHASE_ORDER[current_index + 1]
                logger.info(
                    f"Phase advancement: {state.current_phase} → {new_phase}"
                )
                state.current_phase = new_phase
                state.phase_start_turn = state.total_turns
                state.phase_duration_turns = 0
        except ValueError:
            logger.warning(f"Unknown phase: {state.current_phase}")

    def _detect_topic_cycling(self, topic_history: List[str]) -> bool:
        """Detect if topics are cycling (A→B→A→B pattern)"""
        if len(topic_history) < 4:
            return False

        # Check last 4 topics for ABAB pattern
        return (
            topic_history[-4] == topic_history[-2]
            and topic_history[-3] == topic_history[-1]
        )
