"""Refusing cause work that arrives before the problem is verified.

Three apply steps refuse cause work on an unverified problem
(``problem_status.cause_work_accepted``): the root-cause claims (step 2c), the
hypotheses (step 3) and the causal chain (``chain_emission``). Each records what
it refused here; the model is told once, at the end of the apply step, in a
single note naming everything refused. Three near-identical notes per turn
crowded other feedback out of a turn record that truncates from the tail.
"""

import logging
from typing import Any

from faultmaven.core.investigation.lifecycle_metrics import (
    cause_work_refused_unverified_total,
)

from .stage_gates import _add_system_feedback

logger = logging.getLogger(__name__)

_REFUSED_KEY = "cause_work_refused"


def refuse_cause_work(
    case_id: str, metadata: dict[str, Any], *, kind: str, what: str, count: int = 1
) -> None:
    """Record one refused piece of cause work: counted by ``kind``
    (``hypothesis`` | ``chain`` | ``conclusion``), named by ``what`` in the
    turn's single note."""
    cause_work_refused_unverified_total.labels(kind=kind).inc(count)
    metadata.setdefault(_REFUSED_KEY, []).append(what)
    logger.info("Case %s: refused %s on an unverified problem", case_id, what)


def report_refused_cause_work(metadata: dict[str, Any]) -> None:
    """The one note for the turn, when anything was refused."""
    refused = metadata.get(_REFUSED_KEY)
    if not refused:
        return
    _add_system_feedback(
        metadata,
        f"CAUSE WORK NOT ACCEPTED: {'; '.join(refused)} arrived before the "
        "problem was verified and was not recorded. Verify the symptom first "
        "(symptom_verified with cited symptom evidence); hypotheses, a causal "
        "chain and a root-cause conclusion can be sent in the same response "
        "that verifies it. Evidence you recorded is kept and can be linked to "
        "them then.",
    )
