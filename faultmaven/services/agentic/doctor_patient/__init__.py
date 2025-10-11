"""Doctor/Patient architecture services - Sub-Agent Orchestrator.

This module contains the sub-agent orchestrator implementation following
Anthropic's context engineering best practices with 49% token reduction.

Architecture: 6 specialized phase agents + orchestrator
- IntakeAgent (Phase 0): Problem detection
- BlastRadiusAgent (Phase 1): Impact assessment
- TimelineAgent (Phase 2): Change correlation
- HypothesisAgent (Phase 3): Root cause theories
- ValidationAgent (Phase 4): Hypothesis testing
- SolutionAgent (Phase 5): Resolution recommendations
"""

from .orchestrator_integration import process_turn_with_orchestrator
from .sub_agents.orchestrator import DiagnosticOrchestrator

__all__ = [
    "process_turn_with_orchestrator",
    "DiagnosticOrchestrator",
]
