"""``scripts/check_pip_root_set.py``: a pip run ``--isolated`` or without
``PIP_CERT`` verifies against exactly the OS cafile's roots (#2756, #2759 pin 1).

The script runs against a temporary copy of the test interpreter's own ``pip``
put first on ``PYTHONPATH``, so the installed pip is never modified. With the
copy's vendored bundle as shipped it fails on both legs; with the bundle linked
to the OS cafile, as the image links it, it passes. Both legs resolve
``verify`` to True, the case requests 2.32.0-2.32.4 measure through no
``conn.ca_certs``; the fallback is exercised only on a pip vendoring requests
2.32.0-2.32.4, which neither this venv nor CI installs.

The script reaches pip's private API (``create_command``, ``_build_session``)
on whatever pip the environment holds, which is unpinned: CI upgrades pip, and
the dev venv may hold another version. The negative control holds only because
the host's OS root set differs from the bundle pip vendors.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_pip_root_set.py"
OS_CAFILE = ssl.get_default_verify_paths().openssl_cafile
#: A guard against a hung worker, not a speed assertion: two pip sessions.
BUDGET_SECONDS = 120
#: The first two would point pip at the OS cafile, SSL_CERT_FILE would move the
#: reference off it; SSL_CERT_DIR moves neither (get_ca_certs lists no capath
#: certificate). The script pops all four.
CA_VARIABLES = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR")


@pytest.fixture
def pip_copy(tmp_path: Path) -> Path:
    """A copy of this interpreter's pip package; returns its vendored bundle."""
    if not os.path.isfile(OS_CAFILE):
        pytest.skip(f"the host has no OS cafile at {OS_CAFILE}")
    spec = importlib.util.find_spec("pip")
    if spec is None:
        pytest.skip("this interpreter has no pip")
    installed = Path(spec.origin).parent
    shutil.copytree(
        installed, tmp_path / "pip", ignore=shutil.ignore_patterns("__pycache__")
    )
    return tmp_path / "pip" / "_vendor" / "certifi" / "cacert.pem"


def run_check(pip_copy: Path, **extra: str) -> subprocess.CompletedProcess:
    environment = {
        **os.environ,
        "PYTHONPATH": str(pip_copy.parents[3]),
        "PIP_CONFIG_FILE": os.devnull,  # no host pip.conf names a cert
        "PIP_CERT": OS_CAFILE,  # as the image sets it: --isolated must ignore it
        **extra,
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=BUDGET_SECONDS,
        check=False,
    )
    first = result.stdout.splitlines()[0] if result.stdout else result.stderr
    assert first.endswith(f" {pip_copy.parents[3] / 'pip' / '__init__.py'}"), first
    return result


@pytest.mark.parametrize("ca_variables", [False, True], ids=["plain", "ca-variables"])
def test_a_pip_with_its_vendored_bundle_fails_both_legs(pip_copy, ca_variables):
    extra = dict.fromkeys(CA_VARIABLES, OS_CAFILE) if ca_variables else {}
    if ca_variables:  # unpopped, it would make pip's own bundle the reference
        extra["SSL_CERT_FILE"] = str(pip_copy)

    result = run_check(pip_copy, **extra)

    assert result.returncode == 1, result.stdout
    why = "not the OS cafile's roots: link it to the OS bundle"
    assert result.stderr.splitlines() == [
        "pip root set check failed:",
        f"--isolated: pip verifies against {pip_copy}, {why}",
        f"PIP_CERT unset: pip verifies against {pip_copy}, {why}",
    ]


def test_a_pip_whose_bundle_is_linked_to_the_os_cafile_passes(pip_copy):
    pip_copy.unlink()
    pip_copy.symlink_to(OS_CAFILE)

    result = run_check(pip_copy)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert any(line.startswith(f"--isolated: {pip_copy}, the OS") for line in lines)
    assert any(line.startswith(f"PIP_CERT unset: {pip_copy}, the OS") for line in lines)
    assert lines[-1] == "pip root set OK"


def test_a_linked_pip_that_trusts_the_host_fails(pip_copy):
    """``PIP_TRUSTED_HOST`` mounts pip's InsecureHTTPAdapter, which names no bundle
    and verifies nothing: a failure, never the default bundle measured instead."""
    pip_copy.unlink()
    pip_copy.symlink_to(OS_CAFILE)

    result = run_check(pip_copy, PIP_TRUSTED_HOST="pypi.org")

    assert result.returncode == 1, result.stdout
    header, *problems = result.stderr.splitlines()
    assert header == "pip root set check failed:"
    assert len(problems) == 1, problems  # --isolated ignores the variable
    assert problems[0].startswith("PIP_CERT unset: pip's bundle None does not load")


def test_a_bundle_that_does_not_load_is_a_named_problem(pip_copy, tmp_path):
    pip_copy.unlink()
    pip_copy.symlink_to(tmp_path / "absent.pem")

    result = run_check(pip_copy)

    assert result.returncode == 1, result.stdout
    header, *problems = result.stderr.splitlines()
    assert header == "pip root set check failed:"
    assert [problem.split(" does not load")[0] for problem in problems] == [
        f"--isolated: pip's bundle {pip_copy}",
        f"PIP_CERT unset: pip's bundle {pip_copy}",
    ]
