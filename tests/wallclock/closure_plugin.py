"""The files a real benchmark run depends on (#1989, #2014).

A pytest plugin, loaded with ``-p tests.wallclock.closure_plugin``. When the
session ends it writes, to the file named by ``FM_CLOSURE_OUT``, one
repository-relative path per line, sorted:

* every ``faultmaven`` and ``tests`` module found in ``sys.modules``, which is
  the code that run imported, lazy imports inside function bodies and the
  modules only ``tests/conftest.py`` pulls in included. A static import walk
  misses both (it listed 112 of the 160 ``faultmaven`` files a run imports);
* the inputs no import names: ``tests/conftest.py``, ``pytest.ini``,
  ``pyproject.toml``, ``requirements/*.txt``;
* the instrument: ``tests/wallclock/ab.py`` and
  ``.github/workflows/benchmarks.yml``. A pull request that edits the rule is
  not a clean sample of the rule's false-red rate.

``scripts/score_ab_shadow_rule.sh`` runs ``pytest tests/benchmarks -m
benchmark`` with this plugin to learn what "touches benchmarked code" means
(about 25 seconds, cached per HEAD sha). Imported is not executed: a module
imported and never called is listed, the safe direction for a list whose error
can only turn a clean pull request into a recorded false red.

‼ It refuses to write when a ``faultmaven`` or ``tests`` module resolves
outside this checkout (an editable install of another tree), because a closure
of the wrong tree is a list of paths that exist nowhere in this one, and
"touches none" would be true of every pull request.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Set

ROOT = Path(__file__).resolve().parents[2]

#: Top-level packages whose modules are repository code.
REPOSITORY_PACKAGES = frozenset({"faultmaven", "tests"})

EXTRA_FILES = (
    "tests/conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "tests/wallclock/ab.py",
    ".github/workflows/benchmarks.yml",
)
EXTRA_GLOBS = ("requirements/*.txt",)

OUT_ENV = "FM_CLOSURE_OUT"


def repository_file(module_name: str, file: Optional[str]) -> Optional[str]:
    """The repository-relative path of one module, ``None`` if it is not ours.

    Raises ``RuntimeError`` for a ``faultmaven`` or ``tests`` module that
    resolves outside this checkout.
    """
    if module_name.split(".")[0] not in REPOSITORY_PACKAGES or not file:
        return None
    path = Path(file).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        raise RuntimeError(
            f"{module_name} resolves to {path}, outside this checkout ({ROOT}); "
            f"another tree is installed ahead of it, and a closure of that "
            f"tree is not a list of this one's files"
        ) from None


def imported_files(modules: Iterable = ()) -> Set[str]:
    """The repository files of ``sys.modules`` (or of ``modules`` pairs)."""
    items = list(modules) or list(sys.modules.items())
    found: Set[str] = set()
    for name, module in items:
        relative = repository_file(name, getattr(module, "__file__", None))
        if relative is not None:
            found.add(relative)
    return found


def benchmarked_files() -> list:
    """Imports, inputs and instrument, sorted, every one an existing file."""
    files = imported_files()
    files.update(EXTRA_FILES)
    for pattern in EXTRA_GLOBS:
        files.update(path.relative_to(ROOT).as_posix() for path in ROOT.glob(pattern))
    missing = sorted(name for name in files if not (ROOT / name).is_file())
    if missing:
        # A moved or renamed input would otherwise stay in the list forever
        # and quietly match nothing.
        raise RuntimeError(f"listed but not files in this checkout: {missing}")
    return sorted(files)


def pytest_sessionfinish(session, exitstatus) -> None:
    out = os.environ.get(OUT_ENV)
    if not out:
        return
    Path(out).write_text("".join(f"{name}\n" for name in benchmarked_files()))
