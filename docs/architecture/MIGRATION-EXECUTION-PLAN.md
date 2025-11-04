# Migration Execution Plan: OODA → Milestone-Based Investigation

**Date**: 2025-11-04
**Strategy**: Sequential test-driven migration with optimal context management
**Priority**: Code quality and context preservation over speed

---

## Core Principles

### 1. No Version Numbers in Code
```bash
# ❌ BAD
test_state_manager_v2.py
case_models_v2.py

# ✅ GOOD
test_state_manager.py
case_models.py

# Old files → Move to archive/
mv old_file.py archive/old_file.py
```

### 2. Preserve API Contract
```python
# ✅ KEEP: All API endpoints and contracts
# Frontend depends on these - DO NOT BREAK
faultmaven/api/v1/routes/agent.py  # PRESERVE
faultmaven/api/v1/routes/case.py   # PRESERVE
faultmaven/api/v1/routes/session.py # PRESERVE

# Update INTERNAL implementation only
# External API signatures remain unchanged
```

### 3. Database Access Pattern
```python
# ✅ ALWAYS use repository abstraction
from faultmaven.dependencies import get_case_repository

# ✅ NEVER import database drivers directly
# ❌ import sqlite3
# ❌ import psycopg2
# ❌ import asyncpg

# ✅ Accept repository via dependency injection
class MilestoneEngine:
    def __init__(
        self,
        case_repository: CaseRepository,  # ← DI pattern
        llm_provider: ILLMProvider,
        logger: Logger
    ):
        self.repo = case_repository  # ← Use abstraction
```

### 4. Context-Optimized Execution
**Sequential approach** - One agent at a time for:
- ✅ Maximum context window usage
- ✅ Easier debugging and rollback
- ✅ Clear progress tracking
- ✅ Reduced cognitive load

---

## Execution Strategy: Single-Agent Sequential

### Why Sequential?

**Context Management Benefits**:
1. **Full context window** available for each task
2. **Clear mental model** - one thing at a time
3. **Easier rollback** - atomic steps
4. **Better testing** - verify each layer completely before moving on

**Trade-off Accepted**:
- Slower execution (8-10 days vs 5-8 days)
- In exchange for: Higher quality, better context usage, easier debugging

---

## Phase-Based Execution (Context-Optimized)

### Phase 0: Pre-Migration Cleanup (1 day)

**Goal**: Organize existing code, create archive structure

**Tasks**:
```bash
# 1. Create archive structure
mkdir -p archive/ooda_framework
mkdir -p archive/tests

# 2. Move deprecated docs (keep for reference)
mv docs/architecture/investigation-phases-and-ooda-integration.md archive/
mv docs/architecture/phase-*.md archive/

# 3. Document API contract (what MUST NOT change)
# Create API_CONTRACT.md listing all endpoints
```

**Deliverables**:
- ✅ Archive structure created
- ✅ API contract documented
- ✅ Baseline test suite runs
- ✅ Git checkpoint created

**Testing**:
```bash
# Baseline - all existing tests should pass
pytest tests/ -v --tb=short

# Save results for comparison
pytest tests/ -v > baseline_test_results.txt
```

---

### Phase 1: Foundation Models (2 days)

**Goal**: Replace core data models with milestone-based versions

#### Step 1.1: Case Model Migration

**Implementation**:
```bash
# 1. Archive old model
mv faultmaven/models/case.py archive/models/case_ooda.py

# 2. Create new case.py (from case-data-model-design.md)
# - CaseStatus (4 states: CONSULTING/INVESTIGATING/RESOLVED/CLOSED)
# - InvestigationProgress (8 milestones)
# - TurnProgress
# - PathSelection
# - ProblemVerification
# - Complete Case model
```

**Testing IMMEDIATELY**:
```python
# tests/models/test_case_models.py (NO version number!)

import pytest
from faultmaven.models.case import (
    Case, CaseStatus, InvestigationProgress,
    InvestigationStage, TurnProgress, PathSelection
)

class TestCaseModel:
    """Test v2.0 case model (milestone-based)"""

    def test_case_creation_defaults(self):
        """Test case created with correct defaults"""
        case = Case(
            user_id="user_123",
            organization_id="org_456",
            title="API Error"
        )

        assert case.status == CaseStatus.CONSULTING
        assert case.current_turn == 0
        assert case.turns_without_progress == 0
        assert case.progress.symptom_verified == False
        assert case.progress.root_cause_identified == False

    def test_milestone_tracking(self):
        """Test milestone flags work independently"""
        case = Case(
            user_id="u1",
            organization_id="org1",
            title="Test"
        )

        # Can complete milestones in any order
        case.progress.root_cause_identified = True
        case.progress.solution_proposed = True

        assert case.progress.symptom_verified == False  # Still false
        assert case.progress.root_cause_identified == True

    def test_stage_computed_from_milestones(self):
        """Test stage is computed property, not stored"""
        case = Case(
            user_id="u1",
            organization_id="org1",
            title="Test",
            status=CaseStatus.INVESTIGATING
        )

        # Understanding stage (no milestones)
        assert case.progress.current_stage == InvestigationStage.UNDERSTANDING

        # Diagnosing stage
        case.progress.symptom_verified = True
        assert case.progress.current_stage == InvestigationStage.DIAGNOSING

        # Resolving stage
        case.progress.solution_proposed = True
        assert case.progress.current_stage == InvestigationStage.RESOLVING

    def test_status_enum_has_exactly_4_values(self):
        """Test CaseStatus has no OODA phase states"""
        statuses = list(CaseStatus)
        assert len(statuses) == 4
        assert CaseStatus.CONSULTING in statuses
        assert CaseStatus.INVESTIGATING in statuses
        assert CaseStatus.RESOLVED in statuses
        assert CaseStatus.CLOSED in statuses

        # Verify no phase states
        status_values = [s.value for s in statuses]
        assert "intake" not in status_values
        assert "blast_radius" not in status_values
        assert "hypothesis" not in status_values

    def test_terminal_status_detection(self):
        """Test terminal status property"""
        case = Case(
            user_id="u1",
            organization_id="org1",
            title="Test"
        )

        assert case.is_terminal == False

        case.status = CaseStatus.RESOLVED
        assert case.is_terminal == True

        case.status = CaseStatus.CLOSED
        assert case.is_terminal == True

class TestTurnProgress:
    """Test turn tracking"""

    def test_turn_progress_creation(self):
        """Test turn progress tracking"""
        turn = TurnProgress(
            turn_number=1,
            milestones_completed=["symptom_verified", "scope_assessed"],
            progress_made=True,
            outcome="milestone_completed"
        )

        assert turn.turn_number == 1
        assert len(turn.milestones_completed) == 2
        assert turn.progress_made == True

    def test_turn_outcome_enum(self):
        """Test all turn outcomes defined"""
        from faultmaven.models.case import TurnOutcome

        outcomes = list(TurnOutcome)
        assert "milestone_completed" in [o.value for o in outcomes]
        assert "data_provided" in [o.value for o in outcomes]
        assert "case_resolved" in [o.value for o in outcomes]

# Archive old tests
mv tests/models/test_case_models.py archive/tests/test_case_models_ooda.py

# Run NEW tests (must pass 100%)
pytest tests/models/test_case_models.py -v --tb=short
```

**Success Criteria**:
```bash
pytest tests/models/test_case_models.py -v
# Must show: 100% pass, no failures
```

---

#### Step 1.2: LLM Response Schemas

**Implementation**:
```bash
# Create new file (no version number)
touch faultmaven/models/llm_schemas.py

# Copy schemas from prompt-implementation-examples.md
# CRITICAL: Include mentioned_request_ids field
```

**Key Schemas**:
```python
# faultmaven/models/llm_schemas.py

from pydantic import BaseModel, Field
from typing import List, Optional

class MilestoneUpdates(BaseModel):
    """Milestone flags from LLM"""
    symptom_verified: Optional[bool] = None
    scope_assessed: Optional[bool] = None
    timeline_established: Optional[bool] = None
    changes_identified: Optional[bool] = None
    root_cause_identified: Optional[bool] = None
    solution_proposed: Optional[bool] = None
    solution_applied: Optional[bool] = None
    solution_verified: Optional[bool] = None

class EvidenceToAdd(BaseModel):
    """Evidence LLM wants to add"""
    raw_content: str
    source: str  # "user_message", "llm_inference", "tool_output"
    form: str    # "text", "json", "log", "code"
    # NO category - processor infers it

class InvestigationStateUpdate(BaseModel):
    """State updates from LLM"""
    milestones: MilestoneUpdates
    evidence_to_add: List[EvidenceToAdd] = Field(default_factory=list)
    mentioned_request_ids: List[str] = Field(
        default_factory=list,
        description="Evidence request IDs agent mentioned this turn"
    )
    # ... other fields

class InvestigationResponse(BaseModel):
    """LLM response for INVESTIGATING status"""
    agent_response: str
    state_update: InvestigationStateUpdate

class ConsultingResponse(BaseModel):
    """LLM response for CONSULTING status"""
    agent_response: str
    decided_to_investigate: bool = False
    # ... other fields

class TerminalResponse(BaseModel):
    """LLM response for RESOLVED/CLOSED status"""
    agent_response: str
    # ... documentation fields
```

**Testing IMMEDIATELY**:
```python
# tests/models/test_llm_schemas.py (NO version number!)

import pytest
from faultmaven.models.llm_schemas import (
    InvestigationResponse, InvestigationStateUpdate,
    MilestoneUpdates, EvidenceToAdd, ConsultingResponse
)

class TestInvestigationSchemas:
    """Test LLM response schemas"""

    def test_investigation_response_structure(self):
        """Test InvestigationResponse has required fields"""
        response = InvestigationResponse(
            agent_response="Testing...",
            state_update=InvestigationStateUpdate(
                milestones=MilestoneUpdates()
            )
        )

        assert response.agent_response == "Testing..."
        assert hasattr(response.state_update, 'milestones')
        assert hasattr(response.state_update, 'mentioned_request_ids')

    def test_mentioned_request_ids_tracking(self):
        """Test mentioned_request_ids field for mention_count"""
        state_update = InvestigationStateUpdate(
            milestones=MilestoneUpdates(),
            mentioned_request_ids=["req_123", "req_456"]
        )

        assert len(state_update.mentioned_request_ids) == 2
        assert "req_123" in state_update.mentioned_request_ids

    def test_milestone_updates_optional(self):
        """Test milestone fields are optional (LLM only sets what changed)"""
        milestones = MilestoneUpdates(
            symptom_verified=True,
            root_cause_identified=True
            # Other fields not set - that's OK
        )

        assert milestones.symptom_verified == True
        assert milestones.root_cause_identified == True
        assert milestones.scope_assessed is None  # Not set

    def test_evidence_no_category(self):
        """Test evidence schema does NOT include category (processor infers)"""
        evidence = EvidenceToAdd(
            raw_content="Error: timeout",
            source="user_message",
            form="text"
        )

        # Should NOT have category field
        assert not hasattr(evidence, 'category')

    def test_consulting_response_schema(self):
        """Test CONSULTING status response"""
        response = ConsultingResponse(
            agent_response="Let me help...",
            decided_to_investigate=False
        )

        assert response.agent_response == "Let me help..."
        assert response.decided_to_investigate == False

# Run tests (must pass 100%)
pytest tests/models/test_llm_schemas.py -v
```

**Success Criteria**:
```bash
pytest tests/models/test_llm_schemas.py -v
# Must show: 100% pass
```

---

#### Step 1.3: Database Repository Integration

**Implementation**:
```python
# faultmaven/infrastructure/database/repositories/case_repository.py

# UPDATE existing repository for new Case model
# NO changes to interface - only internal mapping

from faultmaven.models.case import Case, InvestigationProgress, TurnProgress
from faultmaven.infrastructure.database.repositories.base import CaseRepository

class SQLiteCaseRepository(CaseRepository):
    """SQLite implementation (file_based mode)"""

    async def save(self, case: Case) -> Case:
        """Save case with new v2.0 structure"""

        # Map Case object → database row
        case_data = {
            'case_id': case.case_id,
            'user_id': case.user_id,
            'organization_id': case.organization_id,
            'title': case.title,
            'status': case.status.value,  # NEW: 4 statuses
            'current_turn': case.current_turn,
            'turns_without_progress': case.turns_without_progress,

            # Serialize progress as JSON
            'progress': case.progress.dict(),

            # Serialize turn_history as JSON array
            'turn_history': [t.dict() for t in case.turn_history],

            # ... other fields
        }

        # Use existing database abstraction
        await self.db.execute_insert_or_update(case_data)

        return case

    async def get(self, case_id: str) -> Optional[Case]:
        """Retrieve case and reconstruct v2.0 model"""

        row = await self.db.fetch_one(case_id)
        if not row:
            return None

        # Reconstruct Case object from database row
        return Case(
            case_id=row['case_id'],
            user_id=row['user_id'],
            organization_id=row['organization_id'],
            title=row['title'],
            status=CaseStatus(row['status']),  # Convert string → enum
            current_turn=row['current_turn'],
            turns_without_progress=row['turns_without_progress'],

            # Deserialize JSON → objects
            progress=InvestigationProgress(**row['progress']),
            turn_history=[TurnProgress(**t) for t in row['turn_history']],

            # ... other fields
        )
```

**Testing IMMEDIATELY**:
```python
# tests/infrastructure/test_case_repository.py (NO version number!)

import pytest
from faultmaven.models.case import Case, CaseStatus, InvestigationProgress
from faultmaven.infrastructure.database.repositories.case_repository import SQLiteCaseRepository

@pytest.fixture
async def repo():
    """Create test repository with temp database"""
    repo = SQLiteCaseRepository(db_path=":memory:")
    await repo.initialize()
    return repo

@pytest.mark.asyncio
async def test_save_and_retrieve_case(repo):
    """Test round-trip save/load"""

    # Create case with milestones
    case = Case(
        user_id="user_123",
        organization_id="org_456",
        title="Test Case"
    )
    case.progress.symptom_verified = True
    case.progress.root_cause_identified = True

    # Save
    saved = await repo.save(case)
    assert saved.case_id is not None

    # Retrieve
    retrieved = await repo.get(saved.case_id)
    assert retrieved is not None
    assert retrieved.user_id == "user_123"
    assert retrieved.progress.symptom_verified == True
    assert retrieved.progress.root_cause_identified == True

@pytest.mark.asyncio
async def test_status_enum_persisted_correctly(repo):
    """Test CaseStatus enum saved/loaded correctly"""

    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    saved = await repo.save(case)
    retrieved = await repo.get(saved.case_id)

    assert retrieved.status == CaseStatus.INVESTIGATING
    assert isinstance(retrieved.status, CaseStatus)

# Run tests (must pass 100%)
pytest tests/infrastructure/test_case_repository.py -v
```

**Success Criteria**:
```bash
pytest tests/infrastructure/test_case_repository.py -v
# Must show: 100% pass
```

---

**Phase 1 Complete When**:
```bash
# All model tests pass
pytest tests/models/ -v

# All repository tests pass
pytest tests/infrastructure/test_case_repository.py -v

# Git checkpoint
git add .
git commit -m "Phase 1: Milestone-based models implemented and tested"
```

---

### Phase 2: Prompt System (2 days)

**Goal**: Create milestone-based prompt templates and builders

#### Step 2.1: Prompt Templates

**Implementation**:
```bash
# Create directory structure (if not exists)
mkdir -p faultmaven/services/agentic/prompts

# Create templates.py (NO version number!)
touch faultmaven/services/agentic/prompts/templates.py
```

**Code** (from prompt-templates.md):
```python
# faultmaven/services/agentic/prompts/templates.py

def build_consulting_prompt(case: Case, user_message: str) -> str:
    """Build prompt for CONSULTING status"""
    # Copy implementation from prompt-templates.md Section 1
    pass

def build_investigating_prompt(case: Case, user_message: str) -> str:
    """Build prompt for INVESTIGATING status with adaptive instructions"""
    # Copy implementation from prompt-templates.md Section 2
    # CRITICAL: Include mentioned_request_ids instructions
    pass

def build_terminal_prompt(case: Case, user_message: str) -> str:
    """Build prompt for RESOLVED/CLOSED status"""
    # Copy implementation from prompt-templates.md Section 3
    pass
```

**Testing IMMEDIATELY**:
```python
# tests/unit/prompts/test_templates.py (NO version number!)

import pytest
from faultmaven.models.case import Case, CaseStatus
from faultmaven.services.agentic.prompts.templates import (
    build_consulting_prompt,
    build_investigating_prompt,
    build_terminal_prompt
)

class TestPromptTemplates:
    """Test prompt generation"""

    def test_consulting_prompt_includes_key_elements(self):
        """Test CONSULTING prompt has required sections"""
        case = Case(
            user_id="u1",
            organization_id="org1",
            title="Test",
            status=CaseStatus.CONSULTING
        )

        prompt = build_consulting_prompt(case, "API is slow")

        # Must include these sections
        assert "CONSULTING" in prompt
        assert "API is slow" in prompt
        assert "ConsultingResponse" in prompt  # Response schema
        assert "decided_to_investigate" in prompt

    def test_investigating_prompt_adapts_to_milestones(self):
        """Test INVESTIGATING prompt changes based on completed milestones"""
        case = Case(
            user_id="u1",
            organization_id="org1",
            title="Test",
            status=CaseStatus.INVESTIGATING
        )

        # Before any milestones
        prompt1 = build_investigating_prompt(case, "Here are logs")
        assert "symptom_verified" in prompt1

        # After symptom verified
        case.progress.symptom_verified = True
        prompt2 = build_investigating_prompt(case, "More info")
        # Prompt should focus on next milestones
        assert "root_cause_identified" in prompt2

    def test_mentioned_request_ids_instructions_present(self):
        """Test prompt includes instructions for mentioned_request_ids"""
        case = Case(
            user_id="u1",
            organization_id="org1",
            title="Test",
            status=CaseStatus.INVESTIGATING
        )

        prompt = build_investigating_prompt(case, "Test")

        # Must explain mentioned_request_ids
        assert "mentioned_request_ids" in prompt
        assert "increment mention_count" in prompt.lower() or "track mentions" in prompt.lower()

    def test_terminal_prompt_for_resolved(self):
        """Test RESOLVED status prompt"""
        case = Case(
            user_id="u1",
            organization_id="org1",
            title="Test",
            status=CaseStatus.RESOLVED
        )

        prompt = build_terminal_prompt(case, "Generate report")
        assert "documentation" in prompt.lower() or "report" in prompt.lower()

# Run tests (must pass 100%)
pytest tests/unit/prompts/test_templates.py -v
```

**Success Criteria**:
```bash
pytest tests/unit/prompts/test_templates.py -v
# Must show: 100% pass
```

---

#### Step 2.2: Prompt Builder

**Implementation**:
```python
# faultmaven/services/agentic/prompts/builder.py

from faultmaven.models.case import Case
from faultmaven.services.agentic.prompts.templates import (
    build_consulting_prompt,
    build_investigating_prompt,
    build_terminal_prompt
)

class PromptBuilder:
    """Builds prompts based on case status"""

    def build_prompt(
        self,
        case: Case,
        user_message: str
    ) -> str:
        """Build prompt for current case status"""

        if case.status == CaseStatus.CONSULTING:
            return build_consulting_prompt(case, user_message)

        elif case.status == CaseStatus.INVESTIGATING:
            return build_investigating_prompt(case, user_message)

        elif case.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]:
            return build_terminal_prompt(case, user_message)

        else:
            raise ValueError(f"Unknown case status: {case.status}")
```

**Testing IMMEDIATELY**:
```python
# tests/unit/prompts/test_builder.py

import pytest
from faultmaven.models.case import Case, CaseStatus
from faultmaven.services.agentic.prompts.builder import PromptBuilder

class TestPromptBuilder:
    """Test prompt builder routing"""

    @pytest.fixture
    def builder(self):
        return PromptBuilder()

    def test_routes_to_consulting_prompt(self, builder):
        """Test CONSULTING status routes correctly"""
        case = Case(
            user_id="u1",
            organization_id="org1",
            title="Test",
            status=CaseStatus.CONSULTING
        )

        prompt = builder.build_prompt(case, "Help me")
        assert "CONSULTING" in prompt

    def test_routes_to_investigating_prompt(self, builder):
        """Test INVESTIGATING status routes correctly"""
        case = Case(
            user_id="u1",
            organization_id="org1",
            title="Test",
            status=CaseStatus.INVESTIGATING
        )

        prompt = builder.build_prompt(case, "Here's data")
        assert "INVESTIGATING" in prompt or "milestone" in prompt.lower()

    def test_routes_to_terminal_prompt(self, builder):
        """Test RESOLVED/CLOSED route correctly"""
        for status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]:
            case = Case(
                user_id="u1",
                organization_id="org1",
                title="Test",
                status=status
            )

            prompt = builder.build_prompt(case, "Generate docs")
            assert len(prompt) > 0

# Run tests
pytest tests/unit/prompts/test_builder.py -v
```

**Success Criteria**:
```bash
pytest tests/unit/prompts/test_builder.py -v
# Must show: 100% pass
```

---

**Phase 2 Complete When**:
```bash
# All prompt tests pass
pytest tests/unit/prompts/ -v

# Git checkpoint
git add .
git commit -m "Phase 2: Milestone-based prompts implemented and tested"
```

---

### Phase 3: Response Processors (3 days)

**Goal**: Create processors for each case status

**Key Requirement**: Use `CaseRepository` abstraction (dependency injection)

#### Step 3.1: Investigating Processor (Most Complex)

**Implementation**:
```python
# faultmaven/services/agentic/processors/investigating_processor.py

from faultmaven.models.case import Case, InvestigationProgress
from faultmaven.models.llm_schemas import InvestigationResponse
from faultmaven.infrastructure.database.repositories.base import CaseRepository  # ← Interface
from faultmaven.dependencies import get_case_repository

class InvestigatingProcessor:
    """Process LLM responses for INVESTIGATING status"""

    def __init__(
        self,
        case_repository: CaseRepository,  # ← Dependency injection (interface)
        logger: Logger
    ):
        self.repo = case_repository  # ← Use abstraction
        self.logger = logger

    async def process(
        self,
        case: Case,
        user_message: str,
        llm_response: InvestigationResponse
    ) -> Tuple[Case, Dict[str, Any]]:
        """Process LLM response and update case"""

        # 1. Apply milestone updates
        milestones_completed = await self._apply_milestone_updates(
            case,
            llm_response.state_update.milestones
        )

        # 2. Handle mentioned_request_ids → increment mention_count
        await self._process_mentioned_requests(
            case,
            llm_response.state_update.mentioned_request_ids
        )

        # 3. Add evidence with inferred categories
        await self._add_evidence(
            case,
            llm_response.state_update.evidence_to_add
        )

        # 4. Check status transitions
        case = await self._check_status_transitions(case)

        # 5. Update turn tracking
        turn_progress = self._create_turn_progress(
            case,
            milestones_completed,
            llm_response
        )
        case.turn_history.append(turn_progress)
        case.current_turn += 1

        # 6. Update progress tracking
        if turn_progress.progress_made:
            case.turns_without_progress = 0
        else:
            case.turns_without_progress += 1

        # 7. Save case using repository abstraction
        updated_case = await self.repo.save(case)  # ← Use abstraction

        # 8. Return case and metadata
        metadata = {
            'milestones_completed': milestones_completed,
            'progress_made': turn_progress.progress_made,
            'turn_number': updated_case.current_turn
        }

        return updated_case, metadata

    async def _apply_milestone_updates(
        self,
        case: Case,
        milestones: MilestoneUpdates
    ) -> List[str]:
        """Apply milestone updates from LLM"""
        completed = []

        # Only update milestones that LLM set
        if milestones.symptom_verified is not None:
            if not case.progress.symptom_verified and milestones.symptom_verified:
                case.progress.symptom_verified = True
                completed.append("symptom_verified")

        if milestones.root_cause_identified is not None:
            if not case.progress.root_cause_identified and milestones.root_cause_identified:
                case.progress.root_cause_identified = True
                completed.append("root_cause_identified")

        # ... other milestones

        return completed

    async def _process_mentioned_requests(
        self,
        case: Case,
        mentioned_ids: List[str]
    ):
        """Increment mention_count for mentioned evidence requests"""
        for req_id in mentioned_ids:
            # Find request in case.evidence_requests
            for req in case.evidence_requests:
                if req.request_id == req_id:
                    req.mention_count += 1
                    self.logger.info(f"Incremented mention_count for {req_id} to {req.mention_count}")

    async def _add_evidence(
        self,
        case: Case,
        evidence_list: List[EvidenceToAdd]
    ):
        """Add evidence with category inference"""
        for ev in evidence_list:
            # Infer category from content and context
            category = self._infer_evidence_category(
                ev.raw_content,
                case.progress
            )

            # Create Evidence object
            evidence = Evidence(
                raw_content=ev.raw_content,
                source=ev.source,
                form=ev.form,
                category=category  # ← Processor infers
            )

            case.evidence.append(evidence)

    def _infer_evidence_category(
        self,
        content: str,
        progress: InvestigationProgress
    ) -> str:
        """Infer evidence category from content and investigation state"""

        # Simple heuristics (can be enhanced with ML)
        content_lower = content.lower()

        # SYMPTOM indicators
        if any(word in content_lower for word in ['error', 'timeout', 'failure', '500', '404']):
            return "SYMPTOM"

        # ROOT_CAUSE indicators
        if any(word in content_lower for word in ['root cause', 'bug in', 'issue with']):
            return "ROOT_CAUSE"

        # SCOPE indicators
        if any(word in content_lower for word in ['affected users', 'all regions', 'scope']):
            return "SCOPE"

        # TIMELINE indicators
        if any(word in content_lower for word in ['started at', 'since', 'after deployment']):
            return "TIMELINE"

        # Default based on investigation state
        if not progress.symptom_verified:
            return "SYMPTOM"
        elif not progress.root_cause_identified:
            return "DIAGNOSTIC"
        else:
            return "OTHER"

    async def _check_status_transitions(self, case: Case) -> Case:
        """Check if case should transition status"""

        # INVESTIGATING → RESOLVED: solution_verified = True
        if case.progress.solution_verified:
            case.status = CaseStatus.RESOLVED
            case.resolved_at = datetime.now(timezone.utc)
            case.closed_at = datetime.now(timezone.utc)

        return case

    def _create_turn_progress(
        self,
        case: Case,
        milestones_completed: List[str],
        llm_response: InvestigationResponse
    ) -> TurnProgress:
        """Create turn progress record"""

        # Determine if progress was made
        progress_made = (
            len(milestones_completed) > 0 or
            len(llm_response.state_update.evidence_to_add) > 0
        )

        # Determine outcome
        if len(milestones_completed) > 0:
            outcome = "milestone_completed"
        elif case.progress.solution_verified:
            outcome = "case_resolved"
        elif len(llm_response.state_update.evidence_to_add) > 0:
            outcome = "data_provided"
        else:
            outcome = "conversation"

        return TurnProgress(
            turn_number=case.current_turn + 1,
            milestones_completed=milestones_completed,
            progress_made=progress_made,
            outcome=outcome
        )

# Usage (in milestone_engine.py)
from faultmaven.dependencies import get_case_repository

repo = get_case_repository()  # ← Get right implementation
processor = InvestigatingProcessor(
    case_repository=repo,  # ← Inject
    logger=logger
)
```

**Testing IMMEDIATELY** (Most Critical Tests):
```python
# tests/unit/processors/test_investigating_processor.py

import pytest
from unittest.mock import AsyncMock
from faultmaven.models.case import Case, CaseStatus
from faultmaven.models.llm_schemas import (
    InvestigationResponse, InvestigationStateUpdate,
    MilestoneUpdates, EvidenceToAdd
)
from faultmaven.services.agentic.processors.investigating_processor import InvestigatingProcessor

@pytest.fixture
def mock_repo():
    """Mock repository for testing (NO real database)"""
    repo = AsyncMock()
    repo.save = AsyncMock(side_effect=lambda case: case)  # Return case as-is
    return repo

@pytest.fixture
def processor(mock_repo):
    """Create processor with mock dependencies"""
    return InvestigatingProcessor(
        case_repository=mock_repo,  # ← Mock injected
        logger=mock_logger
    )

@pytest.mark.asyncio
async def test_milestone_completion(processor, mock_repo):
    """Test milestone updates applied correctly"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    llm_response = InvestigationResponse(
        agent_response="I've verified the symptom and identified root cause",
        state_update=InvestigationStateUpdate(
            milestones=MilestoneUpdates(
                symptom_verified=True,
                root_cause_identified=True
            )
        )
    )

    updated_case, metadata = await processor.process(
        case=case,
        user_message="Here are the logs",
        llm_response=llm_response
    )

    # Verify milestones updated
    assert updated_case.progress.symptom_verified == True
    assert updated_case.progress.root_cause_identified == True

    # Verify metadata
    assert len(metadata['milestones_completed']) == 2
    assert 'symptom_verified' in metadata['milestones_completed']

    # Verify repository save called
    mock_repo.save.assert_called_once()

@pytest.mark.asyncio
async def test_mentioned_request_ids_increment_mention_count(processor, mock_repo):
    """Test mention_count increments when request mentioned"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    # Add evidence request with mention_count=0
    from faultmaven.models.case import EvidenceRequest
    req = EvidenceRequest(
        request_id="req_123",
        category="SYMPTOM",
        description="Provide error logs",
        mention_count=0
    )
    case.evidence_requests.append(req)

    # LLM mentions the request
    llm_response = InvestigationResponse(
        agent_response="Can you provide the error logs (req_123)?",
        state_update=InvestigationStateUpdate(
            milestones=MilestoneUpdates(),
            mentioned_request_ids=["req_123"]  # ← Mentioned
        )
    )

    updated_case, _ = await processor.process(case, "...", llm_response)

    # Find request and verify mention_count incremented
    req_after = next(r for r in updated_case.evidence_requests if r.request_id == "req_123")
    assert req_after.mention_count == 1

@pytest.mark.asyncio
async def test_evidence_category_inference(processor, mock_repo):
    """Test processor infers evidence category correctly"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    # LLM adds evidence WITHOUT category
    llm_response = InvestigationResponse(
        agent_response="...",
        state_update=InvestigationStateUpdate(
            milestones=MilestoneUpdates(),
            evidence_to_add=[
                EvidenceToAdd(
                    raw_content="Error: Connection timeout at 2pm",
                    source="user_message",
                    form="text"
                    # NO category field!
                )
            ]
        )
    )

    updated_case, _ = await processor.process(case, "...", llm_response)

    # Verify evidence added with inferred category
    assert len(updated_case.evidence) == 1
    added_evidence = updated_case.evidence[0]
    assert added_evidence.category == "SYMPTOM"  # ← Processor inferred

@pytest.mark.asyncio
async def test_progress_detection_with_milestones(processor, mock_repo):
    """Test progress detected when milestones completed"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING,
        turns_without_progress=2  # Was stuck
    )

    # LLM completes milestone
    llm_response = InvestigationResponse(
        agent_response="Root cause identified",
        state_update=InvestigationStateUpdate(
            milestones=MilestoneUpdates(
                root_cause_identified=True
            )
        )
    )

    updated_case, metadata = await processor.process(case, "...", llm_response)

    # Progress made
    assert metadata['progress_made'] == True
    assert updated_case.turns_without_progress == 0  # Reset

@pytest.mark.asyncio
async def test_status_transition_to_resolved(processor, mock_repo):
    """Test INVESTIGATING → RESOLVED when solution verified"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    # LLM verifies solution
    llm_response = InvestigationResponse(
        agent_response="Solution verified working!",
        state_update=InvestigationStateUpdate(
            milestones=MilestoneUpdates(
                solution_verified=True
            )
        )
    )

    updated_case, _ = await processor.process(case, "...", llm_response)

    # Status transitioned
    assert updated_case.status == CaseStatus.RESOLVED
    assert updated_case.resolved_at is not None

# Run tests (MUST PASS 100%)
pytest tests/unit/processors/test_investigating_processor.py -v
```

**Success Criteria**:
```bash
pytest tests/unit/processors/test_investigating_processor.py -v
# Must show: 100% pass, especially:
# - test_milestone_completion
# - test_mentioned_request_ids_increment_mention_count
# - test_evidence_category_inference
# - test_progress_detection_with_milestones
# - test_status_transition_to_resolved
```

---

#### Step 3.2: Consulting & Terminal Processors

**Implementation**: Similar pattern to InvestigatingProcessor

**Testing**: Similar test structure with mocks

**Success Criteria**:
```bash
pytest tests/unit/processors/ -v
# All processor tests pass 100%
```

---

**Phase 3 Complete When**:
```bash
# All processor tests pass
pytest tests/unit/processors/ -v --cov=faultmaven/services/agentic/processors

# Coverage check
# Target: 90%+ coverage on processors

# Git checkpoint
git add .
git commit -m "Phase 3: Response processors implemented with repository abstraction"
```

---

### Phase 4: Integration & Orchestration (2 days)

**Goal**: Wire everything together with milestone engine

#### Step 4.1: Milestone Engine

**Implementation**:
```python
# faultmaven/services/agentic/orchestration/milestone_engine.py

from faultmaven.models.case import Case
from faultmaven.infrastructure.database.repositories.base import CaseRepository
from faultmaven.services.agentic.prompts.builder import PromptBuilder
from faultmaven.services.agentic.processors.investigating_processor import InvestigatingProcessor
from faultmaven.services.agentic.processors.consulting_processor import ConsultingProcessor
from faultmaven.services.agentic.processors.terminal_processor import TerminalProcessor
from faultmaven.infrastructure.llm.base import ILLMProvider

class MilestoneInvestigationEngine:
    """Main investigation engine using milestone-based approach"""

    def __init__(
        self,
        case_repository: CaseRepository,  # ← DI
        llm_provider: ILLMProvider,       # ← DI
        logger: Logger
    ):
        self.repo = case_repository
        self.llm = llm_provider
        self.logger = logger

        # Initialize components
        self.prompt_builder = PromptBuilder()

        self.processors = {
            CaseStatus.CONSULTING: ConsultingProcessor(case_repository, logger),
            CaseStatus.INVESTIGATING: InvestigatingProcessor(case_repository, logger),
            CaseStatus.RESOLVED: TerminalProcessor(case_repository, logger),
            CaseStatus.CLOSED: TerminalProcessor(case_repository, logger)
        }

    async def process_turn(
        self,
        case: Case,
        user_message: str,
        attachments: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Process one conversation turn"""

        try:
            # 1. Build prompt based on case status
            prompt = self.prompt_builder.build_prompt(case, user_message)

            # 2. Call LLM
            llm_response = await self.llm.generate(
                prompt=prompt,
                response_schema=self._get_response_schema(case.status)
            )

            # 3. Process response with appropriate processor
            processor = self.processors[case.status]
            updated_case, metadata = await processor.process(
                case=case,
                user_message=user_message,
                llm_response=llm_response
            )

            # 4. Return result
            return {
                'response': llm_response.agent_response,
                'case': updated_case,
                'metadata': metadata
            }

        except Exception as e:
            self.logger.error(f"Turn processing error: {e}")
            raise

    def _get_response_schema(self, status: CaseStatus):
        """Get LLM response schema for case status"""
        from faultmaven.models.llm_schemas import (
            ConsultingResponse,
            InvestigationResponse,
            TerminalResponse
        )

        if status == CaseStatus.CONSULTING:
            return ConsultingResponse
        elif status == CaseStatus.INVESTIGATING:
            return InvestigationResponse
        else:
            return TerminalResponse

# Usage (in agent_service.py)
from faultmaven.dependencies import get_case_repository

repo = get_case_repository()
engine = MilestoneInvestigationEngine(
    case_repository=repo,  # ← Injected
    llm_provider=llm_provider,
    logger=logger
)

result = await engine.process_turn(case, user_message)
```

**Testing IMMEDIATELY**:
```python
# tests/integration/test_milestone_engine.py

import pytest
from unittest.mock import AsyncMock
from faultmaven.models.case import Case, CaseStatus
from faultmaven.services.agentic.orchestration.milestone_engine import MilestoneInvestigationEngine

@pytest.fixture
def mock_repo():
    repo = AsyncMock()
    repo.save = AsyncMock(side_effect=lambda case: case)
    return repo

@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    return llm

@pytest.fixture
def engine(mock_repo, mock_llm):
    return MilestoneInvestigationEngine(
        case_repository=mock_repo,
        llm_provider=mock_llm,
        logger=mock_logger
    )

@pytest.mark.asyncio
async def test_process_turn_basic(engine, mock_llm):
    """Test basic turn processing"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    # Mock LLM response
    from faultmaven.models.llm_schemas import InvestigationResponse, InvestigationStateUpdate
    mock_llm.generate.return_value = InvestigationResponse(
        agent_response="Let me help...",
        state_update=InvestigationStateUpdate(milestones=MilestoneUpdates())
    )

    result = await engine.process_turn(
        case=case,
        user_message="API is slow"
    )

    assert 'response' in result
    assert 'case' in result
    assert 'metadata' in result
    assert result['case'].current_turn == 1

@pytest.mark.asyncio
async def test_one_turn_resolution(engine, mock_llm):
    """Test complete investigation in one turn"""
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )

    # Mock LLM completing all milestones
    mock_llm.generate.return_value = InvestigationResponse(
        agent_response="Complete analysis...",
        state_update=InvestigationStateUpdate(
            milestones=MilestoneUpdates(
                symptom_verified=True,
                timeline_established=True,
                root_cause_identified=True,
                solution_proposed=True,
                solution_verified=True
            )
        )
    )

    result = await engine.process_turn(
        case=case,
        user_message="Here's comprehensive log with everything"
    )

    updated_case = result['case']

    # Verify all milestones completed
    assert updated_case.progress.symptom_verified == True
    assert updated_case.progress.root_cause_identified == True
    assert updated_case.progress.solution_verified == True

    # Status should transition to RESOLVED
    assert updated_case.status == CaseStatus.RESOLVED

# Run tests
pytest tests/integration/test_milestone_engine.py -v
```

**Success Criteria**:
```bash
pytest tests/integration/test_milestone_engine.py -v
# Must show: 100% pass, especially one-turn resolution test
```

---

**Phase 4 Complete When**:
```bash
# Integration tests pass
pytest tests/integration/ -v

# Git checkpoint
git add .
git commit -m "Phase 4: Milestone engine integrated and tested"
```

---

### Phase 5: API Layer (1 day)

**Goal**: Update API endpoints (preserve contract)

#### Step 5.1: Update Internal Implementation Only

**Critical**: **DO NOT change API signatures** (frontend depends on them)

**Implementation**:
```python
# faultmaven/api/v1/routes/agent.py

# ✅ KEEP: All endpoint signatures unchanged
# ✅ UPDATE: Internal implementation to use milestone engine

from faultmaven.dependencies import get_case_repository
from faultmaven.services.agentic.orchestration.milestone_engine import MilestoneInvestigationEngine

@router.post("/agent/process")
async def process_agent_turn(
    request: AgentTurnRequest,  # ✅ KEEP: Same request schema
    session_id: str = Depends(get_session_id)
) -> AgentTurnResponse:  # ✅ KEEP: Same response schema
    """
    Process agent turn (API contract unchanged)

    Internal implementation now uses milestone-based engine
    """

    # Get dependencies
    repo = get_case_repository()
    llm_provider = get_llm_provider()

    # Create engine
    engine = MilestoneInvestigationEngine(
        case_repository=repo,
        llm_provider=llm_provider,
        logger=logger
    )

    # Get case
    case = await repo.get(request.case_id)

    # Process turn (NEW implementation)
    result = await engine.process_turn(
        case=case,
        user_message=request.user_message,
        attachments=request.attachments
    )

    # Map to API response (SAME format as before)
    return AgentTurnResponse(
        response=result['response'],
        case_id=result['case'].case_id,
        status=result['case'].status.value,
        # ... other fields
    )
```

**Testing**:
```python
# tests/api/test_agent_routes.py

import pytest
from fastapi.testclient import TestClient

def test_agent_process_endpoint_signature_unchanged(client: TestClient):
    """Test API contract preserved"""

    response = client.post(
        "/api/v1/agent/process",
        json={
            "case_id": "case_123",
            "user_message": "Test message"
        }
    )

    # Response structure unchanged
    assert response.status_code == 200
    data = response.json()
    assert 'response' in data
    assert 'case_id' in data
    assert 'status' in data

# Run tests
pytest tests/api/test_agent_routes.py -v
```

**Success Criteria**:
```bash
# API tests pass (contract preserved)
pytest tests/api/ -v
```

---

**Phase 5 Complete When**:
```bash
# All API tests pass
pytest tests/api/ -v

# Git checkpoint
git add .
git commit -m "Phase 5: API layer updated (contract preserved)"
```

---

### Phase 6: Archive Old Code (1 day)

**Goal**: Move old OODA code to archive

**Implementation**:
```bash
# Archive old modules (keep for reference)
mkdir -p archive/ooda_framework

# Move (don't delete) old code
mv faultmaven/core/investigation/ooda_engine.py archive/ooda_framework/
mv faultmaven/core/investigation/phases.py archive/ooda_framework/
mv faultmaven/core/investigation/phase_loopback.py archive/ooda_framework/
mv faultmaven/core/investigation/iteration_strategy.py archive/ooda_framework/
mv faultmaven/core/investigation/ooda_step_extraction.py archive/ooda_framework/

# Move old phase orchestration
mv faultmaven/services/agentic/orchestration/phase_orchestrator.py archive/ooda_framework/
mv -r faultmaven/services/agentic/orchestration/phase_handlers/ archive/ooda_framework/

# Archive old tests
mkdir -p archive/tests
mv tests/core/investigation/test_ooda_engine.py archive/tests/
mv tests/core/investigation/test_phases.py archive/tests/
mv tests/core/investigation/test_phase_loopback.py archive/tests/
mv tests/services/agentic/test_phase_orchestrator.py archive/tests/
mv -r tests/services/agentic/phase_handlers/ archive/tests/

# Archive old docs (keep for historical reference)
mv docs/architecture/investigation-phases-and-ooda-integration.md archive/
mv docs/architecture/phase-*.md archive/
```

**Verification**:
```bash
# No remaining imports to archived modules
grep -r "ooda_engine\|phase_orchestrator\|phase_loopback" faultmaven/
grep -r "ooda_engine\|phase_orchestrator\|phase_loopback" tests/

# Should return: No matches

# All tests still pass (using new code)
pytest tests/ -v
```

**Success Criteria**:
```bash
# No broken imports
grep -r "import.*ooda_engine" faultmaven/  # Returns nothing
grep -r "import.*phases" faultmaven/       # Returns nothing

# All tests pass
pytest tests/ -v --tb=short
# Must show: 100% pass
```

---

**Phase 6 Complete When**:
```bash
# Archive complete, no broken imports, all tests pass
git add .
git commit -m "Phase 6: Archived old OODA framework code"
```

---

### Phase 7: Final Integration & Documentation (1 day)

**Goal**: End-to-end testing and documentation

#### Step 7.1: End-to-End Tests

**Testing**:
```python
# tests/integration/test_end_to_end.py

import pytest

@pytest.mark.asyncio
async def test_complete_investigation_flow():
    """Test complete investigation from CONSULTING → RESOLVED"""

    # 1. Create case
    case = Case(
        user_id="u1",
        organization_id="org1",
        title="API Timeout Issue",
        status=CaseStatus.CONSULTING
    )

    # 2. CONSULTING → INVESTIGATING
    # ... test transition

    # 3. Complete milestones
    # ... test milestone completion

    # 4. INVESTIGATING → RESOLVED
    # ... test resolution

    assert case.status == CaseStatus.RESOLVED
    assert case.progress.solution_verified == True

@pytest.mark.asyncio
async def test_one_turn_resolution_realistic():
    """Test realistic one-turn resolution scenario"""
    # User provides comprehensive diagnostic data upfront
    pass

# Run full suite
pytest tests/ -v --cov --tb=short
```

**Success Criteria**:
```bash
# Full test suite passes
pytest tests/ -v --cov --html=coverage_report.html

# Coverage targets:
# - Overall: 80%+
# - Processors: 90%+
# - Models: 95%+
```

#### Step 7.2: Update Documentation

**Tasks**:
```bash
# Update README
# - Milestone-based approach
# - Architecture overview
# - Quick start guide

# Create migration changelog
touch docs/CHANGELOG-v2.0.md
# Document what changed, why, and how to use new system

# Update API documentation
# - Note internal changes
# - Confirm contract unchanged
```

---

**Phase 7 Complete When**:
```bash
# Full suite passes
pytest tests/ -v --cov

# Documentation updated
# CHANGELOG-v2.0.md created
# README.md reflects new architecture

# Final git checkpoint
git add .
git commit -m "Phase 7: Migration complete - milestone-based investigation v2.0"
git tag v2.0.0
```

---

## Summary: Execution Timeline

| Phase | Duration | Key Deliverables |
|-------|----------|------------------|
| 0. Pre-Migration | 1 day | Archive structure, API contract doc |
| 1. Foundation | 2 days | Case models, LLM schemas, repository |
| 2. Prompts | 2 days | Templates, builder, tests |
| 3. Processors | 3 days | All 3 processors with tests |
| 4. Integration | 2 days | Milestone engine, wiring |
| 5. API Layer | 1 day | Update internals, preserve contract |
| 6. Archive | 1 day | Move old code, verify cleanup |
| 7. Final Testing | 1 day | E2E tests, documentation |
| **TOTAL** | **13 days** | **Complete v2.0 migration** |

---

## Success Criteria Checklist

### Code Quality
- [ ] ✅ No version numbers in filenames
- [ ] ✅ All old code in archive/ (not deleted)
- [ ] ✅ No direct database imports (sqlite3, psycopg2)
- [ ] ✅ All classes use repository abstraction (DI)
- [ ] ✅ API contract preserved (endpoints unchanged)

### Testing
- [ ] ✅ 100% pass rate on all tests
- [ ] ✅ 80%+ overall coverage
- [ ] ✅ 90%+ coverage on processors
- [ ] ✅ One-turn resolution test passes
- [ ] ✅ E2E investigation flow test passes

### Documentation
- [ ] ✅ CHANGELOG-v2.0.md created
- [ ] ✅ README.md updated
- [ ] ✅ API docs reflect internal changes
- [ ] ✅ Team briefing created

### Cleanup
- [ ] ✅ No broken imports
- [ ] ✅ Old modules in archive/
- [ ] ✅ Old tests in archive/tests/
- [ ] ✅ Old docs in archive/

---

## Ready to Start?

I will execute this plan **sequentially** for optimal context management:

**Phase 0 → Phase 1 → Phase 2 → ... → Phase 7**

Each phase:
1. ✅ Implement code
2. ✅ Write tests immediately
3. ✅ Verify 100% pass
4. ✅ Git checkpoint
5. ✅ Move to next phase

**Context optimization**: Full context window available for each phase

**Quality over speed**: Taking 13 days to build it right

---

**Ready to begin Phase 0 (Pre-Migration Cleanup)?**

Say "start" and I'll begin with:
1. Creating archive structure
2. Documenting API contract
3. Running baseline tests
