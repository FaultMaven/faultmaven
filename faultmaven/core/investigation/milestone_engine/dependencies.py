"""The dependencies of MilestoneEngine, held once and shared with its collaborators.

MilestoneEngine and every collaborator it builds read a dependency through the one
``EngineDeps`` instance the engine owns, so each dependency has exactly one
binding: replacing ``engine.deps.X`` reaches every reader. Every field
defaults to None so a test can build a holder with only what it exercises;
``MilestoneEngine.__init__`` always passes every field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultmaven.models.interfaces import ILLMProvider
    from faultmaven.modules.knowledge.contracts import IKnowledgeService


@dataclass
class EngineDeps:
    """Every dependency MilestoneEngine and its collaborators read."""

    llm_provider: ILLMProvider | None = None
    repository: Any = None
    knowledge_service: IKnowledgeService | None = None
    checkpoint_service: Any | None = None
    investigation_tools: Any = None
    da_provider: Any | None = None
    da_model: str | None = None
    sanitizer: Any | None = None
    redis_client: Any | None = None
    report_service: Any | None = None
    team_service: Any | None = None
    share_repository: Any | None = None
    runbook_kb: Any | None = None
    conversion_service: Any | None = None
    hypothesis_manager: Any = None
    state_validator: Any = None
    progress_monitor: Any = None
    llm_error_handler: Any = None
