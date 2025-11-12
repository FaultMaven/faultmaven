"""Phase Handlers for OODA Investigation Framework

This module provides phase-specific handlers that execute investigation phases
with OODA (Observe-Orient-Decide-Act) cycles.

Phase Handlers:
- Phase 0: IntakeHandler (problem confirmation, no OODA)
- Phase 1: BlastRadiusHandler (scope assessment)
- Phase 2: TimelineHandler (temporal context)
- Phase 3: HypothesisHandler (theory generation)
- Phase 4: ValidationHandler (systematic testing)
- Phase 5: SolutionHandler (fix implementation)
- Phase 6: DocumentHandler (artifact generation)

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

from faultmaven.services.agentic.phase_handlers.base import BasePhaseHandler, PhaseHandlerResult
from faultmaven.services.agentic.phase_handlers.intake_handler import IntakeHandler
from faultmaven.services.agentic.phase_handlers.blast_radius_handler import BlastRadiusHandler
from faultmaven.services.agentic.phase_handlers.timeline_handler import TimelineHandler
from faultmaven.services.agentic.phase_handlers.hypothesis_handler import HypothesisHandler
from faultmaven.services.agentic.phase_handlers.validation_handler import ValidationHandler
from faultmaven.services.agentic.phase_handlers.solution_handler import SolutionHandler
from faultmaven.services.agentic.phase_handlers.document_handler import DocumentHandler

__all__ = [
    "BasePhaseHandler",
    "PhaseHandlerResult",
    "IntakeHandler",
    "BlastRadiusHandler",
    "TimelineHandler",
    "HypothesisHandler",
    "ValidationHandler",
    "SolutionHandler",
    "DocumentHandler",
]
