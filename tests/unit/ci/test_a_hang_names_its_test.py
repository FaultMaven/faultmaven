"""A hung test leaves its stack in a log CI keeps (#3078).

A hang in a sliced test step ran into its job's ``timeout-minutes``, which
cancels the job and loses its log, so nothing named the test. Now pytest's
faulthandler dumps a long-running test's stack, and each test step's own
``timeout-minutes`` fails the step after that dump and before the job's
timeout, so the job completes and keeps the log.
"""

import re
from pathlib import Path

import iniconfig
import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"

# A shell line of a step's ``run`` that invokes pytest (not a comment naming it).
_PYTEST_LINE = re.compile(r"^\s*pytest\s", re.MULTILINE)


def _faulthandler_timeout() -> int:
    raw = iniconfig.IniConfig(REPO_ROOT / "pytest.ini")["pytest"].get(
        "faulthandler_timeout"
    )
    assert raw is not None, "pytest.ini sets no faulthandler_timeout"
    return int(raw)


def _test_steps() -> list[tuple[str, dict, dict]]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return [
        (job_id, job, step)
        for job_id, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if _PYTEST_LINE.search(step.get("run", ""))
    ]


def test_a_test_past_its_limit_dumps_its_stack():
    assert _faulthandler_timeout() > 0


def test_every_test_step_fails_after_the_dump_and_before_its_job_is_cancelled():
    steps = _test_steps()
    names = {step["name"] for _, _, step in steps}
    # Positive control: the sliced suite steps are among those found.
    assert {"Run Tests (Standalone)", "Run Tests (Cloud)"} <= names

    dump_after = _faulthandler_timeout()
    for job_id, job, step in steps:
        where = f"{job_id} / {step['name']!r}"
        assert "timeout-minutes" in job, f"{where}: job sets no timeout-minutes"
        assert "timeout-minutes" in step, f"{where}: step sets no timeout-minutes"
        assert step["timeout-minutes"] < job["timeout-minutes"], (
            f"{where}: a hang must fail the step before the job's timeout "
            "cancels the job and loses its log"
        )
        assert step["timeout-minutes"] * 60 > dump_after, (
            f"{where}: the step must outlive the faulthandler dump, or the log "
            "never names the test"
        )
