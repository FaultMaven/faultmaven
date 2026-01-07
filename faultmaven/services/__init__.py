"""Services Package

This package contains the service layer components that orchestrate
business logic and coordinate between different parts of the system.

The services are organized into logical groups:
- agentic/: Complete agentic framework with orchestration, engines, management, and safety
- domain/: Core business domain services (cases, data, planning)
- analytics/: ML and analytics services (dashboard, confidence scoring)
- base.py: Base service class for consistent patterns
- converters/: Data transformation utilities

Note: SessionService has been moved to modules/auth/domain/services/
Import AuthSessionService from faultmaven.modules.auth.domain.services.auth_session_service
"""

# Import from new organized structure
# Note: AgentService import removed to avoid circular dependency
# Import AgentService directly from faultmaven.services.agentic if needed
from .domain import (
    CaseService,
    DataService,
    PlanningService
)
from .analytics import (
    AnalyticsDashboardService,
    ConfidenceService
)
from .base import BaseService

# Re-export SessionService from auth module for backward compatibility
from faultmaven.modules.auth.domain.services.auth_session_service import AuthSessionService as SessionService

__all__ = [
    # Base
    "BaseService",
    # Domain Services
    "CaseService",
    "SessionService",  # Re-exported from modules/auth
    "DataService",
    "PlanningService",
    # Analytics Services
    "AnalyticsDashboardService",
    "ConfidenceService",
]