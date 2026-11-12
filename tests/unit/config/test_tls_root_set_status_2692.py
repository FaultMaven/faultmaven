"""``root_set_status`` reads the set a process runs on from its environment, so
a worker answers for itself (#2692, ``features.tls_root_set``).

The first two cases launch through ``run_server`` (``uvicorn.run`` patched) and
read the status in a ``spawn`` child, as a uvicorn worker is started. The rest
apply the set in this process, with its written file under the test's
``tmp_path``; ``tests/conftest.py`` restores the owned variables after each.
In force means the one file the variables name holds exactly the set its
sources give now; a source that cannot be read leaves it unverifiable.
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
        status = pool.apply(root_set_status)
    print("STATUS " + json.dumps([status.in_force, status.certificates, status.description]))


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
    in_force, count, description = _launched(tmp_path)

    assert in_force is True
    assert count == _host_set_count()
    assert description.endswith(
        f": {count} certificates, TLS_EXTRA_CA_FILE "
        f"{os.path.realpath(tmp_path / 'extra-ca.pem')}."
    )


def test_a_worker_missing_an_owned_variable_reports_it(tmp_path):
    in_force, count, description = _launched(tmp_path, "AWS_CA_BUNDLE")

    assert in_force is False
    assert count is None
    assert description == (
        "The TLS root set is not in force: "
        "the TLS root variables do not name one file."
    )


@pytest.fixture
def applied(tmp_path, monkeypatch):
    """The set applied in this process, with TLS_EXTRA_CA_FILE: its path."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("FAULTMAVEN_SKIP_DOTENV", "1")
    (tmp_path / "extra-ca.pem").write_bytes(EXTRA)
    monkeypatch.setenv("TLS_EXTRA_CA_FILE", str(tmp_path / "extra-ca.pem"))
    path = apply_root_set().path
    assert root_set_status().in_force is True
    return path


def _reason(status) -> str:
    assert status.in_force is False
    return status.description.removeprefix("The TLS root set is not in force: ")


def test_a_variable_naming_another_file_with_the_same_bytes_is_not_one_file(
    applied, tmp_path, monkeypatch
):
    """``apply_root_set`` would accept a byte copy and re-export its own path:
    after it, every variable names the set's file itself."""
    copy = tmp_path / "copy.pem"
    copy.write_bytes(Path(applied).read_bytes())
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(copy))

    assert _reason(root_set_status()) == "the TLS root variables do not name one file."


def test_an_altered_set_file_is_not_in_force(applied):
    """One CA written over the set: it loads, at the path every variable names."""
    from tests.unit.config.test_tls_root_set_2692 import _ca_pem

    Path(applied).write_bytes(_ca_pem("attacker CA"))

    status = root_set_status()

    assert _reason(status) == (
        f"{applied} is not the OS CA bundle plus TLS_EXTRA_CA_FILE now."
    )
    assert status.certificates == 1


def test_a_set_file_removed_under_the_process_does_not_load(applied):
    os.remove(applied)

    status = root_set_status()

    assert _reason(status) == f"the file they name, {applied}, does not load."
    assert status.certificates is None


def test_a_certificate_directory_with_a_hashed_entry_is_not_in_force(
    applied, tmp_path, monkeypatch
):
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "1a2b3c4d.0").write_bytes(EXTRA)
    monkeypatch.setenv(DIRECTORY_VARIABLE, str(tmp_path / "certs"))

    assert _reason(root_set_status()) == (
        f"{DIRECTORY_VARIABLE} is not a directory free of certificates."
    )


def test_a_changed_extra_file_since_the_start_is_not_in_force(applied, tmp_path):
    """The set is read once per start: a new CA takes effect at the next."""
    from tests.unit.config.test_tls_root_set_2692 import _ca_pem

    (tmp_path / "extra-ca.pem").write_bytes(EXTRA + _ca_pem("added later"))

    assert _reason(root_set_status()) == (
        f"{applied} is not the OS CA bundle plus TLS_EXTRA_CA_FILE now."
    )


def test_a_removed_extra_ca_source_cannot_be_verified(applied, tmp_path):
    """A rotated Secret removes the setting's resolved file while the set file
    the process started with is intact and still in use."""
    os.remove(tmp_path / "extra-ca.pem")

    status = root_set_status()

    count = _host_set_count()
    assert status.in_force is False
    assert status.certificates == count
    assert status.description == (
        "The TLS root set cannot be verified because its sources changed since "
        f"this process started: it keeps the file it started with, {applied} "
        f"({count} certificates), and a restart re-reads them."
    )


def _symlink(tmp_path, monkeypatch) -> None:
    (tmp_path / "link.pem").symlink_to(tmp_path / "extra-ca.pem")
    monkeypatch.setenv("TLS_EXTRA_CA_FILE", str(tmp_path / "link.pem"))


def _relative(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TLS_EXTRA_CA_FILE", "extra-ca.pem")


def _lowercase(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TLS_EXTRA_CA_FILE")
    monkeypatch.setenv("tls_extra_ca_file", str(tmp_path / "extra-ca.pem"))


@pytest.mark.parametrize("spelling", [_symlink, _relative, _lowercase])
def test_the_reader_writes_nothing(applied, tmp_path, monkeypatch, spelling):
    """``apply_root_set`` writes the setting back as one real path, and finds
    its temporary directory by writing a probe; the reader does neither. Each
    spelling names the same file, so the set is still in force."""
    spelling(tmp_path, monkeypatch)
    environment = dict(os.environ)
    entries = sorted(os.listdir(tmp_path))

    def probe():
        raise AssertionError("the reader looked up a temporary directory")

    monkeypatch.setattr(tempfile, "gettempdir", probe)

    assert root_set_status().in_force is True
    assert dict(os.environ) == environment
    assert sorted(os.listdir(tmp_path)) == entries
