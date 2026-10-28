"""A failed statement's exception text carries none of its values (#2377).

The values reach the text by three channels. SQLAlchemy's ``[parameters: …]``
suffix is closed by ``hide_parameters=True`` on every engine the app builds.
PostgreSQL's own report is the second: DETAIL ("Failing row contains (…)",
"Key (email)=(…)") and CONTEXT (every bound value under
``log_parameter_max_length_on_error``) reach the client, and the driver error,
SQLAlchemy's DBAPI wrapper and its exception each carry them. The third is the
primary message itself: a class-22 error quotes its input
(``invalid input syntax for type integer: "…"``, ``time zone "…" not
recognized``), and asyncpg's own encoding error repeats the argument
(``invalid input for query argument $1: '…'``).

:func:`strip_server_values` reduces the driver error to the fields it keeps,
the primary message with its input hidden and the hint, in place, and puts that
text where the full one stood. ``str()`` and a logged traceback then name no
value, while the class, the SQLSTATE, the constraint name, the argument number
and the chain down to the driver error stay as they were (``db_errors`` reads
them). A class-22 message is hidden from its first quote to its last, whatever
its shape, so a quoted type or parameter name goes with the input. asyncpg's
encoding error drops the codec's exception it was raised from, whose text
repeats the value, and names its class instead. psycopg2's ``pgerror``,
``diag`` and ``cursor.query`` keep the server's whole report: they are
read-only, and nothing prints or reads them.

A driver error is known by its class, looked up in ``sys.modules`` and never
imported (``alembic/env.py`` runs where only SQLite is installed): an error of a
driver no one loaded cannot exist. Any other exception is left as it is. The
driver error is the wrapper's ``__cause__``, or the original exception itself
when SQLAlchemy did not translate it (asyncpg at connect time).

Two properties of the hook, both accepted. A ``handle_error`` listener turns on
the dialect's events, so every DBAPI error builds the event context, with no
functional change. An error raised while SQLAlchemy is already handling one on
the same connection (``_reentrant_error``) skips listeners, so the guarantee is
for every first-order database error.
"""

import logging
import re
import sys

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.engine.interfaces import ExceptionContext

logger = logging.getLogger(__name__)

#: What stands where a value stood.
HIDDEN = "[value hidden]"

#: asyncpg's encoding error: the argument, then its value and the codec's words.
_ARGUMENT = re.compile(
    r"(?s)^((?:invalid input for )?query argument \$\d+"
    r"(?: in element #\d+ of executemany\(\) sequence)?): "
)


def install_value_free_errors(engine: Engine) -> None:
    """Keep the server's value-bearing fields out of every error ``engine`` raises."""
    event.listen(engine, "handle_error", strip_server_values)


def strip_server_values(ctx: ExceptionContext) -> None:
    """The ``handle_error`` listener. It never raises: an exception from a listener
    replaces the error being handled, and ``except IntegrityError`` would miss it."""
    try:
        original = ctx.original_exception
        for error in (original, getattr(original, "__cause__", None)):
            if error is None:
                continue
            full = str(error)
            if not _keep_message_and_hint(error):
                continue
            kept = str(error)
            for outer in (original, ctx.sqlalchemy_exception):
                if outer is not None and outer is not error and outer.args:
                    text = str(outer.args[0]).replace(full, kept)
                    outer.args = (text, *outer.args[1:])
    except Exception as failure:  # noqa: BLE001 - see the docstring
        logger.warning(
            "Could not strip values from a database error: %s",
            type(failure).__name__,
        )
    return None


def _keep_message_and_hint(error: BaseException) -> bool:
    """Reduce a driver's error to its primary message, input hidden, and hint."""
    asyncpg = sys.modules.get("asyncpg")
    psycopg2 = sys.modules.get("psycopg2")
    if asyncpg is not None and isinstance(
        error, (asyncpg.PostgresError, asyncpg.InterfaceError)
    ):
        error.detail = None  # its __str__ appends .detail and .hint to args[0]
        sqlstate = getattr(error, "sqlstate", None)
        if isinstance(error, asyncpg.PostgresError):
            error.context = error.internal_query = None
            if error.message is not None:
                error.message = _hide_input(error, error.message, sqlstate)
        error.args = (_hide_input(error, str(error.args[0]), sqlstate),)
        return True
    if psycopg2 is not None and isinstance(error, psycopg2.Error):
        primary = error.diag.message_primary
        if primary is None:
            return False
        hint = error.diag.message_hint
        primary = _hide_input(error, primary, error.pgcode)
        error.args = (primary + (f"\nHINT:  {hint}" if hint else "") + "\n",)
        return True
    return False


def _hide_input(error: BaseException, message: str, sqlstate: str | None) -> str:
    """``message`` with its input hidden, its argument number or unquoted text kept."""
    argument = _ARGUMENT.match(message)
    if argument:  # the codec's own error, its cause, says the value again
        codec, error.__cause__, error.__context__ = error.__cause__, None, None
        named = f" ({type(codec).__name__})" if codec is not None else ""
        return f"{argument.group(1)}: {HIDDEN}{named}"
    first, last = message.find('"'), message.rfind('"')
    if (sqlstate or "").startswith("22") and first >= 0:
        # Fails closed: PostgreSQL quotes the input without escaping a quote in
        # it, and the shapes vary, so everything from the first quote to the
        # last goes (to the end, when only one is left).
        end = last + 1 if last > first else len(message)
        return f"{message[:first]}{HIDDEN}{message[end:]}"
    return message
