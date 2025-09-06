"""Protection Integration Tests

Tests protection endpoints in full application context with real FastAPI app.
Focus on end-to-end contract validation and OpenAPI compliance.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import Mock, AsyncMock, patch

from faultmaven.main import app


class TestProtectionIntegration:
    """Integration tests for protection endpoints with full app context."""

    @pytest.mark.asyncio
    async def test_protection_endpoints_integration_with_real_app(self):
        """Test protection endpoints work with real FastAPI application."""
        
        # Create a mock protection system for integration testing
        mock_protection_system = Mock()
        mock_protection_system.get_protection_status = AsyncMock(return_value={
            "system": "FaultMaven Unified Protection",
            "timestamp": "2025-01-15T10:30:00Z",
            "overall_status": "active",
            "basic_protection": {
                "status": "active",
                "components": ["rate_limiting", "request_deduplication"]
            },
            "intelligent_protection": {
                "status": "active",
                "components": ["behavioral_analysis", "ml_anomaly_detection"]
            }
        })
        
        mock_protection_system.get_protection_metrics = AsyncMock(return_value={
            "timestamp": "2025-01-15T10:30:00Z",
            "basic_metrics": {
                "rate_limiting": {"requests_checked": 1000, "requests_blocked": 10},
                "deduplication": {"requests_checked": 1000, "duplicates_found": 5}
            },
            "intelligent_metrics": {
                "behavioral_analysis": {"patterns_detected": 3, "anomalies_found": 1},
                "ml_detection": {"models_used": 2, "predictions_made": 500}
            },
            "combined_metrics": {
                "total_requests_processed": 1000,
                "total_requests_protected": 15,
                "protection_rate": 0.85
            }
        })
        
        # Mock config attributes
        mock_basic_config = Mock()
        mock_basic_config.enabled = True
        mock_basic_config.rate_limiting_enabled = True
        mock_basic_config.deduplication_enabled = True
        mock_basic_config.fail_open_on_redis_error = False
        mock_timeouts = Mock()
        mock_timeouts.enabled = True
        mock_basic_config.timeouts = mock_timeouts
        
        mock_intelligent_config = Mock()
        mock_intelligent_config.enable_behavioral_analysis = True
        mock_intelligent_config.enable_ml_detection = True
        mock_intelligent_config.enable_reputation_system = True
        mock_intelligent_config.enable_smart_circuit_breakers = False
        
        mock_protection_system.environment = "testing"
        mock_protection_system.basic_protection_enabled = True
        mock_protection_system.intelligent_protection_enabled = True
        mock_protection_system.basic_config = mock_basic_config
        mock_protection_system.intelligent_config = mock_intelligent_config
        mock_protection_system.protection_status = {"last_update": "2025-01-15T10:30:00"}
        
        # Test with real FastAPI application
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Patch the app.extra to provide protection system
            with patch.object(app, "extra", {"protection_system": mock_protection_system}):
                
                # Test all three protection endpoints
                endpoints_and_expected = [
                    ("/api/v1/protection/health", "overall_status"),
                    ("/api/v1/protection/metrics", "combined_metrics"),
                    ("/api/v1/protection/config", "system_info")
                ]
                
                for endpoint, expected_key in endpoints_and_expected:
                    response = await client.get(endpoint)
                    
                    # Validate HTTP response
                    assert response.status_code == 200, f"Endpoint {endpoint} failed with {response.status_code}"
                    assert response.headers["content-type"] == "application/json"
                    
                    # Validate response structure
                    data = response.json()
                    assert isinstance(data, dict), f"Endpoint {endpoint} should return dict"
                    assert expected_key in data, f"Endpoint {endpoint} missing expected key {expected_key}"

    @pytest.mark.asyncio 
    async def test_protection_endpoints_openapi_compliance(self):
        """Test that protection endpoints are properly documented in OpenAPI spec."""
        
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Get OpenAPI spec
            response = await client.get("/openapi.json")
            assert response.status_code == 200
            
            openapi_spec = response.json()
            paths = openapi_spec.get("paths", {})
            
            # Verify all protection endpoints are documented
            protection_endpoints = [
                "/api/v1/protection/health",
                "/api/v1/protection/metrics", 
                "/api/v1/protection/config"
            ]
            
            for endpoint in protection_endpoints:
                assert endpoint in paths, f"Protection endpoint {endpoint} not documented in OpenAPI spec"
                
                # Verify GET method is documented
                endpoint_spec = paths[endpoint]
                assert "get" in endpoint_spec, f"GET method not documented for {endpoint}"
                
                # Verify response schema is defined
                get_spec = endpoint_spec["get"]
                assert "responses" in get_spec, f"No responses defined for {endpoint}"
                assert "200" in get_spec["responses"], f"No 200 response defined for {endpoint}"

    @pytest.mark.asyncio
    async def test_protection_endpoints_error_handling_integration(self):
        """Test error handling across the full application stack."""
        
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Test with no protection system available (app.extra empty)
            with patch.object(app, "extra", {}):
                endpoints = [
                    "/api/v1/protection/health",
                    "/api/v1/protection/metrics",
                    "/api/v1/protection/config"
                ]
                
                for endpoint in endpoints:
                    response = await client.get(endpoint)
                    
                    # Should return 503 Service Unavailable
                    assert response.status_code == 503, f"Endpoint {endpoint} should return 503 when protection system unavailable"
                    assert response.headers["content-type"] == "application/json"
                    
                    error_data = response.json()
                    assert "detail" in error_data
                    assert "Protection system not available" in error_data["detail"]

    @pytest.mark.asyncio
    async def test_protection_endpoints_contract_validation(self):
        """Validate that protection endpoints meet the exact contract requirements."""
        
        # Mock protection system with comprehensive data
        mock_protection_system = Mock()
        mock_protection_system.get_protection_status = AsyncMock(return_value={
            "system": "FaultMaven Unified Protection",
            "timestamp": "2025-01-15T10:30:00Z", 
            "overall_status": "active",
            "basic_protection": {
                "status": "active",
                "components": ["rate_limiting", "request_deduplication", "timeouts"]
            },
            "intelligent_protection": {
                "status": "active", 
                "components": ["behavioral_analysis", "ml_anomaly_detection", "reputation_system"]
            },
            "statistics": {
                "total_requests": 5000,
                "protected_requests": 150
            }
        })
        
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            with patch.object(app, "extra", {"protection_system": mock_protection_system}):
                response = await client.get("/api/v1/protection/health")
                
                assert response.status_code == 200
                data = response.json()
                
                # Validate contract compliance - exact structure expected
                assert "system" in data
                assert "overall_status" in data 
                assert "basic_protection" in data
                assert "intelligent_protection" in data
                
                # Validate nested structures
                assert "status" in data["basic_protection"]
                assert "components" in data["basic_protection"]
                assert "status" in data["intelligent_protection"]
                assert "components" in data["intelligent_protection"]
                
                # Validate component arrays
                assert isinstance(data["basic_protection"]["components"], list)
                assert isinstance(data["intelligent_protection"]["components"], list)
                
                # Validate statistics if present
                if "statistics" in data:
                    assert "total_requests" in data["statistics"]
                    assert "protected_requests" in data["statistics"]