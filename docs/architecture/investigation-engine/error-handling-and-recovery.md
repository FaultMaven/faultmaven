# Error Handling and Recovery Patterns

**Version**: 3.0
**Last Updated**: 2026-02-19
**Status**: Operational Design
**Architecture**: Evidence-Driven Investigation Framework

---

## Overview

This document defines error handling and recovery strategies for the FaultMaven investigation framework, ensuring graceful degradation and automatic recovery from failure conditions.

**Related Documents**:

- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) - Core architecture
- [Investigation Data Models](./investigation-data-models.md) - Data structures
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) - State transitions

---

## Table of Contents

1. [Error Categories](#1-error-categories)
2. [LLM Error Handling](#2-llm-error-handling)
3. [Response Parsing Errors](#3-response-parsing-errors)
   - 3.1 [Graceful Parsing with Fallback](#31-graceful-parsing-with-fallback)
   - 3.2 [System Feedback Loop](#32-system-feedback-loop)
   - 3.3 [Defensive Schema Coercion](#33-defensive-schema-coercion)
4. [State Validation](#4-state-validation)
5. [Progress Transparency](#5-progress-transparency)
6. [Recovery Strategies](#6-recovery-strategies)
7. [Error Context Propagation](#7-error-context-propagation)

---

## 1. Error Categories

### 1.1 Transient Errors (Retryable)

- LLM API rate limiting (HTTP 429)
- Network timeouts
- Temporary service unavailability (HTTP 5xx)
- Redis connection failures

**Strategy**: Automatic retry with exponential backoff. `LLMException` with `status_code >= 500`, `status_code == 429`, or explicit `retryable=True` triggers retries.

> **Not all 429s are transient.** Providers reuse HTTP 429 for both rate-limiting (transient) and **billing/quota exhaustion** (permanent). A 429 whose body indicates exhausted credits/quota is classified as billing, not rate-limiting — see [§1.6](#16-billing--quota-exhaustion-operator-action-required).

### 1.2 Non-Retryable Client Errors (Fail Fast)

- Invalid API key (HTTP 401)
- Malformed request / invalid model (HTTP 400, 404)
- Tool calling incompatibility (e.g., DeepSeek proprietary tokens)

**Strategy**: Fail immediately, no retries. `LLMException` with `status_code` 4xx or `retryable=False` (default). The router fails fast rather than burning timeout retrying errors that will never succeed.

### 1.3 User-Recoverable Errors

- Invalid user input
- Missing required information
- Ambiguous evidence classification
- Blocked evidence access

**Strategy**: Request clarification or alternative input

### 1.4 System Errors (Escalation Required)

- LLM authentication failures
- State corruption
- Database connection loss
- Unrecoverable parsing failures

**Strategy**: Log error, notify monitoring, suggest escalation

### 1.5 Logic Errors (Investigation Stalls)

- No progress for 5+ turns
- Hypothesis anchoring (same category tested repeatedly)
- Evidence contradictions
- Gate milestone dependencies violated

**Strategy**: Progress transparency surfaces milestone dependencies when stalled; agent state repair handles internal failures (see [Progress Transparency](./progress-transparency.md))

### 1.6 Billing / Quota Exhaustion (Operator Action Required)

- LLM provider out of credits / hard spend cap reached
- Billing not enabled for the account or tier
- Provider quota exhausted (e.g. OpenAI `insufficient_quota`, HTTP 402)

**Strategy**: Fail fast, **never retry**, surface an operator-actionable message. This is a *permanent* condition distinct from transient rate-limiting — waiting cannot add credits, so retrying only burns time and trips the circuit breaker. The error is classified with a stable `error_code` of `QUOTA_EXHAUSTED` that propagates through every layer (provider → circuit breaker → error handler → engine → API → UI), so the user is told to add credits rather than "try again". At the API boundary it maps to **HTTP 402 Payment Required** (`x-error-code: QUOTA_EXHAUSTED`, no `Retry-After`).

---

## 2. LLM Error Handling

### 2.1 Retry Configuration and Error Retryability

LLM errors carry retryability information via `LLMException`:

```python
from faultmaven.exceptions import LLMException

class LLMException(FaultMavenException):
    def __init__(self, message, status_code=None, retryable=None, error_code=None):
        self.status_code = status_code
        # Auto-classify permanent billing/quota exhaustion from the provider
        # body (single chokepoint — every provider folds the upstream body into
        # the message). See is_billing_quota_error().
        if error_code is None and is_billing_quota_error(message, status_code):
            error_code = QUOTA_EXHAUSTED
        self.error_code = error_code
        # Retryability: billing (permanent) > explicit > status_code > default.
        if error_code == QUOTA_EXHAUSTED:
            self.retryable = False  # waiting cannot add credits
        elif retryable is not None:
            self.retryable = retryable
        elif status_code is not None:
            self.retryable = status_code >= 500 or status_code == 429
        else:
            self.retryable = False  # Fail-fast default
```

The `BaseExternalClient.call_external()` method checks `retryable` before retrying:

- `retryable=True` (5xx, 429, explicit): retry with exponential backoff
- `retryable=False` (other 4xx, billing, default): fail immediately, no retries

The provider registry re-raises the last provider's error directly (preserving retryability and `error_code`) rather than wrapping in a generic exception.

**Circuit breaker carries the classification.** When repeated failures open the breaker, `CircuitBreakerError` carries the `error_code` of the failure that tripped it (latched across the failing streak so a trailing transient error does not erase a permanent billing signal). This keeps a billing condition distinguishable as `QUOTA_EXHAUSTED` even on turns where the request never reaches the provider — without it, an open breaker would mask billing as a generic 500.

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

        # Billing/quota exhaustion is checked first — it is permanent and must
        # not be mistaken for a transient 429/5xx. is_billing_error() inspects
        # the typed error_code (walking __cause__ for wrapped errors) and falls
        # back to body markers.
        if self.is_billing_error(error):
            return self._handle_billing(error)

        elif isinstance(error, RateLimitError):
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

    def _handle_billing(self, error: Exception) -> ErrorResult:
        """Handle billing/quota exhaustion (permanent, operator-actionable)."""

        logger.error(f"LLM provider billing/quota exhausted: {error}")

        return ErrorResult(
            action=ErrorAction.ESCALATE,
            message=(
                "FaultMaven's AI provider is out of quota or credits. An "
                "administrator needs to add credits or update the provider's "
                "billing plan before the investigation can continue."
            ),
            error_code="QUOTA_EXHAUSTED"  # → HTTP 402 at the API boundary
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

### 3.2 System Feedback Loop

Validation errors from multiple sources are merged into `system_feedback` on the turn record, which `build_investigation_context()` includes in the next turn's prompt:

| Source | Feedback Key | Content |
|--------|-------------|---------|
| Reasoning-first validator | `reasoning_validation_errors` | Missing milestone justifications |
| Progress monitor | `breakout_action` (turn metadata; the monitor result also carries a `prompt_injection` field) | Transparency guidance + repair-pattern injection (e.g., "try different category" on anchoring) |
| State validator | `validation_repairs` | Automatic state corrections applied |

This ensures the LLM receives corrective instructions for the next turn even when the current turn's issues are non-fatal.

Rule 2 (Evidence-Grounded) compliance is enforced solely at the prompt layer; there is no post-generation diagnostic-reasoning validator. See [agent-behavioral-rules.md § Post-Generation Validators (Historical Case Study)](./agent-behavioral-rules.md#post-generation-validators-historical-case-study) for the architectural reasoning behind the earlier removal.

### 3.3 Defensive Schema Coercion

Some LLMs (notably Fireworks/DeepSeek V3) return shapes that would otherwise fail Pydantic validation with a hard 500: required fields omitted, object fields returned as JSON-encoded strings, or `null` where an object is expected. The response schemas (`faultmaven/core/investigation/schemas.py`) carry narrow `mode="before"` validators / Optional-with-default fields so these LLM quirks degrade to safe defaults instead of crashing the turn:

| Field | Defensive treatment | Rationale |
| --- | --- | --- |
| `*StateUpdate.outcome` | `Optional[TurnOutcome]` with default `CONVERSATION` | The server recomputes outcome from actual state changes via `determine_turn_outcome()`; the LLM's value is ignored. Making it optional turns an LLM omission into a no-op instead of a 500. |
| `BaseInteractionResponse.suggested_follow_ups` | `field_validator(mode="before")` parses a JSON string into a list, returning `None` on parse failure | Suggestions are advisory UI affordances; a malformed list shouldn't fail the entire turn. |
| `state_updates` (top-level) | Coerced to `{}` when the LLM returns `null` or an unparseable string (see `milestone_engine.py` JSON repair passes) | Allows Pydantic field defaults to fire when the LLM truncates output mid-object. |

The rule: defensive coercion is reserved for fields where the server has an authoritative or safe-default value. Fields whose values genuinely come from the LLM (`agent_response`, `evidence_to_add` entries, milestone justifications) stay strict — they cannot be quietly defaulted without losing fidelity, so they remain required and surface as a validation error when missing.

### 3.4 Never-500 Backstop for Parse-Time Validation Errors

A single malformed sub-record emitted by the LLM (e.g. `evidence_to_add` with `source_type=text` and no `source_file_id`; `evidence_need_updates{state: FULFILLED}` with no `fulfilling_evidence_id`) makes the *whole* `InvestigationResponse_*` fail `model_validate_json` — an unhandled `ValidationError` that 500s the turn **before any milestone logic runs**, so the per-milestone surgical strip never gets a chance. The cross-field invariants themselves are correct and stay (they gate on real facts); what's added is a general parse-time recovery policy, `_validate_with_degradation` (general, not per-invariant):

1. Validate as-is.
2. On failure, **prune the exact list entries the `ValidationError` loc points at** (general across `evidence_to_add` / `evidence_need_updates` / `hypotheses_to_add` / any list field) and re-validate — the bad sub-records are quarantined and logged (`structured_output_degraded`), the rest of the turn survives.
3. Else drop `state_updates` entirely and keep the conversational `agent_response`.
4. Else re-raise.

Wired into both the schema-tool-call and text-fallback parse paths. This is distinct from the field-level defensive coercion in §3.3 (which handles known per-field LLM quirks): the backstop is the general safety net for *any* schema with cross-field validators. Provider-native constrained generation remains the upstream mitigation; the backstop is the safety net, not a per-variant patch.

---

## 4. State Validation

### 4.1 Evidence-Driven State Validator

The `StateValidator` validates investigation state consistency using the **evidence-driven** architecture with gate milestones and progress milestones.

```python
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional, Tuple

from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InvestigationProgress,
    HypothesisState,
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
        Validate gate milestone and progress milestone dependencies.

        Gate milestones: mitigation_accepted, mitigation_verified,
            solution_accepted, solution_verified
        Progress indicators: symptom_verified, cause_state, solution_proposed

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

        # solution_accepted requires solution_proposed
        if progress.solution_accepted and not progress.solution_proposed:
            issues.append(ValidationIssue(
                code="MILESTONE_ORDER_002",
                message="solution_accepted=True but solution_proposed=False",
                severity=ValidationSeverity.ERROR,
                field="progress.solution_accepted",
                suggested_fix="Set solution_proposed=True"
            ))

        # solution_verified requires solution_accepted (stage-gate dependency)
        if progress.solution_verified and not progress.solution_accepted:
            issues.append(ValidationIssue(
                code="MILESTONE_ORDER_003",
                message="solution_verified=True but solution_accepted=False",
                severity=ValidationSeverity.ERROR,
                field="progress.solution_verified",
                suggested_fix="Set solution_accepted=True or reset solution_verified=False"
            ))

        # mitigation_verified requires mitigation_accepted (stage-gate dependency).
        # Post-redesign the mitigation gates live on the single mitigation record.
        _mit = progress.mitigation
        if _mit is not None and _mit.verified and not _mit.accepted:
            issues.append(ValidationIssue(
                code="MILESTONE_ORDER_004",
                message="mitigation_verified=True but mitigation_accepted=False",
                severity=ValidationSeverity.ERROR,
                field="progress.mitigation",
                suggested_fix="Set mitigation_accepted=True or reset mitigation_verified=False"
            ))

        # cause_state == IDENTIFIED should have a likelihood (progress consistency).
        # The LLM's grounded cause signal materializes into cause_state; the
        # boolean root_cause_identified field was removed (replaced by the enum).
        if progress.cause_state == CauseState.IDENTIFIED:
            likelihood = getattr(progress, "root_cause_likelihood", None)
            if likelihood is None or likelihood == 0.0:
                issues.append(ValidationIssue(
                    code="MILESTONE_INCOMPLETE_001",
                    message="root_cause_identified=True but root_cause_likelihood is not set",
                    severity=ValidationSeverity.WARNING,
                    field="progress.root_cause_likelihood",
                    suggested_fix="Set root_cause_likelihood to confidence value"
                ))

        return issues

    def _validate_status_consistency(self, case: Case) -> List[ValidationIssue]:
        """Ensure status matches progress state."""
        issues = []

        # RESOLVED requires solution_verified
        if case.state == CaseState.RESOLVED and not case.progress.solution_verified:
            issues.append(ValidationIssue(
                code="STATUS_MISMATCH_001",
                message="Status is RESOLVED but solution_verified=False",
                severity=ValidationSeverity.ERROR,
                field="status",
                suggested_fix="Set solution_verified=True or change status to INVESTIGATING"
            ))

        # INVESTIGATING should have problem_statement
        if case.state == CaseState.INVESTIGATING:
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
            # VALIDATED is derived from the chain root node (project_hypothesis_states_from_roots
            # is the sole writer): a hypothesis reads VALIDATED iff its chain ROOT node is
            # VALIDATED. Assert that invariant via the shared is_chain_root_validated (the same
            # predicate the projection and synthesize_rcc use) — not the retired flat
            # "≥2 supporting_evidence" bar — so a node-axis-grounded hypothesis is not
            # spuriously flagged.
            if hypothesis.state == HypothesisState.VALIDATED and not is_chain_root_validated(
                hypothesis, case.causal_nodes
            ):
                issues.append(ValidationIssue(
                    code="HYPOTHESIS_STATE_001",
                    message=f"Hypothesis {hyp_id} is VALIDATED but its chain root node is not VALIDATED",
                    severity=ValidationSeverity.WARNING,
                    field=f"hypotheses.{hyp_id}",
                    suggested_fix="VALIDATED is derived from the chain root node; only project_hypothesis_states_from_roots may set it"
                ))

        return issues

    def _validate_evidence_links(self, case: Case) -> List[ValidationIssue]:
        """Validate evidence-hypothesis links are consistent."""
        issues = []
        evidence_ids = {e.evidence_id for e in case.evidence}

        for hyp_id, hypothesis in case.hypotheses.items():
            # evidence_links is a List[HypothesisEvidenceLink]; iterate
            # directly and read the FK from each link row.
            for link in hypothesis.evidence_links:
                ev_ref = link.evidence_id
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

## 5. Progress Transparency

> **Redesigned.** The `StagnationDetector` and `StagnationBreaker` have been replaced by `ProgressMonitor` (`core/investigation/progress_monitor.py`). See [Progress Transparency](./progress-transparency.md) for the full design.

### 5.1 Overview

The progress monitor tracks investigation progress per stage and surfaces milestone dependencies when progress stalls. It operates in two modes: **silent** (default) and **transparent** (activated after N investigative turns without a milestone completing).

Key design principles:

- Only detect agent-internal failures, never judge user behavior
- Influence through visibility (making the situation clear), not steering
- Stage-scoped: counter resets on milestone completion or stage change

### 5.2 Agent State Repair Patterns

The five repair patterns (HYPOTHESIS_ANCHORING, HYPOTHESIS_DEADLOCK, EXHAUSTED, FIX_FAILURE_CYCLE, ACTION_LOOP) — their stages, detection thresholds, and repair actions — are canonical in:

See **[Progress Transparency — Agent State Repair](./progress-transparency.md#agent-state-repair-exception-handling)**.

### 5.3 Integration

Called after each turn in `MilestoneEngine.process_turn()`. Prompt injection stored in `system_feedback` for next turn. `ProgressTransparencyInfo` returned in API response for frontend display.

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
            if hypothesis.likelihood < 0.3 and hypothesis.state == HypothesisState.ACTIVE:
                hypothesis.state = HypothesisState.RETIRED
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

## 8. Concurrency Protection

### 8.1 Per-Case Asyncio Locks

The `process_turn()` method performs read-modify-write on case state across an LLM invocation that takes seconds. Without protection, concurrent requests for the same case can interleave and corrupt state (lost evidence, milestone regression, duplicate hypotheses).

```python
class MilestoneEngine:
    def __init__(self):
        # Per-case asyncio locks (defaultdict creates lock on first access)
        self._case_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def process_turn(self, case: Case, ...) -> dict:
        # Acquire per-case lock — concurrent turns on DIFFERENT cases proceed in parallel
        async with self._case_locks[case.case_id]:
            return await self._process_turn_impl(case, ...)
```

**Key characteristics:**

- Locks are per-case, not global — different cases process concurrently
- Uses `asyncio.Lock` (cooperative, not OS-level) — suitable for the async architecture
- Lock is acquired at the top of `process_turn()` before any state reads
- `defaultdict(asyncio.Lock)` creates locks lazily on first access
- Concurrent requests for the same case queue and execute sequentially

---

## Summary

This error handling framework provides:

1. **Categorized error handling** - Different strategies for transient, user-recoverable, system, and logic errors

2. **Automatic retry with backoff** - For transient LLM errors

3. **Graceful parsing fallback** - Extract meaningful responses even when JSON parsing fails

4. **System feedback loop** - Wire structural validation errors and breakout prompts to next-turn context

5. **Evidence-driven state validation** - Ensure gate milestone and progress milestone consistency

6. **Progress monitoring** - Identify when investigation is stalled and surface pending-milestone guidance (includes repair patterns for anchoring, deadlock, action loops, fix-failure cycles, and exhaustion). See [Progress Transparency](./progress-transparency.md).

7. **Recovery strategies** - Memory compression, hypothesis simplification, fallback prompts

8. **Error context propagation** - Comprehensive error tracking for debugging

9. **Concurrency protection** - Per-case asyncio locks prevent state corruption from concurrent turns

**Integration Points**:

- `MilestoneEngine.process_turn()` - Per-case lock, calls StateValidator, ProgressMonitor, and stage-gate side effects
- `LLMProvider.generate()` - Uses LLMErrorHandler for retry logic
- `ResponseParser.parse()` - Handles parsing failures gracefully
- `build_investigation_context()` - Consumes system_feedback from previous turn for LLM context

---

**END OF DOCUMENT**
