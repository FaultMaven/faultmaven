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

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer
from faultmaven.models import QueryRequest, TroubleshootingResponse


class AgentService:
    """Agent Service using interface dependencies via dependency injection"""

    def __init__(
        self,
        llm_provider: ILLMProvider,
        tools: List[BaseTool],
        tracer: ITracer,
        sanitizer: ISanitizer,
        session_service: Optional[Any] = None,
        logger: Optional[logging.Logger] = None
    ):
        """Initialize with interface dependencies via dependency injection
        
        Args:
            llm_provider: Interface for LLM operations
            tools: List of tool interfaces for agent execution
            tracer: Interface for distributed tracing
            sanitizer: Interface for data sanitization
            session_service: Optional session service for session validation
            logger: Optional logger instance
        """
        self._llm = llm_provider
        self._tools = tools
        self._tracer = tracer
        self._sanitizer = sanitizer
        self._session_service = session_service
        self._logger = logger or logging.getLogger(__name__)

    async def process_query(
        self,
        request: QueryRequest
    ) -> TroubleshootingResponse:
        """
        Main business logic for query processing using interface dependencies
        
        Args:
            request: QueryRequest with query, session_id, context, etc.
            
        Returns:
            TroubleshootingResponse with investigation results
            
        Raises:
            ValueError: If request validation fails
            RuntimeError: If agent processing fails
        """
        with self._tracer.trace("agent_service_process_query"):
            # 1. Validate and sanitize input
            await self._validate_request(request)
            
            sanitized_query = self._sanitizer.sanitize(request.query)
            
            # 2. Generate investigation ID
            investigation_id = str(uuid.uuid4())
            self._logger.debug(f"Created investigation {investigation_id}")
            
            try:
                # 3. Create and configure agent with interfaces
                from faultmaven.core.agent.agent import FaultMavenAgent
                agent = FaultMavenAgent(llm_interface=self._llm)
                
                # 4. Execute troubleshooting using interfaces
                start_time = datetime.utcnow()
                
                result = await agent.run(
                    query=sanitized_query,
                    session_id=request.session_id,
                    tools=self._tools,  # Pass interface-based tools
                    context=request.context or {}
                )
                
                end_time = datetime.utcnow()
                processing_time = (end_time - start_time).total_seconds()
                
                # 5. Format response using interfaces
                response = self._format_response(
                    investigation_id=investigation_id,
                    session_id=request.session_id,
                    agent_result=result,
                    start_time=start_time,
                    end_time=end_time,
                    processing_time=processing_time
                )
                
                self._logger.info(
                    f"Successfully processed investigation {investigation_id} "
                    f"with confidence {response.confidence_score}"
                )
                
                return response
                
            except Exception as e:
                self._logger.error(
                    f"Agent processing failed for investigation {investigation_id}: {e}"
                )
                raise RuntimeError(f"Agent processing failed: {str(e)}") from e

    async def _validate_request(self, request: QueryRequest) -> None:
        """Validate request using interface methods
        
        Args:
            request: QueryRequest to validate
            
        Raises:
            ValueError: If validation fails
            FileNotFoundError: If session not found
        """
        if not request.query or not request.query.strip():
            raise ValueError("Query cannot be empty")
            
        if not request.session_id or not request.session_id.strip():
            raise ValueError("Session ID cannot be empty")
            
        # Validate session exists if session service is available
        if self._session_service:
            session = await self._session_service.get_session(request.session_id)
            if not session:
                raise FileNotFoundError(f"Session {request.session_id} not found")
                
        # Additional validation can be added here

    def _format_response(
        self,
        investigation_id: str,
        session_id: str,
        agent_result: dict,
        start_time: datetime,
        end_time: datetime,
        processing_time: float
    ) -> TroubleshootingResponse:
        """Format response using interface sanitization
        
        Args:
            investigation_id: Unique investigation identifier
            session_id: Session identifier
            agent_result: Raw result from agent execution
            start_time: Investigation start time
            end_time: Investigation completion time
            processing_time: Processing time in seconds
            
        Returns:
            Formatted TroubleshootingResponse
        """
        # Process findings from agent result
        findings = []
        raw_findings = agent_result.get('findings', [])
        
        if isinstance(raw_findings, list):
            for finding_data in raw_findings:
                if isinstance(finding_data, dict):
                    # Create properly formatted finding
                    finding_dict = {
                        'type': finding_data.get('type', 'observation'),
                        'message': finding_data.get('message', finding_data.get('description', 'No description available')),
                        'severity': finding_data.get('severity', 'medium'),
                        'timestamp': datetime.utcnow().isoformat(),
                        'source': finding_data.get('source', 'agent_analysis'),
                        'confidence': finding_data.get('confidence', 0.5)
                    }
                    findings.append(finding_dict)
                else:
                    # Handle non-dict findings
                    findings.append({
                        'type': 'general',
                        'message': str(finding_data),
                        'severity': 'info',
                        'timestamp': datetime.utcnow().isoformat(),
                        'source': 'agent_analysis',
                        'confidence': 0.5
                    })
        
        # Extract other response components
        recommendations = agent_result.get('recommendations', [])
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)] if recommendations else []
            
        next_steps = agent_result.get('next_steps', [])
        if not isinstance(next_steps, list):
            next_steps = [str(next_steps)] if next_steps else []
        
        # Create response object
        response = TroubleshootingResponse(
            investigation_id=investigation_id,
            session_id=session_id,
            findings=self._sanitizer.sanitize(findings),  # Sanitize output
            root_cause=agent_result.get('root_cause'),
            recommendations=self._sanitizer.sanitize(recommendations),
            confidence_score=float(agent_result.get('confidence', 0.0)),
            status="completed",
            estimated_mttr=agent_result.get('estimated_mttr'),
            next_steps=self._sanitizer.sanitize(next_steps),
            created_at=start_time,
            completed_at=end_time
        )
        
        return response

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
        with self._tracer.trace("agent_service_analyze_findings"):
            self._logger.debug(f"Analyzing {len(findings)} findings for session {session_id}")
            
            try:
                # Sanitize input findings
                sanitized_findings = self._sanitizer.sanitize(findings)
                
                # Group findings by type
                findings_by_type = self._group_findings_by_type(sanitized_findings)
                
                # Identify patterns
                patterns = await self._identify_patterns(findings_by_type)
                
                # Calculate severity distribution
                severity_dist = self._calculate_severity_distribution(sanitized_findings)
                
                # Generate insights
                insights = {
                    "total_findings": len(sanitized_findings),
                    "findings_by_type": {
                        type_name: len(items) for type_name, items in findings_by_type.items()
                    },
                    "severity_distribution": severity_dist,
                    "patterns_identified": patterns,
                    "critical_issues": self._extract_critical_issues(sanitized_findings),
                    "session_id": session_id,
                    "analysis_timestamp": datetime.utcnow().isoformat()
                }
                
                # Sanitize output
                return self._sanitizer.sanitize(insights)
                
            except Exception as e:
                self._logger.error(f"Failed to analyze findings: {e}")
                raise RuntimeError(f"Analysis failed: {str(e)}") from e

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

    async def get_investigation_status(
        self, 
        investigation_id: str, 
        session_id: str
    ) -> Dict[str, Any]:
        """
        Get the status of a specific investigation using interface dependencies
        
        Args:
            investigation_id: Investigation identifier
            session_id: Session identifier
            
        Returns:
            Investigation status information
        """
        with self._tracer.trace("agent_service_get_investigation_status"):
            try:
                # In a full implementation, this would query persistent storage
                # via an ISessionStore interface to retrieve investigation status
                status = {
                    "investigation_id": investigation_id,
                    "session_id": session_id,
                    "status": "completed",  # Placeholder
                    "progress": 100.0,
                    "phase": "completed",
                    "last_updated": datetime.utcnow().isoformat()
                }
                
                return self._sanitizer.sanitize(status)
                
            except Exception as e:
                self._logger.error(f"Failed to get investigation status: {e}")
                raise RuntimeError(f"Status retrieval failed: {str(e)}") from e

    async def cancel_investigation(
        self, 
        investigation_id: str, 
        session_id: str
    ) -> bool:
        """
        Cancel an ongoing investigation using interface dependencies
        
        Args:
            investigation_id: Investigation identifier
            session_id: Session identifier
            
        Returns:
            True if cancellation was successful
        """
        with self._tracer.trace("agent_service_cancel_investigation"):
            try:
                # In a full implementation, this would use ISessionStore
                # to update investigation status and potentially signal
                # the running agent to stop
                self._logger.info(
                    f"Cancelled investigation {investigation_id} for session {session_id}"
                )
                return True
                
            except Exception as e:
                self._logger.error(f"Failed to cancel investigation: {e}")
                return False

    async def get_investigation_results(
        self, 
        investigation_id: str, 
        session_id: str
    ) -> TroubleshootingResponse:
        """
        Get investigation results by ID with proper validation
        
        Args:
            investigation_id: Investigation identifier
            session_id: Session identifier for access control
            
        Returns:
            TroubleshootingResponse with investigation results
            
        Raises:
            ValueError: If investigation_id or session_id is invalid
            FileNotFoundError: If investigation not found
            RuntimeError: If retrieval fails
        """
        with self._tracer.trace("agent_service_get_investigation_results"):
            self._logger.debug(f"Retrieving investigation {investigation_id} for session {session_id}")
            
            # Validate inputs
            if not investigation_id or not investigation_id.strip():
                raise ValueError("Investigation ID cannot be empty")
            if not session_id or not session_id.strip():
                raise ValueError("Session ID cannot be empty")
            
            try:
                # In a full implementation, this would query persistent storage
                # via IInvestigationStore interface to retrieve results
                # For now, return placeholder response
                
                from datetime import datetime
                placeholder_response = TroubleshootingResponse(
                    investigation_id=investigation_id,
                    session_id=session_id,
                    status="completed",
                    findings=[
                        {
                            "type": "info",
                            "message": f"Investigation {investigation_id} completed",
                            "severity": "info",
                            "timestamp": datetime.utcnow().isoformat(),
                            "source": "investigation_store"
                        }
                    ],
                    root_cause="Investigation completed successfully",
                    recommendations=["Review investigation findings", "Take appropriate action"],
                    confidence_score=0.8,
                    estimated_mttr="15 minutes",
                    next_steps=["Monitor system", "Verify fix effectiveness"],
                    created_at=datetime.utcnow(),
                    completed_at=datetime.utcnow()
                )
                
                return placeholder_response
                
            except Exception as e:
                self._logger.error(f"Failed to retrieve investigation results: {e}")
                if "not found" in str(e).lower():
                    raise FileNotFoundError(f"Investigation {investigation_id} not found")
                raise RuntimeError(f"Investigation retrieval failed: {str(e)}") from e

    async def list_session_investigations(
        self, 
        session_id: str, 
        limit: int = 10, 
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        List investigations for a session with pagination
        
        Args:
            session_id: Session identifier
            limit: Maximum number of results
            offset: Pagination offset
            
        Returns:
            List of investigation summary dictionaries
            
        Raises:
            ValueError: If session_id is invalid or pagination params are invalid
            FileNotFoundError: If session not found
            RuntimeError: If listing fails
        """
        with self._tracer.trace("agent_service_list_session_investigations"):
            self._logger.debug(f"Listing investigations for session {session_id} (limit={limit}, offset={offset})")
            
            # Validate inputs
            if not session_id or not session_id.strip():
                raise ValueError("Session ID cannot be empty")
            if limit <= 0 or limit > 100:
                raise ValueError("Limit must be between 1 and 100")
            if offset < 0:
                raise ValueError("Offset must be non-negative")
            
            try:
                # In a full implementation, this would query persistent storage
                # via ISessionStore interface to list investigations
                # For now, return placeholder data
                
                from datetime import datetime, timedelta
                
                # Generate some placeholder investigations
                base_time = datetime.utcnow()
                investigations = []
                
                for i in range(min(limit, 3)):  # Return up to 3 placeholder investigations
                    investigations.append({
                        "investigation_id": f"inv_{session_id}_{i + offset + 1}",
                        "query": f"Sample troubleshooting query {i + offset + 1}",
                        "status": "completed",
                        "priority": "medium",
                        "findings_count": 2 + i,
                        "recommendations_count": 1 + i,
                        "confidence_score": 0.7 + (i * 0.1),
                        "created_at": (base_time - timedelta(hours=i + 1)).isoformat(),
                        "completed_at": (base_time - timedelta(hours=i)).isoformat(),
                        "estimated_mttr": f"{15 + (i * 5)} minutes"
                    })
                
                return self._sanitizer.sanitize(investigations)
                
            except Exception as e:
                self._logger.error(f"Failed to list session investigations: {e}")
                if "session not found" in str(e).lower():
                    raise FileNotFoundError(f"Session {session_id} not found")
                raise RuntimeError(f"Investigation listing failed: {str(e)}") from e

    async def health_check(self) -> Dict[str, Any]:
        """
        Check health of agent service and all dependencies
        
        Returns:
            Dictionary with health status and component details
        """
        with self._tracer.trace("agent_service_health_check"):
            try:
                health_info = {
                    "service": "agent_service_refactored",
                    "status": "healthy",
                    "timestamp": datetime.utcnow().isoformat(),
                    "components": {
                        "llm_provider": "unknown",
                        "sanitizer": "unknown", 
                        "tracer": "unknown",
                        "tools": "unknown"
                    }
                }
                
                # Check LLM provider
                try:
                    if self._llm and hasattr(self._llm, 'generate_response'):
                        health_info["components"]["llm_provider"] = "healthy"
                    else:
                        health_info["components"]["llm_provider"] = "unavailable"
                except Exception:
                    health_info["components"]["llm_provider"] = "unhealthy"
                
                # Check sanitizer
                try:
                    if self._sanitizer and hasattr(self._sanitizer, 'sanitize'):
                        # Test sanitization
                        test_result = self._sanitizer.sanitize("test")
                        health_info["components"]["sanitizer"] = "healthy"
                    else:
                        health_info["components"]["sanitizer"] = "unavailable"
                except Exception:
                    health_info["components"]["sanitizer"] = "unhealthy"
                
                # Check tracer
                try:
                    if self._tracer and hasattr(self._tracer, 'trace'):
                        health_info["components"]["tracer"] = "healthy"
                    else:
                        health_info["components"]["tracer"] = "unavailable"
                except Exception:
                    health_info["components"]["tracer"] = "unhealthy"
                
                # Check tools
                try:
                    if self._tools and len(self._tools) > 0:
                        health_info["components"]["tools"] = f"healthy ({len(self._tools)} tools available)"
                    else:
                        health_info["components"]["tools"] = "no tools available"
                except Exception:
                    health_info["components"]["tools"] = "unhealthy"
                
                # Determine overall status
                unhealthy_components = [
                    comp for status in health_info["components"].values() 
                    for comp in [status] if "unhealthy" in str(status)
                ]
                
                if unhealthy_components:
                    health_info["status"] = "degraded"
                elif any("unavailable" in str(status) for status in health_info["components"].values()):
                    health_info["status"] = "degraded"
                
                return health_info
                
            except Exception as e:
                self._logger.error(f"Health check failed: {e}")
                return {
                    "service": "agent_service_refactored",
                    "status": "unhealthy",
                    "error": str(e),
                    "timestamp": datetime.utcnow().isoformat()
                }