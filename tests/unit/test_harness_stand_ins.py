"""The harness may substitute what is absent — never what is under test (#942).

``tests/conftest.py`` installs ``sys.modules`` entries before collection so the
suite does not pay for ~690 MiB of torch (#868) or re-register TORCH_LIBRARY.
That is legitimate. What is not legitimate is a substitution that fails **open**,
and the root conftest used to do it in both directions:

* **The stand-in did not look like a module to a non-importing probe.** A
  hand-built module object has ``__spec__ is None``, and
  ``importlib.util.find_spec(name)`` RAISES ``ValueError`` for such a name
  rather than returning a spec. ``find_spec`` is the correct way to ask "is this
  installed" without paying the import, so the harness punished the right
  pattern: the probe answered differently in tests than in production, a green
  suite was not evidence the probe worked, and a red one pointed several layers
  away from the cause (#942, via #939).

* **Real first-party modules were substituted.** Six ``faultmaven.*`` names were
  stood in with ``SimpleNamespace``s of ``Mock``s for the whole session. Four
  name modules that exist on disk and have production importers, so tests that
  appeared to exercise them exercised a ``Mock``; one test had already grown a
  hand-rolled ``del sys.modules[...]`` to reach past them. The other two named
  package paths that do not exist at all — fossils of a layout that moved.

The tests below pin the invariant at three levels: the source shape (every
substitution goes through the one helper), the helper's own refusal, and the
runtime result (what actually ended up in ``sys.modules``).

Each has a positive control, because every check here is a search that reports
success when it finds nothing — a scan that walks zero files, a sweep over an
empty registry, and a probe that never runs all pass while proving nothing.
"""

import ast
import importlib.machinery
import importlib.util
import pathlib
import sys
import types

import pytest

from tests.conftest import (
    HARNESS_STAND_INS,
    OBSERVABILITY_RESET_FIELDS,
    _install_stand_in,
)

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "faultmaven"
TESTS_ROOT = REPO_ROOT / "tests"

# The stand-ins the trap was filed about: the heavy-ML stack the harness
# substitutes on every box, unconditionally. Enumerated rather than counted, so
# the check cannot be satisfied by a registry that shrank, nor by one that
# quietly turned into a different set of names.
_REQUIRED_STAND_INS = frozenset(
    {
        "torch",
        "torch.nn",
        "torch.optim",
        "torch.cuda",
        "transformers",
        "transformers.utils",
        "sentence_transformers",
        "sklearn",
        "sklearn.ensemble",
        "sklearn.preprocessing",
    }
)

# There is deliberately NO separate `len(HARNESS_STAND_INS) >= N` floor here.
# The registry holds 11 entries as measured (the 10 above plus a `ctypes`
# compat shim), so any floor safe to assert would be <= 10 — implied by the
# required set above and therefore unable to ever fire on its own. A floor that
# cannot bind reads as coverage and is not. The enumeration is the floor:
# mutating the helper to stop recording installs reds the assertion below.

# Conftest files the source scan must have walked (12 measured). The floor
# exists because a scan that resolves the wrong directory walks zero files,
# finds zero violations and passes — the mis-resolution this catches lands at
# 0 or 1, so 8 binds on it while leaving room for a legitimate consolidation
# of a few conftests. It is not implied by anything else in this test.
_MIN_CONFTESTS_SCANNED = 8

# First-party modules the shadowing sweep must have examined. Importing the
# four formerly-shadowed modules pulls in 87 transitively when this file runs
# alone, and far more in a full session. The floor exists because a sweep over
# an empty — or wrongly-prefixed — set of names is vacuously clean.
_MIN_FIRST_PARTY_EXAMINED = 60

# The four ``faultmaven.*`` modules the root conftest used to replace with
# ``SimpleNamespace(...=Mock)`` for the whole session, each with its production
# importer. Reverting the conftest change reds every assertion about these.
_FORMERLY_SHADOWED = {
    "faultmaven.core.processing.log_analyzer": "LogProcessor",
    "faultmaven.infrastructure.observability.alerting": "alert_manager",
    "faultmaven.infrastructure.observability.apm_metrics": "metrics_collector",
    "faultmaven.infrastructure.observability.apm_integration": "apm_integration",
}

# Third-party packages that are really installed and that the suite must
# exercise for real. ``pypdf`` was stubbed with an empty ``SimpleNamespace``
# although the venv has had a real pypdf throughout, so ``from pypdf import
# PdfReader`` failed and any ``PYPDF_AVAILABLE``-style probe read False for the
# whole session — which is also why the suite could not be evidence that a
# security bump to pypdf works.
_MUST_BE_REAL = {"pypdf": "PdfReader"}


# --------------------------------------------------------------------------- #
# Level 1 — source shape: every substitution goes through the one helper
# --------------------------------------------------------------------------- #


# Mutating calls on ``sys.modules``. ``setdefault`` is what the removed stubs
# used; ``update`` is the other plausible spelling of the same mistake.
_MUTATING_CALLS = frozenset({"setdefault", "update"})

# The single sanctioned installer, in the root conftest. It is exempt BY NAME —
# not by "it is inside a function", which was the pre-review rule and which
# exempted any local helper anyone cared to write, including a copy of this one.
_SANCTIONED_INSTALLER = "_install_stand_in"


def _sys_modules_writes(
    source: str, label: str, sanctioned: str | None = None
) -> list[str]:
    """Every ``sys.modules`` substitution in ``source``, wherever it appears.

    Takes the source text so synthetic violating files can be pushed through
    this exact function: a scanner only ever run against a clean tree passes
    just as well when the scanner itself is broken.

    The scan is deliberately NOT restricted to module level. The invariant is
    "every substitution goes through the one helper", and an installer called
    from module level does its work inside a function body, so a module-level
    scan cannot see the very shape the sanctioned helper itself has. Exemption
    is therefore by the sanctioned function's NAME, applied only to the root
    conftest; everything else is in scope no matter how deeply nested.

    Three reach-paths for the dict are resolved, because ``sys.modules`` is not
    the only spelling: ``import sys as s`` (``s.modules[...]``) and
    ``from sys import modules`` (a bare ``modules[...]``) install exactly the
    same session-wide substitution.
    """
    tree = ast.parse(source)

    sys_aliases = {"sys"}
    modules_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys":
                    sys_aliases.add(alias.asname or "sys")
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            for alias in node.names:
                if alias.name == "modules":
                    modules_aliases.add(alias.asname or "modules")

    exempt: set[int] = set()
    if sanctioned:
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == sanctioned
            ):
                for descendant in ast.walk(node):
                    exempt.add(id(descendant))

    def _is_sys_modules(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in modules_aliases
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "modules"
            and isinstance(node.value, ast.Name)
            and node.value.id in sys_aliases
        )

    def _leaves(target: ast.AST):
        """Flatten tuple/list unpacking, so ``sys.modules[x], y = ...`` counts."""
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                yield from _leaves(element)
        else:
            yield target

    violations: list[str] = []
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for leaf in _leaves(target):
                    if isinstance(leaf, ast.Subscript) and _is_sys_modules(leaf.value):
                        violations.append(f"{label}:{node.lineno}: sys.modules[...] =")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATING_CALLS
            and _is_sys_modules(node.func.value)
        ):
            violations.append(
                f"{label}:{node.lineno}: sys.modules.{node.func.attr}(...)"
            )
    return violations


# Every shape the scanner must decide, and the verdict it must reach. Kept as a
# table rather than one synthetic file asserted by exact line numbers: the old
# control pinned ``lines == {3,4,6,7,8}``, so ADDING a shape reddened the
# control instead of extending coverage, and the control fought the fix.
# Six of these were measured MISSED by the review against the shipped scanner.
_SCANNER_SHAPES: dict[str, tuple[str, bool]] = {
    "plain subscript write": ("import sys\nsys.modules['a.b'] = OBJ\n", True),
    "setdefault statement": ("import sys\nsys.modules.setdefault('a.b', OBJ)\n", True),
    "update spelling": ("import sys\nsys.modules.update({'a.b': OBJ})\n", True),
    "setdefault bound in an assignment": (
        "import sys\nm = sys.modules.setdefault('a.b', OBJ)\n",
        True,
    ),
    "nested inside a module-level if": (
        "import sys\nif True:\n    sys.modules['a.b'] = OBJ\n",
        True,
    ),
    # --- the six the review measured as MISSED ---
    "local helper indirection": (
        "import sys\ndef _put(n, m):\n    sys.modules[n] = m\n_put('a.b', OBJ)\n",
        True,
    ),
    "from sys import modules": (
        "from sys import modules\nmodules['a.b'] = OBJ\n",
        True,
    ),
    "aliased import sys as s": ("import sys as s\ns.modules['a.b'] = OBJ\n", True),
    "match/case block": (
        "import sys\nx = 1\nmatch x:\n    case 1:\n        sys.modules['a.b'] = OBJ\n",
        True,
    ),
    "setdefault in an if header": (
        "import sys\nif sys.modules.setdefault('a.b', OBJ):\n    pass\n",
        True,
    ),
    "tuple-unpacking target": (
        "import sys\nsys.modules['a.b'], q = OBJ, 2\n",
        True,
    ),
    # --- negative controls: the scanner must NOT flag these ---
    "reads sys.modules without writing": (
        "import sys\nx = sys.modules.get('a.b')\n",
        False,
    ),
    "a different object named modules": (
        "import other\nother.modules['a.b'] = OBJ\n",
        False,
    ),
    "setdefault on an unrelated dict": ("d = {}\nd.setdefault('a.b', OBJ)\n", False),
}


@pytest.mark.parametrize("shape", sorted(_SCANNER_SHAPES))
def test_the_scanner_decides_every_substitution_shape(shape):
    """POSITIVE **and** NEGATIVE control for the scan below.

    Without the positives, a scanner that had silently stopped matching would
    report a clean tree — the exact failure this whole file is about. Without
    the negatives, a scanner that flagged everything would also pass, and the
    real scan would be unusable rather than merely blind.
    """
    source, must_flag = _SCANNER_SHAPES[shape]
    found = _sys_modules_writes(source, "synthetic")

    if must_flag:
        assert found, f"{shape!r} is a session-wide substitution and was not flagged"
    else:
        assert not found, f"{shape!r} is not a substitution but was flagged: {found}"


def test_the_sanctioned_exemption_is_by_name_not_by_being_a_function():
    """The helper is exempt because of what it is called, and only there.

    The pre-review scanner skipped every function body, so ANY local helper was
    exempt — the one shape that most needs catching, since it is the shape the
    sanctioned helper itself has. Both directions are asserted: the same source
    is flagged when no name is sanctioned, and clean when it is. A blanket
    function-body skip passes the second and fails the first.
    """
    source = (
        "import sys\n"
        "def _install_stand_in(name, module):\n"
        "    sys.modules[name] = module\n"
        "def _sneaky(name, module):\n"
        "    sys.modules[name] = module\n"
    )

    unexempted = _sys_modules_writes(source, "synthetic")
    assert len(unexempted) == 2, unexempted

    exempted = _sys_modules_writes(source, "synthetic", sanctioned="_install_stand_in")
    assert len(exempted) == 1, (
        "exempting by name must leave the second helper flagged — otherwise the "
        f"exemption is a blanket function skip again: {exempted}"
    )
    assert "_sneaky" not in "".join(exempted)  # it is reported by line, not name


def test_no_conftest_substitutes_a_module_outside_the_helper():
    """New stand-ins must go through ``_install_stand_in``.

    That helper is what attaches the ``ModuleSpec`` and what refuses a
    first-party name. A raw ``sys.modules[...] =`` beside it re-arms both traps
    for the next caller and nothing downstream would notice — the runtime
    checks further down only see names the registry knows about.
    """
    conftests = sorted(TESTS_ROOT.rglob("conftest.py"))

    assert len(conftests) >= _MIN_CONFTESTS_SCANNED, (
        f"walked only {len(conftests)} conftest files under {TESTS_ROOT} — the "
        "scan resolved the wrong directory, so its clean verdict says nothing"
    )

    violations: list[str] = []
    for path in conftests:
        # encoding is pinned: several scanned conftests carry non-ASCII em
        # dashes, and read_text() without it follows the ambient locale — under
        # a POSIX-locale container the guard would die with UnicodeDecodeError
        # instead of returning a verdict.
        text = path.read_text(encoding="utf-8")
        # Only the ROOT conftest may define the sanctioned installer.
        sanctioned = (
            _SANCTIONED_INSTALLER if path == TESTS_ROOT / "conftest.py" else None
        )
        violations += _sys_modules_writes(
            text, str(path.relative_to(REPO_ROOT)), sanctioned=sanctioned
        )

    assert violations == [], (
        "module-level sys.modules substitution outside _install_stand_in:\n  "
        + "\n  ".join(violations)
        + "\nRoute it through tests/conftest.py::_install_stand_in, which "
        "attaches a real ModuleSpec (#942) and refuses first-party names."
    )


# --------------------------------------------------------------------------- #
# Level 2 — the helper's own refusal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "faultmaven",
        "faultmaven.core.processing.log_analyzer",
        "faultmaven.infrastructure.observability.alerting",
        "faultmaven.some.module.that.does.not.exist",
    ],
)
def test_installing_a_first_party_stand_in_is_refused(name):
    """The helper refuses where the mistake is made, not only in review.

    A first-party shadow is silent: the module still imports, still exposes the
    expected attribute names, and the tests still pass — against ``Mock``.
    Refusal covers a name that does not exist on disk too, because both fossils
    removed with #942 were exactly that shape.
    """
    _MISSING = object()
    before = sys.modules.get(name, _MISSING)

    with pytest.raises(RuntimeError, match="first-party"):
        _install_stand_in(name, types.SimpleNamespace())

    assert (
        sys.modules.get(name, _MISSING) is before
    ), "the refused stand-in was installed anyway"


_PROBE_NAME = "l3_probe_third_party"


def test_the_helper_accepts_a_third_party_name():
    """POSITIVE CONTROL: the refusal above discriminates.

    A helper that raised for everything would satisfy the previous test while
    installing nothing at all, and the whole harness would be broken in a way
    those parametrised cases cannot see.

    Cleanup is an explicit ``try/finally``, NOT ``monkeypatch``. This test used
    ``monkeypatch.delitem(sys.modules, name, raising=False)`` to arrange the
    teardown, which does not work: for an ABSENT key delitem records no undo
    entry at all, so the module the helper then created survived the whole
    session — this file leaking a stand-in into ``sys.modules``, which is the
    one thing it exists to forbid. (``setitem`` does record on an absent key,
    which is why only the registry half was cleaned and the leak was silent.)
    """
    assert _PROBE_NAME not in sys.modules, "a previous run leaked the probe"
    try:
        installed = _install_stand_in(_PROBE_NAME, types.ModuleType(_PROBE_NAME))

        assert sys.modules[_PROBE_NAME] is installed
        assert (
            importlib.util.find_spec(_PROBE_NAME) is installed.__spec__
        ), "the helper installed a module the standard probe still cannot resolve"
    finally:
        # pop, not the helper: _install_stand_in no-ops on a present name, so
        # there is no "uninstall" path through it.
        sys.modules.pop(_PROBE_NAME, None)
        HARNESS_STAND_INS.pop(_PROBE_NAME, None)


def test_the_probe_stand_in_did_not_outlive_its_test():
    """...and the cleanup above is pinned from OUTSIDE that test.

    Asserting the teardown inside the test that performs it cannot catch a
    teardown that never runs. This runs after it and looks at the shared state
    directly, which is how the leak was found; deleting the ``finally`` above
    reds this and nothing else.
    """
    assert _PROBE_NAME not in sys.modules, "probe stand-in leaked into sys.modules"
    assert _PROBE_NAME not in HARNESS_STAND_INS, "probe stand-in leaked into registry"


# --------------------------------------------------------------------------- #
# Level 3 — the runtime result
# --------------------------------------------------------------------------- #


def test_a_spec_less_module_still_breaks_the_standard_probe():
    """POSITIVE CONTROL for the whole ``__spec__`` arm.

    If CPython ever stopped raising here, the next test would pass on every
    stand-in whether or not the harness had attached a spec, and #942 would be
    silently un-pinned. Assert the mechanism is real before asserting it is
    handled.
    """
    name = "l3_probe_spec_less"
    sys.modules[name] = types.ModuleType(name)
    try:
        with pytest.raises(ValueError):
            importlib.util.find_spec(name)
    finally:
        del sys.modules[name]


def test_every_harness_stand_in_answers_a_non_importing_probe():
    """``find_spec`` must answer for a stand-in exactly as it does in
    production: with a spec, not by raising.

    Anything else means an availability probe resolves differently under the
    harness, so the suite cannot be evidence that the probe works.
    """
    assert _REQUIRED_STAND_INS <= set(HARNESS_STAND_INS), (
        "the harness did not stand in for "
        f"{sorted(_REQUIRED_STAND_INS - set(HARNESS_STAND_INS))}. The likely "
        "cause is that the name was ALREADY in sys.modules when conftest ran, "
        "so _install_stand_in no-opped and recorded nothing — i.e. something "
        "imported the real (heavy, #868) package before collection. The other "
        "causes are that the substitution was deleted, or installed without "
        "going through the helper."
    )
    for name, module in sorted(HARNESS_STAND_INS.items()):
        assert sys.modules.get(name) is module, (
            f"{name} is in the stand-in registry but is not what is installed — "
            "the registry is describing a substitution that did not happen"
        )
        spec = importlib.util.find_spec(name)  # must not raise ValueError
        assert isinstance(spec, importlib.machinery.ModuleSpec), (name, spec)
        assert spec is module.__spec__
        assert spec.name == name, (
            f"{name} carries a spec named {spec.name!r} — a mislabelled spec "
            "resolves, so it passes a naive check while still lying"
        )


@pytest.mark.parametrize("name, attr", sorted(_FORMERLY_SHADOWED.items()))
def test_a_formerly_shadowed_module_is_the_real_module(name, attr):
    """The four ``faultmaven.*`` modules the conftest replaced must import for
    real — from a file inside this checkout, not a ``SimpleNamespace``.

    ``getattr`` is checked too: the stubs exposed exactly these names, so
    "the attribute exists" was true of the shadow as well. What separates them
    is where the object comes from.
    """
    module = importlib.import_module(name)

    assert isinstance(module, types.ModuleType), type(module)
    origin = getattr(module.__spec__, "origin", None)
    assert origin is not None, f"{name} has no origin — it is not loaded from disk"
    assert pathlib.Path(origin).resolve().is_relative_to(PACKAGE_ROOT), (
        f"{name} resolved to {origin}, outside {PACKAGE_ROOT} — it measured a "
        "different checkout"
    )
    owner = _defining_module(getattr(module, attr))
    assert owner == name, (
        f"{name}.{attr} is defined in {owner!r}, not in {name!r} — the module "
        "is shadowed again."
    )


def _defining_module(obj) -> str:
    """Where ``obj`` is defined — the discriminator both stand-in checks use.

    A class carries its own ``__module__``; an instance carries its type's.
    Note this is NOT "is it a Mock": the log_analyzer stub bound the Mock
    CLASS, and ``type(Mock).__module__`` is "builtins", so a not-a-Mock check
    would have passed straight over it.
    """
    return obj.__module__ if isinstance(obj, type) else type(obj).__module__


def _is_genuinely_first_party(module) -> bool:
    """A real module of this package, as opposed to a stand-in for one.

    ``__file__ is None`` is NOT the discriminator: three genuine namespace
    packages (``faultmaven.api``, ``faultmaven.providers``,
    ``faultmaven.modules.case.domain.services``) have no ``__init__.py`` and so
    no ``__file__``. What every real one does have is a spec that locates it
    inside the package directory — by ``origin`` for a regular module, by
    ``submodule_search_locations`` for a namespace package. All three stand-in
    shapes fail that: a ``SimpleNamespace`` is not a module, a hand-built
    ``ModuleType`` has no spec, and a helper-installed stand-in has a spec that
    locates nothing.
    """
    if not isinstance(module, types.ModuleType):
        return False
    spec = getattr(module, "__spec__", None)
    if spec is None:
        return False
    locations = list(spec.submodule_search_locations or [])
    if spec.origin is not None:
        locations.append(spec.origin)
    return any(
        pathlib.Path(loc).resolve().is_relative_to(PACKAGE_ROOT) for loc in locations
    )


def test_no_first_party_module_in_sys_modules_is_a_stand_in():
    """The sweep the registry checks cannot do.

    ``_install_stand_in`` refuses first-party names and the source scan keeps
    substitutions going through it, but neither sees a shadow installed by a
    plugin, a fixture, or an import that resolved somewhere unexpected. This
    looks at what is actually in ``sys.modules``.

    Scoped to the ``faultmaven.*`` namespace on purpose. A sweep over all of
    ``sys.modules`` would be a universal over a set nobody checked — 27 entries
    are spec-less under this harness and several of them legitimately
    (``typing.io``, ``pyexpat.errors``, ``cython_runtime``).
    """
    for name in _FORMERLY_SHADOWED:
        importlib.import_module(name)

    first_party = {
        name: module
        for name, module in list(sys.modules.items())
        if (name == "faultmaven" or name.startswith("faultmaven."))
        and module is not None
    }

    assert len(first_party) >= _MIN_FIRST_PARTY_EXAMINED, (
        f"only {len(first_party)} first-party modules were loaded — the sweep "
        "is close to vacuous, so its clean verdict says little"
    )

    shadowed = sorted(
        f"{name} ({type(module).__name__})"
        for name, module in first_party.items()
        if not _is_genuinely_first_party(module)
    )
    assert shadowed == [], (
        "first-party modules replaced by a stand-in: "
        + ", ".join(shadowed)
        + " — a test that appears to exercise these exercises the stand-in."
    )


def test_the_shadow_sweep_rejects_each_stand_in_shape():
    """POSITIVE CONTROL for the sweep's discriminator.

    A ``_is_genuinely_first_party`` that returned True for everything would
    make the sweep above pass over any shadow at all, which is precisely the
    pre-#942 state it exists to detect.
    """
    real = importlib.import_module("faultmaven.core.processing.log_analyzer")
    assert _is_genuinely_first_party(real) is True

    namespace_pkg = importlib.import_module("faultmaven.api")
    assert getattr(namespace_pkg, "__file__", None) is None
    assert _is_genuinely_first_party(namespace_pkg) is True, (
        "a genuine namespace package was rejected — the sweep would fail on a "
        "clean tree"
    )

    assert (
        _is_genuinely_first_party(types.SimpleNamespace(LogProcessor=object)) is False
    )
    assert _is_genuinely_first_party(types.ModuleType("faultmaven.fake")) is False

    helper_shaped = types.ModuleType("faultmaven.fake")
    helper_shaped.__spec__ = importlib.machinery.ModuleSpec(
        "faultmaven.fake", loader=None
    )
    assert _is_genuinely_first_party(helper_shaped) is False, (
        "a stand-in carrying a spec that locates nothing was accepted — giving "
        "stand-ins specs (#942) must not buy them past this sweep"
    )


@pytest.mark.parametrize("name, attr", sorted(_MUST_BE_REAL.items()))
def test_an_installed_third_party_package_is_not_stubbed_away(name, attr):
    """A third-party package the suite must exercise for real.

    Substituting an *absent* heavy dependency is the harness doing its job.
    Substituting an installed one the suite is supposed to cover is the same
    fail-open as a first-party shadow, one namespace over.
    """
    spec = importlib.util.find_spec(name)
    assert spec is not None and spec.origin is not None, (
        f"{name} does not resolve to a real installation — if it were genuinely "
        "uninstalled this test would be asking the wrong question, so check the "
        "environment before relaxing it"
    )
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).is_file()
    owner = _defining_module(getattr(module, attr))
    assert owner == name or owner.startswith(f"{name}."), (
        f"{name}.{attr} is defined in {owner!r}, outside the {name!r} package — "
        "the import resolved to a stand-in"
    )
    assert name not in HARNESS_STAND_INS


# --------------------------------------------------------------------------- #
# The reset that keeps the un-stubbed singletons from accumulating
# --------------------------------------------------------------------------- #


def test_the_observability_reset_names_state_that_actually_exists():
    """The autouse reset fixture must not quietly become a no-op.

    It clears named attributes and skips anything missing, so a rename upstream
    would leave it silently clearing nothing while every TestClient request kept
    accumulating residue on shared singletons. Checking the names here is what
    makes that loud. Each named attribute must also actually be a container --
    ``hasattr(x, "clear")`` is the fixture's own test, so a field that stopped
    being one would be skipped just as silently.
    """
    assert OBSERVABILITY_RESET_FIELDS, "the reset fixture has nothing to reset"

    for module_name, (singleton_name, fields) in OBSERVABILITY_RESET_FIELDS.items():
        module = importlib.import_module(module_name)
        singleton = getattr(module, singleton_name)
        assert fields, f"{singleton_name} has an empty field list"
        for field in fields:
            container = getattr(singleton, field)
            assert hasattr(container, "clear"), (
                f"{module_name}.{singleton_name}.{field} is {type(container).__name__},"
                " which the fixture cannot clear -- it would skip it in silence"
            )


def test_one_module_object_cannot_be_installed_under_two_names():
    """The helper mutates ``__spec__`` on a caller-owned object.

    So installing the same object twice would relabel the first entry's spec,
    and ``find_spec`` on the ORIGINAL name would answer with a spec carrying the
    second name — resolving, and lying, which is exactly the failure
    ``spec.name == name`` was added to catch. Every call site passes a fresh
    object today; this refuses so that a future one cannot re-use one silently.
    """
    shared = types.ModuleType("l3_alias_first")
    first = "l3_alias_first"
    second = "l3_alias_second"
    try:
        _install_stand_in(first, shared)
        assert sys.modules[first].__spec__.name == first

        with pytest.raises(RuntimeError, match="cannot carry two names"):
            _install_stand_in(second, shared)

        assert second not in sys.modules, "the aliased stand-in was installed anyway"
        assert (
            sys.modules[first].__spec__.name == first
        ), "the first entry's spec was relabelled"
    finally:
        for name in (first, second):
            sys.modules.pop(name, None)
            HARNESS_STAND_INS.pop(name, None)
