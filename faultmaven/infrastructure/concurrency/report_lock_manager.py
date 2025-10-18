"""Report Generation Lock Manager

Purpose: Application-level locking for report generation to prevent race conditions

This module provides distributed locking using Redis to ensure only one report
generation process runs per case at a time, preventing version conflicts and
concurrent modification issues.

Key Features:
- Redis-based distributed locks
- Timeout protection to prevent deadlocks
- Automatic lock release on completion
- Context manager support for clean usage
"""

import asyncio
import logging
from typing import Optional
from contextlib import asynccontextmanager
import redis.asyncio as redis


class ReportLockManager:
    """
    Manages distributed locks for report generation operations.

    Uses Redis SETNX (SET if Not eXists) for atomic lock acquisition
    with automatic expiration to prevent deadlocks.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        lock_timeout_seconds: int = 300,  # 5 minutes default
        poll_interval_seconds: float = 0.5
    ):
        """
        Initialize the lock manager.

        Args:
            redis_client: Async Redis client
            lock_timeout_seconds: Lock expiration time (prevents deadlocks)
            poll_interval_seconds: How often to retry lock acquisition
        """
        self.redis = redis_client
        self.lock_timeout = lock_timeout_seconds
        self.poll_interval = poll_interval_seconds
        self.logger = logging.getLogger(__name__)

    def _lock_key(self, case_id: str) -> str:
        """Generate Redis key for case report generation lock."""
        return f"lock:report_generation:case:{case_id}"

    async def acquire_lock(
        self,
        case_id: str,
        wait_timeout: Optional[int] = None
    ) -> bool:
        """
        Acquire lock for report generation on a case.

        Args:
            case_id: Case identifier
            wait_timeout: Max seconds to wait for lock (None = don't wait)

        Returns:
            True if lock acquired, False if timeout/failed
        """
        lock_key = self._lock_key(case_id)
        lock_value = f"locked_at_{asyncio.get_event_loop().time()}"

        # Try immediate acquisition
        acquired = await self.redis.set(
            lock_key,
            lock_value,
            nx=True,  # Only set if not exists
            ex=self.lock_timeout  # Auto-expire after timeout
        )

        if acquired:
            self.logger.debug(
                f"Acquired report generation lock for case {case_id}"
            )
            return True

        # If wait_timeout is None, don't retry
        if wait_timeout is None:
            self.logger.debug(
                f"Failed to acquire lock for case {case_id} (already locked)"
            )
            return False

        # Poll for lock with timeout
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < wait_timeout:
            await asyncio.sleep(self.poll_interval)

            acquired = await self.redis.set(
                lock_key,
                lock_value,
                nx=True,
                ex=self.lock_timeout
            )

            if acquired:
                self.logger.debug(
                    f"Acquired report generation lock for case {case_id} "
                    f"after {asyncio.get_event_loop().time() - start_time:.2f}s"
                )
                return True

        self.logger.warning(
            f"Failed to acquire lock for case {case_id} "
            f"after {wait_timeout}s timeout"
        )
        return False

    async def release_lock(self, case_id: str) -> bool:
        """
        Release report generation lock for a case.

        Args:
            case_id: Case identifier

        Returns:
            True if lock released, False if no lock existed
        """
        lock_key = self._lock_key(case_id)
        deleted = await self.redis.delete(lock_key)

        if deleted:
            self.logger.debug(
                f"Released report generation lock for case {case_id}"
            )
            return True
        else:
            self.logger.debug(
                f"No lock to release for case {case_id} (already released)"
            )
            return False

    @asynccontextmanager
    async def lock(
        self,
        case_id: str,
        wait_timeout: Optional[int] = 30
    ):
        """
        Context manager for acquiring and auto-releasing locks.

        Usage:
            async with lock_manager.lock(case_id):
                # Generate reports safely
                report = await generate_report(case_id)

        Args:
            case_id: Case identifier
            wait_timeout: Max seconds to wait for lock

        Raises:
            LockAcquisitionError: If lock cannot be acquired
        """
        acquired = await self.acquire_lock(case_id, wait_timeout)

        if not acquired:
            raise LockAcquisitionError(
                f"Failed to acquire report generation lock for case {case_id}"
            )

        try:
            yield
        finally:
            await self.release_lock(case_id)

    async def is_locked(self, case_id: str) -> bool:
        """
        Check if a case has an active report generation lock.

        Args:
            case_id: Case identifier

        Returns:
            True if locked, False if available
        """
        lock_key = self._lock_key(case_id)
        exists = await self.redis.exists(lock_key)
        return bool(exists)


class LockAcquisitionError(Exception):
    """Raised when lock cannot be acquired within timeout."""
    pass
