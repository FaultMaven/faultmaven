"""Specialist Agent Interfaces for FaultMaven Microservice Architecture

This module defines the interfaces for the 6 specialist agents in the microservice
blueprint. Each agent has a specific role in the troubleshooting workflow and
supports both in-process and distributed deployment modes.

Agent Responsibilities:
- Triage Agent: Problem categorization and severity assessment
- Scoping Agent: Clarifying questions and scope refinement  
- Diagnostic Agent: Hypothesis generation and safe testing
- Validation Agent: Assumption and claim verification
- Pattern Agent: Pattern matching with success rate tracking
- Learning Agent: Continuous learning and knowledge base updates

Design Principles:
- Stateless agent implementations for scalability
- Budget-aware execution with cancellation support
- Comprehensive result documentation for observability
- Error handling with graceful degradation
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, AsyncContextManager
from enum import Enum

from ..microservice_contracts.agent_contracts import (
    AgentRequest, AgentResponse, TriageResult, ScopingResult,
    DiagnosticResult, ValidationResult, PatternResult, LearningResult,
    Budget, ExecutionContext
)


class AgentStatus(Enum):
    """Agent execution status enumeration."""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ITriageAgent(ABC):
    """Triage Agent Interface
    
    Responsibilities:
    - Categorize problems (absurd, vague, specific, complex)
    - Estimate severity and urgency levels
    - Assess user skill level from query patterns
    - Provide initial routing recommendations
    
    SLOs:
    - p95 latency < 200ms
    - 99.5% availability
    - Classification accuracy > 90%
    
    Categories:
    - absurd: Clearly invalid or nonsensical queries
    - vague: Insufficient information for specific help
    - specific: Clear problem with actionable details
    - complex: Multi-faceted issues requiring decomposition
    """
    
    @abstractmethod
    async def triage_problem(self, request: AgentRequest) -> TriageResult:
        """Categorize and assess problem severity and complexity.
        
        This method analyzes the user query to classify the problem type,
        estimate severity, assess user skill level, and provide routing
        recommendations for the orchestrator.
        
        Args:
            request: AgentRequest with query, context, budget, and metadata
            
        Returns:
            TriageResult containing:
            - category: Problem classification (absurd/vague/specific/complex)
            - severity: Severity assessment (low/medium/high/critical)
            - urgency: Time sensitivity (low/medium/high/emergency)
            - user_skill_estimate: Estimated user skill level
            - routing_recommendations: Suggested next agents
            - confidence_score: Classification confidence (0.0-1.0)
            - reasoning: Explanation of classification decisions
            
        Classification Logic:
            - absurd: Nonsensical, physically impossible, or clearly invalid
            - vague: Missing critical details, unclear scope, ambiguous
            - specific: Sufficient detail for targeted troubleshooting
            - complex: Multiple interrelated issues, system-wide impact
            
        Severity Factors:
            - Impact: Number of users/systems affected
            - Business criticality: Revenue or safety implications
            - Urgency: Time sensitivity and SLA considerations
            - Complexity: Technical difficulty and resource requirements
            
        User Skill Assessment:
            - Query sophistication and technical terminology usage
            - Problem description clarity and diagnostic detail
            - Historical interaction patterns (if available)
            - Self-reported expertise and comfort level
        """
        pass
    
    @abstractmethod
    async def estimate_resolution_effort(self, problem: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Estimate effort required for problem resolution.
        
        Args:
            problem: Problem description
            context: Problem context including severity and complexity
            
        Returns:
            Effort estimation including:
            - time_estimate: Expected resolution time range
            - resource_requirements: Skills and tools needed
            - confidence_interval: Estimation confidence bounds
            - risk_factors: Factors that could increase effort
        """
        pass
    
    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """Get triage agent health and performance metrics."""
        pass


class IScopingAgent(ABC):
    """Scoping Agent Interface
    
    Responsibilities:
    - Generate targeted clarifying questions (max 2 per turn)
    - Refine problem scope and boundaries
    - Identify missing context and information gaps
    - Template-based question generation for consistency
    
    SLOs:
    - p95 latency < 300ms
    - 99.5% availability
    - Question effectiveness rate > 80%
    
    Question Templates:
    - Error context: When did this start? What changed recently?
    - Environment: Which system/environment? What configuration?
    - Impact: Who is affected? What's the business impact?
    - Reproduction: Can you reproduce this? What are the exact steps?
    """
    
    @abstractmethod
    async def generate_questions(self, request: AgentRequest) -> ScopingResult:
        """Generate targeted clarifying questions to refine problem scope.
        
        This method analyzes vague or incomplete problem descriptions to
        generate up to 2 targeted questions that will most effectively
        clarify the scope and provide actionable context.
        
        Args:
            request: AgentRequest with problem description and available context
            
        Returns:
            ScopingResult containing:
            - questions: List of clarifying questions (max 2)
            - question_rationale: Explanation for each question's importance
            - information_gaps: Identified missing information categories
            - scope_assessment: Current understanding of problem boundaries
            - priority_ranking: Question priority order
            - expected_clarification: Anticipated information from each question
            
        Question Generation Strategy:
            - Identify the most critical information gaps
            - Prioritize questions by impact on troubleshooting effectiveness
            - Use templates for consistency but adapt to specific context
            - Avoid redundant questions based on available context
            - Focus on actionable information that enables progress
            
        Question Categories:
            - temporal: Timeline and change correlation questions
            - environmental: System, configuration, and deployment context
            - impact: Scope of effect and user impact assessment
            - technical: Specific error messages, logs, and symptoms
            - procedural: Steps to reproduce and previous attempts
        """
        pass
    
    @abstractmethod
    async def assess_scope_clarity(self, problem: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Assess how well-scoped a problem description is.
        
        Args:
            problem: Problem description to assess
            context: Available context information
            
        Returns:
            Scope assessment including:
            - clarity_score: Overall clarity rating (0.0-1.0)
            - completeness: Information completeness assessment
            - actionability: Whether enough info exists to proceed
            - missing_categories: Categories of missing information
        """
        pass
    
    @abstractmethod
    async def refine_scope(self, original_problem: str, additional_info: Dict[str, Any]) -> str:
        """Refine problem scope based on additional information.
        
        Args:
            original_problem: Original problem description
            additional_info: New information from clarifying questions
            
        Returns:
            Refined problem statement with improved scope and clarity
        """
        pass


class IDiagnosticAgent(ABC):
    """Diagnostic Agent Interface
    
    Responsibilities:
    - Generate top-K hypotheses for problem causes
    - Design and execute safe diagnostic tests
    - Budget-aware execution with cancellation support
    - Parallel test execution with result correlation
    
    SLOs:
    - p95 latency < 800ms
    - 99.5% availability  
    - Hypothesis accuracy > 75%
    
    Test Types:
    - Information gathering: Log analysis, config inspection
    - Non-invasive probes: Network connectivity, service health
    - Safe diagnostic commands: Read-only operations
    - Controlled experiments: Isolated test environments only
    """
    
    @abstractmethod
    async def generate_hypotheses(self, request: AgentRequest) -> DiagnosticResult:
        """Generate ranked hypotheses for problem causes.
        
        This method analyzes the problem description and available context
        to generate multiple hypotheses about root causes, ranked by
        likelihood and testability.
        
        Args:
            request: AgentRequest with problem details and diagnostic context
            
        Returns:
            DiagnosticResult containing:
            - hypotheses: List of ranked root cause hypotheses
            - hypothesis_scores: Likelihood scores for each hypothesis
            - test_plans: Diagnostic test plans for hypothesis validation
            - risk_assessments: Safety assessment for each test
            - resource_requirements: Budget and tool requirements
            - parallel_execution_plan: Tests that can run concurrently
            
        Hypothesis Generation:
            - Pattern matching against known issues
            - Causal reasoning based on symptoms and context
            - Statistical correlation from historical data
            - Expert system rules and decision trees
            - Machine learning prediction models
            
        Hypothesis Ranking Factors:
            - Historical frequency of similar issues
            - Match quality with reported symptoms
            - Diagnostic test feasibility and safety
            - Fix complexity and success probability
            - Business impact and urgency alignment
        """
        pass
    
    @abstractmethod
    async def execute_diagnostic_tests(
        self, 
        hypotheses: List[Dict[str, Any]], 
        budget: Budget,
        context: ExecutionContext
    ) -> DiagnosticResult:
        """Execute diagnostic tests for hypothesis validation.
        
        This method runs safe diagnostic tests in parallel where possible,
        respecting budget constraints and supporting cancellation for
        responsive user experience.
        
        Args:
            hypotheses: List of hypotheses with associated test plans
            budget: Execution budget (time, tokens, calls)
            context: Execution context with safety constraints
            
        Returns:
            DiagnosticResult with test results, validated hypotheses,
            budget usage, and next step recommendations
            
        Test Execution Strategy:
            - Prioritize tests by information value and safety
            - Execute non-interfering tests in parallel
            - Respect budget constraints with early termination
            - Support cancellation for long-running tests
            - Aggregate results for hypothesis validation
            
        Safety Constraints:
            - Only read-only operations in production
            - User confirmation for any state-changing operations
            - Automatic test cancellation on budget exhaustion
            - Graceful degradation on partial test failures
        """
        pass
    
    @abstractmethod
    async def correlate_findings(self, test_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Correlate diagnostic test findings to update hypothesis confidence.
        
        Args:
            test_results: Results from executed diagnostic tests
            
        Returns:
            Correlation analysis including:
            - updated_hypotheses: Hypotheses with updated confidence scores
            - supporting_evidence: Evidence supporting each hypothesis
            - contradictory_evidence: Evidence contradicting hypotheses
            - confidence_changes: How test results affected confidence
        """
        pass
    
    @abstractmethod 
    async def cancel_execution(self, execution_id: str) -> bool:
        """Cancel ongoing diagnostic test execution.
        
        Args:
            execution_id: Identifier for the execution to cancel
            
        Returns:
            True if cancellation successful, False otherwise
        """
        pass


class IValidationAgent(ABC):
    """Validation Agent Interface
    
    Responsibilities:
    - Validate assumptions and claims from other agents
    - Verify proposed actions before execution
    - Cross-reference with known good practices
    - Risk assessment for proposed solutions
    
    SLOs:
    - p95 latency < 300ms
    - 99.5% availability
    - Validation accuracy > 95%
    
    Validation Types:
    - Logical consistency: Internal consistency of claims
    - Technical accuracy: Accuracy against known facts
    - Safety assessment: Risk evaluation of proposed actions
    - Best practice compliance: Alignment with standards
    """
    
    @abstractmethod
    async def validate_assumptions(self, request: AgentRequest) -> ValidationResult:
        """Validate assumptions and claims for accuracy and safety.
        
        This method examines assumptions, claims, and proposed actions to
        verify their logical consistency, technical accuracy, and safety
        implications before execution.
        
        Args:
            request: AgentRequest with assumptions, claims, or proposed actions
            
        Returns:
            ValidationResult containing:
            - validation_status: Overall validation result
            - validated_items: Individual validation results
            - risk_assessment: Safety and risk analysis
            - compliance_check: Best practice compliance assessment
            - confidence_score: Validation confidence level
            - recommendations: Modifications or additional precautions
            
        Validation Dimensions:
            - logical_consistency: Internal logical coherence
            - technical_accuracy: Accuracy against known facts and constraints
            - safety_implications: Potential negative consequences
            - compliance: Alignment with best practices and standards
            - feasibility: Practical implementability
            
        Risk Categories:
            - data_loss: Risk of data corruption or deletion
            - service_disruption: Risk of service unavailability
            - security_impact: Risk of security vulnerabilities
            - performance_impact: Risk of performance degradation
            - irreversible_changes: Changes that cannot be undone
        """
        pass
    
    @abstractmethod
    async def verify_proposed_action(self, action: Dict[str, Any], context: Dict[str, Any]) -> ValidationResult:
        """Verify proposed action for safety and effectiveness.
        
        Args:
            action: Proposed action with details and parameters
            context: Action context including environment and constraints
            
        Returns:
            ValidationResult with safety assessment, effectiveness prediction,
            and required precautions or modifications
        """
        pass
    
    @abstractmethod
    async def cross_reference_solution(self, solution: Dict[str, Any]) -> Dict[str, Any]:
        """Cross-reference solution against knowledge base and best practices.
        
        Args:
            solution: Proposed solution to cross-reference
            
        Returns:
            Cross-reference results including:
            - similar_solutions: Previously successful similar solutions
            - best_practice_alignment: Compliance with established practices
            - risk_precedents: Historical risks from similar actions
            - success_probability: Estimated success likelihood
        """
        pass


class IPatternAgent(ABC):
    """Pattern Agent Interface
    
    Responsibilities:
    - Pattern matching with success rates and versioning
    - Maintain pattern database with effectiveness tracking
    - Learn from successful resolution patterns
    - Support pattern similarity and clustering analysis
    
    SLOs:
    - p95 latency < 400ms
    - 99.5% availability
    - Pattern match accuracy > 85%
    
    Pattern Types:
    - Symptom patterns: Common error symptom combinations
    - Solution patterns: Effective resolution workflows
    - Anti-patterns: Known ineffective or dangerous approaches
    - Escalation patterns: When to involve human experts
    """
    
    @abstractmethod
    async def match_patterns(self, request: AgentRequest) -> PatternResult:
        """Find patterns matching current problem and context.
        
        This method searches the pattern database for similar issues,
        considering symptom patterns, context similarity, and historical
        effectiveness to provide ranked pattern matches.
        
        Args:
            request: AgentRequest with problem description and context
            
        Returns:
            PatternResult containing:
            - pattern_matches: Ranked list of matching patterns
            - success_rates: Historical success rates for each pattern
            - confidence_scores: Pattern match confidence levels
            - context_similarity: How well context matches historical cases
            - effectiveness_data: Detailed effectiveness metrics
            - pattern_versions: Version information for matched patterns
            
        Pattern Matching Algorithm:
            - Symptom similarity using embedding vectors
            - Context matching including environment and system type
            - Historical success rate weighting
            - Recency bias for pattern relevance
            - User skill level and experience matching
            
        Success Metrics:
            - resolution_rate: Percentage of cases successfully resolved
            - user_satisfaction: Average user satisfaction scores
            - time_to_resolution: Average time to problem resolution
            - escalation_rate: Percentage requiring human intervention
            - false_positive_rate: Pattern mismatch frequency
        """
        pass
    
    @abstractmethod
    async def update_pattern_success(
        self, 
        pattern_id: str, 
        outcome: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> bool:
        """Update pattern success metrics based on resolution outcome.
        
        Args:
            pattern_id: Identifier of the pattern used
            outcome: Resolution outcome including success, time, satisfaction
            context: Problem context for pattern refinement
            
        Returns:
            True if pattern update successful
            
        Notes:
            - Updates aggregated success metrics
            - Refines pattern matching algorithms
            - Identifies patterns needing retirement
            - Triggers pattern versioning when needed
        """
        pass
    
    @abstractmethod
    async def create_pattern(self, problem: str, solution: str, metadata: Dict[str, Any]) -> str:
        """Create new pattern from successful resolution.
        
        Args:
            problem: Problem description that was resolved
            solution: Successful solution approach
            metadata: Pattern metadata including context and effectiveness
            
        Returns:
            Pattern identifier for future reference
        """
        pass
    
    @abstractmethod
    async def get_pattern_analytics(self, pattern_id: Optional[str] = None) -> Dict[str, Any]:
        """Get pattern effectiveness analytics.
        
        Args:
            pattern_id: Optional specific pattern ID, or None for overall stats
            
        Returns:
            Analytics including success rates, usage frequency, and trends
        """
        pass


class ILearningAgent(ABC):
    """Learning Agent Interface
    
    Responsibilities:
    - Batch learning on troubleshooting outcomes
    - Governed knowledge base and pattern updates
    - Shadow evaluation of new knowledge before deployment
    - Canary rollout with automatic rollback capabilities
    
    SLOs:
    - Batch processing latency < 5 minutes
    - 99.5% availability
    - Knowledge accuracy improvement > 5% per update
    
    Learning Types:
    - Outcome learning: Success/failure pattern analysis
    - User feedback: Explicit user satisfaction and corrections
    - Expert review: Human expert knowledge incorporation
    - System performance: Automated metric-based learning
    """
    
    @abstractmethod
    async def process_learning_batch(self, outcomes: List[Dict[str, Any]]) -> LearningResult:
        """Process batch of troubleshooting outcomes for learning.
        
        This method analyzes a batch of resolution outcomes to extract
        patterns, update success rates, identify new knowledge, and
        generate knowledge base updates with proper governance.
        
        Args:
            outcomes: List of troubleshooting outcomes with results and metadata
            
        Returns:
            LearningResult containing:
            - learned_patterns: New patterns identified from outcomes
            - knowledge_updates: Proposed knowledge base updates
            - pattern_updates: Updates to existing pattern success rates
            - confidence_improvements: Improvements to confidence calibration
            - governance_status: Approval status for knowledge updates
            - deployment_plan: Staged deployment plan for updates
            
        Learning Pipeline:
            - Outcome aggregation and pattern extraction
            - Success rate calculation and trend analysis
            - New knowledge identification and validation
            - Governance workflow for knowledge approval
            - Shadow evaluation against historical cases
            - Canary deployment with success metrics
            
        Governance Process:
            - Automated validation against known good outcomes
            - Human expert review for significant changes
            - Shadow evaluation period for new knowledge
            - Gradual rollout with automatic rollback triggers
            - Comprehensive audit trail for all changes
        """
        pass
    
    @abstractmethod
    async def evaluate_knowledge_update(
        self, 
        update: Dict[str, Any], 
        validation_cases: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Evaluate proposed knowledge update against validation cases.
        
        Args:
            update: Proposed knowledge base update
            validation_cases: Historical cases for validation
            
        Returns:
            Evaluation results including:
            - accuracy_improvement: Change in accuracy metrics
            - coverage_improvement: Change in knowledge coverage
            - risk_assessment: Risk of negative impact
            - recommendation: Deploy/reject/modify recommendation
        """
        pass
    
    @abstractmethod
    async def deploy_knowledge_update(
        self, 
        update_id: str, 
        deployment_config: Dict[str, Any]
    ) -> bool:
        """Deploy approved knowledge update with canary rollout.
        
        Args:
            update_id: Identifier of approved knowledge update
            deployment_config: Deployment configuration including rollout stages
            
        Returns:
            True if deployment initiated successfully
            
        Deployment Process:
            - Shadow evaluation (0% traffic, logging only)
            - Canary deployment (5% traffic) 
            - Gradual rollout (25%, 50%, 100%)
            - Automatic rollback on metric degradation
            - Success confirmation and audit logging
        """
        pass
    
    @abstractmethod
    async def rollback_update(self, update_id: str) -> bool:
        """Rollback knowledge update due to performance degradation.
        
        Args:
            update_id: Identifier of update to rollback
            
        Returns:
            True if rollback successful
        """
        pass
    
    @abstractmethod
    async def get_learning_metrics(self) -> Dict[str, Any]:
        """Get learning system performance metrics.
        
        Returns:
            Learning metrics including:
            - knowledge_growth_rate: Rate of knowledge base expansion
            - accuracy_improvements: Historical accuracy improvements
            - deployment_success_rate: Success rate of knowledge deployments
            - rollback_frequency: Frequency of required rollbacks
        """
        pass


# Base Agent Interface
class IBaseAgent(ABC):
    """Base interface for all specialist agents providing common functionality."""
    
    @abstractmethod
    async def execute(self, request: AgentRequest) -> AgentResponse:
        """Execute agent-specific logic with standardized request/response.
        
        Args:
            request: Standardized agent request with query, context, and budget
            
        Returns:
            AgentResponse with agent-specific results and execution metadata
        """
        pass
    
    @abstractmethod
    async def get_capabilities(self) -> Dict[str, Any]:
        """Get agent capabilities and supported operations.
        
        Returns:
            Capabilities description including:
            - supported_operations: List of operations this agent can perform
            - input_requirements: Required input data and formats
            - output_formats: Output data formats and schemas
            - resource_requirements: Typical resource usage patterns
        """
        pass
    
    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """Get agent health status and performance metrics.
        
        Returns:
            Health status including:
            - status: healthy/degraded/unhealthy
            - performance_metrics: Key performance indicators
            - resource_utilization: Current resource usage
            - error_rates: Recent error frequency and types
        """
        pass
    
    @abstractmethod
    async def get_execution_context(self) -> ExecutionContext:
        """Get current execution context and constraints.
        
        Returns:
            ExecutionContext with current budget limits, safety constraints,
            and execution preferences
        """
        pass