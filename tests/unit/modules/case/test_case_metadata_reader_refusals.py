"""Which refusal the cross-enterprise read got, asked of the database.

SQLSTATE 42501 is not by itself "EXECUTE is missing": under the functions'
``row_security = off`` a read row-level security would filter raises it too, as
does a missing schema privilege. The reader asks ``has_function_privilege``
rather than reading the (localized) message. Both branches are exercised on a
real PostgreSQL in ``tests/integration/security/test_admin_case_metadata_postgres.py``;
this pins the classification itself without one.
"""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import DBAPIError

from faultmaven.modules.case.domain.models.metadata import (
    CaseMetadataNotGrantedError,
    CaseMetadataRefusedError,
    CaseMetadataUnavailableError,
)
from faultmaven.modules.case.infrastructure.case_metadata_reader import (
    PostgreSQLCaseMetadataReader,
)

pytestmark = pytest.mark.unit


class _DriverError(Exception):
    def __init__(self, code):
        super().__init__(f"database says {code}")
        self.sqlstate = code


def _refusal(code) -> DBAPIError:
    orig = Exception("dbapi")
    orig.__cause__ = _DriverError(code)
    return DBAPIError("SELECT admin_case_metadata_count(...)", {}, orig)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _Session:
    """Refuses the read with ``code``; answers the privilege probe with
    ``may_execute``."""

    def __init__(self, code, may_execute):
        self.code = code
        self.may_execute = may_execute
        self.probed = False

    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def execute(self, statement, params=None):
        if "has_function_privilege" in str(statement):
            self.probed = True
            return _Result(self.may_execute)
        raise _refusal(self.code)


async def _read(session):
    await PostgreSQLCaseMetadataReader(session).list_case_metadata(
        state=None, source=None, limit=1, offset=0
    )


async def test_42501_without_execute_is_not_granted():
    session = _Session("42501", may_execute=False)
    with pytest.raises(CaseMetadataNotGrantedError):
        await _read(session)
    assert session.probed


async def test_42501_with_execute_is_a_refusal_of_another_kind(caplog):
    session = _Session("42501", may_execute=True)
    with caplog.at_level("ERROR"):
        with pytest.raises(CaseMetadataRefusedError) as caught:
            await _read(session)
    assert not isinstance(caught.value, CaseMetadataNotGrantedError)
    # The database's own message, verbatim, for the operator.
    assert "database says 42501" in caplog.text


async def test_a_missing_function_is_unavailable_without_a_probe():
    session = _Session("42883", may_execute=True)
    with pytest.raises(CaseMetadataUnavailableError) as caught:
        await _read(session)
    assert type(caught.value) is CaseMetadataUnavailableError
    assert not session.probed


async def test_any_other_database_error_propagates():
    with pytest.raises(DBAPIError):
        await _read(_Session("57014", may_execute=True))
