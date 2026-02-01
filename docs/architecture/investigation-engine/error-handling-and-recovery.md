# Error Handling and Recovery Patterns

**Version**: 2.0
**Last Updated**: 2026-02-01
**Status**: Operational Design
**Architecture**: Milestone-Based Investigation Framework

---

## Overview

This document defines error handling and recovery strategies for the FaultMaven investigation framework, ensuring graceful degradation and automatic recovery from failure conditions.

**Related Documents**:
- [Opportunistic Investigation Framework](./opportunistic-investigation-framework.md) - Core architecture
- [Investigation Data Models](./investigation-data-models.md) - Data structures
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) - State transitions

---

## Table of Contents

1. [Error Categories](#1-error-categories)
2. [LLM Error Handling](#2-llm-error-handling)
3. [Response Parsing Errors](#3-response-parsing-errors)
4. [State Validation](#4-state-validation)
5. [Stagnation Detection](#5-stagnation-detection)
6. [Recovery Strategies](#6-recovery-strategies)
7. [Error Context Propagation](#7-error-context-propagation)

---

## 1. Error Categories

### 1.1 Transient Errors (Retryable)
- LLM API rate limiting
- Network timeouts
- Temporary service unavailability
- Redis connection failures

**Strategy**: Automatic retry with exponential backoff

### 1.2 User-Recoverable Errors
- Invalid user input
- Missing required information
- Ambiguous evidence classification
- Blocked evidence access

**Strategy**: Request clarification or alternative input

### 1.3 System Errors (Escalation Required)
- LLM authentication failures
- State corruption
- Database connection loss
- Unrecoverable parsing failures

**Strategy**: Log error, notify monitoring, suggest escalation

### 1.4 Logic Errors (Investigation Stalls)
- No progress for 3+ turns
- Hypothesis anchoring (same category tested repeatedly)
- Evidence contradictions
- Milestone dependencies violated

**Strategy**: Detect and force alternative paths or enter degraded mode

---

## 2. LLM Error Handling

### 2.1 Retry Configuration

```python
from dataclasses import dataclass
from typing import Tuple

@dataclass
class RetryConfig:
    """Configuration for LLM retry behavior."""
    max_retries: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0
    exponential_base: float = 2.0

    # Error types that should be retried
    retryable_errors: Tuple[type, ...] = (
        RateLimitError,
        TimeoutError,
        ConnectionError,
    )
```

### 2.2 Error Handler Implementation

```python
class LLMErrorHandler:
    """Handles LLM API errors with automatic recovery."""

    def __init__(self, config: RetryConfig = None):
        self.config = config or RetryConfig()

    async def handle_error(
        self,
        error: Exception,
        case: Case,
        retry_count: int = 0
    ) -> ErrorResult:
        """
        Handle LLM API errors with appropriate recovery.

        Args:
            error: The exception that occurred
            case: Current investigation case
            retry_count: Number of retries attempted

        Returns:
            ErrorResult with recovery action and message
        """

        if isinstance(error, RateLimitError):
            return await self._handle_rate_limit(error, retry_count)

        elif isinstance(error, TimeoutError):
            return await self._handle_timeout(error, case, retry_count)

        elif isinstance(error, AuthenticationError):
            return self._handle_auth_error(error)

        elif isinstance(error, TokenLimitError):
            return self._handle_token_limit(error, case)

        else:
            return await self._handle_unknown(error, retry_count)

    async def _handle_rate_limit(
        self,
        error: RateLimitError,
        retry_count: int
    ) -> ErrorResult:
        """Handle rate limiting with exponential backoff."""

        if retry_count >= self.config.max_retries:
            return ErrorResult(
                action=ErrorAction.FAIL,
                message="LLM service temporarily unavailable. Please try again in a few minutes.",
                error_code="RATE_LIMIT_EXCEEDED"
            )

        delay = min(
            self.config.base_delay_seconds * (self.config.exponential_base ** retry_count),
            self.config.max_delay_seconds
        )

        await asyncio.sleep(delay)

        return ErrorResult(
            action=ErrorAction.RETRY,
            message=f"Rate limited. Retrying in {delay:.1f}s...",
            retry_count=retry_count + 1
        )

    async def _handle_timeout(
        self,
        error: TimeoutError,
        case: Case,
        retry_count: int
    ) -> ErrorResult:
        """Handle request timeouts."""

        if retry_count >= self.config.max_retries:
            return ErrorResult(
                action=ErrorAction.USE_FALLBACK_PROMPT,
                message="Request timed out. Using simplified prompt.",
                error_code="TIMEOUT_EXCEEDED"
            )

        return ErrorResult(
            action=ErrorAction.RETRY,
            message="Request timed out. Retrying...",
            retry_count=retry_count + 1
        )

    def _handle_auth_error(self, error: AuthenticationError) -> ErrorResult:
        """Handle authentication errors (non-retryable)."""

        logger.error(f"LLM authentication error: {error}")

        return ErrorResult(
            action=ErrorAction.ESCALATE,
            message="System configuration error. Please contact support.",
            error_code="AUTH_FAILED"
        )

    def _handle_token_limit(self, error: TokenLimitError, case: Case) -> ErrorResult:
        """Handle token limit exceeded."""

        return ErrorResult(
            action=ErrorAction.COMPRESS_MEMORY,
            message="Context too large. Compressing conversation history...",
            error_code="TOKEN_LIMIT"
        )


class ErrorAction(str, Enum):
    """Actions to take after error handling."""
    RETRY = "retry"
    USE_FALLBACK_PROMPT = "use_fallback_prompt"
    COMPRESS_MEMORY = "compress_memory"
    ESCALATE = "escalate"
    FAIL = "fail"


@dataclass
class ErrorResult:
    """Result of error handling."""
    action: ErrorAction
    message: str
    error_code: str = None
    retry_count: int = 0
```

---

## 3. Response Parsing Errors

### 3.1 Graceful Parsing with Fallback

```python
class ResponseParser:
    """Parse LLM responses with fallback handling."""

    def parse_structured_response(
        self,
        llm_response: str,
        schema: Type[BaseModel]
    ) -> Tuple[Optional[BaseModel], bool]:
        """
        Parse LLM response into structured format.

        Args:
            llm_response: Raw LLM output
            schema: Expected Pydantic schema

        Returns:
            (parsed_response, used_fallback)
        """

        # Try JSON extraction first
        try:
            json_content = self._extract_json(llm_response)
            if json_content:
                parsed = schema.model_validate_json(json_content)
                return parsed, False
        except (json.JSONDecodeError, ValidationError) as e:
            logger.warning(f"JSON parsing failed: {e}")

        # Fallback: Extract minimal response
        return self._extract_minimal_response(llm_response, schema), True

    def _extract_json(self, text: str) -> Optional[str]:
        """Extract JSON from response text."""

        # Try markdown code block
        json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if json_match:
            return json_match.group(1).strip()

        # Try raw JSON object
        brace_match = re.search(r'\{[\s\S]*\}', text)
        if brace_match:
            return brace_match.group(0)

        return None

    def _extract_minimal_response(
        self,
        text: str,
        schema: Type[BaseModel]
    ) -> BaseModel:
        """Extract minimal valid response when parsing fails."""

        # Create minimal response with just agent_response
        return schema(
            agent_response=text[:2000],  # Truncate if too long
            state_updates=None,
            _parse_fallback=True
        )
```

---

## 4. State Validation

### 4.1 Milestone-Based State Validator

The `StateValidator` validates investigation state consistency using the **milestone-based** architecture.

```python
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional, Tuple

from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    InvestigationProgress,
    HypothesisStatus,
)


class ValidationSeverity(str, Enum):
    """Severity of validation issues."""
    WARNING = "warning"      # Non-blocking, log only
    ERROR = "error"          # Blocking, requires correction
    CRITICAL = "critical"    # Severe inconsistency


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

    def validate_case(self, case: Case) -> List[ValidationIssue]:
        """Run all validations on a case."""
        issues = []
        issues.extend(self._validate_milestone_ordering(case.progress))
        issues.extend(self._validate_status_consistency(case))
        issues.extend(self._validate_hypothesis_states(case))
        issues.extend(self._validate_evidence_links(case))
        issues.extend(self._validate_likelihood_bounds(case))
        return issues

    def _validate_milestone_ordering(
        self,
        progress: InvestigationProgress
    ) -> List[ValidationIssue]:
        """
        Validate milestone dependencies are respected.

        Milestones can only go False → True, never revert.
        Some milestones have logical dependencies.
        """
        issues = []

        # solution_verified requires solution_proposed
        if progress.solution_verified and not progress.solution_proposed:
            issues.append(ValidationIssue(
                code="MILESTONE_ORDER_001",
                message="solution_verified=True but solution_proposed=False",
                severity=ValidationSeverity.ERROR,
                field="progress.solution_verified",
                suggested_fix="Set solution_proposed=True or reset solution_verified=False"
            ))

        # solution_applied requires solution_proposed
        if progress.solution_applied and not progress.solution_proposed:
            issues.append(ValidationIssue(
                code="MILESTONE_ORDER_002",
                message="solution_applied=True but solution_proposed=False",
                severity=ValidationSeverity.ERROR,
                field="progress.solution_applied",
                suggested_fix="Set solution_proposed=True"
            ))

        # root_cause_identified should have likelihood
        if progress.root_cause_identified and progress.root_cause_likelihood is None:
            issues.append(ValidationIssue(
                code="MILESTONE_INCOMPLETE_001",
                message="root_cause_identified=True but root_cause_likelihood is None",
                severity=ValidationSeverity.WARNING,
                field="progress.root_cause_likelihood",
                suggested_fix="Set root_cause_likelihood to confidence value"
            ))

        return issues

    def _validate_status_consistency(self, case: Case) -> List[ValidationIssue]:
        """Ensure status matches progress state."""
        issues = []

        # RESOLVED requires solution_verified
        if case.status == CaseStatus.RESOLVED and not case.progress.solution_verified:
            issues.append(ValidationIssue(
                code="STATUS_MISMATCH_001",
                message="Status is RESOLVED but solution_verified=False",
                severity=ValidationSeverity.ERROR,
                field="status",
                suggested_fix="Set solution_verified=True or change status to INVESTIGATING"
            ))

        # INVESTIGATING should have problem_statement
        if case.status == CaseStatus.INVESTIGATING:
            has_statement = (
                case.problem_verification and
                case.problem_verification.symptom_statement
            )
            if not has_statement:
                issues.append(ValidationIssue(
                    code="STATUS_MISMATCH_002",
                    message="Status is INVESTIGATING but symptom_statement is empty",
                    severity=ValidationSeverity.WARNING,
                    field="problem_verification.symptom_statement"
                ))

        return issues

    def _validate_hypothesis_states(self, case: Case) -> List[ValidationIssue]:
        """Validate hypothesis lifecycle states."""
        issues = []

        for hyp_id, hypothesis in case.hypotheses.items():
            # VALIDATED requires sufficient evidence
            if hypothesis.status == HypothesisStatus.VALIDATED:
                supporting = sum(
                    1 for link in hypothesis.evidence_links.values()
                    if link.stance.value == "supports"
                )
                if supporting < 2:
                    issues.append(ValidationIssue(
                        code="HYPOTHESIS_STATE_001",
                        message=f"Hypothesis {hyp_id} is VALIDATED with only {supporting} supporting evidence",
                        severity=ValidationSeverity.WARNING,
                        field=f"hypotheses.{hyp_id}",
                        suggested_fix="Require at least 2 supporting evidence items"
                    ))

        return issues

    def _validate_evidence_links(self, case: Case) -> List[ValidationIssue]:
        """Validate evidence-hypothesis links are consistent."""
        issues = []
        evidence_ids = {e.id for e in case.evidence}

        for hyp_id, hypothesis in case.hypotheses.items():
            for ev_ref in hypothesis.evidence_links.keys():
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

    def _validate_likelihood_bounds(self, case: Case) -> List[ValidationIssue]:
        """Validate all likelihood/confidence values are in [0, 1]."""
        issues = []

        for hyp_id, hypothesis in case.hypotheses.items():
            if hypothesis.likelihood < 0.0 or hypothesis.likelihood > 1.0:
                issues.append(ValidationIssue(
                    code="BOUNDS_001",
                    message=f"Hypothesis {hyp_id} likelihood {hypothesis.likelihood} outside [0,1]",
                    severity=ValidationSeverity.ERROR,
                    field=f"hypotheses.{hyp_id}.likelihood"
                ))

        return issues

    def is_valid(self, case: Case) -> Tuple[bool, List[ValidationIssue]]:
        """Check if case state is valid (no ERROR or CRITICAL issues)."""
        issues = self.validate_case(case)
        blocking = [
            i for i in issues
            if i.severity in (ValidationSeverity.ERROR, ValidationSeverity.CRITICAL)
        ]
        return len(blocking) == 0, issues
```

---

## 5. Stagnation Detection

### 5.1 Progress Stall Detection

The system detects investigation stalls using **turn-based tracking**, not phase-based.

```python
class StagnationDetector:
    """Detects investigation stalls and anchoring patterns."""

    def __init__(
        self,
        no_progress_threshold: int = 3,
        category_anchoring_threshold: int = 4,
        action_loop_threshold: int = 5
    ):
        self.no_progress_threshold = no_progress_threshold
        self.category_anchoring_threshold = category_anchoring_threshold
        self.action_loop_threshold = action_loop_threshold

    def detect_stagnation(self, case: Case) -> Optional[StagnationType]:
        """
        Detect if investigation is stagnating.

        Returns StagnationType if stagnating, None otherwise.
        """

        # Pattern 1: No milestones completed in N turns
        if case.turns_without_progress >= self.no_progress_threshold:
            return StagnationType.NO_PROGRESS

        # Pattern 2: Hypothesis category anchoring
        if self._detect_category_anchoring(case):
            return StagnationType.HYPOTHESIS_ANCHORING

        # Pattern 3: Repeated action sequences
        if self._detect_action_loop(case):
            return StagnationType.ACTION_LOOP

        # Pattern 4: All hypotheses inconclusive
        if self._detect_hypothesis_deadlock(case):
            return StagnationType.HYPOTHESIS_DEADLOCK

        return None

    def _detect_category_anchoring(self, case: Case) -> bool:
        """
        Detect if agent is stuck testing same hypothesis category.

        Triggers if 4+ hypotheses in same category are REFUTED or INCONCLUSIVE.
        """
        category_counts = {}

        for hypothesis in case.hypotheses.values():
            if hypothesis.status in (HypothesisStatus.REFUTED, HypothesisStatus.INCONCLUSIVE):
                cat = hypothesis.category.value
                category_counts[cat] = category_counts.get(cat, 0) + 1

        for category, count in category_counts.items():
            if count >= self.category_anchoring_threshold:
                logger.warning(f"Category anchoring: {count} failed hypotheses in '{category}'")
                return True

        return False

    def _detect_action_loop(self, case: Case) -> bool:
        """
        Detect if agent is repeating same actions.

        Triggers if same action sequence appears 5+ times.
        """
        if len(case.turn_history) < self.action_loop_threshold:
            return False

        recent_turns = case.turn_history[-self.action_loop_threshold:]
        action_sequences = [
            tuple(t.actions_taken)
            for t in recent_turns
            if t.actions_taken
        ]

        if len(action_sequences) >= 3:
            unique_sequences = set(action_sequences)
            if len(unique_sequences) == 1:
                logger.warning("Action loop: same actions repeated in recent turns")
                return True

        return False

    def _detect_hypothesis_deadlock(self, case: Case) -> bool:
        """
        Detect if all hypotheses are inconclusive.
        """
        if not case.hypotheses:
            return False

        all_inconclusive = all(
            h.status == HypothesisStatus.INCONCLUSIVE
            for h in case.hypotheses.values()
        )

        return all_inconclusive and len(case.hypotheses) >= 3


class StagnationType(str, Enum):
    """Types of investigation stagnation."""
    NO_PROGRESS = "no_progress"
    HYPOTHESIS_ANCHORING = "hypothesis_anchoring"
    ACTION_LOOP = "action_loop"
    HYPOTHESIS_DEADLOCK = "hypothesis_deadlock"
```

### 5.2 Breaking Out of Stagnation

```python
class StagnationBreaker:
    """Strategies to break out of stagnation."""

    def break_stagnation(
        self,
        case: Case,
        stagnation_type: StagnationType
    ) -> BreakoutAction:
        """
        Determine action to break out of stagnation.

        Returns recommended action and updated case state.
        """

        if stagnation_type == StagnationType.NO_PROGRESS:
            return self._handle_no_progress(case)

        elif stagnation_type == StagnationType.HYPOTHESIS_ANCHORING:
            return self._handle_anchoring(case)

        elif stagnation_type == StagnationType.ACTION_LOOP:
            return self._handle_action_loop(case)

        elif stagnation_type == StagnationType.HYPOTHESIS_DEADLOCK:
            return self._handle_deadlock(case)

        return BreakoutAction(action="none", message="No action needed")

    def _handle_no_progress(self, case: Case) -> BreakoutAction:
        """Handle no progress in 3+ turns."""

        # Enter degraded mode
        case.degraded_mode = DegradedMode(
            mode_type=DegradedModeType.NO_PROGRESS,
            reason=f"No progress in {case.turns_without_progress} turns",
            entered_at=datetime.now(timezone.utc)
        )

        return BreakoutAction(
            action="enter_degraded_mode",
            message="Investigation not progressing. Offering alternative approaches.",
            prompt_injection="Ask user for clarification or offer to escalate."
        )

    def _handle_anchoring(self, case: Case) -> BreakoutAction:
        """Handle hypothesis category anchoring."""

        # Identify anchored category
        anchored_category = self._find_anchored_category(case)

        return BreakoutAction(
            action="force_alternative_category",
            message=f"Tested many '{anchored_category}' hypotheses. Exploring other categories.",
            prompt_injection=f"Do NOT propose hypotheses in '{anchored_category}' category. "
                           f"Try different categories like: {self._suggest_categories(anchored_category)}"
        )

    def _handle_action_loop(self, case: Case) -> BreakoutAction:
        """Handle repeated action sequences."""

        return BreakoutAction(
            action="request_user_input",
            message="Investigation appears stuck in a loop. Requesting user guidance.",
            prompt_injection="Ask user for additional context or a different approach."
        )

    def _handle_deadlock(self, case: Case) -> BreakoutAction:
        """Handle all hypotheses inconclusive."""

        # Retire all inconclusive hypotheses
        for hypothesis in case.hypotheses.values():
            if hypothesis.status == HypothesisStatus.INCONCLUSIVE:
                hypothesis.status = HypothesisStatus.RETIRED

        return BreakoutAction(
            action="reset_hypotheses",
            message="All hypotheses inconclusive. Starting fresh hypothesis generation.",
            prompt_injection="Generate completely new hypotheses based on available evidence."
        )


@dataclass
class BreakoutAction:
    """Action to break out of stagnation."""
    action: str
    message: str
    prompt_injection: str = None
```

---

## 6. Recovery Strategies

### 6.1 Recovery Manager

```python
class RecoveryManager:
    """Manages automatic recovery from error conditions."""

    def __init__(self, llm_provider: ILLMProvider):
        self.llm = llm_provider

    async def attempt_recovery(
        self,
        error: Exception,
        case: Case,
        context: Dict
    ) -> RecoveryResult:
        """
        Attempt automatic recovery from error.

        Tries strategies in order until one succeeds.
        """

        strategies = [
            ("memory_compression", self._try_memory_compression),
            ("simplify_hypotheses", self._try_simplify_hypotheses),
            ("fallback_prompt", self._try_fallback_prompt),
            ("milestone_reset", self._try_milestone_reset),
        ]

        for name, strategy in strategies:
            try:
                result = await strategy(error, case, context)
                if result.success:
                    logger.info(f"Recovery successful: {name}")
                    return result
            except Exception as e:
                logger.warning(f"Recovery strategy '{name}' failed: {e}")
                continue

        return RecoveryResult(
            success=False,
            case=case,
            message="All recovery strategies failed. Manual intervention required."
        )

    async def _try_memory_compression(
        self,
        error: Exception,
        case: Case,
        context: Dict
    ) -> RecoveryResult:
        """Compress conversation history to reduce token usage."""

        if "token" not in str(error).lower():
            return RecoveryResult(success=False, case=case)

        # Summarize older turns
        if len(case.turn_history) > 10:
            older_turns = case.turn_history[:-10]
            summary = await self._summarize_turns(older_turns)
            case.compressed_history_summary = summary

            return RecoveryResult(
                success=True,
                case=case,
                message="Compressed conversation history to reduce tokens"
            )

        return RecoveryResult(success=False, case=case)

    async def _try_simplify_hypotheses(
        self,
        error: Exception,
        case: Case,
        context: Dict
    ) -> RecoveryResult:
        """Retire low-likelihood hypotheses to simplify state."""

        retired_count = 0
        for hypothesis in case.hypotheses.values():
            if hypothesis.likelihood < 0.3 and hypothesis.status == HypothesisStatus.ACTIVE:
                hypothesis.status = HypothesisStatus.RETIRED
                retired_count += 1

        if retired_count > 0:
            return RecoveryResult(
                success=True,
                case=case,
                message=f"Retired {retired_count} low-likelihood hypotheses"
            )

        return RecoveryResult(success=False, case=case)

    async def _try_fallback_prompt(
        self,
        error: Exception,
        case: Case,
        context: Dict
    ) -> RecoveryResult:
        """Use simplified prompt template."""

        context["use_fallback_prompt"] = True

        return RecoveryResult(
            success=True,
            case=case,
            message="Switched to simplified prompt template",
            context_updates={"use_fallback_prompt": True}
        )

    async def _try_milestone_reset(
        self,
        error: Exception,
        case: Case,
        context: Dict
    ) -> RecoveryResult:
        """
        Reset to earlier milestone state.

        Only used as last resort - reverts recent progress.
        """

        # Find last successful milestone
        if not case.turn_history:
            return RecoveryResult(success=False, case=case)

        # Find turn with last milestone completion
        for turn in reversed(case.turn_history[:-3]):
            if turn.milestones_completed:
                # Reset turns_without_progress counter
                case.turns_without_progress = 0

                return RecoveryResult(
                    success=True,
                    case=case,
                    message=f"Reset progress counter. Last milestone: {turn.milestones_completed[-1]}"
                )

        return RecoveryResult(success=False, case=case)


@dataclass
class RecoveryResult:
    """Result of recovery attempt."""
    success: bool
    case: Case
    message: str = ""
    context_updates: Dict = None
```

---

## 7. Error Context Propagation

### 7.1 Error Context Model

```python
@dataclass
class ErrorContext:
    """Comprehensive error context for debugging and monitoring."""

    # Error identification
    error_id: str = field(default_factory=lambda: f"err_{uuid4().hex[:12]}")
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_type: str = ""
    error_message: str = ""
    stack_trace: str = ""

    # Investigation context
    case_id: str = ""
    current_turn: int = 0
    current_status: str = ""
    current_stage: str = ""

    # Operation context
    operation: str = ""  # "process_turn", "parse_response", "validate_state", etc.
    operation_params: Dict = field(default_factory=dict)

    # State snapshot
    milestones_completed: List[str] = field(default_factory=list)
    active_hypotheses_count: int = 0
    evidence_count: int = 0
    turns_without_progress: int = 0

    # Recovery context
    recovery_attempted: bool = False
    recovery_strategy: Optional[str] = None
    recovery_success: bool = False

    def to_dict(self) -> Dict:
        """Convert to dict for logging/monitoring."""
        return {
            "error_id": self.error_id,
            "timestamp": self.timestamp.isoformat(),
            "error_type": self.error_type,
            "error_message": self.error_message,
            "case_id": self.case_id,
            "current_turn": self.current_turn,
            "current_status": self.current_status,
            "operation": self.operation,
            "recovery_attempted": self.recovery_attempted,
            "recovery_success": self.recovery_success
        }

    def to_log_context(self) -> Dict:
        """Minimal context for structured logging."""
        return {
            "error_id": self.error_id,
            "case_id": self.case_id,
            "operation": self.operation,
            "error_type": self.error_type
        }
```

### 7.2 Error Tracking Integration

```python
class ErrorTracker:
    """Track and report errors for monitoring."""

    def __init__(self):
        self.recent_errors: List[ErrorContext] = []
        self.max_recent = 100

    def record_error(self, context: ErrorContext) -> None:
        """Record error for tracking and monitoring."""

        # Log with context
        logger.error(
            f"Investigation error: {context.error_type}",
            extra=context.to_log_context()
        )

        # Store in recent errors
        self.recent_errors.append(context)
        if len(self.recent_errors) > self.max_recent:
            self.recent_errors.pop(0)

        # Check for error patterns
        self._check_error_patterns(context)

    def _check_error_patterns(self, context: ErrorContext) -> None:
        """Detect recurring error patterns."""

        recent_same_type = [
            e for e in self.recent_errors[-10:]
            if e.error_type == context.error_type
        ]

        if len(recent_same_type) >= 5:
            logger.warning(
                f"Error pattern detected: {context.error_type} occurred 5+ times recently",
                extra={"error_type": context.error_type, "count": len(recent_same_type)}
            )
```

---

## Summary

This error handling framework provides:

1. **Categorized error handling** - Different strategies for transient, user-recoverable, system, and logic errors

2. **Automatic retry with backoff** - For transient LLM errors

3. **Graceful parsing fallback** - Extract meaningful responses even when JSON parsing fails

4. **Milestone-based state validation** - Ensure investigation state consistency

5. **Stagnation detection** - Identify when investigation is stuck (no progress, anchoring, loops)

6. **Recovery strategies** - Memory compression, hypothesis simplification, fallback prompts

7. **Error context propagation** - Comprehensive error tracking for debugging

**Integration Points**:
- `MilestoneEngine.process_turn()` - Calls StateValidator and StagnationDetector
- `LLMProvider.generate()` - Uses LLMErrorHandler for retry logic
- `ResponseParser.parse()` - Handles parsing failures gracefully

---

**END OF DOCUMENT**
