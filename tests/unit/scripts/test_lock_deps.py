"""lock-deps.sh runs under the one uv CI installs (#2777), and gives the image a
lockfile without the test extra (#2231). Run on a copy, against a stand-in uv."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "lock-deps.sh"
FAKE_UV = '#!/bin/sh\n[ "$1" = --version ] && echo "uv $V (0123abcd)" && exit 0\necho "$*" >> "$LOG"\n'
# What CI's `pip install "uv==$(sed ...)"` installs.
CI_UV = subprocess.check_output(
    ["sed", "-n", "s/^UV_VERSION=//p", str(SCRIPT)], text=True
).strip()


def _run(tmp: Path, version: str) -> tuple[subprocess.CompletedProcess, dict]:
    (tmp / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp / "scripts")
    (tmp / "pyproject.toml").write_text('requires-python = ">=3.11.5"\n')
    (tmp / "uv").write_text(FAKE_UV)
    (tmp / "uv").chmod(0o755)
    (tmp / "log").touch()
    env = {"PATH": f"{tmp}:/usr/bin:/bin", "V": version, "LOG": str(tmp / "log")}
    proc = subprocess.run(
        ["bash", str(tmp / "scripts" / "lock-deps.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    calls = (tmp / "log").read_text().splitlines()
    return proc, {c.split(" -o ")[1].split()[0]: c.split() for c in calls}


def test_any_uv_but_the_one_ci_installs_is_refused(tmp_path):
    assert CI_UV
    proc, compiles = _run(tmp_path, "0.0.1")
    assert proc.returncode == 1 and compiles == {}
    assert f"pip install uv=={CI_UV}" in proc.stdout


def test_the_image_lockfile_is_the_cloud_extra_alone(tmp_path):
    proc, compiles = _run(tmp_path, CI_UV)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    def extras(name: str) -> list[str]:
        args = compiles[f"requirements/{name}.txt"]
        return [args[i + 1] for i, a in enumerate(args) if a == "--extra"]

    assert extras("image") == ["cloud"]
    assert extras("cloud") == ["cloud", "test"]  # the CI jobs keep the test extra
