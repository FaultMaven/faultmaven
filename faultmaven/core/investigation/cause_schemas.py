"""v3 KB-resolution schemas — CauseChunk + Indicator evaluation results.

Extracted to a leaf module so `agent.tools.kb_qa` can import these types
without transiting through `core.investigation.schemas`, which re-exports
`QueryIntent` from `agent.domain.models.agentic` for runtime resolution of
the `TurnPayload.intent` annotation. That re-export creates a chain
`agent.tools → core.schemas → agent.domain` that the Agent module layer
contract (contract 7 in .importlinter) disallows.

This module imports nothing from `agent.*`, so it stays clean.

See docs/architecture/investigation-engine/indicator-resolution.md §5–§6 for
the design rationale and the engine handoff points where these types are read.
"""

from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field


class CauseChunk(BaseModel):
    """A parsed `### Cause N` subsection retrieved from a v3 runbook.

    Built from a ChromaDB chunk's metadata. Fields map directly to the
    runbook subsection's labelled sub-fields. The engine reads
    ``statement`` / ``mechanism`` into ``RootCauseConclusion`` and
    ``mitigation`` / ``resolution`` into the ``Solution`` record on
    KB-resolution same-turn collapse.
    """

    runbook_id: str
    cause_letter: str = Field(description="Single uppercase letter A-Z")
    cause_name: str
    statement: str = Field(description="Direct copy → RootCauseConclusion.root_cause")
    mechanism: str = Field(description="Direct copy → RootCauseConclusion.mechanism")
    indicators: List[str] = Field(
        default_factory=list,
        description="Indicator prose entries (one per line) for evaluator fallback",
    )
    match_predicates: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Deterministic match predicates parsed from <!-- match: --> hints",
    )
    mitigation: str
    resolution: str
    verification: str
    is_fallback: bool = Field(
        default=False,
        description="True iff Indicator includes [Default] (fallback Cause Z)",
    )


class IndicatorResult(BaseModel):
    """Outcome of evaluating a single Indicator entry against case state."""

    indicator_text: str
    matched: bool
    method: Literal["deterministic", "case_evidence_qa"]


class CauseMatch(BaseModel):
    """Outcome of evaluating all Indicators of a Cause."""

    cause_name: str
    indicator_results: List[IndicatorResult] = Field(default_factory=list)
    matched: bool = Field(description="All indicators evaluated true")
    is_fallback: bool = Field(default=False)


class CauseMatchResult(BaseModel):
    """Per-runbook evaluation summary."""

    runbook_id: str
    causes: List[CauseMatch] = Field(default_factory=list)
    matched_causes: List[CauseMatch] = Field(default_factory=list)
    verdict: Literal["none", "single", "multiple"]
    selected_cause: CauseMatch | None = Field(
        default=None,
        description=(
            "Engine's resolved choice. verdict='single' → the matched Cause; "
            "verdict='none' → the fallback Cause; "
            "verdict='multiple' → None (LLM disambiguates)."
        ),
    )
