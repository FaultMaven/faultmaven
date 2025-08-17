"""
Client protection infrastructure

This module provides the core infrastructure for protecting against
malicious or malfunctioning clients through rate limiting, request
deduplication, and timeout management.
"""

from .rate_limiter import RedisRateLimiter
from .request_hasher import RequestHasher
from .timeout_handler import TimeoutHandler

__all__ = [
    "RedisRateLimiter",
    "RequestHasher", 
    "TimeoutHandler"
]