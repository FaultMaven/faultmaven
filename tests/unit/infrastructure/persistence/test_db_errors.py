"""Reading PostgreSQL's structured error fields out of a SQLAlchemy exception.

One helper, used wherever a caller must tell one database refusal from another
(the last-admin trigger, the case metadata read). These pin the two-hop shape
it reads and what it answers for anything else.
"""

import pytest
from sqlalchemy.exc import DBAPIError

from faultmaven.infrastructure.persistence.db_errors import driver_error, sqlstate

pytestmark = pytest.mark.unit


class _DriverError(Exception):
    def __init__(self, code=None):
        super().__init__("driver")
        self.sqlstate = code


def _wrapped(code=None) -> DBAPIError:
    """``DBAPIError.orig`` is the DBAPI-level exception; the driver error is its
    ``__cause__`` — the shape SQLAlchemy's asyncpg dialect raises."""
    orig = Exception("dbapi")
    orig.__cause__ = _DriverError(code)
    return DBAPIError("SELECT 1", {}, orig)


def test_reads_the_sqlstate_two_hops_down():
    exc = _wrapped("42501")
    assert sqlstate(exc) == "42501"
    assert isinstance(driver_error(exc), _DriverError)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("not a database error"),
        DBAPIError("SELECT 1", {}, Exception("no cause")),
        _wrapped(None),
    ],
)
def test_answers_none_for_anything_else(exc):
    assert sqlstate(exc) is None
