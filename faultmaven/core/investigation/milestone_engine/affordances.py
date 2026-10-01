import json
from typing import Any

from faultmaven.core.investigation.causal_graph.similarity import (
    hypothesis_statements_duplicate,
)
from faultmaven.core.investigation.cause_assurance import evidence_datum_key
from faultmaven.core.investigation.prompts.templates.investigation import (
    SCHEMA_INSTRUCTIONS,
)
from faultmaven.core.investigation.verification_status import (
    VerificationStatus,
    assess_verification_status,
    is_progress_stalled,
    is_stalled,
    restatement_hold_governs,
)
from faultmaven.modules.case.contracts import Case, CaseState

from .cause_state import _investigation_confirmation_suggestions


def gate1_statement_is_confirmable(statement_at_turn_start: "str | None") -> bool:
    """Whether a statement the user could have SEEN stood when the turn began.

    INV-01 requires the user to confirm a statement presented on a prior turn,
    so consent is admissible only if one stood going in. A statement first
    written this turn fails here — that is the model writing and confirming in
    one shot, collapsing the User-Agent Handshake.

    Whitespace is not a statement. Judging that the same way
    ``_gate1_is_pending`` does matters: if the two disagreed, a whitespace-only
    statement would leave Gate 1 permanently pending (an empty block quote
    above buttons) while every click was refused here, forever.

    ONE function, called at all three consent sites — the LLM path, the DECIDE
    click, and a resolver-minted confirmation. They used to hold three separate
    bars, so a minted "yes" could commit where the LLM path would have refused
    (the fm#918 shape). Sharing the predicate is what stops them drifting apart
    again; ``TestGate1ConsentPredicate`` pins all three call sites.

    Note what this deliberately does NOT do: it does not compare the statement
    across the turn. Binding consent to the wording the user actually saw is
    handled where the statement is WRITTEN — a revision arriving on a consent
    turn is dropped rather than applied — so by the time this runs there is
    nothing left to compare. An earlier draft took both values and checked
    them; with the write-side guard in place that second half could never fire,
    and a guard that cannot fire is indistinguishable from dead code.
    """
    return bool((statement_at_turn_start or "").strip())


def _gate1_is_pending(case: "Case") -> bool:
    """Whether Gate 1 (problem-statement confirmation) is open for this case.

    Returns True when the LLM has proposed a problem statement and the user
    has not yet confirmed it. Subsumes the prior handshake-deferred-recovery
    condition: the same affordance pair is appropriate on every Gate-1-pending
    turn, not only on the recovery turn after the same-turn guard fires.

    Used by ``engine_owned_affordances`` so the engine emits the canonical
    confirmation pair deterministically regardless of LLM compliance with the
    INQUIRY prompt's confirmation-suggestion enumeration. Matches the pattern
    already established for Gate 2 and Gate 3.
    """
    if case.state != CaseState.INQUIRY:
        return False
    inq = case.inquiry
    if inq is None:
        return False
    # Stripped, to match ``gate1_statement_is_confirmable``. A whitespace-only
    # statement is not a statement: judged truthy here and empty there, it
    # would leave Gate 1 permanently pending — an empty block quote above
    # buttons whose consent path refuses every click, forever.
    if not (inq.proposed_problem_statement or "").strip():
        return False
    return not inq.problem_statement_confirmed


def _restates_standing_solution(s_item, case: "Case") -> bool:
    """Whether an emitted solution merely RESTATES one already on the case (#1136).

    Re-proposing the standing fix is what a well-behaved model does while it waits
    for the user to apply it — every turn, in the same words. Each restatement
    minted a fresh ``sol_*`` row, and a minted row counted as progress, so a case
    parked on an unapplied fix reset ``turns_without_progress`` indefinitely and no
    stall net could ever arm (observed: ``case_07a2d687f057``, turns 8-18 — eleven
    consecutive turns whose only artifact was the same fix re-offered).

    The engine already names this situation on the *action* side: INV-32's
    ``_supersede_pending_solution_offers(reason="reproposal")`` retires the standing
    pending offer when a new one arrives. This is the same judgement applied to the
    progress signal.

    Deliberately NOT a mint-time skip (the INV-36 hypothesis treatment): a
    ``Solution`` row anchors a ``ProposedAction``, which is the compliance-detection
    chain the user's later "I ran it" is matched against. Dropping the row to fix a
    counter would risk that chain. The row is kept; only ``metadata`` records
    whether it was NEW. Duplicate rows accumulating on the case is a real but
    separate defect — see the PR's follow-up note.

    Text comparison reuses ``hypothesis_statements_duplicate`` for its two
    fail-open guards, both of which matter more here than for hypotheses: the
    numeric-discriminator guard keeps a REVISED fix distinct (``-Xmx256m`` is not a
    restatement of ``-Xmx512m``), and the mutual-mirror bar keeps a more-specific
    elaboration distinct from the general fix it refines. Same ``solution_type`` is
    required as well — the same words proposed as a WORKAROUND and as a permanent
    SOLUTION are different offers.
    """
    description = getattr(s_item, "description", None)
    if not description:
        # No text to compare — fail open (treat as new), never dedup on absence.
        return False
    for standing in case.solutions or []:
        if standing.solution_type != s_item.solution_type:
            continue
        if not standing.immediate_action:
            continue
        if hypothesis_statements_duplicate(description, standing.immediate_action):
            return True
    return False


def _restates_standing_evidence(ev_item, case: "Case") -> bool:
    """Whether an emitted evidence row quotes an extract already on the case (#1136).

    The counterpart of ``_restates_standing_solution`` on the supply side: a user
    re-submitting a snapshot they already sent (or a model re-extracting the same
    lines from the same file) minted new ``ev_*`` rows, and a minted row counted as
    progress.

    The bar is **exact match after normalisation**, not the fuzzy mirror used for
    solutions, and the difference is deliberate. An evidence ``extract`` is a quoted
    span, not a paraphrase — two spans are the same datum or they are not, and a
    near-miss is far more likely to be a genuinely different span (an adjacent log
    window, the next occurrence of a repeating line) than a restatement. Source
    identity is required too: the same text observed in two different files is two
    observations, which is precisely the independent-corroboration signal the
    grading layer counts.

    Fail-open on an empty extract — a row with nothing quoted is never deduped away.
    """
    if not getattr(ev_item, "extract", None):
        return False
    key = evidence_datum_key(ev_item)
    return any(evidence_datum_key(standing) == key for standing in case.evidence or [])


#: The recovery that is true of EVERY restatement-held shape: the leading cause
#: reads as a restatement of the problem, so what moves it is a mechanism, not
#: another observation. Shared by the restatement-held handoff and by the
#: composite wall+hold turn, where the insufficient-evidence handoff substitutes
#: it for its data ask — one string, so the two turns cannot drift apart on the
#: one piece of advice that is correct on both.
_MECHANISM_MOVE = {
    "label": "Ask for the cause to be stated as a mechanism",
    "action_type": "FREE_SPEECH",
    "body": (
        "The leading explanation currently restates the problem rather than "
        "explaining it. Asking what specifically is misconfigured, exhausted, "
        "or failing — and how that produces the symptom — is what moves it "
        "forward. More data will not."
    ),
}


def _insufficient_evidence_handoff_suggestions(
    case: "Case | None" = None, *, hold=None
) -> list:
    """Deterministic structured-handoff affordances for an insufficient-evidence
    case (verification-status Phase 1).

    These are the code-guaranteed *options* half of the structured handoff (the
    boundary statement — *what specifically* is needed — stays model-authored in
    the prose). The moves are keep-engaging by construction: they invite the
    discriminating data or a fresh angle so the case never collapses into a
    fabricated cause or a silent spin — the two failure modes the handoff exists
    to prevent. They deliberately do **not** steer toward close: pausing/closing
    is the user's call (the prompt's handoff already names it as an option), and
    the engine nudging abandonment would be soft-collapse (D4). Non-clickable
    FREE_SPEECH — the user supplies the content.

    THE COMPOSITE (#1195 review). A case can reach this cell on a model-declared
    data wall while ALSO carrying a governing §7.1 restatement hold. The status
    stays ``INSUFFICIENT_EVIDENCE`` there — the wall is a real, user-declared
    boundary and the close must record it — but the DATA ASK must not survive:
    the user has already declared that data unobtainable, and the same turn
    tells the MODEL that more evidence will not validate the held root. Asking
    anyway is the exact contradiction #1195 exists to remove, reached by a
    different route. So on a governing hold the data ask is replaced by
    ``_MECHANISM_MOVE``, the one move that IS actionable there. The fresh-angle
    move survives unchanged: it asks for a DIRECTION, not a datum, and stays
    true of a walled case.

    ``case`` is optional so the pair remains constructible without one (the peer
    builders take no argument and several tests call it bare); every engine call
    site passes it, and without it the historical data-ask pair is returned.
    ``hold`` lets the caller hand over the read it already did.
    """
    # ``hold`` is passed by ``engine_owned_affordances``, which computed it
    # once for the whole call; deriving it again here would re-sweep the graph.
    if hold is None and case is not None:
        hold = restatement_hold_governs(case)
    first = (
        _MECHANISM_MOVE
        if hold is not None
        else {
            "label": "Share data that would distinguish the causes",
            "action_type": "FREE_SPEECH",
            "body": (
                "The investigation has narrowed the problem but can't ground a "
                "single cause from the current evidence. New discriminating data "
                "would let it resume."
            ),
        }
    )
    return [
        first,
        {
            "label": "Suggest a diagnostic angle not yet tried",
            "action_type": "FREE_SPEECH",
            "body": (
                "Point the investigation at an angle the differential hasn't "
                "covered — a different subsystem, timeframe, or signal."
            ),
        },
    ]


def _insufficient_evidence_handoff_pending(
    case: "Case", *, status: "VerificationStatus | None" = None
) -> bool:
    """Whether the engine should drive the insufficient-evidence structured
    handoff this turn (verification-status Phase 1).

    Code-guarded promotion of the §5.3 direction: the engine computes the
    objective, work-gated stall (``INSUFFICIENT_EVIDENCE`` — not grounded, work
    gate passed, stalled) and *drives* the handoff, rather than depending on the
    LLM to state the boundary. This is a soundness fix (the engine must not spin
    silently or fabricate a cause on a walled case), always on — not a flag-gated
    enhancement; it is validated by simulation, not toggled.

    Scoped to ``INVESTIGATING``: the reading is only meaningful mid-investigation
    (a stall in INQUIRY or a terminal case is a different concern), and this
    keeps the handoff from colliding with the Gate-1 / disposition affordances,
    which own their own states.

    Must be evaluated AFTER the deductive-validation stamp in the turn pipeline
    (``_recompute_assessment_state``) so ``assess_verification_status`` reads a
    fresh grounding grade and never pre-empts the deductive arm (the #593
    re-derive-after-stamp ordering). Both ``engine_owned_affordances`` call
    sites in ``process_turn`` satisfy this — they run well after
    ``_apply_investigation_updates``.

    NOT every work-gated stall reaches here. A stall whose only block is the
    §7.1 restatement guard reads ``RESTATEMENT_HELD`` instead (#1195) and gets
    ``_restatement_held_pending``'s moves: this handoff asks for discriminating
    data, and on that shape more data provably cannot help — the engine says so
    to the model in the same turn. The carve-out is made once, at the join, so
    this predicate and the reported status cannot disagree.
    """
    if case.state != CaseState.INVESTIGATING:
        return False
    # Cheap short-circuit before the (relatively expensive) grounding-grade
    # computation: INSUFFICIENT_EVIDENCE requires a stall, so a not-yet-stalled
    # case — the large majority of INVESTIGATING turns — can never reach the
    # handoff. Uses the full progress axis (time thresholds OR a declared data
    # wall), the same predicate ``assess_verification_status`` uses, so it cannot
    # disagree and a fully-declared wall fires the handoff immediately.
    if not is_progress_stalled(case):
        return False
    if status is None:
        status = assess_verification_status(case)
    return status == VerificationStatus.INSUFFICIENT_EVIDENCE


def _restatement_held_suggestions(case: "Case", *, hold=None) -> list:
    """Deterministic affordances for a case whose leading cause is held by the
    §7.1 RESTATEMENT guard (#1195) — the fourth peer of the insufficient-evidence
    handoff, the hypothesis-vacuum pull-back and the treatment-blocked handoff:
    same mechanism (a code-guarded branch substituting a deterministic pair
    regardless of LLM compliance), different trigger, different ask.

    Its two ungrounded peers ask for data that would GROUND a cause. Here the
    causal grounding is already in hand — in the incident, three independent
    qualifying causal supports against a bar of two, at 100% evidence coverage —
    and what blocks validation is that the ROOT's STATEMENT adds no content
    beyond the problem and the other standing hypotheses. Asking such a case for
    discriminating data is not merely unhelpful: it is the exact opposite of what
    the engine tells the MODEL in the same turn ("MORE SUPPORTING EVIDENCE WILL
    NOT VALIDATE IT" — the restatement recovery note in ``context_builder``).
    Removing that contradiction by SUPPRESSION alone would leave a silent case,
    which is the failure ``_insufficient_evidence_handoff_pending`` exists to
    prevent — so the carve-out ships with this replacement, not without one.

    The mechanism move is unconditional — it is the recovery for every held
    shape. The SIBLING move is offered only when the hold actually depends on
    another standing hypothesis (``RestatementHold.involves_siblings``). The
    frame is ``anchors | other-hypothesis tokens``, so a root that restates the
    PROBLEM STATEMENT alone is held with no two hypotheses overlapping at all —
    and telling that user "two of the causes on the table may be one cause
    worded twice" asserts an overlap that does not exist (#1195 review, finding
    5). That is the same class of wrong guidance this fix exists to remove, so
    the engine discriminates instead: re-run the novelty core with the siblings
    dropped from the frame, and offer the move only if that releases the root.

    When the move IS offered, both recoveries are named for the reason the
    model-facing note names both: the sibling-held population has two shapes the
    engine cannot tell apart (the fm#1137 known limit). A root held by a TRUE
    DUPLICATE of its own hypothesis needs the mechanism stated distinctly; a
    root held by frame DILUTION — a different cause's verbose statement
    happening to cover this one — clears the moment that alternative is settled.

    Neither move asks for data, and neither steers toward close: a hold the
    engine can describe is not a reason to abandon the case (D4 soft-collapse).
    Non-clickable FREE_SPEECH — the user supplies the content.
    """
    # ``hold`` is passed by ``engine_owned_affordances``, which has already
    # computed it: recomputing here ran the tokenization sweep a SECOND time per
    # call and opened a window where the two reads could disagree (#1195
    # review). Absent (a bare call, or a cleared hook — see verification_status)
    # it degrades to the move that is true of every shape and drops the one that
    # needs evidence for its claim: the smaller, always-true offer is the safe
    # direction.
    if hold is None:
        hold = restatement_hold_governs(case)
    moves = [_MECHANISM_MOVE]
    if hold is not None and hold.involves_siblings:
        moves.append(
            {
                "label": "Say whether the standing explanations are the same cause",
                "action_type": "FREE_SPEECH",
                # The "these two may be one cause worded twice" framing was
                # retired with fm#1122: a root whose whole overlap ONE standing
                # explanation accounts for is now released as a duplicate, so a
                # root that is still sibling-held is one the standing
                # explanations SPAN — each contributing something the others do
                # not. Ruling one out is what collapses that span; asking
                # whether they are the same cause no longer describes the
                # population this move is offered to.
                "body": (
                    "The leading explanation spans two of the causes on the "
                    "table rather than picking one. Ruling one of them out — or "
                    "saying which single mechanism is doing the work — clears "
                    "the overlap that is holding it."
                ),
            }
        )
    return moves


def _restatement_held_pending(
    case: "Case", *, status: "VerificationStatus | None" = None
) -> bool:
    """Whether the engine should drive the restatement-held handoff this turn
    (#1195 — the fourth code-guarded branch).

    Reads the SAME join as its three peers rather than calling
    ``restatement_held_root_ids`` itself, so the affordance and the reported
    status can never disagree: a case has exactly one verification status, which
    is what makes all four branches mutually exclusive by construction.

    Scoped to ``INVESTIGATING`` and ordered with its peers, below the
    state-machine gates. Must be evaluated AFTER the per-turn recompute so the
    status read is fresh (the #593 re-derive-after-stamp ordering); both
    ``engine_owned_affordances`` call sites satisfy that.
    """
    if case.state != CaseState.INVESTIGATING:
        return False
    # Cheap short-circuit before the grounding-grade computation, mirroring the
    # siblings: RESTATEMENT_HELD is carved out of the not-grounded × stalled
    # cell, so it requires the full progress axis exactly as
    # ``INSUFFICIENT_EVIDENCE`` does (time thresholds OR a declared data wall).
    if not is_progress_stalled(case):
        return False
    if status is None:
        status = assess_verification_status(case)
    return status == VerificationStatus.RESTATEMENT_HELD


def _hypothesis_vacuum_suggestions() -> list:
    """Deterministic pull-back affordances for the NOT_YET_PRODUCTIVE vacuum
    (#656 P3.1, INV-38).

    The engine's half of the corrective is the *moves*; the boundary statement —
    *why* nothing has grounded — stays model-authored in the prose. These moves
    pull the investigation back to the basis a hypothesis needs: a precise symptom
    and a place to look. They are keep-engaging by construction (re-establish the
    diagnostic direction), never steering toward close — the engine nudging
    abandonment would be soft-collapse (D4), and the vacuum is the engine's own
    failure to elicit, not the case's to give up on. Non-clickable FREE_SPEECH:
    the user supplies the content.

    Distinct from ``_insufficient_evidence_handoff_suggestions``: that fires ABOVE
    the work gate on a built differential and asks for *discriminating* data; this
    fires with ZERO hypotheses and asks for the *foundational* symptom framing
    that lets a first hypothesis form at all.
    """
    return [
        {
            "label": "Describe the expected vs. observed behavior",
            "action_type": "FREE_SPEECH",
            "body": (
                "The investigation hasn't formed a working theory yet. A sharp "
                "expected-vs-observed contrast — what should happen, what actually "
                "happens — gives it a symptom precise enough to hypothesize from."
            ),
        },
        {
            "label": "Point to where the problem shows up",
            "action_type": "FREE_SPEECH",
            "body": (
                "Name a system, signal, or recent change tied to the problem — a "
                "concrete place to look seeds the first diagnostic direction."
            ),
        },
    ]


def _hypothesis_vacuum_pending(
    case: "Case", *, status: "VerificationStatus | None" = None
) -> bool:
    """Whether the engine should drive the NOT_YET_PRODUCTIVE pull-back this turn
    (#656 P3.1, DF-6 gap A — the 0-hypothesis corner of NOT_YET_PRODUCTIVE).

    ``assess_verification_status`` returns ``NOT_YET_PRODUCTIVE`` from turn 1
    whenever the work gate hasn't passed — too early to act on, which is why that
    status "drives nothing" today (DF-6). This predicate is the corrective: once
    the vacuum has PERSISTED past the stall thresholds (``is_stalled`` — the same
    turn / no-progress floor the insufficient-evidence handoff uses), a case still
    holding ZERO hypotheses has no diagnostic direction at all, and the engine
    pulls it back toward symptom / expected-vs-observed clarification so a
    hypothesis can form. Without this the only nets for a stuck case
    (insufficient-evidence handoff, exhaustion, deadlock) each require ≥2
    hypotheses, so a model that never hypothesizes evades every one and spins
    silently — the #656 empty-graph spin (`case_5db5417fe445`: 0 hypotheses across
    the whole session, INVESTIGATING for 13 turns).

    Scoped to the true 0-hypothesis VACUUM, not the whole work-gate-failing range:
    a case holding ≥1 hypothesis already has a diagnostic direction (pulling it
    back to re-describe the symptom would be wrong — it needs breadth /
    discrimination, a different and lower-stakes concern), so the corrective is
    deliberately confined to the vacuum the incident exhibits.

    INVESTIGATING-scoped and ordered LAST beside the insufficient-evidence handoff
    (both are mid-investigation readings below the state-machine gates); the two
    are mutually exclusive by construction (this requires 0 hypotheses; that
    requires the ≥2 work gate). Must be evaluated AFTER the per-turn recompute so
    the status read is fresh (the same #593 re-derive-after-stamp ordering the
    sibling handoff requires; both ``engine_owned_affordances`` call sites
    satisfy it).
    """
    if case.state != CaseState.INVESTIGATING:
        return False
    # The vacuum is specifically ZERO hypotheses — a case with a direction is not
    # pulled back. Cheapest discriminator, checked first.
    if case.hypotheses:
        return False
    # Act only once the vacuum has PERSISTED past the stall floor, not on every
    # early NOT_YET_PRODUCTIVE turn. The declared-data-wall arm of the full
    # progress axis is vacuous here (it ranges over residual candidates, of which
    # there are none at 0 hypotheses), so the cheap time arm ``is_stalled`` is the
    # exact and sufficient stall reading.
    if not is_stalled(case):
        return False
    # Authoritative guard: a 0-hypothesis case that is somehow grounded (a chain
    # validated with no backing hypothesis) reads HEALTHY/TREATMENT_BLOCKED, not
    # NOT_YET_PRODUCTIVE, and is not a vacuum — the status join decides.
    if status is None:
        status = assess_verification_status(case)
    return status == VerificationStatus.NOT_YET_PRODUCTIVE


def _treatment_blocked_suggestions() -> list:
    """Deterministic affordances for a case that HAS a cause but cannot reach a
    verified fix (§5.1's grounded × stalled cell — "failed fix, no access, change
    window, waiting on another team").

    The third peer of the insufficient-evidence handoff and the hypothesis-vacuum
    pull-back: same mechanism (a code-guarded branch that substitutes a
    deterministic affordance pair regardless of LLM compliance), different
    trigger, different ask. Where those two ask for data that would *ground* a
    cause, this one has the cause — what it lacks is a path to *verifying the
    fix*. Asking such a case for more diagnostic data is the wrong question, and
    was the observable symptom before this branch existed: the engine either
    re-offered a close every turn or restated the same fix and waited.

    **Names the blocker, never proposes a disposition.** Offering to close here
    would resurrect through the affordance channel exactly the deferred-close nag
    #1138 removed (five offers against five typed declines). Disposition stays
    with the disposition gate, which is checked first in
    ``engine_owned_affordances`` — so before a decline that gate owns the turn,
    and after it these moves take over without re-asking the settled question.
    Keep-engaging by construction (D4: the engine must never steer toward
    abandonment).

    Both sub-shapes of the cell are covered: a fix proposed but not yet applied
    (blocked on access, a window, or another team), and a grounded cause with no
    fix on the table yet. Non-clickable FREE_SPEECH — the user supplies the
    content.
    """
    return [
        {
            "label": "Say what's blocking the fix",
            "action_type": "FREE_SPEECH",
            "body": (
                "The investigation has a cause but can't confirm a fix from here. "
                "Naming what stands in the way — access, a change window, another "
                "team, or a fix already tried that didn't hold — lets it work the "
                "blocker instead of re-asking for data."
            ),
        },
        {
            "label": "Report what happened when the fix was applied",
            "action_type": "FREE_SPEECH",
            "body": (
                "If the change went in, its outcome is the decisive observation — "
                "what recovered, what didn't, or what broke instead. If it hasn't "
                "gone in yet, say so and the case holds without re-asking."
            ),
        },
    ]


def _treatment_blocked_pending(
    case: "Case", *, status: "VerificationStatus | None" = None
) -> bool:
    """Whether the engine should drive the treatment-blocked handoff this turn
    (#1136 — the third code-guarded branch).

    ``TREATMENT_BLOCKED`` was unreachable in-flight before #1136 (the grounding
    axis required ``CONFIRMED``, which only the resolution confirm-stamp mints),
    so the cell drove nothing because nothing ever landed in it. Making the axis
    read "any validated root" lands the **most common stall shape** there — a
    mechanistically identified cause waiting on a fix — and a reachable cell that
    drives nothing is the same defect this issue exists to close, one cell over.

    Scoped to ``INVESTIGATING`` and ordered with its peers, below the
    state-machine gates. Mutually exclusive with them by construction: all four
    read the same join, and a case has exactly one verification status.

    Must be evaluated AFTER the per-turn recompute so the status read is fresh
    (the #593 re-derive-after-stamp ordering); both
    ``engine_owned_affordances`` call sites satisfy that.
    """
    if case.state != CaseState.INVESTIGATING:
        return False
    # Cheap short-circuit before the grounding-grade computation, mirroring the
    # sibling handoff: TREATMENT_BLOCKED requires a stall. The plain time arm is
    # the exact reading here — the declared-data-wall arm belongs to the
    # not-grounded branch (it is about failing to GROUND a cause, which this cell
    # has already done), exactly as ``assess_verification_status`` scopes it.
    if not is_stalled(case):
        return False
    if status is None:
        status = assess_verification_status(case)
    return status == VerificationStatus.TREATMENT_BLOCKED


def _schema_prompt_instruction(schema: dict) -> str:
    """In-prompt schema block for providers that need the schema in prompt
    text (json_object / prompt_only strategies).

    ``SCHEMA_INSTRUCTIONS`` documents the investigation-turn output shape —
    ``evidence_trail``, milestone/outcome ``state_updates``, 2-4
    ``suggested_follow_ups``. Response models that don't carry that shape
    (TerminalResponse, InquiryResponse) must not receive it: instructing
    "outcome: REQUIRED" against a schema with no such field, or "2-4
    suggestions" on a turn whose template says to leave them empty, misleads
    exactly the weak providers this path serves. The gate keys on the schema
    itself — does it declare ``evidence_trail``? — so any future model
    gets the block iff it actually has the documented shape, rather than by
    class pedigree. The exact JSON schema remains the authority either way.
    """
    instructions = (
        f"{SCHEMA_INSTRUCTIONS}\n"
        if "evidence_trail" in schema.get("properties", {})
        else ""
    )
    schema_json = json.dumps(schema, indent=2)
    return (
        f"\n\n{instructions}"
        "You MUST respond with valid JSON matching this exact schema:\n\n"
        f"```json\n{schema_json}\n```\n\n"
        "IMPORTANT:\n"
        "- Use the exact field names shown in the schema\n"
        "- Do not add extra fields not in the schema\n"
        "- Do not include any text before or after the JSON\n"
        "- Ensure all required fields are present\n"
    )


#: Which verification status each mid-investigation gate reports on the turn it
#: fires. A dict rather than the if/elif chain it replaces: the labels are
#: produced in ``engine_owned_affordances`` and consumed at the return boundary,
#: so a gate added in one place and forgotten in the other used to fall through
#: in SILENCE — the turn simply carried no status. Two gates
#: (``treatment_blocked``, ``restatement_held``) were added that way before this
#: map existed. The state-machine gates (``disposition``, ``gate1``) are absent
#: on purpose: they are handshakes, not readings of the join, and have no status
#: to report.
#:
#: ``insufficient_evidence_restatement_held`` maps to INSUFFICIENT_EVIDENCE
#: deliberately — it is the same disposition wearing a different affordance
#: pair, and the turn metadata must agree with the persisted status.
_GATE_VERIFICATION_STATUS: dict[str, VerificationStatus] = {
    "insufficient_evidence": VerificationStatus.INSUFFICIENT_EVIDENCE,
    "insufficient_evidence_restatement_held": (
        VerificationStatus.INSUFFICIENT_EVIDENCE
    ),
    "restatement_held": VerificationStatus.RESTATEMENT_HELD,
    "not_yet_productive": VerificationStatus.NOT_YET_PRODUCTIVE,
    "treatment_blocked": VerificationStatus.TREATMENT_BLOCKED,
}


def engine_owned_affordances(
    case: "Case", metadata: dict[str, Any] | None = None
) -> tuple[str, list] | None:
    """Return ``(gate_name, affordance_list)`` when a state-machine gate is pending.

    The state machine has a small enumerable set of gates: imperative
    pending_transition (set by ``propose_transition`` via
    ``metadata['override_suggestions']``) and Gate 1 (problem-statement
    confirmation). When a gate is pending, the engine knows the canonical
    affordance pair; the LLM cannot add value there and shouldn't try.

    Gate 2 (investigation path) and Gate 3 (post-mitigation continuation)
    were removed (redesign R5): there is no prospective path fork, and a
    mitigation simply continues the flow when verified.

    Returns ``None`` when no gate is pending — the LLM's own DECIDE /
    EVIDENCE / FREE_SPEECH suggestions pass through unmodified.

    Gate identifiers (telemetry-stable labels):
      - ``"disposition"`` — pending_transition / propose_transition override
      - ``"gate1"`` — problem-statement confirmation
      - ``"insufficient_evidence"`` — work-gated stall with no grounded cause
        (code-guarded, always on)
      - ``"restatement_held"`` — a work-gated stall whose leading cause is held
        by the §7.1 restatement guard alone: the block is the cause's PHRASING,
        not missing data, so the moves ask for a distinct restatement rather
        than for evidence (#1195, code-guarded, always on)
      - ``"not_yet_productive"`` — persisted 0-hypothesis vacuum; a pull-back to
        symptom / expected-vs-observed clarification (code-guarded, always on)

    The disposition branch sits above gate1 because pending_transition can
    fire while gate1 is technically open. The mid-investigation readings sit
    LAST — any pending state-machine handshake (disposition, gate1) takes
    precedence over them — and are mutually exclusive with each other: all four
    read the same ``assess_verification_status`` join, and a case has exactly one
    verification status. Their order among themselves is therefore presentational
    only; ``restatement_held`` is written beside ``insufficient_evidence`` because
    it is the cell carved out of it.
    """
    md = metadata or {}

    if md.get("override_suggestions"):
        return ("disposition", md["override_suggestions"])

    if _gate1_is_pending(case):
        return ("gate1", _investigation_confirmation_suggestions(case))

    # The four mid-investigation readings below all ask the SAME join, and each
    # used to recompute it — across the two ``engine_owned_affordances`` call
    # sites that is up to eight recomputes per turn, each now carrying a
    # causal-graph tokenization sweep (#1195 review). Compute it ONCE here and
    # hand it down: cheaper, and it makes the mutual exclusivity structural
    # rather than merely argued — the four branches read one value. The
    # predicates keep their own cheap pre-checks, and each still computes the
    # status itself when called directly (tests, and any future caller that has
    # not got one).
    #
    # BOTH cheap guards are hoisted with it, not dropped (#1195 review). All
    # four readings require INVESTIGATING, and all four require a stall — the
    # two ungrounded ones via the full progress axis, the other two via its time
    # arm, which it subsumes. Without them here an ordinary PROGRESSING turn
    # would newly pay ``grade_cause_assurance`` plus a ``work_gate_passed``
    # rebuild of every evidence datum key, twice a turn, where each predicate
    # previously returned early. ``is_progress_stalled`` is exactly what
    # ``_insufficient_evidence_handoff_pending`` already ran first, so this
    # restores the pre-existing cost profile rather than adding to it.
    if case.state != CaseState.INVESTIGATING:
        return None
    if not is_progress_stalled(case):
        return None
    # ‼ KNOWN GAP, deliberately not closed here. A case carrying a qualifying
    # ``causal_absence_evidence`` row has had its cause confirmed ELIMINATED,
    # and every reading below asks for something that would help GROUND one —
    # discriminating data, a distinct restatement, expected-vs-observed. None
    # is coherent on such a case. Observed: a user who declined the resolve
    # offer once and then went quiet was served "Describe the expected vs.
    # observed behavior" about a problem they had already confirmed gone.
    #
    # Vetoing the readings on ``_has_causal_absence`` is the obvious fix and is
    # WRONG as a drive-by: the #1136 fixtures build work-gate-passing cases out
    # of absence rows, so the veto also silences ``treatment_blocked`` on every
    # case they represent. Whether a resolution confirmation should override
    # the verification-status join is a question about that cell's design, not
    # about this consolidator, and it wants its own change rather than seven
    # shipped tests rewritten to accommodate a guard added here.
    status = assess_verification_status(case)
    # ONE hold read per call, hoisted so neither branch below re-derives it: two
    # reads of the same fact in one turn is both a second tokenization sweep and
    # a window where they could disagree. Computed only for the two statuses
    # that can use it — the other branches would pay a graph sweep for an answer
    # they never look at. (The join above derives it once more internally on its
    # way to RESTATEMENT_HELD; threading that back out would mean returning a
    # tuple from a function whose whole contract is "one value per case", so
    # that second read stands, and the sweep-count pins record it.)
    hold = (
        restatement_hold_governs(case)
        if status
        in (
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            VerificationStatus.RESTATEMENT_HELD,
        )
        else None
    )

    if _insufficient_evidence_handoff_pending(case, status=status):
        # The composite (#1195 review): a declared data wall AND a governing
        # restatement hold. Same status, same disposition — but a different pair
        # and its own telemetry label, because a turn that silently swaps its
        # advice is a turn nobody can measure, and #1140's whole lesson was that
        # an unobservable hold costs a database read to find.
        gate = (
            "insufficient_evidence_restatement_held"
            if hold is not None
            else "insufficient_evidence"
        )
        return (gate, _insufficient_evidence_handoff_suggestions(case, hold=hold))

    if _restatement_held_pending(case, status=status):
        return ("restatement_held", _restatement_held_suggestions(case, hold=hold))

    if _hypothesis_vacuum_pending(case, status=status):
        return ("not_yet_productive", _hypothesis_vacuum_suggestions())

    if _treatment_blocked_pending(case, status=status):
        return ("treatment_blocked", _treatment_blocked_suggestions())

    return None


#: Metadata key: this turn arrived while a disposition handshake was standing,
#: whoever proposed it. Turn-scoped and never persisted. Distinct from its
#: sibling below, which is about the ENGINE's own offer and about refusal; this
