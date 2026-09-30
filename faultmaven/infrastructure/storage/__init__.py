"""Storage backends for FaultMaven.

This package provides storage-neutral file storage backends.

Storage Backend Selection:
    Set STORAGE_BACKEND environment variable:
    - filesystem (default): Local filesystem
    - s3: AWS S3

Usage:
    from faultmaven.infrastructure.storage import get_storage_backend

    backend = get_storage_backend()

    # File operations
    await backend.store_file("evidence/file.log", data, content_type="text/plain")
    data = await backend.retrieve_file("evidence/file.log")

S3 Configuration (when STORAGE_BACKEND=s3):
    - S3_BUCKET_NAME: Required bucket name
    - S3_REGION: AWS region (default: us-east-1)
    - S3_KEY_PREFIX: Optional key prefix (default: evidence/)
    - S3_ENDPOINT_URL: Optional custom endpoint (for S3-compatible services)

AWS credentials are loaded from environment or IAM role.
"""

from faultmaven.infrastructure.storage.base import (
    IFileStorageBackend,
    StorageType,
    StoredFile,
)
from faultmaven.infrastructure.storage.factory import (
    get_storage_backend,
    reset_storage_backend,
)
from faultmaven.infrastructure.storage.filesystem import FilesystemStorageBackend

# S3 backend is optional (requires boto3)
try:
    from faultmaven.infrastructure.storage.s3 import S3StorageBackend

    S3_AVAILABLE = True
except ImportError:
    S3StorageBackend = None  # type: ignore
    S3_AVAILABLE = False

__all__ = [
    # Interface and types
    "IFileStorageBackend",
    "StoredFile",
    "StorageType",
    # Factory
    "get_storage_backend",
    "reset_storage_backend",
    # Implementations
    "FilesystemStorageBackend",
    "S3StorageBackend",
    "S3_AVAILABLE",
]
