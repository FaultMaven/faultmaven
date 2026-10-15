"""`scripts/score_ab_shadow_rule.sh`: the scorer that decides promotion (#1989).

The first version of this command was a snippet in `performance.md`. It took
the pull request from the run's `.pull_requests`, which is `[]` for a run
whose pull request has since merged, so `gh pr diff` failed, nothing under
`pipefail` said so, and a grep over the empty diff printed `0`: on the run of
a merged pull request that edited `tests/wallclock/ab.py` it reported "touches
no benchmarked code", and promotion is judged after the pull requests merge.
Run outside a clone it printed nothing and exited 0, which reads as "no runs
yet". Both are pinned here, against a stub `gh` that answers from fixtures, so
no network and no repository history are needed. The stub answers with raw
API JSON and applies the `--jq` it is given through real `jq`, so the
script's three `--jq` expressions are exercised by every test, and
`TestTheJqExpressionsOnRecordedShapes` runs them on API JSON recorded from
the real repository (`fixtures/ab_shadow/`).

#2014: a pull request scores red if ANY of its evaluated runs was red, a run
not yet completed is a named bucket and not an ERROR, and the closure is
collected from a real benchmark run, not an import walk.

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


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ab_shadow"

#: A closure the stub benchmark run writes: three files, one in the hot path.
CLOSURE = [
    "faultmaven/utils/serialization.py",
    "tests/wallclock/ab.py",
    "tests/wallclock/record.py",
]

GH_STUB = r"""#!/usr/bin/env bash
# Answers `gh` from $GH_FIXTURES with RAW API JSON and applies the --jq it is
# given through real jq, as gh does. Fixture file names are the contract:
#   runs.json           what `gh run list --json ...` would print
#   run_list_fails      (exists) -> `gh run list` fails
#   pulls_<sha>.json    `commits/<sha>/pulls` (absent = the lookup fails)
#   files_<pr>.json     `pulls/<pr>/files` (absent = the listing fails)
#   report_<run>.json   the benchmark-ab artifact's ab_report.json
#   download_msg_<run>  the message of a download that fails (default: the
#                       "no artifact matches" one, when there is no report)
#   download_fails_<run>(exists) -> the download fails with an HTTP 500
set -u
fx="${GH_FIXTURES:?}"
echo "gh $*" >> "${fx}/calls.log"
jq_expr=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [ "${args[$i]}" = "--jq" ] && jq_expr="${args[$((i + 1))]}"
  [ "${args[$i]}" = "--json" ] && json_fields="${args[$((i + 1))]}"
done
apply() { jq -r "${jq_expr:-.}" "$1"; }
case "$1 $2" in
  "run list")
    [ ! -e "${fx}/run_list_fails" ] || { echo "HTTP 401: Bad credentials" >&2; exit 1; }
    case "${json_fields:-}" in
      *status*conclusion* | *conclusion*status*) ;;
      *) echo "run list must request status and conclusion: ${json_fields:-}" >&2; exit 98 ;;
    esac
    apply "${fx}/runs.json"
    ;;
  "run download")
    id="$3"
    dir=""
    while [ $# -gt 0 ]; do [ "$1" = "--dir" ] && dir="$2"; shift; done
    if [ -e "${fx}/download_fails_${id}" ]; then echo "HTTP 500: Server Error" >&2; exit 1; fi
    if [ -f "${fx}/download_msg_${id}" ]; then cat "${fx}/download_msg_${id}" >&2; exit 1; fi
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
        [ -f "${fx}/pulls_${sha}.json" ] || exit 1
        apply "${fx}/pulls_${sha}.json"
        ;;
      */pulls/*/files)
        pr="${path#*/pulls/}"; pr="${pr%/files}"
        [ -f "${fx}/files_${pr}.json" ] || { echo "HTTP 404" >&2; exit 1; }
        apply "${fx}/files_${pr}.json"
        ;;
      *) echo "unexpected gh api path: ${path}" >&2; exit 99 ;;
    esac
    ;;
  *) echo "unexpected gh call: $*" >&2; exit 99 ;;
esac
"""

#: Stands for the interpreter: the scorer runs the real benchmark suite with
#: the closure plugin, and the stub writes what that run would have written.
PYTHON_STUB = r"""#!/usr/bin/env bash
echo "python $*" >> "${GH_FIXTURES:?}/calls.log"
[ "$1 $2 $3 $4" = "-m pytest tests/benchmarks -m" ] || exit 99
case " $* " in *" -p tests.wallclock.closure_plugin "*) ;; *) exit 98 ;; esac
rc=0
[ ! -e "${GH_FIXTURES}/closure_rc" ] || rc="$(cat "${GH_FIXTURES}/closure_rc")"
# exit 1 = a benchmark failed: the session still ended and wrote the closure
if [ "${rc}" = 0 ] || [ "${rc}" = 1 ]; then cp "${GH_FIXTURES}/closure.txt" "${FM_CLOSURE_OUT:?}"; fi
exit "${rc}"
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
        self.tmp = tmp_path
        self.fx = tmp_path / "fixtures"
        self.bin = tmp_path / "bin"
        self.cache = tmp_path / "cache"
        self.fx.mkdir()
        self.bin.mkdir()
        for name, body in (("gh", GH_STUB), ("python-stub", PYTHON_STUB)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.fx / "closure.txt").write_text("".join(f"{n}\n" for n in CLOSURE))
        self.runs()

    def runs(self, *runs: tuple) -> None:
        """Newest first. (id, sha) is a completed run; (id, sha, status) is not."""
        records = []
        for index, run in enumerate(runs):
            run_id, sha, *rest = run
            status = rest[0] if rest else "completed"
            records.append(
                {
                    "conclusion": "success" if status == "completed" else "",
                    # newest first -> descending timestamps
                    "createdAt": f"2026-10-14T{23 - index:02d}:00:00Z",
                    "databaseId": int(run_id),
                    "headSha": sha,
                    "status": status,
                }
            )
        # the API order is not guaranteed: the script sorts, so shuffle it
        (self.fx / "runs.json").write_text(json.dumps(list(reversed(records))))

    def pull(self, sha: str, pr) -> None:
        body = [] if pr is None else [{"number": pr, "state": "closed"}]
        (self.fx / f"pulls_{sha}.json").write_text(json.dumps(body))

    def files(self, pr, *names: str) -> None:
        body = [{"filename": n, "status": "modified"} for n in names]
        (self.fx / f"files_{pr}.json").write_text(json.dumps(body))

    def report(self, run_id, body: str) -> None:
        (self.fx / f"report_{run_id}.json").write_text(body)

    def run(self, *args: str, cwd: Path = REPO_ROOT, **env: str):
        full_env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "GH_FIXTURES": str(self.fx),
            "PYTHON": str(self.bin / "python-stub"),
            "AB_SCORE_CACHE": str(self.cache),
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
        rig.runs(("38552467851", "a79df1c0000"))
        rig.pull("a79df1c0000", 1988)
        rig.files(
            1988,
            ".github/workflows/benchmarks.yml",
            "tests/wallclock/ab.py",
            "tests/wallclock/record.py",
        )
        rig.report("38552467851", report(would_fail=False, over_count=1, median=0.984))
        result = rig.run("2026-10-12")
        assert result.returncode == 0, result.stderr
        assert rows(result) == [
            ["38552467851", "1988", "2", "false", "1", "0.984", "a79df1c", ""]
        ]
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

    def test_a_pull_request_is_red_if_any_of_its_runs_was_red(self, rig):
        """‼ #2014: pushes after review are routine, and the criterion says a
        `true` on a pull request that touches none restarts the count. Run 3
        was `true`, the later run 4 `false`: the pull request is red, and
        its row names the red run and both heads."""
        rig.runs(("4", "s4"), ("3", "s3"))
        for sha in ("s4", "s3"):
            rig.pull(sha, 4000)
        rig.files(4000, "docs/a.md")
        rig.report("4", report(would_fail=False, over_count=0, median=1.00))
        rig.report("3", report(would_fail=True, over_count=9, median=1.20))
        result = rig.run("2026-10-12")
        assert rows(result) == [["3", "4000", "0", "true", "9", "1.2", "s4,s3", ""]]
        assert "evaluated across 1 pull requests" in result.stderr
        assert "0 with count_would_fail false" in result.stderr
        assert "1 with it true" in result.stderr

    def test_a_clean_pull_request_with_several_runs_scores_once_and_lists_heads(
        self, rig
    ):
        rig.runs(("6", "s6"), ("5", "s5"))
        for sha in ("s6", "s5"):
            rig.pull(sha, 4100)
        rig.files(4100, "docs/a.md")
        rig.report("6", report(would_fail=False, over_count=2, median=1.02))
        rig.report("5", report(would_fail=False, over_count=1, median=1.01))
        result = rig.run("2026-10-12")
        assert rows(result) == [["6", "4100", "0", "false", "2", "1.02", "s6,s5", ""]]
        assert "1 with count_would_fail false" in result.stderr

    @pytest.mark.parametrize(
        "newest_report, expected",
        [
            (report(would_fail=None, gated=False), "was not gated or predates"),
            (None, "has no benchmark-ab artifact"),
        ],
    )
    def test_a_newest_run_that_was_not_scored_is_named_on_the_row(
        self, rig, newest_report, expected
    ):
        """The older run's head is scored, the newer is not: the row says so
        instead of falling back silently."""
        rig.runs(("8", "s8"), ("7", "s7"))
        for sha in ("s8", "s7"):
            rig.pull(sha, 4200)
        rig.files(4200, "docs/a.md")
        if newest_report is not None:
            rig.report("8", newest_report)
        rig.report("7", report(would_fail=False))
        result = rig.run("2026-10-12")
        (row,) = rows(result)
        assert row[:4] == ["7", "4200", "0", "false"]
        assert row[6] == "s7"
        assert "newest run 8 (s8)" in row[7] and expected in row[7]

    def test_a_newest_run_still_running_is_named_on_the_row(self, rig):
        rig.runs(("8", "s8", "in_progress"), ("7", "s7"))
        for sha in ("s8", "s7"):
            rig.pull(sha, 4300)
        rig.files(4300, "docs/a.md")
        rig.report("7", report(would_fail=False))
        result = rig.run("2026-10-12")
        (row,) = rows(result)
        assert row[6] == "s7"
        assert "newest run 8 (s8) is in_progress, not scored" in row[7]

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
        assert "evaluated across 0 pull requests" in result.stderr
        assert "1 not gated or not evaluated" in result.stderr

    def test_a_run_with_no_artifact_is_skipped_not_an_error(self, rig):
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        rig.files(2001, "docs/x.md")
        result = rig.run("2026-10-12")
        assert result.returncode == 0 and rows(result) == []
        assert "1 completed with no artifact" in result.stderr

    def test_no_runs_is_a_sentence_not_silence(self, rig):
        result = rig.run("2026-10-12")
        assert result.returncode == 0 and rows(result) == []
        assert "listed 0 A/B runs since 2026-10-12" in result.stderr

    def test_the_closure_comes_from_a_real_benchmark_run_with_the_plugin(self, rig):
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        rig.files(2001, "docs/x.md")
        rig.report("1", report(would_fail=False))
        rig.run("2026-10-12")
        assert any(
            c.startswith("python -m pytest tests/benchmarks -m benchmark")
            and "-p tests.wallclock.closure_plugin" in c
            for c in rig.calls()
        )


class TestRunsStillInFlight:
    """#2014 item 1: not completed is a bucket, never an ERROR."""

    @pytest.mark.parametrize("status", ["in_progress", "queued", "waiting"])
    def test_a_run_not_completed_is_a_named_bucket_and_exit_zero(self, rig, status):
        rig.runs(("1", "s1", status))
        rig.pull("s1", 2001)
        result = rig.run("2026-10-12")
        assert result.returncode == 0, result.stdout + result.stderr
        assert rows(result) == []
        assert "1 in_progress" in result.stderr
        # nothing is downloaded for a run that has not finished
        assert not [c for c in rig.calls() if c.startswith("gh run download")]

    @pytest.mark.parametrize(
        "message",
        [
            "no valid artifacts found to download",
            "no artifact matches any of the names or patterns provided",
        ],
    )
    def test_both_gh_messages_for_a_missing_artifact_are_a_skip(self, rig, message):
        """`gh` 2.46.0 says the first for a run with no artifacts at all (a
        run cancelled early) and the second for a run that has others."""
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        (rig.fx / "download_msg_1").write_text(message)
        result = rig.run("2026-10-12")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ERROR" not in result.stdout
        assert "1 completed with no artifact" in result.stderr

    def test_the_run_list_asks_for_status_and_conclusion(self, rig):
        """The stub refuses a `gh run list` that does not (exit 98)."""
        result = rig.run("2026-10-12")
        assert result.returncode == 0, result.stderr


class TestTheInputs:
    @pytest.mark.parametrize(
        "bad",
        ["notadate", "2026-13-45", "2026-02-30", "26-10-12", "2026-10-1", "2026/10/12"],
    )
    def test_a_bad_date_exits_nonzero_with_a_message(self, rig, bad):
        result = rig.run(bad)
        assert result.returncode == 2
        assert "is not a" in result.stderr and bad in result.stderr
        assert "listed" not in result.stderr
        assert not [c for c in rig.calls() if c.startswith("gh ")]

    @pytest.mark.parametrize("bad", ["0", "-3", "many"])
    def test_a_bad_limit_exits_nonzero(self, rig, bad):
        result = rig.run("2026-10-12", AB_SCORE_LIMIT=bad)
        assert result.returncode == 2
        assert "AB_SCORE_LIMIT" in result.stderr

    def test_a_listing_that_hits_the_limit_warns(self, rig):
        rig.runs(("3", "s3", "queued"), ("2", "s2", "queued"))
        result = rig.run("2026-10-12", AB_SCORE_LIMIT="2")
        assert result.returncode == 0
        assert "WARNING: the listing hit AB_SCORE_LIMIT=2" in result.stderr

    def test_a_listing_under_the_limit_does_not_warn(self, rig):
        rig.runs(("2", "s2", "queued"))
        result = rig.run("2026-10-12", AB_SCORE_LIMIT="3")
        assert "WARNING" not in result.stderr


class TestTheClosureCache:
    @pytest.fixture
    def clone(self, tmp_path):
        """A git clone that has the plugin file: all the script checks."""
        repo = tmp_path / "clone"
        (repo / "tests" / "wallclock").mkdir(parents=True)
        (repo / "tests" / "wallclock" / "closure_plugin.py").write_text("")
        for cmd in (
            ["git", "init", "-q"],
            ["git", "add", "."],
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-q",
                "-m",
                "x",
            ],
        ):
            subprocess.run(cmd, cwd=repo, check=True)
        return repo

    def _runs_of_the_benchmark(self, rig):
        return [c for c in rig.calls() if c.startswith("python ")]

    def test_the_second_run_at_one_head_reuses_the_closure(self, rig, clone):
        assert rig.run("2026-10-12", cwd=clone).returncode == 0
        assert rig.run("2026-10-12", cwd=clone).returncode == 0
        assert len(self._runs_of_the_benchmark(rig)) == 1
        assert len(list(rig.cache.iterdir())) == 1

    def test_a_dirty_tree_is_neither_read_from_nor_written_to_the_cache(
        self, rig, clone
    ):
        (clone / "tests" / "wallclock" / "closure_plugin.py").write_text("# edited\n")
        assert rig.run("2026-10-12", cwd=clone).returncode == 0
        assert rig.run("2026-10-12", cwd=clone).returncode == 0
        assert len(self._runs_of_the_benchmark(rig)) == 2
        assert not rig.cache.exists()

    def test_a_new_head_collects_again(self, rig, clone):
        rig.run("2026-10-12", cwd=clone)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "y",
            ],
            cwd=clone,
            check=True,
        )
        rig.run("2026-10-12", cwd=clone)
        assert len(self._runs_of_the_benchmark(rig)) == 2
        assert len(list(rig.cache.iterdir())) == 2


class TestTheJqExpressionsOnRecordedShapes:
    """#2014 item 6: the script's three `--jq` expressions on JSON recorded
    from the real API (`fixtures/ab_shadow/`), through real `jq`. The stub
    applies whatever `--jq` the script passes, so a typo in an expression, or
    a field the API does not return, fails here and not in a promotion."""

    def test_run_list_commit_pulls_and_pull_files_shapes(self, rig):
        (rig.fx / "runs.json").write_text((FIXTURES / "run_list.json").read_text())
        # the fixture's three runs: 38718549574 (done), 38717634410 (running),
        # 38717515685 (done, older)
        sha_done = "f137dfc2c82fc4442607a137bcd160a052dd18b7"
        sha_running = "2c8f7f71f7b0cacd9968620d46474d51422da3b2"
        sha_old = "262684db2efaff2ea8ef1850efbb896559284237"
        for sha in (sha_done, sha_running, sha_old):
            shutil.copy(FIXTURES / "commit_pulls.json", rig.fx / f"pulls_{sha}.json")
        shutil.copy(FIXTURES / "pull_files.json", rig.fx / "files_2019.json")
        rig.report("38718549574", report(would_fail=False, over_count=1, median=1.0))
        rig.report("38717515685", report(would_fail=True, over_count=5, median=1.3))
        result = rig.run("2026-10-12")
        assert result.returncode == 0, result.stdout + result.stderr
        (row,) = rows(result)
        # red wins; the running middle run is a bucket, and named as not newest
        assert row[:6] == ["38717515685", "2019", "0", "true", "5", "1.3"]
        assert row[6] == "f137dfc,262684d"
        assert "1 in_progress" in result.stderr
        assert "3 A/B runs" in result.stderr

    def test_an_empty_commit_pulls_list_is_no_pull_request(self, rig):
        """`.[0].number // empty` on `[]` prints nothing: the lookup is an
        ERROR row, not a pull request called "null"."""
        rig.runs(("1", "s1"))
        rig.pull("s1", None)
        result = rig.run("2026-10-12")
        assert result.returncode == 1
        assert "ERROR\t1\tno pull request found for commit s1" in result.stdout


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
        assert sorted(r[0] for r in rows(result)) == ["2", "ERROR"]

    def test_a_failed_run_listing_stops_the_scorer(self, rig):
        (rig.fx / "run_list_fails").write_text("")
        result = rig.run("2026-10-12")
        assert result.returncode == 2
        assert "ERROR: gh run list failed" in result.stderr
        assert rows(result) == []

    @pytest.mark.parametrize("rc", ["2", "3", "4"])
    def test_a_benchmark_run_that_cannot_collect_stops_the_scorer(self, rig, rc):
        """No closure means every pull request would read as touching none."""
        (rig.fx / "closure_rc").write_text(rc)
        result = rig.run("2026-10-12")
        assert result.returncode == 2
        assert f"the benchmark run exited {rc}" in result.stderr

    def test_a_failing_benchmark_still_yields_the_closure(self, rig):
        """Exit 1 is a benchmark that failed after importing everything."""
        (rig.fx / "closure_rc").write_text("1")
        rig.runs(("1", "s1"))
        rig.pull("s1", 2001)
        rig.files(2001, "tests/wallclock/ab.py")
        rig.report("1", report(would_fail=False))
        result = rig.run("2026-10-12")
        assert result.returncode == 0, result.stderr
        assert rows(result)[0][2] == "1"

    def test_an_empty_closure_stops_the_scorer(self, rig):
        (rig.fx / "closure.txt").write_text("")
        result = rig.run("2026-10-12")
        assert result.returncode == 2
        assert "wrote no closure" in result.stderr

    def test_outside_a_clone_it_fails_instead_of_printing_nothing(self, rig, tmp_path):
        """‼ The second defect: from /tmp it exited 0 with no output, which
        reads as "no runs yet"."""
        elsewhere = tmp_path / "not-a-clone"
        elsewhere.mkdir()
        result = rig.run("2026-10-12", cwd=elsewhere)
        assert result.returncode == 2
        assert "not inside a git clone" in result.stderr
        assert result.stdout == ""

    def test_a_clone_without_the_plugin_fails(self, rig, tmp_path):
        old_clone = tmp_path / "old-clone"
        old_clone.mkdir()
        subprocess.run(["git", "init", "-q", str(old_clone)], check=True)
        result = rig.run("2026-10-12", cwd=old_clone)
        assert result.returncode == 2
        assert "has no tests/wallclock/closure_plugin.py" in result.stderr

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
