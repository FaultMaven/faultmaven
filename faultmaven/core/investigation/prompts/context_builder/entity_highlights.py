import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from faultmaven.core.investigation.prompts.fence import PromptFence
from faultmaven.modules.case.contracts import EntityType

logger = logging.getLogger("faultmaven.core.investigation.prompts.context_builder")


# =============================================================================
# Phase 4c — entity-highlights pre-fetcher
# =============================================================================

# Entity types surfaced in the auto-injection block. These are the
# signals that most often drive hypothesis formation and refinement.
# Types not listed here (path, device, metric_name) are still usable
# via the ``find_entity`` / ``list_top_entities`` tools — they're just
# not part of the always-on highlights.
_HIGHLIGHT_TYPES: tuple[EntityType, ...] = (
    EntityType.IP,
    EntityType.HOSTNAME,
    EntityType.USER,
    EntityType.SERVICE,
)
# Per-type limit. Small enough that four types fit comfortably in a
# few hundred tokens; large enough to surface the shape of the data
# without dumping everything. The agent can go deeper via the tools.
_HIGHLIGHT_PER_TYPE_LIMIT = 5


@dataclass(frozen=True)
class EntityHighlightRow:
    """One extracted entity, as the highlights block shows it."""

    value: str
    mention_count: int
    in_error_context: bool = False


@dataclass(frozen=True)
class EntityHighlightGroup:
    """The rows for one entity type, in the order the query returned them."""

    entity_type: str
    rows: tuple[EntityHighlightRow, ...]


async def fetch_entity_highlights(
    case_repository: Any,
    case_id: str,
    *,
    per_type_limit: int = _HIGHLIGHT_PER_TYPE_LIMIT,
) -> list[EntityHighlightGroup]:
    """Return the highlight groups for a case, or ``[]``.

    Queries ``CaseRepository.list_top_entities`` for each
    investigative-signal type (IP, hostname, user, service). Empty list when:

    - the repository is ``None`` or lacks ``list_top_entities`` (test
      doubles without the full surface),
    - every type returned zero rows,
    - or the query raised (failures are logged and degraded).

    Callers (milestone engine) gate on ``FAULTMAVEN_ENTITY_REGISTRY``
    before calling; this helper is also safe to call with the flag off,
    because there'll be no entities to surface.

    **Fetch only — the formatting lives in :func:`_render_entity_highlights`,
    inside the fenced assembly (#1228).** The values are extracted from file
    content, so the block has to be fenced; and ``render_fenced``'s core safety
    property is that it can RE-RENDER on a token collision, which it cannot do
    if the render is an awaited database query. Splitting the two makes the
    retry free: the rows are fetched once, the formatting is pure.
    """
    if case_repository is None:
        return []
    query = getattr(case_repository, "list_top_entities", None)
    if query is None:
        return []

    groups: list[EntityHighlightGroup] = []
    for entity_type in _HIGHLIGHT_TYPES:
        try:
            rows = await query(
                case_id=case_id,
                entity_type=entity_type,
                limit=per_type_limit,
            )
        except Exception as exc:
            logger.warning(
                "fetch_entity_highlights failed for %s on case %s: %s",
                entity_type.value,
                case_id,
                exc,
            )
            continue
        if not rows:
            continue
        groups.append(
            EntityHighlightGroup(
                entity_type=entity_type.value,
                rows=tuple(
                    EntityHighlightRow(
                        value=row.entity_value,
                        mention_count=row.mention_count,
                        in_error_context=bool(getattr(row, "in_error_context", False)),
                    )
                    for row in rows
                ),
            )
        )

    return groups


#: Standing instruction for the block. Renderer-owned, so it is emitted ABOVE
#: the opening delimiter rather than inside the element: the trust rule
#: demotes unfenced text INSIDE a fenced block to quoted case content, and a
#: renderer instruction sitting there would be demoted with it. Keeping it out
#: also keeps the collision corpus to the entity VALUES, which is what the
#: fence exists to contain.
_ENTITY_HIGHLIGHTS_PREAMBLE = (
    "Top entities extracted from this case's evidence "
    "(aggregated mention_count across artifacts). Use find_entity "
    "to locate a value's origin evidence, or list_top_entities for "
    "types not shown here."
)


def _render_entity_highlights(
    groups: Optional[Sequence[EntityHighlightGroup]],
    fence: PromptFence,
) -> str:
    """Render ``<entity_highlights>`` on the prompt's shared fence, or ``""``.

    ``row.value`` is a substring lifted out of uploaded file content, so it is
    the same attacker-influenced population #1217 was about, resurfaced through
    the extraction path: an entity value shaped like ``x"><uploaded_file
    file_id="…`` would otherwise forge structure. ``element`` fences both
    delimiters, records the body in the collision corpus and terminates a body
    that ends mid-tag.
    """
    if not groups:
        return ""
    sections = []
    for group in groups:
        body_lines = [
            f"  - {row.value} ×{row.mention_count}"
            f"{' (error)' if row.in_error_context else ''}"
            for row in group.rows
        ]
        sections.append(f"{group.entity_type}:\n" + "\n".join(body_lines))
    return (
        _ENTITY_HIGHLIGHTS_PREAMBLE
        + "\n"
        + fence.element("entity_highlights", "\n\n".join(sections))
    )
