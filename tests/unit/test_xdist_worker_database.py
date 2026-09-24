"""An xdist worker's database survives a test that empties the environment (#1636).

The root conftest gives each worker its own SQLite file. Set only as
``DATABASE_URL`` it was lost by any test that cleared or pinned the
environment and rebuilt settings -- the rebuilt object fell back to the
shipped ``./data/faultmaven.db``, the file every worker shares. So on a worker
it is also the settings field's default. This pins that, the way those tests
actually do it.

Only meaningful on a worker; a serial run has no per-worker file to lose.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from tests.conftest import WORKER_DATABASE_URL_ENV

pytestmark = pytest.mark.unit

SHARED_DEFAULT = "sqlite+aiosqlite:///./data/faultmaven.db"


def _worker_url() -> str:
    url = os.environ.get(WORKER_DATABASE_URL_ENV)
    if not url:
        pytest.skip("not an xdist worker with a per-worker database")
    return url


@pytest.mark.parametrize(
    "environment",
    [{}, {"CHAT_PROVIDER": "openai", "REDIS_HOST": "localhost"}],
    ids=["emptied", "pinned"],
)
def test_a_cleared_environment_still_resolves_the_worker_database(environment):
    from faultmaven.config.settings import get_settings, reset_settings

    worker_url = _worker_url()
    assert worker_url != SHARED_DEFAULT
    try:
        with patch.dict(os.environ, environment, clear=True):
            reset_settings()
            assert get_settings().database.database_url == worker_url
    finally:
        reset_settings()
