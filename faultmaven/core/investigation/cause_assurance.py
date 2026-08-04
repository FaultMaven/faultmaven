"""Assurance grading for an identified cause (pure, contracts-only).

Lives apart from ``causal_graph`` deliberately: consumers include the terminal
runbook-harvest gate (``terminal_transitions``), and ``causal_graph`` already
pulls in ``hypothesis_manager`` which pulls in ``terminal_transitions`` — so
putting this in ``causal_graph`` and importing it back would close an import
cycle. Keeping it here (it needs nothing from ``causal_graph``, only the case
contracts) breaks that back-edge.

``grade_cause_assurance`` is the single source of truth: it classifies a case
into one of three mutually-exclusive assurance grades — the M2 confirmation
ladder — in one pass. The §7 harvest bar is ``CONFIRMED`` (counterfactual
confirmation, gone⇒gone). Validation method (empirical vs deductive) does NOT
raise the grade: both are mechanistic per M2/§7.1.1 — a deductive derivation
is itself assembled from LLM-mediated refutations plus an asserted-exhaustive
differential, so only the counterfactual outcome of actually removing the
cause clears the top bar.

The ``CauseAssuranceGrade`` enum lives in the domain layer
(``modules.case.contracts``) so it can be a persisted field on
``InvestigationProgress`` (the ``VerificationStatus`` precedent); this module
imports and re-exports it.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from faultmaven.core.investigation.lifecycle_metrics import (
    absence_confirmation_bearing_rejected_total,
    absence_row_link_refused_total,
)
from faultmaven.modules.case.contracts import (
    CauseAssuranceGrade,
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceStance,
    HypothesisState,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    RootCauseConclusion,
    SolutionOutcome,
    classify_solution_outcome,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, CausalNode

__all__ = [
    "CONFIRMED_RCC_LIKELIHOOD_FLOOR",
    "ENGINE_EVIDENCE_AUTHOR",
    "ENGINE_RCC_AUTHOR",
    "MECHANISTIC_RCC_LIKELIHOOD",
    "CauseAssuranceGrade",
    "absence_row_link_refused",
    "cached_content_tokens",
    "conclusion_overclaims",
    "confirm_root_from_resolution_absence",
    "content_tokens",
    "counterfactual_link_decisive",
    "evidence_category_map",
    "grade_cause_assurance",
    "has_actionable_solution",
    "has_problem_definition",
    "has_resolution_confirmation",
    "has_root_cause_record",
    "latest_disconfirmation_turn",
    "problem_anchor_statements",
    "resolution_confirmation_rows",
    "root_counterfactually_confirmed",
    "runbook_conversion_ready",
]

# The marker on an engine-synthesized RootCauseConclusion (§9.3) — distinguishes
# the engine's faithful mirror (which may be refreshed/retired) from the LLM's
# own authored conclusion (which always wins and is never overwritten). Defined
# here (grade semantics, contracts-only) so both ``causal_graph`` (the mint) and
# the terminal confirm-stamp below can share it without an import cycle.
ENGINE_RCC_AUTHOR = "engine:chain_validation"

# The ``collected_by`` marker on ENGINE-authored Evidence rows (the M6
# failed-fix disconfirmation mint is the only producer). The entire
# disconfirmation/confirmation split below keys on this authorship — a typo'd
# literal at any read or write site would silently let a failed fix read as a
# resolution confirmation, so the mint (``causal_graph._attach_engine_refutation``)
# and every reader share this one constant (the ``ENGINE_RCC_AUTHOR`` precedent).
ENGINE_EVIDENCE_AUTHOR = "engine"

# M2 confidence vocabulary (§0 M2, §7.2): a validated-but-unconfirmed root is
# MECHANISTIC grade — the engine-synthesized conclusion reads CONFIDENT at a
# FIXED likelihood (a cap, not a floor: the LLM's own higher
# root_cause_likelihood must not leak a mechanistic cause into "verified").
# Only a counterfactually CONFIRMED root (causal_absence SUPPORTS on the root,
# gone⇒gone) reads VERIFIED, floored at 0.9.
MECHANISTIC_RCC_LIKELIHOOD = 0.8
CONFIRMED_RCC_LIKELIHOOD_FLOOR = 0.9

# §7.1 (INV-29): a stance the LLM itself declares at confidence below this bar
# is a hedge, not grounding. Read by BOTH belief axes so they cannot drift: the
# chain tally (``causal_graph._node_evidence_tally`` — a hedged SUPPORTS link
# is not CAUSAL grounding) and the flat prior cap
# (``hypothesis_manager.update_hypothesis_likelihood`` — a hedged link does not
# lift the evidence-free cap). Lives here (the shared leaf) because
# causal_graph imports hypothesis_manager: neither could import it from the
# other. Absent/None reads as unset -> full confidence; an EXPLICIT 0.0 stays
# filtered.
CAUSAL_STANCE_CONFIDENCE_MIN = 0.6

# Hypothesis states that keep a chain's root a STANDING cause (mirrors
# ``causal_graph._STANDING_HYP_STATES`` — kept literal here to avoid the
# import cycle; the two must not drift).
_STANDING_HYP_STATES = (HypothesisState.ACTIVE, HypothesisState.VALIDATED)


logger = logging.getLogger(__name__)


def absence_row_link_refused(
    category,
    stance,
    *,
    axis: str,
    evidence_id: str | None = None,
    node_or_hypothesis_id: str | None = None,
    case_id: str | None = None,
    turn: int | None = None,
) -> bool:
    """The M2 trust boundary (#987): is this LLM-emitted evidence link refused?

    TRUE iff the backing evidence row is ``causal_absence_evidence`` — whatever
    the stance, whichever axis. Absence rows are STAND-ALONE audit records and
    carry NO model-authored stance; every counterfactual link on one is
    engine-minted (the resolution confirm-stamp's SUPPORTS,
    ``causal_graph._attach_engine_refutation``'s REFUTES).

    **The invariant is a property of the EVIDENCE CATEGORY, not of the link
    target.** That is why this predicate exists at all rather than being
    inlined: the two belief axes — chain ``node_evidence_links``
    (``causal_graph.ingest_emitted_chain``) and flat ``hypothesis_evidence_links``
    (``milestone_engine._apply_hypothesis_evidence_links``) — are separate entry
    points into the same truth, so a rule enforced on one is a rule a single
    stance choice routes around. Both call THIS function; neither owns a copy.

    Why REFUTES is refused, not just SUPPORTS (the pre-#987 boundary): the
    prompt contract emits ``causal_absence_evidence`` ONLY for a CONFIRMED fix
    (templates.py, TREATMENT "EVIDENCE TYPES FOR THIS STAGE"), and its FAILURE
    PATH explicitly forbids an absence row for a failed fix — "a failure is not
    an 'absence' (the cause persists)" — recording that outcome by REFUTING the
    hypothesis instead. So an absence-REFUTES link is never a sanctioned
    emission, and reading one as a failed-fix disconfirmation inverts the row's
    own meaning: in #987 the LLM's SUCCESS-confirmation row ("post-fix
    authentication succeeded") was REFUTES-linked to the true root, which drove
    that root to REFUTED at belief 0, fired M6, retracted the conclusion, and
    left a RESOLVED case asserting no cause was ever known. The old "accept
    REFUTES to feed M6" rationale was self-refuting: under its own contract
    there is no failed-fix absence row to feed M6 with. M6's sanctioned trigger
    — the hypothesis REFUTED with a refutation_reason — is untouched by this.

    Refusal is METERED AND LOGGED, never silent: the strip hides the emission,
    so without the counter a model that routinely mis-links absence rows is
    indistinguishable from one that follows the contract. This is a
    prompt-adherence signal, not a truth signal — the engine is correct either
    way.

    ``category`` is the row's ``EvidenceCategory`` (a missing/dangling row reads
    ``None`` and is NOT refused here — the callers already drop unresolvable
    evidence refs). The identifiers are for the log line only.
    """
    if category != EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE:
        return False
    stance_label = str(getattr(stance, "value", stance) or "unset").lower()
    absence_row_link_refused_total.labels(axis=axis, stance=stance_label).inc()
    logger.warning(
        "Absence-row link refused (%s axis): case=%s turn=%s evidence=%s "
        "target=%s stance=%s — causal_absence rows are stand-alone audit "
        "records; counterfactual links on them are engine-minted only (#987)",
        axis,
        case_id,
        turn,
        evidence_id,
        node_or_hypothesis_id,
        stance_label,
        extra={
            "event": "absence_row_link_refused",
            "axis": axis,
            "case_id": case_id,
            "turn": turn,
            "evidence_id": evidence_id,
            "target_id": node_or_hypothesis_id,
            "stance": stance_label,
        },
    )
    return True


def evidence_category_map(case: "Case") -> dict:
    """The one ``evidence_id → category`` map every assurance/tally reader
    shares (this module and ``causal_graph`` import it from here), so the
    "dangling evidence_id is ignored, never assumed" discipline has a single
    owner — parallel hand-written comprehensions would let a future filter or
    key change reach some readers and not others, silently splitting the M2
    grade from node-state derivation."""
    return {e.evidence_id: getattr(e, "category", None) for e in case.evidence}


# ---------------------------------------------------------------------------
# Statement/content tokenization — the ONE lexical vocabulary every comparison
# reader shares (restatement guard, evidence-independence mirror, orphan
# re-attach, and the absence-bearing check below). Lives here (the shared leaf)
# because both ``causal_graph`` and this module need it and causal_graph
# imports this module; causal_graph re-binds the private aliases so its call
# sites and the calibration tests keep their names.
# ---------------------------------------------------------------------------

# Function/filler words dropped before comparing two statements. This is the
# GENERAL base list; the sim analyzer may EXTEND it with scenario-specific noise
# words (e.g. a recurring service name) for its own runs — those deliberately
# stay out of the engine, which must not bake any one scenario's vocabulary in.
_RESTATEMENT_STOPWORDS = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "of",
    "to",
    "in",
    "on",
    "at",
    "for",
    "and",
    "or",
    "that",
    "this",
    "it",
    "its",
    "by",
    "with",
    "as",
    "from",
    "has",
    "have",
    "had",
    "be",
    "been",
    "into",
    "not",
    "but",
    "which",
    "when",
    "then",
    "so",
    "new",
    "version",
    "service",
    "application",
}


def _stem(token: str) -> str:
    """Collapse common English inflections so morphological variants of the same
    word match (``leaks``/``leaking`` → ``leak``; ``connections`` → ``connection``;
    ``caches`` → ``cache``). Deliberately CONSERVATIVE so it cannot merge UNRELATED
    words into a coincidental shared token (which could push an orphan past the T1
    STRONG + 2-token guard into a wrong auto-attach — the campaign's
    NO-INCORRECT-CONCLUSION line):

    - ``-ing``/``-ed`` strip only when the stem is 4+ chars, so short silent-e
      collisions never form (``caring``→``caring`` not ``car``; ``coding`` stays).
    - plurals strip a SINGLE trailing ``-s`` (Porter step-1a style), so silent-e
      nouns keep their ``e`` (``caches``→``cache``, ``nodes``→``node``) and
      ``-ss``/``-us``/``-is`` (``process``, ``status``, ``basis``) are left alone —
      that ``-s`` is not a plural marker.

    Single-``-s`` stripping cannot merge two *unrelated* words (they would have to
    differ only by a trailing ``s``, i.e. be the same word). Tokens with non-alpha
    chars (ids, dotted names, paths) are left untouched."""
    if not token.isalpha() or len(token) <= 3:
        return token
    for suf in ("ing", "ed"):
        if token.endswith(suf) and len(token) - len(suf) >= 4:
            return token[: -len(suf)]
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if (
        token.endswith("s")
        and not token.endswith(("ss", "us", "is"))
        and len(token) - 1 >= 3
    ):
        return token[:-1]
    return token


def content_tokens(text: str) -> set[str]:
    """Lowercased, STEMMED content tokens (filler words dropped) for comparing
    whether two statements describe the same cause. Stopwords are dropped before
    stemming so the stopword list stays readable (plain words)."""
    raw = "".join(
        c.lower() if (c.isalnum() or c in ".-_:/") else " " for c in (text or "")
    )
    return {
        _stem(t) for t in raw.split() if len(t) >= 2 and t not in _RESTATEMENT_STOPWORDS
    }


# Tokenization bound for evidence CONTENT comparison: ``summary`` is capped at
# 500 chars by the model but ``extract`` is an UNBOUNDED verbatim slice — the
# per-character tokenizer over a 20KB log extract, swept several times per turn
# on the async path, is the event-loop-blocking shape that produced the cloud
# liveness kills (#651). The first 4000 chars carry ample discriminating signal
# for a Jaccard comparison.
_TOKENIZE_MAX_CHARS = 4000


@lru_cache(maxsize=4096)
def cached_content_tokens(text: str) -> frozenset[str]:
    """Memoized tokenizer for immutable evidence content: the same rows are
    re-tokenized by every derive pass, every context build, and every
    count-held consult — identical text always yields identical tokens, so
    pay the per-character cost once per row, not per sweep."""
    return frozenset(content_tokens(text[:_TOKENIZE_MAX_CHARS]))


def problem_anchor_statements(case: "Case") -> list[str]:
    """The case's problem-frame statements: the PROBLEM node D's statement plus
    the verified symptom statement (they can differ — D is chain-anchored, the
    symptom is inquiry-anchored). Shared by the §7.1 restatement guard (the
    statements a ROOT must add explanatory depth OVER) and the absence-bearing
    frame below. Empty when neither exists (both consumers are then inert)."""
    anchors = [
        n.statement
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.PROBLEM and n.statement
    ]
    symptom = getattr(
        getattr(case, "problem_verification", None), "symptom_statement", None
    )
    if symptom:
        anchors.append(symptom)
    return anchors


def counterfactual_link_decisive(link) -> bool:
    """Whether an absence-backed REFUTES link carries DECISIVE (§7.2 strongest
    grade) force: declared at ``stance_confidence >= CAUSAL_STANCE_CONFIDENCE_MIN``.
    The refute-side twin of the §7.1 support filter (INV-29): every stance is an
    LLM self-claim, and a self-HEDGED counterfactual must not single-handedly
    refute a node, zero a sibling's belief for proof-by-exclusion (§7.1.1 guard
    #3 — a hedged refute enabling a deductive validation of the survivor is a
    conclusion-grade action), or demote the identified cause (M6). A hedged
    absence-REFUTES still counts as ORDINARY refuting evidence (it feeds
    ``refutes > supports`` and ``_net_refuted``) — the decisive single-shot
    power is what the confidence bar gates. ``None`` reads as unset → full
    confidence (the engine's own M6 links carry no declared confidence and are
    decisive by construction); an EXPLICIT 0.0 stays filtered."""
    confidence = getattr(link, "stance_confidence", None)
    if confidence is None:
        confidence = 1.0
    return confidence >= CAUSAL_STANCE_CONFIDENCE_MIN


def _validated_roots(case: "Case") -> list["CausalNode"]:
    """The case's VALIDATED root nodes — the only harvest-relevant unit (§7 never
    harvests an intermediate rung or a candidate)."""
    return [
        n
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT and n.node_state == NodeState.VALIDATED
    ]


def root_counterfactually_confirmed(
    node: "CausalNode", evidence_category_by_id: dict
) -> bool:
    """M2 counterfactual confirmation, per node: a SUPPORTS evidence link backed
    by a ``causal_absence_evidence`` row — the cause was removed and the problem
    went with it (gone⇒gone). The confirmation must be LINKED to this node: a
    case-level absence row with no bearing on the root does not confirm it (the
    same bearing discipline as ``_node_evidence_tally``'s counterfactual-refute
    arm). A dangling ``evidence_id`` is ignored, never assumed. The only live
    producer of such a link is the resolution confirm-stamp below — the LLM
    chain-emission ingest strips SUPPORTS-on-absence links (see
    ``causal_graph.ingest_emitted_chain``), so this predicate cannot be
    satisfied by an LLM self-claim."""
    return any(
        link.stance == EvidenceStance.SUPPORTS
        and evidence_category_by_id.get(link.evidence_id)
        == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        for link in node.evidence_links
    )


# ---------------------------------------------------------------------------
# Resolution-confirmation row qualification (§9.5) — the ONE definition of
# "an absence row that can stand as a resolution confirmation", shared by the
# resolution-readiness gate, the closure→resolve pivot (both in
# ``terminal_transitions``) and the confirm-stamp's candidate filter below.
# Before this was shared, the stamp carried the discipline and the gate did
# not: the gate read READY off the mere existence of ANY causal_absence row —
# including the ENGINE's own M6 failed-fix DISCONFIRMATION rows (minted by
# ``causal_graph._attach_engine_refutation``), so a failed fix satisfied the
# gate's "confirmation the problem is now resolved" (#656).
# ---------------------------------------------------------------------------


def _engine_absence_row_ids(case: "Case") -> set:
    """Ids of ENGINE-authored causal_absence rows — the M6 failed-fix
    disconfirmation markers (``_attach_engine_refutation`` is the only
    producer, and it mints exactly one per counterfactual disconfirmation of
    a grounded cause)."""
    return {
        getattr(e, "evidence_id", None)
        for e in (getattr(case, "evidence", None) or [])
        if getattr(e, "category", None) == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        and getattr(e, "collected_by", None) == ENGINE_EVIDENCE_AUTHOR
    }


def latest_disconfirmation_turn(case: "Case") -> int:
    """The turn of the newest ENGINE-KNOWN failed-fix disconfirmation: the max
    ``collected_at_turn`` over ENGINE-authored causal_absence rows. ``-1``
    when none.

    AUTHORSHIP-keyed, deliberately not link-keyed, for two reviewed reasons:
    node pruning (a T1 re-root) can delete the refuted node together with the
    engine row's REFUTES link — the window must survive that; and an LLM
    refutation link must not WIDEN the window — a late "we'd ruled out X
    earlier" exclusion note on a sibling would otherwise retroactively mask an
    already-recorded legitimate confirmation (READY regressing to NEEDS_INFO
    on a resolved case, with the close-pivot converting the user's next "yes
    it's resolved" into a CLOSED misdisposition). A failed fix the engine
    never saw (the cause was never grounded, so M6 never fired) sets no
    window — that residual premature-row trust is accepted: the RESOLVED
    handshake and the stamp's per-root refutation window + bearing check
    still guard the truth surface."""
    engine_ids = _engine_absence_row_ids(case)
    if not engine_ids:
        return -1
    return max(
        (
            getattr(e, "collected_at_turn", 0) or 0
            for e in (getattr(case, "evidence", None) or [])
            if getattr(e, "evidence_id", None) in engine_ids
        ),
        default=-1,
    )


def _disconfirmation_row_ids(case: "Case") -> set:
    """Ids of absence rows that ARE failed-fix disconfirmations: rows
    REFUTES-linked (on either belief axis) to a cause the ENGINE itself marked
    disconfirmed — a node carrying an engine-authored absence-REFUTES link, or
    a hypothesis attached to such a node. The LLM's own failed-fix row
    co-targets the node M6 marked, so it is caught; a REFUTES link to any
    OTHER node/hypothesis is proof-by-exclusion of a SIBLING — the natural
    dual-use emission ("the fix worked, so it wasn't the network flap")
    records the confirmation and the exclusion in ONE row, and that row must
    stay confirmable (blanket REFUTES-linked exclusion regressed READY to
    NEEDS_INFO right after the user confirmed — the reviewed stuck-loop
    shape). Residual, stated: a failed fix the engine never saw (M6 never
    fired) leaves its LLM row unmarked here — the same accepted trust class
    as the unset window above, guarded downstream by the handshake, the
    stamp's per-root refutation window, and the bearing check."""
    engine_ids = _engine_absence_row_ids(case)
    if not engine_ids:
        return set()
    nodes = getattr(case, "causal_nodes", None) or {}
    marked_nodes = {
        node_id
        for node_id, node in nodes.items()
        if any(
            link.stance == EvidenceStance.REFUTES and link.evidence_id in engine_ids
            for link in node.evidence_links
        )
    }
    if not marked_nodes:
        return set()
    disconfirmations: set = set()
    for node_id in marked_nodes:
        for link in nodes[node_id].evidence_links:
            if link.stance == EvidenceStance.REFUTES:
                disconfirmations.add(link.evidence_id)
    for hyp in (getattr(case, "hypotheses", None) or {}).values():
        if hyp is None or getattr(hyp, "root_node_id", None) not in marked_nodes:
            continue
        for link in getattr(hyp, "evidence_links", None) or []:
            if link.stance == EvidenceStance.REFUTES:
                disconfirmations.add(link.evidence_id)
    return disconfirmations


def resolution_confirmation_rows(case: "Case") -> list:
    """The case's causal_absence rows eligible to CONFIRM a resolution:

    - Not engine-authored: the engine only mints absence rows as failed-fix
      DISCONFIRMATIONS (M6) — a disconfirmation must never read as the
      confirmation it disproves.
    - Not itself a failed-fix disconfirmation (``_disconfirmation_row_ids``):
      REFUTES-linked, on either belief axis, to the cause the engine marked
      disconfirmed. A REFUTES link to a SIBLING (proof-by-exclusion) does not
      disqualify — the dual-use "fix worked, so it wasn't X" row stays
      confirmable.
    - At or after the latest engine-known failed-fix disconfirmation
      (``latest_disconfirmation_turn``): a premature "it's stable" row from a
      fix window that later FAILED must not confirm anything afterward. The
      comparison is ``>=``, not ``>`` — the mixed single-turn shape ("the
      restart didn't fix it, but correcting resolv.conf did") stamps the
      failed-fix row AND the legitimate confirmation at the SAME turn, and
      turn granularity cannot order within-turn events; masking that
      confirmation strands the resolve behind an ask the user just answered
      (NO-COLLAPSE), while the handshake still guards the truth surface. The
      LLM's own same-turn failed-fix row is excluded by the disconfirmation
      rule above, not by the window.

    Content-level bearing on a specific root and the per-root refutation
    window are the stamp's additional, root-scoped concerns
    (``_select_bearing_row`` / ``_root_disconfirmation_turn``); this predicate
    is the case-level metadata bar the readiness gate shares."""
    disconfirmation_ids = _disconfirmation_row_ids(case)
    last_disconfirm = latest_disconfirmation_turn(case)
    return [
        e
        for e in (getattr(case, "evidence", None) or [])
        if getattr(e, "category", None) == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        and getattr(e, "collected_by", None) != ENGINE_EVIDENCE_AUTHOR
        and getattr(e, "evidence_id", None) not in disconfirmation_ids
        and (getattr(e, "collected_at_turn", 0) or 0) >= last_disconfirm
    ]


def has_resolution_confirmation(case: "Case") -> bool:
    """Whether a QUALIFYING resolution-confirmation row is on the case — the
    resolution gate's READY bar and the closure→resolve pivot's trigger (both
    previously satisfied by ANY absence row, including the engine's own M6
    disconfirmations)."""
    return bool(resolution_confirmation_rows(case))


# An absence row BEARS on a statement corpus when it shares at least this many
# content tokens with it — the same substantive-overlap floor the T1
# orphan-reattach uses (a 1-token containment is a coincidence, not bearing).
# Calibration home: test_absence_bearing_calibration.py.
_BEARING_MIN_SHARED_TOKENS = 2


def _row_content_tokens(row) -> frozenset[str]:
    """An evidence row's content tokens: the LLM-declared ``summary`` plus the
    verbatim ``extract`` slice when present (same content definition as the
    §7.1 independence mirror), bounded and memoized."""
    text = " ".join(
        part
        for part in (getattr(row, "summary", None), getattr(row, "extract", None))
        if part
    )
    return cached_content_tokens(text)


def _root_disconfirmation_turn(case: "Case", root: "CausalNode") -> int:
    """The newest turn of an absence row REFUTES-linked to THIS root (node
    axis) or to a hypothesis attached to it — ANY author, ANY confidence.
    ``-1`` when none. The stamp's per-root refutation window: the CONFIRMED
    grade must never be minted from a row recorded at-or-before a refutation
    of the very root being confirmed (a hedged self-claimed failed fix does
    not decisively DEMOTE the root — §7.2 — but it does mark its fix window,
    so only a STRICTLY NEWER confirmation, i.e. the user's post-refute
    gone⇒gone, may complete the top grade). Strict ``>`` on purpose: within
    the mint the conservative direction wins the same-turn ambiguity — the
    resolution itself is unaffected (the gate's ``>=`` case-level window
    still reads READY); the grade just stays honest."""
    refuted_row_ids = {
        link.evidence_id
        for link in root.evidence_links
        if link.stance == EvidenceStance.REFUTES
    }
    for hyp in (getattr(case, "hypotheses", None) or {}).values():
        if hyp is None or getattr(hyp, "root_node_id", None) != root.node_id:
            continue
        for link in getattr(hyp, "evidence_links", None) or []:
            if link.stance == EvidenceStance.REFUTES:
                refuted_row_ids.add(link.evidence_id)
    if not refuted_row_ids:
        return -1
    return max(
        (
            getattr(e, "collected_at_turn", 0) or 0
            for e in (getattr(case, "evidence", None) or [])
            if getattr(e, "category", None) == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
            and getattr(e, "evidence_id", None) in refuted_row_ids
        ),
        default=-1,
    )


def _select_bearing_row(case: "Case", root: "CausalNode", candidates: list):
    """Pick the confirmation row to cite for ``root`` — the content-level
    bearing check (#656): the NEWEST candidate that is not affirmatively
    about a DIFFERENT chain.

    Frame = what a confirmation of THIS root may legitimately talk about: the
    root's statement, its mechanism (the nodes its chain reaches on the way
    to ``D``), its attached hypotheses, and the problem anchors ("the DNS
    errors stopped after the fix" confirms gone⇒gone without naming the
    cause). Elsewhere = the rest of the graph: other chains' node statements
    and their hypotheses — the corpus a mis-picked row would actually be
    about.

    A row is REFUSED (counter ``absence_confirmation_bearing_rejected_total``)
    only when it bears on another chain (≥2 shared content tokens) while
    bearing on nothing in the frame (<2): a row affirmatively about a
    DIFFERENT candidate cause must not be cited as this root's gone⇒gone
    proof (NO INCORRECT CONCLUSION). Among the survivors the NEWEST row wins
    — recency, not specificity: the cited confirmation is the row temporally
    closest to the RESOLVED handshake, and a frame-echoing OLDER row (the row
    summary is LLM-authored with the root statement in context, so premature
    rows echo frame tokens by construction) must never outrank the user's
    actual latest confirmation, however terse (a generic "user confirms it's
    working" row is fine — the handshake is the trust bar, and a terse row
    must not strand a count-held root at NO_ROOT, the INV-29 rescue).

    This is a MIS-CITATION guard, not a trust bar. Refusal never blocks the
    resolution itself; the grade stays honest — MECHANISTIC when a validated
    root stands, NO_ROOT on the count-held shape (the held root stays
    INCONCLUSIVE and no conclusion is minted).

    Returns None when every candidate is refused."""
    frame: set[str] = set()
    for anchor in problem_anchor_statements(case):
        frame |= content_tokens(anchor)
    frame |= content_tokens(root.statement or "")
    own_root_ids = {root.node_id} | _chain_descendant_ids(case, root.node_id)
    for nid in own_root_ids:
        node = case.causal_nodes.get(nid)
        if node is not None and node.node_type != NodeType.PROBLEM:
            frame |= content_tokens(node.statement or "")
    for h in case.hypotheses.values():
        if h is not None and h.statement and h.root_node_id == root.node_id:
            frame |= content_tokens(h.statement)
    elsewhere_sets: list[set[str]] = []
    for node in case.causal_nodes.values():
        if node.node_type == NodeType.PROBLEM or node.node_id in own_root_ids:
            continue
        toks = content_tokens(node.statement or "")
        if toks:
            elsewhere_sets.append(toks)
    for h in case.hypotheses.values():
        if (
            h is not None
            and h.statement
            and h.root_node_id
            and h.root_node_id != root.node_id
        ):
            toks = content_tokens(h.statement)
            if toks:
                elsewhere_sets.append(toks)

    survivors: list = []
    for row in candidates:
        row_tokens = _row_content_tokens(row)
        if len(row_tokens & frame) < _BEARING_MIN_SHARED_TOKENS and any(
            len(row_tokens & other) >= _BEARING_MIN_SHARED_TOKENS
            for other in elsewhere_sets
        ):
            absence_confirmation_bearing_rejected_total.inc()
            continue
        survivors.append(row)
    if not survivors:
        return None
    return max(survivors, key=lambda e: e.collected_at_turn or 0)


def _chain_descendant_ids(case: "Case", root_id: str) -> set[str]:
    """Node ids the target root's chain reaches (transitive cause→effect
    closure) — its MECHANISM rungs. Iterative walk over the case edges
    (traversal order is irrelevant to the set); a malformed cyclic graph
    terminates via the visited set."""
    descendants: set[str] = set()
    frontier = [root_id]
    while frontier:
        current = frontier.pop()
        for edge in case.causal_edges or []:
            if edge.cause_node_id == current:
                nxt = edge.effect_node_id
                if nxt not in descendants and nxt != root_id:
                    descendants.add(nxt)
                    frontier.append(nxt)
    return descendants


def _confirmation_provenance(case: "Case", root: "CausalNode", evidence_id: str) -> str:
    """How a resolution-confirmed cause was ESTABLISHED, in one line (#987).

    The constructive half of the rule this campaign writes down: *constructive
    transitions may be derived from confirmation plus evidence with recorded
    provenance; destructive transitions require established preconditions.* The
    confirm-stamp is constructive — it promotes a cause the user explicitly
    confirmed — so what it owes is not a stricter gate but an honest record of
    WHY the promotion happened. Written onto both the durable node link
    (``reasoning``) and the conclusion (``established_by``), so a reader of
    either surface can see that the cause rests on the user's handshake plus a
    named causal-absence row rather than on a bare engine assertion.
    """
    return (
        f"engine: user-confirmed resolution at turn {case.current_turn} — "
        f"causal-absence {evidence_id} bears on root {root.node_id} "
        f"(M2 gone⇒gone)"
    )[:500]


# Graph hooks — inversion seam. ``causal_graph`` (the higher layer: it imports
# this module) REGISTERS its primitives at import time so the confirm-stamp can
# consult the §7.1 count-held set and re-derive node states without an import
# back-edge (the architecture contract forbids the cycle, deferred or not).
_GRAPH_HOOKS: dict = {}


def register_graph_hooks(
    *,
    support_count_held_root_ids,
    derive_node_states,
    sole_cluster_origin,
    project_hypothesis_states_from_roots,
) -> None:
    """Called once from ``causal_graph`` at module import."""
    _GRAPH_HOOKS["count_held"] = support_count_held_root_ids
    _GRAPH_HOOKS["derive"] = derive_node_states
    _GRAPH_HOOKS["sole_cluster_origin"] = sole_cluster_origin
    _GRAPH_HOOKS["project_hyp_states"] = project_hypothesis_states_from_roots


def _graph_hooks() -> dict:
    """The registered hooks; cold-start fallback loads ``causal_graph`` by
    name (call-time, both modules long initialized — no init-order hazard)
    for any entry point that reaches the stamp without the engine stack."""
    if not _GRAPH_HOOKS:
        import importlib

        importlib.import_module("faultmaven.core.investigation.causal_graph")
    return _GRAPH_HOOKS


def confirm_root_from_resolution_absence(case: "Case") -> bool:
    """M2 confirm-side twin of the failed-fix refute stamp: make the
    ``CONFIRMED`` grade reachable from the live flow — called at RESOLVED
    **transition execution** (after the user's explicit confirmation), never
    on the mere appearance of an absence row.

    The prompt's verify-turn contract records the resolution-confirming
    ``causal_absence_evidence`` row as a STAND-ALONE audit row ("do NOT link
    it"), and the chain-emission ingest strips any LLM attempt to SUPPORTS-link
    absence evidence — so this stamp is the only producer of the counterfactual
    confirmation the grade requires. The row alone is an LLM self-claim: a
    premature "pods are stable" absence row emitted mid-rollout (observed live
    in the gate sims) must not confirm anything. The trigger is therefore the
    RESOLVED handshake — the user's explicit consent — which is strictly
    stronger evidence than the row's existence.

    Target root — exactly ONE, chosen conservatively (NO INCORRECT CONCLUSION):

    - Validated roots of STANDING hypotheses (ACTIVE/VALIDATED) when any exist:
      an orphan validated node whose hypothesis decayed to RETIRED without
      disproof must not veto the user's confirmation of the standing cause.
    - Otherwise all validated roots (the weak-model chain-without-hypothesis
      shape). Several candidate nodes either way are first collapsed to
      DISTINCT causes (§7.1.2 ``distinct_cause_clusters``: duplicate emissions
      and same-causal-line roots are ONE cause — the ancestor-most member is
      the cited origin); with several distinct causes remaining (an
      unarbitrated MECE violation) the engine never guesses which cause the
      fix removed — the case stays MECHANISTIC pending arbitration.
    - With NO validated root at all: the COUNT-HELD roots (§7.1/INV-29 —
      really causally supported and blocked only by the independent-support
      bar, ``causal_graph.support_count_held_root_ids``). The user's gone⇒gone
      handshake is the decisive second observation, so the count bar must not
      veto it; after linking, a re-derive validates the root via the
      confirmed-root bypass. The same sole-candidate discipline applies, and a
      root held for any OTHER reason (restating, net-refuted, AND-gate, no
      qualifying causal support) never qualifies.

    Row selection — order-independent, windowed, and bearing-checked:

    - Repositories load ``case.evidence`` newest-first, in-memory construction
      appends oldest-first — so selection keys on ``collected_at_turn`` (the
      NEWEST qualifying row), never on list position.
    - The case-level metadata bar is the SHARED
      ``resolution_confirmation_rows`` predicate (non-engine-authored, not a
      failed-fix disconfirmation, at-or-after the engine-known failure
      window) — the same bar the resolution-readiness gate reads, so the gate
      can never call a case confirmable on a row the stamp would refuse for
      metadata.
    - Per-root refutation window (``_root_disconfirmation_turn``, strict
      ``>``): the cited row must be NEWER than any refutation recorded
      against THIS root — any author, any confidence. A hedged self-claimed
      failed fix does not demote the root (§7.2), but the top grade is never
      minted from a row at-or-before it. This also excludes any row
      REFUTES-linked to the target chain itself (it can never be newer than
      its own refutation turn).
    - Rows already SUPPORTS-linked anywhere never qualify (another root's
      confirmation is not double-booked); a REFUTES link to a SIBLING does
      not disqualify — the dual-use "fix worked, so it wasn't X" row is a
      legitimate citation.
    - Content-level bearing on THIS root (``_select_bearing_row``, #656): the
      NEWEST candidate not affirmatively about a DIFFERENT chain — recency
      over specificity, so an older frame-echoing row never outranks the
      user's actual latest confirmation; a terse generic row is accepted
      (the handshake is the signal).
    - Idempotent: a root already counterfactually confirmed is left alone.

    On success, an ENGINE-authored conclusion mirror naming this root is
    upgraded in place (VERIFIED + the confirming row cited) — terminal cases
    never recompute, so the mirror would otherwise stay frozen at the
    mechanistic grade beside a CONFIRMED case. Returns True if it attached a
    link; the caller re-persists grade, over-claim flag, and verification
    status so the terminal blob reflects the confirmation.
    """
    candidate_roots = _validated_roots(case)
    count_held_ids: set = set()
    if not candidate_roots:
        # §7.1 independent-support bar (INV-29): a sole root held from
        # VALIDATED ONLY by the count bar — really causally supported,
        # net-supporting, AND-gate satisfied, not restating — may still be
        # confirmed here: the user's explicit gone⇒gone handshake IS the
        # decisive second observation (strictly stronger than any empirical
        # count; M2). Without this, the count bar would VETO the confirmation
        # — the strongest evidence class yielding the weakest grade — and the
        # case would terminate NO_ROOT with harvest permanently blocked.
        count_held_fn = _graph_hooks().get("count_held")
        if count_held_fn is not None:
            count_held_ids = count_held_fn(case)
        candidate_roots = [
            case.causal_nodes[nid] for nid in count_held_ids if nid in case.causal_nodes
        ]
    if not candidate_roots:
        return False
    standing_root_ids = {
        h.root_node_id
        for h in case.hypotheses.values()
        if h is not None and h.state in _STANDING_HYP_STATES and h.root_node_id
    }
    targets = [n for n in candidate_roots if n.node_id in standing_root_ids]
    if not targets:
        targets = candidate_roots
    cat_by_id = evidence_category_map(case)
    if len(targets) == 1:
        cluster_ids = {targets[0].node_id}
        root = targets[0]
    else:
        # §7.1.2 MECE arbitration: several candidate NODES are a coherence
        # violation only when they are several DISTINCT causes. Duplicates and
        # same-causal-line roots collapse to ONE cluster — the user's gone⇒gone
        # handshake is not ambiguous about the CAUSE there, and a duplicate
        # emission must not veto it (the INV-29 stamp-veto lesson). A genuine
        # multi-cluster contest still refuses: the engine never guesses which
        # cause the fix removed — the case stays MECHANISTIC pending
        # arbitration. The hook is read defensively (.get, like every other
        # hook consumer here): this sits on the unguarded RESOLVED-execution
        # path, so a missing registration must degrade to the safe refusal,
        # never a KeyError that 500s the transition.
        arbitrate = _graph_hooks().get("sole_cluster_origin")
        if arbitrate is None:
            return False
        resolved = arbitrate(case, {n.node_id for n in targets})
        if resolved is None:
            return False
        # The cited node is the cluster's ORIGIN (most live in-cluster
        # descendants, by the SAME reachability the cluster count used — on a
        # deepened chain the fix's confirmed removal is asserted of the
        # origin, not its consequence; a mixed cluster prefers the member
        # that actually heads the line over an edge-less duplicate of a
        # consequence). The origin is a member of the cluster, which is a
        # subset of the target ids, so the lookup cannot miss. NOTE for the
        # node-dedup follow-on (#656): this writes the durable confirmation
        # link onto ONE member of a duplicate cluster — any later dedup/merge
        # of persisted cases must migrate evidence_links to the surviving
        # node.
        origin_id, cluster_ids = resolved
        root = {n.node_id: n for n in targets}[origin_id]
    # Idempotence is CLUSTER-wide: a confirmation anywhere in the cluster is
    # the cause's confirmation (a retried resolve, or a chain deepened after
    # a confirmed resolution, must not stamp the same cause twice under a
    # different node id).
    if any(
        root_counterfactually_confirmed(case.causal_nodes[nid], cat_by_id)
        for nid in cluster_ids
        if nid in case.causal_nodes
    ):
        return False

    # Rows already SUPPORTS-linked anywhere are spoken for (another root's
    # confirmation must not be double-booked). REFUTES links do NOT exclude a
    # row here: a REFUTES on the TARGET's chain is covered by the per-root
    # window below (the row can never be newer than its own refutation turn),
    # and a REFUTES on a SIBLING is proof-by-exclusion — the dual-use
    # "fix worked, so it wasn't X" row is a legitimate citation.
    supports_linked_ids = {
        link.evidence_id
        for node in case.causal_nodes.values()
        for link in node.evidence_links
        if link.stance == EvidenceStance.SUPPORTS
    }
    # Per-root refutation window (strict >), taken CLUSTER-wide: the cited
    # confirmation must be NEWER than any refutation recorded against ANY
    # member of the cause's cluster — a hedged failed-fix refute on a
    # duplicate node (which per §7.2 does not demote it, so it stays in the
    # cluster) is a refutation of the SAME cause and must window the mint
    # exactly as one on the cited origin would. Covers the shapes the
    # case-level engine window cannot see (a hedged self-claimed failed fix
    # never fires M6; an LLM decisive refute on the root marks the failure
    # even when the engine marker is absent). The gate's liveness is
    # untouched; only the top-grade mint holds.
    root_refute_turn = max(
        _root_disconfirmation_turn(case, case.causal_nodes[nid])
        for nid in cluster_ids
        if nid in case.causal_nodes
    )
    candidates = [
        e
        for e in resolution_confirmation_rows(case)
        if e.evidence_id not in supports_linked_ids
        and (e.collected_at_turn or 0) > root_refute_turn
    ]
    if not candidates:
        return False
    absence_row = _select_bearing_row(case, root, candidates)
    if absence_row is None:
        return False

    provenance = _confirmation_provenance(case, root, absence_row.evidence_id)
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=absence_row.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning=provenance,
            linked_at_turn=case.current_turn,
        )
    )
    if root.node_id in count_held_ids:
        # The confirmation completes the empirical bar for a count-held root:
        # re-derive so the confirmed-root bypass VALIDATES it before the
        # caller re-reads the grade (grade_cause_assurance requires a
        # VALIDATED root) and before the mirror upgrade names it.
        derive_fn = _graph_hooks().get("derive")
        if derive_fn is not None:
            derive_fn(case)
    rcc = case.root_cause_conclusion
    prior_hyp = (
        case.hypotheses.get(getattr(rcc, "validated_hypothesis_id", None) or "")
        if rcc is not None
        else None
    )
    if rcc is None:
        # A count-held root was INCONCLUSIVE until this stamp, so the per-turn
        # mirror synthesis never minted a conclusion for it — and terminal
        # cases never recompute. Without a mint HERE, the case would freeze
        # as CONFIRMED (the sole harvest authority) with NO cause text for
        # the report/harvest layers to read. Mint the minimal faithful
        # mirror for the root the user just confirmed; an LLM-authored
        # conclusion, had one existed, is never touched.
        _mint_confirmed_mirror(case, root, provenance)
    elif (
        getattr(rcc, "determined_by", None) == ENGINE_RCC_AUTHOR
        and prior_hyp is not None
        and prior_hyp.root_node_id in cluster_ids
        and prior_hyp.root_node_id != root.node_id
    ):
        # The engine mirror names ANOTHER member of the just-confirmed
        # cluster (per-turn synthesis picks in iteration order, so on a
        # chain deepened late it can name the consequence). Same cause,
        # wrong depth: re-mint naming the confirmed origin — otherwise the
        # terminal blob reads CONFIRMED beside a CONFIDENT mirror citing the
        # consequence and never the confirming row (the frozen-mechanistic-
        # mirror shape the upgrade exists to prevent). LLM-authored
        # conclusions are never touched.
        case.root_cause_conclusion = None
        _mint_confirmed_mirror(case, root, provenance)
    else:
        _upgrade_engine_mirror(case, root, absence_row.evidence_id, provenance)
    return True


def _mint_confirmed_mirror(case: "Case", root: "CausalNode", provenance: str) -> None:
    """Mint the engine conclusion mirror for a just-confirmed root when NO
    conclusion exists (the count-held-at-RESOLVED shape). Mechanism text
    follows the standing hypothesis's chain when one names this root (same
    shape as the per-turn synthesis); the hypothesis-less fallback states the
    degenerate mechanism. Confidence is grade-derived: the root is
    counterfactually CONFIRMED, so VERIFIED at the floor.

    ``provenance`` records HOW the cause was established (#987) — this mint is
    a promotion from the user's confirmation plus the cited absence row, not a
    chain validation, and the conclusion must say so rather than assert the
    cause bare."""
    hyp = next(
        (
            h
            for h in case.hypotheses.values()
            if h is not None
            and h.state in _STANDING_HYP_STATES
            and h.root_node_id == root.node_id
        ),
        None,
    )
    inter: list[str] = []
    if hyp is not None:
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
    likelihood = CONFIRMED_RCC_LIKELIHOOD_FLOOR
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=root.statement[:1000],
        mechanism=mechanism,
        confidence_level=ConfidenceLevel.from_score(likelihood),
        likelihood=likelihood,
        validated_hypothesis_id=hyp.hypothesis_id if hyp is not None else None,
        evidence_basis=[
            link.evidence_id
            for link in root.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        ],
        determined_by=ENGINE_RCC_AUTHOR,
        established_by=provenance,
    )


def _upgrade_engine_mirror(
    case: "Case", root: "CausalNode", absence_evidence_id: str, provenance: str
) -> None:
    """Scoped re-mint at the stamp site: terminal cases never run the per-turn
    recompute (and ``terminal_transitions`` cannot import ``causal_graph``'s
    full mirror synthesis), so without this an ENGINE-authored conclusion would
    stay frozen at CONFIDENT/0.8 — and its ``evidence_basis``, minted pre-stamp,
    would never cite the gone⇒gone row — beside a CONFIRMED terminal grade. The
    upgrade keeps the mirror's text/root and raises only the grade-derived
    fields. LLM-authored conclusions are never touched (their retraction and
    refresh lifecycle is a separate correction tracked on #656).

    ``provenance`` (#987) records that the upgrade to VERIFIED rests on the
    user's confirmation plus the cited gone⇒gone row — the grade jump is a
    promotion from confirmation, and the record says so."""
    rcc = case.root_cause_conclusion
    if rcc is None or getattr(rcc, "determined_by", None) != ENGINE_RCC_AUTHOR:
        return
    hyp = case.hypotheses.get(getattr(rcc, "validated_hypothesis_id", None) or "")
    if hyp is None or hyp.root_node_id != root.node_id:
        return
    likelihood = max(rcc.likelihood or 0.0, CONFIRMED_RCC_LIKELIHOOD_FLOOR)
    evidence_basis = list(rcc.evidence_basis or [])
    if absence_evidence_id not in evidence_basis:
        evidence_basis.append(absence_evidence_id)
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=rcc.root_cause,
        mechanism=rcc.mechanism,
        confidence_level=ConfidenceLevel.from_score(likelihood),
        likelihood=likelihood,
        validated_hypothesis_id=rcc.validated_hypothesis_id,
        evidence_basis=evidence_basis,
        contributing_factors=list(rcc.contributing_factors or []),
        determined_by=ENGINE_RCC_AUTHOR,
        established_by=provenance,
    )


def conclusion_overclaims(rcc, grade: CauseAssuranceGrade) -> bool:
    """The M2 over-claim seam predicate — ONE definition shared by the WARNING
    in the per-turn recompute, the ``seam_overclaim`` flag in the DEBUG
    grounding trace, and the terminal re-grade, so the prod signal and the
    greppable trace can never disagree about the same turn."""
    return (
        rcc is not None
        and rcc.confidence_level == ConfidenceLevel.VERIFIED
        and grade != CauseAssuranceGrade.CONFIRMED
    )


def grade_cause_assurance(case: "Case") -> CauseAssuranceGrade:
    """Classify the case's identified cause into a single assurance grade, in one
    pass over its validated roots. The single source of truth for §7 gating.

    A confidently-wrong LLM must not turn an unverified cause into reusable
    knowledge, so only ``CONFIRMED`` — a validated root whose removal was
    observed to remove the problem — clears the bar.
    """
    validated_roots = _validated_roots(case)
    if not validated_roots:
        return CauseAssuranceGrade.NO_ROOT
    evidence_category_by_id = evidence_category_map(case)
    if any(
        root_counterfactually_confirmed(r, evidence_category_by_id)
        for r in validated_roots
    ):
        return CauseAssuranceGrade.CONFIRMED
    return CauseAssuranceGrade.MECHANISTIC


def has_root_cause_record(case: "Case") -> bool:
    """True when the case carries a populated ``RootCauseConclusion`` (an RCC row
    whose ``root_cause`` text is set) — the Root Cause a runbook copies verbatim."""
    return bool(
        case.root_cause_conclusion
        and getattr(case.root_cause_conclusion, "root_cause", None)
    )


def has_problem_definition(case: "Case") -> bool:
    """True when the case has a verified symptom statement — the source of a
    runbook's Problem Definition / Symptom Recognition section."""
    return bool(
        case.problem_verification
        and getattr(case.problem_verification, "symptom_statement", None)
    )


def has_actionable_solution(case: "Case") -> bool:
    """True when at least one *non-failed* solution carries actionable content —
    commands, implementation steps, or a long-term fix. Without it a generated
    runbook has no Resolution to offer and the LLM would have to invent one.

    A solution whose matching ``ProposedAction`` was never executed —
    superseded/rejected or engine-downgraded to DIAGNOSTIC (``SolutionOutcome.FAILED``)
    — does not count, so a case whose only actionable fixes were never run is not
    offered for conversion (its runbook would launder a never-run fix's commands into
    the remediation slot). Cases with no compliance chain classify their solutions
    ``PROPOSED`` and still count, preserving the prior behavior."""
    proposed_actions = getattr(case, "proposed_actions", None) or []
    for sol in case.solutions or []:
        if (
            getattr(sol, "commands", None)
            or getattr(sol, "implementation_steps", None)
            or getattr(sol, "longterm_fix", None)
        ) and classify_solution_outcome(
            sol, proposed_actions
        ) != SolutionOutcome.FAILED:
            return True
    return False


def runbook_conversion_ready(case: "Case") -> bool:
    """The single canonical predicate for auto-converting a case into a runbook.

    True iff the case clears the runbook-conversion critical bar — the same bar
    ``assess_runbook_readiness`` treats as the NOT_SUITABLE boundary:

    - a verified problem definition (symptom statement), AND
    - a **CONFIRMED** root cause with a populated ``RootCauseConclusion`` record
      (``grade_cause_assurance == CONFIRMED`` — counterfactually borne out,
      gone⇒gone — AND an RCC row), AND
    - at least one actionable solution.

    This is the ONE source of truth every runbook-conversion gate defers to (the
    RESOLVED offer affordance, the content-readiness assessment, and the
    trust-boundary guard) so they cannot drift apart (#698). It is a **prior, not
    a gate** on the investigation itself: it only decides whether a resolved case
    is sound and substantial enough that turning it into reusable knowledge won't
    seed the KB with a wrong or empty cause (NO INCORRECT CONCLUSION at the
    knowledge layer). The grade half enforces soundness; the problem-definition
    and actionable-solution halves enforce that the runbook won't be a shell the
    LLM has to fabricate content for.
    """
    return (
        has_problem_definition(case)
        and has_root_cause_record(case)
        and grade_cause_assurance(case) == CauseAssuranceGrade.CONFIRMED
        and has_actionable_solution(case)
    )
