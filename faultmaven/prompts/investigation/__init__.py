"""Investigation Framework Prompts (v3.2.0 OODA)

This module provides prompt templates for the OODA-based investigation framework
with dual engagement modes and flexible phase progression.

Prompt Categories:
- Consultant Mode: Phase 0 (Intake) - Expert colleague providing guidance
- Lead Investigator Mode: Phases 1-6 - War room lead driving resolution
- Strategy-Specific: Active Incident vs Post-Mortem variations
- Phase-Specific: Tailored prompts for each investigation phase

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

from faultmaven.prompts.investigation.consultant_mode import (
    get_consultant_mode_prompt,
    CONSULTANT_SYSTEM_PROMPT,
)
from faultmaven.prompts.investigation.lead_investigator import (
    get_lead_investigator_prompt,
    LEAD_INVESTIGATOR_SYSTEM_PROMPT,
)
from faultmaven.prompts.investigation.strategy_prompts import (
    get_strategy_specific_guidance,
    ACTIVE_INCIDENT_GUIDANCE,
    POST_MORTEM_GUIDANCE,
)

__all__ = [
    "get_consultant_mode_prompt",
    "get_lead_investigator_prompt",
    "get_strategy_specific_guidance",
    "CONSULTANT_SYSTEM_PROMPT",
    "LEAD_INVESTIGATOR_SYSTEM_PROMPT",
    "ACTIVE_INCIDENT_GUIDANCE",
    "POST_MORTEM_GUIDANCE",
]
