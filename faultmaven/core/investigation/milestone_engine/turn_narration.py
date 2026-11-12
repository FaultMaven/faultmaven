"""The narration step (#2636 B1b, #2615 stage R): a decision cycle's notice
codes, rendered after its record is written.

The deciders record codes (``stage_gates.record_decision``); a notice code
holds its place in ``system_notices`` with a token. ``narrate_turn`` replaces
each token with what the code renders (``narration.notices``): from the
cycle's record for a code in ``READS_THE_RECORD``, from its params for every
other. It runs once per turn, in ``_finalize_turn``, before
the turn record copies the notices.
"""

import logging
from typing import Any, Optional

from faultmaven.core.investigation.narration.codes import DECISIONS_KEY, NoticeCode
from faultmaven.core.investigation.narration.notices import (
    READS_THE_RECORD,
    render_notice,
)
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


def narrate_turn(case: Any, metadata: dict) -> None:
    """Render the turn's decision log after its record."""
    log = metadata.get(DECISIONS_KEY) or []
    outcome = _outcome(case, metadata, log)
    for entry in log:
        if entry["token"] is None:
            continue
        params = entry["params"]
        if entry["code"] == NoticeCode.MITIGATION_VERIFY_REFUSED:
            params = {**params, "stored_disposition": _disposition_before(case, log)}
        texts = render_notice(entry["code"], params, outcome, log)
        _replace_token(metadata, entry["token"], texts)
