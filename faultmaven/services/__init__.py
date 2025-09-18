"""Services Package

This package contains the service layer components that orchestrate
business logic and coordinate between different parts of the system.

The services are organized into logical groups:
- agentic/: Complete agentic framework with orchestration, engines, management, and safety
- domain/: Core business domain services (cases, sessions, data, knowledge, planning)
- analytics/: ML and analytics services (dashboard, confidence scoring)
- base.py: Base service class for consistent patterns
- converters/: Data transformation utilities
"""

# Import from new organized structure
# Note: AgentService import removed to avoid circular dependency
# Import AgentService directly from faultmaven.services.agentic if needed
from .domain import (
    CaseService,
    SessionService,
    DataService,
    KnowledgeService,
    PlanningService
)
from .analytics import (
    AnalyticsDashboardService,
    ConfidenceService
)
from .base import BaseService

__all__ = [
    # Base
    "BaseService",
    # Domain Services
    "CaseService",
    "SessionService",
    "DataService",
    "KnowledgeService",
    "PlanningService",
    # Analytics Services
    "AnalyticsDashboardService",
    "ConfidenceService",
]