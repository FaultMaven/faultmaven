"""Unified Test Doubles and Fixtures for Testing

Consolidated test double implementations and testing utilities that provide:
- Lightweight test doubles with minimal mocking
- Unified test environment for cross-phase integration
- Performance tracking and validation utilities
- Simple, predictable behavior for testing without heavy mocking
"""

import asyncio
import time
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, AsyncGenerator
from contextlib import asynccontextmanager
import pytest
from unittest.mock import Mock, AsyncMock

# Try to import models, fall back to basic types if not available
try:
    from faultmaven.models import DataType, DataInsightsResponse
    from faultmaven.models.interfaces import (
        ILLMProvider, BaseTool, ITracer, ISanitizer, 
        IDataClassifier, ILogProcessor, IStorageBackend, ToolResult
    )
    MODELS_AVAILABLE = True
except ImportError:
    # Create minimal stubs for testing environments
    MODELS_AVAILABLE = False
    class DataType:
        LOG_FILE = "log_file"
        ERROR_TRACE = "error_trace"
        TEXT = "text"
    
    class DataInsightsResponse:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    
    class ToolResult:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    
    # Interface stubs
    ILLMProvider = object
    BaseTool = object
    ITracer = object
    ISanitizer = object
    IDataClassifier = object
    ILogProcessor = object
    IStorageBackend = object


class LightweightLLMProvider(ILLMProvider):
    """Lightweight LLM provider for testing"""
    
    def __init__(self, response_latency_ms: int = 10):
        self.call_count = 0
        self.last_prompt = None
        self.response_latency_ms = response_latency_ms
        self.response_templates = {
            "database": {
                'findings': [
                    {
                        'type': 'error',
                        'message': 'Database connection timeout detected',
                        'severity': 'high',
                        'confidence': 0.9,
                        'source': 'log_analysis'
                    }
                ],
                'recommendations': [
                    'Increase database connection timeout to 30 seconds',
                    'Monitor connection pool utilization'
                ],
                'confidence': 0.85,
                'root_cause': 'Database connection pool exhaustion under high load'
            },
            "memory": {
                'findings': [
                    {
                        'type': 'performance',
                        'message': 'Memory usage spike detected',
                        'severity': 'high',
                        'confidence': 0.95
                    }
                ],
                'recommendations': ['Investigate memory leaks', 'Review garbage collection'],
                'confidence': 0.92,
                'root_cause': 'Memory leak in user session management'
            },
            "default": {
                'findings': [
                    {
                        'type': 'info',
                        'message': 'System analysis completed',
                        'severity': 'info',
                        'confidence': 0.7
                    }
                ],
                'recommendations': ['Continue monitoring system health'],
                'confidence': 0.7,
                'root_cause': None
            }
        }
    
    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate text using the LLM provider (implements ILLMProvider interface)"""
        self.call_count += 1
        self.last_prompt = prompt
        # Add realistic delay
        if self.response_latency_ms > 0:
            await asyncio.sleep(self.response_latency_ms / 1000.0)
        
        # Return contextual response based on query content
        prompt_lower = prompt.lower()
        if "database" in prompt_lower or "connection" in prompt_lower:
            template = self.response_templates["database"]
            return template.get('root_cause', f"Database analysis response to: {prompt[:50]}...")
        elif "memory" in prompt_lower or "heap" in prompt_lower:
            template = self.response_templates["memory"]
            return template.get('root_cause', f"Memory analysis response to: {prompt[:50]}...")
        else:
            return f"Test response to: {prompt[:50]}..."
    
    async def generate_response(self, prompt: str, **kwargs) -> str:
        """Generate response (backwards compatibility method)"""
        return await self.generate(prompt, **kwargs)
    
    def configure_response(self, template_key: str, response_data: Dict[str, Any]):
        """Configure response for specific template key"""
        self.response_templates[template_key] = response_data


class LightweightTool(BaseTool):
    """Lightweight tool for testing"""
    
    def __init__(self, name: str = "test_tool", description: str = None):
        self.name = name
        self.description = description or f"Test tool {name}"
        self.call_count = 0
    
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Execute the tool with the given parameters"""
        self.call_count += 1
        query = params.get("query", str(params))
        
        # Add small processing delay
        await asyncio.sleep(0.005)  # 5ms
        
        return ToolResult(
            success=True,
            data=f"Tool {self.name} executed with: {query}",
            error=None
        )
    
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Query or input for the tool"
                    }
                }
            }
        }


class LightweightSanitizer(ISanitizer):
    """Lightweight sanitizer for testing"""
    
    def __init__(self):
        self.sanitize_count = 0
    
    def sanitize(self, content: Any) -> Any:
        """Sanitize content while preserving data structure"""
        self.sanitize_count += 1
        
        # Handle None
        if content is None:
            return None
        
        # Handle strings - replace sensitive data
        if isinstance(content, str):
            return content.replace("secret", "[REDACTED]").replace("password", "[REDACTED]")
        
        # Handle lists - sanitize each item
        if isinstance(content, list):
            return [self.sanitize(item) for item in content]
        
        # Handle dictionaries - sanitize values
        if isinstance(content, dict):
            return {key: self.sanitize(value) for key, value in content.items()}
        
        # Return other types as-is (int, float, bool, etc.)
        return content


class LightweightTracer(ITracer):
    """Lightweight tracer for testing"""
    
    def __init__(self):
        self.traces = []
    
    def trace(self, operation_name: str):
        """Return a context manager for tracing operations"""
        return TracingContext(operation_name, self.traces)


class TracingContext:
    """Context manager for tracing operations"""
    
    def __init__(self, operation_name: str, traces: list):
        self.operation_name = operation_name
        self.traces = traces
        self.trace_id = f"trace_{len(traces)}"
    
    def __enter__(self):
        self.traces.append({"operation": self.operation_name, "trace_id": self.trace_id, "status": "started"})
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.traces[-1]["status"] = "completed"
        else:
            self.traces[-1]["status"] = "failed"
            self.traces[-1]["error"] = str(exc_val)
        return False  # Don't suppress exceptions


class LightweightDataClassifier(IDataClassifier):
    """Lightweight data classifier for testing"""
    
    def __init__(self, classification_time_ms: int = 5):
        self.classify_count = 0
        self.call_count = 0  # For performance tests
        self.classification_time_ms = classification_time_ms
    
    async def classify(self, content: str, filename: Optional[str] = None) -> DataType:
        self.classify_count += 1
        self.call_count += 1  # For performance tests
        
        # Add realistic delay
        if self.classification_time_ms > 0:
            await asyncio.sleep(self.classification_time_ms / 1000.0)
        
        if filename and filename.endswith('.log'):
            return DataType.LOG_FILE
        elif 'error' in content.lower() or 'traceback' in content.lower():
            return DataType.ERROR_TRACE
        return DataType.TEXT


class LightweightLogProcessor(ILogProcessor):
    """Lightweight log processor for testing"""
    
    def __init__(self, classification_time_ms: int = 5):
        self.call_count = 0
        self.process_count = 0  # Keep for backwards compatibility
        self.classification_time_ms = classification_time_ms
    
    async def process(self, content: str, data_type: Optional[DataType] = None) -> Dict[str, Any]:
        """Process log content and extract insights"""
        self.call_count += 1
        self.process_count += 1  # Keep for backwards compatibility
        
        # Add realistic delay
        if self.classification_time_ms > 0:
            await asyncio.sleep(self.classification_time_ms / 1000.0)
        
        error_count = content.lower().count('error')
        critical_count = content.lower().count('critical')
        warning_count = content.lower().count('warn')
        
        # Business logic: CRITICAL logs should be counted as errors since they're highest severity
        total_error_count = error_count + critical_count
        
        # Return dictionary with insights as expected by interface
        insights = {
            "error_count": total_error_count,  # Combined error + critical count
            "critical_count": critical_count,   # Keep separate for detailed analysis
            "warning_count": warning_count,
            "total_lines": len(content.split('\n')),
            "patterns_detected": ["test_pattern"] if total_error_count > 0 else [],
            "anomalies": ["high_error_rate"] if total_error_count > 5 else []
        }
        
        return {
            "processing_time_ms": 50,
            "confidence_score": 0.8 if total_error_count > 0 else 0.6,
            "insights": insights,
            "recommendations": ["Test recommendation", "Review logs"] if total_error_count > 0 else ["Continue monitoring"]
        }


class LightweightStorageBackend(IStorageBackend):
    """Lightweight storage backend for testing"""
    
    def __init__(self, storage_latency_ms: int = 5):
        self.data = {}
        self.store_count = 0
        self.operation_count = 0  # Track all operations for performance tests
        self.storage_latency_ms = storage_latency_ms
    
    async def store(self, key: str, data: Any) -> None:
        """Store data with given key (implements IStorageBackend interface)"""
        self.store_count += 1
        self.operation_count += 1
        
        # Add realistic delay
        if self.storage_latency_ms > 0:
            await asyncio.sleep(self.storage_latency_ms / 1000.0)
        
        self.data[key] = data
    
    async def retrieve(self, key: str) -> Optional[Any]:
        """Retrieve data by key"""
        self.operation_count += 1
        
        # Add realistic delay
        if self.storage_latency_ms > 0:
            await asyncio.sleep(self.storage_latency_ms / 1000.0)
        
        return self.data.get(key)
    
    # Additional utility methods for backwards compatibility and testing
    async def store_with_metadata(self, data_id: str, content: str, metadata: Dict[str, Any]) -> bool:
        """Store with metadata (backwards compatibility method)"""
        await self.store(data_id, {"content": content, "metadata": metadata})
        return True
    
    async def delete(self, data_id: str) -> bool:
        """Delete data by key"""
        self.operation_count += 1
        if data_id in self.data:
            del self.data[data_id]
            return True
        return False
    
    async def list_ids(self) -> List[str]:
        """List all stored keys"""
        self.operation_count += 1
        return list(self.data.keys())


# =============================================================================
# UNIFIED TEST ENVIRONMENT (from integration_fixtures.py)
# =============================================================================

class UnifiedTestEnvironment:
    """
    Unified test environment providing infrastructure for all test phases:
    
    - Logging Integration - Real log coordination
    - Service Layer - Real business logic with test doubles  
    - API Layer - Real FastAPI application
    - Infrastructure - Real network, persistence, security
    """
    
    def __init__(self):
        self.log_capture = None
        self.llm_provider = None
        self.test_http_server = None
        self.redis_store = None
        self.performance_tracker = None
        self.active_sessions = []
        self.metrics = {}
        self.fake_redis = {}
        self.start_time = time.time()
    
    async def initialize(self):
        """Initialize all phase infrastructure."""
        # Service Layer Setup - Use lightweight test doubles
        self.llm_provider = LightweightLLMProvider()
        
        # Infrastructure Setup - Simple in-memory storage
        self.fake_redis = {}
        
        # Performance tracking
        self.start_time = time.time()
        
    async def cleanup(self):
        """Clean up all test infrastructure."""
        # Clean up sessions
        for session_id in self.active_sessions:
            try:
                # Best effort cleanup
                if session_id in self.fake_redis:
                    del self.fake_redis[session_id]
            except Exception:
                pass  # Best effort cleanup
        
        # Clean up other resources
        self.active_sessions.clear()
        self.metrics.clear()
    
    def create_test_session(self, session_id: str = None) -> str:
        """Create a test session and track it for cleanup."""
        if session_id is None:
            session_id = f"integration-test-{int(time.time() * 1000)}"
        
        self.active_sessions.append(session_id)
        return session_id
    
    @asynccontextmanager
    async def performance_tracking(self, operation_name: str):
        """Track performance metrics for any cross-phase operation."""
        try:
            import psutil
            process = psutil.Process()
            start_memory = process.memory_info().rss / 1024 / 1024  # MB
        except ImportError:
            start_memory = 0
        
        start_time = time.time()
        
        try:
            yield
        finally:
            end_time = time.time()
            try:
                import psutil
                process = psutil.Process()
                end_memory = process.memory_info().rss / 1024 / 1024  # MB
            except ImportError:
                end_memory = start_memory
            
            self.metrics[operation_name] = {
                "duration": end_time - start_time,
                "memory_start": start_memory,
                "memory_end": end_memory,
                "memory_increase": end_memory - start_memory,
                "timestamp": start_time
            }
    
    def get_cross_phase_metrics(self) -> Dict[str, Any]:
        """Get metrics aggregated across all phases."""
        if not self.metrics:
            return {}
        
        durations = [m["duration"] for m in self.metrics.values()]
        memory_increases = [m["memory_increase"] for m in self.metrics.values()]
        
        return {
            "total_operations": len(self.metrics),
            "avg_duration": sum(durations) / len(durations),
            "max_duration": max(durations),
            "total_memory_increase": sum(memory_increases),
            "max_memory_increase": max(memory_increases),
            "operations": list(self.metrics.keys())
        }


class PerformanceValidator:
    """Performance validation utilities for testing."""
    
    def __init__(self):
        self.baseline_targets = {
            "api_request": {"max_duration": 2.0, "max_memory": 50},
            "service_operation": {"max_duration": 1.0, "max_memory": 30}, 
            "infrastructure_call": {"max_duration": 0.5, "max_memory": 20},
            "logging_coordination": {"max_duration": 0.1, "max_memory": 10}
        }
    
    def validate_operation_performance(self, operation_name: str, metrics: Dict[str, Any]) -> bool:
        """Validate that an operation meets performance targets."""
        targets = self.baseline_targets.get(operation_name, {})
        
        duration_ok = metrics["duration"] <= targets.get("max_duration", float('inf'))
        memory_ok = metrics["memory_increase"] <= targets.get("max_memory", float('inf'))
        
        return duration_ok and memory_ok
    
    def validate_80_percent_improvement(self, current_duration: float, legacy_duration: float) -> bool:
        """Validate 80%+ improvement over legacy implementation."""
        improvement = (legacy_duration - current_duration) / legacy_duration
        return improvement >= 0.8
    
    def get_performance_summary(self, all_metrics: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Generate performance summary across all operations."""
        if not all_metrics:
            return {}
        
        total_duration = sum(m["duration"] for m in all_metrics.values())
        total_memory = sum(m["memory_increase"] for m in all_metrics.values())
        max_duration = max(m["duration"] for m in all_metrics.values())
        max_memory = max(m["memory_increase"] for m in all_metrics.values())
        
        return {
            "total_operations": len(all_metrics),
            "total_duration": total_duration,
            "avg_duration": total_duration / len(all_metrics),
            "max_duration": max_duration,
            "total_memory_increase": total_memory,
            "max_memory_increase": max_memory,
            "meets_targets": {
                "duration": total_duration < 10.0,  # All operations <10s
                "memory": total_memory < 100.0      # Total increase <100MB
            }
        }


class TestExternalClient:
    """Lightweight external client for testing infrastructure logging."""
    
    def __init__(self, client_name: str = "test_client", service_name: str = "TestService"):
        self.client_name = client_name
        self.service_name = service_name
        self.call_count = 0
    
    async def call_external(self, operation_name: str, operation_func, params: Dict[str, Any], 
                          timeout: float = 5.0, validate_response=None):
        """Simulate external API call with logging."""
        self.call_count += 1
        
        # Simulate some processing time
        await asyncio.sleep(0.01)
        
        # Call the operation function
        try:
            if timeout <= 0.1:  # Simulate timeout for very short timeouts
                raise asyncio.TimeoutError("Simulated timeout")
                
            result = await operation_func(params)
            
            # Validate response if validator provided
            if validate_response and not validate_response(result):
                raise RuntimeError(f"External call {operation_name} failed after validation. Response validation failed")
            
            return {
                "status": "success",
                "operation": operation_name,
                "api_response": result
            }
        except Exception as e:
            raise RuntimeError(f"External call {operation_name} failed after 1 attempts. {str(e)}")
    
    async def simple_api_call(self, params: Dict[str, Any]):
        """Simple API call simulation."""
        await asyncio.sleep(0.005)  # 5ms processing time
        return {
            "data": f"Response for {params}",
            "timestamp": time.time(),
            **params  # Echo back the parameters
        }


class IntegrationValidator:
    """Cross-phase integration validation utilities."""
    
    def validate_end_to_end_workflow(self, 
                                   api_response: Dict[str, Any],
                                   service_result: Any, 
                                   infrastructure_logs: List[Dict],
                                   correlation_id: str) -> Dict[str, Any]:
        """Validate that an end-to-end workflow worked correctly."""
        validations = {
            "api_layer": {
                "response_received": api_response is not None,
                "status_ok": api_response.get("status") in ["success", "completed", 200],
                "has_investigation_id": "investigation_id" in api_response
            },
            "service_layer": {
                "result_generated": service_result is not None,
                "business_logic_executed": hasattr(service_result, "findings") or "findings" in str(service_result)
            },
            "infrastructure": {
                "external_calls_logged": len(infrastructure_logs) > 0,
                "correlation_maintained": any(correlation_id in str(log) for log in infrastructure_logs)
            },
            "integration": {
                "correlation_id_propagated": correlation_id is not None,
                "cross_layer_communication": True  # Will be validated by caller
            }
        }
        
        # Calculate overall success
        all_validations = []
        for layer_validations in validations.values():
            all_validations.extend(layer_validations.values())
        
        validations["overall"] = {
            "success_rate": sum(1 for v in all_validations if v) / len(all_validations),
            "all_layers_integrated": all(any(v.values()) for v in validations.values() if v != validations["overall"])
        }
        
        return validations
    
    def validate_error_propagation(self, 
                                 api_error: Optional[Dict],
                                 service_error: Optional[Exception],
                                 infrastructure_error: Optional[Exception],
                                 error_logs: List[Dict]) -> Dict[str, Any]:
        """Validate that errors propagate correctly across all layers."""
        return {
            "error_captured": any([api_error, service_error, infrastructure_error]),
            "error_logged": len(error_logs) > 0,
            "error_structured": all("correlation_id" in log for log in error_logs if log.get("level") == "ERROR"),
            "error_recovery_attempted": any("retry" in str(log) or "fallback" in str(log) for log in error_logs)
        }
    
    def validate_concurrent_operations(self, 
                                     operation_results: List[Any],
                                     expected_count: int,
                                     max_duration: float) -> Dict[str, Any]:
        """Validate concurrent operation handling across phases."""
        successful_operations = [r for r in operation_results if not isinstance(r, Exception)]
        failed_operations = [r for r in operation_results if isinstance(r, Exception)]
        
        return {
            "expected_operations": expected_count,
            "successful_operations": len(successful_operations),
            "failed_operations": len(failed_operations),
            "success_rate": len(successful_operations) / len(operation_results),
            "within_time_limit": True,  # Caller should validate timing
            "no_resource_conflicts": len(failed_operations) == 0 or all("timeout" not in str(e) for e in failed_operations)
        }


# =============================================================================
# PYTEST FIXTURES
# =============================================================================

@pytest.fixture
async def unified_test_env() -> AsyncGenerator[UnifiedTestEnvironment, None]:
    """Fixture providing unified test environment for all phases."""
    env = UnifiedTestEnvironment()
    await env.initialize()
    try:
        yield env
    finally:
        await env.cleanup()


@pytest.fixture
def performance_validator():
    """Fixture providing performance validation utilities."""
    return PerformanceValidator()


@pytest.fixture
def integration_validator():
    """Fixture providing cross-phase integration validation."""
    return IntegrationValidator()


@pytest.fixture
def test_external_client():
    """Fixture providing external client for testing."""
    return TestExternalClient()