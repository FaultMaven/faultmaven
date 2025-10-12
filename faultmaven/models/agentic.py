"""Agentic Framework Core Models and Interfaces

This module defines the core data models and interfaces for FaultMaven's 7-component 
agentic framework, enabling true Plan→Execute→Observe→Re-plan cycles with comprehensive
state management and observability.

The agentic framework consists of:
1. State & Session Manager - Persistent memory and execution state
2. Query Intake & Classification Engine - Intelligent query processing and routing
3. Tool & Skill Broker - Dynamic orchestration of tools and skills  
4. Guardrails & Policy Layer - Safety, security, and compliance enforcement
5. Response Synthesizer & Formatter - Intelligent response generation
6. Error Handling & Fallback Manager - Robust error recovery and graceful degradation
7. Business Logic & Workflow Engine - Plan-execute-observe-adapt workflow orchestration

Key Features:
- Interface-based design with dependency injection support
- Comprehensive state management for multi-turn conversations
- Dynamic tool capability registration and execution
- Policy-driven safety enforcement
- Observability throughout the agentic loop
- Graceful error recovery with fallback strategies
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Union, Callable
from datetime import datetime
from pydantic import BaseModel, Field
from enum import Enum
import uuid


# Core Agentic Data Models

class AgentExecutionPhase(str, Enum):
    """Phases in the agentic execution loop (LEGACY)

    NOTE: v3.2.0 uses InvestigationPhase (0-6) from investigation.py
    This enum maintained for backward compatibility
    """
    INTAKE = "intake"
    CLASSIFICATION = "classification"
    PLANNING = "planning"
    EXECUTION = "execution"
    OBSERVATION = "observation"
    ADAPTATION = "adaptation"
    SYNTHESIS = "synthesis"
    COMPLETION = "completion"


# v3.2.0: Import investigation phase enums
try:
    from faultmaven.models.investigation import (
        InvestigationPhase,
        OODAStep,
        EngagementMode,
        InvestigationStrategy
    )
except ImportError:
    # Fallback if investigation.py not yet available
    pass


class AgentRole(str, Enum):
    """Different agent roles in the system"""
    PRIMARY_AGENT = "primary_agent"
    SPECIALIST_AGENT = "specialist_agent"
    COORDINATION_AGENT = "coordination_agent"
    VALIDATION_AGENT = "validation_agent"


class AgentCapabilityType(str, Enum):
    """Types of capabilities an agent can have"""
    REASONING = "reasoning"
    TOOL_EXECUTION = "tool_execution"
    KNOWLEDGE_RETRIEVAL = "knowledge_retrieval"
    DATA_ANALYSIS = "data_analysis"
    COMMUNICATION = "communication"
    VALIDATION = "validation"
    COORDINATION = "coordination"


class ExecutionPriority(str, Enum):
    """Priority levels for agentic operations"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class SafetyLevel(str, Enum):
    """Safety levels for operations"""
    SAFE = "safe"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    RESTRICTED = "restricted"
    DANGEROUS = "dangerous"


class AgentExecutionState(BaseModel):
    """Represents the current execution state of an agentic workflow"""
    session_id: str
    agent_id: str = "faultmaven-agent"
    current_phase: AgentExecutionPhase = AgentExecutionPhase.INTAKE
    execution_context: Dict[str, Any] = Field(default_factory=dict)
    plan_stack: List[Dict[str, Any]] = Field(default_factory=list)
    observation_buffer: List[Dict[str, Any]] = Field(default_factory=list)
    adaptation_history: List[Dict[str, Any]] = Field(default_factory=list)
    confidence_metrics: Dict[str, float] = Field(default_factory=dict)
    active_tools: List[str] = Field(default_factory=list)
    safety_constraints: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    # Additional fields for test compatibility
    current_step: int = 1
    workflow_status: str = "planning"
    context: Dict[str, Any] = Field(default_factory=dict)
    last_updated: datetime = Field(default_factory=datetime.utcnow)


class ConversationMemory(BaseModel):
    """Rich conversation memory with semantic understanding"""
    conversation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_profile: Dict[str, Any] = Field(default_factory=dict)
    interaction_patterns: Dict[str, Any] = Field(default_factory=dict)
    domain_context: Dict[str, Any] = Field(default_factory=dict)
    conversation_history: List[Dict[str, Any]] = Field(default_factory=list)
    learned_preferences: Dict[str, Any] = Field(default_factory=dict)
    troubleshooting_context: Dict[str, Any] = Field(default_factory=dict)
    semantic_memory: Dict[str, Any] = Field(default_factory=dict)
    
    # Additional fields for test compatibility
    session_id: str = ""
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)


class AgentCapability(BaseModel):
    """Represents a specific capability of an agent"""
    capability_id: str
    capability_type: AgentCapabilityType
    name: str
    description: str
    version: str = "1.0.0"
    parameters: Dict[str, Any] = Field(default_factory=dict)
    dependencies: List[str] = Field(default_factory=list)
    safety_level: SafetyLevel = SafetyLevel.SAFE
    performance_metrics: Dict[str, float] = Field(default_factory=dict)
    enabled: bool = True


class PlanNode(BaseModel):
    """Individual node in an execution plan"""
    node_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    action_type: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    dependencies: List[str] = Field(default_factory=list)
    success_criteria: Dict[str, Any] = Field(default_factory=dict)
    fallback_actions: List[str] = Field(default_factory=list)
    estimated_duration: Optional[float] = None
    priority: ExecutionPriority = ExecutionPriority.NORMAL
    safety_level: SafetyLevel = SafetyLevel.SAFE


class ExecutionPlan(BaseModel):
    """Complete execution plan for an agentic workflow"""
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    query_id: Optional[str] = None
    nodes: List[PlanNode] = Field(default_factory=list)
    execution_order: List[str] = Field(default_factory=list)
    parallel_groups: List[List[str]] = Field(default_factory=list)
    estimated_total_duration: Optional[float] = None
    risk_assessment: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: str = "system"
    
    # Additional fields for test compatibility
    query: str = ""
    steps: List[PlanNode] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)


class ObservationData(BaseModel):
    """Data collected during execution observation"""
    observation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    phase: AgentExecutionPhase
    source: str
    observation_type: str
    data: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    quality_score: Optional[float] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AdaptationEvent(BaseModel):
    """Represents an adaptation made to the execution plan"""
    adaptation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    trigger_reason: str
    adaptation_type: str
    changes: Dict[str, Any] = Field(default_factory=dict)
    impact_assessment: Dict[str, Any] = Field(default_factory=dict)
    confidence: float
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class AgentMetrics(BaseModel):
    """Performance and quality metrics for agent execution"""
    session_id: str
    total_execution_time: float = 0.0
    plan_generation_time: float = 0.0
    execution_time: float = 0.0
    observation_time: float = 0.0
    adaptation_time: float = 0.0
    synthesis_time: float = 0.0
    success_rate: float = 0.0
    confidence_score: float = 0.0
    user_satisfaction: Optional[float] = None
    tool_usage_stats: Dict[str, int] = Field(default_factory=dict)
    error_count: int = 0
    adaptation_count: int = 0


class PolicyViolation(BaseModel):
    """Represents a policy violation detected by guardrails"""
    violation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    policy_rule_id: str
    violation_type: str
    severity: str
    description: str
    context: Dict[str, Any] = Field(default_factory=dict)
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    resolution_status: str = "pending"


class ComponentMessage(BaseModel):
    """Message for inter-component communication"""
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_component: str
    target_component: str
    message_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    priority: ExecutionPriority = ExecutionPriority.NORMAL
    correlation_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# Core Agentic Interfaces

class IAgentStateManager(ABC):
    """Interface for managing agent execution state and conversation context"""
    
    @abstractmethod
    async def get_execution_state(self, session_id: str) -> Optional[AgentExecutionState]:
        """Retrieve current execution state for a session"""
        pass
    
    @abstractmethod
    async def update_execution_state(self, session_id: str, state: AgentExecutionState) -> bool:
        """Update execution state for a session"""
        pass
    
    @abstractmethod
    async def get_conversation_memory(self, session_id: str) -> Optional[ConversationMemory]:
        """Retrieve conversation memory for context"""
        pass
    
    @abstractmethod
    async def update_conversation_memory(self, session_id: str, memory: ConversationMemory) -> bool:
        """Update conversation memory"""
        pass
    
    @abstractmethod
    async def create_execution_plan(self, session_id: str, query: str, context: Dict[str, Any]) -> ExecutionPlan:
        """Create a new execution plan based on query and context"""
        pass
    
    @abstractmethod
    async def record_observation(self, observation: ObservationData) -> bool:
        """Record an observation during execution"""
        pass
    
    @abstractmethod
    async def record_adaptation(self, adaptation: AdaptationEvent) -> bool:
        """Record an adaptation to the execution plan"""
        pass


class IQueryClassificationEngine(ABC):
    """Interface for query intake and classification"""
    
    @abstractmethod
    async def classify_query(self, query: str, context: Optional[Dict[str, Any]] = None) -> "QueryClassification":
        """Classify a user query and determine processing strategy"""
        pass
    
    @abstractmethod
    async def extract_intent(self, query: str) -> Dict[str, Any]:
        """Extract user intent from query"""
        pass
    
    @abstractmethod
    async def assess_complexity(self, query: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Assess query complexity to determine sync vs async processing"""
        pass
    
    @abstractmethod
    async def identify_domain(self, query: str) -> Optional[str]:
        """Identify the domain/category of the query"""
        pass
    
    @abstractmethod
    async def extract_entities(self, query: str) -> List[Dict[str, Any]]:
        """Extract entities from the query"""
        pass


class IToolSkillBroker(ABC):
    """Interface for tool and skill orchestration"""
    
    @abstractmethod
    async def discover_capabilities(self, requirements: Dict[str, Any]) -> List[AgentCapability]:
        """Discover available capabilities matching requirements"""
        pass
    
    @abstractmethod
    async def register_capability(self, capability: AgentCapability) -> bool:
        """Register a new capability with the broker"""
        pass
    
    @abstractmethod
    async def execute_capability(self, capability_id: str, parameters: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a specific capability"""
        pass
    
    @abstractmethod
    async def assess_capability_safety(self, capability_id: str, parameters: Dict[str, Any]) -> SafetyLevel:
        """Assess the safety level of executing a capability"""
        pass
    
    @abstractmethod
    async def get_capability_performance(self, capability_id: str) -> Dict[str, float]:
        """Get performance metrics for a capability"""
        pass


class IGuardrailsPolicyLayer(ABC):
    """Interface for guardrails and policy enforcement"""
    
    @abstractmethod
    async def evaluate_request(self, request: Dict[str, Any], context: Dict[str, Any]) -> List[PolicyViolation]:
        """Evaluate a request against all policies"""
        pass
    
    @abstractmethod
    async def check_safety_constraints(self, operation: str, parameters: Dict[str, Any]) -> bool:
        """Check if an operation meets safety constraints"""
        pass
    
    @abstractmethod
    async def enforce_user_permissions(self, user_id: str, operation: str) -> bool:
        """Check if user has permission for operation"""
        pass
    
    @abstractmethod
    async def apply_data_sanitization(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply data sanitization policies"""
        pass
    
    @abstractmethod
    async def audit_operation(self, operation: str, user_id: str, parameters: Dict[str, Any]) -> bool:
        """Record operation for audit purposes"""
        pass


class IResponseSynthesizer(ABC):
    """Interface for response synthesis and formatting"""
    
    @abstractmethod
    async def synthesize_response(self, data: List[Dict[str, Any]], context: Dict[str, Any], user_preferences: Dict[str, Any]) -> str:
        """Synthesize a response from multiple data sources"""
        pass
    
    @abstractmethod
    async def format_response(self, content: str, format_type: str, context: Dict[str, Any]) -> str:
        """Format response according to specified type and context"""
        pass
    
    @abstractmethod
    async def personalize_response(self, content: str, user_profile: Dict[str, Any]) -> str:
        """Personalize response based on user profile"""
        pass
    
    @abstractmethod
    async def assess_response_quality(self, response: str, context: Dict[str, Any]) -> float:
        """Assess the quality of a response"""
        pass


class IErrorFallbackManager(ABC):
    """Interface for error handling and fallback management"""
    
    @abstractmethod
    async def handle_error(self, error: Exception, context: Dict[str, Any]) -> Dict[str, Any]:
        """Handle an error with appropriate fallback strategy"""
        pass
    
    @abstractmethod
    async def get_fallback_strategy(self, error_type: str, context: Dict[str, Any]) -> str:
        """Get appropriate fallback strategy for error type"""
        pass
    
    @abstractmethod
    async def execute_fallback(self, strategy: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a fallback strategy"""
        pass
    
    @abstractmethod
    async def record_error_pattern(self, error: Exception, context: Dict[str, Any]) -> bool:
        """Record error pattern for learning"""
        pass
    
    @abstractmethod
    async def assess_system_health(self) -> Dict[str, Any]:
        """Assess overall system health"""
        pass


class IBusinessLogicWorkflowEngine(ABC):
    """Interface for business logic and workflow orchestration"""
    
    @abstractmethod
    async def execute_workflow(self, plan: ExecutionPlan, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a complete workflow plan"""
        pass
    
    @abstractmethod
    async def orchestrate_agents(self, agents: List[str], task: Dict[str, Any]) -> Dict[str, Any]:
        """Orchestrate multiple agents for a complex task"""
        pass
    
    @abstractmethod
    async def manage_workflow_state(self, session_id: str, state: Dict[str, Any]) -> bool:
        """Manage workflow state transitions"""
        pass
    
    @abstractmethod
    async def coordinate_tool_execution(self, tools: List[str], parameters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Coordinate execution of multiple tools"""
        pass
    
    @abstractmethod
    async def adapt_workflow(self, session_id: str, observations: List[ObservationData]) -> ExecutionPlan:
        """Adapt workflow based on observations"""
        pass


class IComponentBus(ABC):
    """Interface for inter-component communication"""
    
    @abstractmethod
    async def send_message(self, message: ComponentMessage) -> bool:
        """Send a message to another component"""
        pass
    
    @abstractmethod
    async def subscribe(self, component_name: str, message_types: List[str], handler: Callable) -> bool:
        """Subscribe to specific message types"""
        pass
    
    @abstractmethod
    async def unsubscribe(self, component_name: str, message_types: List[str]) -> bool:
        """Unsubscribe from message types"""
        pass


# Specialized Data Models for LangGraph Integration

class AgenticLangGraphState(BaseModel):
    """Extended LangGraph state for agentic workflows"""
    # Core identifiers
    session_id: str
    case_id: Optional[str] = None
    workflow_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    
    # Agentic execution state
    current_phase: AgentExecutionPhase = AgentExecutionPhase.INTAKE
    execution_state: Optional[AgentExecutionState] = None
    conversation_memory: Optional[ConversationMemory] = None
    
    # Planning and execution
    current_plan: Optional[ExecutionPlan] = None
    active_nodes: List[str] = Field(default_factory=list)
    completed_nodes: List[str] = Field(default_factory=list)
    failed_nodes: List[str] = Field(default_factory=list)
    
    # Observation and adaptation
    observations: List[ObservationData] = Field(default_factory=list)
    adaptations: List[AdaptationEvent] = Field(default_factory=list)
    confidence_history: List[float] = Field(default_factory=list)
    
    # Component interactions
    component_messages: List[ComponentMessage] = Field(default_factory=list)
    policy_violations: List[PolicyViolation] = Field(default_factory=list)
    
    # User interaction
    user_query: Optional[str] = None
    user_context: Dict[str, Any] = Field(default_factory=dict)
    response_preferences: Dict[str, Any] = Field(default_factory=dict)
    
    # Results and metrics
    final_response: Optional[str] = None
    response_sources: List[Dict[str, Any]] = Field(default_factory=list)
    execution_metrics: Optional[AgentMetrics] = None
    
    # Error handling
    errors_encountered: List[Dict[str, Any]] = Field(default_factory=list)
    fallback_strategies_used: List[str] = Field(default_factory=list)
    
    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Factory and Utility Classes

class AgenticComponentFactory:
    """Factory for creating agentic components"""
    
    def __init__(self, container):
        """Initialize with dependency injection container"""
        self.container = container
    
    def create_state_manager(self) -> IAgentStateManager:
        """Create state manager component"""
        # Will be implemented in the actual component
        pass
    
    def create_classification_engine(self) -> IQueryClassificationEngine:
        """Create query classification engine"""
        # Will be implemented in the actual component
        pass
    
    def create_tool_broker(self) -> IToolSkillBroker:
        """Create tool and skill broker"""
        # Will be implemented in the actual component
        pass
    
    def create_guardrails_layer(self) -> IGuardrailsPolicyLayer:
        """Create guardrails and policy layer"""
        # Will be implemented in the actual component
        pass
    
    def create_response_synthesizer(self) -> IResponseSynthesizer:
        """Create response synthesizer"""
        # Will be implemented in the actual component
        pass
    
    def create_error_manager(self) -> IErrorFallbackManager:
        """Create error handling and fallback manager"""
        # Will be implemented in the actual component
        pass
    
    def create_workflow_engine(self) -> IBusinessLogicWorkflowEngine:
        """Create business logic and workflow engine"""
        # Will be implemented in the actual component
        pass


# Additional Models for Test Compatibility

class QueryInput(BaseModel):
    """Input query from user for processing"""
    query_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str
    context: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class QueryContext(BaseModel):
    """Standardized context structure for query classification and processing

    This model provides a typed, validated structure for passing context information
    between components, replacing raw dictionaries for better type safety and clarity.

    Attributes:
        session_id: Unique session identifier
        case_id: Optional case identifier if query is case-related
        conversation_history: Previous conversation messages for context-aware classification
        same_provider_for_response: Flag indicating if classification and response use same LLM provider
        user_metadata: Optional user-specific metadata (preferences, permissions, etc.)

    Examples:
        >>> context = QueryContext(
        ...     session_id="abc123",
        ...     case_id="case456",
        ...     conversation_history="User: What's the error?\\nAssistant: Connection timeout",
        ...     same_provider_for_response=True
        ... )
        >>> context.validate_for_classification()
        True
    """
    session_id: str = ""
    case_id: Optional[str] = None
    conversation_history: str = ""
    same_provider_for_response: bool = False
    user_metadata: Dict[str, Any] = Field(default_factory=dict)

    def validate_for_classification(self) -> bool:
        """Validate that context has minimum required fields for classification

        Returns:
            True if context is valid for classification, False otherwise
        """
        return bool(self.session_id)

    def has_conversation_context(self) -> bool:
        """Check if conversation history is available

        Returns:
            True if conversation_history is non-empty
        """
        return bool(self.conversation_history and self.conversation_history.strip())


class QueryIntent(str, Enum):
    """Query intent classification - v3.0 Response-Format-Driven Design

    16 intents designed backward from 9 ResponseType formats (1.8:1 ratio).
    Each intent maps to distinct ResponseType or shares format with semantically different intents.
    """
    # GROUP 1: SIMPLE ANSWER INTENTS (10) → ResponseType.ANSWER
    INFORMATION = "information"  # Merged: EXPLANATION, DOCUMENTATION into INFORMATION
    STATUS_CHECK = "status_check"  # Merged: MONITORING into STATUS_CHECK
    PROCEDURAL = "procedural"  # How-to and capability questions (e.g., "can I do X?", "how do I Y?")
    VALIDATION = "validation"  # Hypothetical/confirmation questions (e.g., "this won't work, right?")
    BEST_PRACTICES = "best_practices"
    GREETING = "greeting"
    GRATITUDE = "gratitude"
    OFF_TOPIC = "off_topic"
    META_FAULTMAVEN = "meta_faultmaven"
    CONVERSATION_CONTROL = "conversation_control"

    # GROUP 2: STRUCTURED PLAN INTENTS (3) → ResponseType.PLAN_PROPOSAL
    CONFIGURATION = "configuration"
    OPTIMIZATION = "optimization"
    DEPLOYMENT = "deployment"  # NEW v3.0: Deployment planning and execution

    # GROUP 3: VISUAL RESPONSE INTENTS (2) → Specialized ResponseTypes
    VISUALIZATION = "visualization"  # NEW v3.0: Diagrams, flowcharts → ResponseType.VISUAL_DIAGRAM
    COMPARISON = "comparison"  # NEW v3.0: Feature comparisons, pros/cons → ResponseType.COMPARISON_TABLE

    # GROUP 4: DIAGNOSTIC INTENT (1) → Dynamic ResponseType (workflow-driven)
    TROUBLESHOOTING = "troubleshooting"  # Merged: PROBLEM_RESOLUTION, ROOT_CAUSE_ANALYSIS, INCIDENT_RESPONSE

    # GROUP 5: FALLBACK (1)
    UNKNOWN = "unknown"


class QueryClassification(BaseModel):
    """Complete query classification result"""
    classification_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    query: str
    normalized_query: str
    intent: QueryIntent
    confidence: float
    complexity: str = "moderate"  # simple, moderate, complex
    domain: str = "general"
    urgency: str = "medium"  # low, medium, high, critical
    entities: List[Dict[str, Any]] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)
    classification_timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    classification_method: str = "pattern_based"
    processing_recommendations: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class QueryComplexity(BaseModel):
    """Assessment of query complexity"""
    complexity_score: float
    factors: Dict[str, Any] = Field(default_factory=dict)
    processing_recommendation: str
    estimated_duration: Optional[float] = None


class QueryDomain(BaseModel):
    """Domain classification for query"""
    domain_id: str
    domain_name: str
    confidence: float
    subdomain: Optional[str] = None


class QueryUrgency(str, Enum):
    """Urgency levels for queries - v3.0 standardized naming"""
    LOW = "low"
    MEDIUM = "medium"  # Renamed from NORMAL for consistency
    HIGH = "high"
    CRITICAL = "critical"


class ClassificationResult(BaseModel):
    """Complete classification result"""
    query_classification: QueryClassification
    intent: QueryIntent
    complexity: QueryComplexity
    domain: QueryDomain
    urgency: QueryUrgency
    processing_metadata: Dict[str, Any] = Field(default_factory=dict)


class QueryClassificationResult(BaseModel):
    """Alias for ClassificationResult - used in tests"""
    query_classification: QueryClassification
    intent: QueryIntent
    complexity: QueryComplexity
    domain: QueryDomain
    urgency: QueryUrgency
    processing_metadata: Dict[str, Any] = Field(default_factory=dict)


class SecurityBoundary(BaseModel):
    """Security boundary definition"""
    boundary_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    rules: List[Dict[str, Any]] = Field(default_factory=list)
    enforcement_level: str = "strict"


class ContentPolicy(BaseModel):
    """Content policy definition"""
    policy_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    rules: List[Dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True


class ComplianceResult(BaseModel):
    """Result of compliance check"""
    compliant: bool
    violations: List[PolicyViolation] = Field(default_factory=list)
    score: float = 1.0


class PIIDetectionResult(BaseModel):
    """Result of PII detection"""
    entities_found: List[Dict[str, Any]] = Field(default_factory=list)
    anonymized_text: Optional[str] = None
    confidence: float = 1.0


class ValidationRequest(BaseModel):
    """Request for validation"""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: Dict[str, Any]
    validation_type: str
    context: Dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    """Result of validation"""
    request_id: str
    valid: bool
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class SecurityLevel(str, Enum):
    """Security levels"""
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class GuardrailsResult(BaseModel):
    """Result from guardrails evaluation"""
    evaluation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    passed: bool
    violations: List[PolicyViolation] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    security_level: SecurityLevel


class ProcessingResult(BaseModel):
    """Generic processing result"""
    result_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    success: bool
    data: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class QualityMetrics(BaseModel):
    """Quality metrics for various operations"""
    accuracy: float = 0.0
    completeness: float = 0.0
    relevance: float = 0.0
    coherence: float = 0.0
    overall_score: float = 0.0


class ResponseTemplate(BaseModel):
    """Template for response formatting"""
    template_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    format_type: str
    template_content: str
    variables: List[str] = Field(default_factory=list)


class ResponseSource(BaseModel):
    """Source of response information"""
    source_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    content: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SynthesisRequest(BaseModel):
    """Request for response synthesis"""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sources: List[ResponseSource] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)
    format_requirements: Dict[str, Any] = Field(default_factory=dict)


class SynthesisResult(BaseModel):
    """Result of response synthesis"""
    request_id: str
    synthesized_content: str
    quality_metrics: QualityMetrics
    sources_used: List[str] = Field(default_factory=list)


class ContentFormat(str, Enum):
    """Content format types"""
    MARKDOWN = "markdown"
    HTML = "html"
    JSON = "json"
    PLAIN_TEXT = "plain_text"


class QualityAssessment(BaseModel):
    """Assessment of content quality"""
    assessment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content_id: str
    metrics: QualityMetrics
    feedback: List[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class AgentCapabilities(BaseModel):
    """Extended agent capabilities"""
    capabilities_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    available_tools: List[str] = Field(default_factory=list)
    skill_levels: Dict[str, float] = Field(default_factory=dict)
    performance_history: Dict[str, float] = Field(default_factory=dict)


class ToolExecutionRequest(BaseModel):
    """Request for tool execution"""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tool_name: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    safety_requirements: List[str] = Field(default_factory=list)


class ToolExecutionResult(BaseModel):
    """Result of tool execution"""
    request_id: str
    tool_name: str
    success: bool
    result_data: Dict[str, Any] = Field(default_factory=dict)
    execution_time: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SafetyAssessment(BaseModel):
    """Safety assessment for operations"""
    assessment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    operation: str
    safety_level: SafetyLevel
    risks: List[str] = Field(default_factory=list)
    mitigations: List[str] = Field(default_factory=list)


class PerformanceMetrics(BaseModel):
    """Performance metrics tracking"""
    metrics_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    operation_type: str
    latency: float = 0.0
    throughput: float = 0.0
    error_rate: float = 0.0
    resource_usage: Dict[str, float] = Field(default_factory=dict)


class CapabilityDiscovery(BaseModel):
    """Result of capability discovery"""
    discovery_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    discovered_capabilities: List[AgentCapability] = Field(default_factory=list)
    matching_score: float = 0.0
    recommendations: List[str] = Field(default_factory=list)


class ExecutionStep(BaseModel):
    """Individual step in execution plan"""
    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    operation_type: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    prerequisites: List[str] = Field(default_factory=list)
    success_criteria: Dict[str, Any] = Field(default_factory=dict)


class AgentResponse(BaseModel):
    """Response from an agent"""
    response_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    content: str
    confidence: float = 1.0
    sources: List[ResponseSource] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorContext(BaseModel):
    """Context information for errors"""
    context_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    operation: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    system_state: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorType(str, Enum):
    """Types of errors"""
    SYSTEM_ERROR = "system_error"
    USER_ERROR = "user_error"
    NETWORK_ERROR = "network_error"
    TIMEOUT_ERROR = "timeout_error"
    VALIDATION_ERROR = "validation_error"
    SECURITY_ERROR = "security_error"


class ErrorSeverity(str, Enum):
    """Error severity levels"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RecoveryStrategy(BaseModel):
    """Strategy for error recovery"""
    strategy_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    steps: List[Dict[str, Any]] = Field(default_factory=list)
    success_probability: float = 0.0
    estimated_time: Optional[float] = None


class CircuitBreakerState(str, Enum):
    """Circuit breaker states"""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class HealthStatus(str, Enum):
    """System health status"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class AlertLevel(str, Enum):
    """Alert levels"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ErrorClassification(BaseModel):
    """Classification of an error"""
    classification_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    error_type: ErrorType
    severity: ErrorSeverity
    category: str
    tags: List[str] = Field(default_factory=list)
    confidence: float = 1.0


class RecoveryResult(BaseModel):
    """Result of recovery attempt"""
    recovery_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    strategy_used: str
    success: bool
    recovery_time: float = 0.0
    side_effects: List[str] = Field(default_factory=list)


class FallbackConfig(BaseModel):
    """Configuration for fallback behavior"""
    config_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    error_types: List[ErrorType] = Field(default_factory=list)
    fallback_strategies: List[str] = Field(default_factory=list)
    max_retries: int = 3
    backoff_strategy: str = "exponential"


class ComplianceReport(BaseModel):
    """Comprehensive compliance evaluation report"""
    report_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    evaluation_id: str
    compliance_status: str
    violations_found: List[PolicyViolation] = Field(default_factory=list)
    risk_level: str = "low"
    recommendations: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SafetyClassification(BaseModel):
    """Classification of content safety level"""
    classification_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content_id: str
    safety_level: SafetyLevel
    risk_factors: List[str] = Field(default_factory=list)
    confidence_score: float = 1.0
    reasoning: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ContentFilter(BaseModel):
    """Content filtering configuration and results"""
    filter_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    filter_type: str
    rules: List[Dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True
    sensitivity_level: str = "medium"
    whitelist: List[str] = Field(default_factory=list)
    blacklist: List[str] = Field(default_factory=list)


class ContentBlock(BaseModel):
    """Block of content for synthesis"""
    block_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content_type: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    priority: int = 1
    source_reference: Optional[str] = None


class PresentationFormat(str, Enum):
    """Format for presenting content"""
    MARKDOWN = "markdown"
    HTML = "html"
    JSON = "json" 
    PLAIN_TEXT = "plain_text"
    STRUCTURED = "structured"


class SynthesisMetadata(BaseModel):
    """Metadata for synthesis operations"""
    metadata_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    synthesis_strategy: str
    quality_scores: Dict[str, float] = Field(default_factory=dict)
    processing_time: float = 0.0
    resource_usage: Dict[str, Any] = Field(default_factory=dict)


class WorkflowDefinition(BaseModel):
    """Definition of a workflow"""
    workflow_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    steps: List[Dict[str, Any]] = Field(default_factory=list)
    version: str = "1.0.0"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowExecution(BaseModel):
    """Execution instance of a workflow"""
    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    status: str
    current_step: Optional[str] = None
    execution_context: Dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    steps_completed: List[str] = Field(default_factory=list)
    steps_failed: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowStep(BaseModel):
    """Individual step in a workflow"""
    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    operation: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    dependencies: List[str] = Field(default_factory=list)
    timeout: Optional[int] = None
    retry_policy: Dict[str, Any] = Field(default_factory=dict)


class WorkflowResult(BaseModel):
    """Result of workflow execution"""
    result_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    execution_id: str
    success: bool
    outputs: Dict[str, Any] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)
    execution_time: float = 0.0


class PlanningResult(BaseModel):
    """Result of planning operations"""
    planning_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plan: ExecutionPlan
    confidence: float = 1.0
    alternatives: List[ExecutionPlan] = Field(default_factory=list)
    reasoning: Optional[str] = None


class ObservationResult(BaseModel):
    """Result of observation operations"""
    observation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    observations: List[ObservationData] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)
    insights: List[str] = Field(default_factory=list)


class AdaptationResult(BaseModel):
    """Result of adaptation operations"""
    adaptation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    adaptations: List[AdaptationEvent] = Field(default_factory=list)
    updated_plan: Optional[ExecutionPlan] = None
    rationale: str
    impact_assessment: Dict[str, Any] = Field(default_factory=dict)


class FallbackStrategy(BaseModel):
    """Strategy for handling failures"""
    strategy_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    triggers: List[str] = Field(default_factory=list)
    actions: List[Dict[str, Any]] = Field(default_factory=list)
    success_rate: float = 0.0


class SystemHealthStatus(BaseModel):
    """System health status information"""
    status_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    overall_health: HealthStatus
    component_health: Dict[str, HealthStatus] = Field(default_factory=dict)
    performance_metrics: Dict[str, float] = Field(default_factory=dict)
    alerts: List[Dict[str, Any]] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorMetrics(BaseModel):
    """Metrics for error tracking"""
    metrics_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    error_count: int = 0
    error_rate: float = 0.0
    recovery_rate: float = 0.0
    mean_time_to_recovery: float = 0.0
    error_categories: Dict[str, int] = Field(default_factory=dict)


class AgenticUtils:
    """Utility functions for agentic framework operations"""
    
    @staticmethod
    def generate_correlation_id() -> str:
        """Generate a unique correlation ID"""
        return str(uuid.uuid4())
    
    @staticmethod
    def create_component_message(source: str, target: str, message_type: str, payload: Dict[str, Any]) -> ComponentMessage:
        """Create a component message"""
        return ComponentMessage(
            source_component=source,
            target_component=target,
            message_type=message_type,
            payload=payload
        )
    
    @staticmethod
    def validate_execution_state(state: AgentExecutionState) -> bool:
        """Validate execution state consistency"""
        # Implement validation logic
        return True
    
    @staticmethod
    def merge_observations(observations: List[ObservationData]) -> Dict[str, Any]:
        """Merge multiple observations into consolidated data"""
        merged = {}
        for obs in observations:
            merged.update(obs.data)
        return merged