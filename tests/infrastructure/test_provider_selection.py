"""Tests for Provider Selection (PR #3 - Provider Selector Normalization).

Tests that provider factories correctly select implementations based on
doc-aligned environment variables:
- TENANT_PROVIDER: single | multi
- DB_BACKEND: sqlite | postgres
- CACHE_BACKEND: memory | redis
- VECTOR_BACKEND: chroma | pinecone
- STORAGE_BACKEND: filesystem | s3
"""

import inspect
import os
from unittest.mock import MagicMock

import pytest

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def clean_env():
    """Fixture to ensure clean environment for each test."""
    env_vars = [
        "TENANT_PROVIDER",
        "DB_BACKEND",
        "CACHE_BACKEND",
        "VECTOR_BACKEND",
        "STORAGE_BACKEND",
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


# =============================================================================
# Tenant Provider Selection Tests
# =============================================================================


class TestTenantProviderSelection:
    """Tests for TENANT_PROVIDER selector."""

    def test_default_is_single_tenant(self, clean_env, reset_settings_cache):
        """Test that default tenant provider is 'single'."""
        from faultmaven.config.settings import TenantProvider, get_settings

        settings = get_settings()
        assert settings.providers.tenant_provider == TenantProvider.SINGLE

    def test_tenant_provider_single(self, clean_env, reset_settings_cache):
        """Test TENANT_PROVIDER=single selects single tenant."""
        os.environ["TENANT_PROVIDER"] = "single"

        from faultmaven.config.settings import TenantProvider, get_settings

        settings = get_settings()
        assert settings.providers.tenant_provider == TenantProvider.SINGLE

    def test_tenant_provider_multi(self, clean_env, reset_settings_cache):
        """Test TENANT_PROVIDER=multi selects multi tenant."""
        os.environ["TENANT_PROVIDER"] = "multi"

        from faultmaven.config.settings import TenantProvider, get_settings

        settings = get_settings()
        assert settings.providers.tenant_provider == TenantProvider.MULTI


# =============================================================================
# Database Backend Selection Tests
# =============================================================================


class TestDbBackendSelection:
    """Tests for DB_BACKEND selector."""

    def test_default_is_sqlite(self, clean_env, reset_settings_cache):
        """Test that default database backend is 'sqlite'."""
        from faultmaven.config.settings import DbBackend, get_settings

        settings = get_settings()
        assert settings.providers.db_backend == DbBackend.SQLITE

    def test_db_backend_sqlite(self, clean_env, reset_settings_cache):
        """Test DB_BACKEND=sqlite."""
        os.environ["DB_BACKEND"] = "sqlite"

        from faultmaven.config.settings import DbBackend, get_settings

        settings = get_settings()
        assert settings.providers.db_backend == DbBackend.SQLITE

    def test_db_backend_postgres(self, clean_env, reset_settings_cache):
        """Test DB_BACKEND=postgres."""
        os.environ["DB_BACKEND"] = "postgres"

        from faultmaven.config.settings import DbBackend, get_settings

        settings = get_settings()
        assert settings.providers.db_backend == DbBackend.POSTGRES


# =============================================================================
# Cache Backend Selection Tests
# =============================================================================


class TestCacheBackendSelection:
    """Tests for CACHE_BACKEND selector."""

    def test_default_is_memory(self, clean_env, reset_settings_cache):
        """Test that default cache backend is 'memory'."""
        from faultmaven.config.settings import CacheBackend, get_settings

        settings = get_settings()
        assert settings.providers.cache_backend == CacheBackend.MEMORY

    def test_cache_backend_memory(self, clean_env, reset_settings_cache):
        """Test CACHE_BACKEND=memory."""
        os.environ["CACHE_BACKEND"] = "memory"

        from faultmaven.config.settings import CacheBackend, get_settings

        settings = get_settings()
        assert settings.providers.cache_backend == CacheBackend.MEMORY

    def test_cache_backend_redis(self, clean_env, reset_settings_cache):
        """Test CACHE_BACKEND=redis."""
        os.environ["CACHE_BACKEND"] = "redis"

        from faultmaven.config.settings import CacheBackend, get_settings

        settings = get_settings()
        assert settings.providers.cache_backend == CacheBackend.REDIS


# =============================================================================
# Vector Backend Selection Tests
# =============================================================================


class TestVectorBackendSelection:
    """Tests for VECTOR_BACKEND selector."""

    def test_default_is_chroma(self, clean_env, reset_settings_cache):
        """Test that default vector backend is 'chroma'."""
        from faultmaven.config.settings import VectorBackend, get_settings

        settings = get_settings()
        assert settings.providers.vector_backend == VectorBackend.CHROMA

    def test_vector_backend_chroma(self, clean_env, reset_settings_cache):
        """Test VECTOR_BACKEND=chroma."""
        os.environ["VECTOR_BACKEND"] = "chroma"

        from faultmaven.config.settings import VectorBackend, get_settings

        settings = get_settings()
        assert settings.providers.vector_backend == VectorBackend.CHROMA

    def test_vector_backend_pinecone(self, clean_env, reset_settings_cache):
        """Test VECTOR_BACKEND=pinecone."""
        os.environ["VECTOR_BACKEND"] = "pinecone"

        from faultmaven.config.settings import VectorBackend, get_settings

        settings = get_settings()
        assert settings.providers.vector_backend == VectorBackend.PINECONE


# =============================================================================
# Storage Backend Selection Tests
# =============================================================================


class TestStorageBackendSelection:
    """Tests for STORAGE_BACKEND selector."""

    def test_default_is_filesystem(self, clean_env, reset_settings_cache):
        """Test that default storage backend is 'filesystem'."""
        from faultmaven.config.settings import StorageBackend, get_settings

        settings = get_settings()
        assert settings.providers.storage_backend == StorageBackend.FILESYSTEM

    def test_storage_backend_filesystem(self, clean_env, reset_settings_cache):
        """Test STORAGE_BACKEND=filesystem."""
        os.environ["STORAGE_BACKEND"] = "filesystem"

        from faultmaven.config.settings import StorageBackend, get_settings

        settings = get_settings()
        assert settings.providers.storage_backend == StorageBackend.FILESYSTEM

    def test_storage_backend_s3(self, clean_env, reset_settings_cache):
        """Test STORAGE_BACKEND=s3."""
        os.environ["STORAGE_BACKEND"] = "s3"

        from faultmaven.config.settings import StorageBackend, get_settings

        settings = get_settings()
        assert settings.providers.storage_backend == StorageBackend.S3


# =============================================================================
# Factory Integration Tests
# =============================================================================


class TestTenantProviderFactory:
    """Tests for tenant provider factory integration."""

    def test_factory_creates_single_tenant_by_default(
        self, clean_env, reset_settings_cache
    ):
        """Test factory creates SingleTenantProvider by default."""
        from faultmaven.providers.tenancy.factory import create_tenant_provider
        from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

        mock_repo = MagicMock()
        provider = create_tenant_provider(mock_repo)

        assert isinstance(provider, SingleTenantProvider)

    def test_factory_multi_fails_closed_until_ready(self):
        """``multi`` is held closed until P2 ships its isolation (ADR-010) —
        fail-closed on every container path, incl. the gate-less jobs/CLI path."""
        from unittest.mock import MagicMock, patch

        import pytest

        from faultmaven.config.settings import TenantProvider as TenantProviderEnum
        from faultmaven.providers.tenancy.factory import (
            TenancyConfigurationError,
            create_tenant_provider,
        )

        mock_settings = MagicMock()
        mock_settings.providers = MagicMock()
        mock_settings.providers.tenant_provider = TenantProviderEnum.MULTI
        mock_repo = MagicMock()

        with patch(
            "faultmaven.providers.tenancy.factory.get_settings",
            return_value=mock_settings,
        ):
            with pytest.raises(TenancyConfigurationError):
                create_tenant_provider(mock_repo)

    def test_di_wrapper_reraises_fatal_tenancy_error(self):
        """The container DI wrapper must NOT swallow a fatal tenancy misconfig —
        otherwise gate-less paths (jobs CLI / cron) degrade to tenant_provider=None."""
        from unittest.mock import MagicMock, patch

        import pytest

        from faultmaven.container.providers.services import (
            create_tenant_provider as wrapper,
        )
        from faultmaven.providers.tenancy.factory import TenancyConfigurationError

        with patch(
            "faultmaven.providers.tenancy.factory.create_tenant_provider",
            side_effect=TenancyConfigurationError("no plugin"),
        ):
            with pytest.raises(TenancyConfigurationError):
                wrapper(MagicMock(), MagicMock())  # org_repo (truthy), settings


# =============================================================================
# Settings Purity Tests
# =============================================================================


class TestSettingsPurity:
    """Tests that only settings module reads environment variables."""

    def test_tenancy_factory_uses_settings_not_env(
        self, clean_env, reset_settings_cache
    ):
        """Test that the tenancy factory reads from settings, not os.getenv directly."""
        import faultmaven.providers.tenancy.factory as factory_module

        create_src = inspect.getsource(factory_module.create_tenant_provider)
        resolve_src = inspect.getsource(factory_module.requested_tenant_provider)

        # Neither the factory nor its name resolver reads the environment directly.
        assert "os.getenv" not in create_src
        assert "os.getenv" not in resolve_src
        # The provider name is sourced from settings (via the resolver).
        assert "get_settings" in resolve_src
