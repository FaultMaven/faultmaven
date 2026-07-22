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
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from faultmaven.core.investigation.causal_graph import (
    find_canonical_node_id,
    find_duplicate_hypothesis,
    ingest_emitted_chain,
    seed_problem_node,
)
from faultmaven.core.investigation.hypothesis_manager import (
    ANCHORING_SAME_CATEGORY_THRESHOLD,
    HypothesisManager,
    create_hypothesis_manager,
)
from faultmaven.modules.case.contracts import (
    EvidenceNeed,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    NeedPriority,
    NeedPurpose,
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

# Provenance key on a seeded ROOT node's metadata carrying the cause's runbook
# ``interventions`` (captured at seed time so the SOLUTION-stage context render
# surfaces them as candidate-solution priors without re-fetching the runbook).
# A THIRD origin-derived surface — a node carries it only if it was seeded — so
# the provenance-blindness invariant test bans it from every safety module too.
SEEDED_INTERVENTIONS_KEY = "seeded_interventions"

# A rung indicator is authored as "[Step N] <observable>" (the runbook's own
# step numbering). The step reference is meaningful only inside the runbook, so
# it is stripped when the indicator becomes a case evidence-need's request_text.
_STEP_REF_PREFIX_RE = re.compile(r"^\s*\[Step\s+\d+\]\s*")


@dataclass
class SeededRunbook:
    """A retrieved runbook with its loaded causes, ranked by retrieval score."""

    item_id: str
    score: float
    causes: list[dict]


class SkipClass(str, Enum):
    """Why a matched runbook's cause was not seeded — so a zero-seed is never
    silent, and the 'runbook contributed nothing' alarm can distinguish a real
    quality problem from normal, expected non-seeding.

    Two families. **Expected non-seeding, never alarmed** — ``INTENTIONAL``
    (fallback cause), ``BENIGN_DEDUP`` (already-represented cause), and
    ``CONVERGES_UNMODELED`` (a grammar-legal cross-chain convergence the seeder
    does not model): all are normal outcomes on a well-authored runbook.
    **Actionable** — ``QUALITY_DROP`` (a malformed cause) and ``UNSUPPORTED_SHAPE``
    (a well-formed cause in a shape the seeder does not model): a real cause the
    seeder should have handled did not seed, which the 'contributed nothing' alarm
    reports."""

    INTENTIONAL = "intentional"
    """Fallback (`Z`/`[Default]`) cause — never a candidate root by design.
    Expected non-seeding; never alarmed."""

    BENIGN_DEDUP = "benign_dedup"
    """Root already represented — a second retrieved runbook overlapping on a
    cause already seeded (exact-normalized root match, or a paraphrase of a
    standing hypothesis caught by the INV-36 dedup predicate). Normal and correct;
    never alarmed."""

    CONVERGES_UNMODELED = "converges_unmodeled"
    """A grammar-legal cross-chain convergence the seeder does not yet model: the
    v4 ``converges: <Cause>.<ref>`` directive makes this cause's chain terminate
    inside ANOTHER Cause's chain, so it cannot form a self-contained root→D path.
    Rejected — never partially seeded or flattened. This is a legal, well-authored
    construct (the sole cross-chain grammar), so it is expected non-seeding and
    never alarmed — modeling it is future work, not a quality defect."""

    QUALITY_DROP = "quality_drop"
    """A real cause the seeder could not instantiate (no chain, non-root head,
    bad node_type, empty statement, ingest produced nothing). Actionable — the
    observability signal that a matched runbook silently contributed less than it
    should."""

    UNSUPPORTED_SHAPE = "unsupported_shape"
    """A well-formed cause using a structure the seeder does not yet model: an
    ``and_group`` AND-convergence, or a non-linear chain (a second root, a
    branching fork, a dangling edge ref). Rejected (not flattened) so a
    co-necessary A∧B is never silently mis-seeded as an OR-alternative A∨B, and a
    branch/fork is never silently linearized to one arbitrary path. Actionable."""


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
    seeded_need_ids: list[str] = field(default_factory=list)
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
        modeled, e.g. ``and_group``). A runbook whose only skips are the expected
        non-seeding classes — ``benign_dedup`` (overlap), ``intentional`` (the
        fallback cause), or ``converges_unmodeled`` (a grammar-legal cross-chain
        convergence) — is NOT flagged: those are normal outcomes on a
        well-authored runbook, not quality drops. Caveat: a runbook never entered
        because the ``max_causes`` budget was already spent produces no skip
        record and is not covered here — that zero-contribution is benign
        (budget, not quality).
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


def confirmed_root_seed_origin(case: "Case") -> Optional[str]:
    """The runbook a RESOLVED case's CONFIRMED root cause was SEEDED from, or
    ``None`` if that cause was reached by exploration (or the case has no single
    counterfactually-confirmed root).

    This is the direct provenance signal behind runbook-generation UNIQUENESS
    (Phase 5.2b): if a case was resolved by validating a cause the seeder planted
    from an existing runbook, generating a runbook from it would only duplicate
    that runbook. The offer gate reads this to skip the redundant offer and point
    the user at the covering runbook instead.

    Keyed on the CONFIRMED root's distinct-cause CLUSTER
    (``distinct_cause_clusters``), NOT "does any seed exist on the case"
    (``case_has_seeded_candidates``): a case routinely carries seeded candidates
    the investigation later refuted, and those must never suppress a runbook for
    a DIFFERENT, self-discovered cause. A seed marker anywhere in the confirmed
    cause's cluster counts — the confirmed root itself, a re-emitted duplicate of
    it, or a deepened rung on the same causal line all collapse into one cluster,
    so a seeded candidate the LLM validated-then-restated still resolves to its
    origin. Clustering ranges over ALL roots (not just the confirmed ones) so a
    seeded *candidate* duplicate that never itself validated still collapses onto
    the confirmed root.

    NOT a safety mechanism, and deliberately outside the provenance-blindness
    invariant that keeps a seed indistinguishable from a self-generated
    hypothesis for every VALIDATION / decay / anchoring / gating path. The only
    reader is the runbook-generation OFFER gate — a knowledge-lifecycle decision,
    not a conclusion. The worst outcome of a wrong answer here is a missing or
    redundant "generate runbook" affordance, never an incorrect conclusion or a
    collapse under pressure: the manual ``POST /knowledge/runbooks/create``
    escape hatch and the async ``EXISTING_COVERS`` similarity backstop both
    remain. Known false-negatives (a reused node the seeder never restamped, a
    ``BENIGN_DEDUP`` overlap, a retrieval miss) are exactly why this is a cheap
    SYNC tier ABOVE the similarity backstop, not a replacement for it.
    """
    nodes = getattr(case, "causal_nodes", None) or {}
    for member_id in _confirmed_cause_cluster_members(case):
        origin = (nodes[member_id].metadata or {}).get(SEEDED_FROM_RUNBOOK_KEY)
        if origin:
            return origin
    return None


def confirmed_cause_interventions(case: "Case") -> list[dict[str, str]]:
    """The runbook ``interventions`` captured on a RESOLVED case's CONFIRMED root
    cause, or ``[]`` if that cause was self-discovered (or the case has no single
    counterfactually-confirmed seeded root).

    The R9 read half of seed-time interventions capture: at seed time
    ``_seed_one_cause`` stashes a cause's ``interventions`` list onto its ROOT
    node's metadata (``SEEDED_INTERVENTIONS_KEY``); once that root is
    counterfactually confirmed, the SOLUTION-stage context render surfaces those
    interventions as CANDIDATE-solution priors (quadrant + text) so the LLM emits
    a quadrant-carrying ``SolutionToAdd`` instead of re-deriving the fix from
    prose. Keyed on the confirmed root's distinct-cause CLUSTER exactly like
    ``confirmed_root_seed_origin`` (a validated-then-restated seed still resolves
    to its captured interventions).

    NOT a safety mechanism, and deliberately outside the provenance-blindness
    invariant: the only reader is the prompt-render path (a prior offered to the
    LLM, still gated by M5 and the user's accept/verify), never a VALIDATION /
    decay / anchoring / gating path. Returns the seed-time-sanitized list verbatim
    (``[{"quadrant","ref","text"}]``); an empty list when nothing was captured.
    """
    nodes = getattr(case, "causal_nodes", None) or {}
    for member_id in _confirmed_cause_cluster_members(case):
        interventions = (nodes[member_id].metadata or {}).get(SEEDED_INTERVENTIONS_KEY)
        if interventions:
            return interventions
    return []


def _confirmed_cause_cluster_members(case: "Case") -> list[str]:
    """Node ids of every member of the CONFIRMED root's distinct-cause cluster(s).

    Shared core of the two case-level origin readers
    (``confirmed_root_seed_origin`` + ``confirmed_cause_interventions``): find the
    cluster(s) containing a counterfactually-confirmed VALIDATED root and return
    their members (in cluster iteration order) for a provenance scan. Empty when
    the case has no single counterfactually-confirmed root.

    Clusters over the LIVE roots only. A REFUTED seeded root must never be the
    basis for "resolved by applying runbook X" — a disproven seed was, by
    definition, NOT what resolved the case. Excluding them before clustering also
    honors ``_live_descendant_ids``'s precondition (endpoints are never REFUTED):
    it prunes refuted intermediates but not a refuted START node, so a refuted
    seeded root could otherwise cluster with a deeper confirmed root on its line
    and falsely claim its origin. Seeded-candidate duplicates are CANDIDATE (not
    REFUTED) and are kept, so the duplicate-collapse holds. Clustering ranges over
    ALL live roots (not just the confirmed ones) so a seeded *candidate* duplicate
    that never itself validated still collapses onto the confirmed root.
    """
    from faultmaven.core.investigation.causal_graph import distinct_cause_clusters
    from faultmaven.core.investigation.cause_assurance import (
        evidence_category_map,
        root_counterfactually_confirmed,
    )
    from faultmaven.modules.case.contracts import NodeState

    nodes = getattr(case, "causal_nodes", None) or {}
    live_root_ids = {
        nid
        for nid, node in nodes.items()
        if node.node_type == NodeType.ROOT and node.node_state != NodeState.REFUTED
    }
    if not live_root_ids:
        return []

    cat_by_id = evidence_category_map(case)
    confirmed_root_ids = {
        rid
        for rid in live_root_ids
        if nodes[rid].node_state == NodeState.VALIDATED
        and root_counterfactually_confirmed(nodes[rid], cat_by_id)
    }
    if not confirmed_root_ids:
        return []

    members: list[str] = []
    for cluster in distinct_cause_clusters(case, live_root_ids):
        if not cluster & confirmed_root_ids:
            continue
        members.extend(cluster)
    return members


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
            needs_before = len(case.evidence_needs)
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
                # Needs are appended in-place by _seed_one_cause on success; the
                # tail slice is exactly those minted for this cause.
                report.seeded_need_ids.extend(
                    n.need_id for n in case.evidence_needs[needs_before:]
                )
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
            "for case %s (%d cause(s) skipped, %d rung evidence-need(s) emitted)",
            len(report.seeded_hypothesis_ids),
            len(report.runbooks_used),
            getattr(case, "case_id", "?"),
            len(report.skipped),
            len(report.seeded_need_ids),
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
    one next rung, terminating at D). These well-formed-but-unmodeled shapes would
    otherwise be *silently* mis-seeded, so each is rejected:

    - a **missing/empty node ref** — a non-problem rung with no ``ref`` (``None``
      or ``""``). It cannot be wired linearly and poisons the resolve checks:
      ``ref_to_index`` keys ``None``/``""`` as a valid node, so a null-ref edge
      would "resolve" and pass the walk, then be dropped by ``produces_by_ref``
      (which skips null-ref edges) — a disconnected/self-referential seed;
    - a **second root** mid-chain (the head-is-root check upstream passes, but a
      later root makes it two chains, not one);
    - a **branching fork** — a rung with more than one outgoing edge, which
      ``produces_by_ref``'s last-edge-wins would flatten to one arbitrary branch;
    - a **convergence/join** — a rung produced by more than one cause (a repeated
      ``effect_ref``) without an ``and_group``, which is a merge, not a link in a
      single path (``and_group`` AND-convergence is rejected separately upstream).
      Convergence *onto D* counts too: ``"D"`` and every problem-node ref denote
      the one case D node, so they are canonicalized before the merge check — two
      rungs producing D via different literals is still a join;
    - a **dangling ref** — an edge whose ``cause_ref``/``effect_ref`` resolves to
      no node, which would silently leave a rung disconnected;
    - a **cycle, fragment, or non-D-terminating / inverted chain** — even when
      every ref appears at most once, the edges may form a disjoint path + cycle,
      a chain that never reaches D, or a rung off the root→D route. The final
      reachability walk requires the edges to form exactly one simple path from
      the head root through every rung, terminating at D.

    Honor-or-reject (same discipline as ``and_group``): REJECT rather than
    mis-model. Zero instances in the shipped pack; the guard protects the
    case→runbook conversion (produce) path, where LLM-authored chains are far
    likelier to branch than the curated corpus, so a shape gap cannot go live the
    day the flywheel closes.
    """
    # Every non-problem rung must carry a usable string ref. A missing/None or
    # empty ref cannot be wired linearly, and it silently poisons the resolve
    # checks below: ``ref_to_index`` keys ``None``/``""`` as a valid node, so an
    # edge with a null cause_ref/effect_ref would "resolve", pass the walk, then
    # be dropped by ``produces_by_ref`` (which skips null-ref edges) — minting a
    # disconnected or self-referential seed. Reject rather than mis-seed. (The
    # curated pack always refs; LLM-authored produce-path chains may not.)
    for n in non_problem:
        ref = n.get("ref")
        if not isinstance(ref, str) or not ref:
            return "chain node has a missing or empty ref"

    # Exactly one root: a second root makes this two chains, not a linear one.
    if sum(1 for n in non_problem if n.get("node_type") == NodeType.ROOT.value) > 1:
        return "multiple roots (not a single linear chain)"

    # "D" and every problem-node ref denote the single case D node; canonicalize
    # so a join onto D via different literals is still seen as one effect.
    def _canon(ref: Optional[str]) -> Optional[str]:
        return "D" if (ref == "D" or ref in problem_refs) else ref

    # Every edge must resolve on both ends; no rung may fork (produce >1 effect)
    # nor be a merge point (be produced by >1 cause) — a single linear chain has
    # each cause_ref and each canonical effect_ref appear at most once.
    forked: set = set()
    converged: set = set()
    produces: dict = {}
    for edge in chain_edges:
        cause_ref = edge.get("cause_ref")
        effect_ref = _canon(edge.get("effect_ref"))
        if cause_ref not in ref_to_index:
            return "edge cause_ref does not resolve to a chain node"
        if not (effect_ref == "D" or effect_ref in ref_to_index):
            return "edge effect_ref does not resolve to a chain node"
        if cause_ref in forked:
            return "branching node (a rung produces more than one effect)"
        if effect_ref in converged:
            return "converging node (a rung is produced by more than one cause)"
        forked.add(cause_ref)
        converged.add(effect_ref)
        produces[cause_ref] = effect_ref

    # Reachability: the edges must form exactly one simple path from the head root
    # through every non-problem rung, terminating at D. The ≤once checks above make
    # the successor map deterministic, so the walk catches what they cannot —
    # cycles, disconnected fragments, non-D-terminating and inverted chains.
    root_ref = non_problem[0].get("ref")
    visited: set = set()
    cur = root_ref
    while cur != "D":
        if cur in visited:
            return "cycle in chain (not a single linear path)"
        if cur not in produces:
            return "chain does not terminate at the problem (D)"
        visited.add(cur)
        cur = produces[cur]
    if len(visited) != len(non_problem):
        return "disconnected rung (not every node lies on the root→D path)"
    return None


def _sanitize_interventions(raw: Any) -> list[dict[str, str]]:
    """Normalize a cause's ``interventions`` for seed-time capture.

    ``interventions`` is authored by the extractor as
    ``list[{"quadrant","ref","text"}]`` but this reads verbatim
    ``metadata["causes"]`` (malformable on the produce/conversion path). Keep only
    well-formed dict entries carrying non-empty text; coerce each field to a
    bounded string; drop everything else. The ``quadrant`` is preserved verbatim
    (honor-or-reject against ``InterventionQuadrant`` happens later, at solution
    apply). Never raises — a malformed interventions list must not fail seeding.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for iv in raw:
        if not isinstance(iv, dict):
            continue
        text = str(iv.get("text") or "").strip()[:1000]
        if not text:
            continue
        out.append(
            {
                "quadrant": str(iv.get("quadrant") or "").strip()[:50],
                "ref": str(iv.get("ref") or "").strip()[:50],
                "text": text,
            }
        )
    return out


def _emit_rung_needs(
    case: "Case",
    item_id: str,
    cause: dict[str, Any],
    hyp_id: str,
    current_turn: int,
) -> list[str]:
    """Emit the seeded cause's ``rung_indicators`` as engine-minted evidence-needs.

    A v4 cause's ``rung_indicators`` (``dict[rung_ref -> list[observable]]``) are
    the per-rung checkable signals that a rung actually holds — the richest slice
    of the ``metadata["causes"]`` record and, until now, structurally write-only
    (consumed only as prose by the LLM). Each indicator becomes one PENDING
    ``CAUSAL_VERIFICATION`` need in ``case.evidence_needs``, motivated by the
    seeded hypothesis, so a seeded chain arrives carrying its *own* discriminators
    rather than leaning entirely on the LLM to invent them.

    Prior, not gate — every property keeps a seeded need mechanically identical to
    an LLM-emitted one, granting no evidentiary weight:

    - **PENDING, never auto-fulfilled.** Fulfillment requires a real evidence row
      (``fulfilling_evidence_ids``); the seeder links none. A seeded need grounds
      only when an actual datum arrives, exactly like any other need.
    - **priority=LOW.** Sinks a seeded ask in the rendered ``<evidence_needs>``
      ordering. Not a suppression guarantee: surfacing *selection*
      (``select_surfaced_causal_needs``) is deliberately priority- and
      origin-blind — it ranks by discriminating power (``request_text`` rarity)
      plus rotation — so a discriminating seeded rung is shown like any other.
    - **obtainability=UNKNOWN (fail-safe).** A seeded need never contributes to the
      declared-data-wall (``verification_status._candidate_unresolvable`` counts a
      candidate walled only when *all* its discriminators are UNOBTAINABLE). It
      makes the wall *honestly computable* for a seeded candidate — if the model
      later declares a seeded rung ungettable — without ever moving a case toward
      INSUFFICIENT_EVIDENCE on its own.
    - **Clears when the seed dies.** The needs are motivated solely by the seeded
      hypothesis, so the engine's motivator-based auto-supersession retires them
      when that hypothesis is retired (evidence-needs-design §7.4) — no bespoke
      cleanup.
    - **Provenance-blind to safety.** Origin lives only in the ``rationale``
      (``SEEDED_RATIONALE_PREFIX``) — a read surface the provenance-blindness
      invariant test bans from every safety module (now including
      ``verification_status`` and ``evidence_need_surfacing``, the need-consuming
      paths). Nothing branches on it.

    Best-effort and self-contained: appends directly to ``case.evidence_needs`` and
    returns the new ``need_id``s (for observability + tests). Malformed indicators
    are skipped, never raised — seeding a cause must not fail because a rung's
    observable was empty.
    """
    rung_indicators = cause.get("rung_indicators") or {}
    if not isinstance(rung_indicators, dict):
        return []
    letter = cause.get("cause_letter", "?")
    new_need_ids: list[str] = []
    seen_text: set[str] = set()
    for ref, indicators in rung_indicators.items():
        # rung_indicators is authored as dict[ref -> list[str]] by the extractor,
        # but this reads verbatim metadata["causes"] (malformable on the produce
        # path). A non-list value must be skipped, not iterated: a bare string
        # would enumerate per-character (one garbage need per char), a scalar
        # would raise TypeError and abort the cause. The seeder contract is
        # "never raised", so skip the malformed rung and keep the candidate.
        if not isinstance(indicators, list):
            continue
        for indicator in indicators:
            request_text = _STEP_REF_PREFIX_RE.sub("", str(indicator)).strip()[:500]
            if not request_text or request_text in seen_text:
                # Empty after stripping the step ref, or a duplicate observable
                # already asked for by an earlier rung of this same cause.
                continue
            seen_text.add(request_text)
            need = EvidenceNeed(
                case_id=case.case_id,
                purpose=NeedPurpose.CAUSAL_VERIFICATION,
                request_text=request_text,
                rationale=(
                    f"{SEEDED_RATIONALE_PREFIX} {item_id} "
                    f"(Cause {letter} rung {ref}): expected observable if this "
                    "candidate holds."
                )[:500],
                priority=NeedPriority.LOW,
                motivating_hypothesis_ids=[hyp_id],
                created_at_turn=current_turn,
            )
            case.evidence_needs.append(need)
            new_need_ids.append(need.need_id)
    return new_need_ids


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
    flattened / mis-seeded); a grammar-legal ``converges:`` cross-chain directive
    is a CONVERGES_UNMODELED reject (expected non-seed, not alarmed); a root
    already represented (exact-normalized match, or an INV-36 paraphrase of a
    standing hypothesis) is a BENIGN_DEDUP.

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

    # A v4 Cause may omit **Chain** for a simple one-step cause — the grammar
    # makes Chain optional and declares that "its absence yields a degenerate
    # root → D chain on ingestion" (cause_grammar). Such a cause names its root
    # directly in **Statement**, implying root → D (one hop). Synthesize that
    # chain here so a one-step cause seeds a single candidate like a chained one,
    # rather than being dropped as "no root/intermediate nodes" — which would
    # leave the flywheel's simplest converted causes contributing nothing and
    # falsely trip runbooks_contributing_nothing(). The D rung carries no
    # statement (it maps onto the case's engine-seeded D, whose spec ingest
    # rejects). 0/640 in the shipped pack (every real cause is chained); this
    # path is exercised only by the produce/conversion side. A cause with
    # neither a chain nor a Statement is genuinely empty and still QUALITY_DROPs
    # below.
    seed_statement = (cause.get("cause_statement") or "").strip()
    if not chain_nodes and seed_statement:
        chain_nodes = [
            {"ref": "root", "statement": seed_statement, "node_type": "root"},
            {"ref": "D", "statement": "", "node_type": "problem"},
        ]
        chain_edges = [{"cause_ref": "root", "effect_ref": "D"}]

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

    # Cross-chain convergence is the v4 grammar's ONE cross-chain construct: a
    # ``converges: <Cause>.<ref>`` directive, which both producers (the fm-side
    # runbook_cause_extractor and the kb-toolkit pack builder) emit as a chain edge
    # carrying a truthy ``converges`` key whose ``effect_ref`` points INTO another
    # Cause's chain (e.g. "B.s1"). That target resolves to no node in THIS cause's
    # chain, so the cause cannot form a self-contained root→D path. Detect it HERE,
    # before _reject_nonlinear_shape — otherwise the convergence edge is misdiagnosed
    # as a dangling ref and mis-classed UNSUPPORTED_SHAPE, tripping the "contributed
    # nothing" alarm on a grammar-LEGAL, well-authored runbook. Honor-or-reject: the
    # convergence is not yet modeled, so REJECT the whole cause (never partially seed
    # or flatten it), tagged CONVERGES_UNMODELED — an EXPECTED non-seed, not alarmed.
    # 0/640 in the shipped pack; produce-path / future-authoring protection.
    if any(e.get("converges") for e in chain_edges):
        return (
            None,
            [],
            _skip(
                item_id,
                cause,
                SkipClass.CONVERGES_UNMODELED,
                "cross-chain convergence (converges: directive) not yet modeled",
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

    # The statement the seed's hypothesis WOULD carry — hoisted above ingest so
    # the paraphrase dedup below can test it BEFORE any node is minted (a dedup
    # that fires after ingest would leave orphan rungs). Reused verbatim as the
    # hypothesis statement on the seed path.
    hyp_statement = (cause.get("cause_statement") or specs[0].statement)[:500]

    # Don't double-seed — BEFORE ingest, so a dedup never mints orphan nodes. Two
    # pre-ingest checks, both BENIGN_DEDUP (a second runbook overlapping an
    # already-represented cause is normal and correct, never alarmed):
    #
    # (1) Exact-normalized root. A root whose statement matches an existing node
    #     reuses it under ingest's exact-match dedup (find_canonical_node_id is
    #     that same key); if that reused root already heads a hypothesis, the cause
    #     is already represented. Deciding here (not after ingest) means a second
    #     runbook sharing this root but diverging mid-chain never mints its
    #     divergent intermediate rungs as orphans.
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

    # (2) Paraphrase of a standing CHAIN-HEADING hypothesis. Two retrieved runbooks
    #     describing the same cause in different words would otherwise co-seed two
    #     paraphrase OR-siblings, inflating the differential and raising
    #     validate_by_exclusion friction (exclusion needs ≥2 siblings
    #     counterfactually refuted, so a phantom sibling spuriously raises the bar).
    #     Reuse the EXISTING INV-36 dedup predicate — the SAME
    #     find_duplicate_hypothesis (mutual-Jaccard + polarity guard +
    #     numeric-discriminator guard, deliberately fail-open) already applied to
    #     the LLM's hypotheses_to_add — not a bespoke scorer. Same predicate and
    #     same dedup DECISION as the INV-36 path that would have deduped this exact
    #     statement had the LLM emitted it; the difference is the reconciliation —
    #     INV-36 surfaces the matched id so the LLM UPDATES the standing hypothesis,
    #     whereas here the cause is a silent skip record (there is no LLM emission
    #     to merge). The fail-open guards (negation, numeric discriminators) keep
    #     genuinely distinct siblings — a negated restatement, or one differing only
    #     by a number — separate.
    #
    #     Scoped to CHAIN-HEADING hypotheses (root_node_id set) — the same scope as
    #     the exact-root check above. A chain-less standing hypothesis must never
    #     paraphrase-suppress a structurally-rich runbook cause: doing so would
    #     silently discard the cause's chain, its rung-indicator evidence-needs, and
    #     its interventions, classed benign and never surfaced. If both a chain-less
    #     match and a chain-heading paraphrase exist, the duplicate-sibling cost is
    #     preferred over the silent structural loss — the redundant sibling is the
    #     LLM's to reconcile. (Today the sole call site is the INQUIRY→INVESTIGATING
    #     transition, where only this-batch root-bearing seeds exist, so the scopes
    #     coincide; the guard hardens the documented mid-INVESTIGATING re-seed
    #     follow-on, under which a bare standing hypothesis could otherwise exist.)
    dup_hid = find_duplicate_hypothesis(hyp_statement, case)
    if dup_hid is not None and case.hypotheses[dup_hid].root_node_id:
        return (
            None,
            [],
            _skip(
                item_id,
                cause,
                SkipClass.BENIGN_DEDUP,
                f"duplicates standing hypothesis {dup_hid} (paraphrase)",
            ),
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

    # R9: capture the cause's runbook interventions on the seeded ROOT node so the
    # SOLUTION-stage render can surface them as candidate-solution priors once the
    # root is confirmed — without re-fetching the runbook. Only on a freshly-minted
    # root (root_id in new_node_ids): a reused self-generated root must stay
    # origin-free, same discipline as the provenance stamp above. Read surface only
    # (provenance-blind); the SOLUTION-stage decision it feeds is a prior, gated by
    # M5 + user accept/verify, never a conclusion.
    interventions = _sanitize_interventions(cause.get("interventions"))
    if interventions and root_id in new_node_ids:
        root_node = case.causal_nodes.get(root_id)
        if root_node is not None:
            root_node.metadata = {
                **(root_node.metadata or {}),
                SEEDED_INTERVENTIONS_KEY: interventions,
            }

    letter = cause.get("cause_letter", "?")
    name = cause.get("cause_name", "")
    hypothesis = hm.create_hypothesis(
        statement=hyp_statement,
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

    # Seed the cause's per-rung indicators as evidence-needs motivated by this
    # hypothesis — the chain arrives with its own discriminators (prior, not gate;
    # PENDING; provenance-blind; clears when the hypothesis is retired).
    _emit_rung_needs(case, item_id, cause, hypothesis.hypothesis_id, current_turn)

    return hypothesis.hypothesis_id, new_node_ids, None
