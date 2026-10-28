"""A failed statement's exception text carries none of its values (#2377).

The values reach the text by two channels. SQLAlchemy's ``[parameters: …]``
suffix is closed by ``hide_parameters=True`` on every engine the app builds.
The other is PostgreSQL's own report: DETAIL ("Failing row contains (…)",
"Key (email)=(…)") and CONTEXT (every bound value under
``log_parameter_max_length_on_error``) reach the client, and the driver error,
SQLAlchemy's DBAPI wrapper and its exception each carry them in their text.
:func:`strip_server_values` reduces the driver error to the fields it keeps,
the primary message and the hint, in place, and puts that text where the full
one stood. ``str()`` and a logged traceback then name no value, while the
class, the SQLSTATE, the constraint name and the cause chain stay as they were
(``db_errors`` reads them).

No driver is imported (``alembic/env.py`` runs where only SQLite is installed):
asyncpg's error is known by its ``detail``, which its ``__str__`` appends, and
psycopg2's by ``diag``, whose ``args[0]`` is libpq's whole report. Either is the
wrapper's ``__cause__``, or the original exception itself when SQLAlchemy did
not translate it (asyncpg at connect time).

Two properties of the hook, both accepted. A ``handle_error`` listener turns on
the dialect's events, so every DBAPI error builds the event context, with no
functional change. An error raised while SQLAlchemy is already handling one on
the same connection (``_reentrant_error``) skips listeners, so the guarantee is
for every first-order database error.
"""

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.engine.interfaces import ExceptionContext


def install_value_free_errors(engine: Engine) -> None:
    """Keep the server's value-bearing fields out of every error ``engine`` raises."""
    event.listen(engine, "handle_error", strip_server_values)


def strip_server_values(ctx: ExceptionContext) -> None:
    """The ``handle_error`` listener. It never raises: an exception from a listener
    replaces the error being handled, and ``except IntegrityError`` would miss it."""
    try:
        original = ctx.original_exception
        for error in (original, getattr(original, "__cause__", None)):
            full = str(error) if error is not None else ""
            if not full or not _keep_message_and_hint(error):
                continue
            kept = str(error)
            for outer in (original, ctx.sqlalchemy_exception):
                if outer is not None and outer is not error and outer.args:
                    text = str(outer.args[0]).replace(full, kept)
                    outer.args = (text, *outer.args[1:])
    except Exception:  # noqa: BLE001 - see the docstring
        return None
    return None


def _keep_message_and_hint(error: BaseException) -> bool:
    """Reduce a PostgreSQL driver's error to its primary message and hint."""
    diag = getattr(error, "diag", None)
    primary = getattr(diag, "message_primary", None)
    if primary is not None:  # psycopg2, whose args[0] has no severity prefix
        hint = getattr(diag, "message_hint", None)
        error.args = (primary + (f"\nHINT:  {hint}" if hint else "") + "\n",)
        return True
    if hasattr(error, "detail"):  # asyncpg
        error.detail = None
        error.context = None
        return True
    return False
