"""The name an exception is reported by wherever its text may not go.

An exception's text can carry a connection URI with its password, SQL with its
values, or an internal host or storage key. Its class name carries none of
that, so a failure reported to a case reader, or to the model whose answer a
case reader is served, names the class (#2072, #2332).

**The server log follows the same rule (#2844).** A log line never holds the
text of an exception whose message can carry a third party's output: a
client's, a backend's or a store's. It names the class chain
(:func:`exception_class_chain`), and where the frames matter it adds them
without the messages (:func:`exception_frames`). The rendered-line redactor
(``infrastructure/logging/url_redaction.py``), which strips URL query strings
and blanks credential header values (``cookie``, ``set-cookie``,
``authorization``, ``proxy-authorization``), is defence in depth under this
rule, not a licence to log text.
"""

import re
import traceback

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


def exception_frames(exc: BaseException) -> str:
    """The traceback frames of ``exc`` and of each exception its traceback would
    show (the ``__cause__``, else an unsuppressed ``__context__``,
    cycle-guarded), each under its class name, with no exception's message:
    where a failure was raised, for a log line that may hold no exception's
    text (#2844). It does not descend into an ``ExceptionGroup``'s member
    exceptions: a member's frames appear only if it is also a cause or
    context in the chain."""
    frames, link, seen = [], exc, set()
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        frames.append(f"{exception_class_name(link)}:\n")
        frames.extend(traceback.format_tb(link.__traceback__))
        if link.__cause__ is not None or link.__suppress_context__:
            link = link.__cause__
        else:
            link = link.__context__
    return "".join(frames)
