"""Unit tests for DeploymentProfile and ProfileManager.

Tests:
- Profile detection from environment
- Feature matrix queries
- Profile validation
- Provider selection based on profile

Design Reference: Phase 3, Week 14-15 - Deployment Profile Pattern
"""

import os
import pytest
from unittest.mock import patch

from faultmaven.config.deployment_profile import (
    DeploymentProfile,
    ProfileManager,
    FEATURE_MATRIX,
)


class TestDeploymentProfile:
    """Test DeploymentProfile enum."""

    def test_profile_values(self):
        """Test that profile enum has correct values."""
        assert DeploymentProfile.CORE.value == "core"
        assert DeploymentProfile.TEAM.value == "team"
        assert DeploymentProfile.ENTERPRISE.value == "enterprise"

    def test_profile_from_string(self):
        """Test creating profile from string value."""
        assert DeploymentProfile("core") == DeploymentProfile.CORE
        assert DeploymentProfile("team") == DeploymentProfile.TEAM
        assert DeploymentProfile("enterprise") == DeploymentProfile.ENTERPRISE

    def test_feature_matrix_completeness(self):
        """Test that all profiles have complete feature matrix."""
        required_features = [
            "database_backend",
            "session_storage",
            "storage_backend",
            "vector_storage",
            "pii_redaction",
            "tracing_enabled",
            "metrics_enabled",
            "tenant_provider",
        ]

        for profile in DeploymentProfile:
            assert profile in FEATURE_MATRIX, f"Missing feature matrix for {profile}"
            features = FEATURE_MATRIX[profile]

            for feature in required_features:
                assert feature in features, f"Missing feature '{feature}' in {profile} profile"


class TestProfileManager:
    """Test ProfileManager functionality."""

    def setup_method(self):
        """Reset profile cache before each test."""
        ProfileManager.reset_profile()

    def teardown_method(self):
        """Clean up after each test."""
        ProfileManager.reset_profile()

    def test_get_current_profile_default(self):
        """Test that default profile is CORE when not specified."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove DEPLOYMENT_PROFILE if it exists
            if "DEPLOYMENT_PROFILE" in os.environ:
                del os.environ["DEPLOYMENT_PROFILE"]

            ProfileManager.reset_profile()  # Clear cache
            profile = ProfileManager.get_current_profile()
            assert profile == DeploymentProfile.CORE

    def test_get_current_profile_from_env(self):
        """Test profile detection from DEPLOYMENT_PROFILE environment variable."""
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "enterprise"}):
            ProfileManager.reset_profile()
            profile = ProfileManager.get_current_profile()
            assert profile == DeploymentProfile.ENTERPRISE

        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "team"}):
            ProfileManager.reset_profile()
            profile = ProfileManager.get_current_profile()
            assert profile == DeploymentProfile.TEAM

        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "core"}):
            ProfileManager.reset_profile()
            profile = ProfileManager.get_current_profile()
            assert profile == DeploymentProfile.CORE

    def test_get_current_profile_invalid_fallback(self):
        """Test that invalid profile falls back to CORE."""
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "invalid"}):
            ProfileManager.reset_profile()
            profile = ProfileManager.get_current_profile()
            assert profile == DeploymentProfile.CORE

    def test_set_profile_override(self):
        """Test manual profile override (for testing)."""
        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
        assert ProfileManager.get_current_profile() == DeploymentProfile.ENTERPRISE

        ProfileManager.set_profile(DeploymentProfile.TEAM)
        assert ProfileManager.get_current_profile() == DeploymentProfile.TEAM

    def test_reset_profile(self):
        """Test profile cache reset."""
        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
        assert ProfileManager.get_current_profile() == DeploymentProfile.ENTERPRISE

        ProfileManager.reset_profile()
        # Should re-read from environment (which defaults to CORE)
        with patch.dict(os.environ, {}, clear=True):
            profile = ProfileManager.get_current_profile()
            assert profile == DeploymentProfile.CORE

    def test_is_feature_enabled_core_profile(self):
        """Test feature flags for CORE profile."""
        ProfileManager.set_profile(DeploymentProfile.CORE)

        # CORE should have these disabled
        assert ProfileManager.is_feature_enabled("pii_redaction") is False
        assert ProfileManager.is_feature_enabled("presidio_enabled") is False
        assert ProfileManager.is_feature_enabled("tracing_enabled") is False
        assert ProfileManager.is_feature_enabled("opik_enabled") is False
        assert ProfileManager.is_feature_enabled("metrics_enabled") is False
        assert ProfileManager.is_feature_enabled("prometheus_enabled") is False
        assert ProfileManager.is_feature_enabled("multi_tenant_enabled") is False

        # CORE should have these enabled
        assert ProfileManager.is_feature_enabled("redis_enabled") is False

    def test_is_feature_enabled_team_profile(self):
        """Test feature flags for TEAM profile."""
        ProfileManager.set_profile(DeploymentProfile.TEAM)

        # TEAM should have these enabled
        assert ProfileManager.is_feature_enabled("redis_enabled") is True
        assert ProfileManager.is_feature_enabled("metrics_enabled") is True

        # TEAM should have these disabled
        assert ProfileManager.is_feature_enabled("pii_redaction") is False
        assert ProfileManager.is_feature_enabled("presidio_enabled") is False
        assert ProfileManager.is_feature_enabled("tracing_enabled") is False
        assert ProfileManager.is_feature_enabled("opik_enabled") is False
        assert ProfileManager.is_feature_enabled("prometheus_enabled") is False
        assert ProfileManager.is_feature_enabled("multi_tenant_enabled") is False

    def test_is_feature_enabled_enterprise_profile(self):
        """Test feature flags for ENTERPRISE profile."""
        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)

        # ENTERPRISE should have everything enabled
        assert ProfileManager.is_feature_enabled("pii_redaction") is True
        assert ProfileManager.is_feature_enabled("presidio_enabled") is True
        assert ProfileManager.is_feature_enabled("tracing_enabled") is True
        assert ProfileManager.is_feature_enabled("opik_enabled") is True
        assert ProfileManager.is_feature_enabled("metrics_enabled") is True
        assert ProfileManager.is_feature_enabled("prometheus_enabled") is True
        assert ProfileManager.is_feature_enabled("multi_tenant_enabled") is True
        assert ProfileManager.is_feature_enabled("redis_enabled") is True

    def test_is_feature_enabled_unknown_feature(self):
        """Test that unknown feature raises ValueError."""
        ProfileManager.set_profile(DeploymentProfile.CORE)

        with pytest.raises(ValueError, match="Unknown feature"):
            ProfileManager.is_feature_enabled("nonexistent_feature")

    def test_get_feature_value(self):
        """Test retrieving feature values."""
        ProfileManager.set_profile(DeploymentProfile.CORE)
        assert ProfileManager.get_feature_value("database_backend") == "sqlite"
        assert ProfileManager.get_feature_value("session_storage") == "inmemory"
        assert ProfileManager.get_feature_value("storage_backend") == "filesystem"

        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
        assert ProfileManager.get_feature_value("database_backend") == "postgresql"
        assert ProfileManager.get_feature_value("session_storage") == "redis"
        assert ProfileManager.get_feature_value("storage_backend") == "s3"

    def test_get_feature_value_unknown_feature(self):
        """Test that unknown feature value raises ValueError."""
        ProfileManager.set_profile(DeploymentProfile.CORE)

        with pytest.raises(ValueError, match="Unknown feature"):
            ProfileManager.get_feature_value("nonexistent_feature")

    def test_get_database_backend(self):
        """Test database backend selection."""
        ProfileManager.set_profile(DeploymentProfile.CORE)
        assert ProfileManager.get_database_backend() == "sqlite"

        ProfileManager.set_profile(DeploymentProfile.TEAM)
        assert ProfileManager.get_database_backend() == "postgresql"

        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
        assert ProfileManager.get_database_backend() == "postgresql"

    def test_get_session_storage(self):
        """Test session storage backend selection."""
        ProfileManager.set_profile(DeploymentProfile.CORE)
        assert ProfileManager.get_session_storage() == "inmemory"

        ProfileManager.set_profile(DeploymentProfile.TEAM)
        assert ProfileManager.get_session_storage() == "redis"

        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
        assert ProfileManager.get_session_storage() == "redis"

    def test_get_storage_backend(self):
        """Test file storage backend selection."""
        ProfileManager.set_profile(DeploymentProfile.CORE)
        assert ProfileManager.get_storage_backend() == "filesystem"

        ProfileManager.set_profile(DeploymentProfile.TEAM)
        assert ProfileManager.get_storage_backend() == "s3"

        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
        assert ProfileManager.get_storage_backend() == "s3"

    def test_get_vector_storage(self):
        """Test vector storage backend selection."""
        ProfileManager.set_profile(DeploymentProfile.CORE)
        assert ProfileManager.get_vector_storage() == "inmemory"

        ProfileManager.set_profile(DeploymentProfile.TEAM)
        assert ProfileManager.get_vector_storage() == "chromadb"

        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
        assert ProfileManager.get_vector_storage() == "chromadb"

    def test_validate_profile_requirements_core(self):
        """Test profile validation for CORE profile (should always pass)."""
        ProfileManager.set_profile(DeploymentProfile.CORE)

        with patch.dict(os.environ, {}, clear=True):
            is_valid, errors = ProfileManager.validate_profile_requirements()
            assert is_valid is True
            assert len(errors) == 0

    def test_validate_profile_requirements_team_missing_database(self):
        """Test profile validation for TEAM profile without DATABASE_URL."""
        ProfileManager.set_profile(DeploymentProfile.TEAM)

        with patch.dict(os.environ, {}, clear=True):
            is_valid, errors = ProfileManager.validate_profile_requirements()
            assert is_valid is False
            assert any("DATABASE_URL" in error for error in errors)

    def test_validate_profile_requirements_team_missing_redis(self):
        """Test profile validation for TEAM profile without Redis."""
        ProfileManager.set_profile(DeploymentProfile.TEAM)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://..."}, clear=False):
            is_valid, errors = ProfileManager.validate_profile_requirements()
            assert is_valid is False
            assert any("REDIS" in error for error in errors)

    def test_validate_profile_requirements_team_valid(self):
        """Test profile validation for TEAM profile with all requirements."""
        ProfileManager.set_profile(DeploymentProfile.TEAM)

        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://localhost/faultmaven",
                "REDIS_HOST": "localhost",
            },
            clear=False,
        ):
            is_valid, errors = ProfileManager.validate_profile_requirements()
            assert is_valid is True
            assert len(errors) == 0

    def test_validate_profile_requirements_enterprise_missing_presidio(self):
        """Test profile validation for ENTERPRISE profile without Presidio."""
        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)

        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://localhost/faultmaven",
                "REDIS_HOST": "localhost",
                "OPIK_API_KEY": "test-key",
            },
            clear=False,
        ):
            is_valid, errors = ProfileManager.validate_profile_requirements()
            assert is_valid is False
            assert any("PRESIDIO_URL" in error for error in errors)

    def test_validate_profile_requirements_enterprise_missing_opik(self):
        """Test profile validation for ENTERPRISE profile without Opik."""
        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)

        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://localhost/faultmaven",
                "REDIS_HOST": "localhost",
                "PRESIDIO_URL": "http://localhost:3000",
            },
            clear=False,
        ):
            is_valid, errors = ProfileManager.validate_profile_requirements()
            assert is_valid is False
            assert any("OPIK_API_KEY" in error for error in errors)

    def test_validate_profile_requirements_enterprise_skip_checks(self):
        """Test profile validation with SKIP_SERVICE_CHECKS=true."""
        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)

        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://localhost/faultmaven",
                "REDIS_HOST": "localhost",
                "SKIP_SERVICE_CHECKS": "true",
            },
            clear=False,
        ):
            is_valid, errors = ProfileManager.validate_profile_requirements()
            assert is_valid is True  # SKIP_SERVICE_CHECKS allows missing services

    def test_validate_profile_requirements_enterprise_valid(self):
        """Test profile validation for ENTERPRISE profile with all requirements."""
        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)

        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://localhost/faultmaven",
                "REDIS_HOST": "localhost",
                "PRESIDIO_URL": "http://localhost:3000",
                "OPIK_API_KEY": "test-opik-key",
            },
            clear=False,
        ):
            is_valid, errors = ProfileManager.validate_profile_requirements()
            assert is_valid is True
            assert len(errors) == 0

    def test_get_profile_summary(self):
        """Test getting profile summary."""
        ProfileManager.set_profile(DeploymentProfile.CORE)
        summary = ProfileManager.get_profile_summary()

        assert summary["profile"] == "core"
        assert "features" in summary
        assert "description" in summary
        assert "Community Edition" in summary["description"]

        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
        summary = ProfileManager.get_profile_summary()

        assert summary["profile"] == "enterprise"
        assert "Enterprise Edition" in summary["description"]
        assert summary["features"]["pii_redaction"] is True

    def test_llm_provider_limits(self):
        """Test LLM provider limits per profile."""
        ProfileManager.set_profile(DeploymentProfile.CORE)
        assert ProfileManager.get_feature_value("max_llm_providers") == 3

        ProfileManager.set_profile(DeploymentProfile.TEAM)
        assert ProfileManager.get_feature_value("max_llm_providers") == 5

        ProfileManager.set_profile(DeploymentProfile.ENTERPRISE)
        assert ProfileManager.get_feature_value("max_llm_providers") == 7
