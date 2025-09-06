"""Agent Service Refactored Module

Purpose: Interface-based agent service with dependency injection

This refactored service demonstrates the application of dependency injection
principles using interface-based dependencies rather than concrete implementations.
It maintains the same business logic as the original AgentService while being
fully testable and following clean architecture principles.

Core Responsibilities:
- Agent lifecycle management through interfaces
- Query processing orchestration with interface dependencies
- Investigation state management with interface-based storage
- Result aggregation and formatting with interface-based sanitization

Key Differences from Original:
- Uses ILLMProvider instead of concrete LLMRouter
- Uses List[BaseTool] instead of concrete tool instances  
- Uses ITracer instead of @trace decorator
- Uses ISanitizer instead of concrete DataSanitizer
- All dependencies injected via constructor
- Fully testable with mocked interfaces
"""

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from faultmaven.services.base import BaseService
from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer
from faultmaven.models import QueryRequest, TroubleshootingResponse, AgentResponse, ViewState, UploadedData, Source, SourceType, ResponseType, PlanStep, TitleGenerateRequest, TitleResponse
from faultmaven.models.case import Case, CasePriority, CaseStatus, CaseContext, MessageType
from faultmaven.exceptions import ValidationException
# Agentic Framework Components (production ready - no fallbacks needed)
from faultmaven.services.agentic import (
    BusinessLogicWorkflowEngine,
    QueryClassificationEngine,
    ToolSkillBroker,
    GuardrailsPolicyLayer,
    ResponseSynthesizer,
    ErrorFallbackManager,
    AgentStateManager
)


class AgentService(BaseService):
    """Agent Service using interface dependencies via dependency injection"""

    def __init__(
        self,
        llm_provider: ILLMProvider,
        tools: List[BaseTool],
        tracer: ITracer,
        sanitizer: ISanitizer,
# Agentic Framework Components (required - no fallbacks)
        business_logic_workflow_engine: BusinessLogicWorkflowEngine,
        query_classification_engine: QueryClassificationEngine,
        tool_skill_broker: ToolSkillBroker,
        guardrails_policy_layer: GuardrailsPolicyLayer,
        response_synthesizer: ResponseSynthesizer,
        error_fallback_manager: ErrorFallbackManager,
        agent_state_manager: AgentStateManager,
        session_service: Optional[Any] = None,
        settings: Optional[Any] = None
    ):
        """Initialize with interface dependencies and Agentic Framework components
        
        Args:
            llm_provider: Interface for LLM operations
            tools: List of tool interfaces for agent execution
            tracer: Interface for distributed tracing
            sanitizer: Interface for data sanitization
            session_service: Optional session service for session validation
            settings: Configuration settings for the service
            business_logic_workflow_engine: Plan-execute-observe-adapt workflow orchestration
            query_classification_engine: Intelligent query processing and routing
            tool_skill_broker: Dynamic orchestration of tools and skills
            guardrails_policy_layer: Safety, security, and compliance enforcement
            response_synthesizer: Intelligent response generation and formatting
            error_fallback_manager: Robust error recovery and graceful degradation
            agent_state_manager: Persistent memory and execution state management
        """
        super().__init__()
        self._llm = llm_provider
        self._tools = tools
        self._tracer = tracer
        self._sanitizer = sanitizer
        self._session_service = session_service
        self._settings = settings
        
        # Agentic Framework Components (production ready)
        self._workflow_engine = business_logic_workflow_engine
        self._classification_engine = query_classification_engine
        self._tool_broker = tool_skill_broker
        self._guardrails_layer = guardrails_policy_layer
        self._response_synthesizer = response_synthesizer
        self._error_manager = error_fallback_manager
        self._state_manager = agent_state_manager
        
        # Pure Agentic Framework - production ready, no legacy components
        self.logger.info("AgentService initialized with 7-Component Agentic Framework")

    # --- Output finalization helpers ---
    def _remove_redaction_tags(self, text: str) -> str:
        try:
            import re
            # Replace placeholders like <PERSON>, <LOCATION>, <US_DRIVER_LICENSE>
            return re.sub(r"<[A-Z_]+>", "[redacted]", text or "")
        except (AttributeError, TypeError) as e:
            # Handle cases where text is not string-like or regex fails
            logger.warning(f"Redaction tag removal failed: {e}")
            return text or ""

    def _finalize_text(self, text: str) -> str:
        # Apply sanitizer then strip redaction artifacts for user display
        try:
            sanitized = self._sanitizer.sanitize(text)
        except Exception as e:
            from faultmaven.models.exceptions import ProtectionSystemError
            logger.error(f"Text sanitization failed: {e}")
            # For text finalization, we can fall back to unsanitized text
            # but this should be logged as a protection system issue
            sanitized = text or ""
        return self._remove_redaction_tags(sanitized)

    async def generate_title(self, request: TitleGenerateRequest) -> TitleResponse:
        """Dedicated title generation with validation and guardrails."""
        # Validate
        if not request.session_id or not request.session_id.strip():
            raise ValidationException("Session ID cannot be empty")
        max_words = max(3, min(int(request.max_words or 8), 12))

        # Build brief context string
        context_text = ""
        ctx = request.context or {}
        try:
            if isinstance(ctx, dict) and ctx:
                # Prefer last user message or summary fields if present
                for key in ("last_user_message", "summary", "messages", "notes"):
                    if key in ctx and ctx[key]:
                        context_text = str(ctx[key])
                        break
                if not context_text:
                    context_text = str(ctx)[:800]
        except Exception:
            context_text = ""

        # Create a case id if needed for view_state
        try:
            case_id = await self._session_service.get_or_create_current_case_id(request.session_id) if self._session_service else str(uuid.uuid4())
        except Exception:
            case_id = str(uuid.uuid4())

        prompt = (
            "You are a helpful assistant generating a concise conversation title. "
            "Return ONLY the title text, no quotes, no punctuation at the end. "
            f"Aim for {max_words} words or fewer, clear and specific.\n\n"
            f"Context (may be partial):\n{context_text}\n\nTitle:"
        )
        try:
            title_text = await self._llm.generate(prompt, temperature=0.3, max_tokens=24)
        except Exception as e:
            # Safe fallback
            title_text = "Troubleshooting Session"

        # Post-process: sanitize and enforce word cap
        title_text = (title_text or "Troubleshooting Session").strip().strip('"\' .,:;')
        words = [w for w in title_text.split() if w]
        if len(words) > max_words:
            title_text = " ".join(words[:max_words])

        # Persist title to case if case service is available
        try:
            from faultmaven.container import container as di
            case_service = di.get_case_service() if hasattr(di, 'get_case_service') else None
        except Exception:
            case_service = None

        try:
            # Ensure a persistent case exists via case service if available
            if case_service:
                # Prefer existing current case; if none, create/link one
                current_case_id = None
                try:
                    if self._session_service and hasattr(self._session_service, 'get_current_case_id'):
                        current_case_id = await self._session_service.get_current_case_id(request.session_id)
                except Exception:
                    current_case_id = None

                if not current_case_id and hasattr(case_service, 'get_or_create_case_for_session'):
                    try:
                        current_case_id = await case_service.get_or_create_case_for_session(
                            session_id=request.session_id,
                            user_id=None,
                            force_new=False
                        )
                    except Exception:
                        current_case_id = None

                # If we have a case id now, update its title
                if current_case_id and hasattr(case_service, 'update_case'):
                    try:
                        await case_service.update_case(current_case_id, {"title": title_text})
                        case_id = current_case_id  # Prefer persistent case id for view_state
                    except Exception:
                        pass

                # Optionally record a system event for title set
                try:
                    from faultmaven.models.case import MessageType
                    if self._session_service and current_case_id:
                        await self._session_service.record_case_message(
                            session_id=request.session_id,
                            message_content=f"Case title set to: {title_text}",
                            message_type=MessageType.SYSTEM_EVENT,
                            author_id=None,
                            metadata={"event": "case_title_set"}
                        )
                except Exception:
                    pass
        except Exception:
            # Non-fatal; title still returned to client
            pass

        try:
            view_state = await self._create_view_state(case_id, request.session_id)
        except Exception as view_error:
            self.logger.error(f"ViewState creation failed in title generation: {view_error}")
            raise
        
        try:
            sanitized_title = self._sanitizer.sanitize(title_text)
            return TitleResponse(title=sanitized_title, view_state=view_state)
        except Exception as response_error:
            self.logger.error(f"TitleResponse creation failed: {response_error}")
            raise

    async def process_query(
        self,
        request: QueryRequest
    ) -> AgentResponse:
        """
        Main business logic for query processing using interface dependencies
        
        Args:
            request: QueryRequest with query, session_id, context, etc.
            
        Returns:
            AgentResponse with case analysis results using v3.1.0 schema
            
        Raises:
            ValueError: If request validation fails
            RuntimeError: If agent processing fails
        """
        return await self.execute_operation(
            "process_query",
            self._execute_query_processing,
            request,
            validate_inputs=self._validate_request
        )

    async def _execute_query_processing(self, request: QueryRequest) -> AgentResponse:
        """Execute the core query processing logic"""
        # Use ITracer interface for operation tracing
        with self._tracer.trace("process_query_workflow"):
            # 0. Compatibility shim: handle title generation flag in context
            try:
                if request.context and request.context.get("is_title_generation") is True:
                    # Delegate to dedicated title path and wrap as answer
                    tg = await self.generate_title(TitleGenerateRequest(session_id=request.session_id, context=request.context))
                    response = AgentResponse(
                        content=tg.title,
                        response_type=ResponseType.ANSWER,
                        session_id=request.session_id,
                        view_state=tg.view_state,
                        sources=[],
                        plan=None,
                    )
                    
                    # Record assistant response to case
                    if self._session_service:
                        try:
                            await self._session_service.record_case_message(
                                session_id=request.session_id,
                                message_content=response.content,
                                message_type=MessageType.AGENT_RESPONSE,
                                author_id=None,
                                metadata={"intent": "title_generation"}
                            )
                        except Exception as e:
                            self.logger.warning(f"Failed to record assistant title generation response: {e}")
                    
                    return response
            except Exception:
                # Fall through to normal flow on any error
                pass
            # 1. Sanitize input
            with self._tracer.trace("sanitize_input"):
                sanitized_query = self._sanitizer.sanitize(request.query)
            
            # 2. Get or create case_id for this conversation thread (no hard dependency on preexisting session)
            if self._session_service:
                try:
                    case_id = await self._session_service.get_or_create_current_case_id(request.session_id)
                except FileNotFoundError:
                    # Session doesn't exist; proceed with an ephemeral case_id
                    case_id = str(uuid.uuid4())
                except Exception:
                    case_id = str(uuid.uuid4())
            else:
                # Fallback if no session service available
                case_id = str(uuid.uuid4())
            
            # Log business event for case analysis start
            self.log_business_event(
                "case_analysis_started",
                "info",
                {
                    "case_id": case_id,
                    "session_id": request.session_id,
                    "query_length": len(sanitized_query)
                }
            )
            
            # 3. Skip legacy monolithic agent initialization (removed in modular monolith)
            # Kept a span for compatibility/metrics without side effects
            with self._tracer.trace("initialize_agent"):
                pass
            
            # 4. Record user query and retrieve conversation history for context
            conversation_context = ""
            
            # Fourth Replacement: AgentStateManager Integration
            # Enhanced memory management with persistent state and intelligent context
            enhanced_context = None
            try:
                from faultmaven.container import container as di
                agent_state_manager = di.get_agent_state_manager()
                
                if agent_state_manager:
                    # Create comprehensive state context for enhanced memory management
                    state_context = {
                        "session_id": request.session_id,
                        "case_id": case_id,
                        "query": sanitized_query,
                        "timestamp": datetime.utcnow().isoformat(),
                        "request_metadata": {
                            "query_length": len(sanitized_query),
                            "has_context": bool(request.context),
                            "priority": getattr(request, 'priority', 'normal')
                        }
                    }
                    
                    # Use AgentStateManager for enhanced context and state management
                    state_result = await agent_state_manager.get_enhanced_context(
                        session_id=request.session_id,
                        case_id=case_id,
                        current_query=sanitized_query,
                        context=state_context
                    )
                    
                    if state_result and state_result.get("success"):
                        enhanced_context = state_result.get("context", "")
                        if enhanced_context:
                            # Enhanced context includes conversation history, patterns, and state
                            self.logger.info(f"AgentStateManager provided enhanced context: {len(enhanced_context)} chars with memory patterns")
                            
                        # Update agent state with current interaction
                        await agent_state_manager.update_agent_state(
                            session_id=request.session_id,
                            case_id=case_id,
                            interaction_data={
                                "query": sanitized_query,
                                "timestamp": datetime.utcnow().isoformat(),
                                "context_provided": bool(enhanced_context)
                            }
                        )
                    else:
                        self.logger.debug("AgentStateManager returned no enhanced context")
                        
            except Exception as e:
                self.logger.debug(f"AgentStateManager enhancement failed, falling back to session service: {e}")
            
            if self._session_service:
                # First, record the user's query to the case
                with self._tracer.trace("record_user_query"):
                    try:
                        await self._session_service.record_case_message(
                            session_id=request.session_id,
                            message_content=sanitized_query,
                            message_type=MessageType.USER_QUERY,
                            author_id=None,
                            metadata={"source": "api", "type": "user_query"}
                        )
                    except Exception as e:
                        self.logger.warning(f"Failed to record user message: {e}")
                
                # Also update case message_count and updated_at for consistency
                with self._tracer.trace("update_case_query_count"):
                    try:
                        from faultmaven.container import container as di
                        case_service = di.get_case_service() if hasattr(di, 'get_case_service') else None
                        if case_service and hasattr(case_service, 'add_case_query'):
                            await case_service.add_case_query(case_id, sanitized_query)
                    except Exception as e:
                        self.logger.warning(f"Failed to update case query count: {e}")
                
                # Then retrieve conversation context (will now include the user query)
                with self._tracer.trace("retrieve_conversation_history"):
                    try:
                        conversation_context = await self._session_service.format_conversation_context(
                            request.session_id, case_id, limit=5
                        )
                        if conversation_context:
                            self.logger.debug(f"Retrieved conversation context for case {case_id}: {len(conversation_context)} chars")
                        else:
                            self.logger.debug(f"No conversation context available for case {case_id}")
                    except Exception as e:
                        self.logger.warning(f"Failed to retrieve conversation context: {e}")
            
            # 5. Enhanced query with conversation context (prioritize AgentStateManager)
            enhanced_query = sanitized_query
            final_context = enhanced_context if enhanced_context else conversation_context
            
            if final_context:
                enhanced_query = f"{final_context}\n{sanitized_query}"
                context_source = "AgentStateManager" if enhanced_context else "session_service"
                self.logger.debug(f"Injected {context_source} context. Original query: {len(sanitized_query)} chars, Enhanced: {len(enhanced_query)} chars")
            else:
                self.logger.debug("No enhanced context available from either AgentStateManager or session service")
            
            # 6. Pure Agentic Framework Processing - Start execution here
            start_time = datetime.utcnow()
            
            # Skip all legacy processing - go directly to Agentic Framework
            try:
                with self._tracer.trace("agentic_framework_execution"):
                    # Build context for Agentic Framework
                    agentic_context = {
                        "query": enhanced_query,
                        "original_query": sanitized_query,
                        "session_id": request.session_id,
                        "case_id": case_id,
                        "conversation_context": final_context,
                        "timestamp": start_time.isoformat()
                    }
                    
                    # Execute pure Agentic Framework workflow
                    features = {}
                    evidence = []
                    
                    # Execute Classification Engine
                    classification_result = await self._classification_engine.classify_query(
                        query=sanitized_query,
                        context=agentic_context
                    )
                    
                    # Extract features from classification
                    features = {
                        "intent": classification_result.get("intent", "troubleshooting"),
                        "complexity": classification_result.get("complexity", "medium"),
                        "urgency": classification_result.get("urgency", "normal"),
                        "domain": classification_result.get("domain", "general")
                    }
                    
                    # Execute Tool Skill Broker
                    orchestration_context = {
                        "query": sanitized_query,
                        "session_id": request.session_id,
                        "case_id": case_id,
                        "classification": classification_result,
                        "conversation_context": final_context,
                        "available_tools": [tool.get_schema() for tool in self._tools] if self._tools else []
                    }
                    
                    orchestration_result = await self._tool_broker.orchestrate_capabilities(
                        context=orchestration_context,
                        intent=features.get("intent", "troubleshooting"),
                        complexity=features.get("complexity", "medium")
                    )
                    
                    # Collect evidence from orchestration
                    if orchestration_result and orchestration_result.get("evidence"):
                        evidence.extend(orchestration_result["evidence"])
                    
                    # Execute Business Logic Workflow Engine
                    workflow_context = {
                        "query": sanitized_query,
                        "enhanced_query": enhanced_query,
                        "session_id": request.session_id,
                        "case_id": case_id,
                        "classification": classification_result,
                        "tool_orchestration": orchestration_result,
                        "conversation_context": final_context,
                        "available_tools": [tool.get_schema() for tool in self._tools] if self._tools else [],
                        "features": features,
                        "evidence": evidence,
                        "timestamp": start_time.isoformat()
                    }
                    
                    workflow_result = await self._workflow_engine.execute_agentic_workflow(
                        context=workflow_context,
                        goal="troubleshoot_issue",
                        max_iterations=3,
                        planning_strategy="adaptive",
                        execution_mode="autonomous"
                    )
                    
                    if workflow_result and workflow_result.get("evidence"):
                        evidence.extend(workflow_result["evidence"])
                        
                    # Update features with workflow confidence
                    features.update({
                        "workflow_confidence": workflow_result.get("confidence_boost", 0.7),
                        "agentic_planning": workflow_result.get("plan_executed", False),
                        "autonomous_observations": len(workflow_result.get("observations", [])),
                        "plan_adaptations": len(workflow_result.get("adaptations", []))
                    })
                    
                    # Use Response Synthesizer to generate final response
                    synthesis_context = {
                        "query": sanitized_query,
                        "evidence": evidence,
                        "features": features,
                        "classification": classification_result,
                        "workflow_result": workflow_result,
                        "session_id": request.session_id,
                        "case_id": case_id
                    }
                    
                    synthesized_response = await self._response_synthesizer.synthesize_response(
                        context=synthesis_context,
                        evidence=evidence,
                        confidence=features.get("workflow_confidence", 0.7)
                    )
                    
                    # Create view state
                    view_state = await self._create_view_state(case_id, request.session_id)
                    
                    # Use synthesized content or fallback
                    content = synthesized_response.get("content", "I'll help you troubleshoot this issue. Can you provide more details about what you're experiencing?")
                    sources = synthesized_response.get("sources", [])
                    
                    # Create final response
                    response = AgentResponse(
                        content=self._sanitizer.sanitize(content),
                        response_type=ResponseType.ANSWER,
                        session_id=request.session_id,
                        view_state=view_state,
                        sources=sources,
                        plan=workflow_result.get("execution_plan") if workflow_result else None
                    )
                    
                    # Record assistant response to case
                    if self._session_service:
                        try:
                            await self._session_service.record_case_message(
                                session_id=request.session_id,
                                message_content=response.content,
                                message_type=MessageType.AGENT_RESPONSE,
                                author_id=None,
                                metadata={
                                    "intent": features.get("intent", "troubleshooting"),
                                    "agentic_framework": True,
                                    "confidence": features.get("workflow_confidence", 0.7)
                                }
                            )
                        except Exception as e:
                            self.logger.warning(f"Failed to record assistant response: {e}")
                    
                    # Log processing metrics
                    end_time = datetime.utcnow()
                    processing_time = (end_time - start_time).total_seconds()
                    
                    self.log_metric(
                        "agentic_processing_time",
                        processing_time,
                        "seconds",
                        {
                            "case_id": case_id,
                            "intent": features.get("intent"),
                            "evidence_count": len(evidence),
                            "confidence": features.get("workflow_confidence", 0.7)
                        }
                    )
                    
                    return response
                    
            except Exception as e:
                # Use Error Fallback Manager for graceful degradation
                self.logger.error(f"Agentic Framework execution failed: {e}")
                try:
                    error_result = await self._error_manager.handle_execution_error(
                        error=e,
                        context=agentic_context,
                        component="agentic_framework_execution"
                    )
                    
                    view_state = await self._create_view_state(case_id, request.session_id)
                    content = error_result.get("recovery_message", "I'm having trouble processing your request. Could you please rephrase or provide more details?")
                    
                    return AgentResponse(
                        content=self._sanitizer.sanitize(content),
                        response_type=ResponseType.ANSWER,
                        session_id=request.session_id,
                        view_state=view_state,
                        sources=[],
                        plan=None
                    )
                except Exception:
                    # Final fallback - basic response
                    view_state = await self._create_view_state(case_id, request.session_id)
                    return AgentResponse(
                        content="I'm experiencing technical difficulties. Please try again or contact support.",
                        response_type=ResponseType.ANSWER,
                        session_id=request.session_id,
                        view_state=view_state,
                        sources=[],
                        plan=None
                    )

    async def _create_view_state(self, case_id: str, session_id: str) -> ViewState:
        """Create view state for the response"""
        try:
            from faultmaven.models.api import ViewState, ActiveCase
            return ViewState(
                active_case=ActiveCase(
                    case_id=case_id,
                    session_id=session_id
                )
            )
        except Exception:
            # Fallback view state
            return ViewState(active_case={"case_id": case_id, "session_id": session_id})
    async def process_query_for_case(
        self,
        case_id: str,
        request: QueryRequest
    ) -> AgentResponse:
        """
        Process query for a specific case with conversation context injection
        
        This method retrieves conversation history from the case and injects it
        into the query processing workflow to provide better context-aware responses.
        
        Args:
            case_id: Case identifier to provide conversation context
            request: QueryRequest with query, session_id, context, etc.
            
        Returns:
            AgentResponse with case analysis results using v3.1.0 schema
            
        Raises:
            ValueError: If case_id or request validation fails
            FileNotFoundError: If case not found
            RuntimeError: If agent processing fails
        """
        import logging
        logger = logging.getLogger(__name__)
        
        # Validate inputs first
        if not case_id or not case_id.strip():
            raise ValueError("Case ID cannot be empty")
        if not request:
            raise ValueError("QueryRequest cannot be None")
        if not request.query or not request.query.strip():
            raise ValueError("Query cannot be empty")
        if not request.session_id or not request.session_id.strip():
            raise ValueError("Session ID cannot be empty")
        
        logger.info(f"Processing query for case {case_id} in session {request.session_id}")
        
        return await self.execute_operation(
            "process_query_for_case",
            self._execute_case_query_processing,
            case_id,
            request,
            validate_inputs=self._validate_case_request
        )
    
    async def _execute_case_query_processing(self, case_id: str, request: QueryRequest) -> AgentResponse:
        """Execute the core case query processing logic with conversation context"""
        import logging
        logger = logging.getLogger(__name__)
        
        # Use ITracer interface for operation tracing
        with self._tracer.trace("process_case_query_workflow"):
            # 1. Sanitize input
            with self._tracer.trace("sanitize_input"):
                sanitized_query = self._sanitizer.sanitize(request.query)
            
            # 2. Verify case exists and get conversation context
            conversation_context = ""
            if self._session_service:
                # Record user query first
                with self._tracer.trace("record_case_user_query"):
                    try:
                        await self._session_service.record_case_message(
                            session_id=request.session_id,
                            message_content=sanitized_query,
                            message_type=MessageType.USER_QUERY,
                            author_id=None,
                            metadata={"source": "api", "type": "user_query", "case_id": case_id}
                        )
                    except Exception as e:
                        logger.warning(f"Failed to record user message for case {case_id}: {e}")
                
                # Update case query count if supported
                with self._tracer.trace("update_case_query_count"):
                    try:
                        from faultmaven.container import container as di
                        case_service = di.get_case_service() if hasattr(di, 'get_case_service') else None
                        if case_service and hasattr(case_service, 'add_case_query'):
                            await case_service.add_case_query(case_id, sanitized_query)
                    except Exception as e:
                        logger.warning(f"Failed to update case query count for case {case_id}: {e}")
                
                # Retrieve conversation context specific to this case
                with self._tracer.trace("retrieve_case_conversation_history"):
                    try:
                        conversation_context = await self._session_service.format_conversation_context(
                            request.session_id, case_id, limit=10  # Increased limit for case context
                        )
                        if conversation_context:
                            logger.debug(f"Retrieved case conversation context for {case_id}: {len(conversation_context)} chars")
                        else:
                            logger.debug(f"No conversation context available for case {case_id}")
                    except Exception as e:
                        logger.warning(f"Failed to retrieve conversation context for case {case_id}: {e}")
            
            # 3. Create enhanced context-aware request
            enhanced_request = QueryRequest(
                query=f"Case Context:\n{conversation_context}\n\nCurrent Query:\n{sanitized_query}" if conversation_context else sanitized_query,
                session_id=request.session_id,
                context={
                    **(request.context or {}),
                    "case_id": case_id,
                    "has_conversation_context": bool(conversation_context),
                    "context_length": len(conversation_context) if conversation_context else 0
                },
                priority=request.priority
            )
            
            # Log business event for case-specific processing
            self.log_business_event(
                "case_query_processing_started",
                "info",
                {
                    "case_id": case_id,
                    "session_id": request.session_id,
                    "query_length": len(sanitized_query),
                    "context_available": bool(conversation_context),
                    "context_length": len(conversation_context) if conversation_context else 0
                }
            )
            
            # 4. Delegate to existing process_query logic with enhanced context
            with self._tracer.trace("delegate_to_process_query"):
                agent_response = await self._execute_query_processing(enhanced_request)
            
            # 5. Update the view_state to reflect the specific case being processed
            if agent_response.view_state:
                agent_response.view_state.active_case.case_id = case_id
            
            # 6. Record assistant response to case
            if self._session_service:
                with self._tracer.trace("record_case_assistant_response"):
                    try:
                        await self._session_service.record_case_message(
                            session_id=request.session_id,
                            message_content=agent_response.content,
                            message_type=MessageType.AGENT_RESPONSE,
                            author_id=None,
                            metadata={
                                "case_id": case_id,
                                "response_type": agent_response.response_type.value,
                                "sources_count": len(agent_response.sources) if agent_response.sources else 0
                            }
                        )
                    except Exception as e:
                        logger.warning(f"Failed to record assistant response for case {case_id}: {e}")
            
            # Log completion
            self.log_business_event(
                "case_query_processing_completed",
                "info",
                {
                    "case_id": case_id,
                    "session_id": request.session_id,
                    "response_type": agent_response.response_type.value,
                    "sources_count": len(agent_response.sources) if agent_response.sources else 0
                }
            )
            
            return agent_response
    
    async def _validate_case_request(self, case_id: str, request: QueryRequest) -> None:
        """Validate case-specific request parameters"""
        if not case_id or not case_id.strip():
            raise ValidationException("Case ID cannot be empty")
        
        if not request:
            raise ValidationException("QueryRequest cannot be None")
            
        if not request.query or not request.query.strip():
            raise ValidationException("Query cannot be empty")
            
        if not request.session_id or not request.session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        # Check if session exists (best effort, non-blocking)
        if self._session_service:
            try:
                _ = await self._session_service.get_session(request.session_id, validate=False)
            except Exception:
                # Proceed without blocking - the workflow can continue with ephemeral session
                pass

    async def _validate_request(self, request: QueryRequest) -> None:
        """Validate request using interface methods
        
        Args:
            request: QueryRequest to validate
            
        Raises:
            ValidationException: If validation fails
            FileNotFoundError: If session not found
        """
        if not request.query or not request.query.strip():
            raise ValidationException("Query cannot be empty")
            
        if not request.session_id or not request.session_id.strip():
            raise ValidationException("Session ID cannot be empty")
            
        # Best-effort session check; don't block if not found
        if self._session_service:
            try:
                _ = await self._session_service.get_session(request.session_id, validate=False)
            except Exception:
                # Proceed without a persisted session; workflow remains functional
                pass
                
        # Additional validation can be added here

    async def _format_agent_response(
        self,
        case_id: str,
        session_id: str,
        query: str,
        agent_result: dict,
        start_time: datetime,
        end_time: datetime,
        processing_time: float
    ) -> AgentResponse:
        """Format v3.1.0 AgentResponse using interface sanitization
        
        Args:
            case_id: Unique case identifier for this troubleshooting case
            session_id: Session identifier
            query: User's sanitized query
            agent_result: Raw result from agent execution
            start_time: Case analysis start time
            end_time: Case analysis completion time
            processing_time: Processing time in seconds
            
        Returns:
            Formatted AgentResponse using v3.1.0 schema
        """
        # Handle None agent_result defensively
        if agent_result is None:
            agent_result = {
                'findings': [],
                'recommendations': [],
                'next_steps': [],
                'root_cause': 'Processing error occurred',
                'confidence_score': 0.0,
                'estimated_mttr': 'Unknown'
            }
        
        # 1. Determine response type based on agent result
        response_type = self._determine_response_type(agent_result)
        
        # 2. Extract sources from agent result and tools
        sources = await self._extract_sources(agent_result)
        
        # 3. Create ViewState
        view_state = await self._create_view_state(case_id, session_id)
        
        # 4. Generate content using ResponseSynthesizer (Agentic Framework) with fallback
        try:
            from faultmaven.container import container as di
            response_synthesizer = di.get_response_synthesizer()
            
            if response_synthesizer:
                # Use Agentic Framework ResponseSynthesizer for enhanced content generation
                synthesis_context = {
                    "query": query,
                    "session_id": session_id,
                    "case_id": case_id,
                    "agent_result": agent_result,
                    "processing_time": processing_time,
                    "response_type": response_type
                }
                
                synthesized_response = await response_synthesizer.synthesize_response(
                    context=synthesis_context,
                    sources=sources,
                    intent=agent_result.get("intent", "troubleshooting")
                )
                
                content = synthesized_response.get("content", "")
                self.logger.info(f"ResponseSynthesizer enhanced content generation with intent: {agent_result.get('intent', 'unknown')}")
            else:
                # Fallback to original content generation
                content = self._generate_content(agent_result, query)
                
        except Exception as e:
            self.logger.warning(f"ResponseSynthesizer failed, falling back to basic formatting: {e}")
            # Fallback to original content generation
            content = self._generate_content(agent_result, query)
        
        # 5. Handle plan for PLAN_PROPOSAL responses
        plan = None
        if response_type == ResponseType.PLAN_PROPOSAL:
            plan = self._extract_plan_steps(agent_result)
        
        # 6. Create AgentResponse
        response = AgentResponse(
            content=self._sanitizer.sanitize(content),
            response_type=response_type,
            session_id=request.session_id,
            view_state=view_state,
            sources=sources,
            plan=plan
        )
        
        return response

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        """Safely convert a value to float, returning default on error"""
        try:
            if value is None:
                return default
            return float(value)
        except (ValueError, TypeError):
            return default

    def _determine_response_type(self, agent_result: dict) -> ResponseType:
        """Determine the response type based on agent result"""
        # Check for clarification indicators
        if self._needs_clarification(agent_result):
            return ResponseType.CLARIFICATION_REQUEST
        
        # Check for confirmation indicators
        if self._needs_confirmation(agent_result):
            return ResponseType.CONFIRMATION_REQUEST
        
        # Check for multi-step plan indicators
        if self._has_plan(agent_result):
            return ResponseType.PLAN_PROPOSAL
        
        # Default to answer
        return ResponseType.ANSWER

    def _needs_clarification(self, agent_result: dict) -> bool:
        """Check if the agent result indicates need for clarification"""
        # Look for clarification keywords in recommendations or findings
        clarification_keywords = ['clarif', 'unclear', 'more information', 'specify', 'which', 'ambiguous']
        
        # Check recommendations
        recommendations = agent_result.get('recommendations', [])
        if isinstance(recommendations, list):
            for rec in recommendations:
                if isinstance(rec, str):
                    rec_lower = rec.lower()
                    if any(keyword in rec_lower for keyword in clarification_keywords):
                        return True
        
        # Check findings
        findings = agent_result.get('findings', [])
        if isinstance(findings, list):
            for finding in findings:
                if isinstance(finding, dict):
                    message = finding.get('message', '')
                    if isinstance(message, str) and any(keyword in message.lower() for keyword in clarification_keywords):
                        return True
                elif isinstance(finding, str):
                    if any(keyword in finding.lower() for keyword in clarification_keywords):
                        return True
        
        return False

    def _needs_confirmation(self, agent_result: dict) -> bool:
        """Check if the agent result indicates need for confirmation"""
        # Look for confirmation keywords in recommendations
        confirmation_keywords = ['confirm', 'verify', 'proceed', 'approve', 'authorize']
        
        # Check recommendations only (as per test requirements)
        recommendations = agent_result.get('recommendations', [])
        if isinstance(recommendations, list):
            for rec in recommendations:
                if isinstance(rec, str):
                    rec_lower = rec.lower()
                    if any(keyword in rec_lower for keyword in confirmation_keywords):
                        return True
        
        return False

    def _has_plan(self, agent_result: dict) -> bool:
        """Check if the agent result contains a multi-step plan"""
        # Check for explicit plan or multiple next_steps
        next_steps = agent_result.get('next_steps', [])
        return isinstance(next_steps, list) and len(next_steps) > 2

    async def _extract_sources(self, agent_result: dict) -> List[Source]:
        """Extract sources from agent result and tools"""
        sources = []
        
        # Extract from knowledge base results if available
        kb_results = agent_result.get('knowledge_base_results', [])
        for kb_result in kb_results:
            if isinstance(kb_result, dict):
                text = kb_result.get('content') or kb_result.get('snippet') or ''
                preview = str(text)[:200]
                if len(str(text)) > 200:
                    preview += "..."
                sources.append(Source(
                    type=SourceType.KNOWLEDGE_BASE,
                    content=preview,
                    confidence=None,
                    metadata={"title": kb_result.get('title', 'Knowledge Base Document')}
                ))
        
        # Extract from tool results if available
        tool_results = agent_result.get('tool_results', [])
        for tool_result in tool_results:
            if isinstance(tool_result, dict):
                tool_name = tool_result.get('tool_name', 'unknown')
                if 'web_search' in tool_name.lower():
                    sources.append(Source(
                        type=SourceType.WEB_SEARCH,
                        content=(tool_result.get('content', '')[:200] + "..."),
                        confidence=None,
                        metadata={"source": tool_result.get('source', 'Web Search')}
                    ))
                elif 'log' in tool_name.lower():
                    sources.append(Source(
                        type=SourceType.LOG_FILE,
                        content=(tool_result.get('content', '')[:200] + "..."),
                        confidence=None,
                        metadata={"filename": tool_result.get('filename', 'Log File')}
                    ))
        
        return sources[:10]  # Limit to 10 sources

    async def _create_view_state(self, case_id: str, session_id: str) -> ViewState:
        """Create ViewState for the current case"""
        # Import necessary models
        from faultmaven.models.api import User, Case
        
        # Create a default user for ViewState (in production this would come from session)
        user = User(
            user_id="default_user",
            email="user@example.com", 
            name="Default User"
        )
        
        # Create active case object
        active_case = Case(
            case_id=case_id,
            title="Active Case",
            status="active",
            created_at="2025-08-30T00:00:00Z",
            updated_at="2025-08-30T00:00:00Z",
            session_id=session_id
        )
        
        # Get uploaded data from session if available
        uploaded_data = []
        if self._session_service:
            try:
                session = await self._session_service.get_session(session_id)
                if session and hasattr(session, 'data_uploads'):
                    for data_id in session.data_uploads:
                        uploaded_data.append(UploadedData(
                            id=data_id,
                            name=f"data_{data_id}",
                            type="unknown"
                        ))
            except Exception as e:
                self.logger.warning(f"Failed to get session data uploads: {e}")
        
        return ViewState(
            session_id=session_id,
            user=user,
            active_case=active_case,
            cases=[active_case],
            messages=[],
            uploaded_data=uploaded_data
        )

    def _generate_content(self, agent_result: dict, query: str) -> str:
        """Generate content from agent result"""
        # Check if agent is in error state first
        current_phase = agent_result.get('current_phase')
        if current_phase == 'error':
            # Be transparent about errors instead of faking responses
            error_info = agent_result.get('case_context', {})
            error_message = error_info.get('error', 'Unknown error occurred')
            
            return (
                f"I'm unable to process your query at the moment due to a technical issue.\n\n"
                f"Error details: {error_message}\n\n"
                f"This might be due to:\n"
                f"• LLM service connectivity issues\n"
                f"• System configuration problems\n"
                f"• Temporary service outage\n\n"
                f"Please try again in a moment, or contact support if the issue persists."
            )
        
        # Start with any direct response content
        content_parts = []
        
        # Add root cause if available
        root_cause = agent_result.get('root_cause')
        if root_cause:
            content_parts.append(f"Root Cause: {root_cause}")
        
        # Add key findings
        findings = agent_result.get('findings', [])
        if findings:
            content_parts.append("Key Findings:")
            for finding in findings[:3]:  # Limit to top 3 findings
                if isinstance(finding, dict):
                    message = finding.get('message', finding.get('description', 'Finding discovered'))
                    content_parts.append(f"• {message}")
                elif isinstance(finding, str):
                    content_parts.append(f"• {finding}")
                else:
                    # Convert non-dict, non-string to a meaningful message
                    content_parts.append(f"• Analysis finding identified")
        
        # Add recommendations
        recommendations = agent_result.get('recommendations', [])
        if recommendations:
            content_parts.append("Recommendations:")
            for rec in recommendations[:3]:  # Limit to top 3 recommendations
                if isinstance(rec, str):
                    content_parts.append(f"• {rec}")
                elif isinstance(rec, dict):
                    # Extract meaningful text from dict recommendation
                    rec_text = rec.get('text', rec.get('description', rec.get('action', 'Review system configuration')))
                    content_parts.append(f"• {rec_text}")
                else:
                    # Convert non-dict, non-string to a meaningful recommendation
                    content_parts.append(f"• Follow standard troubleshooting procedures")
        
        # If no content found but not in error state, indicate system limitation
        if not content_parts:
            content_parts = [
                f"I'm unable to provide specific insights for your query: '{query}'.",
                "This may be due to:",
                "• Insufficient context in your query",
                "• System processing limitations", 
                "• Temporary analysis service issues",
                "",
                "Try providing more specific details about your problem, such as:",
                "• Error messages you're seeing",
                "• Steps that led to the issue", 
                "• System components involved"
            ]
        
        return "\n\n".join(content_parts)

    def _extract_plan_steps(self, agent_result: dict) -> List[PlanStep]:
        """Extract plan steps from agent result"""
        steps = []
        next_steps = agent_result.get('next_steps', [])
        
        for step in next_steps:
            if isinstance(step, str):
                steps.append(PlanStep(description=step))
            elif isinstance(step, dict):
                description = step.get('description', step.get('step', str(step)))
                steps.append(PlanStep(description=description))
        
        return steps

    async def analyze_findings(
        self, 
        findings: List[Dict[str, Any]], 
        session_id: str
    ) -> Dict[str, Any]:
        """
        Perform deep analysis on investigation findings using interface dependencies
        
        Args:
            findings: List of findings to analyze
            session_id: Session identifier
            
        Returns:
            Analysis results with patterns and correlations
            
        Raises:
            RuntimeError: If analysis fails
        """
        return await self.execute_operation(
            "analyze_findings",
            self._execute_findings_analysis,
            findings,
            session_id
        )
    
    async def _execute_findings_analysis(
        self, 
        findings: List[Dict[str, Any]], 
        session_id: str
    ) -> Dict[str, Any]:
        """Execute the core findings analysis logic"""
        # Use ITracer interface for operation tracing
        with self._tracer.trace("findings_analysis_workflow"):
            # Sanitize input findings
            with self._tracer.trace("sanitize_findings"):
                sanitized_findings = self._sanitizer.sanitize(findings)
            
            # Log business event
            self.log_business_event(
                "findings_analysis_started",
                "info",
                {
                    "session_id": session_id,
                    "findings_count": len(sanitized_findings)
                }
            )
            
            # Group findings by type
            with self._tracer.trace("group_findings_by_type"):
                findings_by_type = self._group_findings_by_type(sanitized_findings)
            
            # Identify patterns
            with self._tracer.trace("identify_patterns"):
                patterns = await self._identify_patterns(findings_by_type)
            
            # Calculate severity distribution
            with self._tracer.trace("calculate_severity_distribution"):
                severity_dist = self._calculate_severity_distribution(sanitized_findings)
            
            # Generate insights
            with self._tracer.trace("generate_insights"):
                insights = {
                    "total_findings": len(sanitized_findings),
                    "findings_by_type": {
                        type_name: len(items) for type_name, items in findings_by_type.items()
                    },
                    "severity_distribution": severity_dist,
                    "patterns_identified": patterns,
                    "critical_issues": self._extract_critical_issues(sanitized_findings),
                    "session_id": session_id,
                    "analysis_timestamp": datetime.utcnow().isoformat() + 'Z'
                }
            
            # Log metrics
            self.log_metric(
                "findings_analyzed",
                len(sanitized_findings),
                "count",
                {"session_id": session_id}
            )
            
            if patterns:
                self.log_metric(
                    "patterns_identified",
                    len(patterns),
                    "count",
                    {"session_id": session_id}
                )
            
            # Sanitize output
            with self._tracer.trace("sanitize_output"):
                return self._sanitizer.sanitize(insights)

    def _group_findings_by_type(
        self, findings: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Group findings by their type"""
        grouped = {}
        for finding in findings:
            if isinstance(finding, dict):
                finding_type = finding.get("type", "unknown")
                if finding_type not in grouped:
                    grouped[finding_type] = []
                grouped[finding_type].append(finding)
        return grouped

    async def _identify_patterns(
        self, findings_by_type: Dict[str, List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        """Identify patterns in grouped findings using interface dependencies"""
        patterns = []
        
        # Check for error clustering
        if "error" in findings_by_type and len(findings_by_type["error"]) > 3:
            patterns.append({
                "pattern": "error_clustering",
                "description": "Multiple errors detected in close proximity",
                "count": len(findings_by_type["error"]),
                "severity": "high"
            })
        
        # Check for performance degradation
        if "performance" in findings_by_type:
            patterns.append({
                "pattern": "performance_issues",
                "description": "Performance-related findings detected",
                "count": len(findings_by_type["performance"]),
                "severity": "medium"
            })
        
        # Check for security issues
        if "security" in findings_by_type:
            patterns.append({
                "pattern": "security_concerns",
                "description": "Security-related findings detected",
                "count": len(findings_by_type["security"]),
                "severity": "critical"
            })
        
        return patterns

    def _calculate_severity_distribution(
        self, findings: List[Dict[str, Any]]
    ) -> Dict[str, int]:
        """Calculate distribution of findings by severity"""
        distribution = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
        
        for finding in findings:
            if isinstance(finding, dict):
                severity = finding.get("severity", "info").lower()
                if severity in distribution:
                    distribution[severity] += 1
                    
        return distribution

    def _extract_critical_issues(
        self, findings: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Extract critical issues from findings"""
        critical_issues = []
        
        for finding in findings:
            if isinstance(finding, dict):
                severity = finding.get("severity", "").lower()
                if severity in ["critical", "high"]:
                    critical_issues.append({
                        "type": finding.get("type"),
                        "message": finding.get("message"),
                        "severity": finding.get("severity"),
                        "source": finding.get("source"),
                        "timestamp": finding.get("timestamp"),
                        "confidence": finding.get("confidence")
                    })
                    
        return critical_issues

    async def get_case_status(
        self, 
        case_id: str, 
        session_id: str
    ) -> Dict[str, Any]:
        """
        Get the status of a specific case using interface dependencies
        
        Args:
            case_id: Case identifier
            session_id: Session identifier
            
        Returns:
            Case status information
        """
        return await self.execute_operation(
            "get_case_status",
            self._execute_status_retrieval,
            case_id,
            session_id
        )

    
    async def _execute_status_retrieval(
        self,
        case_id: str, 
        session_id: str
    ) -> Dict[str, Any]:
        """Execute the core status retrieval logic"""
        # Use ITracer interface for operation tracing
        with self._tracer.trace("case_status_retrieval"):
            # In a full implementation, this would query persistent storage
            # via an ISessionStore interface to retrieve case status
            with self._tracer.trace("retrieve_status_data"):
                status = {
                    "case_id": case_id,
                    "session_id": session_id,
                    "status": "completed",  # Placeholder
                    "progress": 100.0,
                    "phase": "completed",
                    "last_updated": datetime.utcnow().isoformat() + 'Z'
                }
            
            with self._tracer.trace("sanitize_status_output"):
                return self._sanitizer.sanitize(status)

    async def cancel_case(
        self, 
        case_id: str, 
        session_id: str
    ) -> bool:
        """
        Cancel an ongoing case using interface dependencies
        
        Args:
            case_id: Case identifier
            session_id: Session identifier
            
        Returns:
            True if cancellation was successful
        """
        return await self.execute_operation(
            "cancel_case",
            self._execute_case_cancellation,
            case_id,
            session_id
        )
    
    async def _execute_case_cancellation(
        self,
        case_id: str, 
        session_id: str
    ) -> bool:
        """Execute the core case cancellation logic"""
        # Use ITracer interface for operation tracing
        with self._tracer.trace("cancel_case"):
            # In a full implementation, this would use ISessionStore
            # to update case status and potentially signal
            # the running agent to stop
            
            # Log business event
            self.log_business_event(
                "case_cancelled",
                "info",
                {
                    "case_id": case_id,
                    "session_id": session_id
                }
            )
            
            return True

    async def get_case_results(
        self, 
        case_id: str, 
        session_id: str
    ) -> TroubleshootingResponse:
        """
        Get case results by ID with proper validation
        
        Args:
            case_id: Case identifier
            session_id: Session identifier for access control
            
        Returns:
            TroubleshootingResponse with case results
            
        Raises:
            ValueError: If case_id or session_id is invalid
            FileNotFoundError: If case not found
            RuntimeError: If retrieval fails
        """
        def validate_inputs(case_id: str, session_id: str) -> None:
            if not case_id or not case_id.strip():
                raise ValueError("Case ID cannot be empty")
            if not session_id or not session_id.strip():
                raise ValueError("Session ID cannot be empty")
        
        return await self.execute_operation(
            "get_case_results",
            self._execute_results_retrieval,
            case_id,
            session_id,
            validate_inputs=validate_inputs
        )
    
    async def _execute_results_retrieval(
        self,
        case_id: str, 
        session_id: str
    ) -> TroubleshootingResponse:
        """Execute the core results retrieval logic"""
        # In a full implementation, this would query persistent storage
        # via ICaseStore interface to retrieve results
        # For now, return placeholder response
        
        placeholder_response = TroubleshootingResponse(
            case_id=case_id,
            session_id=session_id,
            status="completed",
            findings=[
                {
                    "type": "info",
                    "message": f"Case {case_id} completed",
                    "severity": "info",
                    "timestamp": datetime.utcnow().isoformat() + 'Z',
                    "source": "case_store"
                }
            ],
            root_cause="Case completed successfully",
            recommendations=["Review case findings", "Take appropriate action"],
            confidence_score=0.8,
            estimated_mttr="15 minutes",
            next_steps=["Monitor system", "Verify fix effectiveness"],
            created_at=datetime.utcnow(),
            completed_at=datetime.utcnow()
        )
        
        return placeholder_response

    async def list_session_cases(
        self, 
        session_id: str, 
        limit: int = 10, 
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        List cases for a session with pagination
        
        Args:
            session_id: Session identifier
            limit: Maximum number of results
            offset: Pagination offset
            
        Returns:
            List of case summary dictionaries
            
        Raises:
            ValueError: If session_id is invalid or pagination params are invalid
            FileNotFoundError: If session not found
            RuntimeError: If listing fails
        """
        def validate_inputs(session_id: str, limit: int, offset: int) -> None:
            if not session_id or not session_id.strip():
                raise ValueError("Session ID cannot be empty")
            if limit <= 0 or limit > 100:
                raise ValueError("Limit must be between 1 and 100")
            if offset < 0:
                raise ValueError("Offset must be non-negative")
        
        return await self.execute_operation(
            "list_session_cases",
            self._execute_cases_listing,
            session_id,
            limit,
            offset,
            validate_inputs=validate_inputs
        )
    
    async def _execute_cases_listing(
        self,
        session_id: str, 
        limit: int, 
        offset: int
    ) -> List[Dict[str, Any]]:
        """Execute the core cases listing logic"""
        # In a full implementation, this would query persistent storage
        # via ISessionStore interface to list cases
        # For now, return placeholder data
        
        from datetime import timedelta
        
        # Generate some placeholder cases
        base_time = datetime.utcnow()
        cases = []
        
        for i in range(min(limit, 3)):  # Return up to 3 placeholder cases
            cases.append({
                "case_id": f"case_{session_id}_{i + offset + 1}",
                "query": f"Sample troubleshooting query {i + offset + 1}",
                "status": "completed",
                "priority": "medium",
                "findings_count": 2 + i,
                "recommendations_count": 1 + i,
                "confidence_score": 0.7 + (i * 0.1),
                "created_at": (base_time - timedelta(hours=i + 1)).isoformat() + 'Z',
                "completed_at": (base_time - timedelta(hours=i)).isoformat() + 'Z',
                "estimated_mttr": f"{15 + (i * 5)} minutes"
            })
        
        # Log business metric
        self.log_metric(
            "session_cases_listed",
            len(cases),
            "count",
            {"session_id": session_id}
        )
        
        return self._sanitizer.sanitize(cases)


    async def health_check(self) -> Dict[str, Any]:
        """
        Check health of agent service and all dependencies
        
        Returns:
            Dictionary with health status and component details
        """
        # Get base health from BaseService
        base_health = await super().health_check()
        
        # Add component-specific health checks
        components = {
            "llm_provider": "unknown",
            "sanitizer": "unknown", 
            "tracer": "unknown",
            "tools": "unknown"
        }
        
        # Check LLM provider
        try:
            if self._llm and hasattr(self._llm, 'generate_response'):
                components["llm_provider"] = "healthy"
            else:
                components["llm_provider"] = "unavailable"
        except Exception:
            components["llm_provider"] = "unhealthy"
        
        # Check sanitizer
        try:
            if self._sanitizer and hasattr(self._sanitizer, 'sanitize'):
                # Test sanitization
                test_result = self._sanitizer.sanitize("test")
                components["sanitizer"] = "healthy"
            else:
                components["sanitizer"] = "unavailable"
        except Exception:
            components["sanitizer"] = "unhealthy"
        
        # Check tracer
        try:
            if self._tracer and hasattr(self._tracer, 'trace'):
                components["tracer"] = "healthy"
            else:
                components["tracer"] = "unavailable"
        except Exception:
            components["tracer"] = "unhealthy"
        
        # Check tools
        try:
            if self._tools and len(self._tools) > 0:
                components["tools"] = f"healthy ({len(self._tools)} tools available)"
            else:
                components["tools"] = "no tools available"
        except Exception:
            components["tools"] = "unhealthy"
        
        # Determine overall status
        unhealthy_components = [
            comp for status in components.values() 
            for comp in [status] if "unhealthy" in str(status)
        ]
        
        status = "healthy"
        if unhealthy_components:
            status = "degraded"
        elif any("unavailable" in str(status) for status in components.values()):
            status = "degraded"
        
        # Combine with base health
        health_info = {
            **base_health,
            "service": "agent_service",
            "status": status,
            "components": components
        }
        
        return health_info

    # Agentic Framework Helper Methods
    
    def _extract_tool_requests(self, execution_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract tool requests from workflow execution results."""
        tool_requests = []
        
        try:
            execution_data = execution_result.get("execution_data", {})
            step_outputs = execution_data.get("step_outputs", {})
            
            for step_id, output in step_outputs.items():
                if "tool_result" in output:
                    tool_requests.append({
                        "tool_name": output.get("tool_name", "unknown"),
                        "context": output.get("context", {}),
                        "query": execution_data.get("query", "")
                    })
        except Exception as e:
            self.logger.warning(f"Failed to extract tool requests: {e}")
        
        return tool_requests
    
    def _extract_sources_from_results(self, tool_results: List[Dict[str, Any]]) -> List[Source]:
        """Extract sources from tool execution results."""
        sources = []
        
        try:
            for result in tool_results:
                if result.get("success") and "content" in result:
                    sources.append(Source(
                        type=SourceType.KNOWLEDGE_BASE,
                        content=str(result["content"])[:200],
                        confidence=None,
                        metadata={"tool": result.get("tool_name", "unknown")}
                    ))
        except Exception as e:
            self.logger.warning(f"Failed to extract sources from results: {e}")
        
        return sources[:5]  # Limit to 5 sources
    
    def _extract_plan_from_workflow(self, execution_result: Dict[str, Any]) -> Optional[List[PlanStep]]:
        """Extract plan steps from workflow execution results."""
        if not execution_result or not execution_result.get("success"):
            return None
        
        try:
            plan_steps = []
            execution_data = execution_result.get("execution_data", {})
            
            if "successful_steps" in execution_data:
                for i, step in enumerate(execution_data["successful_steps"]):
                    plan_steps.append(PlanStep(
                        description=f"Step {i+1}: {step.get('step_id', 'Processing')}"
                    ))
            
            return plan_steps if plan_steps else None
        except Exception as e:
            self.logger.warning(f"Failed to extract plan from workflow: {e}")
            return None
    
    def _determine_response_type_from_synthesis(self, synthesis_result) -> ResponseType:
        """Determine response type from synthesis result."""
        try:
            if hasattr(synthesis_result, 'response_type'):
                return ResponseType(synthesis_result.response_type)
            elif hasattr(synthesis_result, 'content'):
                content = synthesis_result.content.lower()
                if any(word in content for word in ["confirm", "proceed", "authorize"]):
                    return ResponseType.CONFIRMATION_REQUEST
                elif any(word in content for word in ["clarify", "more information", "specify"]):
                    return ResponseType.CLARIFICATION_REQUEST
                else:
                    return ResponseType.ANSWER
        except Exception:
            pass
        
        return ResponseType.ANSWER
    
    async def _create_enhanced_view_state(
        self, 
        session_id: str, 
        memory_context: Dict[str, Any], 
        planning_state: Dict[str, Any]
    ) -> ViewState:
        """Create enhanced view state with memory and planning context."""
        try:
            # Get basic view state
            basic_view_state = await self._create_view_state("temp", session_id)
            
            # Enhance with memory and planning context
            basic_view_state.memory_context = memory_context
            basic_view_state.planning_state = planning_state
            
            return basic_view_state
            
        except Exception as e:
            self.logger.error(f"Failed to create enhanced view state: {e}")
            # Fallback to basic view state
            return await self._create_view_state("temp", session_id)
    
    async def _create_safety_response(self, request: QueryRequest, reason: str) -> AgentResponse:
        """Create a safe response when input validation fails."""
        try:
            view_state = await self._create_view_state("safety", request.session_id)
            
            return AgentResponse(
                content=f"I cannot process this request due to safety concerns: {reason}. Please rephrase your query.",
                response_type=ResponseType.ANSWER,
                session_id=request.session_id,
                view_state=view_state,
                sources=[],
                plan=None
            )
        except Exception as e:
            self.logger.error(f"Failed to create safety response: {e}")
            raise
    
    async def _create_basic_agentic_response(
        self, 
        request: QueryRequest, 
        sanitized_query: str, 
        execution_result: Optional[Dict[str, Any]]
    ) -> AgentResponse:
        """Create a basic response when advanced synthesis fails."""
        try:
            view_state = await self._create_view_state("basic", request.session_id)
            
            if execution_result and execution_result.get("success"):
                content = execution_result.get("execution_data", {}).get("final_response", {}).get("content", 
                    "I've processed your request using the agentic framework. The analysis is complete.")
            else:
                content = "I'm processing your request. Let me analyze the situation and provide recommendations."
            
            return AgentResponse(
                content=content,
                response_type=ResponseType.ANSWER,
                session_id=request.session_id,
                view_state=view_state,
                sources=[],
                plan=None
            )
        except Exception as e:
            self.logger.error(f"Failed to create basic agentic response: {e}")
            raise
    
    async def _convert_langgraph_to_agent_response(
        self, 
        langgraph_result: Dict[str, Any], 
        session_id: str, 
        view_state: ViewState
    ) -> AgentResponse:
        """Convert LangGraph agent result to AgentResponse format."""
        try:
            # Extract content from LangGraph result
            content = langgraph_result.get("case_context", {}).get("agent_response", "")
            if not content:
                content = "Investigation completed using advanced reasoning."
            
            # Extract findings as sources
            sources = []
            findings = langgraph_result.get("findings", [])
            for finding in findings[:3]:  # Limit to 3 sources
                if isinstance(finding, dict):
                    sources.append(Source(
                        type=SourceType.KNOWLEDGE_BASE,
                        content=finding.get("finding", "")[:200],
                        confidence=None,
                        metadata={"phase": finding.get("phase", "analysis")}
                    ))
            
            return AgentResponse(
                content=content,
                response_type=ResponseType.ANSWER,
                session_id=session_id,
                view_state=view_state,
                sources=sources,
                plan=None
            )
            
        except Exception as e:
            self.logger.error(f"Failed to convert LangGraph result: {e}")
            # Return basic response
            return AgentResponse(
                content="Analysis completed using advanced AI reasoning.",
                response_type=ResponseType.ANSWER,
                session_id=session_id,
                view_state=view_state,
                sources=[],
                plan=None
            )


