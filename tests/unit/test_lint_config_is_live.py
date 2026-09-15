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

WHICH SECTIONS `pyproject.toml` HOLDS IS NOT AN INVARIANT, and this file must
never make it one. Both of the non-ruff sections above are deleted outright in
the same change that adds this file (fm#1445) for being read by nothing —
`[tool.mypy].files` because `ignore_errors = true` makes mypy a no-op nothing
runs, and `[tool.flake8]` because flake8 cannot read `pyproject.toml` without a
plugin this repository does not install. That is the same verdict this file
exists to reach, one scope up. A guard that put a floor under the TOTAL number
of declared paths, or that named a section it required to be present, would
therefore have turned red on the correct cleanup and defended the defect — the
first cut of this file did exactly that.

So the sections live in `_SECTIONS`, an absent one contributes nothing and is
not an error, and the anti-vacuity guard is split in two:
`test_the_parser_reads_every_shape_it_claims_to` proves the parser can still
read all three shapes — including the two the real file no longer holds —
against a synthetic document, and `test_the_configs_declare_paths_to_check`
only asks that the real file's ruff section was found. Ruff is the linter this
repository runs, so that one IS an invariant; the other two are not.
"""

import fnmatch
import pathlib
import tomllib
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

RUFF_KEY = "[tool.ruff.lint.per-file-ignores]"
FLAKE8_KEY = "[tool.flake8].per-file-ignores"
MYPY_KEY = "[tool.mypy].files"

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


def _ruff_patterns(section: Any) -> list[str]:
    """ruff: a table keyed by the pattern itself."""
    return list(section)


def _flake8_patterns(section: Any) -> list[str]:
    """flake8: a list of `pattern:CODE,CODE` strings.

    A pattern may itself contain no colon, so split once from the left.
    """
    return [str(entry).split(":", 1)[0] for entry in section]


def _plain_patterns(section: Any) -> list[str]:
    """mypy: a plain list of paths."""
    return [str(entry) for entry in section]


# Where each declaration shape lives under `[tool]`, and how it spells a path.
# Entries are kept for sections the file no longer holds so that re-adding one
# brings its check back with it, rather than needing someone to remember this
# file. An absent section contributes nothing — see the module docstring.
_SECTIONS: dict[str, tuple[tuple[str, ...], Any]] = {
    RUFF_KEY: (("ruff", "lint", "per-file-ignores"), _ruff_patterns),
    FLAKE8_KEY: (("flake8", "per-file-ignores"), _flake8_patterns),
    MYPY_KEY: (("mypy", "files"), _plain_patterns),
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


def _section(config: dict, path: tuple[str, ...]) -> Any:
    """The value at `tool.<path>`, or None where the section is absent."""
    node: Any = config.get("tool", {})
    for step in path:
        if not isinstance(node, dict) or step not in node:
            return None
        node = node[step]
    return node


def _declared_paths(config: dict) -> list[tuple[str, str]]:
    """(config key, path pattern) for every path the lint configs name."""
    declared: list[tuple[str, str]] = []
    for key, (path, patterns_of) in _SECTIONS.items():
        section = _section(config, path)
        if section:
            declared += [(key, pattern) for pattern in patterns_of(section)]
    return declared


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_the_parser_reads_every_shape_it_claims_to():
    """GUARD THE GUARD, as a positive control rather than as an inventory.

    A parse that found nothing would make the sweep below pass over anything.
    The document is built here rather than asserted against `pyproject.toml`
    because two of the three shapes are no longer in that file, and requiring
    them to be would forbid exactly the cleanup this file argues for. Proving
    the parser against a synthetic document instead keeps both true at once:
    deleting a dead section stays free, and re-adding one brings its check back
    already working.

    Every entry of every section must come out, not merely the first — the
    sweep's reach is the whole point, and a parser that read one entry per
    section would report a clean verdict it had not earned.
    """
    synthetic = tomllib.loads("""
        [tool.ruff.lint.per-file-ignores]
        "__init__.py" = ["F401"]
        "faultmaven/main.py" = ["F841"]

        [tool.flake8]
        per-file-ignores = ["conftest.py:F401", "faultmaven/agent/doctrine.py:F841"]

        [tool.mypy]
        files = ["faultmaven/session_management.py", "faultmaven/models/api.py"]
        """)
    assert sorted(_declared_paths(synthetic)) == sorted(
        [
            (RUFF_KEY, "__init__.py"),
            (RUFF_KEY, "faultmaven/main.py"),
            (FLAKE8_KEY, "conftest.py"),
            (FLAKE8_KEY, "faultmaven/agent/doctrine.py"),
            (MYPY_KEY, "faultmaven/session_management.py"),
            (MYPY_KEY, "faultmaven/models/api.py"),
        ]
    )

    # An absent section is not an error, and is the state the real file is in
    # for two of the three. It must contribute nothing rather than raise.
    assert _declared_paths({}) == []
    assert _declared_paths({"tool": {"ruff": {}}}) == []


def test_the_configs_declare_paths_to_check():
    """The sweep below is pointed at the real file and found ruff's section.

    Deliberately NOT a floor on the total, and deliberately not a list of
    sections that must be present: see the module docstring. Ruff is the linter
    this repository runs, so its per-file-ignores existing is an invariant;
    `[tool.flake8]` and `[tool.mypy].files` existing is not, and asserting it
    was how the first cut of this file came to defend the defect it was written
    to catch.
    """
    ruff = [
        pattern for key, pattern in _declared_paths(_pyproject()) if key == RUFF_KEY
    ]
    assert ruff, (
        f"no {RUFF_KEY} entries were parsed out of {PYPROJECT.name} — ruff is the "
        "linter this repository gates on, so the sweep below is reading either the "
        "wrong file or the wrong key, and its clean verdict says nothing"
    )


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
        for key, pattern in _declared_paths(_pyproject())
        if not _matches(pattern, files)
    )
    assert dead == [], (
        "lint configs name paths that match no file, so those entries are never "
        "consulted and cannot be noticed going stale:\n  " + "\n  ".join(dead)
    )
