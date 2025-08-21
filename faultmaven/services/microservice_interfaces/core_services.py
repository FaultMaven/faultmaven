"""Core Service Interfaces for FaultMaven Microservice Architecture

This module defines the interfaces for the 7 core services in the microservice
blueprint. Each interface supports both in-process and distributed deployment
modes with consistent error handling, SLO compliance, and observability.

Design Principles:
- Interface contracts work for both local and remote calls
- Consistent error semantics across services
- Built-in observability with decision records
- Budget enforcement and resource management
- Circuit breaker and health monitoring support
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union
from contextlib import asynccontextmanager

from ..microservice_contracts.core_contracts import (
    TurnContext, DecisionRecord, RetrievalRequest, RetrievalResponse,
    ConfidenceRequest, ConfidenceResponse, PolicyEvaluation,
    LoopCheckRequest, LoopCheckResponse, GatewayResult
)


class IOrchestratorService(ABC):
    """Orchestrator/Router Service Interface
    
    Responsibilities:
    - Accept user turns and run state machine
    - Route to agents within budget constraints
    - Enforce thresholds via Global Confidence Service
    - Emit decision records for observability
    - Manage agent health and circuit breakers
    
    SLOs:
    - p95 routing latency < 300ms
    - 99.9% availability
    - Budget adherence rate > 95%
    
    Error Handling:
    - Graceful degradation on agent failures
    - Circuit breaker integration
    - Fallback routing strategies
    """
    
    @abstractmethod
    async def process_turn(self, context: TurnContext) -> DecisionRecord:
        """Process user turn and route to appropriate agents.
        
        This method orchestrates the complete turn processing workflow including
        agent selection, budget enforcement, confidence evaluation, and result
        aggregation. It maintains state across the conversation and emits
        comprehensive decision records for observability.
        
        Args:
            context: TurnContext containing query, session, budget, and metadata
            
        Returns:
            DecisionRecord with routing decisions, agent responses, confidence,
            budget usage, and complete audit trail
            
        Raises:
            BudgetExceededException: When budget limits are exceeded
            ServiceUnavailableException: When required services are down
            ValidationException: When turn context is invalid
            
        Implementation Notes:
            - Uses top-2 agent selection based on utility function
            - Enforces time, token, and call budgets
            - Integrates with LoopGuard for stall detection
            - Supports epsilon exploration for agent diversity
        """
        pass
    
    @abstractmethod
    async def get_session_state(self, session_id: str) -> Dict[str, Any]:
        """Get current session state and conversation context.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dictionary containing session state, conversation history,
            and current workflow status
            
        Raises:
            SessionNotFoundException: When session doesn't exist
        """
        pass
    
    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """Get service health status and dependent service availability.
        
        Returns:
            Health status including:
            - Service status (healthy/degraded/unhealthy)
            - Agent availability and circuit breaker states
            - Performance metrics (p95 latency, error rate)
            - Resource utilization
        """
        pass


class IGlobalConfidenceService(ABC):
    """Global Confidence/Decision Service Interface
    
    Responsibilities:
    - Compute calibrated confidence from feature vectors
    - Apply hysteresis and dwell time for stability
    - Expose model version and calibration metrics
    - Support confidence band thresholds
    
    SLOs:
    - p95 latency < 50ms
    - 99.95% availability
    - Calibration error < 0.1 (ECE)
    
    Calibration:
    - Platt or Isotonic scaling
    - Monthly refresh cycle
    - ECE/Brier score reporting
    """
    
    @abstractmethod
    async def score_confidence(self, request: ConfidenceRequest) -> ConfidenceResponse:
        """Compute calibrated confidence score from feature vector.
        
        This method takes extracted features from the troubleshooting workflow
        and returns a calibrated confidence score with band classification and
        recommended actions based on thresholds.
        
        Args:
            request: ConfidenceRequest with feature vector and context
            
        Returns:
            ConfidenceResponse with calibrated score, confidence band,
            recommended actions, and model metadata
            
        Features (v1):
            - retrieval_score: Quality of knowledge base matches (0.0-1.0)
            - provider_confidence: LLM provider confidence score (0.0-1.0)
            - hypothesis_score: Strength of diagnostic hypothesis (0.0-1.0)
            - validation_result: Validation agent assessment (0.0-1.0)
            - pattern_boost: Pattern matching confidence bonus (0.0-0.2)
            - history_slope: Confidence trend over last 3 turns (-1.0-1.0)
            
        Confidence Bands:
            - low < 0.5: Request clarification or gather more evidence
            - gray 0.5-0.8: Propose solution with caveats
            - high ≥ 0.8: Propose solution confidently
            - apply ≥ 0.9: Apply with user confirmation
            - resolved ≥ 0.95: Mark issue as resolved after verification
        """
        pass
    
    @abstractmethod
    async def get_model_info(self) -> Dict[str, Any]:
        """Get confidence model information and calibration metrics.
        
        Returns:
            Model metadata including:
            - model_version: Current model identifier
            - calibration_method: Platt or Isotonic
            - last_calibration: Timestamp of last calibration
            - ece_score: Expected Calibration Error
            - brier_score: Brier score for probabilistic accuracy
            - feature_importance: Feature contribution weights
        """
        pass
    
    @abstractmethod
    async def update_model(self, model_data: bytes, version: str) -> bool:
        """Update confidence model with new calibration.
        
        Args:
            model_data: Serialized model binary data
            version: New model version identifier
            
        Returns:
            True if update successful, False otherwise
            
        Notes:
            - Supports canary deployment pattern
            - Automatic rollback on validation failure
            - Shadow evaluation before full deployment
        """
        pass


class IUnifiedRetrievalService(ABC):
    """Unified Retrieval Service Interface
    
    Responsibilities:
    - Federated access to KB, Pattern DB, and Playbooks
    - Explainable scoring and result ranking
    - Adapter timeout management and failover
    - Semantic caching with TTL and invalidation
    
    SLOs:
    - p95 latency < 200ms
    - 99.9% availability
    - Cache hit rate > 30%
    
    Adapters:
    - KB: Documentation and troubleshooting guides
    - Patterns: Symptom to cause mappings
    - Playbooks: Procedural instructions
    """
    
    @abstractmethod
    async def search(self, request: RetrievalRequest) -> RetrievalResponse:
        """Perform federated search across all knowledge sources.
        
        This method queries multiple knowledge adapters in parallel, normalizes
        scores, applies recency bias, and returns ranked evidence with full
        provenance information for explainability.
        
        Args:
            request: RetrievalRequest with query, filters, and preferences
            
        Returns:
            RetrievalResponse with ranked evidence list, timing metrics,
            and adapter-specific metadata
            
        Search Strategy:
            - Parallel queries to enabled adapters (KB, Pattern, Playbook)
            - Hybrid BM25 + embedding ranking
            - Adapter timeout enforcement (default 5s)
            - Score normalization across adapters
            - Recency bias for time-sensitive information
            
        Caching:
            - Semantic cache based on query + context hash
            - TTL based on content type (KB: 1h, Patterns: 24h, Playbooks: 4h)
            - Cache invalidation on content updates
            - Cache warming for frequent queries
        """
        pass
    
    @abstractmethod
    async def search_patterns(self, symptoms: List[str], context: Dict[str, Any]) -> RetrievalResponse:
        """Search for patterns matching specific symptoms.
        
        Args:
            symptoms: List of symptom descriptions
            context: Problem context for pattern filtering
            
        Returns:
            RetrievalResponse with pattern matches including success rates,
            confidence scores, and historical effectiveness data
        """
        pass
    
    @abstractmethod
    async def invalidate_cache(self, source_type: Optional[str] = None) -> bool:
        """Invalidate cached results for updated content.
        
        Args:
            source_type: Optional source type to invalidate (kb/pattern/playbook)
                        If None, invalidates all caches
            
        Returns:
            True if invalidation successful
        """
        pass
    
    @abstractmethod
    async def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache performance statistics.
        
        Returns:
            Cache statistics including:
            - hit_rate: Overall cache hit percentage
            - miss_rate: Cache miss percentage  
            - entry_count: Number of cached entries
            - memory_usage: Cache memory consumption
            - adapter_stats: Per-adapter performance metrics
        """
        pass


class IPolicySafetyService(ABC):
    """Policy/Safety Service Interface
    
    Responsibilities:
    - Classify actions (allow, deny, risk assessment)
    - Generate structured confirmation requests
    - Enforce RBAC and permission checking
    - Audit trail for policy decisions
    
    SLOs:
    - p95 latency < 80ms
    - 99.9% availability
    - 100% policy coverage for risky actions
    
    Action Classes:
    - command_execution: System command execution
    - data_modification: Data/configuration changes
    - network_change: Network topology changes
    - permission_change: Access control modifications
    """
    
    @abstractmethod
    async def evaluate_action(self, action: Dict[str, Any], context: Dict[str, Any]) -> PolicyEvaluation:
        """Evaluate action against safety policies and generate risk assessment.
        
        This method analyzes proposed actions for safety risks, compliance
        violations, and required approvals. It generates structured confirmation
        requests with rationale, risks, rollback procedures, and monitoring.
        
        Args:
            action: Action details including type, target, parameters
            context: Execution context including user role, environment, urgency
            
        Returns:
            PolicyEvaluation with decision, risk level, required confirmations,
            and structured approval workflow
            
        Risk Levels:
            - low: Informational actions, read-only operations
            - medium: Configuration changes, non-critical restarts
            - high: Data modifications, service restarts
            - critical: Data deletion, security changes, production changes
            
        Confirmation Requirements:
            - low: No confirmation required
            - medium: User confirmation
            - high: User + rationale confirmation  
            - critical: Multi-party approval + detailed justification
        """
        pass
    
    @abstractmethod
    async def create_confirmation(self, action: Dict[str, Any], risk_assessment: Dict[str, Any]) -> Dict[str, Any]:
        """Create structured confirmation request for risky actions.
        
        Args:
            action: Action requiring confirmation
            risk_assessment: Risk analysis from evaluate_action
            
        Returns:
            Structured confirmation request with:
            - action_id: Unique identifier for tracking
            - action_description: Human-readable action summary
            - rationale: Why this action is recommended
            - risks: Potential negative consequences
            - rollback_procedure: Steps to undo the action
            - monitoring_steps: How to verify action success
            - required_role: Minimum role required for approval
            - timeout: How long approval is valid
        """
        pass
    
    @abstractmethod
    async def record_decision(self, action_id: str, approved: bool, approver: str, context: Dict[str, Any]) -> bool:
        """Record policy decision for audit trail.
        
        Args:
            action_id: Action identifier from confirmation request
            approved: Whether action was approved
            approver: User/system that made the decision
            context: Additional decision context
            
        Returns:
            True if decision recorded successfully
        """
        pass


class ISessionCaseService(ABC):
    """Session/Case Service Interface
    
    Responsibilities:
    - Persist sessions and case history
    - Manage conversation context windows
    - Expose session summaries and analytics
    - Handle session lifecycle and cleanup
    
    SLOs:
    - p95 latency < 100ms
    - 99.9% availability
    - Cross-session context retrieval < 200ms
    
    Storage:
    - Redis for hot session data with TTL
    - Document DB for case history and summaries
    - Automatic session cleanup and archiving
    """
    
    @abstractmethod
    async def create_session(self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Create new session with optional user association.
        
        Args:
            user_id: Optional user identifier for session attribution
            metadata: Optional session metadata (device, location, etc.)
            
        Returns:
            Session identifier (UUID)
            
        Notes:
            - Sessions are automatically cleaned up after TTL expiration
            - User sessions are limited to prevent resource exhaustion
            - Session creation triggers analytics event
        """
        pass
    
    @abstractmethod
    async def get_session_context(self, session_id: str, window_size: Optional[int] = None) -> Dict[str, Any]:
        """Get session context with conversation history window.
        
        Args:
            session_id: Session identifier
            window_size: Optional context window size (default from config)
            
        Returns:
            Session context including:
            - conversation_history: Recent conversation turns
            - case_summary: Current case summary if available
            - user_profile: User preferences and history
            - metadata: Session metadata and analytics
            
        Raises:
            SessionNotFoundException: When session doesn't exist or expired
        """
        pass
    
    @abstractmethod
    async def add_turn(self, session_id: str, turn_data: Dict[str, Any]) -> bool:
        """Add conversation turn to session history.
        
        Args:
            session_id: Session identifier
            turn_data: Turn data including query, response, metadata
            
        Returns:
            True if turn added successfully
            
        Notes:
            - Turns are automatically PII-sanitized before storage
            - Large responses are truncated with full text stored separately
            - Turn addition extends session TTL
        """
        pass
    
    @abstractmethod
    async def create_case(self, session_id: str, case_data: Dict[str, Any]) -> str:
        """Create persistent case from session for cross-session continuity.
        
        Args:
            session_id: Source session identifier
            case_data: Case initialization data and metadata
            
        Returns:
            Case identifier for future reference
            
        Notes:
            - Cases persist beyond session TTL for continuity
            - Case creation requires explicit user consent
            - Case data is sanitized and anonymized
        """
        pass
    
    @abstractmethod
    async def get_user_cases(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get user's recent cases for context and continuity.
        
        Args:
            user_id: User identifier
            limit: Maximum number of cases to return
            
        Returns:
            List of case summaries with metadata
        """
        pass


class ILoopGuardService(ABC):
    """LoopGuard/Monitor Service Interface
    
    Responsibilities:
    - Detect loops and stalls using multi-signal analysis
    - Suggest recovery strategies (reframe, pivot, meta, escalate)
    - Maintain conversation state for pattern detection
    - Provide debouncing to prevent false positives
    
    SLOs:
    - p95 latency < 40ms
    - 99.9% availability
    - Loop detection accuracy > 95%
    
    Detection Signals:
    - Embedding similarity over 3-turn window
    - Confidence slope analysis
    - Question novelty assessment
    - Response repetition patterns
    """
    
    @abstractmethod
    async def check_for_loops(self, request: LoopCheckRequest) -> LoopCheckResponse:
        """Analyze conversation history for loops and stalls.
        
        This method uses multiple signals to detect when conversations are
        stuck in loops or making no progress, providing recovery suggestions
        based on the type and severity of the detected pattern.
        
        Args:
            request: LoopCheckRequest with conversation history and metadata
            
        Returns:
            LoopCheckResponse with loop status, detection reasoning,
            and recommended recovery actions
            
        Detection Signals (v1):
            - embedding_similarity_3w: Cosine similarity of last 3 query embeddings
            - confidence_slope_3w: Confidence score trend over 3 turns
            - question_novelty: Measure of new information in queries
            - response_repetition: Detection of repeated response patterns
            
        Recovery Strategies:
            - none: No loop detected, continue normal flow
            - reframe: Suggest alternative problem framing
            - pivot: Switch to different troubleshooting approach
            - meta: Ask about the troubleshooting process itself
            - escalate: Recommend human expert involvement
            
        Debouncing:
            - Requires 2 consecutive loop detections before triggering
            - Different thresholds for different loop types
            - Cooldown period after recovery attempt
        """
        pass
    
    @abstractmethod
    async def reset_loop_state(self, session_id: str) -> bool:
        """Reset loop detection state for session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            True if state reset successfully
            
        Notes:
            - Used after successful recovery actions
            - Clears detection history and thresholds
            - Resets debouncing counters
        """
        pass
    
    @abstractmethod
    async def get_loop_metrics(self, session_id: str) -> Dict[str, Any]:
        """Get loop detection metrics for session analysis.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Metrics including:
            - detection_count: Number of loops detected
            - recovery_success_rate: Success rate of recovery strategies
            - signal_history: History of detection signals
            - false_positive_rate: Estimated false positive rate
        """
        pass


class IGatewayProcessingService(ABC):
    """Gateway Processing Service Interface
    
    Responsibilities:
    - Pre-processing validation and enhancement
    - Clarity checking and reality filtering
    - Assumption extraction and context enrichment
    - PII redaction and security scanning
    
    SLOs:
    - p95 latency < 150ms
    - 99.9% availability
    - PII detection accuracy > 99%
    
    Processing Pipeline:
    - Input validation and sanitization
    - Clarity and coherence assessment
    - Reality checking against known impossibilities
    - Assumption identification and extraction
    - Context enrichment from metadata
    """
    
    @abstractmethod
    async def process_query(self, query: str, context: Dict[str, Any]) -> GatewayResult:
        """Process incoming query through gateway pipeline.
        
        This method performs comprehensive pre-processing including validation,
        clarity assessment, reality checking, assumption extraction, and
        security scanning before passing to the orchestrator.
        
        Args:
            query: Raw user query text
            context: Query context including session, metadata, preferences
            
        Returns:
            GatewayResult with processed query, assessments, extracted
            assumptions, and processing recommendations
            
        Processing Steps:
            1. Input validation and basic sanitization
            2. PII detection and redaction
            3. Clarity assessment (vague, specific, complex)
            4. Reality filtering (impossible, improbable, plausible)
            5. Assumption extraction and categorization
            6. Context enrichment from session history
            7. Processing recommendations for orchestrator
            
        Clarity Categories:
            - vague: Requires clarification before proceeding
            - specific: Sufficient detail for direct processing
            - complex: May benefit from decomposition
            - absurd: Clearly invalid or nonsensical
            
        Reality Filtering:
            - impossible: Violates physical/logical constraints
            - improbable: Highly unlikely but not impossible
            - plausible: Reasonable and actionable
            - verified: Matches known patterns or cases
        """
        pass
    
    @abstractmethod
    async def extract_assumptions(self, query: str) -> List[Dict[str, Any]]:
        """Extract implicit assumptions from user query.
        
        Args:
            query: User query text
            
        Returns:
            List of extracted assumptions with:
            - assumption: Assumption text
            - confidence: Extraction confidence (0.0-1.0)
            - category: Assumption category (technical, environmental, etc.)
            - verifiable: Whether assumption can be verified
            - risk_level: Risk if assumption is incorrect
        """
        pass
    
    @abstractmethod
    async def validate_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and enrich query context.
        
        Args:
            context: Raw context data
            
        Returns:
            Validated and enriched context with:
            - validated_fields: Successfully validated context fields
            - enrichments: Additional context from external sources
            - warnings: Context validation warnings
            - suggestions: Suggestions for context improvement
        """
        pass


# Health Check Mixin for all services
class ServiceHealthMixin:
    """Mixin providing common health check functionality for all services."""
    
    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """Get service health status.
        
        Returns:
            Health status with standardized format:
            - status: healthy/degraded/unhealthy
            - timestamp: Health check timestamp
            - version: Service version
            - dependencies: Dependency health status
            - metrics: Key performance metrics
            - errors: Recent error information
        """
        pass
    
    @abstractmethod
    async def ready_check(self) -> bool:
        """Check if service is ready to handle requests.
        
        Returns:
            True if service is ready, False otherwise
            
        Notes:
            - Used by Kubernetes readiness probes
            - Checks critical dependencies
            - Validates configuration
        """
        pass