# Investigation Workflow Implementation Plan

> **Purpose**: Complete the FaultMaven investigation workflow by filling all gaps between the target design and current implementation.
>
> **Created**: 2026-02-01
> **Status**: Ready for Implementation

---

## Executive Summary

This plan addresses **12 implementation gaps** across 5 categories:
1. Missing Components (4 items)
2. Partial Implementations (3 items)
3. Data Model Gaps (2 items)
4. Error Handling & Recovery (2 items)
5. Documentation Sync (1 item)

**Estimated Effort**: ~40-60 hours of development
**Priority Order**: Critical → High → Medium → Low

---

## Phase 1: Critical Missing Components

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
**Effort**: 6-8 hours
**Location**: `core/investigation/state_validator.py` (new file)

**Current State**:
- No formal state validation
- Inconsistent state possible (e.g., solution_verified=True without solution_proposed=True)
- No loop detection (repeated same actions)

**Implementation Tasks**:

```python
# File: core/investigation/state_validator.py (NEW)

"""State Validator for Investigation Engine.

Validates case state consistency and detects investigation loops.
Reference: docs/architecture/investigation-engine/error-handling-and-recovery.md
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple
from faultmaven.modules.case.contracts import Case, CaseStatus, InvestigationProgress

class ValidationSeverity(str, Enum):
    """Severity of validation issues."""
    WARNING = "warning"      # Non-blocking, log only
    ERROR = "error"          # Blocking, requires correction
    CRITICAL = "critical"    # Severe inconsistency, investigation should pause

@dataclass
class ValidationIssue:
    """Single validation issue."""
    code: str
    message: str
    severity: ValidationSeverity
    field: Optional[str] = None
    suggested_fix: Optional[str] = None

class StateValidator:
    """Validates investigation state consistency."""

    def __init__(self, loop_detection_threshold: int = 5):
        self.loop_detection_threshold = loop_detection_threshold

    def validate_case(self, case: Case) -> List[ValidationIssue]:
        """Run all validations on a case."""
        issues = []
        issues.extend(self.validate_milestone_ordering(case.progress))
        issues.extend(self.validate_status_consistency(case))
        issues.extend(self.validate_hypothesis_states(case))
        issues.extend(self.validate_evidence_links(case))
        issues.extend(self.detect_investigation_loop(case))
        return issues

    def validate_milestone_ordering(self, progress: InvestigationProgress) -> List[ValidationIssue]:
        """Ensure milestone dependencies are respected."""
        issues = []

        # solution_verified requires solution_proposed
        if progress.solution_verified and not progress.solution_proposed:
            issues.append(ValidationIssue(
                code="MILESTONE_ORDER_001",
                message="solution_verified=True but solution_proposed=False",
                severity=ValidationSeverity.ERROR,
                field="solution_verified",
                suggested_fix="Set solution_proposed=True or reset solution_verified=False"
            ))

        # solution_applied requires solution_proposed
        if progress.solution_applied and not progress.solution_proposed:
            issues.append(ValidationIssue(
                code="MILESTONE_ORDER_002",
                message="solution_applied=True but solution_proposed=False",
                severity=ValidationSeverity.ERROR,
                field="solution_applied",
                suggested_fix="Set solution_proposed=True"
            ))

        # root_cause_identified should have likelihood
        if progress.root_cause_identified and progress.root_cause_likelihood is None:
            issues.append(ValidationIssue(
                code="MILESTONE_INCOMPLETE_001",
                message="root_cause_identified=True but root_cause_likelihood is None",
                severity=ValidationSeverity.WARNING,
                field="root_cause_likelihood",
                suggested_fix="Set root_cause_likelihood to confidence value"
            ))

        return issues

    def validate_status_consistency(self, case: Case) -> List[ValidationIssue]:
        """Ensure status matches progress state."""
        issues = []

        # RESOLVED requires solution_verified
        if case.status == CaseStatus.RESOLVED and not case.progress.solution_verified:
            issues.append(ValidationIssue(
                code="STATUS_MISMATCH_001",
                message="Status is RESOLVED but solution_verified=False",
                severity=ValidationSeverity.ERROR,
                field="status",
                suggested_fix="Either set solution_verified=True or change status to INVESTIGATING"
            ))

        # INVESTIGATING should have problem_statement
        if case.status == CaseStatus.INVESTIGATING and not case.problem_statement:
            issues.append(ValidationIssue(
                code="STATUS_MISMATCH_002",
                message="Status is INVESTIGATING but problem_statement is empty",
                severity=ValidationSeverity.WARNING,
                field="problem_statement",
                suggested_fix="Set problem_statement from verification data"
            ))

        return issues

    def validate_hypothesis_states(self, case: Case) -> List[ValidationIssue]:
        """Validate hypothesis lifecycle states."""
        issues = []

        for hyp_id, hypothesis in case.hypotheses.items():
            # VALIDATED requires sufficient evidence
            if hypothesis.status.value == "validated":
                supporting = sum(1 for link in hypothesis.evidence_links.values()
                               if link.stance.value == "supports")
                if supporting < 2:
                    issues.append(ValidationIssue(
                        code="HYPOTHESIS_STATE_001",
                        message=f"Hypothesis {hyp_id} is VALIDATED with only {supporting} supporting evidence",
                        severity=ValidationSeverity.WARNING,
                        field=f"hypotheses.{hyp_id}",
                        suggested_fix="Require at least 2 supporting evidence items"
                    ))

            # Likelihood bounds
            if hypothesis.likelihood < 0.0 or hypothesis.likelihood > 1.0:
                issues.append(ValidationIssue(
                    code="HYPOTHESIS_BOUNDS_001",
                    message=f"Hypothesis {hyp_id} has likelihood {hypothesis.likelihood} outside [0,1]",
                    severity=ValidationSeverity.ERROR,
                    field=f"hypotheses.{hyp_id}.likelihood"
                ))

        return issues

    def validate_evidence_links(self, case: Case) -> List[ValidationIssue]:
        """Validate evidence-hypothesis links are consistent."""
        issues = []

        evidence_ids = {e.id for e in case.evidence}
        hypothesis_ids = set(case.hypotheses.keys())

        for hyp_id, hypothesis in case.hypotheses.items():
            for ev_ref, link in hypothesis.evidence_links.items():
                # Skip new_index references (created same turn)
                if ev_ref.startswith("new_index_"):
                    continue
                if ev_ref not in evidence_ids:
                    issues.append(ValidationIssue(
                        code="LINK_DANGLING_001",
                        message=f"Hypothesis {hyp_id} links to non-existent evidence {ev_ref}",
                        severity=ValidationSeverity.WARNING,
                        field=f"hypotheses.{hyp_id}.evidence_links"
                    ))

        return issues

    def detect_investigation_loop(self, case: Case) -> List[ValidationIssue]:
        """Detect if investigation is stuck in a loop."""
        issues = []

        if len(case.turn_history) < self.loop_detection_threshold:
            return issues

        # Check last N turns for repetitive patterns
        recent_turns = case.turn_history[-self.loop_detection_threshold:]

        # Pattern 1: No milestones completed in N turns
        milestones_in_recent = sum(
            len(t.milestones_completed) for t in recent_turns
        )
        if milestones_in_recent == 0 and case.turns_without_progress >= self.loop_detection_threshold:
            issues.append(ValidationIssue(
                code="LOOP_DETECTED_001",
                message=f"No milestones completed in last {self.loop_detection_threshold} turns",
                severity=ValidationSeverity.WARNING,
                suggested_fix="Consider triggering degraded mode or requesting user clarification"
            ))

        # Pattern 2: Same actions repeated
        action_sequences = [tuple(t.actions_taken) for t in recent_turns if t.actions_taken]
        if len(action_sequences) >= 3:
            unique_sequences = set(action_sequences)
            if len(unique_sequences) == 1:
                issues.append(ValidationIssue(
                    code="LOOP_DETECTED_002",
                    message="Same action sequence repeated in recent turns",
                    severity=ValidationSeverity.WARNING,
                    suggested_fix="Agent may be stuck; consider alternative approach"
                ))

        return issues

    def is_valid(self, case: Case) -> Tuple[bool, List[ValidationIssue]]:
        """Check if case state is valid (no ERROR or CRITICAL issues)."""
        issues = self.validate_case(case)
        blocking = [i for i in issues if i.severity in (ValidationSeverity.ERROR, ValidationSeverity.CRITICAL)]
        return len(blocking) == 0, issues
```

**Integration Point** (in `milestone_engine.py`):

```python
# Add at start of process_turn()
from faultmaven.core.investigation.state_validator import StateValidator

class MilestoneEngine:
    def __init__(self, ...):
        self.state_validator = StateValidator(loop_detection_threshold=5)

    async def process_turn(self, case: Case, user_message: str) -> dict:
        # Validate state before processing
        is_valid, issues = self.state_validator.is_valid(case)
        if not is_valid:
            # Log issues and attempt recovery
            for issue in issues:
                if issue.severity == ValidationSeverity.ERROR:
                    logger.error(f"State validation failed: {issue.code} - {issue.message}")
            # Could trigger recovery or return error

        # ... rest of processing ...

        # Validate state after processing
        is_valid, issues = self.state_validator.is_valid(case)
        metadata["validation_issues"] = [asdict(i) for i in issues]
```

**Acceptance Criteria**:
- [ ] `StateValidator` class created with all validation methods
- [ ] Milestone ordering validated
- [ ] Status consistency validated
- [ ] Loop detection working (5+ same-action threshold)
- [ ] Integrated into `MilestoneEngine.process_turn()`
- [ ] Unit tests cover all validation scenarios

---

### 1.3 Implement Memory Compression for Token Management

**Priority**: High
**Effort**: 8-10 hours
**Location**: `core/investigation/memory/` (new directory)

**Current State**:
- Context builder has token budget (8000 tokens)
- No compression when budget exceeded
- Old messages simply truncated
- No summarization of previous turns

**Implementation Tasks**:

```python
# File: core/investigation/memory/__init__.py (NEW)
from .memory_manager import MemoryManager, MemoryConfig
from .compressor import ConversationCompressor

# File: core/investigation/memory/memory_manager.py (NEW)

"""Memory Manager for Investigation Context.

Manages working memory, case memory, and compressed memory tiers.
Reference: docs/architecture/investigation-engine/prompt-engineering-guide.md
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from datetime import datetime

from faultmaven.modules.case.contracts import Case, TurnProgress
from faultmaven.infrastructure.llm.contracts import ILLMProvider

@dataclass
class MemoryConfig:
    """Configuration for memory management."""
    working_memory_turns: int = 10        # Last N turns in full detail
    compressed_memory_turns: int = 50     # Turns to include in summary
    token_budget: int = 8000              # Max tokens for context
    compression_threshold: float = 0.8    # Compress when at 80% budget
    summary_max_tokens: int = 500         # Max tokens for compressed summary

@dataclass
class MemoryContext:
    """Assembled memory context for prompt."""
    working_memory: List[Dict[str, Any]]      # Recent turns (full)
    compressed_summary: Optional[str]          # Summary of older turns
    key_evidence: List[Dict[str, Any]]         # Important evidence
    active_hypotheses: List[Dict[str, Any]]    # Current hypotheses
    total_tokens: int                          # Estimated token count
    was_compressed: bool                       # Whether compression was applied

class MemoryManager:
    """Manages investigation memory across tiers."""

    def __init__(
        self,
        llm_provider: ILLMProvider,
        config: Optional[MemoryConfig] = None
    ):
        self.llm = llm_provider
        self.config = config or MemoryConfig()
        self._compression_cache: Dict[str, str] = {}  # case_id -> compressed summary

    async def build_context(self, case: Case) -> MemoryContext:
        """Build memory context for prompt generation."""
        # Tier 1: Working Memory (last N turns)
        working_turns = case.turn_history[-self.config.working_memory_turns:]
        working_memory = self._format_turns(working_turns, case.messages)

        # Estimate tokens
        working_tokens = self._estimate_tokens(working_memory)

        # Tier 2: Compressed Memory (if needed)
        compressed_summary = None
        was_compressed = False

        older_turns = case.turn_history[:-self.config.working_memory_turns]
        if older_turns and working_tokens > (self.config.token_budget * self.config.compression_threshold):
            compressed_summary = await self._get_or_generate_summary(case, older_turns)
            was_compressed = True

        # Key evidence (prioritized)
        key_evidence = self._extract_key_evidence(case)

        # Active hypotheses
        active_hypotheses = self._extract_active_hypotheses(case)

        total_tokens = self._calculate_total_tokens(
            working_memory, compressed_summary, key_evidence, active_hypotheses
        )

        return MemoryContext(
            working_memory=working_memory,
            compressed_summary=compressed_summary,
            key_evidence=key_evidence,
            active_hypotheses=active_hypotheses,
            total_tokens=total_tokens,
            was_compressed=was_compressed
        )

    async def _get_or_generate_summary(
        self,
        case: Case,
        turns: List[TurnProgress]
    ) -> str:
        """Get cached summary or generate new one."""
        cache_key = f"{case.id}:{len(turns)}"

        if cache_key in self._compression_cache:
            return self._compression_cache[cache_key]

        summary = await self._compress_turns(turns, case)
        self._compression_cache[cache_key] = summary
        return summary

    async def _compress_turns(
        self,
        turns: List[TurnProgress],
        case: Case
    ) -> str:
        """Use LLM to compress turn history into summary."""
        from faultmaven.core.investigation.memory.compressor import ConversationCompressor

        compressor = ConversationCompressor(self.llm)
        return await compressor.compress(
            turns=turns,
            messages=case.messages,
            max_tokens=self.config.summary_max_tokens
        )

    def _format_turns(
        self,
        turns: List[TurnProgress],
        messages: List[Dict]
    ) -> List[Dict[str, Any]]:
        """Format turns for context inclusion."""
        formatted = []
        for turn in turns:
            turn_messages = [
                m for m in messages
                if m.get("turn_number") == turn.turn_number
            ]
            formatted.append({
                "turn": turn.turn_number,
                "milestones_completed": turn.milestones_completed,
                "outcome": turn.outcome.value if turn.outcome else None,
                "user_summary": turn.user_message_summary,
                "agent_summary": turn.agent_response_summary,
                "messages": turn_messages[-2:] if turn_messages else []  # Last user+agent
            })
        return formatted

    def _extract_key_evidence(self, case: Case) -> List[Dict[str, Any]]:
        """Extract most relevant evidence for context."""
        # Prioritize: high likelihood, recent, linked to active hypotheses
        evidence_list = sorted(
            case.evidence,
            key=lambda e: (e.likelihood, e.created_at or datetime.min),
            reverse=True
        )
        return [
            {
                "id": e.id,
                "summary": e.summary,
                "category": e.category.value if e.category else None,
                "likelihood": e.likelihood
            }
            for e in evidence_list[:10]  # Top 10
        ]

    def _extract_active_hypotheses(self, case: Case) -> List[Dict[str, Any]]:
        """Extract active hypotheses for context."""
        active = [
            h for h in case.hypotheses.values()
            if h.status.value in ("active", "captured")
        ]
        return [
            {
                "id": h.id if hasattr(h, 'id') else None,
                "statement": h.statement,
                "likelihood": h.likelihood,
                "evidence_count": len(h.evidence_links)
            }
            for h in sorted(active, key=lambda h: h.likelihood, reverse=True)
        ]

    def _estimate_tokens(self, content: Any) -> int:
        """Rough token estimation (4 chars ≈ 1 token)."""
        import json
        text = json.dumps(content) if not isinstance(content, str) else content
        return len(text) // 4

    def _calculate_total_tokens(self, *components) -> int:
        """Calculate total tokens across all components."""
        return sum(self._estimate_tokens(c) for c in components if c)


# File: core/investigation/memory/compressor.py (NEW)

"""Conversation Compressor using LLM summarization."""

from typing import List, Dict
from faultmaven.modules.case.contracts import TurnProgress
from faultmaven.infrastructure.llm.contracts import ILLMProvider

COMPRESSION_PROMPT = """Summarize the following investigation history into a concise narrative.
Focus on:
1. Key findings and evidence discovered
2. Hypotheses explored and their outcomes
3. Important decisions made
4. Current state of the investigation

Keep the summary under {max_tokens} tokens. Use bullet points for clarity.

Investigation History:
{history}

Summary:"""

class ConversationCompressor:
    """Compresses conversation history using LLM."""

    def __init__(self, llm_provider: ILLMProvider):
        self.llm = llm_provider

    async def compress(
        self,
        turns: List[TurnProgress],
        messages: List[Dict],
        max_tokens: int = 500
    ) -> str:
        """Compress turns into summary using LLM."""
        # Build history text
        history_parts = []
        for turn in turns:
            turn_text = f"Turn {turn.turn_number}:"
            if turn.milestones_completed:
                turn_text += f" Completed: {', '.join(turn.milestones_completed)}"
            if turn.user_message_summary:
                turn_text += f" User: {turn.user_message_summary}"
            if turn.agent_response_summary:
                turn_text += f" Agent: {turn.agent_response_summary}"
            history_parts.append(turn_text)

        history = "\n".join(history_parts)

        prompt = COMPRESSION_PROMPT.format(
            max_tokens=max_tokens,
            history=history
        )

        response = await self.llm.generate(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=0.3  # Low temp for factual summary
        )

        return response.content
```

**Integration** (update `context_builder.py`):

```python
# In prompts/context_builder.py

from faultmaven.core.investigation.memory import MemoryManager, MemoryConfig

class ContextBuilder:
    def __init__(self, llm_provider: ILLMProvider, config: Optional[MemoryConfig] = None):
        self.memory_manager = MemoryManager(llm_provider, config)

    async def build_context(self, case: Case, ...) -> str:
        memory_context = await self.memory_manager.build_context(case)

        # Use memory_context.working_memory for recent turns
        # Use memory_context.compressed_summary for older context
        # ...
```

**Acceptance Criteria**:
- [ ] `MemoryManager` class manages 3-tier memory
- [ ] `ConversationCompressor` summarizes old turns via LLM
- [ ] Compression triggered when approaching token budget
- [ ] Cache prevents re-compression of unchanged history
- [ ] Integrated into `ContextBuilder`
- [ ] Unit tests for memory management

---

### 1.4 Implement Investigation Strategy Selector

**Priority**: High
**Effort**: 4-6 hours
**Location**: `core/investigation/strategy_selector.py` (new file)

**Current State**:
- `investigation_router.py` exists in `modules/case/domain/services/`
- Only handles path selection (MITIGATION_FIRST vs ROOT_CAUSE)
- No strategy-based behavior modification

**Implementation Tasks**:

```python
# File: core/investigation/strategy_selector.py (NEW)

"""Strategy Selector for Investigation Behavior.

Determines investigation strategy based on context and adjusts
engine behavior accordingly.

Reference: docs/architecture/investigation-engine/investigation-data-models.md
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, List

from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    InvestigationStrategy,
    ProblemVerification,
)
from faultmaven.modules.case.domain.services.investigation_router import (
    determine_investigation_path,
    PathSelection,
)

class EscalationReason(str, Enum):
    """Reasons for escalation recommendation."""
    MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
    HYPOTHESIS_SPACE_EXHAUSTED = "hypothesis_space_exhausted"
    USER_REQUESTED = "user_requested"
    CRITICAL_BLOCKER = "critical_blocker"
    TIME_EXCEEDED = "time_exceeded"

@dataclass
class StrategyConfig:
    """Configuration thresholds for strategy behaviors."""
    # ACTIVE_INCIDENT thresholds
    active_incident_max_turns_before_escalation: int = 10
    active_incident_hypothesis_confidence_threshold: float = 0.5  # Accept TESTING
    active_incident_evidence_threshold: str = "supports"  # Not "strongly_supports"

    # POST_MORTEM thresholds
    post_mortem_hypothesis_confidence_threshold: float = 0.7  # Require VALIDATED
    post_mortem_evidence_threshold: str = "strongly_supports"
    post_mortem_max_hypotheses_before_exhausted: int = 10

@dataclass
class StrategyDecision:
    """Result of strategy selection."""
    strategy: InvestigationStrategy
    path: PathSelection
    config: StrategyConfig

    # Behavior modifiers
    accept_partial_root_cause: bool
    require_validated_hypothesis: bool
    evidence_confidence_threshold: str
    escalation_turn_threshold: Optional[int]

    # Current state assessment
    should_escalate: bool = False
    escalation_reason: Optional[EscalationReason] = None
    alternative_approaches: List[str] = None

class StrategySelector:
    """Selects and configures investigation strategy."""

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()

    def select_strategy(
        self,
        case: Case,
        verification: Optional[ProblemVerification] = None
    ) -> StrategyDecision:
        """Select strategy based on case context."""
        # Determine base strategy from verification data
        if verification:
            strategy = self._determine_from_verification(verification)
        elif case.investigation_strategy:
            strategy = case.investigation_strategy
        else:
            strategy = InvestigationStrategy.POST_MORTEM  # Default: thorough

        # Get path selection
        path = determine_investigation_path(verification) if verification else None

        # Build behavior configuration
        if strategy == InvestigationStrategy.ACTIVE_INCIDENT:
            decision = self._build_active_incident_decision(case, path)
        else:
            decision = self._build_post_mortem_decision(case, path)

        # Check escalation conditions
        self._check_escalation(decision, case)

        return decision

    def _determine_from_verification(
        self,
        verification: ProblemVerification
    ) -> InvestigationStrategy:
        """Determine strategy from problem verification."""
        # ONGOING + CRITICAL/HIGH = ACTIVE_INCIDENT
        is_ongoing = verification.temporal_state == "ongoing"
        is_urgent = verification.urgency_level in ("critical", "high")

        if is_ongoing and is_urgent:
            return InvestigationStrategy.ACTIVE_INCIDENT

        return InvestigationStrategy.POST_MORTEM

    def _build_active_incident_decision(
        self,
        case: Case,
        path: Optional[PathSelection]
    ) -> StrategyDecision:
        """Build decision for active incident strategy."""
        return StrategyDecision(
            strategy=InvestigationStrategy.ACTIVE_INCIDENT,
            path=path,
            config=self.config,
            accept_partial_root_cause=True,
            require_validated_hypothesis=False,  # Accept TESTING status
            evidence_confidence_threshold=self.config.active_incident_evidence_threshold,
            escalation_turn_threshold=self.config.active_incident_max_turns_before_escalation,
            alternative_approaches=[
                "Try known mitigation from runbook",
                "Rollback recent changes",
                "Escalate to on-call engineer"
            ]
        )

    def _build_post_mortem_decision(
        self,
        case: Case,
        path: Optional[PathSelection]
    ) -> StrategyDecision:
        """Build decision for post-mortem strategy."""
        return StrategyDecision(
            strategy=InvestigationStrategy.POST_MORTEM,
            path=path,
            config=self.config,
            accept_partial_root_cause=False,
            require_validated_hypothesis=True,  # Require VALIDATED
            evidence_confidence_threshold=self.config.post_mortem_evidence_threshold,
            escalation_turn_threshold=None,  # No time-based escalation
            alternative_approaches=[
                "Explore alternative hypothesis categories",
                "Request additional evidence",
                "Consult knowledge base for similar cases"
            ]
        )

    def _check_escalation(self, decision: StrategyDecision, case: Case) -> None:
        """Check if escalation should be recommended."""
        # Time-based escalation for active incidents
        if decision.escalation_turn_threshold:
            if case.current_turn >= decision.escalation_turn_threshold:
                decision.should_escalate = True
                decision.escalation_reason = EscalationReason.MAX_ATTEMPTS_EXCEEDED
                return

        # Hypothesis exhaustion for post-mortem
        if decision.strategy == InvestigationStrategy.POST_MORTEM:
            refuted_count = sum(
                1 for h in case.hypotheses.values()
                if h.status.value == "refuted"
            )
            if refuted_count >= self.config.post_mortem_max_hypotheses_before_exhausted:
                decision.should_escalate = True
                decision.escalation_reason = EscalationReason.HYPOTHESIS_SPACE_EXHAUSTED
```

**Integration** (in `milestone_engine.py`):

```python
from faultmaven.core.investigation.strategy_selector import StrategySelector

class MilestoneEngine:
    def __init__(self, ...):
        self.strategy_selector = StrategySelector()

    async def process_turn(self, case: Case, ...) -> dict:
        # Get strategy decision
        strategy_decision = self.strategy_selector.select_strategy(
            case=case,
            verification=case.problem_verification
        )

        # Use strategy to modify behavior
        if strategy_decision.should_escalate:
            metadata["escalation_recommended"] = True
            metadata["escalation_reason"] = strategy_decision.escalation_reason.value

        # Pass thresholds to hypothesis validation
        # ...
```

**Acceptance Criteria**:
- [ ] `StrategySelector` class created
- [ ] ACTIVE_INCIDENT vs POST_MORTEM strategies differentiated
- [ ] Behavior thresholds configured per strategy
- [ ] Escalation detection implemented
- [ ] Integrated into `MilestoneEngine`
- [ ] Unit tests for strategy selection

---

## Phase 2: Data Model Alignment

### 2.1 Standardize Field Naming

**Priority**: Medium
**Effort**: 2-3 hours

**Issues**:
- `solution_applied` vs `resolution_applied` inconsistency
- `stance_confidence` called `completeness` in evidence links

**Tasks**:

1. **Audit all occurrences**:
   ```bash
   grep -rn "resolution_applied" faultmaven/
   grep -rn "solution_applied" faultmaven/
   grep -rn "completeness" faultmaven/ | grep -i evidence
   ```

2. **Standardize to spec names**:
   - `resolution_applied` → `solution_applied`
   - `completeness` → `stance_confidence` (in `HypothesisEvidenceLink`)

3. **Update in**:
   - `modules/case/contracts.py`
   - `modules/case/domain/models.py`
   - `core/investigation/schemas.py`
   - `core/investigation/milestone_engine.py`
   - `core/investigation/hypothesis_manager.py`

**Acceptance Criteria**:
- [ ] All field names match specification
- [ ] No duplicate/conflicting field names
- [ ] All tests pass after rename

---

### 2.2 Add Missing Evidence Model Fields

**Priority**: Medium
**Effort**: 2-3 hours

**Missing Fields** (per spec):
- `primary_purpose`: str - Why this evidence was collected
- `advances_milestones`: List[str] - Which milestones this advances
- `tests_hypothesis_id`: Optional[str] - Direct link to hypothesis being tested

**Tasks**:

```python
# In modules/case/contracts.py - Evidence model

class Evidence(BaseModel):
    # Existing fields...

    # Add these:
    primary_purpose: Optional[str] = Field(
        default=None,
        description="Why this evidence was collected (symptom_verification, hypothesis_testing, etc.)"
    )
    advances_milestones: List[str] = Field(
        default_factory=list,
        description="Milestones this evidence helps complete"
    )
    tests_hypothesis_id: Optional[str] = Field(
        default=None,
        description="If collected to test a specific hypothesis"
    )
```

**Acceptance Criteria**:
- [ ] Fields added to Evidence model
- [ ] Schema updated to include fields
- [ ] MilestoneEngine populates fields when adding evidence
- [ ] Tests updated

---

### 2.3 Add Missing Solution Model Fields

**Priority**: Low
**Effort**: 1-2 hours

**Missing Fields**:
- `verification_criteria`: List[str] - How to verify solution worked
- `immediate_action` / `longterm_fix`: Separate fields instead of single `description`

**Tasks**:

```python
# In modules/case/contracts.py - Solution model

class Solution(BaseModel):
    # Existing fields...

    # Add these:
    immediate_action: Optional[str] = Field(
        default=None,
        description="Quick mitigation step"
    )
    longterm_fix: Optional[str] = Field(
        default=None,
        description="Permanent resolution"
    )
    verification_criteria: List[str] = Field(
        default_factory=list,
        description="Steps to verify solution effectiveness"
    )
```

**Acceptance Criteria**:
- [ ] Fields added to Solution model
- [ ] Schema includes new fields
- [ ] Report generation uses new fields

---

## Phase 3: Error Handling & Recovery

### 3.1 Implement Retry with Exponential Backoff

**Priority**: High
**Effort**: 3-4 hours
**Location**: `core/investigation/resilience.py` (new file)

**Current State**:
- No retry logic in `MilestoneEngine`
- LLM failures cause immediate turn failure

**Implementation**:

```python
# File: core/investigation/resilience.py (NEW)

"""Resilience utilities for investigation engine."""

import asyncio
from typing import TypeVar, Callable, Awaitable, Optional
from functools import wraps
import logging

logger = logging.getLogger(__name__)

T = TypeVar('T')

class RetryConfig:
    """Configuration for retry behavior."""
    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        exponential_base: float = 2.0,
        retryable_exceptions: tuple = (Exception,)
    ):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.retryable_exceptions = retryable_exceptions

async def retry_with_backoff(
    func: Callable[..., Awaitable[T]],
    config: Optional[RetryConfig] = None,
    *args,
    **kwargs
) -> T:
    """Execute function with exponential backoff retry."""
    config = config or RetryConfig()
    last_exception = None

    for attempt in range(1, config.max_attempts + 1):
        try:
            return await func(*args, **kwargs)
        except config.retryable_exceptions as e:
            last_exception = e
            if attempt == config.max_attempts:
                logger.error(f"All {config.max_attempts} retry attempts failed: {e}")
                raise

            delay = min(
                config.base_delay * (config.exponential_base ** (attempt - 1)),
                config.max_delay
            )
            logger.warning(
                f"Attempt {attempt} failed: {e}. Retrying in {delay:.1f}s..."
            )
            await asyncio.sleep(delay)

    raise last_exception

def with_retry(config: Optional[RetryConfig] = None):
    """Decorator for retry with backoff."""
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            return await retry_with_backoff(func, config, *args, **kwargs)
        return wrapper
    return decorator
```

**Integration** (in `milestone_engine.py`):

```python
from faultmaven.core.investigation.resilience import retry_with_backoff, RetryConfig

# LLM call with retry
llm_config = RetryConfig(
    max_attempts=3,
    base_delay=1.0,
    retryable_exceptions=(LLMProviderError, asyncio.TimeoutError)
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

### 3.2 Implement Alternative Prompt Tier

**Priority**: Medium
**Effort**: 4-6 hours
**Location**: `core/investigation/prompts/fallback_templates.py` (new file)

**Current State**:
- Single prompt template per status/stage
- No fallback if primary prompt fails

**Implementation**:

```python
# File: core/investigation/prompts/fallback_templates.py (NEW)

"""Fallback prompt templates for degraded mode.

Simpler prompts used when:
1. Primary prompt exceeds token limit
2. LLM fails to produce valid structured output
3. Investigation is stuck (degraded mode)
"""

FALLBACK_INQUIRY_TEMPLATE = """You are an investigation assistant.
The user has reported an issue. Respond helpfully.

Issue: {problem_description}

Respond with a JSON object:
{
  "agent_response": "your response",
  "state_updates": {
    "problem_confirmation": {
      "problem_type": "error|slowness|unavailability|data_issue|other",
      "severity_guess": "critical|high|medium|low|unknown"
    }
  }
}
"""

FALLBACK_INVESTIGATING_TEMPLATE = """You are investigating an issue.

Problem: {problem_statement}
Current Evidence: {evidence_summary}
Hypotheses: {hypothesis_summary}

What is your next step? Respond with JSON:
{
  "agent_response": "your response",
  "state_updates": {
    "milestones": {"symptom_verified": true/false},
    "evidence_to_add": [],
    "hypotheses_to_add": [],
    "outcome": "milestone_completed|progress|conversation|blocked"
  }
}
"""

FALLBACK_STUCK_TEMPLATE = """The investigation appears stuck after {turns_without_progress} turns.

Current state:
- Problem: {problem_statement}
- Completed milestones: {completed_milestones}
- Active hypotheses: {active_hypotheses}

Suggest a different approach or ask the user for clarification.
Keep response brief and actionable.

Response:"""

class FallbackPromptSelector:
    """Selects appropriate fallback prompt."""

    @staticmethod
    def get_fallback_prompt(
        status: str,
        case_context: dict,
        failure_reason: str = "unknown"
    ) -> str:
        """Get fallback prompt based on status and context."""
        if status == "inquiry":
            return FALLBACK_INQUIRY_TEMPLATE.format(**case_context)
        elif status == "investigating":
            if case_context.get("turns_without_progress", 0) >= 3:
                return FALLBACK_STUCK_TEMPLATE.format(**case_context)
            return FALLBACK_INVESTIGATING_TEMPLATE.format(**case_context)
        else:
            return FALLBACK_INVESTIGATING_TEMPLATE.format(**case_context)
```

**Integration**:

```python
# In milestone_engine.py

async def process_turn(self, case, user_message):
    try:
        response = await self._call_llm_with_primary_prompt(...)
    except (TokenLimitError, ValidationError) as e:
        # Fall back to simpler prompt
        fallback_prompt = FallbackPromptSelector.get_fallback_prompt(
            status=case.status.value,
            case_context=self._build_fallback_context(case),
            failure_reason=str(e)
        )
        response = await self._call_llm_simple(fallback_prompt)
        metadata["used_fallback_prompt"] = True
```

**Acceptance Criteria**:
- [ ] Fallback templates created for each status
- [ ] `FallbackPromptSelector` selects appropriate template
- [ ] Fallback triggered on token limit or validation error
- [ ] Simpler schema for fallback responses
- [ ] Integration tested

---

## Phase 4: Testing & Documentation

### 4.1 Add Unit Tests for New Components

**Priority**: High
**Effort**: 8-10 hours

**Test Files to Create**:

```
tests/unit/core/investigation/
├── test_state_validator.py
├── test_memory_manager.py
├── test_strategy_selector.py
├── test_resilience.py
└── test_fallback_prompts.py
```

**Test Coverage Targets**:
- StateValidator: 90%+
- MemoryManager: 85%+
- StrategySelector: 90%+
- Resilience utilities: 95%+

---

### 4.2 Update Design Documentation

**Priority**: Medium
**Effort**: 4-6 hours

**Files to Update**:

1. `docs/architecture/investigation-engine/investigation-data-models.md`
   - Add new fields
   - Update field names

2. `docs/architecture/investigation-engine/error-handling-and-recovery.md`
   - Document implemented retry logic
   - Document fallback prompts

3. Create `docs/architecture/decisions/ADR-XXX-milestone-based-investigation.md`
   - Document evolution from OODA to milestone-based
   - Rationale for design decisions

---

## Implementation Schedule

| Phase | Component | Priority | Effort | Dependencies |
|-------|-----------|----------|--------|--------------|
| 1.1 | WorkingConclusionGenerator integration | Critical | 4-6h | None |
| 1.2 | StateValidator | Critical | 6-8h | None |
| 1.3 | MemoryManager | High | 8-10h | None |
| 1.4 | StrategySelector | High | 4-6h | None |
| 2.1 | Field naming standardization | Medium | 2-3h | None |
| 2.2 | Evidence model fields | Medium | 2-3h | 2.1 |
| 2.3 | Solution model fields | Low | 1-2h | 2.1 |
| 3.1 | Retry with backoff | High | 3-4h | None |
| 3.2 | Fallback prompts | Medium | 4-6h | 3.1 |
| 4.1 | Unit tests | High | 8-10h | All above |
| 4.2 | Documentation | Medium | 4-6h | All above |

**Total Estimated Effort**: 47-64 hours

---

## Success Criteria

The implementation is complete when:

1. **All critical components implemented**:
   - [ ] WorkingConclusionGenerator integrated and generating conclusions
   - [ ] StateValidator validating every turn
   - [ ] MemoryManager compressing old context
   - [ ] StrategySelector differentiating ACTIVE_INCIDENT vs POST_MORTEM

2. **Data models aligned**:
   - [ ] No naming inconsistencies
   - [ ] All spec fields present

3. **Error handling robust**:
   - [ ] LLM calls retry on transient failures
   - [ ] Fallback prompts used when primary fails

4. **Tests passing**:
   - [ ] All new unit tests pass
   - [ ] No regression in existing tests
   - [ ] Coverage targets met

5. **Documentation updated**:
   - [ ] CLAUDE.md reflects actual implementation
   - [ ] Design docs match code
   - [ ] ADR documents major decisions

---

## Next Steps

1. Review this plan with stakeholders
2. Create GitHub issues for each phase
3. Begin Phase 1 implementation (Critical items)
4. Weekly progress check-ins
