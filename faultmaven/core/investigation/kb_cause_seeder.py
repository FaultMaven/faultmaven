"""KB cause seeder — structural KB → engine cohesion.

When KB retrieval surfaces a runbook whose ``metadata["causes"]`` record aligns
with a fresh case, the engine instantiates that runbook's Cause chains directly
as **CANDIDATE** causal-graph nodes, edges, and hypotheses — instead of the LLM
re-deriving one flat hypothesis from retrieved prose.

A seeded cause is a **prior, not a gate**: it is created candidate-only, its
hypothesis prior is capped like any other (``NEW_HYPOTHESIS_MAX_PRIOR``), it
links no evidence, and it is subject to the same confidence decay, anchoring
detection, and failed-fix demotion as a self-generated hypothesis. It is *not*
the retired runbook-cause matcher (a deterministic grounding arm, NO-GO'd in
#658): seeding grants **zero evidentiary privilege**. VALIDATED is unreachable
here — ``derive_node_states`` / ``project_hypothesis_states_from_roots`` remain
the sole VALIDATED writers.

Provenance markers (``node.metadata["seeded_from_runbook"]`` and the hypothesis
``rationale``) are **read surfaces only** — no safety mechanism branches on them
(enforced by the provenance-blindness invariant test). This is what keeps a
seeded prior mechanically indistinguishable from a self-generated one.

Pure module: no I/O, no LLM. Runbook causes are loaded by the caller and passed
in already ranked; this module only mutates the case graph.

See ``docs/architecture/knowledge-and-ai/kb-cause-seeder.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from faultmaven.core.investigation.causal_graph import (
    ingest_emitted_chain,
    seed_problem_node,
)
from faultmaven.core.investigation.hypothesis_manager import (
    ANCHORING_SAME_CATEGORY_THRESHOLD,
    HypothesisManager,
    create_hypothesis_manager,
)
from faultmaven.modules.case.contracts import (
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    NodeType,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)

# Distinct runbooks seeded per retrieval (top by rerank score). Retrieval has
# already done the semantic case↔runbook alignment; this bounds fan-out.
MAX_SEEDED_RUNBOOKS = 2

# Total causes seeded per turn. Derived from the anchoring condition-1 threshold
# (not a hardcoded copy) so the seeder alone can never manufacture a false
# anchoring flag, and so a future change to the anchoring threshold cannot
# silently let it. Seeded hypotheses default to category OTHER (a Cause record
# carries no category signal), so a cap at threshold-1 keeps them below the
# same-category fixation trigger.
MAX_SEEDED_CAUSES = ANCHORING_SAME_CATEGORY_THRESHOLD - 1
if MAX_SEEDED_CAUSES >= ANCHORING_SAME_CATEGORY_THRESHOLD:  # pragma: no cover
    raise AssertionError(
        f"MAX_SEEDED_CAUSES ({MAX_SEEDED_CAUSES}) must be < "
        f"ANCHORING_SAME_CATEGORY_THRESHOLD ({ANCHORING_SAME_CATEGORY_THRESHOLD})"
    )

# Initial prior for a seeded hypothesis. A plausible-but-unverified prior, well
# below NEW_HYPOTHESIS_MAX_PRIOR (0.5) — a runbook match is a lead to test, not a
# near-conclusion. create_hypothesis caps it regardless.
KB_SEED_PRIOR = 0.3

# Provenance key on a seeded node's metadata (read surface only).
SEEDED_FROM_RUNBOOK_KEY = "seeded_from_runbook"


@dataclass
class SeededRunbook:
    """A retrieved runbook with its loaded causes, ranked by retrieval score."""

    item_id: str
    score: float
    causes: list[dict]


@dataclass
class SeedReport:
    """What a seeding pass produced (for observability + tests)."""

    seeded_hypothesis_ids: list[str] = field(default_factory=list)
    seeded_node_ids: list[str] = field(default_factory=list)
    runbooks_used: list[str] = field(default_factory=list)

    @property
    def seeded_anything(self) -> bool:
        return bool(self.seeded_hypothesis_ids)


@dataclass
class _NodeSpec:
    """Duck-typed spec for ``ingest_emitted_chain`` (statement/node_type/produces)."""

    statement: str
    node_type: NodeType
    produces: Optional[str] = None
    and_group: Optional[str] = None


def seed_candidate_causes(
    case: "Case",
    runbooks: list[SeededRunbook],
    current_turn: int,
    *,
    hypothesis_manager: Optional[HypothesisManager] = None,
    max_runbooks: int = MAX_SEEDED_RUNBOOKS,
    max_causes: int = MAX_SEEDED_CAUSES,
) -> SeedReport:
    """Instantiate ranked runbook Cause chains as candidate graph nodes/hypotheses.

    ``runbooks`` are already ranked (best first) and their ``causes`` are the
    verbatim ``metadata["causes"]`` records. Causes within a runbook are consumed
    in author order (authored most-likely-first) — no bespoke re-scoring. Seeds
    at most ``max_runbooks`` runbooks and ``max_causes`` total causes. Idempotent
    against the existing graph: an identical-statement cause reuses its node
    (``ingest_emitted_chain`` dedup) and never double-seeds a root that already
    heads a hypothesis. Best-effort — a malformed cause is skipped, never raised.
    """
    report = SeedReport()
    problem = seed_problem_node(case)
    if problem is None:
        # No verified symptom → no problem node → nothing to anchor a chain to.
        return report
    d_id = problem.node_id
    hm = hypothesis_manager or create_hypothesis_manager()

    for runbook in runbooks[:max_runbooks]:
        if len(report.seeded_hypothesis_ids) >= max_causes:
            break
        for cause in runbook.causes or []:
            if len(report.seeded_hypothesis_ids) >= max_causes:
                break
            hyp_id, new_node_ids = _seed_one_cause(
                case, runbook.item_id, cause, current_turn, hm, d_id
            )
            if hyp_id is not None:
                report.seeded_hypothesis_ids.append(hyp_id)
                report.seeded_node_ids.extend(new_node_ids)
                if runbook.item_id not in report.runbooks_used:
                    report.runbooks_used.append(runbook.item_id)

    if report.seeded_anything:
        logger.info(
            "KB cause seeder: seeded %d candidate cause(s) from %d runbook(s) "
            "for case %s",
            len(report.seeded_hypothesis_ids),
            len(report.runbooks_used),
            getattr(case, "case_id", "?"),
        )
    return report


def _seed_one_cause(
    case: "Case",
    item_id: str,
    cause: dict[str, Any],
    current_turn: int,
    hm: HypothesisManager,
    d_id: str,
) -> tuple[Optional[str], list[str]]:
    """Seed one Cause's chain. Returns (hypothesis_id | None, new_node_ids)."""
    if cause.get("is_fallback_cause"):
        return None, []  # fallback cause has no chain to instantiate

    chain_nodes = cause.get("chain_nodes") or []
    chain_edges = cause.get("chain_edges") or []

    # Non-problem rungs become nodes; the "problem" rung maps onto the case's
    # single engine-seeded D (ingest_emitted_chain rejects PROBLEM specs).
    non_problem = [n for n in chain_nodes if n.get("node_type") != "problem"]
    if not non_problem:
        return None, []

    # The chain must be authored root-first (root → … → problem).
    try:
        if NodeType(non_problem[0].get("node_type")) != NodeType.ROOT:
            return None, []
    except ValueError:
        return None, []

    problem_refs = {
        n.get("ref") for n in chain_nodes if n.get("node_type") == "problem"
    }
    ref_to_index = {n.get("ref"): i for i, n in enumerate(non_problem)}
    produces_by_ref: dict[str, str] = {}
    for edge in chain_edges:
        cause_ref, effect_ref = edge.get("cause_ref"), edge.get("effect_ref")
        if cause_ref is not None and effect_ref is not None:
            produces_by_ref[cause_ref] = effect_ref

    specs: list[_NodeSpec] = []
    for node in non_problem:
        statement = (node.get("statement") or "").strip()
        if not statement:
            return None, []  # CausalNode rejects an empty statement
        try:
            node_type = NodeType(node.get("node_type"))
        except ValueError:
            return None, []
        effect_ref = produces_by_ref.get(node.get("ref"))
        if effect_ref in problem_refs or effect_ref == "D":
            produces = "D"
        elif effect_ref in ref_to_index:
            produces = f"new_index_{ref_to_index[effect_ref]}"
        else:
            produces = None
        specs.append(
            _NodeSpec(statement=statement, node_type=node_type, produces=produces)
        )

    before = set(case.causal_nodes)
    # One ingest call per cause keeps new_index_N references local to this chain.
    created = ingest_emitted_chain(case, specs, [], [], current_turn)

    ordered = [cid for cid in created if cid]
    if not ordered or created[0] is None:
        return None, []
    root_id = created[0]  # chain is root-first, so the first minted node is root

    # Don't double-seed: if a hypothesis already heads at this (possibly reused)
    # root, the cause is already represented — leave one cause on one node.
    existing_roots = {
        h.root_node_id for h in case.hypotheses.values() if h.root_node_id
    }
    if root_id in existing_roots:
        return None, []

    # Provenance on NEWLY-minted nodes only — never overwrite a reused
    # (self-generated) node's origin. Read surface only.
    new_node_ids = [cid for cid in ordered if cid not in before]
    for node_id in new_node_ids:
        node = case.causal_nodes.get(node_id)
        if node is not None:
            node.metadata = {**(node.metadata or {}), SEEDED_FROM_RUNBOOK_KEY: item_id}

    statement = (cause.get("cause_statement") or specs[0].statement)[:500]
    letter = cause.get("cause_letter", "?")
    name = cause.get("cause_name", "")
    hypothesis = hm.create_hypothesis(
        statement=statement,
        category=HypothesisCategory.OTHER,
        initial_likelihood=KB_SEED_PRIOR,
        current_turn=current_turn,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        state=HypothesisState.ACTIVE,
        rationale=f"Seeded from runbook {item_id} (Cause {letter}: {name})".strip(),
    )
    # Link the hypothesis to its seeded chain (root heads the path; D tails it).
    hypothesis.root_node_id = root_id
    hypothesis.path = [*ordered, d_id]
    case.hypotheses[hypothesis.hypothesis_id] = hypothesis

    return hypothesis.hypothesis_id, new_node_ids
