"""Investigation Core Module

This module provides the core investigation framework for FaultMaven's
OODA (Observe-Orient-Decide-Act) based troubleshooting system.

Components:
- phases: 7 investigation phase definitions and transitions
- ooda_engine: OODA cycle execution and iteration management
- engagement_modes: Consultant vs Lead Investigator mode management
- hypothesis_manager: Hypothesis lifecycle and confidence decay
- memory_manager: Hierarchical memory with hot/warm/cold tiers
- strategy_selector: Active Incident vs Post-Mortem strategy selection

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

from faultmaven.core.investigation.phases import (
    PhaseDefinition,
    PhaseTransitionRule,
    PhaseCompletionCriteria,
    get_phase_definition,
    can_transition,
    detect_entry_phase,
)

from faultmaven.core.investigation.ooda_engine import (
    OODAEngine,
    AdaptiveIntensityController,
)

from faultmaven.core.investigation.engagement_modes import (
    EngagementModeManager,
    ProblemSignalDetector,
)

from faultmaven.core.investigation.hypothesis_manager import (
    HypothesisManager,
    create_hypothesis_manager,
)

from faultmaven.core.investigation.memory_manager import (
    HierarchicalMemoryManager,
    MemoryCompressionEngine,
)

from faultmaven.core.investigation.strategy_selector import (
    InvestigationStrategySelector,
    StrategyConfig,
)

__all__ = [
    # Phase management
    "PhaseDefinition",
    "PhaseTransitionRule",
    "PhaseCompletionCriteria",
    "get_phase_definition",
    "can_transition",
    "detect_entry_phase",
    # OODA engine
    "OODAEngine",
    "AdaptiveIntensityController",
    # Engagement modes
    "EngagementModeManager",
    "ProblemSignalDetector",
    # Hypothesis management
    "HypothesisManager",
    "create_hypothesis_manager",
    # Memory management
    "HierarchicalMemoryManager",
    "MemoryCompressionEngine",
    # Strategy selection
    "InvestigationStrategySelector",
    "StrategyConfig",
]
