"""Concurrency Infrastructure

Purpose: Distributed locking and concurrency control utilities

This package provides infrastructure for managing concurrent access to shared
resources in a distributed FaultMaven deployment.
"""

from .report_lock_manager import ReportLockManager, LockAcquisitionError

__all__ = ["ReportLockManager", "LockAcquisitionError"]
