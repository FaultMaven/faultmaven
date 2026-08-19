"""Pure causal-graph mechanics for the Two-Dimensional Hypothesis Methodology.

Operates on a case's causal graph — ``causal_nodes`` (dict ``node_id -> CausalNode``)
and ``causal_edges`` (list of ``CausalEdge``) — with NO I/O and NO LLM. These are
the engine-side validation primitives the ``cause_state`` derivation, the
failed-treatment demotion, and (later) the prompt-driven chain emission all build
on, keeping the methodology's load-bearing invariants unit-testable against
hand-built graphs. Mostly pure/deterministic; the exceptions are the two
authoring paths — ``_attach_engine_refutation`` (M6, mints an Evidence row) and
``synthesize_rcc_from_validated_root`` (§9.3, mints a RootCauseConclusion) — which
use a ``uuid4`` id / wall-clock timestamp, so assert on their shape, not exact
equality.

Spec: docs/architecture/investigation-engine/two-dimensional-hypothesis-methodology.md
  - §0 invariants (M4 empirical/deductive validation, M7 AND-proof)
  - §7.1 / §7.1.1 (empirical vs deductive validation, strict exclusion)

Contents: structural primitives (AND-proof, chain-root validation, deductive
strict-exclusion); LLM-emitted-chain ingestion + orphan-chain resolution;
grounded-root promotion; and M6 counterfactual-disconfirmation demotion. (The
transitional flat->graph bridge was removed in PR B2c — the graph is now
emission-only.) Belief propagation (§6.1 / §9.4) is a follow-on.
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

# The statement/content tokenizer moved to ``cause_assurance`` (the shared
# leaf — the absence-bearing check there needs it and cannot import this
# module); the private aliases keep this module's call sites and the
# calibration tests' import paths stable. ``_stem`` is re-exported solely for
# its stemming-contract unit pins.
from faultmaven.core.investigation.cause_assurance import (
    CAUSAL_STANCE_CONFIDENCE_MIN,
    CONFIRMED_RCC_LIKELIHOOD_FLOOR,
    ENGINE_EVIDENCE_AUTHOR,
    MECHANISTIC_RCC_LIKELIHOOD,
    _stem,  # noqa: F401
    absence_row_link_refused,
    counterfactual_link_decisive,
    register_graph_hooks,
    resolution_confirmation_rows,
    root_counterfactually_confirmed,
)
from faultmaven.core.investigation.cause_assurance import (
    ENGINE_RCC_AUTHOR as _ENGINE_RCC_AUTHOR,
)
from faultmaven.core.investigation.cause_assurance import (
    cached_content_tokens as _cached_content_tokens,
)
from faultmaven.core.investigation.cause_assurance import (
    content_tokens as _content_tokens,
)
from faultmaven.core.investigation.cause_assurance import (
    evidence_category_map as _evidence_category_map,
)
from faultmaven.core.investigation.cause_assurance import (
    problem_anchor_statements as _problem_anchor_statements,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.lifecycle_metrics import (
    causal_and_group_regroup_refused_total,
    causal_and_set_late_grouping_total,
    hypothesis_support_mirrored_to_root_total,
    llm_rcc_cause_linked_total,
    llm_rcc_cause_named_total,
    llm_rcc_retracted_disconfirmed_total,
    m6_demotion_refused_total,
    rcc_precedence_inversion_total,
    root_validation_blocked_restatement_total,
    root_validation_blocked_support_count_total,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisState,
    InvestigationActionType,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    RootCauseConclusion,
    ValidationMethod,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, Hypothesis

# Observability only. This module derives graph truth and is otherwise free of
# I/O, but it already owns the §7.1/§7.1.2 calibration counters, and the #1096
# grouping events below need a per-case witness a counter cannot carry (which
# group, which members, which turn).
logger = logging.getLogger(__name__)

# §7.1.1 guard 3: a sibling counts as "excluded" only when its refutation is
# ABSOLUTE — REFUTED and belief at/under this bar. A merely-inconclusive or
# weakly-refuted sibling does NOT count, so deductive validation cannot fire on
# partial exclusion.
DEDUCTIVE_EXCLUSION_MAX_BELIEF = 0.05

# The M2 confidence-band constants (MECHANISTIC_RCC_LIKELIHOOD /
# CONFIRMED_RCC_LIKELIHOOD_FLOOR) and the engine-mirror author marker live in
# ``cause_assurance`` (grade semantics, contracts-only) and are imported above —
# the terminal confirm-stamp there shares them and cannot import this module.


# ---------------------------------------------------------------------------
# Edge / AND-set helpers
# ---------------------------------------------------------------------------


def incoming_and_groups(
    node_id: str, edges: list[CausalEdge]
) -> dict[str | None, list[str]]:
    """Group the *direct causes* of ``node_id`` by their ``and_group``.

    Returns ``{and_group_key: [cause_node_id, ...]}``. Edges sharing the same
    ``(effect_node_id, and_group)`` are co-necessary (an AND-set, M7). A
    ``None`` key collects the independent (OR-alternative) direct causes — each
    is its own sufficient cause, not part of a conjunction.

    A blank key ("" or whitespace) names no group and normalizes to ``None``.
    ``_add_edge`` normalizes on the way in, but ``and_group`` is an
    unconstrained ``Optional[str]`` at every layer and rows persisted before
    that guard cannot be reached by it — so the normalization lives HERE, where
    every reader is healed at once: the M7 prover would otherwise keep a
    silently-strengthened gate on legacy data, and ``validated_and_conjuncts``
    would publish "the cause required these conditions too" over causes the
    graph holds as independent alternatives.
    """
    groups: dict[str | None, list[str]] = {}
    for e in edges:
        if e.effect_node_id == node_id:
            key = e.and_group
            if isinstance(key, str) and not key.strip():
                key = None
            groups.setdefault(key, []).append(e.cause_node_id)
    return groups


def _state(node_id: str, nodes: dict[str, CausalNode]) -> NodeState | None:
    n = nodes.get(node_id)
    return n.node_state if n else None


def validated_and_conjuncts(case: "Case", chain_node_ids: list[str]) -> list[str]:
    """The VALIDATED causes co-necessary (M7 AND-set) with a chain, as statements.

    An AND-set is the graph's only representation of "the problem needed BOTH of
    these" — edges sharing ``(effect_node_id, and_group)`` are co-necessary. The
    conclusion mirror renders ONE chain (root -> ... -> D): its root becomes the
    cause text and its intermediate rungs the mechanism. A conjunct that is not
    itself on that chain is therefore established by the investigation and absent
    from the conclusion, which is how a genuinely two-factor cause reaches the
    report as a single clause (#1096). This is what the conclusion's
    ``contributing_factors`` carries.

    Only VALIDATED conjuncts are named. An AND-member still standing as a
    candidate is not established, and a conclusion is the one place that must
    never assert more than the graph proves — the same one-directional guarantee
    the rest of the engine lane keeps.

    Chain nodes themselves are excluded (they are already the conclusion's root
    and mechanism). The result is de-duplicated and SORTED: conjuncts of one
    effect are co-equal (an AND-set is unordered by construction) and neither
        repository loads ``causal_edges`` with an ``ORDER BY``, so deriving order
    from row order would make the list — and the equality check the mirror's
    faithfulness short-circuit runs on it — vary with fetch order on PostgreSQL,
    re-minting the conclusion every recompute and flipping the report's bullets
    between regenerations. Sorted, the output is a function of the graph's
    CONTENT rather than of its storage.
    """
    on_chain = set(chain_node_ids)
    statements: set[str] = set()
    for node_id in chain_node_ids:
        for and_group, cause_ids in incoming_and_groups(
            node_id, case.causal_edges
        ).items():
            if and_group is None:
                continue  # OR alternatives: each is its own sufficient cause
            for cause_id in cause_ids:
                if cause_id in on_chain:
                    continue
                node = case.causal_nodes.get(cause_id)
                if node is None or node.node_state != NodeState.VALIDATED:
                    continue
                statements.add(node.statement)
    return sorted(statements)


def conjuncts_for_chain(case: "Case", hyp=None, root=None) -> list[str]:
    """``validated_and_conjuncts`` over the chain a conclusion would mirror.

    The ONE chain-builder, shared by the per-turn mirror here and the terminal
    confirm-stamp in ``cause_assurance`` (which reaches it through the graph
    hook). Both must name the same conjuncts for the same case; implementing
    the rule twice across that module boundary would let the two conclusions a
    single case passes through disagree, with nothing watching.

    The path is the chain when the hypothesis carries one — ``chain_path_to_problem``
    builds it as ``[root, ..., D]``, so an AND-set at any depth including D is
    covered. A path-less hypothesis still has its root (M3 requires it before
    validation), and the fallback adds the PROBLEM node: the canonical
    two-factor shape is both conjuncts pointing straight at D, so reading the
    root's incoming edges alone would report none.
    """
    chain = list(getattr(hyp, "path", None) or []) if hyp is not None else []
    if not chain:
        root_id = getattr(root, "node_id", None) or getattr(hyp, "root_node_id", None)
        problem = next(
            (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
            None,
        )
        chain = [x for x in (root_id, problem.node_id if problem else None) if x]
    return validated_and_conjuncts(case, chain)


# ---------------------------------------------------------------------------
# M7 — AND-gate proof (symmetric: strict to prove, asymmetric to refute)
# ---------------------------------------------------------------------------


def and_constraints_refuted(
    node_id: str, nodes: dict[str, CausalNode], edges: list[CausalEdge]
) -> bool:
    """M7 disproof (asymmetric): refuting ANY one co-necessary member refutes
    the conjunction — the node cannot occur via that AND-path.

    Returns True if any member of any AND-set feeding ``node_id`` is REFUTED.
    OR-alternative causes (``and_group is None``) are independent and do not
    count — one of them being refuted doesn't break the others.
    """
    for and_group, cause_ids in incoming_and_groups(node_id, edges).items():
        if and_group is None:
            continue  # OR alternatives, not a conjunction
        if any(_state(cid, nodes) == NodeState.REFUTED for cid in cause_ids):
            return True
    return False


def and_constraints_satisfied(
    node_id: str, nodes: dict[str, CausalNode], edges: list[CausalEdge]
) -> bool:
    """M7 proof (symmetric, strict): every co-necessary member of every AND-set
    feeding ``node_id`` must be VALIDATED.

    A node with no AND-sets (only OR-alternative parents, or no parents) is
    vacuously satisfied — its own evidence governs it (M4), not a conjunction.
    While any AND-member is still a candidate/inconclusive, this returns False:
    the node cannot be considered conjunctively established.
    """
    for and_group, cause_ids in incoming_and_groups(node_id, edges).items():
        if and_group is None:
            continue
        if not all(_state(cid, nodes) == NodeState.VALIDATED for cid in cause_ids):
            return False
    return True


# ---------------------------------------------------------------------------
# Chain-level validation (what cause_state=IDENTIFIED reads)
# ---------------------------------------------------------------------------


def is_chain_root_validated(
    hypothesis: Hypothesis, nodes: dict[str, CausalNode]
) -> bool:
    """A chain (hypothesis) grounds ``cause_state=IDENTIFIED`` only when its
    ROOT node exists and is VALIDATED (methodology §9.2). The root is the top of
    the ``root -> ... -> D`` path; M3 requires it to be set before validation.
    """
    root_id = hypothesis.root_node_id
    if not root_id:
        return False
    return _state(root_id, nodes) == NodeState.VALIDATED


# ---------------------------------------------------------------------------
# §7.1.1 — deductive validation (proof by exclusion, strict)
# ---------------------------------------------------------------------------
# `exhaustive` (guard #1) is NEVER engine-inferred: F4 family-completeness is an
# LLM-judgment guideline, not an engine sweep, so a pure `(case)` function cannot
# certify the OR-set complete. It is supplied ONLY by an explicit agent assertion
# (``validate_by_exclusion`` is called with the survivors the LLM certified via
# ``deductive_validations``); this predicate still re-checks every guard the engine
# CAN compute (≥2 members, all-but-survivor absolutely refuted), so a mis-asserted
# exhaustiveness cannot fabricate a validation on its own. See #593 / the
# deductive-validation-wiring design doc for the full division of labour.


def deductively_validated(
    survivor_id: str,
    or_set_ids: list[str],
    nodes: dict[str, CausalNode],
    *,
    exhaustive: bool,
) -> bool:
    """Validate ``survivor_id`` by exclusion (§7.1.1): if its OR-set has ``N``
    mutually-exclusive members and the other ``N-1`` are ABSOLUTELY excluded,
    the survivor is validated by deduction — even if it cannot be observed.

    Strict guards (all required):

    - ``exhaustive`` — the OR-set must be certified collectively exhaustive
      (family-completeness sweep passed). Proof-by-exclusion over an incomplete
      differential concludes the wrong survivor; the caller asserts this.
    - the survivor must be a member of ``or_set_ids`` and there must be ≥2
      members (with one survivor you have learned nothing by exclusion).
    - every non-survivor must be ABSOLUTELY excluded: ``REFUTED`` AND
      ``belief <= DEDUCTIVE_EXCLUSION_MAX_BELIEF``. A merely inconclusive or
      weakly-refuted sibling blocks the deduction (the survivor stays a
      candidate — graceful denial).

    This is binary by design — it backs an invariant (M4), not a probabilistic
    estimate. Deductive validation is *mechanistic* grade only (§7.2): it
    unlocks treatment but counterfactual confirmation is still required to
    resolve.
    """
    if not exhaustive:
        return False
    members = list(dict.fromkeys(or_set_ids))  # dedup, preserve order
    if survivor_id not in members or len(members) < 2:
        return False
    for cid in members:
        if cid == survivor_id:
            continue
        node = nodes.get(cid)
        if node is None:
            return False
        if node.node_state != NodeState.REFUTED:
            return False
        if (node.belief or 0.0) > DEDUCTIVE_EXCLUSION_MAX_BELIEF:
            return False
    return True


# ---------------------------------------------------------------------------
# §7.1 — empirical node-state derivation (what feeds is_chain_root_validated)
# ---------------------------------------------------------------------------

# §7.1 validation difficulty (INV-29 / #573): a ROOT validates empirically only on
# at least this many INDEPENDENT causal supports. Every causal link is an LLM
# self-labeled claim (the runbook-provenance arm was decommissioned, #658), so a
# single self-certified datum must not mint a conclusion — one confidently-wrong
# categorization is exactly the #656 turn-6 shape. Non-ROOT rungs keep the ≥1
# bar (they carry no conclusion of their own; the ROOT bar rules the chain).
ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN = 2

# Jaccard at or above which two causal-evidence contents are ONE observation
# for the independence count (mutual restatement — e.g. the same config diff
# recorded twice with different phrasing). Same value as the frame-owner bar
# but deliberately its own knob: they calibrate different comparisons.
_EVIDENCE_MIRROR_JACCARD = 0.6

# Exact maximum-independent-set search bound for the independence count: 2^n
# subsets at n<=15 is ~32k cheap checks; a node with more causal supports than
# that falls back to the conservative greedy bound.
_MIS_EXACT_MAX = 15


def _node_evidence_tally(
    node: CausalNode, evidence_by_id: dict[str, EvidenceCategory | None]
) -> tuple[int, int, list[str], int, int]:
    """``(supports, refutes, causal_support_ev_ids, raw_causal_links,
    counterfactual_refutes)`` for a node from its rung links.

    Counts only links whose backing evidence row actually exists (a dangling
    ``evidence_id`` is ignored, never assumed). ``causal_support_ev_ids`` is the
    ordered, per-evidence-deduped list of evidence ids behind SUPPORTS links
    that are causally grounding — backed by ``CAUSAL_EVIDENCE`` (the §7.1
    "direct observable fact" bar) AND declared at ``stance_confidence >=
    CAUSAL_STANCE_CONFIDENCE_MIN`` (a self-hedged link is not grounding) — so a
    node validates only on real causal grounding. ``raw_causal_links`` counts
    CAUSAL_EVIDENCE-backed SUPPORTS links BEFORE the confidence filter/dedup
    (metrics attribution only: "blocked by the INV-29 bar" vs "never causally
    supported"). ``counterfactual_refutes`` is the subset of REFUTES links
    backed by ``CAUSAL_ABSENCE_EVIDENCE`` AND declared at ``stance_confidence
    >= CAUSAL_STANCE_CONFIDENCE_MIN`` (``counterfactual_link_decisive`` — the
    refute-side twin of the SUPPORTS filter: a self-hedged counterfactual is
    ordinary refuting evidence, not the decisive grade) — a counterfactual
    disconfirmation (the cause was addressed yet ``D`` persisted), the §7.2
    strongest grade, which refutes DECISIVELY (it is not outweighed by
    correlational support). A hedged absence-REFUTES still counts in
    ``refutes``.
    """
    supports = refutes = raw_causal_links = counterfactual_refutes = 0
    causal_support_ev_ids: list[str] = []
    for link in node.evidence_links:
        if link.evidence_id not in evidence_by_id:
            continue  # dangling reference — never counts
        if link.stance == EvidenceStance.SUPPORTS:
            supports += 1
            # Causal grounding is a CAUSAL_EVIDENCE-backed datum (§7.1).
            if evidence_by_id[link.evidence_id] == EvidenceCategory.CAUSAL_EVIDENCE:
                raw_causal_links += 1
                # None (schema default is 1.0, but reloaded rows can carry
                # NULL) reads as unset -> full confidence; an EXPLICIT 0.0 is
                # a declared no-confidence link and must stay filtered.
                confidence = getattr(link, "stance_confidence", None)
                if confidence is None:
                    confidence = 1.0
                if confidence >= CAUSAL_STANCE_CONFIDENCE_MIN and (
                    link.evidence_id not in causal_support_ev_ids
                ):
                    causal_support_ev_ids.append(link.evidence_id)
        elif link.stance == EvidenceStance.REFUTES:
            refutes += 1
            if evidence_by_id[
                link.evidence_id
            ] == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE and counterfactual_link_decisive(
                link
            ):
                counterfactual_refutes += 1
    return (
        supports,
        refutes,
        causal_support_ev_ids,
        raw_causal_links,
        counterfactual_refutes,
    )


def _causal_evidence_tokens(case: Case) -> dict[str, frozenset[str]]:
    """``evidence_id -> content tokens`` for the case's CAUSAL_EVIDENCE rows —
    the raw material of the §7.1 independence count. Content = the LLM-declared
    ``summary`` plus the verbatim ``extract`` slice when present (the extract is
    what actually distinguishes two observations of the same subsystem)."""
    tokens: dict[str, frozenset[str]] = {}
    for e in case.evidence or []:
        if getattr(e, "category", None) != EvidenceCategory.CAUSAL_EVIDENCE:
            continue
        text = " ".join(
            part
            for part in (getattr(e, "summary", None), getattr(e, "extract", None))
            if part
        )
        tokens[e.evidence_id] = _cached_content_tokens(text)
    return tokens


def _independent_causal_support_count(
    ev_ids: list[str], tokens_by_id: dict[str, set[str]]
) -> int:
    """How many mutually INDEPENDENT observations exist among these
    causal-support evidence rows (§7.1 / INV-29): the size of a MAXIMUM
    INDEPENDENT SET of the pairwise mutual-mirror graph
    (``_EVIDENCE_MIRROR_JACCARD``) — the largest selection of rows no two of
    which mirror each other. Re-recording one datum (a mirror pair) still
    counts ONE; two genuinely distinct rows count TWO even when a later
    "bridge" row paraphrases both.

    Maximum-independent-set semantics — NOT connected components and NOT
    greedy leader clustering — for two load-bearing reasons: it is
    order-invariant (link order is not stable across persistence reloads; no
    ORDER BY on the junction table), and it is MONOTONE under added evidence
    (components were not: a new bridge row merging two independent rows into
    one component DEMOTED an already-validated root — adding corroboration
    must never retract a conclusion). Exact bitmask search for realistic
    sizes; beyond the cap, a greedy fallback that only ever under-counts
    (conservative — holds, never falsely validates). Rows too short to
    tokenize are unjudgeable and count ZERO — an unjudgeable row must never
    supply the decisive support (NO-INCORRECT-CONCLUSION)."""
    token_sets = [
        toks for eid in ev_ids if (toks := tokens_by_id.get(eid))  # empty -> 0
    ]
    n = len(token_sets)
    if n <= 1:
        return n
    mirrors = [
        [
            j != i
            and _mutual_mirror(token_sets[i], token_sets[j], _EVIDENCE_MIRROR_JACCARD)
            for j in range(n)
        ]
        for i in range(n)
    ]
    if n <= _MIS_EXACT_MAX:
        best = 1
        for mask in range(1, 1 << n):
            members = [i for i in range(n) if mask >> i & 1]
            if len(members) <= best:
                continue
            if any(mirrors[a][b] for a in members for b in members if a < b):
                continue
            best = len(members)
        return best
    # Greedy fallback (lowest-degree first): a valid independent set, so the
    # count is a LOWER bound — it can only hold a root, never validate one
    # the exact answer would refuse.
    order = sorted(range(n), key=lambda i: sum(mirrors[i]))
    chosen: list[int] = []
    for i in order:
        if not any(mirrors[i][c] for c in chosen):
            chosen.append(i)
    return len(chosen)


# Block reasons for the §7.1 causal-grounding bar (INV-29). ONE predicate
# produces them (``root_support_block_reasons``) and every consumer reads the
# same classification — the derive-time metric label, the count-held set (the
# stamp + anti-anchoring exemption), and the context annotation — so the
# metric can never measure a different population than the interventions act
# on, and each slice gets ITS OWN recovery guidance.
BLOCK_REASON_COUNT = "count"  # fewer qualifying supports than the bar
BLOCK_REASON_MIRROR = "mirror_collapse"  # enough rows, mutual restatements
BLOCK_REASON_HEDGED = "hedged_only"  # causal links exist, all self-hedged

# The reasons that make a root COUNT-HELD: really causally grounded and
# blocked only by the independence arithmetic — the shape one more
# independent observation (or the RESOLVED gone⇒gone confirmation, strictly
# stronger) completes. HEDGED_ONLY is deliberately excluded: zero qualifying
# supports means a confirmation-completes-the-bar read would rest entirely
# on self-hedged claims.
_COUNT_HELD_REASONS = frozenset({BLOCK_REASON_COUNT, BLOCK_REASON_MIRROR})


def root_support_block_reasons(case: Case) -> dict[str, str]:
    """Per-ROOT classification of WHY the §7.1 causal-grounding bar holds an
    otherwise-eligible root (net-supporting, AND-gate satisfied, not
    restating, not refuted, not already validated, ≥1 raw causal link):
    ``count`` / ``mirror_collapse`` / ``hedged_only``. Roots blocked by
    anything else (restatement, refutation, AND-gate, no causal link at all)
    are absent — their recovery is a different story the §7.1 machinery does
    not own."""
    reasons: dict[str, str] = {}
    nodes = case.causal_nodes
    if not nodes:
        return reasons
    # Cheap eligibility pre-scan before the three corpus builds below: the
    # common late-investigation state (every ROOT already settled) must not
    # pay a full tokenization sweep per prompt build for an empty answer.
    if not any(
        n.node_type == NodeType.ROOT
        and n.node_state not in (NodeState.VALIDATED, NodeState.REFUTED)
        for n in nodes.values()
    ):
        return reasons
    evidence_by_id = _evidence_category_map(case)
    tokens = _causal_evidence_tokens(case)
    restating = _restating_root_ids(case)
    for node in nodes.values():
        if node.node_type != NodeType.ROOT or node.node_id in restating:
            continue
        if node.node_state in (NodeState.VALIDATED, NodeState.REFUTED):
            continue
        (
            supports,
            refutes,
            causal_support_ev_ids,
            raw_causal_links,
            counterfactual_refutes,
        ) = _node_evidence_tally(node, evidence_by_id)
        if counterfactual_refutes >= 1 or refutes > supports:
            continue  # refuted territory — nothing to complete
        if raw_causal_links < 1:
            continue  # never causally supported — not this bar's population
        if not (
            supports > refutes
            and and_constraints_satisfied(node.node_id, nodes, case.causal_edges)
        ):
            continue  # blocked by the generic bar, not by §7.1 grounding
        reason = _support_block_reason(causal_support_ev_ids, tokens)
        if reason is not None:
            reasons[node.node_id] = reason
    return reasons


def _support_block_reason(
    causal_support_ev_ids: list[str], tokens: dict[str, frozenset[str]]
) -> str | None:
    """The §7.1 grounding-bar verdict for one node's qualifying supports:
    None when the bar is met, else the block reason. Shared by
    ``root_support_block_reasons`` and the derive-time metric label so the
    two can never classify one root differently."""
    if not causal_support_ev_ids:
        return BLOCK_REASON_HEDGED
    if len(causal_support_ev_ids) < ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN:
        return BLOCK_REASON_COUNT
    if (
        _independent_causal_support_count(causal_support_ev_ids, tokens)
        < ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN
    ):
        return BLOCK_REASON_MIRROR
    return None


def support_count_held_root_ids(case: Case) -> set[str]:
    """ROOT node ids held from VALIDATED **only** by the independence
    arithmetic (reasons ``count``/``mirror_collapse`` — see
    ``root_support_block_reasons``). Read by the resolution confirm-stamp
    (``cause_assurance.confirm_root_from_resolution_absence`` — the user's
    explicit confirmation IS the decisive second observation, so the count
    bar must not veto it) and the anti-anchoring exemption (a true cause
    awaiting its second observation must not be force-retired)."""
    return {
        node_id
        for node_id, reason in root_support_block_reasons(case).items()
        if reason in _COUNT_HELD_REASONS
    }


def root_restates_case_frame(node: "CausalNode", case: Case) -> bool:
    """§7.1 restatement guard predicate (single-node form; ``_restating_root_ids``
    is the batch form — keep their semantics identical): a ROOT whose statement
    carries less than ``ROOT_NOVELTY_MIN_FRACTION`` novel content tokens beyond
    the case frame is a restatement, not an explanation, and no validation lane
    may ADMIT it (the guard is an ENTRY bar on validation/minting, not a
    standing predicate — see ``derive_node_states``).

    The frame = problem anchors + OTHER standing hypotheses' statements. A
    hypothesis is treated as the node's OWN (excluded from the frame) when it is
    attached to the node (``root_node_id`` match) or when it is unattached but
    MUTUALLY mirrors the node (Jaccard ≥ ``_FRAME_OWNER_JACCARD``): during the
    normal attachment lag a chain root's own not-yet-linked hypothesis restates
    it verbatim and must not block it. One-way containment deliberately does NOT
    make an owner: the #656 disjunction root fully CONTAINS each sibling
    hypothesis it OR-s, but shares few tokens mutually — those siblings stay in
    the frame, which is what catches the incident shape.

    ROOT-only by design (rungs adjacent to ``D`` legitimately paraphrase).
    Known limits (§7.1): the check is lexical — synonym paraphrases and
    filler-padded restatements read as novel and pass; the guard is one layer
    of the #656 layered defense.
    """
    if node.node_type != NodeType.ROOT or not node.statement:
        return False
    statement_tokens = _content_tokens(node.statement)
    if not statement_tokens:
        return False
    anchors, hyp_token_sets = _frame_components(case)
    return _node_restates(statement_tokens, node.node_id, anchors, hyp_token_sets)


def _frame_components(case: Case) -> tuple:
    """Tokenize the frame's raw material ONCE: (anchor-token union,
    [(root_node_id, hypothesis-statement tokens), ...])."""
    anchors: set = set()
    for anchor in _problem_anchor_statements(case):
        anchors |= _content_tokens(anchor)
    hyp_token_sets = []
    if getattr(case, "hypotheses", None):
        for h in _standing_hypotheses(case):
            if h.statement:
                tokens = _content_tokens(h.statement)
                if tokens:
                    hyp_token_sets.append((getattr(h, "root_node_id", None), tokens))
    return anchors, hyp_token_sets


def _node_restates(
    statement_tokens: set, node_id: str, anchors: set, hyp_token_sets: list
) -> bool:
    """Novelty core shared by the single-node and batch forms."""
    frame = set(anchors)
    for rid, tokens in hyp_token_sets:
        if rid == node_id:
            continue  # attached own hypothesis
        if rid is None and _mutual_mirror(
            statement_tokens, tokens, _FRAME_OWNER_JACCARD
        ):
            continue  # unattached presumptive owner (attachment lag)
        frame |= tokens
    if not frame:
        return False  # no frame to restate — the guard is inert
    novel = len(statement_tokens - frame) / len(statement_tokens)
    return novel < ROOT_NOVELTY_MIN_FRACTION


def _mutual_mirror(a_tokens: set, b_tokens: set, threshold: float) -> bool:
    """MUTUAL restatement (Jaccard at/above ``threshold``) — both texts are ~the
    same claim. Distinct from one-way containment: a disjunction root contains
    each of its sources but is not mutually theirs. Callers pass their own bar
    (frame ownership vs causal-evidence independence — different comparisons,
    separately calibrated)."""
    if not a_tokens or not b_tokens:
        return False
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens) >= threshold


def _restating_root_ids(case: Case) -> set:
    """Batch form of ``root_restates_case_frame`` for ``derive_node_states``:
    statements, anchors, and hypotheses are immutable within one derive call,
    so tokenize the frame components once and test each ROOT against them."""
    anchors, hyp_token_sets = _frame_components(case)
    if not anchors and not hyp_token_sets:
        return set()
    restating: set = set()
    for node in case.causal_nodes.values():
        if node.node_type != NodeType.ROOT or not node.statement:
            continue
        statement_tokens = _content_tokens(node.statement)
        if not statement_tokens:
            continue
        if _node_restates(statement_tokens, node.node_id, anchors, hyp_token_sets):
            restating.add(node.node_id)
    return restating


def derive_node_states(case: Case) -> bool:
    """Derive every causal node's ``node_state`` from its OWN rung evidence
    (§7.1) plus the M7 AND-gate. A root reaches VALIDATED only from real rung
    evidence — never from a fabricated EMPIRICAL grade. This is what
    makes ``cause_state=IDENTIFIED`` (via ``is_chain_root_validated``, §9.2) a
    derived truth signal: a root reaches VALIDATED only when real causal evidence
    bears it out, never because the flat model already "knew" the answer.

    This is the EMPIRICAL lane only: a node's state follows its OWN rung evidence
    plus the M7 AND-gate used strictly as a VALIDATION gate. Structural
    refutation propagation (refuting an effect because an upstream cause is
    refuted) is deliberately NOT done here — it over-refutes a node that still
    has an intact OR-alternative and would invert precedence over a node's own
    direct observation; that belongs to the §9.4 belief-propagation slice.

    Per non-PROBLEM node (the PROBLEM node ``D`` is the engine-owned anchor and
    is left untouched):

    - **REFUTED** — a counterfactual disconfirmation bears on it (a
      ``CAUSAL_ABSENCE_EVIDENCE`` REFUTES link at ``stance_confidence >=
      CAUSAL_STANCE_CONFIDENCE_MIN`` — §7.2 strongest grade, decisive; a
      self-hedged one is ordinary refuting evidence, the refute-side twin of
      the §7.1 SUPPORTS filter), OR its links net-refute it ``refutes >
      supports`` (strict; a correlational tie is INCONCLUSIVE, not a
      disproof). ``validation_method=NONE``, ``actionable=False``.
    - **VALIDATED** — not refuted, causally grounded, net-supporting
      (``supports > refutes``), every AND-set feeding it is fully VALIDATED
      (M7 proof, strict), AND — for a ROOT — its statement carries novel content
      beyond the case frame (``root_restates_case_frame``, §7.1 restatement
      guard: the symptom dressed as a cause holds at INCONCLUSIVE instead).
      Causal grounding (§7.1 / INV-29): a non-ROOT rung needs ≥1 causally-grounding
      SUPPORTS link (CAUSAL_EVIDENCE-backed, ``stance_confidence`` at/above
      ``CAUSAL_STANCE_CONFIDENCE_MIN``); a ROOT — the node that mints a
      conclusion — needs ``ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN`` INDEPENDENT
      such supports (distinct evidence rows that are not mutual restatements of
      each other), because every causal link is an LLM self-labeled claim and
      one self-certified datum must not conclude a case (#573/#656). A
      counterfactually CONFIRMED root (engine-stamped ``causal_absence``
      SUPPORTS, gone⇒gone — the M2 top grade, engine-only producer) satisfies
      the ROOT bar outright: confirmation dominates empirical counting, and a
      confirmed 1-support root recomputed post-RESOLVED must not demote.
      EMPIRICAL grade; a validated ROOT is marked ``actionable`` (M1).
      Method/actionable/reason are kept mutually consistent so the node
      satisfies its M1/M4/refutation model-validators on reload
      (``CausalNode(**...)``; ``validate_assignment`` is off in memory).
    - **INCONCLUSIVE** — has bearing evidence but neither validates nor refutes
      (including a support/refute tie).
    - **CANDIDATE** — no bearing evidence yet (the lazy default; a freshly
      emitted, untested rung).

    A node already VALIDATED by DEDUCTION (§7.1.1, proof-by-exclusion) carries no
    supporting evidence of its own by design, so the evidence-local lane LEAVES
    it intact — it is only overturned here by DIRECT refuting evidence
    (``refutes > supports``), never silently demoted to CANDIDATE.

    Iterates to a fixpoint (bounded by node count) so a cause validated this pass
    can satisfy its effect's AND-gate within the same recompute. On a DAG the
    longest dependency path is ``len(nodes)-1`` edges, so ``len(nodes)+1`` passes
    settle it; a malformed cyclic graph simply stops at the bound (no hang).
    Returns True if any node's state changed.
    """
    nodes = case.causal_nodes
    edges = case.causal_edges
    evidence_by_id: dict[str, EvidenceCategory | None] = _evidence_category_map(case)
    restating_roots = _restating_root_ids(case)
    causal_tokens = _causal_evidence_tokens(case)

    changed_any = False
    # Fixpoint: a validated parent can unlock a child's AND-gate. Bound the loop
    # by node count + 1 (a strictly longer dependency chain cannot exist).
    for _ in range(len(nodes) + 1):
        changed_this_pass = False
        for node in nodes.values():
            if node.node_type == NodeType.PROBLEM:
                continue
            (
                supports,
                refutes,
                causal_support_ev_ids,
                raw_causal_links,
                counterfactual_refutes,
            ) = _node_evidence_tally(node, evidence_by_id)
            deductively_valid = (
                node.node_state == NodeState.VALIDATED
                and node.validation_method == ValidationMethod.DEDUCTIVE
            )
            # §7.1 causal-grounding bar (INV-29/#573): a ROOT needs
            # ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN independent causal supports
            # — or a counterfactual confirmation, which dominates counting —
            # while a non-ROOT rung keeps ≥1. The len() pre-check short-circuits
            # the pairwise independence work when the raw count can't reach the
            # bar anyway (independent count ≤ deduped link count).
            if node.node_type == NodeType.ROOT:
                causally_grounded = (
                    len(causal_support_ev_ids) >= ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN
                    and _independent_causal_support_count(
                        causal_support_ev_ids, causal_tokens
                    )
                    >= ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN
                ) or root_counterfactually_confirmed(node, evidence_by_id)
            else:
                causally_grounded = len(causal_support_ev_ids) >= 1
            # Everything the empirical bar requires EXCEPT the causal-grounding
            # bar and the restatement guard; computed separately so each block
            # branch below is provably "would have validated but for THIS bar"
            # (AND-gate blocks stay attributed to the AND-gate).
            generic_ok = supports > refutes and and_constraints_satisfied(
                node.node_id, nodes, edges
            )
            would_validate = causally_grounded and generic_ok
            # A counterfactual disconfirmation (§7.2/§7.3) refutes decisively; a
            # correlational tie/majority is the lesser ``refutes > supports`` bar.
            if counterfactual_refutes >= 1 or refutes > supports:
                target_state = NodeState.REFUTED
            elif deductively_valid:
                continue  # owned by the deductive lane — never demote here
            elif would_validate and (
                node.node_id not in restating_roots
                or node.node_state == NodeState.VALIDATED
            ):
                # The restatement guard is an ENTRY bar: it blocks the
                # transition INTO VALIDATED, but a root that already validated
                # is ruled by its EVIDENCE alone — otherwise a later sibling
                # emission whose wording overlaps would retract a correct,
                # evidence-backed conclusion (non-monotonic flap), and the
                # deductive lane (never re-derived here) would split from the
                # mint path. Pre-guard persisted conclusions are therefore
                # grandfathered — deliberately: closed cases never recompute
                # anyway, and their confidence is the grade-cap work on #656.
                target_state = NodeState.VALIDATED
            elif would_validate:
                # §7.1 restatement guard: supported and gate-satisfied, but the
                # "cause" restates the case frame — hold at INCONCLUSIVE (a
                # live candidate needing a real mechanism, never a validated
                # conclusion). The LLM refines the statement; the engine never
                # validates the symptom as its own cause.
                target_state = NodeState.INCONCLUSIVE
            elif supports or refutes:
                target_state = NodeState.INCONCLUSIVE
            else:
                target_state = NodeState.CANDIDATE

            # §7.1.1 guard #3 (engine-computable half): a COUNTERFACTUAL
            # (absence-based, §7.2 strongest) refutation is an ABSOLUTE exclusion —
            # drive ``belief`` to 0 so proof-by-exclusion (``deductively_validated``)
            # may count this sibling as excluded. A merely-correlational net-refute
            # keeps its belief above ``DEDUCTIVE_EXCLUSION_MAX_BELIEF``, so it BLOCKS
            # the deduction (graceful denial — noise-sensitive exclusion never fires
            # on a weakly-refuted sibling). Set independent of the state-change
            # short-circuit below: a node already correlationally REFUTED may become
            # counterfactually refuted on a later turn, and must then drop to 0. The
            # exhaustiveness half of the guards (guard #1) stays agent-asserted.
            if counterfactual_refutes >= 1 and node.belief != 0.0:
                node.belief = 0.0

            if target_state == node.node_state:
                continue

            # Calibration counter: ONE increment per BLOCK EVENT — the state
            # transition where a root that would otherwise have validated is
            # held by the restatement guard — never per fixpoint pass or per
            # re-derive of an already-held node. (A root already INCONCLUSIVE
            # from generic evidence whose AND-gate later unlocks is a slight
            # undercount; preferred over the unbounded overcount of counting
            # evaluations.)
            if (
                target_state == NodeState.INCONCLUSIVE
                and would_validate
                and node.node_id in restating_roots
            ):
                root_validation_blocked_restatement_total.inc()

            # INV-29 calibration counter, same block-event semantics: the
            # state transition where a ROOT with real causal-category support
            # that clears the generic bar is held ONLY by the grounding bar
            # (raw_causal_links >= 1 separates "blocked by this bar" from
            # "never causally supported at all"). Labeled with the SAME
            # block-reason classification the count-held set and the context
            # annotation read (_support_block_reason), so the metric measures
            # exactly the populations the interventions act on. Fires
            # regardless of the restatement verdict — a root failing BOTH
            # bars is attributed here (the restatement counter requires
            # would_validate, so the two never double-count one event).
            if (
                target_state == NodeState.INCONCLUSIVE
                and generic_ok
                and not causally_grounded
                and raw_causal_links >= 1
                and node.node_type == NodeType.ROOT
            ):
                reason = _support_block_reason(causal_support_ev_ids, causal_tokens)
                root_validation_blocked_support_count_total.labels(
                    reason=reason or BLOCK_REASON_COUNT
                ).inc()

            # Keep the FINAL field combination invariant-valid (the model
            # validators run on reload via CausalNode(**...)): M4 validated ⇒
            # method != NONE; M1 validated ROOT ⇒ actionable; REFUTED ⇔
            # refutation_reason present (and a reason is illegal on any other
            # state, so it is cleared when leaving REFUTED).
            if target_state == NodeState.VALIDATED:
                node.validation_method = ValidationMethod.EMPIRICAL
                node.refutation_reason = None
                if node.node_type == NodeType.ROOT:
                    node.actionable = True
            elif target_state == NodeState.REFUTED:
                node.validation_method = ValidationMethod.NONE
                node.actionable = False
                if not node.refutation_reason:
                    node.refutation_reason = (
                        "refuted by rung evidence / a refuted AND-member (M7)"
                    )
            else:  # CANDIDATE / INCONCLUSIVE
                node.validation_method = ValidationMethod.NONE
                node.refutation_reason = None
            node.node_state = target_state
            changed_this_pass = changed_any = True
        if not changed_this_pass:
            break
    return changed_any


# ---------------------------------------------------------------------------
# §7.1.1 — deductive-validation stamping (the caller of ``deductively_validated``)
# ---------------------------------------------------------------------------


def _survivor_or_sets(survivor_id: str, edges: list[CausalEdge]) -> list[list[str]]:
    """The OR-differential(s) ``survivor_id`` competes in.

    For each effect the survivor causes *as an OR-alternative* (its edge to that
    effect carries no ``and_group`` — an AND-member is not competing by exclusion),
    return that effect's set of OR-alternative direct causes (``incoming_and_groups``
    ``None`` group), which includes the survivor. A survivor may point at several
    effects (S2 convergence); each is a separate differential to try exclusion over.
    """
    or_sets: list[list[str]] = []
    effects = {
        e.effect_node_id
        for e in edges
        if e.cause_node_id == survivor_id and e.and_group is None
    }
    for eff in effects:
        or_group = incoming_and_groups(eff, edges).get(None, [])
        if survivor_id in or_group:
            or_sets.append(or_group)
    return or_sets


def validate_by_exclusion(case: Case, asserted_survivor_ids: set[str]) -> bool:
    """Stamp ``DEDUCTIVE`` on each asserted survivor whose OR-differential has
    collapsed to it (§7.1.1, proof by exclusion). Returns True if any node changed.

    ``asserted_survivor_ids`` carries the agent's exhaustiveness certification — the
    one guard (§7.1.1 #1) the engine cannot compute (F4 family-completeness is
    LLM-judgment, not an engine sweep). A survivor the LLM never asserted never
    enters this lane. For each asserted survivor the engine STILL independently
    requires (via ``deductively_validated``, ``exhaustive=True``) that ≥2
    alternatives existed and ALL but the survivor are ABSOLUTELY refuted (REFUTED +
    ``belief <= DEDUCTIVE_EXCLUSION_MAX_BELIEF`` — the counterfactual bar set in
    ``derive_node_states``), so a mis-asserted exhaustiveness cannot fabricate a
    validation on its own — the differential must genuinely have collapsed.

    Runs AFTER ``derive_node_states`` (siblings must have reached REFUTED first). A
    survivor already VALIDATED (empirically or deductively) or REFUTED is left alone
    — an assertion neither re-validates nor resurrects. Deductive validation is
    mechanistic grade only (§7.2): it validates the ROOT (unlocking treatment) but
    the case still needs a counterfactual confirmation to resolve, and harvest is
    RESOLVED-only — so the exhaustiveness assertion is backstopped downstream.

    The §7.1 restatement guard applies here too (graceful denial): a surviving
    "cause" whose statement restates the problem has excluded its alternatives
    without ever stating a mechanism — stamping it would conclude "the problem
    causes itself". The survivor stays un-stamped and the investigation stays
    open until the LLM states what the surviving cause actually IS.
    """
    if not asserted_survivor_ids:
        return False
    nodes = case.causal_nodes
    edges = case.causal_edges
    restating_roots = _restating_root_ids(case)
    changed = False
    for sid in asserted_survivor_ids:
        node = nodes.get(sid)
        if node is None or node.node_type != NodeType.ROOT:
            continue  # only a ROOT cause is validated by exclusion
        if node.node_state in (NodeState.VALIDATED, NodeState.REFUTED):
            continue  # already settled — assertion does not re-open it
        if sid in restating_roots:
            continue  # §7.1 restatement guard — no lane validates a restatement
        for or_set in _survivor_or_sets(sid, edges):
            if deductively_validated(sid, or_set, nodes, exhaustive=True):
                node.node_state = NodeState.VALIDATED
                node.validation_method = ValidationMethod.DEDUCTIVE
                node.actionable = True  # M1: a VALIDATED ROOT is actionable
                node.refutation_reason = None
                changed = True
                break
    return changed


# ---------------------------------------------------------------------------
# Graph anchoring + path walking (the PROBLEM node D + root->D paths)
# ---------------------------------------------------------------------------


def seed_problem_node(case: Case) -> CausalNode | None:
    """Return the case's single PROBLEM node ``D``, creating it from the
    confirmed problem statement when absent.

    ``D`` is engine-owned (deterministic), not LLM-emitted: it anchors every
    chain. Returns None when there is no problem statement to anchor on yet.
    Idempotent — at most one PROBLEM node per case.
    """
    problem_node = next(
        (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
        None,
    )
    if problem_node is not None:
        return problem_node
    pv = case.problem_verification
    statement = pv.symptom_statement if pv else None
    if not statement or not statement.strip():
        return None
    problem_node = CausalNode(
        statement=statement[:500],
        node_type=NodeType.PROBLEM,
        generated_at_turn=case.current_turn,
    )
    case.causal_nodes[problem_node.node_id] = problem_node
    return problem_node


def chain_path_to_problem(root_id: str, case: Case) -> list[str]:
    """Walk cause→effect edges from ``root_id`` down to the PROBLEM node ``D``,
    returning the ordered path ``[root_id, ..., d_id]`` (methodology: a
    ``Hypothesis`` is a root→D path).

    Breadth-first search for the shortest ``root → D`` route. A node may have
    several downstream edges (convergence, S2) and some branches dead-end; a
    greedy single-arrow walk would wrongly report an open chain when it picked a
    dead branch first, so the search explores all branches. Returns ``[]`` if no
    path reaches ``D`` (the chain is still open, or ``root_id`` *is* ``D`` — a
    root cause cannot be the symptom itself) — the caller then leaves
    ``root_node_id``/``path`` unset.
    """
    problem = next(
        (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
        None,
    )
    if problem is None or root_id not in case.causal_nodes:
        return []
    d_id = problem.node_id
    if root_id == d_id:
        return []  # the symptom is not its own root cause
    # Adjacency: cause -> [effects].
    out: dict[str, list[str]] = {}
    for e in case.causal_edges:
        out.setdefault(e.cause_node_id, []).append(e.effect_node_id)
    parent: dict[str, str | None] = {root_id: None}
    queue: deque[str] = deque([root_id])
    while queue:
        cur = queue.popleft()
        if cur == d_id:
            path: list[str] = []
            node: str | None = d_id
            while node is not None:
                path.append(node)
                node = parent[node]
            return list(reversed(path))
        for nxt in out.get(cur, []):
            if nxt not in parent:
                parent[nxt] = cur
                queue.append(nxt)
    return []  # open chain — no route to D


def _normalize_statement(s: str | None) -> str:
    """Canonical key for exact-match node identity (engine ingest dedup):
    whitespace-collapsed, lowercased, capped at the same 500 chars
    ``CausalNode`` stores — so both operands of a comparison normalize
    identically (no asymmetric truncation)."""
    return " ".join((s or "")[:500].split()).lower()


def find_canonical_node_id(
    case: Case, node_type: NodeType, statement: str | None
) -> str | None:
    """The id of the existing node an emitted ``(node_type, statement)`` spec
    would REUSE under ``ingest_emitted_chain``'s exact-match dedup, or ``None`` if
    it would mint a fresh node.

    Single source of the dedup key so external callers never drift from ingest's
    own identity reconciliation. The KB cause seeder uses it to detect — *before*
    ingesting — that a runbook's root would collapse onto an already-seeded root,
    so it can skip that cause without first minting orphan intermediate rungs.
    """
    key = (node_type, _normalize_statement(statement))
    for nid, n in case.causal_nodes.items():
        if (n.node_type, _normalize_statement(n.statement)) == key:
            return nid
    return None


def ingest_emitted_chain(
    case: Case,
    nodes_to_add: list,
    edges_to_add: list,
    node_evidence: list,
    current_turn: int,
    evidence_created_ids: list | None = None,
) -> list[str | None]:
    """Build the causal graph from a turn's LLM-emitted chain fragments (lazy
    backward expansion, methodology §5/S3). Pure: no I/O, no LLM.

    The sole source of the causal graph (the transitional bridge was removed in
    PR B2c). Each item is a duck-typed schema object:

    - ``nodes_to_add`` — ``statement``, ``node_type``, optional ``produces``
      (the node it directly causes: an existing id, ``'D'``, or ``'new_index_N'``
      into this same list) and ``and_group``.
    - ``edges_to_add`` — explicit ``cause``/``effect`` refs (+ ``and_group``,
      ``reasoning``) for convergence (S2) beyond a node's own ``produces``.
    - ``node_evidence`` — ``node_ref``, ``evidence_id``/``evidence_id_ref``,
      ``stance``, ``reasoning``, ``stance_confidence``. The evidence ref may be a
      real ``ev_...`` id or ``'new_index_N'`` referencing evidence created this
      turn (resolved via ``evidence_created_ids``).

    ``evidence_created_ids`` are the evidence ids added earlier this turn (the
    caller's ``metadata['evidence_added']``), against which ``new_index_N``
    evidence refs resolve — without it, same-turn rung evidence is dropped.

    Returns the created node ids in emission order (``None`` for any skipped
    node, so ``new_index_N`` indices stay aligned), so the caller can resolve
    ``new_index_N`` references (e.g. linking a hypothesis to its root node)
    against them. Best-effort and
    never raises: unresolvable refs, unknown evidence, and malformed nodes
    (empty statement, or a type other than root/intermediate — ``D`` is
    engine-seeded, never emitted) are skipped; ``D`` is seeded if a problem
    statement exists, otherwise ingestion is a no-op.
    """
    evidence_created_ids = evidence_created_ids or []
    problem = seed_problem_node(case)
    if problem is None:
        return []
    d_id = problem.node_id

    # Pass 1: create the nodes; record ids in order for new_index_N resolution.
    # A skipped node holds None so later indices still line up.
    #
    # Identity reconciliation (engine-derive lane): if an emitted statement EXACTLY
    # restates an existing same-type node, REUSE that node instead of minting a
    # duplicate — so its id flows into edges, evidence, and the hypothesis root_ref,
    # keeping one cause on one node. The LLM re-asserts a standing cause on later
    # turns; prevention is the chain rendered in <causal_graph> (which it is told to
    # reference), and this is the safe backstop for a verbatim re-emit it does
    # anyway. Exact normalized match ONLY: a fuzzy threshold cannot separate a true
    # duplicate from a distinct OR-sibling differing in one parameter (the
    # over-merge trap), so paraphrases are deliberately NOT merged here. Reuse
    # covers intra-turn repeats too (earlier nodes are already in causal_nodes).
    # Index existing nodes by (type, normalized statement) ONCE — so the per-spec
    # dedup is an O(1) dict lookup, not a full re-scan + re-normalization of every
    # node on every spec (this path runs each DIAGNOSIS turn and the graph grows
    # over a case). Newly-minted nodes are added to the index so intra-turn
    # verbatim repeats also dedup; first id wins (the canonical node). Skip the
    # build entirely on the common no-emission turn — nothing to dedup against.
    canonical_by_key: dict[tuple, str] = {}
    if nodes_to_add:
        for nid, n in case.causal_nodes.items():
            canonical_by_key.setdefault(
                (n.node_type, _normalize_statement(n.statement)), nid
            )

    created: list[str | None] = []
    for spec in nodes_to_add:
        statement = (getattr(spec, "statement", None) or "").strip()
        node_type = getattr(spec, "node_type", None)
        if not statement or node_type not in (
            NodeType.ROOT,
            NodeType.INTERMEDIATE,
        ):
            # Empty statement (CausalNode rejects it) or a non-{root,intermediate}
            # type (a second PROBLEM node would violate the one-D-per-case index).
            created.append(None)
            continue
        # Identity reconciliation (engine-derive lane): an emitted statement that
        # EXACTLY restates an existing same-type node REUSES it rather than minting
        # a duplicate — so its id flows into edges, evidence, and the hypothesis
        # root_ref, keeping one cause on one node. The LLM re-asserts a standing
        # cause on later turns; prevention is the chain rendered in <causal_graph>
        # (which it is told to reference), and this is the safe backstop for a
        # verbatim re-emit. Exact normalized match ONLY: a fuzzy threshold cannot
        # separate a true duplicate from a distinct OR-sibling differing in one
        # parameter (the over-merge trap), so paraphrases are NOT merged here.
        key = (node_type, _normalize_statement(statement))
        canonical = canonical_by_key.get(key)
        if canonical is not None:
            created.append(canonical)  # reuse the canonical node, no duplicate
            continue
        node = CausalNode(
            statement=statement[:500],
            node_type=node_type,
            generated_at_turn=current_turn,
        )
        case.causal_nodes[node.node_id] = node
        canonical_by_key[key] = node.node_id
        created.append(node.node_id)

    def _resolve(ref: str | None) -> str | None:
        if not ref:
            return None
        if ref == "D":
            return d_id
        if ref.startswith("new_index_"):
            try:
                idx = int(ref[len("new_index_") :])
            except ValueError:
                return None
            return created[idx] if 0 <= idx < len(created) else None
        return ref if ref in case.causal_nodes else None

    def _add_edge(cause_id, effect_id, and_group, reasoning):
        if not cause_id or not effect_id or cause_id == effect_id:
            return
        # An AND-set key is a GROUPING token — see _normalize_and_group for what
        # is folded away and why.
        and_group = _normalize_and_group(and_group)
        if cause_id not in case.causal_nodes or effect_id not in case.causal_nodes:
            return
        existing = next(
            (
                e
                for e in case.causal_edges
                if e.cause_node_id == cause_id and e.effect_node_id == effect_id
            ),
            None,
        )
        if existing is not None:
            # Idempotent on the EDGE — but an existing edge may still GAIN a
            # group. Co-necessity is usually recognized after the fact: the
            # model proposes A and B as independent candidates, and only later
            # sees that the problem needed both. Expressing that means
            # re-emitting the edges with a shared and_group, and a flat
            # "already exists" drop left the AND-set half-formed (one member
            # carrying the key), so no conjunction ever existed to render —
            # the #1096 factor loss, through a second door. Monotone by design:
            # an edge may go None -> group, never group -> other group (a
            # silent regrouping of a standing conjunction) and never
            # group -> None (a later ungrouped restatement is not a
            # retraction; treating it as one would make the published
            # conjunction flicker turn to turn).
            if existing.and_group is None and and_group is not None:
                existing.and_group = and_group
                _observe_late_grouping(case, effect_id, and_group)
            elif and_group != existing.and_group and existing.and_group is not None:
                # Refused, and the model gets no witness of it from here
                # (ingest is pure and has no system_feedback channel), so the
                # refusal is recorded where an operator can see it — the model
                # is now reasoning over a grouping the graph does not have.
                attempt = "ungroup" if and_group is None else "regroup"
                causal_and_group_regroup_refused_total.labels(attempt=attempt).inc()
                logger.info(
                    f"Refused an and_group {attempt} on edge "
                    f"{cause_id}->{effect_id} for case {case.case_id}: "
                    f"{existing.and_group!r} stands (the merge is monotone)",
                    extra={
                        "event": "causal_and_group_regroup_refused",
                        "case_id": case.case_id,
                        "turn": current_turn,
                        "attempt": attempt,
                        "standing_group": existing.and_group,
                        "emitted_group": and_group,
                    },
                )
            return
        case.causal_edges.append(
            CausalEdge(
                cause_node_id=cause_id,
                effect_node_id=effect_id,
                and_group=and_group,
                reasoning=reasoning,
                created_at_turn=current_turn,
            )
        )

    # Pass 2: edges from each node's `produces`, then explicit edges.
    for i, spec in enumerate(nodes_to_add):
        produces = getattr(spec, "produces", None)
        if produces:
            _add_edge(
                created[i], _resolve(produces), getattr(spec, "and_group", None), None
            )
    for e in edges_to_add:
        _add_edge(
            _resolve(getattr(e, "cause", None)),
            _resolve(getattr(e, "effect", None)),
            getattr(e, "and_group", None),
            getattr(e, "reasoning", None),
        )

    # Node-targeted evidence (rung-level stance).
    existing_ev = {ev.evidence_id for ev in case.evidence}
    cat_by_id = _evidence_category_map(case)

    def _resolve_ev(ref: str | None) -> str | None:
        # Evidence ref: a real ev_ id, or 'new_index_N' into this turn's evidence.
        if not ref:
            return None
        if ref.startswith("new_index_"):
            try:
                idx = int(ref[len("new_index_") :])
            except ValueError:
                return None
            return (
                evidence_created_ids[idx]
                if 0 <= idx < len(evidence_created_ids)
                else None
            )
        return ref

    for link in node_evidence:
        nid = _resolve(getattr(link, "node_ref", None))
        node = case.causal_nodes.get(nid) if nid else None
        ev_id = _resolve_ev(
            getattr(link, "evidence_id", None) or getattr(link, "evidence_id_ref", None)
        )
        stance = getattr(link, "stance", None)
        if node is None or ev_id not in existing_ev or stance is None:
            continue
        # M2 trust boundary (#987), category-gated: NO model-authored stance on
        # a causal_absence row is accepted — absence rows are stand-alone audit
        # records and every counterfactual link on one is engine-minted. The
        # rule, its rationale, and its metering live in ONE place
        # (``cause_assurance.absence_row_link_refused``), shared verbatim with
        # the flat hypothesis axis in
        # ``milestone_engine._apply_hypothesis_evidence_links`` — enforcing it
        # here alone would leave a boundary a single stance choice routes
        # around (the #987 second finding).
        if absence_row_link_refused(
            cat_by_id.get(ev_id),
            stance,
            axis="node",
            evidence_id=ev_id,
            node_or_hypothesis_id=nid,
            case_id=getattr(case, "case_id", None),
            turn=current_turn,
        ):
            continue
        # Upsert by (node, evidence) — matching the junction table's ON
        # CONFLICT DO UPDATE. A re-emission is a genuine re-assessment (a
        # raised ``stance_confidence`` after corroboration, a stance flip);
        # first-write-wins would freeze a link's confidence forever, which is
        # load-bearing now that the §7.1 filter reads it — the model's only
        # escape would be minting a duplicate evidence row that the
        # independence mirror then collapses.
        #
        # Engine verdicts on absence rows are safe from LLM overwrite by
        # CONSTRUCTION now, not by a clause here: the category gate above
        # refuses every model-authored link on a causal_absence row before
        # this point, so neither a create nor an in-place overwrite of the
        # confirm-stamp's SUPPORTS or M6's REFUTES can be reached from an
        # emission.
        #
        # An OMITTED stance_confidence (schema default None) means "full
        # confidence" on a NEW link but "keep the existing value" on an
        # upsert — otherwise a routine graph re-listing that omits the field
        # would silently PROMOTE a previously deliberate hedge (e.g. 0.5) to
        # 1.0 and pull it into the §7.1 causal tally.
        emitted_confidence = getattr(link, "stance_confidence", None)
        existing_idx = next(
            (i for i, el in enumerate(node.evidence_links) if el.evidence_id == ev_id),
            None,
        )
        if emitted_confidence is None:
            if existing_idx is not None:
                emitted_confidence = node.evidence_links[existing_idx].stance_confidence
            else:
                emitted_confidence = 1.0
        fresh = NodeEvidenceLink(
            evidence_id=ev_id,
            stance=stance,
            reasoning=getattr(link, "reasoning", None) or "node evidence",
            stance_confidence=emitted_confidence,
        )
        if existing_idx is None:
            node.evidence_links.append(fresh)
        else:
            node.evidence_links[existing_idx] = fresh

    return created


def mirror_hypothesis_support_to_root_nodes(case: Case, current_turn: int) -> int:
    """#695 B1 — surface a hypothesis's flat causal SUPPORTS links onto its chain
    ROOT node, so the node axis (the sole source of truth for
    ``derive_node_states`` / ``grade_cause_assurance`` / the runbook gate) sees
    the grounding the LLM recorded on the hypothesis. Returns the number of node
    links created. Pure: no I/O, no LLM.

    The flat ``hypothesis_evidence`` axis and the ``causal_node_evidence`` axis
    are disjoint channels — a SUPPORTS link on a hypothesis is not mirrored onto
    that hypothesis's root node — so a hypothesis grounded purely on the flat
    axis leaves its root node with ZERO causal support and can never reach the
    ROOT validation bar (``ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN`` independent
    causal supports). This closes that gap.

    Trust boundary (identical to ``ingest_emitted_chain``): mirror ONLY
      * links whose evidence row is ``CAUSAL_EVIDENCE`` — the only category the
        ROOT bar counts. Symptom rows bear on ``D``, not the cause; and
        ``CAUSAL_ABSENCE`` rows are deliberately excluded, so this can never
        create the absence-SUPPORTS link that would satisfy the engine-reserved
        counterfactual CONFIRMATION mint;
      * ``SUPPORTS`` stance (grounding, not refutation);
      * onto a real ROOT node the hypothesis is attached to.
    It NEVER overwrites an existing node link — the LLM's explicit
    ``node_evidence`` emission (or a prior mirror) wins; it only FILLS a missing
    one, so it is idempotent across turns and repairs historical gaps on replay.
    The mirrored link is a CANDIDATE only: ``derive_node_states`` still applies
    the independence / restatement guard / M7 AND-gate, so mirroring cannot
    manufacture a validation the evidence does not independently support.
    """
    if not case.hypotheses or not case.causal_nodes:
        return 0
    cat_by_id = _evidence_category_map(case)
    mirrored = 0
    for hyp in case.hypotheses.values():
        root_id = getattr(hyp, "root_node_id", None)
        if not root_id:
            continue
        node = case.causal_nodes.get(root_id)
        if node is None or node.node_type != NodeType.ROOT:
            continue
        existing_ev_ids = {el.evidence_id for el in node.evidence_links}
        for link in hyp.evidence_links:
            if link.stance != EvidenceStance.SUPPORTS:
                continue
            ev_id = link.evidence_id
            if cat_by_id.get(ev_id) != EvidenceCategory.CAUSAL_EVIDENCE:
                continue
            if ev_id in existing_ev_ids:
                continue  # explicit node emission (or a prior mirror) wins
            node.evidence_links.append(
                NodeEvidenceLink(
                    evidence_id=ev_id,
                    stance=EvidenceStance.SUPPORTS,
                    reasoning=link.reasoning or "mirrored from hypothesis support",
                    stance_confidence=link.stance_confidence,
                    linked_at_turn=current_turn,
                )
            )
            existing_ev_ids.add(ev_id)
            mirrored += 1
    if mirrored:
        hypothesis_support_mirrored_to_root_total.inc(mirrored)
    return mirrored


def _net_refuted(hyp: Hypothesis) -> bool:
    """Decisive-disconfirmation test for M6: refuting evidence at least matches
    supporting evidence (and there is at least one refuting link).

    A *lone* refuting link among a body of supporting evidence is NOT decisive —
    treating it as such tears down a legitimately grounded cause that merely
    attracted one contrary data point during search (a real false-demotion risk).
    A refutation that flips or ties the support balance is. With no links at all
    this is False (absence of evidence is not disconfirmation).
    """
    refuting = sum(
        1 for link in hyp.evidence_links if link.stance == EvidenceStance.REFUTES
    )
    if refuting == 0:
        return False
    supporting = sum(
        1 for link in hyp.evidence_links if link.stance == EvidenceStance.SUPPORTS
    )
    return refuting >= supporting


def _representative_cause_hypothesis(case: Case) -> Hypothesis | None:
    """The hypothesis that best represents the currently-grounded cause.

    When the conclusion names one (``validated_hypothesis_id``) that is
    authoritative. Otherwise the cause was grounded case-wide (high likelihood +
    causal evidence / ``evidence_basis``) WITHOUT naming a hypothesis — the
    common grounding shape, and the one in case_e970a5c24fe1 (``likelihood`` 1.0,
    ``validated_hypothesis_id`` null). The engine's best proxy is then the
    strongest hypothesis by ORIGINAL confidence: ``initial_likelihood`` is stable
    where ``likelihood`` is not — refutation zeroes ``likelihood``, so a
    just-refuted believed-cause would vanish from a max-by-``likelihood`` pick.
    """
    rcc = case.root_cause_conclusion
    vhid = getattr(rcc, "validated_hypothesis_id", None) if rcc else None
    if vhid:
        return case.hypotheses.get(vhid)
    if not case.hypotheses:
        return None
    return max(case.hypotheses.values(), key=lambda h: h.initial_likelihood)


def _hypothesis_disconfirmed(hyp: Hypothesis) -> bool:
    """Hypothesis-side disconfirmation: REFUTED, or net-refuted (refuting
    evidence at least matches support, ``_net_refuted``). The single definition
    shared by the M6 trigger and the RCC retraction below so the two cannot
    drift; the M6 trigger layers a node-side counterfactual clause on top."""
    return hyp.state == HypothesisState.REFUTED or _net_refuted(hyp)


def retract_disconfirmed_rcc(case: Case) -> bool:
    """Clear a ``RootCauseConclusion`` whose NAMED cause has been disconfirmed, at
    the SOURCE — so a disproven cause is asserted by NO consumer (terminal gate,
    report, copilot UI, KB-runbook conversion), not just one guarded reader.
    Returns True if it retracted one.

    Link-based ONLY — it acts solely on the RCC's explicit ``validated_hypothesis_id``
    cause link. It deliberately does NOT infer the cause from a likelihood proxy:
    ``_representative_cause_hypothesis``'s ``max(initial_likelihood)`` fallback would
    refuse a valid RCC whenever an unrelated early-refuted alternative dominated
    (a NO-COLLAPSE stall), since ``initial_likelihood`` never decays. This
    complements the M6 retraction (``demote_disconfirmed_cause_via_evidence``,
    gated on ``cause_state=IDENTIFIED``) by covering the gap where an RCC's
    hypothesis is refuted but no chain root ever validated — so cause_state never
    reached IDENTIFIED and M6's gate never fired. A free-text RCC with no
    ``validated_hypothesis_id`` (no reliable cause link) is left untouched — a
    documented residual, not a guess.
    """
    rcc = case.root_cause_conclusion
    vhid = getattr(rcc, "validated_hypothesis_id", None) if rcc else None
    if not vhid:
        return False
    hyp = case.hypotheses.get(vhid)
    if hyp is not None and _hypothesis_disconfirmed(hyp):
        # §7.6 / INV-34: an LLM conclusion linked to its cause (link_llm_rcc_to_cause)
        # is retracted here just like an engine one when that cause is disconfirmed.
        if getattr(rcc, "determined_by", None) != _ENGINE_RCC_AUTHOR:
            llm_rcc_retracted_disconfirmed_total.inc()
        case.root_cause_conclusion = None
        return True
    return False


def link_llm_rcc_to_cause(case: Case) -> bool:
    """§7.6 / INV-34 + §7.7 / INV-35 — attribute an LLM-authored
    ``RootCauseConclusion`` to the STANDING hypothesis it names, so the link-based
    retraction lifecycle (``retract_disconfirmed_rcc``, the M6
    ``_representative_cause_hypothesis`` pick) can reach it.

    An LLM conclusion arrives with ``validated_hypothesis_id`` unset, so without a
    link a disconfirmed conclusion lingers and M6's max-``initial_likelihood`` proxy
    can wipe a re-grounded one that names a DIFFERENT live cause. Two tiers, both
    guarded by the SAME trust discipline — a STANDING hypothesis (never a refuted or
    retired one) with substantive shared-token overlap — so neither can attribute a
    conclusion to a refuted, retired, or textually-unrelated cause. That guard is
    load-bearing: a wrong link would let ``retract_disconfirmed_rcc`` wipe a valid,
    just-authored conclusion on an unrelated refutation (a NO-COLLAPSE breach).

      * **Tier 1 — authoritative (INV-35).** When the LLM named its cause's root
        node (``names_root_node_id``), pick the SINGLE standing hypothesis rooted
        there — exact identity, no lexical ambiguity resolution — and confirm the
        conclusion text substantively overlaps it (a coherence rail against a
        stale or mis-copied id). Counts ``llm_rcc_cause_named_total``.
      * **Tier 2 — lexical fallback (INV-34).** For an id-less conclusion (older
        turns, a same-turn root the LLM has no ``cn_`` id for yet, a non-compliant
        model), scan standing hypotheses by ``restatement_score`` (orphan-chain T1
        discipline): link only when EXACTLY ONE clears ``RESTATEMENT_STRONG`` (so it
        is the only one ``>= AMBIGUOUS``) with substantive overlap — "when unsure,
        don't link". Counts ``llm_rcc_cause_linked_total``.

    Giving the conclusion a cause link is NOT authorship — the engine never re-words
    the LLM's ``root_cause`` text (``determined_by`` stays the LLM's; the mirror
    synthesis may REPLACE the whole conclusion when a validated root outranks it,
    §7.7, but it never edits the LLM's prose in place). It only
    records which standing hypothesis the LLM's stated cause corresponds to. An
    unattributable conclusion stays the documented residual it has always been (no
    regression). A conclusion already linked to a PRESENT hypothesis is left stable
    (membership not liveness — a REFUTED linked hypothesis retains the link so
    ``retract_disconfirmed_rcc`` acts this recompute); a stale/dangling link is
    cleared before re-resolving. Returns True if it wrote a link.
    """
    rcc = case.root_cause_conclusion
    if rcc is None or getattr(rcc, "determined_by", None) == _ENGINE_RCC_AUTHOR:
        return False
    current = getattr(rcc, "validated_hypothesis_id", None)
    if current and current in case.hypotheses:
        return False
    # A dangling link (points at a hypothesis no longer present) is cleared before
    # we re-resolve: left set, it would make ``_representative_cause_hypothesis``
    # return None (the ``if vhid`` branch short-circuits the max-likelihood
    # fallback) and silently disable the M6 demotion for this case. A failed
    # re-resolve below then correctly leaves it None (proxy fallback restored).
    if current:
        rcc.validated_hypothesis_id = None
    statement = getattr(rcc, "root_cause", None) or ""
    if not statement:
        return False
    standing = list(_standing_hypotheses(case))

    # Tier 1 — authoritative: the LLM named its cause's root node. Restrict to a
    # SINGLE standing hypothesis rooted there (never a refuted/retired one — that
    # would let retract_disconfirmed_rcc wipe a just-authored valid conclusion) and
    # require substantive overlap (a stale or mis-copied id must not mis-attribute).
    named = getattr(rcc, "names_root_node_id", None)
    if named:
        named_matches = [
            h for h in standing if getattr(h, "root_node_id", None) == named
        ]
        if len(named_matches) == 1 and _substantive_overlap(
            statement, named_matches[0].statement
        ):
            rcc.validated_hypothesis_id = named_matches[0].hypothesis_id
            llm_rcc_cause_named_total.inc()
            return True

    # Tier 2 — lexical fallback: the SOLE hypothesis >= AMBIGUOUS must clear STRONG
    # with a substantive overlap (mirrors resolve_orphan_chains' T1 gate; a second
    # contender >= AMBIGUOUS means "don't guess").
    scored = [
        (s, h)
        for h in standing
        if (s := restatement_score(statement, h.statement)) >= RESTATEMENT_AMBIGUOUS
    ]
    if (
        len(scored) == 1
        and scored[0][0] >= RESTATEMENT_STRONG
        and _substantive_overlap(statement, scored[0][1].statement)
    ):
        rcc.validated_hypothesis_id = scored[0][1].hypothesis_id
        llm_rcc_cause_linked_total.inc()
        return True
    return False


_DISCONFIRMATION_REASON = (
    "counterfactual disconfirmation: the cause was addressed or confirmed "
    "correct yet the problem persisted"
)


def _node_has_counterfactual_refute(
    node: CausalNode, cat_by_id: dict[str, EvidenceCategory | None]
) -> bool:
    """Does the node carry a DECISIVE REFUTES link backed by
    ``CAUSAL_ABSENCE_EVIDENCE`` — a counterfactual disconfirmation (the §7.2
    strongest grade)? Same confidence bar as the tally
    (``counterfactual_link_decisive``): a self-hedged counterfactual neither
    triggers the M6 node-side demotion here nor — via the idempotence check in
    ``_attach_engine_refutation`` — suppresses the engine from attaching its
    own decisive refutation when M6 does fire."""
    return any(
        link.stance == EvidenceStance.REFUTES
        and cat_by_id.get(link.evidence_id) == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        and counterfactual_link_decisive(link)
        for link in node.evidence_links
    )


def _disconfirmed_cause_trigger(
    case: Case, *, node_side: bool
) -> tuple[Hypothesis, str] | None:
    """Shared M6 trigger for both demote paths. Returns ``(hypothesis, reason)``
    when a grounded (``cause_state=IDENTIFIED``) case's representative cause is
    disconfirmed, else None. Disconfirmation is the hypothesis being REFUTED or
    net-refuted (``_net_refuted``); and — only when ``node_side`` (the chain-mode
    case the prompt mandates) — its ROOT node carrying a counterfactual
    (CAUSAL_ABSENCE) refutation even when the flat hypothesis was untouched.

    ``node_side`` is FALSE for the flat path so its behavior is exactly as
    before: a persisted root (reloaded regardless of the flag, e.g. after the
    flag is flipped off) with a counterfactual refute must NOT demote a healthy
    flat hypothesis — node-derived disconfirmation is a chain-mode concept.

    Returns ``(hypothesis, reason, kind)``. **``kind`` distinguishes two
    materially different claims that share this trigger (#987):**

    - ``"evidence"`` — the hypothesis is REFUTED or net-refuted by its OWN
      evidence links. This asserts nothing about any fix; it is grounded in the
      links themselves and needs no external precondition.
    - ``"counterfactual"`` — the FAILED-FIX claim: the root carries a
      counterfactual (CAUSAL_ABSENCE) refutation, i.e. "the cause was addressed
      yet the problem persisted". That claim is about events outside the graph,
      so it is the one M6 must ESTABLISH rather than infer.

    Conflating them is what made the first #987 fix over-broad: gating the whole
    trigger on fix-application evidence silently blocked ordinary evidence-based
    disconfirmation, leaving a net-refuted cause standing as IDENTIFIED with its
    conclusion intact. When BOTH hold, ``"evidence"`` wins — it is the live,
    unconditional path, and it lets the engine record what it can actually
    substantiate instead of the stronger failed-fix story."""
    p = case.progress
    if p.cause_state != CauseState.IDENTIFIED:
        return None
    hyp = _representative_cause_hypothesis(case)
    if hyp is None:
        return None
    root = case.causal_nodes.get(hyp.root_node_id) if hyp.root_node_id else None
    evidence_side = _hypothesis_disconfirmed(hyp)
    counterfactual_side = (
        node_side
        and root is not None
        and _node_has_counterfactual_refute(root, _evidence_category_map(case))
    )
    if not (evidence_side or counterfactual_side):
        return None
    return (
        hyp,
        (hyp.refutation_reason or _DISCONFIRMATION_REASON)[:200],
        "evidence" if evidence_side else "counterfactual",
    )


# A hypothesis is a STANDING cause only while ACTIVE or VALIDATED — a REFUTED,
# RETIRED (abandoned/decayed) or CAPTURED (not-yet-pursued) one is not, so a stale
# root under it must not keep grounding the case. One definition, used by every
# chain-mode cause_state query below.
_STANDING_HYP_STATES = {HypothesisState.ACTIVE, HypothesisState.VALIDATED}


def _standing_hypotheses(case: Case):
    """Yield the case's standing-cause hypotheses (ACTIVE/VALIDATED)."""
    return (
        h for h in case.hypotheses.values() if h and h.state in _STANDING_HYP_STATES
    )


def any_chain_root_validated(case: Case) -> bool:
    """§9.2: does some STANDING hypothesis's chain ROOT node read VALIDATED? This
    is the chain-mode ``cause_state=IDENTIFIED`` signal."""
    return any(
        is_chain_root_validated(h, case.causal_nodes)
        for h in _standing_hypotheses(case)
    )


def project_hypothesis_states_from_roots(case: Case) -> bool:
    """#695 Defect A — derive each hypothesis's VALIDATED state from its chain
    ROOT node's ``node_state``. This is the SOLE producer of a VALIDATED
    hypothesis (the flat likelihood-threshold transition was removed). Returns
    True if any state changed.

    Invariant: a hypothesis reads ``VALIDATED`` ⟺ its chain root node is
    ``VALIDATED``. Because ``grade_cause_assurance`` returns ``NO_ROOT`` ⟺ no
    validated root node, this makes the report's "Validated" bucket, the cause
    grade, the runbook gate, and ``cause_state`` resolve to ONE determination —
    they can no longer disagree (the #695 divergence).

    Scope is VALIDATED only:
      * a STANDING (ACTIVE/VALIDATED) hypothesis whose root node is VALIDATED
        becomes VALIDATED;
      * a VALIDATED hypothesis whose root is no longer VALIDATED reverts to
        ACTIVE (a stale projection — e.g. the root lost validation to an
        evidence tie or the restatement guard on a pre-guard persisted case).
    ``REFUTED``/``RETIRED`` are NEVER touched: refutation is owned by the
    explicit lifecycle (``refute_hypothesis`` / M6 ``_net_refuted`` / an
    explicit user-or-LLM refute), which is already evidence-grounded and can
    legitimately refute a hypothesis whose root node is not itself REFUTED. A
    hypothesis with no ``root_node_id`` can never be VALIDATED — the graph is
    emission-only, so there is no flat validation floor.

    Ordering (load-bearing): run AFTER node-state settling — ``derive_node_states``
    INCLUDING the ``validate_by_exclusion`` re-derive — and AFTER the M6 demotion,
    so it reads final node states and cannot resurrect a just-REFUTED hypothesis
    to ACTIVE. Called at both settle points: the per-turn assessment recompute and
    the terminal confirm-stamp (a case reaching CONFIRMED via
    ``confirm_root_from_resolution_absence`` never recomputes, so its hypothesis
    must be re-projected there or the report would show it un-validated beside a
    CONFIRMED grade).
    """
    changed = False
    for hyp in case.hypotheses.values():
        # Only ACTIVE/VALIDATED are projection targets — CAPTURED (not yet
        # active), REFUTED, and RETIRED are owned by other lifecycle paths and
        # must never be overwritten here (the REFUTED no-clobber rule).
        if hyp.state not in (HypothesisState.ACTIVE, HypothesisState.VALIDATED):
            continue
        root = case.causal_nodes.get(hyp.root_node_id) if hyp.root_node_id else None
        root_validated = root is not None and root.node_state == NodeState.VALIDATED
        if root_validated and hyp.state != HypothesisState.VALIDATED:
            hyp.state = HypothesisState.VALIDATED
            changed = True
        elif not root_validated and hyp.state == HypothesisState.VALIDATED:
            hyp.state = HypothesisState.ACTIVE
            # Re-entering the ACTIVE differential restarts the stagnation clock:
            # the age-based decay sweep skips non-ACTIVE hypotheses, so a
            # last_progress_at_turn left at the pre-validation touch would charge
            # the reverted candidate for the turns it spent VALIDATED and could
            # decay/retire it the instant it reverts. A just-devalidated theory
            # gets the same fresh grace as a newly-formed candidate.
            hyp.last_progress_at_turn = case.current_turn
            hyp.last_updated_turn = case.current_turn
            changed = True
    return changed


# §7.1.2 MECE arbitration (#656): Jaccard at/above which two ROOT
# statements are ONE cause recorded twice (duplicate emission), not competing
# explanations. Same value as the other mirror bars but deliberately its own
# knob: root-statement identity is a different comparison from evidence
# independence or frame ownership, separately calibrated. Like every mirror
# bar it is LEXICAL: negation is stopworded, so opposite-polarity statements
# ("disk full" / "disk NOT full") read as one cause — an accepted limit of
# the token layer, shared with INV-27/INV-29 and pinned in the tests.
_ROOT_DISTINCT_JACCARD = 0.6


def _live_descendant_ids(
    node_id: str, adjacency: dict, nodes: dict[str, CausalNode]
) -> set[str]:
    """Nodes reachable from ``node_id`` along cause→effect edges WITHOUT
    passing through a REFUTED node. §7.1.2 reachability is deliberately
    liveness-aware, unlike the bearing-frame walk
    (``cause_assurance._chain_descendant_ids``, which renders a chain's
    recorded mechanism): a REFUTED rung is a BROKEN link — two validated
    roots joined only through a disproven intermediate are competing
    explanations, not one line of explanation, and merging them would mask a
    real contest. A refuted node is neither returned nor expanded (endpoints
    under comparison are VALIDATED/count-held, never refuted, so this only
    prunes intermediates). Iterative; a malformed cyclic graph terminates via
    the visited set."""
    descendants: set[str] = set()
    frontier = [node_id]
    while frontier:
        current = frontier.pop()
        for nxt in adjacency.get(current, ()):
            if nxt in descendants or nxt == node_id:
                continue
            node = nodes.get(nxt)
            if node is not None and node.node_state == NodeState.REFUTED:
                continue
            descendants.add(nxt)
            frontier.append(nxt)
    return descendants


def _edge_adjacency(case: Case) -> dict:
    """cause_node_id → [effect ids], built once per traversal batch (the
    per-frontier full-edge-list rescan is what made the naive walk O(V·E))."""
    adjacency: dict[str, list[str]] = {}
    for edge in case.causal_edges or []:
        adjacency.setdefault(edge.cause_node_id, []).append(edge.effect_node_id)
    return adjacency


def _cluster_relations(case: Case, root_ids) -> tuple[list, dict]:
    """Shared §7.1.2 groundwork: (sorted in-graph members, live-descendant map)."""
    members = sorted(rid for rid in root_ids if rid in case.causal_nodes)
    adjacency = _edge_adjacency(case)
    desc = {
        rid: _live_descendant_ids(rid, adjacency, case.causal_nodes) for rid in members
    }
    return members, desc


# ``causal_edges.and_group`` is String(64); the field is an unconstrained
# Optional[str] at every application layer above it (CausalNodeToAdd,
# CausalEdgeToAdd, CausalEdge). Since #1096 the prompt asks for a group key
# whenever a cause needs two conditions, and it invites a DESCRIPTIVE one —
# "memory-exhaustion-requires-unbounded-cache-and-reduced-limit" is already 60
# characters. An over-long key inserts fine on SQLite and raises "value too
# long for type character varying(64)" on PostgreSQL, i.e. it would fail the
# case save in cloud only.
_AND_GROUP_MAX_LEN = 64


def _normalize_and_group(and_group: object) -> str | None:
    """The ONE normalization of an AND-set key, applied where edges are written.

    - A blank key ("" or whitespace) names no group. The field is unconstrained
      end to end, so a model emitting ``and_group:""`` on independent
      alternatives would otherwise collapse them into one conjunction —
      silently strengthening the M7 gate and, since #1096, publishing "the
      cause required these conditions too" about causes that are alternatives.
    - A NUMBER is honored, as its string form. ``and_group: 1`` is plausible
      JSON from a model numbering its groups, and the key is an opaque identity
      token — "1" groups exactly what the emitter meant to group, so there is
      nothing to gain by discarding it and a conjunction to lose (the #1096
      factor loss again). Deliberate, not incidental: the schema declares
      ``Optional[str]`` and Pydantic v2 does not coerce int->str, so this
      reaches only the duck-typed callers (``kb_cause_seeder``'s specs). Bool
      is excluded — ``and_group: true`` is a model confusing the field for a
      flag, not naming a group, and "True" would silently group everything
      that made the same mistake. Any other type (list, dict) names no group.
    - An over-long key is folded to fit the column. A plain truncation would
      make two distinct long keys sharing a 64-char prefix into ONE group —
      the same silent M7 strengthening by another route — so the fold keeps a
      prefix and appends a digest of the WHOLE key. It is a pure function of
      that key, so the same logical group emitted on a later turn normalizes to
      the same token and the AND-set still forms.

    The key is an identity token, never rendered: ``validated_and_conjuncts``
    publishes the member nodes' statements, not the group name.
    """
    if isinstance(and_group, bool) or not isinstance(and_group, (str, int, float)):
        return None
    and_group = str(and_group).strip()
    if not and_group:
        return None
    if len(and_group) <= _AND_GROUP_MAX_LEN:
        return and_group
    digest = hashlib.sha256(and_group.encode("utf-8")).hexdigest()[:8]
    return f"{and_group[: _AND_GROUP_MAX_LEN - 9]}-{digest}"


def _observe_late_grouping(case: Case, effect_id: str, and_group: str) -> None:
    """Record a grouping that arrived AFTER its members were already validated.

    Since #1096 a conjunction is not a MECE contest (§7.1.2), so a grouping token
    over two already-VALIDATED rivals dissolves an arbitration hold: it grants
    identification, publishes the conjunction and unblocks the confirm-stamp.
    That is the intended mechanism — the model authors causal structure
    everywhere else, and demanding M7 proof before honoring a grouping would
    recreate the deadlock the fix removes — but the merge is MONOTONE, so the
    grant is permanent and never re-examined, and a stuck contest is exactly
    the state a model has an incentive to escape.

    So the SEQUENCE is made observable, not refused. Fires only when this
    grouping completes an AND-set whose members were ALREADY validated: a
    conjunction modeled up front — the shape the prompt asks for — never
    reaches here, because its causes are still CANDIDATE when the edges are
    emitted. Non-zero is a population to audit, not a verdict: a genuine late
    recognition is indistinguishable from a hallucinated one at this point, and
    the engine is deliberately not the one guessing which it saw.
    """
    members = [
        e.cause_node_id
        for e in case.causal_edges
        if e.effect_node_id == effect_id and e.and_group == and_group
    ]
    validated = sorted(
        nid for nid in members if _state(nid, case.causal_nodes) == NodeState.VALIDATED
    )
    if len(validated) < 2:
        return
    causal_and_set_late_grouping_total.inc()
    logger.info(
        f"Late and_group {and_group!r} joined {len(validated)} already-validated "
        f"causes of {effect_id} for case {case.case_id}: they are now ONE "
        f"conjunctive cause and no longer contest each other",
        extra={
            "event": "causal_and_set_late_grouping",
            "case_id": case.case_id,
            "turn": case.current_turn,
            "effect_node_id": effect_id,
            "and_group": and_group,
            "validated_members": validated,
        },
    )


def _co_necessary_sets(edges: list[CausalEdge]) -> list[list[str]]:
    """The graph's AND-sets: each is the list of causes sharing one
    ``(effect_node_id, and_group)`` — co-necessary for that effect (M7).

    Only genuine sets (>1 member) are returned; a lone member names a
    conjunction of one, which is just an ordinary cause. Read through
    ``incoming_and_groups`` so the blank-key normalization ("" is not a group)
    keeps its single home — a legacy row with ``and_group=""`` must not fuse
    independent alternatives into one cause here any more than it may satisfy
    the M7 gate.
    """
    effects = {e.effect_node_id for e in edges or [] if e.and_group is not None}
    sets: list[list[str]] = []
    for effect_id in sorted(effects):
        for key, cause_ids in incoming_and_groups(effect_id, edges).items():
            if key is not None and len(cause_ids) > 1:
                sets.append(cause_ids)
    return sets


def _distinct_cause_partition(case: Case, root_ids) -> tuple[list, dict]:
    """One relations pass behind §7.1.2: (clusters, live-descendant map).
    Shared by ``distinct_cause_clusters`` and ``sole_cluster_origin`` so the
    stamp's origin pick reads the SAME reachability its cluster count was
    built from (recomputing relations per consumer is both wasted work and a
    divergence seam)."""
    members, desc = _cluster_relations(case, root_ids)
    if not members:
        return [], desc
    tokens = {
        rid: _cached_content_tokens(case.causal_nodes[rid].statement or "")
        for rid in members
    }

    parent = {rid: rid for rid in members}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            # Deterministic: the lexically-smaller representative wins.
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo

    # CO-NECESSITY (M7) — an AND-set is a CONJUNCTION, not a differential.
    # S2's "at most one root can be the cause" holds between OR-alternatives;
    # co-necessary causes are the explicit counterexample, and their
    # simultaneous validation is the correct end state rather than a coherence
    # violation. Without this the engine punished exactly the shape the prompt
    # asks for on a two-condition cause (#1096): two validated conjuncts read as
    # a MECE contest, which holds identification (no cause_state=IDENTIFIED, so
    # no M5 solution license), asserts no conclusion at all, and refuses the
    # resolution confirm-stamp — leaving the case unable to reach CONFIRMED. The
    # exclusion lane already draws this line (``_survivor_or_sets`` builds its
    # differential from ``and_group is None`` edges only); §7.1.2 was the lane
    # that missed it.
    for and_set in _co_necessary_sets(case.causal_edges):
        in_scope = sorted(rid for rid in and_set if rid in parent)
        for other in in_scope[1:]:
            _union(in_scope[0], other)

    for i, a in enumerate(members):
        for b in members[i + 1 :]:
            if (
                b in desc[a]
                or a in desc[b]
                or _mutual_mirror(tokens[a], tokens[b], _ROOT_DISTINCT_JACCARD)
            ):
                _union(a, b)

    clusters: dict[str, set[str]] = {}
    for rid in members:
        clusters.setdefault(_find(rid), set()).add(rid)
    return [clusters[key] for key in sorted(clusters)], desc


def distinct_cause_clusters(case: Case, root_ids) -> list[set[str]]:
    """Cluster ROOT node ids into DISTINCT-cause groups (§7.1.2, MECE
    arbitration). Two roots are the SAME cause — one cluster — when:

    - their statements are MUTUAL mirrors (``_ROOT_DISTINCT_JACCARD``): the
      duplicate-emission shape, one cause recorded as two nodes (the model
      re-stated an existing root instead of referencing its ``cn_`` id); or
    - one lies on the other's LIVE causal path (``_live_descendant_ids``,
      either direction): a DEEPENED chain — "log rotation broken" → "disk
      full" is one line of explanation at two depths, not a differential (S2
      competition is between ORIGINS, not between a cause and its own
      consequence). A path through a REFUTED rung does NOT connect: the link
      is disproven, so the endpoints are genuine competitors; or
    - they are CO-NECESSARY — members of one AND-set (M7), sharing an
      ``(effect, and_group)``. A conjunction is ONE cause carrying two
      conditions, so both conjuncts standing validated is the correct end
      state, not the "several simultaneously-proven exclusive causes" a MECE
      hold exists to catch (#1096). Strictly a merge of the CONJUNCTS: an
      AND-set beside an independent alternative is still a real differential,
      and union-find keeps those in separate clusters.

    Grouping is the transitive closure (connected components) of those
    relations, iterated in sorted-id order so the result is order-invariant
    across dict/DB orderings. A root whose statement yields no content tokens
    merges with nothing on the mirror relation — conservative by design: an
    unjudgeable statement stays a DISTINCT cause, and the safe direction under
    NO-INCORRECT-CONCLUSION is holding identification, never concluding on an
    arbitrary pick.
    """
    return _distinct_cause_partition(case, root_ids)[0]


def _origin_of(cluster: set, desc: dict) -> str:
    """The ORIGIN of one distinct-cause cluster — the node the confirm-stamp
    cites (§7.1.2: on a deepened line the gone⇒gone confirmation is asserted
    of the origin, not its consequence): the member with the MOST live
    in-cluster descendants, lexical id on ties.

    That single sort IS the whole policy — no "has no member-ancestor"
    pre-filter is needed, because on a DAG a live member-ancestor strictly
    dominates: if ``r`` live-reaches member ``m`` then ``desc(r) ⊇ desc(m) ∪
    {m}`` (members are never REFUTED, so paths extend through them), giving
    ``r`` a strictly higher count. An edge-less duplicate of a consequence has
    zero descendants and never outranks the line's head; pure duplicates all
    count zero and tie to stable id order (same statement — any is faithful).
    A malformed cycle degenerates to the same id-order tie-break.
    """
    return sorted(cluster, key=lambda m: (-len(desc[m] & cluster), m))[0]


def sole_cluster_origin(case: Case, root_ids) -> "tuple[str, set] | None":
    """§7.1.2 arbitration entry point for the confirm-stamp, in ONE relations
    pass: ``None`` unless ``root_ids`` collapse to exactly one distinct cause
    (an unarbitrated MECE violation — the engine never guesses which cause the
    fix removed); otherwise ``(origin_id, member_ids)`` — the node to cite and
    the full cluster the stamp's idempotence and refutation-window checks must
    range over (a confirmation or failed-fix refute anywhere in the cluster
    belongs to the CAUSE)."""
    clusters, desc = _distinct_cause_partition(case, root_ids)
    if len(clusters) != 1:
        return None
    cluster = clusters[0]
    return _origin_of(cluster, desc), set(cluster)


def mece_contested_root_ids(case: Case) -> set:
    """§7.1.2 MECE arbitration (#656): the VALIDATED standing-chain roots
    that stand as simultaneously-validated DISTINCT causes — a coherence
    violation under S2 (roots are mutually-exclusive origins; at most one can
    be the cause, so "several are simultaneously proven" means the evidence has
    not discriminated yet). Returns the union of the contested roots' ids, or
    empty when identification is uncontested.

    - Only roots of STANDING hypotheses count — the same population that
      grounds ``cause_state=IDENTIFIED`` (``any_chain_root_validated``) and the
      same standing preference the confirm-stamp applies: an orphan validated
      node whose hypothesis decayed never contests the standing cause.
    - Duplicates, same-LIVE-causal-line roots and CO-NECESSARY conjuncts
      collapse first (``distinct_cause_clusters``): a duplicate emission is not
      a differential, and holding on one would deadlock — no evidence can ever
      discriminate a statement from its own restatement (NO-COLLAPSE); an M7
      AND-set is one cause carrying two conditions, and no evidence can
      discriminate between conditions the graph holds as both required.
    - A counterfactually CONFIRMED root (M2 top grade, engine-only producer)
      settles the contest outright: the gone⇒gone confirmation IS the
      discrimination, so validated siblings never hold a proven cause hostage.
      On a REOPENED case whose old confirmation has gone stale (the problem
      recurred), this deliberately still settles: recurrence is discharged by
      the failed-fix machinery (M6 demotes the confirmed root on
      disconfirmation), not by re-litigating the confirmation here —
      conclusion refresh/retraction is tracked on #656.

    This predicate HOLDS case-level identification only (the mirror of the
    §7.1.1 exclusion collapse: forward validation concludes only when the
    differential has collapsed to one cause). Node states are untouched —
    each root's VALIDATED standing is ruled by its own evidence (§7.1 entry-bar
    lesson: re-adjudicating settled nodes is what causes flap).
    """
    nodes = case.causal_nodes
    root_ids = {
        h.root_node_id
        for h in _standing_hypotheses(case)
        if h.root_node_id and _state(h.root_node_id, nodes) == NodeState.VALIDATED
    }
    if len(root_ids) < 2:
        return set()
    # Cluster BEFORE the confirmation scan: the common >=2-roots shape is a
    # duplicate emission, which collapses to one cluster with no evidence-map
    # build at all.
    clusters = distinct_cause_clusters(case, root_ids)
    if len(clusters) < 2:
        return set()
    cat_by_id = _evidence_category_map(case)
    if any(root_counterfactually_confirmed(nodes[rid], cat_by_id) for rid in root_ids):
        return set()
    # The clusters partition root_ids (all in-graph — a dangling root_node_id
    # never reads VALIDATED), so the contested union IS the root set.
    return set(root_ids)


def _chain_outranks_llm_conclusion() -> bool:
    """Whether a standing validated, uncontested chain root outranks an
    LLM-authored ``RootCauseConclusion`` (§7.7 precedence; kill switch
    ``FAULTMAVEN_CHAIN_AUTHORED_CONCLUSION``, default on).

    The ONE precedence consult. Reading configuration is the single impurity in
    this module's mirror path; it is deliberately kept to one call site so the
    switch cannot be honored in one lane and missed in another.
    """
    from faultmaven.config.settings import get_settings

    return bool(get_settings().features.chain_authored_conclusion)


def _conclusion_provider_label() -> str:
    """CHAT provider label for the conclusion-precedence counter.

    Delegates to the resolution-metric helper rather than re-deriving the label:
    the two counters are read as a ratio, so a second resolution rule that drifted
    would silently divide one provider's numerator by another's denominator.
    Imported lazily — this module is the graph leaf and must not take a module-
    level dependency on the terminal layer.
    """
    from faultmaven.core.investigation.terminal_transitions import (
        _resolve_resolution_provider,
    )

    return _resolve_resolution_provider()


def synthesize_rcc_from_validated_root(case: Case) -> bool:
    """§9.3 — mirror the validated chain into a ``RootCauseConclusion`` so the
    disposition / report layer reads cause text rendered from what the chain
    actually proves.

    The validated root IS the cause, so a derived RCC (root statement + the chain
    as mechanism, at the confidence the M2 grade supports — CONFIDENT for a
    mechanistic root, VERIFIED only for a counterfactually confirmed one) is a
    faithful mirror, not an assertion. Because it can assert no more than the
    chain proves, it **outranks** an LLM-authored conclusion (§7.7): when a
    standing validated root exists, the mirror is minted over one, and the LLM's
    own conclusion is surfaced only as the fallback for the no-root case (below).
    With the precedence switched off, an LLM-authored conclusion is instead left
    untouched and the mirror is minted only into an empty or engine-authored slot.

    Every refusal sits AHEAD of the precedence. While identification is
    MECE-contested the engine asserts nothing at all, and with no standing
    validated root this is a no-op — whatever conclusion the case carries, LLM
    text included, stands byte-identical. An engine mirror is also refreshed when
    it has gone stale — its named hypothesis is no longer a standing-validated
    root (the grounding chain handed off to another root via an INCONCLUSIVE
    drift, NOT a refutation, so M6 never cleared it) — or when its confidence no
    longer agrees with the root's M2 grade (confirmation arrived, so the mirror
    upgrades to VERIFIED; or a pre-cap persisted mirror over-claims VERIFIED on a
    mechanistic root, so it corrects down). Returns True if it wrote one.

    Replacement is one-way by design: a mirror that took over from an LLM
    conclusion and whose root later demotes is retracted like any other mirror
    (``retract_stale_engine_rcc``) and the case is then left with NO conclusion.
    The replaced text is not kept anywhere to restore — retaining it would be a
    second conclusion namespace, the thing single authority retires — and
    re-surfacing it would assert a cause no validated root backs, whether or not
    it named the same cause the demoted root did.
    """
    rcc = case.root_cause_conclusion
    llm_authored = (
        rcc is not None and getattr(rcc, "determined_by", None) != _ENGINE_RCC_AUTHOR
    )
    if llm_authored and not _chain_outranks_llm_conclusion():
        # Precedence off: an LLM-authored conclusion is never overwritten.
        # Checked before any graph work — the scans below would be pure waste.
        return False

    # §7.1.2 defense-in-depth: while identification is MECE-contested the
    # engine asserts NO conclusion — a mirror naming one of several
    # simultaneously-validated exclusive causes is an arbitrary pick. The
    # per-turn recompute already gates its call on the same predicate (and
    # retract_stale_engine_rcc clears a standing contested mirror), so this
    # refusal protects direct/non-recompute callers; no IDENTIFIED-with-null-
    # conclusion split is possible because cause_state is held by the
    # identical predicate.
    if mece_contested_root_ids(case):
        return False

    cat_by_id = _evidence_category_map(case)  # one snapshot for every check below

    def _hyp_confirmed(h: "Hypothesis") -> bool:
        r = case.causal_nodes.get(h.root_node_id or "")
        return r is not None and root_counterfactually_confirmed(r, cat_by_id)

    validated_hyps = [
        h
        for h in _standing_hypotheses(case)
        if is_chain_root_validated(h, case.causal_nodes)
    ]
    # ONE confirmation scan feeds the faithfulness check, the root selection,
    # and the mint band below — separate scans of the same predicate would let
    # an asymmetric edit split the named root from the minted confidence.
    confirmed_hyps = [h for h in validated_hyps if _hyp_confirmed(h)]

    # The faithfulness short-circuit and the "keep the named root" selection tie
    # below both read the STANDING MIRROR. An LLM-authored conclusion is not one:
    # its cause link (§7.6) says which hypothesis its prose corresponds to, never
    # that its text renders that root — so treating it as a prior would let a
    # linked, correctly-graded LLM conclusion short-circuit the very replacement
    # the precedence exists to perform.
    prior = None
    if rcc is not None and not llm_authored:
        prior = case.hypotheses.get(getattr(rcc, "validated_hypothesis_id", None) or "")
        if prior is not None and prior in validated_hyps:
            # The mirror still names a grounding root — but it must also (a)
            # claim the confidence its M2 grade supports ("verified" ⇔
            # counterfactually confirmed; expected level derived from the SAME
            # mint constants below, so a constant/band change can never split
            # the check from the mint into re-mint churn), and (b) name the
            # confirmed root when one exists — a mirror asserting an
            # unconfirmed sibling while another root carries the gone⇒gone
            # confirmation is not faithful. Disagreement → fall through, re-mint.
            prior_confirmed = prior in confirmed_hyps
            expected_level = ConfidenceLevel.from_score(
                CONFIRMED_RCC_LIKELIHOOD_FLOOR
                if prior_confirmed
                else MECHANISTIC_RCC_LIKELIHOOD
            )
            if (
                rcc.confidence_level == expected_level
                and (prior_confirmed or not confirmed_hyps)
                and list(rcc.contributing_factors or [])
                == conjuncts_for_chain(case, prior)
            ):
                return False  # faithful mirror: root, grade AND conjuncts agree
        # else: stale/misgraded engine mirror — refresh from the current
        # validated root.
    # No restatement check here BY DESIGN: the §7.1 guard is an ENTRY bar on
    # validation (empirical lane + exclusion lane), so no restating root can
    # freshly validate — and a root that stands VALIDATED (incl. grandfathered
    # pre-guard ones) must be mirrorable, or cause_state=IDENTIFIED would
    # split from a permanently-absent conclusion. The mirror follows the
    # validation verdict; it never re-adjudicates it.
    #
    # Root selection order: a counterfactually CONFIRMED root first (the M2 top
    # grade must be what the conclusion asserts), then the prior mirror's own
    # root (a level-only correction must not silently swap the named cause),
    # then the first standing validated chain.
    # Within the confirmed set, the prior mirror's own root wins: this function
    # now also re-mints for reasons unrelated to the cause (a conjunct that
    # validated this turn), and taking confirmed_hyps[0] blindly would swap the
    # published cause on such a refresh — the swap the "keep the named root"
    # rule below exists to prevent, one tier up.
    hyp = None
    if confirmed_hyps:
        hyp = prior if prior in confirmed_hyps else confirmed_hyps[0]
    if hyp is None and prior is not None and prior in validated_hyps:
        hyp = prior
    if hyp is None:
        hyp = next(iter(validated_hyps), None)
    if hyp is None:
        return False
    root = case.causal_nodes[hyp.root_node_id]
    # Mechanism = the chain's intermediate rungs (root -> ... -> D), if any.
    inter = [
        case.causal_nodes[nid].statement
        for nid in (hyp.path or [])[1:-1]
        if nid in case.causal_nodes
    ]
    mechanism = (
        " → ".join(inter + ["the problem"])
        if inter
        else "Directly produces the observed problem."
    )[:2000]
    # M2 confidence grades: the GRADE, not the LLM's likelihood, rules the
    # mirror in both directions. A validated root — EMPIRICAL or DEDUCTIVE
    # alike — is mechanistic, so the mirror reads CONFIDENT at the fixed
    # grade-band value (the LLM's own root_cause_likelihood can neither push a
    # mechanistic cause into "verified" nor drag the engine's validation
    # verdict below its band). Only a counterfactually CONFIRMED root
    # (causal_absence SUPPORTS link, gone⇒gone) reads VERIFIED, floored at 0.9.
    # confidence_level must agree with likelihood
    # (RootCauseConclusion.confidence_consistency), so derive it from the final
    # score rather than hardcoding.
    if hyp in confirmed_hyps:
        likelihood = max(
            case.progress.root_cause_likelihood or 0.0,
            CONFIRMED_RCC_LIKELIHOOD_FLOOR,
        )
    else:
        likelihood = MECHANISTIC_RCC_LIKELIHOOD
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=root.statement[:1000],
        mechanism=mechanism,
        confidence_level=ConfidenceLevel.from_score(likelihood),
        likelihood=likelihood,
        validated_hypothesis_id=hyp.hypothesis_id,
        evidence_basis=[
            link.evidence_id
            for link in root.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        ],
        # The cause's co-necessary conjuncts (#1096). Derived from the graph like
        # every other field here — the mirror renders one chain, so without this
        # a cause the investigation established as a conjunction would reach the
        # report as its first conjunct alone. NOT the LLM's own factor prose:
        # single authority is unchanged, this is the graph speaking.
        contributing_factors=conjuncts_for_chain(case, hyp),
        determined_by=_ENGINE_RCC_AUTHOR,
    )
    if llm_authored:
        # The mirror REPLACED an LLM-authored conclusion — the one event this
        # counter measures. Deliberately not incremented on a first mint into an
        # empty conclusion, nor on a mirror refreshing a mirror: those say nothing
        # about how often the chain outranks the model's prose.
        rcc_precedence_inversion_total.labels(
            provider=_conclusion_provider_label()
        ).inc()
    return True


def retract_stale_engine_rcc(case: Case, contested_ids: set | None = None) -> bool:
    """Clear an ENGINE-authored RootCauseConclusion whose named grounding root no
    longer stands validated — the mirror exists only to reflect a validated
    chain, so when the chain's root demotes (e.g. the §7.1 restatement guard on
    a pre-guard persisted case, or an evidence tie) the mirror must not outlive
    it: readiness/report readers key on the RCC's presence and would otherwise
    keep asserting a conclusion nothing grounds. Likewise cleared when the named
    root, though still validated, is MECE-CONTESTED (§7.1.2): a mirror asserting
    ONE of several simultaneously-validated exclusive causes is an arbitrary
    pick, not a reflection — the engine withholds its conclusion pending
    discrimination, and the mirror re-mints automatically when the contest
    resolves. LLM-authored conclusions are NEVER touched here (their retraction
    lifecycle is a separate concern — tracked on #656).

    Note what this means for a mirror that REPLACED an LLM conclusion (§7.7): when
    its root demotes it is cleared like any other mirror and the case is left with
    NO conclusion. The replaced text is not restored — the engine keeps no copy of
    it (a copy would be a second conclusion namespace), and re-surfacing it would
    assert a cause no validated root backs, whether or not it named the same cause
    the demoted root did.

    Returns True if it cleared one. ``contested_ids`` lets the per-turn recompute
    pass its already-computed §7.1.2 set (the once-per-derive snapshot pattern);
    ``None`` means compute here.
    """
    rcc = case.root_cause_conclusion
    if rcc is None or getattr(rcc, "determined_by", None) != _ENGINE_RCC_AUTHOR:
        return False
    prior = case.hypotheses.get(getattr(rcc, "validated_hypothesis_id", None) or "")
    if (
        prior
        and prior.state in _STANDING_HYP_STATES
        and is_chain_root_validated(prior, case.causal_nodes)
    ):
        if contested_ids is None:
            contested_ids = mece_contested_root_ids(case)
        if prior.root_node_id not in contested_ids:
            return False  # still faithfully mirrors a standing UNCONTESTED root
    case.root_cause_conclusion = None
    return True


def any_chain_root_inconclusive(case: Case) -> bool:
    """Does some STANDING hypothesis's chain ROOT node read INCONCLUSIVE — i.e.
    a cause we have gathered bearing-but-indecisive evidence on (neither validated
    nor refuted)? Such a root is a live CANDIDATE, not nothing. Used by the
    cause_state soft floor: a once-grounded root that loses validation to an
    evidence tie (INCONCLUSIVE, NOT counterfactually REFUTED) holds the case at
    CANDIDATES rather than flapping down to UNKNOWN (finding-5 / NO-COLLAPSE). A
    truly REFUTED root is excluded here, so a real M6 disconfirmation still drops
    the case fully. (Also captures a never-yet-grounded root that has bearing-but-
    indecisive evidence — that too is a live candidate, so CANDIDATES over UNKNOWN
    is the honest read.)"""
    return any(
        h.root_node_id
        and _state(h.root_node_id, case.causal_nodes) == NodeState.INCONCLUSIVE
        for h in _standing_hypotheses(case)
    )


def _node_has_engine_counterfactual_refute(node: CausalNode, case: Case) -> bool:
    """Does the node already carry the ENGINE's own M6 refutation — a REFUTES
    link backed by an engine-authored ``CAUSAL_ABSENCE_EVIDENCE`` row?"""
    engine_absence_ids = {
        e.evidence_id
        for e in case.evidence
        if getattr(e, "category", None) == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        and getattr(e, "collected_by", None) == ENGINE_EVIDENCE_AUTHOR
    }
    return any(
        link.stance == EvidenceStance.REFUTES and link.evidence_id in engine_absence_ids
        for link in node.evidence_links
    )


def _fix_application_turn(case: Case) -> int | None:
    """The turn at which the case RECORDS that a fix was executed, or ``None``
    when no such record exists — M6's first precondition (#987).

    The authoritative record is a ``ProposedAction`` in state ``accepted`` whose
    type is **SOLUTION** — a MITIGATION is by definition not a fix of the cause
    (INV-42), so a failed workaround must never establish that the cause was
    addressed. Per ``classify_solution_outcome``, ``accepted`` means the user
    *executed* it, and the turn is read from ``accepted_in_turn`` (EXECUTION),
    never ``proposed_in_turn`` (the OFFER). The NEWEST such turn wins — a failed
    fix is disconfirmed by what happened after the LAST fix, not the first.

    Deliberately NO compliance-gate fallback: ``solution_accepted`` records
    THAT a fix was executed but not WHEN, and flooring the window at 0 made
    every pre-fix symptom row on the case read as a post-fix persistence
    observation. A precondition that cannot be dated cannot establish "what
    happened after the fix", so it establishes nothing. On the no-ProposedAction
    shape M6's counterfactual arm simply does not fire — the evidence-based arm
    is unaffected and still demotes a genuinely refuted cause.
    """
    turns = [
        a.accepted_in_turn
        for a in (getattr(case, "proposed_actions", None) or [])
        if getattr(a, "state", None) == "accepted"
        # SOLUTION only — a MITIGATION is by definition NOT a fix of the cause
        # (the prompt: a mitigation "does NOT eliminate the root cause, so the
        # cause is still present"). A workaround that failed to relieve the
        # symptom says nothing about whether the cause was addressed, so it must
        # never establish "the cause was addressed yet the problem persisted"
        # and refute the root at belief 0.
        #
        # Enum OR raw string, the same read ``classify_solution_outcome`` does
        # (its ``_action_type_value`` is private to the domain module, so the
        # one-line equivalent is inlined rather than crossing the contracts
        # boundary): reading only ``.value`` would silently miss a string-typed
        # action and refuse M6 forever on that deployment. Failing closed is the
        # right DIRECTION for this gate, but not by accident.
        and getattr(
            getattr(a, "action_type", None), "value", getattr(a, "action_type", None)
        )
        == InvestigationActionType.SOLUTION.value
        # ``accepted_in_turn`` (EXECUTION), never ``proposed_in_turn`` (the
        # OFFER): keying on the proposal turn let evidence recorded in the very
        # turn the fix was offered — before it was ever run — satisfy "the
        # problem persisted afterwards". Actions accepted before this field
        # existed carry None and simply do not establish the precondition,
        # which is the fail-closed direction.
        and getattr(a, "accepted_in_turn", None) is not None
    ]
    if turns:
        return max(turns)
    return None


def _problem_persistence_observed_after(case: Case, fix_turn: int) -> bool:
    """Does the case OBSERVE the problem still present at/after ``fix_turn``? —
    M6's second precondition (#987).

    The observation is a ``SYMPTOM_EVIDENCE`` row collected at/after the fix
    turn: that is exactly the encoding the prompt's TREATMENT FAILURE PATH
    mandates for a fix that did not hold ("symptom_evidence: New symptoms that
    emerge after a failed fix"). ``>=`` and not ``>`` because turn granularity
    cannot order within-turn events — the user's "I ran it, still failing"
    lands the executed fix and the persisting symptom in ONE turn.

    Deliberately a POSITIVE observation, not the absence of a resolution: M6 is
    a destructive transition (it refutes the standing cause, zeroes its root's
    belief, and retracts the conclusion), so it must be established from
    something the case actually recorded. "Nothing said it was fixed" is not an
    observation that it stayed broken.
    """
    return any(
        getattr(e, "category", None) == EvidenceCategory.SYMPTOM_EVIDENCE
        and (getattr(e, "collected_at_turn", 0) or 0) >= fix_turn
        for e in (getattr(case, "evidence", None) or [])
    )


def _evidence_disconfirmation_provenance(hyp) -> str:
    """Provenance for an EVIDENCE-based M6 demotion (#987).

    The honest record when the cause fell to its own links rather than to a
    failed fix: what the engine derived, and the tally it derived it from. It
    deliberately makes NO claim about a fix having been applied — that is the
    counterfactual arm's claim, and asserting it here unestablished is exactly
    the fabrication this campaign removed.
    """
    links = getattr(hyp, "evidence_links", None) or []
    refuting = sum(1 for link in links if link.stance == EvidenceStance.REFUTES)
    supporting = sum(1 for link in links if link.stance == EvidenceStance.SUPPORTS)
    if getattr(hyp, "state", None) == HypothesisState.REFUTED:
        detail = (
            f"the hypothesis was refuted "
            f"({hyp.refutation_reason or 'no reason recorded'})"
        )
    else:
        detail = (
            f"its evidence links net-refute it "
            f"({refuting} refuting vs {supporting} supporting)"
        )
    return f"the identified cause no longer stands — {detail}"[:400]


def _has_undatable_solution_acceptance(case: Case) -> bool:
    """An accepted SOLUTION carrying NO ``accepted_in_turn`` — a fix the case
    records as executed but cannot date (#987).

    Only reachable on acceptances stamped before ``accepted_in_turn`` existed,
    so this is a TRANSITION signal that drains as in-flight cases close, not a
    steady-state one. It exists purely so the refusal metric can separate "a
    fix ran, we just can't date it" from "nothing was ever tried" — the former
    is a real (bounded) suppression of legitimate failed-fix demotions and
    should be visible as such.
    """
    return any(
        getattr(a, "state", None) == "accepted"
        and getattr(
            getattr(a, "action_type", None), "value", getattr(a, "action_type", None)
        )
        == InvestigationActionType.SOLUTION.value
        and getattr(a, "accepted_in_turn", None) is None
        for a in (getattr(case, "proposed_actions", None) or [])
    )


def m6_disconfirmation_basis(case: Case) -> tuple[int, str] | None:
    """M6's ESTABLISHED preconditions, or ``None`` (metered by refusal reason).

    Returns ``(fix_turn, provenance)`` when the case record establishes BOTH
    halves of "the cause was addressed, yet the problem persisted":

    1. a RECORDED fix application (``_fix_application_turn``), and
    2. an OBSERVED persistence of the problem at/after it
       (``_problem_persistence_observed_after``), with
    3. NO qualifying resolution-confirmation row at/after the fix turn — a
       standing gone⇒gone confirmation is direct evidence the problem did NOT
       persist, so the failed-fix premise is false on the case's own record.

    Why this gate exists (#987): M6 previously fired on the mere presence of a
    counterfactual refute and then MINTED a row asserting "the cause was
    addressed or confirmed correct, yet the problem persisted" — a fact it had
    never checked. In the incident it was false: the fix had SUCCEEDED, the LLM
    had mis-linked its own success-confirmation row as a REFUTES, and M6
    laundered that into a fabricated observation which refuted the true root at
    belief 0 and retracted a correct conclusion.

    The rule, stated once: **constructive transitions may be derived from
    confirmation plus evidence with recorded provenance; DESTRUCTIVE
    transitions require established preconditions.** M6 is destructive, so it
    establishes. The category gate at ingest closes the specific #987 route;
    this closes the mechanism, which stays reachable from any future path that
    can put a refutation on a cause.

    **SCOPE — the counterfactual (failed-fix) arm ONLY.** This is a precondition
    for the CLAIM "a fix was applied and the problem persisted", which is about
    events outside the graph. It is NOT a precondition for demotion in general:
    an EVIDENCE-based disconfirmation (the hypothesis REFUTED or net-refuted by
    its own links) is grounded in the graph and demotes unconditionally — see
    ``_disconfirmed_cause_trigger``'s ``kind``. Gating both on fix-application
    evidence was the over-broad first cut of this fix, and it left a net-refuted
    cause standing as IDENTIFIED with its conclusion intact.

    Refusing the counterfactual arm does NOT leave a disproven cause standing:
    the hypothesis keeps whatever state the model gave it, a REFUTED hypothesis
    stops being STANDING (so ``any_chain_root_validated`` no longer grounds
    ``cause_state``), and ``retract_disconfirmed_rcc`` still clears a conclusion
    naming it. What is withheld is only the DURABLE engine refutation.
    """
    fix_turn = _fix_application_turn(case)
    if fix_turn is None:
        # Two different worlds, separately labeled: nothing was ever tried, vs
        # a fix WAS executed but carries no execution turn — an acceptance
        # stamped before ``accepted_in_turn`` existed. Both refuse (an undatable
        # precondition establishes nothing about "after the fix"), but only the
        # second is a TRANSITION artifact that drains as in-flight cases close.
        # Folding them into one series would teach operators to read a real
        # suppression window as the benign "nothing was tried" baseline.
        m6_demotion_refused_total.labels(
            reason=(
                "undatable_acceptance"
                if _has_undatable_solution_acceptance(case)
                else "no_fix_applied"
            )
        ).inc()
        return None
    if any(
        (getattr(row, "collected_at_turn", 0) or 0) >= fix_turn
        for row in resolution_confirmation_rows(case)
    ):
        m6_demotion_refused_total.labels(reason="resolution_confirmed").inc()
        return None
    if not _problem_persistence_observed_after(case, fix_turn):
        m6_demotion_refused_total.labels(reason="no_persistence").inc()
        return None
    return fix_turn, (
        f"a fix recorded as EXECUTED at turn {fix_turn} did not hold — symptom "
        f"evidence at/after that turn observes the problem still present, and "
        f"no resolution confirmation stands"
    )


def _attach_engine_refutation(
    case: Case, node_id: str, reason: str, provenance: str
) -> None:
    """Attach a DURABLE engine-authored REFUTES link (+ backing row) to
    ``node_id`` — the Option-(c) mechanism that makes M6 evidence-driven.
    Without a persisted refuting fact, ``derive_node_states`` would re-validate
    the root next turn from the stale supporting evidence and resurrect the
    disconfirmed cause (the turn-28 bug).

    Idempotent on the ENGINE's own refutation specifically: skips only when
    the node already carries an ENGINE-authored CAUSAL_ABSENCE refute. An
    LLM-recorded decisive refute on the node deliberately does NOT suppress
    the mint — the engine row is the durable failed-fix MARKER the
    disconfirmation window keys on (``cause_assurance``: authorship-keyed so
    pruning cannot collapse it), so "M6 mints exactly one engine row per
    disconfirmation" must hold even when the model already recorded the
    failure itself (reviewed: suppressing it left the window at -1 and let a
    stale premature 'stable' row re-qualify as the resolution confirmation).

    The row records an ENGINE INFERENCE WITH PROVENANCE, never an observation
    (#987). It is engine-authored (``collected_by=ENGINE_EVIDENCE_AUTHOR`` —
    the marker every reader already keys on, and the reason
    ``resolution_confirmation_rows`` excludes it from ever reading as a
    confirmation), and its text now states what the engine DERIVED and the
    case records it derived it FROM — supplied by ``m6_disconfirmation_basis``,
    which is the only caller and which fires only on ESTABLISHED preconditions.
    It previously asserted a first-person observation ("the cause was addressed
    ... yet the problem persisted") that no one had checked; that sentence was
    false in the #987 incident and is what made a fabrication durable.

    Residual, stated: the row still lives in the evidence table under
    ``CAUSAL_ABSENCE_EVIDENCE`` because the whole disconfirmation-window
    machinery (``_engine_absence_row_ids``, ``latest_disconfirmation_turn``,
    ``_disconfirmation_row_ids``) is keyed on that category × engine
    authorship. Its authorship already excludes it from every observation
    reader; giving engine inferences their own category is a larger,
    separately-reviewable change.
    """
    node = case.causal_nodes.get(node_id)
    if node is None or _node_has_engine_counterfactual_refute(node, case):
        return
    ev_id = f"ev_{uuid4().hex[:12]}"
    case.evidence.append(
        Evidence(
            evidence_id=ev_id,
            summary=f"Engine inference (M6), not an observation: {provenance}."[:500],
            primary_purpose="engine inference: failed-treatment disconfirmation",
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            # USER_DESCRIPTION is the only source_type the evidence_source_invariant
            # CHECK permits with no source_file_id (this engine-authored fact has no
            # file); collected_by marks the true author.
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by=ENGINE_EVIDENCE_AUTHOR,
            collected_at_turn=case.current_turn,
            collected_at=datetime.now(timezone.utc),
        )
    )
    node.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=ev_id,
            stance=EvidenceStance.REFUTES,
            reasoning=reason,
            linked_at_turn=case.current_turn,
        )
    )


def demote_disconfirmed_cause_via_evidence(case: Case) -> bool:
    """M6 (Option c): on counterfactual disconfirmation of the grounded cause,
    refute the flat hypothesis AND attach a DURABLE engine refutation to its
    root, then retract the conclusion. Rather than imperatively flipping
    ``node_state``, the root's refutation is recorded as EVIDENCE so the
    subsequent
    ``derive_node_states`` — this turn and every later turn — keeps the root
    REFUTED instead of re-validating it from the now-stale supporting evidence.

    Shares the M6 trigger with the flat path (``_disconfirmed_cause_trigger``),
    which ALSO fires when the disconfirmation lands on the root NODE rather than
    the flat hypothesis — so the conclusion is retracted even when only the node
    was refuted, closing the truth-split where ``cause_state`` drops but a stale
    ``RootCauseConclusion`` lingers. Returns True if it acted.

    Each disconfirmation KIND records only what the engine can substantiate
    (#987):

    - ``evidence`` — the hypothesis is refuted/net-refuted by its OWN links.
      Fires unconditionally (it was always evidence-grounded) and records an
      evidence-based inference. Gating THIS on fix-application evidence was the
      over-broad first cut of the #987 fix: it left a net-refuted cause standing
      as IDENTIFIED with its conclusion intact.
    - ``counterfactual`` — the FAILED-FIX claim, about events outside the graph.
      Fires only on preconditions the case record ESTABLISHES
      (``m6_disconfirmation_basis``); otherwise the demotion is refused and
      metered. The engine must never assert a failed fix it did not establish —
      that assertion, minted as a durable row, is what made #987 permanent.

    Either way the hypothesis's own state still governs downstream, so a
    genuinely disproven cause stops grounding ``cause_state`` regardless.
    """
    p = case.progress
    # Chain path: a counterfactual refute on the root NODE also disconfirms the
    # cause. After #987 the only producer of such a link is the engine's own
    # durable marker (the category gate at ingest refuses model-authored links
    # on absence rows), so this arm is what keeps a prior M6 LATCHED across
    # turns rather than a fresh LLM-driven entry point.
    trigger = _disconfirmed_cause_trigger(case, node_side=True)
    if trigger is None:
        return False
    hyp, reason, kind = trigger

    if kind == "counterfactual":
        basis = m6_disconfirmation_basis(case)
        if basis is None:
            return False
        provenance = basis[1]
    else:
        provenance = _evidence_disconfirmation_provenance(hyp)

    if hyp.state != HypothesisState.REFUTED:
        HypothesisManager().refute_hypothesis(hyp, case.current_turn, [], reason)
    if hyp.root_node_id:
        _attach_engine_refutation(case, hyp.root_node_id, reason, provenance)
    # Retract the conclusion so the disposition layer cannot keep treating the
    # cause as known; the cause_state itself is re-derived from the (now refuted)
    # root by the caller's derive + recompute.
    #
    # §7.6 / INV-34 refresh: for a LINKED conclusion the trigger's ``hyp`` IS the
    # conclusion's named cause — ``link_llm_rcc_to_cause`` runs earlier this
    # recompute, so ``_representative_cause_hypothesis`` resolves to the hypothesis
    # the LLM's conclusion names; a conclusion re-grounded onto a DIFFERENT
    # still-standing cause points the trigger there and this demotion never fires
    # on it (the refresh is delivered by the link). RESIDUAL, accepted: an
    # UNLINKABLE conclusion (ambiguous/paraphrased text, no vhid) leaves the
    # trigger on the max-``initial_likelihood`` proxy, so if that proxy is the
    # disconfirmed cause the conclusion is still cleared even when it named a
    # different cause — the pre-existing NO-INCORRECT-first behavior (never leave a
    # possibly-disproven conclusion standing on a guess; inferring a
    # different-cause link lexically would only trade this for the wrong-link
    # collapse the link bar guards against).
    rcc = case.root_cause_conclusion
    # Count only a genuine "named cause was disconfirmed" retraction: the cleared
    # conclusion was LINKED to the very hypothesis this demotion disconfirmed. A
    # collateral proxy-path wipe of an unlinkable conclusion is not that event, so
    # it must not inflate the failed-fix signal (lifecycle-metrics § INV-34).
    if (
        rcc is not None
        and getattr(rcc, "determined_by", None) != _ENGINE_RCC_AUTHOR
        and getattr(rcc, "validated_hypothesis_id", None) == hyp.hypothesis_id
    ):
        llm_rcc_retracted_disconfirmed_total.inc()
    case.root_cause_conclusion = None
    p.root_cause_likelihood = 0.0
    return True


# ---------------------------------------------------------------------------
# Orphan-chain resolution (the invariant: "every chain explaining D is attached
# to exactly one hypothesis"). The divergence the prompt (step 2) does not fully
# prevent: the LLM emits a real root->D chain but leaves it unlinked, so the
# hypothesis keeps running flat while a parallel orphan chain describes the SAME
# cause (double-representation). This deterministic post-pass runs each turn
# AFTER chain-ingest.
# ---------------------------------------------------------------------------

# A root whose statement restates a hypothesis at/above STRONG, with no other
# hypothesis at/above AMBIGUOUS, is an UNAMBIGUOUS double-representation and is
# re-attached automatically (T1). A weaker or contested match is left for an
# LLM nudge (T2a) — a wrong auto-attach is itself an incorrect conclusion, so
# "when unsure, don't". The scoring + thresholds mirror the sim analyzer's
# ``_restatement_score`` / ``_RESTATEMENT_THRESHOLD``
# (fm-sre-simulator/scripts/analyze_chain_emission.py) so engine and harness
# agree on what "restates" means — keep them reconciled when either moves.
RESTATEMENT_STRONG = 0.6
RESTATEMENT_AMBIGUOUS = 0.4

# §7.1 restatement guard's validation bar: the minimum NOVEL-token fraction a
# ROOT statement must carry beyond the case frame (problem anchors + other
# standing hypotheses) to be ADMITTED to VALIDATED. Deliberately its own knob,
# decoupled from the T1 orphan-reattach threshold (opposite error economics —
# a wrong re-attach is recoverable via the LLM nudge; a wrong validation mints
# a false conclusion). The calibration figures live in ONE executable home:
# test_restatement_guard_calibration.py (methodology prose: §7.1).
ROOT_NOVELTY_MIN_FRACTION = 0.3

# Jaccard at or above which an UNATTACHED hypothesis is treated as a node's
# presumptive OWNER (excluded from that node's frame): both statements are
# mutually ~the same claim, the normal chain-emission shape during the
# attachment lag. One-way containment stays IN the frame (the #656 disjunction
# root contains each source hypothesis but mutually mirrors none — jaccard
# ~0.33 — so the incident is still caught).
_FRAME_OWNER_JACCARD = 0.6


def restatement_score(a: str, b: str) -> float:
    """How strongly statement ``a`` restates ``b`` (0..1): the max of Jaccard and
    the two containments, so a specific elaboration largely covered by a more
    general statement (or vice versa) still scores high. Fuzzy by nature."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    jaccard = inter / len(ta | tb)
    return max(jaccard, inter / len(ta), inter / len(tb))


# A T1 auto-attach needs at least this many shared content tokens. The score
# alone is not enough: a 1–2 token statement fully contained in another yields
# containment 1.0 (a STRONG score) on a single coincidental word, which would
# auto-attach on flimsy evidence. Requiring a substantive overlap keeps the
# deterministic re-root honest ("when unsure, don't") without touching the
# shared thresholds.
_MIN_SHARED_TOKENS_FOR_REATTACH = 2


def _substantive_overlap(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` share enough content tokens that a STRONG score
    reflects real overlap, not a single-word containment artifact."""
    return (
        len(_content_tokens(a) & _content_tokens(b)) >= _MIN_SHARED_TOKENS_FOR_REATTACH
    )


# Hypothesis dedup (INV-36) fails OPEN — deduping DROPS an LLM emission for the
# turn, so its bar is deliberately STRICTER than §7.1.2's reversible fold
# (``_ROOT_DISTINCT_JACCARD`` = 0.6): only a near-verbatim restatement collapses.
# A genuinely-distinct short statement that differs by one substantive token
# (e.g. "memory leak in connection pool" vs "... cache pool" → Jaccard 0.6)
# MUST survive; the actual incident duplicate was verbatim-identical (~1.0).
_HYPOTHESIS_DUPLICATE_JACCARD = 0.8

# Standalone negation cues (apostrophes stripped before matching, so "isn't" →
# "isnt"). "not" is a content stopword, so a hypothesis and its negation
# tokenize IDENTICALLY and would mirror at Jaccard 1.0 — dropping a
# disputing/competing hypothesis as a "duplicate" of what it contradicts is a
# NO-COLLAPSE breach. Used ONLY to REFUSE a dedup on asymmetric polarity (fail
# open); it never causes a dedup.
_NEGATION_CUES = frozenset(
    {
        "not",
        "no",
        "never",
        "without",
        "none",
        "nor",
        "neither",
        "cannot",
        "cant",
        "dont",
        "doesnt",
        "didnt",
        "isnt",
        "arent",
        "wasnt",
        "werent",
        "wont",
        "couldnt",
        "shouldnt",
        "wouldnt",
        "non",
        "unable",
    }
)


def _has_negation(text: str) -> bool:
    """True when the raw statement carries a standalone negation cue. Cheap
    polarity probe for the dedup guard — apostrophes are stripped so contractions
    match, then the text is split on non-alphanumerics."""
    cleaned = "".join(
        c.lower() if c.isalnum() else " " for c in (text or "").replace("'", "")
    )
    return any(w in _NEGATION_CUES for w in cleaned.split())


def _numeric_discriminators(text: str) -> set[str]:
    """Maximal digit runs in the raw statement. ``_content_tokens`` drops
    single-digit tokens (len < 2) and stopwords like "version"/"node", so two
    hypotheses distinguished ONLY by a number ("server 1 down" vs "server 2
    down", "version 5" vs "version 6") tokenize identically and would mirror at
    Jaccard 1.0. Preserving the digit runs lets the dedup keep them distinct."""
    out: set[str] = set()
    cur: list[str] = []
    for c in text or "":
        if c.isdigit():
            cur.append(c)
        elif cur:
            out.add("".join(cur))
            cur = []
    if cur:
        out.add("".join(cur))
    return out


def hypothesis_statements_duplicate(a: str, b: str) -> bool:
    """Two hypothesis statements are DUPLICATES for INV-36 dedup: same polarity,
    same numeric discriminators, AND MUTUAL mirrors at
    ``_HYPOTHESIS_DUPLICATE_JACCARD``.

    The bar is stricter than §7.1.2's fold because deduping DROPS an emission
    rather than holding it — it must fail open. Uses the SYMMETRIC mirror, not
    ``restatement_score``'s containment: a more-SPECIFIC elaboration of a standing
    hypothesis scores high on one-way containment but below the mutual-Jaccard
    bar, so it stays a DISTINCT refinement of the differential. Two fail-open
    guards refuse a dedup the token mirror alone would wrongly accept: a **polarity
    guard** (one statement carries a negation cue the other lacks — a dispute is
    never a duplicate of the claim it contradicts) and a **numeric-discriminator
    guard** (the statements differ by a number the similarity tokenizer drops —
    "server 1" vs "server 2")."""
    if _has_negation(a) != _has_negation(b):
        return False
    if _numeric_discriminators(a) != _numeric_discriminators(b):
        return False
    return _mutual_mirror(
        _content_tokens(a), _content_tokens(b), _HYPOTHESIS_DUPLICATE_JACCARD
    )


def find_duplicate_hypothesis(statement: str, case: "Case") -> str | None:
    """Return the id of a standing hypothesis whose statement duplicates
    ``statement`` (``hypothesis_statements_duplicate``), else ``None`` — the
    INV-36 dedup predicate for ``hypotheses_to_add``.

    Same-batch duplicates are caught for free: the apply loop inserts each minted
    hypothesis into ``case.hypotheses`` before the next item is checked, so an
    earlier sibling this turn is already in the scanned set. Terminal
    (``REFUTED``/``RETIRED``) hypotheses are deliberately NOT dedup targets:
    terminal states are immutable (``_apply_hypothesis_updates`` refuses changes
    and instructs "open a NEW hypothesis if that theory is back in play"), so
    deduping against them would DEADLOCK the revival — the re-mint refused here
    and the update refused there, with contradictory guidance. The gate-inflation
    vector is duplicate ACTIVE/CAPTURED records; a revival minting a fresh
    hypothesis is legitimate diagnostic work, not spurious inflation. The caller
    surfaces the matched id to the LLM so a genuine re-examination updates the
    standing hypothesis rather than cloning it."""
    for hid, hyp in case.hypotheses.items():
        if hyp.state.is_terminal:
            continue
        if hypothesis_statements_duplicate(statement, hyp.statement):
            return hid
    return None


def _referenced_node_ids(case: Case) -> set[str]:
    """Every node id that lies on some hypothesis path or is a hypothesis root —
    the single definition of "load-bearing" used by both the GC and the
    orphan-resolution post-pass."""
    referenced: set[str] = set()
    for h in case.hypotheses.values():
        if h.root_node_id:
            referenced.add(h.root_node_id)
        referenced.update(h.path or [])
    return referenced


def prune_abandoned_nodes(case: Case, abandoned_node_ids: list[str]) -> None:
    """Drop the nodes of a chain abandoned by a hypothesis re-root, but only the
    ones now dead — referenced by no hypothesis (as a root or on a path). The
    PROBLEM node D is never collected (it anchors every chain). Edges are pruned
    to those whose endpoints both survive, so a surviving node's connectivity is
    never severed. No-op for any node still load-bearing for another hypothesis.
    """
    referenced = _referenced_node_ids(case)
    for node_id in abandoned_node_ids:
        node = case.causal_nodes.get(node_id)
        if node is None or node.node_type == NodeType.PROBLEM:
            continue  # keep D; skip already-gone
        if node_id in referenced:
            continue  # still load-bearing for some hypothesis
        case.causal_nodes.pop(node_id, None)
    case.causal_edges[:] = [
        e
        for e in case.causal_edges
        if e.cause_node_id in case.causal_nodes
        and e.effect_node_id in case.causal_nodes
    ]


def _hypothesis_lacks_real_chain(hyp: "Hypothesis") -> bool:
    """True when the hypothesis is flat or carries only a degenerate stub (a
    2-node root->D path). Re-attaching only such a hypothesis avoids clobbering
    one that already owns a real multi-rung chain — that case is a genuine
    separate representation, left for an LLM nudge instead."""
    return not hyp.path or len(hyp.path) <= 2


def resolve_orphan_chains(case: Case) -> list[dict]:
    """Resolve emitted chains the LLM left unlinked (the invariant: every chain
    explaining D attaches to exactly one hypothesis). Run AFTER chain ingest.

    For each ORPHAN root (a ROOT node on no hypothesis path, anchoring a chain
    that reaches D), score its statement against every hypothesis:

    - **T1 — deterministic re-attach.** Exactly one hypothesis restates it
      ``>= RESTATEMENT_STRONG`` with no other ``>= RESTATEMENT_AMBIGUOUS``, and
      that hypothesis lacks a real chain of its own: re-root it onto the orphan
      chain and GC its abandoned stub. Mutates the graph in place.
    - **T2a — ambiguous.** The orphan restates a hypothesis but not
      unambiguously (best in ``[AMBIGUOUS, STRONG)``, or two-plus hypotheses
      ``>= AMBIGUOUS``, or the sole strong match already owns a real chain):
      returned for the caller to surface as a one-turn LLM nudge. NOT
      auto-resolved.
    - **benign.** Matches no hypothesis (best ``< AMBIGUOUS``): left as a
      standalone candidate root ("an unexplained candidate root is fine").

    Returns the ambiguous orphans (each ``{root_id, statement,
    candidate_hypotheses}``) for T2a; T1 re-attachments are applied in place.
    """
    if not any(n.node_type == NodeType.PROBLEM for n in case.causal_nodes.values()):
        return []

    # ``referenced`` is the set of load-bearing nodes; it only changes when a T1
    # re-attach below mutates a hypothesis path, so compute it once and refresh
    # it only after an actual re-attach (not every iteration). Snapshot the
    # orphan-root candidates up front; a prior re-attach may GC or adopt a later
    # candidate, so re-check existence/membership per root.
    referenced = _referenced_node_ids(case)
    orphan_root_ids = [
        nid
        for nid, n in case.causal_nodes.items()
        if n.node_type == NodeType.ROOT and nid not in referenced
    ]

    # Hypotheses already re-rooted in THIS pass — excluded from later scoring so
    # one orphan cannot re-attach a hypothesis a previous orphan already took
    # (no churn), and an already-taken hypothesis cannot inflate another orphan's
    # ambiguity count and wrongly downgrade its clean match to a nudge.
    adopted: set[str] = set()
    ambiguous: list[dict] = []
    for root_id in orphan_root_ids:
        node = case.causal_nodes.get(root_id)
        if node is None or root_id in referenced:
            continue  # GC'd or adopted onto a path by a prior re-attach
        path = chain_path_to_problem(root_id, case)
        if not path:
            continue  # open chain not yet anchored to D — leave it

        scored = [
            (s, h)
            for h in case.hypotheses.values()
            if h.hypothesis_id not in adopted
            and (s := restatement_score(node.statement, h.statement))
            >= RESTATEMENT_AMBIGUOUS
        ]
        if not scored:
            continue  # benign standalone candidate root

        # T1: exactly one hypothesis matches (so the sole match is also the only
        # one >= STRONG, since STRONG > AMBIGUOUS), it owns no real chain, and the
        # overlap is substantive (not a single-word containment artifact).
        if (
            len(scored) == 1
            and scored[0][0] >= RESTATEMENT_STRONG
            and _hypothesis_lacks_real_chain(scored[0][1])
            and _substantive_overlap(node.statement, scored[0][1].statement)
        ):
            hyp = scored[0][1]
            old_path = hyp.path or []
            hyp.root_node_id = root_id
            hyp.path = path
            prune_abandoned_nodes(case, old_path)
            adopted.add(hyp.hypothesis_id)
            referenced = _referenced_node_ids(case)  # graph changed — refresh
            continue

        # T2a: matched but ambiguous (or the strong match already owns a chain).
        ambiguous.append(
            {
                "root_id": root_id,
                "statement": node.statement,
                "candidate_hypotheses": [
                    h.statement
                    for _, h in sorted(scored, key=lambda sh: sh[0], reverse=True)
                ],
            }
        )
    return ambiguous


# ---------------------------------------------------------------------------
# Hook registration (inversion seam — see cause_assurance.register_graph_hooks)
# ---------------------------------------------------------------------------

register_graph_hooks(
    support_count_held_root_ids=support_count_held_root_ids,
    derive_node_states=derive_node_states,
    sole_cluster_origin=sole_cluster_origin,
    project_hypothesis_states_from_roots=project_hypothesis_states_from_roots,
    conjuncts_for_chain=conjuncts_for_chain,
)
