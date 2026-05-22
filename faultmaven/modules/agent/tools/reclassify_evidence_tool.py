"""Reclassify Evidence Tool — Phase 1.5

Gives the agent a way to correct the classifier's data-type decision on
an existing evidence row without the user leaving the conversation.
Triggered by corrective-intent phrases ("that's actually a log file",
"treat server.log as config") — see the prompt rule in
``core/investigation/prompts/templates.py``.

The tool delegates to ``InvestigationService.reclassify_evidence`` which
handles auth, storage fetch, preprocessing re-run, and persistence. This
tool just validates inputs and passes them through.

Design Reference:
    docs/working/WIP-data-processing-improvement-plan.md Phase 1.5
"""

import logging
from typing import Any, Dict

from faultmaven.config.settings import get_settings
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceException,
)
from faultmaven.models.api import DataType
from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext

logger = logging.getLogger(__name__)


class ReclassifyEvidenceTool(AgentTool):
    """Reclassify an evidence row under a user-specified data type.

    Use this when the user corrects the classifier (e.g. "that's a log,
    not metrics"). Re-runs preprocessing on the stored raw file, updates
    the evidence's structural_index, and records the change in
    ``metadata.extractor.attempts``.

    Behaviour in the agent prompt is to CALL this tool first when such a
    correction is detected, then respond to the substance of the user's
    actual question using the re-extracted structural index (which
    becomes available in the agent's context on the next turn).
    """

    def __init__(self, investigation_service: Any = None):
        self.investigation_service = investigation_service

    @property
    def name(self) -> str:
        return "reclassify_evidence"

    @property
    def description(self) -> str:
        return (
            "Reclassify a previously uploaded evidence file under a "
            "different data type. Use this when the user corrects the "
            "classifier — e.g. 'that's actually a log file', 'treat "
            "server.log as config'. The evidence is re-extracted with "
            "the chosen data type; the new structural index is available "
            "in context on the next turn. Returns the updated "
            "classification so the agent can confirm the change. Call "
            "list_evidence first to find the evidence_id."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "evidence_id": {
                    "type": "string",
                    "description": (
                        "The evidence_id to reclassify. Get this from "
                        "list_evidence or the <evidence id=...> tag in "
                        "your context."
                    ),
                },
                "data_type": {
                    "type": "string",
                    "enum": [t.value for t in DataType],
                    "description": (
                        "The correct data type for this evidence. Must "
                        "be one of the DataType enum values."
                    ),
                },
            },
            "required": ["evidence_id", "data_type"],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute reclassification through the investigation service."""
        if not get_settings().preprocessing.reclassify_enabled:
            return ToolResult(
                success=False,
                data=None,
                error=(
                    "Reclassification is not enabled on this deployment. "
                    "Inform the user and do not retry."
                ),
            )

        if self.investigation_service is None:
            return ToolResult(
                success=False,
                data=None,
                error="Investigation service not wired; reclassification unavailable",
            )

        evidence_id = params.get("evidence_id")
        data_type_raw = params.get("data_type")
        if not evidence_id or not isinstance(evidence_id, str):
            return ToolResult(
                success=False,
                data=None,
                error="evidence_id is required (string)",
            )
        if not data_type_raw or not isinstance(data_type_raw, str):
            return ToolResult(
                success=False,
                data=None,
                error="data_type is required (string)",
            )
        try:
            data_type = DataType(data_type_raw)
        except ValueError:
            valid = ", ".join(t.value for t in DataType)
            return ToolResult(
                success=False,
                data=None,
                error=f"Unknown data_type '{data_type_raw}'. Valid: {valid}",
            )

        try:
            updated = await self.investigation_service.reclassify_evidence(
                case_id=context.case_id,
                evidence_id=evidence_id,
                user_id=context.user_id,
                data_type=data_type,
                trigger="agent_tool",
            )
        except NotFoundError as e:
            return ToolResult(success=False, data=None, error=str(e))
        except AuthorizationError as e:
            return ToolResult(success=False, data=None, error=str(e))
        except ConflictError as e:
            # No backing file — nothing to re-extract.
            return ToolResult(success=False, data=None, error=str(e))
        except ServiceException as e:
            return ToolResult(success=False, data=None, error=str(e))
        except Exception as e:
            logger.exception("reclassify_evidence tool failed: %s", e)
            return ToolResult(
                success=False,
                data=None,
                error=f"Reclassification failed: {str(e)}",
            )

        return ToolResult(
            success=True,
            data={
                "evidence_id": updated.evidence_id,
                "data_type": updated.source_type.value,
                "summary": updated.summary,
                "note": (
                    "Reclassification complete. The re-extracted "
                    "structural index is now attached to this evidence "
                    "and will appear in your context on the next turn."
                ),
            },
        )
