"""Verification status — the unified reading of whether a grounded cause is
reachable, and if not, why.

See ``docs/architecture/investigation-engine/insufficient-evidence-handling.md``.

This module is a pure, compute-only **join of the two existing layers** — the
causal-graph grounding grade (``grade_cause_assurance``) and the hypothesis-layer
stall signals. By reading the existing signals rather than re-deriving them, it
stays a coordinating read over one source of truth per axis — not a third
parallel signal. Its output is consumed by the turn pipeline
(``milestone_engine._recompute_assessment_state`` writes it onto
``case.progress.verification_status`` each turn, and ``engine_owned_affordances``
drives the code-guarded structured handoff from it) and by the terminal
capture-on-close hook. The ``VerificationStatus`` enum lives in the domain layer
(``modules.case.contracts``) so it can be a persisted field; this module imports
and re-exports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.core.investigation.cause_assurance import (
    CauseAssuranceGrade,
    _graph_hooks,
    evidence_datum_key,
    grade_cause_assurance,
)

# Thresholds come from the neutral ``exhaustion_thresholds`` module — the same
# definition ``_detect_exhaustion`` reads — so the dimensions the two readers
# measure identically cannot drift (turn/stall thresholds; category/evidence
# breadth). The hypothesis dimension is deliberately NOT shared (see that module):
# ``WORK_GATE_MIN_HYPOTHESES`` here gates *generation depth*, while the exhaustion
# detector gates *elimination depth* via its own ``EXHAUSTION_MIN_REFUTED``.
from faultmaven.core.investigation.exhaustion_thresholds import (
    EXHAUSTION_MIN_TURNS,
    EXHAUSTION_STALL_THRESHOLD,
    WORK_GATE_MIN_CATEGORIES,
    WORK_GATE_MIN_EVIDENCE,
    WORK_GATE_MIN_HYPOTHESES,
)
from faultmaven.modules.case.contracts import (
    NeedObtainability,
    NeedPurpose,
    VerificationStatus,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

# ``VerificationStatus`` is defined in the domain layer (``modules.case``) and
# imported here — the same direction as ``CauseState`` / ``NeedObtainability`` —
# so it can be a persisted field on ``InvestigationProgress`` without inverting
# the module dependency. Re-exported so existing ``from
# ...verification_status import VerificationStatus`` call sites keep working.
__all__ = [
    "VerificationStatus",
    "assess_verification_status",
    "restatement_hold_governs",
    "work_gate_passed",
    "is_stalled",
    "is_progress_stalled",
]


def work_gate_passed(case: "Case") -> bool:
    """Whether real diagnostic work has happened (the §5.2 work gate): ≥N
    hypotheses across ≥N categories with ≥N evidence items.

    Also the **observability primitive** for the per-provider gate-crossing
    metric — a configured model that never crosses this is mis-provisioned, a
    provider-health fact rather than a per-case verdict.

    The evidence dimension counts DISTINCT observations (``evidence_datum_key``),
    not rows (#1136). The same datum recorded twice — a user re-submitting a
    snapshot, a model re-extracting the same lines — is one thing the
    investigation knows, and letting it satisfy ``WORK_GATE_MIN_EVIDENCE`` would
    move a case out of ``NOT_YET_PRODUCTIVE`` on no new work, blaming the case for
    what the reasoner did not gather. That is the §5.2 line this gate exists to
    draw, and it is the evidence-side twin of the hypothesis dedup INV-36 added
    for the same reason. Hypotheses need no such treatment here — duplicates are
    already refused at mint.
    """
    categories = {h.category for h in case.hypotheses.values()}
    distinct_evidence = {evidence_datum_key(e) for e in case.evidence}
    return (
        len(case.hypotheses) >= WORK_GATE_MIN_HYPOTHESES
        and len(categories) >= WORK_GATE_MIN_CATEGORIES
        and len(distinct_evidence) >= WORK_GATE_MIN_EVIDENCE
    )


def _is_grounded(case: "Case", grade: "CauseAssuranceGrade | None" = None) -> bool:
    """Grounding axis for the disposition join: a **validated root**
    (``grade_cause_assurance`` at or above ``MECHANISTIC``) **anchored on a
    verified symptom**. ``grade`` lets the per-turn recompute pass the grade it
    just persisted, so both persisted signals derive from one graph snapshot;
    when omitted the grade is computed fresh.

    THE BAR IS "ANY VALIDATED ROOT", NOT ``CONFIRMED`` (#1136, resolving the
    calibration question §3.5 left open). The axis previously read the top M2
    grade, which made the entire grounded ROW of the §5.1 grid dead code
    in-flight: the only producer of ``CONFIRMED`` is the resolution
    confirm-stamp (``confirm_root_from_resolution_absence``, reached only from
    ``terminal_transitions._execute_resolved_transition``), which fires AFTER the
    last per-turn recompute — deliberately, so a premature "it's stable now"
    absence row cannot self-confirm. So on every live INVESTIGATING turn the axis
    was false by construction, ``HEALTHY`` and ``TREATMENT_BLOCKED`` were
    unreachable, and a stalled case holding a validated root disposed
    ``INSUFFICIENT_EVIDENCE`` — asserting "no cause could be grounded" over a case
    that holds one, to consumers (the handoff, ``closed_insufficient_evidence``,
    the Data Boundary report block) that take the claim at face value.

    That reachability collapse is why the bar moved. It is a *disposition* read,
    not a harvest read: the question this axis answers is "does the case have a
    cause to act on?", and a mechanistically validated root is one. Whether the
    fix has been counterfactually borne out is the question the PROGRESS axis
    answers alongside it — which is exactly the ``TREATMENT_BLOCKED`` cell ("have
    a cause but can't reach a verified fix"). Requiring the counterfactual on the
    grounding axis collapsed those two questions into one and lost the cell.

    The harvest bar is untouched at ``CONFIRMED`` — see COUPLING below.

    The symptom-verified anchor closes the composition seam (§4 limitation 1):
    the §7 grade walks the causal graph and can read ``CONFIRMED`` off a
    validated root that has **no backing hypothesis and no verified symptom** — a
    shape a weak model can materialize (a chain without a hypothesis layer).
    Without the anchor, ``verification_status`` reads ``HEALTHY`` over an empty
    hypothesis layer and masks a stuck investigation (observed: a validated root,
    0 hypotheses, ``symptom_verified=False`` → HEALTHY while the case spins).

    Requiring ``symptom_verified`` here aligns the join's grounding axis with the
    *same* anchor ``cause_state`` already requires for ``IDENTIFIED`` (and that
    ``terminal_transitions._cause_identified`` enforces): cause identification is
    anchored on evidence that the problem exists. It does **not** touch
    ``grade_cause_assurance`` itself — the §7 harvest grade is unchanged,
    and its RESOLVED-case consumers (KB harvest, conversion) already run with a
    verified symptom, so they are unaffected. The residual desync (grade
    ``GROUNDED`` × ``symptom_verified`` but ``cause_state`` not IDENTIFIED) is not
    silenced — ``_log_grounding_assessment``'s ``seam_divergence`` still surfaces
    it for monitoring.

    COUPLING (keep in sync): this axis intentionally diverges from the raw
    ``grade_cause_assurance`` readers — ``terminal_transitions.assess_runbook_readiness``
    (KB harvest gate) and ``cause_assurance.runbook_conversion_ready`` (the
    canonical case→runbook offer/enforcement predicate). Since #1136 the
    divergence runs in BOTH directions, and the safety argument differs per
    direction:

    - **Looser** on the grade: this accepts ``MECHANISTIC``, harvest requires
      ``CONFIRMED``. Safe *structurally*, not by gating — those readers call
      ``grade_cause_assurance`` directly and never call this function, so no
      change to the disposition bar can reach them. A mechanistically validated
      cause disposes ``TREATMENT_BLOCKED`` here and remains un-harvestable there.
      That asymmetry is the intended design: acting on a cause and publishing it
      as reusable knowledge warrant different bars.
    - **Stricter** on the symptom anchor: this additionally requires
      ``symptom_verified``, which the raw grade does not. Safe because both
      readers are gated behind RESOLVED, which requires ``_cause_identified`` →
      ``symptom_verified``, so the divergent state is unreachable there. If a
      **pre-resolution** harvest/convert path is ever added it must apply this
      same anchor — otherwise it would harvest a cause the disposition layer
      calls ungrounded (mirrors the note in ``_cause_identified``: "if a
      non-terminal RCC harvest is ever added, enforce the anchor at RCC
      production").
    """
    if not (case.progress and case.progress.symptom_verified):
        return False
    if grade is None:
        grade = grade_cause_assurance(case)
    # Any validated root clears the bar. Written as "not NO_ROOT" rather than an
    # explicit MECHANISTIC/CONFIRMED set so a future grade inserted ABOVE
    # NO_ROOT is grounded by default — the failure that matters here is a
    # grounded case reading ungrounded (it loses the whole grid row), and this
    # spelling cannot produce it.
    return grade != CauseAssuranceGrade.NO_ROOT


def is_stalled(case: "Case") -> bool:
    """Time-threshold arm of the progress axis: ``EXHAUSTED``'s stall thresholds
    ONLY — deliberately NOT its ≥2 preconditions (those are the work gate).

    The full progress axis is ``is_progress_stalled`` (this OR a declared data
    wall); this cheap two-int check is the fast arm, evaluated first."""
    return (
        case.current_turn >= EXHAUSTION_MIN_TURNS
        and case.turns_without_progress >= EXHAUSTION_STALL_THRESHOLD
    )


def _residual_candidates(case: "Case") -> list:
    """Candidates still in play (design §2 *residual candidate*): hypotheses
    whose state is NOT ``REFUTED`` or ``RETIRED``."""
    return [h for h in case.hypotheses.values() if not h.state.is_terminal]


def _candidate_unresolvable(case: "Case", hypothesis_id: str) -> bool:
    """Whether a candidate is *unresolvable* (D1): it has **≥1** outstanding
    ``causal_verification`` discriminator **and all** of them are declared
    ``UNOBTAINABLE``. A candidate with **zero** discriminators is *unknown*, not
    unresolvable — the guard against a vacuous ∀-over-the-empty-set."""
    discriminators = [
        n
        for n in case.evidence_needs
        if n.purpose == NeedPurpose.CAUSAL_VERIFICATION
        and n.is_outstanding
        and hypothesis_id in n.motivating_hypothesis_ids
    ]
    if not discriminators:
        return False
    return all(
        n.obtainability == NeedObtainability.UNOBTAINABLE for n in discriminators
    )


def _declared_wall(case: "Case") -> bool:
    """Declared-data-wall arm of the progress axis (§5.3): **every** residual
    candidate is unresolvable. A stall the model's obtainability declarations
    *establish* — so a fully-declared wall reaches the handoff immediately rather
    than waiting out the time thresholds. Requires ≥1 residual candidate (an
    empty differential is not a wall)."""
    residual = _residual_candidates(case)
    if not residual:
        return False
    return all(_candidate_unresolvable(case, h.hypothesis_id) for h in residual)


def is_progress_stalled(case: "Case") -> bool:
    """Progress axis for the NOT-grounded disposition (§5.3): stalled by the time
    thresholds **or** by a declared data wall. Used for the
    ``OPEN``→``INSUFFICIENT_EVIDENCE`` decision and the handoff trigger.

    The wall arm is scoped to the not-grounded branch on purpose — it is about
    not being able to *ground* a cause, so it must not touch the grounded
    ``TREATMENT_BLOCKED`` (fix-reachability) disposition, which reads the plain
    time-``is_stalled``. Monotonic: the wall can only move a not-grounded case
    toward ``INSUFFICIENT_EVIDENCE``. The cheap time check is evaluated first via
    short-circuit, before the (relatively expensive) ``_declared_wall`` walk."""
    return is_stalled(case) or _declared_wall(case)


def restatement_hold_governs(case: "Case"):
    """The §7.1 restatement hold when it is what GOVERNS this case, else None.

    The single predicate every #1195 consumer reads — the disposition join
    below, ``milestone_engine``'s affordances, and
    ``terminal_transitions.derive_closure_reason``. Deliberately ONE condition
    rather than a set of facts each caller must remember to combine: this fix
    has now twice shipped a consumer that applied some of the guards and not
    others (the closure path dropped the grounding shield; nothing carried the
    symptom anchor at all). A caller that reads this cannot half-apply it.

    Two conditions, and the claim it licenses is what makes each necessary —
    *the evidence already grounds a cause here, so more data will not move it*:

    - ``hold.is_sole_root_block`` — the guard holds every root the case has not
      refuted. False while some other root is blocked by something evidence can
      move, and false when a root is already VALIDATED (the case then HAS a
      cause; see ``causal_graph.RestatementHold``).
    - ``symptom_verified`` — the same anchor ``_is_grounded`` demands, for the
      same reason. Without it the problem itself was never established from
      data, and "the evidence grounds a cause" is a claim about a symptom nobody
      confirmed. A causal chain can clear the §7.1 support bar over an
      unverified symptom (the weak-model chain-without-a-verified-problem shape
      §4 limitation 1 describes), and on that case the honest reading is the
      evidence one — which also keeps
      ``report_generation_service._insufficient_evidence_boundary_block``'s
      "the reported problem was never established" arm reachable at close.

    Read through the ``cause_assurance`` graph-hook seam because a direct
    ``causal_graph`` import closes the causal_graph -> hypothesis_manager ->
    terminal_transitions -> verification_status cycle. ``.get`` like every other
    hook consumer: an unregistered or cleared hook degrades to the pre-#1195
    reading rather than raising on a live turn.

    Note what this does NOT check: the stall, and the declared data wall. Those
    are the *disposition*'s business and differ per consumer — the join wants a
    stalled case, the closure reason must fire on a case closed before any
    stall, and the composite wall+hold turn needs the hold even though its
    status stays ``INSUFFICIENT_EVIDENCE``.
    """
    summarize = _graph_hooks().get("restatement_hold")
    if summarize is None:
        return None
    if not (case.progress and case.progress.symptom_verified):
        return None
    hold = summarize(case)
    if hold is None or not hold.is_sole_root_block:
        return None
    return hold


def assess_verification_status(
    case: "Case", *, grade: "CauseAssuranceGrade | None" = None
) -> VerificationStatus:
    """Compute the verification status as the grounding × progress join.

    Reads the grounding grade × the progress axis (``is_progress_stalled`` —
    time thresholds OR a model-declared data wall). The result is persisted onto
    ``case.progress.verification_status`` each turn by the caller
    (``milestone_engine._recompute_assessment_state``), which passes the grade
    it just persisted via ``grade`` so both signals derive from one graph
    snapshot; the model-declared obtainability it reads is likewise durable
    (the ``evidence_needs`` column). The caller must run this AFTER any
    deductive-validation stamp in the turn pipeline (mirroring the #593
    recompute), so the grounding-first disposition reads a fresh grade rather
    than pre-empting the deductive arm.

    One cell is not a plain product of the two axes: the (not-grounded ×
    stalled) cell splits on the REASON for the stall (#1195). It reads
    ``RESTATEMENT_HELD`` instead of ``INSUFFICIENT_EVIDENCE`` only when the
    stall is a TIME stall (no model-declared data wall) AND
    ``restatement_hold_governs`` holds. Either failing leaves the honest
    evidence reading in place — and when a wall and a hold are BOTH live the
    status keeps the wall while the affordances drop the data ask, because
    neither cell is true alone. See the comments at those branches.
    """
    if _is_grounded(case, grade=grade):
        # Grounded × stalled = TREATMENT_BLOCKED, meaning "have a cause but can't
        # reach a *verified fix*". That is a fix-reachability stall — the time
        # thresholds only. The declared data wall is about not being able to
        # *ground* a cause (discriminator obtainability); on an already-grounded
        # case it is moot, so it must NOT flip HEALTHY → TREATMENT_BLOCKED. The
        # wall arm therefore belongs only to the not-grounded branch below.
        return (
            VerificationStatus.TREATMENT_BLOCKED
            if is_stalled(case)
            else VerificationStatus.HEALTHY
        )
    # Not grounded. Separate "no real work yet" from a genuine wall (§5.2) so a
    # model that produced nothing is never blamed on the case.
    if not work_gate_passed(case):
        return VerificationStatus.NOT_YET_PRODUCTIVE
    if not is_progress_stalled(case):
        return VerificationStatus.OPEN
    # WHICH ARM produced the stall decides the STATUS. A model-declared data
    # wall is an EXPLICIT assertion that the discriminating data cannot be
    # obtained — the canonical insufficient-evidence archetype, and the reason
    # the wall arm exists. It wins here, including when the clock has also run
    # out: "the wall only counts when the clock has not run out" is not a rule
    # anyone would defend.
    #
    # THE COMPOSITE (wall AND hold) is decided deliberately, and not here
    # (#1195 review). Both facts are true of such a case and NEITHER cell is
    # true alone: the wall's "no cause can be grounded from currently available
    # data" is false about a root three independent supports already ground,
    # and the hold's "the block is lexical, not evidential" is false about
    # discriminators the model declared unobtainable. What is true of both is
    # that MORE DATA WILL NOT HELP. So the status keeps the wall — it is a real,
    # user-declared boundary that the close must record — while the AFFORDANCES
    # drop the data ask, which is the channel the contradiction actually
    # reached: ``_insufficient_evidence_handoff_suggestions`` swaps in the
    # mechanism move on a governing hold, under its own gate label. The user is
    # told both things; neither audience is told to go and fetch data.
    if _declared_wall(case):
        return VerificationStatus.INSUFFICIENT_EVIDENCE
    # #1195: a time stall is not always an evidence deficiency. A ROOT that
    # clears every validation bar and is held at INCONCLUSIVE by the §7.1
    # RESTATEMENT guard alone is blocked on its own PHRASING against the case
    # frame — the engine's own prompt annotation says so in the same turn ("MORE
    # SUPPORTING EVIDENCE WILL NOT VALIDATE IT"). Reporting INSUFFICIENT_EVIDENCE
    # there asserts "no cause can be grounded from currently available data" over
    # a case the engine has already grounded, and sends the user to fetch data
    # that cannot move the hold (observed: case_a3d354f08765 — 100% coverage, 14
    # evidence rows, 3 independent causal supports against a bar of 2).
    #
    # ``restatement_hold_governs`` carries every condition that claim needs (the
    # hold is the case's sole root block, no root already promoted, and the
    # symptom is verified) as ONE read, so no consumer can apply half of them.
    #
    # Placed HERE — inside the stalled arm, below the work gate — deliberately:
    #   - ABOVE ``OPEN`` would be wrong: a progressing case makes no false claim
    #     and needs no corrective; the contradiction is specific to this cell.
    #   - ABOVE the work gate would be wrong: ``NOT_YET_PRODUCTIVE`` is a
    #     provider-health reading ("too little work to judge"), never a per-case
    #     evidence verdict, so it does not contradict the hold. (A restatement
    #     hold below the work gate therefore still reads NOT_YET_PRODUCTIVE and
    #     receives no corrective unless the 0-hypothesis vacuum fires — the
    #     documented INV-38 residual band, not a new gap.)
    #   - It is also the cheapest correct placement: the hold summary tokenizes
    #     the causal graph, and this arm is reached only by a stalled,
    #     work-gated, ungrounded case with no declared wall.
    #
    # This does NOT release the held root (that is the open #1122 product
    # decision). It changes only what the case is REPORTED as, and — via
    # ``milestone_engine._restatement_held_pending`` — what the user is offered.
    if restatement_hold_governs(case) is not None:
        return VerificationStatus.RESTATEMENT_HELD
    return VerificationStatus.INSUFFICIENT_EVIDENCE
    # #1195: a time stall is not always an evidence deficiency. A ROOT that
    # clears every validation bar and is held at INCONCLUSIVE by the §7.1
    # RESTATEMENT guard alone is blocked on its own PHRASING against the case
    # frame — the engine's own prompt annotation says so in the same turn ("MORE
    # SUPPORTING EVIDENCE WILL NOT VALIDATE IT"). Reporting INSUFFICIENT_EVIDENCE
    # there asserts "no cause can be grounded from currently available data" over
    # a case the engine has already grounded, and sends the user to fetch data
    # that cannot move the hold (observed: case_a3d354f08765 — 100% coverage, 14
    # evidence rows, 3 independent causal supports against a bar of 2).
    #
    # ``is_sole_root_block``, not merely "something is held" (#1195 review,
    # finding 1): the claim the carve-out licenses is about the CASE, and it is
    # false while some other live ROOT is blocked by something evidence can
    # actually move. See ``causal_graph.RestatementHold``.
    #
    # Placed HERE — inside the stalled arm, below the work gate — deliberately:
    #   - ABOVE ``OPEN`` would be wrong: a progressing case makes no false claim
    #     and needs no corrective; the contradiction is specific to this cell.
    #   - ABOVE the work gate would be wrong: ``NOT_YET_PRODUCTIVE`` is a
    #     provider-health reading ("too little work to judge"), never a per-case
    #     evidence verdict, so it does not contradict the hold. (A restatement
    #     hold below the work gate therefore still reads NOT_YET_PRODUCTIVE and
    #     receives no corrective unless the 0-hypothesis vacuum fires — the
    #     documented INV-38 residual band, not a new gap.)
    #   - It is also the cheapest correct placement: the hold summary tokenizes
    #     the causal graph (~1.3 ms on a 22-root graph), and this arm is reached
    #     only by a stalled, work-gated, ungrounded case with no declared wall.
    #
    # This does NOT release the held root (that is the open #1122 product
    # decision). It changes only what the case is REPORTED as, and — via
    # ``milestone_engine._restatement_held_pending`` — what the user is offered.
    #
    # Reached through the ``cause_assurance`` graph-hook seam rather than by a
    # direct import: ``causal_graph`` -> ``hypothesis_manager`` ->
    # ``terminal_transitions`` -> here is a real cycle, and the seam is what the
    # other below-the-graph readers (the confirm-stamp, the hypothesis-state
    # projection) already use. ``.get`` like every other hook consumer: an
    # unregistered or cleared hook degrades to the pre-#1195 reading instead of
    # raising on a live turn. That degradation is REAL, not theoretical —
    # ``_graph_hooks()``'s import fallback does not repopulate a dict that was
    # emptied after import — so it is pinned in both directions
    # (``test_restatement_hold_hook_is_registered``, and the cleared-hooks pin
    # beside it) rather than assumed away.
    summarize = _graph_hooks().get("restatement_hold")
    hold = summarize(case) if summarize is not None else None
    if hold is not None and hold.is_sole_root_block:
        return VerificationStatus.RESTATEMENT_HELD
    return VerificationStatus.INSUFFICIENT_EVIDENCE
