"""Persistent-database boot gate (fm#1647).

FaultMaven needs a persistent database. An empty ``DATABASE_URL``,
``:memory:`` or an in-memory SQLite spelling is not a supported deployment
shape: a standalone boot with one used to get through the
startup migration and then die three layers down, in the single-tenant
enterprise seed, as ``RuntimeError: Critical bootstrap failure`` wrapping a
SQLAlchemy URL parse error.

The rule is :func:`~faultmaven.config.settings.persistent_database_configured`,
the ONE predicate the store factories already key off (fm#1128). Those
factories still pick an in-memory store when it says False — unit tests
compose services without a database. What this module refuses is BOOTING a
process on that arm, so the refusal lives here, at boot, rather than in
settings validation, which tests construct freely.

Called from two boot paths, before either writes anything. The ``fm-*`` operator
CLIs initialise the container too and do NOT call it yet (#1659):

- the web lifespan (``faultmaven/main.py``), straight after the deployment
  coherence gate and before ``resolve_pseudonym_key`` (which creates the data
  directory and writes the standalone key file), and outside the
  test-environment skip, because no test boots the app without a database;
- the CronJob runner (``faultmaven/jobs/run.py``), which mirrors the coherence
  gate — without it a job fails in the container's bootstrap exactly the way
  the API used to.

Cloud never reaches the refusal in practice: deployment coherence already
requires PostgreSQL there. It is not special-cased, because the rule does not
depend on the mode.
"""

from __future__ import annotations

from typing import Any

from faultmaven.config.settings import persistent_database_configured

#: The shipped ``DATABASE_URL`` default, named in the refusal so the operator
#: is told the one-line fix rather than only what is wrong. Kept equal to the
#: settings field's default by a test, not by import: the field default is
#: rebound per worker by the test harness, and the message must name what ships.
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/faultmaven.db"


class NonPersistentDatabaseError(RuntimeError):
    """Raised at startup when ``DATABASE_URL`` configures no persistent database."""


def require_persistent_database(settings: Any) -> None:
    """Refuse to boot unless a persistent database is configured.

    Raises:
        NonPersistentDatabaseError: when ``persistent_database_configured``
            says False for ``settings.database.database_url``.
    """
    database_url = getattr(getattr(settings, "database", None), "database_url", None)
    if persistent_database_configured(database_url):
        return
    raise NonPersistentDatabaseError(
        f"DATABASE_URL={database_url!r} configures no persistent database. "
        "FaultMaven needs a database — standalone included: an empty value, "
        "':memory:' and in-memory SQLite URLs are not supported. Unset "
        f"DATABASE_URL to use the default local SQLite file ({DEFAULT_DATABASE_URL}), "
        "which needs no setup, or point it at a PostgreSQL database."
    )
