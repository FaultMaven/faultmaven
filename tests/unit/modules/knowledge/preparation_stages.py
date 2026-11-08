"""Stages the preparation worker runs in place of ``preparation.prepare`` (#2622).

A test names one in the parent's ``_PREPARATION_STAGE``; only the worker
imports it, so this module imports the standard library and nothing else
(``prepare_and_measure`` imports the stage it wraps when it runs).
Each takes ``prepare``'s arguments and calls ``enter`` as ``prepare`` does.
"""

import collections
import itertools
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: The record each stage below logs once it has entered its stage.
ENTERED = "the test stage has entered stage 1"


def _entered(enter) -> None:
    enter(0)
    enter(1)
    logger.warning(ENTERED)


def prepare_and_measure(file_path, content_type, enter):
    """``prepare``, answering with its refusal code and the worker's peak
    address space in bytes (``VmPeak``), as the refusal's reason."""
    from faultmaven.modules.knowledge.domain.services.preparation import prepare

    refused = prepare(file_path, content_type, enter).get("refused")
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
    )
    peak = int(status["VmPeak"].split()[0]) * 1024
    return {"refused": "DOCUMENT_TOO_SHORT", "reason": f"{refused} {peak}"}


def hold_the_lock_forever(file_path, content_type, enter):
    """One C call that holds the interpreter lock and never returns: a deque
    with no room consumes an endless ``itertools.count`` inside C, running no
    bytecode, so the lock is never dropped. No regex: this helper adds no
    ReDoS pattern."""
    _entered(enter)
    collections.deque(itertools.count(), maxlen=0)


def never_return(file_path, content_type, enter):
    _entered(enter)
    while True:
        time.sleep(1)


def answer_with_my_pid(file_path, content_type, enter):
    _entered(enter)
    return {"refused": "DOCUMENT_TOO_SHORT", "reason": str(os.getpid())}


def answer_too_much(file_path, content_type, enter):
    _entered(enter)
    return {"refused": "DOCUMENT_TOO_SHORT", "reason": "x" * 200_000}


def run_out_of_memory(file_path, content_type, enter):
    _entered(enter)
    return bytearray(4 * 1024**3)


def die_before_reading(file_path, content_type, enter):
    enter(0)
    os.abort()


def die_while_parsing(file_path, content_type, enter):
    _entered(enter)
    os.abort()


def raise_while_parsing(file_path, content_type, enter):
    _entered(enter)
    raise RuntimeError("a fault in our own code, not the document's")


def die_in_stage_1b(file_path, content_type, enter):
    _entered(enter)
    enter("1b")
    os.abort()


def tear_a_frame_while_parsing(file_path, content_type, enter):
    """Dies mid-frame, as a worker killed while writing one does. The frame
    file is the one ``enter`` writes through (``preparation_worker.main``)."""
    _entered(enter)
    out = _free_variable(_free_variable(enter, "send"), "out")
    out.write(b'{"result": {"refused": "DOCUMENT_TOO_SH')
    out.flush()
    os.abort()


def log_past_the_cap(file_path, content_type, enter):
    _entered(enter)
    for n in range(60):
        logger.warning("record %d", n)
    return {"refused": "DOCUMENT_TOO_SHORT", "reason": "logged"}


def _free_variable(function, name: str):
    return function.__closure__[function.__code__.co_freevars.index(name)].cell_contents


def die_after_parsing(file_path, content_type, enter):
    _entered(enter)
    enter(2)
    os.abort()
