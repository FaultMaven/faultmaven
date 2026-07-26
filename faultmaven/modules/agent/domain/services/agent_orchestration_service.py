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
import json
import logging
import re
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from faultmaven.config.settings import get_settings
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    LLMException,
    NotFoundError,
    ServiceError,
    ValidationException,
)
from faultmaven.models.investigation_session import InvestigationSession, SessionState
from faultmaven.modules.agent.domain.events.execution_events import (
    AgentContext,
    ExecutionEvent,
    ExecutionEventType,
    LLMEvent,
    LLMEventType,
    Message,
    ToolCall,
)
from faultmaven.modules.agent.domain.events.execution_events import (
    ToolResult as DomainToolResult,
)
from faultmaven.modules.agent.domain.services.llm_client import (
    LLMClient,
    create_llm_client,
)
from faultmaven.modules.agent.domain.services.query_classifier import (
    ProcessingMode,
    classify_query,
)
from faultmaven.modules.agent.tools.base import (
    AgentToolRegistry,
    ToolContext,
    derive_kb_context_metadata,
)
from faultmaven.modules.agent.tools.base import tool_registry as agent_tool_registry
from faultmaven.modules.agent.tools.vectorize_file_tool import (
    VECTORIZATION_MAX_SIZE_BYTES,
)
from faultmaven.modules.case.contracts import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
    ICaseRepository,
)
from faultmaven.modules.preprocessing.extractors.utils import COVERAGE_SEPARATOR

logger = logging.getLogger(__name__)


# --- Per-evidence DA failure tracking (scenario-driven processing) ---
@dataclass
class EvidenceDAState:
    """Per-evidence directed analysis tracking within a single execution.

    Tracks mechanical failure signals for auto-vectorization decisions.
    Ephemeral — lives only within one _execute_with_streaming call.
    """

    evidence_id: str
    empty_search_count: int = 0
    da_call_count: int = 0
    last_da_confidence: float = 1.0
    has_timed_out: bool = False
    content_size_bytes: int = 0
    vectorized: bool = False


# --- R3: Query entity extraction patterns (compiled once) ---
_RE_TIMESTAMP_HM = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_RE_TIMESTAMP_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_RE_SERVICE_NAME = re.compile(r"\b(?:in|from|on)\s+([a-z][\w-]*)\b", re.IGNORECASE)
_RE_HTTP_ERROR = re.compile(r"\b[45]\d{2}\b")
_RE_ERROR_CODE = re.compile(r"\bE\d{4}\b")
_RE_IP_ADDRESS = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

# --- R5: Context budget and compression ---
TOOL_RESULT_BUDGET = 30000
_HIGH_SIGNAL_KEYWORDS = frozenset(
    {
        "error",
        "exception",
        "fail",
        "timeout",
        "refused",
        "denied",
        "critical",
        "fatal",
        "panic",
        "crash",
        "kill",
        "oom",
        "traceback",
        "stacktrace",
        "caused by",
    }
)

# --- Mode-specific data access prompts (scenario-driven processing) ---

DATA_ACCESS_TRIAGE = """## Data Access Strategy

The structural indexes in <evidence_collected> are your primary source.
Summarize the key findings: errors by severity, anomalies, misconfigurations, notable patterns.
If a structural_index is [TRUNCATED], use search_file to retrieve specific sections.
Do NOT call deep_analysis or vectorize_file in triage mode — the structural index is the answer."""

DATA_ACCESS_DIRECTED_ANALYSIS = """## Data Access Strategy

The user has a specific question. The <structural_index role="orientation"> for each
evidence file is a map of the file's contents (time range, services, error distribution).

Use this map to formulate targeted queries:
- **deep_analysis** — Primary tool. Ask a focused question about specific evidence.
- **search_file** — Supplementary. Use for exact keyword, regex, or timestamp matching.

You do NOT need to try search_file before deep_analysis. Use whichever is appropriate
for the question. If your analysis is insufficient, the system will automatically
index large files for semantic search — you do not need to manage this."""

# Agent system prompts by type
AGENT_SYSTEM_PROMPTS = {
    AgentType.INVESTIGATOR: """You are an expert troubleshooting investigator for technical issues.

Your role is to:
1. Analyze the problem using a data-driven, opportunistic approach (gathering evidence, verifying facts, and letting investigation milestones emerge from data)
2. Generate hypotheses about root causes based on evidence (only if root cause is not immediately obvious)
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
- search_file: Search a file for keywords, regex patterns, or timestamps (fast, free)
- deep_analysis: LLM-interpreted analysis of specific data sections (primary tool for answering questions)
- knowledge_base_search: Semantic search over vectorized evidence

{data_access_strategy}

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
    token_usage: dict[str, int]
    tool_calls: list[dict[str, Any]]
    duration_ms: int
    error_message: str | None = None


class AgentOrchestrationService:
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
        case_repo: Case repository (handles agent executions and evidence)
        tool_registry: Registry of available tools
        llm_client: LLM client for API calls
    """

    def __init__(
        self,
        case_repo: ICaseRepository,
        session_service: Any,
        tool_registry: AgentToolRegistry | None = None,
        llm_client: LLMClient | None = None,
        max_retries: int = 3,
        retry_initial_delay: float = 1.0,
        tool_timeout: int = 30,
        max_parallel_tools: int = 5,
        checkpoint_service: Any = None,
        team_service: Any = None,
        share_repository: Any = None,
    ):
        """Initialize agent orchestration service.

        Args:
            case_repo: Case repository (handles agent executions and serves
                as the source of evidence via ``case.evidence``).
            session_service: Investigation session service (required)
            tool_registry: Registry of available tools (uses global if not provided)
            llm_client: LLM client (creates default if not provided)
            max_retries: Maximum retry attempts for LLM calls
            retry_initial_delay: Initial retry delay in seconds
            tool_timeout: Tool execution timeout in seconds
            max_parallel_tools: Maximum parallel tool executions
            team_service: Team service for resolving user team memberships (optional)

        Raises:
            ValueError: If required dependencies are not provided
        """
        self.case_repo = case_repo

        # Cross-turn in-flight proactive vectorization registry.
        # AgentOrchestrationService is a DI singleton, so this dict
        # survives across turns. See MilestoneEngine._inflight_vectorize
        # for rationale — persistent Evidence.vectorized covers completed
        # state, this registry covers in-flight state.
        self._inflight_vectorize: dict[str, asyncio.Task] = {}

        # Require explicit dependency injection
        if session_service is None:
            raise ValueError(
                "session_service is required for AgentOrchestrationService"
            )

        self.session_service = session_service

        self.tool_registry = tool_registry or agent_tool_registry
        self._llm_client = llm_client
        self.max_retries = max_retries
        self.retry_initial_delay = retry_initial_delay
        self.tool_timeout = tool_timeout
        self.max_parallel_tools = max_parallel_tools
        self.checkpoint_service = checkpoint_service
        self.team_service = team_service
        self.share_repository = share_repository

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
        logger.info(
            "execute_agent",
            extra={
                "session_id": session_id,
                "organization_id": organization_id,
                "agent_type": agent_type.value,
                "message_length": len(user_message),
            },
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
            all_tool_calls: list[AgentToolCall] = []

            # Resolve the user's team memberships, then the KB items shared to
            # those teams — the "shared-to-my-teams" arm of the read allowlist
            # (ADR-013 §D4 / ADR-011 D3). Both degrade to empty (global +
            # personal still work): team_service is None in standalone, and the
            # share repo is queried only when there are teams to resolve.
            team_ids: list[str] = []
            if self.team_service:
                try:
                    team_ids = await self.team_service.list_all_user_team_ids(
                        session.user_id
                    )
                except Exception:
                    pass  # Graceful degradation — global + personal still work

            shared_kb_ids: list[str] = []
            if team_ids and self.share_repository:
                try:
                    shared_kb_ids = await self.share_repository.list_resource_ids(
                        resource_type="knowledge_item",
                        scope_type="team",
                        scope_ids=team_ids,
                    )
                except Exception:
                    pass  # Graceful degradation

            # Create tool context for tool execution
            case = await self.case_repo.get(session.case_id)
            tool_context = ToolContext(
                session_id=session_id,
                case_id=session.case_id,
                organization_id=organization_id,
                user_id=session.user_id,
                shared_kb_ids=shared_kb_ids,
                case_repository=self.case_repo,
                execution_id=execution_id,
                in_memory_case=case,
                kb_context_metadata=derive_kb_context_metadata(case),
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
            await self.case_repo.update_agent_execution(execution)

            # Step 8b: Save conversation to case.messages
            # This ensures conversation history is available for future turns
            try:
                case = await self.case_repo.get(session.case_id)
                if case:
                    current_turn = case.current_turn + 1

                    # Save user message. author_id is the authenticated principal
                    # driving the session — on a team-shared case that is not
                    # necessarily the case owner, and a NULL here is permanently
                    # indistinguishable from a pre-migration row (ADR-013 D4).
                    await self.case_repo.add_message(
                        session.case_id,
                        {
                            "role": "user",
                            "content": user_message,
                            "turn_number": current_turn,
                            "author_id": session.user_id,
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                    )

                    # Save agent response
                    await self.case_repo.add_message(
                        session.case_id,
                        {
                            "role": "assistant",
                            "content": final_response,
                            "turn_number": current_turn,
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                    )

                    logger.debug(
                        f"Saved conversation turn {current_turn} to case.messages "
                        f"for case {session.case_id}"
                    )
                else:
                    logger.warning(
                        f"Could not save conversation: case {session.case_id} not found"
                    )

            except Exception as e:
                # Don't fail the execution if message saving fails
                logger.error(
                    f"Failed to save conversation for case {session.case_id}: {e}",
                    exc_info=True,
                )

            # Step 9: Update session token usage
            total = total_tokens.get("input_tokens", 0) + total_tokens.get(
                "output_tokens", 0
            )
            await self.session_service.add_execution_to_session(
                session_id=session_id,
                organization_id=organization_id,
                execution_id=execution_id,
                token_usage=total,
            )

            # Step 9b: Create checkpoint (fail-safe)
            # We capture the state AFTER the turn is complete and messages are saved
            if session and session.case_id and self.checkpoint_service:
                current_case = await self.case_repo.get(session.case_id)
                if current_case:
                    await self.checkpoint_service.create_checkpoint(
                        current_case, trigger="turn_complete"
                    )

            # Step 10: Check if budget now exceeded and pause if needed
            budget_check = await self.session_service.check_budget_exceeded(
                session_id, organization_id
            )
            if budget_check.get("is_over_budget"):
                try:
                    await self.session_service.pause_session(
                        session_id, organization_id
                    )
                    logger.info(f"Session {session_id} paused due to budget exceeded")
                except Exception as e:
                    logger.warning(
                        f"Failed to pause session after budget exceeded: {e}"
                    )

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

        if session.state != SessionState.ACTIVE:
            raise ConflictError(
                f"Session {session_id} is not active (status: {session.state.value})",
                resource_type="Session",
                resource_id=session_id,
                conflict_reason=f"session_{session.state.value}",
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
            organization_id=session.organization_id,
            agent_type=agent_type,
            agent_model=self.llm_client.model,
            status=ExecutionStatus.QUEUED,
            prompt=user_message,
        )
        execution.mark_started()

        saved = await self.case_repo.create_agent_execution(execution)
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
            execution = await self.case_repo.get_agent_execution(execution_id)
            if execution:
                execution.mark_failed(error_message)
                await self.case_repo.update_agent_execution(execution)
        except Exception as e:
            logger.error(f"Failed to mark execution as failed: {e}", exc_info=True)

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

        # Classify query for scenario-driven processing mode
        classification = classify_query(
            user_message, has_attachments=bool(case.evidence)
        )
        if classification.mode == ProcessingMode.DIRECTED_ANALYSIS:
            data_access = DATA_ACCESS_DIRECTED_ANALYSIS
        else:
            data_access = DATA_ACCESS_TRIAGE

        logger.info(
            "query_classified",
            extra={
                "case_id": session.case_id,
                "mode": classification.mode.value,
                "confidence": classification.confidence,
                "entities": classification.detected_entities,
            },
        )

        # Build system prompt with mode-specific data access strategy
        prompt_template = AGENT_SYSTEM_PROMPTS.get(
            agent_type, AGENT_SYSTEM_PROMPTS[AgentType.INVESTIGATOR]
        )
        system_prompt = prompt_template.replace("{data_access_strategy}", data_access)

        # Build state summary following Section 11.5 of prompt guide
        state_summary = self._build_state_summary(case, session)

        # Add case context to system prompt
        case_context = f"""

## Current Investigation

<state_summary>
{state_summary}
</state_summary>

## Session Context
- **Session Goal**: {session.session_goal or 'No specific goal set'}
"""
        # R3: Inject coverage advisory if gaps detected
        advisory = self._build_coverage_advisories(user_message, case)
        if advisory:
            case_context += advisory

        system_prompt += case_context

        logger.debug(
            "Built agent context for case %s: system_prompt length=%d chars",
            session.case_id,
            len(system_prompt),
        )

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
            "processing_mode": classification.mode.value,
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
    ) -> list[Message]:
        """Get conversation history from case messages.

        Following prompt engineering guide Section 11.5, we provide recent
        conversation turns to maintain context while minimizing tokens.

        Args:
            case_id: Case ID
            limit: Maximum number of turns to include (default 10 turns = 20 messages)

        Returns:
            List of Messages representing recent conversation history
        """
        messages: list[Message] = []

        try:
            # Get case with full message history
            case = await self.case_repo.get(case_id)

            if not case:
                logger.warning(
                    f"Case {case_id} not found when building conversation history"
                )
                return messages

            # Use case.messages (the ACTUAL conversation)
            if case.messages:
                # Get recent messages (limit turns × 2 messages per turn)
                max_messages = limit * 2
                recent_messages = case.messages[-max_messages:]

                logger.debug(
                    f"Building conversation history for case {case_id}: "
                    f"using {len(recent_messages)} of {len(case.messages)} total messages"
                )

                for msg in recent_messages:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    turn_number = msg.get("turn_number", "?")

                    if not content:
                        logger.debug(
                            f"Skipping empty message at turn {turn_number} role={role}"
                        )
                        continue

                    if role == "user":
                        messages.append(Message.user(content))
                    elif role == "assistant":
                        messages.append(Message.assistant(content))
                    else:
                        logger.warning(
                            f"Unknown message role '{role}' at turn {turn_number}, "
                            f"treating as user message"
                        )
                        messages.append(Message.user(content))

                logger.debug(
                    f"Built conversation history: {len(messages)} messages for case {case_id}"
                )

            else:
                logger.debug(
                    f"No message history found for case {case_id}, starting fresh conversation"
                )

        except Exception as e:
            logger.error(
                f"Failed to get conversation history for case {case_id}: {e}",
                exc_info=True,
            )

        return messages

    def _build_state_summary(
        self,
        case: Any,
        session: InvestigationSession,
    ) -> str:
        """Build compact state summary following Section 11.5 of prompt guide.

        Provides investigation context in ~100-200 tokens instead of full history.

        Args:
            case: Case object with investigation state
            session: Current investigation session

        Returns:
            Compact state summary string
        """
        summary_parts = []

        # Title and description
        summary_parts.append(f"Title: {case.title}")
        if case.description:
            # Truncate long descriptions
            desc = (
                case.description[:200] + "..."
                if len(case.description) > 200
                else case.description
            )
            summary_parts.append(f"Description: {desc}")

        # Problem verification (core investigation context)
        if hasattr(case, "problem_verification") and case.problem_verification:
            pv = case.problem_verification
            if hasattr(pv, "symptom_statement") and pv.symptom_statement:
                summary_parts.append(f"Investigation: {pv.symptom_statement}")

            if hasattr(pv, "temporal_state") and pv.temporal_state:
                temporal = (
                    pv.temporal_state.value
                    if hasattr(pv.temporal_state, "value")
                    else str(pv.temporal_state)
                )
                summary_parts.append(f"Temporal State: {temporal}")

            if hasattr(pv, "urgency_level") and pv.urgency_level:
                urgency = (
                    pv.urgency_level.value
                    if hasattr(pv.urgency_level, "value")
                    else str(pv.urgency_level)
                )
                summary_parts.append(f"Urgency: {urgency}")

        # Status and progress
        status_str = (
            case.state.value if hasattr(case.state, "value") else str(case.state)
        )
        summary_parts.append(f"Status: {status_str}")

        # Turn count
        if hasattr(case, "current_turn"):
            summary_parts.append(f"Turns: {case.current_turn} total")

        # Evidence count
        if hasattr(case, "evidence") and case.evidence:
            summary_parts.append(f"Evidence: {len(case.evidence)} artifacts")

        # Active hypotheses
        if hasattr(case, "hypotheses") and case.hypotheses:
            active_hypotheses = [
                h
                for h in case.hypotheses.values()
                if hasattr(h, "state") and h.state != "retired"
            ]
            if active_hypotheses:
                summary_parts.append(f"Hypotheses: {len(active_hypotheses)} active")

                # Include top hypothesis if available
                top_hypothesis = max(
                    active_hypotheses,
                    key=lambda h: getattr(h, "likelihood", 0),
                    default=None,
                )
                if top_hypothesis and hasattr(top_hypothesis, "statement"):
                    confidence = int(getattr(top_hypothesis, "likelihood", 0.5) * 100)
                    statement = top_hypothesis.statement[:100]
                    if len(top_hypothesis.statement) > 100:
                        statement += "..."
                    summary_parts.append(
                        f"Leading Hypothesis: {statement} ({confidence}% confidence)"
                    )

        # Solutions
        if hasattr(case, "solutions") and case.solutions:
            summary_parts.append(f"Solutions: {len(case.solutions)} proposed")

        # Join with newlines
        return "\n".join(summary_parts)

    # ============================================================
    # Coverage Advisory (R3)
    # ============================================================

    @staticmethod
    def _extract_query_entities(message: str) -> dict[str, list[str]]:
        """Extract timestamps, services, error codes, IPs from user message.

        Used to detect coverage gaps between what the user asks about
        and what the evidence actually covers.
        """
        entities: dict[str, list[str]] = {
            "timestamps": [],
            "services": [],
            "error_codes": [],
            "ip_addresses": [],
        }

        # Timestamps (HH:MM or HH:MM:SS)
        for m in _RE_TIMESTAMP_HM.finditer(message):
            entities["timestamps"].append(m.group(0))

        # Date stamps (YYYY-MM-DD)
        for m in _RE_TIMESTAMP_DATE.finditer(message):
            entities["timestamps"].append(m.group(0))

        # Service names (words after "in/from/on")
        for m in _RE_SERVICE_NAME.finditer(message):
            entities["services"].append(m.group(1).lower())

        # HTTP error codes + E-codes
        for m in _RE_HTTP_ERROR.finditer(message):
            entities["error_codes"].append(m.group(0))
        for m in _RE_ERROR_CODE.finditer(message):
            entities["error_codes"].append(m.group(0))

        # IP addresses
        for m in _RE_IP_ADDRESS.finditer(message):
            entities["ip_addresses"].append(m.group(0))

        return entities

    @staticmethod
    def _detect_coverage_gaps(entities: dict[str, list[str]], case: Any) -> list[str]:
        """Compare query entities against evidence coverage metadata.

        Checks whether user-referenced timestamps, services, and error codes
        are covered by the data behind the case's evidence rows.

        Post-010: the structural-index JSON (file_meta with time_range etc.)
        lives on ``uploaded_files.structural_index``, not on
        ``Evidence.extract`` which is now an optional verbatim quote. We
        walk ``case.uploaded_files`` directly so coverage gaps are still
        detected after the redesign.
        """
        gaps: list[str] = []

        if not hasattr(case, "evidence") or not case.evidence:
            return gaps

        # Collect coverage metadata from uploaded_files.structural_index
        import json as _json

        all_time_ranges: list[str] = []
        all_services: list[str] = []

        for uf in getattr(case, "uploaded_files", None) or []:
            structural = getattr(uf, "structural_index", None) or ""
            if not structural:
                continue
            try:
                d = _json.loads(structural)
                if isinstance(d, dict) and d.get("v") == 1:
                    meta = d.get("file_meta") or {}
                    tr = meta.get("time_range")
                    if tr:
                        all_time_ranges.append(str(tr))
                    continue
            except (ValueError, TypeError):
                pass
            # Legacy: parse old COVERAGE_SEPARATOR format on plaintext records
            if COVERAGE_SEPARATOR in structural:
                coverage_section = structural.split(COVERAGE_SEPARATOR)[1]
                for line in coverage_section.split("\n"):
                    if line.startswith("First timestamp:") or line.startswith(
                        "Last timestamp:"
                    ):
                        all_time_ranges.append(line)

        coverage_lower = " ".join(all_time_ranges + all_services).lower()

        # Check timestamps — warn if queried times are mentioned but not in ranges
        for ts in entities.get("timestamps", []):
            if ts not in coverage_lower and all_time_ranges:
                gaps.append(
                    f"Queried time '{ts}' not found in evidence coverage "
                    f"(available: {'; '.join(all_time_ranges[:3])})"
                )

        # Service coverage check removed: new file_meta model does not carry
        # a services list; service presence is checked via search_file instead.

        return gaps

    def _build_coverage_advisories(self, user_message: str, case: Any) -> str:
        """Build coverage advisory string for the system prompt.

        Extracts entities from the user message, checks them against
        evidence coverage metadata, and returns advisory text if gaps exist.
        """
        entities = self._extract_query_entities(user_message)

        # Skip if no entities extracted
        if not any(entities.values()):
            return ""

        gaps = self._detect_coverage_gaps(entities, case)
        if not gaps:
            return ""

        advisory = "\n\n## Coverage Advisory\n\n"
        advisory += (
            "**Warning**: The user's query references entities not fully covered "
            "by available evidence:\n"
        )
        for gap in gaps[:5]:  # Cap at 5 advisories
            advisory += f"- {gap}\n"
        advisory += (
            "\nConsider: search_file with different keywords, "
            "request additional evidence from the user, "
            "or clearly state what data is missing.\n"
        )

        return advisory

    # ============================================================
    # Tool Result Compression (R5)
    # ============================================================

    @staticmethod
    def _compress_tool_result(content: str, aggressive: bool = False) -> str:
        """Compress tool result content to stay within context budget.

        Standard mode: keeps first 3 lines + high-signal lines + last 2 lines.
        Aggressive mode: keeps first line + high-signal lines only.

        Args:
            content: Raw tool result content
            aggressive: If True, apply aggressive compression

        Returns:
            Compressed content string
        """
        lines = content.split("\n")
        if len(lines) <= 10:
            return content

        signal_lines: list[str] = []
        for line in lines:
            line_lower = line.lower()
            if any(kw in line_lower for kw in _HIGH_SIGNAL_KEYWORDS):
                signal_lines.append(line)

        if aggressive:
            kept = [lines[0]]
            if signal_lines:
                kept.append("--- [compressed: keeping high-signal lines] ---")
                kept.extend(signal_lines[:20])
            kept.append(f"--- [compressed: {len(lines)} total lines] ---")
        else:
            kept = lines[:3]
            if signal_lines:
                kept.append("--- [compressed: keeping high-signal lines] ---")
                # Deduplicate signal lines that are already in first 3
                for sl in signal_lines[:20]:
                    if sl not in kept:
                        kept.append(sl)
            kept.append("---")
            kept.extend(lines[-2:])
            kept.append(f"--- [{len(lines)} total lines] ---")

        return "\n".join(kept)

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

        # Per-evidence DA failure tracking (scenario-driven processing)
        evidence_da_states: dict[str, EvidenceDAState] = {}
        # R5: Context budget tracking
        tool_result_chars = 0

        # Proactive vectorization: start background tasks for DA-mode large files
        proactive_vectorization_tasks: dict[str, asyncio.Task] = {}
        processing_mode = context.context_data.get("processing_mode", "")
        if processing_mode == "directed_analysis":
            proactive_vectorization_tasks = await self._start_proactive_vectorization(
                context,
                tool_context,
            )

        while iteration < max_iterations:
            iteration += 1

            # Call LLM with retry
            tool_calls_to_execute: list[ToolCall] = []
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
                            total_input_tokens += llm_event.metadata.get(
                                "input_tokens", 0
                            )
                            total_output_tokens += llm_event.metadata.get(
                                "output_tokens", 0
                            )

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

            # Add tool results to context (with DA tracking + R5 compression)
            for result in tool_results:
                content_for_context = result.content

                # Per-evidence DA failure tracking
                evidence_id = self._extract_evidence_id_from_tool_call(
                    result.tool_call_id, tool_calls_to_execute
                )
                if evidence_id and result.tool_name in (
                    "search_file",
                    "deep_analysis",
                ):
                    # Lazy-init per-evidence state. da_call_count is in-turn
                    # only; cross-turn persistence would need a backing DB
                    # column on Evidence.
                    if evidence_id not in evidence_da_states:
                        ev_size = await self._get_evidence_size(
                            evidence_id, tool_context
                        )
                        # Check if proactive vectorization already completed
                        proactive_done = False
                        if evidence_id in proactive_vectorization_tasks:
                            task = proactive_vectorization_tasks[evidence_id]
                            if task.done():
                                proactive_done = (
                                    not task.cancelled()
                                    and task.exception() is None
                                    and task.result()
                                )
                        evidence_da_states[evidence_id] = EvidenceDAState(
                            evidence_id=evidence_id,
                            content_size_bytes=ev_size,
                            da_call_count=0,
                            vectorized=proactive_done,
                        )
                    state = evidence_da_states[evidence_id]

                    # Check proactive vectorization on subsequent iterations
                    if (
                        not state.vectorized
                        and evidence_id in proactive_vectorization_tasks
                    ):
                        task = proactive_vectorization_tasks[evidence_id]
                        if task.done():
                            success = (
                                not task.cancelled()
                                and task.exception() is None
                                and task.result()
                            )
                            if success:
                                state.vectorized = True
                                content_for_context += (
                                    "\n\n[SYSTEM] This file has been "
                                    "automatically indexed for semantic "
                                    "search. Use knowledge_base_search to "
                                    "find content by meaning rather than "
                                    "keywords."
                                )
                                logger.info(
                                    "proactive_vectorization_completed",
                                    extra={
                                        "evidence_id": evidence_id,
                                        "content_size_bytes": state.content_size_bytes,
                                    },
                                )

                    # Track search_file empty results
                    if result.tool_name == "search_file" and result.success:
                        try:
                            data = json.loads(result.content)
                            if (
                                isinstance(data, dict)
                                and data.get("results_count", 0) == 0
                            ):
                                state.empty_search_count += 1
                            else:
                                state.empty_search_count = 0
                        except (json.JSONDecodeError, TypeError):
                            pass

                        # Advisory after 3 consecutive empty searches on same file
                        if state.empty_search_count >= 3:
                            content_for_context += (
                                "\n\n[SYSTEM] "
                                f"Last {state.empty_search_count} search_file calls "
                                "on this file returned zero results. "
                                "Consider using deep_analysis with a different "
                                "query approach."
                            )

                    # Track deep_analysis confidence. Counter is in-turn
                    # only by design — see _should_auto_vectorize.
                    if result.tool_name == "deep_analysis" and result.success:
                        try:
                            data = json.loads(result.content)
                            if isinstance(data, dict):
                                state.da_call_count += 1
                                state.last_da_confidence = float(
                                    data.get("confidence", 1.0)
                                )
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass

                    # Track timeouts
                    if (
                        not result.success
                        and "timed out" in (result.content or "").lower()
                    ):
                        state.has_timed_out = True

                    # Check auto-vectorization trigger. Reactive path:
                    # wrap with the configurable reactive bound. Proactive
                    # kicks are elsewhere (_start_proactive_vectorization)
                    # and intentionally unbounded.
                    if not state.vectorized and self._should_auto_vectorize(state):
                        reactive_timeout = float(
                            get_settings().agent.vectorization_reactive_timeout_seconds
                        )
                        try:
                            vectorized = await asyncio.wait_for(
                                self._auto_vectorize(evidence_id, tool_context),
                                timeout=reactive_timeout,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Reactive auto-vectorization timed out for "
                                "%s after %ss; agent proceeds without "
                                "semantic search results for this turn",
                                evidence_id,
                                reactive_timeout,
                            )
                            vectorized = False
                        if vectorized:
                            state.vectorized = True
                            content_for_context += (
                                "\n\n[SYSTEM] This file has been automatically "
                                "indexed for semantic search. Use "
                                "knowledge_base_search to find content by "
                                "meaning rather than keywords."
                            )
                            logger.info(
                                "auto_vectorization_triggered",
                                extra={
                                    "evidence_id": evidence_id,
                                    "trigger": self._vectorization_trigger_reason(
                                        state
                                    ),
                                    "content_size_bytes": state.content_size_bytes,
                                },
                            )
                    # Small-file DA failure: inject raw content instead of vectorizing
                    elif (
                        not state.vectorized
                        and self._da_has_failed(state)
                        and 0
                        < state.content_size_bytes
                        < get_settings().agent.vectorization_min_size_bytes
                    ):
                        try:
                            raw_content = await self._get_raw_file_content(
                                evidence_id, tool_context
                            )
                            if raw_content:
                                content_for_context += (
                                    f"\n\n[SYSTEM] Full file content "
                                    f"(small file, "
                                    f"{state.content_size_bytes} bytes):\n"
                                    f"{raw_content[:50000]}"
                                )
                        except Exception:
                            pass

                # R5: Track budget and compress if needed
                tool_result_chars += len(content_for_context)
                if tool_result_chars > TOOL_RESULT_BUDGET:
                    content_for_context = self._compress_tool_result(
                        content_for_context, aggressive=True
                    )
                elif tool_result_chars > int(TOOL_RESULT_BUDGET * 0.8):
                    content_for_context = self._compress_tool_result(
                        content_for_context, aggressive=False
                    )

                context.add_tool_result(
                    content=content_for_context,
                    tool_call_id=result.tool_call_id,
                    tool_name=result.tool_name,
                )

                yield ExecutionEvent.tool_result(
                    tool_name=result.tool_name,
                    result=result.content,  # Original content for event
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
        tool_calls: list[ToolCall],
        tool_context: ToolContext,
    ) -> list[DomainToolResult]:
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
        results: list[DomainToolResult] = []

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
            organization_id=execution.organization_id,
            tool_name=tool_call.name,
            tool_input=tool_call.arguments,
        )
        tc_record.mark_started()

        try:
            # Add to execution
            execution.add_tool_call(tc_record)
            # Create tool call record (will be updated after execution)
            await self.case_repo.create_agent_tool_call(tc_record)

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

            except TimeoutError:
                content = f"Tool execution timed out after {self.tool_timeout}s"
                tc_record.mark_failed("Timeout")
                result = None

            # Save updated tool call
            await self.case_repo.update_agent_tool_call(tc_record)

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
                # Update tool call with failure status
                await self.case_repo.update_agent_tool_call(tc_record)
            except Exception:
                # If update fails, try to create it
                try:
                    await self.case_repo.create_agent_tool_call(tc_record)
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
    # DA Failure Tracking & Auto-Vectorization Helpers
    # ============================================================

    @staticmethod
    def _extract_evidence_id_from_tool_call(
        tool_call_id: str, tool_calls: list[ToolCall]
    ) -> str | None:
        """Extract evidence_id from the original tool call arguments."""
        for tc in tool_calls:
            if tc.id == tool_call_id:
                args = tc.arguments if isinstance(tc.arguments, dict) else {}
                return args.get("evidence_id")
        return None

    async def _get_evidence_size(
        self, evidence_id: str, tool_context: ToolContext
    ) -> int:
        """Get the file size in bytes for an evidence record.

        Reads ``size_bytes`` from the ``UploadedFile`` linked via
        ``evidence.source_file_id``.
        """
        try:
            case = tool_context.in_memory_case
            if case is None and tool_context.case_repository is not None:
                case = await tool_context.case_repository.get(tool_context.case_id)
            if case is None:
                return 0
            for ev in getattr(case, "evidence", []) or []:
                if getattr(ev, "evidence_id", None) == evidence_id:
                    file_meta = case.find_uploaded_file(ev.source_file_id)
                    return int(file_meta.size_bytes if file_meta else 0)
        except Exception:
            return 0
        return 0

    async def _get_raw_file_content(
        self, evidence_id: str, tool_context: ToolContext
    ) -> str | None:
        """Retrieve raw file content for a small evidence file.

        Uses `case.evidence` + UploadedFile.storage_ref via FileStorageService
        (storage redesign 2026-04 phase 2 deleted the standalone evidence
        service). Returns None if the case/evidence/file isn't accessible.
        """
        try:
            case = tool_context.in_memory_case
            if case is None and tool_context.case_repository is not None:
                case = await tool_context.case_repository.get(tool_context.case_id)
            if case is None:
                return None

            target = None
            for ev in getattr(case, "evidence", []) or []:
                if getattr(ev, "evidence_id", None) == evidence_id:
                    target = ev
                    break
            if target is None:
                return None

            file_meta = case.find_uploaded_file(target.source_file_id)
            storage_ref = file_meta.storage_ref if file_meta else None
            if not storage_ref:
                return None

            from faultmaven.modules.evidence.domain.services.file_storage_service import (
                FileStorageService,
            )

            storage = FileStorageService()
            file_data = await storage.retrieve_file(storage_ref)
            return file_data.decode("utf-8", errors="replace")
        except Exception:
            return None

    def _should_auto_vectorize(self, state: EvidenceDAState) -> bool:
        """Mechanical decision: should this evidence be auto-vectorized?

        Returns True only if the file exceeds the size threshold AND at
        least one DA failure signal has fired. No LLM judgment involved.

        Note (v5.2): da_call_count >= 3 removed — 3 DA calls is thorough
        investigation, not failure. Replaced by proactive vectorization.
        """
        settings = get_settings()
        if state.content_size_bytes < settings.agent.vectorization_min_size_bytes:
            logger.debug(
                "Auto-vectorize blocked by size gate: %d < %d for %s",
                state.content_size_bytes,
                settings.agent.vectorization_min_size_bytes,
                state.evidence_id,
            )
            return False
        return (
            state.has_timed_out
            or state.empty_search_count >= 3
            or (state.da_call_count >= 1 and state.last_da_confidence < 0.2)
        )

    @staticmethod
    def _da_has_failed(state: EvidenceDAState) -> bool:
        """Check if DA failure signals have fired (ignoring size threshold)."""
        return (
            state.has_timed_out
            or state.empty_search_count >= 3
            or (state.da_call_count >= 1 and state.last_da_confidence < 0.2)
        )

    async def _start_proactive_vectorization(
        self,
        context: AgentContext,
        tool_context: ToolContext,
    ) -> dict[str, asyncio.Task]:
        """Start background vectorization for qualifying DA-mode evidence.

        Returns a dict of evidence_id → asyncio.Task for each file that
        qualifies (above size threshold, not already vectorized). Tasks
        run concurrently with the DA tool loop.
        """
        settings = get_settings()
        case_id = context.context_data.get("case_id", "")
        if not case_id:
            return {}

        try:
            case = await self.case_repo.get(case_id)
        except Exception:
            return {}
        if not case:
            return {}

        tasks: dict[str, asyncio.Task] = {}
        for ev in case.evidence:
            file_meta = case.find_uploaded_file(ev.source_file_id)
            size = file_meta.size_bytes if file_meta else 0
            if not (
                size >= settings.agent.vectorization_min_size_bytes
                and size <= VECTORIZATION_MAX_SIZE_BYTES
                and not ev.vectorized
            ):
                continue

            existing = self._inflight_vectorize.get(ev.evidence_id)
            if existing is not None and not existing.done():
                tasks[ev.evidence_id] = existing
                logger.debug(
                    "proactive_vectorization_reused_inflight",
                    extra={"evidence_id": ev.evidence_id, "case_id": case_id},
                )
                continue

            task = asyncio.create_task(
                self._auto_vectorize(ev.evidence_id, tool_context)
            )
            self._inflight_vectorize[ev.evidence_id] = task
            task.add_done_callback(
                lambda t, eid=ev.evidence_id: self._inflight_vectorize.pop(eid, None)
            )
            tasks[ev.evidence_id] = task
            logger.info(
                "proactive_vectorization_started",
                extra={
                    "evidence_id": ev.evidence_id,
                    "content_size_bytes": size,
                    "case_id": case_id,
                },
            )
        return tasks

    async def _auto_vectorize(
        self, evidence_id: str, tool_context: ToolContext
    ) -> bool:
        """Auto-vectorize an evidence file. No user confirmation needed.

        No internal ``asyncio.wait_for``: time-bound policy belongs at
        the caller. Proactive callers (_start_proactive_vectorization)
        run this unbounded as a background task — time-bounding a
        fire-and-forget task only guarantees wasted CPU when encode
        outlasts the bound. Reactive callers (tool-loop fallback path)
        wrap this with ``asyncio.wait_for`` using
        ``AgentSettings.vectorization_reactive_timeout_seconds``
        because they block the agent. Mirrors the split in
        MilestoneEngine._vectorize_evidence.
        """
        try:
            result = await self.tool_registry.execute_tool(
                tool_name="vectorize_file",
                params={"evidence_id": evidence_id},
                context=tool_context,
            )
            return result.success
        except Exception as e:
            logger.warning("Auto-vectorization failed for %s: %s", evidence_id, e)
            return False

    @staticmethod
    def _vectorization_trigger_reason(state: EvidenceDAState) -> str:
        """Return the trigger reason for logging."""
        if state.has_timed_out:
            return "tool_timeout"
        if state.empty_search_count >= 3:
            return "repeated_empty_searches"
        if state.last_da_confidence < 0.2:
            return "low_confidence"
        return "unknown"

    # ============================================================
    # Retry Logic
    # ============================================================

    async def _execute_with_retry(
        self,
        func: Callable,
        max_retries: int | None = None,
        initial_delay: float | None = None,
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
                delay = initial_delay * (2**retries)
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
    ) -> AgentExecution | None:
        """Get an execution by ID with authorization check.

        Args:
            execution_id: Execution ID
            organization_id: Organization ID for authorization

        Returns:
            AgentExecution if found and authorized
        """
        execution = await self.case_repo.get_agent_execution(execution_id)
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
    ) -> tuple[list[AgentExecution], int]:
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

        executions, total = await self.case_repo.list_agent_executions_by_case(
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
        await self.case_repo.update_agent_execution(execution)
        return True
