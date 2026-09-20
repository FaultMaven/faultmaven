"""The docs-only classifier decides whether the test suites run at all (#1539).

`.github/workflows/ci-cd.yml`'s `changes` job publishes `docs_only`, and every
heavy job -- both test suites and the image build -- declares an `if:` against
it. A wrong `true` skips all of them, and a skipped required check reads as
passing, so the failure mode is a silent merge with no tests rather than a red
build. Until this file existed the classifier was a heredoc inside the
workflow: nothing could import it and nothing could test it, and it had already
been wrong twice in two days.

What each group here holds down:

* the extraction itself -- the workflow must call the module, not inline it;
* the case table from the issue, run against the REAL tests/ tree, with the
  over-correction control (an unpinned archival move stays inert) that a
  "declare every rename non-inert" fix would fail while passing every negative;
* the fail-closed arm -- `RESOLVED` unset means `docs_only=false` whatever the
  diff says -- asserted beside a positive control, because a harness that
  forgets `RESOLVED` answers `false` to everything and that is exactly what a
  clean sweep looks like;
* the pin probe's three refusals, which must exit non-zero having written
  nothing rather than shrug and classify;
* the bare-directory decision (#1539): a directory a test builds with
  `pathlib`'s `/` is recovered when the literals are WALKED, and a bare
  directory literal still pins nothing.

‼ This module names no document in a bare string literal, and
`test_this_module_pins_no_document_of_its_own` enforces that. The pin probe
collects the string literals of every `tests/**/*.py`, including this one, so a
path written inline here would arm the suites for the document it mentions --
and the over-correction control would fail, pinned by the test asserting it is
unpinned. The case table and every path string live in `docs_only_cases.json`,
which the probe does not parse. A multi-line source blob is fine: the probe
collects the blob, not the paths inside it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "classify_docs_only.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"

_FIXTURE = json.loads((Path(__file__).parent / "docs_only_cases.json").read_text())
CASES = _FIXTURE["cases"]
PATHS = _FIXTURE["paths"]

# A synthetic tests/ tree has to satisfy the classifier's own positive control
# before it can answer anything, so most fixture trees start from this.
CONTROL_SOURCE = textwrap.dedent('''
    """Stand-in for the tests that pin these two documents on the real tree."""
    A = "CLAUDE.md"
    B = "docs/architecture"
    ''')


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("classify_docs_only", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def real_literals(mod):
    """Every literal the pin probe finds on this repository's own tests/."""
    return mod.collect_literals(REPO_ROOT / "tests")


def _tests_tree(root: Path, sources: dict) -> Path:
    tests = root / "tests"
    (tests / "unit").mkdir(parents=True, exist_ok=True)
    for name, body in sources.items():
        (tests / "unit" / name).write_text(body, encoding="utf-8")
    return tests


def _run(cwd: Path, changed: str, resolved):
    """Drive the script the way the workflow step does."""
    (cwd / "changed.txt").write_text(changed, encoding="utf-8")
    out = cwd / "github_output"
    out.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GITHUB_OUTPUT"] = str(out)
    env.pop("RESOLVED", None)
    if resolved is not None:
        env["RESOLVED"] = resolved
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, out.read_text(encoding="utf-8")


def _silent(*_args, **_kwargs):
    return None


# --------------------------------------------------------------------------
# The extraction
# --------------------------------------------------------------------------


def test_the_workflow_calls_the_module_instead_of_inlining_it():
    """A classifier nothing can import is a classifier nothing can test.

    That is the regression this issue is about, so it gets an assertion rather
    than a comment asking the next person not to undo it.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "python3 .github/scripts/classify_docs_only.py" in workflow
    assert (
        "python3 - <<" not in workflow
    ), "the classifier has been inlined back into the workflow as a heredoc"
    assert SCRIPT.is_file()


def test_the_file_list_still_lists_both_sides_of_a_rename():
    """The other half of the gate, and the first way it was wrong.

    `--name-only` prints only a rename's DESTINATION, so moving a file into an
    inert root arrived as one harmless path with the source invisible -- this
    repository's normal archival move, 321 of them in main's history. The flag
    that fixes it is protected today by a comment asking the next person not to
    drop it; the classifier cannot see the difference, because by the time it
    runs the source path is simply absent from its input.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "git diff --name-only --no-renames" in workflow


def test_this_module_pins_no_document_of_its_own(mod, tmp_path):
    """The probe reads this file too, so a path named here arms its document.

    What this file contributes to the repository's literal set is measured the
    way CI measures it -- by running the probe over a tests/ tree holding this
    module, and subtracting a run over the controls alone. Reimplementing the
    walk here would let the two drift, and the guard would go quiet exactly
    when the grammar it is checking changed.

    The contribution is then checked against every tracked path the classifier
    would otherwise call inert. A hit means the case table has moved back
    inline and at least one case is now asserting something this file caused.
    """
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    inert = [p for p in tracked if mod.classify(p) is None]
    assert inert, "no tracked path is inert -- the check would pass vacuously"

    with_me = mod.collect_literals(
        _tests_tree(
            tmp_path / "with_me",
            {
                "t0.py": CONTROL_SOURCE,
                "t1.py": Path(__file__).read_text(encoding="utf-8"),
            },
        )
    )
    baseline = mod.collect_literals(
        _tests_tree(tmp_path / "baseline", {"t0.py": CONTROL_SOURCE})
    )
    mine = with_me - baseline
    assert mine, "this module contributed no literals -- the probe missed it"

    offenders = {p: mod.pinned_by(p, mine) for p in inert if mod.pinned_by(p, mine)}
    assert not offenders, (
        "this test module names documents in bare string literals, which pins "
        f"them for the whole repository: {offenders}"
    )


# --------------------------------------------------------------------------
# The case table
# --------------------------------------------------------------------------


def test_the_pin_probe_finds_its_positive_controls_on_the_real_tree(real_literals, mod):
    for control in mod.POSITIVE_CONTROLS:
        assert control in real_literals


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case_table(case, mod, real_literals):
    literals = case.get("literals")
    if literals is None:
        literals = real_literals
    verdict = mod.decide(case["changed"], literals, log=_silent)
    assert verdict == case["expected"], case["why"]


# --------------------------------------------------------------------------
# The fail-closed arm
# --------------------------------------------------------------------------


@pytest.mark.parametrize("resolved", [None, "", "false", "True", "TRUE", "1", "yes"])
def test_an_unresolved_file_list_is_never_docs_only(tmp_path, resolved):
    """Anything but the exact string "true" refuses to classify.

    Asserted against a diff that IS docs-only, so the test cannot pass by the
    input being executable. The paired positive control below is the other
    half: without it, a harness that simply never sets the variable reports
    every case `false` and reads as a clean sweep.
    """
    _tests_tree(tmp_path, {"t0.py": CONTROL_SOURCE})
    proc, written = _run(tmp_path, PATHS["docs_only_diff"] + "\n", resolved)
    assert proc.returncode == 0, proc.stderr
    assert written == "docs_only=false\n"
    assert "could not be resolved" in proc.stdout


def test_the_same_diff_is_docs_only_once_resolved_is_true(tmp_path):
    """Positive control for the test above."""
    _tests_tree(tmp_path, {"t0.py": CONTROL_SOURCE})
    proc, written = _run(tmp_path, PATHS["docs_only_diff"] + "\n", "true")
    assert proc.returncode == 0, proc.stderr
    assert written == "docs_only=true\n"


def test_a_code_diff_is_not_docs_only_through_the_script(tmp_path):
    """The `false` that `decide()` produces, asserted through the process.

    Every other process-level `docs_only=false` here comes from an EARLY
    RETURN -- an unresolved file list or an empty diff -- and reaches neither
    `decide()` nor the `emit()` call that carries its answer. Without this
    case the one line that writes the CI verdict is exercised in the `true`
    direction only, and replacing it with a hardcoded `emit("true", ...)` --
    the exact failure this whole file exists to prevent, because it skips
    every suite on a diff that changes the image -- passes all 36 of the
    others. Measured, not supposed.
    """
    _tests_tree(tmp_path, {"t0.py": CONTROL_SOURCE})
    proc, written = _run(tmp_path, PATHS["executable_diff"] + "\n", "true")
    assert proc.returncode == 0, proc.stderr
    assert written == "docs_only=false\n"


def test_an_empty_diff_is_not_docs_only(tmp_path):
    _tests_tree(tmp_path, {"t0.py": CONTROL_SOURCE})
    proc, written = _run(tmp_path, "\n   \n", "true")
    assert proc.returncode == 0, proc.stderr
    assert written == "docs_only=false\n"
    assert "the diff is empty" in proc.stdout


# --------------------------------------------------------------------------
# The pin probe's refusals
# --------------------------------------------------------------------------


def test_a_missing_tests_directory_refuses_rather_than_sweeps(tmp_path):
    proc, written = _run(tmp_path, PATHS["arbitrary_docs_file"] + "\n", "true")
    assert proc.returncode == 1
    assert written == ""
    assert "::error::tests/ is missing" in proc.stdout


def test_an_unparsable_test_file_refuses_rather_than_sweeps(tmp_path):
    _tests_tree(tmp_path, {"t0.py": CONTROL_SOURCE, "t1.py": "def broken(:\n"})
    proc, written = _run(tmp_path, PATHS["arbitrary_docs_file"] + "\n", "true")
    assert proc.returncode == 1
    assert written == ""
    assert "::error::cannot parse" in proc.stdout


def test_a_probe_pointed_at_the_wrong_tree_refuses_rather_than_sweeps(tmp_path):
    """Every document inert is also what a broken probe looks like."""
    _tests_tree(tmp_path, {"t0.py": 'X = "nothing-this-repo-pins.md"\n'})
    proc, written = _run(tmp_path, PATHS["arbitrary_docs_file"] + "\n", "true")
    assert proc.returncode == 1
    assert written == ""
    assert "the pin probe found no literal" in proc.stdout


# --------------------------------------------------------------------------
# The bare-directory decision (#1539)
# --------------------------------------------------------------------------


def test_a_directory_built_by_division_pins_beneath_itself(tmp_path, mod):
    """The gap: `ROOT / "docs" / "architecture"` names a directory, but the
    literal walk saw only `docs` and `architecture` and `pinned_by` matched
    neither -- so a test that rglob'd that directory pinned nothing in it.

    Closed by joining the chain when the literals are collected, which is the
    same class of fix as `--no-renames`: get the detector's input right rather
    than teach the detector to guess.

    The walked directory here is not `docs/architecture`, which is the pair the
    issue names, because the probe's own positive control requires that string
    to be a plain literal in the tree -- it would pin the path whether or not
    the chain were recovered, and the test would pass on a reverted fix. A
    directory that appears ONLY as a chain is what isolates the mechanism; the
    mutation that removes the join fails this test.
    """
    source = textwrap.dedent("""
        import pathlib

        WALKED = pathlib.Path(__file__).resolve().parents[3] / "docs" / "runbooks-design"

        def test_every_design_document_has_a_title():
            for doc in WALKED.rglob("*.md"):
                assert doc.read_text().startswith("#")
        """)
    tree = _tests_tree(tmp_path, {"t0.py": CONTROL_SOURCE, "t1.py": source})
    literals = mod.collect_literals(tree)
    doc = PATHS["walked_directory_doc"]
    assert mod.pinned_by(doc, literals) == PATHS["walked_directory"]
    assert mod.decide([doc], literals, log=_silent) == "false"


def test_only_the_maximal_division_chain_is_recovered(tmp_path, mod):
    """`ROOT / "docs" / "reference" / "api" / "openapi.json"` is the shape that
    exists on this tree today. Emitting its prefixes too would add the parent
    directory and arm every prose document under it on the strength of a test
    that reads one generated artifact.
    """
    source = textwrap.dedent("""
        import pathlib

        SPEC = (
            pathlib.Path(__file__).resolve().parents[3]
            / "docs"
            / "reference"
            / "api"
            / "openapi.json"
        )
        """)
    tree = _tests_tree(tmp_path, {"t0.py": CONTROL_SOURCE, "t1.py": source})
    literals = mod.collect_literals(tree)
    assert PATHS["generated_spec"] in literals
    assert PATHS["reference_dir"] not in literals
    assert PATHS["reference_api_dir"] not in literals
    assert mod.pinned_by(PATHS["reference_prose"], literals) is None


def test_a_bare_directory_literal_still_pins_nothing(tmp_path, mod):
    """The decision, not an oversight.

    Dropping `pinned_by`'s `"/" in literal` condition was the other candidate
    answer. It cannot be taken on this tree: the bare directory name is already
    a literal, so that arm would pin every path beneath it and the gate would
    answer `false` for every docs-only diff -- dead, and dead silently, because
    a conservative `false` is indistinguishable from a correct one.
    """
    source = textwrap.dedent("""
        ROOT_NAME = "docs"
        SECTION = "architecture"
        """)
    tree = _tests_tree(tmp_path, {"t0.py": CONTROL_SOURCE, "t1.py": source})
    literals = mod.collect_literals(tree)
    assert PATHS["bare_docs_literal"] in literals
    doc = PATHS["unpinned_docs_subtree_doc"]
    assert mod.pinned_by(doc, literals) is None
    assert mod.decide([doc], literals, log=_silent) == "true"


def test_the_gate_still_fires_on_this_repository(real_literals, mod):
    """The whole point of not taking the bare-directory arm.

    If a future literal ever does pin all of the documentation root, every case
    in the table above flips to `false` together and each failure reads as its
    own puzzle. This one says the gate is dead.
    """
    assert mod.decide([PATHS["docs_only_diff"]], real_literals, log=_silent) == "true"
