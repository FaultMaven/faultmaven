"""The preparation worker: one document's Stages 0-5 in its own process (#2622).

Run as ``python -m`` this module by ``document_preprocessor``, one process per
job, which kills it at the deadline (Principle 14). It reads one request line
on stdin and writes frames on stdout, one JSON object a line, in order:
``{"stage": n}`` as each stage starts, ``{"log": [logger, level, message]}``
for each log record, and last ``{"result": ...}``. The parent emits the
records under its own request context, so the worker imports no settings.

Before it reads the file the worker bounds itself: it dies with its parent,
volunteers for the OOM killer, and sets its CPU and address-space limits. An
allocation past the address-space limit exits with ``OUT_OF_MEMORY_EXIT``.
"""

import ctypes
import importlib
import json
import logging
import os
import resource
import signal
import sys
import traceback
from pathlib import Path
from typing import Any, BinaryIO, Dict

#: The exit code of a worker an allocation past its memory limit stopped.
OUT_OF_MEMORY_EXIT = 3

#: Log records forwarded per job, and characters per record (its head and its
#: tail); past the count, one record counts the rest. So a file that makes a
#: parser warn on every object cannot fill the parent's output limit.
MAX_FORWARDED_RECORDS = 50
MAX_RECORD_CHARS = 2000


def _clipped(message: str) -> str:
    if len(message) <= MAX_RECORD_CHARS:
        return message
    half = MAX_RECORD_CHARS // 2
    return f"{message[:half]} [...] {message[-half:]}"


_PR_SET_PDEATHSIG = 1


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


def _run(request: Dict[str, Any], send, forward: _Forward) -> None:
    """Import the stage the request names, bound this process, and run it."""
    module_name, _, function_name = request["stage"].partition(":")
    stage = getattr(importlib.import_module(module_name), function_name)
    from pypdf import Configuration, apply_configuration

    _bound(request["parent_pid"], request["memory_bytes"], request["cpu_seconds"])
    with apply_configuration(Configuration(**request["pdf_limits"])):
        result = stage(
            Path(request["file_path"]),
            request["content_type"],
            lambda n: send({"stage": n}),
        )
    forward.report_dropped()
    send({"result": result})


def main() -> int:
    """One job: the request from stdin, the frames to stdout."""
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
            _run(request, send, forward)
        finally:
            forward.report_dropped()
    except MemoryError:
        os._exit(OUT_OF_MEMORY_EXIT)
    except Exception:
        failure = "the preparation failed\n" + traceback.format_exc()
        send({"log": [__name__, logging.ERROR, _clipped(failure)]})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
