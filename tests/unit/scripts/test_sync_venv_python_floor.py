"""scripts/sync-venv.sh builds and keeps venvs at or above requires-python's floor.

The floor (pyproject.toml) is 3.11.5: earlier 3.11 releases mis-match the
possessive regexes the secret redactor relies on. The script runs here against
a stub ``uv`` that records its calls and a stand-in interpreter that reports a
chosen version, so nothing is created or installed.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "sync-venv.sh"

# Runs `-c CODE [ARGS]` on the real interpreter, reporting VERSION.
_STAND_IN = """\
import collections, sys
version = tuple(int(part) for part in sys.argv[1].split("."))
code, args = sys.argv[3], sys.argv[4:]
fields = "major minor micro releaselevel serial"
sys.version_info = collections.namedtuple("version_info", fields)(*version, "final", 0)
sys.version = sys.argv[1] + " (stand-in)"
sys.argv = ["-c", *args]
exec(compile(code, "<string>", "exec"), {"__name__": "__main__"})
"""

# What the script and the stub call by name; their PATH holds nothing else.
_TOOLS = ("dirname", "sha256sum", "mkdir", "cp")

# Records each call; `uv venv` makes the venv a stand-in reporting the floor.
_UV = """\
#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
if [ "$1" = venv ]; then
    mkdir -p "$2/bin" && cp "{at_floor}" "$2/bin/python"
fi
"""


def _executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def _interpreter(path: Path, stand_in: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return _executable(
        path, f'#!/bin/sh\nexec "{sys.executable}" "{stand_in}" {version} "$@"\n'
    )


@pytest.fixture
def run(tmp_path):
    stand_in = tmp_path / "stand_in.py"
    stand_in.write_text(_STAND_IN)
    log = tmp_path / "uv-calls"
    at_floor = _interpreter(tmp_path / "at-floor" / "python", stand_in, "3.11.5")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = _executable(bin_dir / "uv", _UV.format(log=log, at_floor=at_floor))
    if not os.access(stub, os.X_OK):
        pytest.skip("tmp_path does not allow executing the stubs")
    bash = shutil.which("bash")
    assert bash, "bash is not on PATH"
    for tool in _TOOLS:
        found = shutil.which(tool)
        assert found, f"{tool} is not on PATH"
        (bin_dir / tool).symlink_to(found)
    # The stub's directory is the whole PATH, so no real uv can be reached.
    env = {**os.environ, "PATH": str(bin_dir)}

    def _run(venv: Path, version: str = ""):
        if version:
            _interpreter(venv / "bin" / "python", stand_in, version)
        result = subprocess.run(
            [bash, str(SCRIPT), "test", str(venv)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    return _run


@pytest.mark.unit
class TestSyncVenvPythonFloor:
    def test_a_new_venv_is_asked_for_a_3_11_at_or_above_the_floor(self, run, tmp_path):
        venv = tmp_path / "new-venv"

        result, calls = run(venv)

        assert result.returncode == 0, result.stderr
        assert calls[0] == f"venv {venv} --python >=3.11.5,<3.12 --quiet"

    def test_an_existing_venv_below_the_floor_is_refused_before_any_sync(
        self, run, tmp_path
    ):
        result, calls = run(tmp_path / "old-venv", "3.11.4")

        assert result.returncode == 1
        assert "below pyproject.toml's floor of 3.11.5" in result.stderr
        assert calls == []

    @pytest.mark.parametrize("version", ["3.11.5", "3.11.15"])
    def test_an_existing_venv_at_or_above_the_floor_is_synced(
        self, run, tmp_path, version
    ):
        """3.11.15 sorts below 3.11.5 as text: a string comparison refuses it."""
        result, calls = run(tmp_path / "venv", version)

        assert result.returncode == 0, result.stderr
        assert [call.split()[:2] for call in calls] == [
            ["pip", "sync"],
            ["pip", "install"],
        ]
