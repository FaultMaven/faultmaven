"""Storage backend factory.

Creates the appropriate storage backend based on STORAGE_BACKEND setting.

Usage:
    from faultmaven.infrastructure.storage.factory import get_storage_backend

    backend = get_storage_backend()  # Uses settings
    backend = get_storage_backend(storage_type="s3")  # Explicit override
"""

import logging
from typing import Optional

from faultmaven.config.settings import StorageBackend
from faultmaven.infrastructure.storage.base import IFileStorageBackend, StorageType


logger = logging.getLogger(__name__)


# Singleton instance
_storage_backend: Optional[IFileStorageBackend] = None


def get_storage_backend(
    storage_type: Optional[str] = None,
    reset: bool = False,
) -> IFileStorageBackend:
    """Get the storage backend instance.

    Creates a singleton storage backend based on configuration.
    The backend type is determined by:
    1. Explicit storage_type parameter (if provided)
    2. STORAGE_BACKEND environment variable / settings

    Args:
        storage_type: Optional explicit storage type ("filesystem" or "s3")
        reset: If True, recreate the backend even if already exists

    Returns:
        IFileStorageBackend implementation

    Raises:
        ValueError: If storage type is invalid
        ImportError: If S3 is requested but boto3 not installed

    Example:
        # Use configured backend
        backend = get_storage_backend()

        # Explicit filesystem backend
        backend = get_storage_backend(storage_type="filesystem")

        # Reset singleton for testing
        backend = get_storage_backend(reset=True)
    """
    global _storage_backend

    if _storage_backend is not None and not reset:
        return _storage_backend

    # Get settings
    from faultmaven.config.settings import get_settings

    settings = get_settings()

    # Determine storage type
    if storage_type:
        backend_type = storage_type.lower()
    else:
        backend_type = settings.providers.storage_backend.value

    logger.info(f"Creating storage backend: {backend_type}")

    if backend_type == StorageBackend.FILESYSTEM.value:
        _storage_backend = _create_filesystem_backend(settings)
    elif backend_type == StorageBackend.S3.value:
        _storage_backend = _create_s3_backend(settings)
    else:
        raise ValueError(f"Unknown storage backend: {backend_type}")

    return _storage_backend


def _create_filesystem_backend(settings) -> IFileStorageBackend:
    """Create filesystem storage backend.

    Args:
        settings: FaultMaven settings

    Returns:
        FilesystemStorageBackend instance
    """
    from faultmaven.infrastructure.storage.filesystem import FilesystemStorageBackend

    # Get storage root from evidence settings
    storage_root = settings.evidence_storage.evidence_storage_root

    # Build base URL
    base_url = f"http://{settings.server.host}:{settings.server.port}"

    backend = FilesystemStorageBackend(
        storage_root=storage_root,
        base_url=base_url,
    )

    logger.info(f"Filesystem storage backend created: {storage_root}")
    return backend


def _create_s3_backend(settings) -> IFileStorageBackend:
    """Create S3 storage backend.

    Args:
        settings: FaultMaven settings

    Returns:
        S3StorageBackend instance

    Raises:
        ImportError: If boto3 is not installed
        ValueError: If required S3 settings are missing
    """
    from faultmaven.infrastructure.storage.s3 import S3StorageBackend

    # Get S3 settings (these would need to be added to settings.py)
    # For now, we use environment variables as fallback
    import os

    bucket_name = os.getenv("S3_BUCKET_NAME")
    if not bucket_name:
        raise ValueError(
            "S3_BUCKET_NAME environment variable is required for S3 storage. "
            "Set STORAGE_BACKEND=filesystem for local development."
        )

    region = os.getenv("S3_REGION", "us-east-1")
    prefix = os.getenv("S3_KEY_PREFIX", "evidence/")
    endpoint_url = os.getenv("S3_ENDPOINT_URL")  # For S3-compatible services

    backend = S3StorageBackend(
        bucket_name=bucket_name,
        region=region,
        prefix=prefix,
        endpoint_url=endpoint_url,
    )

    logger.info(f"S3 storage backend created: bucket={bucket_name}, region={region}")
    return backend


def reset_storage_backend() -> None:
    """Reset the storage backend singleton.

    Use this for testing or when configuration changes.
    """
    global _storage_backend
    _storage_backend = None
    logger.debug("Storage backend reset")
