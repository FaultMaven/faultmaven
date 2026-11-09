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
    render_notice,
    unnarrated_rechecks,
)
from faultmaven.core.investigation.narration.offers import render_offer
from faultmaven.core.investigation.offers import summarize_offer
from faultmaven.modules.case.contracts import CaseOutcome, ResolutionReason

from .stage_gates import _NOTICES_HEAD_KEY

logger = logging.getLogger(__name__)

#: Set to whether the cycle's record was written (``record_case_outcome``):
#: the narration renders from no record the cycle did not write.
OUTCOME_WRITTEN_KEY = "outcome_written"


def _case_summary(case: Any, code: str, params: dict) -> str:
    """The summaries composed from the case, after the record (moved into
    ``narration`` in S2): the step-0 request turned READY, and the model's
    proposal, by the composer its branch chose."""
    from faultmaven.core.investigation.terminal_transitions import (
        assess_closure_readiness,
        assess_resolution_readiness,
    )

    from .terminal_replies import _build_resolution_confirmation

    composer = params.get("composer", "confirmation")
    if code == OfferCode.REQUEST_READY or composer == "confirmation":
        return _build_resolution_confirmation(case)
    if composer == "readiness":
        return assess_resolution_readiness(case).message
    return assess_closure_readiness(case).message


def _conflict(code: str, params: dict, outcome: Optional[CaseOutcome]) -> None:
    """The decision and the record disagree on the resolution's reason: the
    decision's code is rendered, and the conflict counted (A4); stage S3
    switches the deciders to the record."""
    if outcome is None:
        return
    if code == NoticeCode.PENDING_CHECK:
        decided = ResolutionReason.PENDING_CHECK
    elif "reason" in params and code in (
        OfferCode.NEEDS_INFO_UNMET_CLOSE,
        OfferCode.RESOLUTION_NEEDS_INFO,
    ):
        decided = params["reason"]
    else:
        return
    if outcome.resolution.reason != decided:
        logger.info(
            "Case narration: %s decided on %s, the record reads %s (#2636)",
            code,
            decided,
            outcome.resolution.reason,
        )
        case_outcome_conflicts_total.labels(field=f"narration.{code}").inc()


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
    outcome: Optional[CaseOutcome] = (
        case.progress.outcome if metadata.get(OUTCOME_WRITTEN_KEY) else None
    )
    # The watermark (#2555): the first turn this step narrates the case.
    if outcome is not None and outcome.narration_since is None:
        outcome.narration_since = case.current_turn
    log = metadata.get(DECISIONS_KEY) or []
    narrated = []
    for entry in log:
        code, params = entry["code"], entry["params"]
        _conflict(code, params, outcome)
        if code in OfferCode.__members__.values():
            if code in (OfferCode.REQUEST_READY, OfferCode.MODEL_PROPOSAL):
                text = _case_summary(case, code, params)
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
            narrated = unnarrated_rechecks(outcome)
        _replace_token(metadata, entry["token"], texts)
    for recheck in narrated:
        recheck.narrated_turn = case.current_turn
