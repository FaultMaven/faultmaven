"""``root_set_status`` reads the set a process runs on from its environment, so
a worker answers for itself (#2692, ``features.tls_root_set``).

The first two cases launch through ``run_server`` (``uvicorn.run`` patched) and
read the status in a ``spawn`` child, as a uvicorn worker is started. The rest
apply the set in this process, with its written file under the test's
``tmp_path``; ``tests/conftest.py`` restores the owned variables after each.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from faultmaven.config import tls_root_set
from faultmaven.config.tls_root_set import (
    DIRECTORY_VARIABLE,
    apply_root_set,
    pem_digests,
    root_set_status,
)
from tests.unit.config.test_tls_root_set_2692 import EXTRA
from tests.unit.config.test_tls_root_set_launchers_2692 import _environment

pytestmark = pytest.mark.unit

_LAUNCHED = """
import json, multiprocessing, os, sys
from unittest import mock

from faultmaven.config.tls_root_set import root_set_status


def serve(app, **options):
    for name in sys.argv[1:]:
        del os.environ[name]
    with multiprocessing.get_context("spawn").Pool(1) as pool:
        root_set, problem = pool.apply(root_set_status)
    print("STATUS " + json.dumps([problem, root_set.certificates, root_set.extra_ca_file]))


if __name__ == "__main__":
    with mock.patch("uvicorn.run", serve):
        from faultmaven.server import run_server

        run_server()
"""


def _launched(tmp_path, *removed: str) -> list:
    (tmp_path / "extra-ca.pem").write_bytes(EXTRA)
    (tmp_path / "launched.py").write_text(_LAUNCHED)
    result = subprocess.run(
        [sys.executable, str(tmp_path / "launched.py"), *removed],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
        env=_environment(
            TLS_EXTRA_CA_FILE=str(tmp_path / "extra-ca.pem"), TMPDIR=str(tmp_path),
            RELOAD="false", WORKERS="1", CONFIG_PRESET="none",
            SKIP_SERVICE_CHECKS="true",
        ),
    )  # fmt: skip
    line = next(
        (x for x in result.stdout.splitlines() if x.startswith("STATUS ")), None
    )
    assert line, result.stdout[-2000:] + result.stderr[-4000:]
    return json.loads(line.removeprefix("STATUS "))


def _host_set_count() -> int:
    return len(pem_digests(tls_root_set._resolve_os_bundle()[1] + b"\n" + EXTRA))


def test_a_worker_of_a_launched_process_reports_the_set_with_its_count(tmp_path):
    problem, count, extra = _launched(tmp_path)

    assert problem is None
    assert count == _host_set_count()
    assert extra == os.path.realpath(tmp_path / "extra-ca.pem")


def test_a_worker_missing_an_owned_variable_reports_it(tmp_path):
    problem, count, _ = _launched(tmp_path, "AWS_CA_BUNDLE")

    assert problem == "AWS_CA_BUNDLE does not name the set's file"
    assert count == _host_set_count()


@pytest.fixture
def applied(tmp_path, monkeypatch):
    """The set applied in this process, with TLS_EXTRA_CA_FILE: its path."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("FAULTMAVEN_SKIP_DOTENV", "1")
    (tmp_path / "extra-ca.pem").write_bytes(EXTRA)
    monkeypatch.setenv("TLS_EXTRA_CA_FILE", str(tmp_path / "extra-ca.pem"))
    path = apply_root_set().path
    assert root_set_status()[1] is None
    return path


def test_a_variable_naming_another_file_with_the_same_bytes_is_not_the_set(
    applied, tmp_path, monkeypatch
):
    """``apply_root_set`` would accept a byte copy and re-export its own path:
    after it, every variable names the set's file itself."""
    copy = tmp_path / "copy.pem"
    copy.write_bytes(Path(applied).read_bytes())
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(copy))

    assert root_set_status()[1] == "REQUESTS_CA_BUNDLE does not name the set's file"


def test_a_set_file_removed_under_the_process_does_not_load(applied):
    os.remove(applied)

    assert root_set_status()[1] == f"the set's file {applied} does not load"


def test_a_certificate_directory_with_a_hashed_entry_is_not_the_set(
    applied, tmp_path, monkeypatch
):
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "1a2b3c4d.0").write_bytes(EXTRA)
    monkeypatch.setenv(DIRECTORY_VARIABLE, str(tmp_path / "certs"))

    assert root_set_status()[1] == (
        f"{DIRECTORY_VARIABLE} is not a directory free of certificates"
    )


def test_a_changed_extra_file_since_the_start_is_not_the_set(applied, tmp_path):
    """The set is read once per start: a new CA takes effect at the next."""
    from tests.unit.config.test_tls_root_set_2692 import _ca_pem

    (tmp_path / "extra-ca.pem").write_bytes(EXTRA + _ca_pem("added later"))

    assert root_set_status()[1] == "SSL_CERT_FILE does not name the set's file"


def test_a_set_that_cannot_be_computed_reports_the_refusal(applied, tmp_path):
    (tmp_path / "extra-ca.pem").write_bytes(b"not a certificate\n")

    root_set, problem = root_set_status()

    assert root_set is None
    assert problem.startswith("TLS root set: TLS_EXTRA_CA_FILE=")


def test_the_reader_writes_nothing(applied):
    """``apply_root_set`` re-writes a missing set; the reader must not."""
    os.remove(applied)
    environment = dict(os.environ)

    root_set_status()

    assert not os.path.exists(applied)
    assert dict(os.environ) == environment
