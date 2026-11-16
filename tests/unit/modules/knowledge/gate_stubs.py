"""A runbook gate worker for the gate's tests (#2661).

Served in place of ``validation_worker`` (``runbook_gate._VALIDATION_WORKER``),
it runs the stub ``FM_GATE_STUB`` names, whatever check the gate asked for,
under the real worker's CPU-time deadline (``validation_worker.timed``). A
worker inherits the test's environment, so a test picks the stub with
``monkeypatch.setenv``. Every stub ends by its own state, never by a clock: a
loop runs until a deadline stops it, a sleep outlasts any bound a test sets.
"""

import logging
import os
import re
import resource
import signal
import sys
import time

from faultmaven.infrastructure import worker_process
from faultmaven.modules.knowledge.domain.services.validation_worker import timed

# Its importable name, not ``__main__``: the parent re-emits the records
# under it, where the test listens.
logger = logging.getLogger(__spec__.name)

#: The record a stub logs once it has entered its check.
ENTERED = "the test check has entered"

#: Cubic backtracking: one ``sub`` over 500 characters is one C call that never
#: drops the interpreter lock. Over a constant: no input reaches it.
_BACKTRACKING = re.compile(r"a*a*b")

_SCORE = {
    "overall": 90.0,
    "grade": "A",
    "completeness": 90.0,
    "clarity": 90.0,
    "actionability": 90.0,
    "comprehensiveness": 90.0,
}


def _cpu() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


#: CPU spent before the work: the imports, and ``FM_GATE_STUB_IMPORT_CPU``
#: seconds more burnt here, so a deadline counted from the process's start
#: would leave the work less than its own.
_burn_until = float(os.environ.get("FM_GATE_STUB_IMPORT_CPU", "0"))
while _cpu() < _burn_until:
    pass
CPU_BEFORE_WORK = _cpu()


def passes(check: str) -> dict:
    """The answer of a check that passes."""
    if check == "validate_and_score":
        return {"validation": {"passed": True}, "quality": _SCORE}
    if check == "enforce_runbook_quality":
        return {"refused": None, "quality": _SCORE}
    return {"refused": None}


def holds_the_lock_in_c(check: str) -> dict:
    logger.warning(ENTERED)
    while True:
        _BACKTRACKING.sub("", "a" * 500)


def loops_in_python(check: str) -> dict:
    logger.warning(ENTERED)
    while True:
        pass


def works_half_its_deadline(check: str) -> dict:
    """Spends half its deadline of CPU in the work, then answers, logging the
    CPU spent before the work began."""
    started = _cpu()
    while _cpu() - started < float(os.environ["FM_GATE_STUB_WORK_CPU"]):
        pass
    logger.warning("cpu before work %.3f", CPU_BEFORE_WORK)
    return passes(check)


def sleeps(check: str) -> dict:
    time.sleep(3600)
    return passes(check)


def allocates(check: str) -> dict:
    hoard = bytearray(1 << 30)
    return {"refused": None, "size": len(hoard)}


def floods(check: str) -> dict:
    return {"refused": {"errors": ["x" * (1 << 20)], "warnings": []}}


def raises(check: str) -> dict:
    raise RuntimeError("a check that failed")


def is_killed(check: str) -> dict:
    os.kill(os.getpid(), signal.SIGKILL)
    return passes(check)


def _check(request, send):
    stub = globals()[os.environ["FM_GATE_STUB"]]
    return timed(request["work_cpu_seconds"], lambda: stub(request["check"]))


if __name__ == "__main__":
    if os.environ.get("FM_GATE_STUB") == "never_starts":
        time.sleep(3600)
    sys.exit(worker_process.serve(_check, "test check", result_parts=lambda a: [a]))
