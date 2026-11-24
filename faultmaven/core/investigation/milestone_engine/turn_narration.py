"""The narration step (#2636 B1b, #2615 stage R): a decision cycle's codes,
rendered after its record is written.

The deciders record codes (``stage_gates.record_decision``); a notice code
holds its place in ``system_notices`` with a token. ``narrate_turn`` replaces
each token with what the code renders (``narration.notices``): from the
cycle's record for a code in ``READS_THE_RECORD``, from its params for every
other but the unbound re-checks (below). Each offer code's prose is rendered
(``narration.offers.render_offer``) from its params, published under its key
(``PROSE_KEYS``) and written as the offer's summary (B1d). The held check
(``PENDING_CHECK``) and the offers in ``RECORD_PROSE`` state the record when
no switched decision of the narration disagrees with it on a field it was
decided from, and every one states its decider's params when one does
(``_switched_outcome``, #2811 S2b-2c). The request text of the need the record holds the resolution for is
read once, through the narrators' text accessor (``content_by_id``), and
handed to the renderers that state it. It runs once per
turn, in ``_finalize_turn``, before the turn record copies the notices. A
statement-revision replay's offers are narrated there too: a turn that
replays never leaves at a step-0 exit, so it always reaches it.

The unbound re-checks are told from the stored record (#2555): once per
narration, at the first entry's place with the last entry's params; once the
turn record is created, each row named on a line it kept is marked narrated
on the record (``mark_told``, ``narrated_turn``). The narration's conflicts are
collected (``NARRATION_CONFLICTS_KEY``) and counted after the turn commits
(#2689).
"""

import logging
from typing import Any, Optional

from faultmaven.core.investigation.narration import codes as narration_codes
from faultmaven.core.investigation.narration.codes import (
    DECIDED_FROM,
    DECIDED_PARAMS,
    DECISIONS_KEY,
    PROSE_KEYS,
    REFINES,
    NoticeCode,
    OfferCode,
)
from faultmaven.core.investigation.narration.fix_verification import content_by_id
from faultmaven.core.investigation.narration.notices import (
    READS_THE_RECORD,
    render_notice,
    unbound_lines,
)
from faultmaven.core.investigation.narration.offers import (
    RECORD_PROSE,
    record_params,
    render_offer,
)
from faultmaven.core.investigation.narration.statement import (
    OutcomeBaseline,
    offer_stated,
)
from faultmaven.core.investigation.offers import offer_view, summarize_offer
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.core.investigation.problem_status import restores_to
from faultmaven.modules.case.contracts import AnswerKind, CaseOutcome

from .stage_gates import _NOTICES_HEAD_KEY

logger = logging.getLogger(__name__)

#: Set to whether the cycle's record was written (``record_case_outcome``):
#: the narration renders from no record the cycle did not write.
OUTCOME_WRITTEN_KEY = "outcome_written"

#: The narration's conflicts (``narration.<code>``), handed to the turn's
#: commit plan by ``_finalize_turn`` and counted after the commit (#2689).
NARRATION_CONFLICTS_KEY = "narration_conflicts"

#: What each published offer prose states (``narration.statement.offer_stated``),
#: by its ``PROSE_KEYS`` key: turn-local, popped by ``_compose_turn_reply``.
OUTCOME_STATED_KEY = "outcome_stated"

#: The unbound re-check lines this narration told, as ``(line, ids)`` pairs,
#: for ``mark_told`` once the turn record is created (#2555).
UNBOUND_TOLD_KEY = "unbound_told"


def _outcome(case: Any, metadata: dict, log: list) -> Optional[CaseOutcome]:
    """The record the cycle wrote; when its write failed, the outcome computed
    in memory for the narration alone, never stored (its fold counts nothing,
    #2689), and only when a decision in the log is rendered from the record:
    a notice code in ``READS_THE_RECORD``, or an offer in ``RECORD_PROSE`` by
    its code and composer. None when no such decision is logged, or when that
    computation fails too: the renderers then fall back on the facts their
    deciders recorded."""
    if metadata.get(OUTCOME_WRITTEN_KEY):
        return case.progress.outcome
    if not any(_reads_the_record(e["code"], e["params"]) for e in log):
        return None
    try:
        return narration_outcome(case)
    except Exception:  # noqa: BLE001 — the write's failure is already counted
        logger.exception(
            "Case %s: no outcome to narrate from (#2636)",
            getattr(case, "case_id", None),
        )
        return None


def _replace_token(metadata: dict, token: str, texts: list[str]) -> None:
    """Put ``texts`` where ``token`` stands, less any text the channel already
    holds: a turn repeats no notice, as ``merge_system_notices`` keeps it."""
    notices = metadata.get("system_notices") or []
    if token not in notices:
        return
    texts = [t for t in texts if t not in notices]
    index = notices.index(token)
    notices[index : index + 1] = texts
    if index < metadata.get(_NOTICES_HEAD_KEY, 0):
        metadata[_NOTICES_HEAD_KEY] += len(texts) - 1


def _disposition_before(case: Any, log: list) -> Optional[str]:
    """The mitigation's disposition stored before this cycle: it is written
    once, so the case's own, unless this cycle wrote it
    (``MITIGATION_DISPOSED``)."""
    if any(e["code"] == NoticeCode.MITIGATION_DISPOSED for e in log):
        return None
    mitigation = case.progress.mitigation
    return mitigation.disposition if mitigation is not None else None


_OFFER_CODES = frozenset(OfferCode)


def _recorded(outcome: CaseOutcome, field: str) -> Any:
    section, name = field.split(".")
    return getattr(getattr(outcome, section), name)


def _agrees(field: str, decided: Any, recorded: Any) -> bool:
    # A leg is compared as each states it: identified or not.
    if field == "cause.identification_leg":
        return (decided is None) == (recorded is None)
    return decided == recorded


def _decided_as_recorded(params: dict, outcome: CaseOutcome, field: str) -> bool:
    return _agrees(field, params[DECIDED_PARAMS[field]], _recorded(outcome, field))


def _disagreements(code: str, params: dict, outcome: CaseOutcome) -> list[str]:
    """Each field ``code``'s decision was decided from (``DECIDED_FROM``)
    that ``outcome`` does not state as decided. A field that refines another
    (``REFINES``) is compared only where its parent agrees, so a decision
    lists a disagreement once. The one comparison of a decision with the
    record: the conflict counter's and the narration's."""
    fields = DECIDED_FROM.get((code, params.get("composer")), ())
    found = []
    for field in fields:
        parent = REFINES.get(field)
        if parent in fields and not _decided_as_recorded(params, outcome, parent):
            continue
        if not _decided_as_recorded(params, outcome, field):
            found.append(field)
    return found


def _reads_the_record(code: str, params: dict) -> bool:
    """Whether the decision renders from the record: a notice code in
    ``READS_THE_RECORD``, or an offer in ``RECORD_PROSE``."""
    return code in READS_THE_RECORD or (code, params.get("composer")) in RECORD_PROSE


def _switched(code: str, params: dict) -> bool:
    """Whether the decision's text states the record (#2811 S2b-2c): the
    held check, or an offer in ``RECORD_PROSE``."""
    return code == NoticeCode.PENDING_CHECK or (
        (code, params.get("composer")) in RECORD_PROSE
    )


def _switched_outcome(
    case: Any, log: list, outcome: Optional[CaseOutcome]
) -> Optional[CaseOutcome]:
    """What every switched decision of the narration renders from, decided
    once per narration: ``outcome`` when no switched decision in ``log``
    disagrees with it on a field it was decided from (``_disagreements``),
    else None, so every one renders from its decider's params, as before
    #2811 S2b-2c (the conflict is counted when the record was stored). One
    decision logged under two codes (step 2's held check and its needs-info
    request) thus names one need. An offer that lapsed in its cycle shows
    nothing and is not compared, as ``_conflicts`` skips it."""
    if outcome is None:
        return None
    for entry in log:
        params = entry["params"]
        if (
            _switched(entry["code"], params)
            and not _lapsed(case, params.get("proposal_id"))
            and _disagreements(entry["code"], params, outcome)
        ):
            return None
    return outcome


def _conflicts(log: list, outcome: CaseOutcome) -> list[str]:
    """The conflict labels (``narration.<code>``) of each field a decision was
    decided from (``DECIDED_FROM``) that the stored record does not state as
    decided: the resolution's reason, the closure verdict, the problem's
    status, whether a cause is identified, or why a held check holds. One
    decision logged under two codes (step 2's pending check and its
    needs-info request) is one conflict, and a field that refines another
    (``REFINES``) is compared only where its parent agrees, so a decision
    counts it at most once (``_disagreements``). Counted only after the turn
    commits (#2689). On a conflict, every switched decision's text is its
    decider's (A4, ``_switched_outcome``); stage S3d (#3053) switches the
    remaining deciders to the record."""
    counted = set()
    found = []
    for entry in log:
        params = entry["params"]
        for field in _disagreements(entry["code"], params, outcome):
            decided = params[DECIDED_PARAMS[field]]
            if (field, decided) in counted:
                continue
            counted.add((field, decided))
            _count(entry["code"], field, decided, _recorded(outcome, field))
            found.append(f"narration.{entry['code']}")
    return found


def _count(code: str, what: str, decided: Any, recorded: Any) -> None:
    logger.info(
        "Case narration: %s's %s was %s, the record reads %s (#2636)",
        code,
        what,
        decided,
        recorded,
    )


def _lapsed(case: Any, proposal_id: Optional[str]) -> bool:
    """Whether the offer ``proposal_id`` names was answered LAPSED: for an
    offer code's, in the cycle that opened it (``OfferView``, #2811 S2b-2d;
    it asks for no decline)."""
    return proposal_id is not None and offer_view(case).lapsed(proposal_id)


def _narrate_offer(
    case: Any,
    metadata: dict,
    code: str,
    params: dict,
    outcome: Optional[CaseOutcome],
    request_text: Optional[str],
) -> None:
    """Publish the offer's prose under its key, and write it as the summary
    of the offer it opened while that offer is open. For an offer in
    ``RECORD_PROSE`` the prose states ``outcome`` (``record_params``), the
    record where no switched decision of the narration disagrees with it
    (``_switched_outcome``), else its decider's params. An offer that lapsed in
    the cycle that opened it shows nothing: no prose, so no confirmation pair
    (ruled 2026-11-12, R2). Any other answer keeps the prose (#2770)."""
    if _lapsed(case, params.get("proposal_id")):
        return
    if outcome is not None:
        params = record_params(code, params, outcome, request_text)
    text = render_offer(code, params)
    if code in PROSE_KEYS:
        metadata[PROSE_KEYS[code]] = text
        metadata.setdefault(OUTCOME_STATED_KEY, {})[PROSE_KEYS[code]] = offer_stated(
            code, params
        )
    proposal_id = params.get("proposal_id")
    if proposal_id is not None and not summarize_offer(case, proposal_id, text):
        logger.info(
            "Case %s: offer %s closed in the cycle that opened it; no summary "
            "written (#2636)",
            getattr(case, "case_id", None),
            proposal_id,
        )


def _engine_offer_stands(case: Any, metadata: dict, params: dict) -> dict:
    """``params`` with whether the engine's offer still stands at narration,
    and the compliance flags of an offer that lapsed after the drop (B3),
    read from one view of the offer record (``OfferView``, #2811 S2b-2d),
    which asks for no decline."""
    proposal_id = params["proposal_id"]
    view = offer_view(case)
    if view.offer_stands(proposal_id):
        return {**params, "stands": True}
    answers = [str(a) for a in view.answers_for(proposal_id)]
    if AnswerKind.LAPSED not in answers:
        message = (
            f"offer {proposal_id} closed after the drop, not lapsed (answers "
            f"recorded: {answers or 'none'}) (#2636)"
        )
        if narration_codes.TOKENS_FAIL:
            raise AssertionError(message)
        logger.error("Case %s: %s", getattr(case, "case_id", None), message)
        return params
    metadata["transition_superseded_by_engine"] = False
    metadata["transition_dropped_for_lapsed_offer"] = True
    return {**params, "stands": False}


def outcome_baseline(case: Any) -> OutcomeBaseline:
    """The turn-start snapshot the outcome statement compares against (#2811
    S2c-3a), taken before any write of the turn: a deep copy of the record,
    the status its problem's hold or finding restores, selected by the
    record's own status, whether that status is the live one, and the ids of
    the actions already accepted."""
    accepted = frozenset(
        a.action_id
        for a in getattr(case, "proposed_actions", None) or ()
        if a.state == "accepted"
    )
    record = case.progress.outcome
    if record is None:
        return OutcomeBaseline(None, None, False, accepted)
    status = record.problem.status
    return OutcomeBaseline(
        record=record.model_copy(deep=True),
        restores_to=restores_to(case, status),
        problem_comparable=status == case.progress.problem_status,
        accepted=accepted,
    )


def mark_told(case: Any, metadata: dict, kept: list[str]) -> None:
    """Mark narrated, on the stored record, each unbound re-check named on a
    line this narration told (``UNBOUND_TOLD_KEY``) that the turn record kept
    (``kept``, its ``system_notices``): a row named only on a line the record
    cut or dropped is told on a later turn (#2555)."""
    named = {
        evidence_id
        for line, ids in metadata.pop(UNBOUND_TOLD_KEY, None) or []
        if line in kept
        for evidence_id in ids
    }
    for recheck in case.progress.outcome.unbound_rechecks if named else []:
        if recheck.evidence_id in named:
            recheck.narrated_turn = case.current_turn


def narrate_turn(case: Any, metadata: dict) -> None:
    """Render the turn's decision log after its record. A conflict is
    collected only on a record the cycle stored (counting (i)), into
    ``NARRATION_CONFLICTS_KEY``. A model proposal dropped for an engine offer
    that lapsed later in the cycle also sets the turn's compliance flags
    ``transition_dropped_for_lapsed_offer`` and
    ``transition_superseded_by_engine`` (false). The unbound re-checks render
    once, at the first such entry's place with the last one's params, and
    only from a stored record; their lines go to ``UNBOUND_TOLD_KEY``."""
    log = metadata.get(DECISIONS_KEY) or []
    outcome = _outcome(case, metadata, log)
    stored = bool(metadata.get(OUTCOME_WRITTEN_KEY)) and outcome is not None
    if stored:
        # An offer that lapsed in its cycle showed nothing: not compared.
        metadata.setdefault(NARRATION_CONFLICTS_KEY, []).extend(
            _conflicts(
                [e for e in log if not _lapsed(case, e["params"].get("proposal_id"))],
                outcome,
            )
        )
    unbound = [e for e in log if e["code"] == NoticeCode.UNBOUND_RECHECKS]
    told = unbound_lines(unbound[-1]["params"], outcome) if stored and unbound else []
    if told:
        metadata[UNBOUND_TOLD_KEY] = told
    need_id = outcome.resolution.pending_check_need_id if outcome is not None else None
    content = content_by_id(case, need_id) if need_id is not None else None
    request_text = content.text if content is not None else None
    switched = _switched_outcome(case, log, outcome)
    for entry in log:
        code, params = entry["code"], entry["params"]
        agreed = switched if _switched(code, params) else outcome
        if code in _OFFER_CODES:
            _narrate_offer(case, metadata, code, params, agreed, request_text)
            continue
        if entry["token"] is None:
            continue
        if code == NoticeCode.PENDING_CHECK:
            params = {**params, "record_request_text": request_text}
        if code == NoticeCode.MITIGATION_VERIFY_REFUSED:
            params = {**params, "stored_disposition": _disposition_before(case, log)}
        if code == NoticeCode.ENGINE_OFFER_STANDS:
            params = _engine_offer_stands(case, metadata, params)
        if code == NoticeCode.UNBOUND_RECHECKS:
            texts = [line for line, _ids in told] if entry is unbound[0] else []
        else:
            texts = render_notice(code, params, agreed, log)
        _replace_token(metadata, entry["token"], texts)
