"""Domain Services Package

Provides core business domain services for cases, sessions, data processing,
knowledge management, and strategic planning.
"""

from .case_service import CaseService
from .session_service import SessionService
from .data_service import DataService
from .planning_service import PlanningService

# Backward compatibility - Investigation services moved to modules/agent/domain/services/
# TODO: Remove after full migration complete (Phase 4)
from faultmaven.modules.agent.domain.services.investigation_orchestrator import InvestigationOrchestrator
from faultmaven.modules.agent.domain.services.investigation_service import InvestigationService

__all__ = [
    "CaseService",
    "SessionService",
    "DataService",
    "PlanningService",
    # Agent Services (backward compatibility)
    "InvestigationOrchestrator",
    "InvestigationService",
]