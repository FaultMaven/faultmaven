"""The narration step (#2636 B1b, #2615 stage R): a decision cycle's codes,
rendered after its record is written.

The deciders record codes (``stage_gates.record_decision``); a notice code
holds its place in ``system_notices`` with a token. ``narrate_turn`` replaces
each token with what the code renders (``narration.notices``): from the
cycle's record for a code in ``READS_THE_RECORD``, from its params for every
other but the unbound re-checks (below). Each offer code's prose is rendered
from its params (``narration.offers.render_offer``), published under its key
(``PROSE_KEYS``) and written as the offer's summary (B1d). It runs once per
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
    NoticeCode,
    OfferCode,
)
from faultmaven.core.investigation.narration.notices import (
    READS_THE_RECORD,
    render_notice,
    unbound_lines,
)
from faultmaven.core.investigation.narration.offers import render_offer
from faultmaven.core.investigation.offers import summarize_offer
from faultmaven.core.investigation.outcome import compute_case_outcome
from faultmaven.modules.case.contracts import AnswerKind, CaseOutcome

from .stage_gates import _NOTICES_HEAD_KEY

logger = logging.getLogger(__name__)

#: Set to whether the cycle's record was written (``record_case_outcome``):
#: the narration renders from no record the cycle did not write.
OUTCOME_WRITTEN_KEY = "outcome_written"

#: The narration's conflicts (``narration.<code>``), handed to the turn's
#: commit plan by ``_finalize_turn`` and counted after the commit (#2689).
NARRATION_CONFLICTS_KEY = "narration_conflicts"

#: The unbound re-check lines this narration told, as ``(line, ids)`` pairs,
#: for ``mark_told`` once the turn record is created (#2555).
UNBOUND_TOLD_KEY = "unbound_told"


def _outcome(case: Any, metadata: dict, log: list) -> Optional[CaseOutcome]:
    """The record the cycle wrote; when its write failed, the outcome computed
    in memory for the narration alone, never stored (its fold counts nothing,
    #2689), and only when a code in the log has a renderer that
    reads the record (``READS_THE_RECORD``). None when no such code is logged,
    or when that computation fails too: the renderers then fall back on the
    facts their deciders recorded."""
    if metadata.get(OUTCOME_WRITTEN_KEY):
        return case.progress.outcome
    if not any(entry["code"] in READS_THE_RECORD for entry in log):
        return None
    try:
        return compute_case_outcome(case.progress.outcome, case)
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


def _conflicts(log: list, outcome: CaseOutcome) -> list[str]:
    """The conflict labels (``narration.<code>``) of each field a decision was
    decided from (``DECIDED_FROM``) that the stored record does not state as
    decided: the resolution's reason, the closure verdict, the problem's
    status, or whether a cause is identified. One decision logged under two
    codes (step 2's pending check and its needs-info request) is one
    conflict. Counted only after the turn commits (#2689). The decision's text
    is rendered either way (A4); stage S3 switches the deciders to the
    record."""
    counted = set()
    found = []
    for entry in log:
        params = entry["params"]
        fields = DECIDED_FROM.get((entry["code"], params.get("composer")), ())
        for field in fields:
            decided = params[DECIDED_PARAMS[field]]
            recorded = _recorded(outcome, field)
            if _agrees(field, decided, recorded) or (field, decided) in counted:
                continue
            counted.add((field, decided))
            _count(entry["code"], field, decided, recorded)
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
    offer code's, in the cycle that opened it."""
    return proposal_id is not None and any(
        a.proposal_id == proposal_id and a.answer == AnswerKind.LAPSED
        for a in case.offer_record.answers
    )


def _narrate_offer(case: Any, metadata: dict, code: str, params: dict) -> None:
    """Publish the offer's prose under its key, and write it as the summary
    of the offer it opened while that offer is open. An offer that lapsed in
    the cycle that opened it shows nothing: no prose, so no confirmation pair
    (ruled 2026-11-12, R2). Any other answer keeps the prose (#2770)."""
    if _lapsed(case, params.get("proposal_id")):
        return
    text = render_offer(code, params)
    if code in PROSE_KEYS:
        metadata[PROSE_KEYS[code]] = text
    proposal_id = params.get("proposal_id")
    if proposal_id is not None and not summarize_offer(case, proposal_id, text):
        logger.info(
            "Case %s: offer %s closed in the cycle that opened it; no summary "
            "written (#2636)",
            getattr(case, "case_id", None),
            proposal_id,
        )


def _offer_stands(case: Any, proposal_id: str) -> bool:
    open_offer = case.offer_record.open
    return open_offer is not None and open_offer.proposal_id == proposal_id


def _engine_offer_stands(case: Any, metadata: dict, params: dict) -> dict:
    """``params`` with whether the engine's offer still stands at narration,
    and the compliance flags of an offer that lapsed after the drop (B3)."""
    proposal_id = params["proposal_id"]
    if _offer_stands(case, proposal_id):
        return {**params, "stands": True}
    answers = [
        str(a.answer) for a in case.offer_record.answers if a.proposal_id == proposal_id
    ]
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
    for entry in log:
        code, params = entry["code"], entry["params"]
        if code in _OFFER_CODES:
            _narrate_offer(case, metadata, code, params)
            continue
        if entry["token"] is None:
            continue
        if code == NoticeCode.MITIGATION_VERIFY_REFUSED:
            params = {**params, "stored_disposition": _disposition_before(case, log)}
        if code == NoticeCode.ENGINE_OFFER_STANDS:
            params = _engine_offer_stands(case, metadata, params)
        if code == NoticeCode.UNBOUND_RECHECKS:
            texts = [line for line, _ids in told] if entry is unbound[0] else []
        else:
            texts = render_notice(code, params, outcome, log)
        _replace_token(metadata, entry["token"], texts)
