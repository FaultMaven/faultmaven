"""The runbook gate, in a worker its deadline kills (Principle 14, #2661).

Each check runs in ``validation_worker``, never in the API process, under
``FairSlots`` keyed by the user. It answers, or refuses without admitting
content: ``GateTimedOut`` (422) past the deadline, ``GateBusy`` (503) past the
slot wait, ``GateUnavailable`` (503) for any other end. Every input is bounded
at 100,000 characters first, so the limits are measured at that bound.
"""

import asyncio
import dataclasses
import json
import logging
import math
from typing import Any, Dict, Optional

from faultmaven.exceptions import FaultMavenException
from faultmaven.infrastructure import worker_process
from faultmaven.modules.knowledge.domain.models.validation import (
    QualityScore,
    ValidationResult,
)
from faultmaven.modules.knowledge.domain.services import validation_worker
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookQualityError,
)

logger = logging.getLogger(__name__)

#: The check alone, in its worker. At 100,000 characters the slowest check took
#: 0.08-0.11 s on runbook-shaped text and 2.34 s CPU on the slowest shape found
#: (about 1,250 causes with near-duplicate Statements); about 4x that.
GATE_WORK_SECONDS = 10.0

#: Spawn to ``ready``: the interpreter and the validator's imports, about
#: 0.3 s alone (0.6 s here) and at most 0.82 s with four starting at once.
GATE_START_SECONDS = 5.0

#: Each transfer: at most 400 KB of UTF-8 in, at most about 2.3 MB out.
GATE_TRANSFER_SECONDS = 1.0

#: ``RLIMIT_AS``: a worker peaked at 58 MiB on the largest answer; about 4x.
GATE_MEMORY_BYTES = 256 * 1024 * 1024

#: What the parent reads: the largest answer measured is 2.31 MB (3,316 causes
#: with one-word Statements, an error each), 4x that plus 1 MiB of log records.
GATE_OUTPUT_LIMIT_BYTES = 10 * 1024 * 1024

#: Checks at once, as before the worker.
GATE_SLOTS = 4

#: How long a caller waits behind full slots before it is told to retry; the
#: ``Retry-After`` of that answer.
GATE_SLOT_WAIT_SECONDS = 10

#: The ruled answer past the work deadline (#2661): the message and the help.
GATE_TIMEOUT_MESSAGE = (
    "This document took too long to check. Split it into smaller runbooks."
)

#: The worker module. Always this value: tests point it at one of their own.
_VALIDATION_WORKER = validation_worker.__name__

_gate_slots_state: Optional[tuple] = None


class GateTimedOut(RunbookQualityError):
    """The check ran past ``GATE_WORK_SECONDS``: refused as input-shaped (422)."""

    reason = "validation_timeout"

    def __init__(self, *_args) -> None:  # ``args`` back from copy and pickle
        super().__init__(
            errors=[f"The check did not finish within {GATE_WORK_SECONDS:g} seconds."]
        )
        self.args = (GATE_TIMEOUT_MESSAGE,)


class GateUnavailable(FaultMavenException):
    """The check did not answer (a crash or a bound other than the deadline):
    503, retryable, and nothing was admitted."""

    error_code = "RUNBOOK_VALIDATION_UNAVAILABLE"

    def __init__(self, message: str = "The runbook check could not run.") -> None:
        super().__init__(message)
        self.retry_after_seconds = math.ceil(GATE_SLOT_WAIT_SECONDS)


class GateBusy(GateUnavailable):
    """No slot within ``GATE_SLOT_WAIT_SECONDS``: 503, retryable."""

    error_code = "RUNBOOK_VALIDATION_BUSY"

    def __init__(self, *_args) -> None:  # ``args`` back from copy and pickle
        super().__init__("The server is busy checking other runbooks.")


def _gate_slots() -> worker_process.FairSlots:
    """The slots, made on the running loop (``worker_process.loop_slots``)."""
    global _gate_slots_state
    _gate_slots_state = worker_process.loop_slots(_gate_slots_state, GATE_SLOTS)
    return _gate_slots_state[1]


async def _run(
    check: str, content: str, principal: str, slot_wait: Optional[float] = None
) -> Dict[str, Any]:
    """One check in its worker, under a slot: its answer, or a refusal. A caller
    no request waits on passes a ``slot_wait`` of its own deadline."""
    slots = _gate_slots()
    try:
        async with asyncio.timeout(slot_wait or GATE_SLOT_WAIT_SECONDS):
            await slots.acquire(principal)
    except TimeoutError:
        logger.warning("runbook check %s found no slot free", check)
        raise GateBusy() from None
    phases = worker_process.Phases(
        GATE_START_SECONDS,
        GATE_TRANSFER_SECONDS,
        GATE_WORK_SECONDS,
        GATE_TRANSFER_SECONDS,
    )
    try:
        end = await worker_process.run(
            _VALIDATION_WORKER,
            {"check": check},
            memory_bytes=GATE_MEMORY_BYTES,
            cpu_seconds=math.ceil(sum(dataclasses.astuple(phases))) + 1,
            output_limit_bytes=GATE_OUTPUT_LIMIT_BYTES,
            phases=phases,
            parts=(content,),
        )
        if end.timed_out and end.phase == "work":
            logger.warning("runbook check %s ran past its deadline; killed", check)
            raise GateTimedOut()
        if end.result is None:
            raise worker_process.WorkerFailed(
                f"ended in {end.phase}: exit {end.code}, timed out {end.timed_out}, "
                f"{end.failure}"
            )
        return json.loads(end.result[0])
    except (worker_process.WorkerFailed, ValueError) as e:
        logger.error("runbook check %s did not answer: %s", check, e)
        raise GateUnavailable() from e
    finally:
        slots.release(principal)


def _enforce(answer: Dict[str, Any]) -> None:
    if answer["refused"] is not None:
        raise RunbookQualityError(**answer["refused"])


async def avalidate_and_score(
    content: str, *, principal: str, slot_wait: Optional[float] = None
) -> tuple[ValidationResult, QualityScore]:
    """``validate_and_score``, in a worker."""
    answer = await _run("validate_and_score", content, principal, slot_wait)
    return (
        ValidationResult.model_validate(answer["validation"]),
        QualityScore.model_validate(answer["quality"]),
    )


async def aenforce_runbook_quality(content: str, *, principal: str) -> None:
    """``enforce_runbook_quality``, in a worker."""
    _enforce(await _run("enforce_runbook_quality", content, principal))


async def aenforce_runbook_security(content: str, *, principal: str) -> None:
    """``enforce_runbook_security``, in a worker."""
    _enforce(await _run("enforce_runbook_security", content, principal))
