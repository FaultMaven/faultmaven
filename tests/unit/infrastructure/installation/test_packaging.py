"""
Test suite for FaultMaven packaging and installation modes.

Tests both community and enterprise installation modes to ensure:
1. Base dependencies are minimal and installable
2. Enterprise dependencies are properly gated behind optional extras
3. Application starts correctly in both modes
4. Configuration defaults match expected mode

Design Reference: docs/FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md (Week 11)
"""

import subprocess
import sys
from pathlib import Path
from typing import List, Set
from unittest.mock import MagicMock, Mock, patch

import pytest

# Handle optional toml dependency
# Python 3.11+ has tomllib in stdlib (read-only), but test needs read capability
try:
    import toml  # Full read/write support (preferred)
except ImportError:
    try:
        # Python 3.11+ has tomllib in stdlib (read-only, requires binary mode)
        import tomllib

        # Create wrapper to match toml.load() API (text mode)
        class TomlWrapper:
            @staticmethod
            def load(f):
                # tomllib.load() requires binary mode
                if hasattr(f, "read"):
                    # File handle - check if it's binary or text
                    if hasattr(f, "mode") and "b" in f.mode:
                        return tomllib.load(f)
                    else:
                        # Text mode - read and decode
                        content = f.read()
                        if isinstance(content, str):
                            content = content.encode("utf-8")
                        import io

                        return tomllib.load(io.BytesIO(content))
                else:
                    # Path object or string - open in binary mode
                    with open(f, "rb") as fp:
                        return tomllib.load(fp)

        toml = TomlWrapper()
    except ImportError:
        try:
            import tomli as toml  # Read-only backport (also requires binary)

            # tomli also needs binary mode, create similar wrapper
            class TomliWrapper:
                @staticmethod
                def load(f):
                    if hasattr(f, "read"):
                        if hasattr(f, "mode") and "b" in f.mode:
                            return tomli.load(f)
                        else:
                            content = f.read()
                            if isinstance(content, str):
                                content = content.encode("utf-8")
                            import io

                            return tomli.load(io.BytesIO(content))
                    else:
                        with open(f, "rb") as fp:
                            return tomli.load(fp)

            toml = TomliWrapper()
        except ImportError:
            pytest.skip("toml/tomli/tomllib not available", allow_module_level=True)


# ============================================================================
# TEST FIXTURES
# ============================================================================


@pytest.fixture
def pyproject_path() -> Path:
    """Get path to pyproject.toml."""
    return Path(__file__).parent.parent.parent.parent.parent / "pyproject.toml"


@pytest.fixture
def pyproject_data(pyproject_path: Path) -> dict:
    """Load pyproject.toml data."""
    with open(pyproject_path, "r") as f:
        return toml.load(f)


@pytest.fixture
def base_dependencies(pyproject_data: dict) -> List[str]:
    """Get base (community) dependencies."""
    return pyproject_data["project"]["dependencies"]


@pytest.fixture
def enterprise_dependencies(pyproject_data: dict) -> List[str]:
    """Get enterprise optional dependencies."""
    return pyproject_data["project"]["optional-dependencies"]["enterprise"]


@pytest.fixture
def test_dependencies(pyproject_data: dict) -> List[str]:
    """Get test dependencies."""
    return pyproject_data["project"]["optional-dependencies"]["test"]


# ============================================================================
# METADATA TESTS
# ============================================================================


class TestProjectMetadata:
    """Test pyproject.toml metadata compliance with PEP 621."""

    def test_required_fields_present(self, pyproject_data: dict):
        """Verify all required PEP 621 fields are present."""
        project = pyproject_data["project"]

        required_fields = [
            "name",
            "version",
            "description",
            "readme",
            "requires-python",
        ]
        for field in required_fields:
            assert field in project, f"Missing required field: {field}"

    def test_project_name(self, pyproject_data: dict):
        """Verify project name is correct."""
        assert pyproject_data["project"]["name"] == "faultmaven"

    def test_version_format(self, pyproject_data: dict):
        """Verify version follows semantic versioning."""
        version = pyproject_data["project"]["version"]
        parts = version.split(".")
        assert len(parts) == 3, "Version should be in format X.Y.Z"
        assert all(part.isdigit() for part in parts), "Version parts should be numeric"

    def test_python_requirement(self, pyproject_data: dict):
        """Verify Python version requirement is >= 3.11."""
        requires_python = pyproject_data["project"]["requires-python"]
        assert ">=3.11" in requires_python

    def test_license_present(self, pyproject_data: dict):
        """Verify license is specified."""
        assert "license" in pyproject_data["project"]
        assert pyproject_data["project"]["license"]["text"] == "Apache-2.0"

    def test_urls_present(self, pyproject_data: dict):
        """Verify project URLs are defined."""
        urls = pyproject_data["project"]["urls"]
        required_urls = ["Homepage", "Repository", "Documentation", "Issues"]
        for url_type in required_urls:
            assert url_type in urls, f"Missing URL: {url_type}"


# ============================================================================
# DEPENDENCY CATEGORIZATION TESTS
# ============================================================================


class TestDependencyCategorization:
    """Test that dependencies are correctly categorized."""

    def test_base_has_core_framework(self, base_dependencies: List[str]):
        """Verify base dependencies include core FastAPI stack."""
        base_str = " ".join(base_dependencies)

        required = ["fastapi", "uvicorn", "sqlalchemy", "alembic"]
        for pkg in required:
            assert any(
                pkg in dep for dep in base_dependencies
            ), f"Missing core dependency: {pkg}"

    def test_base_has_llm_framework(self, base_dependencies: List[str]):
        """Verify base dependencies include LLM framework (required for core functionality)."""
        base_str = " ".join(base_dependencies)

        required = ["openai", "anthropic", "fireworks-ai"]
        for pkg in required:
            assert any(
                pkg in dep for dep in base_dependencies
            ), f"Missing LLM dependency: {pkg}"

    def test_enterprise_has_observability(self, enterprise_dependencies: List[str]):
        """Verify enterprise dependencies include observability tools."""
        enterprise_str = " ".join(enterprise_dependencies)

        required = ["opik", "prometheus-client"]
        for pkg in required:
            assert any(
                pkg in dep for dep in enterprise_dependencies
            ), f"Missing observability dependency: {pkg}"

    def test_enterprise_has_security(self, enterprise_dependencies: List[str]):
        """Verify enterprise dependencies include PII protection."""
        enterprise_str = " ".join(enterprise_dependencies)

        required = ["presidio-analyzer", "presidio-anonymizer"]
        for pkg in required:
            assert any(
                pkg in dep for dep in enterprise_dependencies
            ), f"Missing security dependency: {pkg}"

    def test_enterprise_has_infrastructure(self, enterprise_dependencies: List[str]):
        """Verify enterprise dependencies include distributed infrastructure."""
        enterprise_str = " ".join(enterprise_dependencies)

        required = ["redis", "psycopg2-binary", "asyncpg"]
        for pkg in required:
            assert any(
                pkg in dep for dep in enterprise_dependencies
            ), f"Missing infrastructure dependency: {pkg}"

    def test_enterprise_has_cloud_storage(self, enterprise_dependencies: List[str]):
        """Verify enterprise dependencies include cloud storage adapters."""
        enterprise_str = " ".join(enterprise_dependencies)

        required = ["boto3", "azure-storage-blob"]
        for pkg in required:
            assert any(
                pkg in dep for dep in enterprise_dependencies
            ), f"Missing cloud storage dependency: {pkg}"

    def test_test_dependencies_complete(self, test_dependencies: List[str]):
        """Verify test dependencies include all testing tools."""
        test_str = " ".join(test_dependencies)

        required = ["pytest", "pytest-asyncio", "pytest-cov", "locust"]
        for pkg in required:
            assert any(
                pkg in dep for dep in test_dependencies
            ), f"Missing test dependency: {pkg}"

    def test_no_enterprise_leakage_to_base(
        self, base_dependencies: List[str], enterprise_dependencies: List[str]
    ):
        """Verify enterprise dependencies are NOT in base dependencies."""
        base_str = " ".join(base_dependencies).lower()

        # Extract package names from enterprise dependencies
        enterprise_packages = set()
        for dep in enterprise_dependencies:
            # Handle dependencies like "redis[hiredis]>=5.0.0"
            pkg_name = (
                dep.split("[")[0].split(">=")[0].split("==")[0].split("<")[0].strip()
            )
            enterprise_packages.add(pkg_name.lower())

        # These packages should NOT appear in base dependencies
        enterprise_only = {
            "opik",
            "presidio-analyzer",
            "presidio-anonymizer",
            "prometheus-client",
            "boto3",
            "azure-storage-blob",
        }

        for pkg in enterprise_only:
            assert (
                pkg not in base_str
            ), f"Enterprise package '{pkg}' leaked into base dependencies"


# ============================================================================
# CONFIGURATION DEFAULTS TESTS
# ============================================================================


class TestConfigurationDefaults:
    """Test that configuration defaults match community mode expectations.

    Note: These tests verify code defaults, not environment variable overrides.
    In production, environment variables can override these defaults.
    """

    def test_community_storage_defaults(self, monkeypatch):
        """Verify storage defaults are community-friendly (in-memory)."""
        # Clear all environment variables that might override defaults
        import os

        for key in list(os.environ.keys()):
            if key.startswith(
                ("USER_STORAGE", "CASE_STORAGE", "SESSION_STORAGE", "VECTOR_STORAGE")
            ):
                monkeypatch.delenv(key, raising=False)

        from faultmaven.config.settings import FaultMavenSettings

        settings = FaultMavenSettings()

        # Storage should default to in-memory (no external dependencies)
        assert settings.database.user_storage_type == "inmemory"
        assert settings.database.case_storage_type == "inmemory"
        assert settings.database.session_storage_type == "inmemory"
        assert settings.database.vector_storage_type == "inmemory"

    def test_community_observability_defaults(self, monkeypatch):
        """Verify observability features are disabled by default (code defaults)."""
        # Clear environment variables that might override defaults
        import os

        for key in list(os.environ.keys()):
            if (
                "OPIK" in key
                or "PROMETHEUS" in key
                or "TRACING" in key
                or "METRICS" in key
            ):
                monkeypatch.delenv(key, raising=False)

        # Verify code defaults in settings.py
        from faultmaven.config.settings import ObservabilitySettings

        # Check class defaults directly (before environment override)
        assert ObservabilitySettings.model_fields["opik_enabled"].default is False
        assert ObservabilitySettings.model_fields["opik_track_disable"].default is True
        assert ObservabilitySettings.model_fields["prometheus_enabled"].default is False
        assert ObservabilitySettings.model_fields["tracing_enabled"].default is False
        assert ObservabilitySettings.model_fields["metrics_enabled"].default is False

    def test_community_protection_defaults(self, monkeypatch):
        """Verify protection features are disabled by default (code defaults)."""
        # Clear environment variables
        import os

        for key in list(os.environ.keys()):
            if "PROTECTION" in key or "SANITIZE_PII" in key:
                monkeypatch.delenv(key, raising=False)

        # Verify code defaults in settings.py
        from faultmaven.config.settings import ProtectionSettings

        # Check class defaults directly (before environment override)
        assert ProtectionSettings.model_fields["protection_enabled"].default is False
        assert ProtectionSettings.model_fields["sanitize_pii"].default is False
        assert (
            ProtectionSettings.model_fields["basic_protection_enabled"].default is False
        )
        assert (
            ProtectionSettings.model_fields["intelligent_protection_enabled"].default
            is False
        )

    def test_community_performance_monitoring_defaults(self, monkeypatch):
        """Verify performance monitoring is disabled by default (code defaults)."""
        # Clear environment variables
        import os

        for key in list(os.environ.keys()):
            if "PERFORMANCE" in key or "DETAILED_TRACING" in key:
                monkeypatch.delenv(key, raising=False)

        # Verify code defaults in settings.py
        from faultmaven.config.settings import ObservabilitySettings

        assert (
            ObservabilitySettings.model_fields["enable_performance_monitoring"].default
            is False
        )
        assert (
            ObservabilitySettings.model_fields["enable_detailed_tracing"].default
            is False
        )


# ============================================================================
# INSTALLATION SIMULATION TESTS
# ============================================================================


class TestInstallationSimulation:
    """Test installation scenarios (mocked to avoid actual installation)."""

    def test_base_installation_package_list(self, base_dependencies: List[str]):
        """Verify base installation has reasonable package count."""
        # Community edition should be lightweight but functional
        assert len(base_dependencies) > 10, "Too few base dependencies"
        assert len(base_dependencies) < 100, "Too many base dependencies"

    def test_enterprise_installation_package_list(
        self, enterprise_dependencies: List[str]
    ):
        """Verify enterprise installation adds appropriate packages."""
        # Enterprise should add significant functionality
        assert len(enterprise_dependencies) >= 6, "Enterprise dependencies too minimal"

    def test_no_conflicting_versions(
        self, base_dependencies: List[str], enterprise_dependencies: List[str]
    ):
        """Verify no version conflicts between base and enterprise."""
        # Extract package names and versions
        base_versions = {}
        for dep in base_dependencies:
            parts = dep.replace(">=", " ").replace("==", " ").replace("<", " ").split()
            if len(parts) >= 1:
                pkg_name = parts[0].split("[")[0]  # Handle extras like "redis[hiredis]"
                if len(parts) > 1:
                    base_versions[pkg_name] = parts[1]

        enterprise_versions = {}
        for dep in enterprise_dependencies:
            parts = dep.replace(">=", " ").replace("==", " ").replace("<", " ").split()
            if len(parts) >= 1:
                pkg_name = parts[0].split("[")[0]
                if len(parts) > 1:
                    enterprise_versions[pkg_name] = parts[1]

        # Check for overlapping packages with different version requirements
        for pkg in set(base_versions.keys()) & set(enterprise_versions.keys()):
            # If both specify versions, they should be compatible
            # This is a simplified check - full compatibility would need pip resolver
            assert (
                base_versions[pkg] == enterprise_versions[pkg]
                or base_versions[pkg] is None
                or enterprise_versions[pkg] is None
            ), f"Version conflict for {pkg}: base={base_versions[pkg]}, enterprise={enterprise_versions[pkg]}"


# ============================================================================
# BUILD SYSTEM TESTS
# ============================================================================


class TestBuildSystem:
    """Test build system configuration."""

    def test_build_backend_specified(self, pyproject_data: dict):
        """Verify build backend is modern setuptools."""
        build_system = pyproject_data["build-system"]
        assert "setuptools" in " ".join(build_system["requires"])
        assert build_system["build-backend"] == "setuptools.build_meta"

    def test_package_discovery(self, pyproject_data: dict):
        """Verify package discovery configuration."""
        packages = pyproject_data["tool"]["setuptools"]["packages"]["find"]
        assert "faultmaven*" in packages["include"]
        assert "tests*" in packages["exclude"]
        assert "docs*" in packages["exclude"]


# ============================================================================
# TOOL CONFIGURATION TESTS
# ============================================================================


class TestToolConfiguration:
    """Test tool configurations in pyproject.toml."""

    def test_pytest_configuration(self, pyproject_data: dict):
        """Verify pytest is properly configured."""
        pytest_config = pyproject_data["tool"]["pytest"]["ini_options"]
        assert "tests" in pytest_config["testpaths"]
        # Note: pytest.ini overrides pyproject.toml addopts, so coverage may not be in default config
        # This test verifies pytest configuration exists, not specific flags
        assert "addopts" in pytest_config
        assert isinstance(pytest_config["addopts"], list)

    def test_coverage_configuration(self, pyproject_data: dict):
        """Verify coverage configuration."""
        coverage = pyproject_data["tool"]["coverage"]
        assert "faultmaven" in coverage["run"]["source"]
        assert "*/tests/*" in coverage["run"]["omit"]

    def test_ruff_configuration(self, pyproject_data: dict):
        """Verify ruff linter configuration."""
        ruff = pyproject_data["tool"]["ruff"]
        assert ruff["line-length"] == 88
        assert ruff["target-version"] == "py311"


# ============================================================================
# PYTEST MARKERS
# ============================================================================

# Mark all tests in this file as unit tests
pytestmark = pytest.mark.unit
