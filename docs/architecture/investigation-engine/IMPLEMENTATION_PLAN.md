# Investigation Workflow Implementation Plan

> **Purpose**: Complete the FaultMaven investigation workflow by filling all gaps between the target design and current implementation.
>
> **Created**: 2026-02-01
> **Updated**: 2026-02-01 (v2 - aligned with corrected design docs)
> **Status**: Ready for Implementation

---

## Executive Summary

This plan addresses **10 implementation gaps** across 4 phases:
1. **Phase 1**: Core Components (4 items) - Critical/High priority
2. **Phase 2**: Data Model Alignment (2 items) - Medium priority
3. **Phase 3**: Error Handling & Recovery (2 items) - High/Medium priority
4. **Phase 4**: Testing & Documentation (2 items) - High/Medium priority

**Estimated Effort**: ~35-50 hours of development
**Priority Order**: Critical → High → Medium

---

## Design Documents Reference

The implementation follows these design specifications:

| Document | Purpose |
|----------|---------|
| `investigation-data-models.md` | Data structures, field naming conventions |
| `error-handling-and-recovery.md` | Error handling, state validation, stagnation detection |
| `investigation-lifecycle-logic.md` | Status transitions, path routing |
| `opportunistic-investigation-framework.md` | Core architecture principles |

---

## Phase 1: Core Components

### 1.1 Integrate WorkingConclusionGenerator into MilestoneEngine

**Priority**: Critical
**Effort**: 4-6 hours
**Location**: `core/investigation/milestone_engine.py`

**Current State**:
- `working_conclusion_generator.py` exists with `generate_working_conclusion()` and `calculate_progress_metrics()`
- Functions are never called from `MilestoneEngine.process_turn()`
- Progress metrics (momentum, blocked reasons) are not tracked

**Implementation Tasks**:

```python
# File: core/investigation/milestone_engine.py

# Task 1.1.1: Import working conclusion generator
from faultmaven.core.investigation.working_conclusion_generator import (
    generate_working_conclusion,
    calculate_progress_metrics,
    InvestigationMomentum,
)

# Task 1.1.2: Add to process_turn() after milestone updates (around line 450)
async def process_turn(self, case: Case, user_message: str) -> dict:
    # ... existing code ...

    # After milestone updates, calculate progress metrics
    progress_metrics = calculate_progress_metrics(
        investigation_state=self._build_investigation_state(case),
        current_turn=case.current_turn
    )

    # Store in turn progress
    turn_progress.momentum = progress_metrics.investigation_momentum
    turn_progress.blocked_reasons = progress_metrics.blocked_reasons
    turn_progress.next_steps = progress_metrics.next_steps

    # Generate working conclusion if significant progress
    if metadata["milestones_completed"] or progress_metrics.momentum == InvestigationMomentum.HIGH:
        working_conclusion = generate_working_conclusion(
            case=case,
            progress_metrics=progress_metrics
        )
        case.working_conclusion = working_conclusion

    # ... rest of existing code ...
```

**Acceptance Criteria**:
- [ ] `calculate_progress_metrics()` called every turn
- [ ] `InvestigationMomentum` tracked in `TurnProgress`
- [ ] Working conclusion generated when milestones complete
- [ ] Unit tests pass for new integration

---

### 1.2 Implement StateValidator Class

**Priority**: Critical
**Effort**: 4-6 hours
**Location**: `core/investigation/state_validator.py` (new file)

**Design Reference**: `error-handling-and-recovery.md` Section 4

**Current State**:
- No formal state validation
- Inconsistent state possible (e.g., solution_verified=True without solution_proposed=True)

**Implementation**:

The StateValidator is fully specified in `error-handling-and-recovery.md`. Implementation should:

1. Create `core/investigation/state_validator.py` with:
   - `ValidationSeverity` enum (WARNING, ERROR, CRITICAL)
   - `ValidationIssue` dataclass
   - `StateValidator` class with methods:
     - `validate_case()` - run all validations
     - `_validate_milestone_ordering()` - check milestone dependencies
     - `_validate_status_consistency()` - ensure status matches progress
     - `_validate_hypothesis_states()` - validate hypothesis lifecycle
     - `_validate_evidence_links()` - check for dangling references
     - `_validate_likelihood_bounds()` - ensure values in [0, 1]
     - `is_valid()` - check if any blocking issues exist

2. Integrate into `MilestoneEngine.process_turn()`:
   ```python
   # At start of process_turn
   is_valid, issues = self.state_validator.is_valid(case)
   if not is_valid:
       for issue in issues:
           logger.warning(f"State issue: {issue.code} - {issue.message}")

   # At end of process_turn
   is_valid, issues = self.state_validator.is_valid(case)
   metadata["validation_issues"] = [asdict(i) for i in issues]
   ```

**Acceptance Criteria**:
- [ ] `StateValidator` class implemented per spec
- [ ] Milestone ordering validated
- [ ] Status consistency validated
- [ ] Likelihood bounds validated
- [ ] Integrated into `MilestoneEngine.process_turn()`
- [ ] Unit tests cover all validation scenarios

---

### 1.3 Implement StagnationDetector Class

**Priority**: High
**Effort**: 4-6 hours
**Location**: `core/investigation/stagnation_detector.py` (new file)

**Design Reference**: `error-handling-and-recovery.md` Section 5

**Current State**:
- `turns_without_progress` tracked but not used for stagnation detection
- Anchoring detection exists in `hypothesis_manager.py` but not integrated
- No action loop detection

**Implementation**:

1. Create `core/investigation/stagnation_detector.py` with:
   - `StagnationType` enum (NO_PROGRESS, HYPOTHESIS_ANCHORING, ACTION_LOOP, HYPOTHESIS_DEADLOCK)
   - `StagnationDetector` class with:
     - `detect_stagnation()` - main detection method
     - `_detect_category_anchoring()` - 4+ failed hypotheses in same category
     - `_detect_action_loop()` - same actions repeated 5+ times
     - `_detect_hypothesis_deadlock()` - all hypotheses inconclusive
   - `StagnationBreaker` class with:
     - `break_stagnation()` - determine action to break out
     - `_handle_no_progress()` - enter degraded mode
     - `_handle_anchoring()` - force alternative category
     - `_handle_action_loop()` - request user input
     - `_handle_deadlock()` - retire and regenerate hypotheses

2. Integrate into `MilestoneEngine.process_turn()`:
   ```python
   # After turn processing
   stagnation_type = self.stagnation_detector.detect_stagnation(case)
   if stagnation_type:
       breakout = self.stagnation_breaker.break_stagnation(case, stagnation_type)
       metadata["stagnation_detected"] = stagnation_type.value
       metadata["breakout_action"] = breakout.action
       if breakout.prompt_injection:
           # Add to next turn's system message
           case.system_feedback = breakout.prompt_injection
   ```

**Acceptance Criteria**:
- [ ] `StagnationDetector` class implemented
- [ ] All 4 stagnation types detected
- [ ] `StagnationBreaker` class implemented
- [ ] Integrated into `MilestoneEngine`
- [ ] Unit tests for detection and breakout

---

### 1.4 Implement Path Selection Integration

**Priority**: High
**Effort**: 2-3 hours
**Location**: `core/investigation/milestone_engine.py`

**Design Reference**: `investigation-lifecycle-logic.md`, `investigation-data-models.md` Section 1.4

**Current State**:
- `investigation_router.py` exists with `determine_investigation_path()`
- Path selection not integrated into MilestoneEngine prompt generation

**Implementation**:

1. Call `determine_investigation_path()` when transitioning INQUIRY → INVESTIGATING:
   ```python
   # In _check_automatic_transitions()
   if should_transition_to_investigating:
       path_selection = determine_investigation_path(case.problem_verification)
       case.path_selection = path_selection
   ```

2. Include path guidance in prompt context:
   ```python
   # In prompts/context_builder.py
   if case.path_selection:
       context["investigation_path"] = case.path_selection.path.value
       context["path_rationale"] = case.path_selection.rationale
       if case.path_selection.path == InvestigationPath.MITIGATION_FIRST:
           context["mitigation_guidance"] = "Mitigation can be applied during stages 1-2 if correlation is strong."
   ```

**Acceptance Criteria**:
- [ ] Path selection called on status transition
- [ ] Path stored in case.path_selection
- [ ] Path guidance included in prompts
- [ ] Unit tests for path integration

---

## Phase 2: Data Model Alignment

### 2.1 Standardize Field Naming in Implementation

**Priority**: Medium
**Effort**: 3-4 hours

**Design Reference**: `investigation-data-models.md` Field Naming Conventions

**Issues to Fix**:

| Current Name | Standard Name | Location |
|--------------|---------------|----------|
| `solution_applied` vs `resolution_applied` | `solution_applied` | Multiple files |
| `completeness` | `stance_confidence` | `HypothesisEvidenceLink` |
| `root_cause_confidence` | `root_cause_likelihood` | `InvestigationProgress` |
| `confidence_score` | `likelihood` | Various models |

**Tasks**:

1. **Audit all occurrences**:
   ```bash
   grep -rn "resolution_applied" faultmaven/
   grep -rn "completeness" faultmaven/ | grep -i evidence
   grep -rn "root_cause_confidence" faultmaven/
   grep -rn "confidence_score" faultmaven/
   ```

2. **Update in priority order**:
   - `modules/case/contracts.py` - primary definitions
   - `modules/case/domain/models.py` - domain models
   - `core/investigation/schemas.py` - LLM output schemas
   - `core/investigation/milestone_engine.py` - engine code
   - `core/investigation/hypothesis_manager.py` - hypothesis code

3. **Add migration if needed** for database fields

**Acceptance Criteria**:
- [ ] All field names match specification
- [ ] No duplicate/conflicting field names
- [ ] All tests pass after rename
- [ ] Database migration created if needed

---

### 2.2 Align Stage Enum in Implementation

**Priority**: Medium
**Effort**: 2-3 hours

**Design Reference**: `investigation-data-models.md` Section 1.2

**Current State**:
- Implementation may use user-facing names (UNDERSTANDING, DIAGNOSING, RESOLVING)
- Design specifies internal enum (SYMPTOM_VERIFICATION, HYPOTHESIS_FORMULATION, etc.)

**Tasks**:

1. Verify `InvestigationStage` enum matches spec:
   ```python
   class InvestigationStage(str, Enum):
       SYMPTOM_VERIFICATION = "symptom_verification"
       HYPOTHESIS_FORMULATION = "hypothesis_formulation"
       HYPOTHESIS_VALIDATION = "hypothesis_validation"
       SOLUTION = "solution"
   ```

2. Add `stage_display_name` property to `InvestigationProgress` if not present

3. Update any code using wrong stage names

**Acceptance Criteria**:
- [ ] `InvestigationStage` enum has 4 values
- [ ] `current_stage` property returns enum values
- [ ] `stage_display_name` property returns user-friendly names
- [ ] All stage references use correct enum

---

## Phase 3: Error Handling & Recovery

### 3.1 Implement Retry with Exponential Backoff

**Priority**: High
**Effort**: 3-4 hours
**Location**: `core/investigation/resilience.py` (new file)

**Design Reference**: `error-handling-and-recovery.md` Section 2

**Implementation**:

```python
# File: core/investigation/resilience.py

from dataclasses import dataclass
from typing import TypeVar, Callable, Awaitable, Tuple
import asyncio

T = TypeVar('T')

@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0
    exponential_base: float = 2.0
    retryable_errors: Tuple[type, ...] = (Exception,)

async def retry_with_backoff(
    func: Callable[..., Awaitable[T]],
    config: RetryConfig = None,
    *args,
    **kwargs
) -> T:
    """Execute function with exponential backoff retry."""
    config = config or RetryConfig()
    last_exception = None

    for attempt in range(1, config.max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except config.retryable_errors as e:
            last_exception = e
            if attempt == config.max_retries:
                raise

            delay = min(
                config.base_delay_seconds * (config.exponential_base ** (attempt - 1)),
                config.max_delay_seconds
            )
            logger.warning(f"Attempt {attempt} failed: {e}. Retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)

    raise last_exception
```

**Integration** (in `milestone_engine.py`):

```python
from faultmaven.core.investigation.resilience import retry_with_backoff, RetryConfig

# LLM call with retry
llm_config = RetryConfig(
    max_retries=3,
    base_delay_seconds=2.0,
    retryable_errors=(RateLimitError, TimeoutError, ConnectionError)
)

response = await retry_with_backoff(
    self.llm.generate_structured,
    llm_config,
    prompt=prompt,
    schema=schema
)
```

**Acceptance Criteria**:
- [ ] `retry_with_backoff` function implemented
- [ ] Exponential backoff with configurable parameters
- [ ] Integrated into LLM calls in MilestoneEngine
- [ ] Logging for retry attempts
- [ ] Unit tests for retry behavior

---

### 3.2 Implement Fallback Prompts

**Priority**: Medium
**Effort**: 3-4 hours
**Location**: `core/investigation/prompts/fallback_templates.py` (new file)

**Design Reference**: `error-handling-and-recovery.md` Section 6 (RecoveryManager)

**Implementation**:

```python
# File: core/investigation/prompts/fallback_templates.py

"""Fallback prompt templates for error recovery.

Used when:
1. Primary prompt exceeds token limit
2. LLM fails to produce valid structured output
3. Investigation is stuck (degraded mode)
"""

FALLBACK_INQUIRY_TEMPLATE = """You are an investigation assistant.
The user has reported an issue. Understand and confirm the problem.

Issue: {problem_description}

Respond with JSON:
{
  "agent_response": "your response to the user",
  "state_updates": {
    "problem_confirmation": {
      "problem_type": "error|slowness|unavailability|data_issue|other",
      "severity_guess": "critical|high|medium|low"
    }
  }
}
"""

FALLBACK_INVESTIGATING_TEMPLATE = """You are investigating an issue.

Problem: {problem_statement}
Evidence count: {evidence_count}
Hypotheses: {hypothesis_count}

What is your next step? Respond with JSON:
{
  "agent_response": "your response",
  "state_updates": {
    "milestones": {},
    "outcome": "milestone_completed|data_requested|conversation"
  }
}
"""

FALLBACK_STUCK_TEMPLATE = """The investigation has not progressed in {turns_without_progress} turns.

Current state:
- Problem: {problem_statement}
- Completed milestones: {completed_milestones}

Suggest a different approach or ask for clarification.
Keep response brief and actionable.
"""

class FallbackPromptSelector:
    """Selects appropriate fallback prompt."""

    @staticmethod
    def get_fallback_prompt(status: str, context: dict) -> str:
        if status == "inquiry":
            return FALLBACK_INQUIRY_TEMPLATE.format(**context)
        elif context.get("turns_without_progress", 0) >= 3:
            return FALLBACK_STUCK_TEMPLATE.format(**context)
        else:
            return FALLBACK_INVESTIGATING_TEMPLATE.format(**context)
```

**Integration** (in `milestone_engine.py`):

```python
async def process_turn(self, case, user_message):
    try:
        response = await self._call_llm_with_primary_prompt(...)
    except (TokenLimitError, ValidationError) as e:
        logger.warning(f"Primary prompt failed: {e}, using fallback")
        fallback_prompt = FallbackPromptSelector.get_fallback_prompt(
            status=case.status.value,
            context=self._build_fallback_context(case)
        )
        response = await self._call_llm_simple(fallback_prompt)
        metadata["used_fallback_prompt"] = True
```

**Acceptance Criteria**:
- [ ] Fallback templates for each status
- [ ] `FallbackPromptSelector` implemented
- [ ] Fallback triggered on token limit or validation errors
- [ ] Simpler schema for fallback responses
- [ ] Integration tested

---

## Phase 4: Testing & Documentation

### 4.1 Add Unit Tests for New Components

**Priority**: High
**Effort**: 6-8 hours

**Test Files to Create**:

```
tests/unit/core/investigation/
├── test_state_validator.py
├── test_stagnation_detector.py
├── test_resilience.py
└── test_fallback_prompts.py
```

**Test Coverage Targets**:
- StateValidator: 90%+
- StagnationDetector: 90%+
- Resilience utilities: 95%+
- FallbackPromptSelector: 85%+

**Key Test Scenarios**:

1. **StateValidator**:
   - Valid case state
   - Milestone ordering violations
   - Status consistency violations
   - Dangling evidence links
   - Likelihood out of bounds

2. **StagnationDetector**:
   - No progress detection (3+ turns)
   - Category anchoring (4+ same category)
   - Action loop (5+ same actions)
   - Hypothesis deadlock (all inconclusive)

3. **Resilience**:
   - Successful on first try
   - Successful after retries
   - Max retries exceeded
   - Backoff timing correct

---

### 4.2 Create Architecture Decision Record

**Priority**: Medium
**Effort**: 2-3 hours
**Location**: `docs/architecture/decisions/ADR-XXX-milestone-based-investigation.md`

**Content**:

```markdown
# ADR-XXX: Milestone-Based Investigation Framework

## Status
Accepted

## Context
The original design referenced an OODA (Observe, Orient, Decide, Act) loop
for investigation orchestration. During implementation, this evolved to a
milestone-based approach.

## Decision
Replace OODA-based architecture with milestone-based architecture:
- Remove phase numbers (0-6)
- Use 4 CaseStatus values: INQUIRY, INVESTIGATING, RESOLVED, CLOSED
- Use 4 InvestigationStage values for optional progress detail
- Track progress via 9 boolean milestones
- Allow opportunistic milestone completion (multiple per turn)

## Consequences
- Simpler mental model for developers
- More flexible progress tracking
- Easier to extend with new milestones
- All error-handling docs updated to v2.0
```

---

## Implementation Schedule

| Phase | Component | Priority | Effort | Dependencies |
|-------|-----------|----------|--------|--------------|
| 1.1 | WorkingConclusionGenerator | Critical | 4-6h | None |
| 1.2 | StateValidator | Critical | 4-6h | None |
| 1.3 | StagnationDetector | High | 4-6h | None |
| 1.4 | Path Selection | High | 2-3h | None |
| 2.1 | Field naming | Medium | 3-4h | None |
| 2.2 | Stage enum | Medium | 2-3h | 2.1 |
| 3.1 | Retry with backoff | High | 3-4h | None |
| 3.2 | Fallback prompts | Medium | 3-4h | 3.1 |
| 4.1 | Unit tests | High | 6-8h | 1.x, 3.x |
| 4.2 | ADR | Medium | 2-3h | All |

**Total Estimated Effort**: 35-50 hours

---

## Success Criteria

The implementation is complete when:

1. **All core components implemented**:
   - [ ] WorkingConclusionGenerator integrated and generating conclusions
   - [ ] StateValidator validating every turn
   - [ ] StagnationDetector identifying stalls and triggering breakouts
   - [ ] Path selection integrated into status transitions

2. **Data models aligned**:
   - [ ] All field names match specification
   - [ ] Stage enum has correct 4 values
   - [ ] Computed properties return correct types

3. **Error handling robust**:
   - [ ] LLM calls retry on transient failures
   - [ ] Fallback prompts used when primary fails

4. **Tests passing**:
   - [ ] All new unit tests pass
   - [ ] No regression in existing tests
   - [ ] Coverage targets met

5. **Documentation updated**:
   - [ ] CLAUDE.md reflects actual implementation ✅
   - [ ] Design docs match code ✅
   - [ ] ADR documents design evolution

---

## Changes from v1

This plan was revised to align with corrected design documentation:

| Change | Reason |
|--------|--------|
| Removed MemoryManager | Not specified in design; memory is part of context_builder |
| Simplified StrategySelector → Path Selection | Design uses simpler path routing, not full strategy system |
| Updated StateValidator | Now validates milestones, not phases/OODA |
| Added StagnationDetector | Extracted from error-handling as separate component |
| Updated field naming list | Based on new Field Naming Conventions section |

---

**END OF DOCUMENT**
