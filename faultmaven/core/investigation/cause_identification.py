"""Which leg identifies the root cause (``cause_identification_leg``).

A leaf module: it imports only the case contracts, so every reader of the
cause-identification gate (the resolution gate, M5, the offer record's lapse
rule, the outcome record) can import it at module level (#2636).
"""

from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import CauseState, WorkingConclusion

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

# Likelihood at or above which a ``working_conclusion`` counts as a known cause
# (the proxy fallback in ``cause_identification_leg`` below). This is the
# single source of truth for that threshold: new-hypothesis priors are capped
# strictly BELOW this value (``hypothesis_manager.NEW_HYPOTHESIS_MAX_PRIOR``) so
# no emission ALONE can trip the gate — only real case evidence can lift
# belief past it. The ``cap < gate`` invariant is pinned by an import-time check.
CAUSE_IDENTIFIED_LIKELIHOOD = 0.6


def cause_identification_leg(
    case: "Case", working_conclusion: WorkingConclusion | None = None
) -> "str | None":
    """Which leg of the cause-identification gate is satisfied, or ``None``.

    The single source of truth for cause-knowledge. Returns the FIRST satisfied
    leg, in trust order:

    - ``"chain"`` — the engine-derived ``progress.cause_state`` is
      ``IDENTIFIED``: a validated, uncontested chain root. This is the
      authoritative signal — recomputed every turn and never path-stripped
      (two-dimensional-hypothesis-methodology.md §9.2; the R1 cause_state gate is
      recorded in investigation-invariants.md's INV-17/19/20/21 flow-redesign
      retirement note, #411).
    - ``"rcc"`` — the LLM-authored ``root_cause_conclusion`` **backstop**: the
      chain recompute under-reported ``cause_state`` (the LLM grounded the cause
      without the chain formally validating), but the conclusion names a cause.
    - ``"working_conclusion"`` — the engine working-conclusion proxy
      **backstop**: a standing hypothesis above ``CAUSE_IDENTIFIED_LIKELIHOOD``.
    - ``None`` — no leg; the cause is not known.

    The ``rcc``/``working_conclusion`` legs are kept because the LLM does not
    always populate the conclusion even when the cause IS identified — relying on
    the proxies alone under-reports a known cause and stalls an otherwise
    resolvable case (the k8s-pvc gate failure). Both backstop legs are gated on a
    **verified symptom** (the evidence-grounded anchor: an unanchored or stale
    conclusion, emitted before the symptom is verified or left behind after a
    symptom claim is withdrawn, must not report the cause as known) and suppressed
    while identification is MECE-contested (§7.1.2 / INV-34: the arbitrary pick
    between competing validated causes, or an LLM over-claim of ONE contested
    cause, is READ-suppressed — not retracted — until the contest resolves; a
    decisively DISCONFIRMED RCC is already ``None`` here, retracted at source via
    retract_disconfirmed_rcc / the M6 demotion, so every consumer sees one truth).
    The ``chain`` leg already implies a verified, uncontested cause, so these
    constraints only bind the backstops.

    The ``chain`` vs ``rcc``/``working_conclusion`` split is exactly the #673
    dual-authoring retirement gate: a resolution licensed by a backstop leg with
    no validated chain root is what INV-41's ``resolution_cause_leg_total``
    measures at the provider floor.

    Scope: enforced here, at the authoritative gate that drives disposition / the
    M5 solution gate. Other readers of ``root_cause_conclusion`` (the report
    renderer, the case UI) may surface an unanchored RCC mid-investigation
    (informational only); the one consequential consumer — KB runbook harvesting —
    runs on RESOLVED cases (symptom verified), so it never harvests an unanchored
    cause. If a non-terminal RCC harvest is ever added, enforce the anchor at RCC
    production instead.
    """
    if (
        case.progress
        and getattr(case.progress, "cause_state", None) == CauseState.IDENTIFIED
    ):
        return "chain"

    # Backstop legs are trusted ONLY once the symptom is verified.
    if not (case.progress and getattr(case.progress, "symptom_verified", False)):
        return None

    # While identification is MECE-contested, NO backstop proxy counts.
    if getattr(case.progress, "cause_identification_contested", False):
        return None
    # Nor while it is held on an open root (#2036): the chain has validated a
    # cause whose why is unanswered, so a conclusion or a likelihood naming that
    # cause would assert as the root what the chain says is not yet the root.
    if getattr(case.progress, "cause_identification_held_open", False):
        return None

    if case.root_cause_conclusion and getattr(
        case.root_cause_conclusion, "root_cause", None
    ):
        return "rcc"
    wc = (
        working_conclusion
        if working_conclusion is not None
        else case.working_conclusion
    )
    if (
        wc
        and getattr(wc, "statement", None)
        and getattr(wc, "likelihood", 0) >= CAUSE_IDENTIFIED_LIKELIHOOD
        # #987: a MIRROR of the RootCauseConclusion is not an independent
        # signal — whenever one exists the `rcc` leg above already governs. A
        # working conclusion can be built before a retraction later in the same
        # turn (M5's settled preview is built in the solutions step, and a
        # caller's case can still carry last turn's), so counting a mirror here
        # would let a retracted conclusion keep satisfying the backstop through
        # its own stale reflection after retraction cleared every other consumer.
        and not getattr(wc, "mirrors_root_cause_conclusion", False)
    ):
        return "working_conclusion"
    return None
