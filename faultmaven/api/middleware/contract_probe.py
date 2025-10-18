"""
Runtime Contract Probe Middleware

Purpose: Monitor API contract compliance in real-time
Scope: Log critical headers, status codes, and response shapes
Target: Capture first failure per test with correlation_id for fast triage

This middleware logs contract-critical data points for every request,
enabling rapid identification of contract violations in production.
"""

import json
import time
import uuid
from typing import Dict, Any, Optional, List

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from faultmaven.infrastructure.logging.config import get_logger

logger = get_logger(__name__)


class ContractProbeMiddleware(BaseHTTPMiddleware):
    """
    Runtime contract compliance probe
    
    Logs essential contract data for triage:
    - Status codes (401 vs 500 for auth issues)
    - Critical headers (Location, X-Total-Count, Link, Retry-After)
    - Response shapes (array vs object envelope detection)
    - Correlation IDs for failure tracking
    """

    def __init__(
        self,
        app: ASGIApp,
        probe_enabled: bool = True,
        log_all_requests: bool = False,
        failure_sample_rate: float = 1.0
    ):
        super().__init__(app)
        self.probe_enabled = probe_enabled
        self.log_all_requests = log_all_requests
        self.failure_sample_rate = failure_sample_rate
        self._failure_cache: Dict[str, int] = {}  # Track failure patterns

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.probe_enabled:
            return await call_next(request)

        # Generate correlation ID for this request
        correlation_id = str(uuid.uuid4())[:8]
        start_time = time.time()
        
        # Extract request context
        method = request.method
        path = request.url.path
        query_params = dict(request.query_params) if request.query_params else {}
        
        response = await call_next(request)
        
        # Calculate response time
        response_time = time.time() - start_time
        
        # Probe contract-critical data points
        probe_data = self._extract_probe_data(
            method, path, query_params, response, correlation_id, response_time
        )
        
        # Log contract compliance data
        self._log_contract_probe(probe_data, request, response)
        
        return response

    def _extract_probe_data(
        self, 
        method: str, 
        path: str, 
        query_params: Dict[str, str],
        response: Response,
        correlation_id: str,
        response_time: float
    ) -> Dict[str, Any]:
        """Extract contract-critical data points"""
        
        probe_data = {
            "correlation_id": correlation_id,
            "method": method,
            "path": path,
            "status_code": response.status_code,
            "response_time_ms": round(response_time * 1000, 2),
            "timestamp": time.time(),
        }
        
        # Extract critical headers
        critical_headers = {}
        header_checks = {
            "Location": response.headers.get("Location"),
            "X-Total-Count": response.headers.get("X-Total-Count"),
            "Link": response.headers.get("Link"),
            "Retry-After": response.headers.get("Retry-After"),
            "Content-Type": response.headers.get("Content-Type"),
        }
        
        for header_name, header_value in header_checks.items():
            if header_value is not None:
                critical_headers[header_name] = header_value
                
        probe_data["headers"] = critical_headers
        
        # Analyze response body shape for contract violations
        if hasattr(response, 'body') and response.body:
            try:
                # For FastAPI, we need to get body differently in middleware
                # This is a simplified approach - in production you'd want more robust body extraction
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    probe_data["response_shape"] = self._analyze_response_shape(response)
            except Exception as e:
                logger.debug(f"Could not analyze response shape: {e}")
        
        # Detect contract violations
        violations = self._detect_violations(probe_data, path, method)
        if violations:
            probe_data["contract_violations"] = violations
            
        return probe_data

    def _analyze_response_shape(self, response: Response) -> Dict[str, Any]:
        """Analyze response body shape for contract compliance"""
        try:
            # This is a simplified approach - extracting response body in middleware
            # is complex in FastAPI/Starlette. In production, you'd want to use
            # a response interceptor or modify the endpoint handlers directly.
            
            return {
                "content_type": response.headers.get("Content-Type"),
                "content_length": response.headers.get("Content-Length", "unknown"),
                "is_json": "application/json" in response.headers.get("Content-Type", ""),
                # Body content analysis would require more complex middleware setup
            }
        except Exception as e:
            logger.debug(f"Response shape analysis failed: {e}")
            return {"analysis_error": str(e)}

    def _detect_violations(self, probe_data: Dict[str, Any], path: str, method: str) -> List[str]:
        """Detect common contract violations"""
        violations = []
        status_code = probe_data["status_code"]
        headers = probe_data.get("headers", {})
        
        # Critical violation: 500 for auth issues (should be 401/403)
        if status_code == 500 and any(auth_path in path for auth_path in ["/cases", "/sessions"]):
            violations.append("AUTH_500_VIOLATION: Protected endpoint returned 500, should be 401/403")
        
        # Critical violation: Missing Location header on 201/202
        if status_code in [201, 202]:
            location = headers.get("Location")
            if not location:
                violations.append(f"MISSING_LOCATION_HEADER: {status_code} response missing Location header")
            elif location in ["null", "", None]:
                violations.append(f"NULL_LOCATION_HEADER: {status_code} response has null/empty Location header")
        
        # Critical violation: Missing Retry-After on 202
        if status_code == 202 and not headers.get("Retry-After"):
            violations.append("MISSING_RETRY_AFTER: 202 response missing Retry-After header")
        
        # Critical violation: Missing pagination headers on list endpoints
        if method == "GET" and any(list_path in path for list_path in ["/cases", "/sessions"]):
            if status_code == 200:
                if not headers.get("X-Total-Count"):
                    violations.append("MISSING_PAGINATION_HEADER: List endpoint missing X-Total-Count header")
        
        return violations

    def _log_contract_probe(self, probe_data: Dict[str, Any], request: Request, response: Response):
        """Log contract probe data for triage"""
        
        status_code = probe_data["status_code"]
        path = probe_data["path"]
        correlation_id = probe_data["correlation_id"]
        violations = probe_data.get("contract_violations", [])
        
        # Always log violations
        if violations:
            failure_key = f"{probe_data['method']}:{path}:{status_code}"
            
            # Sample failures to avoid log spam
            if failure_key not in self._failure_cache:
                self._failure_cache[failure_key] = 0
            
            self._failure_cache[failure_key] += 1
            
            if self._failure_cache[failure_key] == 1:  # Log first occurrence
                logger.error(
                    f"CONTRACT_VIOLATION [{correlation_id}] {probe_data['method']} {path} -> {status_code}",
                    extra={
                        "correlation_id": correlation_id,
                        "contract_violations": violations,
                        "probe_data": probe_data,
                        "request_info": {
                            "method": probe_data["method"],
                            "path": path,
                            "user_agent": request.headers.get("User-Agent"),
                        }
                    }
                )
            elif self._failure_cache[failure_key] % 10 == 0:  # Log every 10th occurrence
                logger.warning(
                    f"CONTRACT_VIOLATION_RECURRING [{correlation_id}] {failure_key} (count: {self._failure_cache[failure_key]})"
                )
        
        # Log all requests if configured (for debugging)
        elif self.log_all_requests:
            logger.info(
                f"CONTRACT_PROBE [{correlation_id}] {probe_data['method']} {path} -> {status_code} ({probe_data['response_time_ms']}ms)",
                extra={"probe_data": probe_data}
            )

    def get_failure_summary(self) -> Dict[str, Any]:
        """Get summary of detected failures for monitoring dashboard"""
        return {
            "total_failure_patterns": len(self._failure_cache),
            "failure_counts": dict(self._failure_cache),
            "timestamp": time.time()
        }


# Health check endpoint for contract probe status
async def get_contract_probe_health() -> Dict[str, Any]:
    """Health check for contract probe middleware"""
    return {
        "service": "contract_probe_middleware",
        "status": "healthy",
        "timestamp": time.time(),
        "description": "Runtime API contract compliance monitoring"
    }