"""A model send that was not minted fails its test (``tests/raw_model_content_guard.py``).

#1998 P2b (Fable AMEND A4). The model boundary reports a raw send (warn mode)
until P3 refuses it; in the suite the report has to be a failure. Two layers
make it one, and these tests pin each, and that each is in place:

* the ``error::...RawModelContentWarning`` line, in every pytest config a run
  can be rooted at — alone, it fails a raw send, and a raw send the code under
  test swallows (``except Exception``) passes, which is why the second layer
  exists;
* the autouse recorder, registered by ``tests/conftest.py`` and re-exported by
  ``tests/integration/conftest.py`` — it fails both, the swallowed one
  included, and lets a test that marks its raw send as expected pass.

The layers are proved in child pytest sessions (``pytester``), the way
``test_network_guard.py`` proves its report hook: the recorder is a fixture,
so only a session of its own can show a test failing at its teardown. The
recorder's own rules are asserted in this process, on
:func:`tests.raw_model_content_guard.recording`.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import textwrap
import warnings

import iniconfig
import pytest

from faultmaven.infrastructure.llm.dispatch import (
    RawModelContentWarning,
    route_model_call,
)
from tests.model_content import vetted_messages
from tests.raw_model_content_guard import MARKER, recording

pytest_plugins = ["pytester"]

pytestmark = [pytest.mark.unit, pytest.mark.security]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The ini filter that makes the warning an error.
ERROR_LINE = "error::faultmaven.infrastructure.llm.dispatch.RawModelContentWarning"
#: The autouse recorder's fixture name.
RECORDER = "_raw_model_content_fails_its_test"
#: Every pytest config a run can be rooted at, repo-relative, and the conftest
#: that registers the recorder for a run rooted there.
CONFTESTS = {
    "pytest.ini": "tests/conftest.py",
    "tests/integration/pytest.ini": "tests/integration/conftest.py",
}
CONFIGS = tuple(CONFTESTS)
#: Anything pytest adopts as a rootdir (``test_app_boot_is_shared.py``).
_ROOTDIR_MARKERS = ("pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml")

RAW = [{"role": "user", "content": "raw text"}]


class _Router:
    """A router double: the seam checks before any router runs."""

    async def route(self, **kwargs):
        return "answer"


def _section(config: str) -> iniconfig.SectionWrapper:
    return iniconfig.IniConfig(str(REPO_ROOT / config))["pytest"]


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Both layers are in place wherever a run can be rooted
# ---------------------------------------------------------------------------


def test_every_rootdir_config_is_listed():
    """A new directory with its own pytest config is a new rootdir: the
    filter and the recorder have to reach it too."""
    found = {
        marker.relative_to(REPO_ROOT).as_posix()
        for name in _ROOTDIR_MARKERS
        for marker in (REPO_ROOT / "tests").rglob(name)
    }
    assert found <= set(CONFIGS), sorted(found - set(CONFIGS))


@pytest.mark.parametrize("config", CONFIGS)
def test_every_config_makes_the_warning_an_error_and_registers_the_marker(config):
    section = _section(config)
    assert ERROR_LINE in _lines(section["filterwarnings"]), config
    markers = [line.split(":", 1)[0].strip() for line in _lines(section["markers"])]
    assert MARKER in markers, config


@pytest.mark.parametrize("config", CONFIGS)
def test_every_rootdir_binds_the_recorder(config):
    """The directory's conftest binds the recorder at module level: defined
    there (the root) or re-exported (a nested rootdir, whose run never
    registers ``tests/conftest.py``)."""
    conftest = REPO_ROOT / CONFTESTS[config]
    tree = ast.parse(conftest.read_text(encoding="utf-8"))
    bound = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            bound |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.ImportFrom):
            bound |= {a.asname or a.name for a in node.names}
    assert RECORDER in bound, conftest


def test_the_recorder_wraps_this_test(request):
    """A positive control: the fixture is autouse, so it is in every test's
    closure, this one's included."""
    assert RECORDER in request.fixturenames


# ---------------------------------------------------------------------------
# The recorder, in this process
# ---------------------------------------------------------------------------


async def test_a_raw_send_fails_at_exit_even_when_the_code_swallowed_it():
    """With the ini's ``error`` in force outside, the recorder's ``always``
    comes first: the warning is recorded, not raised, so a fail-open handler
    has nothing to catch, and the recorder fails at its exit, naming the site."""
    caught = []
    with warnings.catch_warnings(record=True) as outside:
        warnings.simplefilter("error", RawModelContentWarning)  # the ini's line
        with pytest.raises(pytest.fail.Exception) as failed:
            with recording(expected=False, test="t::swallows"):
                try:
                    await route_model_call(_Router(), messages=RAW)
                except Exception as exc:  # the fail-open handler under test
                    caught.append(exc)
    assert caught == []
    message = str(failed.value)
    assert "t::swallows sent model-bound content the redaction module did not mint" in (
        message
    )
    assert "tests/unit/test_raw_model_content_guard.py:" in message
    assert "model-bound content not minted by the redaction module: messages" in (
        message
    )
    # Handed back to whatever records outside, under ``always``: not raised.
    assert [w.category for w in outside] == [RawModelContentWarning]


async def test_a_raw_send_the_test_expects_is_handed_back_not_failed():
    with warnings.catch_warnings(record=True) as outside:
        warnings.simplefilter("error", RawModelContentWarning)
        with recording(expected=True, test="t::expects"):
            await route_model_call(_Router(), messages=RAW)
    assert [w.category for w in outside] == [RawModelContentWarning]


async def test_a_minted_send_records_nothing():
    with warnings.catch_warnings(record=True) as outside:
        with recording(expected=False, test="t::mints"):
            await route_model_call(_Router(), messages=vetted_messages(RAW))
    assert not [w for w in outside if issubclass(w.category, RawModelContentWarning)]


def test_other_categories_keep_their_filters_inside():
    """``always`` for this category only: the configs' other filters (an
    ``error`` such as ``Duplicate Operation ID``, the ignores) still apply
    inside a test."""

    class Other(UserWarning):
        pass

    with warnings.catch_warnings():
        warnings.simplefilter("error", Other)
        with recording(expected=False, test="t::other"):
            with pytest.raises(Other):
                warnings.warn("an error outside stays an error inside", Other)


def test_other_warnings_are_handed_back_to_pytest():
    """What the recorder took of another category reaches the recording
    outside it, so pytest's warnings summary is unchanged."""
    with warnings.catch_warnings(record=True) as outside:
        warnings.simplefilter("always", UserWarning)
        with recording(expected=False, test="t::hands-back"):
            warnings.warn("handed back", UserWarning)
    assert [str(w.message) for w in outside] == ["handed back"]


# ---------------------------------------------------------------------------
# Both layers, in child sessions
# ---------------------------------------------------------------------------

_CHILD_TESTS = """
import asyncio
import warnings

import pytest

from faultmaven.infrastructure.llm.dispatch import (
    RawModelContentWarning,
    route_model_call,
)
from tests.model_content import vetted_messages


class _Router:
    async def route(self, **kwargs):
        return "answer"


def _send(messages):
    return asyncio.run(route_model_call(_Router(), messages=messages))


def _raw():
    return [{"role": "user", "content": "raw text"}]


def test_a_raw_send_the_code_under_test_swallows():
    try:
        _send(_raw())
    except Exception:  # a fail-open handler, as at four production sites
        pass


def test_a_raw_send():
    _send(_raw())


def test_a_minted_send():
    _send(vetted_messages(_raw()))


@pytest.mark.raw_model_content_expected
def test_a_raw_send_the_test_marks_as_expected():
    _send(_raw())


def test_a_raw_send_the_test_asserts_with_pytest_warns():
    with pytest.warns(RawModelContentWarning):
        _send(_raw())


def test_another_warning():
    warnings.warn("handed back to the summary", UserWarning)
"""


def _child_ini(filterwarnings: list[str]) -> str:
    """A child session's config: the marker, and the given filter lines."""
    lines = ["[pytest]", "markers =", f"    {MARKER}: expected raw sends"]
    if filterwarnings:
        lines += ["filterwarnings ="] + [f"    {line}" for line in filterwarnings]
    return "\n".join(lines) + "\n"


def _summary_line(outcome: str, test: str) -> str:
    """A ``-rA`` summary line, as pytest prints it with and without ``CI`` set:
    ``OUTCOME path::test``, then `` - <message>`` when ``CI`` is set."""
    return rf"{outcome} \S+::{re.escape(test)}( - .*)?$"


def _child(pytester, monkeypatch, filterwarnings, *plugins, tests=None):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
    )
    pytester.makefile(".ini", pytest=_child_ini(filterwarnings))
    if tests is None:
        pytester.makepyfile(textwrap.dedent(_CHILD_TESTS))
    else:
        pytester.makepyfile(
            **{name: textwrap.dedent(source) for name, source in tests.items()}
        )
    args = ["-p", "no:cacheprovider", "-rA"]
    for plugin in plugins:
        args += ["-p", plugin]
    return pytester.runpytest_subprocess(*args, timeout=600)


@pytest.mark.parametrize("config", CONFIGS)
def test_the_ini_line_alone_fails_a_raw_send_but_not_a_swallowed_one(
    pytester, monkeypatch, config
):
    """The filter, read from the config itself, with no recorder: a raw send
    fails (raised at the send), and so does a marked one (the marker is the
    recorder's, and there is none); the one the code under test swallows
    PASSES — the gap the recorder closes."""
    filters = [line for line in _lines(_section(config)["filterwarnings"])]
    assert ERROR_LINE in filters
    result = _child(pytester, monkeypatch, [ERROR_LINE])
    result.assert_outcomes(passed=4, failed=2)
    result.stdout.re_match_lines(
        [
            _summary_line("PASSED", "test_a_raw_send_the_code_under_test_swallows"),
            _summary_line("FAILED", "test_a_raw_send"),
            _summary_line("FAILED", "test_a_raw_send_the_test_marks_as_expected"),
        ]
    )


@pytest.mark.parametrize(
    ("registered_by", "filterwarnings"),
    [
        ("tests.conftest", [ERROR_LINE]),
        # A run rooted at tests/integration registers only that directory's
        # conftest, which re-exports the recorder.
        ("tests.integration.conftest", [ERROR_LINE]),
        # The recorder alone: it does not lean on the ini line.
        ("tests.conftest", []),
    ],
    ids=["root", "integration", "root-without-the-ini-line"],
)
def test_the_recorder_fails_a_raw_send_even_when_the_code_swallowed_it(
    pytester, monkeypatch, registered_by, filterwarnings
):
    result = _child(pytester, monkeypatch, filterwarnings, registered_by)
    result.assert_outcomes(passed=6, errors=2)
    result.stdout.re_match_lines(
        [
            _summary_line("ERROR", "test_a_raw_send_the_code_under_test_swallows"),
            _summary_line("ERROR", "test_a_raw_send"),
        ]
    )
    result.stdout.fnmatch_lines(
        [
            "*::test_a_raw_send_the_code_under_test_swallows sent model-bound "
            "content the redaction module did not mint (1 send(s))*",
        ]
    )
    result.stdout.fnmatch_lines(["*UserWarning: handed back to the summary*"])


_CHILD_SEND = """
import asyncio

import pytest

from faultmaven.infrastructure.llm.dispatch import route_model_call


class _Router:
    async def route(self, **kwargs):
        return "answer"


def _send_raw():
    asyncio.run(
        route_model_call(_Router(), messages=[{"role": "user", "content": "raw"}])
    )
"""

_CHILD_MARKED_TOO_WIDELY = {
    "test_under_a_module_mark": _CHILD_SEND
    + """
pytestmark = pytest.mark.raw_model_content_expected


def test_a_raw_send_under_a_module_mark():
    _send_raw()
""",
    "test_under_a_class_mark": _CHILD_SEND
    + """
@pytest.mark.raw_model_content_expected
class TestUnderAClassMark:
    def test_a_raw_send_under_a_class_mark(self):
        _send_raw()
""",
    "test_marked_one_case_at_a_time": _CHILD_SEND
    + """
@pytest.mark.parametrize(
    "case", [pytest.param("raw", marks=pytest.mark.raw_model_content_expected)]
)
def test_a_raw_send_marked_on_its_own_case(case):
    _send_raw()
""",
}


def test_only_the_tests_own_marker_opts_it_out(pytester, monkeypatch):
    """The opt-out is per test (#2034 review F2): a module's ``pytestmark`` and
    a class decorator reach every test they cover, so neither exempts a raw
    send. A test's own decorator does (the main child session), and so does the
    mark on one ``pytest.param`` case."""
    result = _child(
        pytester,
        monkeypatch,
        [ERROR_LINE],
        "tests.conftest",
        tests=_CHILD_MARKED_TOO_WIDELY,
    )
    result.assert_outcomes(passed=3, errors=2)
    result.stdout.re_match_lines(
        [
            _summary_line("PASSED", "test_a_raw_send_marked_on_its_own_case[raw]"),
            _summary_line(
                "ERROR", "TestUnderAClassMark::test_a_raw_send_under_a_class_mark"
            ),
            _summary_line("ERROR", "test_a_raw_send_under_a_module_mark"),
        ]
    )
