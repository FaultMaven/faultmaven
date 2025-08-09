"""
FaultMaven Enhanced Logging Infrastructure

This module provides a production-ready logging system that eliminates duplicate
log entries through architectural patterns, clear responsibility boundaries, and
unified request coordination.

Key features:
- Thread-safe request context management using contextvars
- Automatic deduplication of log operations
- Layer-specific performance tracking
- Error cascade prevention
- Unified logging patterns across all application layers

Phase 1 Components:
- coordinator: Request-scoped logging coordination and context management
- config: Structlog configuration with JSON formatting and processors

Phase 2 Components:
- unified: UnifiedLogger with operation context managers and boundary logging
- Factory functions for creating appropriate logger instances per layer
"""

from .coordinator import LoggingCoordinator, RequestContext, request_context, ErrorContext, PerformanceTracker
from .config import get_logger, FaultMavenLogger
from .unified import UnifiedLogger, get_unified_logger, clear_logger_cache

__all__ = [
    # Phase 1 - Core Infrastructure
    'LoggingCoordinator',
    'RequestContext', 
    'ErrorContext',
    'PerformanceTracker',
    'request_context',
    'get_logger',
    'FaultMavenLogger',
    
    # Phase 2 - Unified Patterns
    'UnifiedLogger',
    'get_unified_logger',
    'clear_logger_cache',
]