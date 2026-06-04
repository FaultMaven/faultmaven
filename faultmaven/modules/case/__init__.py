"""Case Module - Vertical Slice

Manages case lifecycle, investigation sessions, and case data ingestion.

Public API:
    From domain.models:
        - Case, CaseState, CaseSeverity, MessageType
        - InvestigationProgress, Evidence, Hypothesis, Solution

    From domain.services (import directly to avoid circular imports):
        - CaseService, InvestigationSessionService, CaseActionManager

Structure:
- api/: API routes for case endpoints
- domain/: Domain models and services
- infrastructure/: Persistence layer (repositories)
"""

# Domain models - core case types
from faultmaven.modules.case.domain.models import (  # Core; Investigation; Evidence & Hypothesis; Solution
    Case,
    CaseSeverity,
    CaseState,
    Evidence,
    EvidenceCategory,
    Hypothesis,
    HypothesisState,
    InvestigationProgress,
    InvestigationStrategy,
    MessageType,
    Solution,
    SolutionType,
)

# Domain services - import directly to avoid circular imports:
# from faultmaven.modules.case.domain.services.case_service import CaseService
# from faultmaven.modules.case.domain.services.investigation_session_service import InvestigationSessionService

__all__ = [
    # Core
    "Case",
    "CaseState",
    "CaseSeverity",
    "MessageType",
    # Investigation
    "InvestigationProgress",
    "InvestigationStrategy",
    # Evidence & Hypothesis
    "Evidence",
    "EvidenceCategory",
    "Hypothesis",
    "HypothesisState",
    # Solution
    "Solution",
    "SolutionType",
]
