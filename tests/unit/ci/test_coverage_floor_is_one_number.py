"""The overall coverage floor is one number (#2619).

``COVERAGE_FAIL_UNDER`` in ``ci-cd.yml`` is the floor both aggregate jobs gate
on, and Principle 9's Quality Gates line is its one prose statement. The floor
and its documents drifted apart twice (#1363, #2619), so the workflow's two
steps and the principle are tied to the one value here.
"""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github/workflows/ci-cd.yml"
PRINCIPLES = (
    REPO_ROOT / "docs/architecture/core-architecture/architectural-design-principles.md"
)
KEY = "COVERAGE_FAIL_UNDER"
AGGREGATE_JOBS = {"test-standalone", "test-cloud"}
# Principle 9's canonical statement of the floor, in its Quality Gates list.
P9_FLOOR = re.compile(
    r"^- ✅ Overall coverage cannot drop below (\d+)%: .*`COVERAGE_FAIL_UNDER`",
    re.MULTILINE,
)


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _floor_in_workflow() -> int:
    text = _workflow_text()
    # safe_load keeps the last of two duplicate keys, so count the source.
    definitions = re.findall(rf"^\s*{KEY}\s*:", text, re.MULTILINE)
    assert len(definitions) == 1, f"ci-cd.yml defines {KEY} {len(definitions)} times"
    return int(yaml.safe_load(text)["env"][KEY])


def test_the_floor_is_set_once_for_the_whole_workflow():
    assert 0 < _floor_in_workflow() <= 100


def test_both_aggregate_jobs_gate_on_the_one_key():
    text = _workflow_text()
    jobs = yaml.safe_load(text)["jobs"]
    reports = {
        name: [
            step["run"]
            for step in job.get("steps", [])
            if "coverage report" in step.get("run", "")
        ]
        for name, job in jobs.items()
    }
    gating = {name for name, runs in reports.items() if runs}

    assert gating == AGGREGATE_JOBS
    for name in AGGREGATE_JOBS:
        assert len(reports[name]) == 1, f"{name} runs one coverage report"
        assert f'--fail-under="${KEY}"' in reports[name][0], name
    assert not re.search(
        r"fail-under[= ]*\d", text
    ), "a literal floor in ci-cd.yml bypasses the key"


def test_principle_9_states_the_floor_ci_enforces():
    stated = P9_FLOOR.findall(PRINCIPLES.read_text(encoding="utf-8"))

    assert (
        len(stated) == 1
    ), f"Principle 9's Quality Gates states the floor once, naming {KEY}"
    assert int(stated[0]) == _floor_in_workflow()


def test_the_gate_compares_the_total_unrounded():
    """coverage fails when round(total, precision) < fail_under, so at the
    default precision of 0 a floor of 70 passes 69.5%."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        report = tomllib.load(f)["tool"]["coverage"]["report"]

    assert report.get("precision", 0) >= 2
