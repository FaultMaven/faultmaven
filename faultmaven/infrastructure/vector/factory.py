"""Vector backend factory.

Creates the appropriate vector backend based on VECTOR_BACKEND setting.

Usage:
    from faultmaven.infrastructure.vector.factory import get_vector_backend

    backend = get_vector_backend()  # Uses settings
    backend = get_vector_backend(backend_type="pinecone")  # Explicit override
"""

import logging
import os
from typing import Optional

from faultmaven.config.settings import VectorBackend
from faultmaven.infrastructure.vector.base import IVectorBackend, VectorBackendType

logger = logging.getLogger(__name__)


# Singleton instance
_vector_backend: Optional[IVectorBackend] = None


def get_vector_backend(
    backend_type: Optional[str] = None,
    reset: bool = False,
) -> IVectorBackend:
    """Get the vector backend instance.

    Creates a singleton vector backend based on configuration.
    The backend type is determined by:
    1. Explicit backend_type parameter (if provided)
    2. VECTOR_BACKEND environment variable / settings

    Args:
        backend_type: Optional explicit backend type ("chroma" or "pinecone")
        reset: If True, recreate the backend even if already exists

    Returns:
        IVectorBackend implementation

    Raises:
        ValueError: If backend type is invalid
        ImportError: If required package not installed

    Example:
        # Use configured backend
        backend = get_vector_backend()

        # Explicit Pinecone backend
        backend = get_vector_backend(backend_type="pinecone")

        # Reset singleton for testing
        backend = get_vector_backend(reset=True)
    """
    global _vector_backend

    if _vector_backend is not None and not reset:
        return _vector_backend

    # Get settings
    from faultmaven.config.settings import get_settings

    settings = get_settings()

    # Determine backend type
    if backend_type:
        backend = backend_type.lower()
    else:
        backend = settings.providers.vector_backend.value

    logger.info(f"Creating vector backend: {backend}")

    if backend == VectorBackend.CHROMA.value:
        _vector_backend = _create_chroma_backend(settings)
    elif backend == VectorBackend.PINECONE.value:
        _vector_backend = _create_pinecone_backend(settings)
    else:
        raise ValueError(f"Unknown vector backend: {backend}")

    return _vector_backend


def _create_chroma_backend(settings) -> IVectorBackend:
    """Create ChromaDB vector backend.

    Args:
        settings: FaultMaven settings

    Returns:
        ChromaVectorBackend instance
    """
    from faultmaven.infrastructure.vector.chroma import ChromaVectorBackend

    # Get Chroma settings
    # Check if settings has a proper embedding configuration
    try:
        if hasattr(settings, "embedding") and hasattr(
            settings.embedding, "chroma_persist_directory"
        ):
            persist_directory = settings.embedding.chroma_persist_directory
        else:
            persist_directory = "./data/chroma"

        if hasattr(settings, "embedding") and hasattr(
            settings.embedding, "chroma_collection_name"
        ):
            collection_name = settings.embedding.chroma_collection_name
        else:
            collection_name = "faultmaven_kb"
    except AttributeError:
        persist_directory = "./data/chroma"
        collection_name = "faultmaven_kb"

    # Check for HTTP client configuration
    chroma_host = os.getenv("CHROMADB_HOST")
    chroma_port = os.getenv("CHROMADB_PORT")

    backend = ChromaVectorBackend(
        persist_directory=persist_directory,
        default_collection=collection_name,
        host=chroma_host,
        port=int(chroma_port) if chroma_port else None,
    )

    logger.info(f"ChromaDB backend created: {persist_directory}")
    return backend


def _create_pinecone_backend(settings) -> IVectorBackend:
    """Create Pinecone vector backend.

    Args:
        settings: FaultMaven settings

    Returns:
        PineconeVectorBackend instance

    Raises:
        ImportError: If pinecone is not installed
        ValueError: If required settings are missing
    """
    from faultmaven.infrastructure.vector.pinecone import PineconeVectorBackend

    # Get Pinecone settings from environment
    api_key = os.getenv("PINECONE_API_KEY")
    if not api_key:
        raise ValueError(
            "PINECONE_API_KEY environment variable is required for Pinecone backend. "
            "Set VECTOR_BACKEND=chroma for local development."
        )

    index_name = os.getenv("PINECONE_INDEX", "faultmaven")
    environment = os.getenv("PINECONE_ENVIRONMENT", "us-east-1")
    dimension = int(os.getenv("PINECONE_DIMENSION", "1536"))

    backend = PineconeVectorBackend(
        api_key=api_key,
        index_name=index_name,
        environment=environment,
        dimension=dimension,
    )

    logger.info(f"Pinecone backend created: index={index_name}")
    return backend


def reset_vector_backend() -> None:
    """Reset the vector backend singleton.

    Use this for testing or when configuration changes.
    """
    global _vector_backend
    _vector_backend = None
    logger.debug("Vector backend reset")
