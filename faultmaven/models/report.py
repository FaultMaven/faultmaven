"""Report models — compatibility surface over the Case-owned canon.

The Case module owns the ``reports`` table, so the report models live in
``faultmaven.modules.case.domain.owned_models.report`` and are published
through ``faultmaven.modules.case.contracts`` — which is what this module
re-exports from, per the cross-module rule (importing the owned_models
directly would put ``faultmaven.models`` on a path from Case infrastructure
into Case domain and break import-linter contract 5). This module used to carry a
second, hand-maintained copy of ``ReportType`` / ``ReportStatus`` /
``CaseReport`` / ``RunbookSource`` / ``RunbookMetadata`` / the request-response
DTOs — a fork that had already drifted (it lacked ``auto_generated`` and
``updated_at``) and that carried both halves of #520's report arm a second
time, where a fix to the canonical models would not have reached it.

Nothing imported those duplicates: ``faultmaven.models.__init__`` re-exports the
canonical ones from ``case.contracts``, and every live importer of this module
wanted ``RunbookMatch``. So the duplicates are gone and the names are re-exported
instead — one definition per concept, which is what stops the drift class from
regrowing here.

``RunbookMatch`` is genuinely owned here: it is the dedup search-result type,
defined at the models layer so infrastructure (``RunbookKnowledgeBase``) can
return it without importing module internals.
"""

from pydantic import BaseModel, Field

from faultmaven.modules.case.contracts import (
    PERSISTED_REPORT_TYPES,
    CaseClosureRequest,
    CaseClosureResponse,
    CaseReport,
    ReportGenerationRequest,
    ReportGenerationResponse,
    ReportRecommendation,
    ReportStatus,
    ReportType,
    RunbookMetadata,
    RunbookRecommendation,
    RunbookRef,
    RunbookSource,
)


class RunbookMatch(BaseModel):
    """A published runbook surfaced by dedup similarity search.

    An honest reference to a live KB item (``knowledge_items`` row + its
    ChromaDB chunks) — not a reconstructed report. The previous shape minted a
    ``CaseReport`` with an invented ``generation_status``/``version`` and a
    defaulted ``generated_at``: a report row that never existed. This carries
    only what the search established.
    """

    item_id: str = Field(
        ..., description="KB item id (knowledge_items.item_id / parent_document_id)"
    )
    title: str = Field(..., description="Runbook title as stored in the KB")
    scope: str = Field(
        ..., description="Visibility floor of the KB item ('personal' or 'global')"
    )
    similarity_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Best-chunk cosine similarity from vector search",
    )


__all__ = [
    "RunbookMatch",
    # Re-exported from the Case-owned canon — see the module docstring.
    "PERSISTED_REPORT_TYPES",
    "CaseClosureRequest",
    "CaseClosureResponse",
    "CaseReport",
    "ReportGenerationRequest",
    "ReportGenerationResponse",
    "ReportRecommendation",
    "ReportStatus",
    "ReportType",
    "RunbookMetadata",
    "RunbookRecommendation",
    "RunbookRef",
    "RunbookSource",
]
