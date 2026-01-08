"""Services Package

This package contains legacy service layer components and backward compatibility re-exports.

After module extraction, most services have moved to modules/:
- CaseService, DataService → modules/case/domain/services/
- SessionService → modules/auth/domain/services/
- PlanningService → REMOVED (no longer exists)

Remaining services in this package:
- agentic/: Complete agentic framework with orchestration, engines, management, and safety
- analytics/: ML and analytics services (dashboard, confidence scoring)
- base.py: Base service class for consistent patterns
- converters/: Data transformation utilities
"""

# Import from modules (after module extraction)
# Note: AgentService import removed to avoid circular dependency
# Import AgentService directly from faultmaven.services.agentic if needed
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.domain.services.case_data_ingestion_service import CaseDataIngestionService as DataService

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
    "DataService",  # Re-exported as CaseDataIngestionService
    # Analytics Services
    "AnalyticsDashboardService",
    "ConfidenceService",
]