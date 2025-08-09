"""Model Cache Manager

Purpose: Centralized caching for expensive ML models to prevent re-downloading
and re-loading on every container initialization.

This module provides singleton-pattern caching for sentence transformers and
other ML models used across the FaultMaven system.

Key Features:
- Singleton pattern for model instances
- Lazy loading with error handling
- Memory-efficient model sharing
- Thread-safe initialization
"""

import logging
import threading
from typing import Optional

# Import with graceful fallback
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SentenceTransformer = None
    SENTENCE_TRANSFORMERS_AVAILABLE = False


class ModelCache:
    """Centralized cache for ML models with singleton pattern"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                    cls._instance._models = {}
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self.logger = logging.getLogger(__name__)
            self._models = {}
            self._initialized = True
            self.logger.debug("ModelCache initialized")
    
    def get_bge_m3_model(self) -> Optional[SentenceTransformer]:
        """
        Get cached BGE-M3 model instance.
        
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
            return None
        
        # Load model with thread safety
        with self._lock:
            # Double-check pattern: another thread might have loaded it
            if model_key in self._models:
                return self._models[model_key]
            
            try:
                self.logger.info(f"Loading BGE-M3 model for first time (this may take a moment)...")
                model = SentenceTransformer(model_key)
                self._models[model_key] = model
                self.logger.info("✅ BGE-M3 model loaded and cached successfully")
                return model
            except Exception as e:
                self.logger.error(f"Failed to load BGE-M3 model: {e}")
                return None
    
    def clear_cache(self):
        """Clear all cached models (useful for testing)"""
        with self._lock:
            self._models.clear()
            self.logger.debug("Model cache cleared")
    
    def get_cache_info(self) -> dict:
        """Get information about cached models"""
        return {
            "cached_models": list(self._models.keys()),
            "cache_size": len(self._models),
            "sentence_transformers_available": SENTENCE_TRANSFORMERS_AVAILABLE
        }


# Global model cache instance
model_cache = ModelCache()