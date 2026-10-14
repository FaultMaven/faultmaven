"""The files a recorded benchmark number depends on (#1989).

``python -m tests.wallclock.benchmarked_files`` prints them, one repository-
relative path per line, sorted.

Why it exists: the A/B job's shadow count rule is promoted on evidence from
pull requests that touch **no benchmarked code** (``docs/development/testing/
performance.md``), so "benchmarked code" has to be a list a script can
check a pull request against, and a hand-written list drifts. The first one
named five directories and covered 58 of the 112 ``faultmaven`` files the
benchmark modules import (``faultmaven/utils/serialization.py``, which both
repositories' hot paths run, counted as "touches none"). This one is read
off the interpreter instead.

What it lists:

* **The import closure.** Every ``faultmaven`` and ``tests`` module found in
  ``sys.modules`` after importing every benchmark module under
  ``tests/benchmarks/`` (the A/B job runs ``pytest tests/benchmarks/``, so
  those are the modules whose rows it records). Imported code, not executed
  code: a module imported and never called is listed, which is the safe
  direction for this list.
* **Inputs no import names.** ``tests/conftest.py`` (pytest loads it for the
  run), ``pytest.ini`` and ``pyproject.toml``, and ``requirements/*.txt``
  (a dependency change moves the data layer without touching a source file).
* **The instrument.** ``tests/wallclock/ab.py`` and
  ``.github/workflows/benchmarks.yml``: they decide how the numbers are
  compared, so a pull request that edits them is not a clean sample of the
  rule's false-red rate whatever it does to the suite.

What it does not list, and the direction of that error: the modules only
``tests/conftest.py`` imports (measured: 44 more ``faultmaven`` files, the
LLM providers, health and protection modules the SQLite CRUD benchmarks never
execute). A pull request touching only those is read as touching no
benchmarked code. If it did move a benchmark, the count rule fires on a
"clean" pull request, which is recorded as a false red and restarts the
count: the error delays promotion and cannot hasten it.

‼ It imports the application, which is its job, so it reports the tree the
interpreter imports. It refuses to answer when ``faultmaven`` or ``tests``
resolves outside this checkout (an editable install of another tree), because
a closure of the wrong tree is a list of paths that exist nowhere in this one,
and "touches none" would be true of every pull request.

Cost, measured on a development box: about 4-5 seconds, one fresh interpreter
(it must be one: in a process that has imported more, ``sys.modules`` holds
more than the benchmarks import).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Set

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = ROOT / "tests" / "benchmarks"

#: Top-level packages whose modules are repository code. Everything else
#: found in ``sys.modules`` is the standard library or a dependency.
REPOSITORY_PACKAGES = frozenset({"faultmaven", "tests"})

#: Files the run reads that no import statement names, and the instrument.
EXTRA_FILES = (
    "tests/conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "tests/wallclock/ab.py",
    ".github/workflows/benchmarks.yml",
)
EXTRA_GLOBS = ("requirements/*.txt",)


def benchmark_modules() -> List[str]:
    """Every benchmark module pytest collects from ``tests/benchmarks/``."""
    return sorted(
        f"tests.benchmarks.{path.stem}" for path in BENCHMARK_DIR.glob("test_*.py")
    )


def repository_file(module_name: str, file: Optional[str]) -> Optional[str]:
    """The repository-relative path of one module, ``None`` if it is not ours.

    Raises ``RuntimeError`` for a ``faultmaven`` or ``tests`` module that
    resolves outside this checkout: see the note on wrong trees above.
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


def closure(modules: Iterable[str]) -> Set[str]:
    """The repository files imported by ``modules`` in THIS interpreter."""
    for name in modules:
        importlib.import_module(name)
    found: Set[str] = set()
    for name, module in list(sys.modules.items()):
        relative = repository_file(name, getattr(module, "__file__", None))
        if relative is not None:
            found.add(relative)
    return found


def benchmarked_files() -> List[str]:
    """Closure, inputs and instrument, sorted, every one an existing file."""
    files = closure(benchmark_modules())
    files.update(EXTRA_FILES)
    for pattern in EXTRA_GLOBS:
        files.update(path.relative_to(ROOT).as_posix() for path in ROOT.glob(pattern))
    missing = sorted(name for name in files if not (ROOT / name).is_file())
    if missing:
        # A moved or renamed input would otherwise stay in the list forever
        # and quietly match nothing.
        raise RuntimeError(f"listed but not files in this checkout: {missing}")
    return sorted(files)


def main() -> int:
    if not benchmark_modules():
        raise SystemExit(f"no benchmark modules under {BENCHMARK_DIR}")
    sys.stdout.write("".join(f"{name}\n" for name in benchmarked_files()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
