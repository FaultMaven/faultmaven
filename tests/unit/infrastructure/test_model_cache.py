"""Tests for model cache lazy loading behavior.

Tests cover:
- Lazy loading configuration
- Load time tracking
- Model caching behavior
- Thread safety
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


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
        with patch(
            "faultmaven.infrastructure.model_cache.SENTENCE_TRANSFORMERS_AVAILABLE",
            False,
        ):
            cache.get_bge_m3_model(triggered_by="startup")

            load_info = cache.get_model_load_info("BAAI/bge-m3")
            assert load_info is not None
            assert load_info.load_triggered_by == "startup"
            assert (
                load_info.error is not None
            )  # Should have error since ST not available

    def test_load_info_tracks_lazy_trigger(self):
        """Test that lazy loading is tracked correctly."""
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        # Note: cache is already cleared by fixture

        with patch(
            "faultmaven.infrastructure.model_cache.SENTENCE_TRANSFORMERS_AVAILABLE",
            False,
        ):
            cache.get_bge_m3_model(triggered_by="lazy")

            load_info = cache.get_model_load_info("BAAI/bge-m3")
            assert load_info is not None
            assert load_info.load_triggered_by == "lazy"

    def test_cache_info_includes_load_details(self):
        """Test that cache info includes load timing details."""
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        # Note: cache is already cleared by fixture

        with patch(
            "faultmaven.infrastructure.model_cache.SENTENCE_TRANSFORMERS_AVAILABLE",
            False,
        ):
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

        with patch(
            "faultmaven.infrastructure.model_cache.SENTENCE_TRANSFORMERS_AVAILABLE",
            False,
        ):
            cache.get_bge_m3_model()

        # Verify load info was recorded
        load_info = cache.get_model_load_info("BAAI/bge-m3")
        assert (
            load_info is not None
        ), "Load info should be recorded even when model unavailable"

        cache.clear_cache()

        # Verify load info is cleared
        assert cache.get_model_load_info("BAAI/bge-m3") is None
        assert cache.get_cache_info()["cache_size"] == 0


class TestAsyncEmbedBoundary:
    """The `aembed_texts`/`aembed_query` async boundary — the single place that
    runs CPU-bound BGE-M3 `encode()` off the event loop via `asyncio.to_thread`.

    This is where the cold-start timeout regression is guarded now: a synchronous
    `encode()` on the loop thread froze the request loop and starved the k8s
    liveness probe. Every embed call site funnels through this boundary, so
    testing it here covers all of them.
    """

    @pytest.mark.asyncio
    async def test_aembed_texts_returns_one_vector_per_text(self):
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        mock_model = MagicMock()
        mock_model.encode.return_value = np.random.rand(3, 1024)
        cache._models["BAAI/bge-m3"] = mock_model

        result = await cache.aembed_texts(["a", "b", "c"])

        assert result is not None
        assert len(result) == 3
        assert len(result[0]) == 1024
        mock_model.encode.assert_called_once_with(["a", "b", "c"])

    @pytest.mark.asyncio
    async def test_aembed_query_returns_single_vector(self):
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        mock_model = MagicMock()
        mock_model.encode.return_value = np.random.rand(1024)
        cache._models["BAAI/bge-m3"] = mock_model

        result = await cache.aembed_query("query")

        assert result is not None
        assert len(result) == 1024
        mock_model.encode.assert_called_once_with("query")

    @pytest.mark.asyncio
    async def test_aembed_returns_none_when_model_unavailable(self):
        """When BGE-M3 can't load, the boundary returns None so callers can fall
        back to ChromaDB's default embedding instead of crashing."""
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        with patch(
            "faultmaven.infrastructure.model_cache.SENTENCE_TRANSFORMERS_AVAILABLE",
            False,
        ):
            assert await cache.aembed_texts(["a"]) is None
            assert await cache.aembed_query("a") is None

    @pytest.mark.asyncio
    async def test_encode_runs_off_the_event_loop(self):
        """BGE-M3 `encode()` is CPU-bound; the boundary must offload it so the
        event loop can service concurrent requests (e.g. the liveness probe)
        while embedding runs.

        Regression guard for the cold-start timeout incident where a synchronous
        `encode()` froze the request loop. See docs/architecture/data-processing/
        data-preprocessing-design-specification.md §5.7.
        """
        from faultmaven.infrastructure.model_cache import ModelCache

        cache = ModelCache()
        loop_thread_id = threading.get_ident()
        encode_thread = {}

        def slow_encode(_texts):
            # Record the thread encode ran on — must NOT be the loop thread.
            encode_thread["id"] = threading.get_ident()
            time.sleep(0.05)
            return np.random.rand(1, 1024)

        mock_model = MagicMock()
        mock_model.encode.side_effect = slow_encode
        cache._models["BAAI/bge-m3"] = mock_model

        concurrent_tick = {"ran": False}

        async def concurrent_work():
            # Yields to the loop — should run while encode sleeps if encode is
            # correctly offloaded.
            await asyncio.sleep(0)
            concurrent_tick["ran"] = True

        await asyncio.gather(cache.aembed_texts(["content"]), concurrent_work())

        assert concurrent_tick["ran"] is True
        assert encode_thread["id"] != loop_thread_id, (
            "encode() ran on the event loop thread — asyncio.to_thread offload "
            "regressed."
        )


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

    def test_preload_models_default_includes_bge_m3(self):
        """preload_models defaults to ['BAAI/bge-m3'] so the embedding model
        warms at startup instead of stalling the first request path.

        Opting out requires an explicit PRELOAD_MODELS='' env var. See
        data-preprocessing-design-specification.md §5.7.
        """
        from faultmaven.config.settings import EmbeddingSettings

        settings = EmbeddingSettings()

        assert settings.preload_models == ["BAAI/bge-m3"]

    def test_preload_models_opt_out_via_env(self):
        """Operators can opt out of preload by setting PRELOAD_MODELS=''."""
        import os

        from faultmaven.config.settings import EmbeddingSettings

        with patch.dict(os.environ, {"PRELOAD_MODELS": "[]"}):
            settings = EmbeddingSettings()
            assert settings.preload_models == []


class TestModelLoadInfo:
    """Test ModelLoadInfo dataclass."""

    def test_model_load_info_creation(self):
        """Test ModelLoadInfo can be created with required fields."""
        from datetime import datetime, timezone

        from faultmaven.infrastructure.model_cache import ModelLoadInfo

        info = ModelLoadInfo(
            model_name="BAAI/bge-m3",
            loaded_at=datetime.now(timezone.utc),
            load_time_seconds=2.5,
            load_triggered_by="startup",
        )

        assert info.model_name == "BAAI/bge-m3"
        assert info.load_time_seconds == 2.5
        assert info.load_triggered_by == "startup"
        assert info.error is None

    def test_model_load_info_with_error(self):
        """Test ModelLoadInfo can track errors."""
        from datetime import datetime, timezone

        from faultmaven.infrastructure.model_cache import ModelLoadInfo

        info = ModelLoadInfo(
            model_name="BAAI/bge-m3",
            loaded_at=datetime.now(timezone.utc),
            load_time_seconds=0.1,
            load_triggered_by="lazy",
            error="Model not found",
        )

        assert info.error == "Model not found"
