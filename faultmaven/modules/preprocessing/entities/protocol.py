"""EntityExtractor protocol and shared types.

The protocol decouples the per-data-type extraction logic from the
preprocessing orchestrator: each data type that carries entities worth
indexing registers an implementation; everything else returns an empty
list. See ``registry.extract_entities_for_data_type`` for the dispatch
table.

Why a dataclass rather than returning ``CaseEntity`` directly:
extractors don't know the ``case_id`` or ``evidence_id`` yet — those
are only available after the preprocessor has built the Evidence row.
The orchestrator stamps them in when it materialises the final
``CaseEntity`` list to upsert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from faultmaven.modules.case.domain.models import EntityType


@dataclass(frozen=True)
class EntityObservation:
    """A single entity found during extraction.

    Carries everything the registry needs except identifiers — the
    preprocessing orchestrator fills in ``case_id`` and ``evidence_id``
    when converting to ``CaseEntity``.
    """

    entity_type: EntityType
    entity_value: str
    mention_count: int = 1
    in_error_context: bool = False


@runtime_checkable
class EntityExtractor(Protocol):
    """Produces entity observations for a single evidence's content.

    Implementations are data-type specific: the logs extractor looks
    for syslog-shaped patterns, the config extractor looks for
    structured key/value pairs, etc.

    The ``error_line_indices`` argument is optional context — the logs
    extractor populates it based on its severity scan. Extractors
    without a meaningful notion of "error context" ignore it and emit
    ``in_error_context=False`` uniformly.
    """

    @property
    def data_type_name(self) -> str:
        """Debug-only label, used in logs and metrics."""
        ...

    def extract(
        self,
        content: str,
        error_line_indices: set[int] | None = None,
    ) -> list[EntityObservation]: ...
