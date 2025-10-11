# API Impact Analysis: OODA v2.0 Migration

**Version:** 1.0  
**Date:** 2025-10-09  
**Related Documents:** 
- OODA-Based Prompt Engineering Architecture v2.0
- Agent Orchestration Design v2.0
- System Architecture v2.0

---

## Executive Summary

The migration from the **7-Component Agentic Framework** to the **OODA-based system** will have **minimal API surface changes**. The v3.1.0 schema is already well-designed and can accommodate the new orchestration layer with only **internal processing changes** and **minor response enhancements**.

**Key Insight**: The existing API is sufficiently flexible. Changes are primarily in:
1. **Internal request processing** (how queries are handled)
2. **Response metadata enrichment** (new fields in existing structures)
3. **Optional new endpoints** (additive, non-breaking)

---

## Current API Endpoints (No Changes Required)

### Core Endpoints (Remain Unchanged)

```yaml
POST   /api/v1/cases/{case_id}/queries    # Main query endpoint - NO BREAKING CHANGES
GET    /api/v1/cases/{case_id}            # Retrieve case - ENHANCED RESPONSE
POST   /api/v1/sessions                   # Create session - NO CHANGES
GET    /api/v1/sessions/{session_id}      # Get session - NO CHANGES
DELETE /api/v1/sessions/{session_id}      # Delete session - NO CHANGES
POST   /api/v1/knowledge/ingest           # Upload knowledge - NO CHANGES
GET    /api/v1/knowledge/search           # Search knowledge - NO CHANGES
POST   /api/v1/data/upload                # Upload files - NO CHANGES
GET    /api/v1/health                     # Health check - NO CHANGES
```

**All existing endpoints maintain backward compatibility.**

---

## What Changes: Internal Processing Only

### POST /api/v1/cases/{case_id}/queries

**External API Contract**: ✅ **NO BREAKING CHANGES**

**Request Format** (Unchanged):
```json
{
  "session_id": "sess-abc123",
  "query": "Production API is returning 500 errors",
  "context": {
    "uploaded_files": ["error.log"]
  }
}
```

**Response Format** (v3.1.0 Schema - Enhanced, Not Changed):
```json
{
  "schema_version": "3.1.0",
  "content": "I can help you troubleshoot this. Let me analyze the error logs...",
  "response_type": "ANSWER",
  "view_state": {
    "session_id": "sess-abc123",
    "case_id": "case-xyz789",
    "running_summary": "Investigating API 500 errors",
    "uploaded_data": ["error.log"],
    
    // NEW FIELDS (additive, backward compatible)
    "orchestration_metadata": {
      "agent_mode": "investigator",
      "current_phase": 1,
      "phase_name": "Problem Definition",
      "investigation_mode": "active_incident",
      "urgency_level": "high"
    }
  },
  "sources": [...],
  "plan": null,
  
  // NEW FIELD (additive, backward compatible)
  "investigation_context": {
    "ooda_step": "frame",
    "ooda_iteration": 1,
    "evidence_requests": [
      {
        "evidence_id": "ev-001",
        "label": "Database connection metrics",
        "category": "infrastructure",
        "guidance": {
          "commands": ["kubectl logs api-service"],
          "alternatives": ["Check CloudWatch metrics"]
        }
      }
    ],
    "hypotheses": [
      {
        "hypothesis_id": "hyp-001",
        "statement": "Database connection pool exhausted",
        "likelihood": 0.75,
        "category": "infrastructure"
      }
    ]
  }
}
```

**What Changed Internally**:

**OLD: 7-Component Agentic Framework Processing**
```python
# services/agent.py - OLD implementation
async def process_query(request: QueryRequest) -> AgentResponse:
    # 1. Query Classification Engine
    classification = await classify_query(request.query)
    
    # 2. Workflow Engine orchestration
    workflow_result = await workflow_engine.execute(
        query=request.query,
        classification=classification
    )
    
    # 3. Response Synthesizer
    response = await synthesizer.synthesize(workflow_result)
    
    return response
```

**NEW: OODA-based Orchestration**
```python
# services/agent.py - NEW implementation
async def process_query(request: QueryRequest) -> AgentResponse:
    # 1. Load investigation state
    state = await state_manager.load_state(request.session_id)
    
    # 2. Route to appropriate handler (Consultant vs Investigator)
    if state.agent_mode == AgentMode.CONSULTANT:
        # Simple mode detection and potential mode switch
        response = await consultant_handler.process(request, state)
    else:
        # Full OODA orchestration
        response = await investigator_handler.process(request, state)
    
    # 3. Update state and persist
    await state_manager.save_state(state)
    
    return response

async def investigator_handler.process(request, state):
    # Phase-based routing
    phase_handler = get_phase_handler(state.current_phase)
    
    # If in RCA phase, use OODA steps
    if state.current_phase == 4:  # RCA
        ooda_handler = get_ooda_handler(state.current_ooda_step)
        result = await ooda_handler.execute(request, state)
    else:
        result = await phase_handler.execute(request, state)
    
    # Enrich response with OODA context
    response = format_response(result, state)
    return response
```

**Key Differences**:
- ✅ **Request format**: Identical
- ✅ **Response structure**: Compatible (v3.1.0 schema)
- ✅ **Response type**: Still uses ResponseType enum
- ⚠️ **Response metadata**: Enhanced with new optional fields
- ⚠️ **Internal processing**: Completely different (stateful vs stateless)

---

## Enhanced Response Fields (Additive, Non-Breaking)

### New Fields in ViewState

```typescript
// OLD ViewState (v3.1.0)
interface ViewState {
  session_id: string;
  case_id: string;
  running_summary: string;
  uploaded_data: string[];
}

// NEW ViewState (v3.1.0 + OODA enhancements)
interface ViewState {
  session_id: string;
  case_id: string;
  running_summary: string;
  uploaded_data: string[];
  
  // NEW: Orchestration metadata (optional, backward compatible)
  orchestration_metadata?: {
    agent_mode: "consultant" | "investigator";
    current_phase?: number;
    phase_name?: string;
    investigation_mode?: "active_incident" | "post_mortem";
    urgency_level?: "low" | "medium" | "high" | "critical";
  };
}
```

### New Top-Level Field: investigation_context

```typescript
// NEW: Investigation context (optional field)
interface AgentResponse {
  schema_version: "3.1.0";
  content: string;
  response_type: ResponseType;
  view_state: ViewState;
  sources: Source[];
  plan?: Plan;
  
  // NEW: OODA investigation context (optional, backward compatible)
  investigation_context?: {
    ooda_step?: "frame" | "scan" | "branch" | "test" | "conclude";
    ooda_iteration?: number;
    evidence_requests?: EvidenceRequest[];
    hypotheses?: Hypothesis[];
    phase_transition_available?: boolean;
    escalation_recommended?: boolean;
  };
}
```

**Backward Compatibility**: 
- Old clients ignore new fields
- New clients can leverage enhanced metadata
- No breaking changes to existing contracts

---

## Optional New Endpoints (Additive)

### 1. POST /api/v1/cases/{case_id}/evidence

**Purpose**: Allow explicit evidence submission separate from queries

```python
@router.post("/cases/{case_id}/evidence")
async def submit_evidence(
    case_id: str,
    evidence: EvidenceSubmission,
    session_service: ISessionService = Depends(get_session_service),
    agent_service: IAgentService = Depends(get_agent_service)
):
    """
    Submit evidence for an active investigation
    
    Request:
    {
      "session_id": "sess-abc123",
      "evidence": {
        "label": "Database slow query log",
        "content": "SELECT * FROM users WHERE...",
        "category": "infrastructure",
        "source": "manual"
      }
    }
    
    Response:
    {
      "evidence_id": "ev-003",
      "added": true,
      "coverage_score": 0.85,
      "message": "Evidence added successfully"
    }
    """
    # Load investigation state
    state = await agent_service.load_investigation_state(
        case_id, 
        evidence.session_id
    )
    
    # Add evidence
    evidence_id = await agent_service.add_evidence(state, evidence.evidence)
    
    # Save state
    await agent_service.save_investigation_state(state)
    
    return {
        "evidence_id": evidence_id,
        "added": True,
        "coverage_score": calculate_coverage(state),
        "message": "Evidence added successfully"
    }
```

**Impact**: NEW - Optional enhancement, no existing functionality affected

---

### 2. POST /api/v1/cases/{case_id}/transition

**Purpose**: Allow explicit phase transitions

```python
@router.post("/cases/{case_id}/transition")
async def transition_phase(
    case_id: str,
    transition: PhaseTransitionRequest,
    agent_service: IAgentService = Depends(get_agent_service)
):
    """
    Request phase transition
    
    Request:
    {
      "session_id": "sess-abc123",
      "target_phase": 3,
      "reason": "Ready to mitigate"
    }
    
    Response:
    {
      "success": true,
      "previous_phase": 2,
      "current_phase": 3,
      "phase_name": "Mitigation"
    }
    """
    state = await agent_service.load_investigation_state(
        case_id,
        transition.session_id
    )
    
    # Validate transition
    can_transition, reason = await agent_service.can_transition(
        state,
        transition.target_phase
    )
    
    if not can_transition:
        raise HTTPException(400, detail=reason)
    
    # Execute transition
    previous_phase = state.current_phase
    await agent_service.transition_phase(state, transition.target_phase)
    
    return {
        "success": True,
        "previous_phase": previous_phase,
        "current_phase": state.current_phase,
        "phase_name": get_phase_name(state.current_phase)
    }
```

**Impact**: NEW - Optional enhancement, no existing functionality affected

---

### 3. GET /api/v1/cases/{case_id}/export

**Purpose**: Export investigation for documentation

```python
@router.get("/cases/{case_id}/export")
async def export_investigation(
    case_id: str,
    format: str = Query("json", enum=["json", "markdown", "pdf"]),
    agent_service: IAgentService = Depends(get_agent_service)
):
    """
    Export investigation in various formats
    
    Response (markdown):
    # Post-Mortem: API Timeout Incident
    
    **Date**: 2025-10-09
    **Severity**: Critical
    
    ## Summary
    ...
    
    ## Root Cause
    ...
    """
    state = await agent_service.load_investigation_state(case_id)
    
    if format == "markdown":
        doc = await agent_service.generate_postmortem(state)
        return Response(doc, media_type="text/markdown")
    
    elif format == "json":
        return state.to_dict()
    
    elif format == "pdf":
        pdf = await agent_service.generate_pdf(state)
        return Response(pdf, media_type="application/pdf")
```

**Impact**: NEW - Optional enhancement, no existing functionality affected

---

## Enhanced GET /api/v1/cases/{case_id}

**External API**: Compatible, enhanced response

**OLD Response**:
```json
{
  "case_id": "case-xyz789",
  "session_id": "sess-abc123",
  "created_at": "2025-10-09T14:20:00Z",
  "status": "active",
  "summary": "Investigating API errors",
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

**NEW Response** (Enhanced, backward compatible):
```json
{
  "case_id": "case-xyz789",
  "session_id": "sess-abc123",
  "created_at": "2025-10-09T14:20:00Z",
  "updated_at": "2025-10-09T15:30:00Z",
  "status": "active",
  "summary": "Investigating API errors",
  "messages": [...],
  
  // NEW: Investigation metadata (optional)
  "investigation_metadata": {
    "agent_mode": "investigator",
    "current_phase": 4,
    "phase_name": "Root Cause Analysis",
    "ooda_step": "test",
    "ooda_iteration": 2,
    "urgency_level": "high",
    "investigation_mode": "active_incident"
  },
  
  // NEW: Investigation state summary (optional)
  "investigation_summary": {
    "problem_statement": "API returning 500 errors",
    "anomaly_frame": {
      "statement": "API timing out connecting to database",
      "confidence": 0.85,
      "severity": "critical"
    },
    "evidence_count": 5,
    "hypotheses_count": 3,
    "top_hypothesis": {
      "statement": "Connection leak in v2.3.1",
      "likelihood": 0.85
    },
    "root_cause_identified": false
  }
}
```

**Impact**: ⚠️ Enhanced response (backward compatible)

---

## Internal State Storage Changes

### Redis Key Structure Changes

**OLD Structure** (7-Component Agentic):
```
session:{session_id}                  → SessionState
case:{case_id}                        → CaseState
user:{user_id}:sessions               → Set<session_id>
```

**NEW Structure** (OODA-based):
```
session:{session_id}                  → SessionState (unchanged)
case:{case_id}                        → CaseState (unchanged)
investigation:{case_id}:{session_id}  → InvestigationState (NEW)
user:{user_id}:sessions               → Set<session_id> (unchanged)
```

**New Investigation State Structure**:
```python
@dataclass
class InvestigationState:
    investigation_id: str
    case_id: str
    session_id: str
    created_at: datetime
    updated_at: datetime
    
    # Agent configuration
    agent_mode: AgentMode  # consultant | investigator
    investigation_mode: Optional[InvestigationMode]  # active_incident | post_mortem
    urgency_level: Optional[UrgencyLevel]
    
    # Lifecycle progress
    current_phase: int
    phase_history: List[PhaseExecution]
    
    # OODA progress (Phase 4 only)
    current_ooda_step: Optional[OODAStep]
    current_ooda_iteration: int
    ooda_iterations: List[OODAIteration]
    
    # Problem definition
    problem_statement: Optional[str]
    anomaly_frame: Optional[AnomalyFrame]
    
    # Investigation data
    evidence_items: Dict[str, EvidenceItem]
    hypotheses: Dict[str, Hypothesis]
    
    # Results
    root_cause_identified: bool
    root_cause: Optional[Dict]
    solution: Optional[Dict]
    
    # Memory
    conversation_history: List[Dict]
```

**Storage Implementation**:
```python
# services/agent.py
class AgentService:
    async def load_investigation_state(
        self,
        case_id: str,
        session_id: str
    ) -> InvestigationState:
        """Load investigation state from Redis"""
        key = f"investigation:{case_id}:{session_id}"
        data = await self.redis.get(key)
        
        if data:
            return InvestigationState.from_dict(json.loads(data))
        else:
            # Create new investigation
            return InvestigationState(
                investigation_id=f"inv-{uuid.uuid4().hex[:12]}",
                case_id=case_id,
                session_id=session_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                agent_mode=AgentMode.CONSULTANT,
                current_phase=0
            )
    
    async def save_investigation_state(
        self,
        state: InvestigationState
    ) -> None:
        """Save investigation state to Redis"""
        key = f"investigation:{state.case_id}:{state.session_id}"
        state.updated_at = datetime.utcnow()
        
        await self.redis.set(
            key,
            json.dumps(state.to_dict()),
            ex=86400  # 24 hour TTL
        )
```

---

## Migration Path: Gradual Rollout

### Phase 1: Parallel Processing (Week 1-2)

**Goal**: Run both systems side-by-side

```python
# services/agent.py
class AgentService:
    def __init__(
        self,
        use_ooda_system: bool = False,  # Feature flag
        ...
    ):
        self.use_ooda = use_ooda_system
        self.legacy_workflow = WorkflowEngine(...)  # OLD
        self.ooda_orchestrator = OrchestrationController(...)  # NEW
    
    async def process_query(self, request: QueryRequest) -> AgentResponse:
        if self.use_ooda:
            # NEW: OODA-based processing
            return await self._process_with_ooda(request)
        else:
            # OLD: 7-Component Agentic processing
            return await self._process_with_workflow_engine(request)
```

**Rollout Strategy**:
- Enable OODA for 10% of new sessions
- Monitor response quality and performance
- Compare metrics between old and new systems
- Gradually increase percentage

### Phase 2: Default to OODA (Week 3-4)

**Goal**: Make OODA the default, keep legacy as fallback

```python
async def process_query(self, request: QueryRequest) -> AgentResponse:
    try:
        # Try OODA first
        return await self._process_with_ooda(request)
    except OODAProcessingError as e:
        # Fallback to legacy on error
        logger.warning(f"OODA processing failed, falling back: {e}")
        return await self._process_with_workflow_engine(request)
```

### Phase 3: Remove Legacy (Week 5+)

**Goal**: Complete migration, remove old code

```python
async def process_query(self, request: QueryRequest) -> AgentResponse:
    # Only OODA processing
    return await self._process_with_ooda(request)
```

---

## Client Impact Assessment

### Browser Extension

**Required Changes**: ✅ **MINIMAL**

**What Works Without Changes**:
- All existing query submission
- Response display (content, response_type)
- Source attribution
- View state synchronization

**Optional Enhancements** (client can choose to adopt):
```javascript
// Display OODA investigation metadata
if (response.investigation_context) {
  displayInvestigationProgress({
    phase: response.view_state.orchestration_metadata.phase_name,
    step: response.investigation_context.ooda_step,
    iteration: response.investigation_context.ooda_iteration
  });
  
  // Show evidence requests
  if (response.investigation_context.evidence_requests) {
    showEvidenceRequests(response.investigation_context.evidence_requests);
  }
  
  // Show hypotheses
  if (response.investigation_context.hypotheses) {
    showHypotheses(response.investigation_context.hypotheses);
  }
}
```

### API Clients

**Required Changes**: ✅ **NONE**

**Optional Enhancements**:
- Can use new `/evidence` endpoint for structured evidence submission
- Can use `/transition` endpoint for explicit phase control
- Can use `/export` endpoint for documentation generation
- Can display enhanced investigation metadata

---

## Performance Impact

### Response Time Changes

**OLD System** (7-Component Agentic):
```
Query Classification:        50ms
Workflow Engine:            100ms
Tool Execution:             500ms
Response Synthesis:          50ms
----------------------------------
Total:                      700ms
```

**NEW System** (OODA-based):
```
State Load (Redis):          10ms
Phase/OODA Routing:          20ms
Prompt Assembly:             30ms
Tool Execution:             500ms
State Save (Redis):          10ms
Response Formatting:         30ms
----------------------------------
Total:                      600ms
```

**Expected Improvement**: ~15% faster due to:
- Simpler routing logic
- Cached state in Redis
- Optimized prompt assembly
- Eliminated redundant classification steps

### Storage Impact

**Additional Redis Storage**:
- Per investigation: ~50-200KB (depending on evidence/hypotheses)
- 1000 active investigations: ~50-200MB
- TTL: 24 hours (configurable)

**Impact**: ✅ **MINIMAL** - Well within Redis capacity

---

## Testing Strategy

### 1. Contract Testing

**Goal**: Ensure API contracts unchanged

```python
# tests/api/test_contract_compatibility.py
async def test_query_endpoint_backward_compatibility():
    """Verify old clients still work"""
    
    # OLD request format
    request = {
        "session_id": "sess-test",
        "query": "Test query"
    }
    
    response = await client.post(
        "/api/v1/cases/test-case/queries",
        json=request
    )
    
    # Verify v3.1.0 schema compliance
    assert response.status_code == 200
    assert "schema_version" in response.json()
    assert response.json()["schema_version"] == "3.1.0"
    assert "content" in response.json()
    assert "response_type" in response.json()
    assert "view_state" in response.json()
    
    # New fields should be present but optional
    assert "investigation_context" in response.json()  # Can be null
```

### 2. Integration Testing

**Goal**: Verify OODA system works end-to-end

```python
async def test_ooda_investigation_flow():
    """Test complete OODA investigation"""
    
    # 1. Initial query (should create investigation)
    response1 = await submit_query("API is down")
    assert response1["view_state"]["orchestration_metadata"]["agent_mode"] == "investigator"
    assert response1["view_state"]["orchestration_metadata"]["current_phase"] == 1
    
    # 2. Provide evidence (should advance)
    response2 = await submit_query("Here are the error logs: ...")
    assert response2["investigation_context"]["evidence_requests"] is not None
    
    # 3. Continue investigation
    response3 = await submit_query("Database connection metrics show...")
    assert len(response3["investigation_context"]["hypotheses"]) > 0
```

### 3. Performance Testing

**Goal**: Ensure no performance regression

```python
async def test_response_time_performance():
    """Verify response times acceptable"""
    
    start = time.time()
    response = await submit_query("Test query")
    duration = time.time() - start
    
    # Should be faster than 1 second (excluding LLM time)
    assert duration < 1.0
```

---

## Rollback Plan

### Immediate Rollback

**If issues detected in production**:

1. **Feature Flag**:

**Current Implementation:**
```python
@app.route('/query', methods=['POST'])
def query():
    """
    Simple query endpoint
    
    Request:
    {
        "query": "What is the error?"
    }
    
    Response:
    {
        "response": "LLM generated response",
        "session_id": "abc123"
    }
    """
    session_id = request.headers.get('X-Session-ID')
    data = request.json
    query = data.get('query')
    
    # Simple LLM call
    response = llm.generate(query)
    
    return jsonify({
        'response': response,
        'session_id': session_id
    })
```

**New Implementation (v2.0):**
```python
@app.route('/query', methods=['POST'])
async def query():
    """
    Enhanced query endpoint with full orchestration
    
    Request:
    {
        "query": "Production API is down with 500 errors",
        "investigation_id": "inv-abc123" (optional, auto-created if missing)
    }
    
    Response:
    {
        "answer": "I can help you troubleshoot...",
        "investigation_id": "inv-abc123",
        "metadata": {
            "agent_mode": "investigator",
            "current_phase": 1,
            "phase_name": "Problem Definition",
            "ooda_step": "frame",
            "urgency_level": "high",
            "investigation_mode": "active_incident"
        },
        "evidence_requests": [
            {
                "evidence_id": "ev-001",
                "label": "Error logs from API",
                "description": "Need to see the exact error messages",
                "category": "symptoms",
                "guidance": {
                    "commands": ["kubectl logs api-service"],
                    "file_locations": ["/var/log/api.log"]
                }
            }
        ],
        "hypotheses": [],
        "phase_transition_prompt": null,
        "escalation": null
    }
    """
    
    # 1. Extract investigation_id from request or header
    investigation_id = (
        request.json.get('investigation_id') or 
        request.headers.get('X-Investigation-ID')
    )
    
    # 2. Process through orchestration controller
    agent = FaultMavenAgent()
    result = await agent.process_message(
        user_message=request.json['query'],
        investigation_id=investigation_id
    )
    
    # 3. Set investigation_id header for client
    response = jsonify(result)
    response.headers['X-Investigation-ID'] = result['investigation_id']
    
    return response
```

**Breaking Changes:**
- ❌ Response format changed significantly
- ❌ Session ID → Investigation ID terminology change
- ❌ Single `response` string → Complex structured response
- ❌ Requires async/await support

**Backward Compatibility Strategy:**
```python
@app.route('/query', methods=['POST'])
async def query():
    """Version-aware endpoint"""
    
    # Check API version
    api_version = request.headers.get('X-API-Version', '1.0')
    
    if api_version == '1.0':
        # Legacy mode: simple response
        result = await agent.process_message(...)
        return jsonify({
            'response': result['message'],
            'session_id': result['investigation_id']
        })
    
    else:
        # v2.0 mode: full response
        result = await agent.process_message(...)
        return jsonify(result)
```

---

### 2. POST /evidence (NEW ENDPOINT)

**Purpose:** Allow clients to submit evidence separately from chat

```python
@app.route('/evidence', methods=['POST'])
async def submit_evidence():
    """
    Submit evidence for active investigation
    
    Request:
    {
        "investigation_id": "inv-abc123",
        "evidence": {
            "label": "API error logs",
            "content": "TimeoutException: Connection to db timed out...",
            "category": "symptoms",
            "source": "kubectl logs"
        }
    }
    
    Response:
    {
        "evidence_id": "ev-003",
        "added": true,
        "investigation_id": "inv-abc123",
        "message": "Evidence added successfully",
        "coverage_score": 0.75
    }
    """
    
    investigation_id = request.json['investigation_id']
    evidence_data = request.json['evidence']
    
    # Load state
    state = await state_manager.load_state(investigation_id)
    
    # Add evidence
    evidence_tracker = EvidenceTracker()
    evidence_id = evidence_tracker.add_evidence(state, evidence_data)
    
    # Save state
    await state_manager.save_state(state)
    
    return jsonify({
        'evidence_id': evidence_id,
        'added': True,
        'investigation_id': investigation_id,
        'coverage_score': evidence_tracker.calculate_coverage_score(state)
    })
```

**Impact:** NEW - No breaking changes, additive feature

---

### 3. GET /investigation/{investigation_id} (ENHANCED)

**Current Implementation:**
```python
@app.route('/session/<session_id>', methods=['GET'])
def get_session(session_id):
    """
    Simple session retrieval
    
    Response:
    {
        "session_id": "abc123",
        "messages": [
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."}
        ]
    }
    """
    session = session_store.get(session_id)
    return jsonify(session)
```

**New Implementation:**
```python
@app.route('/investigation/<investigation_id>', methods=['GET'])
async def get_investigation(investigation_id):
    """
    Retrieve complete investigation state
    
    Response:
    {
        "investigation_id": "inv-abc123",
        "created_at": "2025-10-09T14:20:00Z",
        "updated_at": "2025-10-09T15:30:00Z",
        "status": "in_progress",
        "metadata": {
            "agent_mode": "investigator",
            "current_phase": 4,
            "phase_name": "Root Cause Analysis",
            "ooda_step": "test",
            "ooda_iteration": 2,
            "urgency_level": "high",
            "investigation_mode": "active_incident"
        },
        "problem_statement": "API returning 500 errors",
        "anomaly_frame": {
            "statement": "API timing out connecting to database",
            "confidence": 0.85,
            "severity": "critical",
            "affected_components": ["api-service", "database"]
        },
        "evidence_summary": {
            "total_count": 5,
            "by_category": {
                "symptoms": 2,
                "timeline": 1,
                "scope": 2
            },
            "coverage_score": 0.85
        },
        "hypotheses_summary": {
            "total_count": 3,
            "active_count": 2,
            "top_hypothesis": {
                "statement": "Connection leak in v2.3.1",
                "likelihood": 0.85,
                "tested": false
            }
        },
        "root_cause": null,
        "solution": null,
        "conversation_history": [
            {
                "timestamp": "2025-10-09T14:20:00Z",
                "role": "user",
                "content": "API is down!"
            },
            {
                "timestamp": "2025-10-09T14:20:30Z",
                "role": "assistant",
                "content": "Let me help you troubleshoot..."
            }
        ],
        "escalation": null
    }
    """
    
    # Load state
    state = await state_manager.load_state(investigation_id)
    
    # Format for API response
    return jsonify(format_investigation_response(state))
```

**Breaking Changes:**
- ⚠️ Endpoint path changed: `/session/{id}` → `/investigation/{id}`
- ⚠️ Response structure completely different
- ⚠️ Much more detailed information

**Migration Path:**
- Keep `/session/{id}` as alias to `/investigation/{id}` with v1.0 format
- Add `/investigation/{id}` for full v2.0 response

---

### 4. POST /investigation/{investigation_id}/transition (NEW)

**Purpose:** Allow explicit phase transitions

```python
@app.route('/investigation/<investigation_id>/transition', methods=['POST'])
async def transition_phase(investigation_id):
    """
    Trigger phase transition
    
    Request:
    {
        "target_phase": 3,
        "reason": "User confirmed ready to mitigate"
    }
    
    Response:
    {
        "success": true,
        "investigation_id": "inv-abc123",
        "previous_phase": 2,
        "current_phase": 3,
        "phase_name": "Mitigation",
        "message": "Transitioned to Mitigation phase"
    }
    """
    
    target_phase = request.json['target_phase']
    
    # Load state
    state = await state_manager.load_state(investigation_id)
    
    # Validate and execute transition
    phase_engine = PhaseTransitionEngine()
    can_transition, reason = phase_engine.can_transition(state, target_phase)
    
    if not can_transition:
        return jsonify({
            'success': False,
            'error': reason
        }), 400
    
    # Execute transition
    previous_phase = state.current_phase
    state = phase_engine.transition(state, target_phase)
    await state_manager.save_state(state)
    
    return jsonify({
        'success': True,
        'investigation_id': investigation_id,
        'previous_phase': previous_phase,
        'current_phase': state.current_phase,
        'phase_name': phase_engine.get_phase_name(state.current_phase)
    })
```

**Impact:** NEW - Additive feature

---

### 5. GET /investigation/{investigation_id}/export (NEW)

**Purpose:** Export investigation for post-mortem or handoff

```python
@app.route('/investigation/<investigation_id>/export', methods=['GET'])
async def export_investigation(investigation_id):
    """
    Export investigation in various formats
    
    Query Params:
    - format: json|markdown|pdf (default: json)
    
    Response (markdown):
    # Post-Mortem: API Timeout Incident
    
    **Date:** 2025-10-09
    **Duration:** 14:20 - 15:30 UTC (70 minutes)
    **Severity:** Critical
    
    ## Summary
    ...
    
    ## Timeline
    ...
    
    ## Root Cause
    ...
    """
    
    format_type = request.args.get('format', 'json')
    
    # Load state
    state = await state_manager.load_state(investigation_id)
    
    if format_type == 'markdown':
        # Generate markdown post-mortem
        doc = generate_postmortem_markdown(state)
        return Response(doc, mimetype='text/markdown')
    
    elif format_type == 'json':
        return jsonify(state.__dict__)
    
    elif format_type == 'pdf':
        # Generate PDF (requires additional library)
        pdf = generate_postmortem_pdf(state)
        return send_file(pdf, mimetype='application/pdf')
```

**Impact:** NEW - Additive feature

---

### 6. DELETE /investigation/{investigation_id} (ENHANCED)

**Current:** Simple session deletion

**New Implementation:**
```python
@app.route('/investigation/<investigation_id>', methods=['DELETE'])
async def delete_investigation(investigation_id):
    """
    Delete investigation and all associated data
    
    Query Params:
    - archive: true|false (default: false)
    
    Response:
    {
        "deleted": true,
        "investigation_id": "inv-abc123",
        "archived": false
    }
    """
    
    should_archive = request.args.get('archive', 'false').lower() == 'true'
    
    # Load state
    state = await state_manager.load_state(investigation_id)
    
    if should_archive:
        # Archive to cold storage
        await archive_investigation(state)
    
    # Delete from active storage
    await state_manager.delete_state(investigation_id)
    
    return jsonify({
        'deleted': True,
        'investigation_id': investigation_id,
        'archived': should_archive
    })
```

**Breaking Changes:** None (backward compatible)

---

### 7. WebSocket /ws/investigation/{investigation_id} (NEW)

**Purpose:** Real-time streaming for long-running investigations

```python
@socketio.on('connect')
async def handle_connect(investigation_id):
    """
    WebSocket connection for real-time updates
    """
    join_room(investigation_id)
    emit('connected', {'investigation_id': investigation_id})

@socketio.on('query')
async def handle_query(data):
    """
    Handle query via WebSocket with streaming
    
    Client sends:
    {
        "query": "What's wrong?",
        "investigation_id": "inv-abc123"
    }
    
    Server emits multiple events:
    1. { "type": "thinking", "message": "Analyzing evidence..." }
    2. { "type": "evidence_request", "data": {...} }
    3. { "type": "hypothesis_generated", "data": {...} }
    4. { "type": "response_chunk", "chunk": "Based on..." }
    5. { "type": "response_complete", "data": {...} }
    """
    
    investigation_id = data['investigation_id']
    query = data['query']
    
    # Stream processing
    agent = FaultMavenAgent()
    
    async for event in agent.process_message_streaming(query, investigation_id):
        emit('investigation_update', event, room=investigation_id)

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnect"""
    pass
```

**Impact:** NEW - Optional, for enhanced UX

---

## API Response Format Changes

### Response Structure Evolution

**v1.0 Response:**
```json
{
  "response": "Here is the answer to your question...",
  "session_id": "abc123"
}
```

**v2.0 Response:**
```json
{
  "answer": "Here is the answer to your question...",
  "investigation_id": "inv-abc123",
  "metadata": {
    "agent_mode": "investigator",
    "current_phase": 2,
    "phase_name": "Triage",
    "ooda_step": "branch",
    "urgency_level": "high",
    "investigation_mode": "active_incident",
    "root_cause_identified": false
  },
  "evidence_requests": [
    {
      "evidence_id": "ev-001",
      "label": "Database connection metrics",
      "description": "Need to verify connection pool exhaustion",
      "category": "infrastructure",
      "guidance": {
        "commands": ["kubectl exec db-proxy -- mysql -e 'SHOW PROCESSLIST;'"],
        "file_locations": ["/var/log/mysql/slow-query.log"],
        "ui_locations": ["Grafana > Database Dashboard"],
        "alternatives": ["If kubectl unavailable, check CloudWatch metrics"]
      }
    }
  ],
  "hypotheses": [
    {
      "hypothesis_id": "hyp-001",
      "statement": "Connection leak in database query builder",
      "category": "code",
      "likelihood": 0.85,
      "supporting_evidence": ["ev-002", "ev-005"],
      "tested": false
    }
  ],
  "phase_transition_prompt": {
    "current_phase": 2,
    "suggested_next_phase": 3,
    "reason": "High urgency + actionable hypothesis",
    "user_confirmation_required": true,
    "prompt": "Would you like to proceed to Mitigation to restore service?"
  },
  "escalation": null
}
```

---

## Header Changes

### New Headers

**Request Headers:**
```
X-Investigation-ID: inv-abc123     (replaces X-Session-ID)
X-API-Version: 2.0                 (for version negotiation)
X-Client-Type: browser-extension   (optional, for telemetry)
```

**Response Headers:**
```
X-Investigation-ID: inv-abc123
X-Current-Phase: 2
X-Phase-Name: Triage
X-OODA-Step: branch
X-Urgency-Level: high
X-RateLimit-Remaining: 95