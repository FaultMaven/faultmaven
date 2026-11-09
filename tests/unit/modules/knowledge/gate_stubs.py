"""A runbook gate worker for the gate's tests (#2661).

Served in place of ``validation_worker`` (``runbook_gate._VALIDATION_WORKER``),
it runs the check ``FM_GATE_STUB`` names, whatever check the gate asked for.
A worker inherits the test's environment, so a test picks the stub with
``monkeypatch.setenv``. It imports the worker and the standard library only.
"""

import json
import logging
import os
import re
import sys

from faultmaven.infrastructure import worker_process

# Its importable name, not ``__main__``: the parent re-emits the records
# under it, where the test listens.
logger = logging.getLogger(__spec__.name)

#: The record a stub logs once it has entered its check.
ENTERED = "the test check has entered"

#: About 3-5 s of work: on a thread, where nothing can stop it, the test
#: process still ends. A worker is killed long before.
REPEATS = 100

#: Cubic backtracking: one ``sub`` over 500 characters is one C call of about
#: 50 ms that never drops the interpreter lock. Over a constant: no input
#: reaches it.
_BACKTRACKING = re.compile(r"a*a*b")


def holds_the_lock_in_c(content: str) -> dict:
    logger.warning(ENTERED)
    for _ in range(REPEATS):
        _BACKTRACKING.sub("", "a" * 500)
    return {"refused": None}


def loops_in_python(content: str) -> dict:
    logger.warning(ENTERED)
    for _ in range(REPEATS):
        for _ in range(400_000):
            pass
    return {"refused": None}


def exits(content: str) -> dict:
    os._exit(7)


def _check(request, send):
    stub = globals()[os.environ["FM_GATE_STUB"]]
    (content,) = request["parts"]
    return lambda: stub(content)


if __name__ == "__main__":
    sys.exit(
        worker_process.serve(
            _check, "test check", result_parts=lambda a: [json.dumps(a)]
        )
    )
