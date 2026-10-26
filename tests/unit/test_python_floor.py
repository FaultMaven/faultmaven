"""``import faultmaven`` refuses a Python below ``MINIMUM_PYTHON``.

pip and uv check requires-python only when they install, so this is what holds
for an editable install made under an older floor and for a run from the source
tree. That ``MINIMUM_PYTHON`` matches requires-python is test_packaging's job.
"""

import os
import subprocess
import sys

import pytest

import faultmaven
from tests.source_tree import PACKAGE_ROOT

pytestmark = pytest.mark.unit

MAJOR, MINOR, MICRO = faultmaven.MINIMUM_PYTHON
FLOOR = f"{MAJOR}.{MINOR}.{MICRO}"
# Above the floor as a number, below it as text (3.11.15 vs 3.11.5): only for a
# floor whose micro is 2-9, which test_above_sorts_below_the_floor_as_text checks.
ABOVE = f"{MAJOR}.{MINOR}.{MICRO + 10}"
NEXT_MINOR = f"{MAJOR}.{MINOR + 1}.0"

# Imports faultmaven from the tree these tests belong to (PACKAGE_ROOT, not
# wherever this process imported it from) on an interpreter reporting argv[1]
# as its version. -I keeps PYTHONPATH and the working directory out; the
# __file__ check catches an editable install's finder answering for another
# checkout.
_IMPORT_AS = """
import collections, sys
VersionInfo = collections.namedtuple("version_info", "major minor micro releaselevel serial")
sys.version_info = VersionInfo(*map(int, sys.argv[1].split(".")), "final", 0)
sys.path.insert(0, sys.argv[2])
import faultmaven
assert faultmaven.__file__ == sys.argv[3], faultmaven.__file__
"""


def _import_as(version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _IMPORT_AS,
            version,
            str(PACKAGE_ROOT.parent),
            str(PACKAGE_ROOT / "__init__.py"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        # 3.13+ colours tracebacks under FORCE_COLOR, which would split the
        # strings the refusal test looks for.
        env={**os.environ, "NO_COLOR": "1"},
    )


def test_an_interpreter_below_the_floor_is_refused_at_import():
    """3.11.4: the last release with the regex bugs, below any floor test_packaging allows."""
    result = _import_as("3.11.4")

    assert result.returncode == 1, result.stderr
    assert f'File "{PACKAGE_ROOT / "__init__.py"}"' in result.stderr
    assert f"RuntimeError: FaultMaven requires Python {FLOOR} or later" in result.stderr
    assert "is Python 3.11.4." in result.stderr


@pytest.mark.parametrize("version", [FLOOR, ABOVE, NEXT_MINOR])
def test_an_interpreter_at_or_above_the_floor_imports(version):
    result = _import_as(version)

    assert result.returncode == 0, result.stderr


def test_above_sorts_below_the_floor_as_text():
    """Otherwise the ABOVE case no longer catches versions compared as strings."""
    assert ABOVE < FLOOR
