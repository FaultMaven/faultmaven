"""The name an exception is reported by wherever its text may not go.

An exception's text can carry a connection URI with its password, SQL with its
values, or an internal host or storage key. Its class name carries none of
that, so a failure reported to a case reader, or to the model whose answer a
case reader is served, names the class (#2072, #2332). So do the case delete
path's server log lines (#2587); the rule for the server log in general is
pending #2844.
"""

import re

from faultmaven.exceptions import walk_cause_chain

# A plain ASCII identifier. ``str.isidentifier`` is wider (it accepts non-ASCII
# letters), and a class's ``__name__`` is whatever string its creator chose.
CLASS_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"


def exception_class_name(exc: BaseException) -> str:
    """``exc``'s class name if it is a plain identifier, else ``"Exception"``."""
    name = type(exc).__name__
    return name if re.fullmatch(CLASS_NAME_PATTERN, name) else "Exception"


def exception_class_chain(exc: BaseException) -> str:
    """The class names of ``exc`` and of each ``__cause__`` ancestor
    (:func:`~faultmaven.exceptions.walk_cause_chain`: explicit links only,
    cycle-guarded), joined by ``" <- "``. For a log line that may hold no
    exception's text (#2587)."""
    return " <- ".join(exception_class_name(link) for link in walk_cause_chain(exc))
