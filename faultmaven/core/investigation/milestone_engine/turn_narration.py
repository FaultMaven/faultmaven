"""The narration step (#2636, #2615 stage R): a decision cycle's codes, rendered
after its record is written.

The deciders record codes (``stage_gates.record_decision``); a notice code holds
its place in ``system_notices`` with a token. ``narrate_turn`` replaces each
token with what the code renders from the record (``narration.notices``), fills
the summary and prose key of each offer the cycle opened
(``narration.offers``), and writes the narrated re-checks and the watermark into
the record. It runs once per turn, in ``_finalize_turn``, before the turn record
copies the notices.
"""

import logging
from typing import Any, Optional

from faultmaven.core.investigation.lifecycle_metrics import (
    case_outcome_conflicts_total,
)
from faultmaven.core.investigation.narration.codes import (
    DECISIONS_KEY,
    PROSE_KEYS,
    NoticeCode,
    OfferCode,
)
from faultmaven.core.investigation.narration.notices import (
    named_rechecks,
    render_notice,
)
from faultmaven.core.investigation.narration.offers import render_offer
from faultmaven.core.investigation.offers import summarize_offer
from faultmaven.core.investigation.outcome import compute_case_outcome
from faultmaven.modules.case.contracts import CaseOutcome, ResolutionReason

from .stage_gates import _NOTICES_HEAD_KEY

logger = logging.getLogger(__name__)

#: Set to whether the cycle's record was written (``record_case_outcome``):
#: the narration renders from no record the cycle did not write.
OUTCOME_WRITTEN_KEY = "outcome_written"


def _confirmation(case: Any) -> str:
    """The resolve confirmation, composed from the case after the record
    (stage S2 moves it into ``narration``)."""
    from .terminal_replies import _build_resolution_confirmation

    return _build_resolution_confirmation(case)


def _conflict(code: str, params: dict, outcome: Optional[CaseOutcome]) -> None:
    """The decision and the record disagree on what it decided from (the
    resolution's reason, or the closure verdict): the decision's branch is
    rendered, and the conflict counted (A4); stage S3 switches the deciders
    to the record."""
    if outcome is None:
        return
    if code == NoticeCode.PENDING_CHECK:
        decided, recorded = ResolutionReason.PENDING_CHECK, outcome.resolution.reason
    elif "reason" in params and code in _READINESS_CODES:
        decided, recorded = params["reason"], outcome.resolution.reason
    elif "verdict" in params and code == OfferCode.MODEL_PROPOSAL:
        decided, recorded = params["verdict"], outcome.closure.eligibility
    else:
        return
    if recorded != decided:
        logger.info(
            "Case narration: %s decided on %s, the record reads %s (#2636)",
            code,
            decided,
            recorded,
        )
        case_outcome_conflicts_total.labels(field=f"narration.{code}").inc()


_READINESS_CODES = (
    OfferCode.NEEDS_INFO_UNMET_CLOSE,
    OfferCode.RESOLUTION_NEEDS_INFO,
    OfferCode.MODEL_PROPOSAL,
)


def _outcome(case: Any, metadata: dict, log: list) -> Optional[CaseOutcome]:
    """The record the cycle wrote; when its write failed, the outcome
    computed in memory for the narration alone, never stored (#2636). None
    only when that fails too."""
    if metadata.get(OUTCOME_WRITTEN_KEY):
        return case.progress.outcome
    if not log:
        return None
    try:
        return compute_case_outcome(case.progress.outcome, case)
    except Exception:  # noqa: BLE001 — the write's failure is already counted
        logger.exception(
            "Case %s: no outcome to narrate from (#2636)",
            getattr(case, "case_id", None),
        )
        return None


def _first_narration(case: Any, outcome: CaseOutcome) -> None:
    """The watermark (#2555): the first time a case is narrated, the unbound
    re-checks a record before it held are marked narrated, unrendered; every
    row that first appears after is narrated once."""
    before = outcome._before_narration or frozenset()
    for recheck in outcome.unbound_rechecks:
        if recheck.evidence_id in before and recheck.narrated_turn is None:
            recheck.narrated_turn = case.current_turn
    outcome.narration_since = case.current_turn


def _replace_token(metadata: dict, token: str, texts: list[str]) -> None:
    notices = metadata.get("system_notices") or []
    if token not in notices:
        return
    index = notices.index(token)
    notices[index : index + 1] = texts
    if index < metadata.get(_NOTICES_HEAD_KEY, 0):
        metadata[_NOTICES_HEAD_KEY] += len(texts) - 1


def narrate_turn(case: Any, metadata: dict, *, offers_only: bool = False) -> None:
    """Render the turn's decision log after its record. ``offers_only``: a
    step-0 exit after a replay, which narrates the offers it holds and no
    notice (A11)."""
    log = metadata.get(DECISIONS_KEY) or []
    outcome = _outcome(case, metadata, log)
    if outcome is not None and outcome.narration_since is None:
        _first_narration(case, outcome)
    named = []
    for entry in log:
        code, params = entry["code"], entry["params"]
        _conflict(code, params, outcome)
        if code in set(OfferCode):
            if code == OfferCode.REQUEST_READY or (
                params.get("composer") == "confirmation"
            ):
                text = _confirmation(case)
            else:
                text = render_offer(code, params, outcome)
            pid = params.get("proposal_id")
            if pid is not None and not summarize_offer(case, pid, text):
                continue
            if code in PROSE_KEYS:
                metadata[PROSE_KEYS[code]] = text
            continue
        if offers_only or entry["token"] is None:
            continue
        texts = render_notice(code, params, outcome, log)
        if code == NoticeCode.UNBOUND_RECHECKS and texts:
            named = named_rechecks(outcome)
        _replace_token(metadata, entry["token"], texts)
    # Only the rows the notice named: the rest are narrated on a later turn.
    for recheck in named:
        recheck.narrated_turn = case.current_turn
