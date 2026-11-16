"""The runbook gate's worker (#2661): one check over one body, a phased job of
``worker_process`` run as ``python -m`` by ``runbook_gate``. A refusal is an
answer, never a failure. It imports nothing of ``modules.case``.

The work runs under a CPU-time deadline: ``ITIMER_PROF`` counts this process's
CPU from the start of the work, serialising the answer included, and its
``SIGPROF`` ends the process, inside C code too.
"""

import json
import signal
import sys
from typing import Any, Callable, Dict, List

from faultmaven.infrastructure import worker_process
from faultmaven.modules.knowledge.domain.services import runbook_validator as rv


def _scored(content: str) -> Dict[str, Any]:
    validation, quality = rv.validate_and_score(content)
    return {"validation": validation.model_dump(), "quality": quality.model_dump()}


def _quality(content: str) -> Dict[str, Any]:
    """``enforce_runbook_quality``'s verdict, and the score of a body it passes,
    which the upload stores with its draft (#2661)."""
    validation, quality = rv.validate_and_score(content)
    if not validation.passed:
        refused = {"errors": validation.errors, "warnings": validation.warnings}
        return {"refused": refused}
    return {"refused": None, "quality": quality.model_dump()}


def _security(content: str) -> Dict[str, Any]:
    try:
        rv.enforce_runbook_security(content)
    except rv.RunbookQualityError as refused:
        return {"refused": {"errors": refused.errors, "warnings": refused.warnings}}
    return {"refused": None}


#: The checks, by the name the request gives.
CHECKS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "validate_and_score": _scored,
    "enforce_runbook_quality": _quality,
    "enforce_runbook_security": _security,
}


def timed(cpu_seconds: float, answer: Callable[[], Any]) -> Callable[[], str]:
    """The work: ``answer()`` serialised, under ``cpu_seconds`` of CPU time."""

    def work() -> str:
        signal.setitimer(signal.ITIMER_PROF, cpu_seconds)
        result = json.dumps(answer())
        signal.setitimer(signal.ITIMER_PROF, 0)
        return result

    return work


def _check(request: Dict[str, Any], send) -> Callable[[], str]:
    check = CHECKS[request["check"]]
    (content,) = request["parts"]
    return timed(request["work_cpu_seconds"], lambda: check(content))


def _parts(answer: str) -> List[str]:
    return [answer]


if __name__ == "__main__":
    sys.exit(worker_process.serve(_check, "runbook check", result_parts=_parts))
