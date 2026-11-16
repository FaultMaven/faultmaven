"""The runbook gate, in a worker its deadline stops (Principle 14, #2661).

The checks one request needs run in one ``validation_worker`` process, never in
the API process, under ``FairSlots`` keyed by the user who asked, and fail
closed. A refusal that depends only on the input is a 422 with its ``reason``
(``GateRefused``): past the CPU-time deadline, past the memory limit, or an
answer past the output limit. One that depends on load is a 503 with
``Retry-After`` (``GateUnavailable``): no slot within the wait, the wall-clock
backstop, a kill, or a worker that could not start. A check that raises is a
defect in it: the generic 500 (``GateFailed``). Every input is bounded at
100,000 characters first, except the text verify publishes: its frontmatter is
re-emitted with YAML aliases expanded, so it can grow past the bound (#2674).
"""

import asyncio
import json
import logging
import math
import signal
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
from faultmaven.modules.knowledge.exceptions import (
    RUNBOOK_CHECK_RETRY_AFTER_SECONDS,
    GateBusy,
    GateFailed,
    GateOutputTooLarge,
    GateTimedOut,
    GateTooLarge,
    GateUnavailable,
    RunbookQualityError,
)

logger = logging.getLogger(__name__)

#: Each check's CPU time, from its start, serialising its answer included
#: (``ITIMER_PROF``). Measured 2026-11-16 (#2661 3b2), alone and at full load
#: (below): the legitimate worst at 100,000 characters, the pack's three
#: slowest runbooks and a dense runbook-shaped body, is 0.13 s alone and 0.18 s
#: at full load, so this is 17x that. Every shape got the same verdict at full
#: load as alone. Hostile shapes end here by design: a 45,000-item YAML alias
#: list and a line of ``dd`` tokens (#2837) reach it alone and loaded. A
#: 49,000-word title (0.46-0.51 s alone, 0.47-0.51 s loaded) and 1.8 KB of
#: nested frontmatter (1.15-1.23 s, 1.22-1.28 s) are refused within it.
GATE_WORK_CPU_SECONDS = 3.0

#: ``RLIMIT_CPU`` for a call of one check, the backstop if the timer fails: the
#: worker's imports (0.5 s of CPU measured), the check's deadline and slack;
#: each further check adds its deadline. SIGXCPU is then reachable only past a
#: failed timer.
GATE_CPU_LIMIT_SECONDS = 10

#: The share of the cores a check gets at worst: every slot of every Principle
#: 14 worker running at once. Evidence Tier 1's ``max(2, cores)`` slots
#: (``TIER1_SLOTS``, ``preprocessing_service.py``), document preparation's and
#: the gate's. "Full load" below is that, measured on two cores: each slot busy
#: in a loop, Tier 1 with 10 MB of source code, preparation with a large DOCX.
GATE_SLOTS = 4
_CORES = index_cores()
_CPU_SHARE = min(1.0, _CORES / (max(2, int(_CORES)) + PREPARATION_SLOTS + GATE_SLOTS))

#: Spawn to ``ready``: the imports' 0.5 s of CPU, doubled, at the worst share.
#: Measured 2026-11-16 (#2661 3b2): imports cost 0.34-0.75 s of CPU, and spawn
#: to ``ready`` took 0.36-0.76 s alone and at most 2.36 s at full load on two
#: cores, inside 0.75 of this (3 s).
GATE_START_SECONDS = 1.0 / _CPU_SHARE

#: Each check's wall-clock backstop, a 503: its CPU deadline plus 2 s, at the
#: worst share, so the CPU deadline is reached first. On two cores the share is
#: 1/4: 20 s. Measured 2026-11-16 (#2661 3b2) at full load on two cores, a
#: check that ran to its 3 s deadline took 10.1-13.8 s of wall time. That is
#: past 0.75 of the 16 s that plus 1 s gave; 2 s is the smallest whole value
#: that keeps it inside 0.75 (15 s). No check at full load ended at this bound.
GATE_WORK_WALL_SECONDS = (GATE_WORK_CPU_SECONDS + 2) / _CPU_SHARE

#: Each transfer: at most 400 KB of UTF-8 in, the output limit out.
GATE_TRANSFER_SECONDS = 1.0

#: How long a request waits behind full slots before it is told to retry; the
#: ``Retry-After`` of every 503 here. A request makes one call, of at most two
#: checks (upload: quality and security; verify: the draft's security and the
#: published text's). Its worst on two cores is wait + start + transfers + two
#: walls = 5 + 4 + 2 + 2 x 20 = 51 s, inside a 60 s ingress read timeout; the
#: slowest upload call measured at full load took 15.3 s. On one core the share
#: is 1/8 and it is 95 s, past it: there the ingress can answer before the gate
#: does. Measured 2026-11-16 (#2661 3b2) at full load on two cores, with the
#: four slots held by legitimate checks a fifth user got a slot in 0.08-1.06 s.
#: With the four held by checks running to their deadline, four of five got
#: the busy 503 at 5 s, as designed.
GATE_SLOT_WAIT_SECONDS = RUNBOOK_CHECK_RETRY_AFTER_SECONDS

#: A conversion's checks, which run after its model calls were paid for, wait
#: for a slot as long as the longest call takes at worst (two checks), so a
#: busy gate does not drop a paid-for draft. A case's conversion holds its
#: case's in-flight lease meanwhile. 46 s on two cores. Measured 2026-11-16
#: (#2661 3b2) at full load on two cores, with the four slots held by checks
#: running to their deadline, a conversion's check got a slot in 0.8-10.3 s.
GATE_BACKGROUND_SLOT_WAIT_SECONDS = (
    GATE_START_SECONDS + 2 * GATE_TRANSFER_SECONDS + 2 * GATE_WORK_WALL_SECONDS
)

#: ``RLIMIT_AS``: a worker peaked at 58 MiB on the largest answer; about 4x.
GATE_MEMORY_BYTES = 256 * 1024 * 1024

#: What the parent reads: the largest answer measured is 2.31 MB (3,316 causes
#: with one-word Statements, an error each), 4x that plus 1 MiB of log records.
GATE_OUTPUT_LIMIT_BYTES = 10 * 1024 * 1024

#: The principal of a check no user asked for: an ingest with no owner, as the
#: pack's runbooks have. They share one slot key, so they wait behind users'
#: checks as one principal does.
SYSTEM_GATE_PRINCIPAL = "system:kb-ingest"

#: The worker module. Always this value: tests point it at one of their own.
_VALIDATION_WORKER = validation_worker.__name__

_gate_slots_state: Optional[tuple] = None


@dataclass(frozen=True)
class SecurityAnswer:
    """The security check's answer for one exact text, given earlier in this
    request, so a later step publishing that same text does not check it
    again. ``enforce`` raises its refusal where the check would have run."""

    content: str
    refused: Optional[Dict[str, List[str]]]

    def enforce(self) -> None:
        if self.refused is not None:
            raise RunbookQualityError(**self.refused)


def _gate_slots() -> worker_process.FairSlots:
    """The slots, made on the running loop (``worker_process.loop_slots``)."""
    global _gate_slots_state
    _gate_slots_state = worker_process.loop_slots(_gate_slots_state, GATE_SLOTS)
    return _gate_slots_state[1]


def _refusal(checks: List[str], end: worker_process.WorkerEnd) -> Exception:
    """What a worker that gave no answer ends as."""
    if end.code in (-signal.SIGPROF, -signal.SIGXCPU):
        logger.warning("runbook checks %s ran past a CPU deadline", checks)
        return GateTimedOut()
    if end.code == worker_process.OUT_OF_MEMORY_EXIT:
        logger.warning("runbook checks %s ran past the memory limit", checks)
        return GateTooLarge()
    if end.phase == "work" and (end.failure is not None or end.code == 1):
        # The check raised: a defect in it, which no retry changes. A failure
        # before the work (the request read, the bounds set) is a worker that
        # could not start, below.
        logger.error("runbook checks %s failed: %s", checks, end.failure)
        return GateFailed()
    # The wall-clock backstop in any phase, a kill (the OOM killer's included),
    # or a worker that could not start: load, never the input alone.
    logger.error(
        "runbook checks %s did not answer: ended in %s, exit %s, timed out %s",
        checks,
        end.phase,
        end.code,
        end.timed_out,
    )
    return GateUnavailable()


async def _run(
    checks: List[Tuple[str, str]], principal: str, slot_wait: Optional[float] = None
) -> List[Dict[str, Any]]:
    """The ``(check, text)`` pairs in one worker, in order, under the
    principal's slot: their answers up to the first refusal, or a refusal of
    the call. A caller no request waits on passes its own ``slot_wait``."""
    if not isinstance(principal, str) or not principal:
        raise ValueError("a runbook check runs for the user who asked: none given")
    texts = list(dict.fromkeys(text for _, text in checks))
    names = [name for name, _ in checks]
    slots = _gate_slots()
    try:
        async with asyncio.timeout(slot_wait or GATE_SLOT_WAIT_SECONDS):
            await slots.acquire(principal)
    except TimeoutError:
        logger.warning("runbook checks %s found no slot free", names)
        raise GateBusy() from None
    try:
        end = await worker_process.run(
            _VALIDATION_WORKER,
            {
                "checks": [[name, texts.index(text)] for name, text in checks],
                "work_cpu_seconds": GATE_WORK_CPU_SECONDS,
            },
            memory_bytes=GATE_MEMORY_BYTES,
            cpu_seconds=math.ceil(
                GATE_CPU_LIMIT_SECONDS + (len(checks) - 1) * GATE_WORK_CPU_SECONDS
            ),
            output_limit_bytes=GATE_OUTPUT_LIMIT_BYTES,
            phases=worker_process.Phases(
                GATE_START_SECONDS,
                GATE_TRANSFER_SECONDS,
                len(checks) * GATE_WORK_WALL_SECONDS,
                GATE_TRANSFER_SECONDS,
            ),
            parts=tuple(texts),
        )
        if end.result is None:
            raise _refusal(names, end)
        return json.loads(end.result[0])
    except worker_process.WorkerOutputExceeded:
        logger.warning("runbook checks %s answered past the output limit", names)
        raise GateOutputTooLarge() from None
    except (worker_process.WorkerFailed, ValueError) as e:
        logger.error("runbook checks %s did not answer: %s", names, e)
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
    (answer,) = await _run([("validate_and_score", content)], principal, slot_wait)
    return (
        ValidationResult.model_validate(answer["validation"]),
        QualityScore.model_validate(answer["quality"]),
    )


async def aenforce_runbook_quality(
    content: str, *, principal: str
) -> tuple[QualityScore, SecurityAnswer]:
    """``enforce_runbook_quality``, in a worker, with the security check of the
    same text in the same call: the score of the body it passes, for the draft
    row the upload writes, and the security answer its publication enforces."""
    answers = await _run(
        [("enforce_runbook_quality", content), ("enforce_runbook_security", content)],
        principal,
    )
    _enforce(answers[0])
    return (
        QualityScore.model_validate(answers[0]["quality"]),
        SecurityAnswer(content, answers[1]["refused"]),
    )


async def aenforce_runbook_security(
    content: str, *, principal: str, then: Optional[str] = None
) -> Optional[SecurityAnswer]:
    """``enforce_runbook_security``, in a worker. With ``then``, the text this
    request publishes next is checked in the same call: its answer comes back
    for the publication to enforce, once this text's check passed."""
    checks = [("enforce_runbook_security", content)]
    if then is not None and then != content:
        checks.append(("enforce_runbook_security", then))
    answers = await _run(checks, principal)
    _enforce(answers[0])
    return None if then is None else SecurityAnswer(then, answers[-1]["refused"])
