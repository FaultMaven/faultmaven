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
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from faultmaven.core.investigation.lifecycle_metrics import (
    absence_confirmation_bearing_rejected_total,
    absence_row_link_refused_total,
)
from faultmaven.modules.case.contracts import (
    CONFIRMED_ESTABLISHED_BY,
    CauseAssuranceGrade,
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceStance,
    HypothesisState,
    InvestigationActionType,
    NeedPurpose,
    NeedState,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    RootCauseConclusion,
    SolutionOutcome,
    classify_solution_outcome,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, CausalNode, EvidenceNeed

__all__ = [
    "CONFIRMED_RCC_LIKELIHOOD_FLOOR",
    "ENGINE_EVIDENCE_AUTHOR",
    "ENGINE_RCC_AUTHOR",
    "MECHANISTIC_RCC_LIKELIHOOD",
    "FIX_FAILED",
    "FIX_SUCCEEDED",
    "FIX_UNKNOWN",
    "FIX_INTERCEPTION",
    "FIX_REMEDIATION",
    "SUBJECT_CAUSE",
    "SUBJECT_PROBLEM",
    "SUBJECT_UNBOUND",
    "BearingFrame",
    "CauseAssuranceGrade",
    "FixWindow",
    "SubjectIndex",
    "absence_row_link_refused",
    "bearing_frame",
    "cached_content_tokens",
    "cause_elimination_rows",
    "conclusion_overclaims",
    "confirm_root_from_resolution_absence",
    "confirmation_stamp_rows",
    "confirming_polarity_rows",
    "content_tokens",
    "counterfactual_link_decisive",
    "evidence_category_map",
    "evidence_datum_key",
    "attempt_window",
    "conclusion_node",
    "fix_application_turn",
    "fix_windows",
    "grade_cause_assurance",
    "has_actionable_solution",
    "has_problem_definition",
    "has_resolution_confirmation",
    "has_root_cause_record",
    "interception_relief",
    "latest_disconfirmation_turn",
    "latest_decided_fix_window",
    "confirming_window",
    "latest_fix_window",
    "licensed_roots",
    "observation_subject",
    "observed_turn",
    "off_cause_attempt",
    "pending_symptom_check",
    "problem_anchor_statements",
    "problem_leg_anchor",
    "resolution_confirmation_rows",
    "root_counterfactually_confirmed",
    "runbook_conversion_ready",
    "subject_index",
    "symptom_elimination_rows",
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

    TRUE iff the backing evidence row is an ABSENCE row — ``causal_absence`` or
    ``symptom_absence`` — whatever the stance, whichever axis. Absence rows are
    STAND-ALONE audit records and carry NO model-authored stance; every
    counterfactual link on one is engine-minted (the resolution confirm-stamp's
    SUPPORTS, ``causal_graph._attach_engine_refutation``'s REFUTES).

    BOTH categories, because the prompt says so for both and the erosion
    argument is identical: "a fix confirms the cause, so a confidence-bearing
    link would erode the very hypothesis it proves". ``symptom_absence`` carries
    no counterfactual force (only ``causal_absence`` feeds
    ``root_counterfactually_confirmed`` / the M6 node-side trigger), but it does
    move likelihood and node tallies — and a rule the engine states to the model
    must be a rule the engine actually enforces, or the prompt is lying about
    the boundary.

    **The invariant is a property of the EVIDENCE CATEGORY, not of the link
    target.** That is why this predicate exists at all rather than being
    inlined: the two belief axes — chain ``node_evidence_links``
    (``causal_graph.ingest_emitted_chain``) and flat ``hypothesis_evidence_links``
    (``milestone_engine._apply_hypothesis_evidence_links``) — are separate entry
    points into the same truth, so a rule enforced on one is a rule a single
    stance choice routes around. Both call THIS function; neither owns a copy.

    Why REFUTES is refused, not just SUPPORTS (the pre-#987 boundary): the
    prompt contract emits ``causal_absence_evidence`` ONLY for a CONFIRMED fix
    (templates/treatment.py, TREATMENT "EVIDENCE TYPES FOR THIS STAGE"), and its FAILURE
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
    if category not in (
        EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
    ):
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


def evidence_datum_key(row) -> tuple:
    """The identity of the OBSERVATION a row carries: ``(source, normalised
    extract)``. Two rows sharing a key are the same datum recorded twice.

    Owned here — the contracts-only leaf — because two unrelated readers need the
    same notion and must not drift: the work gate's evidence count
    (``verification_status.work_gate_passed``, where duplicates would spuriously
    satisfy ``WORK_GATE_MIN_EVIDENCE`` and flip a case out of
    ``NOT_YET_PRODUCTIVE`` on one datum) and the progress signal
    (``milestone_engine._restates_standing_evidence``). This is the same hazard
    INV-36 names for hypotheses — "duplicates spuriously re-satisfy the ≥2-active
    work gate, corrupting the axis" — on the evidence dimension of the same gate.

    Source identity is part of the key on purpose: the same text observed in two
    different files is two independent observations, which is precisely the
    corroboration signal the grading layer counts. Only the SAME text from the
    SAME source is one datum.

    A row with no extract gets a key unique to itself, so unquoted rows are never
    collapsed into each other — absence of a quote is not evidence of sameness.
    """
    extract = getattr(row, "extract", None)
    if not extract:
        return ("\x00unique", getattr(row, "evidence_id", id(row)))
    return (
        getattr(row, "source_file_id", None),
        " ".join(extract.split()).lower(),
    )


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
    conclusion-grade action), or demote the identified cause (M6). Nor does a
    hedged REFUTES, absence-backed or not, count in the balances that demote a
    cause or a node (#2106): the node tally's ``refutes > supports``, M6's
    evidence arm and the conclusion retraction (``cause_refuted_by_evidence``)
    leave it out, so there it is only bearing. The flat likelihood update
    (``update_likelihood_from_evidence``) still counts it (#2154, open).
    ``None`` reads as unset → full
    confidence (the engine's own M6 links carry no declared confidence and are
    decisive by construction); an EXPLICIT 0.0 stays filtered."""
    return stance_declared_confident(link)


def stance_declared_confident(link) -> bool:
    """Whether the link's own declared ``stance_confidence`` is at or above
    ``CAUSAL_STANCE_CONFIDENCE_MIN`` (§7.1, INV-29): below it the stance is a
    self-hedge. ``None`` reads as unset, so full confidence; an EXPLICIT 0.0
    stays filtered."""
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
# Recorded subjects (#1906 D1) and the data's clock (#2145).
#
# A post-fix observation confirms or disconfirms only the subject it names,
# through recorded ids: ``Evidence.rechecks_evidence_id`` names the row it
# re-observes, and ``ProblemVerification.verifying_evidence_ids`` the rows that
# showed the problem. A row that names none, or names one that binds to
# nothing, never confirms and never disconfirms. Every reader below that
# decides confirm, disconfirm or persistence from a post-fix row reads
# ``SubjectIndex.subject``; content tokens only rank bound rows
# (``_select_bearing_row``).
#
# Strict citation (#2557, INV-48): a causal_absence row CONFIRMS the cause only
# when it re-observes a row the root's terminal verdict cites, by citation
# alone; a consequence or a relief's result holds with no fix applied. The
# window reads still bind through SUPPORTS, so such a re-check can fail a fix.
#
# The clock table. A comparison relative to a fix, a window or a
# disconfirmation reads ``observed_turn`` (``>= posited``,
# ``>= last_disconfirm``, ``FixWindow.holds``, the stamp's
# ``> root_refute_turn``): a row counts as after the fix only if its data was
# submitted after it. A "this turn" notice reads ``collected_at_turn``
# (``_flag_absence_inside_a_failed_fix_window``, the unbound-row feedback).
# ``_select_bearing_row`` ranks by ``collected_at_turn``: the row recorded
# closest to the handshake.
# ---------------------------------------------------------------------------

SUBJECT_PROBLEM = "problem"
SUBJECT_CAUSE = "cause"
SUBJECT_UNBOUND = "unbound"

_ABSENCE_CATEGORIES = (
    EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
    EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
)
_PROBLEM_OBSERVATIONS = (
    EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
    EvidenceCategory.SYMPTOM_EVIDENCE,
)


def observed_turn(case: "Case", row) -> int:
    """The turn a row's DATA was submitted (#2145): the upload turn of the file
    it was read from, not the turn it was written in. A row written later can
    carry an observation from earlier data, and only the data's turn says
    whether it was made after a fix. ``collected_at_turn`` when the row has no
    file (an engine inference) or its file is not on the case (logged).

    Residuals, stated: an older extract cited from a newer file is dated by
    the file (catching it needs blob I/O); a byte-identical post-fix re-paste
    is deduped onto the pre-fix upload, whose turn is kept, so it is dated
    pre-fix."""
    return subject_index(case).observed(row)


@dataclass
class SubjectIndex:
    """One pass over a case for the subject reads of a turn: the rows, the
    verifying ids (in their recorded order), the two cause pools
    (``_cause_pool``) and the SUPPORTS links. Built by ``subject_index``; a
    reader that loops over rows builds it once.

    ``windows`` picks the pool a read binds a cause observation against: the
    CONFIRMATION pool by default (what a resolution can confirm, what the
    re-check list offers), the WINDOW pool for the readers that decide what a
    fix's window observed (``fix_windows``, ``failed_fix_of``, the false-alarm
    refusal, the unbound-row feedback's symptom rows)."""

    case: "Case"
    rows: dict
    verifying: tuple
    pool_roots: frozenset
    window_roots: frozenset
    pool_hypotheses: frozenset
    node_supports: dict
    hypothesis_supports: dict
    #: node id -> the rows its terminal verdict cites (the cited basis, D6).
    citations: dict
    upload_turns: dict
    _bound: dict

    def observed(self, row) -> int:
        """``observed_turn`` off this index."""
        collected = getattr(row, "collected_at_turn", 0) or 0
        file_id = getattr(row, "source_file_id", None)
        if not file_id:
            return collected
        turn = self.upload_turns.get(file_id)
        if turn is None:
            _log_undated(
                getattr(self.case, "case_id", None),
                getattr(row, "evidence_id", None),
                file_id,
            )
            return collected
        return turn

    def cause_targets(
        self, evidence_id: str, windows: bool = False, roots=None, *, cited=None
    ) -> frozenset:
        """What a cause observation naming ``evidence_id`` re-observes, or
        empty when it binds to none (always for an absence row: absence rows
        carry no links). ``roots``, when given, replaces the pool: the root
        set a fix window binds against (#2036 D2.3), never a hypothesis.

        On the CONFIRMATION read (``roots`` None and not ``windows``; the
        ``cited`` read a kinded window makes with the pool roots its fix
        targeted) the row binds iff a pool root's terminal verdict cites it
        (``root_terminal_evidence_ids``, #2036 D6): to the roots citing it, by
        citation alone, and for the cluster when one member cites it (#2557).
        A pool citing no row binds nothing until the verdict is re-sent. A
        rootless hypothesis binds as before (it has no verdict to cite).

        On a WINDOW read the row binds to the ROOT nodes it supports, when
        they are one cause and touch ``roots``. Only node-axis SUPPORTS
        count, and the links may touch only ROOT nodes, all in one cluster
        (``sole_cluster_origin``, ruled 2026-10-27: a cluster counts as one
        cause): a row SUPPORTS-linked to an INTERMEDIATE rung is about the
        consequence as much as the cause, so a re-check of it (a renewed
        certificate) does not show the cause gone (#2036, c1021-1)."""
        if cited is None:
            cited = roots is None and not windows
        key = (evidence_id, windows if roots is None else frozenset(roots), cited)
        if key in self._bound:
            return self._bound[key]
        target = self.rows.get(evidence_id)
        bound: frozenset = frozenset()
        if (
            target is not None
            and getattr(target, "category", None) not in _ABSENCE_CATEGORIES
        ):
            nodes = getattr(self.case, "causal_nodes", None) or {}
            linked = self.node_supports.get(evidence_id, frozenset())
            hypotheses = self.pool_hypotheses if roots is None else frozenset()
            if roots is None:
                roots = self.window_roots if windows else self.pool_roots
            if roots and cited:
                citing = frozenset(
                    n for n in self.pool_roots if evidence_id in self.citations[n]
                )
                if citing & roots or (
                    citing and _one_cause(self.case, citing | frozenset(roots))
                ):
                    bound = citing
            elif roots:
                if (
                    linked & roots
                    and all(nodes[n].node_type == NodeType.ROOT for n in linked)
                    and _one_cause(self.case, linked)
                ):
                    bound = linked
            elif hypotheses:
                hits = self.hypothesis_supports.get(evidence_id, frozenset())
                hits &= hypotheses
                if len(hits) == 1 and not linked:
                    bound = hits
        self._bound[key] = bound
        return bound

    def subject(self, row, windows: bool = False, roots=None, *, cited=None) -> str:
        """What ``row`` observes: ``SUBJECT_PROBLEM``, ``SUBJECT_CAUSE`` or
        ``SUBJECT_UNBOUND``.

        - problem: it names one of the verifying rows, and it is a
          symptom_absence row (the problem gone) or a symptom_evidence row
          (the problem still present);
        - cause: it names a row of the cause pool (``cause_targets``), and it
          is a causal_absence row;
        - unbound: anything else — a row that names nothing (ruled 2026-10-31,
          enforcement), names no row of the case, or names a subject its
          category does not observe; and an engine-authored row, which
          observes nothing. A ref to the row itself or to an absence row is
          stored unbound when the turn records it
          (``response_application._record_rechecks``).

        ``roots`` and ``cited`` bind it as ``cause_targets`` does."""
        target_id = getattr(row, "rechecks_evidence_id", None)
        if (
            not isinstance(target_id, str)
            or target_id not in self.rows
            or getattr(row, "collected_by", None) == ENGINE_EVIDENCE_AUTHOR
        ):
            return SUBJECT_UNBOUND
        category = getattr(row, "category", None)
        if category in _PROBLEM_OBSERVATIONS:
            return SUBJECT_PROBLEM if target_id in self.verifying else SUBJECT_UNBOUND
        if category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE and (
            self.cause_targets(target_id, windows, roots, cited=cited)
        ):
            return SUBJECT_CAUSE
        return SUBJECT_UNBOUND

    def addresses(self, row, root_ids, windows: bool = False) -> bool:
        """Whether a cause-bound ``row`` re-observes the cause ``root_ids``
        stand for: what it names binds (``cause_targets``) to a member of that
        cause, or a root in one cluster with it."""
        named = getattr(row, "rechecks_evidence_id", "") or ""
        target = self.cause_targets(named, windows)
        roots = set(root_ids)
        return bool(target) and bool(
            target & roots or _one_cause(self.case, target | roots)
        )

    def bindable_rows(self) -> tuple[list, list]:
        """The rows a re-check could name and bind to, against the
        confirmation pool: the verifying rows in their recorded order, and
        the cited basis of its roots (``cause_targets``) in the order
        recorded. The narrowed re-verification checklist and the unbound-row
        feedback both render this."""
        problem = [self.rows[i] for i in self.verifying if i in self.rows]
        cause = [
            row
            for evidence_id, row in self.rows.items()
            if evidence_id not in self.verifying and self.cause_targets(evidence_id)
        ]
        order = lambda row: getattr(row, "collected_at_turn", 0) or 0  # noqa: E731
        return problem, sorted(cause, key=order)


@lru_cache(maxsize=4096)
def _log_undated(case_id, evidence_id, file_id) -> None:
    """Once per row: its file is not on the case, so ``observed_turn`` falls
    back to the turn the row was recorded."""
    logger.warning(
        "Case %s: evidence %s names file %s, which is not on the case; dated by "
        "the turn it was recorded (#2145)",
        case_id,
        evidence_id,
        file_id,
    )


def _one_cause(case: "Case", root_ids) -> bool:
    """Whether ``root_ids`` are one cause: a single node, or nodes that collapse
    to one distinct-cause cluster (``sole_cluster_origin``). A missing hook
    refuses, the conservative direction."""
    if len(root_ids) <= 1:
        return True
    arbitrate = _graph_hooks().get("sole_cluster_origin")
    return arbitrate is not None and arbitrate(case, set(root_ids)) is not None


def _cause_pool(case: "Case") -> tuple[frozenset, frozenset, frozenset]:
    """The causes a cause observation can be about, as ``(confirmation roots,
    window roots, rootless hypothesis ids)``.

    The CONFIRMATION pool is the first non-empty leg, in the ruled order
    (2026-10-27, ``_cause_posited_turn``'s):

    1. the identified roots (``CausalNode.is_identified_root``);
    2. the count-held roots whose hypothesis marked them terminal
       (``support_count_held_root_ids``, as the confirm-stamp admits them);
    3. the case's own FALLEN cause — the roots the last recompute recorded as
       identified (``progress.identified_cause_root_ids``) and the roots
       carrying the engine's failed-fix disconfirmation (only M6 mints one,
       on the identified cause it demoted) — only when nothing above stands
       AND no other standing hypothesis is rooted elsewhere: a cause re-fixed
       after it fell can still be shown removed, while nothing is confirmed
       through the fallen one past any other standing hypothesis rooted
       elsewhere (by node id, whatever its age). Once fixes are kinded, it
       stands only for the fallen roots the latest remediation targets
       (#2485): a fix of an orphan root no hypothesis names is an
       interception of it, and a remediation of another cause leaves the
       fallen one nothing to confirm. With no remediation, or an unkinded
       latest one, every fallen root stands;
    4. a VALIDATED hypothesis with no root node (inert while a rootless
       hypothesis cannot validate).

    The WINDOW pool is the confirmation roots plus the fallen roots, always
    (on leg 4 too). A fix's window must keep observing the cause it addressed
    when that cause falls (M6 demotes it) or loses its verdict in the very
    turn its fix is seen failing (#2042), so a re-check of it can still fail
    the window (``FixWindow.cause_rows``); only a confirmation-bound cause
    row can make a window SUCCEEDED (``FixWindow.confirmed_cause_rows``).

    With no confirmation pool nothing confirms the cause: the case can close,
    never resolve."""
    identified, held, fallen = _pool_legs(case)
    if identified:
        return frozenset(identified), frozenset(identified | fallen), frozenset()
    if held:
        return frozenset(held), frozenset(held | fallen), frozenset()
    other_standing = any(
        h is not None
        and h.state in _STANDING_HYP_STATES
        and h.root_node_id
        and h.root_node_id not in fallen
        for h in (getattr(case, "hypotheses", None) or {}).values()
    )
    if fallen and not other_standing:
        fixes = _window_fixes(case)
        kinded, targets = fixes[max(fixes)] if fixes else (False, ())
        if not kinded:
            return frozenset(fallen), frozenset(fallen), frozenset()
        named = fallen.intersection(targets)
        if named:
            return frozenset(named), frozenset(fallen), frozenset()
    return (
        frozenset(),
        frozenset(fallen),
        frozenset(
            h.hypothesis_id
            for h in (getattr(case, "hypotheses", None) or {}).values()
            if h is not None
            and h.state == HypothesisState.VALIDATED
            and not h.root_node_id
        ),
    )


def _pool_legs(case: "Case") -> tuple[set, set, set]:
    """``(identified, count-held terminal, fallen)`` roots, as ``_cause_pool``
    reads them; the count-held leg only when nothing is identified."""
    nodes = getattr(case, "causal_nodes", None) or {}
    engine_ids = _engine_absence_row_ids(case)
    recorded = set(
        getattr(getattr(case, "progress", None), "identified_cause_root_ids", None)
        or ()
    )
    identified = {
        node_id
        for node_id, node in nodes.items()
        if getattr(node, "is_identified_root", False)
    }
    fallen = {
        node_id
        for node_id, node in nodes.items()
        if getattr(node, "node_type", None) == NodeType.ROOT
        and (
            node_id in recorded
            or any(
                link.stance == EvidenceStance.REFUTES and link.evidence_id in engine_ids
                for link in node.evidence_links
            )
        )
    }
    held: set = set()
    if not identified and nodes:
        count_held_fn = _graph_hooks().get("count_held")
        held = {
            node_id
            for node_id in (count_held_fn(case) if count_held_fn is not None else ())
            if node_id in nodes and nodes[node_id].root_terminal is True
        }
    return identified, held, fallen


def conclusion_node(
    case: "Case", hypothesis_id: str | None, named: str | None
) -> str | None:
    """The standing hypothesis root a root-cause conclusion names, read only
    for a fix's licence (#2036 D2.3): the root of the hypothesis it is linked
    to (``hypothesis_id``) while that hypothesis stands, else the node it
    names (``named``) when a standing hypothesis is rooted there; None
    otherwise."""
    nodes = getattr(case, "causal_nodes", None) or {}
    roots = {
        h.hypothesis_id: h.root_node_id
        for h in (getattr(case, "hypotheses", None) or {}).values()
        if h is not None and h.state in _STANDING_HYP_STATES and h.root_node_id in nodes
    }
    if hypothesis_id in roots:
        return roots[hypothesis_id]
    return named if named in roots.values() else None


def licensed_roots(case: "Case") -> frozenset:
    """The causes a fix's licence (M5) can name when no root is identified or
    count-held: the root of every standing rooted hypothesis. A conclusion
    naming an orphan or a ruled-out root licenses no remediation of it
    (#2485)."""
    nodes = getattr(case, "causal_nodes", None) or {}
    return frozenset(
        h.root_node_id
        for h in (getattr(case, "hypotheses", None) or {}).values()
        if h is not None
        and h.state in _STANDING_HYP_STATES
        and h.root_node_id in nodes
        and nodes[h.root_node_id].node_type == NodeType.ROOT
    )


def subject_index(case: "Case") -> SubjectIndex:
    """The case's ``SubjectIndex``, built now."""
    pv = getattr(case, "problem_verification", None)
    node_supports: dict = {}
    for node_id, node in (getattr(case, "causal_nodes", None) or {}).items():
        for link in node.evidence_links:
            if link.stance == EvidenceStance.SUPPORTS:
                node_supports.setdefault(link.evidence_id, set()).add(node_id)
    hypothesis_supports: dict = {}
    for hyp in (getattr(case, "hypotheses", None) or {}).values():
        for link in getattr(hyp, "evidence_links", None) or []:
            if hyp is not None and link.stance == EvidenceStance.SUPPORTS:
                hypothesis_supports.setdefault(link.evidence_id, set()).add(
                    hyp.hypothesis_id
                )
    pool_roots, window_roots, pool_hypotheses = _cause_pool(case)
    citations = {
        node_id: frozenset(
            (getattr(node, "metadata", None) or {}).get("root_terminal_evidence_ids")
            or ()
        )
        for node_id, node in (getattr(case, "causal_nodes", None) or {}).items()
    }
    return SubjectIndex(
        case=case,
        rows={
            e.evidence_id: e
            for e in (getattr(case, "evidence", None) or [])
            if getattr(e, "evidence_id", None)
            and getattr(e, "collected_by", None) != ENGINE_EVIDENCE_AUTHOR
        },
        verifying=tuple(
            dict.fromkeys(getattr(pv, "verifying_evidence_ids", None) or ())
        ),
        pool_roots=pool_roots,
        window_roots=window_roots,
        pool_hypotheses=pool_hypotheses,
        node_supports={k: frozenset(v) for k, v in node_supports.items()},
        hypothesis_supports={k: frozenset(v) for k, v in hypothesis_supports.items()},
        citations=citations,
        upload_turns={
            f.file_id: f.uploaded_at_turn
            for f in (getattr(case, "uploaded_files", None) or [])
        },
        _bound={},
    )


def observation_subject(case: "Case", row) -> str:
    """What a post-fix observation confirms: ``"problem"``, ``"cause"`` or
    ``"unbound"`` (``SubjectIndex.subject``, against the confirmation
    pool)."""
    return subject_index(case).subject(row)


# ---------------------------------------------------------------------------
# Resolution-confirmation row qualification (§9.5) — the ONE definition of
# "an absence row that can stand as a resolution confirmation", shared by the
# resolution-readiness gate, the closure→resolve pivot (both in
# ``terminal_transitions``) and the confirm-stamp's candidate filter below.
# Before this was shared, the stamp carried the discipline and the gate did
# not: the gate read READY off the mere existence of ANY causal_absence row —
# including the ENGINE's own M6 failed-fix DISCONFIRMATION rows (minted by
# ``causal_graph._attach_engine_refutation``), so a failed fix satisfied the
# gate's "confirmation the problem is now resolved" (#656). A confirmation
# has two legs (#1906): the cause observed removed (``cause_elimination_rows``,
# the stamp's candidates) and the reported problem observed gone after it
# (``symptom_elimination_rows``); the gate and the pivot need both
# (``resolution_confirmation_rows``). Neither leg admits what a failed fix
# window holds (``fix_windows``, #1927).
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


def cause_elimination_rows(
    case: "Case", *, subjects: "SubjectIndex | None" = None, windows=None
) -> list:
    """The case's causal_absence rows that stand as the CAUSE leg of a
    resolution confirmation — the cause was observed removed:

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
    - In the latest fix window, and not a failed one (``confirming_window``,
      read off ``fix_windows``): a resolution confirms the latest fix, so rows
      from before it confirm nothing about it, and a fix seen failing — the
      cause shown removed, then the problem seen again — confirms nothing,
      whatever is recorded in its window afterwards. Only a new recorded fix
      opens a window that can confirm (#1927). M6 demotes on the same
      windows, so the gate and M6 cannot disagree about whether a fix
      failed.
    - About the case's cause (#2036): none while identification is held on an
      open root (a validated cause whose why is unanswered is not yet the
      root cause, so nothing shows the root cause removed), and none recorded
      before that cause was attached as a hypothesis's root
      (``_cause_posited_turn``). Twin of the rule that a cause cannot be shown
      removed before the problem is verified
      (``_reclassify_premature_causal_absence``).
    - Bound to the cause (#1906 D1, ``SubjectIndex.subject``): it names the
      cause's cited basis (#2557). A row naming nothing shows nothing removed.

    Every turn comparison reads ``observed_turn`` (#2145): the turn the row's
    data was submitted, not the turn it was written.

    Content-level bearing on a specific root and the per-root refutation
    window are the stamp's additional, root-scoped concerns
    (``_select_bearing_row`` / ``_root_disconfirmation_turn``); this predicate
    is the case-level metadata bar the readiness gate shares.

    A cause-side observation only: "the config now reads 100", "ExecStart now
    names the installed binary". Whether the reported problem went with the
    cause is the other leg, ``symptom_elimination_rows`` (#1906).

    ``subjects`` and ``windows`` are the caller's ``subject_index(case)`` and
    ``fix_windows(case)`` when it already has them."""
    return [
        e
        for e, why in cause_recheck_status(case, subjects=subjects, windows=windows)
        if why is None
    ]


def cause_recheck_status(
    case: "Case", *, subjects: "SubjectIndex | None" = None, windows=None
) -> list:
    """Every cause-bound causal_absence row, as ``(row, why)``: ``why`` is
    None for a row of the cause leg (``cause_elimination_rows``), else why it
    does not count yet. The ``<fix_verification>`` block shows the second
    kind, so the model asks for what is missing, not for a re-check already on
    record (#1906 D7). A row bound only to a fix's window is listed with
    ``uncited_recheck_why`` (#2557)."""
    subjects = subject_index(case) if subjects is None else subjects
    held_open = getattr(case.progress, "cause_identification_held_open", False)
    disconfirmation_ids = _disconfirmation_row_ids(case)
    last_disconfirm = latest_disconfirmation_turn(case)
    posited = _cause_posited_turn(case)
    windows = fix_windows(case, subjects) if windows is None else windows
    window = confirming_window(case, windows=windows, subjects=subjects)
    no_window = None if window is not None else _no_confirming_window(case, windows)
    window_bound = {e.evidence_id for w in windows for e in w.cause_rows}
    uncited = uncited_recheck_why(subjects)
    out = []
    for e in getattr(case, "evidence", None) or []:
        bound = subjects.subject(e) == SUBJECT_CAUSE
        if not bound and (
            e.category != EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
            or e.evidence_id not in window_bound
        ):
            continue
        turn = subjects.observed(e)
        if held_open:
            why = "the cause's identification is held open"
        elif e.evidence_id in disconfirmation_ids:
            why = "it is a failed fix's disconfirmation"
        elif turn < posited:
            why = f"its data predates the cause's positing (turn {posited})"
        elif turn < last_disconfirm:
            why = f"its data predates the failed fix seen at turn {last_disconfirm}"
        elif window is None:
            why = no_window
        elif not window.holds(turn):
            why = "it is outside the latest observed fix's window"
        elif not bound and (
            window.kind is None
            or e.evidence_id in {r.evidence_id for r in window.cause_rows}
        ):
            why = uncited
        elif window.kind is not None and e not in window.confirmed_cause_rows:
            why = (
                f"it re-checks a cause the fix executed at turn {window.fix_turn} "
                "was not run against"
            )
        else:
            why = None
        out.append((e, why))
    return out


def uncited_recheck_why(subjects: "SubjectIndex") -> str:
    """Why a window-bound cause re-check confirms nothing (#2557), naming the
    cited basis to re-check instead (``SubjectIndex.bindable_rows``); the one
    copy of the clause, which the unbound-row feedback reads too."""
    if not subjects.pool_roots:
        return "no root cause is identified to confirm"
    _problem, cause = subjects.bindable_rows()
    ids = ", ".join(e.evidence_id for e in cause[:5])
    if ids:
        return (
            "it re-checks a row the root's verdict does not cite; name one of "
            f"{ids} (the cited basis)"
        )
    return "the root cause's terminal verdict cites no row yet"


def remediations_at(case: "Case", turn: int | None) -> list:
    """The accepted remediations executed at ``turn``, in record order: the
    fixes whose window ``fix_windows`` dates there; ``[]`` for window 0."""
    return [
        a
        for a in _dated_fixes(case)
        if a.accepted_in_turn == turn and _is_remediation(a)
    ]


def _no_confirming_window(case: "Case", windows: list) -> str:
    """Why no window confirms (``confirming_window`` is None): the latest
    attempt was no fix of the cause (``off_cause_attempt``, #2036 D2.3), the
    latest observed fix failed, or it acted on a cause no longer identified
    (``off_cause``). When a newer fix is recorded that nobody has observed
    yet, it is named as the one still to re-check, not as missing (#2495). A
    failed window 0 is named as the fix done outside the case (#2501)."""
    off = off_cause_attempt(case, windows)
    if off is not None:
        return (
            f"the fix executed at turn {off} did not act on the cause now "
            "identified; only a fix of that cause can confirm it"
        )
    observed = [i for i, w in enumerate(windows) if w.observed]
    last = windows[observed[-1]] if observed else None
    after = windows[observed[-1] + 1 :] if observed else []
    newer = [w for w in after if w.fix_turn is not None]
    pending = (
        f"; the newer fix (executed turn {newer[-1].fix_turn}) is not re-checked yet"
        if newer
        else None
    )
    if last is not None and last.state == FIX_FAILED:
        which = (
            "the fix done outside the case"
            if last.fix_turn is None
            else f"the fix executed at turn {last.fix_turn}"
        )
        seen = f"{which} was seen failing at turn {last.failure_turn}"
        if pending:
            return seen + pending
        if last.fix_turn is None:
            return f"{seen}; only a new fix can confirm"
    elif last is not None and last.off_cause:
        seen = (
            f"the fix executed at turn {last.fix_turn} acted on a cause no longer "
            "identified"
        )
        return seen + (
            pending or "; only a fix of the cause now identified can confirm it"
        )
    return "the latest fix was seen failing; only a new fix can confirm"


def confirming_polarity_rows(case: "Case") -> list:
    """The causal_absence rows a report may render as confirming evidence:
    INV-30's polarity guard, ``cause_elimination_rows``' bars before #1906 2a
    without the binding. Each row is:

    - not engine-authored, and not a failed-fix disconfirmation (the
      opposite polarity);
    - at or after the latest engine-known disconfirmation and the turn the
      case's cause was posited;
    - not recorded while identification is held open;
    - in the latest fix window holding a causal_absence row, and that window
      did not FAIL and is not ``off_cause``. Windows open on remediations
      only, so a row after an interception alone is in none, and a fix of a
      cause no longer identified confirms nothing (#2036 D2.3). This stands
      for main's
      confirming window (the latest
      observed window, none when it failed) minus its persistence-only arm,
      which read the token frame 2a removed: a newer window observed only by
      the problem persisting no longer shadows the older window's rows.

    Deliberately not the resolution's cause leg, which also needs the row
    bound to the cause, as no row recorded before 2a is. A regenerated report
    of a case resolved on such rows, its stamped confirmation included, keeps
    citing them (#1906 review R5). Turns are the data's (``observed_turn``)."""
    if getattr(case.progress, "cause_identification_held_open", False):
        return []
    subjects = subject_index(case)
    rows = [
        e
        for e in subjects.rows.values()
        if e.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
    ]
    latest = next(
        (
            window
            for window in reversed(fix_windows(case))
            if any(window.holds(subjects.observed(e)) for e in rows)
        ),
        None,
    )
    if latest is None or latest.state == FIX_FAILED or latest.off_cause:
        return []
    disconfirmation_ids = _disconfirmation_row_ids(case)
    floor = max(latest_disconfirmation_turn(case), _cause_posited_turn(case))
    return [
        e
        for e in rows
        if latest.holds(subjects.observed(e))
        and e.evidence_id not in disconfirmation_ids
        and subjects.observed(e) >= floor
    ]


def confirmation_stamp_rows(case: "Case") -> list:
    """The confirm-stamp's record: the causal_absence rows SUPPORTS-linked on
    a ROOT node, the link ``root_counterfactually_confirmed`` reads; engine
    rows, the M6 disconfirmations, are left out. Read off the record, not a
    re-derived pool or window: the stamp's count-held re-derive can narrow
    the pool so the row it just recorded no longer binds, and every window
    reading after the stamp sees that narrowed pool (#2487).

    The stamp (``confirm_root_from_resolution_absence``, at the RESOLVED
    transition) has been the link's one writer since #663's ingest refusal.
    Residual, accepted (ruled 2026-11-04 on #2487): a model-authored link
    written before that refusal would be read as the stamp's too. No stored
    case carries one, and no current writer produces one."""
    stamped = {
        link.evidence_id
        for node in (getattr(case, "causal_nodes", None) or {}).values()
        if node.node_type == NodeType.ROOT
        for link in node.evidence_links
        if link.stance == EvidenceStance.SUPPORTS
    }
    return [
        e
        for e in getattr(case, "evidence", None) or []
        if e.evidence_id in stamped
        and e.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        and getattr(e, "collected_by", None) != ENGINE_EVIDENCE_AUTHOR
    ]


def _cause_posited_turn(case: "Case") -> int:
    """The turn from which a causal_absence row can be about the case's cause:
    when that cause was attached as a hypothesis's root (#2036). The cause is,
    in order: the identified root; else the root the root-cause conclusion names
    (the backstop leg's cause); else the latest-attached standing root, the
    conservative reading when nothing names one. With no rooted standing
    hypothesis, every turn (0). An attach is dated at the turn the work was
    emitted, a replayed bundle's own turn (#2537), so the rows recorded with
    the cause count.

    A row recorded earlier observed something else gone: a renewed credential or
    a restarted issuer, before the chain was deepened to what produced it. A
    mitigation opens no fix window, so without this bound such a row in window 0
    would confirm the deeper cause's resolution before its fix ran."""
    nodes = getattr(case, "causal_nodes", None) or {}
    hypotheses = getattr(case, "hypotheses", None) or {}
    standing = {
        h.hypothesis_id: nodes[h.root_node_id]
        for h in hypotheses.values()
        if h is not None and h.state in _STANDING_HYP_STATES and h.root_node_id in nodes
    }
    pool = [n for n in standing.values() if n.is_identified_root]
    if not pool:
        rcc = getattr(case, "root_cause_conclusion", None)
        named = standing.get(getattr(rcc, "validated_hypothesis_id", None) or "")
        named_node = nodes.get(getattr(rcc, "names_root_node_id", None) or "")
        pool = [
            n for n in (named, named_node) if n is not None and n in standing.values()
        ] or list(standing.values())
    return max((n.root_attached_turn for n in pool), default=0)


def fix_application_turn(case: "Case") -> int | None:
    """The turn at which the case RECORDS that a fix was executed, or ``None``
    when no such record exists — M6's first precondition (#987), and the
    anchor of a resolution's problem leg (#1906, ``symptom_elimination_rows``).

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

    Any attempt counts, an interception included (#2036 D2.3): a fix was run,
    so a re-check after it names what it re-checks. The windows a resolution
    and M6 read open on remediations only (``fix_windows``).
    """
    turns = _dated_fix_turns(case)
    return turns[-1] if turns else None


def _dated_fix_turns(case: "Case") -> list[int]:
    """The execution turns of every accepted SOLUTION, ascending and distinct,
    remediations and interceptions alike: the fix record
    ``fix_application_turn`` reads it, and window 0 ends at its first."""
    return sorted({a.accepted_in_turn for a in _dated_fixes(case)})


def _dated_fixes(case: "Case") -> list:
    """Every accepted SOLUTION with an execution turn, in record order."""
    return [
        a
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


#: A fix attempt's kind (``ProposedAction.fix_kind``, #2036 D2.3).
FIX_REMEDIATION = "remediation"
FIX_INTERCEPTION = "interception"


def _is_remediation(action) -> bool:
    """A remediation, or an unkinded attempt (accepted before D2.3, or
    licensed only by a conclusion naming no node), which reads as one."""
    return getattr(action, "fix_kind", None) != FIX_INTERCEPTION


def _window_fixes(case: "Case") -> dict:
    """The cause windows' fixes: each execution turn holding a remediation,
    mapped to ``(kinded, targets)``. A turn is read as kinded only when every
    remediation in it is, its targets their union; one with an unkinded
    remediation reads as before D2.3. An interception opens no window."""
    turns: dict = {}
    for a in _dated_fixes(case):
        if _is_remediation(a):
            kinded, targets = turns.get(a.accepted_in_turn, (True, ()))
            turns[a.accepted_in_turn] = (
                kinded and getattr(a, "fix_kind", None) is not None,
                (*targets, *(getattr(a, "target_ids", None) or ())),
            )
    return {
        turn: (kinded, tuple(sorted(set(targets))) if kinded else ())
        for turn, (kinded, targets) in turns.items()
    }


# ---------------------------------------------------------------------------
# Fix windows: THE one answer to "did the latest fix work?" (#1927).
#
# The readiness gate and M6 used to answer it separately from the same rows —
# the gate "it worked" (both legs), M6 "it failed" — coupled only by marks M6
# wrote into the rows the gate reads (an engine row's window, REFUTES links).
# Every defect in the #987 → #1906 → #1927 chain was a state where the two
# disagreed, and each new row shape that escaped the marks reopened it. Both
# now read this predicate: the gate's legs exclude what a failed fix window
# holds, and M6 demotes on a failed window.
# ---------------------------------------------------------------------------

FIX_UNKNOWN = "unknown"
FIX_SUCCEEDED = "succeeded"
FIX_FAILED = "failed"


@dataclass(frozen=True)
class FixWindow:
    """One dated fix and what the case observed after it, until the next.

    ``fix_turn`` is the fix's execution turn (``accepted_in_turn``); ``None``
    for window 0, the rows before any dated fix (an out-of-band fix leaves no
    execution record). ``end_turn`` is the next fix's execution turn, or None
    for the latest. ``cause_rows`` are the window's causal_absence rows bound
    to, or naming a row cited by, a cause of the window pool, and
    ``confirmed_cause_rows``, those bound
    by citation to the confirmation pool (``_cause_pool``, #2557): any re-check
    of the cause the fix addressed can FAIL the window, but only one of the
    case's confirmable cause can make it SUCCEEDED. ``failure_turn`` is the
    turn the problem was observed persisting
    after the cause's removal, when ``state`` is ``FIX_FAILED``.
    ``relief_turn`` is the first turn the problem was observed gone in the
    window, before any failure; on a FAILED window 0, the relief its failure
    read (``_window_zero_relapse``). ``disconfirmation`` is the earliest
    ``(cause_turn, persistence_turn)`` pair that disconfirms the cause the
    fix addressed (``disconfirms_cause``), or None. ``observed``: the fix's
    outcome was looked at — a cause row in the window, or a persistence row
    bound to the problem after the fix.

    ``kind`` is ``FIX_REMEDIATION`` for a window whose fixes were kinded
    when run (#2036 D2.3), with their ``targets``; None for window 0 and for
    an unkinded fix, read as before D2.3. ``off_cause``: a remediation whose
    targets have all left the confirmation pool (deepened past, or another
    cause identified since), which can fail but never confirm."""

    fix_turn: int | None
    end_turn: int | None
    cause_rows: tuple
    state: str
    failure_turn: int | None = None
    observed: bool = False
    relief_turn: int | None = None
    disconfirmation: tuple[int, int] | None = None
    confirmed_cause_rows: tuple = ()
    targets: tuple = ()
    kind: str | None = None
    off_cause: bool = False

    def holds(self, turn: int) -> bool:
        """Whether an observation at ``turn`` belongs to this window."""
        return (self.fix_turn is None or turn >= self.fix_turn) and (
            self.end_turn is None or turn < self.end_turn
        )

    @property
    def disconfirms_cause(self) -> bool:
        """The window records the counterfactual disconfirmation a destructive
        reader may act on (INV-42): the cause observed removed, then the
        problem observed present, never observed gone in between.

        A relapse alone — the problem seen gone after the fix, then seen again
        — FAILS the window, and the gate reads it so: only a new fix earns
        RESOLVED. It disproves nothing about the cause: the problem went with
        the cause, and its return may be the cause back, the effect not
        lasting, another cause, or a misread check (#2039). The cause re-checked
        still gone after the return, with the problem seen present after that,
        is a disconfirmation again."""
        return self.disconfirmation is not None


def fix_windows(
    case: "Case", subjects: "SubjectIndex | None" = None
) -> list[FixWindow]:
    """Every fix window on the case, oldest first: window 0 (the rows before any
    dated fix — an out-of-band fix leaves no execution record), then one per
    turn with a dated remediation (``_window_fixes``). An interception opens
    none (#2036 D2.3): it relieves, never resolves, and a later one does not
    hide an earlier root fix's window. It ends window 0, and an unkinded
    window that has FAILED by then (below).

    A window's outcome is read from raw observations (non-engine rows; no
    engine window or REFUTES mark is consulted, so the derivation stands on
    its own), each bound to the subject it names (#1906 D1,
    ``SubjectIndex.subject``) and dated by its data (``observed_turn``,
    #2145):

    - the cause observed removed: a cause-bound causal_absence row in the
      window;
    - the problem observed gone: a problem-bound symptom_absence row in the
      window;
    - the problem observed persisting: a problem-bound SYMPTOM_EVIDENCE row
      at or after the cause's removal and in a turn AFTER the fix's execution
      turn — a row in that turn may quote pre-fix lines from the same paste
      (the owner's ruling) — up to and INCLUDING the next fix's execution
      turn: a "the problem is back" row in that turn cannot be the next fix's
      failure for the same reason, so it is this window's relapse. A row
      naming none of the rows that showed the problem is not the problem
      persisting: a probe's own failure, a sibling candidate's symptom.

    Read in turn order, the first persistence after the cause's removal makes
    the window FAILED, and FAILED is final: no later relief — a mitigation, a
    delayed effect, a re-check of the cause — makes a fix that was seen
    failing a success. Only a new fix opens a window that can. A turn holding
    both a problem-gone row and a persistence row reads as gone: turn
    granularity cannot order them, and a same-turn ambiguity resolves toward
    the confirmation (NO-COLLAPSE), as for the disconfirmation window and the
    mixed single-turn shape. A relief followed by a later persistence is a
    relapse, FAILED: the gate reads it as a failed fix, a destructive reader
    does not, unless the cause was observed removed after the relief and the
    problem present after that (``FixWindow.disconfirms_cause``). SUCCEEDED
    needs the cause observed removed as well, by a row bound to the
    confirmation pool (``FixWindow.confirmed_cause_rows``): a re-check of a
    fallen cause after another cause's fix fails that fix's window like any
    re-check, but never confirms it;
    without a cause row the window stays UNKNOWN ("I ran it, still failing"
    with no cause re-check may be an implementation error).

    A kinded window is read against the cause its fixes were run against
    (#2036 D2.3). Its cause rows bind to its ``targets`` plus the roots that
    fell and are not the cause now (identified or count-held), so a
    deepening or an M6 demotion since does not re-scope it, and a re-check
    of a cause identified since is not its own. Only the cited basis of its
    targets still in the confirmation pool confirms it, linked or not (#2557;
    ``off_cause`` when none is). It spans interceptions to the next remediation. An unkinded
    window binds as above, and ends at the next attempt of any kind by which
    it has FAILED, as every attempt ended a window before D2.3 (ruling 1);
    while live it spans interceptions, as a kinded one does.

    Window 0 ends at the first attempt of any kind, and fails only on a
    relapse (#2501, ``_window_zero_relapse``): the identified cause shown
    removed after it was first posited, then the problem gone, then present.
    The relapse binds its cause rows to the confirmation roots and to every
    root deepened into one of them (``deepened_to``, transitively), so it
    stands after the cause is deepened, however often, until a dated fix; one
    read only through a cited row (the window's confirmation-bound rows, #2557)
    does not survive a re-root, which erases the citation (#2582). A
    row's floor is the earliest first attach of the roots it supports; a
    later attach of the same root does not move it (save once on a root
    stored with neither key, ``CausalNode.root_first_attached_turn``).
    Persistence with no such relief fails nothing: before any dated fix, a
    causal_absence row is as likely a diagnosis-time exclusion ("DNS ruled
    out") as a fix's outcome. It never carries a disconfirmation, so nothing
    destructive acts on an undated fix."""
    fixes = _window_fixes(case)
    attempts = _dated_fix_turns(case)
    subjects = subject_index(case) if subjects is None else subjects
    nodes = getattr(case, "causal_nodes", None) or {}
    identified, held, fallen = _pool_legs(case)
    # The roots that fell and are not the cause now: a kinded window keeps
    # observing them, never a cause identified since (#2036 D2.3).
    fell = frozenset(fallen - identified - held)
    # (category, subject, observed turn) of every bound row.
    bound = [
        (e.category, subject, subjects.observed(e), e)
        for e in subjects.rows.values()
        if (subject := subjects.subject(e, windows=True)) != SUBJECT_UNBOUND
    ]
    absence = [
        e
        for e in subjects.rows.values()
        if e.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
    ]

    def bound_to(roots, shell, cited=False) -> tuple:
        return tuple(
            e
            for e in absence
            if roots
            and shell.holds(subjects.observed(e))
            and subjects.subject(e, roots=roots, cited=cited) == SUBJECT_CAUSE
        )

    def cited_by(roots, shell) -> tuple:
        cited = set().union(*(subjects.citations.get(n, ()) for n in roots))
        return tuple(
            e
            for e in absence
            if getattr(e, "rechecks_evidence_id", None) in cited
            and shell.holds(subjects.observed(e))
        )

    def merged(*rows) -> tuple:
        inside = {e.evidence_id for e in rows}
        return tuple(e for e in absence if e.evidence_id in inside)

    def read(fix_turn: int | None, end_turn: int | None) -> FixWindow:
        shell = FixWindow(fix_turn, end_turn, (), FIX_UNKNOWN)
        kinded, targets = fixes.get(fix_turn, (False, ()))
        confirming = subjects.pool_roots.intersection(targets)
        own = fell | nodes.keys() & set(targets) if kinded else subjects.window_roots
        if kinded:
            observed_rows = bound_to(own, shell)
            confirmed_cause_rows = bound_to(confirming, shell, cited=True)
        else:
            observed_rows = tuple(
                e
                for category, subject, turn, e in bound
                if subject == SUBJECT_CAUSE and shell.holds(turn)
            )
            confirmed_cause_rows = tuple(
                e
                for e in absence
                if shell.holds(subjects.observed(e))
                and subjects.subject(e) == SUBJECT_CAUSE
            )
        # A row its own roots cite is inside the window as a linked one is, also
        # once the cause falls; it confirms nothing the confirmation read refuses.
        cause_rows = merged(
            *observed_rows, *confirmed_cause_rows, *cited_by(own, shell)
        )
        gone_turns = {
            turn
            for category, subject, turn, e in bound
            if category == EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
            and shell.holds(turn)
        }
        # The problem observed after the fix: in a turn after the fix's
        # execution turn, up to and including the next one's.
        after_fix: set[int] = set()
        if fix_turn is not None:
            after_fix = {
                turn
                for category, subject, turn, e in bound
                if category == EvidenceCategory.SYMPTOM_EVIDENCE
                and turn > fix_turn
                and (end_turn is None or turn <= end_turn)
            }
        # A failure needs the cause observed removed first.
        persist_turns: set[int] = set()
        if cause_rows:
            removal = min(subjects.observed(r) for r in cause_rows)
            persist_turns = {turn for turn in after_fix if turn >= removal}
        # The counterfactual disconfirmation (INV-42): the cause observed
        # removed at r, the problem observed present at p >= r, and no
        # problem-gone row of this window in [r, p] — one in r's or p's turn
        # reads as gone, as below; one in the next fix's turn belongs to that
        # window. The earliest such pair; window 0 has none (no ``after_fix``:
        # its relapse is read apart, ``_window_zero_relapse``).
        cause_turns = sorted({subjects.observed(row) for row in cause_rows})
        disconfirmation: tuple[int, int] | None = None
        for p in sorted(after_fix):
            last_gone = max((g for g in gone_turns if g <= p), default=-1)
            r = next((c for c in cause_turns if last_gone < c <= p), None)
            if r is not None:
                disconfirmation = (r, p)
                break
        state, failure_turn, relief_turn = FIX_UNKNOWN, None, None
        for turn in sorted(gone_turns | persist_turns):
            if turn in gone_turns:
                if relief_turn is None:
                    relief_turn = turn
            else:
                state, failure_turn = FIX_FAILED, turn
                break
        if state != FIX_FAILED and relief_turn is not None and confirmed_cause_rows:
            state = FIX_SUCCEEDED
        if fix_turn is None:
            zero = _with_deepened_from(case, subjects.pool_roots)
            relapse = _window_zero_relapse(
                case,
                bound,
                gone_turns,
                merged(*bound_to(zero, shell), *confirmed_cause_rows),
                subjects,
                end_turn,
                zero or None,
            )
            if relapse is not None:
                state = FIX_FAILED
                relief_turn, failure_turn = relapse
        return FixWindow(
            fix_turn,
            end_turn,
            cause_rows,
            state,
            failure_turn,
            # A relapse read through a deepened-past root is observed too.
            observed=bool(cause_rows or after_fix or state == FIX_FAILED),
            relief_turn=relief_turn,
            disconfirmation=disconfirmation,
            confirmed_cause_rows=confirmed_cause_rows,
            targets=targets,
            kind=FIX_REMEDIATION if kinded else None,
            off_cause=kinded and not confirming,
        )

    turns = sorted(fixes)
    windows = [read(None, attempts[0] if attempts else None)]
    for fix_turn, following in zip(turns, [*turns[1:], None]):
        if fixes[fix_turn][0]:
            windows.append(read(fix_turn, following))
            continue
        # An unkinded window ends at the first interception by which it has
        # FAILED, so a failed legacy fix absorbs no re-check after it; while
        # live it spans them, so its verdict stays the one read before D2.3.
        intercepted = [
            t for t in attempts if t > fix_turn and (following is None or t < following)
        ]
        for end in [*intercepted, following]:
            window = read(fix_turn, end)
            if window.state == FIX_FAILED:
                break
        windows.append(window)
    return windows


def _with_deepened_from(case: "Case", roots) -> frozenset:
    """``roots`` plus every root deepened into one of them, transitively: the
    attach writer records ``deepened_to`` on a root a hypothesis was re-rooted
    past (``causal_graph.pruning.attach_root``, #2036 D2.3)."""
    nodes = getattr(case, "causal_nodes", None) or {}
    out, frontier = set(roots), set(roots)
    while frontier:
        frontier = {
            node_id
            for node_id, node in nodes.items()
            if node_id not in out
            and (getattr(node, "metadata", None) or {}).get("deepened_to") in frontier
        }
        out |= frontier
    return frozenset(out)


def _window_zero_relapse(
    case: "Case", bound, gone_turns, cause_rows, subjects, end_turn, roots=None
) -> tuple[int, int] | None:
    """``(relief_turn, failure_turn)`` when window 0 relapsed, else None
    (#2501): the identified cause shown removed at or after it was first
    posited (``_first_posited_turn``: an earlier row observed something else
    gone), then the problem shown gone, then a problem-bound SYMPTOM_EVIDENCE
    row in a later turn, up to and including the next fix's execution turn. A
    turn holding both reads as gone, as in a dated window. ``cause_rows`` are
    window 0's, bound against ``roots`` (the confirmation roots and the roots
    deepened into them), or against the confirmation pool when None."""
    if not cause_rows:
        return None
    removal = min(
        (
            turn
            for row in cause_rows
            if (floor := _first_posited_turn(case, subjects, row, roots)) is not None
            and (turn := subjects.observed(row)) >= floor
        ),
        default=None,
    )
    if removal is None:
        return None
    relief = min((g for g in gone_turns if g >= removal), default=None)
    if relief is None:
        return None
    failure = min(
        (
            turn
            for category, _, turn, _ in bound
            if category == EvidenceCategory.SYMPTOM_EVIDENCE
            and turn > relief
            and turn not in gone_turns
            and (end_turn is None or turn <= end_turn)
        ),
        default=None,
    )
    return None if failure is None else (relief, failure)


def _first_posited_turn(case: "Case", subjects, row, roots=None) -> int | None:
    """The first turn what cause-bound ``row`` re-checks was posited: the
    earliest ``CausalNode.root_first_attached_turn`` of the roots it supports
    (bound against ``roots`` when given, a cluster counting as one cause) or
    else cites (#2557), or a rootless pool hypothesis's ``generated_at_turn``; None when it binds
    to nothing. Both are dated at the turn the work was emitted, a replayed
    bundle's own turn (#2537). Unlike ``_cause_posited_turn``, the cause
    leg's floor, a later attach of the same root does not move it, save once
    on a root stored with neither key (``CausalNode.root_first_attached_turn``);
    a re-check after that attach then confirms window 0."""
    nodes = getattr(case, "causal_nodes", None) or {}
    hypotheses = getattr(case, "hypotheses", None) or {}
    named = row.rechecks_evidence_id
    turns = [
        (
            nodes[target].root_first_attached_turn
            if target in nodes
            else hypotheses[target].generated_at_turn
        )
        for target in (
            subjects.cause_targets(named, roots=roots) or subjects.cause_targets(named)
        )
        if target in nodes or hypotheses.get(target) is not None
    ]
    return min(turns, default=None)


def latest_fix_window(case: "Case") -> FixWindow:
    """The latest fix window — window 0 when no fix is dated."""
    return fix_windows(case)[-1]


def latest_decided_fix_window(case: "Case") -> FixWindow | None:
    """The latest window with a verdict (SUCCEEDED or FAILED), or None. A
    newer window whose fix has not been observed yet does not erase an older
    window's failure: "still 503, so I'm putting pgbouncer in" records the
    failure and the next fix in one turn."""
    for window in reversed(fix_windows(case)):
        if window.state != FIX_UNKNOWN:
            return window
    return None


def confirming_window(
    case: "Case", *, windows=None, subjects: "SubjectIndex | None" = None
) -> FixWindow | None:
    """The fix window a resolution must be confirmed in, or None.

    "Did the latest fix work?" is the question, so it is the latest window
    whose fix's outcome was looked at (``FixWindow.observed``: the cause
    re-checked, or the problem seen persisting after the fix), and None when
    that window FAILED: a fix seen failing confirms nothing, and only a new
    fix opens a window that can. None too when it is ``off_cause``: a fix of
    a cause no longer in the pool confirms nothing about the one that is
    (#2036 D2.3). Rows from before a newer, observed fix
    confirm nothing about it — an earlier fix's, an out-of-band one's, a
    pre-fix cause observation. A newer fix whose outcome nobody looked at yet
    has no verdict, so the window before it still answers: the execution
    stamp can lag the user's report ("the config reads 100", the acceptance,
    then "checkout works"), and a problem-gone row alone — a relief, possibly
    a workaround's — does not look at the fix. When the model declares that the
    user reported the fix in their previous message
    (``accepted_fix_reported_in``, #2427), the fix is dated to that message
    within its bound (``stage_gates._date_to_the_report``), so its window holds
    that message's rows and the registration turn's paste is post-fix. Read
    off ``fix_windows`` alone, so the gate reads a failed fix the same whether
    or not M6 could act on
    it. With no dated fix it is window 0, the whole case: an out-of-band fix
    confirms on its rows, and nothing once it relapsed (#2501). None when
    nothing was observed at all (no leg can stand then). ``windows`` is the
    caller's ``fix_windows(case)``."""
    if windows is None:
        windows = fix_windows(case, subjects)
    for window in reversed(windows):
        if window.observed:
            return None if window.state == FIX_FAILED or window.off_cause else window
    return None


def off_cause_attempt(case: "Case", windows=None) -> int | None:
    """The execution turn of the latest fix attempt when no fix of the cause
    now identified can still confirm it, else None (#2036 D2.3): an
    interception with no remediation window before it, or after a FAILED or
    ``off_cause`` one; or that ``off_cause`` remediation itself. A live
    remediation window answers for itself, an interception after it
    included. None while the confirmation pool is empty: then there is no
    cause to ask a fix of. ``windows`` is the caller's ``fix_windows(case)``."""
    confirmation, _window, hypotheses = _cause_pool(case)
    if not (confirmation or hypotheses):
        return None
    windows = fix_windows(case) if windows is None else windows
    latest = windows[-1]
    live = latest.fix_turn is not None and latest.state != FIX_FAILED
    if live and not latest.off_cause:
        return None
    attempts = _dated_fix_turns(case)
    if attempts and attempts[-1] > (latest.fix_turn or -1):
        return attempts[-1]
    return latest.fix_turn if live else None


def attempt_window(case: "Case", action) -> FixWindow | None:
    """The cause window an accepted SOLUTION's attempt opened, or None: an
    interception opens none."""
    turn = getattr(action, "accepted_in_turn", None)
    if turn is None or not _is_remediation(action):
        return None
    return next((w for w in fix_windows(case) if w.fix_turn == turn), None)


def interception_relief(
    case: "Case", turn: int, subjects: "SubjectIndex | None" = None
) -> tuple[int | None, int | None]:
    """``(relief_turn, present_turn)`` after an interception run at ``turn``,
    which has no window: the latest turn the problem was seen gone, and the
    latest it was seen present, up to the next dated attempt, read as a
    window reads them (bound rows, by the data's turn): a present row in its
    own turn may quote pre-fix lines, and one in the next attempt's turn is
    still this one's. Present after the relief is a relapse. ``subjects`` is
    the caller's ``subject_index(case)``."""
    subjects = subject_index(case) if subjects is None else subjects
    end = next((t for t in _dated_fix_turns(case) if t > turn), None)
    seen: dict = {}
    for e in subjects.rows.values():
        observed = subjects.observed(e)
        if subjects.subject(e) != SUBJECT_PROBLEM or observed < turn:
            continue
        if e.category == EvidenceCategory.SYMPTOM_EVIDENCE:
            if observed == turn or (end is not None and observed > end):
                continue
        elif end is not None and observed >= end:
            continue
        seen[e.category] = max(seen.get(e.category, observed), observed)
    return (
        seen.get(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE),
        seen.get(EvidenceCategory.SYMPTOM_EVIDENCE),
    )


def symptom_elimination_rows(
    case: "Case",
    cause_rows: list | None = None,
    *,
    subjects: "SubjectIndex | None" = None,
    windows=None,
) -> list:
    """The case's symptom_absence rows that stand as the PROBLEM leg of a
    resolution confirmation — the reported problem was observed gone after the
    fix.

    Gone ⇒ gone (M2) is two observations, and the evidence vocabulary records
    them as two rows (evidence-needs-design §4.3): a cause re-check yields
    causal_absence, a symptom re-check yields symptom_absence. Reading the
    cause row alone as the counterfactual is what let a service shown running
    again read resolution-READY while the scrape target the user reported
    down was still unchecked, and the engine then told the user "the problem
    went with it" (#1906).

    - Not engine-authored, as for the cause leg.
    - Bound to the problem (#1906 D1, ``SubjectIndex.subject``): it names one
      of the rows that showed the problem. Without a verifying row on record
      (a legacy case, or a live symptom claim that cited no row on a turn that
      wrote zero or several symptom rows,
      ``response_application._record_verifying_rows``) nothing binds until a
      later turn cites one, so such a case can close, never resolve.
    - At or after the FIX, by the data's turn (``observed_turn``, #2145): the recorded execution of the fix the resolution
      confirms (``confirming_window``) or the earliest cause-elimination row,
      whichever is earlier — the cause row alone for a fix made out of band,
      which leaves no execution record. A user may check the symptom before the
      cause ("the target is UP" at turn 5, "the config reads 100" at turn 6);
      both are after the fix, and anchoring on the cause row would ask them
      again for what they had already confirmed. A symptom_absence row from
      before the fix is a mitigation's relief, a pre-verification "not there
      now", or a false-alarm finding; none shows that removing the cause
      removed the problem. ``>=`` because one turn can carry both the fix and
      its observations, and turn granularity cannot order within-turn events.
    - At or after the latest engine-known failed-fix disconfirmation
      (``latest_disconfirmation_turn``), as for the cause leg: a "looks
      stable" row from a fix window that later failed confirms nothing.

    Empty when no cause leg stands. ``cause_rows``, ``subjects`` and
    ``windows`` are the caller's ``cause_elimination_rows(case)``,
    ``subject_index(case)`` and ``fix_windows(case)`` when it already has
    them."""
    subjects = subject_index(case) if subjects is None else subjects
    if cause_rows is None:
        cause_rows = cause_elimination_rows(case, subjects=subjects, windows=windows)
    anchor = problem_leg_anchor(case, cause_rows, subjects=subjects, windows=windows)
    if anchor is None:
        return []
    return [
        e
        for e in (getattr(case, "evidence", None) or [])
        if getattr(e, "category", None) == EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
        and subjects.subject(e) == SUBJECT_PROBLEM
        and subjects.observed(e) >= anchor
    ]


def problem_leg_anchor(
    case: "Case", cause_rows: list, *, subjects: "SubjectIndex", windows=None
) -> int | None:
    """The turn the problem leg is read from (``symptom_elimination_rows``),
    or None when no cause leg stands (``cause_rows`` empty): the earlier of
    the confirming fix's execution and the earliest cause row's data, and not
    before the latest engine-known failed-fix disconfirmation. The one owner of
    "after the fix" for the problem leg and for the checks the model asked for
    after it (``pending_symptom_check``)."""
    if not cause_rows:
        return None
    anchor = min(subjects.observed(e) for e in cause_rows)
    window = confirming_window(case, windows=windows, subjects=subjects)
    if window is not None and window.fix_turn is not None:
        anchor = min(anchor, window.fix_turn)
    # No window check of its own: the cause leg admits rows only from the
    # confirming window (``confirming_window``), and the anchor is at or after
    # that window's start (its fix's execution turn).
    return max(anchor, latest_disconfirmation_turn(case))


def pending_symptom_check(
    case: "Case", *, subjects: "SubjectIndex | None" = None, windows=None
) -> "EvidenceNeed | None":
    """The check of the problem the model asked for after the fix that is still
    unanswered, or None (#1906 D5.2, rulings of 2026-11-04). While one is held,
    no resolve offer opens. A close is read as on any resolvable case (ruling
    2): without a standing resolve decline, the user's or the model's close
    pivots to the resolve offer; under one, the user's close closes (#2360)
    and the model's is refused.

    - **Candidates:** the case's ``symptom_verification`` needs that are
      outstanding (PENDING or PARTIALLY_MET), or FULFILLED (below). A
      SUPERSEDED need releases the hold: the model's way out when the user
      cannot run the check.
    - **Asked:** the first of ``created_at_turn`` and ``surfaced_turns`` at or
      after the problem leg's anchor (``problem_leg_anchor``), so a re-ask in
      the answer's own turn does not un-answer it. A need with no such turn
      (asked only before the fix) is not held.
    - **Answered (ruling 1):** a problem-bound row (``SubjectIndex.subject``,
      either category) whose data is from a turn strictly after the ask: a
      row in the ask's own turn does not answer it, because the model asked
      when that turn's data did not show it. Or the need FULFILLED citing at
      least one row (``fulfilling_evidence_ids``) whose data is from the ask's
      turn or later: there the model judged that row the answer (a need
      declared and fulfilled in one response, an attachment of the ask's turn
      cited on the next). A FULFILLED need citing only rows from turns before
      the ask still holds, and its state is not demoted. Engine-authored rows
      are unbound (``SubjectIndex.subject``), so no exclusion is needed for
      them.

    Returns the earliest-asked held need. None unless both legs stand
    (``symptom_elimination_rows``): without the cause leg there is no anchor,
    and without the problem leg nothing is held, since the problem's answer
    is then still what readiness asks for. ``subjects`` and ``windows`` are
    the caller's ``subject_index(case)`` and ``fix_windows(case)`` when it has
    them."""
    candidates = [
        n
        for n in (getattr(case, "evidence_needs", None) or [])
        if n.purpose == NeedPurpose.SYMPTOM_VERIFICATION
        and (n.is_outstanding or n.state == NeedState.FULFILLED)
    ]
    if not candidates:
        return None
    subjects = subject_index(case) if subjects is None else subjects
    cause_rows = cause_elimination_rows(case, subjects=subjects, windows=windows)
    anchor = problem_leg_anchor(case, cause_rows, subjects=subjects, windows=windows)
    if anchor is None or not symptom_elimination_rows(
        case, cause_rows, subjects=subjects, windows=windows
    ):
        return None
    latest_answer = max(
        (
            subjects.observed(e)
            for e in subjects.rows.values()
            if subjects.subject(e) == SUBJECT_PROBLEM
        ),
        default=-1,
    )
    observed = {
        e.evidence_id: subjects.observed(e)
        for e in (getattr(case, "evidence", None) or [])
        if getattr(e, "evidence_id", None)
    }
    held = []
    for need in candidates:
        asked = min(
            (t for t in (need.created_at_turn, *need.surfaced_turns) if t >= anchor),
            default=None,
        )
        if asked is None or latest_answer > asked:
            continue
        if need.state == NeedState.FULFILLED and any(
            observed.get(i, -1) >= asked for i in need.fulfilling_evidence_ids
        ):
            continue
        held.append((asked, need))
    return min(held, key=lambda pair: pair[0])[1] if held else None


def resolution_confirmation_rows(case: "Case") -> list:
    """The rows that CONFIRM a resolution — both legs of gone ⇒ gone, the
    cause-elimination rows then the symptom-elimination rows — or ``[]`` when
    either leg is missing (``symptom_elimination_rows`` is empty without a
    cause leg). The resolution gate's READY bar, the closure→resolve pivot's
    trigger and the resolve offer's signature all read this, so a cause shown
    removed with the reported problem unchecked confirms nothing (#1906)."""
    cause_rows = cause_elimination_rows(case)
    symptom_rows = symptom_elimination_rows(case, cause_rows)
    return cause_rows + symptom_rows if symptom_rows else []


def has_resolution_confirmation(case: "Case") -> bool:
    """Whether the case confirms a resolution: the cause observed removed AND
    the reported problem observed gone after it (``resolution_confirmation_rows``).
    The resolution gate's READY bar and the closure→resolve pivot's trigger
    (once satisfied by ANY absence row, including the engine's own M6
    disconfirmations, #656; then by the cause leg alone, #1906)."""
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

    The candidates are already bound to this cause through the row they name
    (#1906 D1), so tokens decide nothing about WHAT a row observes: they
    rank bound rows and guard the citation. A row naming what the root's
    verdict does not cite never gets here (``SubjectIndex.cause_targets``); a
    row WORDED about the rung, naming the cited basis, does, and is cited. Newest by
    ``collected_at_turn``: the row recorded closest to the handshake.

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
    bearing = bearing_frame(case, root)
    survivors: list = []
    for row in candidates:
        if bearing.about_another_chain(row):
            absence_confirmation_bearing_rejected_total.inc()
            continue
        survivors.append(row)
    if not survivors:
        return None
    return max(survivors, key=lambda e: e.collected_at_turn or 0)


@dataclass(frozen=True)
class BearingFrame:
    """What a row about ``root`` may legitimately talk about (``frame``), and
    the rest of the graph's statements (``elsewhere``), as content-token sets.
    Built by ``bearing_frame``; read by the confirm stamp's mis-citation guard
    (``_select_bearing_row``) and by M6's failed-fix record (#1927)."""

    frame: frozenset[str]
    elsewhere: tuple[frozenset[str], ...]

    def bears(self, row) -> bool:
        """The row shares at least ``_BEARING_MIN_SHARED_TOKENS`` content
        tokens with the frame."""
        return len(_row_content_tokens(row) & self.frame) >= _BEARING_MIN_SHARED_TOKENS

    def about_another_chain(self, row) -> bool:
        """The row bears on another chain while bearing on nothing in the
        frame: affirmatively about a DIFFERENT candidate cause. A terse row
        bearing on neither is not refused."""
        row_tokens = _row_content_tokens(row)
        return len(row_tokens & self.frame) < _BEARING_MIN_SHARED_TOKENS and any(
            len(row_tokens & other) >= _BEARING_MIN_SHARED_TOKENS
            for other in self.elsewhere
        )


def bearing_frame(case: "Case", root: "CausalNode") -> BearingFrame:
    """``root``'s bearing frame: the problem anchors, the root's statement, its
    chain's mechanism rungs and its attached hypotheses; ``elsewhere`` is every
    other non-problem node and every hypothesis attached to another root."""
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
    return BearingFrame(
        frozenset(frame), tuple(frozenset(toks) for toks in elsewhere_sets)
    )


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
    WHY the promotion happened.

    This is the AUDIT form, written onto the durable node link (``reasoning``).
    It names the row and the node so the promotion can be reconstructed from
    the record; those ids are the point of it.

    It is NOT what the user reads. ``RootCauseConclusion.established_by`` is
    rendered verbatim into the resolution summary, so it takes the prose form
    from ``_confirmation_established_by`` below. One string cannot serve both:
    #987 wrote this one onto both surfaces, and the ids and the "M2 gone⇒gone"
    shorthand reached the user as debug output (#1097).
    """
    return (
        f"engine: user-confirmed resolution at turn {case.current_turn} — "
        f"causal-absence {evidence_id} bears on root {root.node_id} "
        f"(M2 gone⇒gone)"
    )[:500]


def _confirmation_established_by() -> str:
    """The same provenance as above, for the surface a USER reads (#1097).

    Says what the two legs of the promotion were — the user's confirmation that
    the problem was resolved, and evidence that the cause was gone with it —
    without the evidence/node ids or the M2 grade shorthand, which mean nothing
    outside the engine and read as a broken product in a report.

    Takes no arguments deliberately: everything that varies between cases here
    is an internal identifier, so there is nothing case-specific left to say
    once those are gone. The report renders it as
    "_Established by: {…}._", and the assurance note above it is suppressed for
    a CONFIRMED cause, so this is the one line carrying how the cause was held.
    """
    return CONFIRMED_ESTABLISHED_BY


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
    mechanism_for_chain,
    project_hypothesis_states_from_roots,
    conjuncts_for_chain,
    summarize_restatement_hold,
) -> None:
    """Called once from ``causal_graph`` at module import."""
    _GRAPH_HOOKS["count_held"] = support_count_held_root_ids
    _GRAPH_HOOKS["derive"] = derive_node_states
    _GRAPH_HOOKS["sole_cluster_origin"] = sole_cluster_origin
    _GRAPH_HOOKS["mechanism"] = mechanism_for_chain
    _GRAPH_HOOKS["project_hyp_states"] = project_hypothesis_states_from_roots
    _GRAPH_HOOKS["and_conjuncts"] = conjuncts_for_chain
    # #1195: read by ``verification_status.assess_verification_status``,
    # ``milestone_engine`` and ``terminal_transitions.derive_closure_reason``,
    # none of which can import ``causal_graph`` directly — that edge closes the
    # cycle causal_graph -> hypothesis_manager -> terminal_transitions ->
    # verification_status. Same seam, same reason as ``project_hyp_states``.
    _GRAPH_HOOKS["restatement_hold"] = summarize_restatement_hold


def _graph_hooks() -> dict:
    """The registered hooks; cold-start fallback loads ``causal_graph`` by
    name (call-time, both modules long initialized — no init-order hazard)
    for any entry point that reaches the stamp without the engine stack.

    The fallback covers a genuine cold start (``causal_graph`` never imported).
    It does NOT re-register a dict that was EMPTIED after import — ``import_module``
    returns the cached module without re-executing it — so a caller that patches
    ``_GRAPH_HOOKS`` to ``{}`` gets ``None`` from every ``.get`` until the patch
    lifts. Every consumer reads with ``.get`` and degrades rather than raising,
    which is what makes that safe; none may treat the fallback as self-healing."""
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
      DISTINCT causes (§7.1.2 ``distinct_cause_clusters``: duplicate emissions,
      same-causal-line roots and co-necessary M7 conjuncts are ONE cause — the
      ancestor-most member is the cited origin); with several distinct causes
      remaining (an
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
    - The case-level metadata bar is the SHARED ``cause_elimination_rows``
      predicate (non-engine-authored, bound to the cause, not a failed-fix
      disconfirmation, at-or-after the engine-known failure window), narrowed
      to rows naming a support of THIS cause (``SubjectIndex.addresses``) —
      the cause leg the
      resolution-readiness gate reads, so the gate can never call a case
      confirmable on a row the stamp would refuse for metadata. The cited row
      is always a cause row: the symptom leg says the problem went, not which
      cause it went with.
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
    # Only a root its hypothesis marked terminal can be confirmed as the root
    # cause (M1, #2036) — on both arms: a confirmed open root would be graded
    # CONFIRMED and harvested as the cause of a state something re-creates.
    candidate_roots = [n for n in _validated_roots(case) if n.is_identified_root]
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
            case.causal_nodes[nid]
            for nid in count_held_ids
            if nid in case.causal_nodes and case.causal_nodes[nid].root_terminal is True
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
    # Bound rows only (#1906 D1): each names this cause's cited basis. Dated
    # by the data (``observed_turn``, #2145).
    subjects = subject_index(case)
    candidates = [
        e
        for e in cause_elimination_rows(case)
        if e.evidence_id not in supports_linked_ids
        and subjects.observed(e) > root_refute_turn
        and subjects.addresses(e, cluster_ids)
    ]
    if not candidates:
        return False
    absence_row = _select_bearing_row(case, root, candidates)
    if absence_row is None:
        return False

    provenance = _confirmation_provenance(case, root, absence_row.evidence_id)
    # The node link carries the AUDIT form (ids); the conclusion carries the
    # prose form, because the report renders it to the user verbatim (#1097).
    established_by = _confirmation_established_by()
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
        _mint_confirmed_mirror(case, root, established_by)
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
        _mint_confirmed_mirror(case, root, established_by)
    else:
        _upgrade_engine_mirror(case, root, absence_row.evidence_id, established_by)
    return True


def _conjuncts_for_root(case: "Case", root: "CausalNode", hyp) -> list[str]:
    """The VALIDATED causes co-necessary with the chain a conclusion mirrors
    (#1096), via the graph hook — ``causal_graph`` owns BOTH the AND-set reading
    and the chain construction (``conjuncts_for_chain``), and this module cannot
    import it (see the module docstring). Building the chain here instead would
    be a second copy of that rule across a module boundary, free to drift until
    the per-turn mirror and this terminal stamp name different conjunct sets for
    one case.

    The hook is read defensively (``.get``, like every other hook consumer
    here): this runs on the unguarded RESOLVED-execution path, so a missing
    registration degrades to naming no conjuncts — the pre-#1096 conclusion —
    never a KeyError that 500s the transition."""
    fn = _graph_hooks().get("and_conjuncts")
    if fn is None:
        return []
    return fn(case, hyp, root)


def _mint_confirmed_mirror(
    case: "Case", root: "CausalNode", established_by: str
) -> None:
    """Mint the engine conclusion mirror for a just-confirmed root when NO
    conclusion exists (the count-held-at-RESOLVED shape). Mechanism text
    follows the standing hypothesis's chain when one names this root (same
    shape as the per-turn synthesis); the hypothesis-less fallback states the
    degenerate mechanism. Confidence is grade-derived: the root is
    counterfactually CONFIRMED, so VERIFIED at the floor.

    ``established_by`` records HOW the cause was established (#987) — this mint
    is a promotion from the user's confirmation plus the cited absence row, not
    a chain validation, and the conclusion must say so rather than assert the
    cause bare. It is the PROSE form (``_confirmation_established_by``), not
    the id-bearing audit line: the report renders this field verbatim (#1097).
    """
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
    # ONE mechanism builder, shared with the per-turn mirror via the graph hook
    # (#1097) — two copies of this rule is what let the rendered field drift.
    # Read defensively like every hook here: this sits on the unguarded
    # RESOLVED-execution path, so a missing registration degrades to the
    # degenerate mechanism rather than a KeyError that 500s the transition.
    mechanism_fn = _graph_hooks().get("mechanism")
    mechanism = (
        mechanism_fn(case, hyp)
        if mechanism_fn is not None and hyp is not None
        else "Directly produces the observed problem."
    )
    # The cause's co-necessary conjuncts (#1096), derived from the graph by the
    # same function the per-turn mirror uses — a conclusion minted HERE must
    # carry the whole conjunction for the same reason one minted there does.
    conjuncts = _conjuncts_for_root(case, root, hyp)
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
        contributing_factors=conjuncts,
        determined_by=ENGINE_RCC_AUTHOR,
        established_by=established_by,
    )


def _upgrade_engine_mirror(
    case: "Case", root: "CausalNode", absence_evidence_id: str, established_by: str
) -> None:
    """Scoped re-mint at the stamp site: terminal cases never run the per-turn
    recompute (and ``terminal_transitions`` cannot import ``causal_graph``'s
    full mirror synthesis), so without this an ENGINE-authored conclusion would
    stay frozen at CONFIDENT/0.8 — and its ``evidence_basis``, minted pre-stamp,
    would never cite the gone⇒gone row — beside a CONFIRMED terminal grade. The
    upgrade keeps the mirror's text/root and raises only the grade-derived
    fields. LLM-authored conclusions are never touched (their retraction and
    refresh lifecycle is a separate correction tracked on #656).

    ``established_by`` (#987) records that the upgrade to VERIFIED rests on the
    user's confirmation plus the cited gone⇒gone row — the grade jump is a
    promotion from confirmation, and the record says so. Prose form: the report
    renders this field verbatim to the user (#1097)."""
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
        # Re-derived, not carried (#1096): the stamp re-derives node states just
        # above, and a terminal case never recomputes again — so a conjunct that
        # validated on the way here would otherwise be frozen out of the only
        # conclusion the report will ever read.
        contributing_factors=_conjuncts_for_root(case, root, hyp),
        determined_by=ENGINE_RCC_AUTHOR,
        established_by=established_by,
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
