"""A first-party import inside an ImportError-swallowing construct must resolve.

The defect (#947): ``tests/unit/api/conftest.py`` wrapped its whole ``simple_app``
fixture body in::

    try:
        from faultmaven.api.v1.dependencies import get_agent_service, ...
        from faultmaven.api.v1.routes import case, data, knowledge, ...
        ...                                       # ~1160 lines of fixture
    except ImportError:
        @app.post("/api/v1/data/upload")
        async def mock_data_upload():
            return {"status": "mock_response"}

``faultmaven.api.v1.routes`` has no module on disk and never did, so the
``except`` branch was taken on every run for the life of the file: the 1160
lines never executed once. Nothing failed, because the fixture still yielded a
working ``FastAPI`` app — one carrying a ``/health`` stub and a single mocked
upload route. That is the harm. The swallow converts "this fixture is broken"
into "this fixture quietly returns something else", and a test written against
it asserts on the stub while reading, to every reviewer, as coverage of the API.

The rule enforced here is narrow and mechanical: **inside a construct that
swallows ``ImportError``, an import that actually executes under it and names a
first-party module must name a module that exists on disk.** All three parts —
does the construct swallow, does the import execute under it, does the module
exist — are decided statically, from the AST, ``builtins`` and the filesystem.
Nothing is imported, which is what keeps the scan from depending on which
optional dependencies happen to be installed in the job that runs it.

The first two parts live in ``tests/import_guard_ast``, shared with
``test_optional_dependency_detection``, which asks the same question about the
same construct. Two copies of that analysis had already diverged before they
were merged, which is the outcome ``tests/error_text_ast`` documents the rule
against.

**Only module existence is checked, deliberately.** #947 also imported two
names ``faultmaven.api.v1.dependencies`` does not bind, and an earlier draft
checked that half too, by parsing the target module for its module-level
bindings. It was withdrawn: deciding "does this module bind this name" from an
AST is not sound, and every way it was wrong was a **false positive** — a red
gate on correct code, which is worse than no gate because it teaches readers to
route around the scan. It called a name bound only under ``if TYPE_CHECKING:``
a runtime binding; it missed names bound by ``for``/``with``/``match``/walrus;
and, on a handler catching only ``ModuleNotFoundError``, it reported a
swallowed name-import that in fact propagates (a missing *name* raises plain
``ImportError``; ``ModuleNotFoundError`` is the narrower subclass raised only
for a missing module). Module existence has none of that surface, and it is the
half that caught #947.

Deliberately NOT flagged:

* A **third-party** import inside the same construct. That is the legitimate
  optional-dependency pattern, and it is a different question owned by
  ``tests/unit/infrastructure/test_optional_dependency_detection.py``. Note the
  two are not the same shape even when they look alike: ``tests/conftest.py``
  guards ``from faultmaven.modules.agent.tools.web_search import WebSearchTool``
  — first-party and present. What may be absent is ``tavily``, one import
  deeper, which this scan neither sees nor should.
* A **relative** import (``from .x import y``) and a string import
  (``importlib.import_module("faultmaven.x")``). Neither carries a resolvable
  absolute module name in the AST node this scan reads.
* Imports in a try's ``else``/``except``/``finally``. Only ``body`` is covered
  by the handler — an ``else`` that raises ``ImportError`` propagates.
* Everything ``tests.import_guard_ast`` declines to call a swallow: a handler
  that may re-raise, a handler chain whose first matching arm is a project
  exception, an aliased exception name. Each is a deliberate false negative,
  taken because the alternative is a red gate on correct code.
* **Anything outside ``tests/``.** The scope is deliberate, not an oversight.
  Running this same scan over ``faultmaven/`` finds 103 first-party imports
  inside a swallowing construct (446 files, 102 resolved) and exactly one that
  cannot resolve — ``case_data_ingestion_service.py``'s
  ``faultmaven.core.processing.classifier``, which keeps
  ``ENHANCED_COMPONENTS_AVAILABLE`` permanently False. That one is already known
  and annotated at its own call site as a deliberate hold pending removal of the
  remaining ``if self._enhanced_mode:`` guards. Widening this gate would mean
  shipping it with a standing exemption on day one — whether as a registry here
  or a marker there — which is how an exemption becomes a place to park
  violations. Widen the scope when that site is closed, not before.

One accepted cost, stated so it is not a surprise: a ``.py`` file under
``tests/`` that cannot be parsed fails this gate rather than dropping silently
out of the population. A file the scan cannot read is a file it is not
checking. There are none today; a deliberately-malformed fixture must live
outside ``tests/`` or carry a non-``.py`` suffix, and the failure message says
so.
"""

import ast
import pathlib

import pytest

from tests.import_guard_ast import (
    executed_when_block_runs,
    suppresses_import_error,
    swallows_import_error,
)

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TESTS_ROOT = REPO_ROOT / "tests"

# A scan that walked zero files reports zero violations and passes. Moving this
# test, or a layout change, would do exactly that — silently.
_MIN_FILES_EXPECTED = 600

# ...and so would a scan that walked every file but stopped recognising the
# construct, or that no-opped on repo paths while still biting the synthetic
# probes below. This is the count of first-party imports the scan found inside a
# swallowing construct and resolved: the population it is actually checking.
#
# Pinned at the ACTUAL value rather than comfortably below it. An earlier draft
# used 3 against a population of 6 — a floor that lets a third of the gate's
# reach disappear while it still reads green, which is the same "green means
# nothing" failure the floor exists to prevent. Two of the six live in
# tests/conftest.py alone, so one file's cleanup would have crossed it
# invisibly. If a legitimate change moves this number, move it deliberately.
_GUARDED_IMPORTS_EXPECTED = 6

# Repo-root packages whose absence is a defect rather than an uninstalled
# dependency. `alembic` is deliberately absent: it is also the name of the
# installed migration library, so a guarded `import alembic` is a third-party
# optional-dependency question, not this one.
_FIRST_PARTY = ("faultmaven", "tests", "scripts", "demo")

# Cheap prefilter: a file with none of these words cannot contain either
# construct. It is a strict superset — every `except` and every `suppress`
# carries one.
_CONSTRUCT_HINTS = ("except", "suppress")

# read_text failures that mean "this path is not a file to check" rather than
# "this file could not be read". Anything else is reported: see the module
# docstring's note on unparseable files.
_ABSENT_PATH_ERRORS = (FileNotFoundError, NotADirectoryError, IsADirectoryError)


def _where(path: pathlib.Path, lineno: int) -> str:
    """Repo-relative when the path is in the repo; the synthetic probes are not."""
    try:
        return f"{path.relative_to(REPO_ROOT)}:{lineno}"
    except ValueError:
        return f"{path}:{lineno}"


def _is_first_party(module: str) -> bool:
    return module.split(".")[0] in _FIRST_PARTY


def _module_exists(module: str) -> bool:
    """A first-party module's existence is a filesystem fact.

    A bare directory counts: several first-party packages ship without an
    ``__init__.py`` (``faultmaven/api``, ``scripts``, ``demo``) and resolve as
    PEP 420 namespace packages. Accepting one here is safe in a way it would not
    be for a third-party name — this directory is checked into the repo rather
    than left behind by an uninstall.
    """
    base = REPO_ROOT.joinpath(*module.split("."))
    return base.with_suffix(".py").is_file() or base.is_dir()


def _guarded_imports(tree):
    """Every import that actually executes inside an ImportError-swallowing construct.

    Traversal is ``executed_when_block_runs``: it descends into class bodies
    (which run at definition time) but not function bodies (which do not), and
    not into ``if TYPE_CHECKING:``. One shared traversal rather than one per
    construct — an earlier draft repeated it inline for the try case and the
    nested case, so a fix to one was a silent half-fix.
    """
    seen: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and swallows_import_error(node):
            body = node.body
        elif isinstance(node, (ast.With, ast.AsyncWith)) and suppresses_import_error(
            node
        ):
            body = node.body
        else:
            continue
        for statement in executed_when_block_runs(body):
            if not isinstance(statement, (ast.Import, ast.ImportFrom)):
                continue
            key = (statement.lineno, statement.col_offset)
            if key in seen:
                continue
            seen.add(key)
            yield statement


def _scan(paths) -> tuple[list[str], int, list[str]]:
    """Returns (violations, resolved, unreadable).

    Takes the paths so a synthetic violating file can be pushed through this
    exact function: a scan only ever run against a clean tree passes just as
    well when the scan itself is broken. ``unreadable`` is returned rather than
    swallowed — a file the scan cannot read is a file it is not checking, and
    that must be visible instead of quietly shrinking the population. Only a
    path that is *not there* is tolerated silently; a permission error or a
    decode error is reported, because those are files that exist and were
    skipped.
    """
    violations: list[str] = []
    unreadable: list[str] = []
    resolved = 0

    for path in sorted(paths):
        try:
            source = path.read_text(encoding="utf-8")
        except _ABSENT_PATH_ERRORS:
            continue  # not a file to check
        except UnicodeDecodeError:
            unreadable.append(f"{_where(path, 0)}: not valid UTF-8")
            continue
        except OSError as exc:
            unreadable.append(f"{_where(path, 0)}: {exc.strerror or exc}")
            continue
        if not any(hint in source for hint in _CONSTRUCT_HINTS):
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            unreadable.append(f"{_where(path, exc.lineno or 0)}: {exc.msg}")
            continue

        for node in _guarded_imports(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif node.level or not node.module:
                continue  # relative: no absolute name to resolve
            else:
                modules = [node.module]

            for module in modules:
                if not _is_first_party(module):
                    continue
                if _module_exists(module):
                    resolved += 1
                else:
                    violations.append(
                        f"{_where(path, node.lineno)}: `{module}` — no such module, "
                        "so the swallowing branch is taken on every run"
                    )

    return violations, resolved, unreadable


def test_no_first_party_import_is_silently_swallowed():
    """A new instance of the #947 shape fails here rather than in six months.

    #947 sat undetected for the life of the file precisely because its failure
    mode is a pass: the fixture kept working, on a stub.
    """
    assert TESTS_ROOT.is_dir(), f"{TESTS_ROOT} is not a directory"
    files = sorted(TESTS_ROOT.rglob("*.py"))
    assert len(files) >= _MIN_FILES_EXPECTED, (
        f"scanned only {len(files)} files under {TESTS_ROOT} — the scan is not "
        "reaching the test tree, so a green result means nothing"
    )

    violations, resolved, unreadable = _scan(files)

    assert not unreadable, (
        "the scan could not read these files, so they were not checked. A "
        "deliberately-malformed fixture belongs outside tests/ or under a "
        "non-.py suffix:\n" + "\n".join(f"  - {u}" for u in unreadable)
    )
    # Violations first: they are the substantive finding, and reporting the
    # population instead would hide them behind a count. The pin below exists
    # only to make a *clean* result mean something, so it is checked second.
    assert not violations, "first-party imports that can never resolve:\n" + "\n".join(
        f"  - {v}" for v in violations
    )
    assert resolved == _GUARDED_IMPORTS_EXPECTED, (
        f"the scan resolved {resolved} first-party imports inside "
        f"ImportError-swallowing constructs, expected {_GUARDED_IMPORTS_EXPECTED}. "
        "Either the scan stopped recognising the construct (a green result would "
        "then mean nothing), or the population legitimately changed — in which "
        "case move _GUARDED_IMPORTS_EXPECTED deliberately."
    )


# A module name guaranteed never to exist, so these probes pin the scan's rule
# rather than the current absence of some real module. Naming
# `faultmaven.api.v1.routes` here would turn several tests red the day somebody
# legitimately adds `faultmaven/api/v1/routes.py`, for a reason none of their
# messages would mention.
_ABSENT = "faultmaven.__no_such_module__"


def _violations(tmp_path, source) -> list[str]:
    probe = tmp_path / "probe_module.py"
    probe.write_text(source)
    violations, _, unreadable = _scan([probe])
    assert unreadable == [], unreadable
    return violations


@pytest.mark.parametrize(
    "source, line, why",
    [
        pytest.param(
            f"try:\n    from {_ABSENT} import case\nexcept ImportError:\n    case = None\n",
            2,
            "the #947 shape itself",
            id="missing-module",
        ),
        pytest.param(
            "try:\n    import tests.__no_such_module__\nexcept ImportError:\n    pass\n",
            2,
            "a plain `import` of a module that is not there",
            id="plain-import",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\n"
            "except ModuleNotFoundError:\n    case = None\n",
            2,
            "a missing MODULE does raise ModuleNotFoundError, so this one swallows",
            id="except-ModuleNotFoundError",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\nexcept Exception:\n    case = None\n",
            2,
            "a broad handler swallows the same ImportError",
            id="except-Exception",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\nexcept:\n    case = None\n",
            2,
            "so does a bare except",
            id="bare-except",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\n"
            "except (ValueError, ImportError):\n    case = None\n",
            2,
            "a tuple handler is still a handler",
            id="tuple-handler",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\n"
            "except builtins.ImportError:\n    case = None\n",
            2,
            "a dotted spelling names the same exception",
            id="dotted-handler",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\n"
            "except ValueError:\n    case = 1\nexcept ImportError:\n    case = None\n",
            2,
            "a non-catching handler first does not stop the catching one",
            id="non-catching-handler-first",
        ),
        pytest.param(
            f"try:\n    if True:\n        from {_ABSENT} import case\n"
            "except ImportError:\n    case = None\n",
            3,
            "nesting the import in control flow does not unguard it",
            id="nested-in-body",
        ),
        pytest.param(
            f"try:\n    class C:\n        from {_ABSENT} import case\n"
            "except ImportError:\n    C = None\n",
            3,
            "a CLASS body executes at definition time, so it IS guarded",
            id="class-body",
        ),
        pytest.param(
            f"try:\n    class Outer:\n        class Inner:\n            from {_ABSENT} import case\n"
            "except ImportError:\n    Outer = None\n",
            4,
            "and so does a class nested in a class",
            id="class-in-class",
        ),
        pytest.param(
            "try:\n    from scripts.__no_such_module__ import x\nexcept ImportError:\n    x = None\n",
            2,
            "`scripts` is a repo-root package, so its absent submodule counts",
            id="scripts-is-first-party",
        ),
        pytest.param(
            "try:\n    import demo.__no_such_module__\nexcept ImportError:\n    pass\n",
            2,
            "and so is `demo`",
            id="demo-is-first-party",
        ),
        pytest.param(
            f"with contextlib.suppress(ImportError):\n    from {_ABSENT} import case\n",
            2,
            "contextlib.suppress is the same swallow in modern spelling",
            id="contextlib-suppress",
        ),
        pytest.param(
            f"with ctx.suppress(ImportError):\n    from {_ABSENT} import case\n",
            2,
            "an aliased contextlib module suppresses just as well",
            id="aliased-suppress-module",
        ),
    ],
)
def test_the_scan_catches_every_spelling(tmp_path, source, line, why):
    """Fed through the REAL entry point, not a re-implementation of its filter.

    Each asserts the module name AND the line number, not merely that *a*
    violation appeared. Asserting on the shared prose ("no such module") was
    vacuous — ``_scan`` has one ``append`` and that literal is always in it — so
    a mutation reporting the wrong line, or resolving a different module, passed
    every probe.
    """
    violations = _violations(tmp_path, source)
    assert len(violations) == 1, why
    assert f"probe_module.py:{line}:" in violations[0], violations[0]
    named = violations[0].split("`")[1]
    assert named.endswith("__no_such_module__"), violations[0]
    assert named in source, f"reported {named!r}, which the probe never imports"


@pytest.mark.parametrize(
    "source, why",
    [
        pytest.param(
            "try:\n    from faultmaven.config.settings import get_settings\n"
            "except ImportError:\n    get_settings = None\n",
            "a first-party import that resolves is the whole point",
            id="resolves",
        ),
        pytest.param(
            "try:\n    from faultmaven.config.settings import no_such_name_at_all\n"
            "except ImportError:\n    no_such_name_at_all = None\n",
            "the name half is deliberately out of scope; the module exists",
            id="missing-name-is-out-of-scope",
        ),
        pytest.param(
            "try:\n    import tavily\nexcept ImportError:\n    tavily = None\n",
            "a third-party optional dependency is a different question",
            id="third-party",
        ),
        pytest.param(
            "try:\n    import scripts.brand_lint\nexcept ImportError:\n    pass\n",
            "a repo-root module that does exist resolves like any other",
            id="scripts-module-that-exists",
        ),
        pytest.param(
            "try:\n    import alembic\nexcept ImportError:\n    alembic = None\n",
            "`alembic` is also the installed library, so it stays third-party",
            id="alembic-is-not-first-party",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\nexcept ImportError:\n    raise\n",
            "a handler that re-raises swallows nothing",
            id="re-raise",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\n"
            "except ImportError:\n    raise\nexcept Exception:\n    case = None\n",
            "first matching handler wins: the later broad arm never runs",
            id="re-raise-then-broad-handler",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\n"
            "except ImportError:\n    if not OPTIONAL:\n        raise\n    case = None\n",
            "a conditional re-raise may propagate, so it is not proven a swallow",
            id="conditional-re-raise",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\n"
            "except ProjectError:\n    case = 1\nexcept ImportError:\n    case = None\n",
            "an unresolvable first arm may be an ImportError subclass and claim it",
            id="project-exception-first",
        ),
        pytest.param(
            f"try:\n    def helper():\n        from {_ABSENT} import case\n"
            "except ImportError:\n    helper = None\n",
            "only the `def` runs under the guard; the import fails loudly later",
            id="import-inside-a-def",
        ),
        pytest.param(
            f"try:\n    def helper():\n        class C:\n            from {_ABSENT} import case\n"
            "except ImportError:\n    helper = None\n",
            "a class inside a def still only runs when the def is called",
            id="class-inside-a-def",
        ),
        pytest.param(
            "from typing import TYPE_CHECKING\n"
            f"try:\n    if TYPE_CHECKING:\n        from {_ABSENT} import case\n"
            "except ImportError:\n    pass\n",
            "TYPE_CHECKING is False at runtime, so that body never runs",
            id="type-checking-block",
        ),
        pytest.param(
            f"try:\n    pass\nexcept ImportError:\n    from {_ABSENT} import case\n",
            "an import in the handler is not swallowed by it",
            id="import-in-handler",
        ),
        pytest.param(
            f"try:\n    pass\nexcept ImportError:\n    pass\nelse:\n    from {_ABSENT} import case\n",
            "`else` is outside the handler's reach — this one raises loudly",
            id="import-in-else",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\nexcept ValueError:\n    case = None\n",
            "a handler that cannot catch ImportError does not hide one",
            id="unrelated-handler",
        ),
        pytest.param(
            f"try:\n    from {_ABSENT} import case\nexcept OSError:\n    case = None\n",
            "OSError is not an ImportError superclass either",
            id="oserror-handler",
        ),
        pytest.param(
            f"with contextlib.suppress(ValueError):\n    from {_ABSENT} import case\n",
            "suppress of something else suppresses nothing here",
            id="suppress-other-exception",
        ),
        pytest.param(
            f"from {_ABSENT} import case\n",
            "an unguarded import fails loudly at collection; that is fine",
            id="unguarded",
        ),
        pytest.param(
            "try:\n    from . import sibling\nexcept ImportError:\n    sibling = None\n",
            "a relative import carries no absolute name to resolve",
            id="relative",
        ),
        pytest.param(
            "try:\n    from faultmaven.api import v1\nexcept ImportError:\n    v1 = None\n",
            "a namespace package with no __init__.py is still a module",
            id="namespace-package",
        ),
    ],
)
def test_the_scan_leaves_the_safe_shapes_alone(tmp_path, source, why):
    """Flagging these would train readers to ignore the scan."""
    assert _violations(tmp_path, source) == [], why


def test_first_party_roots_are_repo_directories_and_alembic_is_not():
    """The ``_FIRST_PARTY`` set is grounded, not a guess.

    Every name in it must be a directory in this repo — otherwise the scan
    calls a third-party import first-party and reddens on an uninstalled
    dependency. And ``alembic`` must stay out: the repo has an ``alembic/``
    directory AND the installed migration library answers to the same name, so
    a guarded ``import alembic`` is the optional-dependency question, not this
    one. Both halves are checked because only the second is counter-intuitive.
    """
    import importlib.util

    for name in _FIRST_PARTY:
        assert (REPO_ROOT / name).is_dir(), f"{name} is not a directory in this repo"

    assert "alembic" not in _FIRST_PARTY
    assert (REPO_ROOT / "alembic").is_dir(), "the premise changed: no alembic/ dir"
    spec = importlib.util.find_spec("alembic")
    origin = spec.origin or ""
    assert "site-packages" in origin, (
        "`alembic` no longer resolves to the installed library "
        f"(resolved to {origin!r}) — revisit whether it belongs in _FIRST_PARTY"
    )


def test_an_absent_path_scans_to_nothing():
    """``_scan`` tolerates a path that is not there rather than erroring."""
    assert _scan([REPO_ROOT / "does-not-exist-and-need-not.py"]) == ([], 0, [])


def test_an_unparseable_file_is_reported_not_skipped(tmp_path):
    """A file the scan cannot parse must be visible, or the population shrinks silently.

    The whole-tree test asserts this list is empty, so a test file the scan
    cannot read fails the gate instead of quietly dropping out of it.
    """
    bad = tmp_path / "broken.py"
    bad.write_text("try:\n    from x import (\nexcept ImportError:\n")
    violations, resolved, unreadable = _scan([bad])
    assert unreadable, "a syntax error must be reported"
    assert (violations, resolved) == ([], 0)


def test_an_undecodable_file_is_reported_not_skipped(tmp_path):
    """Nor may a file be dropped for not being UTF-8."""
    bad = tmp_path / "latin1.py"
    bad.write_bytes(b"# except \xff\xfe not utf-8\n")
    violations, resolved, unreadable = _scan([bad])
    assert unreadable, "a decode error must be reported"
    assert (violations, resolved) == ([], 0)


def test_a_mixed_file_is_counted_both_ways(tmp_path):
    """The pin guards a real population, not an incidental number.

    One resolvable and one unresolvable guarded import in the same file must
    report exactly one of each — otherwise ``resolved`` could drift into
    counting something else and the pin would still read green.
    """
    probe = tmp_path / "probe_module.py"
    probe.write_text(
        "try:\n"
        "    from faultmaven.config.settings import get_settings\n"
        f"    from {_ABSENT} import case\n"
        "except ImportError:\n"
        "    get_settings = case = None\n"
    )
    violations, resolved, unreadable = _scan([probe])
    assert unreadable == []
    assert len(violations) == 1, violations
    assert resolved == 1, resolved
