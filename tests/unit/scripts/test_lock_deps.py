"""``scripts/lock-deps.sh`` runs under one uv, and writes the image its own lockfile.

The lockfiles depend on how uv resolves ``--python-version`` and markers, so
CI's staleness gate installed an unpinned uv that a release could turn red
(#2777): the script now refuses any uv but ``UV_VERSION``, the version CI reads
from it. The image installed ``cloud.txt``, compiled with the test extra, so
pytest, linters and locust shipped to production (#2231): ``image.txt`` is the
cloud extra alone.

The script runs here on a copy, against a stand-in ``uv`` that records each
compile, so nothing is resolved or downloaded.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "lock-deps.sh"

FAKE_UV = """\
#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "uv $FAKE_UV_VERSION (0123abcd 2026-01-01)"
    exit 0
fi
echo "$*" >> "$UV_LOG"
"""


def _ci_uv_version() -> str:
    """What CI's ``pip install "uv==$(sed ...)"`` installs, run the same way."""
    return subprocess.run(
        ["sed", "-n", "s/^UV_VERSION=//p", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _run(tmp_path: Path, uv_version: str) -> tuple[subprocess.CompletedProcess, list]:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "lock-deps.sh")
    (tmp_path / "pyproject.toml").write_text('requires-python = ">=3.11.5"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text(FAKE_UV)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "uv.log"
    log.touch()
    proc = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "lock-deps.sh")],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "FAKE_UV_VERSION": uv_version,
            "UV_LOG": str(log),
        },
        capture_output=True,
        text=True,
    )
    return proc, log.read_text().splitlines()


def test_ci_installs_the_one_uv_the_script_runs_under(tmp_path):
    version = _ci_uv_version()
    assert version, "CI's sed finds no UV_VERSION= line in lock-deps.sh"

    proc, compiles = _run(tmp_path, version)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(compiles) == 4


def test_any_other_uv_is_refused_before_a_lockfile_is_written(tmp_path):
    version = _ci_uv_version()

    proc, compiles = _run(tmp_path, "0.0.1")

    assert proc.returncode == 1
    assert compiles == []
    assert f"pip install uv=={version}" in proc.stdout


def test_the_image_lockfile_is_the_cloud_extra_without_the_test_extra(tmp_path):
    proc, compiles = _run(tmp_path, _ci_uv_version())

    assert proc.returncode == 0, proc.stdout + proc.stderr
    by_output = {line.split(" -o ")[1].split()[0]: line for line in compiles}
    image = by_output["requirements/image.txt"].split()
    cloud = by_output["requirements/cloud.txt"].split()
    assert "--extra" in image and image[image.index("--extra") + 1] == "cloud"
    assert image.count("--extra") == 1
    # The CI jobs that install cloud.txt still get the test extra.
    assert "test" in [cloud[i + 1] for i, arg in enumerate(cloud) if arg == "--extra"]
    # Same resolution flags, so the two agree on every pin they share.
    flags = ("--python-version", "--python-platform")
    for flag in flags:
        assert image[image.index(flag) + 1] == cloud[cloud.index(flag) + 1]
