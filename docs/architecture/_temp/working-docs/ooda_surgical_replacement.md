# OODA Surgical Replacement Plan
## Replacing 5-Step Linear Framework with OODA

**Version:** 1.0  
**Date:** 2025-10-09  
**Objective:** Replace rigid 5-step troubleshooting with flexible OODA framework while preserving all other valid components

---

## Executive Summary

### What We're Replacing

**OUT**: 5-Step Linear SRE Doctrine + Associated Subagent Orchestration
```
❌ Phase 1: Define Blast Radius
❌ Phase 2: Establish Timeline  
❌ Phase 3: Formulate Hypothesis
❌ Phase 4: Validate Hypothesis
❌ Phase 5: Propose Solution
```

**Problems**:
- Too rigid and linear
- Forces artificial progression
- Doesn't handle exploratory troubleshooting well
- Subagent orchestration tightly coupled to phases

**IN**: OODA-Based Adaptive Framework
```
✅ Lifecycle Phases (Strategic): 7 phases with clear purposes
✅ OODA Loops (Tactical): Flexible, iterative investigation
✅ Adaptive orchestration based on investigation state
```

### What We're Keeping

✅ **7-Component Agentic Framework** (minus Workflow Engine internals)
- Query Classification Engine  
- Tool & Skill Broker
- Guardrails & Policy Layer
- Response Synthesizer
- Error Handling & Fallback Manager
- State & Session Manager (enhanced)
- Memory & Planning services

✅ **API Contracts**
- v3.1.0 schema
- 7 ResponseType enum
- ViewState structure
- All existing endpoints

✅ **Frontend**
- Response type-driven rendering
- All UI components
- Case management dashboard

---

## Surgical Replacement Strategy

### Core Principle: Clean Interface Boundaries

```
┌─────────────────────────────────────────────────────────────┐
│              Existing FaultMaven Components                  │
│                    (KEEP AS-IS)                              │
├─────────────────────────────────────────────────────────────┤
│  Classification Engine │ Tool Broker │ Guardrails Layer    │
│  Response Synthesizer  │ Error Manager │ Memory Service    │
│  Planning Service      │ Prompt Engine │ Session Service   │
└─────────────────────────────────────────────────────────────┘
                            ▲
                            │ Clean Interfaces
                            │
┌───────────────────────────▼─────────────────────────────────┐
│           NEW: OODA Workflow Orchestrator                    │
│         (REPLACES: Old Workflow Engine)                      │
├─────────────────────────────────────────────────────────────┤
│  • Phase Lifecycle Management (7 phases)                     │
│  • OODA Loop Controller (Frame→Scan→Branch→Test→Conclude)  │
│  • Investigation State Management                            │
│  • Phase Transition Logic                                    │
│  • OODA-to-ResponseType Mapping                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Part 1: Architecture Design

### 1.1 New Component Structure

```python
# NEW COMPONENT: OODA Workflow Orchestrator
# Location: faultmaven/services/agentic/ooda_workflow_orchestrator.py

from faultmaven.services.agentic.workflow_engine import IWorkflowEngine
from faultmaven.models.interfaces import (
    IClassificationEngine,
    IToolBroker,
    IGuardrailsLayer,
    IMemoryService,
    IPlanningService,
    IPromptEngine
)

class OODAWorkflowOrchestrator(IWorkflowEngine):
    """
    OODA-based workflow orchestration
    
    REPLACES: services/agentic/workflow_engine.py (old 5-step implementation)
    IMPLEMENTS: Same IWorkflowEngine interface
    USES: All existing FaultMaven services via dependency injection
    """
    
    def __init__(
        self,
        # Existing FaultMaven services (injected)
        classification_engine: IClassificationEngine,
        tool_broker: IToolBroker,
        guardrails_layer: IGuardrailsLayer,
        memory_service: IMemoryService,
        planning_service: IPlanningService,
        prompt_engine: IPromptEngine,
        llm_provider: ILLMProvider,
        tracer: ITracer,
        
        # OODA-specific components (new, internal)
        phase_transition_engine: PhaseTransitionEngine = None,
        ooda_controller: OODAController = None,
        evidence_tracker: EvidenceTracker = None,
        hypothesis_manager: HypothesisManager = None
    ):
        """
        Initialize with all dependencies
        
        Existing services are injected from DI container (no changes needed)
        OODA components are created internally (encapsulated)
        """
        # Store existing services
        self.classifier = classification_engine
        self.tools = tool_broker
        self.guardrails = guardrails_layer
        self.memory = memory_service
        self.planning = planning_service
        self.prompts = prompt_engine
        self.llm = llm_provider
        self.tracer = tracer
        
        # Initialize OODA components (internal)
        self.phase_engine = phase_transition_engine or PhaseTransitionEngine()
        self.ooda = ooda_controller or OODAController()
        self.evidence = evidence_tracker or EvidenceTracker()
        self.hypotheses = hypothesis_manager or HypothesisManager()
        
        # Response type mapper (bridges OODA to FaultMaven ResponseType)
        self.response_mapper = OODAResponseTypeMapper()
    
    async def execute(
        self,
        query: str,
        session_id: str,
        case_id: str,
        context: Dict[str, Any]
    ) -> WorkflowResult:
        """
        Main execution method - SAME SIGNATURE as old workflow engine
        
        This is the ONLY public method that matters.
        Internal implementation is completely different (OODA-based).
        """
        # Load investigation state (enhanced with OODA tracking)
        state = await self._load_investigation_state(case_id, session_id)
        
        # Execute OODA-based workflow
        result = await self._execute_ooda_workflow(query, state, context)
        
        # Save updated state
        await self._save_investigation_state(state)
        
        return result
```

### 1.2 Investigation State Model (Enhanced)

```python
# Location: faultmaven/models/ooda_state.py

from dataclasses import dataclass, field
from typing import Optional, List, Dict
from datetime import datetime
from enum import Enum

# IMPORTANT: Separate session and case concerns
@dataclass
class InvestigationState:
    """
    OODA investigation state
    
    CRITICAL: This is case-scoped, not session-scoped
    Multiple sessions can access the same investigation
    """
    # Identity (separate session from case)
    case_id: str                    # Persistent investigation
    session_id: str                 # Current session accessing this case
    user_id: str                    # Case owner
    
    # Timestamps
    created_at: datetime
    updated_at: datetime
    
    # Agent mode
    agent_mode: AgentMode = AgentMode.CONSULTANT
    
    # Lifecycle phases (strategic layer)
    current_phase: int = 0          # 0-6
    phase_history: List[PhaseExecution] = field(default_factory=list)
    
    # OODA state (tactical layer, used in phases 1-5)
    current_ooda_step: Optional[OODAStep] = None
    current_ooda_iteration: int = 0
    ooda_iterations: List[OODAIteration] = field(default_factory=list)
    
    # Investigation mode & urgency
    investigation_mode: Optional[InvestigationMode] = None  # active_incident | post_mortem
    urgency_level: Optional[UrgencyLevel] = None
    
    # Problem definition
    problem_statement: Optional[str] = None
    anomaly_frame: Optional[AnomalyFrame] = None
    
    # Evidence & hypotheses
    evidence_items: Dict[str, EvidenceItem] = field(default_factory=dict)
    hypotheses: Dict[str, Hypothesis] = field(default_factory=dict)
    
    # Results
    root_cause_identified: bool = False
    root_cause: Optional[Dict] = None
    solution: Optional[Dict] = None
    mitigation_applied: Optional[Dict] = None
    
    # Conversation memory (referenced, not duplicated)
    conversation_history_ref: str = ""  # Reference to conversation in Case
    
    # Progress tracking
    iterations_without_progress: int = 0
    same_category_test_count: Dict[str, int] = field(default_factory=dict)
    
    # Escalation
    escalation_recommended: bool = False
    escalation_reason: Optional[str] = None

# Storage structure (CORRECTED for case/session separation)
"""
Redis keys:
- session:{session_id} → SessionData (temporary, TTL)
- case:{case_id} → CaseData (persistent, owned by user)
- investigation:{case_id} → InvestigationState (persistent, case-scoped)
- user:{user_id}:cases → Set<case_id>
"""
```

### 1.3 Phase Lifecycle (7 Phases - Strategic Layer)

```python
# Location: faultmaven/services/agentic/phase_lifecycle.py

class PhaseLifecycle:
    """
    7-phase lifecycle management
    
    These phases provide STRUCTURE and CLARITY
    Each phase has a clear PURPOSE and GOAL
    Within each phase, OODA provides FLEXIBILITY
    """
    
    PHASES = {
        0: {
            "name": "Intake",
            "purpose": "Problem confirmation and initial assessment",
            "uses_ooda": False,
            "typical_response_types": [
                ResponseType.CLARIFICATION_REQUEST,
                ResponseType.ANSWER
            ],
            "completion_criteria": [
                "problem_statement_captured",
                "urgency_assessed"
            ]
        },
        
        1: {
            "name": "Problem Definition",
            "purpose": "Frame the anomaly and understand scope",
            "uses_ooda": True,  # ← OODA helps here
            "ooda_focus": "Frame + Scan",
            "typical_response_types": [
                ResponseType.CLARIFICATION_REQUEST,
                ResponseType.NEEDS_MORE_DATA,
                ResponseType.ANSWER
            ],
            "completion_criteria": [
                "anomaly_framed",
                "blast_radius_defined",
                "evidence_coverage > 0.5"
            ]
        },
        
        2: {
            "name": "Triage",
            "purpose": "Generate hypotheses and prioritize investigation",
            "uses_ooda": True,  # ← OODA helps here
            "ooda_focus": "Scan + Branch",
            "typical_response_types": [
                ResponseType.ANSWER,
                ResponseType.NEEDS_MORE_DATA
            ],
            "completion_criteria": [
                "hypotheses_generated >= 2",
                "evidence_coverage > 0.6"
            ]
        },
        
        3: {
            "name": "Mitigation",
            "purpose": "Restore service (active incidents only)",
            "uses_ooda": True,  # ← OODA helps here
            "ooda_focus": "Test (mitigation options)",
            "typical_response_types": [
                ResponseType.PLAN_PROPOSAL,
                ResponseType.CONFIRMATION_REQUEST,
                ResponseType.ANSWER
            ],
            "completion_criteria": [
                "service_restored"
            ],
            "skip_if": "investigation_mode == post_mortem"
        },
        
        4: {
            "name": "Root Cause Analysis",
            "purpose": "Deep investigation with full OODA cycles",
            "uses_ooda": True,  # ← FULL OODA loops here
            "ooda_focus": "All steps (Frame→Scan→Branch→Test→Conclude)",
            "typical_response_types": [
                ResponseType.ANSWER,
                ResponseType.NEEDS_MORE_DATA,
                ResponseType.PLAN_PROPOSAL
            ],
            "completion_criteria": [
                "root_cause_identified",
                "confidence >= 0.7"
            ]
        },
        
        5: {
            "name": "Solution Design",
            "purpose": "Design permanent fix",
            "uses_ooda": True,  # ← OODA helps here
            "ooda_focus": "Test (solution validation)",
            "typical_response_types": [
                ResponseType.SOLUTION_READY,
                ResponseType.PLAN_PROPOSAL,
                ResponseType.CONFIRMATION_REQUEST
            ],
            "completion_criteria": [
                "solution_designed"
            ]
        },
        
        6: {
            "name": "Documentation",
            "purpose": "Generate post-mortem and runbooks",
            "uses_ooda": False,
            "typical_response_types": [
                ResponseType.ANSWER
            ],
            "completion_criteria": [
                "documentation_generated"
            ]
        }
    }
    
    @classmethod
    def get_phase_info(cls, phase: int) -> Dict:
        """Get metadata about a phase"""
        return cls.PHASES.get(phase, {})
    
    @classmethod
    def uses_ooda(cls, phase: int) -> bool:
        """Check if phase uses OODA"""
        return cls.PHASES.get(phase, {}).get("uses_ooda", False)
    
    @classmethod
    def get_typical_response_types(cls, phase: int) -> List[ResponseType]:
        """Get typical response types for phase"""
        return cls.PHASES.get(phase, {}).get("typical_response_types", [])
```

---

## Part 2: OODA-to-ResponseType Bridge

### 2.1 The Critical Mapping Logic

```python
# Location: faultmaven/services/agentic/ooda_response_mapper.py

class OODAResponseTypeMapper:
    """
    Maps OODA execution results to FaultMaven ResponseType
    
    This is THE CRITICAL BRIDGE between OODA and FaultMaven frontend
    """
    
    def determine_response_type(
        self,
        state: InvestigationState,
        ooda_result: Dict[str, Any],
        context: ConversationContext
    ) -> ResponseType:
        """
        Determine ResponseType based on:
        1. Current phase
        2. OODA step (if applicable)
        3. Investigation state
        4. Result content
        """
        
        phase = state.current_phase
        
        # Phase 0: Intake
        if phase == 0:
            return self._map_intake_response(state, ooda_result)
        
        # Phase 1: Problem Definition
        elif phase == 1:
            return self._map_problem_definition_response(state, ooda_result)
        
        # Phase 2: Triage
        elif phase == 2:
            return self._map_triage_response(state, ooda_result)
        
        # Phase 3: Mitigation
        elif phase == 3:
            return self._map_mitigation_response(state, ooda_result)
        
        # Phase 4: RCA (Full OODA)
        elif phase == 4:
            return self._map_rca_response(state, ooda_result)
        
        # Phase 5: Solution
        elif phase == 5:
            return self._map_solution_response(state, ooda_result)
        
        # Phase 6: Documentation
        elif phase == 6:
            return ResponseType.ANSWER
        
        # Default
        return ResponseType.ANSWER
    
    def _map_intake_response(
        self,
        state: InvestigationState,
        result: Dict
    ) -> ResponseType:
        """Phase 0: Intake mapping"""
        
        # If we don't have problem statement yet
        if not state.problem_statement:
            return ResponseType.CLARIFICATION_REQUEST
        
        # If urgency not assessed
        if not state.urgency_level:
            return ResponseType.CLARIFICATION_REQUEST
        
        # Otherwise, provide initial assessment
        return ResponseType.ANSWER
    
    def _map_problem_definition_response(
        self,
        state: InvestigationState,
        result: Dict
    ) -> ResponseType:
        """Phase 1: Problem Definition mapping"""
        
        # If we're requesting evidence
        if result.get("evidence_requests"):
            return ResponseType.NEEDS_MORE_DATA
        
        # If anomaly frame not confident enough
        if state.anomaly_frame and state.anomaly_frame.confidence < 0.6:
            return ResponseType.CLARIFICATION_REQUEST
        
        # If we need more information to proceed
        if len(state.evidence_items) < 2:
            return ResponseType.NEEDS_MORE_DATA
        
        # Otherwise, present framing
        return ResponseType.ANSWER
    
    def _map_triage_response(
        self,
        state: InvestigationState,
        result: Dict
    ) -> ResponseType:
        """Phase 2: Triage mapping"""
        
        # If requesting more evidence
        if result.get("evidence_requests"):
            return ResponseType.NEEDS_MORE_DATA
        
        # If presenting hypotheses
        if result.get("hypotheses"):
            return ResponseType.ANSWER
        
        # Default
        return ResponseType.ANSWER
    
    def _map_mitigation_response(
        self,
        state: InvestigationState,
        result: Dict
    ) -> ResponseType:
        """Phase 3: Mitigation mapping"""
        
        # If presenting mitigation plan with multiple steps
        if result.get("mitigation_options") and len(result["mitigation_options"]) > 1:
            return ResponseType.PLAN_PROPOSAL
        
        # If asking for confirmation before action
        if result.get("requires_confirmation"):
            return ResponseType.CONFIRMATION_REQUEST
        
        # If escalation recommended
        if result.get("escalation_recommended"):
            return ResponseType.ESCALATION_REQUIRED
        
        # Default: present mitigation option
        return ResponseType.ANSWER
    
    def _map_rca_response(
        self,
        state: InvestigationState,
        result: Dict
    ) -> ResponseType:
        """Phase 4: RCA (Full OODA) mapping"""
        
        ooda_step = state.current_ooda_step
        
        # Frame step
        if ooda_step == OODAStep.FRAME:
            if result.get("needs_clarification"):
                return ResponseType.CLARIFICATION_REQUEST
            return ResponseType.ANSWER
        
        # Scan step
        elif ooda_step == OODAStep.SCAN:
            if result.get("evidence_gaps"):
                return ResponseType.NEEDS_MORE_DATA
            return ResponseType.ANSWER
        
        # Branch step (generating hypotheses)
        elif ooda_step == OODAStep.BRANCH:
            return ResponseType.ANSWER
        
        # Test step
        elif ooda_step == OODAStep.TEST:
            if result.get("requires_confirmation"):
                return ResponseType.CONFIRMATION_REQUEST
            if result.get("test_plan") and len(result["test_plan"]) > 1:
                return ResponseType.PLAN_PROPOSAL
            return ResponseType.ANSWER
        
        # Conclude step
        elif ooda_step == OODAStep.CONCLUDE:
            if result.get("root_cause_identified"):
                return ResponseType.SOLUTION_READY
            if result.get("escalation_recommended"):
                return ResponseType.ESCALATION_REQUIRED
            return ResponseType.ANSWER
        
        # Default
        return ResponseType.ANSWER
    
    def _map_solution_response(
        self,
        state: InvestigationState,
        result: Dict
    ) -> ResponseType:
        """Phase 5: Solution mapping"""
        
        # If solution ready with implementation plan
        if result.get("solution") and result["solution"].get("implementation_steps"):
            # Multi-step solution
            if len(result["solution"]["implementation_steps"]) > 3:
                return ResponseType.PLAN_PROPOSAL
            # Ready to implement
            return ResponseType.SOLUTION_READY
        
        # If needs confirmation before implementing
        if result.get("requires_confirmation"):
            return ResponseType.CONFIRMATION_REQUEST
        
        # Default
        return ResponseType.ANSWER
```

### 2.2 Response Assembly

```python
# Location: faultmaven/services/agentic/ooda_response_assembler.py

class OODAResponseAssembler:
    """
    Assembles complete AgentResponse with proper structure
    """
    
    def assemble_response(
        self,
        state: InvestigationState,
        case: Case,
        ooda_result: Dict,
        response_type: ResponseType,
        llm_content: str
    ) -> AgentResponse:
        """
        Assemble complete v3.1.0 AgentResponse
        """
        
        # Build ViewState (COMPLETE, all required fields)
        view_state = self._build_view_state(state, case)
        
        # Extract sources from result
        sources = self._extract_sources(ooda_result)
        
        # Build plan if PLAN_PROPOSAL
        plan = None
        if response_type == ResponseType.PLAN_PROPOSAL:
            plan = self._build_plan(ooda_result)
        
        # Build investigation_context (OODA-specific, optional)
        investigation_context = self._build_investigation_context(state, ooda_result)
        
        return AgentResponse(
            schema_version="3.1.0",
            content=llm_content,              # Plain text response
            response_type=response_type,      # UPPERCASE enum
            view_state=view_state,            # Complete ViewState
            sources=sources,
            plan=plan,
            next_action_hint=ooda_result.get("next_action_hint"),
            estimated_time_to_resolution=self._estimate_time(state),
            confidence_score=self._calculate_confidence(state),
            
            # OODA-specific enhancement (optional, backward compatible)
            investigation_context=investigation_context
        )
    
    def _build_view_state(
        self,
        state: InvestigationState,
        case: Case
    ) -> ViewState:
        """Build COMPLETE ViewState with ALL required fields"""
        
        return ViewState(
            # Required FaultMaven fields
            session_id=state.session_id,
            case_id=state.case_id,
            user_id=state.user_id,
            case_title=case.title,
            case_status=self._map_phase_to_case_status(state.current_phase),
            running_summary=self._generate_running_summary(state),
            uploaded_data=case.uploaded_data,
            conversation_count=len(case.conversation_history),
            last_updated=state.updated_at.isoformat(),
            can_upload_data=self._can_upload_data(state.current_phase),
            needs_more_info=self._needs_more_info(state),
            
            # OODA enhancement (optional, additive)
            orchestration_metadata={
                "agent_mode": state.agent_mode.value,
                "current_phase": state.current_phase,
                "phase_name": PhaseLifecycle.PHASES[state.current_phase]["name"],
                "investigation_mode": state.investigation_mode.value if state.investigation_mode else None,
                "urgency_level": state.urgency_level.value if state.urgency_level else None
            }
        )
    
    def _map_phase_to_case_status(self, phase: int) -> str:
        """Map OODA phase to FaultMaven case status"""
        mapping = {
            0: "active",           # Intake
            1: "investigating",    # Problem Definition
            2: "investigating",    # Triage
            3: "investigating",    # Mitigation
            4: "investigating",    # RCA
            5: "investigating",    # Solution
            6: "solved"            # Documentation
        }
        return mapping.get(phase, "active")
    
    def _build_investigation_context(
        self,
        state: InvestigationState,
        result: Dict
    ) -> Optional[Dict]:
        """Build OODA-specific context (optional enhancement)"""
        
        if not PhaseLifecycle.uses_ooda(state.current_phase):
            return None
        
        return {
            "ooda_step": state.current_ooda_step.value if state.current_ooda_step else None,
            "ooda_iteration": state.current_ooda_iteration,
            "evidence_requests": result.get("evidence_requests", []),
            "hypotheses": [
                self._format_hypothesis(h)
                for h in result.get("hypotheses", [])
            ],
            "phase_transition_available": result.get("phase_complete", False),
            "escalation_recommended": state.escalation_recommended
        }
```

---

## Part 3: Integration with Existing Services

### 3.1 DI Container Registration (Minimal Changes)

```python
# Location: faultmaven/container.py

class DIContainer:
    """
    Dependency Injection Container
    
    CHANGE: Only swap out workflow engine registration
    KEEP: All other service registrations unchanged
    """
    
    def __init__(self):
        # ... existing initialization ...
        pass
    
    async def initialize(self):
        """Initialize all services"""
        
        # ... existing service initialization (UNCHANGED) ...
        
        # Classification Engine (KEEP)
        self._services["classification_engine"] = ClassificationEngine(...)
        
        # Tool Broker (KEEP)
        self._services["tool_broker"] = ToolBroker(...)
        
        # Guardrails Layer (KEEP)
        self._services["guardrails_layer"] = GuardrailsLayer(...)
        
        # Memory Service (KEEP)
        self._services["memory_service"] = MemoryService(...)
        
        # Planning Service (KEEP)
        self._services["planning_service"] = PlanningService(...)
        
        # Prompt Engine (KEEP)
        self._services["prompt_engine"] = PromptEngine(...)
        
        # CHANGE: Replace workflow engine
        # OLD:
        # self._services["workflow_engine"] = WorkflowEngine(...)
        
        # NEW:
        self._services["workflow_engine"] = OODAWorkflowOrchestrator(
            classification_engine=self.get("classification_engine"),
            tool_broker=self.get("tool_broker"),
            guardrails_layer=self.get("guardrails_layer"),
            memory_service=self.get("memory_service"),
            planning_service=self.get("planning_service"),
            prompt_engine=self.get("prompt_engine"),
            llm_provider=self.get("llm_provider"),
            tracer=self.get("tracer")
            # OODA components created internally
        )
        
        # ... rest of initialization (UNCHANGED) ...
```

### 3.2 Agent Service (No Changes Required)

```python
# Location: faultmaven/services/agent.py

class AgentService:
    """
    Agent Service - NO CHANGES NEEDED
    
    Uses IWorkflowEngine interface - doesn't care about implementation
    """
    
    def __init__(
        self,
        workflow_engine: IWorkflowEngine,  # ← Interface, not concrete class
        # ... other dependencies ...
    ):
        self._workflow = workflow_engine
        # ... rest of initialization ...
    
    async def process_query(self, request: QueryRequest) -> AgentResponse:
        """
        Process query - NO CHANGES NEEDED
        
        Workflow engine is swapped at DI level, not here
        """
        
        # Classification (UNCHANGED)
        classification = await self._classifier.classify(request.query)
        
        # Guardrails (UNCHANGED)
        validated_query = await self._guardrails.validate(request.query)
        
        # Memory retrieval (UNCHANGED)
        memory_context = await self._memory.retrieve_context(
            request.session_id,
            validated_query
        )
        
        # Planning (UNCHANGED)
        plan = await self._planning.plan_response_strategy(
            validated_query,
            memory_context
        )
        
        # Workflow execution (CHANGED internally, same interface)
        result = await self._workflow.execute(
            query=validated_query,
            session_id=request.session_id,
            case_id=request.case_id,
            context={
                "classification": classification,
                "memory": memory_context,
                "plan": plan
            }
        )
        
        # Response synthesis (UNCHANGED)
        response = await self._synthesizer.synthesize(result)
        
        # Memory consolidation (UNCHANGED)
        await self._memory.consolidate_insights(request.session_id, result)
        
        return response
```

---

## Part 4: Implementation Roadmap

### Week 1-2: Core OODA Framework

**Goal**: Build OODA orchestration skeleton

#### Tasks:
1. **Create OODA data models** (3 days)
   ```
   - models/ooda_state.py (InvestigationState, etc.)
   - models/ooda_enums.py (OODAStep, PhaseLifecycle, etc.)
   ```

2. **Implement Phase Lifecycle** (2 days)
   ```
   - services/agentic/phase_lifecycle.py
   - 7-phase metadata and rules
   ```

3. **Implement OODA Controller** (3 days)
   ```
   - services/agentic/ooda_controller.py
   - Frame→Scan→Branch→Test→Conclude logic
   ```

4. **Implement Phase Transition Engine** (2 days)
   ```
   - services/agentic/phase_transition_engine.py
   - Transition validation and execution
   ```

**Deliverable**: Core OODA framework components (not integrated yet)

---

### Week 3-4: Response Type Bridge

**Goal**: Connect OODA to FaultMaven ResponseType system

#### Tasks:
1. **Implement Response Type Mapper** (3 days)
   ```
   - services/agentic/ooda_response_mapper.py
   - All phase→ResponseType mapping logic
   ```

2. **Implement Response Assembler** (3 days)
   ```
   - services/agentic/ooda_response_assembler.py
   - Complete AgentResponse assembly
   - ViewState generation with ALL required fields
   ```

3. **Implement Evidence Tracker** (2 days)
   ```
   - services/agentic/evidence_tracker.py
   - Evidence collection and management
   ```

4. **Implement Hypothesis Manager** (2 days)
   ```
   - services/agentic/hypothesis_manager.py
   - Hypothesis lifecycle management
   ```

**Deliverable**: Complete OODA-to-FaultMaven bridge

---

### Week 5-6: Workflow Orchestrator Integration

**Goal**: Create OODAWorkflowOrchestrator and integrate with existing services

#### Tasks:
1. **Implement OODAWorkflowOrchestrator** (5 days)
   ```
   - services/agentic/ooda_workflow_orchestrator.py
   - Implements IWorkflowEngine interface
   - Integrates all OODA components
   - Calls existing FaultMaven services
   ```

2. **Update DI Container** (1 day)
   ```
   - container.py
   - Replace workflow_engine registration
   ```

3. **Update State Storage** (2 days)
   ```
   - infrastructure/persistence/redis_investigation_store.py
   - Separate session and case storage
   - Investigation state persistence
   ```

4. **Integration Testing** (2 days)
   ```
   - Test with existing services
   - Verify all interfaces work
   ```

**Deliverable**: Fully integrated OODA orchestrator

---

### Week 7-8: Testing & Validation

**Goal**: Comprehensive testing and bug fixes

#### Tasks:
1. **Unit Tests** (3 days)
   ```
   - Test all OODA components individually
   - Test response type mapping
   - Test phase transitions
   ```

2. **Integration Tests** (3 days)
   ```
   - End-to-end OODA flows
   - Multi-phase investigations
   - All ResponseType paths
   ```

3. **Contract Tests** (2 days)
   ```
   - Verify v3.1.0 schema compliance
   - Test all API endpoints
   - Validate ViewState structure
   ```

4. **Bug Fixes** (2 days)
   ```
   - Address issues found in testing
   ```

**Deliverable**: Tested, production-ready OODA system

---

### Week 9-10: Documentation & Deployment

**Goal**: Document changes and deploy to production

#### Tasks:
1. **Architecture Documentation** (2 days)
   ```
   - Update SYSTEM_ARCHITECTURE.md
   - Document OODA framework
   - Update component diagrams
   ```

2. **Developer Guide** (2 days)
   ```
   - OODA development guide
   - Phase lifecycle guide
   - Troubleshooting guide
   ```

3. **Migration Guide** (1 day)
   ```
   - What changed
   - How to migrate
   - Breaking changes (should be none)
   ```

4. **Staged Deployment** (3 days)
   ```
   - Deploy to dev environment
   - Feature flag rollout (10% → 50% → 100%)
   - Monitor metrics
   ```

5. **Production Deployment** (2 days)
   ```
   - Full rollout
   - Monitor for issues
   - Rollback plan ready
   ```

**Deliverable**: OODA in production

---

## Part 5: Quality Assurance

### 5.1 Testing Strategy

```python
# tests/integration/test_ooda_workflow.py

class TestOODAWorkflow:
    """Integration tests for OODA workflow"""
    
    async def test_complete_investigation_flow(self):
        """Test complete investigation from start to finish"""
        
        # Phase 0: Intake
        response1 = await agent.process_query("API is down")
        assert response1.view_state.orchestration_metadata["current_phase"] == 0
        assert response1.response_type in [
            ResponseType.CLARIFICATION_REQUEST,
            ResponseType.ANSWER
        ]
        
        # Phase 1: Problem Definition
        response2 = await agent.process_query("Errors started at 14:20 UTC")
        assert response2.view_state.orchestration_metadata["current_phase"] == 1
        assert response2.investigation_context["ooda_step"] == "frame"
        
        # ... continue through all phases ...
        
        # Verify final state
        assert response_final.view_state.case_status == "solved"
        assert response_final.response_type == ResponseType.ANSWER
    
    async def test_response_type_mapping_accuracy(self):
        """Verify ResponseType mapping works correctly"""
        
        # Test all phase→ResponseType mappings
        test_cases = [
            (0, "needs_clarification", ResponseType.CLARIFICATION_REQUEST),
            (1, "needs_evidence", ResponseType.NEEDS_MORE_DATA),
            (2, "presenting_hypotheses", ResponseType.ANSWER),
            (3, "mitigation_plan", ResponseType.PLAN_PROPOSAL),
            (4, "root_cause_found", ResponseType.SOLUTION_READY),
            (5, "solution_ready", ResponseType.SOLUTION_READY),
            (6, "documentation", ResponseType.ANSWER)
        ]
        
        for phase, scenario, expected_type in test_cases:
            result = self._simulate_scenario(phase, scenario)
            actual_type = mapper.determine_response_type(state, result, context)
            assert actual_type == expected_type
    
    async def test_viewstate_completeness(self):
        """Verify ViewState has ALL required fields"""
        
        response = await agent.process_query("Test query")
        view_state = response.view_state
        
        # Required fields check
        required_fields = [
            "session_id", "case_id", "user_id", "case_title",
            "case_status", "running_summary", "uploaded_data",
            "conversation_count", "last_updated",
            "can_upload_data", "needs_more_info"
        ]
        
        for field in required_fields:
            assert hasattr(view_state, field), f"Missing required field: {field}"
            assert getattr(view_state, field) is not None
```

### 5.2 Validation Checklist

**Before Deployment:**

✅ **API Contract Compliance**
- [ ] All responses follow v3.1.0 schema
- [ ] All 7 ResponseType values supported
- [ ] ViewState has ALL required fields
- [ ] No breaking changes to existing endpoints

✅ **Functional Requirements**
- [ ] All 7 phases work correctly
- [ ] OODA loops execute properly in phases 1-5
- [ ] Phase transitions validated correctly
- [ ] Evidence tracking functional
- [ ] Hypothesis management working
- [ ] Escalation logic correct

✅ **Integration Requirements**
- [ ] Classification Engine integrated
- [ ] Tool Broker integrated
- [ ] Guardrails Layer integrated
- [ ] Memory Service integrated
- [ ] Planning Service integrated
- [ ] Prompt Engine integrated

✅ **Frontend Compatibility**
- [ ] All ResponseType paths render correctly
- [ ] ViewState drives UI properly
- [ ] Case dashboard displays correctly
- [ ] OODA visualization (if added) works

✅ **Performance Requirements**
- [ ] Response times < 1 second (excluding LLM)
- [ ] Memory usage acceptable
- [ ] No performance regression vs old system

✅ **Data Integrity**
- [ ] Session/case separation maintained
- [ ] Investigation state persisted correctly
- [ ] Multi-session case access works
- [ ] Case ownership preserved

---

## Part 6: Rollback Plan

### If Issues Detected

**Immediate Rollback** (< 5 minutes):
```python
# In container.py, comment out new registration, uncomment old:

# NEW (disable)
# self._services["workflow_engine"] = OODAWorkflowOrchestrator(...)

# OLD (re-enable)
self._services["workflow_engine"] = WorkflowEngine(...)
```

**Feature Flag Rollback** (if gradual rollout):
```python
# In config/feature_flags.py
ENABLE_OODA_FRAMEWORK = False  # Switch to False
```

**Data Migration Rollback**:
- Investigation state is additive (doesn't break old data)
- Old workflow engine can ignore OODA fields
- No data loss on rollback

---

## Summary

### What We're Doing

**Replacing**: 5-step linear framework → 7-phase OODA framework
**Keeping**: Everything else (all 6 other agentic components, API contracts, frontend)
**Strategy**: Surgical replacement via clean interface boundaries

### Key Design Decisions

1. **OODAWorkflowOrchestrator implements IWorkflowEngine**
   - Same interface as old workflow engine
   - Swapped at DI level, transparent to other services

2. **OODA-to-ResponseType bridge**
   - OODAResponseTypeMapper maps OODA execution to ResponseType
   - Ensures frontend compatibility

3. **7 Lifecycle Phases + OODA Loops**
   - Phases provide structure (strategic)
   - OODA provides flexibility (tactical)
   - Not all phases use full OODA (e.g., Intake, Documentation)

4. **Complete ViewState**
   - All FaultMaven required fields present
   - OODA metadata added as optional enhancement

5. **Session/Case Separation**
   - session_id: temporary connection
   - case_id: persistent investigation
   - investigation_id removed (conflated concerns)

### Timeline

**10 weeks total**
- Weeks 1-2: Core OODA framework
- Weeks 3-4: Response type bridge
- Weeks 5-6: Integration
- Weeks 7-8: Testing
- Weeks 9-10: Deployment

### Success Criteria

✅ Zero breaking changes to API
✅ Frontend works without modifications
✅ All existing services integrated
✅ More flexible than old 5-step system
✅ Maintains or improves performance
✅ Clean, maintainable architecture

This plan provides a **surgical replacement** that preserves all valid components while introducing the flexibility and adaptability of OODA.