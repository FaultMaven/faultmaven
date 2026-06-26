"""v4 KB-resolution schemas — the input Cause record + rung-level match results.

A retrieved runbook Cause is a CAUSAL CHAIN: one ROOT and a ``root → … → D``
ladder with per-rung indicators and quadrant-tagged interventions. The matcher
(``indicator_evaluator.IndicatorEvaluator``) evaluates each rung's indicators
against current case state and returns rung-level results with a k-of-n belief.
See docs/architecture/investigation-engine/runbook-cause-matching.md §3–§5 and
the implementation plan in runbook-cause-matcher-implementation.md.

Leaf module (imports nothing from ``agent.*``) so ``agent.tools.kb_qa`` can
import these types without violating the Agent module layer contract
(.importlinter contract 7).
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class CauseRecord(BaseModel):
    """A v4 per-Cause graph record — the matcher's INPUT.

    Mirrors the KB pack's per-Cause record (``knowledge_items.metadata['causes']``
    entry, i.e. ``pack.json`` ``runbooks[].causes[]``). Tolerant: optional fields
    default empty so a degenerate (no-``Chain``) Cause still loads. The graph
    fields (``chain_nodes``/``chain_edges``/``interventions``) are carried for the
    lazy instantiation step (increment 4); the matcher itself reads only
    ``rung_indicators`` + ``match_predicates``.
    """

    cause_letter: str
    cause_name: str = ""
    cause_statement: str = ""
    chain_nodes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="[{ref, node_type, statement}] in root→D order",
    )
    chain_edges: List[Dict[str, Any]] = Field(default_factory=list)
    rung_indicators: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="rung ref → [indicator prose] (token-anchored)",
    )
    match_predicates: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Deterministic predicates parsed from <!-- match --> hints",
    )
    interventions: List[Dict[str, Any]] = Field(default_factory=list)
    is_fallback_cause: bool = False

    @property
    def path(self) -> List[str]:
        """Rung refs in chain order root→D (degenerate → ``['root', 'D']``)."""
        refs = [str(n.get("ref")) for n in self.chain_nodes if n.get("ref")]
        return refs or ["root", "D"]


RungMethod = Literal["deterministic", "case_evidence_qa", "untested"]


class RungResult(BaseModel):
    """Outcome of evaluating one rung indicator against case state (spec §5)."""

    rung_ref: str
    indicator_text: str
    matched: bool
    refuted: bool = Field(
        default=False,
        description="Deterministically contradicted by evidence — prunes the chain",
    )
    method: RungMethod


class CauseMatch(BaseModel):
    """Rung-level evaluation of a single Cause (spec §5)."""

    cause_letter: str
    cause_name: str = ""
    path: List[str] = Field(default_factory=list, description="rung refs root→D")
    rung_results: List[RungResult] = Field(default_factory=list)
    belief: float = Field(
        default=0.0,
        description="Scaled k-of-n confidence (0..1); 0 if any rung refuted",
    )
    is_fallback: bool = False


class CauseMatchResult(BaseModel):
    """Per-runbook evaluation summary (spec §5). ``verdict`` drives §4."""

    runbook_id: str
    causes: List[CauseMatch] = Field(default_factory=list)
    live_causes: List[CauseMatch] = Field(
        default_factory=list,
        description="belief above the surface threshold, not refuted, not fallback",
    )
    verdict: Literal["none", "single", "multiple"]
    selected_cause: Optional[CauseMatch] = Field(
        default=None,
        description=(
            "Resolved choice so downstream consumers don't re-derive it. "
            "verdict='single' → the live Cause; 'none' → the fallback Cause; "
            "'multiple' → None (LLM disambiguates)."
        ),
    )
