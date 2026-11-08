"""Stages the preparation worker runs in place of ``preparation.prepare`` (#2622).

A test names one in the parent's ``_PREPARATION_STAGE``; only the worker
imports it, so this module imports the standard library and nothing else.
Each takes ``prepare``'s arguments and calls ``enter`` as ``prepare`` does.
"""

import logging
import os
import re
import time

logger = logging.getLogger(__name__)

#: The record each stage below logs once it has entered its stage.
ENTERED = "the test stage has entered stage 1"


def _entered(enter) -> None:
    enter(0)
    enter(1)
    logger.warning(ENTERED)


def hold_the_lock(file_path, content_type, enter):
    """One C call that holds the interpreter lock throughout (a large
    ``re.sub``), then the moment it returned, as the refusal's reason."""
    text = "ab" * 5_000_000
    _entered(enter)
    re.sub("a", "c", text)
    ended = time.monotonic()
    return {"refused": "DOCUMENT_TOO_SHORT", "reason": repr(ended)}


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


def die_after_parsing(file_path, content_type, enter):
    _entered(enter)
    enter(2)
    os.abort()
