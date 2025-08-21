"""Orchestrator/Router Service - Phase B Implementation

This module implements the IOrchestratorService interface from the microservice
architecture blueprint, providing central coordination and routing with agent
selection, budget management, and state machine orchestration.

Key Features:
- Top-2 agent selection based on utility function (clarity, retrieval_score, prior_success, health, cost, latency, budget_remaining)
- Budget management with time_ms, token, and call budgets per turn enforcement
- State machine routing: gateway → agent selection → confidence checking → response
- Health & circuit breaker integration with per-agent success rate, p95 latency, error streak tracking
- 5% epsilon exploration for safe alternative sampling
- Decision records emission via DecisionRecorder for comprehensive observability

Implementation Notes:
- Uses existing Agent Service and Phase A foundation services
- Integrates with Gateway and LoopGuard services for complete workflow
- Thread-safe circuit breaker implementation with exponential backoff
- Performance optimized (p95 < 300ms routing latency)
- Event-driven architecture with decision record emissions
"""

import asyncio
import logging
import time
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set
from collections import defaultdict, deque
from threading import RLock
import json

from faultmaven.services.microservice_interfaces.core_services import IOrchestratorService
from faultmaven.services.microservice_interfaces.core_services import (
    IGlobalConfidenceService, ILoopGuardService, IGatewayProcessingService
)
from faultmaven.models.microservice_contracts.core_contracts import (
    TurnContext, DecisionRecord, Budget, ConfidenceRequest, ConfidenceResponse,
    LoopCheckRequest, ActionType
)
from faultmaven.models.interfaces import ILLMProvider, ITracer, ISanitizer
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException, BudgetExceededException


class AgentHealth:
    """Health tracking for individual agents"""
    
    def __init__(self, agent_id: str, window_size: int = 100):
        self.agent_id = agent_id
        self.window_size = window_size
        
        # Performance tracking
        self.response_times = deque(maxlen=window_size)
        self.success_count = 0
        self.error_count = 0
        self.total_requests = 0
        
        # Circuit breaker state
        self.consecutive_errors = 0
        self.circuit_open = False
        self.last_failure_time = None
        self.next_retry_time = None
        
        # Cost and performance metrics
        self.avg_tokens_used = 0.0
        self.avg_cost_estimate = 0.0
        self.last_update = datetime.utcnow()

    @property
    def success_rate(self) -> float:
        """Calculate current success rate"""
        if self.total_requests == 0:
            return 1.0  # Assume healthy for new agents
        return self.success_count / self.total_requests

    @property  
    def p95_latency(self) -> float:
        """Calculate p95 latency from recent response times"""
        if not self.response_times:
            return 0.0
        
        sorted_times = sorted(self.response_times)
        p95_index = int(len(sorted_times) * 0.95)
        return sorted_times[min(p95_index, len(sorted_times) - 1)]

    @property
    def health_score(self) -> float:
        """Calculate overall health score (0.0-1.0)"""
        if self.circuit_open:
            return 0.0
        
        # Weight factors for health calculation
        success_weight = 0.5
        latency_weight = 0.3
        freshness_weight = 0.2
        
        # Success rate component (0.0-1.0)
        success_component = self.success_rate
        
        # Latency component (inverse - lower latency = higher score)
        p95 = self.p95_latency
        latency_component = max(0.0, 1.0 - (p95 / 10000.0))  # Normalize against 10s max
        
        # Freshness component (recent usage gets bonus)
        time_since_update = (datetime.utcnow() - self.last_update).total_seconds()
        freshness_component = max(0.0, 1.0 - (time_since_update / 3600.0))  # 1 hour decay
        
        return (
            success_component * success_weight +
            latency_component * latency_weight +
            freshness_component * freshness_weight
        )

    def record_success(self, response_time_ms: float, tokens_used: int = 0) -> None:
        """Record successful request"""
        self.total_requests += 1
        self.success_count += 1
        self.consecutive_errors = 0
        self.response_times.append(response_time_ms)
        self.last_update = datetime.utcnow()
        
        # Update average token usage
        if tokens_used > 0:
            if self.total_requests == 1:
                self.avg_tokens_used = tokens_used
            else:
                self.avg_tokens_used = (self.avg_tokens_used * 0.9) + (tokens_used * 0.1)
        
        # Close circuit breaker if it was open
        if self.circuit_open:
            self.circuit_open = False
            self.next_retry_time = None

    def record_failure(self, response_time_ms: Optional[float] = None) -> None:
        """Record failed request"""
        self.total_requests += 1
        self.error_count += 1
        self.consecutive_errors += 1
        self.last_failure_time = datetime.utcnow()
        self.last_update = datetime.utcnow()
        
        if response_time_ms:
            self.response_times.append(response_time_ms)
        
        # Open circuit breaker if too many consecutive errors
        if self.consecutive_errors >= 5:  # Threshold for circuit breaker
            self.circuit_open = True
            # Exponential backoff: 2^errors seconds, max 5 minutes
            backoff_seconds = min(2 ** min(self.consecutive_errors, 8), 300)
            self.next_retry_time = datetime.utcnow() + timedelta(seconds=backoff_seconds)

    def can_accept_request(self) -> bool:
        """Check if agent can accept requests (circuit breaker logic)"""
        if not self.circuit_open:
            return True
        
        # Check if retry time has passed
        if self.next_retry_time and datetime.utcnow() >= self.next_retry_time:
            return True
        
        return False


class OrchestratorService(IOrchestratorService):
    """
    Implementation of IOrchestratorService interface
    
    Provides central coordination and routing with agent selection, budget management,
    state machine orchestration, and comprehensive decision record emission.
    """

    # Agent utility weights for selection algorithm
    UTILITY_WEIGHTS = {
        'clarity_score': 0.25,
        'retrieval_score': 0.20,
        'prior_success': 0.20,
        'cost_efficiency': 0.15,
        'latency_efficiency': 0.10,
        'health_score': 0.10
    }

    # State machine states
    STATE_TRANSITIONS = {
        'start': ['gateway_processing'],
        'gateway_processing': ['agent_selection', 'gateway_rejected'],
        'agent_selection': ['agent_execution', 'no_agents_available'],
        'agent_execution': ['confidence_evaluation', 'agent_failed'],
        'confidence_evaluation': ['loop_checking', 'low_confidence'],
        'loop_checking': ['response_generation', 'loop_recovery'],
        'response_generation': ['completed'],
        'gateway_rejected': ['completed'],
        'agent_failed': ['agent_selection', 'completed'],
        'no_agents_available': ['completed'],
        'low_confidence': ['completed'],
        'loop_recovery': ['completed'],
        'completed': []
    }

    def __init__(
        self,
        agent_service,  # Use duck typing for flexibility
        gateway_service: Optional[IGatewayProcessingService] = None,
        confidence_service: Optional[IGlobalConfidenceService] = None,
        loop_guard_service: Optional[ILoopGuardService] = None,
        decision_recorder = None,  # Use duck typing
        tracer: Optional[ITracer] = None,
        exploration_epsilon: float = 0.05,
        max_agents_per_turn: int = 2,
        circuit_breaker_enabled: bool = True
    ):
        """
        Initialize Orchestrator Service
        
        Args:
            agent_service: Core agent service for processing queries
            gateway_service: Optional gateway service for pre-processing
            confidence_service: Optional confidence service for scoring
            loop_guard_service: Optional loop guard service for stall detection
            decision_recorder: Optional decision recorder for observability
            tracer: Optional tracer for distributed tracing
            exploration_epsilon: Exploration rate for agent selection (0.0-1.0)
            max_agents_per_turn: Maximum number of agents to try per turn
            circuit_breaker_enabled: Whether to enable circuit breaker functionality
        """
        self._agent_service = agent_service
        self._gateway_service = gateway_service
        self._confidence_service = confidence_service
        self._loop_guard_service = loop_guard_service
        self._decision_recorder = decision_recorder
        self._tracer = tracer
        self._exploration_epsilon = exploration_epsilon
        self._max_agents_per_turn = max_agents_per_turn
        self._circuit_breaker_enabled = circuit_breaker_enabled
        self._logger = logging.getLogger(self.__class__.__name__)
        
        # Thread safety
        self._health_lock = RLock()
        
        # Agent health tracking
        self._agent_health: Dict[str, AgentHealth] = {}
        
        # Performance metrics
        self._metrics = {
            'turns_processed': 0,
            'gateway_rejections': 0,
            'agent_selections': 0,
            'agent_failures': 0,
            'budget_exhaustions': 0,
            'loop_detections': 0,
            'avg_routing_time_ms': 0.0,
            'circuit_breaker_trips': 0
        }
        
        # Available agent IDs (in production, this would be discovered dynamically)
        self._available_agents = ['primary_agent', 'secondary_agent', 'fallback_agent']
        
        # Initialize agent health tracking
        with self._health_lock:
            for agent_id in self._available_agents:
                self._agent_health[agent_id] = AgentHealth(agent_id)

    @trace("orchestrator_process_turn")
    async def process_turn(self, context: TurnContext) -> DecisionRecord:
        """
        Process user turn through complete orchestration pipeline
        
        Args:
            context: TurnContext with query, session, budget, and metadata
            
        Returns:
            DecisionRecord with comprehensive routing decisions and audit trail
        """
        start_time = time.time()
        session_id = context.session_id
        turn_id = context.turn_id
        
        # Initialize decision record
        decision_record = DecisionRecord(
            record_id=f"orchestrator_{turn_id}_{int(start_time)}",
            session_id=session_id,
            turn_id=turn_id,
            timestamp=datetime.utcnow(),
            service_name="orchestrator_service",
            operation_type="process_turn",
            input_data={
                'query': context.query,
                'budget': context.budget.dict(),
                'context': context.context
            },
            budget_consumed=Budget(),
            success=False,
            processing_time_ms=0
        )
        
        try:
            # State machine execution
            current_state = 'start'
            state_history = [current_state]
            
            # Step 1: Gateway processing
            current_state, gateway_result = await self._execute_gateway_processing(
                context, decision_record, current_state
            )
            state_history.append(current_state)
            
            if current_state == 'gateway_rejected':
                return await self._finalize_decision_record(
                    decision_record, state_history, start_time, 
                    "Gateway processing rejected query"
                )
            
            # Step 2: Agent selection
            current_state, selected_agents = await self._execute_agent_selection(
                context, decision_record, current_state, gateway_result
            )
            state_history.append(current_state)
            
            if current_state == 'no_agents_available':
                return await self._finalize_decision_record(
                    decision_record, state_history, start_time,
                    "No agents available for processing"
                )
            
            # Step 3: Agent execution with fallback
            current_state, agent_result = await self._execute_agent_processing(
                context, decision_record, current_state, selected_agents
            )
            state_history.append(current_state)
            
            if current_state == 'agent_failed':
                return await self._finalize_decision_record(
                    decision_record, state_history, start_time,
                    "All selected agents failed to process query"
                )
            
            # Step 4: Confidence evaluation
            current_state, confidence_result = await self._execute_confidence_evaluation(
                context, decision_record, current_state, agent_result
            )
            state_history.append(current_state)
            
            # Step 5: Loop checking
            current_state, loop_result = await self._execute_loop_checking(
                context, decision_record, current_state, confidence_result
            )
            state_history.append(current_state)
            
            if current_state == 'loop_recovery':
                return await self._finalize_decision_record(
                    decision_record, state_history, start_time,
                    "Loop detected - recovery suggestions provided",
                    agent_result
                )
            
            # Step 6: Response generation (final step)
            current_state = 'response_generation'
            state_history.append(current_state)
            
            # Finalize successful processing
            decision_record.success = True
            return await self._finalize_decision_record(
                decision_record, state_history, start_time,
                "Turn processed successfully", agent_result
            )
            
        except BudgetExceededException as e:
            self._metrics['budget_exhaustions'] += 1
            return await self._finalize_decision_record(
                decision_record, state_history, start_time,
                f"Budget exceeded: {str(e)}"
            )
            
        except Exception as e:
            self._logger.error(f"Turn processing failed for {session_id}: {e}")
            return await self._finalize_decision_record(
                decision_record, state_history, start_time,
                f"Processing error: {str(e)}"
            )

    async def _execute_gateway_processing(
        self, context: TurnContext, decision_record: DecisionRecord, current_state: str
    ) -> Tuple[str, Optional[Any]]:
        """Execute gateway processing step"""
        
        if not self._gateway_service:
            # Skip gateway processing if not available
            return 'agent_selection', None
        
        try:
            # Check budget for gateway processing
            if not context.budget.has_time_budget(50):  # 50ms budget for gateway
                raise BudgetExceededException("Insufficient time budget for gateway processing")
            
            gateway_result = await self._gateway_service.process_query(
                context.query, context.context
            )
            
            # Consume budget
            context.budget.consume_time(gateway_result.processing_time_ms)
            context.budget.consume_calls(1)
            
            # Record in decision record
            decision_record.decision_factors['gateway_processing'] = {
                'clarity_score': gateway_result.clarity_score,
                'reality_score': gateway_result.reality_score,
                'passed_filters': gateway_result.passed_filters,
                'pii_redacted': gateway_result.pii_redacted,
                'assumptions_extracted': len(gateway_result.assumptions)
            }
            
            # Update processed query if it was modified
            if gateway_result.processed_query != context.query:
                context.query = gateway_result.processed_query
                decision_record.input_data['processed_query'] = gateway_result.processed_query
            
            if not gateway_result.passed_filters:
                self._metrics['gateway_rejections'] += 1
                decision_record.decision_factors['rejection_reason'] = 'failed_gateway_filters'
                return 'gateway_rejected', gateway_result
            
            return 'agent_selection', gateway_result
            
        except Exception as e:
            self._logger.warning(f"Gateway processing failed: {e}")
            # Continue without gateway processing
            return 'agent_selection', None

    async def _execute_agent_selection(
        self, context: TurnContext, decision_record: DecisionRecord, 
        current_state: str, gateway_result: Optional[Any]
    ) -> Tuple[str, List[str]]:
        """Execute agent selection step using utility-based algorithm"""
        
        try:
            # Get available agents (excluding those with open circuit breakers)
            available_agents = self._get_available_agents()
            
            if not available_agents:
                return 'no_agents_available', []
            
            # Calculate utility scores for each agent
            agent_utilities = []
            
            for agent_id in available_agents:
                utility_score = await self._calculate_agent_utility(
                    agent_id, context, gateway_result
                )
                agent_utilities.append((agent_id, utility_score))
            
            # Sort by utility (highest first)
            agent_utilities.sort(key=lambda x: x[1], reverse=True)
            
            # Apply exploration epsilon
            selected_agents = self._apply_exploration_selection(
                agent_utilities, self._max_agents_per_turn
            )
            
            # Record selection decision
            decision_record.decision_factors['agent_selection'] = {
                'available_agents': len(available_agents),
                'selected_agents': selected_agents,
                'utility_scores': {agent: score for agent, score in agent_utilities[:5]},
                'exploration_applied': random.random() < self._exploration_epsilon,
                'selection_algorithm': 'utility_based_top_2_with_exploration'
            }
            
            self._metrics['agent_selections'] += 1
            return 'agent_execution', selected_agents
            
        except Exception as e:
            self._logger.error(f"Agent selection failed: {e}")
            return 'no_agents_available', []

    async def _calculate_agent_utility(
        self, agent_id: str, context: TurnContext, gateway_result: Optional[Any]
    ) -> float:
        """
        Calculate utility score for agent selection
        
        Uses weighted combination of:
        - clarity_score: Query clarity from gateway
        - retrieval_score: Mock retrieval quality score
        - prior_success: Agent success rate
        - cost_efficiency: Inverse of cost estimate
        - latency_efficiency: Inverse of latency
        - health_score: Overall agent health
        """
        
        with self._health_lock:
            agent_health = self._agent_health.get(agent_id)
            if not agent_health:
                return 0.0
        
        # Component scores (all normalized to 0.0-1.0)
        scores = {}
        
        # Clarity score from gateway result
        if gateway_result and hasattr(gateway_result, 'clarity_score'):
            scores['clarity_score'] = gateway_result.clarity_score
        else:
            scores['clarity_score'] = 0.7  # Default moderate clarity
        
        # Mock retrieval score (in production, this would come from knowledge base)
        scores['retrieval_score'] = 0.8  # Mock high retrieval quality
        
        # Prior success rate from agent health
        scores['prior_success'] = agent_health.success_rate
        
        # Cost efficiency (inverse of normalized cost)
        normalized_cost = min(agent_health.avg_cost_estimate / 100.0, 1.0)  # Normalize to $1.00 max
        scores['cost_efficiency'] = 1.0 - normalized_cost
        
        # Latency efficiency (inverse of normalized latency)
        normalized_latency = min(agent_health.p95_latency / 5000.0, 1.0)  # Normalize to 5s max
        scores['latency_efficiency'] = 1.0 - normalized_latency
        
        # Health score from agent health
        scores['health_score'] = agent_health.health_score
        
        # Calculate weighted utility score
        utility = sum(
            scores[component] * self.UTILITY_WEIGHTS.get(component, 0.1)
            for component in scores
        )
        
        # Apply budget constraints
        budget_penalty = 0.0
        if not context.budget.has_time_budget(1000):  # Need at least 1s for agent
            budget_penalty += 0.2
        if not context.budget.has_token_budget(100):  # Need at least 100 tokens
            budget_penalty += 0.2
        if not context.budget.has_call_budget(1):
            budget_penalty += 0.3
        
        utility = max(0.0, utility - budget_penalty)
        
        return utility

    def _apply_exploration_selection(
        self, agent_utilities: List[Tuple[str, float]], max_agents: int
    ) -> List[str]:
        """Apply epsilon exploration to agent selection"""
        
        if not agent_utilities:
            return []
        
        # Exploration decision
        if random.random() < self._exploration_epsilon:
            # Exploration: select randomly from top 50%
            top_half_size = max(1, len(agent_utilities) // 2)
            exploration_candidates = agent_utilities[:top_half_size]
            
            selected = random.sample(
                exploration_candidates,
                min(max_agents, len(exploration_candidates))
            )
            return [agent_id for agent_id, _ in selected]
        else:
            # Exploitation: select top N agents
            top_agents = agent_utilities[:max_agents]
            return [agent_id for agent_id, _ in top_agents]

    async def _execute_agent_processing(
        self, context: TurnContext, decision_record: DecisionRecord,
        current_state: str, selected_agents: List[str]
    ) -> Tuple[str, Optional[Any]]:
        """Execute agent processing with fallback"""
        
        agent_results = []
        
        for agent_id in selected_agents:
            try:
                # Check if agent is available (circuit breaker)
                with self._health_lock:
                    agent_health = self._agent_health.get(agent_id)
                    if not agent_health or not agent_health.can_accept_request():
                        self._logger.info(f"Agent {agent_id} circuit breaker open, skipping")
                        continue
                
                # Check remaining budget
                if context.budget.is_exhausted:
                    break
                
                # Execute agent processing
                agent_start_time = time.time()
                
                # Use the actual agent service (duck typing allows flexibility)
                if hasattr(self._agent_service, 'process_query'):
                    result = await self._agent_service.process_query(
                        query=context.query,
                        session_id=context.session_id,
                        context=context.context
                    )
                else:
                    # Fallback for different agent service interfaces
                    result = await self._agent_service.process_troubleshooting_query(
                        query=context.query,
                        session_id=context.session_id,
                        user_context=context.context
                    )
                
                agent_time_ms = int((time.time() - agent_start_time) * 1000)
                
                # Update agent health
                with self._health_lock:
                    if agent_health:
                        # Estimate tokens used (in production, this would come from result)
                        estimated_tokens = len(result.get('response', '')) // 4  # Rough estimate
                        agent_health.record_success(agent_time_ms, estimated_tokens)
                
                # Update budget
                context.budget.consume_time(agent_time_ms)
                context.budget.consume_calls(1)
                context.budget.consume_tokens(len(result.get('response', '')) // 4)
                
                agent_results.append({
                    'agent_id': agent_id,
                    'result': result,
                    'processing_time_ms': agent_time_ms,
                    'success': True
                })
                
                # Use first successful result (primary agent)
                decision_record.decision_factors['agent_processing'] = {
                    'successful_agent': agent_id,
                    'processing_time_ms': agent_time_ms,
                    'attempted_agents': [agent_id],
                    'fallback_used': False
                }
                
                return 'confidence_evaluation', result
                
            except Exception as e:
                self._logger.warning(f"Agent {agent_id} processing failed: {e}")
                
                agent_time_ms = int((time.time() - agent_start_time) * 1000) if 'agent_start_time' in locals() else 0
                
                # Update agent health
                with self._health_lock:
                    if agent_health:
                        agent_health.record_failure(agent_time_ms if agent_time_ms > 0 else None)
                
                agent_results.append({
                    'agent_id': agent_id,
                    'error': str(e),
                    'processing_time_ms': agent_time_ms,
                    'success': False
                })
                
                continue
        
        # All agents failed
        self._metrics['agent_failures'] += 1
        decision_record.decision_factors['agent_processing'] = {
            'attempted_agents': selected_agents,
            'all_failed': True,
            'failure_details': agent_results
        }
        
        return 'agent_failed', None

    async def _execute_confidence_evaluation(
        self, context: TurnContext, decision_record: DecisionRecord,
        current_state: str, agent_result: Any
    ) -> Tuple[str, Any]:
        """Execute confidence evaluation step"""
        
        if not self._confidence_service or not agent_result:
            # Skip confidence evaluation if service not available
            return 'loop_checking', agent_result
        
        try:
            # Extract features for confidence evaluation
            features = self._extract_confidence_features(agent_result, context)
            
            confidence_request = ConfidenceRequest(
                session_id=context.session_id,
                turn_id=context.turn_id,
                feature_vector=features,
                metadata=context.context
            )
            
            confidence_response = await self._confidence_service.score_confidence(confidence_request)
            
            # Record confidence evaluation
            decision_record.decision_factors['confidence_evaluation'] = {
                'confidence_score': confidence_response.calibrated_score,
                'confidence_band': confidence_response.confidence_band.value,
                'recommended_actions': confidence_response.recommended_actions,
                'feature_vector': features
            }
            
            # Check if confidence is too low
            if confidence_response.confidence_band.value in ['low']:
                decision_record.decision_factors['low_confidence_reason'] = confidence_response.reasoning
                return 'low_confidence', confidence_response
            
            return 'loop_checking', {
                'agent_result': agent_result,
                'confidence_result': confidence_response
            }
            
        except Exception as e:
            self._logger.warning(f"Confidence evaluation failed: {e}")
            return 'loop_checking', agent_result

    def _extract_confidence_features(self, agent_result: Any, context: TurnContext) -> Dict[str, float]:
        """Extract confidence features from agent result"""
        
        # Mock feature extraction (in production, this would analyze the actual result)
        features = {
            'retrieval_score': 0.8,  # Mock good retrieval
            'provider_confidence': 0.7,  # Mock moderate LLM confidence
            'hypothesis_score': 0.6,  # Mock hypothesis strength
            'validation_result': 0.8,  # Mock validation success
            'pattern_boost': 0.1,  # Mock pattern matching bonus
            'history_slope': 0.05  # Mock positive confidence trend
        }
        
        # Adjust based on actual result if available
        if isinstance(agent_result, dict):
            if 'confidence' in agent_result:
                features['provider_confidence'] = float(agent_result['confidence'])
            
            if 'solution_confidence' in agent_result:
                features['hypothesis_score'] = float(agent_result['solution_confidence'])
        
        return features

    async def _execute_loop_checking(
        self, context: TurnContext, decision_record: DecisionRecord,
        current_state: str, evaluation_result: Any
    ) -> Tuple[str, Any]:
        """Execute loop checking step"""
        
        if not self._loop_guard_service:
            # Skip loop checking if service not available
            return 'response_generation', evaluation_result
        
        try:
            # Create loop check request
            loop_request = LoopCheckRequest(
                session_id=context.session_id,
                turn_id=context.turn_id,
                current_query=context.query,
                current_confidence=0.7,  # Extract from evaluation result
                conversation_history=context.conversation_history,
                metadata=context.metadata
            )
            
            loop_response = await self._loop_guard_service.check_for_loops(loop_request)
            
            # Record loop check
            decision_record.decision_factors['loop_checking'] = {
                'loop_status': loop_response.loop_status.value,
                'confidence_score': loop_response.confidence_score,
                'recovery_suggested': loop_response.suggested_recovery.value if loop_response.suggested_recovery else None,
                'detection_signals': loop_response.detection_signals
            }
            
            # Handle loop detection
            if loop_response.loop_status.value == 'detected':
                self._metrics['loop_detections'] += 1
                decision_record.decision_factors['loop_recovery'] = {
                    'recovery_action': loop_response.suggested_recovery.value,
                    'recovery_suggestions': loop_response.recovery_suggestions
                }
                return 'loop_recovery', loop_response
            
            return 'response_generation', evaluation_result
            
        except Exception as e:
            self._logger.warning(f"Loop checking failed: {e}")
            return 'response_generation', evaluation_result

    async def _finalize_decision_record(
        self,
        decision_record: DecisionRecord,
        state_history: List[str],
        start_time: float,
        outcome_description: str,
        final_result: Optional[Any] = None
    ) -> DecisionRecord:
        """Finalize and emit decision record"""
        
        processing_time_ms = int((time.time() - start_time) * 1000)
        
        # Update decision record
        decision_record.processing_time_ms = processing_time_ms
        decision_record.outcome_description = outcome_description
        decision_record.metadata = {
            'state_history': state_history,
            'final_state': state_history[-1] if state_history else 'unknown',
            'service_version': '1.0.0'
        }
        
        # Add final result if available
        if final_result:
            if isinstance(final_result, dict):
                decision_record.output_data = final_result
            else:
                decision_record.output_data = {'result': str(final_result)}
        
        # Update metrics
        self._metrics['turns_processed'] += 1
        self._update_avg_routing_time(processing_time_ms)
        
        # Emit decision record if recorder available
        if self._decision_recorder:
            try:
                await self._decision_recorder.record_decision(decision_record)
            except Exception as e:
                self._logger.warning(f"Failed to emit decision record: {e}")
        
        self._logger.info(
            f"Turn processed: session={decision_record.session_id}, "
            f"time={processing_time_ms}ms, outcome={outcome_description}"
        )
        
        return decision_record

    def _get_available_agents(self) -> List[str]:
        """Get list of available agents (excluding those with open circuit breakers)"""
        
        available = []
        
        with self._health_lock:
            for agent_id in self._available_agents:
                agent_health = self._agent_health.get(agent_id)
                if agent_health and agent_health.can_accept_request():
                    available.append(agent_id)
        
        return available

    def _update_avg_routing_time(self, routing_time_ms: int) -> None:
        """Update average routing time metric"""
        total_turns = self._metrics['turns_processed']
        if total_turns == 1:
            self._metrics['avg_routing_time_ms'] = routing_time_ms
        else:
            current_avg = self._metrics['avg_routing_time_ms']
            self._metrics['avg_routing_time_ms'] = (
                (current_avg * (total_turns - 1) + routing_time_ms) / total_turns
            )

    @trace("orchestrator_get_session_state")
    async def get_session_state(self, session_id: str) -> Dict[str, Any]:
        """
        Get current session state and conversation context
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dictionary with session state and context
        """
        try:
            # In production, this would retrieve actual session state
            # For now, return mock state structure
            return {
                'session_id': session_id,
                'state': 'active',
                'current_turn': 1,
                'conversation_context': {
                    'total_turns': 0,
                    'last_activity': datetime.utcnow().isoformat(),
                    'current_phase': 'ready'
                },
                'workflow_status': {
                    'last_agent_used': None,
                    'last_confidence_score': 0.0,
                    'loop_detection_active': True,
                    'budget_remaining': {
                        'time_ms': 2000,
                        'tokens': 1500,
                        'calls': 5
                    }
                },
                'agent_health': {
                    agent_id: {
                        'success_rate': health.success_rate,
                        'p95_latency': health.p95_latency,
                        'circuit_open': health.circuit_open,
                        'health_score': health.health_score
                    }
                    for agent_id, health in self._agent_health.items()
                }
            }
            
        except Exception as e:
            self._logger.error(f"Failed to get session state for {session_id}: {e}")
            raise ServiceException(f"Session state retrieval failed: {str(e)}")

    @trace("orchestrator_health_check")
    async def health_check(self) -> Dict[str, Any]:
        """
        Get service health status and performance metrics
        
        Returns:
            Comprehensive health status including agent health and performance
        """
        try:
            # Calculate service health
            total_turns = self._metrics['turns_processed']
            avg_routing_time = self._metrics['avg_routing_time_ms']
            
            # Agent availability
            available_agents = len(self._get_available_agents())
            total_agents = len(self._available_agents)
            agent_availability = available_agents / total_agents if total_agents > 0 else 0.0
            
            # Service status determination
            service_status = "healthy"
            if avg_routing_time > 300:  # Above SLO
                service_status = "degraded"
            elif agent_availability < 0.5:  # Less than 50% agents available
                service_status = "degraded"
            elif not self._agent_service:  # No agent service
                service_status = "unhealthy"
            
            # Calculate success rates
            failure_rate = (self._metrics['agent_failures'] / max(total_turns, 1)) if total_turns > 0 else 0.0
            
            health_status = {
                "service": "orchestrator_service",
                "status": service_status,
                "timestamp": datetime.utcnow().isoformat(),
                "version": "1.0.0",
                "dependencies": {
                    "agent_service": self._agent_service is not None,
                    "gateway_service": self._gateway_service is not None,
                    "confidence_service": self._confidence_service is not None,
                    "loop_guard_service": self._loop_guard_service is not None,
                    "decision_recorder": self._decision_recorder is not None
                },
                "metrics": {
                    "turns_processed": total_turns,
                    "gateway_rejections": self._metrics['gateway_rejections'],
                    "agent_selections": self._metrics['agent_selections'],
                    "agent_failures": self._metrics['agent_failures'],
                    "budget_exhaustions": self._metrics['budget_exhaustions'],
                    "loop_detections": self._metrics['loop_detections'],
                    "avg_routing_time_ms": round(avg_routing_time, 1),
                    "circuit_breaker_trips": self._metrics['circuit_breaker_trips'],
                    "failure_rate": round(failure_rate, 3)
                },
                "agent_health": {
                    "total_agents": total_agents,
                    "available_agents": available_agents,
                    "agent_availability_rate": round(agent_availability, 3),
                    "individual_health": {
                        agent_id: {
                            "success_rate": round(health.success_rate, 3),
                            "p95_latency_ms": round(health.p95_latency, 1),
                            "circuit_open": health.circuit_open,
                            "health_score": round(health.health_score, 3),
                            "consecutive_errors": health.consecutive_errors
                        }
                        for agent_id, health in self._agent_health.items()
                    }
                },
                "configuration": {
                    "exploration_epsilon": self._exploration_epsilon,
                    "max_agents_per_turn": self._max_agents_per_turn,
                    "circuit_breaker_enabled": self._circuit_breaker_enabled
                },
                "performance": {
                    "slo_target_p95_ms": 300,
                    "current_avg_ms": round(avg_routing_time, 1),
                    "slo_compliance": avg_routing_time <= 300
                }
            }
            
            return health_status
            
        except Exception as e:
            self._logger.error(f"Health check failed: {e}")
            return {
                "service": "orchestrator_service",
                "status": "unhealthy",
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e)
            }

    async def get_service_metrics(self) -> Dict[str, Any]:
        """Get current service metrics"""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "metrics": self._metrics.copy(),
            "agent_health": {
                agent_id: {
                    "success_rate": health.success_rate,
                    "p95_latency": health.p95_latency,
                    "health_score": health.health_score,
                    "circuit_open": health.circuit_open,
                    "total_requests": health.total_requests
                }
                for agent_id, health in self._agent_health.items()
            },
            "configuration": {
                "exploration_epsilon": self._exploration_epsilon,
                "max_agents_per_turn": self._max_agents_per_turn,
                "circuit_breaker_enabled": self._circuit_breaker_enabled,
                "utility_weights": self.UTILITY_WEIGHTS
            }
        }