"""One job in its own process, which its parent kills at the deadline (Principle 14).

The mechanism of document preparation (``document_preprocessor``, #2622) and
Evidence Tier 1 (``preprocessing_service``, #2637): one spawned process per
job, killed and reaped at its deadline, under declared time, memory and output
limits.

The parent's half is ``run``. It starts ``python -m <module>``, writes one
request line on its stdin and reads frames on its stdout, one JSON object a
line, in order: ``{"stage": ...}`` as a stage starts, ``{"log": [logger,
level, message]}`` for each log record, emitted here under the caller's
request context, and last ``{"result": ...}``. Past the deadline, on a
cancellation or on any other end, the worker is killed and reaped before
``run`` returns or raises.

The worker's half is ``serve``. Before it runs the job over its input it
bounds itself: it dies with its parent, volunteers for the OOM killer, and
sets its CPU-time and address-space limits, with no core file. An allocation
past the address-space limit exits with ``OUT_OF_MEMORY_EXIT``. This module
imports the standard library and the bare ``faultmaven`` package, and
``asyncio`` only where the parent uses it, so a worker loads what its job
needs and little else.
"""

import ctypes
import json
import logging
import os
import resource
import signal
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Dict, Optional

import faultmaven

if TYPE_CHECKING:
    import asyncio

#: The exit code of a worker an allocation past its memory limit stopped.
OUT_OF_MEMORY_EXIT = 3

#: Log records forwarded per job, and characters per record (its head and its
#: tail); past the count, one record counts the rest. So input that makes a
#: job warn on every object cannot fill the parent's output limit.
MAX_FORWARDED_RECORDS = 50
MAX_RECORD_CHARS = 2000

#: Where a worker runs: the directory holding this process's ``faultmaven``,
#: so ``-m``, which searches the working directory first, imports the same code.
_PACKAGE_ROOT = Path(faultmaven.__file__).resolve().parents[1]

_PR_SET_PDEATHSIG = 1


class WorkerFailed(RuntimeError):
    """The worker broke the frame protocol: its output passed its limit, or it
    wrote a malformed frame."""


class WorkerNotStarted(WorkerFailed):
    """The worker could not start; the ``OSError`` is the cause."""


@dataclass(frozen=True)
class WorkerEnd:
    """How a worker ended: with its ``result``, killed at its deadline
    (``timed_out``), or exited with ``code`` and no result. ``stage`` is the
    last stage it entered."""

    result: Any = None
    timed_out: bool = False
    code: Optional[int] = None
    stage: Any = None


# ---------------------------------------------------------------------------
# The parent
# ---------------------------------------------------------------------------


async def _frames(
    proc: "asyncio.subprocess.Process", seen: Dict[str, Any], output_limit_bytes: int
):
    """The worker's result, or None at its end of output, a line torn by its
    death included. Stage frames set ``seen["stage"]``; log records are
    emitted here, under this request."""
    received = 0
    while True:
        try:
            line = await proc.stdout.readline()
        except ValueError as e:  # one line past the stream's limit
            raise WorkerFailed("the worker's output passed its limit") from e
        if not line.endswith(b"\n"):
            return None
        received += len(line)
        if received > output_limit_bytes:
            raise WorkerFailed("the worker's output passed its limit")
        try:
            frame = json.loads(line)
            if "result" in frame:
                return frame["result"]
            if "stage" in frame:
                seen["stage"] = frame["stage"]
            else:
                name, level, message = frame["log"]
                logging.getLogger(name).log(level, message)
        except (ValueError, TypeError, KeyError) as e:
            raise WorkerFailed("the worker wrote a malformed frame") from e


async def _reap(proc: "asyncio.subprocess.Process") -> None:
    """Kill the worker and wait until it is reaped, a cancellation meanwhile
    included, so a caller's slot frees only once it is dead. Its output is read
    to the end, unread: a wait alone never returns while a full pipe holds the
    worker's stdout open."""
    import asyncio

    if proc.returncode is None:
        proc.kill()
    reaping = asyncio.ensure_future(proc.communicate())
    cancelled: Optional[asyncio.CancelledError] = None
    while not reaping.done():
        try:
            await asyncio.shield(reaping)
        except asyncio.CancelledError as e:
            cancelled = e
    reaping.result()
    if cancelled is not None:
        raise cancelled


async def run(
    module: str,
    request: Dict[str, Any],
    *,
    memory_bytes: int,
    cpu_seconds: int,
    output_limit_bytes: int,
    deadline_seconds: Optional[float] = None,
) -> WorkerEnd:
    """One worker, ``python -m module``, over ``request``: how it ended.

    The request goes with this process's id, the limits the worker sets on
    itself, and the log level. ``deadline_seconds`` runs from the start; with
    none, the caller's own timeout is the deadline, and its cancellation kills
    the worker like any other. The worker is dead and reaped before this
    returns or raises. Raises ``WorkerNotStarted`` if it cannot start, and
    ``WorkerFailed`` if it breaks the frame protocol.
    """
    # Imported here, not at the top: a worker imports this module and never
    # uses asyncio, which costs a start about 50 ms.
    import asyncio

    request = {
        **request,
        "parent_pid": os.getpid(),
        "memory_bytes": memory_bytes,
        "cpu_seconds": cpu_seconds,
        "log_level": logging.getLogger().getEffectiveLevel(),
    }
    seen: Dict[str, Any] = {"stage": None}
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            module,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            cwd=_PACKAGE_ROOT,
            limit=output_limit_bytes,
        )
    except OSError as e:
        raise WorkerNotStarted(f"the worker could not start: {e!r}") from e
    try:
        async with asyncio.timeout(deadline_seconds) as deadline:
            try:
                proc.stdin.write(json.dumps(request).encode() + b"\n")
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # it died before reading; its exit answers below
            result = await _frames(proc, seen, output_limit_bytes)
            if result is not None:
                return WorkerEnd(result=result, stage=seen["stage"])
            code = await proc.wait()
    except TimeoutError:
        if not deadline.expired():
            raise
        return WorkerEnd(timed_out=True, stage=seen["stage"])
    finally:
        await _reap(proc)
    return WorkerEnd(code=code, stage=seen["stage"])


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


def _clipped(message: str) -> str:
    if len(message) <= MAX_RECORD_CHARS:
        return message
    half = MAX_RECORD_CHARS // 2
    return f"{message[:half]} [...] {message[-half:]}"


class _Forward(logging.Handler):
    """Sends each record to the parent as a frame, up to the cap."""

    def __init__(self, send) -> None:
        super().__init__()
        self._send, self._sent, self._dropped = send, 0, 0

    def emit(self, record: logging.LogRecord) -> None:
        if self._sent >= MAX_FORWARDED_RECORDS:
            self._dropped += 1
            return
        self._sent += 1
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info))
        self._send({"log": [record.name, record.levelno, _clipped(message)]})

    def report_dropped(self) -> None:
        """Count the records past the cap, once: before the result, which the
        parent stops reading at."""
        if self._dropped:
            dropped, self._dropped = self._dropped, 0
            message = f"{dropped} more log records were not forwarded"
            self._send({"log": [__name__, logging.WARNING, message]})


def _bound(parent_pid: int, memory_bytes: int, cpu_seconds: int) -> None:
    """Die with the parent, volunteer for the OOM killer, and cap CPU time and
    address space, with no core file. A missing ``prctl`` or ``oom_score_adj``
    is logged."""
    logger = logging.getLogger(__name__)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
            logger.warning("prctl(PR_SET_PDEATHSIG) failed: %s", ctypes.get_errno())
    except (OSError, AttributeError) as e:
        logger.warning("prctl is unavailable, so this worker can outlive: %s", e)
    if os.getppid() != parent_pid:
        os._exit(1)  # the parent died before the death signal was armed
    try:
        Path("/proc/self/oom_score_adj").write_text("1000")
    except OSError as e:
        logger.warning("oom_score_adj is not settable: %s", e)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


#: A job: given the request and the frame writer, it imports what the request
#: names and returns the work, which runs once the process is bounded and
#: whose return value is the result frame.
Job = Callable[[Dict[str, Any], Callable[[Dict[str, Any]], None]], Callable[[], Any]]


def serve(job: Job, label: str) -> int:
    """One job in this worker: the request from stdin, the frames to stdout,
    and the exit code. ``label`` names the job in the record of a failure."""
    # Frames go to the original stdout; fd 1 is pointed at stderr, so nothing
    # a library prints can be read as a frame.
    out: BinaryIO = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)

    def send(frame: Dict[str, Any]) -> None:
        # A lone surrogate (pypdf decodes one from a ToUnicode map) is framed
        # as it is; the parent's ``json.loads`` reads it back the same way.
        line = json.dumps(frame, ensure_ascii=False) + "\n"
        out.write(line.encode("utf-8", errors="surrogatepass"))
        out.flush()

    try:
        request = json.loads(sys.stdin.buffer.readline())
        forward = _Forward(send)
        logging.getLogger().handlers[:] = [forward]
        logging.getLogger().setLevel(request["log_level"])
        try:
            work = job(request, send)
            _bound(
                request["parent_pid"], request["memory_bytes"], request["cpu_seconds"]
            )
            result = work()
            forward.report_dropped()
            send({"result": result})
        finally:
            forward.report_dropped()
    except MemoryError:
        os._exit(OUT_OF_MEMORY_EXIT)
    except Exception:
        failure = f"the {label} failed\n" + traceback.format_exc()
        send({"log": [__name__, logging.ERROR, _clipped(failure)]})
        return 1
    return 0
