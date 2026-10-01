"""Reading the database's own error out of a SQLAlchemy exception.

Callers that must tell one database refusal from another — a constraint
trigger from any other check violation, a missing function from a missing
privilege — identify it by the structured fields PostgreSQL sends (SQLSTATE,
constraint name), never by message text: messages are localized and reworded
between releases, and matching on the error class alone would swallow every
other error of that class.

The two hops are the whole of the rule, stated once: ``DBAPIError.orig`` is
SQLAlchemy's DBAPI-level wrapper, and the driver exception carrying the fields
is ``orig.__cause__``.
"""

from typing import Optional

from sqlalchemy.exc import DBAPIError


def driver_error(exc: BaseException) -> Optional[BaseException]:
    """The driver's exception behind ``exc``, or ``None`` when ``exc`` is not a
    SQLAlchemy ``DBAPIError`` (including on SQLite, whose driver sends no
    SQLSTATE)."""
    if not isinstance(exc, DBAPIError):
        return None
    return getattr(exc.orig, "__cause__", None)


def sqlstate(exc: BaseException) -> Optional[str]:
    """The five-character SQLSTATE PostgreSQL sent, or ``None``."""
    return getattr(driver_error(exc), "sqlstate", None)
