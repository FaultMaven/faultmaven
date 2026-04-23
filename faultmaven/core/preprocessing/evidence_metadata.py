"""Structured contract for the ``evidence.metadata`` JSON column.

The column is shared by several preprocessing-pipeline phases (classifier
confidence, extractor attempts, entity-cap overflow markers, coverage
details). This module pins the shape so writers don't collide and readers
can tolerate absence.

Ownership (see docs/architecture/data-and-storage/schemas/case-schema.md §4.3
"evidence.metadata JSON contract"):

    classification  — PreprocessingService writes; context_builder reads.
    extractor       — PreprocessingService writes; observability only.
    entities        — PreprocessingService writes on cap overflow.
    coverage        — PreprocessingService writes; context_builder + tool reads.

All top-level keys are Optional; existing rows predate each key. Readers
must treat missing keys and missing fields as "not populated", not as a
degraded state.
"""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ClassificationMetadata(BaseModel):
    """Tier 0 classifier signals for this evidence row.

    Populated at persistence time from ``ClassificationResult``. The
    ``context_builder`` reads ``confidence`` to decide whether to attach
    a ``confidence="low"`` marker to the ``<evidence>`` tag in the agent
    prompt.
    """

    model_config = ConfigDict(extra="ignore")

    confidence: float = Field(ge=0.0, le=1.0)
    source: str
    """Provenance of the classification decision. One of:
    ``user_override`` | ``agent_hint`` | ``source_url`` |
    ``browser_context`` | ``rule_based`` | ``rule_based_best_effort``.
    """

    failed: bool = False
    """True when the classifier hit its confidence floor and short-
    circuited to the user-clarification path. The suggested_types list
    is non-empty in that case."""

    suggested_types: List[str] = Field(default_factory=list)


class ExtractorAttempt(BaseModel):
    """One pass through the extractor for this evidence.

    A single attempt is the norm. Multiple attempts appear when:

    - The Phase 2 sanity-check-and-retry path runs (``triggered_by =
      "sanity_retry"``).
    - Phase 1.5 reclassification runs — the user corrected the
      classifier (``triggered_by = "user_override"``).

    The list is chronological: the most recent attempt is the one whose
    output is currently persisted as ``evidence.preprocessed_content``.
    Hard cap: 5 entries; oldest rotate off the head when exceeded.
    """

    model_config = ConfigDict(extra="ignore")

    data_type: str
    sanity_passed: bool = True
    duration_ms: int = 0
    triggered_by: Optional[str] = None
    """What initiated this attempt. Known values: ``initial``,
    ``sanity_retry`` (Phase 2), ``user_override`` (Phase 1.5). ``None``
    is tolerated for backward-compatibility with attempts recorded
    before the field existed."""


class ExtractorMetadata(BaseModel):
    """Which extractor produced preprocessed_content, and its retry
    history. Pure observability in Phase 1; ``attempts`` gets populated
    in Phase 2 when the retry path lands."""

    model_config = ConfigDict(extra="ignore")

    chosen_type: str
    attempts: List[ExtractorAttempt] = Field(default_factory=list)


class EntitiesMetadata(BaseModel):
    """Overflow markers for the Phase 4 ``case_entities`` writes.

    Entity rows themselves live in the separate table; this structure
    only records when an entity type's per-evidence cap (500) was hit,
    so the agent knows "not all entities are indexed — use raw-file
    drill-down for enumeration."
    """

    model_config = ConfigDict(extra="ignore")

    overflow_types: List[str] = Field(default_factory=list)


class CoverageMetadata(BaseModel):
    """Extractor time-range details beyond the promoted columns.

    The ``evidence.coverage_start_ts`` / ``coverage_end_ts`` columns are
    the queryable projection; this object carries the auxiliary signal
    ("which pattern matched the timestamp format") that the agent tool
    surfaces but the DB doesn't index.
    """

    model_config = ConfigDict(extra="ignore")

    source: Optional[str] = None
    """Which timestamp-format pattern matched. One of ``iso8601_t``,
    ``iso8601``, ``syslog_bsd``, ``epoch_s``, ``epoch_ms``, or None when
    no pattern matched."""


class EvidenceMetadata(BaseModel):
    """Top-level structured view of ``evidence.metadata``.

    Every field is Optional — existing rows in production predate each
    field's introduction and must continue to work. Writers populate
    only the key they own; readers tolerate absence.
    """

    model_config = ConfigDict(extra="ignore")

    classification: Optional[ClassificationMetadata] = None
    extractor: Optional[ExtractorMetadata] = None
    entities: Optional[EntitiesMetadata] = None
    coverage: Optional[CoverageMetadata] = None

    def to_storage_dict(self) -> dict:
        """Serialize for DB storage, stripping None top-level keys so
        the stored JSON stays minimal and the ownership contract stays
        visible (a key is present iff the owning phase wrote it)."""
        return self.model_dump(exclude_none=True, mode="json")

    @classmethod
    def from_storage_dict(cls, raw: Optional[dict]) -> "EvidenceMetadata":
        """Parse from DB. ``None`` / ``{}`` / partially-populated shapes
        are all valid — they map to an instance with absent fields."""
        if not raw:
            return cls()
        return cls.model_validate(raw)


# Exported threshold. Evidence whose classification.confidence is below
# this threshold gets a ``confidence="low"`` marker in the agent's
# context. 0.65 sits below the rule-based classifier's auto-accept bar
# (0.85) but above the classification_failed short-circuit (0.50), so
# it captures the "classified but shaky" band without duplicating the
# user-clarification path. Tunable via feature flag / settings later.
LOW_CONFIDENCE_THRESHOLD: float = 0.65
