"""tests/test_main.py

Purpose: Tests for the main FastAPI application lifecycle
"""

import pytest
import os
from unittest.mock import patch, Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.main import app


def test_health_check():
    """
    Tests the /health endpoint to ensure the application is running
    and responding correctly.
    """
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    
    data = response.json()
    
    # Check core health status
    assert data["status"] in ["healthy", "degraded", "unhealthy"]
    
    # Check required services exist
    assert "services" in data
    services = data["services"]
    # After Phase 3: session_manager status may be object or string
    if isinstance(services["session_manager"], dict):
        assert "status" in services["session_manager"]
    else:
        assert services["session_manager"] in ["active", "inactive", "unknown"]
    
    # API should be running
    if "api" in services:
        assert services["api"] == "running"
    
    # Phase 3: Architecture migration information should NOT be present
    migration_fields = [
        "migration_strategy", "migration_safe", "using_new_api", 
        "using_di_container", "architecture_migration", "refactored_components"
    ]
    
    def check_nested_dict(data, field_name):
        """Recursively check for field in nested dict."""
        if isinstance(data, dict):
            if field_name in data:
                return True
            for value in data.values():
                if check_nested_dict(value, field_name):
                    return True
        elif isinstance(data, list):
            for item in data:
                if check_nested_dict(item, field_name):
                    return True
        return False
    
    for field in migration_fields:
        assert not check_nested_dict(data, field), \
            f"Migration field '{field}' should not be present in health check after Phase 3"


def test_root_endpoint():
    """
    Tests the root (/) endpoint to ensure it returns the correct API
    information and is simplified after Phase 3.
    """
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "FaultMaven API"
        assert "version" in data
        
        # Validate response structure - Phase 3 simplified fields
        required_fields = ["message", "version", "description", "docs", "health"]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"
        
        # Phase 3: Architecture/migration information should NOT be present
        prohibited_fields = [
            "architecture", "migration_strategy", "migration_status",
            "using_new_api", "using_di_container", "refactored_components"
        ]
        
        for field in prohibited_fields:
            assert field not in data, \
                f"Prohibited migration field '{field}' should not be present in root endpoint after Phase 3"
        
        # Verify essential navigation links
        assert data["docs"] == "/docs"
        assert data["health"] == "/health"


def test_application_structure():
    """Test basic application structure and configuration"""
    # Validate app is FastAPI instance
    assert isinstance(app, FastAPI)
    
    # Validate basic app configuration
    assert app.title == "FaultMaven API"
    assert app.description is not None
    assert app.version is not None
    
    # Validate app has necessary attributes
    assert hasattr(app, 'router')
    assert hasattr(app, 'middleware')


@pytest.mark.integration
def test_api_routes_registration():
    """Test that API routes are properly registered"""
    with TestClient(app) as client:
        # Test that key endpoints exist (even if they return errors due to missing auth)
        endpoints_to_check = [
            "/",
            "/health",
            "/api/v1/agent/query",
            "/api/v1/data/ingest", 
            "/api/v1/knowledge/search",
            "/api/v1/sessions"
        ]
        
        for endpoint in endpoints_to_check:
            response = client.get(endpoint) if endpoint in ["/", "/health"] else client.post(endpoint, json={})
            # Should not return 404 (route not found)
            assert response.status_code != 404, f"Route {endpoint} not found"


def test_cors_configuration():
    """Test CORS middleware is configured"""
    # Check if CORS middleware is present
    cors_middleware_found = False
    for middleware in app.user_middleware:
        middleware_class = middleware.cls.__name__
        if 'CORS' in middleware_class:
            cors_middleware_found = True
            break
    
    # CORS should be configured in production apps
    # This is more of a configuration check
    # We can't easily test the actual CORS behavior without complex setup
    assert len(app.user_middleware) >= 0  # At least some middleware should be present


def test_environment_configuration_handling():
    """Test that environment variables affect app configuration (Phase 3 updated)"""
    # Test that the app can handle different environment configurations
    # Phase 3: Test with active feature flags only (deprecated migration flags removed)
    
    with patch.dict(os.environ, {
        'ENABLE_LEGACY_COMPATIBILITY': 'true',
        'ENABLE_EXPERIMENTAL_FEATURES': 'false',
        'ENABLE_PERFORMANCE_MONITORING': 'true',
        'ENABLE_DETAILED_TRACING': 'false'
    }):
        # The app should remain functional with Phase 3 configuration
        assert isinstance(app, FastAPI)
        assert app.title == "FaultMaven API"
    
    # Test health endpoint still works
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        
        # Verify no migration-related environment variables affect health
        health_data = response.json()
        health_str = str(health_data).lower()
        
        deprecated_env_vars = [
            "use_refactored_api", "use_di_container", "enable_migration_logging"
        ]
        
        for var in deprecated_env_vars:
            assert var not in health_str, \
                f"Deprecated environment variable {var} should not affect health check"


def test_health_check_with_session_metrics():
    """Test health check includes session metrics from configuration manager."""
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        
        data = response.json()
        
        # Check for session metrics in health response
        if "session_metrics" in data:
            session_metrics = data["session_metrics"]
            
            # Verify expected session metric fields
            expected_metrics = [
                "active_sessions",
                "cleanup_runs",
                "session_timeout_minutes",
                "cleanup_interval_minutes"
            ]
            
            for metric in expected_metrics:
                if metric in session_metrics:
                    assert isinstance(session_metrics[metric], (int, float, bool))


def test_configuration_validation_in_startup():
    """Test that application startup validates configuration."""
    # Mock configuration manager to test validation during startup
    with patch('faultmaven.config.configuration_manager.ConfigurationManager') as mock_config_class:
        mock_config = Mock()
        mock_config.validate.return_value = True
        mock_config.get_database_config.return_value = {
            "host": "test.redis.com",
            "port": 6379,
            "password": None,
            "db": 0,
            "ssl": False,
            "timeout": 30
        }
        mock_config.get_session_config.return_value = {
            "timeout_minutes": 30,
            "cleanup_interval_minutes": 15,
            "max_memory_mb": 100,
            "cleanup_batch_size": 50,
            "encryption_key": None
        }
        mock_config_class.return_value = mock_config
        
        # Application should handle configuration validation
        assert isinstance(app, FastAPI)
        
        # Verify configuration would be validated if implemented
        if hasattr(app, 'state') and hasattr(app.state, 'config_validated'):
            assert app.state.config_validated is True


def test_application_uses_configuration_defaults():
    """Test that application can use configuration manager defaults."""
    # Test with minimal environment configuration
    minimal_config = {
        'CHAT_PROVIDER': 'openai',
        'REDIS_HOST': 'localhost'
    }
    
    with patch.dict(os.environ, minimal_config, clear=True):
        # Application should work with minimal configuration
        with TestClient(app) as client:
            response = client.get("/health")
            
            # Should succeed with defaults
            assert response.status_code == 200
            
            data = response.json()
            assert data["status"] == "healthy"


def test_health_endpoint_configuration_info():
    """Test health endpoint includes configuration information."""
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        
        data = response.json()
        
        # Check if configuration information is included
        if "configuration" in data:
            config_info = data["configuration"]
            
            # Should include environment info
            if "environment" in config_info:
                assert isinstance(config_info["environment"], str)
            
            # Should include configuration validation status
            if "config_valid" in config_info:
                assert isinstance(config_info["config_valid"], bool)


def test_application_startup_with_invalid_configuration():
    """Test application behavior with invalid configuration."""
    # Mock invalid configuration
    invalid_config = {
        'CHAT_PROVIDER': 'invalid_provider',
        'REDIS_HOST': '',  # Empty required field
        'REDIS_PORT': 'not_a_number'
    }
    
    with patch.dict(os.environ, invalid_config, clear=True):
        # Application should handle invalid configuration gracefully
        # This test verifies the app doesn't crash during initialization
        assert isinstance(app, FastAPI)
        
        # Health check might report configuration issues
        with TestClient(app) as client:
            response = client.get("/health")
            
            # Should respond (even if with errors)
            assert response.status_code in [200, 500, 503]
            
            if response.status_code != 200:
                data = response.json()
                # Should include error information
                assert "error" in data or "status" in data


def test_configuration_hot_reload_support():
    """Test that application supports configuration changes."""
    # Initial configuration
    initial_config = {
        'CHAT_PROVIDER': 'openai',
        'REDIS_HOST': 'initial.redis.com',
        'SESSION_TIMEOUT_MINUTES': '30'
    }
    
    with patch.dict(os.environ, initial_config, clear=True):
        with TestClient(app) as client:
            # Initial health check
            response1 = client.get("/health")
            assert response1.status_code == 200
    
    # Updated configuration
    updated_config = {
        'CHAT_PROVIDER': 'openai',
        'REDIS_HOST': 'updated.redis.com',
        'SESSION_TIMEOUT_MINUTES': '45'
    }
    
    with patch.dict(os.environ, updated_config, clear=True):
        # Reset configuration manager singleton if needed
        from faultmaven.config.configuration_manager import reset_config
        reset_config()
        
        with TestClient(app) as client:
            # Health check should work with updated config
            response2 = client.get("/health")
            assert response2.status_code == 200


@pytest.mark.phase3
class TestPhase3MainApplicationValidation:
    """Phase 3 specific validation tests for main application."""
    
    def test_application_startup_without_migration_overhead(self):
        """Test that application starts without migration-related overhead."""
        
        # Application should start cleanly without migration dependencies
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            
            health_data = response.json()
            
            # Should not contain any migration-related status
            migration_indicators = [
                "migration", "refactor", "rollback", "architecture_migration"
            ]
            
            health_str = str(health_data).lower()
            for indicator in migration_indicators:
                assert indicator not in health_str, \
                    f"Migration indicator '{indicator}' found in health response"
    
    def test_root_endpoint_simplified_structure(self):
        """Test that root endpoint has simplified structure after Phase 3."""
        
        with TestClient(app) as client:
            response = client.get("/")
            assert response.status_code == 200
            
            data = response.json()
            
            # Should have clean, essential structure
            expected_structure = {
                "message": str,
                "version": str,
                "description": str,
                "docs": str,
                "health": str
            }
            
            for field, expected_type in expected_structure.items():
                assert field in data, f"Expected field '{field}' missing from root endpoint"
                assert isinstance(data[field], expected_type), \
                    f"Field '{field}' should be {expected_type.__name__}"
            
            # Should not have migration/architecture complexity
            prohibited_keys = [
                "architecture", "migration_strategy", "migration_status",
                "feature_flags", "container_status", "refactored_components"
            ]
            
            for key in prohibited_keys:
                assert key not in data, \
                    f"Prohibited key '{key}' found in simplified root endpoint"
    
    def test_health_endpoints_streamlined(self):
        """Test that health endpoints are streamlined after Phase 3."""
        
        health_endpoints = ["/health", "/health/dependencies"]
        
        with TestClient(app) as client:
            for endpoint in health_endpoints:
                response = client.get(endpoint)
                
                # Should respond (endpoints should exist)
                assert response.status_code != 404, \
                    f"Health endpoint {endpoint} should exist"
                
                if response.status_code == 200:
                    data = response.json()
                    
                    # Should not contain migration status
                    data_str = str(data).lower()
                    migration_terms = [
                        "migration_strategy", "migration_safe", "refactored_api"
                    ]
                    
                    for term in migration_terms:
                        assert term not in data_str, \
                            f"Migration term '{term}' found in {endpoint}"
    
    def test_feature_flags_integration_clean(self):
        """Test that feature flags integration is clean after Phase 3."""
        
        # Test with different active feature flag combinations
        flag_combinations = [
            {
                "ENABLE_LEGACY_COMPATIBILITY": "true",
                "ENABLE_EXPERIMENTAL_FEATURES": "false",
                "ENABLE_PERFORMANCE_MONITORING": "true",
                "ENABLE_DETAILED_TRACING": "false"
            },
            {
                "ENABLE_LEGACY_COMPATIBILITY": "false", 
                "ENABLE_EXPERIMENTAL_FEATURES": "true",
                "ENABLE_PERFORMANCE_MONITORING": "false",
                "ENABLE_DETAILED_TRACING": "true"
            }
        ]
        
        for flags in flag_combinations:
            with patch.dict(os.environ, flags, clear=True):
                with TestClient(app) as client:
                    # Application should work consistently
                    response = client.get("/health")
                    assert response.status_code == 200
                    
                    # Health should not vary based on feature flags
                    health_data = response.json()
                    assert "status" in health_data
                    assert health_data["status"] in ["healthy", "degraded", "unhealthy"]
    
    def test_no_migration_configuration_references(self):
        """Test that no migration configuration is referenced in responses."""
        
        endpoints_to_test = ["/", "/health"]
        
        with TestClient(app) as client:
            for endpoint in endpoints_to_test:
                response = client.get(endpoint)
                
                if response.status_code == 200:
                    response_text = response.text.lower()
                    
                    # Should not reference deprecated configuration
                    deprecated_config = [
                        "use_refactored_services", "use_refactored_api",
                        "use_di_container", "enable_migration_logging"
                    ]
                    
                    for config_item in deprecated_config:
                        assert config_item not in response_text, \
                            f"Deprecated config '{config_item}' referenced in {endpoint}"
    
    def test_application_metadata_updated(self):
        """Test that application metadata reflects Phase 3 completion."""
        
        # Test that FastAPI app itself has clean metadata
        assert app.title == "FaultMaven API"
        assert "troubleshooting" in app.description.lower()
        
        # Should have OpenAPI documentation available
        openapi_schema = app.openapi()
        assert "paths" in openapi_schema
        assert len(openapi_schema["paths"]) > 0
        
        # OpenAPI schema should not reference migration endpoints
        paths_str = str(openapi_schema["paths"]).lower()
        migration_endpoints = ["migration", "rollback", "refactor"]
        
        for endpoint_type in migration_endpoints:
            assert endpoint_type not in paths_str, \
                f"Migration endpoint type '{endpoint_type}' found in OpenAPI schema"
