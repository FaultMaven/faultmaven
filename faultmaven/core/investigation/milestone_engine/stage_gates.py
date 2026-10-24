import logging
import re
from datetime import (
    UTC,
    datetime,
)
from typing import (
    Any,
    Literal,
    NamedTuple,
    Optional,
    Sequence,
)

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.lifecycle_metrics import (
    pending_action_superseded_stale_total,
    solution_offer_superseded_total,
)
from faultmaven.core.investigation.problem_status import (
    problem_on_hold,
    stage_cause_work,
)
from faultmaven.core.investigation.prompts.context_builder.budget import (
    structural_index_is_searchable,
)
from faultmaven.core.investigation.working_conclusion_generator import (
    generate_working_conclusion,
)
from faultmaven.modules.case.contracts import (
    ActionAttempt,
    Case,
    CaseState,
    EvidenceCategory,
    InterventionQuadrant,
    InvestigationActionType,
    MitigationRecord,
    SolutionType,
    WorkingConclusion,
)

logger = logging.getLogger(__name__)


def _gate_token_match(msg: str, tokens: Sequence[str]) -> Optional[tuple[str, int]]:
    """The token a typed gate answer opens with, and where its match ends.

    Word-boundary prefix match. Bare ``startswith`` also matched words that
    merely share the prefix — "note db latency spiked…" read as "no",
    "yesterday the pod restarted…" as "yes" — turning evidence-bearing
    messages into gate answers. Requiring a word boundary after the token
    keeps the intended matches ("no", "no.", "nope!", "yes, it's resolved")
    while rejecting the prefix-sharing words. When several tokens match at
    the start, the LONGEST wins, so the end reported is where the answer's
    token really ends. ``msg`` must already be stripped/lowercased.
    """
    best: Optional[tuple[str, int]] = None
    for token in tokens:
        match = re.match(rf"{re.escape(token)}\b", msg)
        if match and (best is None or match.end() > best[1]):
            best = (token, match.end())
    return best


def _matches_gate_token(msg: str, tokens: Sequence[str]) -> bool:
    """Whether ``msg`` opens with one of ``tokens`` — :func:`_gate_token_match`'s
    verdict, so the gate and anything that reads the matched token share one
    grammar."""
    return _gate_token_match(msg, tokens) is not None


#: Milestone names the ENGINE derives rather than the LLM claiming them. They
#: enter ``metadata["milestones_completed"]`` from the engine's own recompute
#: (#1284), which makes them real per-turn progress — but NOT evidence claims.
ENGINE_DERIVED_MILESTONES: frozenset[str] = frozenset({"root_cause_identified"})


def llm_claimable_milestones(milestones: "list[str]") -> "list[str]":
    """The subset of a turn's ``milestones_completed`` the LLM could have CLAIMED.

    The turn's list mixes two provenances: names the LLM emitted in
    ``MilestoneUpdates``, and names the ENGINE derived and appended itself
    (#1284). Both are real progress, but only the first kind is a claim — so
    anywhere the list is treated AS a claim (reviewed against cited evidence, or
    attributed to the evidence rows that arrived this turn), the engine's own
    derivations must be filtered out first. Order is preserved.
    """
    return [m for m in milestones if m not in ENGINE_DERIVED_MILESTONES]


CATEGORY_MILESTONE_MAP = {
    EvidenceCategory.SYMPTOM_EVIDENCE: [
        "symptom_verified",  # Confirms problem exists
    ],
    EvidenceCategory.CAUSAL_EVIDENCE: [
        # "root_cause_identified" is NOT here (#675 / INV-35): identification is
        # engine-derived from the validated causal chain (cause_state), not an
        # LLM-claimed milestone. The map's only consumer, _infer_milestones,
        # intersects category-eligible names with this turn's MilestoneUpdates —
        # and MilestoneUpdates no longer carries root_cause_identified, so the
        # entry could never be attributed (it attributed to nothing). Causal
        # evidence's contribution to identification flows through the chain
        # derivation, not this attribution map — which is where the turn's
        # ``root_cause_identified`` milestone comes from instead (#1284, written
        # at the recompute's rising edge in _recompute_assessment_state).
        "solution_proposed",  # Justifies proposed solution
    ],
    # Absence categories map to [] DELIBERATELY (not an oversight). The
    # verification gates (mitigation_verified / solution_verified) are set by
    # the LLM via the User-Agent Handshake / compliance detection — NOT by
    # evidence category. The map's only consumer (_infer_milestones) does
    # *attribution* (intersect category-eligible milestones with what the LLM
    # completed this turn), and these gates are not evidence-attributed.
    # The absence rows' disposition role is read DIRECTLY by the readiness
    # checks: assess_resolution_readiness/_closure consult _resolution_confirmed()
    # to decide RESOLVED vs CLOSED. So absence evidence drives dispositions
    # through readiness, not through this map — keep these at [].
    EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE: [],
    EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE: [],
    # Baseline/environmental data lives on ``uploaded_files``, not Evidence;
    # Evidence rows are only created when the agent extracts a
    # claim-relevant slice.
}


def _case_has_symptom_evidence(case: Case) -> bool:
    """Return True if the case has at least one SYMPTOM_EVIDENCE row.

    Backstop for Behavioral Rule 2 applied to MITIGATION ProposedActions:
    a mitigation must target an observed failure (recorded as
    SYMPTOM_EVIDENCE), not an unverified user claim. Used at the
    ProposedAction-creation site to gate MITIGATION → DIAGNOSTIC
    downgrades. Pure over case state; no side effects.
    """
    return any(e.category == EvidenceCategory.SYMPTOM_EVIDENCE for e in case.evidence)


def _has_searchable_material(case: Case) -> bool:
    """True when the case holds content the ``search_file`` tool can target.

    Two sources: any Evidence row (existing claim-anchored evidence), or any
    uploaded file with a non-trivial structural index. Post-010, a fresh
    upload creates an ``UploadedFile`` (not an Evidence row until the LLM
    extracts a slice), so on the evidence-*delivering* turn the searchable
    material lives on ``uploaded_files`` — ``bool(case.evidence)`` alone
    would be False and wrongly leave ``tool_choice=auto`` (#708). The
    searchability test is the context builder's own
    ``structural_index_is_searchable`` (single source of truth for the
    threshold below which the context builder renders no ``<uploaded_file>``
    at all), so a forced-DA turn is guaranteed a
    real search target and the tool loop cannot crash for lack of one.
    """
    if getattr(case, "evidence", None):
        return True
    return any(
        structural_index_is_searchable(uf.structural_index)
        for uf in getattr(case, "uploaded_files", None) or []
    )


def _should_force_tools(
    processing_mode: Optional[str], case: Case, has_pending: bool
) -> bool:
    """Decide ``tool_choice=required`` for a generation turn.

    Force Directed-Analysis tools only when all three hold: (a) the turn is
    classified ``directed_analysis``, (b) the case has searchable material for
    the tool to target, and (c) the user is not mid-confirmation (a
    ``pending_transition`` turn is a typed confirm/decline with nothing to
    search — forcing tools would crash the loop). This is the linchpin of
    #708: a fresh evidence-bearing upload reroutes to ``directed_analysis``
    AND satisfies ``_has_searchable_material`` via its UploadedFile, so tools
    are forced on the delivering turn instead of leaving it on
    ``tool_choice=auto`` where the agent can reply in prose. Forcing requires
    a call to any offered tool, the schema tool included, so it does not by
    itself guarantee evidence before the conclusion; what it rules out is the
    prose reply that would send the loop to its schema-only iteration.
    """
    return (
        processing_mode == "directed_analysis"
        and _has_searchable_material(case)
        and not has_pending
    )


def _route_toolless_turn_single_shot(
    processing_mode: Optional[str], case: Case, force_tools: bool
) -> bool:
    """Send a turn with nothing to search to the single-shot structured path.

    fm#1116: on gpt-5.x the provider must pin ``reasoning_effort: "none"``
    whenever function tools are attached, so a turn that runs the tool loop
    reasons at zero — and on a turn with NO searchable material (no evidence
    rows, no indexed upload) the loop offers nothing to search anyway. Route
    exactly those turns to the tool-less structured call, where the call site
    can declare ``ReasoningIntent.INFERENCE`` and the provider honours it.

    Measured on the replayed case_bf484a484a77 turn 9 (the ``df -h`` turn:
    no files, no evidence, ``force_tools`` False): a linked causal row
    persisted in 8/20 reps at effort none vs 19/20 at low.

    Kept on the tool loop: forced-tool (directed-analysis) turns, turns with
    searchable material, and ``knowledge_query`` turns — the last rely on
    ``kb_qa``/``web_search``, which the single-shot path cannot call.
    ``agent_meta`` turns (#1328) need no tool and follow the material rule
    like any other non-forced turn.
    """
    if force_tools or processing_mode == "knowledge_query":
        return False
    return not _has_searchable_material(case)


def _solution_cause_validated(
    case: Case, working_conclusion: WorkingConclusion | None = None
) -> bool:
    """M5 gate predicate: is the cause established enough to register a SOLUTION?

    Delegates to the **same** "cause established" predicate the terminal /
    resolution gate uses (``terminal_transitions._cause_identified``):
    ``cause_state == IDENTIFIED`` **or** a set ``RootCauseConclusion`` **or** a
    ``working_conclusion`` at ≥ 0.6. This shared predicate is load-bearing for
    two reasons:

    1. **Consistency — no deadlock.** M5 must never be *stricter* than the gate
       that lets a case RESOLVE. The resolution gate accepts the RCC / working-
       conclusion backstop because ``cause_state`` is a SOFT, under-reporting
       signal (see ``_recompute_cause_state_from_chain``). Keying M5 on the raw
       ``cause_state == IDENTIFIED`` alone would block a permanent fix on a case
       the engine would otherwise let the user resolve — the engine refusing to
       register the very fix that resolves the case.
    2. **Same-turn correctness — no false stall.** This turn's ``cause_state`` is
       recomputed only at the END of ``_apply_investigation_updates`` (after
       chain emission), *after* this gate runs, so reading ``cause_state`` here
       yields the PRIOR turn's value. The ``RootCauseConclusion`` is applied
       early in the same method (before this gate), so on the opportunistic
       same-turn "validate the root AND propose the fix" path the RCC branch of
       ``_cause_identified`` correctly sees this turn's grounding. For the
       working-conclusion leg the M5 call site passes
       ``_settled_working_conclusion``: ``case.working_conclusion`` is last
       turn's until the recompute rebuilds it, and scoring this turn's
       hypotheses as they stand mid-turn would read values the turn does not
       settle on (fm#1679).

    ``working_conclusion`` replaces ``case.working_conclusion`` for that leg;
    ``None`` reads the case's.

    A premature SOLUTION (cause not established by any signal) is downgraded to
    DIAGNOSTIC — flow continues; the LLM grounds the root or proposes a
    mitigation. Mitigation (WORKAROUND) is exempt.

    Scope (deferred): the methodology also exempts ``defensive_fix`` (permanent @
    intermediate), but the solution emission carries no ``InterventionQuadrant``,
    so the engine cannot distinguish it from a remediation — per-quadrant
    precision waits until the emission carries a quadrant. Pure; no side effects.
    """
    # Local import mirrors the module's other terminal_transitions uses (avoids
    # an import cycle) and keeps M5 and the resolution gate on ONE predicate.
    from faultmaven.core.investigation.terminal_transitions import _cause_identified

    return _cause_identified(case, working_conclusion=working_conclusion)


def _settled_working_conclusion(
    case: Case, metadata: dict[str, Any]
) -> WorkingConclusion:
    """The working conclusion this turn's likelihood updates will leave, for M5.

    M5 runs in the solutions step, before two things that move likelihoods:

    - an evidence link has already rewritten its hypothesis by formula
      (``initial + 0.15·supports − 0.20·refutes``), discarding the value the
      model set earlier, so mid-turn a hypothesis can stand above OR below
      where it was and where it will be;
    - the model's own likelihood updates are deferred past chain emission
      (``deferred_likelihood_updates``, one per hypothesis) so the B1 cap sees
      the turn's links.

    Each hypothesis with a pending update is scored at the value
    ``HypothesisManager.likelihood_after_update`` says that update will apply,
    so M5 judges the belief the turn settles on — neither a transient formula
    value (which could license a fix, and a same-turn ``solution_accepted``
    make it permanent, on a belief never held) nor last turn's conclusion.
    The one difference from the settled value: a raise that only chain
    emission's links would uncap is scored capped, so such a license is seen
    next turn. Nothing is written to the case.
    """
    overrides: dict[str, float] = {}
    for h_id, likelihood in metadata.get("deferred_likelihood_updates") or ():
        hypothesis = case.hypotheses.get(h_id)
        # The deferred step skips a hypothesis that became terminal this turn.
        if hypothesis is not None and not hypothesis.state.is_terminal:
            overrides[h_id] = HypothesisManager.likelihood_after_update(
                hypothesis, likelihood, case
            )
    return generate_working_conclusion(
        case=case, current_turn=case.current_turn, likelihood_overrides=overrides
    )


def _refresh_working_conclusion(case: Case) -> None:
    """Rebuild ``case.working_conclusion`` from the case as it stands now.

    The build used to happen only at Step 5.6, after ``_apply_investigation_updates``,
    so the recompute's license re-check read LAST turn's conclusion: a license
    resting on it survived a turn after this turn's updates or an M6 demotion
    took it away (fm#1679). ``_recompute_assessment_state`` now rebuilds it just
    before that re-check; Step 5.6 rebuilds it again because housekeeping decay
    runs after the recompute. ``generate_working_conclusion`` is a pure function
    of the case, so building it twice in a turn is safe.
    """
    if case.state == CaseState.INVESTIGATING:
        case.working_conclusion = generate_working_conclusion(
            case=case, current_turn=case.current_turn
        )


def _coerce_intervention_quadrant(raw: object) -> Optional[InterventionQuadrant]:
    """Honor-or-reject a solution emission's ``quadrant`` string (R9).

    The LLM may tag a ``SolutionToAdd`` with the intervention quadrant of a
    surfaced runbook candidate. Coerce the free-text value to the enum; a missing
    or unrecognized value yields ``None`` (recorded as unquadranted) rather than a
    hard parse failure — BEST_EFFORT providers must not crash the turn on a typo.
    Recorded as DATA only: M5's downgrade logic is unchanged (per-quadrant
    exemptions are a separate, soundness-sensitive decision).
    """
    if not raw:
        return None
    try:
        return InterventionQuadrant(str(raw).strip().lower())
    except ValueError:
        return None


def _determine_action_type(
    case: Case, solution_type: SolutionType
) -> InvestigationActionType:
    """
    Determine whether a proposed solution is a MITIGATION or SOLUTION action.

    Used when creating ProposedAction from SolutionToAdd. The action_type
    determines which stage-gate behavior follows:
    - MITIGATION → mitigation insert (Mitigating)
    - SOLUTION → solution_accepted → enters TREATMENT stage

    Logic:
    1. WORKAROUND solution_type → MITIGATION (explicitly temporary)
    2. Otherwise → SOLUTION

    There is no prospective path fork (redesign R5). A mitigation is an
    opportunistic insert driven by the prompt, surfaced via a WORKAROUND
    solution_type.
    """
    if solution_type == SolutionType.WORKAROUND:
        return InvestigationActionType.MITIGATION

    return InvestigationActionType.SOLUTION


# ProposedAction states that count as a LIVE offer for the solution_proposed
def _supersede_pending_solution_offers(
    case: Case, *, reason: Literal["reproposal", "license_lost"]
) -> tuple[int, int | None]:
    """Mark every PENDING SOLUTION offer superseded; return (count, newest_turn).

    ``newest_turn`` is the greatest ``proposed_in_turn`` among the offers just
    superseded (None when count is 0) — the withdrawal path needs it as the
    INV-33 shadow cutoff, and this single pass already visits exactly those
    actions and reads their turn before flipping state, so it is returned here
    rather than recomputed in a duplicate pre-pass.

    Only pending offers are touched: an ACCEPTED offer records that the user
    executed the fix — a fact supersession cannot unmake (its truth surface
    is the M6 failed-fix machinery, not offer liveness). MITIGATION and
    DIAGNOSTIC actions are out of scope (they never fed ``solution_proposed``
    and mitigations are not licensed by an established cause). ``reason`` is
    a closed vocabulary (typed here AND on the model field) because it feeds
    the ``solution_offer_superseded_total`` metric label — a free-form string
    would grow label cardinality silently.
    """
    count = 0
    newest_turn: int | None = None
    for action in case.proposed_actions:
        if (
            action.action_type == InvestigationActionType.SOLUTION
            and action.state == "pending"
        ):
            if newest_turn is None or action.proposed_in_turn > newest_turn:
                newest_turn = action.proposed_in_turn
            action.state = "superseded"
            action.superseded_reason = reason
            action.superseded_in_turn = case.current_turn
            solution_offer_superseded_total.labels(reason=reason).inc()
            count += 1
    return count, newest_turn


def fix_identity(description: Optional[str]) -> str:
    """A fix's identity (#2105): its description, whitespace-collapsed and
    case-folded. A reworded fix is a new one (#1136)."""
    return " ".join((description or "").split()).casefold()


def standing_fix_action(case: Case, description: Optional[str]) -> Any:
    """The SOLUTION action a re-proposal with ``description`` keeps, or None:
    the pending offer bearing that fix identity, or the latest dated fix while
    its own window has not FAILED (#2105). After a FAILED window the same fix
    applied again is a new attempt."""
    from faultmaven.core.investigation.cause_assurance import (
        FIX_FAILED,
        latest_fix_window,
    )

    identity = fix_identity(description)
    if not identity:
        return None
    window = None
    for action in reversed(case.proposed_actions):
        if (
            action.action_type != InvestigationActionType.SOLUTION
            or fix_identity(action.description) != identity
        ):
            continue
        if action.state == "pending":
            return action
        if action.state == "accepted" and action.accepted_in_turn is not None:
            window = window or latest_fix_window(case)
            if action.accepted_in_turn == window.fix_turn and (
                window.state != FIX_FAILED
            ):
                return action
    return None


def _newest_fix_bearer(case: Case, identity: str, minted: Sequence = ()) -> Any:
    """The newest SOLUTION, or action this apply ``minted``, bearing the fix."""
    return next(
        (
            action
            for action in reversed(case.proposed_actions)
            if (
                action.action_type == InvestigationActionType.SOLUTION
                or action in minted
            )
            and fix_identity(action.description) == identity
        ),
        None,
    )


def _retire_shadowed_diagnostic_asks(case: Case, *, before_turn: int) -> int:
    """Retire stale DIAGNOSTIC pending asks a SOLUTION offer shadowed (INV-33).

    The ``<pending_action>`` render (context_builder: newest pending action of
    ANY type) shows one action at a time — a SOLUTION offer, while it stands,
    shadows any EARLIER ask beneath it. When that offer LEAVES pending state —
    WITHDRAWN on license loss (the cause fell, the case is back in active
    diagnosis) or ACCEPTED (the case moves to TREATMENT to verify the executed
    fix) — the render falls through to a shadowed ask and resurfaces it as the
    current compliance target, an ask the investigation already moved past.
    Retire the shadowed DIAGNOSTIC asks (pending, proposed STRICTLY BEFORE
    ``before_turn`` = the offer's turn) so compliance detection reads a clean
    slate. Asks proposed in the offer's OWN turn or AFTER are a live/reopening
    thread — the de-absolutized Zone-3 prompt (INV-33) now invites a parallel
    diagnostic when the user reopens the thread — and stand (strict ``<``, the
    same-turn create-then-withdraw edge preserves the reopened ask).

    DIAGNOSTIC-ONLY by design. A DIAGNOSTIC ask has no compliance gate (it never
    transitions to ``accepted``), so retiring one is a pure display cleanup with
    zero functional loss — the stale pre-fix evidence request is exactly what
    goes obsolete once the fix is on the table. A pending MITIGATION is NOT
    retired: a workaround is cause-INDEPENDENT symptom relief the user may still
    execute, so its liveness survives (INV-32) and its reappearance as the top
    pending ask is correct, not stale.
    """
    count = 0
    for action in case.proposed_actions:
        if (
            action.action_type == InvestigationActionType.DIAGNOSTIC
            and action.state == "pending"
            and action.proposed_in_turn < before_turn
        ):
            action.state = "superseded"
            action.superseded_reason = "stale_pending"
            action.superseded_in_turn = case.current_turn
            pending_action_superseded_stale_total.inc()
            count += 1
    return count


def _withdraw_unlicensed_solution_offers(
    case: Case, metadata: dict[str, Any] | None = None
) -> int:
    """Re-check the M5 license on standing PENDING solution offers (INV-32).

    A SOLUTION offer is admitted only while a cause is established — the M5
    creation gate, ``_solution_cause_validated``, called here VERBATIM so the
    creation gate, this liveness re-check, and the deferred-close gate can
    never diverge on what "established" means. Re-checked at recompute time,
    after this turn's demotions/retractions have settled: when the license
    has fallen (M6 failed-fix demotion, conclusion retraction, MECE hold, or
    the working-conclusion proxy dropping below its bar — e.g. stagnation
    decay), the pending offer is WITHDRAWN — superseded, out of the
    ``<pending_action>`` context block and the ``solution_proposed``
    derivation — rather than kept standing as "awaiting execution" for a
    cause the engine no longer asserts (#656 DF-3: the frame latch).

    The withdrawal is surfaced to the LLM via ``system_feedback`` so the next
    turn re-grounds the cause (or proposes a WORKAROUND mitigation) instead of
    referencing a proposal the user can no longer see as pending. The notice
    is PREPENDED: the turn record truncates feedback head-first, and on messy
    turns (exactly when withdrawals happen) earlier accumulators can push a
    tail-appended notice past the cap. The notice deliberately does NOT name
    a mechanism — the license can fall to a demotion, a retraction, a MECE
    hold, or plain confidence decay on the working-conclusion proxy, and the
    engine cannot always tell which from here.

    Known timing edges (documented, accepted):

    - The ``working_conclusion`` proxy leg is rebuilt immediately before this
      re-check (``_refresh_working_conclusion`` in the recompute, after the
      cause recompute), so a license resting solely on it clears in the same
      turn when this turn's likelihood updates or an M6 demotion take the
      leading hypothesis below the bar. Housekeeping decay runs AFTER the
      recompute, so a license lost to decay clears on the FOLLOWING turn —
      one-turn lag. The prompt frame still exits same-turn regardless: the
      Zone-3-pending conjunction requires ``cause_state == IDENTIFIED``,
      which the demotion drops in this same recompute. cause_state / RCC /
      contest falls withdraw same-turn.
    - The reverse composition — M5 admits on the truth as of the solutions
      step (the PRIOR turn's cause_state and contest flag; this turn's RCC;
      the working conclusion this turn's likelihood updates will leave), this
      re-check reads the settled truth — means an offer emitted in the very
      turn that knocks its cause down is admitted then withdrawn SAME TURN
      (pinned; the engine must not end a turn presenting a fix for a cause
      it no longer asserts). The assistant's already-delivered prose may
      still describe that fix for one turn; the next turn's context carries
      this notice and no ``<pending_action>`` block.
    - A standing LLM-authored RootCauseConclusion keeps the license through a
      MECE hold (trust boundary — the engine withholds only its OWN
      assertions; LLM-conclusion retraction is the follow-up tracked on
      #656), so the MECE trigger withdraws only licenses resting on
      cause_state / the engine mirror / the working-conclusion proxy.
    """
    if _solution_cause_validated(case):
        return 0
    count, withdrawn_cutoff = _supersede_pending_solution_offers(
        case, reason="license_lost"
    )
    if not count:
        return 0
    # INV-33: retire the DIAGNOSTIC asks the withdrawn offer shadowed, so the
    # <pending_action> render cannot resurface a stale earlier ask now that the
    # SOLUTION on top of it is gone. count>0 ⇒ withdrawn_cutoff is a real turn.
    _retire_shadowed_diagnostic_asks(case, before_turn=withdrawn_cutoff)
    logger.warning(
        f"Withdrew {count} pending SOLUTION offer(s) for case {case.case_id}: "
        f"the established-cause license fell "
        f"(cause_state={case.progress.cause_state.value}, "
        f"rcc={'set' if case.root_cause_conclusion else 'none'})",
        extra={
            "event": "solution_offer_withdrawn",
            "case_id": case.case_id,
            "turn": case.current_turn,
            "withdrawn": count,
        },
    )
    if metadata is not None:
        _add_system_feedback(
            metadata,
            (
                (
                    # #2036: the cause holds but its why is open, so the fix acts on
                    # a state something re-creates.
                    "SYSTEM: Your pending SOLUTION proposal was withdrawn: the cause "
                    "it targets validated, but its why is open (ROOT-CAUSE TEST, "
                    "clause b), so the fix would only intercept a state that comes "
                    "back. Ask for what produced that state; then re-root onto that "
                    "cause or mark the root terminal (root_is_terminal true, with "
                    "root_terminal_basis) and re-propose. To restore service now, "
                    "propose it as a temporary mitigation (WORKAROUND)."
                )
                if case.progress.cause_identification_held_open
                else (
                    "SYSTEM: Your pending SOLUTION proposal was withdrawn because "
                    "the root cause it targeted is no longer established. Re-ground "
                    "the root cause with evidence before re-proposing a permanent "
                    "fix, or propose a temporary mitigation (WORKAROUND) instead."
                )
            ),
            # Prepend (see docstring): truncation keeps the head.
            prepend=True,
        )
    return count


def _flag_absence_inside_a_failed_fix_window(
    case: Case, metadata: dict[str, Any] | None
) -> bool:
    """Tell the model when it records an absence row this turn inside a fix
    window that already FAILED (#1927, ``cause_assurance.fix_windows``).

    Such a row confirms nothing: once a fix was seen failing (the cause shown
    removed, then the problem seen again), only a NEW fix opens a window that
    can resolve the case. Without this notice the user's "it's fine now" and
    the model's absence rows would loop against the readiness gate, which no
    longer counts them. The way forward is a recorded fix: a SolutionToAdd for
    what the user changed, and ``solution_accepted`` once they say it ran. The
    M5 license that admits the SolutionToAdd survives the demoted cause through
    a root_cause_conclusion — on a NEW hypothesis when the cause is the same
    one re-applied correctly — which is the intended re-licensing path. A
    RELAPSE demotes nothing (``FixWindow.disconfirms_cause``, #2039), so the
    notice says only that and leaves the next step to the graph the model
    sees: no new hypothesis while the addressed cause stands, a re-assertion
    once it fell for another reason (a refutation on evidence, an earlier
    window's demotion). Which cause a terse cause row addressed is not
    something the engine can tell (``BearingFrame`` reads it as about no
    chain), so the notice does not assert it.

    Prepended, like the withdrawal notice: the turn record truncates feedback
    head-first. Returns whether the notice was added."""
    from faultmaven.core.investigation.cause_assurance import (
        ENGINE_EVIDENCE_AUTHOR,
        FIX_FAILED,
        latest_fix_window,
    )

    if metadata is None:
        return False
    window = latest_fix_window(case)
    if window.state != FIX_FAILED:
        return False
    absence = (
        EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
    )
    if not any(
        getattr(e, "category", None) in absence
        and getattr(e, "collected_by", None) != ENGINE_EVIDENCE_AUTHOR
        and (getattr(e, "collected_at_turn", 0) or 0) == case.current_turn
        # After the failure: the rows that established it are not an attempt
        # to confirm anything.
        and case.current_turn > window.failure_turn
        for e in case.evidence or []
    ):
        return False
    if window.disconfirms_cause:
        shape = "the cause was shown removed, then the problem was seen again"
        cause = (
            " — re-assert the cause with a root_cause_conclusion on a new "
            "hypothesis if the same cause was addressed again — then"
        )
    else:
        shape = (
            f"the problem was seen gone at turn {window.relief_turn}, then seen "
            "again"
        )
        cause = (
            " — a problem that went away after the fix and came back does not "
            "by itself disprove the cause the fix addressed: if that cause still "
            "stands, no new hypothesis is needed; if it no longer stands, "
            "re-assert it with a root_cause_conclusion on a new hypothesis — then"
        )
    _add_system_feedback(
        metadata,
        "SYSTEM: The fix executed at turn "
        f"{window.fix_turn} did not hold: {shape}. Absence evidence recorded "
        "after that cannot confirm a resolution. If the user has applied (or "
        "will apply) a new fix, record it: propose it as a SolutionToAdd and, "
        "once the user says it ran, set solution_accepted with "
        "accepted_action_ref naming it (new_index_N, or its ACTION_ID once "
        f"proposed){cause} record the "
        "cause gone and the problem gone after it. If the problem is only "
        "quiet after a workaround, offer to close instead.",
        prepend=True,
    )
    return True


def _normalise_id_ref(ref: Any) -> Any:
    """Strip whitespace and one enclosing ``[...]`` from a model-emitted id ref.

    ``None`` and non-strings pass through untouched so callers that probe the
    return value keep their contract.
    """
    if not isinstance(ref, str):
        return ref
    ref = ref.strip()
    if len(ref) >= 2 and ref[0] == "[" and ref[-1] == "]":
        ref = ref[1:-1].strip()
    return ref


def _add_system_feedback(
    metadata: dict[str, Any], message: str, *, prepend: bool = False
) -> None:
    """Accumulate a system notice for the next turn's prompt context.

    ``prepend`` puts the notice at the HEAD: the turn record truncates
    feedback head-first, so a notice that must survive a messy turn (many
    accumulators active) goes in front. Default is chronological append.
    """
    current = metadata.get("system_feedback", "") or ""
    parts = (message, current) if prepend else (current, message)
    metadata["system_feedback"] = "\n".join(p for p in parts if p).strip()


#: The turn's EVIDENCE VALIDATION line as it stands: what it names and says.
_EVIDENCE_VALIDATION_KEY = "evidence_validation_feedback"
_NAMED_ERROR = re.compile(r"Milestones \[([^\]]*)\] (completed without .*)", re.S)


def justification_skipped(case: Case) -> bool:
    """No milestone justification is asked on a terminal case or while a
    terminal transition is in progress (a pending transition, or the solution
    verified). The gate reads it at decision time (#2105)."""
    return case.is_terminal or (
        case.state == CaseState.INVESTIGATING
        and bool(
            getattr(case, "pending_transition", None) or case.progress.solution_verified
        )
    )


def add_evidence_validation_feedback(
    metadata: dict[str, Any], *, not_recorded: Sequence[str], errors: Sequence[str]
) -> None:
    """Which claimed milestones were not recorded, and why: ONE prepended line
    per turn under one ``EVIDENCE VALIDATION:`` prefix. Its two writers, the
    reasoning-first strip and the gate refusing an unjustified fix
    registration (#2105), merge into it."""
    previous = metadata.get(_EVIDENCE_VALIDATION_KEY)
    names = set(not_recorded)
    said: list[str] = list(errors)
    if previous is not None:
        names |= set(previous["not_recorded"])
        said = previous["errors"] + said
        metadata["system_feedback"] = "\n".join(
            part
            for part in (metadata.get("system_feedback") or "").split("\n")
            if part != previous["line"]
        )
    # One message per kind: "Milestones [a] <text>" and "Milestones [b] <text>"
    # merge into "Milestones [a, b] <text>".
    merged: dict[str, list[str]] = {}
    for error in said:
        named = _NAMED_ERROR.fullmatch(error)
        text = named.group(2) if named else error
        merged.setdefault(text, [])
        for name in re.findall(r"'(\w+)'", named.group(1)) if named else ():
            if name not in merged[text]:
                merged[text].append(name)
    head = f"Milestones {sorted(names)} were NOT recorded this turn. " if names else ""
    line = f"EVIDENCE VALIDATION: {head}" + " ".join(
        f"Milestones {named} {text}" if named else text
        for text, named in merged.items()
    )
    metadata[_EVIDENCE_VALIDATION_KEY] = {
        "not_recorded": sorted(names),
        "errors": said,
        "line": line,
    }
    _add_system_feedback(metadata, line, prepend=True)


# Gate signal → the ProposedAction type it may register against (INV-32
# type-matched compliance). ONE table consumed by the acceptance targeting in
# ``_apply_stage_gate_side_effects`` — the mapping previously lived in three
# hand-written sites, and a gate added to one but not another recreates the
# any-type misattribution this table exists to prevent. A SOLUTION acceptance
# registers the action ``decide_fix_acceptance`` decided, never a search for
# one; a MITIGATION acceptance the most recent pending MITIGATION.
# ``mitigation_verified`` is deliberately absent: verification accepts no
# action (its action left pending at the accept step).
_GATE_ACTION_TYPE: dict[str, InvestigationActionType] = {
    "solution_accepted": InvestigationActionType.SOLUTION,
    "mitigation_accepted": InvestigationActionType.MITIGATION,
}


# ---------------------------------------------------------------------------
# Fix acceptance, decided once, in the gate (#2105). ``solution_accepted`` was a
# latch: a second fix the user ran never registered, so RESOLVED was out of
# reach after a failed fix. The acceptance now names the fix
# (``accepted_action_ref``); ONE function resolves it to a fix identity and
# applies one state rule to the newest SOLUTION bearing it. A revision hold
# carries the identity, never a ref, and the replay applies the same rule.
# ---------------------------------------------------------------------------

#: Set by ``process_response_structured`` before the apply: why the response
#: could not justify a ``solution_accepted``, or None. Absent (the replay, a
#: direct call), nothing is checked.
FIX_JUSTIFICATION_KEY = "fix_acceptance_justification_error"

#: Set on a replayed bundle's metadata to the ``StagedCauseWork`` whose hold
#: turn carried a fix acceptance (``held_fix``).
CARRIED_FIX_KEY = "carried_fix_acceptance"


class _FixEntry(NamedTuple):
    """An entry of this response's ``solutions_to_add``. ``action``: the action
    it minted or kept (live turn), None (hold turn). ``is_fix``: a SOLUTION
    action (live), not a WORKAROUND (hold)."""

    identity: str
    action: Any
    is_fix: bool


_NO_PENDING_SOLUTION_NOTICE = (
    "SYSTEM: 'solution_accepted' was not registered — no "
    "SOLUTION proposal is currently pending (it may have been "
    "downgraded, withdrawn or superseded since it was made). If "
    "the user has already carried out the fix and the root cause "
    "stands established, register it in ONE response: re-propose "
    "it as a SolutionToAdd and set solution_accepted with "
    "accepted_action_ref new_index_N naming it, justified by "
    "their report; do not ask them to accept it again. If the root "
    "cause is not established, re-ground it with evidence first. If "
    "the problem is already resolved, record the confirming "
    "evidence instead: causal_absence for the cause gone and "
    "symptom_absence for the reported problem gone."
)


def _license_notice(subject: str) -> str:
    """The M5 refusal: a permanent fix registers only on an established cause."""
    return (
        f"SYSTEM: 'solution_accepted' was not registered: {subject} targets a "
        "root cause that is no longer established, and a permanent fix needs "
        "one (M5). Re-ground the root cause with evidence first, or propose a "
        "temporary mitigation (WORKAROUND)."
    )


def _withdrawn_notice(action_id: str) -> str:
    """The ruled refusal of a fix withdrawn ``license_lost`` once the license
    holds again: the name does not register it, a re-proposal does."""
    return (
        f"SYSTEM: 'solution_accepted' was not registered: {action_id} was "
        "withdrawn while its root cause was not established, and a withdrawn fix "
        "does not register by its ACTION_ID. If the user carried it out, "
        "re-propose it as a SolutionToAdd and set solution_accepted with "
        "accepted_action_ref new_index_N naming it."
    )


def _refuse_withdrawn(metadata: dict[str, Any], action_id: str, licensed: bool) -> None:
    notice = _withdrawn_notice if licensed else _license_notice
    _add_system_feedback(metadata, notice(action_id))


def _license_holds(case: Case, metadata: dict[str, Any]) -> bool:
    """The M5 license now, on the mint path's predicate."""
    return _solution_cause_validated(
        case, working_conclusion=_settled_working_conclusion(case, metadata)
    )


def _note_failed_fix(case: Case, bearer: Any, metadata: dict[str, Any]) -> None:
    """A registered fix named again but not re-sent, that identity no longer
    keeps (its window failed, or a later fix followed): it may be re-applied."""
    if standing_fix_action(case, bearer.description) is not bearer:
        _add_system_feedback(
            metadata,
            f"SYSTEM: 'solution_accepted' was not registered again: SOLUTION "
            f"{bearer.action_id} is registered, and its fix window failed or a "
            "later fix followed it. If the user re-applied it, re-propose it as a "
            "SolutionToAdd and set accepted_action_ref new_index_N naming it.",
        )


def _refuse_fix_ref(case: Case, metadata: dict[str, Any], ref: Any, what: str) -> None:
    logger.warning(
        f"Rejected stage-gate milestone 'solution_accepted' for case "
        f"{case.case_id}: accepted_action_ref {ref!r} names {what}"
    )
    _add_system_feedback(
        metadata,
        f"SYSTEM: 'solution_accepted' was not registered: accepted_action_ref "
        f"{ref} names {what}. It must name the SOLUTION the user carried out: "
        "its ACTION_ID, or new_index_N for one this response proposes.",
    )


def _what_is_named(action: Any) -> str:
    """How a refusal describes the action a ref names."""
    what = f"{action.action_id}, a {action.action_type.value.upper()}"
    if action.action_type == InvestigationActionType.MITIGATION:
        return what + " (set mitigation_accepted for it)"
    if action.downgrade_reason:
        return what + " (downgraded; see its ENGINE_NOTE)"
    return what


def _fix_named_by(
    case: Case, ref: str, action: Any, metadata: dict[str, Any], proposed: set
) -> Optional[tuple[str, str]]:
    """The fix a ref naming ``action`` resolves to, type-matched (INV-32): a
    SOLUTION's own; a DIAGNOSTIC's only when a NEWER SOLUTION bears it, or a
    hold turn proposes it; a MITIGATION's never."""
    identity, what = fix_identity(action.description), _what_is_named(action)
    if action.action_type != InvestigationActionType.SOLUTION:
        bearer = _newest_fix_bearer(case, identity)
        actions = case.proposed_actions
        if action.action_type == InvestigationActionType.MITIGATION or (
            identity not in proposed
            and (bearer is None or actions.index(bearer) < actions.index(action))
        ):
            _refuse_fix_ref(case, metadata, ref, what)
            return None
    return identity, what


def _newest_pending_solution(case: Case) -> Any:
    return next(
        (
            action
            for action in reversed(case.proposed_actions)
            if action.state == "pending"
            and action.action_type == InvestigationActionType.SOLUTION
        ),
        None,
    )


def _fix_entries(proposals: Sequence[Any], held: Optional[Sequence[Any]]) -> list:
    if held is not None:
        return [
            _FixEntry(
                fix_identity(item.description),
                None,
                item.solution_type != SolutionType.WORKAROUND,
            )
            for item in held
        ]
    return [
        _FixEntry(
            fix_identity(action.description),
            action,
            action.action_type == InvestigationActionType.SOLUTION,
        )
        for action in proposals
    ]


def _carried_fixes(case: Case) -> set:
    """The fix identities this pending revision's bundles already carry."""
    staged = case.problem_verification.pending_revision.staged
    return {bundle.held_fix for bundle in staged if bundle.held_fix}


def _note_unnamed_acceptance(
    case: Case, entries: list, metadata: dict[str, Any], *, holding: bool
) -> None:
    """A ``solution_accepted`` with no ref after a fix is registered: absorbed,
    since models re-emit standing booleans, with one line naming what WOULD
    register, should the user have run another fix."""
    line = None
    carried = _carried_fixes(case) if holding else set()
    for index in reversed(range(len(entries))):
        entry = entries[index]
        if not entry.is_fix or entry.identity in carried:
            continue
        kept = standing_fix_action(case, entry.identity) if holding else entry.action
        if kept is not None and kept.state == "accepted":
            continue  # the registered fix, restated
        line = (
            "Only if the user reported carrying out the fix this response "
            f"proposes, set accepted_action_ref to new_index_{index} with "
            "solution_accepted; otherwise leave solution_accepted unset."
        )
        break
    if line is None:
        pending = _newest_pending_solution(case)
        if pending is None:
            return
        aid = pending.action_id
        line = (
            f"SOLUTION {aid} is pending. Only if the user reported carrying out "
            f"{aid}, set accepted_action_ref to {aid} with solution_accepted; "
            "otherwise leave solution_accepted unset."
        )
    _add_system_feedback(
        metadata,
        "SYSTEM: 'solution_accepted' was not registered again: a fix is already "
        "registered, and an acceptance after it registers only the fix it "
        "names. " + line,
    )


def _resolve_fix_identity(
    case: Case,
    ref: Optional[str],
    entries: list,
    metadata: dict[str, Any],
    *,
    holding: bool,
) -> Optional[tuple[str, str]]:
    """Step 1: the fix identity an acceptance names, and how a refusal would
    describe what its ref names; None once a notice has settled it.

    An ACTION_ID gives the named action's fix; ``new_index_N`` the Nth entry of
    this response's ``solutions_to_add``; no ref, with no fix registered yet,
    this response's last fix proposal, else the pending SOLUTION."""
    if ref is None:
        if case.progress.solution_accepted:
            _note_unnamed_acceptance(case, entries, metadata, holding=holding)
            return None
        carried = holding and [
            b.turn
            for b in case.problem_verification.pending_revision.staged
            if b.held_fix
        ]
        if carried:
            _add_system_feedback(
                metadata,
                "SYSTEM: 'solution_accepted' was not registered again: the fix "
                f"execution reported at turn {carried[-1]} stands, held until the "
                "user confirms the revised problem statement. Only if the user "
                "carried out another fix, name it with accepted_action_ref.",
            )
            return None
        fixes = [entry for entry in entries if entry.is_fix]
        if fixes:
            return fixes[-1].identity, "the fix this response proposes"
        pending = _newest_pending_solution(case)
        if pending is not None:
            return fix_identity(pending.description), _what_is_named(pending)
        logger.warning(
            f"Rejected stage-gate milestone 'solution_accepted' for case "
            f"{case.case_id}: no pending SOLUTION ProposedAction exists"
        )
        _add_system_feedback(metadata, _NO_PENDING_SOLUTION_NOTICE)
        return None
    if ref.startswith("new_index_"):
        try:
            index = int(ref.removeprefix("new_index_"))
        except ValueError:
            index = -1
        if not 0 <= index < len(entries):
            _refuse_fix_ref(case, metadata, ref, "no fix this response proposes")
            return None
        entry = entries[index]
        if entry.action is not None:
            return _fix_named_by(case, ref, entry.action, metadata, set())
        if not entry.is_fix:
            _refuse_fix_ref(
                case, metadata, ref, "a WORKAROUND (set mitigation_accepted for it)"
            )
            return None
        return entry.identity, "the fix this response proposes"
    named = next((a for a in case.proposed_actions if a.action_id == ref), None)
    if named is None:
        _refuse_fix_ref(case, metadata, ref, "no action on this case")
        return None
    proposed = {e.identity for e in entries if e.is_fix} if holding else set()
    return _fix_named_by(case, ref, named, metadata, proposed)


def _bearer_registers(
    case: Case, bearer: Any, metadata: dict[str, Any], *, carried: bool
) -> bool:
    """Step 2, THE state rule (live gate and replay): whether the newest
    SOLUTION bearing the named fix registers.

    Pending registers; accepted is a re-assertion, absorbed (with a line when
    identity no longer keeps it, ``_note_failed_fix``).
    Superseded registers only while the M5 license holds now (the mint path's
    predicate), and on a live turn only as a ``reproposal``: one withdrawn
    ``license_lost`` stays refused (ruled 2026-10-21). A decision ``carried``
    through a revision hold registers on the license whatever superseded it,
    since the hold withdraws every pending fix. A minted DIAGNOSTIC: M5 fell."""
    if bearer.action_type != InvestigationActionType.SOLUTION:
        _add_system_feedback(metadata, _license_notice(bearer.action_id))
        return False
    if bearer.state == "pending":
        return True
    if bearer.state == "accepted":
        if not carried:
            _note_failed_fix(case, bearer, metadata)
        return False
    if bearer.state == "superseded":
        licensed = _license_holds(case, metadata)
        if licensed and (carried or bearer.superseded_reason == "reproposal"):
            return True
        _refuse_withdrawn(metadata, bearer.action_id, licensed)
        return False
    _refuse_fix_ref(
        case,
        metadata,
        bearer.action_id,
        f"{bearer.action_id}, a {bearer.state} SOLUTION",
    )
    return False


def _registration_justified(case: Case, metadata: dict[str, Any]) -> bool:
    """A registration, now or carried, is justified like the first fix: the
    verdict ``process_response_structured`` computed before the apply."""
    error = metadata.get(FIX_JUSTIFICATION_KEY)
    if not error or justification_skipped(case):
        return True
    logger.warning(
        f"Rejected stage-gate milestone 'solution_accepted' for case "
        f"{case.case_id}: the registration is not justified"
    )
    add_evidence_validation_feedback(
        metadata, not_recorded=["solution_accepted"], errors=[error]
    )
    return False


def _hold_settles(
    case: Case,
    identity: str,
    entries: list,
    ref: Optional[str],
    what: str,
    metadata: dict[str, Any],
) -> bool:
    """On a hold turn, settle now (absorb or refuse) what a hold cannot change;
    False means carry it."""
    if identity in _carried_fixes(case):
        return True  # carried already in this revision: once is enough
    standing = standing_fix_action(case, identity)
    if standing is not None and standing.state == "accepted":
        return True  # a re-assertion: the registered fix, kept by identity
    if any(entry.is_fix and entry.identity == identity for entry in entries):
        return False  # this response proposes it: the replay mints or keeps it
    bearer = _newest_fix_bearer(case, identity)
    if bearer is None:
        _refuse_fix_ref(case, metadata, ref, what)
        return True
    if bearer.state == "accepted":  # not re-sent: settled as on a live turn
        _note_failed_fix(case, bearer, metadata)
        return True
    pending_revision = case.problem_verification.pending_revision
    if (
        bearer.state == "superseded"
        and bearer.superseded_reason == "license_lost"
        # Withdrawn before the hold began, not by the hold itself.
        and (bearer.superseded_in_turn or 0) < pending_revision.held_since_turn
    ):
        _refuse_withdrawn(metadata, bearer.action_id, _license_holds(case, metadata))
        return True
    return False


def _carry_fix(
    case: Case, identity: str, user_message: str, metadata: dict[str, Any]
) -> None:
    """Record the decision on this turn's staged bundle, for the replay."""
    pending_revision = case.problem_verification.pending_revision
    bundle = next(
        (b for b in pending_revision.staged if b.turn == case.current_turn), None
    )
    if bundle is None:
        stage_cause_work(
            case, updates={}, evidence_added=metadata.get("evidence_slots", [])
        )
        bundle = pending_revision.staged[-1]
    bundle.held_fix = identity
    bundle.held_fix_message = (user_message or "")[:10000]
    logger.info(
        "Case %s: carried a fix acceptance through the revision hold at turn %s",
        case.case_id,
        case.current_turn,
    )
    _add_system_feedback(
        metadata,
        "solution_accepted is held until the user confirms the revised problem "
        "statement: the fix it names then registers as carried out at this "
        "turn, under the same gates as any fix (a permanent fix still needs an "
        "established cause). Do not set it again.",
    )


def decide_fix_acceptance(
    case: Case,
    m: Any,
    metadata: dict[str, Any],
    *,
    proposals: Sequence[Any] = (),
    held: Optional[Sequence[Any]] = None,
    user_message: str = "",
) -> Any:
    """THE decision: whether this response's ``solution_accepted`` registers a
    fix, and which SOLUTION action. Returns the action to register now, or None.

    Called once per apply, after the solutions step. ``proposals``: the actions
    this response's ``solutions_to_add`` entries minted or kept, in order.
    ``held``: on a hold turn, those entries, staged rather than minted.

    Step 1 resolves a fix identity; on a live turn step 2 applies the state rule
    to its newest bearer, and a registration is justified like the first
    (``FIX_JUSTIFICATION_KEY``). A hold turn registers nothing: it settles what
    a hold cannot change and carries the rest, justified, on its bundle; the
    replay applies the same state rule (``_register_carried_fix``)."""
    ref = _normalise_id_ref(getattr(m, "accepted_action_ref", None)) or None
    if not getattr(m, "solution_accepted", False):
        if ref:
            _add_system_feedback(
                metadata,
                f"SYSTEM: accepted_action_ref {ref} was ignored: it is read only "
                "with solution_accepted. Set both only when the user reports "
                "carrying out that fix.",
            )
        return None
    entries = _fix_entries(proposals, held)
    resolved = _resolve_fix_identity(
        case, ref, entries, metadata, holding=held is not None
    )
    if resolved is None:
        return None
    identity, what = resolved
    if held is not None:
        if _hold_settles(case, identity, entries, ref, what, metadata):
            return None
        if _registration_justified(case, metadata):
            _carry_fix(case, identity, user_message, metadata)
        return None
    bearer = _newest_fix_bearer(case, identity, proposals)
    if bearer is None:
        _refuse_fix_ref(case, metadata, ref, what)
        return None
    if not _bearer_registers(case, bearer, metadata, carried=False):
        return None
    if not _registration_justified(case, metadata):
        return None
    return bearer


def _register_carried_fix(
    case: Case, carried: Any, metadata: dict[str, Any], proposals: Sequence[Any]
) -> Any:
    """At the replay: the state rule on the newest bearer of the carried fix
    (``carried``, the staged bundle) among SOLUTIONs and what the replay minted
    (``proposals``); the license line when none bears it."""
    bearer = _newest_fix_bearer(case, carried.held_fix, proposals)
    if bearer is None:
        _add_system_feedback(
            metadata,
            _license_notice(
                f"the fix the user reported running at turn {carried.turn}"
            ),
        )
        return None
    return bearer if _bearer_registers(case, bearer, metadata, carried=True) else None


def _apply_stage_gate_side_effects(
    case: Case,
    completed_gates: set[str],
    user_message: str,
    metadata: dict[str, Any],
    *,
    solution_action: Any = None,
    carried: Any = None,
) -> None:
    """Apply side effects when stage-gate milestones are completed.

    When a stage-gate milestone registers, we:
    1. Mark the ProposedAction OF THE GATE'S TYPE as "accepted" —
       ``solution_accepted`` the SOLUTION ``decide_fix_acceptance`` decided
       (``solution_action``; none, no registration), the mitigation-accept
       signal the most recent pending MITIGATION. Type-matched targeting
       (INV-32 hardening): the old any-type pick could stamp "accepted" on a
       never-executed SOLUTION when the user reported a mitigation (or vice
       versa), and an accepted offer is a PERMANENT liveness source for the
       derived ``solution_proposed`` — a misattributed accept re-latches the
       frame the withdrawal machinery exists to dissolve.
       ``mitigation_verified`` alone accepts nothing (its action was accepted
       at the accept step).
    2. For a SOLUTION (#2105): a superseded action is restored first; the stamp
       and the attempt's message are a ``carried`` decision's hold turn's, else
       this turn's; and
       ``progress.solution_accepted`` plus one ``milestones_completed`` entry
       record each registration.
    3. Create an ActionAttempt audit record per accepted action.
    4. Handle mitigation-verified side effects (3B propose-close).

    This replaces the old compliance_detector.py logic — the LLM now
    detects compliance per Framework §4.1.
    """
    for gate, target_type in _GATE_ACTION_TYPE.items():
        if gate not in completed_gates:
            continue
        is_solution = target_type == InvestigationActionType.SOLUTION
        if is_solution:
            target = solution_action
        else:
            target = next(
                (
                    action
                    for action in reversed(case.proposed_actions)
                    if action.state == "pending" and action.action_type == target_type
                ),
                None,
            )
        if target is None:
            continue
        if target.state == "superseded":
            # Restored to pending, then accepted: the user ran the fix named.
            logger.info(
                f"Restored superseded SOLUTION {target.action_id} "
                f"({target.superseded_reason}) for case {case.case_id}: the "
                "user reported carrying it out"
            )
            target.state = "pending"
            target.superseded_reason = None
            target.superseded_in_turn = None
        target.state = "accepted"
        # #987: stamp WHEN the user executed it. `proposed_in_turn` is the OFFER
        # turn; anything reasoning about what happened *after the fix* (M6's
        # persistence precondition) must key on execution, or evidence from the
        # offering turn — recorded before the fix was ever run — reads as a
        # post-fix outcome. A decision carried through a revision hold was
        # executed at its hold turn, not at the replay.
        by_carry = is_solution and carried is not None
        target.accepted_in_turn = carried.turn if by_carry else case.current_turn
        # INV-33: a SOLUTION acceptance moves the case to TREATMENT (verify the
        # executed fix). Retire the DIAGNOSTIC asks the offer shadowed so an
        # earlier pre-fix ask cannot resurface in <pending_action> once the
        # accepted SOLUTION (now state="accepted", no longer "pending") stops
        # covering it — the symmetric twin of the withdrawal-path retirement.
        # SOLUTION-scoped, NOT mitigation: accepting a SOLUTION moves the case to
        # TREATMENT (diagnosis is done, pre-fix asks are stale), but accepting a
        # MITIGATION keeps it in active diagnosis where a shadowed DIAGNOSTIC is
        # plausibly still live — retiring it there could drop a real ask. A
        # genuinely-stale accumulation under a mitigation falls under the general
        # "DIAGNOSTIC asks carry no lifecycle" boundary INV-33 leaves standing.
        if is_solution:
            _retire_shadowed_diagnostic_asks(case, before_turn=target.proposed_in_turn)
            # The persisted projection: True from the first registration on.
            case.progress.solution_accepted = True
            metadata.setdefault("milestones_completed", []).append("solution_accepted")
        # Create audit trail
        attempt = ActionAttempt(
            action_id=target.action_id,
            user_message=(carried.held_fix_message if by_carry else user_message)[
                :10000
            ],
            submitted_at=datetime.now(UTC),
            compliance_detected=True,
            compliance_confidence=1.0,  # LLM-detected = full confidence
        )
        case.action_attempts.append(attempt)
        logger.info(
            f"Stage-gate milestone(s) {completed_gates} set by LLM for case "
            f"{case.case_id} (action {target.action_id}, "
            f"type={target.action_type.value})"
        )

    # 3B: Mitigation-verified side effects (optional propose-close).
    #
    # Redesign R5: there is no post-mitigation path choice. After a
    # mitigation verifies, the case simply continues opportunistically.
    # The pre-mitigation evidence boundary is carried by
    # ``progress.mitigation.completed_at_turn`` (set in the apply-loop where
    # the record is materialized).
    if "mitigation_verified" in completed_gates:
        # rca_infeasible advisory signal: propose closure as stabilized rather
        # than push RCA on a problem the LLM has flagged as intractable.
        # Reference: investigation-lifecycle-logic.md §2.4.
        rca_infeasible = case.problem_verification and getattr(
            case.problem_verification, "rca_infeasible", False
        )
        from faultmaven.core.investigation.terminal_transitions import (
            ClosureReadiness,
            closure_verdict,
            propose_transition,
        )

        # Don't clobber an in-flight disposition handshake, and never on a
        # terminal case (symmetric with _maybe_propose_deferred_close).
        #
        # Nor on a case whose cause is confirmed eliminated: closure readiness
        # reads SUGGEST_RESOLVE there, and every engine opener reads it before
        # choosing its target (#1885 review) — the deferred proposer pivots on
        # it, the LLM path pivots on it, and the confirm-time guard pivots a
        # pending close on it (INV-37). This one stands down instead of
        # pivoting: the resolve offer that case warrants is the resolution
        # backstop's (INV-43, step 4c), or the model's own RESOLVED, which
        # reads READY on the same bar. A "stabilized" close beside a confirmed
        # elimination would be offered on a premise the case contradicts.
        if (
            rca_infeasible
            and not getattr(case, "pending_transition", None)
            and not case.is_terminal
            and not problem_on_hold(case)
            and closure_verdict(case) != ClosureReadiness.SUGGEST_RESOLVE
        ):
            # The generic fallback is fine for the user-facing sentence but is
            # NOT a rationale: derive_closure_reason's guard requires a real one
            # (the label forecloses future work, so it must carry its own
            # justification). Keep them apart so the log cannot claim a reason
            # the guard will refuse.
            declared_rationale = getattr(
                case.problem_verification, "rca_infeasible_rationale", None
            )
            rationale = (
                declared_rationale
                or "root cause analysis is not feasible for this problem"
            )
            closure_message = (
                "The mitigation is verified and stable. "
                f"Since {rationale}, shall we close this case as stabilized?"
            )
            propose_transition(
                case=case,
                to_state="closed",
                summary=closure_message,
            )
            # Unified same-turn proposal flag: keeps step 0 of
            # _check_automatic_transitions from confirming this close with
            # the very message that produced it (#722 same-turn-confirmation
            # guard) — the mitigation-verified message that triggered this
            # proposal often pattern-matches as a bare "yes".
            metadata["transition_proposed_this_turn"] = True
            metadata["override_suggestions"] = _close_confirmation_suggestions(case)
            metadata["rca_infeasible_closure_message"] = closure_message
            # Read the reason propose_transition just STORED rather than
            # re-deriving it here. Mirroring the derivation meant reproducing 2
            # of its 5 branches, which diverges whenever another branch wins —
            # an rca_infeasible declaration alongside a standing working
            # conclusion and a solution record derives `solution_deferred`,
            # which a two-branch mirror cannot express.
            stored_reason = (getattr(case, "pending_transition", None) or {}).get(
                "closure_reason"
            )
            logger.info(
                f"Proposed CLOSED transition for case {case.case_id} "
                f"(rca_infeasible=True; closure_reason derived as "
                f"{stored_reason}, rationale: {rationale})"
            )

    metadata["compliance_detected"] = True
    metadata["progress_made"] = True


def _apply_stage_gate_signals(
    case: Case,
    m: Any,
    user_message: str,
    metadata: dict[str, Any],
    *,
    proposals: Sequence[Any] = (),
    held: Optional[Sequence[Any]] = None,
) -> None:
    """Apply stage-gate compliance signals (Framework §4.1).

    Runs AFTER the solutions step of ``_apply_investigation_updates`` so the
    guards see ProposedActions created THIS turn: the prompt's KB-resolution
    flow mandates SolutionToAdd + ``solution_accepted`` in ONE response, and
    evaluating these signals before the solutions step deterministically
    rejected that bundle (the guard read a pre-solutions snapshot) while
    telling the LLM to re-propose what it had just proposed.

    ``solution_accepted`` is decided once, by ``decide_fix_acceptance``, on a
    live turn and a hold turn alike (``proposals`` / ``held``, see there); on a
    replayed bundle that carried a decision (``CARRIED_FIX_KEY``) the same
    state rule registers the carried fix and nothing is decided again. A
    re-assertion of the registered fix is absorbed silently — LLMs re-assert
    standing booleans, and rejecting the re-emission produced false "was not
    registered" feedback that invited a redundant re-proposal.

    Type-matched guards (INV-32, via ``_GATE_ACTION_TYPE``): a gate registers
    only against an action of ITS type — the old any-type check let
    ``solution_accepted`` pass against a lone pending DIAGNOSTIC and
    manufacture a permanent accepted-ladder latch, and let a mitigation
    report register against a 3C/3D-downgraded DIAGNOSTIC, defeating the
    downgrade contract (the gate prevents REGISTERING, not the action
    happening in the user's environment). Rejections surface via
    ``system_feedback``. When the mitigation ACCEPT signal is rejected,
    same-turn VERIFY is suppressed under the SAME notice — processing it
    would fire the ordering guard's "set BOTH in the same response" advice,
    directly contradicting the rejection it accompanies.
    """
    p = case.progress

    def _has_pending(action_type: InvestigationActionType) -> bool:
        return any(
            a.state == "pending" and a.action_type == action_type
            for a in case.proposed_actions
        )

    # --- solution_accepted: one decision (#2105) -----------------------
    carried = metadata.get(CARRIED_FIX_KEY)
    if carried is not None:
        solution_action = _register_carried_fix(case, carried, metadata, proposals)
    else:
        solution_action = decide_fix_acceptance(
            case,
            m,
            metadata,
            proposals=proposals,
            held=held,
            user_message=user_message,
        )

    # --- mitigation signals (redesign R2: materialize progress.mitigation;
    # the milestone NAMEs still enter milestones_completed for telemetry) ---
    stab_accepted_signal = bool(getattr(m, "mitigation_accepted", False))
    stab_verified_signal = bool(getattr(m, "mitigation_verified", False))

    if stab_accepted_signal and p.mitigation is not None and p.mitigation.accepted:
        # Already registered. Single-mitigation model (INV-24): a SECOND
        # workaround's execution cannot re-enter the gate ladder. Surface
        # that when a pending MITIGATION stands (the rendered
        # MILESTONE_TO_SET affordance is dead for it — a silent drop leaves
        # the LLM re-emitting forever); a bare re-emission with nothing
        # pending is absorbed silently.
        stab_accepted_signal = False
        if _has_pending(InvestigationActionType.MITIGATION):
            _add_system_feedback(
                metadata,
                "SYSTEM: 'mitigation_accepted' was not re-registered — the "
                "case's single mitigation record already registered "
                "acceptance (no mitigation re-entry, INV-24). Treat the "
                "executed workaround as part of the standing mitigation: "
                "record its outcome as evidence, and set "
                "mitigation_verified only when the situation is confirmed "
                "stable.",
            )
    elif stab_accepted_signal and not _has_pending(InvestigationActionType.MITIGATION):
        logger.warning(
            f"Rejected mitigation_accepted for case {case.case_id}: "
            f"no pending MITIGATION ProposedAction exists"
        )
        notice = (
            "SYSTEM: 'mitigation_accepted' was not registered — no "
            "MITIGATION proposal is currently pending. A mitigation "
            "registers only against a standing WORKAROUND proposal grounded "
            "in symptom evidence; file the symptom evidence and re-propose "
            "the workaround (SolutionToAdd, solution_type=WORKAROUND) "
            "before setting the milestone."
        )
        stab_accepted_signal = False
        if stab_verified_signal and (p.mitigation is None or not p.mitigation.accepted):
            # Same-turn verify presupposes the acceptance just rejected —
            # suppress it under the SAME notice rather than letting the
            # ordering guard below add contradictory retry advice.
            stab_verified_signal = False
            notice += (
                " The same applies to 'mitigation_verified' emitted this "
                "turn — it presupposes the acceptance that was not "
                "registered."
            )
        _add_system_feedback(metadata, notice)

    if stab_accepted_signal:
        if p.mitigation is None:
            # proposed_at_turn = the turn the latest workaround
            # ProposedAction was proposed, else current_turn.
            proposed_turn = case.current_turn
            for action in reversed(case.proposed_actions):
                if action.action_type == InvestigationActionType.MITIGATION:
                    proposed_turn = action.proposed_in_turn
                    break
            p.mitigation = MitigationRecord(proposed_at_turn=proposed_turn)
        if not p.mitigation.accepted:
            p.mitigation.accepted = True
            metadata["milestones_completed"].append("mitigation_accepted")

    if stab_verified_signal:
        # Ordering guard: verification presupposes acceptance. If accept
        # wasn't signalled (now or earlier), reject and surface via
        # system_feedback for retry, instead of crashing the turn on the
        # record validator.
        if p.mitigation is None or not p.mitigation.accepted:
            logger.warning(
                f"Rejected mitigation 'mitigation_verified' for case "
                f"{case.case_id}: prerequisite 'mitigation_accepted' is "
                f"not set (state-machine ordering)."
            )
            _add_system_feedback(
                metadata,
                "MILESTONE ORDER ERROR: You set mitigation_verified=True "
                "without first setting mitigation_accepted=True. "
                "Verification presupposes acceptance — set "
                "mitigation_accepted=True (based on the user's confirmation "
                "signals) before mitigation_verified=True. Set BOTH "
                "milestones in the same response if both happened this "
                "turn, OR set mitigation_accepted=True first and verify on "
                "a follow-up turn after the user confirms.",
            )
            metadata.setdefault("validation_repairs", []).append(
                "Rejected mitigation_verified "
                "(prerequisite mitigation_accepted not set)"
            )
        elif not p.mitigation.verified:
            p.mitigation.verified = True
            p.mitigation.completed_at_turn = case.current_turn
            metadata["milestones_completed"].append("mitigation_verified")

    # --- side effects (Framework §4.1): mark the type-matched
    # ProposedAction accepted + ActionAttempt audit ----------------------
    stage_gate_completed = {
        "mitigation_accepted",
        "mitigation_verified",
    } & set(metadata["milestones_completed"])
    if solution_action is not None:
        stage_gate_completed.add("solution_accepted")
    if stage_gate_completed:
        _apply_stage_gate_side_effects(
            case,
            stage_gate_completed,
            user_message,
            metadata,
            solution_action=solution_action,
            carried=carried,
        )


def _close_confirmation_suggestions(case) -> list:
    """Generate DECIDE follow-up suggestions for close (abandon) confirmation.

    Mirrors the INQUIRY and RESOLVED confirmation patterns: one positive
    (confirm close) and one mild negative (continue investigating).

    Note: the confirmation prompt is purely about the irreversibility of
    closing. The summary is a downstream Dashboard artifact; mentioning it
    here would either promise unconditionally (sometimes false, when the
    substance gate skips) or muddy the decision the user is being asked to
    make. The body text deliberately stays silent about the report.

    Both intents name the standing offer, ``case.pending_transition``, by its
    key (#1812), so this is called with the CLOSED proposal already standing.
    """
    # Local: ``transition_consent`` imports this module.
    from .transition_consent import offer_intent_fields, terminal_offer_key

    offer = offer_intent_fields(
        terminal_offer_key(getattr(case, "pending_transition", None)),
        case=case,
        gate="terminal",
    )
    return [
        {
            "label": "Yes, close this case",
            "action_type": "DECIDE",
            "payload": "Yes, close this case without resolution.",
            "body": "Confirm closing the case. Closing is irreversible — the case becomes read-only.",
            "intent": {"type": "confirmation", "confirmation_value": True, **offer},
        },
        {
            "label": "Not yet, continue investigating",
            "action_type": "DECIDE",
            "payload": "Not yet — I'd like to continue investigating.",
            "body": "Keep the investigation open and continue working toward a solution.",
            "intent": {"type": "confirmation", "confirmation_value": False, **offer},
        },
    ]


#: The two closes a user can decline from the engine and still want later, by
#: the side ``transitions.declined_close_reask`` names: (label, payload, body).
_DECLINED_CLOSE_CARDS = {
    "false_alarm": (
        "Close as false alarm",
        "Close this case as a false alarm.",
        "Close the case on the finding that the reported problem was not "
        "present. You confirm on the next step; closing is irreversible.",
    ),
    "deferred": (
        "Close with the solution documented",
        "Close this case with the solution documented for my team to apply.",
        "Close the case with the root cause and the fix documented for the "
        "out-of-band change. You confirm on the next step; closing is "
        "irreversible.",
    ),
}


def declined_close_card(side: str) -> dict:
    """The one card that keeps a declined close one step away (#1889).

    A close the user declined is not asked again, by the engine or the model,
    until its premise moves. A user who later does want it still needs a
    deterministic path on every client: the status menu is that path on the
    dashboard and the extension, but Slack renders only the server's
    suggestions. So the turn that refuses a re-proposal (and the bare reply to
    the decline itself) carries this card: the same ``status_transition``
    intent the status menu sends, executed by ``_close_on_explicit_intent``,
    with the usual confirm pair after it.

    It names a STATE, not an offer: no ``pending_transition`` stands behind it
    and it carries no ``proposal_id`` (``offer_intent_fields`` is not called;
    the #1812 census of confirmation builders does not include it). It is
    APPENDED to the turn's follow-ups, never substituted for them: a refusal
    that replaced the model's suggestions with a single close button would be
    the re-ask in disguise.
    """
    label, payload, body = _DECLINED_CLOSE_CARDS[side]
    return {
        "label": label,
        "action_type": "DECIDE",
        "payload": payload,
        "body": body,
        "intent": {"type": "status_transition", "to_state": "closed"},
    }


#: The "Mark it resolved" chip (#1895): (label, payload, body).
DECLINED_RESOLVE_CARD_LABEL = "Mark it resolved"
_DECLINED_RESOLVE_CARD_PAYLOAD = "Mark this case resolved."
_DECLINED_RESOLVE_CARD_BODY = (
    "Re-open the resolution the agent proposed. You confirm on the next step."
)


def declined_resolve_card(case) -> Optional[dict]:
    """The chip that keeps a declined resolution one step away, or None when
    no resolve decline stands (#1895).

    RESOLVED is earned, never requested: it is not on the status menu, and a
    user's request never becomes the confirmation row that earns it. So once
    the user has declined the resolution, neither the engine nor the model
    offers it again until a NEW confirmation that the fix held is recorded. A
    user who changes their mind meanwhile needs a deterministic way back on
    every client, Slack included (which renders only the server's
    suggestions). This chip is that way: it re-presents the offer the engine
    made and the user declined, and nothing else.

    Its intent is ``{"type": "status_transition", "to_state": "resolved",
    "proposal_id": <reopen key>}``, the published ``QueryIntent`` shape, which
    every client forwards verbatim. ``proposal_id`` is the reopen key
    (``terminal_transitions.resolve_reopen_key``), a digest of the declined
    entry the decline stands on. The two places that refuse a RESOLVED
    ``status_transition`` (the service boundary and the engine's guard) admit
    it only while that same entry still covers the case
    (``resolve_reopen_admitted``); the engine then PROPOSES resolved with the
    usual confirmation pair, and the user confirms (INV-03). A stale chip (a
    new confirmation has since moved the state) is refused with the 422, and
    by then the engine is offering the resolution itself.

    It names no standing offer (no ``pending_transition`` stands behind it, so
    ``offer_intent_fields`` is not called and the #1812 census of confirmation
    builders does not include it). ``turn_completion`` APPENDS it to the
    follow-ups of EVERY turn this returns a card for, never substituting it
    for them and never twice: the model points a user who changes their mind
    at it and never proposes on a request, so it must be on screen whatever
    the model did. Not built while any offer stands (the chip
    re-opens a declined offer, it does not compete with a live one) or while
    the problem statement is on hold (no transition is proposed then).
    """
    if getattr(case, "pending_transition", None) or problem_on_hold(case):
        return None
    from faultmaven.core.investigation.terminal_transitions import (
        declined_resolve_entry,
        resolve_reopen_key,
    )

    entry = declined_resolve_entry(case)
    if entry is None:
        return None
    return {
        "label": DECLINED_RESOLVE_CARD_LABEL,
        "action_type": "DECIDE",
        "payload": _DECLINED_RESOLVE_CARD_PAYLOAD,
        "body": _DECLINED_RESOLVE_CARD_BODY,
        "intent": {
            "type": "status_transition",
            "to_state": "resolved",
            "proposal_id": resolve_reopen_key(entry),
        },
    }
