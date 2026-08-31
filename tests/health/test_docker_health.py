"""Infrastructure smoke check against the database a deployment configures.

This verifies that the PostgreSQL a deployment *says* it has is actually
reachable and answering. It is a smoke test: its whole value is failing when
the infrastructure is down, so its skip guard deliberately asks "has this
environment been told where the database is?" and never "is the database up?"
— gating on reachability would make it a tautology that can only run when it is
already going to pass.

Until #1257 all three tests in this file carried
``@pytest.mark.skipif(condition=True, ...)``. A literal condition evaluates
nothing, so none of them had ever run, in CI or anywhere else, and the
endpoints they hardcoded (``redis.faultmaven.local``, a ``faultmaven_dev``
database on localhost) appear nowhere else in the repo.

Two of the three were dropped rather than repaired:

* ``test_docker_compose_services`` asserted the reachability of Redis and
  PostgreSQL *as Docker Compose services*, and this repo's
  ``docker-compose.yml`` defines neither — it runs the API and Dashboard, with
  Redis in-process as FakeRedis.
* ``test_redis_connection`` was strictly weaker than a check that already
  gates the same job. ``scripts/check_redis_pairing.py`` runs as a step before
  the Test Cloud suite; it resolves Redis through the app's own
  ``get_async_redis_client()`` and **fails on a silent FakeRedis fallback**,
  which is fm#1031 and the failure that actually happens. A test that builds
  ``redis.Redis(host=...)`` by hand cannot see that fallback at all — it would
  pass green in exactly the scenario the script exists to catch — and its
  set/get round-trip is a subset of what the script already does. Keeping it
  would have been coverage theatre.
"""

import os

import pytest
from sqlalchemy import create_engine, make_url, text

pytestmark = pytest.mark.cloud

# A Postgres smoke check is only meaningful where a Postgres is configured.
# Same condition the `postgres` marker documents.
_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_POSTGRES_CONFIGURED = _DATABASE_URL.startswith("postgresql")


@pytest.mark.postgres
@pytest.mark.skipif(
    not _POSTGRES_CONFIGURED,
    reason="DATABASE_URL is not a PostgreSQL URL - no Postgres configured",
)
def test_postgres_connection():
    """The configured PostgreSQL accepts a connection and answers a query.

    Marked ``postgres`` so it is SELECTED by the Test PostgreSQL Integration
    job, which is the only job that configures a Postgres. Without that marker
    the gate below is true in every job that selects the test and false in the
    only job that could satisfy it — a guard that evaluates but never opens,
    which is #1257 respelled with an environment variable. Review caught it
    where the guard could not: the postgres job's own anti-silent-skip check
    greps the summary for "N skipped", and a DESELECTED test produces no such
    line, so deselection is invisible to it.
    """
    # DATABASE_URL carries the app's async driver (postgresql+asyncpg://).
    # This check is synchronous, so drop the driver and let SQLAlchemy pick the
    # sync default rather than handing asyncpg to create_engine().
    url = make_url(_DATABASE_URL).set(drivername="postgresql")

    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    except Exception as e:
        pytest.fail(f"PostgreSQL connection failed: {e}")
    finally:
        engine.dispose()
