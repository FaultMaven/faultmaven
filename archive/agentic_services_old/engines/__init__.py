"""Agentic Engines Package

Core processing engines for the agentic framework, including
workflow execution and response synthesis.

NOTE: QueryClassificationEngine has been superseded by the doctor/patient
prompting architecture. See docs/architecture/ARCHITECTURE_EVOLUTION.md
"""

from .workflow_engine import BusinessLogicWorkflowEngine
from .response_synthesizer import ResponseSynthesizer

__all__ = [
    "BusinessLogicWorkflowEngine",
    "ResponseSynthesizer"
]
