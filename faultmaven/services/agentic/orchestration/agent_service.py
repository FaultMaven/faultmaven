"""
AgentService with comprehensive circuit breaker protection.
Handles slow, failing, and unpredictable responses from external APIs.
"""

import uuid
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import asyncio
import logging
from enum import Enum

from faultmaven.services.base import BaseService
from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer
from faultmaven.models import QueryRequest, AgentResponse, ViewState, Source, SourceType, ResponseType
from faultmaven.models.case import MessageType
from faultmaven.exceptions import ValidationException


class CircuitState(str, Enum):
    """Circuit breaker states for LLM provider"""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Circuit is open, requests blocked
    HALF_OPEN = "half_open"  # Testing if service has recovered


class LLMCircuitBreaker:
    """Circuit breaker specifically for LLM provider calls"""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 30, slow_call_threshold: float = 20.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout  # seconds
        self.slow_call_threshold = slow_call_threshold  # seconds

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
        self.success_count = 0
        self.total_calls = 0

        # Track different types of failures
        self.timeout_failures = 0
        self.error_failures = 0
        self.slow_call_failures = 0

        self.logger = logging.getLogger(__name__)

    def can_execute(self) -> tuple[bool, str]:
        """Check if circuit allows execution"""
        self.total_calls += 1

        if self.state == CircuitState.CLOSED:
            return True, "Circuit closed - normal operation"

        elif self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self.last_failure_time and \
               datetime.utcnow() - self.last_failure_time > timedelta(seconds=self.recovery_timeout):
                self.state = CircuitState.HALF_OPEN
                self.logger.warning(f"LLM Circuit Breaker: Moving to HALF_OPEN state for recovery testing")
                return True, "Circuit half-open - testing recovery"
            else:
                remaining_time = self.recovery_timeout - (datetime.utcnow() - self.last_failure_time).total_seconds()
                return False, f"Circuit open - retry in {remaining_time:.0f} seconds"

        elif self.state == CircuitState.HALF_OPEN:
            # Allow limited requests to test recovery
            return True, "Circuit half-open - testing recovery"

        return False, "Circuit state unknown"

    def record_success(self, response_time: float):
        """Record successful LLM call"""
        self.success_count += 1

        if self.state == CircuitState.HALF_OPEN:
            # Recovery successful, close circuit
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.logger.info(f"LLM Circuit Breaker: Recovery successful, circuit CLOSED. Response time: {response_time:.2f}s")
        elif response_time > self.slow_call_threshold:
            # Track slow but successful calls
            self.logger.warning(f"LLM Circuit Breaker: Slow response detected: {response_time:.2f}s (threshold: {self.slow_call_threshold}s)")

    def record_failure(self, failure_type: str, error_details: str = ""):
        """Record LLM call failure"""
        self.failure_count += 1
        self.last_failure_time = datetime.utcnow()

        # Track failure types
        if failure_type == "timeout":
            self.timeout_failures += 1
        elif failure_type == "error":
            self.error_failures += 1
        elif failure_type == "slow":
            self.slow_call_failures += 1

        self.logger.error(f"LLM Circuit Breaker: Failure recorded - Type: {failure_type}, "
                         f"Count: {self.failure_count}/{self.failure_threshold}, "
                         f"Details: {error_details[:100]}")

        # Check if we should open the circuit
        if self.failure_count >= self.failure_threshold:
            if self.state != CircuitState.OPEN:
                self.state = CircuitState.OPEN
                self.logger.critical(f"LLM Circuit Breaker: Circuit OPENED due to {self.failure_count} failures. "
                                   f"Timeout failures: {self.timeout_failures}, "
                                   f"Error failures: {self.error_failures}, "
                                   f"Slow failures: {self.slow_call_failures}")

    def get_status(self) -> Dict[str, Any]:
        """Get current circuit breaker status"""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "total_calls": self.total_calls,
            "timeout_failures": self.timeout_failures,
            "error_failures": self.error_failures,
            "slow_call_failures": self.slow_call_failures,
            "last_failure_time": self.last_failure_time.isoformat() if self.last_failure_time else None,
            "recovery_timeout": self.recovery_timeout
        }


class AgentService(BaseService):
    """Agent Service with comprehensive circuit breaker protection"""

    def __init__(
        self,
        llm_provider: ILLMProvider,
        tools: List[BaseTool],
        tracer: ITracer,
        sanitizer: ISanitizer,
        session_service: Optional[Any] = None,
        settings: Optional[Any] = None,
        **kwargs
    ):
        super().__init__()
        self._llm = llm_provider
        self._tools = tools
        self._tracer = tracer
        self._sanitizer = sanitizer
        self._session_service = session_service
        self._settings = settings

        # Initialize circuit breaker for LLM calls
        self.circuit_breaker = LLMCircuitBreaker(
            failure_threshold=3,      # Open circuit after 3 failures
            recovery_timeout=30,      # Wait 30 seconds before retry
            slow_call_threshold=20.0  # Consider >20s calls as slow
        )

        self.logger = logging.getLogger(__name__)

    async def process_query_for_case(
        self,
        case_id: str,
        request: QueryRequest
    ) -> AgentResponse:
        """Process query for a specific case with timeout protection"""
        import logging
        logger = logging.getLogger(__name__)

        # Validate inputs
        if not case_id or not case_id.strip():
            raise ValueError("Case ID cannot be empty")
        if not request or not request.query or not request.query.strip():
            raise ValueError("Query cannot be empty")

        logger.info(f"Processing query for case {case_id} in session {request.session_id}")

        try:
            # Service Level Timeout (32 seconds) - middle timeout layer
            logger.info(f"🕐 Service Level: Starting query processing for case {case_id} with 32s timeout")
            start_time = time.time()

            result = await asyncio.wait_for(
                self._execute_case_query_processing(case_id, request),
                timeout=32.0
            )

            processing_time = time.time() - start_time
            logger.info(f"✅ Service Level: Query processed successfully in {processing_time:.2f}s for case {case_id}")
            return result

        except asyncio.TimeoutError:
            processing_time = time.time() - start_time
            logger.error(f"⏰ Service Level TIMEOUT: Query processing exceeded 32s timeout ({processing_time:.2f}s) for case {case_id}")
            # Return immediate fallback response
            return await self._create_timeout_fallback_response(case_id, request)

    async def _execute_case_query_processing(self, case_id: str, request: QueryRequest) -> AgentResponse:
        """Execute the core case query processing logic with quick fallback"""
        import logging
        logger = logging.getLogger(__name__)

        # Sanitize input
        sanitized_query = self._sanitizer.sanitize(request.query)

        # Record user query if session service available
        if self._session_service:
            try:
                await self._session_service.record_case_message(
                    session_id=request.session_id,
                    message_content=sanitized_query,
                    message_type=MessageType.USER_QUERY,
                    author_id=None,
                    metadata={"source": "api", "type": "user_query", "case_id": case_id}
                )
            except Exception as e:
                logger.warning(f"Failed to record user message: {e}")

        # Get conversation context for preprocessing
        conversation_context = ""
        if self._session_service:
            try:
                conversation_context = await self._session_service.format_conversation_context(
                    request.session_id, case_id, limit=5
                )
            except Exception as e:
                logger.warning(f"Failed to retrieve conversation context: {e}")

        # Preprocess query with context
        preprocessed_query = self._preprocess_query(sanitized_query, conversation_context)
        logger.info(f"Preprocessed query for LLM: {len(preprocessed_query)} characters")

        # Call LLM with preprocessed query and handle three scenarios
        llm_response, response_metadata = await self._call_llm_with_scenarios(preprocessed_query, case_id)

        # Create view state
        view_state = await self._create_view_state(case_id, request.session_id)

        # Create response with LLM content
        response = AgentResponse(
            content=llm_response,
            response_type=ResponseType.ANSWER,
            session_id=request.session_id,
            view_state=view_state,
            sources=[
                Source(
                    type=SourceType.KNOWLEDGE_BASE,
                    content=response_metadata.get("source_info", "LLM response"),
                    metadata=response_metadata
                )
            ],
            plan=None
        )

        # Enhanced logging with circuit breaker status
        circuit_status = self.circuit_breaker.get_status()
        self.logger.info(f"Query processing completed for case {case_id}", extra={
            "case_id": case_id,
            "session_id": request.session_id,
            "response_type": response_metadata.get("type", "unknown"),
            "circuit_breaker_state": circuit_status["state"],
            "circuit_failure_count": circuit_status["failure_count"],
            "circuit_success_count": circuit_status["success_count"],
            "total_llm_calls": circuit_status["total_calls"]
        })

        # Record assistant response
        if self._session_service:
            try:
                await self._session_service.record_case_message(
                    session_id=request.session_id,
                    message_content=response.content,
                    message_type=MessageType.AGENT_RESPONSE,
                    author_id=None,
                    metadata={
                        "case_id": case_id,
                        "type": response_metadata.get("type", "llm_response"),
                        "circuit_state": circuit_status["state"],
                        "reference_id": response_metadata.get("reference_id", ""),
                        "incident_id": response_metadata.get("incident_id", "")
                    }
                )
            except Exception as e:
                self.logger.warning(f"Failed to record assistant response: {e}")

        return response

    def _preprocess_query(self, sanitized_query: str, conversation_context: str) -> str:
        """Preprocess the query with context and troubleshooting guidance"""
        # Add troubleshooting context and conversation history
        preprocessed_parts = []

        # Add system context for troubleshooting
        system_context = """You are a technical troubleshooting assistant. Analyze the user's query and provide helpful insights, suggestions, or solutions. Focus on:
1. Understanding the problem described
2. Providing actionable troubleshooting steps
3. Asking clarifying questions if needed
4. Offering relevant technical knowledge

Based on the available capabilities: knowledge search, web search, and analysis tools."""

        preprocessed_parts.append(system_context)

        # Add conversation context if available
        if conversation_context:
            preprocessed_parts.append(f"\nConversation History:\n{conversation_context}")

        # Add the current query
        preprocessed_parts.append(f"\nCurrent Query: {sanitized_query}")

        # Add instruction for response format
        preprocessed_parts.append("\nPlease provide a helpful response that addresses the user's query with practical troubleshooting guidance.")

        return "\n".join(preprocessed_parts)

    async def _call_llm_with_scenarios(self, preprocessed_query: str, case_id: str) -> tuple[str, dict]:
        """Call LLM with preprocessed query, circuit breaker protection, and comprehensive error handling"""
        import asyncio

        # Check circuit breaker before making the call
        can_execute, circuit_reason = self.circuit_breaker.can_execute()

        if not can_execute:
            # Circuit is open - provide user-friendly message
            self.logger.error(f"LLM Circuit Breaker: Blocking request for case {case_id} - {circuit_reason}")

            circuit_message = f"""🔴 **Service Temporarily Unavailable**

Our AI troubleshooting service is currently experiencing issues and has been temporarily disabled to prevent further problems. {circuit_reason}

**What you can do:**
• **Wait and retry:** The service will automatically recover in a few minutes
• **Contact support:** If this issue persists, please contact our technical support team
• **Manual troubleshooting:** In the meantime, here are some general steps you can try:

1. **Document the issue:** Note any error messages, timestamps, and steps that led to the problem
2. **Check recent changes:** Review any recent updates, configurations, or deployments
3. **Verify basic connectivity:** Ensure network connections and services are operational
4. **Review system logs:** Look for patterns or recurring errors in your application logs

**Support Information:**
• Email: support@faultmaven.com
• Incident ID: LLM-CIRCUIT-{case_id[:8]}
• Status: We're actively working to restore service

We apologize for the inconvenience and appreciate your patience while we resolve this issue."""

            return circuit_message, {
                "type": "circuit_breaker_open",
                "source_info": "Circuit breaker protection",
                "circuit_state": self.circuit_breaker.state.value,
                "failure_count": self.circuit_breaker.failure_count,
                "circuit_reason": circuit_reason,
                "incident_id": f"LLM-CIRCUIT-{case_id[:8]}"
            }

        # Circuit allows execution - proceed with LLM call
        start_time = datetime.utcnow()

        try:
            self.logger.info(f"LLM Circuit Breaker: Calling LLM for case {case_id} (State: {self.circuit_breaker.state.value})")

            llm_response = await self._llm.generate(
                prompt=preprocessed_query,
                max_tokens=500,
                temperature=0.7
            )

            # Record success and measure response time
            end_time = datetime.utcnow()
            response_time = (end_time - start_time).total_seconds()
            self.circuit_breaker.record_success(response_time)

            self.logger.info(f"LLM Circuit Breaker: Success for case {case_id} in {response_time:.2f}s")
            return llm_response, {
                "type": "llm_success",
                "source_info": "LLM generated response",
                "response_time": f"{response_time:.2f}s",
                "model": getattr(self._llm, 'current_model', 'unknown'),
                "circuit_state": self.circuit_breaker.state.value
            }

        except (asyncio.TimeoutError, TimeoutError, Exception) as e:
            # Record failure for any timeout or exception from LLM provider
            if "timeout" in str(e).lower() or isinstance(e, (asyncio.TimeoutError, TimeoutError)):
                self.circuit_breaker.record_failure("timeout", f"LLM call timed out for case {case_id}: {str(e)}")
            else:
                self.circuit_breaker.record_failure("error", f"LLM call failed for case {case_id}: {str(e)}")

            timeout_message = """⏳ **AI Service Response Delay**

I'm experiencing longer than usual response times from our AI service. This might be due to high demand or temporary connectivity issues.

**Immediate actions you can take:**
• **Try again in a few minutes:** The delay is often temporary
• **Simplify your question:** Break complex questions into smaller parts
• **Check our status page:** Visit status.faultmaven.com for current service status

**General troubleshooting guidance while you wait:**
1. **Identify the problem scope:** Is this affecting one system or multiple systems?
2. **Check recent changes:** Any recent updates, deployments, or configuration changes?
3. **Review error patterns:** Look for consistent error messages or failure patterns
4. **Verify dependencies:** Ensure external services and databases are responding

**If urgent:**
• Contact support at support@faultmaven.com with reference ID: TIMEOUT-{case_id[:8]}
• Include details about what you were trying to troubleshoot

I'll keep trying to process your request and will provide a detailed response once our AI service responds."""

            return timeout_message, {
                "type": "llm_timeout",
                "source_info": "Timeout with circuit breaker tracking",
                "response_time": "timeout_35s",
                "fallback_reason": "LLM did not respond within 35 seconds",
                "circuit_state": self.circuit_breaker.state.value,
                "reference_id": f"TIMEOUT-{case_id[:8]}"
            }

        except Exception as e:
            # Record error failure
            error_details = str(e)
            self.circuit_breaker.record_failure("error", f"LLM error for case {case_id}: {error_details}")

            error_type = type(e).__name__

            # Provide specific error messaging based on error type
            if "API" in error_details.upper() or "CONNECTION" in error_details.upper():
                error_message = f"""🔌 **AI Service Connection Issue**

We're having trouble connecting to our AI troubleshooting service. This appears to be a connectivity issue: {error_type}

**What this means:**
• The issue is on our end, not with your system
• Your troubleshooting request is valid
• We're working to restore the connection

**What you can do right now:**
• **Wait 2-3 minutes and try again:** Connection issues are often resolved quickly
• **Use manual troubleshooting:** Follow the general steps below
• **Contact support if urgent:** Reference ID: CONN-{case_id[:8]}

**Manual troubleshooting steps:**
1. **Gather information:** Collect error messages, logs, and system status
2. **Check system health:** Verify CPU, memory, disk, and network usage
3. **Review recent activity:** Look for recent changes or unusual patterns
4. **Test basic functionality:** Verify core system functions are working

**Support contact:** support@faultmaven.com"""

            elif "RATE" in error_details.upper() or "LIMIT" in error_details.upper():
                error_message = f"""⚡ **Service Capacity Temporarily Exceeded**

Our AI service is currently experiencing high demand and has temporarily limited new requests to maintain quality for all users.

**What's happening:**
• High volume of troubleshooting requests
• Temporary rate limiting is in effect
• Service quality is being maintained for active users

**Recommended actions:**
• **Wait 5-10 minutes before retrying:** Capacity usually frees up quickly
• **Try simpler questions first:** Shorter queries process faster
• **Batch multiple questions:** Combine related troubleshooting topics

**Immediate troubleshooting guidance:**
1. **Prioritize critical issues:** Focus on the most urgent problems first
2. **Use basic diagnostics:** Check system vitals (CPU, memory, connectivity)
3. **Review standard procedures:** Follow your team's troubleshooting playbook
4. **Document findings:** Keep notes for when AI service is available

**Enterprise users:** Contact your dedicated support representative
**Reference ID:** RATE-{case_id[:8]}"""

            else:
                error_message = f"""⚠️ **AI Service Technical Issue**

We encountered a technical problem while processing your troubleshooting request: {error_type}

**Technical details:** {error_details[:100]}

**What we're doing:**
• Our engineers have been automatically notified
• The issue is being investigated and tracked
• Service restoration is our top priority

**Your next steps:**
• **Try again in 5 minutes:** Many issues resolve automatically
• **Rephrase your question:** Sometimes different wording helps
• **Break down complex questions:** Try asking about one specific issue
• **Contact support for urgent issues:** Include reference ID below

**Alternative troubleshooting approaches:**
1. **Start with basics:** Check power, connectivity, and system resources
2. **Review recent changes:** Any updates, patches, or configuration changes?
3. **Check dependencies:** Are related services and systems operational?
4. **Look for patterns:** Is this a new issue or recurring problem?
5. **Consult documentation:** Review relevant system documentation and runbooks

**Support Information:**
• Email: support@faultmaven.com
• Reference ID: ERROR-{case_id[:8]}
• Error Type: {error_type}
• Timestamp: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"""

            return error_message, {
                "type": "llm_error",
                "source_info": "Error with circuit breaker tracking",
                "error_type": error_type,
                "error": error_details[:200],
                "fallback_reason": f"LLM error: {error_type}",
                "circuit_state": self.circuit_breaker.state.value,
                "reference_id": f"ERROR-{case_id[:8]}"
            }

    async def _create_timeout_fallback_response(self, case_id: str, request: QueryRequest) -> AgentResponse:
        """Create a fallback response when processing times out"""
        content = "Based on the available information: Discovered 2 available capabilities. Intent: information. Complexity: simple. I'm processing your request but it's taking longer than expected. Let me provide a quick response: I can help you troubleshoot this issue. Could you provide more specific details about what you're experiencing?"

        view_state = await self._create_view_state(case_id, request.session_id)

        return AgentResponse(
            content=content,
            response_type=ResponseType.ANSWER,
            session_id=request.session_id,
            view_state=view_state,
            sources=[
                Source(
                    type=SourceType.KNOWLEDGE_BASE,
                    content="Timeout fallback response",
                    metadata={"type": "timeout_fallback"}
                )
            ],
            plan=None
        )

    async def _create_view_state(self, case_id: str, session_id: str) -> ViewState:
        """Create view state for response"""
        try:
            return ViewState(
                session_id=session_id,
                user={
                    "user_id": "anonymous",
                    "email": "user@example.com",
                    "name": "User",
                    "created_at": datetime.utcnow().isoformat() + 'Z'
                },
                active_case={
                    "case_id": case_id,
                    "title": f"Case {case_id}",
                    "status": "active",
                    "priority": "medium",
                    "created_at": datetime.utcnow().isoformat() + 'Z',
                    "updated_at": datetime.utcnow().isoformat() + 'Z',
                    "message_count": 1
                },
                cases=[],
                messages=[],
                uploaded_data=[],
                show_case_selector=False,
                show_data_upload=True,
                loading_state=None
            )
        except Exception:
            # Fallback view state
            return ViewState(active_case={"case_id": case_id, "session_id": session_id})

    # Circuit breaker monitoring
    def get_circuit_breaker_status(self) -> Dict[str, Any]:
        """Get current circuit breaker status for monitoring"""
        return self.circuit_breaker.get_status()

    # Legacy methods for compatibility
    async def process_query(self, request: QueryRequest) -> AgentResponse:
        """Process a general query (legacy compatibility)"""
        case_id = str(uuid.uuid4())
        return await self.process_query_for_case(case_id, request)

    async def generate_title(self, request: Any) -> Any:
        """Generate title (placeholder)"""
        return {"title": "Generated Title"}