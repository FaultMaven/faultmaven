# File: faultmaven/api/middleware/intelligent_protection.py

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional
from datetime import datetime, timezone
from faultmaven.utils.serialization import to_json_compatible

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from faultmaven.infrastructure.protection.protection_coordinator import (
    ProtectionCoordinator, ProtectionConfig
)
from faultmaven.models.behavioral import RiskLevel, ProtectionDecision
from faultmaven.models.interfaces import ISessionStore


class IntelligentProtectionMiddleware(BaseHTTPMiddleware):
    """
    Intelligent Protection Middleware
    
    Integrates advanced protection mechanisms into the FastAPI request pipeline:
    - Behavioral analysis
    - ML anomaly detection  
    - Reputation-based access control
    - Smart circuit breakers
    - Adaptive protection decisions
    """

    def __init__(
        self,
        app: ASGIApp,
        config: Optional[ProtectionConfig] = None,
        session_store: Optional[ISessionStore] = None,
        enabled: bool = True
    ):
        super().__init__(app)
        self.enabled = enabled
        self.logger = logging.getLogger(__name__)
        
        if not self.enabled:
            self.logger.info("Intelligent Protection Middleware disabled")
            return
        
        # Initialize intelligent protection coordinator
        self.coordinator = ProtectionCoordinator(config, session_store)
        self.initialization_task: Optional[asyncio.Task] = None
        self.initialized = False
        
        # Performance tracking
        self.request_count = 0
        self.protection_decisions = 0
        self.average_processing_time = 0.0
        self._processing_times = []
        
        # Configuration
        self.max_processing_time = 100.0  # milliseconds
        self.skip_paths = {"/health", "/docs", "/redoc", "/openapi.json"}
        
        self.logger.info("Phase 2 Protection Middleware initialized")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Main middleware processing"""
        if not self.enabled:
            return await call_next(request)
        
        start_time = time.time()
        
        try:
            # Initialize coordinator if needed
            if not self.initialized:
                await self._ensure_initialized()
            
            # Skip protection for certain paths
            if self._should_skip_path(request.url.path):
                return await call_next(request)
            
            # Extract session information
            session_id = await self._extract_session_id(request)
            if not session_id:
                # No session - use IP-based tracking
                session_id = self._get_client_identifier(request)
            
            # Prepare request data for analysis
            request_data = await self._prepare_request_data(request)
            
            # Phase 2 protection analysis
            protection_decision = await self._analyze_request(session_id, request_data)
            
            # Apply protection decision
            if not protection_decision.allow_request:
                return await self._create_protection_response(protection_decision)
            
            # Process the request
            response = await call_next(request)
            
            # Post-process response for learning
            await self._process_response(session_id, request_data, response)
            
            # Add protection headers
            self._add_protection_headers(response, protection_decision)
            
            return response
            
        except Exception as e:
            self.logger.error(f"Error in Phase 2 protection middleware: {e}")
            # Continue with request on error to avoid blocking legitimate traffic
            return await call_next(request)
        
        finally:
            # Track performance
            processing_time = (time.time() - start_time) * 1000  # Convert to milliseconds
            await self._update_performance_metrics(processing_time)

    async def _ensure_initialized(self):
        """Ensure coordinator is initialized (async initialization)"""
        if self.initialized:
            return
        
        try:
            if not self.initialization_task:
                self.initialization_task = asyncio.create_task(self.coordinator.initialize())
            
            # Wait for initialization with timeout
            await asyncio.wait_for(self.initialization_task, timeout=30.0)
            self.initialized = True
            self.logger.info("Phase 2 Protection Coordinator initialized successfully")
            
        except asyncio.TimeoutError:
            self.logger.error("Phase 2 Protection initialization timed out")
            self.enabled = False  # Disable on initialization failure
        except Exception as e:
            self.logger.error(f"Phase 2 Protection initialization failed: {e}")
            self.enabled = False

    def _should_skip_path(self, path: str) -> bool:
        """Check if path should skip protection analysis"""
        return any(skip_path in path for skip_path in self.skip_paths)

    async def _extract_session_id(self, request: Request) -> Optional[str]:
        """Extract session ID from request"""
        # Try header first
        session_id = request.headers.get("X-Session-ID")
        if session_id:
            return session_id
        
        # Try query parameter
        session_id = request.query_params.get("session_id")
        if session_id:
            return session_id
        
        # Try to extract from body for POST requests
        if request.method == "POST":
            try:
                # This is a simplified extraction - in practice, you'd need to handle
                # different content types and preserve the request body for downstream use
                if request.headers.get("content-type", "").startswith("application/json"):
                    # Note: This would consume the body stream, so in production
                    # you'd need a more sophisticated approach
                    pass
            except Exception:
                pass
        
        return None

    def _get_client_identifier(self, request: Request) -> str:
        """Get client identifier for requests without session ID"""
        # Use IP address and User-Agent as fallback identifier
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")
        
        # Create a consistent identifier
        import hashlib
        identifier_string = f"{client_ip}:{user_agent}"
        client_id = hashlib.md5(identifier_string.encode()).hexdigest()[:16]
        
        return f"client_{client_id}"

    async def _prepare_request_data(self, request: Request) -> Dict[str, Any]:
        """Prepare request data for protection analysis"""
        request_data = {
            "endpoint": request.url.path,
            "method": request.method,
            "timestamp": datetime.now(timezone.utc),
            "client_ip": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", ""),
            "content_type": request.headers.get("content-type", ""),
            "content_length": int(request.headers.get("content-length", 0)),
            "query_params": dict(request.query_params),
            "path_params": getattr(request, "path_params", {}),
        }
        
        # Add payload size estimate
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                request_data["payload_size"] = int(content_length)
            except ValueError:
                request_data["payload_size"] = 0
        else:
            request_data["payload_size"] = 0
        
        return request_data

    async def _analyze_request(self, session_id: str, request_data: Dict[str, Any]) -> ProtectionDecision:
        """Analyze request using Phase 2 coordinator"""
        try:
            self.request_count += 1
            
            if not self.coordinator or not self.initialized:
                # Fallback decision if coordinator not available
                return ProtectionDecision(
                    decision_id=f"fallback_{session_id}_{int(datetime.now(timezone.utc).timestamp())}",
                    session_id=session_id,
                    allow_request=True,
                    risk_assessment=RiskLevel.LOW,
                    confidence=0.0,
                    explanation="Phase 2 coordinator not available, allowing request",
                    decision_timestamp=datetime.now(timezone.utc)
                )
            
            # Use Phase 2 coordinator for analysis
            decision = await self.coordinator.analyze_request(session_id, request_data)
            self.protection_decisions += 1
            
            return decision
            
        except Exception as e:
            self.logger.error(f"Error in Phase 2 request analysis: {e}")
            # Safe fallback
            return ProtectionDecision(
                decision_id=f"error_{session_id}_{int(datetime.now(timezone.utc).timestamp())}",
                session_id=session_id,
                allow_request=True,
                risk_assessment=RiskLevel.MEDIUM,
                confidence=0.0,
                explanation=f"Analysis error, allowing request: {str(e)}",
                decision_timestamp=datetime.now(timezone.utc)
            )

    async def _create_protection_response(self, decision: ProtectionDecision) -> Response:
        """Create response for blocked requests"""
        # Determine response based on decision
        if "reputation_block" in decision.applied_restrictions:
            status_code = 403
            error_message = "Access denied due to reputation"
            error_code = "REPUTATION_BLOCK"
        elif "anomaly_detected" in decision.applied_restrictions:
            status_code = 429
            error_message = "Suspicious behavior detected"
            error_code = "ANOMALY_DETECTED"
        elif "circuit_breaker" in str(decision.applied_restrictions):
            status_code = 503
            error_message = "Service temporarily unavailable"
            error_code = "CIRCUIT_BREAKER_OPEN"
        elif decision.risk_assessment == RiskLevel.CRITICAL:
            status_code = 403
            error_message = "Access denied due to critical risk assessment"
            error_code = "CRITICAL_RISK"
        else:
            status_code = 429
            error_message = "Request blocked by protection system"
            error_code = "PROTECTION_BLOCK"
        
        # Create error response
        error_response = {
            "error": error_code,
            "message": error_message,
            "detail": decision.explanation,
            "correlation_id": decision.decision_id,
            "timestamp": to_json_compatible(decision.decision_timestamp),
            "risk_level": decision.risk_assessment.value,
            "confidence": decision.confidence
        }
        
        # Add retry information if applicable
        if status_code == 429:
            error_response["retry_after"] = 60  # Suggest retry after 60 seconds
        
        # Create response
        import json
        response = Response(
            content=json.dumps(error_response),
            status_code=status_code,
            headers={
                "Content-Type": "application/json",
                "X-Protection-Decision": decision.decision_id,
                "X-Risk-Level": decision.risk_assessment.value,
                "X-Protection-System": "FaultMaven-Phase2"
            }
        )
        
        # Add retry-after header for rate limiting
        if status_code == 429:
            response.headers["Retry-After"] = "60"
        
        return response

    async def _process_response(self, session_id: str, request_data: Dict[str, Any], response: Response):
        """Process response for learning and adaptation"""
        try:
            if not self.coordinator or not self.initialized:
                return
            
            # Prepare response data
            response_data = {
                "status_code": response.status_code,
                "response_time": 0.0,  # Will be calculated by the caller
                "timestamp": datetime.now(timezone.utc)
            }
            
            # Add error type for failed responses
            if response.status_code >= 400:
                if response.status_code >= 500:
                    response_data["error_type"] = "server_error"
                elif response.status_code >= 400:
                    response_data["error_type"] = "client_error"
            
            # Send to coordinator for learning
            await self.coordinator.process_response(session_id, request_data, response_data)
            
        except Exception as e:
            self.logger.error(f"Error processing response for learning: {e}")

    def _add_protection_headers(self, response: Response, decision: ProtectionDecision):
        """Add protection-related headers to response"""
        response.headers["X-Protection-Decision"] = decision.decision_id
        response.headers["X-Risk-Level"] = decision.risk_assessment.value
        response.headers["X-Protection-Confidence"] = f"{decision.confidence:.2f}"
        response.headers["X-Protection-System"] = "FaultMaven-Phase2"
        
        # Add restrictions header if any applied
        if decision.applied_restrictions:
            response.headers["X-Protection-Restrictions"] = ",".join(decision.applied_restrictions)

    async def _update_performance_metrics(self, processing_time: float):
        """Update performance metrics"""
        self._processing_times.append(processing_time)
        
        # Keep only recent measurements
        if len(self._processing_times) > 1000:
            self._processing_times = self._processing_times[-500:]
        
        # Update average
        self.average_processing_time = sum(self._processing_times) / len(self._processing_times)
        
        # Log warning if processing is slow
        if processing_time > self.max_processing_time:
            self.logger.warning(f"Phase 2 protection processing took {processing_time:.2f}ms "
                              f"(threshold: {self.max_processing_time}ms)")

    async def get_middleware_status(self) -> Dict[str, Any]:
        """Get middleware status for monitoring"""
        coordinator_status = {}
        if self.coordinator and self.initialized:
            try:
                coordinator_status = await self.coordinator.get_system_status()
            except Exception as e:
                coordinator_status = {"error": str(e)}
        
        return {
            "enabled": self.enabled,
            "initialized": self.initialized,
            "request_count": self.request_count,
            "protection_decisions": self.protection_decisions,
            "average_processing_time_ms": round(self.average_processing_time, 2),
            "coordinator_status": coordinator_status
        }

    async def shutdown(self):
        """Graceful shutdown"""
        if self.coordinator and self.initialized:
            await self.coordinator.shutdown()
        
        self.logger.info("Phase 2 Protection Middleware shut down")