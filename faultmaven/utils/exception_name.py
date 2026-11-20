"""The name an exception is reported by wherever its text may not go.

An exception's text can carry a connection URI with its password, SQL with its
values, or an internal host or storage key. Its class name carries none of
that, so a failure reported to a case reader, or to the model whose answer a
case reader is served, names the class (#2072, #2332).

**The server log** follows principles §7, "What a log line may hold"
(``docs/architecture/core-architecture/architectural-design-principles.md``),
the one statement of what a line may hold and of the redactor under it. This
module supplies the names that rule uses: the class chain
(:func:`exception_class_chain`), a log message's name for an exception, the
chain with each first-party ``error_code`` (:func:`exception_log_name`, rule 1),
the frames without any message
(:func:`exception_frames`), and the exception a record's ``exc_info`` names
(:func:`logged_exception`).
"""

import re
import sys
import traceback
from types import TracebackType
from typing import Any, Optional, Tuple

from faultmaven._class_name import class_name
from faultmaven.exceptions import walk_cause_chain


def exception_class_name(exc: BaseException) -> str:
    """``exc``'s class name if it is a plain identifier, else ``"Exception"``."""
    return class_name(exc)


def exception_class_chain(exc: BaseException) -> str:
    """The class names of ``exc`` and of each ``__cause__`` ancestor
    (:func:`~faultmaven.exceptions.walk_cause_chain`: explicit links only,
    cycle-guarded), joined by ``" <- "``. For a log line that may hold no
    exception's text (#2587)."""
    return " <- ".join(exception_class_name(link) for link in walk_cause_chain(exc))


#: A first-party ``error_code`` a line may hold: an upper-case identifier, as
#: every code ``faultmaven`` defines is.
ERROR_CODE_PATTERN = r"[A-Z][A-Z0-9_]{0,63}"


def exception_log_name(exc: BaseException) -> str:
    """How a log message names ``exc`` (principles §7, rule 1: "A first-party
    exception contributes its class and its ``error_code`` ..., not its
    message"): :func:`exception_class_chain`, with each first-party link's code
    as ``Name[CODE]`` (``ServiceException <- LLMException[PROVIDER_AUTH_FAILED]``).

    A link is first-party when its class's module is under ``faultmaven``
    (``faultmaven_cloud`` is not). Its code is written only when it is exactly a
    ``str`` matching :data:`ERROR_CODE_PATTERN`: a code copied from a third
    party's exception, an enum, a ``str`` subclass or a long one is left off.
    Never raises (#2889)."""
    names = []
    try:
        for link in walk_cause_chain(exc):
            code = _first_party_code(link)
            name = exception_class_name(link)
            names.append(f"{name}[{code}]" if code else name)
    except Exception:  # a ``__cause__`` that raises ends the chain
        pass
    return " <- ".join(names) or exception_class_name(exc)


def _first_party_code(link: BaseException) -> Optional[str]:
    try:
        module = type(link).__module__
        if not isinstance(module, str) or module.split(".")[0] != "faultmaven":
            return None
        code = getattr(link, "error_code", None)
    except Exception:  # an ``error_code`` property that raises
        return None
    if type(code) is str and re.fullmatch(ERROR_CODE_PATTERN, code):
        return code
    return None


#: How far :func:`exception_frames` descends into an ``ExceptionGroup``, the caps
#: ``traceback`` applies: members shown per group, and groups nested.
GROUP_MEMBERS_SHOWN = 15
GROUP_DEPTH_SHOWN = 10


def exception_frames(
    exc: BaseException, head_traceback: Optional[TracebackType] = None
) -> str:
    """The traceback frames of ``exc`` and of each exception its traceback would
    show (the ``__cause__``, else an unsuppressed ``__context__``), each under
    its class name, with no exception's message or note: where a failure was
    raised, for a log line that may hold no exception's text (#2844).
    ``head_traceback``, when given, is rendered for ``exc`` in place of its
    ``__traceback__`` (an ``exc_info`` tuple's own, as ``traceback`` renders it).

    An ``ExceptionGroup``'s members follow its own frames, depth first, each
    header prefixed with its position (``[2] OSError:``): at most
    ``GROUP_MEMBERS_SHOWN`` per group and ``GROUP_DEPTH_SHOWN`` groups deep, the
    rest counted (``and 25 more exceptions``). A member already shown is listed
    as ``[2] OSError (shown above)``; a chain link already shown ends the chain
    (#2888)."""
    lines: list = []
    _add_frames(exc, "", 0, set(), lines, head_traceback)
    return "".join(lines)


def _add_frames(exc, prefix: str, depth: int, seen: set, lines: list, tb=None):
    link = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        lines.append(f"{prefix}{exception_class_name(link)}:\n")
        lines.extend(traceback.format_tb(tb or link.__traceback__))
        tb = None
        if isinstance(link, BaseExceptionGroup):
            shown = (
                link.exceptions[:GROUP_MEMBERS_SHOWN]
                if depth < GROUP_DEPTH_SHOWN
                else ()
            )
            for i, member in enumerate(shown, 1):
                if id(member) in seen:
                    name = exception_class_name(member)
                    lines.append(f"{prefix}[{i}] {name} (shown above)\n")
                else:
                    _add_frames(member, f"{prefix}[{i}] ", depth + 1, seen, lines)
            if len(link.exceptions) > len(shown):
                lines.append(
                    f"{prefix}and {len(link.exceptions) - len(shown)} more exceptions\n"
                )
        if link.__cause__ is not None or link.__suppress_context__:
            link = link.__cause__
        else:
            link = link.__context__


def logged_exception(
    exc_info: Any,
) -> Optional[Tuple[BaseException, Optional[TracebackType]]]:
    """The exception a log record's ``exc_info`` names, with the traceback to
    render for it, or ``None``: an exception is itself; a 3-tuple whose second
    item is an exception is that, with the tuple's traceback; any other truthy
    value is the exception being handled (``sys.exc_info()``). Never raises: a
    log call's argument is not checked by its caller (#2888)."""
    try:
        if isinstance(exc_info, BaseException):
            return exc_info, exc_info.__traceback__
        if (
            isinstance(exc_info, tuple)
            and len(exc_info) == 3
            and isinstance(exc_info[1], BaseException)
        ):
            tb = exc_info[2] if isinstance(exc_info[2], TracebackType) else None
            return exc_info[1], tb or exc_info[1].__traceback__
        if exc_info:
            _, error, tb = sys.exc_info()
            return (error, tb) if error is not None else None
    except Exception:  # a truth value that raises
        pass
    return None
