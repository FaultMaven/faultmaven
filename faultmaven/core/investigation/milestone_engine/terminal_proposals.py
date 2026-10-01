import logging
from datetime import (
    UTC,
    datetime,
)

from faultmaven.core.investigation.lifecycle_metrics import (
    engine_proposed_resolution_total,
    evidence_need_status_changed_total,
)
from faultmaven.modules.case.contracts import (
    TERMINAL_HYPOTHESIS_STATES,
    Case,
    CaseState,
    NeedPurpose,
    NeedState,
    SolutionFeasible,
)

from .response_synthesis import (
    _DISPOSITION_GATE_ANSWERED_KEY,
    _ENGINE_DISPOSITION_WITHDRAWN_KEY,
)
from .stage_gates import (
    _close_confirmation_suggestions,
    _solution_cause_validated,
)
from .terminal_replies import _resolution_confirmation_suggestions

logger = logging.getLogger(__name__)


def _maybe_propose_deferred_close(case: "Case", metadata: dict) -> None:
    """Deferred-implementation disposition (redesign §3.1 row 3 / §6 Q2).

    When the cause + fix are known but the fix cannot be applied or verified
    this session (``solution_feasible == DEFERRED`` — e.g. it needs an
    out-of-band change request, a maintenance window, or another team), the
    case should reach a DISPOSITION with the solution documented rather than
    be held open waiting indefinitely (the failure mode observed in validation
    run 2). Which disposition depends on the case; see the pivot below.

    The engine proposes the disposition DETERMINISTICALLY: the LLM does not
    reliably drive to a disposition on its own when implementation is deferred.
    The user still confirms via the standard disposition handshake, and the
    documented root cause + solution are preserved either way
    (``closure_reason=solution_deferred`` on the close branch).

    Which disposition is proposed follows ``assess_closure_readiness``, the same
    resolve-preservation pivot the LLM-proposal path and the confirm-time INV-37
    guard apply. Its trigger is a QUALIFYING COUNTERFACTUAL CONFIRMATION —
    ``_has_causal_absence``, a gone=>gone row, the same bar
    ``assess_resolution_readiness`` uses for READY — NOT merely "a root cause
    and a solution are on record" (that phrasing survives in
    ``assess_closure_readiness``'s own summary line and is stale there too).
    Deferred implementation is a statement about WHEN the remaining work lands,
    not about whether the cause was found, so it must not cost a confirmed case
    its attribution.

    The proposal's rationale is published on
    ``metadata["deferred_solution_gate_message"]`` for the response composer to
    render below the LLM's reply — an engine-proposed disposition has to say why
    it is on the table.
    """
    p = case.progress
    if p.solution_feasible != SolutionFeasible.DEFERRED:
        return
    # Only meaningful once a fix is actually on record.
    if not (p.solution_proposed or case.solutions):
        return
    # INV-32: the closure message asserts "the root cause and fix are
    # documented" — that claim needs the SAME established-cause license the
    # fix offer needed (the M5 wrapper, verbatim — three gates, ONE
    # predicate). Solution records are monotone (never withdrawn), so
    # without this gate a case whose cause fell THIS turn (offer just
    # withdrawn license_lost, feedback telling the LLM to re-ground) would be
    # proposed for closure citing the disconfirmed cause in the same breath.
    if not _solution_cause_validated(case):
        return
    # Don't clobber an in-flight handshake.
    if getattr(case, "pending_transition", None):
        return
    # Defense in depth, and stricter than the `is_terminal` check it replaces:
    # the proposal target is now STATE-DEPENDENT. "closed" was a legal edge
    # from any state, so hardcoding it made this guard free; "resolved" is NOT
    # a legal edge from INQUIRY (LEGAL_TRANSITIONS — resolution requires
    # investigation work). A proposal that cannot execute leaves
    # `pending_transition` standing, so every later confirm turn would fail the
    # same way. Only the INVESTIGATING pipeline calls this today; the guard
    # keeps that assumption true if another caller is ever added.
    if case.state != CaseState.INVESTIGATING:
        return

    from faultmaven.core.investigation.terminal_transitions import (
        assess_closure_readiness,
        deferred_disposition_signature,
        propose_transition,
    )

    # Resolve preservation (INV-37), the SAME choice the other two disposition
    # paths already make: the LLM-proposal path pivots CLOSED->RESOLVED on
    # SUGGEST_RESOLVE, and the confirm-time guard pivots a pending CLOSE the
    # same way. This proposer used to call propose_transition("closed")
    # directly, so it was the one path that could offer "close this case
    # without resolution" on a case its OWN eligibility scored resolvable.
    # Deferred implementation says WHEN the fix lands, not that the cause went
    # unfound, so it must not cost an attributable case its resolution.
    #
    # Scope of the evidence, stated precisely: case_fa29e0023b85 ended with
    # disposition_eligibility {resolved: ready, closed: suggests_alternative},
    # but that column is recomputed at every save, so it describes the FINAL
    # turn — after the user reported applying the fix — not the five earlier
    # turns on which this function offered the close pair. The defect this
    # fixes is therefore path INCONSISTENCY (one proposer disagreeing with the
    # other two), not a reconstructed history of that case. What kept those
    # five turns going is the re-proposal loop, which is a separate concern.
    # The engine's own offer was withdrawn earlier this turn (a decline, a
    # question, a deflection, a contradicting status pick). Re-proposing it
    # now would take the affordances back on the very turn the user acted on
    # them. Turn-scoped: unlike the refusal record this does not survive the
    # turn, so an offer the user only asked about returns on the next one.
    if metadata.get(_ENGINE_DISPOSITION_WITHDRAWN_KEY):
        return
    # The turn began inside a disposition handshake. Same reasoning as the
    # resolution backstop's copy of this guard, and the hazard is if anything
    # sharper here: this proposer can substitute a DIFFERENT target (its
    # SUGGEST_RESOLVE pivot offers RESOLVED) into a channel the user was
    # mid-answer on. ``_note_engine_disposition_withdrawn`` above does not
    # cover it — that one fires only when the withdrawn offer carried an
    # engine signature, so an LLM-opened close the user merely asked about
    # leaves it unset.
    if metadata.get(_DISPOSITION_GATE_ANSWERED_KEY):
        return

    closure = assess_closure_readiness(case)
    # A decline POSTPONES the offer until the case changes underneath it.
    # Re-proposing every turn regardless is what produced five identical
    # offers against five explicit declines (fm#1122): the decline clears
    # `pending_transition`, which is the only state the guards above read, so
    # nothing carried the refusal forward. Keyed on the JUSTIFYING state, not
    # on a decline count: counting declines and giving up would be the engine
    # steering toward abandonment (D4 soft-collapse), and it would also strand
    # a case whose situation later genuinely warrants the offer again.
    signature = deferred_disposition_signature(case, closure.verdict)
    if signature in p.deferred_disposition_declined_signatures:
        return

    if closure.verdict == closure.SUGGEST_RESOLVE:
        to_state = "resolved"
        # Purpose-written, NOT `closure.message`. That text is a pivot-FROM-a-
        # close ("Closing would record it as unresolved and discard the
        # resolution"), which is coherent on the LLM path — where a close was
        # actually requested — and incoherent here, where the engine proposed
        # this turn's disposition unprompted. Reusing it would presuppose a
        # close the user never asked for and would never state the deferred-
        # implementation reason: the same prose/affordance incoherence this
        # function is being fixed to stop producing.
        gate_message = (
            "The fix on this case is confirmed to have eliminated the root "
            "cause — the cause was removed and the problem went with it — so "
            "it qualifies for **resolved**. The implementation work you "
            "flagged as out-of-band (a change request, maintenance window, or "
            "another team) is follow-up: it stays documented on the resolved "
            "case and does not need the incident held open. Shall I mark this "
            "case resolved?"
        )
    else:
        to_state = "closed"
        gate_message = (
            "The root cause and fix are documented, but the fix can't be applied "
            "or verified during this session — it needs out-of-band implementation "
            "(a change request, maintenance window, or another team). Shall I close "
            "this case with the solution documented for your team to apply?"
        )

    propose_transition(case=case, to_state=to_state, summary=gate_message)
    # Provenance AND payload in one key: this proposer is the only writer of
    # `justifying_signature`, so its presence identifies the offer and its
    # value is what a decline is recorded against. A separate `proposed_by`
    # tag was redundant — it duplicated a check the missing signature already
    # covers, and no test could tell the two guards apart.
    case.pending_transition["justifying_signature"] = signature
    # Unified same-turn proposal flag: keeps step 0 of
    # _check_automatic_transitions from confirming this disposition with the
    # very message that produced it (#722 same-turn-confirmation guard).
    metadata["transition_proposed_this_turn"] = True
    # Built only now: each card names the offer ``propose_transition`` just
    # stamped (#1812), and none stood above it.
    metadata["override_suggestions"] = (
        _resolution_confirmation_suggestions(case)
        if to_state == "resolved"
        else _close_confirmation_suggestions(case)
    )
    # Rendered by the response composer, the same way the rca_infeasible
    # sibling's message is. Before this the key was written and read NOWHERE,
    # so the engine proposed a disposition the user saw only as a bare
    # confirm/decline pair with no stated reason.
    metadata["deferred_solution_gate_message"] = gate_message
    logger.info(
        f"Proposed {to_state.upper()} transition for case {case.case_id} "
        f"(solution_feasible=DEFERRED; closure_verdict={closure.verdict}; "
        f"the documented root cause and solution are preserved either way)"
    )


def _maybe_propose_confirmed_resolution(case: "Case", metadata: dict) -> None:
    """Open the RESOLVED handshake on a case whose cause is CONFIRMED gone
    (INV-43).

    The counterpart of Gate 1's engine-owned presentation, on the other end of
    the lifecycle. Gate 1 stopped depending on prompt compliance in #1607: the
    engine composes the standing problem statement and its confirm/refine pair
    on EVERY Gate-1-pending turn, so the affordance is on screen by
    construction. The RESOLVED handshake had no such backstop. Its three
    openers were the LLM's ``proposed_transition``, the user asking, and
    ``_maybe_propose_deferred_close`` — and that last one fires only on
    ``solution_feasible == DEFERRED``, which is the case where the fix has
    NOT been applied. The ordinary shape — fix applied in session, user
    confirms it worked, the model records the ``causal_absence_evidence`` row
    and omits the transition the COMPLETION prompt tells it to co-emit —
    reached no opener at all. ``assess_resolution_readiness`` read READY,
    ``disposition_eligibility.resolved`` persisted ``ready``, and the turn
    shipped whatever follow-ups the model happened to emit. Left to stall, the
    case then fell through to the mid-investigation correctives and was asked
    to restate the problem it had already confirmed resolved.

    So this proposes; it never executes. INV-03 is untouched — the user
    confirms on a later turn through the same handshake every other
    disposition uses. #722's "a proposal is never confirmed by the message that
    produced it" holds here by ORDERING rather than by a flag: the call site is
    past ``_check_automatic_transitions``, so this turn's confirm check is
    already behind us when the offer is made.

    The trigger is the resolution bar itself, ``assess_resolution_readiness``
    READY — a QUALIFYING ``causal_absence_evidence`` row, gone=>gone. Not a
    second, looser reading of "looks finished": a stabilized case carries
    ``symptom_absence`` and is correctly left alone, and the engine's own M6
    failed-fix rows are excluded by ``resolution_confirmation_rows``.

    Re-nag discipline is fm#1122's, shared with the deferred proposer rather
    than reinvented: one ``deferred_disposition_signature``, one
    ``deferred_disposition_declined_signatures`` list. A user who declines
    "mark this resolved" is not asked again until a premise moves, and because
    both proposers key the same signature, declining one cannot leave the
    other free to re-ask the settled question on the next turn.
    """
    # This is what makes it a BACKSTOP: the call site runs it last, after every
    # other opener, so anything they proposed is standing here and this returns.
    # On a case that is both DEFERRED and confirmed, the deferred proposer's own
    # SUGGEST_RESOLVE pivot has already offered RESOLVED with the more specific
    # out-of-band rationale, and overwriting it with the generic message would
    # be strictly worse.
    if getattr(case, "pending_transition", None):
        return
    if case.state != CaseState.INVESTIGATING:
        return
    # The engine's own offer was withdrawn earlier this turn (a decline, a
    # question, a deflection). Re-proposing now takes the affordances back on
    # the very turn the user acted on them — the same turn-scoped guard the
    # deferred proposer carries, for the same reason.
    if metadata.get(_ENGINE_DISPOSITION_WITHDRAWN_KEY):
        return
    # The turn began inside a disposition handshake. Whatever became of that
    # offer — withdrawn on a question, declined, contradicted — the channel was
    # the user's this turn, and opening a different target into it is how a
    # "yes" meant for the offer they were reading lands on the one the engine
    # substituted underneath it.
    if metadata.get(_DISPOSITION_GATE_ANSWERED_KEY):
        return

    from faultmaven.core.investigation.terminal_transitions import (
        ResolutionReadiness,
        assess_resolution_readiness,
        cause_identification_leg,
        closure_verdict,
        deferred_disposition_signature,
        propose_transition,
    )

    if not getattr(case, "progress", None):
        return

    readiness = assess_resolution_readiness(case)
    if readiness.verdict != ResolutionReadiness.READY:
        return

    # Same signature space as the deferred proposer, deliberately: both offer
    # RESOLVED off the same justifying state, so one refusal must silence both.
    # The verdict is SUGGEST_RESOLVE on every case that clears the READY bar
    # (both gate on ``_has_causal_absence``), so this is the same string that
    # proposer would compute — computed rather than hardcoded so the two cannot
    # drift if either gate is re-scoped, and taken through ``closure_verdict``
    # so the user-facing message this call would otherwise build and throw away
    # is not built at all.
    signature = deferred_disposition_signature(case, closure_verdict(case))
    if signature in case.progress.deferred_disposition_declined_signatures:
        return

    # The "still open until you confirm" clause is load-bearing, not padding.
    # Composing this prose sets ``gate_prose_appended``, which SUPPRESSES the
    # INV-40 over-claim notice — and the contract for that suppression is that
    # the gate notice "already frames the real (not-yet-terminal) state". This
    # branch fires in the one state where a model is most likely to have
    # narrated "Case resolved." in the same turn: it thinks the fix worked, and
    # omitting the structured field is exactly the failure this backstop
    # catches. Implying the case is still open (an offer phrased as a question)
    # is what its two siblings do, and it is too weak HERE, where the sentence
    # directly above it may be a false completion claim this prose has to
    # contradict rather than merely sit beside.
    # READY is a confirmed ELIMINATION, which is not the same as a confirmed
    # CAUSE: ``assess_resolution_readiness`` deliberately does not require a
    # root-cause record, so an out-of-band fix reported verbally resolves on
    # the absence row alone (INV-41 names this the ``none`` leg). Saying "the
    # root cause is confirmed eliminated ... records the attribution" on such
    # a case names something the case does not hold — and because composing
    # this prose suppresses the INV-40 over-claim notice, nothing downstream
    # would correct it. This is the one opener with no model involvement, so
    # the claim would be entirely engine-authored.
    if cause_identification_leg(case) is not None:
        confirmed = (
            "The root cause is confirmed eliminated — it was removed and the "
            "problem went with it — which is the bar for **resolved**. The "
            "case is still open until you confirm: marking it resolved records "
            "the attribution and writes up the resolution summary."
        )
    else:
        confirmed = (
            "You've confirmed the problem is gone after the fix, which is the "
            "bar for **resolved**. The case is still open until you confirm: "
            "marking it resolved writes up what happened. No root cause is on "
            "record, so the write-up will say so — if you can name what caused "
            "it, tell me and I'll record that first."
        )
    gate_message = f"{confirmed} Shall I mark this case resolved?"
    propose_transition(case=case, to_state="resolved", summary=gate_message)
    case.pending_transition["justifying_signature"] = signature
    # NOT the #722 guard here (see the docstring — ordering covers that): this
    # is what the turn's ``transition_compliance`` line reads as
    # ``proposed_transition_emitted``. Its two engine-proposing siblings set it
    # for the same reason, and the neighbouring ``llm_proposed_to_status``
    # (read off the response object) is what separates an engine offer from a
    # model one.
    metadata["transition_proposed_this_turn"] = True
    metadata["override_suggestions"] = _resolution_confirmation_suggestions(case)
    # Rendered by the response composer below the model's reply. An
    # engine-proposed disposition has to say why it is on the table — the
    # deferred sibling shipped a bare confirm/decline pair for exactly as long
    # as this key had no reader (fm#1122).
    metadata["resolution_ready_gate_message"] = gate_message
    # The turn's ``transition_compliance`` line reports the verdict only when
    # something proposed; recording it here keeps an engine-opened handshake
    # as self-explaining in the logs as an LLM-opened one.
    metadata["resolution_readiness_verdict"] = readiness.verdict
    metadata["resolution_readiness_missing"] = readiness.missing
    engine_proposed_resolution_total.inc()
    logger.info(
        f"Proposed RESOLVED transition for case {case.case_id} "
        f"(engine backstop: resolution readiness is READY and no other "
        f"opener proposed it this turn; pending user confirmation)"
    )


def _supersede_needs_on_terminal_hypothesis(
    case: "Case", terminal_hyp_id: str, current_turn: int
) -> int:
    """Deterministic engine rule: when a hypothesis reaches a TERMINAL state
    (``REFUTED`` or ``RETIRED``), remove its ID from every need's
    ``motivating_hypothesis_ids``. If the list becomes empty AND the need is
    causal-purpose AND not FULFILLED, mark the need SUPERSEDED.

    Returns the count of needs whose state was flipped to SUPERSEDED.

    Per evidence-needs-design.md §7.4:

    - Needs motivated by multiple hypotheses survive a partial sweep;
      supersession fires only when all motivators are gone.
    - ``symptom_verification`` needs have empty motivating lists by
      design (motivated by the problem statement) — they are exempt
      from this rule.
    - FULFILLED needs are never auto-superseded — they remain as the
      audit trail of what was collected.

    **Both** terminal states are swept, not retirement alone: ``REFUTED`` and
    ``RETIRED`` are equally immutable (``_apply_hypothesis_updates`` refuses to
    revive either) and equally out of the differential
    (``verification_status._residual_candidates``), so a discriminator motivated
    solely by a refuted cause discriminates nothing. Sweeping only retirement
    left those needs PENDING for the life of the case — the staleness leak this
    rule exists to prevent, since nothing else GCs an LLM-authored causal need.

    Wired as an end-of-turn sweep in ``_process_turn_impl`` over **every**
    terminal hypothesis, not a newly-terminal diff. This function is idempotent
    — the first pass removes ``terminal_hyp_id`` from every motivating list, so
    later passes hit the ``continue`` below and change nothing — so re-sweeping
    costs nothing in the steady state and needs no pre-turn snapshot. It also
    self-heals a need that is already carrying a terminal id (one that went
    terminal before this rule existed, or one sitting in the list beside a
    still-active motivator), which a diff could never reach.

    A single integration point covers every terminal write site
    (``hypothesis_manager.py`` low-confidence + anchoring-prevention +
    ``refute_hypothesis``, ``progress_monitor.py`` INCONCLUSIVE → RETIRED, and
    LLM-emitted refutation/retirement here) without threading ``case`` through
    those APIs.

    Persistence rides on the next ``repo.save(case)`` (no scoped repo
    method — needs live on the Case aggregate per Phase 1 §1.5).
    """
    superseded_count = 0
    for need in case.evidence_needs:
        if terminal_hyp_id not in need.motivating_hypothesis_ids:
            continue
        new_motivators = [
            hyp_id
            for hyp_id in need.motivating_hypothesis_ids
            if hyp_id != terminal_hyp_id
        ]
        prior_status = need.state
        if (
            not new_motivators
            and need.purpose == NeedPurpose.CAUSAL_VERIFICATION
            and need.state != NeedState.FULFILLED
        ):
            # Pydantic-frozen behavior: EvidenceNeed isn't frozen, so
            # in-place mutation is allowed and re-validated at save time.
            # The Case domain model's save path runs full validation.
            new_status = NeedState.SUPERSEDED
            new_reason = "all motivating hypotheses are terminal"
            superseded_count += 1
        else:
            new_status = need.state
            new_reason = need.superseded_reason

        # Apply the update. Use object.__setattr__-free path since
        # EvidenceNeed isn't frozen — direct attribute assignment is
        # allowed and re-runs field validators (not the model validator
        # though; the cross-field invariants are checked at save time
        # via Case.model_validate in the repository).
        need.motivating_hypothesis_ids = new_motivators
        need.state = new_status
        need.superseded_reason = new_reason
        need.revoke_obtainability_if_terminal()
        need.updated_at = datetime.now(UTC)

        if new_status == NeedState.SUPERSEDED and prior_status != NeedState.SUPERSEDED:
            try:
                from faultmaven.core.investigation.lifecycle_metrics import (
                    evidence_need_status_changed_total,
                )

                evidence_need_status_changed_total.labels(
                    from_state=prior_status.value, to_state=new_status.value
                ).inc()
            except Exception:
                # Metrics are best-effort; never block lifecycle on them.
                pass

    if superseded_count:
        logger.info(
            f"Superseded {superseded_count} causal-verification need(s) on "
            f"case {case.case_id} after hypothesis {terminal_hyp_id} became "
            f"terminal."
        )
    return superseded_count


def _sweep_needs_for_terminal_hypotheses(case: "Case") -> int:
    """Run the §7.4 supersession rule against every terminal hypothesis.

    The end-of-turn integration point, extracted so tests pin the sweep the
    engine actually runs rather than a replica of it.

    Sweeping the whole terminal set — rather than only the hypotheses that
    turned terminal this turn — is what lets a need already carrying a terminal
    motivator heal itself, and ``_supersede_needs_on_terminal_hypothesis`` is
    idempotent, so repeating the sweep every turn costs nothing once the
    motivating lists are clean.

    Returns the number of needs flipped to SUPERSEDED — 0 in the steady state.
    """
    return sum(
        _supersede_needs_on_terminal_hypothesis(case, h_id, case.current_turn)
        for h_id, h in case.hypotheses.items()
        if h.state in TERMINAL_HYPOTHESIS_STATES
    )
