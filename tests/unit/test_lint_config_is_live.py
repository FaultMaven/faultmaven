"""Every path a lint config names must resolve to a file that exists.

A per-file-ignore for a deleted file does not fail. It does not warn. It is
simply never consulted, and the config goes on advertising a decision about a
file nobody can open. Four such entries were sitting in `pyproject.toml` when
this file was written:

  - `[tool.ruff.lint.per-file-ignores]` exempted `faultmaven/agent/doctrine.py`
    and `faultmaven/data_processing/log_processor.py` from F841. Neither path
    exists; `faultmaven/agent/` itself has not existed since the move to
    `faultmaven/modules/agent/`.
  - `[tool.flake8].per-file-ignores` carried the same two, copied.
  - `[tool.mypy].files` named `faultmaven/session_management.py` and
    `faultmaven/agent/tools/web_search.py`, so a bare `mypy` could not start at
    all — it exited on "cannot read file" before checking anything, while the
    config read as configured.

This is the same defect class as `test_import_contracts_are_live` guards for
`.importlinter`, and it wants the same remedy: resolve the declaration, because
a declaration that is never resolved cannot go red.

Patterns are matched against the FILESYSTEM rather than by asking ruff, for the
reason that file gives: the tool that owns the config is not necessarily
installed in the job running the tests, and a check that skips when its tool is
absent is another thing that passes without looking.
"""

import fnmatch
import pathlib
import tomllib

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Directories that hold no first-party source and would dominate the walk.
PRUNED = {
    ".git",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".grimp_cache",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
    "htmlcov",
    "chroma_db",
    "data",
}


def _repo_files(root: pathlib.Path = REPO_ROOT) -> list[str]:
    """Every tracked-looking file, as a repo-relative POSIX path."""
    found: list[str] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in directory.iterdir():
            if entry.is_dir():
                if entry.name in PRUNED or entry.name.endswith(".egg-info"):
                    continue
                stack.append(entry)
            else:
                found.append(entry.relative_to(root).as_posix())
    return found


def _matches(pattern: str, files: list[str]) -> bool:
    """Does `pattern` name at least one file?

    Mirrors the globbing both tools use: a pattern containing a separator is
    matched against the whole relative path, one without is matched against the
    basename anywhere in the tree (which is what makes `__init__.py` and
    `test_*.py` legitimate entries rather than dead ones).
    """
    if "/" in pattern:
        return any(fnmatch.fnmatch(path, pattern) for path in files)
    return any(fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern) for path in files)


def _declared_paths() -> list[tuple[str, str]]:
    """(config key, path pattern) for every path the lint configs name."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    tools = config.get("tool", {})
    declared: list[tuple[str, str]] = []

    ruff_ignores = tools.get("ruff", {}).get("lint", {}).get("per-file-ignores", {})
    declared += [
        ("[tool.ruff.lint.per-file-ignores]", pattern) for pattern in ruff_ignores
    ]

    # flake8's entries are "pattern:CODE,CODE" strings, and a pattern may itself
    # contain no colon, so split once from the left.
    for entry in tools.get("flake8", {}).get("per-file-ignores", []):
        declared.append(("[tool.flake8].per-file-ignores", entry.split(":", 1)[0]))

    # Absent since fm#1442 — kept in the sweep so that re-adding the key brings
    # the check with it rather than needing someone to remember this file.
    for path in tools.get("mypy", {}).get("files", []):
        declared.append(("[tool.mypy].files", path))

    return declared


def test_the_configs_declare_paths_to_check():
    """Guard the guard: a parse that found nothing would pass over anything."""
    declared = _declared_paths()
    keys = {key for key, _ in declared}
    assert len(declared) >= 6, (
        f"only {len(declared)} path patterns were parsed out of {PYPROJECT.name} — "
        "the sweep below is close to vacuous, so its clean verdict says little"
    )
    assert "[tool.ruff.lint.per-file-ignores]" in keys
    assert "[tool.flake8].per-file-ignores" in keys


def test_the_matcher_rejects_a_path_that_is_not_there(tmp_path):
    """POSITIVE CONTROL.

    A matcher that answered True for everything would make the sweep below pass
    over exactly the entries this file exists to catch. Both the dead shape and
    the two live shapes are built here rather than asserted against the tree,
    because the tree no longer contains the dead one.
    """
    (tmp_path / "faultmaven" / "modules" / "agent").mkdir(parents=True)
    (tmp_path / "faultmaven" / "__init__.py").touch()
    (tmp_path / "faultmaven" / "main.py").touch()
    (tmp_path / "faultmaven" / "modules" / "agent" / "tools.py").touch()
    files = _repo_files(tmp_path)

    # The shape that was in the config: a full path to a file that is gone.
    assert not _matches("faultmaven/agent/doctrine.py", files)
    assert not _matches("faultmaven/session_management.py", files)

    # The shapes that are legitimate: a full path that resolves, and a bare
    # basename glob that is meant to match anywhere in the tree.
    assert _matches("faultmaven/main.py", files)
    assert _matches("__init__.py", files)


def test_every_lint_config_path_resolves():
    files = _repo_files()
    dead = sorted(
        f"{key}: {pattern}"
        for key, pattern in _declared_paths()
        if not _matches(pattern, files)
    )
    assert dead == [], (
        "lint configs name paths that match no file, so those entries are never "
        "consulted and cannot be noticed going stale:\n  " + "\n  ".join(dead)
    )
