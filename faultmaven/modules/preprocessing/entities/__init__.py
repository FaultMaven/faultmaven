"""Phase 4 — case-level entity extraction.

Emits ``CaseEntity`` rows from extracted evidence content. See
``docs/working/WIP-data-processing-improvement-plan.md`` §Phase 4 and
the ``case_entities`` table (alembic migration ``d4e5f6a70819``).
"""

from faultmaven.modules.preprocessing.entities.protocol import (
    EntityExtractor,
    EntityObservation,
)
from faultmaven.modules.preprocessing.entities.registry import (
    extract_entities_for_data_type,
)

__all__ = [
    "EntityExtractor",
    "EntityObservation",
    "extract_entities_for_data_type",
]
