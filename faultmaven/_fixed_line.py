"""The fixed line a process writes where a record cannot go through the log
chain: a literal, a class name and a logger's name, nothing from a record
(principles §7, rules 4 and 5; #2898). It imports the standard library and
``_class_name`` alone: ``logging.lastResort``'s handler uses it before the
chain is imported."""

import json
import sys
from typing import Any, Optional

from faultmaven._class_name import class_name


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
