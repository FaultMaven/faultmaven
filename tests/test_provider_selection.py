"""Tests for Provider Selection (PR #3 - Provider Selector Normalization).

Tests that provider factories correctly select implementations based on
doc-aligned environment variables:
- TENANT_PROVIDER: single | multi
- DB_BACKEND: sqlite | postgres
- CACHE_BACKEND: memory | redis
- VECTOR_BACKEND: chroma | pinecone
- STORAGE_BACKEND: filesystem | s3

Also tests backward compatibility with legacy env vars and deprecation warnings.
"""

import os
import pytest
from unittest.mock import patch, MagicMock
import logging


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def clean_env():
    """Fixture to ensure clean environment for each test."""
    # Store original values
    env_vars = [
        "TENANT_PROVIDER",
        "DEPLOYMENT_MODE",
        "DB_BACKEND",
        "CASE_STORAGE_TYPE",
        "CACHE_BACKEND",
        "VECTOR_BACKEND",
        "STORAGE_BACKEND",
        "ENVIRONMENT",
    ]
    original = {k: os.environ.get(k) for k in env_vars}

    # Clear all relevant env vars
    for var in env_vars:
        if var in os.environ:
            del os.environ[var]

    yield

    # Restore original values
    for var, value in original.items():
        if value is not None:
            os.environ[var] = value
        elif var in os.environ:
            del os.environ[var]


@pytest.fixture
def reset_settings_cache():
    """Reset the settings singleton cache between tests."""
    from faultmaven.config.settings import reset_settings
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def reset_deprecation_warnings():
    """Reset deprecation warning tracking between tests."""
    from faultmaven.config import settings
    settings._legacy_env_warnings_logged.clear()
    yield
    settings._legacy_env_warnings_logged.clear()


# =============================================================================
# Tenant Provider Selection Tests
# =============================================================================

class TestTenantProviderSelection:
    """Tests for TENANT_PROVIDER selector."""

    def test_default_is_single_tenant(self, clean_env, reset_settings_cache):
        """Test that default tenant provider is 'single'."""
        from faultmaven.config.settings import get_settings, TenantProvider

        settings = get_settings()
        assert settings.providers.tenant_provider == TenantProvider.SINGLE

    def test_tenant_provider_single(self, clean_env, reset_settings_cache):
        """Test TENANT_PROVIDER=single selects SingleTenantProvider."""
        os.environ["TENANT_PROVIDER"] = "single"

        from faultmaven.config.settings import get_settings, TenantProvider

        settings = get_settings()
        assert settings.providers.tenant_provider == TenantProvider.SINGLE

    def test_tenant_provider_multi(self, clean_env, reset_settings_cache):
        """Test TENANT_PROVIDER=multi selects MultiTenantProvider."""
        os.environ["TENANT_PROVIDER"] = "multi"

        from faultmaven.config.settings import get_settings, TenantProvider

        settings = get_settings()
        assert settings.providers.tenant_provider == TenantProvider.MULTI

    def test_deployment_mode_backward_compatibility(self, clean_env, reset_settings_cache):
        """Test legacy DEPLOYMENT_MODE is mapped to TENANT_PROVIDER."""
        os.environ["DEPLOYMENT_MODE"] = "multi-tenant"

        from faultmaven.config.settings import get_settings

        settings = get_settings()
        # Should still work via backward compatibility
        assert settings.deployment_mode == "multi-tenant"

    def test_tenant_provider_overrides_deployment_mode(self, clean_env, reset_settings_cache):
        """Test new TENANT_PROVIDER takes precedence over legacy DEPLOYMENT_MODE."""
        os.environ["TENANT_PROVIDER"] = "single"
        os.environ["DEPLOYMENT_MODE"] = "multi-tenant"

        from faultmaven.config.settings import get_settings, TenantProvider

        settings = get_settings()
        # New var should take precedence
        assert settings.providers.tenant_provider == TenantProvider.SINGLE


# =============================================================================
# Database Backend Selection Tests
# =============================================================================

class TestDbBackendSelection:
    """Tests for DB_BACKEND selector."""

    def test_default_is_sqlite(self, clean_env, reset_settings_cache):
        """Test that default database backend is 'sqlite'."""
        from faultmaven.config.settings import get_settings, DbBackend

        settings = get_settings()
        assert settings.providers.db_backend == DbBackend.SQLITE

    def test_db_backend_sqlite(self, clean_env, reset_settings_cache):
        """Test DB_BACKEND=sqlite."""
        os.environ["DB_BACKEND"] = "sqlite"

        from faultmaven.config.settings import get_settings, DbBackend

        settings = get_settings()
        assert settings.providers.db_backend == DbBackend.SQLITE

    def test_db_backend_postgres(self, clean_env, reset_settings_cache):
        """Test DB_BACKEND=postgres."""
        os.environ["DB_BACKEND"] = "postgres"

        from faultmaven.config.settings import get_settings, DbBackend

        settings = get_settings()
        assert settings.providers.db_backend == DbBackend.POSTGRES


# =============================================================================
# Cache Backend Selection Tests
# =============================================================================

class TestCacheBackendSelection:
    """Tests for CACHE_BACKEND selector."""

    def test_default_is_memory(self, clean_env, reset_settings_cache):
        """Test that default cache backend is 'memory'."""
        from faultmaven.config.settings import get_settings, CacheBackend

        settings = get_settings()
        assert settings.providers.cache_backend == CacheBackend.MEMORY

    def test_cache_backend_memory(self, clean_env, reset_settings_cache):
        """Test CACHE_BACKEND=memory."""
        os.environ["CACHE_BACKEND"] = "memory"

        from faultmaven.config.settings import get_settings, CacheBackend

        settings = get_settings()
        assert settings.providers.cache_backend == CacheBackend.MEMORY

    def test_cache_backend_redis(self, clean_env, reset_settings_cache):
        """Test CACHE_BACKEND=redis."""
        os.environ["CACHE_BACKEND"] = "redis"

        from faultmaven.config.settings import get_settings, CacheBackend

        settings = get_settings()
        assert settings.providers.cache_backend == CacheBackend.REDIS


# =============================================================================
# Vector Backend Selection Tests
# =============================================================================

class TestVectorBackendSelection:
    """Tests for VECTOR_BACKEND selector."""

    def test_default_is_chroma(self, clean_env, reset_settings_cache):
        """Test that default vector backend is 'chroma'."""
        from faultmaven.config.settings import get_settings, VectorBackend

        settings = get_settings()
        assert settings.providers.vector_backend == VectorBackend.CHROMA

    def test_vector_backend_chroma(self, clean_env, reset_settings_cache):
        """Test VECTOR_BACKEND=chroma."""
        os.environ["VECTOR_BACKEND"] = "chroma"

        from faultmaven.config.settings import get_settings, VectorBackend

        settings = get_settings()
        assert settings.providers.vector_backend == VectorBackend.CHROMA

    def test_vector_backend_pinecone(self, clean_env, reset_settings_cache):
        """Test VECTOR_BACKEND=pinecone."""
        os.environ["VECTOR_BACKEND"] = "pinecone"

        from faultmaven.config.settings import get_settings, VectorBackend

        settings = get_settings()
        assert settings.providers.vector_backend == VectorBackend.PINECONE


# =============================================================================
# Storage Backend Selection Tests
# =============================================================================

class TestStorageBackendSelection:
    """Tests for STORAGE_BACKEND selector."""

    def test_default_is_filesystem(self, clean_env, reset_settings_cache):
        """Test that default storage backend is 'filesystem'."""
        from faultmaven.config.settings import get_settings, StorageBackend

        settings = get_settings()
        assert settings.providers.storage_backend == StorageBackend.FILESYSTEM

    def test_storage_backend_filesystem(self, clean_env, reset_settings_cache):
        """Test STORAGE_BACKEND=filesystem."""
        os.environ["STORAGE_BACKEND"] = "filesystem"

        from faultmaven.config.settings import get_settings, StorageBackend

        settings = get_settings()
        assert settings.providers.storage_backend == StorageBackend.FILESYSTEM

    def test_storage_backend_s3(self, clean_env, reset_settings_cache):
        """Test STORAGE_BACKEND=s3."""
        os.environ["STORAGE_BACKEND"] = "s3"

        from faultmaven.config.settings import get_settings, StorageBackend

        settings = get_settings()
        assert settings.providers.storage_backend == StorageBackend.S3


# =============================================================================
# Factory Integration Tests
# =============================================================================

class TestTenantProviderFactory:
    """Tests for tenant provider factory integration."""

    def test_factory_creates_single_tenant_by_default(self, clean_env, reset_settings_cache):
        """Test factory creates SingleTenantProvider by default."""
        from faultmaven.providers.tenancy.factory import create_tenant_provider
        from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

        mock_repo = MagicMock()
        provider = create_tenant_provider(mock_repo)

        assert isinstance(provider, SingleTenantProvider)

    def test_factory_creates_multi_tenant_when_configured(self, clean_env, reset_settings_cache):
        """Test factory creates MultiTenantProvider when TENANT_PROVIDER=multi."""
        os.environ["TENANT_PROVIDER"] = "multi"

        from faultmaven.providers.tenancy.factory import create_tenant_provider
        from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider

        mock_repo = MagicMock()
        provider = create_tenant_provider(mock_repo)

        assert isinstance(provider, MultiTenantProvider)

    def test_factory_respects_legacy_deployment_mode(self, clean_env, reset_settings_cache):
        """Test factory still works with legacy DEPLOYMENT_MODE."""
        os.environ["DEPLOYMENT_MODE"] = "multi-tenant"

        from faultmaven.providers.tenancy.factory import create_tenant_provider
        from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider

        mock_repo = MagicMock()
        provider = create_tenant_provider(mock_repo)

        assert isinstance(provider, MultiTenantProvider)


# =============================================================================
# Deprecation Warning Tests
# =============================================================================

class TestDeprecationWarnings:
    """Tests for legacy env var deprecation warnings."""

    def test_deployment_mode_logs_deprecation_warning(
        self, clean_env, reset_settings_cache, reset_deprecation_warnings, caplog
    ):
        """Test that using DEPLOYMENT_MODE logs a deprecation warning."""
        os.environ["DEPLOYMENT_MODE"] = "single-tenant"

        with caplog.at_level(logging.WARNING):
            from faultmaven.config.settings import get_settings
            settings = get_settings()

        # Check deprecation warning was logged
        assert any("DEPRECATION" in record.message for record in caplog.records)
        assert any("DEPLOYMENT_MODE" in record.message for record in caplog.records)

    def test_deprecation_warning_logged_once(
        self, clean_env, reset_settings_cache, reset_deprecation_warnings, caplog
    ):
        """Test that deprecation warning is only logged once per variable."""
        os.environ["DEPLOYMENT_MODE"] = "single-tenant"

        with caplog.at_level(logging.WARNING):
            from faultmaven.config.settings import get_settings, reset_settings
            # First call
            get_settings()
            warning_count_1 = sum(1 for r in caplog.records if "DEPRECATION" in r.message)

            # Second call (should not log again)
            reset_settings()
            get_settings()
            warning_count_2 = sum(1 for r in caplog.records if "DEPRECATION" in r.message)

        # Warning should only appear once
        assert warning_count_1 == 1
        assert warning_count_2 == 1  # No new warnings


# =============================================================================
# Settings Purity Tests
# =============================================================================

class TestSettingsPurity:
    """Tests that only settings module reads environment variables."""

    def test_tenancy_factory_uses_settings_not_env(self, clean_env, reset_settings_cache):
        """Test that tenancy factory reads from settings, not os.getenv directly."""
        import faultmaven.providers.tenancy.factory as factory_module
        import inspect

        source = inspect.getsource(factory_module.create_tenant_provider)

        # Should not contain direct os.getenv calls
        assert "os.getenv" not in source
        # Should use settings
        assert "get_settings" in source or "settings" in source

    def test_repository_factory_uses_settings_not_env(self, clean_env, reset_settings_cache):
        """Test that repository factory reads from settings, not os.getenv directly."""
        from faultmaven.infrastructure.persistence.repository_factory import (
            get_storage_type,
            get_session_storage_type,
        )
        import inspect

        # Check that functions use settings (with fallback for early init)
        source1 = inspect.getsource(get_storage_type)
        source2 = inspect.getsource(get_session_storage_type)

        # Should reference settings
        assert "get_settings" in source1
        assert "get_settings" in source2
