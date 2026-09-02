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

from tests.conftest import HARNESS_STAND_INS, _install_stand_in

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


def _module_level_sys_modules_writes(source: str, label: str) -> list[str]:
    """Module-level substitutions: ``sys.modules[x] = y`` and mutating calls.

    Takes the source text so a synthetic violating file can be pushed through
    this exact function: a scanner only ever run against a clean tree passes
    just as well when the scanner itself is broken.

    Module level only, deliberately. A fixture that swaps ``sys.modules`` and
    restores it is scoped and reversible; a write at import time is a
    session-wide substitution installed before collection, which is the shape
    that fails open. Function and class bodies are therefore not descended
    into — which is also what exempts ``_install_stand_in`` itself.

    Calls are matched wherever they appear in a module-level assignment or
    expression statement, not only as a bare statement, so
    ``m = sys.modules.setdefault(...)`` is caught as well.
    """
    violations: list[str] = []

    def _is_sys_modules(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "modules"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        )

    def _check_calls(node: ast.AST, lineno: int) -> None:
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in _MUTATING_CALLS
                and _is_sys_modules(sub.func.value)
            ):
                violations.append(f"{label}:{lineno}: sys.modules.{sub.func.attr}(...)")

    def _walk(body) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # scoped, reversible — see the docstring
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and _is_sys_modules(
                        target.value
                    ):
                        violations.append(f"{label}:{node.lineno}: sys.modules[...] =")
            # Expression-only statements can be walked whole: they cannot
            # contain a nested def, so this never reaches a scoped write.
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr)):
                _check_calls(node, node.lineno)
            for child in ("body", "orelse", "finalbody"):
                _walk(getattr(node, child, []) or [])
            for handler in getattr(node, "handlers", []) or []:
                _walk(handler.body)

    _walk(ast.parse(source).body)
    return violations


def test_the_scanner_detects_every_substitution_shape():
    """POSITIVE CONTROL for the scan below.

    Without this, a scanner that had silently stopped matching anything would
    report a clean tree — which is the exact failure this whole file is about.
    Each shape is asserted by line number so that a scanner matching only one
    of them cannot pass on the total.
    """
    synthetic = (
        "import sys\n"  # 1
        "from types import SimpleNamespace\n"  # 2
        "sys.modules['a.b'] = SimpleNamespace()\n"  # 3  subscript assign
        "sys.modules.setdefault('c.d', SimpleNamespace())\n"  # 4  bare call
        "if True:\n"  # 5
        "    sys.modules['nested'] = SimpleNamespace()\n"  # 6  module level still
        "m = sys.modules.setdefault('e.f', SimpleNamespace())\n"  # 7  call in assign
        "sys.modules.update({'g.h': SimpleNamespace()})\n"  # 8  update spelling
        "def f():\n"  # 9
        "    sys.modules['scoped'] = SimpleNamespace()\n"  # 10 scoped: NOT a hit
    )
    found = _module_level_sys_modules_writes(synthetic, "synthetic")
    lines = {int(v.split(":")[1]) for v in found}

    assert 3 in lines, "subscript assignment not detected"
    assert 4 in lines, "bare setdefault call not detected"
    assert 6 in lines, "module-level write nested in `if` not detected"
    assert 7 in lines, "setdefault inside an assignment not detected"
    assert 8 in lines, "update() spelling not detected"
    assert 10 not in lines, (
        "a write inside a function body was flagged — those are scoped and "
        "reversible, and flagging them makes the scan unusable"
    )
    assert lines == {3, 4, 6, 7, 8}, found


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
        violations += _module_level_sys_modules_writes(
            path.read_text(), str(path.relative_to(REPO_ROOT))
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
    before = dict(sys.modules)
    with pytest.raises(RuntimeError, match="first-party"):
        _install_stand_in(name, types.SimpleNamespace())
    assert name not in sys.modules or sys.modules[name] is before.get(
        name
    ), "the refused stand-in was installed anyway"


def test_the_helper_accepts_a_third_party_name(monkeypatch):
    """POSITIVE CONTROL: the refusal above discriminates.

    A helper that raised for everything would satisfy the previous test while
    installing nothing at all, and the whole harness would be broken in a way
    those parametrised cases cannot see.
    """
    # Both keys are absent; registering them with monkeypatch is how the
    # entries the helper is about to create get torn down again, so this test
    # cannot leave a probe module in the registry the next test then asserts on.
    monkeypatch.delitem(sys.modules, "l3_probe_third_party", raising=False)
    monkeypatch.setitem(HARNESS_STAND_INS, "l3_probe_third_party", None)

    installed = _install_stand_in(
        "l3_probe_third_party", types.ModuleType("l3_probe_third_party")
    )

    assert sys.modules["l3_probe_third_party"] is installed
    assert (
        importlib.util.find_spec("l3_probe_third_party") is installed.__spec__
    ), "the helper installed a module the standard probe still cannot resolve"


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
        f"{sorted(_REQUIRED_STAND_INS - set(HARNESS_STAND_INS))} — either the "
        "substitution was removed (and the suite is now importing the real "
        "heavy stack, #868) or it was installed without the helper"
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
    obj = getattr(module, attr)
    owner = obj.__module__ if isinstance(obj, type) else type(obj).__module__
    assert owner == name, (
        f"{name}.{attr} is defined in {owner!r}, not in {name!r} — the module "
        "is shadowed again. Asserting merely 'not a Mock' would NOT catch it: "
        "the log_analyzer stub bound the Mock CLASS, and `type(Mock).__module__`"
        " is 'builtins'."
    )


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
    obj = getattr(module, attr)
    owner = obj.__module__ if isinstance(obj, type) else type(obj).__module__
    assert owner == name or owner.startswith(f"{name}."), (
        f"{name}.{attr} is defined in {owner!r}, outside the {name!r} package — "
        "the import resolved to a stand-in"
    )
    assert name not in HARNESS_STAND_INS
