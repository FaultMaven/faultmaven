"""One job in its own process, which its parent kills at the deadline (Principle 14).

The mechanism of document preparation (#2622) and Evidence Tier 1 (#2637).
``run``, the parent, spawns ``python -m <module>``, writes the request line on
its stdin and reads JSON frames from its stdout: ``stage``s, ``log`` records
(emitted here, under the caller's context) and the ``result``. On any end,
the deadline and a cancellation included, the worker is killed and reaped
before ``run`` returns. With ``phases`` each step has its own bound, so the
work's deadline counts the work alone: the worker reports ``ready`` after its
imports, ``working`` once it has the request and is bounded, ``done`` when the
work returns, and a ``failure``; text parts travel raw, never as JSON.

``serve``, the worker, bounds itself before the work: it dies with its parent,
volunteers for the OOM killer and caps CPU time and address space; past the
cap it exits ``OUT_OF_MEMORY_EXIT``. This module imports the standard library
and the bare package, and ``asyncio`` only in the parent's functions.
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
class Phases:
    """A phased job's bound on each step, in seconds: spawn to ``ready``, to
    ``working``, to ``done``, and the result read."""

    start: float
    request: float
    work: float
    result: float


@dataclass(frozen=True)
class WorkerEnd:
    """How a worker ended: its ``result``, ``timed_out``, or its exit ``code``;
    its last ``stage`` or ``phase``, and the ``failure`` a phased job raised."""

    result: Any = None
    timed_out: bool = False
    code: Optional[int] = None
    stage: Any = None
    phase: Optional[str] = None
    failure: Optional[str] = None


def loop_slots(held: Optional[tuple], count: int) -> tuple:
    """``held`` if made on the running loop, else ``(loop, Semaphore(count))``."""
    import asyncio

    loop = asyncio.get_running_loop()
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Semaphore(count))
    return held


async def _control(
    proc: "asyncio.subprocess.Process", seen: Dict[str, Any], output_limit_bytes: int
) -> Optional[Dict[str, Any]]:
    """The worker's next frame but a stage, failure or log record (emitted
    here, under this request), or None at its end of output or a torn line."""
    while True:
        try:
            line = await proc.stdout.readline()
        except ValueError as e:  # one line past the stream's limit
            raise WorkerFailed("the worker's output passed its limit") from e
        if not line.endswith(b"\n"):
            return None
        seen["received"] += len(line)
        if seen["received"] > output_limit_bytes:
            raise WorkerFailed("the worker's output passed its limit")
        try:
            frame = json.loads(line)
            if not isinstance(frame, dict):
                raise TypeError(type(frame).__name__)
            if "stage" in frame:
                seen["stage"] = frame["stage"]
            elif "failure" in frame:
                seen["failure"] = str(frame["failure"])
            elif "log" in frame:
                name, level, message = frame["log"]
                logging.getLogger(name).log(level, message)
            else:
                return frame
        except (ValueError, TypeError, KeyError) as e:
            raise WorkerFailed("the worker wrote a malformed frame") from e


async def _frames(
    proc: "asyncio.subprocess.Process", seen: Dict[str, Any], output_limit_bytes: int
):
    """The worker's result, or None at its end of output or a torn line."""
    frame = await _control(proc, seen, output_limit_bytes)
    if frame is None:
        return None
    if "result" not in frame:
        raise WorkerFailed("the worker wrote a malformed frame")
    return frame["result"]


async def _parts(
    proc: "asyncio.subprocess.Process", seen: Dict[str, Any], output_limit_bytes: int
) -> Optional[list]:
    """A phased worker's raw result parts, or None if it died first."""
    import asyncio

    frame = await _control(proc, seen, output_limit_bytes)
    if frame is None:
        return None
    sizes = frame.get("result_bytes")
    valid = isinstance(sizes, list) and all(isinstance(n, int) for n in sizes)
    if not valid or min(sizes, default=0) < 0:
        raise WorkerFailed("the worker wrote a malformed frame")
    seen["received"] += sum(sizes) + 1
    if seen["received"] > output_limit_bytes:
        raise WorkerFailed("the worker's output passed its limit")
    try:
        data, parts, at = await proc.stdout.readexactly(sum(sizes) + 1), [], 0
        for n in sizes:
            parts.append(str(memoryview(data)[at : at + n], "utf-8", "surrogatepass"))
            at += n
    except asyncio.IncompleteReadError:
        return None
    except UnicodeDecodeError as e:
        raise WorkerFailed("the worker wrote a malformed frame") from e
    if data[at:] != b"\n":
        raise WorkerFailed("the worker wrote a malformed frame")
    return parts


async def _phased(
    proc: "asyncio.subprocess.Process",
    request_bytes: bytes,
    seen: Dict[str, Any],
    output_limit_bytes: int,
    phases: Phases,
) -> WorkerEnd:
    """A phased worker's steps, each under its bound: how it ended."""
    import asyncio

    steps = (
        ("start", phases.start, "ready"),
        ("request", phases.request, "working"),
        ("work", phases.work, "done"),
        ("result", phases.result, None),
    )
    bound, got = None, None
    try:
        for seen["phase"], seconds, report in steps:
            async with asyncio.timeout(seconds) as bound:
                if report == "working":
                    try:
                        proc.stdin.write(request_bytes)
                        await proc.stdin.drain()
                        proc.stdin.close()
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # it died before reading; its exit answers below
                if report is None:
                    got = await _parts(proc, seen, output_limit_bytes)
                else:
                    frame = await _control(proc, seen, output_limit_bytes)
                    if frame is not None and frame.get("phase") != report:
                        raise WorkerFailed("the worker wrote a malformed frame")
                    got = frame
                if got is None:
                    code = await proc.wait()
                    return WorkerEnd(
                        code=code, phase=seen["phase"], failure=seen.get("failure")
                    )
    except TimeoutError:
        if bound is None or not bound.expired():
            raise
        return WorkerEnd(timed_out=True, phase=seen["phase"])
    return WorkerEnd(result=got, phase="result")


async def run(
    module: str,
    request: Dict[str, Any],
    *,
    memory_bytes: int,
    cpu_seconds: int,
    output_limit_bytes: int,
    deadline_seconds: Optional[float] = None,
    phases: Optional[Phases] = None,
    parts: tuple = (),
) -> WorkerEnd:
    """One worker, ``python -m module``, over ``request``: how it ended. A
    ``deadline_seconds`` runs from the start; ``phases`` (a worker served with
    ``result_parts``) bound each step, with ``parts`` and the result raw. Raises
    ``WorkerNotStarted`` or ``WorkerFailed``, the worker reaped first."""
    # Imported here, not at the top: a worker imports this module and never
    # uses asyncio, which costs a start about 50 ms.
    import asyncio

    raw = [part.encode("utf-8", "surrogatepass") for part in parts]
    request = {
        **request,
        "parent_pid": os.getpid(),
        "memory_bytes": memory_bytes,
        "cpu_seconds": cpu_seconds,
        "log_level": logging.getLogger().getEffectiveLevel(),
    }
    if raw:
        request["parts"] = [len(part) for part in raw]
    line = json.dumps(request, ensure_ascii=False).encode("utf-8", "surrogatepass")
    request_bytes = b"".join([line, b"\n", *raw])
    seen: Dict[str, Any] = {"stage": None, "received": 0}
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
    if phases is not None:
        try:
            return await _phased(proc, request_bytes, seen, output_limit_bytes, phases)
        finally:
            await _reap(proc)
    try:
        async with asyncio.timeout(deadline_seconds) as deadline:
            try:
                proc.stdin.write(request_bytes)
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
#: whose return value is the result.
Job = Callable[[Dict[str, Any], Callable[[Dict[str, Any]], None]], Callable[[], Any]]


def serve(
    job: Job,
    label: str,
    result_parts: Optional[Callable[[Any], list]] = None,
) -> int:
    """One job: the request from stdin, frames to stdout, the exit code. With
    ``result_parts`` it is phased: raw parts replace their sizes in the request,
    and ``result_parts(result)`` goes back raw. ``label`` names failures."""
    # Frames go to the original stdout; fd 1 is pointed at stderr, so nothing
    # a library prints can be read as a frame.
    out: BinaryIO = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    phased = result_parts is not None

    def send(frame: Dict[str, Any]) -> None:
        # A lone surrogate (pypdf decodes one from a ToUnicode map) is framed
        # as it is; the parent's ``json.loads`` reads it back the same way.
        line = json.dumps(frame, ensure_ascii=False) + "\n"
        out.write(line.encode("utf-8", errors="surrogatepass"))
        out.flush()

    try:
        if phased:
            send({"phase": "ready"})
        request = json.loads(sys.stdin.buffer.readline())
        if phased:
            request["parts"] = [
                sys.stdin.buffer.read(n).decode("utf-8", "surrogatepass")
                for n in request.get("parts", [])
            ]
        forward = _Forward(send)
        logging.getLogger().handlers[:] = [forward]
        logging.getLogger().setLevel(request["log_level"])
        try:
            work = job(request, send)
            _bound(
                request["parent_pid"], request["memory_bytes"], request["cpu_seconds"]
            )
            if phased:
                send({"phase": "working"})
            result = work()
            if not phased:
                forward.report_dropped()
                send({"result": result})
            else:
                send({"phase": "done"})
                raw = [p.encode("utf-8", "surrogatepass") for p in result_parts(result)]
                forward.report_dropped()
                send({"result_bytes": [len(p) for p in raw]})
                out.write(b"".join([*raw, b"\n"]))
                out.flush()
        finally:
            forward.report_dropped()
    except MemoryError:
        os._exit(OUT_OF_MEMORY_EXIT)
    except Exception as e:
        if phased:
            send({"failure": str(e)[:MAX_RECORD_CHARS]})
        failure = f"the {label} failed\n" + traceback.format_exc()
        send({"log": [__name__, logging.ERROR, _clipped(failure)]})
        return 1
    return 0
