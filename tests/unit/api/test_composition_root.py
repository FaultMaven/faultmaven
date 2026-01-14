"""Tests for Composition Root Pattern (Gap P5-G1 Fix)

Purpose: Validate that:
1. Services are attached to app.state in main.py (Composition Root)
2. FastAPI dependencies correctly retrieve services from request.app.state
3. No Service Locator anti-pattern (container.get_*) in API layer

This addresses Gap P5-G1: Service Locator Anti-Pattern
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


class TestCompositionRootPattern:
    """Test that services are wired via Composition Root, not Service Locator"""

    def test_app_state_has_services_after_startup(self):
        """Test that app.state has services attached after startup"""
        from faultmaven.main import app

        with TestClient(app) as client:
            # After startup, app.state should have services
            # Check a sample of critical services
            # Note: Some services may be None if container initialization fails
            # in test environment, but the attribute should exist
            expected_services = [
                "session_service",
                "case_service",
                "knowledge_service",
            ]

            for service_name in expected_services:
                # Just check the attribute exists (may be None in test env)
                assert hasattr(
                    app.state, service_name
                ), f"app.state should have {service_name} attribute after startup"

    def test_dependency_uses_app_state_not_container(self):
        """Test that dependencies use request.app.state, not container.get_*"""
        from faultmaven.api.v1.dependencies import get_session_service
        import inspect

        # Get source code of the dependency function
        source = inspect.getsource(get_session_service)

        # Should NOT contain container.get_*
        assert (
            "container.get_" not in source
        ), "get_session_service should not use container.get_* (Service Locator)"

        # SHOULD contain request.app.state
        assert (
            "request.app.state" in source or "app.state" in source
        ), "get_session_service should use request.app.state (Composition Root)"

    def test_auth_dependency_uses_app_state(self):
        """Test that auth dependencies use request.app.state"""
        from faultmaven.api.v1.auth_dependencies import get_token_manager
        import inspect

        source = inspect.getsource(get_token_manager)

        assert (
            "container.get_" not in source
        ), "get_token_manager should not use container.get_*"
        assert (
            "request.app.state" in source or "app.state" in source
        ), "get_token_manager should use request.app.state"

    def test_middleware_auth_uses_app_state(self):
        """Test that middleware auth uses app.state"""
        from faultmaven.api.middleware.auth import get_auth_service
        import inspect

        source = inspect.getsource(get_auth_service)

        assert (
            "container.get_" not in source
        ), "get_auth_service should not use container.get_*"
        assert (
            "request.app.state" in source or "app.state" in source
        ), "get_auth_service should use request.app.state"


class TestDependencyFunctionSignatures:
    """Test that dependency functions have correct signatures for Composition Root"""

    def test_dependencies_accept_request_parameter(self):
        """Test that key dependencies accept Request parameter"""
        from faultmaven.api.v1 import dependencies
        import inspect

        # These functions should accept Request parameter
        dependency_functions = [
            "get_session_service",
            "get_case_service",
            "get_knowledge_service",
            "get_evidence_service",
        ]

        for func_name in dependency_functions:
            if hasattr(dependencies, func_name):
                func = getattr(dependencies, func_name)
                sig = inspect.signature(func)
                params = list(sig.parameters.keys())

                assert (
                    "request" in params
                ), f"{func_name} should have 'request' parameter for Composition Root pattern"

    def test_auth_dependencies_accept_request_parameter(self):
        """Test that auth dependencies accept Request parameter"""
        from faultmaven.api.v1 import auth_dependencies
        import inspect

        # These functions should accept Request parameter
        auth_dependency_functions = [
            "get_token_manager",
            "get_user_store",
        ]

        for func_name in auth_dependency_functions:
            if hasattr(auth_dependencies, func_name):
                func = getattr(auth_dependencies, func_name)
                sig = inspect.signature(func)
                params = list(sig.parameters.keys())

                assert (
                    "request" in params
                ), f"{func_name} should have 'request' parameter"


class TestNoServiceLocatorInApiLayer:
    """Test that API layer does not use Service Locator anti-pattern"""

    def test_dependencies_file_no_container_imports(self):
        """Test that dependencies.py doesn't import container for Service Locator"""
        from faultmaven.api.v1 import dependencies
        import inspect

        source = inspect.getsource(dependencies)

        # Should not have container.get_* calls in actual code
        # Skip comments, docstrings, and strings
        lines = source.split("\n")
        in_docstring = False

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Track docstring state
            if '"""' in stripped or "'''" in stripped:
                # Toggle docstring state (simple heuristic)
                if stripped.count('"""') == 1 or stripped.count("'''") == 1:
                    in_docstring = not in_docstring
                continue

            if in_docstring:
                continue

            # Skip comments
            if stripped.startswith("#"):
                continue

            # Skip if container.get_ is in a comment at end of line
            code_part = line.split("#")[0] if "#" in line else line

            # Check for actual Service Locator calls (not in strings)
            if "container.get_" in code_part:
                # Skip if it's in a string (basic check)
                before = code_part.split("container.get_")[0]
                # Count quotes - odd number means we're inside a string
                if before.count('"') % 2 == 1 or before.count("'") % 2 == 1:
                    continue
                pytest.fail(
                    f"Line {i+1} contains Service Locator pattern: {line.strip()}"
                )

    def test_module_routes_no_container_imports(self):
        """Test that module routes don't use container.get_*"""
        import os
        import glob

        module_routes = glob.glob("faultmaven/modules/*/api/routes.py") + glob.glob(
            "faultmaven/modules/*/api/*.py"
        )

        for route_file in module_routes:
            if os.path.exists(route_file):
                with open(route_file, "r") as f:
                    content = f.read()

                # Check for Service Locator calls (not in comments)
                lines = content.split("\n")
                for i, line in enumerate(lines):
                    if line.strip().startswith("#"):
                        continue
                    if "container.get_" in line:
                        # Check if it's in a string or comment
                        code_part = line.split("#")[0] if "#" in line else line
                        if (
                            "container.get_" in code_part
                            and '"' not in code_part
                            and "'" not in code_part
                        ):
                            pytest.fail(
                                f"{route_file}:{i+1} uses Service Locator: {line.strip()}"
                            )


class TestCompositionRootIntegration:
    """Integration tests for Composition Root pattern"""

    def test_service_accessible_via_endpoint(self):
        """Test that services are accessible via endpoints"""
        from faultmaven.main import app

        with TestClient(app) as client:
            # Health endpoint should work, indicating services are wired
            response = client.get("/health")
            assert response.status_code == 200

            # The health check validates that services are available
            data = response.json()
            assert "services" in data

    @pytest.mark.asyncio
    async def test_mock_dependency_injection(self):
        """Test that we can mock services via app.state"""
        app = FastAPI()

        # Simulate Composition Root pattern
        mock_service = Mock()
        mock_service.get_data.return_value = {"test": "data"}
        app.state.test_service = mock_service

        # Create a dependency that uses app.state
        async def get_test_service(request: Request):
            return request.app.state.test_service

        # Simulate request context
        mock_request = Mock(spec=Request)
        mock_request.app = app

        # Test dependency resolution
        service = await get_test_service(mock_request)

        assert service is mock_service
        assert service.get_data() == {"test": "data"}


@pytest.mark.architecture
class TestArchitecturalCompliance:
    """Test architectural compliance with Composition Root pattern"""

    def test_no_service_locator_antipattern(self):
        """Verify no Service Locator anti-pattern in key files"""
        import os

        # Files that should NOT have container.get_* (except in comments/docstrings)
        files_to_check = [
            "faultmaven/api/v1/dependencies.py",
            "faultmaven/api/v1/auth_dependencies.py",
            "faultmaven/api/middleware/auth.py",
            "faultmaven/api/routes/auth.py",
        ]

        violations = []

        for filepath in files_to_check:
            if os.path.exists(filepath):
                with open(filepath, "r") as f:
                    content = f.read()
                    lines = content.split("\n")

                in_docstring = False

                for i, line in enumerate(lines):
                    stripped = line.strip()

                    # Track docstring state
                    if '"""' in stripped or "'''" in stripped:
                        if stripped.count('"""') == 1 or stripped.count("'''") == 1:
                            in_docstring = not in_docstring
                        continue

                    if in_docstring:
                        continue

                    # Skip comments
                    if stripped.startswith("#"):
                        continue

                    # Check for container.get_* pattern in code
                    code_part = line.split("#")[0] if "#" in line else line
                    if "container.get_" in code_part:
                        # Skip if it's in a string
                        before = code_part.split("container.get_")[0]
                        if before.count('"') % 2 == 1 or before.count("'") % 2 == 1:
                            continue
                        violations.append(f"{filepath}:{i+1}: {line.strip()}")

        if violations:
            pytest.fail(
                f"Found Service Locator anti-pattern in {len(violations)} locations:\n"
                + "\n".join(violations)
            )

    def test_main_py_wires_services_to_app_state(self):
        """Test that main.py wires services to app.state"""
        with open("faultmaven/main.py", "r") as f:
            content = f.read()

        # Should have app.state assignments
        assert "app.state." in content, "main.py should wire services to app.state"

        # Should have Composition Root comment or pattern
        assert (
            "Composition Root" in content or "app.state" in content
        ), "main.py should implement Composition Root pattern"
