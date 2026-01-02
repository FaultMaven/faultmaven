"""Tests for model cache lazy loading behavior.

Tests cover:
- Lazy loading configuration
- Load time tracking
- Model caching behavior
- Thread safety
"""

import pytest
from unittest.mock import patch, MagicMock
import threading
import time


@pytest.fixture(autouse=True)
def clean_model_cache():
    """Ensure clean model cache for each test to avoid singleton state leakage."""
    from faultmaven.infrastructure.model_cache import ModelCache
    cache = ModelCache()
    cache.clear_cache()
    yield
    cache.clear_cache()


class TestModelCacheLazyLoading:
    """Test model cache lazy loading behavior."""

    def test_model_cache_singleton(self):
        """Test that ModelCache is a singleton."""
        from faultmaven.infrastructure.model_cache import ModelCache

        cache1 = ModelCache()
        cache2 = ModelCache()

        assert cache1 is cache2

    def test_model_not_loaded_initially(self):
        """Test that models are not loaded until requested."""
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        # Note: cache is already cleared by fixture

        assert not cache.is_model_loaded("BAAI/bge-m3")
        assert cache.get_cache_info()["cache_size"] == 0

    def test_load_info_tracks_triggered_by(self):
        """Test that load info tracks how loading was triggered."""
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        # Note: cache is already cleared by fixture

        # Mock the SentenceTransformer to avoid actual loading
        with patch('faultmaven.infrastructure.model_cache.SENTENCE_TRANSFORMERS_AVAILABLE', False):
            cache.get_bge_m3_model(triggered_by="startup")

            load_info = cache.get_model_load_info("BAAI/bge-m3")
            assert load_info is not None
            assert load_info.load_triggered_by == "startup"
            assert load_info.error is not None  # Should have error since ST not available

    def test_load_info_tracks_lazy_trigger(self):
        """Test that lazy loading is tracked correctly."""
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        # Note: cache is already cleared by fixture

        with patch('faultmaven.infrastructure.model_cache.SENTENCE_TRANSFORMERS_AVAILABLE', False):
            cache.get_bge_m3_model(triggered_by="lazy")

            load_info = cache.get_model_load_info("BAAI/bge-m3")
            assert load_info is not None
            assert load_info.load_triggered_by == "lazy"

    def test_cache_info_includes_load_details(self):
        """Test that cache info includes load timing details."""
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        # Note: cache is already cleared by fixture

        with patch('faultmaven.infrastructure.model_cache.SENTENCE_TRANSFORMERS_AVAILABLE', False):
            cache.get_bge_m3_model(triggered_by="startup")

        info = cache.get_cache_info()

        assert "load_details" in info
        assert "BAAI/bge-m3" in info["load_details"]
        assert "load_time_seconds" in info["load_details"]["BAAI/bge-m3"]
        assert "triggered_by" in info["load_details"]["BAAI/bge-m3"]

    def test_cached_model_not_reloaded(self):
        """Test that cached models are not reloaded on subsequent calls."""
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        # Note: cache is already cleared by fixture

        # Add a mock model directly to the cache
        mock_model = MagicMock()
        cache._models["BAAI/bge-m3"] = mock_model

        # Request should return cached model without loading
        result = cache.get_bge_m3_model()

        assert result is mock_model
        assert cache.is_model_loaded("BAAI/bge-m3")

    def test_clear_cache_clears_load_info(self):
        """Test that clear_cache also clears load info."""
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        # Note: cache is already cleared by fixture

        with patch('faultmaven.infrastructure.model_cache.SENTENCE_TRANSFORMERS_AVAILABLE', False):
            cache.get_bge_m3_model()

        # Verify load info was recorded
        load_info = cache.get_model_load_info("BAAI/bge-m3")
        assert load_info is not None, "Load info should be recorded even when model unavailable"

        cache.clear_cache()

        # Verify load info is cleared
        assert cache.get_model_load_info("BAAI/bge-m3") is None
        assert cache.get_cache_info()["cache_size"] == 0


class TestLazyLoadingConfiguration:
    """Test lazy loading configuration in settings."""

    def test_lazy_load_setting_default_true(self):
        """Test that lazy_load_ml_models defaults to True."""
        from faultmaven.config.settings import EmbeddingSettings

        settings = EmbeddingSettings()

        assert settings.lazy_load_ml_models is True

    def test_lazy_load_setting_can_be_disabled(self):
        """Test that lazy loading can be disabled via env var."""
        import os
        from faultmaven.config.settings import EmbeddingSettings

        with patch.dict(os.environ, {"LAZY_LOAD_ML_MODELS": "false"}):
            settings = EmbeddingSettings()
            assert settings.lazy_load_ml_models is False

    def test_preload_models_default_empty(self):
        """Test that preload_models defaults to empty list."""
        from faultmaven.config.settings import EmbeddingSettings

        settings = EmbeddingSettings()

        assert settings.preload_models == []


class TestModelLoadInfo:
    """Test ModelLoadInfo dataclass."""

    def test_model_load_info_creation(self):
        """Test ModelLoadInfo can be created with required fields."""
        from faultmaven.infrastructure.model_cache import ModelLoadInfo
        from datetime import datetime, timezone

        info = ModelLoadInfo(
            model_name="BAAI/bge-m3",
            loaded_at=datetime.now(timezone.utc),
            load_time_seconds=2.5,
            load_triggered_by="startup"
        )

        assert info.model_name == "BAAI/bge-m3"
        assert info.load_time_seconds == 2.5
        assert info.load_triggered_by == "startup"
        assert info.error is None

    def test_model_load_info_with_error(self):
        """Test ModelLoadInfo can track errors."""
        from faultmaven.infrastructure.model_cache import ModelLoadInfo
        from datetime import datetime, timezone

        info = ModelLoadInfo(
            model_name="BAAI/bge-m3",
            loaded_at=datetime.now(timezone.utc),
            load_time_seconds=0.1,
            load_triggered_by="lazy",
            error="Model not found"
        )

        assert info.error == "Model not found"
