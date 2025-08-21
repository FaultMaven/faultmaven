"""Enhanced Agent Service with Memory and Planning Integration

This module provides an enhanced Agent Service that integrates with the Memory
and Planning systems to deliver intelligent, context-aware troubleshooting
assistance with strategic planning and learning capabilities.

The Enhanced Agent Service provides:
- Memory-aware conversation context and personalization
- Strategic planning for complex troubleshooting scenarios
- Intelligent response type determination with context
- Cross-session learning and pattern recognition
- Advanced error handling with memory integration
"""

import uuid
import time
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from faultmaven.services.base_service import BaseService
from faultmaven.models.interfaces import (
    ILLMProvider, BaseTool, ITracer, ISanitizer, IMemoryService, IPlanningService,
    ConversationContext, UserProfile, StrategicPlan
)
from faultmaven.models import (
    QueryRequest, AgentResponse, ViewState, UploadedData, Source, SourceType, 
    ResponseType, PlanStep
)
from faultmaven.exceptions import ValidationException, AgentException


class EnhancedAgentService(BaseService):
    """Enhanced Agent Service with Memory and Planning Integration
    
    This service extends the basic agent functionality with intelligent
    memory management, strategic planning, and context-aware responses.
    It provides a more sophisticated troubleshooting experience through:
    
    Key Enhancements:
    - Memory-aware context retrieval and personalization
    - Strategic planning for complex problem scenarios  
    - Intelligent response type determination
    - Cross-session learning and pattern recognition
    - Adaptive conversation flow based on user profile
    
    Performance Targets:
    - Context retrieval: < 50ms
    - Planning integration: < 200ms  
    - Total response time: < 2000ms
    - Memory consolidation: async, non-blocking
    
    Integration Architecture:
    - Memory Service: Context retrieval and insight consolidation
    - Planning Service: Strategic plan development and adaptation
    - Agent Core: Traditional troubleshooting logic execution
    - LLM Provider: Enhanced prompting with context and planning
    """
    
    def __init__(
        self,
        llm_provider: ILLMProvider,
        tools: List[BaseTool],
        tracer: ITracer,
        sanitizer: ISanitizer,
        memory_service: Optional[IMemoryService] = None,
        planning_service: Optional[IPlanningService] = None,
        session_service: Optional[Any] = None
    ):
        """Initialize Enhanced Agent Service with intelligent capabilities
        
        Args:
            llm_provider: Interface for LLM operations with enhanced prompting
            tools: List of tool interfaces for agent execution
            tracer: Interface for distributed tracing
            sanitizer: Interface for data sanitization and privacy
            memory_service: Optional memory service for context and learning
            planning_service: Optional planning service for strategic assistance
            session_service: Optional session service for session validation
        """
        super().__init__()
        
        # Core dependencies (required)
        self._llm = llm_provider
        self._tools = tools
        self._tracer = tracer
        self._sanitizer = sanitizer
        
        # Enhanced capabilities (optional but recommended)
        self._memory = memory_service
        self._planning = planning_service
        self._session_service = session_service
        
        # Performance and operational metrics
        self._performance_metrics = {
            "enhanced_queries_processed": 0,
            "memory_context_retrievals": 0,
            "strategic_plans_created": 0,
            "avg_response_time": 0.0,
            "memory_consolidations": 0,
            "plan_adaptations": 0,
            "personalization_applied": 0
        }
        
        self._logger = logging.getLogger(__name__)
    
    async def process_query(self, request: QueryRequest) -> AgentResponse:
        """Enhanced query processing with memory and planning integration
        
        This method provides the main interface for enhanced troubleshooting
        assistance, integrating memory context, strategic planning, and
        intelligent response generation for superior user experience.
        
        Args:
            request: QueryRequest with query, session_id, context, etc.
            
        Returns:
            AgentResponse with enhanced investigation results, strategic
            guidance, and personalized recommendations
            
        Raises:
            ValidationException: If request validation fails
            AgentException: If enhanced processing fails
        """
        return await self.execute_operation(
            "process_enhanced_query",
            self._execute_enhanced_query_processing,
            request,
            validate_inputs=self._validate_enhanced_request
        )
    
    async def _execute_enhanced_query_processing(self, request: QueryRequest) -> AgentResponse:
        """Execute enhanced query processing with memory and planning"""
        
        processing_start = time.time()
        
        # Get or create case_id for this conversation thread
        if self._session_service:
            case_id = await self._session_service.get_or_create_current_case_id(request.session_id)
        else:
            # Fallback if no session service available
            case_id = str(uuid.uuid4())
        
        try:
            with self._tracer.trace("enhanced_query_workflow"):
                # Phase 1: Input sanitization and validation
                with self._tracer.trace("sanitize_and_validate"):
                    sanitized_query = self._sanitizer.sanitize(request.query)
                    enhanced_context = await self._prepare_enhanced_context(request)
                
                # Phase 2: Memory context retrieval and user profiling
                memory_context = None
                user_profile = None
                if self._memory:
                    with self._tracer.trace("memory_context_retrieval"):
                        memory_context = await self._retrieve_memory_context(
                            request.session_id, sanitized_query
                        )
                        user_profile = memory_context.user_profile
                        self._performance_metrics["memory_context_retrievals"] += 1
                
                # Phase 3: Strategic planning for complex scenarios
                strategic_plan = None
                if self._planning and self._should_create_strategic_plan(sanitized_query, enhanced_context):
                    with self._tracer.trace("strategic_planning"):
                        strategic_plan = await self._create_strategic_plan(
                            sanitized_query, enhanced_context, memory_context
                        )
                        self._performance_metrics["strategic_plans_created"] += 1
                
                # Phase 4: Retrieve conversation history for current case
                conversation_context = ""
                if self._session_service:
                    with self._tracer.trace("retrieve_conversation_history"):
                        try:
                            conversation_context = await self._session_service.format_conversation_context(
                                request.session_id, case_id, limit=5
                            )
                            if conversation_context:
                                self.logger.debug(f"Retrieved conversation context for case {case_id}")
                        except Exception as e:
                            self.logger.warning(f"Failed to retrieve conversation context: {e}")
                
                # Phase 5: Enhanced prompt construction with context, planning, and conversation history
                with self._tracer.trace("construct_enhanced_prompt"):
                    enhanced_prompt = await self._construct_enhanced_prompt(
                        sanitized_query, enhanced_context, memory_context, strategic_plan, user_profile, conversation_context
                    )
                
                # Phase 6: Agent execution with enhanced context
                with self._tracer.trace("execute_enhanced_agent"):
                    agent_result = await self._execute_agent_with_context(
                        enhanced_prompt, sanitized_query, enhanced_context, strategic_plan
                    )
                
                # Phase 7: Intelligent response type determination
                with self._tracer.trace("determine_response_type"):
                    response_type = await self._determine_intelligent_response_type(
                        agent_result, enhanced_context, strategic_plan, user_profile
                    )
                
                # Phase 8: Enhanced response formatting with personalization
                with self._tracer.trace("format_enhanced_response"):
                    enhanced_response = await self._format_enhanced_response(
                        case_id, request.session_id, sanitized_query, agent_result,
                        response_type, memory_context, strategic_plan, user_profile
                    )
                
                # Phase 9: Async memory consolidation (non-blocking)
                if self._memory:
                    consolidation_task = self._start_memory_consolidation(
                        request.session_id, agent_result, strategic_plan, user_profile
                    )
                    # Don't await - let it run asynchronously
                
                # Phase 10: Performance tracking and business logging
                processing_time = (time.time() - processing_start) * 1000
                await self._track_enhanced_performance(
                    case_id, request.session_id, processing_time, response_type,
                    memory_context is not None, strategic_plan is not None, conversation_context
                )
                
                self._performance_metrics["enhanced_queries_processed"] += 1
                return enhanced_response
                
        except Exception as e:
            self._logger.error(f"Enhanced query processing failed for session {request.session_id}: {e}")
            raise AgentException(f"Enhanced processing failed: {str(e)}")
    
    async def _prepare_enhanced_context(self, request: QueryRequest) -> Dict[str, Any]:
        """Prepare enhanced context with additional metadata and analysis"""
        
        enhanced_context = request.context.copy() if request.context else {}
        
        # Add query analysis
        enhanced_context.update({
            "original_query": request.query,
            "session_id": request.session_id,
            "processing_timestamp": datetime.utcnow().isoformat() + 'Z',
            "query_analysis": {
                "length": len(request.query),
                "complexity_indicators": self._analyze_query_complexity(request.query),
                "domain_indicators": self._analyze_domain_indicators(request.query),
                "urgency_indicators": self._analyze_urgency_indicators(request.query)
            }
        })
        
        # Add default enhanced fields if not present
        if "user_intent" not in enhanced_context:
            enhanced_context["user_intent"] = self._infer_user_intent(request.query)
        
        if "context_richness" not in enhanced_context:
            enhanced_context["context_richness"] = "enhanced" if len(enhanced_context) > 5 else "basic"
        
        return enhanced_context
    
    async def _retrieve_memory_context(self, session_id: str, query: str) -> Optional[ConversationContext]:
        """Retrieve comprehensive memory context for enhanced responses"""
        try:
            if not self._memory:
                return None
            
            context = await self._memory.retrieve_context(session_id, query)
            
            self.log_business_event(
                "memory_context_retrieved",
                "info",
                {
                    "session_id": session_id,
                    "context_items": len(context.conversation_history),
                    "insights_count": len(context.relevant_insights),
                    "has_user_profile": context.user_profile is not None,
                    "has_domain_context": context.domain_context is not None
                }
            )
            
            return context
            
        except Exception as e:
            self._logger.warning(f"Memory context retrieval failed: {e}")
            return None
    
    async def _create_strategic_plan(
        self, 
        query: str, 
        context: Dict[str, Any], 
        memory_context: Optional[ConversationContext]
    ) -> Optional[StrategicPlan]:
        """Create strategic plan for complex troubleshooting scenarios"""
        try:
            if not self._planning:
                return None
            
            # Enhance planning context with memory insights
            planning_context = context.copy()
            if memory_context:
                planning_context.update({
                    "user_profile": memory_context.user_profile,
                    "conversation_history": memory_context.conversation_history[:5],  # Recent history
                    "domain_context": memory_context.domain_context,
                    "relevant_insights": memory_context.relevant_insights[:3]  # Top insights
                })
            
            strategic_plan = await self._planning.plan_response_strategy(query, planning_context)
            
            self.log_business_event(
                "strategic_plan_created",
                "info",
                {
                    "plan_id": strategic_plan.plan_id,
                    "confidence_score": strategic_plan.confidence_score,
                    "estimated_effort": strategic_plan.estimated_effort,
                    "approach": strategic_plan.solution_strategy.get("approach", "unknown"),
                    "risk_level": strategic_plan.risk_assessment.get("overall_risk_level", "unknown")
                }
            )
            
            return strategic_plan
            
        except Exception as e:
            self._logger.warning(f"Strategic planning failed: {e}")
            return None
    
    def _should_create_strategic_plan(self, query: str, context: Dict[str, Any]) -> bool:
        """Determine if strategic planning is needed for this query"""
        
        # Check for complexity indicators
        complexity_indicators = context.get("query_analysis", {}).get("complexity_indicators", [])
        if any(indicator in complexity_indicators for indicator in ["multiple_components", "distributed_system", "complex_issue"]):
            return True
        
        # Check for urgency that requires planning
        urgency = context.get("urgency", "medium")
        if urgency in ["high", "critical"]:
            return True
        
        # Check for specific planning keywords
        planning_keywords = ["strategy", "approach", "plan", "complex", "multiple", "systematic"]
        if any(keyword in query.lower() for keyword in planning_keywords):
            return True
        
        # Check query length as complexity indicator
        if len(query) > 200:
            return True
        
        return False
    
    async def _construct_enhanced_prompt(
        self,
        query: str,
        context: Dict[str, Any],
        memory_context: Optional[ConversationContext],
        strategic_plan: Optional[StrategicPlan],
        user_profile: Optional[Dict[str, Any]],
        conversation_context: Optional[str] = None
    ) -> str:
        """Construct enhanced prompt with memory, planning, and personalization"""
        
        prompt_parts = []
        
        # Add conversation history first if available
        if conversation_context:
            prompt_parts.append(conversation_context)
        
        # Base prompt with query (this will appear after conversation history)
        prompt_parts.append(f"User Query: {query}")
        
        # Add memory context if available
        if memory_context and memory_context.relevant_insights:
            insights = memory_context.relevant_insights[:3]  # Top 3 insights
            insight_text = "\n".join([f"- {insight.get('description', str(insight))}" for insight in insights])
            prompt_parts.append(f"Relevant Insights from Previous Interactions:\n{insight_text}")
        
        # Add user profile for personalization
        if user_profile:
            skill_level = user_profile.get("skill_level", "intermediate")
            communication_style = user_profile.get("preferred_communication_style", "balanced")
            expertise_domains = user_profile.get("domain_expertise", [])
            
            personalization = f"User Profile: {skill_level} skill level, {communication_style} communication preference"
            if expertise_domains:
                personalization += f", expertise in: {', '.join(expertise_domains[:3])}"
            prompt_parts.append(personalization)
        
        # Add strategic plan guidance if available
        if strategic_plan:
            plan_guidance = f"Strategic Approach: {strategic_plan.solution_strategy.get('approach', 'systematic')}"
            methodology = strategic_plan.solution_strategy.get('methodology', [])
            if methodology:
                plan_guidance += f"\nKey Steps: {'; '.join(methodology[:3])}"
            
            risk_level = strategic_plan.risk_assessment.get('overall_risk_level', 'medium')
            plan_guidance += f"\nRisk Level: {risk_level}"
            
            prompt_parts.append(plan_guidance)
        
        # Add context information
        domain_indicators = context.get("query_analysis", {}).get("domain_indicators", [])
        if domain_indicators:
            prompt_parts.append(f"Detected Domains: {', '.join(domain_indicators)}")
        
        urgency = context.get("urgency", "medium")
        environment = context.get("environment", "unknown")
        prompt_parts.append(f"Context: {urgency} urgency, {environment} environment")
        
        # Construct final enhanced prompt
        enhanced_prompt = "\n\n".join(prompt_parts)
        
        # Add instruction for context-aware response
        instruction = """
Provide a comprehensive troubleshooting response that:
1. Takes into account the user's skill level and communication preferences
2. Leverages relevant insights from previous interactions
3. Follows the strategic approach if provided
4. Addresses the specific urgency and environment context
5. Provides actionable guidance appropriate for the user's expertise level
"""
        
        return f"{enhanced_prompt}\n\n{instruction}"
    
    async def _execute_agent_with_context(
        self,
        enhanced_prompt: str,
        original_query: str,
        context: Dict[str, Any],
        strategic_plan: Optional[StrategicPlan]
    ) -> Dict[str, Any]:
        """Execute agent with enhanced context and strategic guidance"""
        
        # Initialize agent with enhanced context
        from faultmaven.core.agent.agent import FaultMavenAgent
        agent = FaultMavenAgent(llm_interface=self._llm)
        
        # Prepare agent context
        agent_context = context.copy()
        if strategic_plan:
            agent_context["strategic_plan"] = {
                "approach": strategic_plan.solution_strategy.get("approach"),
                "methodology": strategic_plan.solution_strategy.get("methodology", []),
                "risk_level": strategic_plan.risk_assessment.get("overall_risk_level"),
                "estimated_effort": strategic_plan.estimated_effort
            }
        
        # Execute agent with enhanced prompt and context
        result = await agent.run(
            query=enhanced_prompt,
            session_id=context.get("session_id", ""),
            tools=self._tools,
            context=agent_context
        )
        
        return result or {}
    
    async def _determine_intelligent_response_type(
        self,
        agent_result: Dict[str, Any],
        context: Dict[str, Any],
        strategic_plan: Optional[StrategicPlan],
        user_profile: Optional[Dict[str, Any]]
    ) -> ResponseType:
        """Intelligently determine response type based on comprehensive analysis"""
        
        # Check for plan proposal indicators
        if strategic_plan and len(strategic_plan.solution_strategy.get("methodology", [])) > 3:
            return ResponseType.PLAN_PROPOSAL
        
        # Check for clarification needs based on user profile
        if user_profile and user_profile.get("skill_level") == "beginner":
            # More likely to need clarification for complex issues
            complexity_indicators = context.get("query_analysis", {}).get("complexity_indicators", [])
            if len(complexity_indicators) > 2:
                return ResponseType.CLARIFICATION_REQUEST
        
        # Check for confirmation needs based on risk level
        if strategic_plan:
            risk_level = strategic_plan.risk_assessment.get("overall_risk_level", "medium")
            if risk_level in ["high", "critical"]:
                return ResponseType.CONFIRMATION_REQUEST
        
        # Check for traditional indicators
        if self._needs_clarification(agent_result):
            return ResponseType.CLARIFICATION_REQUEST
        
        if self._needs_confirmation(agent_result):
            return ResponseType.CONFIRMATION_REQUEST
        
        if self._has_plan(agent_result):
            return ResponseType.PLAN_PROPOSAL
        
        # Default to answer
        return ResponseType.ANSWER
    
    async def _format_enhanced_response(
        self,
        case_id: str,
        session_id: str,
        query: str,
        agent_result: Dict[str, Any],
        response_type: ResponseType,
        memory_context: Optional[ConversationContext],
        strategic_plan: Optional[StrategicPlan],
        user_profile: Optional[Dict[str, Any]]
    ) -> AgentResponse:
        """Format enhanced response with memory, planning, and personalization"""
        
        # Generate enhanced content with personalization
        content = await self._generate_enhanced_content(
            agent_result, query, strategic_plan, user_profile
        )
        
        # Extract sources with enhanced context
        sources = await self._extract_enhanced_sources(agent_result, memory_context)
        
        # Create enhanced view state
        view_state = await self._create_enhanced_view_state(
            case_id, session_id, memory_context, strategic_plan
        )
        
        # Handle plan for PLAN_PROPOSAL responses
        plan = None
        if response_type == ResponseType.PLAN_PROPOSAL:
            plan = self._extract_enhanced_plan_steps(agent_result, strategic_plan)
        
        # Create enhanced AgentResponse
        response = AgentResponse(
            content=self._sanitizer.sanitize(content),
            response_type=response_type,
            view_state=view_state,
            sources=sources,
            plan=plan
        )
        
        return response
    
    async def _generate_enhanced_content(
        self,
        agent_result: Dict[str, Any],
        query: str,
        strategic_plan: Optional[StrategicPlan],
        user_profile: Optional[Dict[str, Any]]
    ) -> str:
        """Generate enhanced content with personalization and strategic context"""
        
        # Check if agent is in error state first
        current_phase = agent_result.get('current_phase')
        if current_phase == 'error':
            # Be transparent about errors instead of faking responses
            error_info = agent_result.get('case_context', {})
            error_message = error_info.get('error', 'Unknown error occurred')
            
            # Personalize error message based on user profile
            if user_profile and user_profile.get("skill_level") == "beginner":
                intro = "I'm sorry, but I'm having trouble processing your request right now."
            else:
                intro = "I'm unable to process your query at the moment due to a technical issue."
            
            return (
                f"{intro}\n\n"
                f"Error details: {error_message}\n\n"
                f"This might be due to:\n"
                f"• LLM service connectivity issues\n"
                f"• System configuration problems\n"
                f"• Temporary service outage\n\n"
                f"Please try again in a moment, or contact support if the issue persists."
            )
        
        content_parts = []
        
        # Add strategic context if available
        if strategic_plan:
            approach = strategic_plan.solution_strategy.get("approach", "").replace("_", " ").title()
            content_parts.append(f"Strategic Approach: {approach}")
            
            effort = strategic_plan.estimated_effort
            content_parts.append(f"Estimated Effort: {effort}")
        
        # Add personalized introduction based on user profile
        if user_profile:
            skill_level = user_profile.get("skill_level", "intermediate")
            if skill_level == "beginner":
                content_parts.append("Let me guide you through this step-by-step:")
            elif skill_level == "advanced":
                content_parts.append("Here's a comprehensive analysis and approach:")
            else:
                content_parts.append("Here's what I found and recommend:")
        
        # Add traditional content
        root_cause = agent_result.get('root_cause')
        if root_cause:
            content_parts.append(f"Root Cause: {root_cause}")
        
        # Add findings with enhanced formatting
        findings = agent_result.get('findings', [])
        if findings:
            content_parts.append("Key Findings:")
            for finding in findings[:3]:
                if isinstance(finding, dict):
                    message = finding.get('message', finding.get('description', 'Finding discovered'))
                    content_parts.append(f"• {message}")
                elif isinstance(finding, str):
                    content_parts.append(f"• {finding}")
                else:
                    # Convert non-dict, non-string to a meaningful message
                    content_parts.append(f"• Analysis finding identified")
        
        # Add recommendations with personalization
        recommendations = agent_result.get('recommendations', [])
        if recommendations:
            if user_profile and user_profile.get("skill_level") == "beginner":
                content_parts.append("Recommended Steps (with detailed guidance):")
            else:
                content_parts.append("Recommendations:")
            
            for rec in recommendations[:3]:
                if isinstance(rec, str):
                    content_parts.append(f"• {rec}")
                elif isinstance(rec, dict):
                    # Extract meaningful text from dict recommendation
                    rec_text = rec.get('text', rec.get('description', rec.get('action', 'Review system configuration')))
                    content_parts.append(f"• {rec_text}")
                else:
                    # Convert non-dict, non-string to a meaningful recommendation
                    content_parts.append(f"• Follow standard troubleshooting procedures")
        
        # Add strategic guidance if available
        if strategic_plan and strategic_plan.risk_assessment.get("overall_risk_level") in ["high", "critical"]:
            content_parts.append("⚠️ Important: This operation has elevated risk. Please review the plan carefully before proceeding.")
        
        # If no content found but not in error state, provide helpful guidance
        if not content_parts:
            if user_profile and user_profile.get("skill_level") == "beginner":
                content_parts = [
                    f"I'm having difficulty providing specific help for your query: '{query}'.",
                    "This could be because:",
                    "• Your question needs more details",
                    "• The system is temporarily unable to analyze this type of request",
                    "",
                    "To get better help, try including:",
                    "• What exactly isn't working",
                    "• Any error messages you see",
                    "• What you were trying to do when the problem happened"
                ]
            else:
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
    
    async def _extract_enhanced_sources(
        self, 
        agent_result: Dict[str, Any], 
        memory_context: Optional[ConversationContext]
    ) -> List[Source]:
        """Extract sources with enhanced memory context"""
        
        sources = []
        
        # Add traditional sources from agent result
        kb_results = agent_result.get('knowledge_base_results', [])
        for kb_result in kb_results:
            if isinstance(kb_result, dict):
                text = kb_result.get('content') or kb_result.get('snippet') or ''
                sources.append(Source(
                    type=SourceType.KNOWLEDGE_BASE,
                    content=(str(text)[:200] + ("..." if len(str(text)) > 200 else "")),
                    confidence=None,
                    metadata={"title": kb_result.get('title', 'Knowledge Base Document')}
                ))
        
        # Add memory-based sources
        if memory_context and memory_context.relevant_insights:
            for insight in memory_context.relevant_insights[:2]:  # Top 2 memory insights
                if isinstance(insight, dict):
                    sources.append(Source(
                        type=SourceType.KNOWLEDGE_BASE,
                        content=(insight.get('description', str(insight))[:200] + "..."),
                        confidence=None,
                        metadata={"source": "Previous Interaction Insight"}
                    ))
        
        return sources[:10]  # Limit to 10 sources
    
    async def _create_enhanced_view_state(
        self,
        case_id: str,
        session_id: str,
        memory_context: Optional[ConversationContext],
        strategic_plan: Optional[StrategicPlan]
    ) -> ViewState:
        """Create enhanced view state with memory and planning context"""
        
        # Generate enhanced running summary
        summary_parts = [f"Investigation {case_id[:8]}"]
        
        if strategic_plan:
            approach = strategic_plan.solution_strategy.get("approach", "").replace("_", " ")
            summary_parts.append(f"using {approach} approach")
        
        if memory_context and memory_context.relevant_insights:
            summary_parts.append(f"with {len(memory_context.relevant_insights)} relevant insights")
        
        running_summary = " ".join(summary_parts)
        
        # Get uploaded data from session
        uploaded_data = []
        if self._session_service:
            try:
                session = await self._session_service.get_session(session_id)
                if session and hasattr(session, 'data_uploads'):
                    for data_id in session.data_uploads:
                        uploaded_data.append(UploadedData(
                            id=data_id,
                            name=f"data_{data_id}",
                            type="enhanced_context"
                        ))
            except Exception as e:
                self._logger.warning(f"Failed to get session data uploads: {e}")
        
        return ViewState(
            session_id=session_id,
            case_id=case_id,
            running_summary=running_summary,
            uploaded_data=uploaded_data
        )
    
    def _extract_enhanced_plan_steps(
        self, 
        agent_result: Dict[str, Any], 
        strategic_plan: Optional[StrategicPlan]
    ) -> List[PlanStep]:
        """Extract enhanced plan steps with strategic planning integration"""
        
        steps = []
        
        # Use strategic plan methodology if available
        if strategic_plan:
            methodology = strategic_plan.solution_strategy.get("methodology", [])
            for step in methodology:
                steps.append(PlanStep(description=step))
        
        # Fall back to agent result next_steps
        if not steps:
            next_steps = agent_result.get('next_steps', [])
            for step in next_steps:
                if isinstance(step, str):
                    steps.append(PlanStep(description=step))
                elif isinstance(step, dict):
                    description = step.get('description', step.get('step', str(step)))
                    steps.append(PlanStep(description=description))
        
        return steps
    
    def _start_memory_consolidation(
        self,
        session_id: str,
        agent_result: Dict[str, Any],
        strategic_plan: Optional[StrategicPlan],
        user_profile: Optional[Dict[str, Any]]
    ):
        """Start async memory consolidation (non-blocking)"""
        
        if not self._memory:
            return None
        
        # Prepare consolidation data
        consolidation_data = agent_result.copy()
        
        if strategic_plan:
            consolidation_data["strategic_plan"] = {
                "plan_id": strategic_plan.plan_id,
                "confidence": strategic_plan.confidence_score,
                "approach": strategic_plan.solution_strategy.get("approach"),
                "estimated_effort": strategic_plan.estimated_effort
            }
        
        if user_profile:
            consolidation_data["user_interaction"] = {
                "skill_level": user_profile.get("skill_level"),
                "communication_style": user_profile.get("preferred_communication_style"),
                "domains": user_profile.get("domain_expertise", [])
            }
        
        # Start async consolidation
        import asyncio
        task = asyncio.create_task(
            self._memory.consolidate_insights(session_id, consolidation_data)
        )
        
        self._performance_metrics["memory_consolidations"] += 1
        return task
    
    async def _track_enhanced_performance(
        self,
        case_id: str,
        session_id: str,
        processing_time: float,
        response_type: ResponseType,
        memory_enabled: bool,
        planning_enabled: bool,
        conversation_context: Optional[str] = None
    ):
        """Track enhanced performance metrics and business events"""
        
        # Update performance metrics
        current_avg = self._performance_metrics["avg_response_time"]
        total_queries = self._performance_metrics["enhanced_queries_processed"]
        
        if total_queries == 0:
            self._performance_metrics["avg_response_time"] = processing_time
        else:
            self._performance_metrics["avg_response_time"] = (
                (current_avg * total_queries + processing_time) / (total_queries + 1)
            )
        
        # Log enhanced performance metrics
        self.log_metric(
            "enhanced_response_time",
            processing_time,
            "milliseconds",
            {
                "case_id": case_id,
                "response_type": response_type.value,
                "memory_enabled": memory_enabled,
                "planning_enabled": planning_enabled,
                "conversation_context_enabled": bool(conversation_context)
            }
        )
        
        # Log business event
        self.log_business_event(
            "enhanced_query_completed",
            "info",
            {
                "case_id": case_id,
                "session_id": session_id,
                "processing_time_ms": processing_time,
                "response_type": response_type.value,
                "memory_enabled": memory_enabled,
                "planning_enabled": planning_enabled,
                "conversation_context_enabled": bool(conversation_context),
                "performance_target_met": processing_time < 2000  # 2 second target
            }
        )
        
        # Performance warning if targets exceeded
        if processing_time > 2000:  # 2 second target
            self.logger.warning(
                f"Enhanced query processing exceeded target time: {processing_time:.2f}ms "
                f"for session {session_id}"
            )
    
    # Legacy compatibility methods from original AgentService
    def _needs_clarification(self, agent_result: Dict[str, Any]) -> bool:
        """Check if the agent result indicates need for clarification"""
        text_content = str(agent_result.get('recommendations', [])) + str(agent_result.get('findings', []))
        clarification_keywords = ['clarify', 'unclear', 'more information', 'specify', 'which', 'ambiguous']
        return any(keyword in text_content.lower() for keyword in clarification_keywords)

    def _needs_confirmation(self, agent_result: Dict[str, Any]) -> bool:
        """Check if the agent result indicates need for confirmation"""
        text_content = str(agent_result.get('recommendations', []))
        confirmation_keywords = ['confirm', 'verify', 'proceed', 'approve', 'authorize']
        return any(keyword in text_content.lower() for keyword in confirmation_keywords)

    def _has_plan(self, agent_result: Dict[str, Any]) -> bool:
        """Check if the agent result contains a multi-step plan"""
        next_steps = agent_result.get('next_steps', [])
        return isinstance(next_steps, list) and len(next_steps) > 2
    
    def _analyze_query_complexity(self, query: str) -> List[str]:
        """Analyze query for complexity indicators"""
        indicators = []
        query_lower = query.lower()
        
        if len(query) > 200:
            indicators.append("long_description")
        
        if any(term in query_lower for term in ["multiple", "several", "many"]):
            indicators.append("multiple_components")
        
        if any(term in query_lower for term in ["complex", "complicated", "difficult"]):
            indicators.append("complex_issue")
        
        if any(term in query_lower for term in ["distributed", "microservice", "cluster"]):
            indicators.append("distributed_system")
        
        return indicators
    
    def _analyze_domain_indicators(self, query: str) -> List[str]:
        """Analyze query for domain indicators"""
        indicators = []
        query_lower = query.lower()
        
        domains = {
            "database": ["database", "sql", "query", "table"],
            "network": ["network", "connectivity", "dns", "firewall"],
            "application": ["application", "app", "service", "api"],
            "performance": ["slow", "performance", "latency", "timeout"]
        }
        
        for domain, keywords in domains.items():
            if any(keyword in query_lower for keyword in keywords):
                indicators.append(domain)
        
        return indicators
    
    def _analyze_urgency_indicators(self, query: str) -> List[str]:
        """Analyze query for urgency indicators"""
        indicators = []
        query_lower = query.lower()
        
        if any(term in query_lower for term in ["urgent", "emergency", "critical", "asap"]):
            indicators.append("high_urgency")
        
        if any(term in query_lower for term in ["production", "live", "customer", "user"]):
            indicators.append("business_impact")
        
        if any(term in query_lower for term in ["down", "outage", "failure", "broken"]):
            indicators.append("service_disruption")
        
        return indicators
    
    def _infer_user_intent(self, query: str) -> str:
        """Infer user intent from query"""
        query_lower = query.lower()
        
        if any(term in query_lower for term in ["how to", "how do", "how can"]):
            return "how_to_guidance"
        elif any(term in query_lower for term in ["why", "what causes", "what is"]):
            return "explanation_seeking"
        elif any(term in query_lower for term in ["fix", "solve", "resolve", "troubleshoot"]):
            return "problem_resolution"
        elif any(term in query_lower for term in ["help", "assist", "support"]):
            return "assistance_request"
        else:
            return "general_inquiry"
    
    async def _validate_enhanced_request(self, request: QueryRequest) -> None:
        """Validate enhanced request with additional checks"""
        if not request.query or not request.query.strip():
            raise ValidationException("Query cannot be empty")
            
        if not request.session_id or not request.session_id.strip():
            raise ValidationException("Session ID cannot be empty")
        
        if len(request.query) > 10000:
            raise ValidationException("Query too long (max 10000 characters)")
            
        # Validate session exists if session service is available
        if self._session_service:
            session = await self._session_service.get_session(request.session_id)
            if not session:
                raise ValidationException(f"Session {request.session_id} not found")
    
    async def health_check(self) -> Dict[str, Any]:
        """Enhanced health check including memory and planning services"""
        
        # Get base health
        base_health = await super().health_check()
        
        # Check core components
        components = {
            "llm_provider": "unknown",
            "sanitizer": "unknown", 
            "tracer": "unknown",
            "tools": "unknown",
            "memory_service": "unknown",
            "planning_service": "unknown"
        }
        
        # Check core dependencies
        try:
            if self._llm and hasattr(self._llm, 'generate_response'):
                components["llm_provider"] = "healthy"
            else:
                components["llm_provider"] = "unavailable"
        except Exception:
            components["llm_provider"] = "unhealthy"
        
        try:
            if self._sanitizer and hasattr(self._sanitizer, 'sanitize'):
                components["sanitizer"] = "healthy"
            else:
                components["sanitizer"] = "unavailable"
        except Exception:
            components["sanitizer"] = "unhealthy"
        
        try:
            if self._tracer and hasattr(self._tracer, 'trace'):
                components["tracer"] = "healthy"
            else:
                components["tracer"] = "unavailable"
        except Exception:
            components["tracer"] = "unhealthy"
        
        try:
            if self._tools and len(self._tools) > 0:
                components["tools"] = f"healthy ({len(self._tools)} tools available)"
            else:
                components["tools"] = "no tools available"
        except Exception:
            components["tools"] = "unhealthy"
        
        # Check enhanced dependencies
        if self._memory:
            try:
                memory_health = await self._memory.health_check()
                components["memory_service"] = memory_health.get("status", "unknown")
            except Exception:
                components["memory_service"] = "unhealthy"
        else:
            components["memory_service"] = "unavailable"
        
        if self._planning:
            try:
                planning_health = await self._planning.health_check()
                components["planning_service"] = planning_health.get("status", "unknown")
            except Exception:
                components["planning_service"] = "unhealthy"
        else:
            components["planning_service"] = "unavailable"
        
        # Determine overall status
        unhealthy_components = [
            comp for status in components.values() 
            for comp in [status] if "unhealthy" in str(status)
        ]
        
        if unhealthy_components:
            status = "degraded"
        elif any("unavailable" in str(status) for status in components.values() if "memory" in str(status) or "planning" in str(status)):
            status = "degraded"  # Enhanced services unavailable
        else:
            status = "healthy"
        
        # Combine health information
        health_info = {
            **base_health,
            "service": "enhanced_agent_service",
            "status": status,
            "components": components,
            "performance_metrics": self._performance_metrics.copy(),
            "capabilities": {
                "basic_troubleshooting": True,
                "memory_integration": self._memory is not None,
                "strategic_planning": self._planning is not None,
                "context_awareness": True,
                "personalization": self._memory is not None,
                "enhanced_prompting": True
            }
        }
        
        return health_info