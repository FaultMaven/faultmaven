"""``alembic upgrade head`` refuses a ``DATABASE_URL`` naming a Postgres root input (#2787).

libpq, the migration process's driver, loads a URL's ``sslcrl`` / ``sslcrldir``
as trusted roots, and a ``service`` (or a ``dsn`` carrying either) supplies
``sslrootcert`` ahead of the set. ``alembic/env.py`` refuses twice: at
``apply_root_set``, its first statement, and in ``get_database_url``, which the
second mode reaches by making ``apply_root_set`` a no-op in the child. The URL
is a SQLite file, so nothing connects anywhere: without the refusal the
migration would create it. A key counts by each token libpq would split it into
(``+sslcrl``, ``%20service``, ``application_name%3Dx%20sslcrl``). The child runs
without the caller's Postgres root-input variables and with its ``HOME`` in
``tmp_path``; a ``root.crl`` in the password-database home, which ``HOME`` does
not hide, skips the test rather than refuse for another reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from faultmaven.config.settings import set_env_var
from faultmaven.config.tls_root_set import PG_ROOT_INPUT_VARIABLES

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


QUERIES = {
    "service": ("service=fm", "service"),
    "plus": ("+sslcrl=/x", "sslcrl"),
    "encoded-space": ("%20service=x", "service"),
    "inside-another-key": ("application_name%3Dx%20sslcrl=/x", "sslcrl"),
}


def _passwd_home_crl() -> Path | None:
    try:
        import pwd
    except ImportError:
        return None
    crl = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".postgresql" / "root.crl"
    return crl if os.path.exists(crl) else None


@pytest.mark.parametrize(("query", "key"), QUERIES.values(), ids=QUERIES)
@pytest.mark.parametrize(
    ("no_op_root_set", "prefix"),
    [(False, "TLS root set: "), (True, "Refusing to run: ")],
    ids=["at-the-start", "in-get-database-url"],
)
def test_a_migration_refuses_a_url_naming_a_root_input(
    tmp_path, no_op_root_set, prefix, query, key
):
    if _passwd_home_crl() is not None:
        pytest.skip(f"{_passwd_home_crl()} exists: every start refuses it first")
    shutil.copy2(PROJECT_ROOT / "alembic.ini", tmp_path / "alembic.ini")
    shutil.copytree(
        PROJECT_ROOT / "alembic",
        tmp_path / "alembic",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    database = tmp_path / "fm.db"
    env = {k: v for k, v in os.environ.items() if k not in PG_ROOT_INPUT_VARIABLES}
    (tmp_path / "home").mkdir()
    env.update(FAULTMAVEN_SKIP_DOTENV="1", TMPDIR=str(tmp_path))
    env["HOME"] = str(tmp_path / "home")
    set_env_var(env, "DATABASE_URL", f"sqlite:///{database}?{query}")
    path = [str(PROJECT_ROOT)]
    if no_op_root_set:
        (tmp_path / "site").mkdir()
        (tmp_path / "site" / "sitecustomize.py").write_text(
            "import faultmaven.config.tls_root_set as tls_root_set\n"
            "tls_root_set.apply_root_set = lambda: None\n"
        )
        path.insert(0, str(tmp_path / "site"))
    env["PYTHONPATH"] = os.pathsep.join(path)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(tmp_path / "alembic.ini")]
        + ["upgrade", "head"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert result.returncode == 1, result.stdout + result.stderr
    refusal = f"{prefix}the database URL's query names {key},"
    assert refusal in result.stderr, result.stderr
    assert not database.exists()
