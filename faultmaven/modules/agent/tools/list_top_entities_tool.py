"""List Top Entities Tool — Phase 4c.

Exposes the aggregate ``case_repository.list_top_entities`` query:
"which IPs (or users / hostnames / services / …) appear most
frequently in this case, summed across all evidence?"

The LLM uses this when the user asks an open question about the shape
of the data — "which hosts are involved?", "which users are failing
auth?" — rather than chasing a known value (use ``find_entity`` for
that). The aggregation sums ``mention_count`` across evidence, so the
returned count is a case-wide total.

Design Reference:
    docs/working/WIP-data-processing-improvement-plan.md §Phase 4.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext
from faultmaven.modules.case.domain.models import EntityType

logger = logging.getLogger(__name__)

_VALID_TYPES = {t.value for t in EntityType}
# Upper bound on `limit`. The agent asks for "top N" queries; large N
# just wastes context window without adding signal. Cap at 50 — well
# above any reasonable "show me the top entities" ask.
_MAX_LIMIT = 50
_DEFAULT_LIMIT = 10


class ListTopEntitiesTool(AgentTool):
    """Return the top-N entities of a given type for a case."""

    def __init__(self, case_repository: Any = None):
        self.case_repository = case_repository

    @property
    def name(self) -> str:
        return "list_top_entities"

    @property
    def description(self) -> str:
        return (
            "List the most-mentioned entities of a given type for the "
            "current case. Aggregates mention counts across evidence so "
            "the result is a case-wide tally. Use when the user asks "
            "'which IPs…', 'which users…', etc., rather than chasing a "
            "specific value (use find_entity for that). Valid types: "
            + ", ".join(sorted(_VALID_TYPES))
            + ". Empty result means no entities of that type were "
            "extracted — the type may not be covered by the data types "
            "in this case's evidence."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "description": (
                        "Entity type to rank. One of: "
                        + ", ".join(sorted(_VALID_TYPES))
                        + "."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Max distinct values to return. Default "
                        f"{_DEFAULT_LIMIT}, capped at {_MAX_LIMIT}."
                    ),
                },
            },
            "required": ["entity_type"],
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
                    "Case repository not wired; top-entity queries are "
                    "unavailable on this deployment."
                ),
            )

        raw_type = params.get("entity_type")
        if raw_type not in _VALID_TYPES:
            return ToolResult(
                success=False,
                data=None,
                error=(
                    f"Unknown or missing entity_type {raw_type!r}. "
                    f"Valid types: {sorted(_VALID_TYPES)}."
                ),
            )
        entity_type = EntityType(raw_type)

        raw_limit = params.get("limit", _DEFAULT_LIMIT)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                data=None,
                error=f"limit must be an integer, got {raw_limit!r}.",
            )
        if limit < 1:
            limit = 1
        if limit > _MAX_LIMIT:
            limit = _MAX_LIMIT

        try:
            rows = await self.case_repository.list_top_entities(
                case_id=context.case_id,
                entity_type=entity_type,
                limit=limit,
            )
        except Exception as e:
            logger.exception(
                "list_top_entities failed for case %s type %s: %s",
                context.case_id,
                raw_type,
                e,
            )
            return ToolResult(
                success=False,
                data=None,
                error=f"Top-entity query failed: {e}",
            )

        return ToolResult(
            success=True,
            data={
                "case_id": context.case_id,
                "entity_type": raw_type,
                "limit": limit,
                "count": len(rows),
                "entities": [
                    {
                        "entity_value": r.entity_value,
                        "mention_count": r.mention_count,
                        "in_error_context": r.in_error_context,
                        "representative_evidence_id": r.evidence_id,
                        "first_seen_ts": (
                            r.first_seen_ts.isoformat() if r.first_seen_ts else None
                        ),
                    }
                    for r in rows
                ],
                "note": (
                    "mention_count is summed across evidence. "
                    "representative_evidence_id is one evidence where "
                    "the value appears (the one with the highest "
                    "per-evidence mention count). Use find_entity with "
                    "the value to list all evidence mentioning it."
                ),
            },
        )
