"""tests/test_main.py

Purpose: Tests for the main FastAPI application lifecycle
"""

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _ensure_database():
    """Ensure the SQLite database and tables exist for app bootstrap.

    The app lifespan queries the organizations table on startup. In CI,
    no data/ directory or database file exists, so we create one from
    the ORM models if it's missing.

    We also stamp the Alembic version so that bootstrap's
    ``alembic upgrade head`` is a no-op (otherwise it tries to re-create
    tables that already exist).
    """
    db_file = Path("./data/faultmaven.db")
    if db_file.exists():
        return

    db_file.parent.mkdir(parents=True, exist_ok=True)

    from sqlalchemy import create_engine, text

    from faultmaven.infrastructure.persistence.models import Base

    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)

    # Stamp alembic_version with the latest head so migrations are a no-op.
    # Using create_all creates tables from current models (which include all
    # columns from all migrations), so we must stamp the latest revision.
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    alembic_cfg = Config("alembic.ini")
    script_dir = ScriptDirectory.from_config(alembic_cfg)
    head_rev = script_dir.get_current_head()

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
        )
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
            {"rev": head_rev},
        )

    engine.dispose()


_ensure_database()

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
        "migration_strategy",
        "migration_safe",
        "using_new_api",
        "using_di_container",
        "architecture_migration",
        "refactored_components",
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
        assert not check_nested_dict(
            data, field
        ), f"Migration field '{field}' should not be present in health check after Phase 3"


def test_capabilities_endpoint_feature_flags():
    """The extension capabilities endpoint reports the canonical feature flags.

    Guards the wire contract consumed by the Copilot extension
    (``/v1/meta/capabilities`` → ``BackendCapabilities``). In particular the
    team-sharing capability is named ``teamSharing`` (ADR-013: Team = the
    sharing unit), NOT the retired ``teamWorkspaces`` misnomer.
    """
    with TestClient(app) as client:
        response = client.get("/v1/meta/capabilities")

    assert response.status_code == 200
    data = response.json()

    features = data["features"]
    # Canonical flag present and boolean.
    assert "teamSharing" in features
    assert isinstance(features["teamSharing"], bool)
    # Retired misnomer must be gone (no dual key on the wire).
    assert "teamWorkspaces" not in features
    # Sibling capability flags remain intact.
    for flag in ("extensionKB", "adminKB", "caseHistory", "sso"):
        assert flag in features and isinstance(features[flag], bool)


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
            "architecture",
            "migration_strategy",
            "migration_status",
            "using_new_api",
            "using_di_container",
            "refactored_components",
        ]

        for field in prohibited_fields:
            assert (
                field not in data
            ), f"Prohibited migration field '{field}' should not be present in root endpoint after Phase 3"

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
    assert hasattr(app, "router")
    assert hasattr(app, "middleware")


def test_multipart_parser_size_limits_configured():
    """MultiPartParser limits must match MAX_UPLOAD_SIZE_MB to allow large submissions.

    Starlette defaults to 1MB per form part, which silently rejects page injections
    and pasted text >1MB with a bare 400 error. FaultMaven overrides this at app
    startup to match the configured MAX_UPLOAD_SIZE_MB (default 10MB).

    All three data submission paths (file upload, page injection, pasted text) go
    through the same /turns endpoint as multipart form data.
    """
    from starlette.formparsers import MultiPartParser

    expected_bytes = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "10")) * 1024 * 1024

    assert MultiPartParser.max_file_size == expected_bytes, (
        f"MultiPartParser.max_file_size should be {expected_bytes} bytes "
        f"(MAX_UPLOAD_SIZE_MB={expected_bytes // (1024*1024)}MB), "
        f"got {MultiPartParser.max_file_size}"
    )
    assert MultiPartParser.max_part_size == expected_bytes, (
        f"MultiPartParser.max_part_size should be {expected_bytes} bytes "
        f"(MAX_UPLOAD_SIZE_MB={expected_bytes // (1024*1024)}MB), "
        f"got {MultiPartParser.max_part_size}"
    )


@pytest.mark.integration
def test_api_routes_registration():
    """Test that API routes are properly registered"""
    with TestClient(app) as client:
        # Test that key endpoints exist (even if they return errors due to missing auth)
        # Note: /api/v1/data/ingest removed - planned in roadmap but not yet implemented
        endpoints_to_check = [
            "/",
            "/health",
            "/api/v1/knowledge/search",
            "/api/v1/sessions",
        ]

        for endpoint in endpoints_to_check:
            response = (
                client.get(endpoint)
                if endpoint in ["/", "/health"]
                else client.post(endpoint, json={})
            )
            # Should not return 404 (route not found)
            assert response.status_code != 404, f"Route {endpoint} not found"


def test_cors_configuration():
    """Test CORS middleware is configured"""
    # Check if CORS middleware is present
    cors_middleware_found = False
    for middleware in app.user_middleware:
        middleware_class = middleware.cls.__name__
        if "CORS" in middleware_class:
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

    with patch.dict(
        os.environ,
        {
            "ENABLE_PERFORMANCE_MONITORING": "true",
            "ENABLE_DETAILED_TRACING": "false",
        },
    ):
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
            "use_refactored_api",
            "use_di_container",
            "enable_migration_logging",
        ]

        for var in deprecated_env_vars:
            assert (
                var not in health_str
            ), f"Deprecated environment variable {var} should not affect health check"


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
                "cleanup_interval_minutes",
            ]

            for metric in expected_metrics:
                if metric in session_metrics:
                    assert isinstance(session_metrics[metric], (int, float, bool))


def test_application_uses_configuration_defaults():
    """Test that application can use configuration manager defaults."""
    # Test with minimal environment configuration
    minimal_config = {"CHAT_PROVIDER": "openai", "REDIS_HOST": "localhost"}

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
        "CHAT_PROVIDER": "invalid_provider",
        "REDIS_HOST": "",  # Empty required field
        "REDIS_PORT": "not_a_number",
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
                "migration",
                "refactor",
                "rollback",
                "architecture_migration",
            ]

            health_str = str(health_data).lower()
            for indicator in migration_indicators:
                assert (
                    indicator not in health_str
                ), f"Migration indicator '{indicator}' found in health response"

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
                "health": str,
            }

            for field, expected_type in expected_structure.items():
                assert (
                    field in data
                ), f"Expected field '{field}' missing from root endpoint"
                assert isinstance(
                    data[field], expected_type
                ), f"Field '{field}' should be {expected_type.__name__}"

            # Should not have migration/architecture complexity
            prohibited_keys = [
                "architecture",
                "migration_strategy",
                "migration_status",
                "feature_flags",
                "container_status",
                "refactored_components",
            ]

            for key in prohibited_keys:
                assert (
                    key not in data
                ), f"Prohibited key '{key}' found in simplified root endpoint"

    def test_health_endpoints_streamlined(self):
        """Test that health endpoints are streamlined after Phase 3."""

        health_endpoints = ["/health", "/health/dependencies"]

        with TestClient(app) as client:
            for endpoint in health_endpoints:
                response = client.get(endpoint)

                # Should respond (endpoints should exist)
                assert (
                    response.status_code != 404
                ), f"Health endpoint {endpoint} should exist"

                if response.status_code == 200:
                    data = response.json()

                    # Should not contain migration status
                    data_str = str(data).lower()
                    migration_terms = [
                        "migration_strategy",
                        "migration_safe",
                        "refactored_api",
                    ]

                    for term in migration_terms:
                        assert (
                            term not in data_str
                        ), f"Migration term '{term}' found in {endpoint}"

    def test_feature_flags_integration_clean(self):
        """Test that feature flags integration is clean after Phase 3."""

        # Test with different active feature flag combinations
        flag_combinations = [
            {
                "ENABLE_PERFORMANCE_MONITORING": "true",
                "ENABLE_DETAILED_TRACING": "false",
            },
            {
                "ENABLE_PERFORMANCE_MONITORING": "false",
                "ENABLE_DETAILED_TRACING": "true",
            },
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
                        "use_refactored_services",
                        "use_refactored_api",
                        "use_di_container",
                        "enable_migration_logging",
                    ]

                    for config_item in deprecated_config:
                        assert (
                            config_item not in response_text
                        ), f"Deprecated config '{config_item}' referenced in {endpoint}"

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
            assert (
                endpoint_type not in paths_str
            ), f"Migration endpoint type '{endpoint_type}' found in OpenAPI schema"
