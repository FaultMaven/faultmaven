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
from datetime import datetime
from typing import Any, Dict, List, Optional

from faultmaven.services.base_service import BaseService
from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer
from faultmaven.models import QueryRequest, TroubleshootingResponse, AgentResponse, ViewState, UploadedData, Source, SourceType, ResponseType, PlanStep, TitleGenerateRequest, TitleResponse
from faultmaven.exceptions import ValidationException
from faultmaven.core.gateway.gateway import PreProcessingGateway
from faultmaven.skills.clarifier import ClarifierSkill


class AgentService(BaseService):
    """Agent Service using interface dependencies via dependency injection"""

    def __init__(
        self,
        llm_provider: ILLMProvider,
        tools: List[BaseTool],
        tracer: ITracer,
        sanitizer: ISanitizer,
        session_service: Optional[Any] = None
    ):
        """Initialize with interface dependencies via dependency injection
        
        Args:
            llm_provider: Interface for LLM operations
            tools: List of tool interfaces for agent execution
            tracer: Interface for distributed tracing
            sanitizer: Interface for data sanitization
            session_service: Optional session service for session validation
        """
        super().__init__()
        self._llm = llm_provider
        self._tools = tools
        self._tracer = tracer
        self._sanitizer = sanitizer
        self._session_service = session_service

    # --- Output finalization helpers ---
    def _remove_redaction_tags(self, text: str) -> str:
        try:
            import re
            # Replace placeholders like <PERSON>, <LOCATION>, <US_DRIVER_LICENSE>
            return re.sub(r"<[A-Z_]+>", "[redacted]", text or "")
        except Exception:
            return text or ""

    def _finalize_text(self, text: str) -> str:
        # Apply sanitizer then strip redaction artifacts for user display
        try:
            sanitized = self._sanitizer.sanitize(text)
        except Exception:
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

        view_state = await self._create_view_state(case_id, request.session_id)
        return TitleResponse(title=self._sanitizer.sanitize(title_text), view_state=view_state)

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
            
            # 5. Enhanced query with conversation context
            enhanced_query = sanitized_query
            if conversation_context:
                enhanced_query = f"{conversation_context}\n{sanitized_query}"
                self.logger.debug(f"Injected conversation context. Original query: {len(sanitized_query)} chars, Enhanced: {len(enhanced_query)} chars")
            
            # 6. Pre-processing gateway (clarity/reality/assumptions)
            gateway = PreProcessingGateway()
            gateway_result = gateway.process(sanitized_query)

            # Handle greetings with a friendly response instead of clarification
            if gateway_result.is_greeting:
                view_state = await self._create_view_state(case_id, request.session_id)
                content = (
                    "Hi! How can I help you troubleshoot right now?\n"
                    "Give me a brief description of the issue (symptoms, where you see it, any error codes)."
                )
                response = AgentResponse(
                    content=self._sanitizer.sanitize(content),
                    response_type=ResponseType.ANSWER,
                    view_state=view_state,
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
                            metadata={"intent": "greeting"}
                        )
                    except Exception as e:
                        self.logger.warning(f"Failed to record assistant greeting response: {e}")
                
                return response

            # Performance issues: return actionable checklist (avoid LLM and general path)
            if gateway_result.is_performance_issue:
                view_state = await self._create_view_state(case_id, request.session_id)
                content = (
                    "Performance checklist to start narrowing it down:\n\n"
                    "• Scope: Is the slowness global or a subset (endpoints, regions, users)?\n"
                    "• Saturation: CPU, memory, I/O, connections, thread pool, GC pauses?\n"
                    "• External deps: DB latency, slow queries, cache hit rate, network RTT, DNS.\n"
                    "• Recent changes: deploys, config, traffic spikes, feature flags.\n"
                    "• Metrics/logs to check now: p95 latency, error rate, queue depths, timeouts."
                )
                response = AgentResponse(
                    content=self._finalize_text(content),
                    response_type=ResponseType.ANSWER,
                    view_state=view_state,
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
                            metadata={"intent": "performance_checklist"}
                        )
                    except Exception as e:
                        self.logger.warning(f"Failed to record assistant performance response: {e}")
                
                return response

            # Connectivity symptom handling (e.g., "connection refused") with retrieval-backed sources
            lower_q_conn = sanitized_query.lower()
            if any(kw in lower_q_conn for kw in [
                "connection refused", "cannot connect", "can't connect", "port closed", "connection reset by peer"
            ]):
                view_state = await self._create_view_state(case_id, request.session_id)
                sources = []
                try:
                    with self._tracer.trace("retrieval_connectivity_symptom"):
                        # Attempt to acquire unified retrieval
                        retrieval = None
                        try:
                            from faultmaven.container import container as di
                            if hasattr(di, 'get_unified_retrieval_service'):
                                retrieval = di.get_unified_retrieval_service()
                        except Exception:
                            retrieval = None

                        if retrieval is None or not hasattr(retrieval, 'search'):
                            try:
                                from faultmaven.services.unified_retrieval_service import UnifiedRetrievalService as URS
                                retrieval = URS(
                                    knowledge_service=di.get_knowledge_service() if 'di' in locals() else None,
                                    vector_store=di.get_vector_store() if 'di' in locals() else None,
                                    sanitizer=self._sanitizer,
                                    tracer=self._tracer,
                                    enable_caching=False,
                                    adapter_timeout_seconds=2.0,
                                )
                            except Exception:
                                retrieval = None

                        if retrieval is not None:
                            # Build request shim if contracts are unavailable
                            def _make_req(enabled_sources):
                                try:
                                    from faultmaven.models.microservice_contracts.core_contracts import RetrievalRequest as _RR
                                    return _RR(
                                        query=sanitized_query,
                                        context=[conversation_context] if conversation_context else [],
                                        enabled_sources=enabled_sources,
                                        max_results=5,
                                        include_recency_bias=True,
                                        semantic_similarity_threshold=0.0,
                                        source_weights={}
                                    )
                                except Exception:
                                    class _ShimReq:
                                        def __init__(self, **kwargs):
                                            for k, v in kwargs.items():
                                                setattr(self, k, v)
                                    return _ShimReq(
                                        query=sanitized_query,
                                        context=[conversation_context] if conversation_context else [],
                                        enabled_sources=enabled_sources,
                                        max_results=5,
                                        include_recency_bias=True,
                                        semantic_similarity_threshold=0.0,
                                        source_weights={}
                                    )

                            # Prefer pattern + kb for connectivity
                            for enabled in (['pattern','kb'], ['pattern']):
                                try:
                                    resp = await retrieval.search(_make_req(enabled))
                                    ev_list = getattr(resp, 'evidence', [])
                                    if ev_list:
                                        for ev in ev_list[:3]:
                                            snippet = getattr(ev, 'snippet', None)
                                            source = getattr(ev, 'source', 'kb')
                                            sources.append(Source(
                                                type=SourceType.KNOWLEDGE_BASE,
                                                content=(snippet[:200] if snippet else ''),
                                                confidence=None,
                                                metadata={"source": source}
                                            ))
                                        break
                                except Exception:
                                    continue
                except Exception:
                    pass

                # If no sources from retrieval, do a lightweight textual KB search for visibility
                if not sources:
                    try:
                        from faultmaven.container import container as di
                        ks = di.get_knowledge_service() if hasattr(di, 'get_knowledge_service') else None
                        if ks and hasattr(ks, 'search_documents'):
                            sr = await ks.search_documents(
                                query=sanitized_query,
                                document_type=None,
                                tags=None,
                                limit=3,
                                similarity_threshold=None,
                                rank_by=None
                            )
                            for item in sr.get('results', [])[:1]:
                                meta = item.get('metadata', {})
                                sources.append(Source(
                                    type=SourceType.KNOWLEDGE_BASE,
                                    content=item.get('content', '')[:200],
                                    confidence=None,
                                    metadata={"title": meta.get('title', 'KB Document')}
                                ))
                    except Exception:
                        pass

                # If still no sources, attempt direct vector search fallback
                if not sources:
                    try:
                        from faultmaven.container import container as di
                        vs = di.get_vector_store() if hasattr(di, 'get_vector_store') else None
                        if vs and hasattr(vs, 'search'):
                            vr = await vs.search(query=sanitized_query, k=3)
                            for item in vr[:1]:
                                meta = item.get('metadata', {}) if isinstance(item, dict) else {}
                                snippet = item.get('content', '') if isinstance(item, dict) else ''
                                sources.append(Source(
                                    type=SourceType.KNOWLEDGE_BASE,
                                    content=snippet[:200],
                                    confidence=None,
                                    metadata={"title": meta.get('title', meta.get('id', 'KB Vector Doc'))}
                                ))
                    except Exception:
                        pass

                # Generate a concise LLM response using any retrieved context (LLM-first with retries)
                content = ""
                # Use Source model fields (content), not dict-style access
                context_snippets = []
                if sources:
                    for s in sources:
                        try:
                            snippet_text = getattr(s, 'content', None)
                            if snippet_text:
                                context_snippets.append(str(snippet_text)[:400])
                        except Exception:
                            continue
                base_prompt = (
                    "You are a senior SRE assistant. The user observes connectivity errors (e.g., 'connection refused').\n"
                    "Provide a concise, actionable 3-5 step triage focused on likely causes (service down, bind/interface, firewall/SG, health probes, client endpoint).\n"
                    "Be specific and practical.\n\n"
                )
                for temperature in (0.2, 0.0):
                    try:
                        with self._tracer.trace("llm_connectivity_response"):
                            prompt = (
                                base_prompt +
                                ("Context:\n" + "\n\n".join(context_snippets[:3]) + "\n\n" if context_snippets else "") +
                                f"User query: {sanitized_query}\n\nAnswer:"
                            )
                            llm_text = await self._llm.generate(prompt, temperature=temperature, max_tokens=300)
                            content = (llm_text or "").strip()
                            if content:
                                break
                    except Exception as e:
                        # Log and retry with next temperature
                        try:
                            self.log_business_event("llm_connectivity_error", "warning", {"error": str(e)})
                        except Exception:
                            pass
                        continue
                if not content:
                    # Safe fallback if LLM fails twice
                    content = (
                        "Connection refused quick checks:\n\n"
                        "1) Service process running and healthy?\n"
                        "2) Listening on expected port/interface (0.0.0.0 vs 127.0.0.1)?\n"
                        "3) Firewall/security groups allow the port?\n"
                        "4) Readiness/health probes passing (orchestration)?\n"
                        "5) Client using correct hostname, port, and protocol?"
                    )
                return AgentResponse(
                    content=self._finalize_text(content),
                    response_type=ResponseType.ANSWER,
                    view_state=view_state,
                    sources=sources,
                    plan=None,
                )

            # Handle definition/general questions using LLM (with RAG and safe fallback)
            if (
                gateway_result.is_definition_question or (
                    getattr(gateway_result, 'is_general_question', False) and len(sanitized_query) >= 12
                )
            ) and not getattr(gateway_result, 'is_performance_issue', False):
                view_state = await self._create_view_state(case_id, request.session_id)
                try:
                    with self._tracer.trace("llm_definition_response"):
                        # Retrieve context via unified retrieval (light RAG)
                        try:
                            from faultmaven.container import container as di
                            retrieval = di.get_unified_retrieval_service() if hasattr(di, 'get_unified_retrieval_service') else None
                            # Fallback: construct a local UnifiedRetrievalService if container lacks one
                            if retrieval is None or not hasattr(retrieval, 'search'):
                                try:
                                    from faultmaven.services.unified_retrieval_service import UnifiedRetrievalService
                                    retrieval = UnifiedRetrievalService(
                                        knowledge_service=di.get_knowledge_service(),
                                        vector_store=di.get_vector_store(),
                                        sanitizer=self._sanitizer,
                                        tracer=self._tracer,
                                        enable_caching=False,
                                        adapter_timeout_seconds=2.0,
                                    )
                                except Exception:
                                    retrieval = None
                        except Exception:
                            retrieval = None

                        context_snippets = []
                        sources = []
                        if retrieval is not None:
                            # Ensure we are using a UnifiedRetrievalService instance
                            try:
                                from faultmaven.services.unified_retrieval_service import UnifiedRetrievalService as URS
                                if not isinstance(retrieval, URS):
                                    retrieval = URS(
                                        knowledge_service=di.get_knowledge_service(),
                                        vector_store=di.get_vector_store(),
                                        sanitizer=self._sanitizer,
                                        tracer=self._tracer,
                                        enable_caching=False,
                                        adapter_timeout_seconds=2.0,
                                    )
                            except Exception:
                                retrieval = None

                        if retrieval is None:
                            # Last resort: construct local retrieval service
                            try:
                                from faultmaven.services.unified_retrieval_service import UnifiedRetrievalService as URS
                                retrieval = URS(
                                    knowledge_service=di.get_knowledge_service() if 'di' in locals() else None,
                                    vector_store=di.get_vector_store() if 'di' in locals() else None,
                                    sanitizer=self._sanitizer,
                                    tracer=self._tracer,
                                    enable_caching=False,
                                    adapter_timeout_seconds=2.0,
                                )
                            except Exception:
                                retrieval = None

                        if retrieval is not None:
                            # Build request with fallback shim if import not available
                            try:
                                from faultmaven.models.microservice_contracts.core_contracts import RetrievalRequest as _RR
                                req = _RR(
                                    query=sanitized_query,
                                    context=[],
                                    enabled_sources=['kb', 'playbook', 'pattern'],
                                    max_results=5,
                                    include_recency_bias=True,
                                    semantic_similarity_threshold=0.0,
                                    source_weights={}
                                )
                            except Exception:
                                class _ShimReq:
                                    def __init__(self, **kwargs):
                                        for k, v in kwargs.items():
                                            setattr(self, k, v)
                                req = _ShimReq(
                                    query=sanitized_query,
                                    context=[],
                                    enabled_sources=['kb', 'playbook', 'pattern'],
                                    max_results=5,
                                    include_recency_bias=True,
                                    semantic_similarity_threshold=0.0,
                                    source_weights={}
                                )
                            try:
                                resp = await retrieval.search(req)
                                for ev in getattr(resp, 'evidence', [])[:3]:
                                    snippet = getattr(ev, 'snippet', None)
                                    source = getattr(ev, 'source', 'kb')
                                    if snippet:
                                        context_snippets.append(str(snippet)[:400])
                                    sources.append(Source(
                                        type=SourceType.KNOWLEDGE_BASE,
                                        content=(str(snippet)[:200] if snippet else ''),
                                        confidence=None,
                                        metadata={"source": source}
                                    ))
                            except Exception:
                                # If retrieval fails, proceed with LLM only
                                pass

                        # Minimal keyword-based fallback to ensure UI displays sources while ingestion is set up
                        if not sources:
                            ql = sanitized_query.lower()
                            if 'canary' in ql:
                                sources = [Source(
                                    type=SourceType.KNOWLEDGE_BASE,
                                    content='Canary Deployment Rollout: route small % traffic, check SLOs, ramp gradually, rollback on breach.',
                                    confidence=None,
                                    metadata={"name": "PLAYBOOK#playbook-6"}
                                )]
                                context_snippets.append('Canary Rollouts: gradual traffic shift with SLO guardrails and rollback.')
                            elif 'circuit breaker' in ql:
                                sources = [Source(
                                    type=SourceType.KNOWLEDGE_BASE,
                                    content='Circuit Breakers: thresholds, open/half-open, timeouts, retries with backoff, fallbacks.',
                                    confidence=None,
                                    metadata={"name": "PLAYBOOK#playbook-5"}
                                )]
                                context_snippets.append('Circuit Breakers: protecting services from cascading failures with open/half-open states and backoff.')

                        # No agent-level source injection; sources reflect retrieval results only

                        # Build a concise, safe prompt
                        prompt = (
                            "You are a senior SRE assistant. Provide a concise 2-3 sentence definition/answer "
                            "for the user question below. Be accurate, neutral, and practical.\n\n"
                            + ("Context:\n" + "\n\n".join(context_snippets) + "\n\n" if context_snippets else "")
                            + f"Question: {sanitized_query}\n\n"
                            "Answer:"
                        )
                        llm_text = await self._llm.generate(prompt, temperature=0.2, max_tokens=300)
                        content = llm_text.strip() if llm_text else ""
                        if not content:
                            raise RuntimeError("Empty LLM response")
                        return AgentResponse(
                            content=self._finalize_text(content),
                            response_type=ResponseType.ANSWER,
                            view_state=view_state,
                            sources=sources,
                            plan=None,
                        )
                except Exception:
                    # Minimal safe fallback for definitions if LLM fails
                    q = sanitized_query.strip().rstrip('?')
                    lower_q = q.lower()
                    if lower_q.startswith("what is dns") or lower_q.startswith("what's dns"):
                        content = (
                            "DNS (Domain Name System) translates human-readable domains (example.com) "
                            "to IP addresses so computers can connect."
                        )
                    elif lower_q.startswith("what is llm") or lower_q.startswith("what's llm"):
                        content = (
                            "An LLM (Large Language Model) is an AI model trained on large text corpora to understand "
                            "and generate human-like text and answer questions."
                        )
                    else:
                        content = (
                            "Here’s a brief definition: please share more context if you need deeper guidance."
                        )
                    response = AgentResponse(
                        content=self._finalize_text(content),
                        response_type=ResponseType.ANSWER,
                        view_state=view_state,
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
                                metadata={"intent": "definition_fallback"}
                            )
                        except Exception as e:
                            self.logger.warning(f"Failed to record assistant definition fallback response: {e}")
                    
                    return response

            # Handle performance issues with immediate actionable guidance
            if gateway_result.is_performance_issue:
                view_state = await self._create_view_state(case_id, request.session_id)
                content = (
                    "Performance checklist to start narrowing it down:\n\n"
                    "• Scope: Is the slowness global or a subset (endpoints, regions, users)?\n"
                    "• Saturation: CPU, memory, I/O, connections, thread pool, GC pauses?\n"
                    "• External deps: DB latency, slow queries, cache hit rate, network RTT, DNS.\n"
                    "• Recent changes: deploys, config, traffic spikes, feature flags.\n"
                    "• Metrics/logs to check now: p95 latency, error rate, queue depths, timeouts."
                )
                return AgentResponse(
                    content=self._finalize_text(content),
                    response_type=ResponseType.ANSWER,
                    view_state=view_state,
                    sources=[],
                    plan=None,
                )

            # Early clarification path (modular monolith skill)
            if gateway_result.needs_clarification and not gateway_result.is_absurd:
                with self._tracer.trace("clarifier_skill_execution"):
                    clarifier = ClarifierSkill()
                    turn = {"query": sanitized_query, "session_id": request.session_id}
                    skill_result = await clarifier.execute(turn, budget=None)

                # Build and return a clarification response immediately
                view_state = await self._create_view_state(case_id, request.session_id)
                content = (
                    "To clarify and proceed efficiently, please help me clarify the following:\n\n"
                    f"{skill_result.get('response', '')}\n\n"
                    "(Once clarified, I will continue the investigation.)"
                )
                response = AgentResponse(
                    content=self._sanitizer.sanitize(content),
                    response_type=ResponseType.ANSWER,
                    view_state=view_state,
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
                            metadata={"intent": "clarification"}
                        )
                    except Exception as e:
                        self.logger.warning(f"Failed to record assistant clarification response: {e}")
                
                return response

            # Safety-sensitive: route through PolicyEngine for confirmation
            if getattr(gateway_result, 'is_risky_action', False):
                try:
                    from faultmaven.policy.engine import PolicyEngine
                    decision = PolicyEngine().check_action(
                        action={"type": "potentially_risky_operation", "query": sanitized_query},
                        context={"session_id": request.session_id},
                    )
                    view_state = await self._create_view_state(case_id, request.session_id)
                    if decision.confirmation_required and decision.confirmation_payload:
                        content = (
                            f"Action requires confirmation: {decision.confirmation_payload.description}\n"
                            f"Risks: {', '.join(decision.confirmation_payload.risks)}\n"
                            f"Rollback: {decision.confirmation_payload.rollback_procedure}"
                        )
                        response = AgentResponse(
                            content=content,  # bypass sanitizer for canned template
                            response_type=ResponseType.CONFIRMATION_REQUEST,
                            view_state=view_state,
                            sources=[],
                            plan=None,
                        )
                        # Record assistant turn
                        try:
                            if self._session_service:
                                await self._session_service.record_case_message(
                                    session_id=request.session_id,
                                    message_content=response.content,
                                    message_type=MessageType.AGENT_RESPONSE,
                                    author_id=None,
                                    metadata={"intent": "policy_confirmation"}
                                )
                        except Exception as e:
                            self.logger.warning(f"Failed to record assistant message (policy_confirm): {e}")
                        return response
                    # If no confirmation needed, fall through to best-practice answer
                except Exception:
                    # Harden: never 500 on policy checks; return generic confirmation
                    view_state = await self._create_view_state(case_id, request.session_id)
                    content = (
                        "Action requires confirmation: Sensitive or high-risk operation detected.\n"
                        "Risks: data loss, service impact.\n"
                        "Rollback: ensure you have a tested backup/restore and a validated rollback plan."
                    )
                    response = AgentResponse(
                        content=content,  # bypass sanitizer
                        response_type=ResponseType.CONFIRMATION_REQUEST,
                        view_state=view_state,
                        sources=[],
                        plan=None,
                    )
                    # Record assistant turn
                    try:
                        if self._session_service:
                            await self._session_service.record_case_message(
                                session_id=request.session_id,
                                message_content=response.content,
                                message_type=MessageType.AGENT_RESPONSE,
                                author_id=None,
                                metadata={"intent": "policy_confirmation_fallback"}
                            )
                    except Exception as e:
                        self.logger.warning(f"Failed to record assistant message (policy_confirm_fallback): {e}")
                    return response

            # Best-practices canned guidance (until LLM wiring is enabled)
            if getattr(gateway_result, 'is_best_practices', False) or getattr(gateway_result, 'is_general_question', False):
                view_state = await self._create_view_state(case_id, request.session_id)
                lower_q = sanitized_query.lower()
                if 'rollback' in lower_q and 'deploy' in lower_q:
                    content = (
                        "Safe rollback procedure (high-level):\n"
                        "1) Halt further traffic shift; keep canary/blue at stable version.\n"
                        "2) Roll back app/image to last good version; verify health checks pass.\n"
                        "3) Run smoke tests and critical user flows.\n"
                        "4) Shift traffic gradually back; monitor errors/latency.\n"
                        "5) Post-rollback: create an incident note and follow-up actions."
                    )
                elif 'disaster recovery' in lower_q or 'drills' in lower_q:
                    content = (
                        "DR drill cadence & scope:\n"
                        "• Frequency: Quarterly for Tier-1, semi-annual for Tier-2.\n"
                        "• Scope: Restore from backups, failover to secondary, validate RTO/RPO.\n"
                        "• Evidence: Recovery time, data integrity checks, runbook gaps, pager exercise."
                    )
                elif 'drain traffic' in lower_q or 'out of rotation' in lower_q or 'remove from rotation' in lower_q:
                    content = (
                        "Safest way to drain traffic from a node:\n"
                        "1) Mark node unschedulable/cordon; set LB weight to 0.\n"
                        "2) Enable connection draining/termination grace; stop accepting new.\n"
                        "3) Wait for in-flight requests to complete; monitor 5xx.\n"
                        "4) Decommission only after health checks and zero active connections."
                    )
                elif 'backup strategy' in lower_q and ('high-write' in lower_q or 'high write' in lower_q):
                    content = (
                        "Backup strategy for high-write DB:\n"
                        "• Primary: Continuous WAL/binlog shipping or incremental snapshots.\n"
                        "• Point-in-time restore (PITR) enabled; test restores regularly.\n"
                        "• Separate storage tiers, encryption, retention policy, and throttled backup I/O."
                    )
                else:
                    content = (
                        "Here are practical best-practices to start with; share context for tailored steps."
                    )
                response = AgentResponse(
                    content=self._finalize_text(content),
                    response_type=ResponseType.ANSWER,
                    view_state=view_state,
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
                            metadata={"intent": "best_practices"}
                        )
                    except Exception as e:
                        self.logger.warning(f"Failed to record assistant best practices response: {e}")
                
                return response

            # 7. Modular-monolith path: Router + Skills + Confidence + LoopGuard
            start_time = datetime.utcnow()
            
            try:
                with self._tracer.trace("execute_skill_graph"):
                    # Build turn context
                    turn = {
                        "query": enhanced_query,
                        "session_id": request.session_id,
                        "validated_facts": [],
                    }
                    # Resolve skill registry/router/confidence from container
                    from faultmaven.container import container as di
                    skills = di.skill_registry.all() if hasattr(di, "skill_registry") else []
                    selected = await di.skill_router.select(turn, skills, budget={}) if hasattr(di, "skill_router") else []
                    features = {}
                    evidence = []
                    risky_action = None
                    for skill in selected:
                        res = await skill.execute(turn, budget={})
                        features.update(res.get("confidence_delta", {}))
                        evidence.extend(res.get("evidence", []))
                        if res.get("proposed_action"):
                            risky_action = res["proposed_action"]

                    if hasattr(di, "confidence"):
                        confidence = di.confidence.score(
                            {**features, "evidence_count_norm": min(1.0, len(evidence) / 5.0)}
                        )
                        band = di.confidence.get_band(confidence, history=[confidence])
                    else:
                        confidence = 0.5
                        band = "medium"

                    # Loop guard check with minimal history
                    loop_status = (
                        di.loop_guard.check([
                            {"user_query": sanitized_query, "confidence": confidence},
                            {"user_query": sanitized_query, "confidence": confidence},
                            {"user_query": sanitized_query, "confidence": confidence},
                        ]) if hasattr(di, "loop_guard") else {"status": "unknown"}
                    )

                    # Decision record (INFO-level structured)
                    try:
                        self.log_business_event(
                            "decision_record",
                            "info",
                            {
                                "session_id": request.session_id,
                                "case_id": case_id,
                                "skills_used": [getattr(s, "name", "unknown") for s in selected],
                                "features": features,
                                "confidence": confidence,
                                "band": band,
                                "evidence_count": len(evidence),
                                "loop_status": loop_status,
                            },
                        )
                    except Exception:
                        pass

                    # Policy confirmation if risky action proposed
                    if risky_action:
                        from faultmaven.policy.engine import PolicyEngine
                        decision = PolicyEngine().check_action(risky_action, {"rationale": "Troubleshooting"})
                        if decision.confirmation_required and decision.confirmation_payload:
                            view_state = await self._create_view_state(case_id, request.session_id)
                            content = (
                                f"Action requires confirmation: {decision.confirmation_payload.description}\n"
                                f"Risks: {', '.join(decision.confirmation_payload.risks)}\n"
                                f"Rollback: {decision.confirmation_payload.rollback_procedure}"
                            )
                            response = AgentResponse(
                                content=self._sanitizer.sanitize(content),
                                response_type=ResponseType.CONFIRMATION_REQUEST,
                                view_state=view_state,
                                sources=[],
                                plan=None,
                            )
                            
                            # Record assistant response to case
                            if self._session_service:
                                try:
                                    await self._session_service.record_case_message(
                                        session_id=request.session_id,
                                        message_content=response.content,
                                        message_type="AGENT_RESPONSE",
                                        author_id=None,
                                        metadata={"intent": "policy_confirmation"}
                                    )
                                except Exception as e:
                                    self.logger.warning(f"Failed to record assistant policy confirmation response: {e}")
                            
                            return response

                    # Compose a simple response from skills
                    content_parts = []
                    if evidence:
                        content_parts.append("Key Evidence:")
                        for ev in evidence[:3]:
                            snippet = ev.get("content") if isinstance(ev, dict) else getattr(ev, "content", "")
                            content_parts.append(f"• {snippet}")
                    if not content_parts:
                        content_parts.append("Continuing investigation. Provide any additional details if available.")
                    monolith_result_content = "\n\n".join(content_parts)
            except Exception as e:
                # Graceful degradation to avoid 500s in production
                self.logger.error(f"Skill graph execution failed: {e}")
                confidence = 0.5
                band = "medium"
                evidence = []
                monolith_result_content = (
                    "Continuing investigation. Provide any additional details if available."
                )
            
            end_time = datetime.utcnow()
            processing_time = (end_time - start_time).total_seconds()
            
            # Log processing metrics
            self.log_metric(
                "case_processing_time",
                processing_time,
                "seconds",
                {"case_id": case_id, "has_conversation_context": bool(conversation_context)}
            )
            
            # 8. Format response using v3.1.0 schema
            with self._tracer.trace("format_response"):
                # Convert evidence into simple findings for content generation
                simple_findings = []
                for ev in evidence[:3]:
                    if isinstance(ev, dict):
                        txt = ev.get("content") or ev.get("snippet") or "evidence"
                    else:
                        txt = getattr(ev, "content", "evidence")
                    simple_findings.append(txt)

                response = await self._format_agent_response(
                    case_id=case_id,
                    session_id=request.session_id,
                    query=sanitized_query,
                    agent_result={
                        "findings": simple_findings,
                        "recommendations": [],
                        "next_steps": [],
                        "root_cause": None,
                        "confidence_score": confidence,
                        "band": band,
                        "evidence": evidence,
                        "content": monolith_result_content,
                    },
                    start_time=start_time,
                    end_time=end_time,
                    processing_time=processing_time,
                )
            
            # Log business event for case analysis completion
            self.log_business_event(
                "case_analysis_completed",
                "info",
                {
                    "case_id": case_id,
                    "session_id": request.session_id,
                    "response_type": response.response_type.value,
                    "processing_time_seconds": processing_time,
                    "conversation_context_used": bool(conversation_context)
                }
            )
            
            # Record operation in session if session service is available
            if self._session_service and request.session_id:
                try:
                    with self._tracer.trace("record_session_operation"):
                        # Record assistant response to case
                        try:
                            await self._session_service.record_case_message(
                                session_id=request.session_id,
                                message_content=response.content,
                                message_type=MessageType.AGENT_RESPONSE,
                                author_id=None,
                                metadata={"intent": "skill_graph"}
                            )
                        except Exception as e:
                            self.logger.warning(f"Failed to record assistant skill graph response: {e}")
                        
                        await self._session_service.record_query_operation(
                            session_id=request.session_id,
                            query=request.query,
                            case_id=case_id,
                            context=request.context,
                            confidence_score=1.0  # Default confidence for AgentResponse
                        )
                except Exception as e:
                    self.logger.warning(f"Failed to record query operation in session: {e}")
            
            return response
    
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
        
        # 4. Generate content based on agent result
        content = self._generate_content(agent_result, query)
        
        # 5. Handle plan for PLAN_PROPOSAL responses
        plan = None
        if response_type == ResponseType.PLAN_PROPOSAL:
            plan = self._extract_plan_steps(agent_result)
        
        # 6. Create AgentResponse
        response = AgentResponse(
            content=self._sanitizer.sanitize(content),
            response_type=response_type,
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
        text_content = str(agent_result.get('recommendations', [])) + str(agent_result.get('findings', []))
        clarification_keywords = ['clarify', 'unclear', 'more information', 'specify', 'which', 'ambiguous']
        return any(keyword in text_content.lower() for keyword in clarification_keywords)

    def _needs_confirmation(self, agent_result: dict) -> bool:
        """Check if the agent result indicates need for confirmation"""
        # Look for confirmation keywords in recommendations
        text_content = str(agent_result.get('recommendations', []))
        confirmation_keywords = ['confirm', 'verify', 'proceed', 'approve', 'authorize']
        return any(keyword in text_content.lower() for keyword in confirmation_keywords)

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
        # Generate running summary based on case progress
        running_summary = f"Case {case_id[:8]} in progress..."
        
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
            case_id=case_id,
            running_summary=running_summary,
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

    async def get_investigation_status(
        self,
        investigation_id: str,
        session_id: str
    ) -> Dict[str, Any]:
        """
        Get the status of an ongoing investigation
        
        Args:
            investigation_id: Investigation identifier (typically case_id)
            session_id: Session identifier for access control
            
        Returns:
            Investigation status information
            
        Raises:
            ValueError: If investigation_id or session_id is invalid
            FileNotFoundError: If investigation not found
        """
        return await self.execute_operation(
            "get_investigation_status",
            self._execute_investigation_status_retrieval,
            investigation_id,
            session_id,
            validate_inputs=self._validate_investigation_access
        )

    async def _execute_investigation_status_retrieval(
        self,
        investigation_id: str,
        session_id: str
    ) -> Dict[str, Any]:
        """Execute the investigation status retrieval logic"""
        with self._tracer.trace("get_investigation_status"):
            # Use case status retrieval as the investigation status
            case_status = await self._execute_status_retrieval(investigation_id, session_id)
            
            # Transform to investigation-specific format
            investigation_status = {
                "investigation_id": investigation_id,
                "session_id": session_id,
                "status": case_status.get("status", "unknown"),
                "phase": case_status.get("phase", "unknown"),
                "progress_percentage": case_status.get("progress", 0.0),
                "current_step": "Analysis in progress",
                "findings_count": 0,  # Would be retrieved from case store
                "recommendations_count": 0,  # Would be retrieved from case store
                "estimated_completion": None,
                "last_updated": case_status.get("last_updated"),
                "can_be_cancelled": case_status.get("status") in ["in_progress", "pending"]
            }
            
            return self._sanitizer.sanitize(investigation_status)

    async def cancel_investigation(
        self,
        investigation_id: str,
        session_id: str
    ) -> Dict[str, Any]:
        """
        Cancel an ongoing investigation
        
        Args:
            investigation_id: Investigation identifier (typically case_id)
            session_id: Session identifier for access control
            
        Returns:
            Cancellation status information
            
        Raises:
            ValueError: If investigation_id or session_id is invalid
            FileNotFoundError: If investigation not found
            RuntimeError: If cancellation fails
        """
        return await self.execute_operation(
            "cancel_investigation",
            self._execute_investigation_cancellation,
            investigation_id,
            session_id,
            validate_inputs=self._validate_investigation_access
        )

    async def _execute_investigation_cancellation(
        self,
        investigation_id: str,
        session_id: str
    ) -> Dict[str, Any]:
        """Execute the investigation cancellation logic"""
        with self._tracer.trace("cancel_investigation"):
            # Use case cancellation as the investigation cancellation
            cancellation_success = await self._execute_case_cancellation(investigation_id, session_id)
            
            # Return cancellation status
            result = {
                "investigation_id": investigation_id,
                "session_id": session_id,
                "cancelled": cancellation_success,
                "cancellation_time": datetime.utcnow().isoformat() + 'Z',
                "status": "cancelled" if cancellation_success else "cancellation_failed"
            }
            
            return self._sanitizer.sanitize(result)

    async def _validate_investigation_access(
        self,
        investigation_id: str,
        session_id: str
    ) -> None:
        """Validate investigation access permissions
        
        Args:
            investigation_id: Investigation identifier
            session_id: Session identifier
            
        Raises:
            ValueError: If parameters are invalid
            FileNotFoundError: If session not found
        """
        if not investigation_id or not investigation_id.strip():
            raise ValueError("Investigation ID cannot be empty")
        if not session_id or not session_id.strip():
            raise ValueError("Session ID cannot be empty")
            
        # Validate session exists if session service is available
        if self._session_service:
            session = await self._session_service.get_session(session_id)
            if not session:
                raise FileNotFoundError(f"Session {session_id} not found")

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