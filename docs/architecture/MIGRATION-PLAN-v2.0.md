# Migration Plan: OODA Framework → Milestone-Based Investigation (v2.0)

## Executive Summary

**Current State**: Implementation based on OODA (Observe-Orient-Decide-Act) framework with rigid 7-phase progression (Intake → Blast Radius → Timeline → Hypothesis → Validation → Solution → Document)

**Target State**: Milestone-based investigation where agent completes tasks opportunistically based on data availability, not sequential phases

**Migration Strategy**: **Clean replacement** (no backward compatibility needed since system has no production users/data)

---

## 1. Key Architectural Changes

### 1.1 Philosophy Shift

| Aspect | OLD (OODA/Phase-Based) | NEW (Milestone-Based) |
|--------|------------------------|----------------------|
| **Progress Model** | Sequential phases 0-6 | Milestone completions (8 milestones) |
| **Workflow** | Must complete Phase N before Phase N+1 | Complete any milestone when data available |
| **One-Turn Resolution** | Impossible (rigid phases) | ✅ Possible (complete all milestones in 1 turn) |
| **Status** | Phase number (0-6) | Case status (CONSULTING/INVESTIGATING/RESOLVED/CLOSED) |
| **Hypothesis** | Required Phase 3 | **Optional** exploration path |
| **OODA Loop** | Central execution engine | **Not used** (removed entirely) |

### 1.2 Critical Differences

```python
# OLD APPROACH (Phase-Based)
if case.current_phase == 3:  # Hypothesis phase
    generate_hypotheses()
    case.current_phase = 4  # Must advance sequentially

# NEW APPROACH (Milestone-Based)
if has_diagnostic_data(case) and not case.progress.root_cause_identified:
    # Complete root cause IMMEDIATELY if data is available
    identify_root_cause()
    case.progress.root_cause_identified = True
    # No phase advancement needed!
```

**Key Insight**: The new architecture **removes workflow orchestration from the LLM** and uses **simple state flags** instead.

---

## 2. Module Impact Assessment

### 2.1 HIGH IMPACT (Replace/Delete)

| Module | Current Function | Migration Action |
|--------|-----------------|------------------|
| `faultmaven/core/investigation/ooda_engine.py` | OODA loop execution | ❌ **DELETE** (not needed) |
| `faultmaven/core/investigation/phases.py` | Phase definitions | ❌ **DELETE** (replaced by milestones) |
| `faultmaven/core/investigation/phase_loopback.py` | Phase backtracking | ❌ **DELETE** (no phases) |
| `faultmaven/core/investigation/iteration_strategy.py` | OODA iteration control | ❌ **DELETE** (no iterations) |
| `faultmaven/core/investigation/ooda_step_extraction.py` | OODA step parsing | ❌ **DELETE** (no OODA) |
| `faultmaven/models/investigation.py` | `InvestigationState` with OODA | ✏️ **REPLACE** with milestone model |
| `faultmaven/models/case.py` | `CaseDiagnosticState` with phases | ✏️ **UPDATE** to milestone tracking |

### 2.2 MEDIUM IMPACT (Refactor)

| Module | Current Function | Migration Action |
|--------|-----------------|------------------|
| `faultmaven/core/investigation/hypothesis_manager.py` | Required hypothesis workflow | ✏️ **REFACTOR** to optional exploration |
| `faultmaven/core/investigation/working_conclusion_generator.py` | Working conclusion updates | ✏️ **REFACTOR** to milestone-based |
| `faultmaven/core/investigation/engagement_modes.py` | Consultant ↔ Lead Investigator | ✏️ **REFACTOR** (CONSULTING → INVESTIGATING status) |
| `faultmaven/core/investigation/strategy_selector.py` | Strategy selection | ✏️ **REFACTOR** to path selection matrix |
| `faultmaven/core/investigation/memory_manager.py` | OODA iteration memory | ✏️ **REFACTOR** to turn-based memory |

### 2.3 LOW IMPACT (Update References)

| Module | Current Function | Migration Action |
|--------|-----------------|------------------|
| `faultmaven/core/investigation/workflow_progression_detector.py` | Phase transition detection | ✏️ **UPDATE** to milestone detection |
| `faultmaven/models/evidence.py` | Evidence tracking | ✅ **KEEP** (compatible) |
| `faultmaven/core/knowledge/*` | Knowledge base | ✅ **KEEP** (no changes) |
| `faultmaven/core/processing/*` | Data processing | ✅ **KEEP** (no changes) |

---

## 3. Data Model Migration

### 3.1 InvestigationProgress Model

**BEFORE** (`models/investigation.py`):
```python
class InvestigationLifecycle(BaseModel):
    current_phase: InvestigationPhase = Field(default=InvestigationPhase.INTAKE)
    phase_name: str = Field(default="intake")
    case_status: str = Field(default="consulting")
    phase_history: List[PhaseTransition] = Field(default_factory=list)
    turns_in_current_phase: int = Field(default=0)
    # ... more phase-related fields
```

**AFTER** (NEW milestone-based):
```python
class InvestigationProgress(BaseModel):
    """Milestone-based progress tracking"""

    # Verification Milestones
    symptom_verified: bool = False
    scope_assessed: bool = False
    timeline_established: bool = False
    changes_identified: bool = False

    # Investigation Milestones
    root_cause_identified: bool = False
    root_cause_confidence: float = 0.0
    root_cause_method: Optional[str] = None

    # Resolution Milestones
    solution_proposed: bool = False
    solution_applied: bool = False
    solution_verified: bool = False

    @property
    def current_stage(self) -> InvestigationStage:
        """Computed from milestones, not stored"""
        if self.solution_proposed or self.solution_applied:
            return InvestigationStage.RESOLVING
        if self.symptom_verified and not self.root_cause_identified:
            return InvestigationStage.DIAGNOSING
        return InvestigationStage.UNDERSTANDING

    @property
    def verification_complete(self) -> bool:
        return (self.symptom_verified and self.scope_assessed and
                self.timeline_established and self.changes_identified)
```

### 3.2 Case Model Changes

**Key Changes**:
1. ❌ Remove `investigation_state_id` (no separate OODA state)
2. ❌ Remove `current_phase` (deprecated field)
3. ✅ Add `progress: InvestigationProgress`
4. ✅ Add `path_selection: Optional[PathSelection]`
5. ✅ Update `status` to use simplified 4-state model

**Migration**:
```python
# faultmaven/models/case.py

class Case(BaseModel):
    # REMOVE these OODA-related fields:
    # - diagnostic_state.investigation_state_id
    # - diagnostic_state.current_phase
    # - diagnostic_state.turns_in_current_phase

    # ADD milestone-based fields:
    progress: InvestigationProgress = Field(default_factory=InvestigationProgress)
    path_selection: Optional[PathSelection] = None

    # KEEP but simplify:
    status: CaseStatus  # Still CONSULTING/INVESTIGATING/RESOLVED/CLOSED
    evidence: List[Evidence]
    hypotheses: Dict[str, Hypothesis]  # Now optional, not required
    working_conclusion: Optional[WorkingConclusion]

    @property
    def current_stage(self) -> Optional[InvestigationStage]:
        """Computed property - only when INVESTIGATING"""
        if self.status != CaseStatus.INVESTIGATING:
            return None
        return self.progress.current_stage
```

---

## 4. Migration Execution Plan

### Phase 1: Core Models (Week 1)

**Goal**: Replace data models with milestone-based versions

**Tasks**:
1. ✅ Create new `InvestigationProgress` model in `models/case.py`
2. ✅ Add `PathSelection` and path matrix logic
3. ✅ Update `Case` model to use new progress tracking
4. ✅ Remove deprecated fields from `CaseDiagnosticState`
5. ✅ Update `TurnProgress` to track milestone completions

**Files Changed**:
- `faultmaven/models/case.py`
- `faultmaven/models/investigation.py` (deprecate OODA models)

**Tests**:
```bash
pytest tests/models/test_case_models.py -v
```

### Phase 2: Delete OODA Engine (Week 1)

**Goal**: Remove phase-based orchestration

**Tasks**:
1. ❌ Delete `faultmaven/core/investigation/ooda_engine.py`
2. ❌ Delete `faultmaven/core/investigation/phases.py`
3. ❌ Delete `faultmaven/core/investigation/phase_loopback.py`
4. ❌ Delete `faultmaven/core/investigation/iteration_strategy.py`
5. ❌ Delete `faultmaven/core/investigation/ooda_step_extraction.py`

**Validation**:
```bash
# Ensure no imports reference deleted modules
grep -r "from.*ooda_engine" faultmaven/
grep -r "from.*phases" faultmaven/
```

### Phase 3: Refactor Core Logic (Week 2)

**Goal**: Update investigation coordinator to milestone-based

**Tasks**:
1. ✏️ Refactor `investigation_coordinator.py`:
   - Remove phase transition logic
   - Add milestone completion detection
   - Update to use `progress` instead of `current_phase`

2. ✏️ Refactor `hypothesis_manager.py`:
   - Make hypothesis generation **optional**
   - Remove "Phase 3" requirement
   - Allow hypotheses at any time

3. ✏️ Refactor `strategy_selector.py`:
   - Implement path selection matrix (temporal_state × urgency)
   - Remove phase-based strategy

4. ✏️ Update `working_conclusion_generator.py`:
   - Use milestone completions instead of phase
   - Update confidence calculation

**Files Changed**:
- `faultmaven/core/investigation/investigation_coordinator.py`
- `faultmaven/core/investigation/hypothesis_manager.py`
- `faultmaven/core/investigation/strategy_selector.py`
- `faultmaven/core/investigation/working_conclusion_generator.py`

### Phase 4: Update Prompt System (Week 2-3)

**Goal**: Implement milestone-based prompts

**Tasks**:
1. ✅ Create prompt templates from `docs/architecture/prompt-implementation-examples.md`
2. ✅ Implement prompt builder with adaptive instructions
3. ✅ Create LLM response schemas (CONSULTING, INVESTIGATING, TERMINAL)
4. ✅ Implement response processors

**New Files**:
- `faultmaven/agent/prompts/templates.py` (from Part 2 of doc)
- `faultmaven/agent/prompts/builder.py`
- `faultmaven/agent/llm/schemas.py` (response schemas)
- `faultmaven/agent/processors/consulting.py`
- `faultmaven/agent/processors/investigating.py`
- `faultmaven/agent/processors/terminal.py`

**Approach**: Follow implementation guide in `prompt-implementation-examples.md`

### Phase 5: Update Tests (Week 3)

**Goal**: Replace phase-based tests with milestone-based

**Tasks**:
1. Update test fixtures to use new models
2. Replace phase transition tests with milestone completion tests
3. Add one-turn resolution test scenarios
4. Test optional hypothesis workflow

**Key Test Scenarios**:
```python
def test_one_turn_resolution():
    """User provides complete diagnostic data in one message"""
    case = create_case(status=CaseStatus.INVESTIGATING)

    # User uploads comprehensive error log
    result = agent.process_turn(
        case=case,
        user_message="Here's the error log with stack trace and timeline",
        attachments=["comprehensive_log.txt"]
    )

    # Agent completes ALL milestones in one turn
    assert result.progress.symptom_verified == True
    assert result.progress.timeline_established == True
    assert result.progress.root_cause_identified == True
    assert result.progress.solution_proposed == True
    assert result.status == CaseStatus.RESOLVED
```

### Phase 6: Integration & Cleanup (Week 4)

**Goal**: End-to-end testing and cleanup

**Tasks**:
1. Integration testing with full case lifecycle
2. Remove all deprecated OODA references
3. Update documentation
4. Performance testing

---

## 5. Risk Mitigation

### 5.1 High-Risk Areas

| Risk | Mitigation |
|------|-----------|
| **Prompt regression** | Comprehensive prompt testing with real scenarios |
| **Missing milestone detection** | Extensive test coverage for edge cases |
| **Agent confusion** | Clear prompt instructions about opportunistic completion |
| **Integration breaks** | Incremental migration with testing at each phase |

### 5.2 Rollback Plan

Since there's **no production data**, rollback is simple:
```bash
git checkout main  # Revert to pre-migration state
```

---

## 6. Success Criteria

### 6.1 Functional Requirements

- ✅ Agent can complete multiple milestones in one turn
- ✅ One-turn resolution works when user provides complete data
- ✅ Hypothesis generation is optional
- ✅ Path selection uses temporal_state × urgency matrix
- ✅ No phase-based workflow constraints

### 6.2 Test Coverage

- ✅ Unit tests: 80%+ coverage on new models
- ✅ Integration tests: Full case lifecycle scenarios
- ✅ Prompt tests: All status transitions and edge cases

### 6.3 Performance

- ✅ Token usage reduced (simpler state model)
- ✅ Response time unchanged or improved
- ✅ Memory footprint reduced (no OODA iteration history)

---

## 7. Implementation Checklist

### Week 1: Foundation
- [ ] Create `InvestigationProgress` model
- [ ] Add `PathSelection` model
- [ ] Update `Case` model
- [ ] Delete OODA engine modules
- [ ] Run model tests

### Week 2: Core Logic
- [ ] Refactor investigation coordinator
- [ ] Update hypothesis manager (make optional)
- [ ] Implement path selection matrix
- [ ] Update working conclusion generator
- [ ] Run integration tests

### Week 3: Prompts & Processors
- [ ] Implement prompt templates
- [ ] Create response schemas
- [ ] Build response processors
- [ ] Test full conversation flows

### Week 4: Polish & Deploy
- [ ] Update all tests
- [ ] Remove deprecated code
- [ ] Update documentation
- [ ] Final integration testing

---

## 8. Open Questions

### For Discussion

1. **Memory Management**: Current OODA uses hierarchical hot/warm/cold memory. Do we keep this or simplify?
   - **Recommendation**: Keep turn-based memory, remove OODA iteration-specific parts

2. **Engagement Modes**: Current has Consultant ↔ Lead Investigator switching. Map to CONSULTING → INVESTIGATING?
   - **Recommendation**: Yes, simplify to status transitions

3. **Degraded Mode**: Keep confidence cap system or simplify?
   - **Recommendation**: Keep, it's orthogonal to workflow (applies to milestone-based too)

4. **Evidence Requests**: Current mention_count tracking still valid?
   - **Recommendation**: Yes, keep evidence model unchanged

---

## 9. Next Steps

**Immediate Action**:
1. Review this migration plan
2. Confirm approach (clean replacement vs gradual migration)
3. Start with Phase 1 (Core Models)

**Decision Needed**:
- Should we create a `feature/milestone-migration` branch or work on `main`?
- Any modules you want to preserve that aren't listed?

**Ready to Start**: I can begin implementing Phase 1 (Core Models) immediately upon your approval.

---

## Appendix A: File Deletion List

**Safe to Delete** (OODA-specific, no longer needed):
```
faultmaven/core/investigation/ooda_engine.py
faultmaven/core/investigation/phases.py
faultmaven/core/investigation/phase_loopback.py
faultmaven/core/investigation/iteration_strategy.py
faultmaven/core/investigation/ooda_step_extraction.py
tests/core/investigation/test_workflow_progression_detector.py (refactor instead)
```

**Models to Deprecate** (keep file, mark fields deprecated):
```python
# faultmaven/models/investigation.py
class InvestigationState(BaseModel):
    # Mark as deprecated, provide migration path
    _deprecated = True
    _use_instead = "Case.progress (InvestigationProgress)"
```

---

## Appendix B: Comparison Table

| Feature | OODA Framework | Milestone-Based | Migration Complexity |
|---------|---------------|----------------|---------------------|
| Progress Tracking | 7 phases (0-6) | 8 milestones | 🟡 Medium |
| Workflow Control | Phase transitions | Data availability | 🔴 High |
| One-Turn Resolution | ❌ Impossible | ✅ Supported | 🟡 Medium |
| Hypothesis Workflow | Required Phase 3 | Optional anytime | 🟢 Low |
| Memory System | OODA iterations | Turn-based | 🟡 Medium |
| Engagement Modes | Consultant ↔ Lead | CONSULTING → INVESTIGATING | 🟢 Low |
| Path Selection | Phase-based | Matrix (temporal × urgency) | 🟡 Medium |
| State Complexity | High (5 layers) | Low (flat milestones) | 🔴 High |
| LLM Prompts | Phase-specific | Status + milestones | 🔴 High |

**Legend**: 🟢 Low complexity | 🟡 Medium complexity | 🔴 High complexity

---

**Document Version**: 1.0
**Last Updated**: 2025-11-04
**Status**: Draft - Awaiting Approval
