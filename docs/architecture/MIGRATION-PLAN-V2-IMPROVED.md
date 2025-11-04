# Migration Plan v2.0: Test-Driven Clean Replacement Strategy

**Date**: 2025-11-04
**Strategy**: Clean replacement with test-driven development and parallel agent collaboration
**Philosophy**: Build modules with zero dependencies first, test immediately, move up dependency chain

---

## Executive Summary

### Approach: Test-Driven Dependency-First Migration

**Key Principles**:
1. ✅ **Zero backward compatibility** (no production users/data)
2. ✅ **Test immediately** after creating each module
3. ✅ **Dependency order** (build foundation first, then dependent modules)
4. ✅ **Delete old tests** when deleting old modules
5. ✅ **Parallel agent collaboration** for independent modules

**Timeline**: 1-2 weeks with parallel agent execution

---

## 1. Dependency Analysis & Build Order

### Dependency Layers (Bottom-Up)

```
Layer 1 (Zero Dependencies) - BUILD FIRST
├── models/case.py (NEW v2.0 models)
├── models/llm_schemas.py (NEW response schemas)
└── utils/ (keep existing, no changes)

Layer 2 (Depends on Layer 1)
├── services/agentic/prompts/templates.py (depends on: case.py)
├── services/agentic/prompts/builder.py (depends on: case.py, templates.py)
└── services/agentic/management/state_manager.py (UPDATE, depends on: case.py)

Layer 3 (Depends on Layer 1-2)
├── services/agentic/processors/consulting_processor.py (depends on: case.py, llm_schemas.py, state_manager.py)
├── services/agentic/processors/investigating_processor.py (depends on: case.py, llm_schemas.py, state_manager.py)
└── services/agentic/processors/terminal_processor.py (depends on: case.py, llm_schemas.py, state_manager.py)

Layer 4 (Depends on Layer 1-3)
└── services/agentic/orchestration/milestone_engine.py (depends on: processors, prompts, state_manager)

Layer 5 (Top Level)
└── services/agentic/orchestration/agent_service.py (UPDATE, depends on: milestone_engine)
```

**Build Order**: Layer 1 → Layer 2 → Layer 3 → Layer 4 → Layer 5

---

## 2. Test-Driven Module Creation Strategy

### For Each New Module

**Step 1: Create Module**
```python
# Create the module file
touch faultmaven/services/agentic/prompts/templates.py
# Write implementation
```

**Step 2: Create Test File IMMEDIATELY**
```python
# Create test file
touch tests/unit/services/agentic/prompts/test_templates.py
# Write tests covering key functionality
```

**Step 3: Run Tests BEFORE Moving On**
```bash
# Run only the new tests
pytest tests/unit/services/agentic/prompts/test_templates.py -v

# Must see: 100% pass rate
# Only then proceed to next module
```

**Step 4: Integration Check**
```bash
# After each layer complete, run ALL tests
pytest tests/ -v --tb=short
```

### For Each Deleted Module

**Step 1: Identify Old Tests**
```bash
# Find tests for module being deleted
grep -r "from.*phase_orchestrator" tests/
```

**Step 2: Delete Old Tests**
```bash
# Delete test file
rm tests/unit/services/agentic/test_phase_orchestrator.py
```

**Step 3: Delete Old Module**
```bash
# Delete implementation
rm faultmaven/services/agentic/orchestration/phase_orchestrator.py
```

**Step 4: Verify No Broken Imports**
```bash
# Check for remaining imports
grep -r "phase_orchestrator" faultmaven/
grep -r "phase_orchestrator" tests/
# Should return: No matches
```

---

## 3. Parallel Agent Collaboration Plan

### Agent Assignments by Layer

#### **Agent 1: Foundation Agent** (Layer 1 - Critical Path)
**Role**: Build core data models (blocking work)

**Tasks**:
1. Replace `models/case.py` with v2.0 models
2. Create `models/llm_schemas.py`
3. Update database schema
4. Write comprehensive model tests

**Deliverables**:
- ✅ `faultmaven/models/case.py` (v2.0)
- ✅ `faultmaven/models/llm_schemas.py` (NEW)
- ✅ `tests/models/test_case_models_v2.py` (NEW)
- ✅ `tests/models/test_llm_schemas.py` (NEW)
- ✅ Database schema v2.0 applied

**Blocking**: All other agents (must complete first)

---

#### **Agent 2: Prompts Specialist** (Layer 2 - Parallel after Agent 1)
**Role**: Build prompt templates and builders

**Dependencies**: Wait for Agent 1 (models/case.py)

**Tasks**:
1. Create `prompts/templates.py` (3 templates from prompt-templates.md)
2. Create `prompts/builder.py` (prompt builder)
3. Write prompt generation tests
4. Test all 3 status templates

**Deliverables**:
- ✅ `faultmaven/services/agentic/prompts/templates.py`
- ✅ `faultmaven/services/agentic/prompts/builder.py`
- ✅ `tests/unit/prompts/test_templates.py`
- ✅ `tests/unit/prompts/test_builder.py`

**Parallel with**: Agent 3 (State Manager)

---

#### **Agent 3: State Management Specialist** (Layer 2 - Parallel after Agent 1)
**Role**: Update state manager for v2.0

**Dependencies**: Wait for Agent 1 (models/case.py)

**Tasks**:
1. Update `state_manager.py` for v2.0 case model
2. Add `determine_investigation_path()` method
3. Add `check_automatic_status_transitions()` method
4. Write state manager tests

**Deliverables**:
- ✅ `faultmaven/services/agentic/management/state_manager.py` (UPDATED)
- ✅ `tests/unit/management/test_state_manager_v2.py` (NEW)
- ⚠️ Delete `tests/unit/management/test_state_manager.py` (OLD v1.0 tests)

**Parallel with**: Agent 2 (Prompts)

---

#### **Agent 4: Processors Specialist** (Layer 3 - Parallel after Agents 2&3)
**Role**: Build response processors

**Dependencies**: Wait for Agent 2 (prompts) AND Agent 3 (state_manager)

**Tasks**:
1. Create `processors/consulting_processor.py`
2. Create `processors/investigating_processor.py`
3. Create `processors/terminal_processor.py`
4. Write processor tests (include mention_count logic!)

**Deliverables**:
- ✅ `faultmaven/services/agentic/processors/consulting_processor.py`
- ✅ `faultmaven/services/agentic/processors/investigating_processor.py`
- ✅ `faultmaven/services/agentic/processors/terminal_processor.py`
- ✅ `tests/unit/processors/test_consulting_processor.py`
- ✅ `tests/unit/processors/test_investigating_processor.py`
- ✅ `tests/unit/processors/test_terminal_processor.py`

**Critical Tests**:
- Test milestone completion detection
- Test `mentioned_request_ids` → `mention_count` increment
- Test evidence category inference
- Test status transitions

---

#### **Agent 5: Cleanup Agent** (Layer 2-5 - Continuous)
**Role**: Delete obsolete v1.0 code and tests

**Dependencies**: Can start after Agent 1 completes

**Tasks**:
1. Delete old OODA/phase modules AND their tests
2. Delete old prompt files AND their tests
3. Update imports throughout codebase
4. Verify no broken references

**Deletions**:
```bash
# Modules to delete
rm -rf faultmaven/core/investigation/ooda_engine.py
rm -rf faultmaven/core/investigation/phases.py
rm -rf faultmaven/core/investigation/phase_loopback.py
rm -rf faultmaven/core/investigation/iteration_strategy.py
rm -rf faultmaven/core/investigation/ooda_step_extraction.py
rm -rf faultmaven/services/agentic/orchestration/phase_orchestrator.py
rm -rf faultmaven/services/agentic/orchestration/phase_handlers/

# Tests to delete
rm -rf tests/core/investigation/test_ooda_engine.py
rm -rf tests/core/investigation/test_phases.py
rm -rf tests/core/investigation/test_phase_loopback.py
rm -rf tests/services/agentic/test_phase_orchestrator.py
rm -rf tests/services/agentic/phase_handlers/
```

**Verification**:
```bash
# No remaining imports
grep -r "ooda_engine\|phase_orchestrator\|phase_loopback" faultmaven/
grep -r "ooda_engine\|phase_orchestrator\|phase_loopback" tests/
# Should return: No matches
```

**Parallel with**: All other agents (non-blocking)

---

#### **Agent 6: Integration Agent** (Layer 4-5 - After all others)
**Role**: Wire everything together and final testing

**Dependencies**: Wait for Agents 2, 3, 4

**Tasks**:
1. Create `orchestration/milestone_engine.py`
2. Update `orchestration/agent_service.py`
3. Write integration tests
4. Run full test suite
5. Test end-to-end API flows

**Deliverables**:
- ✅ `faultmaven/services/agentic/orchestration/milestone_engine.py`
- ✅ `faultmaven/services/agentic/orchestration/agent_service.py` (UPDATED)
- ✅ `tests/integration/test_milestone_engine.py`
- ✅ `tests/integration/test_end_to_end_v2.py`
- ✅ Full test suite passing

**Critical Tests**:
- One-turn resolution test
- Multi-turn investigation test
- Status transition flows
- Error handling

---

## 4. Detailed Implementation Steps (Test-Driven)

### Layer 1: Foundation (Agent 1) - DAY 1-2

#### **Task 1.1: Replace Models**

**Implementation**:
```bash
# Backup old models
cp faultmaven/models/case.py faultmaven/models/case_v1_backup.py

# Replace with v2.0 models
# Copy all code from case-data-model-design.md sections 1-12 to case.py
```

**Testing IMMEDIATELY**:
```python
# tests/models/test_case_models_v2.py

import pytest
from faultmaven.models.case import (
    Case, CaseStatus, InvestigationProgress,
    InvestigationStage, TurnProgress, PathSelection
)

def test_case_creation():
    """Test basic case creation"""
    case = Case(
        user_id="test_user",
        organization_id="test_org",
        title="API Error"
    )
    assert case.status == CaseStatus.CONSULTING
    assert case.progress.symptom_verified == False
    assert case.current_turn == 0

def test_milestone_completion():
    """Test milestone tracking"""
    case = Case(user_id="u1", organization_id="org1", title="Test")

    # Complete some milestones
    case.progress.symptom_verified = True
    case.progress.scope_assessed = True

    assert case.progress.symptom_verified == True
    assert case.progress.root_cause_identified == False

def test_status_enum():
    """Test CaseStatus has exactly 4 values"""
    statuses = list(CaseStatus)
    assert len(statuses) == 4
    assert CaseStatus.CONSULTING in statuses
    assert CaseStatus.INVESTIGATING in statuses
    assert CaseStatus.RESOLVED in statuses
    assert CaseStatus.CLOSED in statuses

def test_current_stage_computed():
    """Test stage is computed from milestones"""
    case = Case(user_id="u1", organization_id="org1", title="Test")
    case.status = CaseStatus.INVESTIGATING

    # Understanding stage (no milestones)
    assert case.progress.current_stage == InvestigationStage.UNDERSTANDING

    # Diagnosing stage (symptom verified)
    case.progress.symptom_verified = True
    assert case.progress.current_stage == InvestigationStage.DIAGNOSING

    # Resolving stage (solution proposed)
    case.progress.solution_proposed = True
    assert case.progress.current_stage == InvestigationStage.RESOLVING

def test_turn_progress_tracking():
    """Test turn history tracking"""
    turn = TurnProgress(
        turn_number=1,
        milestones_completed=["symptom_verified"],
        progress_made=True,
        outcome="milestone_completed"
    )
    assert turn.turn_number == 1
    assert "symptom_verified" in turn.milestones_completed

# Run tests
# pytest tests/models/test_case_models_v2.py -v
```

**Success Criteria**:
```bash
pytest tests/models/test_case_models_v2.py -v
# Must show: 100% pass rate
# Only then proceed to Task 1.2
```

---

#### **Task 1.2: Create LLM Schemas**

**Implementation**:
```bash
# Create new file
touch faultmaven/models/llm_schemas.py

# Copy schemas from prompt-implementation-examples.md Section 3
# CRITICAL: Include mentioned_request_ids field!
```

**Testing IMMEDIATELY**:
```python
# tests/models/test_llm_schemas.py

import pytest
from faultmaven.models.llm_schemas import (
    InvestigationResponse, InvestigationStateUpdate,
    ConsultingResponse, TerminalResponse,
    MilestoneUpdates, EvidenceToAdd
)

def test_investigation_response_schema():
    """Test InvestigationResponse structure"""
    response = InvestigationResponse(
        agent_response="Testing API error...",
        state_update=InvestigationStateUpdate(
            milestones=MilestoneUpdates(),
            mentioned_request_ids=["req_123", "req_456"]
        )
    )
    assert response.agent_response == "Testing API error..."
    assert "req_123" in response.state_update.mentioned_request_ids

def test_mentioned_request_ids_required():
    """Test mentioned_request_ids field exists and defaults to empty list"""
    state_update = InvestigationStateUpdate(
        milestones=MilestoneUpdates()
    )
    assert hasattr(state_update, 'mentioned_request_ids')
    assert state_update.mentioned_request_ids == []

def test_milestone_updates_structure():
    """Test MilestoneUpdates schema"""
    milestones = MilestoneUpdates(
        symptom_verified=True,
        root_cause_identified=True
    )
    assert milestones.symptom_verified == True
    assert milestones.root_cause_identified == True

def test_evidence_to_add():
    """Test evidence addition schema"""
    evidence = EvidenceToAdd(
        raw_content="Error: Connection timeout",
        source="user_message",
        form="text"
    )
    assert evidence.raw_content == "Error: Connection timeout"
    # Category should be inferred by processor, not specified by LLM

# Run tests
# pytest tests/models/test_llm_schemas.py -v
```

**Success Criteria**:
```bash
pytest tests/models/test_llm_schemas.py -v
# Must show: 100% pass rate
```

---

#### **Task 1.3: Database Schema**

**Implementation**:
```bash
# Backup database
pg_dump faultmaven_dev > backup_v1_$(date +%Y%m%d).sql

# Drop old schema (safe - no production data)
psql -d faultmaven_dev -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

# Apply v2.0 schema
# Copy SQL from db-design-specifications.md
psql -d faultmaven_dev -f schema_v2.sql
```

**Testing IMMEDIATELY**:
```sql
-- Test schema structure
\dt  -- Should show v2.0 tables

-- Verify key tables exist
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('cases', 'turn_history', 'investigation_progress');
-- Should return: 3 rows

-- Verify status constraint (4 values only)
SELECT constraint_name, check_clause
FROM information_schema.check_constraints
WHERE constraint_name LIKE '%status%';
-- Should show: status IN ('consulting', 'investigating', 'resolved', 'closed')

-- Test basic operations
INSERT INTO cases (user_id, organization_id, title, status)
VALUES ('test_user', 'test_org', 'Test Case', 'consulting')
RETURNING case_id;
-- Should succeed
```

**Success Criteria**: All schema tests pass

---

### Layer 2: Prompts & State Manager (Agents 2 & 3) - DAY 2-3

#### **Task 2.1: Prompt Templates** (Agent 2)

**Implementation**:
```bash
mkdir -p faultmaven/services/agentic/prompts

# Copy from prompt-templates.md
# - build_consulting_prompt()
# - build_investigating_prompt()
# - build_terminal_prompt()
```

**Testing IMMEDIATELY**:
```python
# tests/unit/prompts/test_templates.py

import pytest
from faultmaven.models.case import Case, CaseStatus
from faultmaven.services.agentic.prompts.templates import (
    build_consulting_prompt,
    build_investigating_prompt,
    build_terminal_prompt
)

def test_consulting_prompt_generation():
    """Test CONSULTING prompt generates correctly"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="API Error",
        status=CaseStatus.CONSULTING
    )

    prompt = build_consulting_prompt(case, "API is slow")

    assert "CONSULTING" in prompt
    assert "API is slow" in prompt
    assert "ConsultingResponse" in prompt  # Response schema mentioned

def test_investigating_prompt_adapts_to_stage():
    """Test INVESTIGATING prompt adapts based on completed milestones"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="API Error",
        status=CaseStatus.INVESTIGATING
    )

    # Understanding stage
    prompt1 = build_investigating_prompt(case, "Here are the logs")
    assert "symptom_verified" in prompt1

    # Diagnosing stage (after symptom verified)
    case.progress.symptom_verified = True
    prompt2 = build_investigating_prompt(case, "More details")
    assert "root_cause_identified" in prompt2

def test_terminal_prompt():
    """Test RESOLVED/CLOSED prompt"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="API Error",
        status=CaseStatus.RESOLVED
    )

    prompt = build_terminal_prompt(case, "Generate report")
    assert "documentation" in prompt.lower()

def test_mentioned_request_ids_instructions():
    """Test prompts include mentioned_request_ids instructions"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    prompt = build_investigating_prompt(case, "Test")
    assert "mentioned_request_ids" in prompt

# Run tests
# pytest tests/unit/prompts/test_templates.py -v
```

**Success Criteria**: 100% pass rate before moving to Task 2.2

---

#### **Task 2.2: State Manager Updates** (Agent 3 - PARALLEL)

**Implementation**:
```python
# faultmaven/services/agentic/management/state_manager.py

# UPDATE existing methods for v2.0
class StateManager:
    async def get_case(self, case_id: str) -> Case:
        """Get case by ID (replaces get_investigation_state)"""
        case_data = await self.db.get_case(case_id)
        return Case(**case_data)

    async def save_case(self, case: Case):
        """Save complete case state"""
        await self.db.save_case(case.dict())

    def determine_investigation_path(
        self,
        problem_verification: ProblemVerification
    ) -> PathSelection:
        """Determine investigation path from matrix (NEW)"""
        # Logic from milestone-based-investigation-framework.md Section 4.2
        # temporal_state × urgency_level → path
        pass

    def check_automatic_status_transitions(self, case: Case) -> Case:
        """Check if case should transition status (NEW)"""
        # CONSULTING → INVESTIGATING: decided_to_investigate = True
        # INVESTIGATING → RESOLVED: solution_verified = True
        # etc.
        pass
```

**Testing IMMEDIATELY**:
```python
# tests/unit/management/test_state_manager_v2.py

import pytest
from faultmaven.services.agentic.management.state_manager import StateManager
from faultmaven.models.case import Case, CaseStatus, ProblemVerification

@pytest.fixture
async def state_manager():
    # Setup mock database
    return StateManager(db=mock_db)

async def test_get_case(state_manager):
    """Test case retrieval"""
    case = await state_manager.get_case("case_123")
    assert isinstance(case, Case)
    assert case.case_id == "case_123"

async def test_save_case(state_manager):
    """Test case persistence"""
    case = Case(user_id="u1", organization_id="org1", title="Test")
    await state_manager.save_case(case)
    # Verify saved to database

def test_determine_investigation_path(state_manager):
    """Test path selection matrix"""
    verification = ProblemVerification(
        temporal_state="ongoing",
        urgency_level="high"
    )

    path = state_manager.determine_investigation_path(verification)
    assert path.path == "mitigation"  # ongoing + high = mitigation

def test_automatic_status_transitions(state_manager):
    """Test status transitions"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.CONSULTING
    )
    case.consulting.decided_to_investigate = True

    updated_case = state_manager.check_automatic_status_transitions(case)
    assert updated_case.status == CaseStatus.INVESTIGATING

# Delete OLD v1.0 tests
# rm tests/unit/management/test_state_manager.py (old phase-based tests)

# Run NEW tests
# pytest tests/unit/management/test_state_manager_v2.py -v
```

**Success Criteria**: 100% pass rate

---

### Layer 3: Processors (Agent 4) - DAY 3-4

#### **Task 3.1: Investigating Processor** (Most Complex)

**Implementation**:
```bash
mkdir -p faultmaven/services/agentic/processors

# Copy from prompt-implementation-examples.md Section 4
# ~400 lines - core logic
```

**Testing IMMEDIATELY** (Most Critical):
```python
# tests/unit/processors/test_investigating_processor.py

import pytest
from faultmaven.services.agentic.processors.investigating_processor import InvestigatingProcessor
from faultmaven.models.case import Case, CaseStatus
from faultmaven.models.llm_schemas import InvestigationResponse, InvestigationStateUpdate

@pytest.fixture
def processor():
    return InvestigatingProcessor(state_manager=mock_state_manager)

async def test_milestone_completion(processor):
    """Test milestone updates applied correctly"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    llm_response = InvestigationResponse(
        agent_response="I've verified the symptom...",
        state_update=InvestigationStateUpdate(
            milestones=MilestoneUpdates(
                symptom_verified=True,
                scope_assessed=True
            )
        )
    )

    updated_case, metadata = await processor.process(
        case=case,
        user_message="Here are the logs",
        llm_response=llm_response
    )

    assert updated_case.progress.symptom_verified == True
    assert updated_case.progress.scope_assessed == True

async def test_mentioned_request_ids_increment(processor):
    """Test mention_count increments when request mentioned"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    # Add evidence request
    req = EvidenceRequest(
        request_id="req_123",
        category="SYMPTOM",
        mention_count=0
    )
    case.evidence_requests.append(req)

    # LLM mentions the request
    llm_response = InvestigationResponse(
        agent_response="Can you provide the logs (req_123)?",
        state_update=InvestigationStateUpdate(
            mentioned_request_ids=["req_123"]
        )
    )

    updated_case, _ = await processor.process(case, "...", llm_response)

    # Find request and check mention_count
    req_after = next(r for r in updated_case.evidence_requests if r.request_id == "req_123")
    assert req_after.mention_count == 1

async def test_evidence_category_inference(processor):
    """Test evidence category inferred from context"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    # LLM adds evidence without category
    llm_response = InvestigationResponse(
        agent_response="...",
        state_update=InvestigationStateUpdate(
            evidence_to_add=[
                EvidenceToAdd(
                    raw_content="Error: Connection timeout",
                    source="user_message",
                    form="text"
                    # NO category - processor infers it
                )
            ]
        )
    )

    updated_case, _ = await processor.process(case, "...", llm_response)

    # Processor should infer SYMPTOM category
    added_evidence = updated_case.evidence[-1]
    assert added_evidence.category == "SYMPTOM"

async def test_progress_detection(processor):
    """Test progress detection"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING,
        turns_without_progress=2
    )

    # Milestone completed = progress made
    llm_response = InvestigationResponse(
        agent_response="...",
        state_update=InvestigationStateUpdate(
            milestones=MilestoneUpdates(root_cause_identified=True)
        )
    )

    updated_case, metadata = await processor.process(case, "...", llm_response)

    assert metadata.progress_made == True
    assert updated_case.turns_without_progress == 0  # Reset on progress

# Run tests
# pytest tests/unit/processors/test_investigating_processor.py -v
```

**Success Criteria**: 100% pass rate

---

### Layer 4: Integration (Agent 6) - DAY 5-6

#### **Task 4.1: Milestone Engine**

**Implementation**:
```python
# faultmaven/services/agentic/orchestration/milestone_engine.py

# Code from V2_IMPLEMENTATION_PLAN.md Section 2.3
# ~100 lines
```

**Testing IMMEDIATELY**:
```python
# tests/integration/test_milestone_engine.py

import pytest
from faultmaven.services.agentic.orchestration.milestone_engine import MilestoneInvestigationEngine

@pytest.fixture
async def engine():
    return MilestoneInvestigationEngine(
        llm_provider=mock_llm,
        state_manager=mock_state_manager,
        logger=mock_logger
    )

async def test_process_turn_basic(engine):
    """Test basic turn processing"""
    case = create_test_case(status=CaseStatus.INVESTIGATING)

    result = await engine.process_turn(
        case=case,
        user_message="API is returning 500 errors",
        attachments=None
    )

    assert 'response' in result
    assert 'case' in result
    assert result['case'].current_turn == case.current_turn + 1

async def test_one_turn_resolution(engine):
    """Test complete investigation in one turn"""
    case = create_test_case(status=CaseStatus.INVESTIGATING)

    # User provides comprehensive diagnostic data
    result = await engine.process_turn(
        case=case,
        user_message="Here's the error log with stack trace and timeline...",
        attachments=["comprehensive_log.txt"]
    )

    updated_case = result['case']

    # Agent should complete ALL milestones in one turn
    assert updated_case.progress.symptom_verified == True
    assert updated_case.progress.timeline_established == True
    assert updated_case.progress.root_cause_identified == True
    assert updated_case.progress.solution_proposed == True

# Run tests
# pytest tests/integration/test_milestone_engine.py -v
```

**Success Criteria**: Integration tests pass

---

## 5. Parallel Execution Timeline

### Week 1: Foundation & Core

**Days 1-2: Sequential (Agent 1 blocks others)**
```
Day 1-2: Agent 1 (Foundation)
├── Replace models/case.py
├── Create models/llm_schemas.py
├── Update database schema
└── Write & pass all model tests ✓
```

**Days 2-3: Parallel (Agents 2, 3, 5)**
```
Day 2-3: Agent 2 (Prompts)        Day 2-3: Agent 3 (State Manager)    Day 2-3: Agent 5 (Cleanup)
├── Create templates.py           ├── Update state_manager.py          ├── Delete OODA modules
├── Create builder.py             ├── Add path selection method         ├── Delete old tests
├── Write tests                   ├── Write tests                      └── Verify no broken imports
└── Pass tests ✓                  └── Pass tests ✓
```

**Days 3-4: Agent 4 (Processors) - After Agents 2&3**
```
Day 3-4: Agent 4 (Processors)
├── Create consulting_processor.py
├── Create investigating_processor.py
├── Create terminal_processor.py
├── Write comprehensive tests
└── Pass all tests ✓
```

**Days 5-6: Agent 6 (Integration) - After Agent 4**
```
Day 5-6: Agent 6 (Integration)
├── Create milestone_engine.py
├── Update agent_service.py
├── Write integration tests
├── Run full test suite
└── Pass all tests ✓
```

---

## 6. Testing Strategy per Layer

### Layer 1 Tests (Models)
```bash
# Must pass before ANY other work
pytest tests/models/test_case_models_v2.py -v
pytest tests/models/test_llm_schemas.py -v
```

### Layer 2 Tests (Prompts & State)
```bash
# Must pass before Layer 3
pytest tests/unit/prompts/ -v
pytest tests/unit/management/test_state_manager_v2.py -v
```

### Layer 3 Tests (Processors)
```bash
# Most critical - test mention_count, evidence inference, milestones
pytest tests/unit/processors/ -v --cov=faultmaven/services/agentic/processors
```

### Layer 4 Tests (Integration)
```bash
# End-to-end flows
pytest tests/integration/test_milestone_engine.py -v
pytest tests/integration/test_end_to_end_v2.py -v
```

### Full Suite
```bash
# After Layer 4 complete
pytest tests/ -v --cov --tb=short
# Target: 80%+ coverage, 100% pass rate
```

---

## 7. Cleanup Checklist (Agent 5)

### Files to Delete
```bash
# OODA/Phase modules
rm faultmaven/core/investigation/ooda_engine.py
rm faultmaven/core/investigation/phases.py
rm faultmaven/core/investigation/phase_loopback.py
rm faultmaven/core/investigation/iteration_strategy.py
rm faultmaven/core/investigation/ooda_step_extraction.py

# Phase orchestration
rm faultmaven/services/agentic/orchestration/phase_orchestrator.py
rm -rf faultmaven/services/agentic/orchestration/phase_handlers/

# Old prompts
rm -rf faultmaven/services/agentic/prompts/phase_prompts/
```

### Tests to Delete
```bash
# Tests for deleted modules
rm tests/core/investigation/test_ooda_engine.py
rm tests/core/investigation/test_phases.py
rm tests/core/investigation/test_phase_loopback.py
rm tests/services/agentic/test_phase_orchestrator.py
rm -rf tests/services/agentic/phase_handlers/

# Old state manager tests (replace with v2)
rm tests/unit/management/test_state_manager.py
```

### Verification Commands
```bash
# No remaining imports to deleted modules
grep -r "ooda_engine\|phase_orchestrator\|phase_loopback\|iteration_strategy" faultmaven/
grep -r "ooda_engine\|phase_orchestrator\|phase_loopback" tests/

# Should return: No matches (if cleanup complete)
```

---

## 8. Success Criteria

### Per-Layer Criteria

**Layer 1 Complete When**:
- ✅ All model imports work
- ✅ Model tests pass 100%
- ✅ Database schema applied successfully
- ✅ Can create Case objects with v2.0 fields

**Layer 2 Complete When**:
- ✅ Prompts generate for all 3 statuses
- ✅ Prompt tests pass 100%
- ✅ State manager tests pass 100%
- ✅ Path selection matrix works

**Layer 3 Complete When**:
- ✅ All processor tests pass 100%
- ✅ Milestone detection works
- ✅ mention_count increments correctly
- ✅ Evidence category inference works

**Layer 4 Complete When**:
- ✅ Integration tests pass 100%
- ✅ One-turn resolution test passes
- ✅ End-to-end API flow works
- ✅ Full test suite passes

**Cleanup Complete When**:
- ✅ No old modules remain
- ✅ No old tests remain
- ✅ No broken imports
- ✅ grep returns no matches for deleted modules

---

## 9. Command Reference

### Quick Start Commands

```bash
# 1. Create feature branch
git checkout -b feature/v2-milestone-migration

# 2. Run tests after each layer
pytest tests/models/ -v                    # After Layer 1
pytest tests/unit/prompts/ -v              # After Layer 2 (prompts)
pytest tests/unit/management/ -v           # After Layer 2 (state)
pytest tests/unit/processors/ -v           # After Layer 3
pytest tests/integration/ -v               # After Layer 4

# 3. Full test suite (only after Layer 4)
pytest tests/ -v --cov --tb=short

# 4. Verify cleanup
grep -r "phase_orchestrator" faultmaven/ tests/
# Should return: No matches
```

---

## 10. Rollback Plan

### If Layer N Fails

**Option 1: Revert Layer N Only**
```bash
git checkout HEAD -- <files-changed-in-layer-N>
```

**Option 2: Revert Entire Branch**
```bash
git checkout main
git branch -D feature/v2-milestone-migration
```

**Option 3: Cherry-pick Successful Layers**
```bash
# If Layer 1-2 work but Layer 3 fails
git checkout main
git checkout feature/v2-milestone-migration -- faultmaven/models/
git checkout feature/v2-milestone-migration -- faultmaven/services/agentic/prompts/
# Continue with Layer 3 fix
```

---

## 11. Estimated Timeline

### With Parallel Agent Execution

| Phase | Agents | Duration | Deliverables |
|-------|--------|----------|--------------|
| **Foundation** | Agent 1 | 2 days | Models, schemas, database |
| **Core Layer** | Agents 2, 3, 5 | 1-2 days | Prompts, state manager, cleanup |
| **Processing** | Agent 4 | 1-2 days | Processors |
| **Integration** | Agent 6 | 1-2 days | Engine, agent service, tests |
| **TOTAL** | - | **5-8 days** | Complete v2.0 migration |

### Sequential Execution (Comparison)

| Phase | Duration |
|-------|----------|
| Foundation | 2 days |
| Prompts | 1 day |
| State Manager | 1 day |
| Processors | 2 days |
| Integration | 2 days |
| Cleanup | 1 day |
| **TOTAL** | **9 days** |

**Parallel Savings**: ~25-30% time reduction

---

## 12. Ready to Execute?

### Start Command

```bash
# I can begin immediately by dispatching parallel agents:

# Agent 1 (Foundation) - BLOCKS ALL OTHERS
# Start with: Replace models/case.py with v2.0

# Once Agent 1 completes Layer 1:
# Agent 2 (Prompts) + Agent 3 (State Manager) + Agent 5 (Cleanup) - PARALLEL

# Once Agents 2&3 complete:
# Agent 4 (Processors)

# Once Agent 4 completes:
# Agent 6 (Integration)

# Ready to begin?
```

---

**Summary**:
- ✅ Test-driven (every module tested before moving on)
- ✅ Dependency-ordered (zero-dependency modules first)
- ✅ Parallel execution (up to 3 agents working simultaneously)
- ✅ Clean deletion (old modules AND old tests removed together)
- ✅ Rollback-safe (layer-by-layer with git checkpoints)

**Timeline**: 5-8 days to complete v2.0 migration

**Ready to dispatch agents?**
