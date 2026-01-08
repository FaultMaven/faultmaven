"""Agent Orchestration Service (TASK-015)

Purpose: Service layer for AI agent orchestration and execution that
coordinates multi-step troubleshooting investigations with LLM-powered
agents, tool invocations, and streaming responses.

This service bridges the REST API layer (TASK-014) with the LLM integration,
providing:
1. Agent execution workflow with state machine
2. Tool invocation coordination
3. Streaming response handling
4. Error handling and retry logic
5. Token budget tracking and session pause on budget exhaustion

Architecture:
    FastAPI Routes -> AgentOrchestrationService -> LLMClient + Tools

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from faultmaven.services.base import BaseService
from faultmaven.models.investigation_session import InvestigationSession, SessionStatus
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)
from faultmaven.modules.agent.infrastructure.persistence.agent_execution_repository import (
    AgentExecutionRepository,
)
from faultmaven.infrastructure.persistence.case_repository import CaseRepository
from faultmaven.domain.events import (
    AgentContext,
    ExecutionEvent,
    ExecutionEventType,
    LLMEvent,
    LLMEventType,
    Message,
    MessageRole,
    Tool,
    ToolCall,
    ToolResult as DomainToolResult,
)
from faultmaven.modules.agent.tools.base import (
    AgentToolRegistry,
    ToolContext,
    tool_registry as agent_tool_registry,
)
from faultmaven.integrations.llm_client import LLMClient, LLMProvider, create_llm_client
from faultmaven.exceptions import (
    NotFoundError,
    AuthorizationError,
    ConflictError,
    ValidationException,
    ServiceError,
    LLMException,
)

logger = logging.getLogger(__name__)


# Agent system prompts by type
AGENT_SYSTEM_PROMPTS = {
    AgentType.INVESTIGATOR: """You are an expert troubleshooting investigator for technical issues.

Your role is to:
1. Analyze the problem systematically using the OODA loop (Observe, Orient, Decide, Act)
2. Generate hypotheses about root causes based on evidence
3. Use available tools to examine evidence files and gather information
4. Ask clarifying questions when needed
5. Provide clear, actionable recommendations

When investigating:
- Start by reviewing the available evidence using list_evidence and read_file tools
- Look for error patterns, timestamps, and correlations
- Consider multiple hypotheses before concluding
- Document your reasoning process
- Be specific about what you find

Available tools:
- list_evidence: List all evidence files uploaded for this case
- read_file: Read the contents of an evidence file by ID
- search_knowledge: Search the knowledge base for relevant information (coming soon)

Always explain your reasoning and next steps clearly.""",

    AgentType.DEBUGGER: """You are a debugging specialist focused on code and log analysis.

Your role is to:
1. Dive deep into stack traces, error messages, and code
2. Identify the specific location and cause of bugs
3. Trace execution flow through logs and code
4. Suggest specific fixes with code examples
5. Validate that your analysis is consistent with the evidence

When debugging:
- Examine stack traces carefully for the root cause
- Look for patterns in error messages
- Check log timestamps for sequence of events
- Consider thread safety, race conditions, and edge cases
- Provide specific line numbers and code suggestions when possible

Use the available tools to examine evidence files thoroughly.""",

    AgentType.RESEARCHER: """You are a research assistant focused on finding relevant information.

Your role is to:
1. Search knowledge bases for relevant documentation
2. Find similar issues and their solutions
3. Locate best practices and recommended approaches
4. Compile relevant information for the investigation
5. Cite sources for your findings

When researching:
- Use search_knowledge to find relevant documentation (when available)
- Look for similar issues in the evidence
- Identify patterns that match known problems
- Provide links or references when possible
- Summarize key findings clearly""",

    AgentType.VALIDATOR: """You are a validation engineer focused on testing hypotheses.

Your role is to:
1. Verify hypotheses against available evidence
2. Test whether proposed solutions would work
3. Identify potential issues with solutions
4. Confirm root cause analysis is correct
5. Flag any inconsistencies in the analysis

When validating:
- Check if the hypothesis explains all symptoms
- Look for contradicting evidence
- Verify the timeline of events makes sense
- Consider alternative explanations
- Rate confidence level of the diagnosis""",

    AgentType.REPORTER: """You are a technical report writer focused on clear communication.

Your role is to:
1. Summarize investigation findings clearly
2. Document the root cause and solution
3. Write actionable recommendations
4. Create follow-up action items
5. Format reports professionally

When reporting:
- Use clear, concise language
- Structure the report with sections (Summary, Analysis, Recommendations)
- Include relevant evidence references
- Prioritize action items by importance
- Note any caveats or limitations""",
}


@dataclass
class ExecutionResult:
    """Result of an agent execution."""

    execution_id: str
    status: ExecutionStatus
    response: str
    token_usage: Dict[str, int]
    tool_calls: List[Dict[str, Any]]
    duration_ms: int
    error_message: Optional[str] = None


class AgentOrchestrationService(BaseService):
    """Service for AI agent orchestration and execution.

    This service coordinates the execution of AI agents for troubleshooting
    investigations, including:
    - Agent execution workflow management
    - LLM API calls with streaming
    - Tool invocation and result handling
    - Token budget tracking
    - Error handling and retry logic

    Attributes:
        session_service: Investigation session service
        evidence_service: Evidence artifact service
        execution_repo: Agent execution repository
        case_repo: Case repository
        tool_registry: Registry of available tools
        llm_client: LLM client for API calls
    """

    def __init__(
        self,
        execution_repo: AgentExecutionRepository,
        case_repo: CaseRepository,
        session_service: Optional[Any] = None,
        evidence_service: Optional[Any] = None,
        tool_registry: Optional[AgentToolRegistry] = None,
        llm_client: Optional[LLMClient] = None,
        max_retries: int = 3,
        retry_initial_delay: float = 1.0,
        tool_timeout: int = 30,
        max_parallel_tools: int = 5,
    ):
        """Initialize agent orchestration service.

        Args:
            execution_repo: Agent execution repository
            case_repo: Case repository
            session_service: Investigation session service (injected via DI if None)
            evidence_service: Evidence artifact service (injected via DI if None)
            tool_registry: Registry of available tools (uses global if not provided)
            llm_client: LLM client (creates default if not provided)
            max_retries: Maximum retry attempts for LLM calls
            retry_initial_delay: Initial retry delay in seconds
            tool_timeout: Tool execution timeout in seconds
            max_parallel_tools: Maximum parallel tool executions
        """
        super().__init__("agent_orchestration_service")
        self.execution_repo = execution_repo
        self.case_repo = case_repo

        # Lazy injection via DI container (dynamic import to avoid import-linter violations)
        if session_service is None or evidence_service is None:
            import importlib
            ServiceContainer = importlib.import_module('faultmaven.core.container').ServiceContainer
            APIInvestigationSessionService = importlib.import_module('faultmaven.services.investigation_session_service').APIInvestigationSessionService
            APIEvidenceArtifactService = importlib.import_module('faultmaven.services.evidence_artifact_service').APIEvidenceArtifactService

            self.session_service = session_service or ServiceContainer.get(APIInvestigationSessionService)
            self.evidence_service = evidence_service or ServiceContainer.get(APIEvidenceArtifactService)
        else:
            self.session_service = session_service
            self.evidence_service = evidence_service

        self.tool_registry = tool_registry or agent_tool_registry
        self._llm_client = llm_client
        self.max_retries = max_retries
        self.retry_initial_delay = retry_initial_delay
        self.tool_timeout = tool_timeout
        self.max_parallel_tools = max_parallel_tools

    @property
    def llm_client(self) -> LLMClient:
        """Get or create LLM client."""
        if self._llm_client is None:
            self._llm_client = create_llm_client()
        return self._llm_client

    # ============================================================
    # Core Agent Execution
    # ============================================================

    async def execute_agent(
        self,
        session_id: str,
        organization_id: str,
        user_message: str,
        agent_type: AgentType = AgentType.INVESTIGATOR,
        stream: bool = True,
    ) -> AsyncGenerator[ExecutionEvent, None]:
        """Execute AI agent for troubleshooting investigation.

        Workflow:
        1. Validate session is ACTIVE (not PAUSED/COMPLETED)
        2. Check token budget not exceeded
        3. Create execution record (status=INITIALIZING)
        4. Build agent context (case details, session history, evidence, knowledge)
        5. Call LLM with streaming
        6. Handle tool calls (parallel execution)
        7. Stream execution events (thinking, tool_call, response, error)
        8. Update execution record with final response
        9. Update session token usage
        10. Check if budget exceeded -> pause session if necessary

        Args:
            session_id: Investigation session ID
            organization_id: Organization ID for authorization
            user_message: User's question/request
            agent_type: Agent type (investigator, debugger, etc.)
            stream: Whether to stream response events

        Yields:
            ExecutionEvent: Streaming events

        Raises:
            NotFoundError: Session not found
            AuthorizationError: Wrong organization
            ConflictError: Session not ACTIVE or budget exceeded
            ValidationException: Invalid input
            ServiceError: LLM or tool execution failure
        """
        self.log_operation(
            "execute_agent",
            session_id=session_id,
            organization_id=organization_id,
            agent_type=agent_type.value,
            message_length=len(user_message),
        )

        start_time = time.time()
        execution_id = None

        try:
            # Step 1: Validate session
            session = await self._validate_session(session_id, organization_id)

            # Step 2: Check budget
            budget_check = await self.session_service.check_budget_exceeded(
                session_id, organization_id
            )
            if budget_check.get("is_over_budget"):
                raise ConflictError(
                    f"Token budget exceeded for session {session_id}",
                    resource_type="Session",
                    resource_id=session_id,
                    conflict_reason="budget_exceeded",
                )

            # Step 3: Create execution record
            execution = await self._create_execution(
                session=session,
                user_message=user_message,
                agent_type=agent_type,
            )
            execution_id = execution.execution_id

            # Yield started event
            yield ExecutionEvent.started(
                execution_id=execution_id,
                metadata={
                    "agent_type": agent_type.value,
                    "session_id": session_id,
                },
            )

            # Step 4: Build agent context
            context = await self._build_agent_context(
                session=session,
                user_message=user_message,
                agent_type=agent_type,
            )

            # Step 5-7: Execute LLM with streaming and tool handling
            final_response = ""
            total_tokens = {"input_tokens": 0, "output_tokens": 0}
            all_tool_calls: List[AgentToolCall] = []

            # Create tool context for tool execution
            tool_context = ToolContext(
                session_id=session_id,
                case_id=session.case_id,
                organization_id=organization_id,
                user_id=session.user_id,
                evidence_service=self.evidence_service,
                execution_id=execution_id,
            )

            # Execute with retry
            async for event in self._execute_with_streaming(
                context=context,
                tool_context=tool_context,
                execution=execution,
            ):
                if event.event_type == ExecutionEventType.RESPONSE:
                    final_response += event.content

                if event.event_type == ExecutionEventType.COMPLETED:
                    if event.metadata:
                        total_tokens = {
                            "input_tokens": event.metadata.get("input_tokens", 0),
                            "output_tokens": event.metadata.get("output_tokens", 0),
                            "total_tokens": event.metadata.get("total_tokens", 0),
                        }

                yield event

            # Step 8: Update execution with final response
            execution.mark_completed(final_response)
            execution.set_token_usage(
                prompt_tokens=total_tokens.get("input_tokens", 0),
                completion_tokens=total_tokens.get("output_tokens", 0),
            )
            await self.execution_repo.update_execution(execution)

            # Step 9: Update session token usage
            total = total_tokens.get("input_tokens", 0) + total_tokens.get("output_tokens", 0)
            await self.session_service.add_execution_to_session(
                session_id=session_id,
                organization_id=organization_id,
                execution_id=execution_id,
                token_usage=total,
            )

            # Step 10: Check if budget now exceeded and pause if needed
            budget_check = await self.session_service.check_budget_exceeded(
                session_id, organization_id
            )
            if budget_check.get("is_over_budget"):
                try:
                    await self.session_service.pause_session(session_id, organization_id)
                    logger.info(f"Session {session_id} paused due to budget exceeded")
                except Exception as e:
                    logger.warning(f"Failed to pause session after budget exceeded: {e}")

            # Calculate duration
            duration_ms = int((time.time() - start_time) * 1000)

            # Final completed event
            yield ExecutionEvent.completed(
                execution_id=execution_id,
                total_tokens=total,
                duration_ms=duration_ms,
            )

        except (NotFoundError, AuthorizationError, ConflictError, ValidationException):
            if execution_id:
                await self._mark_execution_failed(execution_id, "Validation error")
            raise
        except LLMException as e:
            error_msg = f"LLM error: {str(e)}"
            if execution_id:
                await self._mark_execution_failed(execution_id, error_msg)
            yield ExecutionEvent.error(error_msg, "LLMException", execution_id)
            raise ServiceError(error_msg)
        except Exception as e:
            error_msg = f"Execution error: {str(e)}"
            logger.exception(f"Agent execution failed: {e}")
            if execution_id:
                await self._mark_execution_failed(execution_id, error_msg)
            yield ExecutionEvent.error(error_msg, "ServiceError", execution_id)
            raise ServiceError(error_msg)

    async def _validate_session(
        self,
        session_id: str,
        organization_id: str,
    ) -> InvestigationSession:
        """Validate session for execution.

        Args:
            session_id: Session ID
            organization_id: Organization ID

        Returns:
            Valid InvestigationSession

        Raises:
            NotFoundError: If session not found
            AuthorizationError: If wrong organization
            ConflictError: If session not active
        """
        session = await self.session_service.get_session(session_id, organization_id)
        if not session:
            raise NotFoundError("Session", session_id)

        if session.status != SessionStatus.ACTIVE:
            raise ConflictError(
                f"Session {session_id} is not active (status: {session.status.value})",
                resource_type="Session",
                resource_id=session_id,
                conflict_reason=f"session_{session.status.value}",
            )

        return session

    async def _create_execution(
        self,
        session: InvestigationSession,
        user_message: str,
        agent_type: AgentType,
    ) -> AgentExecution:
        """Create a new execution record.

        Args:
            session: Investigation session
            user_message: User's message
            agent_type: Agent type

        Returns:
            Created AgentExecution
        """
        execution = AgentExecution(
            execution_id=f"exec_{uuid4().hex[:12]}",
            case_id=session.case_id,
            agent_type=agent_type,
            agent_model=self.llm_client.model,
            status=ExecutionStatus.QUEUED,
            prompt=user_message,
        )
        execution.mark_started()

        saved = await self.execution_repo.create_execution(execution)
        return saved

    async def _mark_execution_failed(
        self,
        execution_id: str,
        error_message: str,
    ) -> None:
        """Mark an execution as failed.

        Args:
            execution_id: Execution ID
            error_message: Error message
        """
        try:
            execution = await self.execution_repo.get_execution(execution_id)
            if execution:
                execution.mark_failed(error_message)
                await self.execution_repo.update_execution(execution)
        except Exception as e:
            logger.error(f"Failed to mark execution as failed: {e}")

    # ============================================================
    # Context Building
    # ============================================================

    async def _build_agent_context(
        self,
        session: InvestigationSession,
        user_message: str,
        agent_type: AgentType,
    ) -> AgentContext:
        """Build context for agent execution.

        Context includes:
        1. Case details (title, description, severity, metadata)
        2. Previous executions in this session (conversation history)
        3. Evidence artifacts metadata
        4. Agent instructions based on agent_type
        5. Available tools

        Args:
            session: Investigation session
            user_message: User's message
            agent_type: Agent type

        Returns:
            AgentContext with system_prompt, messages, tools, context_data
        """
        # Get case details
        case = await self.case_repo.get(session.case_id)
        if not case:
            raise NotFoundError("Case", session.case_id)

        # Build system prompt
        system_prompt = AGENT_SYSTEM_PROMPTS.get(agent_type, AGENT_SYSTEM_PROMPTS[AgentType.INVESTIGATOR])

        # Add case context to system prompt
        case_context = f"""

## Current Case
- **Title**: {case.title}
- **Description**: {case.description or 'No description provided'}
- **Status**: {case.status.value if hasattr(case.status, 'value') else case.status}
- **Session Goal**: {session.session_goal or 'No specific goal set'}
"""
        system_prompt += case_context

        # Get conversation history
        messages = await self._get_conversation_history(session.case_id)

        # Add user message
        messages.append(Message.user(user_message))

        # Get available tools
        tools = self.tool_registry.get_all_domain_tools()

        # Build context data
        context_data = {
            "case_id": session.case_id,
            "session_id": session.session_id,
            "organization_id": session.organization_id,
            "case_title": case.title,
            "session_goal": session.session_goal,
        }

        return AgentContext(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            context_data=context_data,
            agent_type=agent_type.value,
            max_tokens=4096,
        )

    async def _get_conversation_history(
        self,
        case_id: str,
        limit: int = 10,
    ) -> List[Message]:
        """Get conversation history from previous executions.

        Args:
            case_id: Case ID
            limit: Maximum number of previous executions to include

        Returns:
            List of Messages representing conversation history
        """
        messages: List[Message] = []

        try:
            executions, _ = await self.execution_repo.list_executions_by_case(case_id)

            # Take most recent completed executions
            completed = [
                e for e in executions
                if e.status == ExecutionStatus.COMPLETED
            ][-limit:]

            for execution in completed:
                if execution.prompt:
                    messages.append(Message.user(execution.prompt))
                if execution.response:
                    messages.append(Message.assistant(execution.response))

        except Exception as e:
            logger.warning(f"Failed to get conversation history: {e}")

        return messages

    # ============================================================
    # LLM Execution with Streaming
    # ============================================================

    async def _execute_with_streaming(
        self,
        context: AgentContext,
        tool_context: ToolContext,
        execution: AgentExecution,
    ) -> AsyncGenerator[ExecutionEvent, None]:
        """Execute LLM with streaming and tool handling.

        This method handles the main execution loop including:
        - Streaming LLM responses
        - Tool call detection and execution
        - Continuation after tool results

        Args:
            context: Agent context
            tool_context: Tool execution context
            execution: Agent execution record

        Yields:
            ExecutionEvent for each streaming chunk
        """
        max_iterations = 10  # Prevent infinite loops
        iteration = 0
        accumulated_response = ""
        total_input_tokens = 0
        total_output_tokens = 0

        while iteration < max_iterations:
            iteration += 1

            # Call LLM with retry
            tool_calls_to_execute: List[ToolCall] = []
            response_text = ""

            try:
                async for llm_event in self._execute_with_retry(
                    lambda: self.llm_client.stream_completion(
                        messages=context.messages,
                        system=context.system_prompt,
                        tools=context.tools if context.tools else None,
                        max_tokens=context.max_tokens,
                    )
                ):
                    if llm_event.event_type == LLMEventType.TEXT_CHUNK:
                        response_text += llm_event.content
                        yield ExecutionEvent.response(
                            content=llm_event.content,
                            execution_id=execution.execution_id,
                        )

                    elif llm_event.event_type == LLMEventType.TOOL_USE:
                        tool_call = llm_event.content
                        tool_calls_to_execute.append(tool_call)
                        yield ExecutionEvent.tool_call(
                            tool_name=tool_call.name,
                            tool_input=tool_call.arguments,
                            tool_call_id=tool_call.id,
                            execution_id=execution.execution_id,
                        )

                    elif llm_event.event_type == LLMEventType.THINKING:
                        yield ExecutionEvent.thinking(
                            content=llm_event.content,
                            execution_id=execution.execution_id,
                        )

                    elif llm_event.event_type == LLMEventType.COMPLETION:
                        if llm_event.metadata:
                            total_input_tokens += llm_event.metadata.get("input_tokens", 0)
                            total_output_tokens += llm_event.metadata.get("output_tokens", 0)

                    elif llm_event.event_type == LLMEventType.ERROR:
                        yield ExecutionEvent.error(
                            error_message=llm_event.content,
                            error_type="LLMError",
                            execution_id=execution.execution_id,
                        )
                        raise LLMException(llm_event.content)

            except LLMException:
                raise
            except Exception as e:
                logger.exception(f"LLM execution error: {e}")
                raise LLMException(f"LLM execution failed: {str(e)}")

            accumulated_response += response_text

            # If no tool calls, we're done
            if not tool_calls_to_execute:
                break

            # Execute tool calls
            context.add_assistant_message(response_text, tool_calls_to_execute)
            tool_results = await self._handle_tool_calls(
                execution=execution,
                tool_calls=tool_calls_to_execute,
                tool_context=tool_context,
            )

            # Add tool results to context
            for result in tool_results:
                context.add_tool_result(
                    content=result.content,
                    tool_call_id=result.tool_call_id,
                    tool_name=result.tool_name,
                )

                yield ExecutionEvent.tool_result(
                    tool_name=result.tool_name,
                    result=result.content,
                    success=result.success,
                    tool_call_id=result.tool_call_id,
                    execution_id=execution.execution_id,
                )

        # Final completion event with token stats
        yield ExecutionEvent.completed(
            execution_id=execution.execution_id,
            total_tokens=total_input_tokens + total_output_tokens,
            duration_ms=0,  # Will be calculated by caller
        )
        # Override the metadata for proper tracking
        yield ExecutionEvent(
            event_type=ExecutionEventType.COMPLETED,
            content="Execution completed",
            metadata={
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "total_tokens": total_input_tokens + total_output_tokens,
            },
            execution_id=execution.execution_id,
        )

    # ============================================================
    # Tool Call Handling
    # ============================================================

    async def _handle_tool_calls(
        self,
        execution: AgentExecution,
        tool_calls: List[ToolCall],
        tool_context: ToolContext,
    ) -> List[DomainToolResult]:
        """Execute tool calls from agent (parallel execution).

        For each tool call:
        1. Create ToolCallRecord (status=PENDING)
        2. Validate tool exists and args valid
        3. Execute tool (with timeout)
        4. Store result in ToolCallRecord
        5. Handle errors gracefully

        Args:
            execution: Parent execution
            tool_calls: List of tool calls to execute
            tool_context: Context for tool execution

        Returns:
            List of ToolResult
        """
        results: List[DomainToolResult] = []

        # Limit parallel execution
        semaphore = asyncio.Semaphore(self.max_parallel_tools)

        async def execute_single_tool(tc: ToolCall) -> DomainToolResult:
            async with semaphore:
                return await self._execute_single_tool(
                    execution=execution,
                    tool_call=tc,
                    tool_context=tool_context,
                )

        # Execute tools in parallel
        tasks = [execute_single_tool(tc) for tc in tool_calls]
        results = await asyncio.gather(*tasks)

        return list(results)

    async def _execute_single_tool(
        self,
        execution: AgentExecution,
        tool_call: ToolCall,
        tool_context: ToolContext,
    ) -> DomainToolResult:
        """Execute a single tool call with tracking.

        Args:
            execution: Parent execution
            tool_call: Tool call to execute
            tool_context: Context for tool execution

        Returns:
            ToolResult with outcome
        """
        # Create tool call record
        tc_record = AgentToolCall(
            tool_call_id=tool_call.id,
            execution_id=execution.execution_id,
            tool_name=tool_call.name,
            tool_input=tool_call.arguments,
        )
        tc_record.mark_started()

        try:
            # Add to execution
            execution.add_tool_call(tc_record)
            await self.execution_repo.save_tool_call(tc_record)

            # Execute with timeout
            try:
                result = await asyncio.wait_for(
                    self.tool_registry.execute_tool(
                        tool_name=tool_call.name,
                        params=tool_call.arguments,
                        context=tool_context,
                    ),
                    timeout=self.tool_timeout,
                )

                if result.success:
                    # Format result for LLM
                    if isinstance(result.data, dict):
                        import json
                        content = json.dumps(result.data, indent=2)
                    else:
                        content = str(result.data)

                    tc_record.mark_success({"data": result.data})
                else:
                    content = f"Tool error: {result.error}"
                    tc_record.mark_failed(result.error or "Unknown error")

            except asyncio.TimeoutError:
                content = f"Tool execution timed out after {self.tool_timeout}s"
                tc_record.mark_failed("Timeout")
                result = None

            # Save updated tool call
            await self.execution_repo.save_tool_call(tc_record)

            return DomainToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                success=tc_record.status == "success",
                content=content,
                error=tc_record.error_message,
            )

        except Exception as e:
            logger.exception(f"Tool execution failed: {e}")
            tc_record.mark_failed(str(e))
            try:
                await self.execution_repo.save_tool_call(tc_record)
            except Exception:
                pass

            return DomainToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                success=False,
                content=f"Tool execution failed: {str(e)}",
                error=str(e),
            )

    # ============================================================
    # Retry Logic
    # ============================================================

    async def _execute_with_retry(
        self,
        func: Callable,
        max_retries: Optional[int] = None,
        initial_delay: Optional[float] = None,
    ) -> AsyncGenerator[LLMEvent, None]:
        """Execute LLM call with exponential backoff retry.

        Retry on:
        - Rate limit errors (429)
        - Temporary server errors (500, 502, 503)
        - Network timeouts

        Do NOT retry on:
        - Invalid request (400)
        - Authentication errors (401)
        - Quota exceeded (permanent)

        Args:
            func: Function that returns an async generator
            max_retries: Maximum retry attempts (uses default if not specified)
            initial_delay: Initial delay in seconds (uses default if not specified)

        Yields:
            LLMEvent from the function

        Raises:
            LLMException: If all retries fail
        """
        max_retries = max_retries or self.max_retries
        initial_delay = initial_delay or self.retry_initial_delay
        retries = 0
        last_error = None

        while retries <= max_retries:
            try:
                # Call the function to get the generator
                generator = func()

                # Yield all events from the generator
                async for event in generator:
                    yield event
                return  # Success

            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                # Check if we should retry
                should_retry = False
                if "429" in error_str or "rate limit" in error_str:
                    should_retry = True
                elif "500" in error_str or "502" in error_str or "503" in error_str:
                    should_retry = True
                elif "timeout" in error_str or "connection" in error_str:
                    should_retry = True
                elif "overloaded" in error_str:
                    should_retry = True

                # Don't retry on permanent errors
                if "400" in error_str or "invalid" in error_str:
                    should_retry = False
                elif "401" in error_str or "unauthorized" in error_str:
                    should_retry = False
                elif "403" in error_str or "forbidden" in error_str:
                    should_retry = False

                if not should_retry or retries >= max_retries:
                    break

                # Calculate backoff delay
                delay = initial_delay * (2 ** retries)
                logger.warning(
                    f"LLM call failed (attempt {retries + 1}/{max_retries + 1}), "
                    f"retrying in {delay}s: {e}"
                )
                await asyncio.sleep(delay)
                retries += 1

        # All retries exhausted
        raise LLMException(f"LLM call failed after {retries} retries: {last_error}")

    # ============================================================
    # Utility Methods
    # ============================================================

    async def get_execution(
        self,
        execution_id: str,
        organization_id: str,
    ) -> Optional[AgentExecution]:
        """Get an execution by ID with authorization check.

        Args:
            execution_id: Execution ID
            organization_id: Organization ID for authorization

        Returns:
            AgentExecution if found and authorized
        """
        execution = await self.execution_repo.get_execution(execution_id)
        if not execution:
            return None

        # Check authorization via case
        case = await self.case_repo.get(execution.case_id)
        if not case or case.organization_id != organization_id:
            return None

        return execution

    async def list_executions(
        self,
        case_id: str,
        organization_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[AgentExecution], int]:
        """List executions for a case with authorization.

        Args:
            case_id: Case ID
            organization_id: Organization ID
            limit: Max results
            offset: Pagination offset

        Returns:
            Tuple of (executions, total_count)
        """
        # Verify authorization
        case = await self.case_repo.get(case_id)
        if not case:
            raise NotFoundError("Case", case_id)
        if case.organization_id != organization_id:
            raise AuthorizationError(
                f"Case {case_id} not accessible by organization {organization_id}"
            )

        executions, total = await self.execution_repo.list_executions_by_case(
            case_id, limit=limit, offset=offset
        )
        return executions, total

    async def cancel_execution(
        self,
        execution_id: str,
        organization_id: str,
    ) -> bool:
        """Cancel a running execution.

        Args:
            execution_id: Execution ID
            organization_id: Organization ID

        Returns:
            True if cancelled, False if not found or not running
        """
        execution = await self.get_execution(execution_id, organization_id)
        if not execution:
            return False

        if execution.status != ExecutionStatus.RUNNING:
            return False

        execution.mark_cancelled()
        await self.execution_repo.update_execution(execution)
        return True
