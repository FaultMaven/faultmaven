"""The contract job's checkout reaches the base its contract step reads.

`api-contract-drift` compares the committed contract with the one at the
job's `CONTRACT_BASE`, HEAD's parent. Two settings have to agree for that to
work — the checkout's `fetch-depth` and the base the job names — and when they
disagree the failure shows only in the merge queue, which pull request CI never
runs. A shallow clone holds no parent, and the queue deletes a group's branch
as soon as the group is dropped or rebuilt, so a fetch made after the checkout
cannot repair it: that is how the step's `git fetch --deepen=1` failed in the
queue.

So the checkout is reproduced rather than its depth read off as a number: a
repository holds a merge-queue commit and a pull request's merge commit, each
is checked out the way actions/checkout does at the depth the job declares,
the branch is deleted on the remote, and the real script runs against the base
the job declares.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"
SCRIPT = REPO_ROOT / "scripts" / "check_contract_version.py"
# Where the contract lives in the synthetic repository. Its real location is a
# constant of the script and plays no part in what this file tests.
SPEC_NAME = "openapi.json"

# Isolated from the developer's git config: a global signing or hook setting
# must not decide whether these commits can be made.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "ci",
    "GIT_AUTHOR_EMAIL": "ci@example.com",
    "GIT_COMMITTER_NAME": "ci",
    "GIT_COMMITTER_EMAIL": "ci@example.com",
}


@pytest.fixture(scope="module")
def job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["api-contract-drift"]


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("check_contract_version", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_spec(repo: Path, version: str, paths: dict, message: str) -> str:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "FaultMaven API", "version": version},
        "paths": paths,
        "components": {"schemas": {}},
    }
    (repo / SPEC_NAME).write_text(json.dumps(spec))
    _git(repo, "add", SPEC_NAME)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


_ROUTE = {"get": {"responses": {"200": {}}}}


@pytest.fixture
def remote(tmp_path) -> tuple[Path, dict]:
    """A remote holding the two commits the job runs on outside a push.

    Each one publishes a surface change with a version bump, so a script that
    can read the base passes and one that cannot exits 2.
    """
    repo = tmp_path / "remote"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit_spec(repo, "1.0.0", {}, "base")

    # merge_group: the queue squashes the pull request onto main.
    _git(repo, "checkout", "-q", "-b", "gh-readonly-queue/main/pr-1-0000000")
    queued = _commit_spec(repo, "1.1.0", {"/a": _ROUTE}, "queued")

    # pull_request: GitHub's merge commit, main as its first parent.
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit_spec(repo, "1.1.0", {"/b": _ROUTE}, "feature")
    _git(repo, "checkout", "-q", "-b", "pull/1/merge", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    merged = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    return repo, {
        "merge_group": ("gh-readonly-queue/main/pr-1-0000000", queued),
        "pull_request": ("pull/1/merge", merged),
    }


def _checkout_like_actions(remote_repo: Path, sha: str, depth: int, dest: Path):
    """What actions/checkout runs: one fetch of the commit, then a checkout.

    `fetch-depth: 0` means the whole history, so no `--depth` at all.
    """
    _git(dest, "init", "-q")
    _git(dest, "remote", "add", "origin", remote_repo.as_uri())
    depth_args = [f"--depth={depth}"] if depth else []
    _git(
        dest,
        "-c",
        "protocol.version=2",
        "fetch",
        "-q",
        "--no-tags",
        *depth_args,
        "origin",
        f"+{sha}:refs/remotes/origin/checkout",
    )
    _git(dest, "checkout", "-q", "--detach", sha)


def _declared_depth(job: dict) -> int:
    (checkout,) = [
        step
        for step in job["steps"]
        if (step.get("uses") or "").startswith("actions/checkout@")
    ]
    # actions/checkout's own default is 1.
    return int((checkout.get("with") or {}).get("fetch-depth", 1))


def _run_check_in(mod, monkeypatch, clone: Path, base: str):
    monkeypatch.setattr(mod, "PROJECT_ROOT", clone)
    monkeypatch.setattr(mod, "SPEC_RELPATH", SPEC_NAME)
    monkeypatch.setattr(mod, "SPEC_PATH", clone / SPEC_NAME)
    return mod.run_check(base)


def _checked_out_without_its_branch(remote, event, depth, tmp_path) -> Path:
    remote_repo, refs = remote
    branch, sha = refs[event]
    clone = tmp_path / "clone"
    clone.mkdir()
    _checkout_like_actions(remote_repo, sha, depth, clone)
    # The queue drops the branch while the job is still running.
    _git(remote_repo, "branch", "-q", "-D", branch)
    return clone


@pytest.mark.parametrize("event", ["merge_group", "pull_request"])
def test_the_declared_checkout_reaches_the_declared_base(
    event, job, mod, remote, tmp_path, monkeypatch
):
    """The real script, at the job's base, in a clone made at the job's depth
    whose branch is already gone — the merge queue's worst case."""
    clone = _checked_out_without_its_branch(
        remote, event, _declared_depth(job), tmp_path
    )

    code, report = _run_check_in(mod, monkeypatch, clone, job["env"]["CONTRACT_BASE"])

    assert code == 0, "\n".join(report)
    assert "1.0.0 -> 1.1.0" in "\n".join(report), report


def test_a_shallow_checkout_fails_loudly_and_says_why(
    job, mod, remote, tmp_path, monkeypatch
):
    """The positive control for the test above: at depth 1 the same harness
    must fail, or its pass proves nothing. And the failure must name the fix
    for what is missing — a parent — not send the reader to fetch a branch."""
    clone = _checked_out_without_its_branch(remote, "merge_group", 1, tmp_path)

    code, report = _run_check_in(mod, monkeypatch, clone, job["env"]["CONTRACT_BASE"])

    assert code == 2, "\n".join(report)
    assert "fetch-depth of at least 2" in "\n".join(report), report


def test_no_contract_step_fetches_after_the_checkout(job):
    """A fetch here runs after the queue may have deleted the branch, and a
    `--depth=1` fetch on a deeper clone makes it shallow again. Both contract
    steps read the job's base; neither resolves one of its own."""
    contract_steps = [
        step
        for step in job["steps"]
        if any(
            marker in (step.get("run") or "")
            for marker in ("check_contract_version.py", "oasdiff")
        )
    ]
    assert len(contract_steps) == 2, [step.get("name") for step in contract_steps]
    for step in contract_steps:
        assert "git fetch" not in step["run"], step["name"]
        assert "${CONTRACT_BASE}" in step["run"], step["name"]
