"""Every module named in `.importlinter` must exist in the import graph.

`Architecture Boundary Check` is a REQUIRED check, and a contract naming a
module that grimp cannot see does not fail — it passes, silently, forever. Two
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
names it could not resolve. `ignore_imports` needs no equivalent check —
import-linter already errors on an ignored import that matches nothing.
"""

import configparser
import pathlib

import grimp
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / ".importlinter"

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
                if module.startswith("faultmaven"):
                    declared.append((contract, module))
    return declared


@pytest.fixture(scope="module")
def graph():
    # cache_dir=None, deliberately. grimp writes `.grimp_cache`, and a cached
    # graph is exactly what this test must not read: with `faultmaven/api`'s
    # `__init__.py` deleted, a cache-backed build still reported its 22 modules,
    # so the sweep passed on a tree where the contracts naming them had gone
    # blind — the failure this file exists to catch.
    return grimp.build_graph("faultmaven", cache_dir=None)


def test_the_config_declares_modules_to_check(graph):
    """Guard the guard: a parse that found nothing would pass over anything."""
    declared = _declared_modules()
    assert len(declared) >= 30, (
        f"only {len(declared)} module references were parsed out of {CONFIG.name} — "
        "the sweep below is close to vacuous, so its clean verdict says little"
    )


def test_every_contract_names_a_module_the_graph_contains(graph):
    modules = set(graph.modules)

    def present(module: str) -> bool:
        # A package counts when the graph holds it or anything beneath it: a
        # namespace portion has no module of its own but still has contents.
        return module in modules or any(m.startswith(module + ".") for m in modules)

    missing = sorted(
        f"{contract}: {module}"
        for contract, module in _declared_modules()
        if not present(module)
    )
    assert missing == [], (
        "contracts name modules that are not in the import graph, so those rules "
        "cannot fail:\n  "
        + "\n  ".join(missing)
        + "\n\nEither the module was renamed or deleted and the contract was not "
        "updated, or its directory has no __init__.py and grimp therefore skips it."
    )
