"""Find Entity Tool — Phase 4c.

Case-scoped entity lookup. The LLM calls this when the user or a
hypothesis references a specific IP, hostname, username, PID, port,
service, or path and the agent needs to know where (and how often) it
appears across the case's evidence.

Returns raw ``CaseEntity`` rows rather than aggregated counts so the
agent can reason about provenance (which evidence, error context, when
first seen) rather than just "the value was mentioned N times".

Design Reference:
    docs/working/WIP-data-processing-improvement-plan.md §Phase 4.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext
from faultmaven.modules.case.domain.models import EntityType

logger = logging.getLogger(__name__)

_VALID_TYPES = {t.value for t in EntityType}


class FindEntityTool(AgentTool):
    """Look up a specific entity value across a case's evidence."""

    def __init__(self, case_repository: Any = None):
        self.case_repository = case_repository

    @property
    def name(self) -> str:
        return "find_entity"

    @property
    def description(self) -> str:
        return (
            "Look up a specific entity value (IP, hostname, user, PID, "
            "port, service, path, device, metric_name) across the case's "
            "evidence. Returns one row per (evidence, type) the entity "
            "appears in, with mention count and whether it appeared in "
            "error context. Use when the user or a hypothesis names a "
            "specific value and you need to know which evidence mentions "
            "it. For aggregated 'top N' queries, use list_top_entities "
            "instead."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_value": {
                    "type": "string",
                    "description": (
                        "The exact value to look up "
                        "(case-sensitive). Example: '10.0.0.5'."
                    ),
                },
                "entity_type": {
                    "type": "string",
                    "description": (
                        "Optional entity type filter. One of: "
                        + ", ".join(sorted(_VALID_TYPES))
                        + ". Omit to match the value across all types."
                    ),
                },
            },
            "required": ["entity_value"],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if self.case_repository is None:
            return ToolResult(
                success=False,
                data=None,
                error=(
                    "Case repository not wired; entity lookups are "
                    "unavailable on this deployment."
                ),
            )

        raw_value = params.get("entity_value")
        if not isinstance(raw_value, str) or not raw_value.strip():
            return ToolResult(
                success=False,
                data=None,
                error="entity_value is required and must be a non-empty string.",
            )
        entity_value = raw_value.strip()

        type_arg: Optional[EntityType] = None
        raw_type = params.get("entity_type")
        if raw_type:
            if raw_type not in _VALID_TYPES:
                return ToolResult(
                    success=False,
                    data=None,
                    error=(
                        f"Unknown entity_type {raw_type!r}. "
                        f"Valid types: {sorted(_VALID_TYPES)}."
                    ),
                )
            type_arg = EntityType(raw_type)

        try:
            rows = await self.case_repository.find_entity(
                case_id=context.case_id,
                entity_value=entity_value,
                entity_type=type_arg,
            )
        except Exception as e:
            logger.exception(
                "find_entity failed for case %s value %r: %s",
                context.case_id,
                entity_value,
                e,
            )
            return ToolResult(
                success=False,
                data=None,
                error=f"Entity lookup failed: {e}",
            )

        return ToolResult(
            success=True,
            data={
                "case_id": context.case_id,
                "entity_value": entity_value,
                "entity_type": raw_type,
                "count": len(rows),
                "matches": [
                    {
                        "entity_type": r.entity_type.value,
                        "entity_value": r.entity_value,
                        "evidence_id": r.evidence_id,
                        "mention_count": r.mention_count,
                        "in_error_context": r.in_error_context,
                        "first_seen_ts": (
                            r.first_seen_ts.isoformat() if r.first_seen_ts else None
                        ),
                    }
                    for r in rows
                ],
                "note": (
                    "Rows ordered by mention_count desc. Empty list "
                    "means this value was never extracted for the case "
                    "— either it isn't present or its data type isn't "
                    "covered by the extractor set."
                ),
            },
        )
