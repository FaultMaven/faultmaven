"""Protection API Endpoint Tests

Tests complete HTTP workflows for protection system monitoring endpoints.
Focus on real request/response validation and clean architecture compliance.
"""

import json
from typing import Dict, Any

import pytest
from httpx import AsyncClient
from unittest.mock import Mock, AsyncMock, patch


class TestProtectionAPIEndpoints:
    """Protection API tests using real HTTP workflows."""
    
    @pytest.mark.asyncio
    async def test_protection_health_endpoint_success(
        self, 
        client: AsyncClient,
        performance_tracker
    ):
        """Test protection health endpoint returns comprehensive status."""
        
        # Mock protection system for testing
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
            },
            "statistics": {
                "total_requests": 1000,
                "protected_requests": 50
            }
        })
        
        # Patch the app.extra to provide protection system
        with patch.object(client._transport.app, "extra", {"protection_system": mock_protection_system}):
            with performance_tracker.time_request("protection_health"):
                response = await client.get("/api/v1/protection/health")
        
        # Validate HTTP response
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        
        # Validate response structure
        data = response.json()
        assert "system" in data
        assert "overall_status" in data
        assert "basic_protection" in data
        assert "intelligent_protection" in data
        assert data["system"] == "FaultMaven Unified Protection"
        assert data["overall_status"] in ["active", "inactive"]
        
        # Validate component structures
        assert "status" in data["basic_protection"]
        assert "components" in data["basic_protection"]
        assert "status" in data["intelligent_protection"] 
        assert "components" in data["intelligent_protection"]
        
        # Verify delegation occurred
        mock_protection_system.get_protection_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_protection_metrics_endpoint_success(
        self,
        client: AsyncClient,
        performance_tracker
    ):
        """Test protection metrics endpoint returns comprehensive metrics."""
        
        # Mock protection system for testing
        mock_protection_system = Mock()
        mock_protection_system.get_protection_metrics = AsyncMock(return_value={
            "timestamp": "2025-01-15T10:30:00Z",
            "basic_metrics": {
                "rate_limiting": {
                    "requests_checked": 5000,
                    "requests_blocked": 25
                },
                "deduplication": {
                    "requests_checked": 5000,
                    "duplicates_found": 15
                }
            },
            "intelligent_metrics": {
                "behavioral_analysis": {
                    "patterns_detected": 3,
                    "anomalies_found": 1
                },
                "ml_detection": {
                    "models_used": 2,
                    "predictions_made": 1000
                }
            },
            "combined_metrics": {
                "total_requests_processed": 5000,
                "total_requests_protected": 40,
                "protection_rate": 0.8
            }
        })
        
        # Patch the app.extra to provide protection system
        with patch.object(client._transport.app, "extra", {"protection_system": mock_protection_system}):
            with performance_tracker.time_request("protection_metrics"):
                response = await client.get("/api/v1/protection/metrics")
        
        # Validate HTTP response
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        
        # Validate response structure
        data = response.json()
        assert "timestamp" in data
        assert "basic_metrics" in data
        assert "intelligent_metrics" in data
        assert "combined_metrics" in data
        
        # Validate combined metrics
        combined = data["combined_metrics"]
        assert "total_requests_processed" in combined
        assert "total_requests_protected" in combined
        assert "protection_rate" in combined
        assert isinstance(combined["protection_rate"], (int, float))
        
        # Verify delegation occurred
        mock_protection_system.get_protection_metrics.assert_called_once()

    @pytest.mark.asyncio
    async def test_protection_config_endpoint_success(
        self,
        client: AsyncClient,
        performance_tracker
    ):
        """Test protection config endpoint returns sanitized configuration."""
        
        # Mock protection system components
        mock_basic_config = Mock()
        mock_basic_config.enabled = True
        mock_basic_config.rate_limiting_enabled = True
        mock_basic_config.deduplication_enabled = True
        mock_basic_config.fail_open_on_redis_error = False
        mock_basic_config.timeouts = Mock()
        mock_basic_config.timeouts.enabled = True
        
        mock_intelligent_config = Mock()
        mock_intelligent_config.enable_behavioral_analysis = True
        mock_intelligent_config.enable_ml_detection = True
        mock_intelligent_config.enable_reputation_system = True
        mock_intelligent_config.enable_smart_circuit_breakers = False
        
        mock_protection_system = Mock()
        mock_protection_system.environment = "testing"
        mock_protection_system.basic_protection_enabled = True
        mock_protection_system.intelligent_protection_enabled = True
        mock_protection_system.basic_config = mock_basic_config
        mock_protection_system.intelligent_config = mock_intelligent_config
        mock_protection_system.protection_status = {
            "last_update": "2025-01-15T10:30:00"
        }
        
        # Patch the app.extra to provide protection system
        with patch.object(client._transport.app, "extra", {"protection_system": mock_protection_system}):
            with performance_tracker.time_request("protection_config"):
                response = await client.get("/api/v1/protection/config")
        
        # Validate HTTP response
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        
        # Validate response structure
        data = response.json()
        assert "timestamp" in data
        assert "system_info" in data
        assert "basic_protection" in data
        assert "intelligent_protection" in data
        
        # Validate system info
        system_info = data["system_info"]
        assert "environment" in system_info
        assert "basic_protection_enabled" in system_info
        assert "intelligent_protection_enabled" in system_info
        assert system_info["environment"] == "testing"
        
        # Validate basic protection config
        basic = data["basic_protection"]
        assert "enabled" in basic
        assert "rate_limiting_enabled" in basic
        assert "deduplication_enabled" in basic
        assert "timeouts_enabled" in basic
        assert "fail_open_on_redis_error" in basic
        
        # Validate intelligent protection config
        intelligent = data["intelligent_protection"]
        assert "behavioral_analysis" in intelligent
        assert "ml_detection" in intelligent
        assert "reputation_system" in intelligent
        assert "smart_circuit_breakers" in intelligent

    @pytest.mark.asyncio
    async def test_protection_health_system_not_available(
        self,
        client: AsyncClient
    ):
        """Test protection health endpoint when protection system not available."""
        
        # Patch the app.extra to NOT provide protection system
        with patch.object(client._transport.app, "extra", {}):
            response = await client.get("/api/v1/protection/health")
        
        # Should return 503 Service Unavailable
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/json"
        
        data = response.json()
        assert "detail" in data
        assert "Protection system not available" in data["detail"]

    @pytest.mark.asyncio
    async def test_protection_metrics_system_not_available(
        self,
        client: AsyncClient
    ):
        """Test protection metrics endpoint when protection system not available."""
        
        # Patch the app.extra to NOT provide protection system
        with patch.object(client._transport.app, "extra", {}):
            response = await client.get("/api/v1/protection/metrics")
        
        # Should return 503 Service Unavailable
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/json"
        
        data = response.json()
        assert "detail" in data
        assert "Protection system not available" in data["detail"]

    @pytest.mark.asyncio
    async def test_protection_config_system_not_available(
        self,
        client: AsyncClient
    ):
        """Test protection config endpoint when protection system not available."""
        
        # Patch the app.extra to NOT provide protection system
        with patch.object(client._transport.app, "extra", {}):
            response = await client.get("/api/v1/protection/config")
        
        # Should return 503 Service Unavailable  
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/json"
        
        data = response.json()
        assert "detail" in data
        assert "Protection system not available" in data["detail"]

    @pytest.mark.asyncio
    async def test_protection_health_service_error(
        self,
        client: AsyncClient
    ):
        """Test protection health endpoint handles service errors gracefully."""
        
        # Mock protection system that throws an exception
        mock_protection_system = Mock()
        mock_protection_system.get_protection_status = AsyncMock(
            side_effect=Exception("Protection service unavailable")
        )
        
        # Patch the app.extra to provide failing protection system
        with patch.object(client._transport.app, "extra", {"protection_system": mock_protection_system}):
            response = await client.get("/api/v1/protection/health")
        
        # Should return 500 Internal Server Error
        assert response.status_code == 500
        assert response.headers["content-type"] == "application/json"
        
        data = response.json()
        assert "detail" in data
        assert "Failed to retrieve protection system health" in data["detail"]

    @pytest.mark.asyncio
    async def test_protection_endpoints_follow_openapi_spec(
        self,
        client: AsyncClient
    ):
        """Test that protection endpoints match OpenAPI specification."""
        
        # Mock protection system for all endpoints
        mock_protection_system = Mock()
        mock_protection_system.get_protection_status = AsyncMock(return_value={"status": "active"})
        mock_protection_system.get_protection_metrics = AsyncMock(return_value={"metrics": "data"})
        mock_protection_system.environment = "testing"
        mock_protection_system.basic_protection_enabled = True
        mock_protection_system.intelligent_protection_enabled = True
        
        # Create basic config mock with required attributes
        mock_basic_config = Mock()
        mock_basic_config.enabled = True
        mock_basic_config.rate_limiting_enabled = True
        mock_basic_config.deduplication_enabled = True
        mock_basic_config.fail_open_on_redis_error = False
        mock_timeouts = Mock()
        mock_timeouts.enabled = True
        mock_basic_config.timeouts = mock_timeouts
        
        # Create intelligent config mock with required attributes
        mock_intelligent_config = Mock()
        mock_intelligent_config.enable_behavioral_analysis = True
        mock_intelligent_config.enable_ml_detection = True
        mock_intelligent_config.enable_reputation_system = True
        mock_intelligent_config.enable_smart_circuit_breakers = False
        
        mock_protection_system.basic_config = mock_basic_config
        mock_protection_system.intelligent_config = mock_intelligent_config
        mock_protection_system.protection_status = {"last_update": "2025-01-15T10:30:00"}
        
        with patch.object(client._transport.app, "extra", {"protection_system": mock_protection_system}):
            
            # Test all expected endpoints from OpenAPI spec exist and respond
            endpoints = [
                "/api/v1/protection/health",
                "/api/v1/protection/metrics", 
                "/api/v1/protection/config"
            ]
            
            for endpoint in endpoints:
                response = await client.get(endpoint)
                
                # All endpoints should be available (not 404)
                assert response.status_code != 404, f"Endpoint {endpoint} not found"
                
                # All endpoints should return JSON
                assert response.headers["content-type"] == "application/json"
                
                # All endpoints should return valid JSON
                data = response.json()
                assert isinstance(data, dict), f"Endpoint {endpoint} should return JSON object"


class TestProtectionAPIErrorScenarios:
    """Test protection API error handling and edge cases."""
    
    @pytest.mark.asyncio
    async def test_protection_endpoints_with_malformed_responses(
        self,
        client: AsyncClient
    ):
        """Test protection endpoints handle malformed service responses."""
        
        # Mock protection system that returns malformed data
        mock_protection_system = Mock()
        mock_protection_system.get_protection_status = AsyncMock(return_value=None)  # Invalid response
        
        with patch.object(client._transport.app, "extra", {"protection_system": mock_protection_system}):
            response = await client.get("/api/v1/protection/health")
        
        # Endpoint should still work (return the None response)
        assert response.status_code == 200
        assert response.json() is None

    @pytest.mark.asyncio
    async def test_protection_endpoints_performance_acceptable(
        self,
        client: AsyncClient,
        performance_tracker
    ):
        """Test that protection endpoints respond within acceptable time limits."""
        
        # Mock fast protection system
        mock_protection_system = Mock()
        mock_protection_system.get_protection_status = AsyncMock(return_value={"status": "active"})
        mock_protection_system.get_protection_metrics = AsyncMock(return_value={"metrics": "data"})
        
        with patch.object(client._transport.app, "extra", {"protection_system": mock_protection_system}):
            
            # Test each endpoint performance
            with performance_tracker.time_request("protection_health"):
                response = await client.get("/api/v1/protection/health")
                assert response.status_code == 200
            
            with performance_tracker.time_request("protection_metrics"):
                response = await client.get("/api/v1/protection/metrics")
                assert response.status_code == 200
            
            # Verify performance is acceptable (under 200ms)
            health_time = performance_tracker.timings.get("protection_health", 0)
            metrics_time = performance_tracker.timings.get("protection_metrics", 0)
            
            assert health_time < 0.2, f"Health endpoint too slow: {health_time}s"
            assert metrics_time < 0.2, f"Metrics endpoint too slow: {metrics_time}s"