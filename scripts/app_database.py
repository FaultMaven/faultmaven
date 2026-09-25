"""The database the maintenance scripts connect to — the one the app uses.

FaultMaven keeps every table in ONE database, configured by ``DATABASE_URL``.
The scripts that open it directly (``check_duplicate_emails.py``,
``resolve_duplicate_emails.py``, ``backfill_closed_at_timestamps.py``) resolve
it here, the way the app and ``alembic/env.py`` do:

1. ``DATABASE_URL`` from the process environment;
2. otherwise ``DATABASE_URL`` from the project's ``.env`` (the process
   environment wins, as ``load_dotenv`` without override gives the app);
3. otherwise the shipped default, the local SQLite file ``data/faultmaven.db``,
   anchored at the project root so the result does not depend on the working
   directory.

The URL is returned as configured, async driver included: the scripts open it
with ``create_async_engine``, as the app does.

Imported by sibling scripts as ``from app_database import ...`` (a script's own
directory is first on ``sys.path``). Nothing here may have import side effects.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATABASE_URL = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'faultmaven.db'}"


def resolve_database_url(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> str:
    """Return the URL of the app's database.

    Args:
        environ: the process environment (defaults to ``os.environ``).
        env_file: the ``.env`` file consulted when ``environ`` has no
            ``DATABASE_URL`` (defaults to the project's ``.env``).
    """
    environ = os.environ if environ is None else environ
    env_file = ENV_FILE if env_file is None else env_file

    url = environ.get("DATABASE_URL")
    if not url and env_file.is_file():
        url = dotenv_values(env_file).get("DATABASE_URL")
    return url or DEFAULT_DATABASE_URL
