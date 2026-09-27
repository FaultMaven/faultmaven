from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from faultmaven.core.investigation.cause_assurance import (
    CAUSAL_STANCE_CONFIDENCE_MIN,
    counterfactual_link_decisive,
    root_counterfactually_confirmed,
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
from faultmaven.modules.case.contracts import (
    CausalNode,
    EvidenceCategory,
    EvidenceStance,
    NodeState,
    NodeType,
)

from .projection import _standing_hypotheses
from .queries import and_constraints_satisfied
from .similarity import _FRAME_OWNER_JACCARD, ROOT_NOVELTY_MIN_FRACTION, _mutual_mirror

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case


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
# Not a grounding-bar reason and NEVER produced by ``root_support_block_reasons``
# (whose population is the causal-grounding bar alone): the §7.1 RESTATEMENT
# guard's own label, produced by ``restatement_held_root_ids`` and overlaid by
# the context annotation so a restatement hold renders its recovery action
# instead of a bare ``[root/inconclusive]`` line. Kept beside its siblings
# because they share ONE consumer and one recovery-note map.
BLOCK_REASON_RESTATEMENT = "restates_frame"  # would validate but for the guard

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


def restatement_held_root_ids(case: Case) -> set[str]:
    """ROOT node ids that clear EVERY validation bar — causally grounded, net
    supporting, AND-gate satisfied, not refuted — and are held at INCONCLUSIVE
    by the §7.1 restatement guard ALONE.

    The STANDING form of that hold. ``root_validation_blocked_restatement_total``
    counts block EVENTS (state transitions), so a root already INCONCLUSIVE from
    generic evidence that later clears the grounding bar and lands on this guard
    transitions nowhere and is never counted — the hold then has no observable
    at all, in metrics or in the prompt. That is how fm#1137 cost a database
    read to localise, and why the model, shown a bare ``[root/inconclusive]``
    beside three confident causal supports, kept collecting evidence for nine
    turns against a bar that evidence cannot move.

    Deliberately NOT merged into ``root_support_block_reasons``: that predicate
    is the causal-grounding bar's population and feeds
    ``support_count_held_root_ids`` (the resolution confirm-stamp and the
    anti-anchoring exemption). A restating root must never reach those — its
    recovery is to state a real mechanism, which no amount of confirmation
    supplies.
    """
    nodes = case.causal_nodes
    if not nodes:
        return set()
    # Same cheap eligibility pre-scan as ``root_support_block_reasons``, for the
    # same reason: the common late-investigation state (every ROOT already
    # settled) must not pay a tokenization sweep per prompt build for an empty
    # answer. Without it this cost ~15x on a settled 40-root graph.
    if not any(
        n.node_type == NodeType.ROOT
        and n.node_state not in (NodeState.VALIDATED, NodeState.REFUTED)
        for n in nodes.values()
    ):
        return set()
    restating = _restating_root_ids(case)
    if not restating:
        return set()
    evidence_by_id = _evidence_category_map(case)
    tokens = _causal_evidence_tokens(case)
    held: set[str] = set()
    for node_id in restating:
        node = nodes.get(node_id)
        if node is None or node.node_state in (NodeState.VALIDATED, NodeState.REFUTED):
            continue
        (
            supports,
            refutes,
            causal_support_ev_ids,
            _raw_causal_links,
            counterfactual_refutes,
        ) = _node_evidence_tally(node, evidence_by_id)
        if counterfactual_refutes >= 1 or refutes >= supports:
            continue  # refuted territory / no net support — a different story
        if not and_constraints_satisfied(node.node_id, nodes, case.causal_edges):
            continue  # blocked by the AND-gate, not by the guard
        grounded = (
            _support_block_reason(causal_support_ev_ids, tokens) is None
        ) or root_counterfactually_confirmed(node, evidence_by_id)
        if grounded:
            held.add(node_id)
    return held


class RestatementHold(NamedTuple):
    """What the §7.1 restatement hold looks like on ONE case — computed in a
    single sweep because three consumers need different facets of it and each
    would otherwise re-tokenize the graph (#1195 review).

    - ``root_ids`` — the held ROOTs (``restatement_held_root_ids``).
    - ``is_sole_root_block`` — whether EVERY ROOT the case has not put to rest
      is held this way. This is what licenses the claim "more supporting
      evidence will not move this forward": it is true of a held root, and false
      of the case as a whole the moment some OTHER root is blocked by something
      evidence *can* move (no causal link yet, the independence count, a hedged
      support). The semantic form was chosen over a literal "exactly one
      unsettled root" (``confirm_root_from_resolution_absence``'s spelling): two
      roots BOTH held by the guard are still a case no amount of data advances,
      and refusing the carve-out there would re-report it as an evidence
      deficiency.

      Only ``REFUTED`` puts a root to rest for this purpose. A ``VALIDATED``
      root is emphatically NOT settled-and-irrelevant: it means the case HAS a
      promoted cause, so the hold on some sibling is not what governs the case
      at all, and any consumer claiming "a cause was supported but never stated
      distinctly" would be describing a case that HAS a stated cause. The
      disposition path is shielded from that by ``_is_grounded``; the closure
      path was not, and read the hold as governing (#1195 review).
    - ``involves_siblings`` — whether any held root is released by dropping the
      OTHER standing hypotheses from its frame. False means the hold is
      **anchor-only**: the root restates the problem statement itself, and no two
      hypotheses need overlap for that (``_node_restates`` unions anchors with
      the sibling statements). The distinction is what keeps the user-facing
      recovery from asserting an overlap that does not exist.
    """

    root_ids: frozenset[str]
    is_sole_root_block: bool
    involves_siblings: bool


def summarize_restatement_hold(case: Case) -> "RestatementHold | None":
    """The §7.1 restatement hold as the disposition layer needs to see it, or
    ``None`` when no root is held.

    Registered on the ``cause_assurance`` graph-hook seam as
    ``restatement_hold`` and read by ``verification_status`` (the
    ``RESTATEMENT_HELD`` cell), ``milestone_engine`` (which of the two recovery
    moves to offer) and ``terminal_transitions`` (the closure reason) — one
    sweep, one answer, so those three can never describe the same case
    differently.
    """
    held = restatement_held_root_ids(case)
    if not held:
        return None
    # Cheap and tokenization-free: every ROOT the case has NOT put to rest.
    # REFUTED only — a VALIDATED root is a promoted cause, and its presence is
    # exactly what must make ``is_sole_root_block`` False (see the docstring).
    # ``restatement_held_root_ids`` excludes both VALIDATED and REFUTED, so
    # ``held`` is a subset of this and the comparison reads "is anything else
    # unresolved, or promoted".
    unresolved = {
        n.node_id
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT and n.node_state != NodeState.REFUTED
    }
    anchors, _hyp_token_sets = _frame_components(case)
    involves_siblings = False
    for node_id in held:
        node = case.causal_nodes.get(node_id)
        statement_tokens = _content_tokens(node.statement) if node else set()
        if not statement_tokens:
            continue
        # Re-run the novelty core with the sibling statements REMOVED. If the
        # root no longer restates, the siblings were load-bearing; if it still
        # does, the problem anchors alone hold it and there is no overlap to
        # ask the user about. (An empty anchor set reads as "released", which is
        # right: the frame was then nothing but siblings.)
        if not _node_restates(statement_tokens, node_id, anchors, []):
            involves_siblings = True
            break
    return RestatementHold(
        root_ids=frozenset(held),
        is_sole_root_block=(unresolved == held),
        involves_siblings=involves_siblings,
    )


def root_restates_case_frame(node: "CausalNode", case: Case) -> bool:
    """§7.1 restatement guard predicate (single-node form; ``_restating_root_ids``
    is the batch form — keep their semantics identical): a ROOT whose statement
    carries less than ``ROOT_NOVELTY_MIN_FRACTION`` novel content tokens beyond
    the case frame is a restatement, not an explanation, and no validation lane
    may ADMIT it (the guard is an ENTRY bar on validation/minting, not a
    standing predicate — see ``derive_node_states``).

    The frame = problem anchors + OTHER standing hypotheses' statements. A
    hypothesis is excluded from a node's frame — it is not "other", it is that
    node's OWN cause — when it is ATTACHED to the node (``root_node_id`` match),
    or when it is unattached and MUTUALLY mirrors the node (Jaccard ≥
    ``_FRAME_OWNER_JACCARD``) — the attachment lag. Deliberately mutual:
    one-way containment does NOT make an owner in EITHER direction. A #656
    disjunction root is contained in each VERBOSE sibling it OR-s, and each
    TERSE sibling is contained in the root; both readings would excuse the
    incident shape, so neither is used (fm#1137 review).

    Non-novelty is then ATTRIBUTED (fm#1122, ``_node_restates``): a root held
    by the anchors alone is the symptom dressed as a cause and stays held; a
    root whose whole deficit is attributable to ONE standing hypothesis — which
    must also be the root's PRINCIPAL source, covering at least as much of it as
    the anchors do — is that hypothesis's own cause said twice, a duplicate,
    released; and a root that is non-novel only against the UNION of several
    standing causes is aggregating the case's open candidates, which is the #656
    shape and stays held. This replaces the fm#1137 known limit (a standing duplicate framing
    its own root and holding it at INCONCLUSIVE indefinitely, with "collect
    more evidence" as the only advice against a bar evidence cannot move). It
    is a quantifier, not a threshold: fm#1137/#1140 swept "how much does one
    sibling cover" and found the classes overlap, so the question asked here is
    whether ONE sibling accounts for ALL of it — which a disjunct cannot do for
    a disjunction.

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
    """Novelty core shared by the single-node and batch forms.

    Two questions, in order. **Is the root novel at all** against the frame
    (problem anchors + other standing hypotheses)? If yes it is not a
    restatement and nothing else matters. If no, **is the case really saying
    this already, or is one claim saying it twice** — the §7.1 ATTRIBUTION test
    below.
    """
    elements = []
    for rid, tokens in hyp_token_sets:
        if rid == node_id:
            continue  # attached own hypothesis
        if rid is None and _mutual_mirror(
            statement_tokens, tokens, _FRAME_OWNER_JACCARD
        ):
            continue  # unattached presumptive owner (attachment lag)
        elements.append(tokens)
    frame = set(anchors)
    for tokens in elements:
        frame |= tokens
    if not frame:
        return False  # no frame to restate — the guard is inert
    residue = statement_tokens - frame
    if len(residue) / len(statement_tokens) >= ROOT_NOVELTY_MIN_FRACTION:
        return False  # novel against the frame — not a restatement
    # §7.1 ATTRIBUTION (fm#1122). "The case already says this" has to be
    # attributable to something the case actually CLAIMS. The problem anchors
    # are such a claim on their own — a root they alone cover is the symptom
    # dressed as a cause, and no sibling excuses it, so that arm is decided
    # first and unconditionally.
    if len(statement_tokens - anchors) / len(statement_tokens) < (
        ROOT_NOVELTY_MIN_FRACTION
    ):
        return True
    # Otherwise the hold comes from the hypotheses, and there are two ways it
    # can: the root's content is DISTRIBUTED across several standing causes, or
    # ONE of them accounts for the whole of it.
    #
    #   * Distributed — no single sibling covers what the frame covers, because
    #     each is missing the others' distinctive content. That is a root
    #     AGGREGATING the case's open candidates ("X or Y causing D") rather
    #     than explaining any of them: the #656 shape, and the thing this guard
    #     exists to refuse. Held.
    #   * Attributable to one — the root says nothing the problem statement
    #     plus that ONE hypothesis does not already say. The two are the same
    #     claim, worded once as a terse cause node and once as a chain
    #     narrative. That is a DUPLICATE, which is graph hygiene (one cause,
    #     one chain — fm#1091 / the orphan re-attach path), not a restatement
    #     of the case frame. Holding it at INCONCLUSIVE fixes nothing and costs
    #     everything: the recovery for a restating root is "collect more
    #     evidence", and no evidence can move a bar that measures wording.
    #     Released — the root is judged on its evidence like any other.
    #
    # Deliberately a QUANTIFIER, not a threshold. fm#1137/#1140 swept "how much
    # does one sibling cover" and found no separator: the incident duplicate
    # covers 0.889 of its root and a verbose #656 disjunct covers 0.667, with
    # mutual Jaccard actually ordering them the wrong way round (0.276 vs
    # 0.286). "Does ONE sibling account for ALL of it" is categorical, and a
    # disjunct cannot account for a disjunction.
    #
    # Strictly a RELAXATION of the novelty test above: every arm that returns
    # True here is a case the plain frame test also called a restatement, so
    # this can only release a held root, never hold a new one. The corpus FP
    # bound and the dilution bound are therefore safe by construction
    # (``test_attribution_only_releases_never_holds``).
    #
    # ``elements``, not ``hyp_token_sets``: the root's OWN hypothesis is out of
    # the frame because it is not "other", and letting it attribute the frame's
    # coverage away is the fm#1140 attempt-2 collapse — pinned by
    # ``test_a_roots_own_hypothesis_cannot_attribute_the_frame_away``.
    #
    # ``anchors | tokens`` is a subset of ``frame``, so its residue is always a
    # SUPERSET of ``residue``; comparing the two by length is therefore the same
    # predicate as comparing them by identity (a known equivalent mutant — do
    # not read a surviving length-based mutation as a missing pin).
    anchor_cover = statement_tokens & anchors
    for tokens in elements:
        if statement_tokens - (anchors | tokens) != residue:
            continue
        # PRINCIPAL SOURCE (fm#1122 review). Subtracting the anchors is what
        # makes the residue test above blind to a DISJUNCT the problem
        # statement happens to pre-name — the #661 contaminated-anchor class,
        # which is 6 of the 7 roots still held in the dev corpus. Delete that
        # disjunct along with the anchors and the remaining fragment is covered
        # by the other disjunct alone, so a DISTRIBUTED aggregation presents as
        # attributable-to-one and a #656 root releases. Review caught exactly
        # that, reproduced on both trees.
        #
        # So the attributing claim must be where most of the root actually
        # comes from, measured on the root's FULL token set rather than on its
        # non-anchor remainder: a root is a hypothesis's duplicate only when
        # that hypothesis, not the problem statement, is its principal source.
        # When the problem statement supplies more, the root is leaning on the
        # problem framing — or on cause content smuggled into it — which is the
        # restatement shape this guard exists for.
        #
        # A comparison between two measured quantities, not a new constant.
        if len(statement_tokens & tokens) < len(anchor_cover):
            continue
        return False
    return True


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
