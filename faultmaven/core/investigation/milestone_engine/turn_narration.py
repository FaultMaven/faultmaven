"""The narration step (#2636 B1b, #2615 stage R): a decision cycle's codes,
rendered after its record is written.

The deciders record codes (``stage_gates.record_decision``); a notice code
holds its place in ``system_notices`` with a token. ``narrate_turn`` replaces
each token with what the code renders (``narration.notices``): from the
cycle's record for a code in ``READS_THE_RECORD``, from its params for every
other. Each offer code's prose is rendered from its params
(``narration.offers.render_offer``), published under its key
(``PROSE_KEYS``) and written as the offer's summary (B1d). It runs once per
turn, in ``_finalize_turn``, before the turn record copies the notices. A
statement-revision replay's offers are narrated there too: a turn that
replays never leaves at a step-0 exit, so it always reaches it.
"""

import logging
from typing import Any, Optional

from faultmaven.core.investigation.lifecycle_metrics import (
    case_outcome_conflicts_total,
)
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
)
from faultmaven.core.investigation.narration.offers import render_offer
from faultmaven.core.investigation.offers import summarize_offer
from faultmaven.core.investigation.outcome import compute_case_outcome
from faultmaven.modules.case.contracts import CaseOutcome

from .stage_gates import _NOTICES_HEAD_KEY

logger = logging.getLogger(__name__)

#: Set to whether the cycle's record was written (``record_case_outcome``):
#: the narration renders from no record the cycle did not write.
OUTCOME_WRITTEN_KEY = "outcome_written"


def _outcome(case: Any, metadata: dict, log: list) -> Optional[CaseOutcome]:
    """The record the cycle wrote; when its write failed, the outcome computed
    in memory for the narration alone, never stored, its fold counting no
    disagreement (#2636), and only when a code in the log has a renderer that
    reads the record (``READS_THE_RECORD``). None when no such code is logged,
    or when that computation fails too: the renderers then fall back on the
    facts their deciders recorded."""
    if metadata.get(OUTCOME_WRITTEN_KEY):
        return case.progress.outcome
    if not any(entry["code"] in READS_THE_RECORD for entry in log):
        return None
    try:
        return compute_case_outcome(case.progress.outcome, case, unreported=[])
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


def _conflicts(log: list, outcome: CaseOutcome) -> None:
    """Count each field a decision was decided from (``DECIDED_FROM``) that
    the stored record does not state as decided: the resolution's reason, the
    closure verdict, the problem's status, or whether a cause is identified.
    One decision logged under two codes (step 2's pending check and its
    needs-info request) is counted once. The decision's text is rendered
    either way (A4); stage S3 switches the deciders to the record."""
    counted = set()
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


def _count(code: str, what: str, decided: Any, recorded: Any) -> None:
    logger.info(
        "Case narration: %s's %s was %s, the record reads %s (#2636)",
        code,
        what,
        decided,
        recorded,
    )
    case_outcome_conflicts_total.labels(field=f"narration.{code}").inc()


def _narrate_offer(case: Any, metadata: dict, code: str, params: dict) -> None:
    """Publish the offer's prose under its key, and write it as the summary
    of the offer it opened while that offer is open."""
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


def narrate_turn(case: Any, metadata: dict) -> None:
    """Render the turn's decision log after its record. A conflict is counted
    only on a record the cycle stored (counting (i))."""
    log = metadata.get(DECISIONS_KEY) or []
    outcome = _outcome(case, metadata, log)
    if metadata.get(OUTCOME_WRITTEN_KEY) and outcome is not None:
        _conflicts(log, outcome)
    for entry in log:
        code, params = entry["code"], entry["params"]
        if code in _OFFER_CODES:
            _narrate_offer(case, metadata, code, params)
            continue
        if entry["token"] is None:
            continue
        if code == NoticeCode.MITIGATION_VERIFY_REFUSED:
            params = {**params, "stored_disposition": _disposition_before(case, log)}
        texts = render_notice(code, params, outcome, log)
        _replace_token(metadata, entry["token"], texts)
