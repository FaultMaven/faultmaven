"""The name an exception is reported by wherever its text may not go.

An exception's text can carry a connection URI with its password, SQL with its
values, or an internal host or storage key. Its class name carries none of
that, so a failure reported to a case reader, or to the model whose answer a
case reader is served, names the class (#2072, #2332).

**The server log** follows principles §7, "What a log line may hold"
(``docs/architecture/core-architecture/architectural-design-principles.md``),
the one statement of what a line may hold and of the redactor under it. This
module supplies the names that rule uses: the class chain
(:func:`exception_class_chain`) and the frames without any message
(:func:`exception_frames`).
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


#: How far :func:`exception_frames` descends into an ``ExceptionGroup``, the caps
#: ``traceback`` applies: members shown per group, and groups nested.
GROUP_MEMBERS_SHOWN = 15
GROUP_DEPTH_SHOWN = 10


def exception_frames(exc: BaseException) -> str:
    """The traceback frames of ``exc`` and of each exception its traceback would
    show (the ``__cause__``, else an unsuppressed ``__context__``), each under
    its class name, with no exception's message or note: where a failure was
    raised, for a log line that may hold no exception's text (#2844).

    An ``ExceptionGroup``'s members follow its own frames, depth first, each
    header prefixed with its position (``[2] OSError:``): at most
    ``GROUP_MEMBERS_SHOWN`` per group and ``GROUP_DEPTH_SHOWN`` groups deep, the
    rest counted (``and 25 more exceptions``). An exception reached twice is
    shown once (#2888)."""
    lines: list = []
    _add_frames(exc, "", 0, set(), lines)
    return "".join(lines)


def _add_frames(exc: BaseException, prefix: str, depth: int, seen: set, lines: list):
    link = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        lines.append(f"{prefix}{exception_class_name(link)}:\n")
        lines.extend(traceback.format_tb(link.__traceback__))
        if isinstance(link, BaseExceptionGroup):
            shown = (
                link.exceptions[:GROUP_MEMBERS_SHOWN]
                if depth < GROUP_DEPTH_SHOWN
                else ()
            )
            for i, member in enumerate(shown, 1):
                _add_frames(member, f"{prefix}[{i}] ", depth + 1, seen, lines)
            if len(link.exceptions) > len(shown):
                lines.append(
                    f"{prefix}and {len(link.exceptions) - len(shown)} more exceptions\n"
                )
        if link.__cause__ is not None or link.__suppress_context__:
            link = link.__cause__
        else:
            link = link.__context__
