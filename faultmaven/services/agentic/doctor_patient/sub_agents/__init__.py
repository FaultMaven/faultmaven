"""Sub-agent architecture for phase-specific troubleshooting.

Implements Anthropic's context engineering principles with specialized
agents for each diagnostic phase. Each agent has 30-50% smaller prompts
compared to the monolithic approach.

Architecture:
    Orchestrator → Routes to phase-specific agent → Specialized processing

Phase Agents (Complete Implementation):
    - IntakeAgent (Phase 0): Identify if problem exists (~300 tokens)
    - BlastRadiusAgent (Phase 1): Define scope and impact (~500 tokens)
    - TimelineAgent (Phase 2): Establish when/what changed (~550 tokens)
    - HypothesisAgent (Phase 3): Generate root cause theories (~400 tokens)
    - ValidationAgent (Phase 4): Test hypotheses with evidence (~700 tokens)
    - SolutionAgent (Phase 5): Recommend specific fixes (~650 tokens)

Total context savings: ~49% reduction vs monolithic 1300-token prompt

Usage:
    from faultmaven.services.agentic.doctor_patient.sub_agents import DiagnosticOrchestrator

    orchestrator = DiagnosticOrchestrator(llm_client)
    response = await orchestrator.process_query(
        user_query="API is down",
        diagnostic_state=current_state,
        conversation_history=messages,
        case_id="case-123"
    )
"""

from .base import PhaseAgent, PhaseContext, PhaseAgentResponse
from .orchestrator import DiagnosticOrchestrator
from .intake_agent import IntakeAgent
from .blast_radius_agent import BlastRadiusAgent
from .timeline_agent import TimelineAgent
from .hypothesis_agent import HypothesisAgent
from .validation_agent import ValidationAgent
from .solution_agent import SolutionAgent

__all__ = [
    # Base classes
    "PhaseAgent",
    "PhaseContext",
    "PhaseAgentResponse",
    # Orchestrator (primary interface)
    "DiagnosticOrchestrator",
    # Individual agents (for testing/direct use)
    "IntakeAgent",
    "BlastRadiusAgent",
    "TimelineAgent",
    "HypothesisAgent",
    "ValidationAgent",
    "SolutionAgent",
]
