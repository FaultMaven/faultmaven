"""Test Cloud's real-Redis step sets the flag the Redis fixtures read (#2279).

``tests/integration/conftest.py`` makes ``real_redis`` and ``session_redis``
fail, rather than skip or run on FakeRedis, when ``REQUIRE_REDIS_ENV`` is set
and nothing names a Redis. The step that must reach the server sets it. The two
spellings are tied here, so a rename on either side cannot switch it off.
"""

import ast
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


def _required_flag() -> str:
    """``REQUIRE_REDIS_ENV`` as the integration conftest assigns it, read
    without importing that conftest (and the app it loads)."""
    tree = ast.parse((REPO_ROOT / "tests/integration/conftest.py").read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and [getattr(t, "id", None) for t in node.targets] == ["REQUIRE_REDIS_ENV"]
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    raise AssertionError("tests/integration/conftest.py assigns no REQUIRE_REDIS_ENV")


def test_the_real_redis_step_sets_the_flag_the_fixtures_read():
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
    )
    module = "tests/integration/test_real_redis_stores.py"
    # A step that runs the module, not one that ``--ignore``s it.
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if module in step.get("run", "").replace(f"--ignore={module}", "")
    ]

    assert len(steps) == 1, "exactly one step runs the real-Redis store tests"
    (step,) = steps
    flag = _required_flag()
    assert str(step.get("env", {}).get(flag, "")).strip(), (
        f"the real-Redis step must set {flag}: without it a step that lost its "
        "REDIS_* environment passes on FakeRedis"
    )
