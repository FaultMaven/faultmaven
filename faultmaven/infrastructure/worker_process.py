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
raises its ``oom_score_adj`` (first choice only where a container's OOM is not
grouped) and caps CPU time and address space; past the
cap it exits ``OUT_OF_MEMORY_EXIT``. A ``log`` record's exception travels as its
frames and class names, never its text, in the record's fourth element, which
the parent logs as the ``frames`` field (#2888). This module imports the
standard library, the bare package and ``exception_name`` (standard library
only), and ``asyncio`` only in the parent's functions.
"""

import ctypes
import itertools
import json
import logging
import os
import resource
import signal
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Dict, Optional

import faultmaven
from faultmaven.utils.exception_name import (
    exception_frames,
    exception_log_name,
    logged_exception,
)

if TYPE_CHECKING:
    import asyncio

#: The exit code of a worker an allocation past its memory limit stopped.
OUT_OF_MEMORY_EXIT = 3

#: Log records forwarded per job, and characters of a record's message and of
#: its frames, each (its head and its tail); past the count, one record counts
#: the rest. So input that makes a job warn on every object cannot fill the
#: parent's output limit.
MAX_FORWARDED_RECORDS = 50
MAX_RECORD_CHARS = 2000

#: Where a worker runs: the directory holding this process's ``faultmaven``,
#: so ``-m``, which searches the working directory first, imports the same code.
_PACKAGE_ROOT = Path(faultmaven.__file__).resolve().parents[1]

_PR_SET_PDEATHSIG = 1

#: Bytes per read of a worker's stderr, which is counted and dropped.
_DRAIN_CHUNK = 1 << 16


class WorkerFailed(RuntimeError):
    """The worker broke the frame protocol: its output passed its limit, or it
    wrote a malformed frame."""


class WorkerOutputExceeded(WorkerFailed):
    """The worker's output passed its limit: a ``WorkerFailed`` a caller whose
    output depends only on its input can tell apart (#2661)."""


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


class FairSlots:
    """Slots under Principle 14's admission rule (#2645): an account holding
    none takes one while one is free, one holding some only while two are free.
    A freed slot goes to the account holding the fewest, earliest place first,
    as the rule admits it. Within it (#2852): the waiter whose requester holds
    the fewest; then the requester granted least recently since the account was
    last idle, one not granted since first; then the earliest. The grant takes
    the account's earliest place, so accounts rank as without requesters. A
    cancelled grant is released. Releasing a key that holds none raises
    ``ValueError`` and changes nothing."""

    def __init__(self, count: int) -> None:
        self._free, self._held, self._by_key = count, Counter(), Counter()
        self._waiting: list = []  # [arrival, (account, requester), future, place]
        self._granted: Dict[str, Dict[Optional[str], int]] = {}  # since idle
        self._order = itertools.count()

    def locked(self) -> bool:
        """No slot free, or someone waits."""
        return self._free == 0 or bool(self._waiting)

    async def acquire(self, account: str, requester: Optional[str] = None) -> None:
        import asyncio

        arrival = next(self._order)
        future = asyncio.get_running_loop().create_future()
        waiter = [arrival, (account, requester), future, arrival]
        self._waiting.append(waiter)
        self._grant()
        try:
            await future
        except BaseException:
            if waiter in self._waiting:
                self._waiting.remove(waiter)
                self._forget_if_idle(account)
            elif not future.cancelled():
                self.release(account, requester)
            raise

    def release(self, account: str, requester: Optional[str] = None) -> None:
        key = (account, requester)
        if not self._by_key[key]:
            raise ValueError(f"FairSlots released for {key!r}, which holds none")
        self._by_key[key] -= 1
        self._held[account] -= 1
        self._by_key += Counter()  # drops a key holding none
        self._held += Counter()
        self._free += 1
        self._forget_if_idle(account)
        self._grant()

    def _forget_if_idle(self, account: str) -> None:
        if not self._held[account] and all(w[1][0] != account for w in self._waiting):
            self._granted.pop(account, None)

    def _grant(self) -> None:
        while True:
            live = [w for w in self._waiting if not w[2].done()]
            place: Dict[str, int] = {}
            for w in live:
                place[w[1][0]] = min(place.get(w[1][0], w[3]), w[3])
            for account in sorted(place, key=lambda a: (self._held[a], place[a])):
                if self._free >= (2 if self._held[account] else 1):
                    mine = [w for w in live if w[1][0] == account]
                    granted = self._granted.setdefault(account, {})
                    waiter = min(
                        mine,
                        key=lambda w: (
                            self._by_key[w[1]],
                            granted.get(w[1][1], -1),
                            w[0],
                        ),
                    )
                    earliest = min(mine, key=lambda w: w[3])
                    earliest[3], waiter[3] = waiter[3], earliest[3]
                    self._waiting.remove(waiter)
                    self._free -= 1
                    self._held[account] += 1
                    self._by_key[waiter[1]] += 1
                    granted[waiter[1][1]] = next(self._order)
                    waiter[2].set_result(None)
                    break
            else:
                return


def admission_principal(principal: str) -> tuple[str, Optional[str]]:
    """``principal``'s ``FairSlots`` key: ``(principal, requester)`` when this
    context bound one for that account, so its requester takes turns with the
    account's others, else ``(principal, None)``; never one joined string, as a
    principal may hold a colon. Admission only: never an authorization check, a
    query, an ownership field or a stored row (#2852)."""
    from faultmaven.config.tenant_context import get_admission_requester

    bound = get_admission_requester()
    if bound is not None and bound[0] == principal:
        return principal, bound[1]
    return principal, None


def loop_slots(held: Optional[tuple], count: int) -> tuple:
    """``held`` if made on the running loop, else ``(loop, FairSlots(count))``."""
    import asyncio

    loop = asyncio.get_running_loop()
    if held is None or held[0] is not loop:
        held = (loop, FairSlots(count))
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
            raise WorkerOutputExceeded("the worker's output passed its limit") from e
        if not line.endswith(b"\n"):
            return None
        seen["received"] += len(line)
        if seen["received"] > output_limit_bytes:
            raise WorkerOutputExceeded("the worker's output passed its limit")
        try:
            frame = json.loads(line)
            if not isinstance(frame, dict):
                raise TypeError(type(frame).__name__)
            if "stage" in frame:
                seen["stage"] = frame["stage"]
            elif "failure" in frame:
                seen["failure"] = str(frame["failure"])
            elif "log" in frame:
                # The exception's frames, when sent, are their own field (#2888).
                name, level, message, *frames = frame["log"]
                extra = {"frames": frames[0]} if frames and frames[0] else None
                logging.getLogger(name).log(level, message, extra=extra)
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


#: The most this process holds, per byte of the output limit, while ``_parts``
#: reads an answer (#3122): its bytes (1), and the decode's 2-byte buffer (2)
#: widening into a 4-byte one (4) at an astral character, both alive while it
#: copies: 7 for an answer with no lone surrogate. One with a lone surrogate
#: holds 8: ``surrogatepass``'s ``UnicodeDecodeError`` copies the input (1)
#: while the decode runs. Tier 1 and preparation (whose ``json.loads`` decodes
#: its line the same way) can receive one, which their 12 and 13 cover; a
#: search cannot, its text decoded with ``errors="replace"``
#: (``_resolve_evidence_content``), nor a token count, whose answer is two
#: integers. Callers that parse the parts add theirs (``*_ANSWER_HOLD``); an
#: answer the worker's code cannot write is its defect, outside these. The
#: widths are CPython's, measured on 3.11.15 (the image's): 7.0 and 8.0.
PHASED_ANSWER_HOLD = 7


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
        raise WorkerOutputExceeded("the worker's output passed its limit")
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
    ``WorkerNotStarted`` or ``WorkerFailed``, the worker reaped first. Its
    stderr is drained and dropped, only its size logged (:func:`_reap`)."""
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
    seen: Dict[str, Any] = {"stage": None, "received": 0, "stderr_bytes": 0}
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            module,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_PACKAGE_ROOT,
            limit=output_limit_bytes,
        )
    except OSError as e:
        raise WorkerNotStarted(f"the worker could not start: {e!r}") from e
    drain = asyncio.ensure_future(_drain(proc.stderr, seen))
    if phases is not None:
        try:
            end = await _phased(proc, request_bytes, seen, output_limit_bytes, phases)
            seen["ended"] = end.result is not None
            return end
        finally:
            await _reap(proc, drain, seen, module)
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
                seen["ended"] = True
                return WorkerEnd(result=result, stage=seen["stage"])
            code = await proc.wait()
    except TimeoutError:
        if not deadline.expired():
            raise
        return WorkerEnd(timed_out=True, stage=seen["stage"])
    finally:
        await _reap(proc, drain, seen, module)
    return WorkerEnd(code=code, stage=seen["stage"])


async def _drain(stream: "asyncio.StreamReader", seen: Dict[str, Any]) -> None:
    """Count the worker's stderr, keeping none of it: a library's print or the
    interpreter's crash text is no log line (principles §7, rule 1; #2898)."""
    while chunk := await stream.read(_DRAIN_CHUNK):
        seen["stderr_bytes"] += len(chunk)


async def _reap(
    proc: "asyncio.subprocess.Process",
    drain: "asyncio.Future",
    seen: Dict[str, Any],
    module: str,
) -> None:
    """Kill the worker and wait until it is reaped, a cancellation meanwhile
    included, so a caller's slot frees only once it is dead. The stderr drain
    stops first, then the output is read to the end, unread: a wait alone never
    returns while a full pipe holds the worker open. Then its stderr's size and
    exit code are logged: at INFO when it ended with its result or exit 0 and
    wrote some, at WARNING when it ended any other way."""
    import asyncio

    running = proc.returncode is None
    if running:
        proc.kill()

    async def finish() -> None:
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)
        seen["stderr_bytes"] += len((await proc.communicate())[1] or b"")

    reaping = asyncio.ensure_future(finish())
    cancelled: Optional[asyncio.CancelledError] = None
    while not reaping.done():
        try:
            await asyncio.shield(reaping)
        except asyncio.CancelledError as e:
            cancelled = e
    reaping.result()
    abnormal = not seen.get("ended") if running else proc.returncode != 0
    if abnormal or seen["stderr_bytes"]:
        logging.getLogger(__name__).log(
            logging.WARNING if abnormal else logging.INFO,
            "Worker %s ended (exit %s) with %d bytes on its stderr, dropped",
            module,
            proc.returncode,
            seen["stderr_bytes"],
        )
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
        # Frames and class names, never the exception's text (#2888).
        logged = logged_exception(record.exc_info)
        frames = _clipped(exception_frames(*logged)) if logged else None
        message = _clipped(record.getMessage())
        self._send({"log": [record.name, record.levelno, message, frames]})

    def report_dropped(self) -> None:
        """Count the records past the cap, once: before the result, which the
        parent stops reading at."""
        if self._dropped:
            dropped, self._dropped = self._dropped, 0
            message = f"{dropped} more log records were not forwarded"
            self._send({"log": [__name__, logging.WARNING, message]})


def _bound(parent_pid: int, memory_bytes: int, cpu_seconds: int) -> None:
    """Die with the parent, raise ``oom_score_adj`` (first choice only where a
    container's OOM is not grouped), and cap CPU time and address space, with
    no core file. A missing ``prctl`` or ``oom_score_adj``
    is logged."""
    logger = logging.getLogger(__name__)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
            logger.warning("prctl(PR_SET_PDEATHSIG) failed: %s", ctypes.get_errno())
    except (OSError, AttributeError) as e:
        logger.warning(
            "prctl is unavailable, so this worker can outlive: %s",
            exception_log_name(e),
        )
    if os.getppid() != parent_pid:
        os._exit(1)  # the parent died before the death signal was armed
    try:
        Path("/proc/self/oom_score_adj").write_text("1000")
    except OSError as e:
        logger.warning("oom_score_adj is not settable: %s", exception_log_name(e))
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
    except BaseException as e:
        # pyo3's ``PanicException`` (a Rust library's panic, as its thread
        # pool failing to start under the address-space cap) is a
        # ``BaseException``; it is a failure like any other (#2851).
        if not isinstance(e, Exception) and type(e).__name__ != "PanicException":
            raise
        if phased:
            send({"failure": str(e)[:MAX_RECORD_CHARS]})
        frames = _clipped(exception_frames(e))
        send({"log": [__name__, logging.ERROR, f"the {label} failed", frames]})
        return 1
    return 0
