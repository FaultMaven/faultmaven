"""A test's skip guard must be able to evaluate, enforced by scan.

The defect (#1257): ``TestS3StorageBackend`` carried

    @pytest.mark.skipif(condition=True, reason="boto3 not installed")

for its whole life. ``condition=True`` evaluates nothing, so its five tests
were skipped in every job — including Test Cloud, which installs
``boto3==1.42.90`` and where the reason string was simply false — and none of
them had ever run. Nothing reports this: a skipped test is green, and a reason
string is prose that no one checks against reality. The same shape sat on three
more tests in ``tests/health/test_docker_health.py``, so it was a class rather
than an accident.

The rule enforced here is narrow and mechanical: **a ``skipif`` condition must
not be a literal.** A guard whose value is fixed at authoring time is not a
guard; it is a disabled test wearing a guard's clothes, and the misdirection is
the harm — a reader (and a reviewer) sees "skipped: boto3 not installed" and
concludes the environment is at fault rather than the decorator.

Deliberately NOT banned: ``@pytest.mark.skip(reason=...)``, which is an honest,
self-declaring "this test is off". It misleads nobody. It is still bounded, by
the registry below, so that turning a test off stays a visible decision instead
of a line in a diff.

Enforcement is shape-based rather than name-based, and folds constants, so it
cannot be sidestepped by moving the literal one indirection away.
"""

import ast
import operator
import pathlib

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TESTS_ROOT = REPO_ROOT / "tests"

# A scan that walked zero files reports zero violations and passes. Moving this
# test, or a layout change, would do exactly that — silently.
_MIN_FILES_EXPECTED = 600

# Tests deliberately turned off, unconditionally, with the reason they carry.
#
# Every entry is a test that is NOT running. That is the point of listing them:
# an unconditional skip is legitimate, but it should be a decision someone made
# and can find again, not a line that disappears into a 40,000-line test tree.
# Adding an entry is how you turn a test off; the scan fails until you do.
#
# Keyed `<file name>::<enclosing def or class>`.
ALLOWED_UNCONDITIONAL_SKIPS: dict[str, str] = {
    "test_knowledge_search.py::TestKnowledgeSearchPerformance": (
        "Knowledge service vector search not yet fully implemented"
    ),
    "test_case_service_integration.py::test_concurrent_case_creation": (
        "Requires the session-per-operation pattern"
    ),
    "test_case_service_integration.py::test_concurrent_updates_same_case": (
        "Requires the session-per-operation pattern"
    ),
    "test_kb_ingestion_and_indexing.py::test_upload_lists_and_indexes_in_chroma": (
        "TestClient does not initialize knowledge_service in app.state"
    ),
    "test_mock_verification.py::test_mock_interception_patch_get_auth_service": (
        "Exploratory test superseded by the auth-mocking patterns in test_cases_api.py"
    ),
    "test_mock_verification.py::test_no_auth_returns_401": (
        "Passes in isolation, fails in the full suite — shared-state pollution"
    ),
    "test_mock_verification.py::test_with_mock_using_override_dependency": (
        "TestClient does not initialize case_service, so the request 401s"
    ),
    "test_configuration_compliance.py::test_settings_validation_with_invalid_values": (
        "Environment variable patching does not work in this test environment"
    ),
    "test_case_repository_reports.py::test_postgresql_add_report": (
        "PostgreSQL-specific SQL — covered by the integration suite"
    ),
}

_COMPARE_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

_NOT_LITERAL = object()


def _pytest_aliases(tree: ast.Module) -> set[str]:
    """Every local name bound to the ``pytest`` module in this file.

    ``import pytest as pt`` then ``@pt.mark.skipif(True, ...)`` is the same
    defect, and a scan keyed on the literal string "pytest" would not see it.
    Resolving the alias is preferred over matching any ``*.mark.skipif``, which
    would fire on unrelated objects that happen to have a ``mark`` attribute.
    """
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    aliases.add(alias.asname or "pytest")
    return aliases or {"pytest"}


def _marker_name(node: ast.AST, aliases: set[str]) -> str | None:
    """The trailing name of a ``<pytest>.mark.X`` attribute chain, else None."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    parts.reverse()
    if len(parts) >= 3 and parts[-3] in aliases and parts[-2] == "mark":
        return parts[-1]
    return None


def _module_constants(tree: ast.Module) -> dict[str, object]:
    """Module-scope ``NAME = <literal>`` bindings.

    Without this, ``_ALWAYS = True`` on one line and ``skipif(_ALWAYS, ...)``
    on the next satisfies a naive check while being exactly the defect. Only
    literals are collected, so a real computed flag (``_BOTO3_AVAILABLE = spec
    is not None and ...``) is not mistaken for one.
    """
    consts: dict[str, object] = {}
    for stmt in tree.body:
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets = [stmt.target]
            value = stmt.value
        else:
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, SyntaxError, TypeError):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                consts[target.id] = literal
    return consts


def _fold(node: ast.AST, consts: dict[str, object]):
    """Constant-fold a condition, or return ``_NOT_LITERAL``.

    Folding rather than an ``isinstance(node, ast.Constant)`` check, because
    every cheap disguise of a literal — ``not False``, ``True or x``, ``1 ==
    1``, a name bound to a literal above — is the same defect and would
    otherwise walk straight through.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id, _NOT_LITERAL)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _fold(node.operand, consts)
        return _NOT_LITERAL if inner is _NOT_LITERAL else (not inner)
    if isinstance(node, ast.BoolOp):
        values = [_fold(v, consts) for v in node.values]
        known = [v for v in values if v is not _NOT_LITERAL]
        # Short-circuit first: `True or <anything>` is truthy whatever the
        # other operand does, and `False and <anything>` is falsy — so one
        # literal operand can fix the whole expression even when its
        # neighbours are real conditions. Folding only when EVERY operand is
        # literal missed exactly that, which is the cheapest way to dress a
        # literal up as a guard.
        if isinstance(node.op, ast.Or):
            for value in known:
                if value:
                    return value
        else:
            for value in known:
                if not value:
                    return value
        if len(known) != len(values):
            return _NOT_LITERAL
        result = values[0]
        for value in values[1:]:
            if isinstance(node.op, ast.And):
                result = result and value
            else:
                result = result or value
        return result
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        func = _COMPARE_OPS.get(type(node.ops[0]))
        if func is None:
            return _NOT_LITERAL
        left = _fold(node.left, consts)
        right = _fold(node.comparators[0], consts)
        if left is _NOT_LITERAL or right is _NOT_LITERAL:
            return _NOT_LITERAL
        try:
            return func(left, right)
        except TypeError:
            return _NOT_LITERAL
    if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        try:
            return ast.literal_eval(node)
        except (ValueError, SyntaxError, TypeError):
            return _NOT_LITERAL
    return _NOT_LITERAL


def _condition_is_literal(cond: ast.AST, consts: dict[str, object]):
    """``(True, rendered_value)`` when the condition cannot vary, else None.

    A **string** condition is re-parsed before being judged: pytest ``eval``s a
    string condition in the module namespace, so ``skipif("sys.platform ==
    'win32'")`` is a real guard while ``skipif("True")`` is the defect spelled
    with quotes.
    """
    if isinstance(cond, ast.Constant) and isinstance(cond.value, str):
        try:
            inner = ast.parse(cond.value, mode="eval").body
        except SyntaxError:
            return None
        folded = _fold(inner, consts)
        return None if folded is _NOT_LITERAL else (True, f"{cond.value!r} (evaluated)")

    folded = _fold(cond, consts)
    return None if folded is _NOT_LITERAL else (True, repr(folded))


def _owner_index(tree: ast.Module) -> list[tuple[int, int, str]]:
    """Line spans of every def/class, innermost-last, for attributing a marker.

    The span starts at the first DECORATOR rather than at ``def``: a decorated
    function's ``lineno`` points at the ``def`` line, so a mark written above it
    would otherwise fall outside its own function.
    """
    spans: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            spans.append((start, node.end_lineno or node.lineno, node.name))
    # Innermost wins: sort by span width, narrowest last.
    spans.sort(key=lambda s: s[1] - s[0], reverse=True)
    return spans


def _owner(spans, lineno: int) -> str:
    name = "<module>"
    for start, end, candidate in spans:
        if start <= lineno <= end:
            name = candidate
    return name


def scan(paths) -> list[str]:
    """Every unrunnable-by-construction test guard under ``paths``.

    Takes the paths so a synthetic violating file can be pushed through this
    exact function. A scan only ever run against a clean tree passes just as
    well when the scan itself is broken.
    """
    violations: list[str] = []
    for path in sorted(paths):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        consts = _module_constants(tree)
        spans = _owner_index(tree)
        aliases = _pytest_aliases(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                name = _marker_name(node.func, aliases)
                if name == "skipif":
                    cond = node.args[0] if node.args else None
                    for kw in node.keywords:
                        if kw.arg == "condition":
                            cond = kw.value
                    if cond is None:
                        violations.append(
                            f"{path}:{node.lineno}: skipif with no condition"
                        )
                        continue
                    literal = _condition_is_literal(cond, consts)
                    if literal is not None:
                        _, rendered = literal
                        violations.append(
                            f"{path}:{node.lineno}: skipif condition is the literal "
                            f"{rendered} — it evaluates nothing, so this test is "
                            f"disabled in every environment. Use a real condition, "
                            f"or pytest.mark.skip(reason=...) and register it in "
                            f"ALLOWED_UNCONDITIONAL_SKIPS."
                        )
                elif name == "skip":
                    owner = _owner(spans, node.lineno)
                    key = f"{path.name}::{owner}"
                    if key not in ALLOWED_UNCONDITIONAL_SKIPS:
                        violations.append(
                            f"{path}:{node.lineno}: unconditional pytest.mark.skip on "
                            f"`{owner}` is not registered. Add "
                            f"'{key}' to ALLOWED_UNCONDITIONAL_SKIPS with its reason, "
                            f"or delete the test."
                        )

            # A bare `@pytest.mark.skip` (no call) carries no reason at all.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for dec in node.decorator_list:
                    if (
                        isinstance(dec, ast.Attribute)
                        and _marker_name(dec, aliases) == "skip"
                    ):
                        violations.append(
                            f"{path}:{dec.lineno}: bare @pytest.mark.skip on "
                            f"`{node.name}` — no reason, no condition."
                        )

        # `pytest.skip()` reached on every entry to a body disables the test
        # from the inside, where no decorator scan and no reviewer will see it.
        for holder in [tree] + [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]:
            for stmt in holder.body:
                call = stmt.value if isinstance(stmt, ast.Expr) else None
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "skip"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in aliases
                ):
                    owner = _owner(spans, stmt.lineno)
                    violations.append(
                        f"{path}:{stmt.lineno}: unconditional pytest.skip() in "
                        f"`{owner}` — the body never runs."
                    )

    return violations


def test_no_test_is_disabled_by_a_guard_that_cannot_evaluate():
    """A new ``skipif(True)`` fails here rather than in a year's triage.

    This is what closes the class. Fixing the four instances was necessary and
    on its own insufficient: the shape had already been copied three times
    inside one file before anyone looked.
    """
    files = sorted(TESTS_ROOT.rglob("*.py"))
    assert TESTS_ROOT.is_dir(), f"{TESTS_ROOT} is not a directory"
    assert len(files) >= _MIN_FILES_EXPECTED, (
        f"scanned only {len(files)} files under {TESTS_ROOT} — the scan is not "
        "reaching the test tree, so a green result means nothing"
    )

    violations = scan(files)
    assert (
        not violations
    ), "tests disabled by a guard that cannot evaluate:\n" + "\n".join(
        f"  - {v}" for v in violations
    )


def test_the_registry_has_no_stale_entries():
    """A registered skip that no longer exists means the registry is fiction.

    Without this, ALLOWED_UNCONDITIONAL_SKIPS silently becomes a graveyard and
    stops describing what is actually turned off — which is the only thing it
    is for.
    """
    live: set[str] = set()
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        spans = _owner_index(tree)
        aliases = _pytest_aliases(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and _marker_name(node.func, aliases) == "skip"
            ):
                live.add(f"{path.name}::{_owner(spans, node.lineno)}")

    stale = sorted(set(ALLOWED_UNCONDITIONAL_SKIPS) - live)
    assert not stale, (
        "ALLOWED_UNCONDITIONAL_SKIPS names skips that no longer exist — remove "
        f"them: {stale}"
    )


# --------------------------------------------------------------------------- #
# The scan, fed the shapes it exists to catch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "source, why",
    [
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif(condition=True, reason="boto3 not installed")\n'
            "def test_x():\n    assert True\n",
            "the #1257 shape, verbatim",
            id="keyword-condition",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif(True, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "positional is the same literal",
            id="positional-condition",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif(1, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "any truthy literal, not just True",
            id="truthy-non-bool",
        ),
        pytest.param(
            "import pytest\n\n\n"
            "_ALWAYS = True\n\n\n"
            '@pytest.mark.skipif(_ALWAYS, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "a literal one indirection away is still a literal",
            id="module-constant",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif("True", reason="nope")\n'
            "def test_x():\n    assert True\n",
            "pytest evals a string condition, so quotes hide nothing",
            id="string-condition",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif(not False, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "constant folding through `not`",
            id="folded-not",
        ),
        pytest.param(
            "import pytest\n\n\n"
            "import sys\n\n\n"
            '@pytest.mark.skipif(True or sys.platform == "win32", reason="nope")\n'
            "def test_x():\n    assert True\n",
            "a short-circuited literal makes the real operand dead",
            id="folded-or",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif(1 == 1, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "constant folding through a comparison",
            id="folded-compare",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif(condition=False, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "a never-skip literal is dead code reading as a guard",
            id="literal-false",
        ),
        pytest.param(
            "import pytest\n\n\n"
            'pytestmark = pytest.mark.skipif(True, reason="nope")\n\n\n'
            "def test_x():\n    assert True\n",
            "module-level pytestmark disables the whole file",
            id="module-pytestmark",
        ),
        pytest.param(
            "import pytest\n\n\n"
            'pytestmark = [pytest.mark.skipif(condition=True, reason="nope")]\n\n\n'
            "def test_x():\n    assert True\n",
            "...and a list of marks is the usual spelling",
            id="module-pytestmark-list",
        ),
        pytest.param(
            "import pytest as pt\n\n\n"
            '@pt.mark.skipif(True, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "an aliased import is not an escape hatch",
            id="aliased-pytest",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skip(reason="unregistered")\n'
            "def test_x():\n    assert True\n",
            "an unregistered unconditional skip",
            id="unregistered-skip",
        ),
        pytest.param(
            "import pytest\n\n\n"
            "@pytest.mark.skip\n"
            "def test_x():\n    assert True\n",
            "a bare mark carries no reason at all",
            id="bare-skip-mark",
        ),
        pytest.param(
            "import pytest\n\n\n"
            "def test_x():\n"
            '    pytest.skip("nope")\n'
            "    assert True\n",
            "skipping from inside the body hides from every decorator scan",
            id="in-body-skip",
        ),
        pytest.param(
            "import pytest\n\n\n"
            'pytest.skip("nope", allow_module_level=True)\n\n\n'
            "def test_x():\n    assert True\n",
            "module-level skip takes the whole file with it",
            id="module-level-skip-call",
        ),
    ],
)
def test_the_scan_catches_every_spelling(tmp_path, source, why):
    """Fed through the REAL entry point, not a re-implementation of its filter.

    Asserting "no violations" over a clean tree passes just as well when the
    scan is broken; these push each spelling through ``scan`` itself.
    """
    probe = tmp_path / "test_probe_module.py"
    probe.write_text(source)
    assert scan([probe]), why


@pytest.mark.parametrize(
    "source, why",
    [
        pytest.param(
            "import pytest\n\n\n"
            "import sys\n\n\n"
            '@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\n'
            "def test_x():\n    assert True\n",
            "the ordinary environment-dependent guard",
            id="platform-check",
        ),
        pytest.param(
            "import importlib.util\n"
            "import pytest\n\n\n"
            '_HAVE = importlib.util.find_spec("boto3") is not None\n\n\n'
            '@pytest.mark.skipif(not _HAVE, reason="cloud-only dependency")\n'
            "def test_x():\n    assert True\n",
            "the shape #1257 was fixed to — a computed flag is not a literal",
            id="computed-flag",
        ),
        pytest.param(
            "import os\n"
            "import pytest\n\n\n"
            '@pytest.mark.skipif(not os.environ.get("REDIS_HOST"), reason="unset")\n'
            "def test_x():\n    assert True\n",
            "an environment-variable gate evaluates per job",
            id="env-gate",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif("sys.platform == \'win32\'", reason="posix only")\n'
            "def test_x():\n    assert True\n",
            "a string condition that references names is a real guard",
            id="string-expression",
        ),
        pytest.param(
            "import pytest\n\n\n"
            "def test_x(tmp_path):\n"
            "    if not tmp_path.exists():\n"
            '        pytest.skip("no tmp")\n'
            "    assert True\n",
            "a conditional in-body skip is a guard, not a disable",
            id="conditional-in-body-skip",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.xfail(reason="ordering", strict=False)\n'
            "def test_x():\n    assert True\n",
            "xfail still runs the test; it is not a disable",
            id="xfail",
        ),
    ],
)
def test_the_scan_leaves_real_guards_alone(tmp_path, source, why):
    """Flagging these would train readers to ignore the scan."""
    probe = tmp_path / "test_probe_module.py"
    probe.write_text(source)
    assert scan([probe]) == [], why
