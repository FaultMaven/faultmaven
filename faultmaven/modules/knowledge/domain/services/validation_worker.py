"""The runbook gate's worker (#2661): the checks one request needs, in order, a
phased job of ``worker_process`` run as ``python -m`` by ``runbook_gate``. A
refusal is an answer, never a failure. It imports nothing of ``modules.case``.

Each check runs under its own CPU-time deadline: ``ITIMER_PROF`` counts this
process's CPU from the start of the check, serialising its answer included,
and its ``SIGPROF`` ends the process, inside C code too. The checks stop at the
first refusal, as the calls they replace would have.
"""

import json
import signal
import sys
from typing import Any, Callable, Dict, List, Tuple

from faultmaven.infrastructure import worker_process
from faultmaven.modules.knowledge.domain.services import runbook_validator as rv
from faultmaven.modules.knowledge.exceptions import RunbookQualityError


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
    except RunbookQualityError as refused:
        return {"refused": {"errors": refused.errors, "warnings": refused.warnings}}
    return {"refused": None}


#: The checks, by the name the request gives.
CHECKS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "validate_and_score": _scored,
    "enforce_runbook_quality": _quality,
    "enforce_runbook_security": _security,
}


def timed(cpu_seconds: float, answer: Callable[[], Any]) -> Tuple[Any, str]:
    """``answer()`` and its serialised form, under ``cpu_seconds`` of CPU time.
    The timer is disarmed however the check ends, so a check that raises is
    reported as the failure it is, never ended by the timer meanwhile."""
    signal.setitimer(signal.ITIMER_PROF, cpu_seconds)
    try:
        result = answer()
        return result, json.dumps(result)
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)


def answers(
    run: Callable[[str, str], Any],
    checks: List[List[Any]],
    parts: List[str],
    cpu_seconds: float,
) -> Callable[[], str]:
    """The work: each ``[check, part]`` in order, each under its own deadline,
    until the first refusal, as a JSON list."""

    def work() -> str:
        given: List[str] = []
        for name, part in checks:
            result, text = timed(cpu_seconds, lambda: run(name, parts[part]))
            given.append(text)
            if isinstance(result, dict) and result.get("refused"):
                break
        return "[" + ",".join(given) + "]"

    return work


def _check(request: Dict[str, Any], send) -> Callable[[], str]:
    return answers(
        lambda name, text: CHECKS[name](text),
        request["checks"],
        request["parts"],
        request["work_cpu_seconds"],
    )


def _parts(answer: str) -> List[str]:
    return [answer]


if __name__ == "__main__":
    sys.exit(worker_process.serve(_check, "runbook check", result_parts=_parts))
