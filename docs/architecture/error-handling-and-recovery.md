# Error Handling and Recovery Patterns

**Version**: 1.0  
**Last Updated**: 2025-10-11  
**Status**: Operational Design  
**Source**: Created from investigation framework operational patterns

---

## Overview

This document defines error handling and recovery strategies for the FaultMaven investigation framework, ensuring graceful degradation and automatic recovery from failure conditions.

---

## Table of Contents

1. [Error Categories](#error-categories)
2. [LLM Error Handling](#llm-error-handling)
3. [Response Parsing Errors](#response-parsing-errors)
4. [State Corruption Detection](#state-corruption-detection)
5. [Infinite Loop Prevention](#infinite-loop-prevention)
6. [Recovery Strategies](#recovery-strategies)
7. [Error Context Propagation](#error-context-propagation)

---

## Error Categories

### 1. Transient Errors (Retryable)
- LLM API rate limiting
- Network timeouts
- Temporary service unavailability
- Redis connection failures

**Strategy**: Automatic retry with exponential backoff

### 2. User-Recoverable Errors
- Invalid user input
- Missing required information
- Ambiguous evidence classification
- Blocked evidence access

**Strategy**: Request clarification or alternative input

### 3. System Errors (Escalation Required)
- LLM authentication failures
- State corruption
- Database connection loss
- Unrecoverable parsing failures

**Strategy**: Log error, notify monitoring, suggest escalation

### 4. Logic Errors (Investigation Stalls)
- Infinite OODA loops
- Hypothesis anchoring
- Evidence contradictions
- Progress stall

**Strategy**: Detect and force alternative paths

---

## LLM Error Handling

### Error Handler Implementation

```python
class LLMErrorHandler:
    """Handles LLM API errors with automatic recovery"""
    
    def __init__(self):
        self.max_retries = 3
        self.base_delay = 2  # seconds
    
    async def handle_llm_error(
        self, 
        error: Exception, 
        state: InvestigationState,
        retry_count: int = 0
    ) -> AgentResponse:
        """
        Handle LLM API errors with appropriate recovery
        
        Returns AgentResponse with error info and retry guidance
        """
        
        if isinstance(error, RateLimitError):
            return await self._handle_rate_limit(error, state, retry_count)
        
        elif isinstance(error, TimeoutError):
            return await self._handle_timeout(error, state, retry_count)
        
        elif isinstance(error, AuthenticationError):
            return self._handle_auth_error(error, state)
        
        elif isinstance(error, InvalidRequestError):
            return self._handle_invalid_request(error, state)
        
        else:
            return self._handle_unknown_error(error, state, retry_count)
    
    async def _handle_rate_limit(
        self, 
        error: RateLimitError, 
        state: InvestigationState,
        retry_count: int
    ) -> AgentResponse:
        """Handle rate limiting with exponential backoff"""
        
        if retry_count >= self.max_retries:
            return AgentResponse(
                answer="⚠️ LLM service temporarily unavailable. Please try again in a few minutes.",
                should_retry=False,
                error_code="RATE_LIMIT_EXCEEDED"
            )
        
        # Calculate backoff delay
        delay = self.base_delay * (2 ** retry_count)
        
        # Wait and retry
        await asyncio.sleep(delay)
        
        return AgentResponse(
            answer=f"Experiencing rate limiting. Retrying in {delay}s...",
            should_retry=True,
            retry_delay_seconds=delay,
            retry_count=retry_count + 1
        )
    
    async def _handle_timeout(
        self, 
        error: TimeoutError, 
        state: InvestigationState,
        retry_count: int
    ) -> AgentResponse:
        """Handle request timeouts"""
        
        if retry_count >= self.max_retries:
            return AgentResponse(
                answer="⚠️ Request timed out multiple times. The investigation prompt may be too complex. Would you like me to simplify?",
                should_retry=False,
                error_code="TIMEOUT_EXCEEDED",
                suggested_actions=["simplify_prompt", "break_into_steps"]
            )
        
        # Retry with simplified prompt
        return AgentResponse(
            answer="Request timed out. Simplifying prompt and retrying...",
            should_retry=True,
            use_simpler_prompt=True,
            retry_count=retry_count + 1
        )
    
    def _handle_auth_error(
        self, 
        error: AuthenticationError, 
        state: InvestigationState
    ) -> AgentResponse:
        """Handle authentication errors (non-retryable)"""
        
        logging.error(f"LLM authentication error: {str(error)}")
        
        return AgentResponse(
            answer="⚠️ System configuration error. Please contact support.",
            should_retry=False,
            error_code="AUTH_FAILED",
            escalate=True
        )
    
    def _handle_invalid_request(
        self, 
        error: InvalidRequestError, 
        state: InvestigationState
    ) -> AgentResponse:
        """Handle invalid request errors"""
        
        logging.warning(f"Invalid LLM request: {str(error)}")
        
        # Check for token limit exceeded
        if "token" in str(error).lower():
            return AgentResponse(
                answer="⚠️ Investigation context too large. Compressing memory...",
                should_retry=True,
                force_memory_compression=True
            )
        
        return AgentResponse(
            answer=f"⚠️ Invalid request: {str(error)}. Please try rephrasing.",
            should_retry=False,
            error_code="INVALID_REQUEST"
        )
    
    def _handle_unknown_error(
        self, 
        error: Exception, 
        state: InvestigationState,
        retry_count: int
    ) -> AgentResponse:
        """Handle unknown errors"""
        
        logging.error(f"Unknown LLM error: {str(error)}", exc_info=True)
        
        if retry_count < self.max_retries:
            return AgentResponse(
                answer=f"Unexpected error occurred. Retrying... (attempt {retry_count + 1}/{self.max_retries})",
                should_retry=True,
                retry_count=retry_count + 1
            )
        
        return AgentResponse(
            answer=f"⚠️ Persistent error: {str(error)}. Investigation may need manual intervention.",
            should_retry=False,
            error_code="UNKNOWN_ERROR",
            escalate=True
        )
```

---

## Response Parsing Errors

### Graceful Parsing Fallback

```python
class ResponseParser:
    """Parse LLM responses with fallback handling"""
    
    def parse(
        self, 
        llm_response: str, 
        expected_format: str
    ) -> Dict:
        """
        Parse LLM response with fallback handling
        
        Returns parsed response or fallback structure
        """
        
        # Try to extract JSON from response
        try:
            json_match = self._extract_json(llm_response)
            
            if json_match:
                parsed = json.loads(json_match)
                return self._validate_response(parsed, expected_format)
        
        except json.JSONDecodeError as e:
            logging.warning(f"JSON parsing failed: {str(e)}")
            # Fall through to text extraction
        
        except ValueError as e:
            logging.warning(f"Response validation failed: {str(e)}")
            # Fall through to text extraction
        
        # Fallback: text-based extraction
        return self._extract_from_text(llm_response, expected_format)
    
    def _extract_from_text(self, response: str, expected_format: str) -> Dict:
        """
        Fallback: extract key information from text when JSON parsing fails
        """
        
        logging.info(f"Using text extraction fallback for format: {expected_format}")
        
        # Basic extraction for critical fields
        extracted = {
            "answer": response,
            "parse_fallback": True,
            "parse_method": "text_extraction"
        }
        
        # Try to extract specific fields based on expected format
        if "phase_complete" in expected_format.lower():
            if any(phrase in response.lower() for phrase in ["phase complete", "✅", "ready to advance"]):
                extracted["phase_complete"] = True
        
        if "hypothesis" in expected_format.lower():
            # Extract hypothesis-like statements
            hypotheses = self._extract_hypothesis_statements(response)
            if hypotheses:
                extracted["hypotheses"] = hypotheses
        
        if "evidence" in expected_format.lower():
            # Extract evidence request indicators
            if any(phrase in response.lower() for phrase in ["need", "please provide", "can you share"]):
                extracted["evidence_requested"] = True
        
        return extracted
    
    def _extract_hypothesis_statements(self, text: str) -> List[str]:
        """Extract hypothesis-like statements from text"""
        
        # Look for numbered lists or bullet points
        hypotheses = []
        
        # Pattern: "Hypothesis N:" or "Theory N:"
        hyp_pattern = r'(?:Hypothesis|Theory)\s+\d+[:\-]\s*(.+?)(?:\n|$)'
        matches = re.findall(hyp_pattern, text, re.MULTILINE)
        
        if matches:
            hypotheses.extend(matches)
        
        # Pattern: Numbered statements
        num_pattern = r'^\d+\.\s+(.+?)(?:\n|$)'
        matches = re.findall(num_pattern, text, re.MULTILINE)
        
        if matches and len(matches) <= 5:  # Likely hypotheses if <= 5 items
            hypotheses.extend(matches)
        
        return hypotheses[:5]  # Limit to 5 hypotheses
```

---

## State Corruption Detection

### State Validation and Repair

```python
class StateValidator:
    """Validates and repairs investigation state"""
    
    def validate_and_repair(self, state: InvestigationState) -> InvestigationState:
        """
        Validate state integrity and repair if corrupted
        
        Returns repaired state or raises if unrecoverable
        """
        
        # Validate phase bounds
        state = self._validate_phase(state)
        
        # Validate OODA consistency
        state = self._validate_ooda_state(state)
        
        # Validate confidence scores
        state = self._validate_confidence_scores(state)
        
        # Validate evidence integrity
        state = self._validate_evidence(state)
        
        # Validate hypothesis integrity
        state = self._validate_hypotheses(state)
        
        return state
    
    def _validate_phase(self, state: InvestigationState) -> InvestigationState:
        """Validate and repair phase number"""
        
        if state.current_phase < 0 or state.current_phase > 6:
            logging.error(f"Invalid phase {state.current_phase}, resetting to 0")
            state.current_phase = 0
            state.engagement_mode = "consultant"  # Reset to consultant
        
        return state
    
    def _validate_ooda_state(self, state: InvestigationState) -> InvestigationState:
        """Validate OODA state consistency"""
        
        # OODA should only be active in phases 1-5
        if state.current_phase not in [1, 2, 3, 4, 5] and state.ooda_active:
            logging.warning(f"OODA active in phase {state.current_phase}, deactivating")
            state.ooda_active = False
            state.current_ooda_step = None
        
        # If OODA active, ensure current_step is valid
        if state.ooda_active and not state.current_ooda_step:
            logging.warning("OODA active but no current step, setting to OBSERVE")
            state.current_ooda_step = OODAStep.OBSERVE
        
        return state
    
    def _validate_confidence_scores(self, state: InvestigationState) -> InvestigationState:
        """Validate and clamp confidence scores"""
        
        # Validate anomaly frame confidence
        if state.anomaly_frame:
            if state.anomaly_frame.confidence < 0 or state.anomaly_frame.confidence > 1:
                logging.warning(f"Invalid anomaly confidence {state.anomaly_frame.confidence}, clamping")
                state.anomaly_frame.confidence = max(0.0, min(1.0, state.anomaly_frame.confidence))
        
        # Validate hypothesis likelihoods
        for hyp in state.hypotheses:
            if hyp.likelihood < 0 or hyp.likelihood > 1:
                logging.warning(f"Invalid hypothesis likelihood {hyp.likelihood}, clamping")
                hyp.likelihood = max(0.0, min(1.0, hyp.likelihood))
        
        return state
    
    def _validate_evidence(self, state: InvestigationState) -> InvestigationState:
        """Validate evidence items"""
        
        # Remove evidence with invalid IDs
        invalid_ids = [eid for eid in state.evidence_items.keys() if not eid.startswith("ev-")]
        
        if invalid_ids:
            logging.warning(f"Removing {len(invalid_ids)} evidence items with invalid IDs")
            for eid in invalid_ids:
                del state.evidence_items[eid]
        
        return state
    
    def _validate_hypotheses(self, state: InvestigationState) -> InvestigationState:
        """Validate hypothesis list"""
        
        # Remove duplicate hypotheses (same statement)
        seen_statements = set()
        unique_hypotheses = []
        
        for hyp in state.hypotheses:
            if hyp.statement.lower() not in seen_statements:
                seen_statements.add(hyp.statement.lower())
                unique_hypotheses.append(hyp)
        
        if len(unique_hypotheses) < len(state.hypotheses):
            logging.warning(f"Removed {len(state.hypotheses) - len(unique_hypotheses)} duplicate hypotheses")
            state.hypotheses = unique_hypotheses
        
        return state
```

---

## Infinite Loop Prevention

### Loop Detection

```python
class InfiniteLoopDetector:
    """Detects and prevents infinite investigation loops"""
    
    def detect_loop(self, state: InvestigationState) -> tuple[bool, Optional[str]]:
        """
        Detect if investigation is in an infinite loop
        
        Returns (is_looping, loop_description)
        """
        
        # Pattern 1: Same OODA step repeated 5+ times
        if self._detect_ooda_step_loop(state):
            return True, "Same OODA step repeated 5+ times"
        
        # Pattern 2: Same hypothesis tested repeatedly
        if self._detect_hypothesis_loop(state):
            return True, "Same hypothesis tested 3+ times without progress"
        
        # Pattern 3: No state changes in 5+ turns
        if self._detect_stagnation(state):
            return True, "No meaningful state changes in 5 turns"
        
        return False, None
    
    def _detect_ooda_step_loop(self, state: InvestigationState) -> bool:
        """Detect repeated OODA step"""
        
        if len(state.ooda_iterations) < 5:
            return False
        
        last_5_steps = [
            iter.current_step 
            for iter in state.ooda_iterations[-5:]
        ]
        
        # If same step repeated 5 times
        if len(set(last_5_steps)) == 1:
            logging.warning(f"OODA loop detected: step {last_5_steps[0]} repeated 5 times")
            return True
        
        return False
    
    def _detect_hypothesis_loop(self, state: InvestigationState) -> bool:
        """Detect same hypothesis tested repeatedly"""
        
        # Count hypothesis test attempts
        test_counts = {}
        
        for hyp in state.hypotheses:
            key = hyp.statement.lower()
            test_counts[key] = test_counts.get(key, 0) + (1 if hyp.tested else 0)
        
        # If any hypothesis tested 3+ times
        max_tests = max(test_counts.values()) if test_counts else 0
        if max_tests >= 3:
            logging.warning(f"Hypothesis loop detected: tested {max_tests} times")
            return True
        
        return False
    
    def _detect_stagnation(self, state: InvestigationState) -> bool:
        """Detect lack of progress"""
        
        if state.current_turn < 5:
            return False
        
        # Check if phase or confidence changed in last 5 turns
        if len(state.phase_history) == 0:
            return True
        
        recent_phase_changes = [
            ph for ph in state.phase_history 
            if state.current_turn - ph.transition_turn <= 5
        ]
        
        if not recent_phase_changes:
            logging.warning("Stagnation detected: no phase changes in 5 turns")
            return True
        
        return False
    
    def break_loop(self, state: InvestigationState, loop_type: str) -> InvestigationState:
        """
        Break out of detected loop
        
        Strategy: Force alternative action or escalate
        """
        
        if "ooda" in loop_type.lower():
            # Force skip to next OODA step or phase
            logging.info("Breaking OODA loop: forcing phase transition")
            state.force_phase_transition = True
        
        elif "hypothesis" in loop_type.lower():
            # Retire current hypothesis, force new category
            logging.info("Breaking hypothesis loop: forcing alternative category")
            state.force_alternative_hypothesis = True
        
        elif "stagnation" in loop_type.lower():
            # Recommend escalation
            logging.info("Breaking stagnation: recommending escalation")
            state.escalation_recommended = True
            state.escalation_reason = "Investigation not making progress after 5 turns"
        
        return state
```

---

## Recovery Strategies

### Automatic Recovery Actions

```python
class RecoveryManager:
    """Manages automatic recovery from error conditions"""
    
    async def attempt_recovery(
        self, 
        error: Exception, 
        state: InvestigationState,
        context: Dict
    ) -> RecoveryResult:
        """
        Attempt automatic recovery from error
        
        Returns RecoveryResult with success status and recovered state
        """
        
        recovery_strategies = [
            self._try_memory_compression,
            self._try_state_simplification,
            self._try_alternative_prompt,
            self._try_phase_reset
        ]
        
        for strategy in recovery_strategies:
            try:
                result = await strategy(error, state, context)
                
                if result.success:
                    logging.info(f"Recovery successful using {strategy.__name__}")
                    return result
            
            except Exception as e:
                logging.warning(f"Recovery strategy {strategy.__name__} failed: {str(e)}")
                continue
        
        # All strategies failed
        return RecoveryResult(
            success=False,
            state=state,
            message="All recovery strategies failed. Manual intervention required."
        )
    
    async def _try_memory_compression(
        self, 
        error: Exception, 
        state: InvestigationState,
        context: Dict
    ) -> RecoveryResult:
        """Try compressing memory to reduce token count"""
        
        if "token" not in str(error).lower():
            return RecoveryResult(success=False, state=state)
        
        # Force memory compression
        memory = HierarchicalMemory()
        await memory.compress(state, force=True)
        
        return RecoveryResult(
            success=True,
            state=state,
            message="Memory compressed to reduce token usage"
        )
    
    async def _try_state_simplification(
        self, 
        error: Exception, 
        state: InvestigationState,
        context: Dict
    ) -> RecoveryResult:
        """Try simplifying state by retiring old hypotheses"""
        
        # Retire hypotheses with low likelihood
        for hyp in state.hypotheses:
            if hyp.likelihood < 0.3 and hyp.status != "retired":
                hyp.status = "retired"
                hyp.retirement_reason = "Low likelihood during error recovery"
        
        return RecoveryResult(
            success=True,
            state=state,
            message="State simplified by retiring low-likelihood hypotheses"
        )
    
    async def _try_alternative_prompt(
        self, 
        error: Exception, 
        state: InvestigationState,
        context: Dict
    ) -> RecoveryResult:
        """Try using simplified prompt tier"""
        
        context["prompt_tier"] = "light"  # Force simplest prompt
        
        return RecoveryResult(
            success=True,
            state=state,
            message="Using simplified prompt tier"
        )
    
    async def _try_phase_reset(
        self, 
        error: Exception, 
        state: InvestigationState,
        context: Dict
    ) -> RecoveryResult:
        """Try resetting to previous phase"""
        
        if not state.phase_history:
            return RecoveryResult(success=False, state=state)
        
        # Revert to previous phase
        prev_phase = state.phase_history[-1]
        state.current_phase = prev_phase.phase_number
        
        return RecoveryResult(
            success=True,
            state=state,
            message=f"Reset to previous phase {prev_phase.phase_number}"
        )
```

---

## Error Context Propagation

### Error Context Model

```python
@dataclass
class ErrorContext:
    """Comprehensive error context for debugging"""
    
    error_id: str
    timestamp: datetime
    error_type: str
    error_message: str
    stack_trace: str
    
    # Investigation context
    investigation_id: str
    current_phase: int
    current_turn: int
    engagement_mode: str
    
    # Operation context
    operation: str  # "phase_transition", "ooda_step", "evidence_collection", etc.
    operation_params: Dict
    
    # State context
    state_snapshot: Dict  # Minimal state snapshot
    
    # Recovery context
    recovery_attempted: bool = False
    recovery_strategy: Optional[str] = None
    recovery_success: bool = False
    
    def to_dict(self) -> Dict:
        """Convert to dict for logging/monitoring"""
        return {
            "error_id": self.error_id,
            "timestamp": self.timestamp.isoformat(),
            "error_type": self.error_type,
            "error_message": self.error_message,
            "investigation_id": self.investigation_id,
            "current_phase": self.current_phase,
            "operation": self.operation,
            "recovery_success": self.recovery_success
        }
```

---

**END OF DOCUMENT**


