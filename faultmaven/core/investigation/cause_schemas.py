"""v4 KB-resolution schemas — the input Cause record + match results.

``CauseRecord`` (below) is the **reference v4 cause shape** — see its
docstring. A retrieved runbook Cause is a CAUSAL CHAIN: one ROOT and a
``root → … → D`` ladder. Its **symptom-level ``cause_statement`` is the
load-bearing match surface**: the matcher (``indicator_evaluator``) judges
holistically *per cause* whether the case is explained by it, then seeds the
chain topology into the case graph as a capped CANDIDATE prior.
``match_predicates`` carry **no matching weight** but are the **load-bearing
validation surface** — the content-addressed differential-intake loop evaluates
submitted telemetry against them (``rung_indicators`` are inert in the live path,
read only by the step-addressed T1 tier, and FM executes no runbook steps).
Authoring contract:
docs/architecture/knowledge-and-ai/runbook-content-architecture.md (§ "Match
surface"); matching mechanism: runbook-cause-matching.md.

Leaf module (imports nothing from ``agent.*``) so ``agent.tools.kb_qa`` can
import these types without violating the Agent module layer contract
(.importlinter contract 7).
"""

import logging
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


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
    """The canonical v4 cause shape — the matcher's INPUT.

    Mirrors the KB pack's per-Cause record (``knowledge_items.metadata['causes']``
    entry, i.e. ``pack.json`` ``runbooks[].causes[]``). Tolerant: optional fields
    default empty so a degenerate (no-``Chain``) Cause still loads.

    Field roles under the match-surface contract (runbook-content-architecture.md
    § "Match surface"):
      - ``cause_statement`` — the **load-bearing match surface** (symptom-level;
        the matcher judges the case holistically against it). ``cause_name`` is
        its subject. Authored to be symptom-level and discriminative from sibling
        Causes (MECE).
      - ``chain_nodes`` / ``chain_edges`` — chain **topology**, instantiated into
        the case graph as a capped CANDIDATE prior (never VALIDATED w/o evidence).
      - ``match_predicates`` — **no matching weight** (matching is holistic over
        the symptom-level ``cause_statement``), but the **load-bearing validation
        surface**: the content-addressed differential-intake loop deterministically
        evaluates submitted telemetry against them → SUPPORTS/REFUTES with
        ``provenance="runbook"``. Each predicate may carry an optional
        ``stance: "supports" | "refutes"`` (default ``"supports"``; a firing
        ``refutes`` eliminates the cause — sibling-elimination only, not
        proof-by-exclusion). ``rung_indicators`` are inert in the live path (only
        the step-addressed T1 tier reads them, and FM runs no runbook steps).

    This is the *reference shape*; the definitions below describe the same cause
    record independently and must stay aligned with it (they live in a separate
    repo / the prompt layer, so this is a manual mirror, not an imported SSOT —
    a change here must be reflected in each, or they drift). Consolidating them
    onto one shared contract is tracked separately.
      1. ``kb_toolkit/core/runbook_grammar.py`` — the toolkit's v4 cause grammar
         (parse + regexes); ``config.py`` / ``validator.py`` / ``chunker.py``
         consume it. The grammar source, not its consumers.
      2. ``kb_toolkit/core/pack_builder.py`` (``_extract_causes``) — **produces**
         the ``causes`` record this class loads via ``CauseRecord(**entry)``;
         its emitted field set must match these fields exactly.
      3. backend ``modules/knowledge/domain/services/runbook_validator.py`` —
         backend v4 validation constants.
      4. backend ``modules/knowledge/domain/services/conversion_service.py`` —
         the v4 grammar embedded in ``CONVERSION_SYSTEM_PROMPT`` **and** in the
         manual ``create_runbook_from_template`` path (two copies).
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
        description="Optional; inert in the live path (step-addressed T1 only). rung ref → [indicator prose]",
    )
    match_predicates: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="No matching weight; the validation surface (differential-intake). From <!-- match --> hints; each may carry optional stance: supports|refutes",
    )
    interventions: List[Dict[str, Any]] = Field(default_factory=list)
    is_fallback_cause: bool = False

    @property
    def path(self) -> List[str]:
        """Rung refs in chain order root→D (degenerate → ``['root', 'D']``)."""
        refs = [str(n.get("ref")) for n in self.chain_nodes if n.get("ref")]
        return refs or ["root", "D"]


def build_cause_records(
    item_id: str, causes_raw: List[Dict[str, Any]]
) -> List[CauseRecord]:
    """Parse a runbook's stored causes list into ``CauseRecord``s.

    Pure ``(item_id, raw) -> List[CauseRecord]`` — the single home for the
    dict→record parse, in ``core`` next to ``CauseRecord`` so both the retrieval
    tool (``agent.tools.kb_qa``) and the engine's intake bind to it without a
    backwards ``core → agent`` dependency. Tolerant per entry: a malformed Cause
    is logged and skipped rather than failing the whole runbook (re-establishes
    the robustness the v3 chunk-parser had against bad stored metadata).
    """
    records: List[CauseRecord] = []
    for entry in causes_raw:
        if not isinstance(entry, dict):
            logger.warning(
                "Skipping non-dict cause entry in runbook %s: %r", item_id, entry
            )
            continue
        try:
            records.append(CauseRecord(**entry))
        except Exception as exc:  # noqa: BLE001 — bad stored metadata, not fatal
            logger.warning(
                "Skipping malformed cause %s in runbook %s: %s",
                entry.get("cause_letter", "?"),
                item_id,
                exc,
            )
    return records


RungMethod = Literal["deterministic", "case_evidence_qa", "untested"]


class RungResult(BaseModel):
    """Outcome of the deterministic (T1) evaluation of one rung indicator.

    Post-#545 the semantic tier is one holistic judgment per CAUSE (not per rung),
    so a rung's ``method`` is only ever ``"deterministic"`` or ``"untested"``;
    ``"case_evidence_qa"`` is retained in ``RungMethod`` for back-compat but no
    longer emitted. See ``indicator_evaluator`` and runbook-cause-matching.md §2.1.
    """

    rung_ref: str
    indicator_text: str
    matched: bool
    refuted: bool = Field(
        default=False,
        description="Deterministically contradicted by evidence — prunes the chain",
    )
    method: RungMethod


class CauseMatch(BaseModel):
    """Per-Cause evaluation: deterministic T1 rung results + a holistic belief."""

    cause_letter: str
    cause_name: str = ""
    path: List[str] = Field(default_factory=list, description="rung refs root→D")
    rung_results: List[RungResult] = Field(default_factory=list)
    belief: float = Field(
        default=0.0,
        description=(
            "0 if any rung is deterministically refuted; else 1.0 when the "
            "per-cause holistic T2 judgment over the symptom-level Statement "
            "supports the Cause, otherwise the deterministic T1 matched-rung "
            "fraction (0 when the Cause carries no indicator rungs) (#545)"
        ),
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
