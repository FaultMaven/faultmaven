"""S3 storage backend implementation.

Provides AWS S3 storage.

Usage:
    backend = S3StorageBackend(
        bucket_name="my-bucket",
        region="us-east-1",
    )
    await backend.store_file("evidence/file.log", data)

Configuration:
    AWS credentials are loaded from:
    1. Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
    2. AWS credentials file (~/.aws/credentials)
    3. IAM role (when running on AWS infrastructure)
"""

import asyncio
import logging
from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, List, Optional

from faultmaven.infrastructure.storage.base import (
    IFileStorageBackend,
    StorageType,
    StoredFile,
)
from faultmaven.utils.optional_dependency import module_is_usable

logger = logging.getLogger(__name__)


# Feature detection for boto3.
#
# NOT `BOTO3_AVAILABLE = True`: an empty leftover `site-packages/boto3/` tree
# imports cleanly as a PEP 420 namespace package, and botocore is a separate
# distribution so its from-imports below would still succeed — leaving the flag
# True and `boto3.client(...)` raising AttributeError inside the backend. See
# faultmaven/utils/optional_dependency.py.
try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError

    BOTO3_AVAILABLE = module_is_usable(boto3, "client")
    _boto3_reason = None if BOTO3_AVAILABLE else "resolved to a namespace package"
except ImportError:
    BOTO3_AVAILABLE = False
    _boto3_reason = "not installed"

if not BOTO3_AVAILABLE:
    # One cleanup for both causes — duplicating it meant a fifth symbol later
    # had to be remembered twice. Note `ClientError = Exception` widens every
    # `except ClientError` in this file to a catch-all; that is only sound
    # because S3StorageBackend.__init__ refuses to construct when the flag is
    # False, so none of those handlers is reachable in this state.
    boto3 = None
    BotoConfig = None
    ClientError = Exception
    logger.debug("boto3 %s - S3 backend unavailable", _boto3_reason)


class S3StorageBackend(IFileStorageBackend):
    """AWS S3 storage backend.

    Attributes:
        bucket_name: S3 bucket name
        region: AWS region (e.g., "us-east-1")
        prefix: Optional key prefix for all operations
    """

    def __init__(
        self,
        bucket_name: str,
        region: str = "us-east-1",
        prefix: str = "",
        endpoint_url: Optional[str] = None,
        signature_version: str = "s3v4",
    ):
        """Initialize S3 storage backend.

        Args:
            bucket_name: S3 bucket name
            region: AWS region
            prefix: Optional key prefix (e.g., "evidence/")
            endpoint_url: Optional custom S3 endpoint (for S3-compatible services)
            signature_version: Signature version (default: s3v4)

        Raises:
            ImportError: If boto3 is not installed
        """
        if not BOTO3_AVAILABLE:
            raise ImportError(
                "boto3 is required for S3 storage. " "Install with: pip install boto3"
            )

        self.bucket_name = bucket_name
        self.region = region
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        self.endpoint_url = endpoint_url

        # Configure S3 client with signature version
        config = BotoConfig(
            signature_version=signature_version,
            region_name=region,
        )

        client_kwargs = {
            "service_name": "s3",
            "config": config,
            "region_name": region,
        }
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url

        self._client = boto3.client(**client_kwargs)

        logger.info(
            f"S3 storage initialized: bucket={bucket_name}, region={region}, "
            f"prefix={self.prefix or '(none)'}"
        )

    def _get_full_key(self, key: str) -> str:
        """Get full S3 key with prefix.

        Args:
            key: Storage key/path

        Returns:
            Full S3 key with prefix
        """
        return f"{self.prefix}{key}"

    async def _call(self, method_name: str, **kwargs: Any) -> Any:
        """Run a blocking boto3 client call off the event loop.

        boto3 is synchronous: awaiting a client call directly would block the
        whole loop for the duration of the S3 round-trip, stalling unrelated
        requests — including ``/health``, which a Kubernetes liveness probe
        escalates into a pod kill. Every S3 call in this backend goes through
        here.
        """
        method = getattr(self._client, method_name)
        return await asyncio.to_thread(partial(method, **kwargs))

    async def store_file(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> StoredFile:
        """Store a file directly to S3.

        Args:
            key: Storage key/path for the file
            data: File content as bytes
            content_type: MIME type of the file
            metadata: Optional metadata to attach

        Returns:
            StoredFile with file metadata
        """
        full_key = self._get_full_key(key)

        put_kwargs = {
            "Bucket": self.bucket_name,
            "Key": full_key,
            "Body": data,
            "ContentType": content_type,
        }

        if metadata:
            put_kwargs["Metadata"] = metadata

        await self._call("put_object", **put_kwargs)

        logger.info(f"Stored file to S3: {full_key} ({len(data)} bytes)")

        return StoredFile(
            key=key,
            size_bytes=len(data),
            content_type=content_type,
            created_at=datetime.now(timezone.utc),
            metadata=metadata,
        )

    async def retrieve_file(self, key: str) -> Optional[bytes]:
        """Retrieve file content from S3.

        Args:
            key: Storage key/path for the file

        Returns:
            File content as bytes, or None if not found
        """
        full_key = self._get_full_key(key)

        def _get() -> bytes:
            # get_object and the streaming body read are both blocking, so
            # they belong in the same worker thread — splitting them would
            # put the (potentially large) download back on the event loop.
            response = self._client.get_object(
                Bucket=self.bucket_name,
                Key=full_key,
            )
            return response["Body"].read()

        try:
            data = await asyncio.to_thread(_get)
            logger.debug(f"Retrieved file from S3: {full_key} ({len(data)} bytes)")
            return data
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchKey":
                return None
            raise

    async def delete_file(self, key: str) -> bool:
        """Delete a file from S3.

        Args:
            key: Storage key/path for the file

        Returns:
            True if file was deleted, False if not found
        """
        full_key = self._get_full_key(key)

        # Check if exists first
        try:
            await self._call("head_object", Bucket=self.bucket_name, Key=full_key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "404":
                return False
            raise

        await self._call("delete_object", Bucket=self.bucket_name, Key=full_key)

        logger.info(f"Deleted file from S3: {full_key}")
        return True

    async def get_file_info(self, key: str) -> Optional[StoredFile]:
        """Get file metadata from S3 without downloading content.

        Args:
            key: Storage key/path for the file

        Returns:
            StoredFile with metadata, or None if not found
        """
        full_key = self._get_full_key(key)

        try:
            response = await self._call(
                "head_object",
                Bucket=self.bucket_name,
                Key=full_key,
            )

            return StoredFile(
                key=key,
                size_bytes=response["ContentLength"],
                content_type=response.get("ContentType", "application/octet-stream"),
                created_at=response.get("LastModified", datetime.now(timezone.utc)),
                metadata=response.get("Metadata"),
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "404":
                return None
            raise

    async def list_keys(self, prefix: str = "") -> List[str]:
        """List object keys under a prefix.

        Args:
            prefix: Only return keys starting with this string. Combined with
                the backend's configured key prefix before the request.

        Returns:
            Storage keys with the backend prefix stripped, so they round-trip
            through the same form ``store_file`` accepted.
        """
        full_prefix = self._get_full_key(prefix)

        def _list() -> List[str]:
            # Paginate: list_objects_v2 caps at 1000 keys per response, and an
            # evidence bucket will exceed that. A single call would silently
            # truncate the sweep, which for orphan cleanup means quietly
            # leaking every file past the first page.
            keys = []
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=full_prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"][len(self.prefix) :])
            return keys

        return await asyncio.to_thread(_list)

    def get_storage_type(self) -> StorageType:
        """Get the storage backend type.

        Returns:
            StorageType.S3
        """
        return StorageType.S3
