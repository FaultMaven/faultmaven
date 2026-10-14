"""`scripts/score_ab_shadow_rule.sh`: the scorer that decides promotion (#1989).

The first version of this command was a snippet in `performance.md`. It took
the pull request from the run's `.pull_requests`, which is `[]` for a run
whose pull request has since merged, so `gh pr diff` failed, nothing under
`pipefail` said so, and a grep over the empty diff printed `0`: on the run of
a merged pull request that edited `tests/wallclock/ab.py` it reported "touches
no benchmarked code", and promotion is judged after the pull requests merge.
Run outside a clone it printed nothing and exited 0, which reads as "no runs
yet". Both are pinned here, against a stub `gh` that answers from fixtures, so
no network and no repository history are needed.

Deliberately unmarked, like its siblings in this directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "score_ab_shadow_rule.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the scorer needs jq and bash",
)

#: A closure the stub helper prints: three files, one of them in the hot path.
CLOSURE = [
    "faultmaven/utils/serialization.py",
    "tests/wallclock/ab.py",
    "tests/wallclock/record.py",
]

GH_STUB = r"""#!/usr/bin/env bash
# Answers `gh` from $GH_FIXTURES. Fixture file names are the contract:
#   runs.tsv            what `gh run list ... --jq` would print
#   run_list_fails      (exists) -> `gh run list` fails
#   pulls_<sha>         the PR number `commits/<sha>/pulls` returns (empty = none)
#   files_<pr>          the PR's changed files; absent -> the listing fails
#   report_<run>.json   the benchmark-ab artifact's ab_report.json; absent -> "no artifact"
#   download_fails_<run>(exists) -> the download fails for a reason other than "no artifact"
set -u
fx="${GH_FIXTURES:?}"
echo "gh $*" >> "${fx}/calls.log"
case "$1 $2" in
  "run list")
    [ ! -e "${fx}/run_list_fails" ] || { echo "HTTP 401: Bad credentials" >&2; exit 1; }
    cat "${fx}/runs.tsv"
    ;;
  "run download")
    id="$3"
    dir=""
    while [ $# -gt 0 ]; do [ "$1" = "--dir" ] && dir="$2"; shift; done
    if [ -e "${fx}/download_fails_${id}" ]; then echo "HTTP 500: Server Error" >&2; exit 1; fi
    if [ ! -f "${fx}/report_${id}.json" ]; then
      echo "no artifact matches any of the names or patterns provided" >&2
      exit 1
    fi
    cp "${fx}/report_${id}.json" "${dir}/ab_report.json"
    ;;
  "api "*)
    path="$2"
    case "${path}" in
      */commits/*/pulls)
        sha="${path#*/commits/}"; sha="${sha%/pulls}"
        [ -e "${fx}/pulls_${sha}" ] || exit 1
        cat "${fx}/pulls_${sha}"
        ;;
      */pulls/*/files)
        pr="${path#*/pulls/}"; pr="${pr%/files}"
        [ -f "${fx}/files_${pr}" ] || { echo "HTTP 404" >&2; exit 1; }
        cat "${fx}/files_${pr}"
        ;;
      *) echo "unexpected gh api path: ${path}" >&2; exit 99 ;;
    esac
    ;;
  *) echo "unexpected gh call: $*" >&2; exit 99 ;;
esac
"""

PYTHON_STUB = r"""#!/usr/bin/env bash
echo "python $*" >> "${GH_FIXTURES:?}/calls.log"
[ ! -e "${GH_FIXTURES}/closure_fails" ] || { echo "ImportError: boom" >&2; exit 1; }
[ "$1 $2" = "-m tests.wallclock.benchmarked_files" ] || exit 99
cat "${GH_FIXTURES}/closure.txt"
"""


def report(*, would_fail, over_count=0, median=1.01, gated=True) -> str:
    return json.dumps(
        {
            "gated": gated,
            "median": median,
            "count_would_fail": would_fail,
            "over_count": over_count if would_fail is not None else None,
        }
    )


class Rig:
    """A fixtures directory, stub executables and a way to run the script."""

    def __init__(self, tmp_path: Path):
        self.fx = tmp_path / "fixtures"
        self.bin = tmp_path / "bin"
        self.fx.mkdir()
        self.bin.mkdir()
        for name, body in (("gh", GH_STUB), ("python-stub", PYTHON_STUB)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.fx / "closure.txt").write_text("".join(f"{n}\n" for n in CLOSURE))
        (self.fx / "runs.tsv").write_text("")

    def runs(self, *runs: tuple) -> None:
        """Newest first, as the script asks `gh run list` to sort them."""
        (self.fx / "runs.tsv").write_text(
            "".join(f"{run_id}\t{sha}\n" for run_id, sha in runs)
        )

    def pull(self, sha: str, pr) -> None:
        (self.fx / f"pulls_{sha}").write_text("" if pr is None else f"{pr}\n")

    def files(self, pr, *names: str) -> None:
        (self.fx / f"files_{pr}").write_text("".join(f"{n}\n" for n in names))

    def report(self, run_id, body: str) -> None:
        (self.fx / f"report_{run_id}.json").write_text(body)

    def run(self, *args: str, cwd: Path = REPO_ROOT, **env: str):
        full_env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "GH_FIXTURES": str(self.fx),
            "PYTHON": str(self.bin / "python-stub"),
            **env,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=cwd,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def calls(self) -> list:
        path = self.fx / "calls.log"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def rows(result) -> list:
    """The data rows of stdout, split into columns (header dropped)."""
    return [
        line.split("\t")
        for line in result.stdout.splitlines()
        if not line.startswith("#")
    ]


class TestTheScoring:
    def test_a_merged_pull_requests_run_is_scored_by_its_commit(self, rig):
        """‼ The regression this scorer exists to not repeat.

        The run's own `pull_requests` is not consulted (the stub does not even
        answer it): the pull request comes from the commit, which still names
        it after the merge, and its changed files come from the pull request.
        It edited the comparator and a closure file, so it touches 2; the old
        command said 0.
        """
        rig.runs(("38552467851", "a79df1c"))
        rig.pull("a79df1c", 1988)
        rig.files(
            1988,
            ".github/workflows/benchmarks.yml",
            "tests/wallclock/ab.py",
            "tests/wallclock/record.py",
        )
        rig.report("38552467851", report(would_fail=False, over_count=1, median=0.984))
        result = rig.run("2026-10-12")
        assert result.returncode == 0, result.stderr
        assert rows(result) == [["38552467851", "1988", "2", "false", "1", "0.984"]]
        assert "touching benchmarked code: 1" in result.stderr

    def test_a_file_reached_only_through_imports_is_touched(self, rig):
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        rig.files(2001, "docs/x.md", "faultmaven/utils/serialization.py")
        rig.report("1", report(would_fail=False))
        assert rows(rig.run("2026-10-12"))[0][2] == "1"

    def test_clean_and_false_red_pull_requests_are_told_apart(self, rig):
        rig.runs(("3", "s3"), ("2", "s2"), ("1", "s1"))
        for sha, pr, would_fail in (
            ("s3", 3003, False),
            ("s2", 3002, True),
            ("s1", 3001, False),
        ):
            rig.pull(sha, pr)
            rig.files(pr, "docs/development/testing/performance.md")
            rig.report(
                sha[1:],
                report(would_fail=would_fail, over_count=4 if would_fail else 0),
            )
        result = rig.run("2026-10-12")
        assert result.returncode == 0, result.stderr
        assert [(r[1], r[2], r[3]) for r in rows(result)] == [
            ("3003", "0", "false"),
            ("3002", "0", "true"),
            ("3001", "0", "false"),
        ]
        assert "2 with count_would_fail false" in result.stderr
        assert "1 with it true" in result.stderr

    def test_one_run_per_pull_request_the_latest_that_was_scored(self, rig):
        """Reruns and pushes of one pull request are not independent samples.

        Newest first: run 5 (PR 4000) has no evaluated report, run 4 is PR
        4000's older run and does, run 3 is also PR 4000 and must NOT be
        counted again. The latest *scored* run stands for the pull request.
        """
        rig.runs(("5", "s5"), ("4", "s4"), ("3", "s3"))
        for sha in ("s5", "s4", "s3"):
            rig.pull(sha, 4000)
        rig.files(4000, "docs/a.md")
        rig.report("5", report(would_fail=None))
        rig.report("4", report(would_fail=False, over_count=2))
        rig.report("3", report(would_fail=True, over_count=9))
        result = rig.run("2026-10-12")
        assert rows(result) == [["4", "4000", "0", "false", "2", "1.01"]]
        assert "scored 1 pull requests" in result.stderr

    @pytest.mark.parametrize(
        "body",
        [
            # not gated: nothing was measured
            report(would_fail=None, gated=False),
            # a report from before the shadow rule: no such key at all
            json.dumps({"gated": True, "median": 1.0}),
        ],
    )
    def test_a_run_that_did_not_evaluate_the_rule_is_not_scored(self, rig, body):
        """‼ Not evaluated is not clean: it must not add to the 20."""
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        rig.files(2001, "docs/x.md")
        rig.report("1", body)
        result = rig.run("2026-10-12")
        assert result.returncode == 0 and rows(result) == []
        assert "scored 0 pull requests" in result.stderr

    def test_a_run_with_no_artifact_is_skipped_not_an_error(self, rig):
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        rig.files(2001, "docs/x.md")
        result = rig.run("2026-10-12")
        assert result.returncode == 0 and rows(result) == []
        assert "1 runs skipped" in result.stderr

    def test_no_runs_is_a_sentence_not_silence(self, rig):
        result = rig.run("2026-10-12")
        assert result.returncode == 0 and rows(result) == []
        assert "listed 0 A/B runs since 2026-10-12" in result.stderr

    def test_the_closure_comes_from_the_helper_command(self, rig):
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        rig.files(2001, "docs/x.md")
        rig.report("1", report(would_fail=False))
        rig.run("2026-10-12")
        assert "python -m tests.wallclock.benchmarked_files" in rig.calls()


class TestTheFailuresAreLoud:
    def test_a_failed_pull_request_lookup_is_an_error_row_not_a_zero(self, rig):
        """‼ The first defect: a lookup that fails must not become touches=0."""
        rig.runs(("1", "s1"))
        rig.report("1", report(would_fail=False))
        # no `pulls_s1` fixture: the stub exits 1, as `gh api` does on a 404
        result = rig.run("2026-10-12")
        assert result.returncode == 1
        assert result.stdout.splitlines()[1].startswith("ERROR\t1\t")
        assert not any(row[2:3] == ["0"] for row in rows(result))

    def test_a_commit_with_no_pull_request_is_an_error_row(self, rig):
        rig.runs(("1", "s1"))
        rig.pull("s1", None)  # `gh api` succeeds with an empty list
        result = rig.run("2026-10-12")
        assert result.returncode == 1
        assert "ERROR\t1\tno pull request found for commit s1" in result.stdout

    def test_a_failed_file_listing_is_an_error_row_not_a_zero(self, rig):
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        rig.report("1", report(would_fail=False))
        # no `files_2001` fixture
        result = rig.run("2026-10-12")
        assert result.returncode == 1
        assert (
            "ERROR\t1\tcould not list the files of pull request 2001" in result.stdout
        )
        assert all(r[0] == "ERROR" for r in rows(result))

    def test_a_failing_stage_of_a_pipeline_is_not_read_as_zero(self, rig):
        """‼ `pipefail` bites here and nowhere the other tests reach.

        The touch count is `comm ... | wc -l | tr`; if `comm` fails the last
        stage still succeeds, and without `pipefail` the row says 0: the
        exact shape of the first version's defect, one pipeline over.
        """
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        rig.files(2001, "docs/x.md")
        rig.report("1", report(would_fail=False))
        broken = rig.bin / "comm"
        broken.write_text("#!/usr/bin/env bash\necho 'comm: boom' >&2\nexit 2\n")
        broken.chmod(0o755)
        result = rig.run("2026-10-12")
        assert result.returncode != 0
        assert rows(result) == [], result.stdout

    def test_a_failed_download_that_is_not_a_missing_artifact_is_an_error(self, rig):
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        (rig.fx / "download_fails_1").write_text("")
        result = rig.run("2026-10-12")
        assert result.returncode == 1
        assert "ERROR\t1\tartifact download failed: HTTP 500" in result.stdout

    def test_an_error_does_not_hide_the_other_rows(self, rig):
        rig.runs(("2", "s2"), ("1", "s1"))
        rig.pull("s2", 2002)
        rig.files(2002, "docs/x.md")
        rig.report("2", report(would_fail=False))
        rig.report("1", report(would_fail=False))  # no pulls_s1: lookup fails
        result = rig.run("2026-10-12")
        assert result.returncode == 1
        assert [r[0] for r in rows(result)] == ["2", "ERROR"]

    def test_a_failed_run_listing_stops_the_scorer(self, rig):
        (rig.fx / "run_list_fails").write_text("")
        result = rig.run("2026-10-12")
        assert result.returncode == 2
        assert "ERROR: gh run list failed" in result.stderr
        assert rows(result) == []

    def test_a_failed_helper_stops_the_scorer(self, rig):
        """No closure means every pull request would read as touching none."""
        (rig.fx / "closure_fails").write_text("")
        result = rig.run("2026-10-12")
        assert result.returncode == 2
        assert "benchmarked_files failed" in result.stderr

    def test_an_empty_closure_stops_the_scorer(self, rig):
        (rig.fx / "closure.txt").write_text("")
        result = rig.run("2026-10-12")
        assert result.returncode == 2
        assert "printed nothing" in result.stderr

    def test_outside_a_clone_it_fails_instead_of_printing_nothing(self, rig, tmp_path):
        """‼ The second defect: from /tmp it exited 0 with no output, which
        reads as "no runs yet"."""
        elsewhere = tmp_path / "not-a-clone"
        elsewhere.mkdir()
        result = rig.run("2026-10-12", cwd=elsewhere)
        assert result.returncode == 2
        assert "not inside a git clone" in result.stderr
        assert result.stdout == ""

    def test_a_clone_without_the_helper_fails(self, rig, tmp_path):
        old_clone = tmp_path / "old-clone"
        old_clone.mkdir()
        subprocess.run(["git", "init", "-q", str(old_clone)], check=True)
        result = rig.run("2026-10-12", cwd=old_clone)
        assert result.returncode == 2
        assert "has no tests/wallclock/benchmarked_files.py" in result.stderr

    def test_it_needs_the_date(self, rig):
        result = rig.run()
        assert result.returncode == 2
        assert "usage:" in result.stderr


def test_the_script_is_executable_and_runs_under_pipefail():
    """`pipefail` is what stops a failed `gh` feeding a filter from reporting
    the filter's status; the script states it on its own line."""
    assert os.access(SCRIPT, os.X_OK)
    lines = [line.strip() for line in SCRIPT.read_text().splitlines()]
    assert "set -euo pipefail" in lines
