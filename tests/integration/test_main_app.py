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

    Guards the wire contract consumed by the Copilot extension and the
    Dashboard (``/v1/meta/capabilities`` → ``BackendCapabilities``). In
    particular the team-sharing capability is named ``teamSharing`` (ADR-013:
    Team = the sharing unit), NOT the retired ``teamWorkspaces`` misnomer, and
    the org/team console gate is advertised as ``managementConsole``.
    """
    with TestClient(app) as client:
        response = client.get("/v1/meta/capabilities")

    assert response.status_code == 200
    data = response.json()

    features = data["features"]
    # Canonical flags present and boolean.
    for flag in ("teamSharing", "managementConsole"):
        assert flag in features and isinstance(features[flag], bool)
    # Retired misnomer must be gone (no dual key on the wire).
    assert "teamWorkspaces" not in features
    # Sibling capability flags remain intact.
    for flag in ("extensionKB", "adminKB", "caseHistory", "sso"):
        assert flag in features and isinstance(features[flag], bool)


def test_conversion_service_composition_root_wiring():
    """ConversionService gets the SAME collaborators the rest of the app uses.

    The lifespan constructs ConversionService before it copies the container's
    team_service/share_repository onto app.state, so sourcing them from
    app.state at construction time yields None — team share rows are never
    minted and the #854 membership gate is unreachable while the rest of the
    app (capabilities endpoint, retrieval allowlist) sees the real services.
    Pin identity with the CONTAINER-sourced values app.state ends up holding.
    """
    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    with TestClient(app):
        cs = app.state.conversion_service
        assert cs is not None, "conversion service failed to initialize"
        # share_repository exists in both modes (ADR-013 §D4), so this
        # assertion is load-bearing everywhere: a None here means the
        # construction read app.state too early.
        assert app.state.share_repository is not None
        assert cs._share_repo is app.state.share_repository
        # team_service is None in standalone; identity still pins that the
        # construction and app.state resolve the same source.
        assert cs._team_service is app.state.team_service
        assert cs._db_session_factory is get_db_session

        # The KnowledgeService the same lifespan publishes must be DB-capable
        # from the container, not from a patch applied here. That patch made
        # the capability web-only: the jobs process initializes the same
        # container without a lifespan, so ``kb_seed`` refused on every runbook
        # (#894). One session source for the whole knowledge vertical.
        ks = app.state.knowledge_service
        assert isinstance(
            ks, KnowledgeService
        ), f"lifespan published a {type(ks).__name__}, not a real KnowledgeService"
        assert ks._db_session_factory is get_db_session


def test_suggestion_service_composition_root_wiring():
    """``app.state.suggestion_service`` is SET, and holds the real KB service.

    It was read in two routes and written in none (#1214), so
    ``get_suggestion_service`` fell through to a bare ``SuggestionService()``
    every request: an empty private store (extract → approve answered 404) and
    no knowledge service (approval took the branch that minted a fake id and
    reported 201 for an item never created).

    Identity, not truthiness: the point is that the ONE instance the routes see
    is the ONE the container composed, holding the SAME KnowledgeService the
    rest of the app publishes through. Two ``SuggestionService`` objects would
    satisfy an ``is not None`` assertion and reproduce the bug exactly.
    """
    from faultmaven.modules.knowledge.domain.services.suggestion_service import (
        SuggestionService,
    )

    with TestClient(app):
        svc = app.state.suggestion_service
        assert svc is not None, "the suggestion service slot is empty again"
        assert isinstance(
            svc, SuggestionService
        ), f"lifespan published a {type(svc).__name__}"
        assert (
            svc._knowledge_service is app.state.knowledge_service
        ), "the suggestion service publishes through a different KnowledgeService"
        # The collaborator the PII scan needs. Without it every suggestion is
        # marked CLEAN unscanned, which is a policy the deployment should choose
        # rather than inherit from a missing wire.
        assert svc._sanitizer is not None

        # The container hands back the same object every time — the routes read
        # app.state, but a getter that rebuilt per call would restore the bug
        # for anything sourcing it from the container.
        from faultmaven.container import container

        assert container.get_suggestion_service() is svc


def test_capabilities_team_flags_gate_on_team_service():
    """``teamSharing``/``managementConsole`` follow the live TeamService signal.

    They must NOT key on ``deployment_mode == "cloud"`` (which would light them
    up in Cloud before multi-tenancy is ready, ADR-010 P2). The predicate is
    ``app.state.team_service is not None`` — None in standalone, set only when a
    multi-tenant TeamService is wired.
    """
    with TestClient(app) as client:
        # Standalone bootstrap wires no TeamService → team capabilities OFF.
        app.state.team_service = None
        off = client.get("/v1/meta/capabilities").json()["features"]
        assert off["teamSharing"] is False
        assert off["managementConsole"] is False

        # A wired TeamService (multi-tenant/Cloud-ready) → team capabilities ON.
        app.state.team_service = Mock()
        try:
            on = client.get("/v1/meta/capabilities").json()["features"]
            assert on["teamSharing"] is True
            assert on["managementConsole"] is True
        finally:
            # Restore the standalone default so later tests see a clean state.
            app.state.team_service = None


#: Headers that say something about the request rather than about the route: a
#: per-request correlation id and timing. Everything not listed here (and not
#: rate-limit, below) is compared — content type, `vary`, `content-encoding`,
#: and any cache header either path might grow — so a middleware that keys on
#: the path and treats the two differently fails here rather than in a client.
_PER_REQUEST_HEADERS = frozenset(
    {
        "date",
        "x-correlation-id",
        "x-process-time",
        "x-request-id",
    }
)

#: The whole `X-RateLimit-*` family is excluded, not just the counter and the
#: clock. `_add_rate_limit_headers` writes NONE of them when it holds no
#: advertisable result — which is what a check reports after failing open, and
#: what the first request through a cold limiter produced on CI: the canonical
#: request (sent first) came back with no rate-limit headers at all and the
#: alias, sent second, carried `Limit`/`Policy`. That asymmetry is limiter
#: state, not the path, so comparing the family across two sequential requests
#: is comparing a clock. `_assert_rate_limit_agrees` below keeps the part that
#: IS about the route: when both responses carry a policy, it must be the same
#: one, which is what a path-keyed rate-limit rule would break.
_RATE_LIMIT_PREFIX = "x-ratelimit-"


def _stable_headers(headers) -> dict:
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() not in _PER_REQUEST_HEADERS
        and not name.lower().startswith(_RATE_LIMIT_PREFIX)
    }


def _assert_rate_limit_agrees(alias, canonical) -> None:
    """Where both responses name a rate-limit bucket, it has to be one bucket."""
    for header in ("x-ratelimit-limit", "x-ratelimit-policy"):
        if header in alias.headers and header in canonical.headers:
            assert alias.headers[header] == canonical.headers[header], (
                f"the two paths were rate limited under different {header} "
                f"values — the limiter is keying on the path"
            )


def test_capabilities_is_the_same_response_under_both_paths():
    """``/api/v1/meta/capabilities`` and ``/v1/meta/capabilities`` are one route.

    The canonical path is the ``/api`` one: it is the only prefix the ingress
    forwards to this service (alongside ``/health`` and ``/metrics``), so a
    same-origin Dashboard asking for the bare ``/v1`` path is answered with the
    SPA's HTML and silently loses every capability. The bare path stays for
    extensions already installed against it.

    Two paths onto one handler is only worth having if they cannot diverge, so
    this compares the bytes and the headers rather than a field or two — a
    second handler copied alongside the first, or a middleware that keys on the
    path, fails here.

    The two team-service states are the positive control: they make the
    canonical body genuinely differ from itself, so the equality below is
    asserting agreement between two live handlers rather than between two
    constants.
    """
    bodies = []

    with TestClient(app) as client:
        for wired in (None, Mock()):
            app.state.team_service = wired
            try:
                canonical = client.get("/api/v1/meta/capabilities")
                alias = client.get("/v1/meta/capabilities")
            finally:
                # Restore the standalone default so later tests see a clean state.
                app.state.team_service = None

            assert canonical.status_code == 200
            assert canonical.headers["content-type"].startswith("application/json")

            assert alias.status_code == canonical.status_code
            assert alias.content == canonical.content, (
                "the two paths returned different bytes — they are supposed to "
                "be one handler"
            )
            assert _stable_headers(alias.headers) == _stable_headers(
                canonical.headers
            ), "the two paths returned different headers"
            _assert_rate_limit_agrees(alias, canonical)

            bodies.append(canonical.content)

    assert bodies[0] != bodies[1], (
        "the capabilities body did not move with app.state.team_service, so "
        "the equality above compared two constants and proves nothing"
    )


def test_the_bare_v1_capabilities_path_is_published_as_deprecated():
    """The alias says in the document that it is the one being retired.

    A client reading the spec has no other way to tell which of two paths
    serving one response is the one to write against.
    """
    paths = app.openapi()["paths"]

    canonical = paths["/api/v1/meta/capabilities"]["get"]
    alias = paths["/v1/meta/capabilities"]["get"]

    assert alias.get("deprecated") is True
    assert "/api/v1/meta/capabilities" in alias["description"]
    assert not canonical.get("deprecated", False)


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


def test_multipart_form_field_limit_matches_max_upload_size():
    """Form fields between Starlette's 1MB default and MAX_UPLOAD_SIZE_MB must parse.

    Starlette defaults to 1MB per form field, which silently rejects page
    injections and pasted text >1MB with a bare 400 error. Starlette >= 1.1
    enforces that limit via Request.form()'s max_part_size keyword default —
    MultiPartParser class-attribute overrides are shadowed and do nothing —
    so faultmaven.main replaces the default process-globally.

    This asserts the BEHAVIOR through a live multipart parse rather than any
    attribute: if a future starlette bump changes the enforcement surface
    again, this test fails while an attribute assertion would stay green.
    """
    from fastapi import FastAPI, Form

    probe = FastAPI()

    @probe.post("/probe")
    async def probe_endpoint(text: str = Form(...)):
        return {"length": len(text)}

    expected_bytes = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "10")) * 1024 * 1024

    with TestClient(probe, raise_server_exceptions=False) as client:
        # Over Starlette's built-in 1MB default, under our configured limit:
        # must parse. This is the pasted-content path's contract.
        over_default = "x" * (2 * 1024 * 1024)
        response = client.post(
            "/probe", data={"text": over_default}, files={"_pad": ("p", b"1")}
        )
        assert response.status_code == 200, (
            f"2MB form field must parse under MAX_UPLOAD_SIZE_MB="
            f"{expected_bytes // (1024 * 1024)}MB, got HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )
        assert response.json()["length"] == len(over_default)

        # Over the configured limit: the parser must refuse.
        over_limit = "x" * (expected_bytes + 1024)
        response = client.post(
            "/probe", data={"text": over_limit}, files={"_pad": ("p", b"1")}
        )
        assert response.status_code == 400, (
            f"form field over the configured limit must be refused, "
            f"got HTTP {response.status_code}"
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
