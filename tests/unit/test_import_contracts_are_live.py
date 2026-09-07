"""Every module named in `.importlinter` must be one the import graph can see.

`Architecture Boundary Check` is a REQUIRED check, and a contract naming a
module grimp cannot resolve does not fail — it passes, silently, forever. Two
ways that happened here, both found only by hand:

  - `faultmaven.api`, `faultmaven.providers` and three more had no
    `__init__.py`. grimp walks regular packages only, so `build_graph` returned
    ZERO modules under them. Contract 2 ("Services cannot import API layer")
    could not fail: a real `from faultmaven.api... import ...` planted in
    `faultmaven/services/base.py` left all contracts KEPT. Contract 13 was
    commented out for the same reason.
  - Contract 11 forbade `...repositories.token_repository`, a module deleted in
    an earlier refactor. The rule went on passing while covering one module
    fewer than it named.

Neither shape is visible in a green run, which is what makes a test necessary:
the linter reports on the contracts it can evaluate and says nothing about the
names it could not resolve.

This resolves modules against the FILESYSTEM rather than by building a grimp
graph, and that is deliberate on two counts:

  - grimp is a lint-time dependency. It is installed for the architecture job,
    not for the test jobs, so a version of this file that imported it collected
    green locally and then failed all three test jobs with
    `ModuleNotFoundError`.
  - grimp caches its graph in `.grimp_cache`, and a cached build is exactly what
    this must not read: with `faultmaven/api/__init__.py` deleted, a
    cache-backed build STILL reported its 22 modules.

The rule mirrored here is grimp's own: a module is reachable from the root
package only if every directory on the way to it is a REGULAR package — one with
an `__init__.py`. A namespace portion is skipped, taking everything beneath it.

`ignore_imports` needs no equivalent check: import-linter already errors on an
ignored import that matches nothing.
"""

import configparser
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / ".importlinter"
ROOT_PACKAGE = "faultmaven"

# The keys whose values are module names. `ignore_imports` is deliberately
# absent: its entries are `importer -> imported` pairs, may carry wildcards, and
# import-linter fails loudly on one that matches nothing.
MODULE_KEYS = ("modules", "source_modules", "forbidden_modules", "layers", "containers")


def _declared_modules() -> list[tuple[str, str]]:
    """(contract name, module) for every first-party module the config names."""
    parser = configparser.ConfigParser()
    parser.read(CONFIG)
    declared: list[tuple[str, str]] = []
    for section in parser.sections():
        if not section.startswith("importlinter:contract"):
            continue
        contract = parser[section].get("name", section)
        for key in MODULE_KEYS:
            for line in parser[section].get(key, "").splitlines():
                module = line.split("#")[0].strip()
                if module == ROOT_PACKAGE or module.startswith(ROOT_PACKAGE + "."):
                    declared.append((contract, module))
    return declared


def _unreachable_reason(module: str, root: pathlib.Path = REPO_ROOT) -> str | None:
    """Why the graph cannot see `module`, or None if it can.

    Mirrors grimp's traversal: every package on the path must be a REGULAR one.
    `root` is a parameter so the control below can build both failure shapes for
    real rather than asserting against a tree that no longer contains either.
    """
    parts = module.split(".")
    directory = root
    for depth, part in enumerate(parts):
        directory = directory / part
        is_last = depth == len(parts) - 1

        if directory.is_dir():
            if not (directory / "__init__.py").is_file():
                return (
                    f"{directory.relative_to(root)} has no __init__.py, so it is "
                    "a namespace package and grimp does not walk into it"
                )
            continue

        if is_last and directory.with_suffix(".py").is_file():
            return None

        return f"{directory.relative_to(root)} does not exist"
    return None


def test_the_config_declares_modules_to_check():
    """Guard the guard: a parse that found nothing would pass over anything."""
    declared = _declared_modules()
    assert len(declared) >= 30, (
        f"only {len(declared)} module references were parsed out of {CONFIG.name} — "
        "the sweep below is close to vacuous, so its clean verdict says little"
    )


def test_the_resolver_rejects_both_shapes_that_occurred(tmp_path):
    """POSITIVE CONTROL for the resolver.

    One that returned None for everything would make the sweep below pass over
    any blind contract at all — which is the state this file exists to end. Both
    shapes are built here rather than asserted against the tree, because the
    tree no longer contains either.
    """
    pkg = tmp_path / ROOT_PACKAGE
    (pkg / "regular").mkdir(parents=True)
    (pkg / "__init__.py").touch()
    (pkg / "regular" / "__init__.py").touch()
    (pkg / "regular" / "leaf.py").touch()
    # A namespace portion: a real directory with no __init__.py, holding a real
    # module. This is the shape `faultmaven/api` had.
    (pkg / "namespace").mkdir()
    (pkg / "namespace" / "leaf.py").touch()

    # Reachable: every directory on the way is a regular package.
    assert _unreachable_reason(f"{ROOT_PACKAGE}.regular", tmp_path) is None
    assert _unreachable_reason(f"{ROOT_PACKAGE}.regular.leaf", tmp_path) is None

    # The contract-2 shape: the directory exists and holds modules, but grimp
    # never walks into it.
    assert "__init__.py" in (
        _unreachable_reason(f"{ROOT_PACKAGE}.namespace", tmp_path) or ""
    )
    assert "__init__.py" in (
        _unreachable_reason(f"{ROOT_PACKAGE}.namespace.leaf", tmp_path) or ""
    )

    # The contract-11 shape: the module was renamed or deleted.
    assert "does not exist" in (
        _unreachable_reason(f"{ROOT_PACKAGE}.regular.gone", tmp_path) or ""
    )


def test_every_contract_names_a_module_the_graph_can_see():
    unreachable = sorted(
        f"{contract}: {module} — {reason}"
        for contract, module in _declared_modules()
        if (reason := _unreachable_reason(module)) is not None
    )
    assert unreachable == [], (
        "contracts name modules the import graph cannot see, so those rules "
        "cannot fail:\n  " + "\n  ".join(unreachable)
    )
