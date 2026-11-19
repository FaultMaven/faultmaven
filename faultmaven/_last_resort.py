"""``logging.lastResort`` through the log chain (principles §7, rule 5; #2888).

The package's ``__init__`` installs the handler, so every process that imports
``faultmaven`` has it, a uvicorn supervisor and a worker child included. This
module imports the standard library and ``fixed_line`` (standard library
only), so ``import faultmaven`` stays cheap; the chain is imported on the first
record that reaches it.
"""

import logging
import sys
import threading
from typing import Any

from faultmaven.utils.fixed_line import write_fixed_line

_rendering = threading.local()


class LastResortHandler(logging.Handler):
    """Renders a record no handler took through the chain's JSON formatter on
    stderr, at WARNING as the standard library's does. A record logged while it
    renders, or a render that fails, is a fixed line."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self._formatter: Any = None

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_rendering, "active", False):
            write_fixed_line("log record logged while rendering", logger=record.name)
            return
        _rendering.active = True
        try:
            if self._formatter is None:
                from faultmaven.infrastructure.logging.config import (
                    build_log_formatter,
                )

                self._formatter = build_log_formatter("json", False, sys.stderr)
            sys.stderr.write(self._formatter.format(record) + "\n")
            sys.stderr.flush()
        except Exception as error:  # noqa: BLE001
            write_fixed_line("log record not rendered", error, record.name)
        finally:
            _rendering.active = False


def install_last_resort() -> None:
    """Make ``logging.lastResort`` a :class:`LastResortHandler`, while it is the
    standard library's own: one a test runner set stands."""
    if logging.lastResort is logging._defaultLastResort:  # type: ignore[attr-defined]
        logging.lastResort = LastResortHandler()
