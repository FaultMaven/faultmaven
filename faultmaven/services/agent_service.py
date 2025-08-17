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
from faultmaven.models import QueryRequest, TroubleshootingResponse, AgentResponse, ViewState, UploadedData, Source, SourceType, ResponseType, PlanStep
from faultmaven.exceptions import ValidationException


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
            # 1. Sanitize input
            with self._tracer.trace("sanitize_input"):
                sanitized_query = self._sanitizer.sanitize(request.query)
            
            # 2. Get or create case_id for this conversation thread
            if self._session_service:
                case_id = await self._session_service.get_or_create_current_case_id(request.session_id)
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
            
            # 3. Create and configure agent with interfaces
            with self._tracer.trace("initialize_agent"):
                from faultmaven.core.agent.agent import FaultMavenAgent
                agent = FaultMavenAgent(llm_interface=self._llm)
            
            # 4. Retrieve conversation history for context
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
            
            # 5. Enhanced query with conversation context
            enhanced_query = sanitized_query
            if conversation_context:
                enhanced_query = f"{conversation_context}\n{sanitized_query}"
            
            # 6. Execute troubleshooting using interfaces
            start_time = datetime.utcnow()
            
            with self._tracer.trace("execute_agent_workflow"):
                agent_context = (request.context or {}).copy()
                agent_context["has_conversation_history"] = bool(conversation_context)
                
                result = await agent.run(
                    query=enhanced_query,
                    session_id=request.session_id,
                    tools=self._tools,  # Pass interface-based tools
                    context=agent_context
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
            
            # 7. Format response using v3.1.0 schema
            with self._tracer.trace("format_response"):
                response = await self._format_agent_response(
                    case_id=case_id,
                    session_id=request.session_id,
                    query=sanitized_query,
                    agent_result=result,
                    start_time=start_time,
                    end_time=end_time,
                    processing_time=processing_time
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
            
        # Validate session exists if session service is available
        if self._session_service:
            session = await self._session_service.get_session(request.session_id)
            if not session:
                raise FileNotFoundError(f"Session {request.session_id} not found")
                
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
                sources.append(Source(
                    type=SourceType.KNOWLEDGE_BASE,
                    name=kb_result.get('title', 'Knowledge Base Document'),
                    snippet=kb_result.get('snippet', kb_result.get('content', ''))[:200] + "..."
                ))
        
        # Extract from tool results if available
        tool_results = agent_result.get('tool_results', [])
        for tool_result in tool_results:
            if isinstance(tool_result, dict):
                tool_name = tool_result.get('tool_name', 'unknown')
                if 'web_search' in tool_name.lower():
                    sources.append(Source(
                        type=SourceType.WEB_SEARCH,
                        name=tool_result.get('source', 'Web Search'),
                        snippet=tool_result.get('content', '')[:200] + "..."
                    ))
                elif 'log' in tool_name.lower():
                    sources.append(Source(
                        type=SourceType.LOG_FILE,
                        name=tool_result.get('filename', 'Log File'),
                        snippet=tool_result.get('content', '')[:200] + "..."
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