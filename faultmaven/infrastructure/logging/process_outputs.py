"""The outputs a process opens itself, each through the chain or as a fixed line
(principles §7, rule 5; #2888 S2b, #2898): a record a handler cannot render
(:class:`ChainStreamHandler`), the uncaught-exception hooks, ``warnings``,
uvicorn's own handlers, asyncio's reprs and Starlette's pasted traceback, and a
third party's own handlers. ``logging.lastResort`` is the package's own.

:func:`install_process_outputs` runs at process entry and in the API worker's
chain; every step is idempotent, and a hook is replaced only while it is the
interpreter's default, so a test runner's or a debugger's stands. It installs no
root handler. :func:`sweep_own_handlers` runs at process entry, after the
embedding model loads and at the end of startup, never from the chain's
re-configuration.
"""

import logging
import sys
import threading
import warnings
from typing import Any

from faultmaven._class_name import class_name
from faultmaven._fixed_line import write_fixed_line

logger = logging.getLogger(__name__)

#: Its lines stay off, as ``access_log=False`` keeps them: never propagated.
_ACCESS_LOGGER = "uvicorn.access"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", _ACCESS_LOGGER)
_CALLBACK = "Exception in callback"


class _FirstLine(logging.Filter):
    """Keeps a message's first line. On ``uvicorn.error`` that drops the
    traceback Starlette pastes into a lifespan failure; on ``asyncio``, the
    ``future:`` and ``handle:`` reprs, and a callback's repr from the first."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - the handler's handleError owns it
            return True
        first = message.split("\n", 1)[0]
        if first.startswith(_CALLBACK):
            first = _CALLBACK
        if first != message:
            record.msg, record.args = first, None
        return True


_FIRST_LINE = _FirstLine()


class ChainStreamHandler(logging.StreamHandler):
    """A handler of the chain. A record it cannot render (a key or a value the
    JSON encoder refuses, a repr that raises) is one fixed line naming the
    failure's class and the logger, never the record's message or arguments,
    which ``logging``'s ``handleError`` writes raw (#2898)."""

    def handleError(self, record: logging.LogRecord) -> None:
        name = getattr(record, "name", None)
        write_fixed_line("log record not rendered", sys.exc_info()[1], name)


def _log(level: int, message: str, exc_info: tuple) -> None:
    try:
        logger.log(level, message, exc_info=exc_info)
    except BaseException:  # noqa: BLE001 - a hook must not raise
        write_fixed_line(message, exc_info[0])


def _excepthook(exc_type: Any, exc: Any, tb: Any) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _log(logging.CRITICAL, "Uncaught exception", (exc_type, exc, tb))


def _thread_excepthook(args: Any) -> None:
    if not issubclass(args.exc_type, SystemExit):
        exc_info = (args.exc_type, args.exc_value, args.exc_traceback)
        _log(logging.ERROR, "Uncaught exception in a thread", exc_info)


def _unraisablehook(unraisable: Any) -> None:
    # Never ``unraisable.object``: its repr is a value.
    exc_info = (unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback)
    _log(logging.ERROR, "Unraisable exception", exc_info)


def _stock_warning_output() -> bool:
    """Whether a warning would be printed: ``catch_warnings(record=True)``
    replaces the writer, and then the warning is recorded instead."""
    impl = getattr(warnings, "_showwarnmsg_impl", None)
    return getattr(impl, "__module__", None) == "warnings"


def _showwarning(message, category, filename, lineno, file=None, line=None):
    """A warning as its class, file and line, never its message."""
    if file is not None or not _stock_warning_output():
        warnings._showwarning_orig(  # type: ignore[attr-defined]
            message, category, filename, lineno, file, line
        )
        return
    try:
        extra = {"warning_file": filename, "warning_line": lineno}
        logger.warning("%s", class_name(category), extra=extra)
    except Exception as error:  # noqa: BLE001
        write_fixed_line("warning not rendered", error)


def _strip(target: logging.Logger) -> None:
    """Remove ``target``'s handlers but the chain's and ``NullHandler``s; it
    then propagates to the chain, ``uvicorn.access`` excepted."""
    foreign = [
        handler
        for handler in target.handlers
        if not isinstance(handler, (logging.NullHandler, ChainStreamHandler))
    ]
    for handler in foreign:
        target.removeHandler(handler)
    if foreign and target.name != _ACCESS_LOGGER:
        target.propagate = True


def install_process_outputs() -> None:
    """Send the interpreter's own outputs through the chain (see the module).
    ``logging.lastResort`` is the package's (``faultmaven/_last_resort.py``)."""
    if sys.excepthook is sys.__excepthook__:
        sys.excepthook = _excepthook
    if threading.excepthook is threading.__excepthook__:
        threading.excepthook = _thread_excepthook
    if sys.unraisablehook is sys.__unraisablehook__:
        sys.unraisablehook = _unraisablehook
    if warnings.showwarning is warnings._showwarning_orig:  # type: ignore[attr-defined]
        warnings.showwarning = _showwarning
    for name in _UVICORN_LOGGERS:
        _strip(logging.getLogger(name))
    for name in ("uvicorn.error", "asyncio"):
        if _FIRST_LINE not in logging.getLogger(name).filters:
            logging.getLogger(name).addFilter(_FIRST_LINE)


def strip_own_handlers(*names: str) -> None:
    """Strip the named loggers' own handlers, as :func:`sweep_own_handlers`
    does every logger's: where a library that adds them is imported."""
    for name in names:
        _strip(logging.getLogger(name))


def sweep_own_handlers() -> None:
    """Strip every logger's own handler (SQLAlchemy's, transformers',
    ``huggingface_hub``'s, ``torch``'s), so its records reach the chain."""
    for candidate in list(logging.root.manager.loggerDict.values()):
        if isinstance(candidate, logging.Logger):
            _strip(candidate)
