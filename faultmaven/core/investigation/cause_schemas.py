"""v4 KB-resolution schemas — the input Cause record + match results.

``CauseRecord`` (below) is the **canonical v4 cause shape (SSOT)** — see its
docstring. A retrieved runbook Cause is a CAUSAL CHAIN: one ROOT and a
``root → … → D`` ladder. Its **symptom-level ``cause_statement`` is the
load-bearing match surface**: the matcher (``indicator_evaluator``) judges
holistically *per cause* whether the case is explained by it (#545), then seeds
the chain topology into the case graph as a capped CANDIDATE prior. Per-rung
indicators / ``match_predicates`` are optional annotations, inert for matching in
evidence-only FaultMaven. Authoring contract:
docs/architecture/knowledge-and-ai/runbook-content-architecture.md (§ "Match
surface"); matching mechanism: runbook-cause-matching.md.

Leaf module (imports nothing from ``agent.*``) so ``agent.tools.kb_qa`` can
import these types without violating the Agent module layer contract
(.importlinter contract 7).
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


def is_problem_node(node: Dict[str, Any]) -> bool:
    """True if a chain-node dict is the engine-seeded PROBLEM (D) node.

    A node is the problem either by ``node_type`` (case-insensitively
    ``"problem"``) or by the literal ``ref == "D"`` — packs may identify it
    either way. Shared by the matcher's chain instantiation
    (``chain_to_specs``) and the evaluator's holistic-condition builder
    (``_build_cause_condition``) so the two can't drift on which node is the
    shared problem — a drift would leak the symptom-under-investigation into the
    cause condition and make every cause match.
    """
    ntype = str(node.get("node_type", "")).strip().lower()
    ref = str(node.get("ref", "")).strip()
    return ntype == "problem" or ref == "D"


class CauseRecord(BaseModel):
    """The **canonical v4 cause shape (SSOT)** — the matcher's INPUT.

    Mirrors the KB pack's per-Cause record (``knowledge_items.metadata['causes']``
    entry, i.e. ``pack.json`` ``runbooks[].causes[]``). Tolerant: optional fields
    default empty so a degenerate (no-``Chain``) Cause still loads.

    Field roles under the ratified match-surface decision (runbook-content-
    architecture.md § "Match surface"):
      - ``cause_statement`` — the **load-bearing match surface** (symptom-level;
        the matcher judges the case holistically against it). ``cause_name`` is
        its subject. Authored to be symptom-level and discriminative from sibling
        Causes (MECE).
      - ``chain_nodes`` / ``chain_edges`` — chain **topology**, instantiated into
        the case graph as a capped CANDIDATE prior (never VALIDATED w/o evidence).
      - ``rung_indicators`` / ``match_predicates`` — **optional annotations,
        INERT for matching** in evidence-only FM (operator/tool-output level).
        Human diagnostic notes / opportunistic fast-path only.

    SSOT note (for the #2 cause-matcher campaign, Phase 1): this is the canonical
    cause shape; the following parallel definitions are FORKS that should be
    consolidated onto it (mirror, don't redefine):
      1. ``kb_toolkit/config/config.py`` — v4 grammar / required-subfield consts
      2. ``kb_toolkit/core/validator.py`` — validation constants
      3. backend ``modules/knowledge/.../runbook_validator.py`` — v2/v4 consts
      4. the document→runbook conversion prompt
    Until consolidated, a change here must be mirrored in all four or they drift.
    """

    cause_letter: str
    cause_name: str = Field(default="", description="Cause subject (match-surface)")
    cause_statement: str = Field(
        default="",
        description="Symptom-level root-cause sentence — the LOAD-BEARING match surface",
    )
    chain_nodes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Topology: [{ref, node_type, statement}] in root→D order (instantiation)",
    )
    chain_edges: List[Dict[str, Any]] = Field(default_factory=list)
    rung_indicators: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Optional, inert for matching: rung ref → [indicator prose]",
    )
    match_predicates: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Optional, inert for matching: predicates from <!-- match --> hints",
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
    selected_record: Optional[CauseRecord] = Field(
        default=None,
        description=(
            "The full input ``CauseRecord`` for ``selected_cause`` — its causal "
            "chain, carried so the engine can instantiate it without re-resolving. "
            "Populated only when verdict='single' (None for 'none'/'multiple')."
        ),
    )
