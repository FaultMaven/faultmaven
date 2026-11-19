"""The class name a line may hold where an exception's text may not: a plain
identifier, else ``"Exception"`` (principles §7, rule 4). It imports nothing
of the package, so ``import faultmaven`` reads it without loading
``faultmaven.utils``."""

import re
from typing import Any

# A plain ASCII identifier. ``str.isidentifier`` is wider (it accepts non-ASCII
# letters), and a class's ``__name__`` is whatever string its creator chose.
CLASS_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"


def class_name(error: Any) -> str:
    """The class name of ``error`` (an exception or its class), if a plain
    identifier, else ``"Exception"``."""
    name = getattr(error if isinstance(error, type) else type(error), "__name__", "")
    if isinstance(name, str) and re.fullmatch(CLASS_NAME_PATTERN, name):
        return name
    return "Exception"
