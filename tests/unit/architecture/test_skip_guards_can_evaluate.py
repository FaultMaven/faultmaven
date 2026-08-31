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

Deliberately NOT banned:

* ``@pytest.mark.skip(reason=...)`` as a decorator is honest and
  self-declaring. It misleads nobody. It is bounded by the registry below so
  that turning a test off stays a visible decision rather than a line that
  disappears into a 40,000-line tree.
* A skip mark used as a **value** — ``item.add_marker(pytest.mark.skip(...))``
  in a ``pytest_collection_modifyitems`` hook, or ``pytest.param(x,
  marks=pytest.mark.skip(...))``. Those are pytest's own documented APIs for
  deciding at collection time, and one parameter being skipped is not the test
  being off. Flagging them would also corrupt the registry, since the only key
  available is the test's own name.
* A helper that calls ``pytest.skip()`` for its callers
  (``def _require_service(): ...``). Extracting a guard is the standard
  refactor; only an unconditional skip inside a **test** disables a test.

Enforcement is shape-based rather than name-based, and folds constants, so it
cannot be sidestepped by moving the literal one indirection away.

WHAT THIS STILL CANNOT SEE, stated so nobody reads a green result as more than
it is: a guard whose condition is real but true in every job that exists
(``skipif(not os.environ.get("NEVER_SET"))``), and **deselection** — a marker
no job selects disables a test with no skip construct at all, and produces no
"N skipped" line for any gate to notice. Both need knowledge of the CI matrix,
which is not in this file. #1257's own fix shipped an instance of the first and
review, not this scan, is what caught it.
"""

import ast
import functools
import operator
import pathlib

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TESTS_ROOT = REPO_ROOT / "tests"

# A scan that walked zero files reports zero violations and passes. Moving this
# test, or a layout change, would do exactly that — silently.
_MIN_FILES_EXPECTED = 600

# Tests deliberately turned off, unconditionally.
#
# Every entry is a test that is NOT running. That is the point of listing them:
# an unconditional skip is legitimate, but it should be a decision someone made
# and can find again. Adding an entry is how you turn a test off; the scan
# fails until you do.
#
# Keys only, no reasons. An earlier draft stored the reason beside the key and
# the two had ALREADY drifted from the `reason=` literals by the time review
# read them — reproducing, one level up, this module's own thesis that a
# duplicated prose string is not checked against reality. The reason belongs at
# the skip, which is its single source of truth.
#
# Keyed `<path relative to tests/>::<dotted owner>`. The path is relative
# rather than a bare filename because three basenames are already duplicated
# under tests/, and a bare name would let one registration silently authorise
# a same-named skip in a sibling directory.
ALLOWED_UNCONDITIONAL_SKIPS: frozenset[str] = frozenset(
    {
        "benchmarks/test_knowledge_search.py::TestKnowledgeSearchPerformance",
        "integration/test_case_service_integration.py::"
        "TestConcurrentOperations.test_concurrent_case_creation",
        "integration/test_case_service_integration.py::"
        "TestConcurrentOperations.test_concurrent_updates_same_case",
        "integration/test_kb_ingestion_and_indexing.py::"
        "test_upload_lists_and_indexes_in_chroma",
        "integration/test_mock_verification.py::"
        "test_mock_interception_patch_get_auth_service",
        "integration/test_mock_verification.py::test_no_auth_returns_401",
        "integration/test_mock_verification.py::"
        "test_with_mock_using_override_dependency",
        "unit/architecture/test_configuration_compliance.py::"
        "TestConfigurationArchitectureCompliance."
        "test_settings_validation_with_invalid_values",
        "unit/infrastructure/persistence/test_case_repository_reports.py::"
        "test_postgresql_add_report",
    }
)

_COMPARE_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

# Builtins that are pure and total on literal arguments. Folding a call is only
# ever attempted when EVERY argument already folded to a literal, so a real
# guard like `bool(os.environ.get("X"))` never reaches these.
_PURE_BUILTINS = {
    "bool": bool,
    "int": int,
    "float": float,
    "str": str,
    "len": len,
    "any": any,
    "all": all,
    "sorted": sorted,
    "tuple": tuple,
    "list": list,
    "set": set,
    "frozenset": frozenset,
}

_NOT_LITERAL = object()


class _Names:
    """What ``pytest``, ``pytest.mark`` and ``pytest.skip`` are called here.

    ``import pytest as pt`` and ``from pytest import mark, skip`` are the same
    constructs under different spellings, and a scan that keys on the literal
    string "pytest" sees neither. ``ast.ImportFrom`` is handled beside
    ``ast.Import`` for exactly that reason — an earlier draft resolved aliases
    only for the first spelling, which left the hole the resolution exists to
    close open for the second.
    """

    def __init__(self, tree: ast.Module):
        self.pytest: set[str] = set()
        self.mark: set[str] = set()
        self.skip: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "pytest":
                        self.pytest.add(alias.asname or "pytest")
            elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name == "mark":
                        self.mark.add(local)
                    elif alias.name == "skip":
                        self.skip.add(local)
        if not self.pytest:
            self.pytest.add("pytest")

    def marker(self, node: ast.AST) -> str | None:
        """The trailing name of a pytest mark chain, in any spelling."""
        parts: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            return None
        parts.append(cur.id)
        parts.reverse()
        # pytest.mark.X  /  pt.mark.X
        if len(parts) >= 3 and parts[-3] in self.pytest and parts[-2] == "mark":
            return parts[-1]
        # mark.X, from `from pytest import mark`
        if len(parts) == 2 and parts[0] in self.mark:
            return parts[1]
        return None

    def is_skip_call(self, func: ast.AST) -> bool:
        """``pytest.skip(...)`` or a bare ``skip(...)`` imported from pytest."""
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "skip"
            and isinstance(func.value, ast.Name)
            and func.value.id in self.pytest
        ):
            return True
        return isinstance(func, ast.Name) and func.id in self.skip


def _module_constants(tree: ast.Module) -> dict[str, object]:
    """Module-scope names bound EXACTLY ONCE to a literal.

    Without this, ``_ALWAYS = True`` on one line and ``skipif(_ALWAYS, ...)``
    on the next satisfies a naive check while being exactly the defect.

    The single-binding rule is what makes it sound in the other direction. A
    name written ``FLAG = True`` and then rebound ``FLAG = os.environ.get(...)``
    is NOT a literal at the point of use, and an earlier draft reported it as
    "the literal True" because it kept the first binding and let the rebinding
    fall through an exception handler. Any name assigned more than once, or
    assigned anything that does not fold, is dropped.
    """
    seen: dict[str, object] = {}
    disqualified: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            target = node.target
            if isinstance(target, ast.Name):
                disqualified.add(target.id)
            continue
        else:
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, SyntaxError, TypeError):
            literal = _NOT_LITERAL
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id in seen or literal is _NOT_LITERAL:
                disqualified.add(target.id)
            else:
                seen[target.id] = literal
    return {k: v for k, v in seen.items() if k not in disqualified}


def _fold(node: ast.AST, consts: dict[str, object]):
    """Constant-fold a condition, or return ``_NOT_LITERAL``.

    Folding rather than an ``isinstance(node, ast.Constant)`` check, because
    every cheap disguise of a literal is the same defect and would otherwise
    walk straight through. Review found four spellings this missed on the first
    pass — ``True if True else False``, ``bool(1)``, ``f"True"`` and
    ``1 in [1, 2]`` — so each has its own branch and its own positive control
    below.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id, _NOT_LITERAL)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _fold(node.operand, consts)
        return _NOT_LITERAL if inner is _NOT_LITERAL else (not inner)
    if isinstance(node, ast.IfExp):
        test = _fold(node.test, consts)
        if test is _NOT_LITERAL:
            return _NOT_LITERAL
        return _fold(node.body if test else node.orelse, consts)
    if isinstance(node, ast.JoinedStr):
        # An f-string with no interpolation is just a string literal.
        if all(isinstance(v, ast.Constant) for v in node.values):
            return "".join(str(v.value) for v in node.values)
        return _NOT_LITERAL
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        func = _PURE_BUILTINS.get(node.func.id)
        if func is None or node.keywords:
            return _NOT_LITERAL
        args = [_fold(a, consts) for a in node.args]
        if any(a is _NOT_LITERAL for a in args):
            return _NOT_LITERAL
        try:
            return func(*args)
        except Exception:
            return _NOT_LITERAL
    if isinstance(node, ast.BoolOp):
        values = [_fold(v, consts) for v in node.values]
        known = [v for v in values if v is not _NOT_LITERAL]
        # Short-circuit first: `True or <anything>` is truthy whatever the
        # other operand does, and `False and <anything>` is falsy — so one
        # literal operand can fix the whole expression even when its
        # neighbours are real conditions.
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
            result = (
                (result and value)
                if isinstance(node.op, ast.And)
                else (result or value)
            )
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


def _condition_is_literal(cond: ast.AST, consts: dict[str, object]) -> str | None:
    """A rendering of the fixed value, or None when the condition can vary.

    A condition that folds to a **string** is re-parsed before being judged:
    pytest ``eval``s a string condition in the module namespace, so
    ``skipif("sys.platform == 'win32'")`` is a real guard while
    ``skipif("True")`` — and ``skipif(f"True")`` — is the defect in quotes.
    """
    folded = _fold(cond, consts)
    if folded is _NOT_LITERAL:
        return None
    if isinstance(folded, str):
        try:
            inner = ast.parse(folded, mode="eval").body
        except SyntaxError:
            return None
        inner_folded = _fold(inner, consts)
        if inner_folded is _NOT_LITERAL:
            return None
        return f"{folded!r} (evaluated)"
    return repr(folded)


def _owner_index(tree: ast.Module) -> list[tuple[int, int, str]]:
    """Dotted line spans of every def/class, for attributing a marker.

    Dotted (``TestFoo.test_bar``) rather than bare, so a registry entry names
    one test rather than every same-named method in the file. The span starts
    at the first DECORATOR rather than at ``def``: a decorated function's
    ``lineno`` points at the ``def`` line, so a mark written above it would
    otherwise fall outside its own function.
    """
    spans: list[tuple[int, int, str]] = []

    def walk(node, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                spans.append((start, child.end_lineno or child.lineno, name))
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    # Innermost wins: sort by span width, narrowest last.
    spans.sort(key=lambda s: s[1] - s[0], reverse=True)
    return spans


def _owner(spans, lineno: int) -> str:
    name = "<module>"
    for start, end, candidate in spans:
        if start <= lineno <= end:
            name = candidate
    return name


def _decorator_and_pytestmark_marks(tree: ast.Module):
    """Skip marks that disable a test at authoring time, and only those.

    Restricted to decorators and ``pytestmark`` assignments on purpose. The
    same expression handed to ``item.add_marker(...)`` or to
    ``pytest.param(marks=...)`` is pytest's own API being used correctly, and
    an earlier draft flagged both — telling the author of a collection hook to
    register it as a disabled test, and attributing a skipped *parameter* to
    the whole test, which would have written a falsehood into the registry.
    """
    found: list[tuple[ast.AST, int]] = []

    def mark_exprs(value: ast.AST):
        if isinstance(value, (ast.List, ast.Tuple)):
            return list(value.elts)
        return [value]

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                found.append((dec, dec.lineno))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    for expr in mark_exprs(node.value):
                        found.append((expr, expr.lineno))
    return found


def scan(paths) -> tuple[list[str], set[str]]:
    """Violations, and the live registry keys, in ONE pass over ``paths``.

    Returns both because the registry-hygiene check needs exactly the key set
    this already computes; making it re-derive them doubled the cost of the
    gate for no added signal.

    Takes the paths so a synthetic violating file can be pushed through this
    exact function. A scan only ever run against a clean tree passes just as
    well when the scan itself is broken.
    """
    violations: list[str] = []
    live: set[str] = set()

    for path in sorted(paths):
        try:
            rel = str(path.relative_to(TESTS_ROOT))
        except ValueError:
            rel = path.name
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            # Reported rather than raised: one unreadable file should fail the
            # gate with a message naming it, not take it down with a traceback
            # from inside a list comprehension.
            violations.append(f"{path}: could not be parsed for scanning ({exc})")
            continue

        names = _Names(tree)
        consts = _module_constants(tree)
        spans = _owner_index(tree)

        # --- skipif: a literal condition is wrong wherever it appears, ---
        # --- including in a `marks=` list or a module-level constant.  ---
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if names.marker(node.func) != "skipif":
                continue
            cond = node.args[0] if node.args else None
            for kw in node.keywords:
                if kw.arg == "condition":
                    cond = kw.value
            if cond is None:
                violations.append(f"{path}:{node.lineno}: skipif with no condition")
                continue
            rendered = _condition_is_literal(cond, consts)
            if rendered is not None:
                violations.append(
                    f"{path}:{node.lineno}: skipif condition is the literal "
                    f"{rendered} — it evaluates nothing, so this test is "
                    f"disabled in every environment. Use a real condition, or "
                    f"pytest.mark.skip(reason=...) and register it in "
                    f"ALLOWED_UNCONDITIONAL_SKIPS."
                )

        # --- unconditional skip marks, only where they disable a test ---
        for expr, lineno in _decorator_and_pytestmark_marks(tree):
            target = expr.func if isinstance(expr, ast.Call) else expr
            if names.marker(target) != "skip":
                continue
            owner = _owner(spans, lineno)
            key = f"{rel}::{owner}"
            live.add(key)
            if not isinstance(expr, ast.Call):
                violations.append(
                    f"{path}:{lineno}: bare @pytest.mark.skip on `{owner}` — "
                    f"no reason, no condition."
                )
            elif key not in ALLOWED_UNCONDITIONAL_SKIPS:
                violations.append(
                    f"{path}:{lineno}: unconditional pytest.mark.skip on "
                    f"`{owner}` is not registered. Add '{key}' to "
                    f"ALLOWED_UNCONDITIONAL_SKIPS, or delete the test."
                )

        # --- pytest.skip() reached on every entry to a TEST body ---
        # A helper that skips for its callers is the standard extract-a-guard
        # refactor, so only a test function (and module scope) counts.
        holders: list[tuple[ast.AST, str]] = [(tree, "<module>")]
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith("test_"):
                holders.append((node, node.name))
        for holder, label in holders:
            for stmt in holder.body:
                call = stmt.value if isinstance(stmt, ast.Expr) else None
                if isinstance(call, ast.Call) and names.is_skip_call(call.func):
                    owner = _owner(spans, stmt.lineno)
                    violations.append(
                        f"{path}:{stmt.lineno}: unconditional pytest.skip() in "
                        f"`{owner if label != '<module>' else '<module>'}` — "
                        f"the body never runs."
                    )

    return violations, live


@functools.lru_cache(maxsize=1)
def _tree_scan() -> tuple[tuple[str, ...], frozenset[str], int]:
    """One pass over the real tree, shared by both gate tests.

    Cached results, not cached ASTs: the trees are freed per file, so this
    costs two small string collections rather than 700 syntax trees held for
    the length of the suite.
    """
    files = sorted(TESTS_ROOT.rglob("*.py"))
    violations, live = scan(files)
    return tuple(violations), frozenset(live), len(files)


def test_no_test_is_disabled_by_a_guard_that_cannot_evaluate():
    """A new ``skipif(True)`` fails here rather than in a year's triage.

    This is what closes the class. Fixing the four instances was necessary and
    on its own insufficient: the shape had already been copied three times
    inside one file before anyone looked.
    """
    violations, _, file_count = _tree_scan()
    assert TESTS_ROOT.is_dir(), f"{TESTS_ROOT} is not a directory"
    assert file_count >= _MIN_FILES_EXPECTED, (
        f"scanned only {file_count} files under {TESTS_ROOT} — the scan is not "
        "reaching the test tree, so a green result means nothing"
    )
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
    _, live, _ = _tree_scan()
    stale = sorted(ALLOWED_UNCONDITIONAL_SKIPS - live)
    assert not stale, (
        "ALLOWED_UNCONDITIONAL_SKIPS names skips that no longer exist — remove "
        f"them: {stale}"
    )


# --------------------------------------------------------------------------- #
# The scan, fed the shapes it exists to catch
# --------------------------------------------------------------------------- #


def _violations(tmp_path, source: str) -> list[str]:
    probe = tmp_path / "test_probe_module.py"
    probe.write_text(source)
    return scan([probe])[0]


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
            '@pytest.mark.skipif(f"True", reason="nope")\n'
            "def test_x():\n    assert True\n",
            "an f-string with no interpolation is a string literal",
            id="fstring-condition",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif(not False, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "constant folding through `not`",
            id="folded-not",
        ),
        pytest.param(
            "import pytest\nimport sys\n\n\n"
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
            '@pytest.mark.skipif(1 in [1, 2], reason="nope")\n'
            "def test_x():\n    assert True\n",
            "membership is a comparison too",
            id="folded-in",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif(True if True else False, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "a conditional expression over literals is a literal",
            id="folded-ifexp",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif(bool(1), reason="nope")\n'
            "def test_x():\n    assert True\n",
            "a pure builtin over literals is a literal",
            id="folded-builtin-call",
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
            "an aliased pytest import is not an escape hatch",
            id="aliased-pytest",
        ),
        pytest.param(
            "from pytest import mark\n\n\n"
            '@mark.skipif(True, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "`from pytest import mark` is the other spelling of the same thing",
            id="from-import-mark",
        ),
        pytest.param(
            "from pytest import mark as m\n\n\n"
            '@m.skipif(True, reason="nope")\n'
            "def test_x():\n    assert True\n",
            "...and it can be aliased too",
            id="from-import-mark-aliased",
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
            "from pytest import skip\n\n\n"
            "def test_x():\n"
            '    skip("nope")\n'
            "    assert True\n",
            "`from pytest import skip` is the same disable, unqualified",
            id="in-body-skip-from-import",
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
    scan is broken; these push each spelling through ``scan`` itself. Every
    false negative review found has its own case here, so the fix for it has an
    independent control that does not depend on the real tree.
    """
    assert _violations(tmp_path, source), why


@pytest.mark.parametrize(
    "source, why",
    [
        pytest.param(
            "import pytest\nimport sys\n\n\n"
            '@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\n'
            "def test_x():\n    assert True\n",
            "the ordinary environment-dependent guard",
            id="platform-check",
        ),
        pytest.param(
            "import importlib.util\nimport pytest\n\n\n"
            '_HAVE = importlib.util.find_spec("boto3") is not None\n\n\n'
            '@pytest.mark.skipif(not _HAVE, reason="cloud-only dependency")\n'
            "def test_x():\n    assert True\n",
            "the shape #1257 was fixed to — a computed flag is not a literal",
            id="computed-flag",
        ),
        pytest.param(
            "import os\nimport pytest\n\n\n"
            '@pytest.mark.skipif(not os.environ.get("REDIS_HOST"), reason="unset")\n'
            "def test_x():\n    assert True\n",
            "an environment-variable gate evaluates per job",
            id="env-gate",
        ),
        pytest.param(
            "import os\nimport pytest\n\n\n"
            "FLAG = True\n"
            'FLAG = os.environ.get("X") is not None\n\n\n'
            '@pytest.mark.skipif(FLAG, reason="computed")\n'
            "def test_x():\n    assert True\n",
            "a rebound name is not the literal it was first bound to",
            id="rebound-constant",
        ),
        pytest.param(
            "import os\nimport pytest\n\n\n"
            '@pytest.mark.skipif(bool(os.environ.get("X")), reason="unset")\n'
            "def test_x():\n    assert True\n",
            "a builtin over a non-literal argument does not fold",
            id="builtin-over-runtime-value",
        ),
        pytest.param(
            "import pytest\n\n\n"
            "try:\n"
            "    import boto3\n\n"
            "    HAVE = True\n"
            "except ImportError:\n"
            "    HAVE = False\n\n\n"
            '@pytest.mark.skipif(not HAVE, reason="optional dependency")\n'
            "def test_x():\n    assert True\n",
            "the try/except availability idiom binds the name twice, so neither "
            "branch's literal is the value at the point of use — flagging it "
            "would fire on the commonest optional-dependency guard there is",
            id="try-except-availability-flag",
        ),
        pytest.param(
            "import pytest\n\n\n"
            '@pytest.mark.skipif("sys.platform == \'win32\'", reason="posix")\n'
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
            "def _require_service():\n"
            '    pytest.skip("service not configured")\n\n\n'
            "def test_x():\n"
            "    _require_service()\n"
            "    assert True\n",
            "extracting a guard into a helper is the standard refactor",
            id="skip-helper",
        ),
        pytest.param(
            "import pytest\n\n\n"
            "def pytest_collection_modifyitems(config, items):\n"
            "    for item in items:\n"
            '        item.add_marker(pytest.mark.skip(reason="deselected"))\n',
            "pytest's own documented collection hook",
            id="add-marker-hook",
        ),
        pytest.param(
            "import pytest\n\n\n"
            "@pytest.mark.parametrize(\n"
            "    'n',\n"
            "    [1, pytest.param(2, marks=pytest.mark.skip(reason='known bad'))],\n"
            ")\n"
            "def test_x(n):\n    assert n\n",
            "one skipped parameter is not the test being off",
            id="param-marks-skip",
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
    """Flagging these would train readers to ignore the scan.

    A gate that bans legitimate patterns does not get obeyed; it gets deleted
    by the next person who hits it. Four of these are false positives review
    found in the first draft.
    """
    assert _violations(tmp_path, source) == [], why


def test_a_bare_skip_mark_is_flagged_even_when_registered(tmp_path, monkeypatch):
    """The bare-mark branch has to be observable on its own.

    A bare ``@pytest.mark.skip`` is normally also caught by the
    "not registered" branch, which made the bare-mark check untestable and, as
    a mutation showed, removable without any control noticing. Registering the
    key isolates it: a mark with no reason at all is still wrong, because the
    registry records THAT a test is off and the reason records WHY.
    """
    probe = tmp_path / "test_bare.py"
    probe.write_text(
        "import pytest\n\n\n@pytest.mark.skip\ndef test_x():\n    assert True\n"
    )
    _, live = scan([probe])
    assert live, "the probe produced no registry key"

    monkeypatch.setitem(globals(), "ALLOWED_UNCONDITIONAL_SKIPS", frozenset(live))
    violations, _ = scan([probe])
    assert violations, "a registered BARE mark must still be flagged"
    assert "no reason" in violations[0], violations


def test_an_unparseable_file_is_reported_not_raised(tmp_path):
    """One bad file must fail the gate with its name, not a raw traceback."""
    probe = tmp_path / "test_broken.py"
    probe.write_text("def test_x(:\n")
    violations, _ = scan([probe])
    assert violations and "could not be parsed" in violations[0]


def test_the_registry_key_carries_the_directory(tmp_path):
    """Three basenames are already duplicated under tests/.

    A bare filename key would let one registration authorise a same-named skip
    in a sibling directory, which is a silent hole in the inventory.
    """
    nested = tmp_path / "sub"
    nested.mkdir()
    probe = nested / "test_dupe.py"
    probe.write_text(
        'import pytest\n\n\n@pytest.mark.skip(reason="off")\ndef test_x():\n'
        "    assert True\n"
    )
    _, live = scan([probe])
    assert live == {"test_dupe.py::test_x"} or all(
        key.endswith("test_dupe.py::test_x") for key in live
    ), live
    # A class-qualified owner distinguishes same-named methods in one file.
    probe2 = nested / "test_two_classes.py"
    probe2.write_text(
        "import pytest\n\n\n"
        "class TestA:\n"
        '    @pytest.mark.skip(reason="off")\n'
        "    def test_x(self):\n        assert True\n\n\n"
        "class TestB:\n"
        "    def test_x(self):\n        assert True\n"
    )
    _, live2 = scan([probe2])
    assert any("TestA.test_x" in key for key in live2), live2
