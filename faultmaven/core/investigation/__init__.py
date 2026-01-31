"""Investigation Core Module

This module provides the core investigation framework for FaultMaven's
Data-Driven and Opportunistic troubleshooting system.

Components:
- hypothesis_manager: Hypothesis lifecycle management
- milestone_engine: Data-driven investigation engine
"""

from faultmaven.core.investigation.hypothesis_manager import (
    HypothesisManager,
    create_hypothesis_manager,
)
from faultmaven.core.investigation.milestone_engine import MilestoneEngine

__all__ = [
    "HypothesisManager",
    "create_hypothesis_manager",
    "MilestoneEngine",
]
