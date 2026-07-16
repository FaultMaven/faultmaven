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
here — the seeder never invokes a VALIDATED writer. Node VALIDATED is written
only by ``derive_node_states`` (empirical) and ``validate_by_exclusion``
(deductive, the #593 exclusion arm — stamps ``DEDUCTIVE`` on a ROOT once ≥2
siblings are counterfactually refuted); hypothesis VALIDATED is projected from
those node states by ``project_hypothesis_states_from_roots``. A candidate-only,
evidence-less seed at ≤0.5 satisfies none of their preconditions.

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
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from faultmaven.core.investigation.causal_graph import (
    find_canonical_node_id,
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

# Distinctive prefix of a seeded hypothesis's rationale — the *second* provenance
# surface (alongside SEEDED_FROM_RUNBOOK_KEY). A read surface only: the
# provenance-blindness invariant test greps safety modules for this literal too,
# so a mechanism can never sniff seed origin out of the rationale text either.
SEEDED_RATIONALE_PREFIX = "Seeded from runbook"


@dataclass
class SeededRunbook:
    """A retrieved runbook with its loaded causes, ranked by retrieval score."""

    item_id: str
    score: float
    causes: list[dict]


class SkipClass(str, Enum):
    """Why a matched runbook's cause was not seeded — so a zero-seed is never
    silent, and the 'runbook contributed nothing' alarm can distinguish a real
    quality problem from normal, expected non-seeding."""

    INTENTIONAL = "intentional"
    """Fallback (`Z`/`[Default]`) cause — never a candidate root by design."""

    BENIGN_DEDUP = "benign_dedup"
    """Root already seeded — a second retrieved runbook overlapping on a cause
    the first already seeded. Normal and correct; not an alarm."""

    QUALITY_DROP = "quality_drop"
    """A real cause the seeder could not instantiate (no chain, non-root head,
    bad node_type, empty statement, ingest produced nothing). The observability
    signal — a matched runbook silently contributing less than it should."""

    UNSUPPORTED_SHAPE = "unsupported_shape"
    """A well-formed cause using a structure the seeder does not yet model: an
    ``and_group`` AND-convergence, or a non-linear chain (a second root, a
    branching fork, a dangling edge ref). Rejected (not flattened) so a
    co-necessary A∧B is never silently mis-seeded as an OR-alternative A∨B, and a
    branch/fork is never silently linearized to one arbitrary path."""


@dataclass
class SkippedCause:
    """One not-seeded cause, class-tagged. Keyed on (item_id, cause_letter) —
    the cause record has no stable id, and a runbook never uses a letter twice."""

    item_id: str
    cause_letter: str
    skip_class: SkipClass
    reason: str


def _skip(
    item_id: str, cause: dict, skip_class: SkipClass, reason: str
) -> "SkippedCause":
    return SkippedCause(
        item_id=item_id,
        cause_letter=str(cause.get("cause_letter", "?")),
        skip_class=skip_class,
        reason=reason,
    )


@dataclass
class SeedReport:
    """What a seeding pass produced (for observability + tests)."""

    seeded_hypothesis_ids: list[str] = field(default_factory=list)
    seeded_node_ids: list[str] = field(default_factory=list)
    runbooks_used: list[str] = field(default_factory=list)
    skipped: list[SkippedCause] = field(default_factory=list)

    @property
    def seeded_anything(self) -> bool:
        return bool(self.seeded_hypothesis_ids)

    def runbooks_contributing_nothing(self) -> list[str]:
        """item_ids of matched runbooks that seeded nothing for an ACTIONABLE
        reason — the 'matched runbook contributed nothing' signal.

        Actionable = a real cause the seeder should have handled did not seed:
        ``quality_drop`` (malformed) or ``unsupported_shape`` (a shape not yet
        modeled, e.g. ``and_group``). A runbook whose only skips are benign dedup
        or the fallback cause is NOT flagged (a second runbook overlapping an
        already-seeded cause is normal). Caveat: a runbook never entered because
        the ``max_causes`` budget was already spent produces no skip record and is
        not covered here — that zero-contribution is benign (budget, not quality).
        """
        seeded = set(self.runbooks_used)
        actionable = {
            s.item_id
            for s in self.skipped
            if s.skip_class in (SkipClass.QUALITY_DROP, SkipClass.UNSUPPORTED_SHAPE)
        }
        return sorted(actionable - seeded)


@dataclass
class _NodeSpec:
    """Duck-typed spec for ``ingest_emitted_chain`` (statement/node_type/produces)."""

    statement: str
    node_type: NodeType
    produces: Optional[str] = None
    and_group: Optional[str] = None


def case_has_seeded_candidates(case: "Case") -> bool:
    """True if any causal node in the case was seeded from a runbook.

    Used to gate the seeded-candidate AUTHORITY prompt variant: the prompt must
    only tell the LLM "candidates are already in your graph" when that is
    actually true this case (the flag can be on while no runbook matched).
    """
    return any(
        SEEDED_FROM_RUNBOOK_KEY in (node.metadata or {})
        for node in case.causal_nodes.values()
    )


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
        # Budget spent: remaining runbooks are not entered, so they leave no skip
        # record. That zero-contribution is benign (budget, not a quality drop)
        # and is deliberately NOT alarmed — see runbooks_contributing_nothing().
        if len(report.seeded_hypothesis_ids) >= max_causes:
            break
        for cause in runbook.causes or []:
            if len(report.seeded_hypothesis_ids) >= max_causes:
                break
            # Guard each cause so one bad cause cannot abort the whole pass or
            # discard the report: an unexpected error is recorded as a skip (so it
            # is visible + alarmed) and the loop continues. This keeps the "never
            # raised" contract true even if _seed_one_cause hits malformed data.
            try:
                hyp_id, new_node_ids, skip = _seed_one_cause(
                    case, runbook.item_id, cause, current_turn, hm, d_id
                )
            except Exception:
                logger.error(
                    "KB cause seeder: seeding cause %s of runbook %s raised — "
                    "recording as a skip and continuing",
                    cause.get("cause_letter", "?"),
                    runbook.item_id,
                    exc_info=True,
                )
                report.skipped.append(
                    _skip(
                        runbook.item_id,
                        cause,
                        SkipClass.QUALITY_DROP,
                        "seeding raised an unexpected error",
                    )
                )
                continue
            if hyp_id is not None:
                report.seeded_hypothesis_ids.append(hyp_id)
                report.seeded_node_ids.extend(new_node_ids)
                if runbook.item_id not in report.runbooks_used:
                    report.runbooks_used.append(runbook.item_id)
            elif skip is not None:
                report.skipped.append(skip)

    contributed_nothing = report.runbooks_contributing_nothing()
    if contributed_nothing:
        logger.warning(
            "KB cause seeder: %d matched runbook(s) contributed no candidate for "
            "case %s despite matching — a real cause could not be seeded "
            "(malformed or unsupported shape; see skip records): %s",
            len(contributed_nothing),
            getattr(case, "case_id", "?"),
            contributed_nothing,
        )
    if report.seeded_anything:
        logger.info(
            "KB cause seeder: seeded %d candidate cause(s) from %d runbook(s) "
            "for case %s (%d cause(s) skipped)",
            len(report.seeded_hypothesis_ids),
            len(report.runbooks_used),
            getattr(case, "case_id", "?"),
            len(report.skipped),
        )
    return report


def _reject_nonlinear_shape(
    non_problem: list[dict],
    chain_edges: list[dict],
    problem_refs: set,
    ref_to_index: dict,
) -> Optional[str]:
    """Reason string if the chain is not a **single linear root→…→D path with
    every edge resolving**; ``None`` if it is well-formed for linear seeding.

    The seeder models only a linear chain (one root, each rung producing exactly
    one next rung, terminating at D). Three well-formed-but-unmodeled shapes would
    otherwise be *silently* mis-seeded:

    - a **second root** mid-chain (the head-is-root check upstream passes, but a
      later root makes it two chains, not one);
    - a **branching fork** — a rung with more than one outgoing edge, which
      ``produces_by_ref``'s last-edge-wins would flatten to one arbitrary branch;
    - a **dangling ref** — an edge whose ``cause_ref``/``effect_ref`` resolves to
      no node, which would silently leave a rung disconnected.

    Honor-or-reject (same discipline as ``and_group``): REJECT rather than
    mis-model. Zero instances in the shipped pack (all 3 are 0/640); the guard
    protects the case→runbook conversion (produce) path, where LLM-authored
    chains are far likelier to branch than the curated corpus, so a shape gap
    cannot go live the day the flywheel closes.
    """
    # Exactly one root: a second root makes this two chains, not a linear one.
    if sum(1 for n in non_problem if n.get("node_type") == NodeType.ROOT.value) > 1:
        return "multiple roots (not a single linear chain)"

    # Every edge must resolve on both ends, and no rung may fork (produce >1
    # effect) — either would silently linearize a non-linear chain.
    forked: set = set()
    for edge in chain_edges:
        cause_ref, effect_ref = edge.get("cause_ref"), edge.get("effect_ref")
        if cause_ref not in ref_to_index:
            return "edge cause_ref does not resolve to a chain node"
        if not (
            effect_ref == "D"
            or effect_ref in problem_refs
            or effect_ref in ref_to_index
        ):
            return "edge effect_ref does not resolve to a chain node"
        if cause_ref in forked:
            return "branching node (a rung produces more than one effect)"
        forked.add(cause_ref)
    return None


def _seed_one_cause(
    case: "Case",
    item_id: str,
    cause: dict[str, Any],
    current_turn: int,
    hm: HypothesisManager,
    d_id: str,
) -> tuple[Optional[str], list[str], Optional[SkippedCause]]:
    """Seed one Cause's chain.

    Returns ``(hypothesis_id, new_node_ids, skip)``: on success the id + node ids
    with ``skip=None``; on a no-seed the class-tagged ``SkippedCause`` so no drop
    is silent. The fallback cause is an INTENTIONAL skip; a real cause the seeder
    can't instantiate is a QUALITY_DROP; a well-formed cause using an unmodeled
    shape (``and_group`` AND-convergence, or a non-linear chain — multiple roots,
    a branching fork, a dangling edge ref) is an UNSUPPORTED_SHAPE reject (never
    flattened / mis-seeded); an already-seeded root is a BENIGN_DEDUP.

    The BENIGN_DEDUP check runs **before** ``ingest_emitted_chain`` (via the
    shared dedup key ``find_canonical_node_id``): a second runbook that shares a
    root but diverges mid-chain is skipped without first minting the divergent
    intermediate rungs, so a dedup never leaves orphan nodes/edges in the graph.
    """
    if cause.get("is_fallback_cause"):
        return (
            None,
            [],
            _skip(item_id, cause, SkipClass.INTENTIONAL, "fallback cause (no chain)"),
        )

    chain_nodes = cause.get("chain_nodes") or []
    chain_edges = cause.get("chain_edges") or []

    # AND-convergence is not yet modeled. The seeder builds edges from
    # cause_ref/effect_ref only and defaults every edge's and_group to None, so a
    # co-necessary AND-set (edges sharing (effect, and_group)) would be silently
    # flattened into independent OR-alternatives — turning "A AND B are both
    # required" into "A OR B", a MECE mis-model. Honor-or-reject: until
    # AND-seeding is built, REJECT such a cause rather than mis-seed it. Zero
    # instances in the shipped pack (and_group = 0/640); the guard exists for the
    # case→runbook conversion (produce) path (converted runbooks generate v4
    # structure) and future authoring. Checked on nodes too, since a producer
    # could carry and_group there (the LLM-facing CausalNodeToAdd schema does).
    if any(e.get("and_group") is not None for e in chain_edges) or any(
        n.get("and_group") is not None for n in chain_nodes
    ):
        return (
            None,
            [],
            _skip(
                item_id,
                cause,
                SkipClass.UNSUPPORTED_SHAPE,
                "and_group AND-convergence not yet modeled",
            ),
        )

    # Non-problem rungs become nodes; the "problem" rung maps onto the case's
    # single engine-seeded D (ingest_emitted_chain rejects PROBLEM specs).
    non_problem = [n for n in chain_nodes if n.get("node_type") != "problem"]
    if not non_problem:
        return (
            None,
            [],
            _skip(item_id, cause, SkipClass.QUALITY_DROP, "no root/intermediate nodes"),
        )

    # The chain must be authored root-first (root → … → problem).
    try:
        if NodeType(non_problem[0].get("node_type")) != NodeType.ROOT:
            return (
                None,
                [],
                _skip(
                    item_id, cause, SkipClass.QUALITY_DROP, "chain head is not a root"
                ),
            )
    except ValueError:
        return (
            None,
            [],
            _skip(
                item_id, cause, SkipClass.QUALITY_DROP, "unrecognized head node_type"
            ),
        )

    problem_refs = {
        n.get("ref") for n in chain_nodes if n.get("node_type") == "problem"
    }
    ref_to_index = {n.get("ref"): i for i, n in enumerate(non_problem)}

    # Reject any non-linear shape (second root, branching fork, dangling edge ref)
    # rather than silently flatten it — same honor-or-reject discipline as the
    # and_group guard above. Runs before spec-building so a mis-shaped chain never
    # ingests.
    nonlinear = _reject_nonlinear_shape(
        non_problem, chain_edges, problem_refs, ref_to_index
    )
    if nonlinear is not None:
        return (None, [], _skip(item_id, cause, SkipClass.UNSUPPORTED_SHAPE, nonlinear))

    produces_by_ref: dict[str, str] = {}
    for edge in chain_edges:
        cause_ref, effect_ref = edge.get("cause_ref"), edge.get("effect_ref")
        if cause_ref is not None and effect_ref is not None:
            produces_by_ref[cause_ref] = effect_ref

    specs: list[_NodeSpec] = []
    for node in non_problem:
        statement = (node.get("statement") or "").strip()
        if not statement:
            return (
                None,
                [],
                _skip(item_id, cause, SkipClass.QUALITY_DROP, "empty node statement"),
            )
        try:
            node_type = NodeType(node.get("node_type"))
        except ValueError:
            return (
                None,
                [],
                _skip(item_id, cause, SkipClass.QUALITY_DROP, "unrecognized node_type"),
            )
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

    # Don't double-seed — BEFORE ingest, so a dedup never mints orphan nodes. A
    # root whose statement matches an existing node reuses it under ingest's
    # exact-match dedup (find_canonical_node_id is that same key); if that reused
    # root already heads a hypothesis, the cause is already represented. Skipping
    # here (not after ingest) means a second runbook sharing this root but
    # diverging mid-chain never mints its divergent intermediate rungs as orphans.
    existing_roots = {
        h.root_node_id for h in case.hypotheses.values() if h.root_node_id
    }
    canonical_root = find_canonical_node_id(case, NodeType.ROOT, specs[0].statement)
    if canonical_root is not None and canonical_root in existing_roots:
        return (
            None,
            [],
            _skip(item_id, cause, SkipClass.BENIGN_DEDUP, "root already seeded"),
        )

    before = set(case.causal_nodes)
    # One ingest call per cause keeps new_index_N references local to this chain.
    created = ingest_emitted_chain(case, specs, [], [], current_turn)

    ordered = [cid for cid in created if cid]
    if not ordered or created[0] is None:
        return (
            None,
            [],
            _skip(
                item_id, cause, SkipClass.QUALITY_DROP, "ingest produced no root node"
            ),
        )
    root_id = created[0]  # chain is root-first, so the first minted node is root

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
        rationale=(
            f"{SEEDED_RATIONALE_PREFIX} {item_id} (Cause {letter}: {name})".strip()
        ),
    )
    # Link the hypothesis to its seeded chain (root heads the path; D tails it).
    hypothesis.root_node_id = root_id
    hypothesis.path = [*ordered, d_id]
    case.hypotheses[hypothesis.hypothesis_id] = hypothesis

    return hypothesis.hypothesis_id, new_node_ids, None
