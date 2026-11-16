"""The runbook gate, in a worker its deadline stops (Principle 14, #2661).

Each check runs in ``validation_worker``, never in the API process, under
``FairSlots`` keyed by the user who asked, and fails closed. A refusal that
depends only on the input is a 422 with its ``reason`` (``GateRefused``): past
the CPU-time deadline, past the memory limit, or an answer past the output
limit. One that depends on load is a 503 with ``Retry-After``
(``GateUnavailable``): no slot within the wait, the wall-clock backstop, or any
other end of the worker. Every input is bounded at 100,000 characters first,
so the limits are measured at that bound.
"""

import asyncio
import json
import logging
import math
import signal
from typing import Any, Dict, Optional

from faultmaven.exceptions import FaultMavenException
from faultmaven.infrastructure import worker_process
from faultmaven.infrastructure.index_slot import index_cores
from faultmaven.modules.knowledge.domain.models.validation import (
    QualityScore,
    ValidationResult,
)
from faultmaven.modules.knowledge.domain.services import validation_worker
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    PREPARATION_SLOTS,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookQualityError,
)

logger = logging.getLogger(__name__)

#: The check's CPU time, from the start of its work, serialising included
#: (``ITIMER_PROF``). The legitimate worst at 100,000 characters is 0.04-0.22 s
#: (the shipped pack and dense runbook-shaped bodies), so this is 14x to 75x
#: that. Hostile shapes end here by design: a line of ``dd`` tokens (13-14 s,
#: #2837) and a 45,000-item YAML alias list (3.2 s); a 49,000-word title (1.6 s)
#: stays under it. The alias and title shapes are re-measured in #2661's 3b2.
GATE_WORK_CPU_SECONDS = 3.0

#: ``RLIMIT_CPU``, the backstop if the timer fails: the worker's imports (0.5 s
#: of CPU measured), the work's deadline and slack. SIGXCPU is then reachable
#: only past a failed timer.
GATE_CPU_LIMIT_SECONDS = 10

#: The share of the cores a check gets at worst: every slot of every Principle
#: 14 worker running at once. Evidence Tier 1's ``max(2, cores)`` slots
#: (``TIER1_SLOTS``, ``preprocessing_service.py``), document preparation's and
#: the gate's.
GATE_SLOTS = 4
_CORES = index_cores()
_CPU_SHARE = min(1.0, _CORES / (max(2, int(_CORES)) + PREPARATION_SLOTS + GATE_SLOTS))

#: Spawn to ``ready``: the imports' 0.5 s of CPU, doubled, at the worst share.
GATE_START_SECONDS = 1.0 / _CPU_SHARE

#: The work's wall-clock backstop, a 503: the CPU deadline plus the 1 s band
#: the verdict may move in, at the worst share, so the CPU deadline is reached
#: first. On two cores the share is 1/4: 16 s.
GATE_WORK_WALL_SECONDS = (GATE_WORK_CPU_SECONDS + 1) / _CPU_SHARE

#: Each transfer: at most 400 KB of UTF-8 in, the output limit out.
GATE_TRANSFER_SECONDS = 1.0

#: How long a request waits behind full slots before it is told to retry; the
#: ``Retry-After`` of every 503 here. The worst request on two cores is two
#: checks (upload: quality then security; verify: security twice) of wait +
#: start + transfers + wall = 2 x (5 + 4 + 2 + 16) = 54 s, inside a 60 s
#: ingress read timeout. On one core the share is 1/8 and it is 2 x 47 s,
#: past it: there the ingress can answer before the gate does.
GATE_SLOT_WAIT_SECONDS = 5

#: A case's conversion, which no request waits on, waits for a slot as long as
#: two checks take at worst, holding its case's in-flight lease meanwhile.
GATE_BACKGROUND_SLOT_WAIT_SECONDS = 2 * (
    GATE_START_SECONDS + 2 * GATE_TRANSFER_SECONDS + GATE_WORK_WALL_SECONDS
)

#: ``RLIMIT_AS``: a worker peaked at 58 MiB on the largest answer; about 4x.
GATE_MEMORY_BYTES = 256 * 1024 * 1024

#: What the parent reads: the largest answer measured is 2.31 MB (3,316 causes
#: with one-word Statements, an error each), 4x that plus 1 MiB of log records.
GATE_OUTPUT_LIMIT_BYTES = 10 * 1024 * 1024

#: The worker module. Always this value: tests point it at one of their own.
_VALIDATION_WORKER = validation_worker.__name__

_gate_slots_state: Optional[tuple] = None


class GateRefused(RunbookQualityError):
    """A check stopped for its input alone: the gate's 422, with ``reason`` and
    the ruled sentence as its one error. Nothing is admitted."""

    reason: str
    message: str

    def __init__(self, *_args) -> None:  # ``args`` back from copy and pickle
        super().__init__(errors=[self.message])
        self.args = (self.message,)


class GateTimedOut(GateRefused):
    """Past ``GATE_WORK_CPU_SECONDS`` of CPU (or the ``RLIMIT_CPU`` backstop)."""

    reason = "validation_timeout"
    message = "This document took too long to check. Split it into smaller runbooks."


class GateOutputTooLarge(GateRefused):
    """An answer past ``GATE_OUTPUT_LIMIT_BYTES``."""

    reason = "validation_output_too_large"
    message = (
        "This document has too many problems to list. Split it into smaller "
        "runbooks."
    )


class GateTooLarge(GateRefused):
    """Past ``GATE_MEMORY_BYTES``."""

    reason = "validation_too_large"
    message = "This document is too large to check. Split it into smaller runbooks."


class GateUnavailable(FaultMavenException):
    """The check could not run: a 503 with ``Retry-After``. Nothing is
    admitted."""

    error_code = "RUNBOOK_VALIDATION_UNAVAILABLE"
    message = "The runbook check could not run. Try again in a moment."
    retry_after_seconds = math.ceil(GATE_SLOT_WAIT_SECONDS)

    def __init__(self, *_args) -> None:  # ``args`` back from copy and pickle
        super().__init__(self.message)
        self.args = (self.message,)


class GateBusy(GateUnavailable):
    """No slot within the wait."""

    error_code = "RUNBOOK_VALIDATION_BUSY"
    message = "The server is busy checking other runbooks. Try again in a moment."


def _gate_slots() -> worker_process.FairSlots:
    """The slots, made on the running loop (``worker_process.loop_slots``)."""
    global _gate_slots_state
    _gate_slots_state = worker_process.loop_slots(_gate_slots_state, GATE_SLOTS)
    return _gate_slots_state[1]


def _refusal(check: str, end: worker_process.WorkerEnd) -> Exception:
    """What a worker that gave no answer ends as."""
    if end.code in (-signal.SIGPROF, -signal.SIGXCPU):
        logger.warning("runbook check %s ran past its CPU deadline", check)
        return GateTimedOut()
    if end.code == worker_process.OUT_OF_MEMORY_EXIT:
        logger.warning("runbook check %s ran past its memory limit", check)
        return GateTooLarge()
    # The wall-clock backstop in any phase, a kill (the OOM killer's included),
    # or a failure: load or a fault, never the input alone.
    logger.error(
        "runbook check %s did not answer: ended in %s, exit %s, timed out %s, %s",
        check,
        end.phase,
        end.code,
        end.timed_out,
        end.failure,
    )
    return GateUnavailable()


async def _run(
    check: str, content: str, principal: str, slot_wait: Optional[float] = None
) -> Dict[str, Any]:
    """One check in its worker, under the principal's slot: its answer, or a
    refusal. A caller no request waits on passes its own ``slot_wait``."""
    if not isinstance(principal, str) or not principal:
        raise ValueError("a runbook check runs for the user who asked: none given")
    slots = _gate_slots()
    try:
        async with asyncio.timeout(slot_wait or GATE_SLOT_WAIT_SECONDS):
            await slots.acquire(principal)
    except TimeoutError:
        logger.warning("runbook check %s found no slot free", check)
        raise GateBusy() from None
    try:
        end = await worker_process.run(
            _VALIDATION_WORKER,
            {"check": check, "work_cpu_seconds": GATE_WORK_CPU_SECONDS},
            memory_bytes=GATE_MEMORY_BYTES,
            cpu_seconds=GATE_CPU_LIMIT_SECONDS,
            output_limit_bytes=GATE_OUTPUT_LIMIT_BYTES,
            phases=worker_process.Phases(
                GATE_START_SECONDS,
                GATE_TRANSFER_SECONDS,
                GATE_WORK_WALL_SECONDS,
                GATE_TRANSFER_SECONDS,
            ),
            parts=(content,),
        )
        if end.result is None:
            raise _refusal(check, end)
        return json.loads(end.result[0])
    except worker_process.WorkerOutputExceeded:
        logger.warning("runbook check %s answered past its output limit", check)
        raise GateOutputTooLarge() from None
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


async def aenforce_runbook_quality(content: str, *, principal: str) -> QualityScore:
    """``enforce_runbook_quality``, in a worker: the score of the body it
    passes, for the draft row the upload writes."""
    answer = await _run("enforce_runbook_quality", content, principal)
    _enforce(answer)
    return QualityScore.model_validate(answer["quality"])


async def aenforce_runbook_security(content: str, *, principal: str) -> None:
    """``enforce_runbook_security``, in a worker."""
    _enforce(await _run("enforce_runbook_security", content, principal))
