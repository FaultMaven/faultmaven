"""The fixed line a process writes where a record cannot go through the log
chain: a literal, a class name and a logger's name, nothing from a record
(principles §7, rules 4 and 5; #2898). Standard library only: the package's
``lastResort`` handler uses it before the chain is imported."""

import json
import re
import sys
from typing import Any, Optional

from faultmaven.utils.exception_name import CLASS_NAME_PATTERN


def class_name(error: Any) -> str:
    """The class name of ``error`` (an exception or its class), if a plain
    identifier, else ``"Exception"``."""
    name = getattr(error if isinstance(error, type) else type(error), "__name__", "")
    return (
        name
        if isinstance(name, str) and re.fullmatch(CLASS_NAME_PATTERN, name)
        else "Exception"
    )


def write_fixed_line(
    event: str, error: Any = None, logger: Optional[str] = None
) -> None:
    """One JSON line on stderr: ``event`` (a literal), ``error``'s class name
    and a logger's name, nothing from a record. Never raises."""
    try:
        line = {"event": event, "level": "error"}
        if error is not None:
            line["error"] = class_name(error)
        if isinstance(logger, str):
            line["logger"] = logger
        sys.stderr.write(json.dumps(line) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - the last line a process has
        pass
