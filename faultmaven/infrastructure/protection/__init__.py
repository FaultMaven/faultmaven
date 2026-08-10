"""
Client protection infrastructure

This module provides the core infrastructure for protecting against
malicious or malfunctioning clients through rate limiting, request
deduplication and request hashing.
"""

from .rate_limiter import RedisRateLimiter
from .request_hasher import RequestHasher

__all__ = ["RedisRateLimiter", "RequestHasher"]
