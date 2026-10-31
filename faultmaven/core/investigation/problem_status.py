"""The single writer of ``InvestigationProgress.problem_status``.

``problem_status`` says where the confirmed problem statement stands against the
evidence. ``symptom_verified`` is its derived boolean view, so it cannot drift
from it. Every transition goes through a function here, which is what lets a
reader trust the value: four places used to assign the old boolean directly
(the milestone loop, the KB-resolution collapse, the evidence-citation revert
and the retraction), each with its own idea of what a change meant.
``tests/unit/core/investigation/test_problem_status.py`` scans the package and
fails on any other assignment.

``cause_work_accepted`` is the one predicate every cause-work gate reads. Cause
work — hypotheses, causal chains, a root-cause conclusion, its method — is
accepted only once the problem is verified. The gates read the status the turn
ENDS with: the milestone step and the evidence-citation revert both run before
them, so one turn that verifies the symptom and brings its cause can still form
hypotheses, ground a chain and identify the cause together. That is the
opportunistic flow, and nothing here may defer it.

Two more outcomes follow verification besides "verified":

* **The statement is inaccurate** (the problem is real, the wording is not):
  ``propose_revision`` opens a revision offer on the case's offer record
  (``core/investigation/offers.py``) and holds the revised statement there for
  the user's re-confirmation (REVISION_PENDING). Cause work arriving meanwhile
  is STAGED on the open revision, not refused — ``cause_work_staged`` — and
  replayed when the user confirms, so the confirmation turn can still verify,
  hypothesize and identify. ``commit_revision`` writes the statement into
  every store that holds it; ``decline_revision`` / ``cancel_revision`` return
  to where the case was. These are the only openers and answerers of the
  revision offer, so this module stays the one writer of ``problem_status``.
* **The problem never existed** (a false alarm): ``invalidate_problem``
  (INVALIDATED). The case can be closed, never resolved; nothing progresses
  until new evidence shows a problem (a revision) or the user disputes the
  finding (``withdraw_invalidation``). A declined close is an answer on the
  offer record whose premise is the finding: it binds while the finding
  stands (``false_alarm_close_declined_at``).

The ``*_refusal`` functions are the guards: each returns why a proposal cannot
be accepted, or None. The transitions assume their guard passed.
"""

import logging
from datetime import UTC, datetime

from faultmaven.core.investigation.causal_graph.similarity import (
    hypothesis_statements_duplicate,
)
from faultmaven.core.investigation.offers import (
    answer_offer,
    open_offer,
    open_revision,
    restage_revision,
    revision_declined,
    standing_false_alarm_decline,
)
from faultmaven.modules.case.contracts import (
    AnswerKind,
    AnswerVia,
    Case,
    ClosureReason,
    EvidenceCategory,
    NeedPurpose,
    NeedState,
    NodeType,
    Offer,
    OfferBasis,
    OfferKind,
    PendingRevision,
    ProblemInvalidation,
    ProblemStatementRecord,
    ProblemStatus,
    ProblemVerification,
    StagedCauseWork,
    StatementRecordKind,
)

logger = logging.getLogger(__name__)

#: A revised statement anchors the causal graph's PROBLEM node, which holds at
#: most this many characters (``causal_graph.ingestion.seed_problem_node``).
MAX_STATEMENT_CHARS = 500


def cause_work_accepted(case: Case) -> bool:
    """Whether this case accepts cause work: hypotheses, chains, a root-cause
    conclusion. True once the problem is verified."""
    return case.progress.problem_status == ProblemStatus.VERIFIED


def cause_work_staged(case: Case) -> bool:
    """Whether cause work arriving now is staged on the pending revision rather
    than applied or refused: the problem is real, its statement awaits the
    user's re-confirmation."""
    return case.progress.problem_status == ProblemStatus.REVISION_PENDING


def problem_on_hold(case: Case) -> bool:
    """Whether the case waits on the user or on new evidence rather than
    investigating: a revision awaiting re-confirmation, or a false alarm.
    Housekeeping, repair patterns and the stall counter pause while it holds."""
    return case.progress.problem_status in (
        ProblemStatus.REVISION_PENDING,
        ProblemStatus.INVALIDATED,
    )


def verify_problem(case: Case, *, via: str) -> bool:
    """Record that evidence shows the stated symptom.

    Only an UNVERIFIED problem verifies this way: a pending revision is
    verified by the user's re-confirmation, and a false alarm by a revision
    naming the new problem. ``via`` names the writer for the log line. Returns
    whether the status changed, so a caller records the milestone on the edge.
    """
    if case.progress.problem_status != ProblemStatus.UNVERIFIED:
        return False
    return _move(case, ProblemStatus.VERIFIED, via=via)


def unverify_problem(case: Case, *, via: str) -> bool:
    """Withdraw the verification: the symptom claim was retracted, or the
    evidence it cited does not support it. Hypotheses already standing are
    left as they are; new cause work is refused until it is verified again.
    """
    if case.progress.problem_status != ProblemStatus.VERIFIED:
        return False
    return _move(case, ProblemStatus.UNVERIFIED, via=via)


def current_statement(case: Case) -> str:
    """The problem statement in force."""
    pv = case.problem_verification
    return (pv.symptom_statement if pv else "") or case.description or ""


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def _acted_on_bar(case: Case) -> str | None:
    """The shared bar for revising or invalidating: once a cause is identified
    and uncontested, or a mitigation or fix was VERIFIED against the statement,
    the statement is history rather than a working frame. An action merely
    accepted does not bar — a fix that failed is exactly when a mis-statement
    surfaces."""
    p = case.progress
    if p.cause_state.value == "identified" and not p.cause_identification_contested:
        return "the root cause is already identified against the current statement"
    if p.mitigation is not None and p.mitigation.verified:
        return "a mitigation was verified against the current statement"
    if p.solution_verified:
        return "a fix was verified against the current statement"
    return None


def _cited(case: Case, evidence_ids: list[str], category: EvidenceCategory) -> bool:
    wanted = set(evidence_ids)
    return any(
        e.evidence_id in wanted and e.category == category for e in case.evidence
    )


def revision_refusal(
    case: Case, text: str, evidence_ids: list[str], basis: str, wording_key: str
) -> str | None:
    """Why a revised statement cannot be proposed now, or None. Any status
    admits one: an unverified or verified problem may turn out mis-stated, a
    false alarm may give way to a different problem, and a pending revision may
    be replaced by a better one."""
    text = (text or "").strip()
    if not text:
        return "the revised statement is empty"
    if len(text) > MAX_STATEMENT_CHARS:
        return f"the revised statement exceeds {MAX_STATEMENT_CHARS} characters"
    if not (basis or "").strip():
        return "it does not say what in the evidence differs from the statement"
    if not _cited(case, evidence_ids, EvidenceCategory.SYMPTOM_EVIDENCE):
        return "it cites no symptom evidence showing the problem as revised"
    bar = _acted_on_bar(case)
    if bar:
        return bar
    current = current_statement(case)
    if current and (
        text.casefold() == current.strip().casefold()
        or hypothesis_statements_duplicate(text, current)
    ):
        return "it restates the current statement; add detail to the evidence instead"
    for hyp in case.hypotheses.values():
        if not hyp.state.is_terminal and hypothesis_statements_duplicate(
            text, hyp.statement
        ):
            return (
                "it restates a cause hypothesis; the problem statement describes "
                "what is observed, never why"
            )
    for node in case.causal_nodes.values():
        if node.node_type == NodeType.ROOT and hypothesis_statements_duplicate(
            text, node.statement
        ):
            return (
                "it restates a root cause; the problem statement describes what "
                "is observed, never why"
            )
    if revision_declined(case, wording_key):
        return "the user already declined this wording"
    return None


def invalidation_refusal(case: Case, evidence_ids: list[str], basis: str) -> str | None:
    """Why the problem cannot be found a false alarm now, or None."""
    from faultmaven.core.investigation.cause_assurance import (
        ENGINE_EVIDENCE_AUTHOR,
    )

    status = case.progress.problem_status
    if status not in (ProblemStatus.UNVERIFIED, ProblemStatus.VERIFIED):
        return f"the problem is {status.value}"
    if not (basis or "").strip():
        return "it does not say what in the evidence shows the problem never existed"
    if not _cited(case, evidence_ids, EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE):
        return (
            "it cites no symptom_absence evidence showing the reported symptom "
            "was not present where and when it was reported"
        )
    bar = _acted_on_bar(case)
    if bar:
        return bar
    # Any cause observed removed, from any fix window: a failed fix's rows
    # prove the problem existed at least as strongly as a successful one's, so
    # this reads the raw rows, not the resolution leg (which excludes what a
    # failed fix window holds, #1927).
    if any(
        getattr(e, "category", None) == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        and getattr(e, "collected_by", None) != ENGINE_EVIDENCE_AUTHOR
        for e in case.evidence or []
    ):
        return "a cause was confirmed eliminated, so the problem existed"
    return None


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------


def pending_revision(case: Case) -> PendingRevision | None:
    """The revised statement awaiting the user's re-confirmation: the payload
    of the open revision offer, or None."""
    offer = open_revision(case)
    return offer.revision if offer is not None else None


def propose_revision(
    case: Case,
    *,
    text: str,
    evidence_ids: list[str],
    basis: str,
    wording_key: str,
) -> PendingRevision:
    """Open a revision offer holding a revised statement for the user's
    re-confirmation. A proposal made while one is open answers it
    ``superseded`` and opens the new wording, keeping the cause work already
    staged, the turn the hold began and the status the case had before it.
    The caller has checked ``revision_refusal``, so no decline binds it."""
    previous = pending_revision(case)
    if previous is not None:
        case.offer_record = answer_offer(
            case,
            proposal_id=open_revision(case).proposal_id,
            answer=AnswerKind.SUPERSEDED,
            via=AnswerVia.ENGINE,
        )
    revision = PendingRevision(
        text=text.strip(),
        evidence_ids=tuple(evidence_ids),
        basis=basis.strip(),
        proposed_at_turn=case.current_turn,
        held_since_turn=(
            previous.held_since_turn if previous is not None else case.current_turn
        ),
        prior_status=(
            previous.prior_status
            if previous is not None
            else case.progress.problem_status
        ),
        wording_key=wording_key,
        staged=previous.staged if previous is not None else (),
    )
    record = open_offer(
        case, kind=OfferKind.REVISION, basis=OfferBasis.REVISION, revision=revision
    )
    if record is None:
        raise ValueError(
            f"case {case.case_id}: a declined wording was proposed again; "
            "revision_refusal admits only a wording the user has not declined"
        )
    case.offer_record = record
    _move(case, ProblemStatus.REVISION_PENDING, via="revision_proposed")
    return revision


def stage_cause_work(case: Case, *, updates: dict, evidence_added: list[str]) -> None:
    """Hold this turn's cause work on the open revision, with the evidence
    ids its ``new_index_N`` refs resolve against."""
    revision = pending_revision(case)
    case.offer_record = restage_revision(
        case,
        (
            *revision.staged,
            StagedCauseWork(
                turn=case.current_turn,
                updates=updates,
                evidence_added=tuple(evidence_added),
            ),
        ),
    )


def hold_fix_on_revision(
    case: Case, *, identity: str, user_message: str, evidence_added: list[str]
) -> None:
    """Carry a fix acceptance through the hold (#2105): set it on this turn's
    staged bundle, staging an empty one when the turn staged nothing."""
    revision = pending_revision(case)
    staged = list(revision.staged)
    index = next((i for i, b in enumerate(staged) if b.turn == case.current_turn), None)
    if index is None:
        staged.append(
            StagedCauseWork(
                turn=case.current_turn, evidence_added=tuple(evidence_added)
            )
        )
        index = len(staged) - 1
    staged[index] = staged[index].model_copy(
        update={"held_fix": identity, "held_fix_message": user_message[:10000]}
    )
    case.offer_record = restage_revision(case, staged)


def commit_revision(case: Case, *, via: AnswerVia) -> PendingRevision:
    """The user re-confirmed the revised statement: write it to every store
    that holds the statement and verify the problem. Returns the revision, whose
    staged cause work the caller replays."""
    pv = case.problem_verification
    offer = open_revision(case)
    assert offer is not None, "commit_revision without a revision open"
    pending = offer.revision
    case.offer_record = answer_offer(
        case,
        proposal_id=offer.proposal_id,
        answer=AnswerKind.ACCEPTED,
        via=via,
    )
    _write_statement(case, pending.text)
    pv.statement_history.append(
        ProblemStatementRecord(
            kind=StatementRecordKind.REVISED,
            text=pending.text,
            turn=case.current_turn,
            evidence_ids=list(pending.evidence_ids),
            rationale=pending.basis,
        )
    )
    pv.invalidation = None
    _supersede_symptom_needs(
        case, f"problem statement revised at turn {case.current_turn}"
    )
    _move(case, ProblemStatus.VERIFIED, via="revision_confirmed")
    return pending


def decline_revision(case: Case, *, via: AnswerVia) -> PendingRevision | None:
    """The user declined the revised statement: back to where the case was,
    with the decline recorded against the wording, so it is not proposed
    again."""
    return _answer_pending(
        case, answer=AnswerKind.DECLINED, via=via, status_via="revision_declined"
    )


def cancel_revision(
    case: Case, *, via: AnswerVia, answer: AnswerKind = AnswerKind.WITHDRAWN
) -> PendingRevision | None:
    """The user moved past the revision (closed the case themselves): back to
    where the case was. Not a decline — nothing binds the wording. The menu
    close withdraws it; a REST close lapses it."""
    return _answer_pending(
        case, answer=answer, via=via, status_via="revision_cancelled"
    )


def invalidate_problem(case: Case, *, evidence_ids: list[str], basis: str) -> None:
    """The evidence shows the reported symptom was never present."""
    pv = case.problem_verification
    pv.invalidation = ProblemInvalidation(
        rationale=basis.strip(),
        evidence_ids=list(evidence_ids),
        turn=case.current_turn,
        prior_status=case.progress.problem_status,
    )
    pv.statement_history.append(
        ProblemStatementRecord(
            kind=StatementRecordKind.INVALIDATED,
            text=current_statement(case),
            turn=case.current_turn,
            evidence_ids=list(evidence_ids),
            rationale=basis.strip(),
        )
    )
    _supersede_symptom_needs(
        case,
        f"the reported symptom was found never present at turn {case.current_turn}",
    )
    _move(case, ProblemStatus.INVALIDATED, via="false_alarm")


def withdraw_invalidation(case: Case, *, basis: str) -> bool:
    """The user disputed the false-alarm finding: the problem returns to where
    it stood before the finding — verified if it was, since nothing refuted
    that verification. Returns whether there was a finding to withdraw."""
    pv = case.problem_verification
    if case.progress.problem_status != ProblemStatus.INVALIDATED:
        return False
    pv.statement_history.append(
        ProblemStatementRecord(
            kind=StatementRecordKind.INVALIDATION_WITHDRAWN,
            text=current_statement(case),
            turn=case.current_turn,
            rationale=(basis or "").strip() or None,
        )
    )
    _clear_invalidation(case, via="false_alarm_withdrawn")
    return True


def false_alarm_close_declined_at(case: Case) -> int | None:
    """The turn the user declined closing on the standing false-alarm finding,
    or None when no finding stands or no close on it was declined. Whoever
    opened that close (the engine, the model, the user's menu), the decline's
    premise is the finding, so it binds while the finding stands
    (``offers.decline_stands``). Read only while the case is INVALIDATED: a
    finding carried under a pending revision is answered by the revision's own
    handshake first."""
    if case.progress.problem_status != ProblemStatus.INVALIDATED:
        return None
    declined = standing_false_alarm_decline(case)
    return declined.at_turn if declined is not None else None


def _clear_invalidation(case: Case, *, via: str) -> None:
    pv = case.problem_verification
    prior = pv.invalidation.prior_status if pv.invalidation else None
    pv.invalidation = None
    _move(case, prior or ProblemStatus.UNVERIFIED, via=via)


def edit_statement_refusal(case: Case, text: str) -> str | None:
    """Why the user cannot edit the statement directly now, or None."""
    if not (text or "").strip():
        return "the problem statement cannot be empty during an investigation"
    if len(text.strip()) > MAX_STATEMENT_CHARS:
        return f"the problem statement exceeds {MAX_STATEMENT_CHARS} characters"
    if case.progress.problem_status == ProblemStatus.REVISION_PENDING:
        return "a revised statement is awaiting confirmation; answer it first"
    return None


def edit_statement(case: Case, text: str) -> None:
    """The user edited the statement directly. An edit is the user's word, not
    evidence, so it never verifies: a verified problem stays verified (the user
    sharpened wording the evidence already showed), an unverified one stays
    unverified. The open symptom needs asked for evidence of the old wording
    and are superseded. A false-alarm finding was a finding about the old
    statement: it is cleared, the problem is unverified against the new one,
    and the offer to close on that finding lapses, whoever made it."""
    text = text.strip()
    if case.problem_verification is None:
        # The Gate-1 transition always creates the record; a case without one
        # is malformed, and the edit gives it the record it should have had.
        case.problem_verification = ProblemVerification(symptom_statement=text)
    _write_statement(case, text)
    case.problem_verification.statement_history.append(
        ProblemStatementRecord(
            kind=StatementRecordKind.EDITED, text=text, turn=case.current_turn
        )
    )
    _supersede_symptom_needs(
        case, f"the user edited the problem statement at turn {case.current_turn}"
    )
    if case.progress.problem_status == ProblemStatus.INVALIDATED:
        # The close offered on the finding lapses with it, at the save
        # (``offers.lapse_offers``).
        case.problem_verification.invalidation = None
        _move(case, ProblemStatus.UNVERIFIED, via="statement_edited")


def record_confirmed_statement(case: Case) -> None:
    """Gate 1 opened the investigation on this statement: the first entry of
    the statement's history."""
    case.problem_verification.statement_history.append(
        ProblemStatementRecord(
            kind=StatementRecordKind.CONFIRMED,
            text=current_statement(case),
            turn=case.current_turn,
        )
    )


def is_false_alarm_close(offer: Offer | None) -> bool:
    """Whether ``offer`` is a close resting on the false-alarm finding,
    whoever opened it (the engine, the model, the user's menu). Such a close
    never outlives the finding: a withdrawal or an edit takes it back without
    the user answering it."""
    return bool(
        offer is not None
        and offer.kind == OfferKind.CLOSE
        and offer.closure_reason == ClosureReason.CLOSED_FALSE_ALARM
    )


def is_engine_false_alarm_close(offer: Offer | None) -> bool:
    """Whether ``offer`` is the engine's own false-alarm close — the one
    terminal offer a revision may withdraw and take the place of."""
    return is_false_alarm_close(offer) and offer.basis == OfferBasis.FALSE_ALARM


def _write_statement(case: Case, text: str) -> None:
    """The statement lives in three places — the case description, the
    verification record, and the causal graph's PROBLEM node every chain ends
    at. They are written together. The node is re-texted in place: chains key
    on its id."""
    case.description = text
    case.problem_verification.symptom_statement = text
    problem = next(
        (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
        None,
    )
    if problem is not None:
        problem.statement = text[:MAX_STATEMENT_CHARS]


def _supersede_symptom_needs(case: Case, reason: str) -> None:
    """Retire the open symptom-verification needs: they asked for evidence of
    a statement that no longer stands (evidence-needs-design §7.4 — only LLM
    judgment or a problem-statement change supersedes them)."""
    from faultmaven.core.investigation.lifecycle_metrics import (
        evidence_need_status_changed_total,
    )

    for need in case.evidence_needs:
        if need.purpose != NeedPurpose.SYMPTOM_VERIFICATION or need.state not in (
            NeedState.PENDING,
            NeedState.PARTIALLY_MET,
        ):
            continue
        prior = need.state
        need.state = NeedState.SUPERSEDED
        need.superseded_reason = reason
        need.revoke_obtainability_if_terminal()
        need.updated_at = datetime.now(UTC)
        evidence_need_status_changed_total.labels(
            from_state=prior.value, to_state=NeedState.SUPERSEDED.value
        ).inc()


def _answer_pending(
    case: Case, *, answer: AnswerKind, via: AnswerVia, status_via: str
) -> PendingRevision | None:
    """Answer the open revision and return the case to the status it had
    before the revision was proposed."""
    offer = open_revision(case)
    if offer is None:
        return None
    case.offer_record = answer_offer(
        case, proposal_id=offer.proposal_id, answer=answer, via=via
    )
    _move(case, offer.revision.prior_status, via=status_via)
    return offer.revision


def _move(case: Case, to: ProblemStatus, *, via: str) -> bool:
    progress = case.progress
    before = progress.problem_status
    if before == to:
        return False
    progress.problem_status = to
    logger.info(
        "Case %s: problem_status %s -> %s at turn %s (via %s)",
        case.case_id,
        before.value,
        to.value,
        case.current_turn,
        via,
    )
    return True
