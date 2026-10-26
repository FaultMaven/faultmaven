"""``import faultmaven`` refuses a Python below ``MINIMUM_PYTHON``.

pip and uv check requires-python only when they install, so this is what holds
for an editable install made under an older floor and for a run from the source
tree. That ``MINIMUM_PYTHON`` matches requires-python is test_packaging's job.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.unit

TREE = str(Path(faultmaven.__file__).resolve().parent.parent)
FLOOR = ".".join(map(str, faultmaven.MINIMUM_PYTHON))
# Above the floor numerically, below it as a string (3.11.15 vs 3.11.5).
ABOVE = ".".join(
    map(str, (*faultmaven.MINIMUM_PYTHON[:2], faultmaven.MINIMUM_PYTHON[2] + 10))
)

# Imports this tree's faultmaven on an interpreter reporting argv[1] as its
# version. -I keeps PYTHONPATH and the working directory out, and the __file__
# check keeps an editable install's finder from answering for another checkout.
_IMPORT_AS = """
import collections, sys
VersionInfo = collections.namedtuple("version_info", "major minor micro releaselevel serial")
sys.version_info = VersionInfo(*map(int, sys.argv[1].split(".")), "final", 0)
sys.path.insert(0, sys.argv[2])
import faultmaven
assert faultmaven.__file__.startswith(sys.argv[2]), faultmaven.__file__
"""


def _import_as(version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", _IMPORT_AS, version, TREE],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_an_interpreter_below_the_floor_is_refused_at_import():
    """3.11.4: the last release with the regex bugs, below any floor test_packaging allows."""
    result = _import_as("3.11.4")

    assert result.returncode == 1, result.stderr
    assert f"RuntimeError: FaultMaven requires Python {FLOOR} or later" in result.stderr
    assert "is Python 3.11.4." in result.stderr


@pytest.mark.parametrize("version", [FLOOR, ABOVE])
def test_an_interpreter_at_or_above_the_floor_imports(version):
    result = _import_as(version)

    assert result.returncode == 0, result.stderr
