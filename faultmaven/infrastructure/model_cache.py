"""Model Cache Manager

Purpose: Centralized caching for expensive ML models to prevent re-downloading
and re-loading on every container initialization.

This module provides singleton-pattern caching for sentence transformers and
other ML models used across the FaultMaven system.

Key Features:
- Singleton pattern for model instances
- Lazy loading with error handling (configurable via LAZY_LOAD_ML_MODELS)
- Memory-efficient model sharing
- Thread-safe initialization
- Load time tracking for observability

Configuration:
- LAZY_LOAD_ML_MODELS=true (default): Models load on first use
- LAZY_LOAD_ML_MODELS=false: Models pre-load at startup
- PRELOAD_MODELS=['BAAI/bge-m3']: Specific models to pre-load
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any

# Import with graceful fallback
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SentenceTransformer = None
    SENTENCE_TRANSFORMERS_AVAILABLE = False


@dataclass
class ModelLoadInfo:
    """Information about a loaded model."""
    model_name: str
    loaded_at: datetime
    load_time_seconds: float
    load_triggered_by: str = "lazy"  # "lazy", "startup", "preload"
    error: Optional[str] = None


class ModelCache:
    """Centralized cache for ML models with singleton pattern and observability."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                    cls._instance._models = {}
                    cls._instance._load_info: Dict[str, ModelLoadInfo] = {}
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.logger = logging.getLogger(__name__)
            self._models = {}
            self._load_info: Dict[str, ModelLoadInfo] = {}
            self._initialized = True
            self.logger.debug("ModelCache initialized")
    
    def get_bge_m3_model(self, triggered_by: str = "lazy") -> Optional[SentenceTransformer]:
        """
        Get cached BGE-M3 model instance.

        Args:
            triggered_by: How loading was triggered ("lazy", "startup", "preload")

        Returns:
            SentenceTransformer model or None if unavailable
        """
        model_key = "BAAI/bge-m3"

        # Return cached model if available
        if model_key in self._models:
            self.logger.debug("Using cached BGE-M3 model")
            return self._models[model_key]

        # Check if sentence transformers is available
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            self.logger.warning("sentence-transformers not available, cannot load BGE-M3")
            self._load_info[model_key] = ModelLoadInfo(
                model_name=model_key,
                loaded_at=datetime.now(timezone.utc),
                load_time_seconds=0,
                load_triggered_by=triggered_by,
                error="sentence-transformers not installed"
            )
            return None

        # Load model with thread safety
        with self._lock:
            # Double-check pattern: another thread might have loaded it
            if model_key in self._models:
                return self._models[model_key]

            start_time = time.time()
            try:
                self.logger.info(f"Loading BGE-M3 model ({triggered_by} load)...")
                model = SentenceTransformer(model_key)
                load_time = time.time() - start_time

                self._models[model_key] = model
                self._load_info[model_key] = ModelLoadInfo(
                    model_name=model_key,
                    loaded_at=datetime.now(timezone.utc),
                    load_time_seconds=round(load_time, 3),
                    load_triggered_by=triggered_by
                )

                self.logger.info(f"✅ BGE-M3 model loaded in {load_time:.2f}s ({triggered_by})")
                return model
            except Exception as e:
                load_time = time.time() - start_time
                self._load_info[model_key] = ModelLoadInfo(
                    model_name=model_key,
                    loaded_at=datetime.now(timezone.utc),
                    load_time_seconds=round(load_time, 3),
                    load_triggered_by=triggered_by,
                    error=str(e)
                )
                self.logger.error(f"Failed to load BGE-M3 model: {e}")
                return None

    def is_model_loaded(self, model_key: str = "BAAI/bge-m3") -> bool:
        """Check if a model is already loaded in cache."""
        return model_key in self._models

    def get_model_load_info(self, model_key: str = "BAAI/bge-m3") -> Optional[ModelLoadInfo]:
        """Get load information for a specific model."""
        return self._load_info.get(model_key)

    def clear_cache(self):
        """Clear all cached models (useful for testing)"""
        with self._lock:
            self._models.clear()
            self._load_info.clear()
            self.logger.debug("Model cache cleared")

    def get_cache_info(self) -> dict:
        """Get information about cached models with load timing."""
        load_details = {}
        for model_key, info in self._load_info.items():
            load_details[model_key] = {
                "loaded_at": info.loaded_at.isoformat(),
                "load_time_seconds": info.load_time_seconds,
                "triggered_by": info.load_triggered_by,
                "error": info.error,
                "currently_cached": model_key in self._models
            }

        return {
            "cached_models": list(self._models.keys()),
            "cache_size": len(self._models),
            "sentence_transformers_available": SENTENCE_TRANSFORMERS_AVAILABLE,
            "load_details": load_details,
            "total_load_time_seconds": sum(
                info.load_time_seconds for info in self._load_info.values()
                if info.error is None
            )
        }


# Global model cache instance
model_cache = ModelCache()