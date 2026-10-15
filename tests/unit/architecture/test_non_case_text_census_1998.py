"""Every ``non_case_text`` site in the package is declared, with its reason (#1998, A3).

``non_case_text`` mints a plain ``str`` as model-bound text with no redaction:
a CLAIM that the text carries no case content. It cannot be checked by
looking at the argument — three of the real sites build their text from tool
names or schemas, not literals — so each claim is made where a reviewer sees
it: declared here as ``(file, function) -> (count, reason)``. A new site, a
moved one or a changed count fails until it is declared.

This census collects every place the package reaches ``non_case_text``:
calls, and the function read without being called (stored, passed, bound with
``partial``). An import under another name fails outright, since the alias
would hide every later site.

#1998 P1 ships the function and this census with no sites: no caller has moved
yet. P2 declares each site as it converts it.

What this does NOT see: a call through a name computed at runtime, and tests
(``tests/`` mints through ``tests/model_content.py``, or calls this freely).
"""

from __future__ import annotations

import ast
import sys
import warnings
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.architecture, pytest.mark.security]

_ROOT = Path(__file__).resolve().parents[3]
#: Where the claims are made: the package.
_PACKAGE = _ROOT / "faultmaven"
_NAME = "non_case_text"
#: The module that defines it: its ``def`` is not a site.
_DEFINER = "faultmaven/infrastructure/security/model_content.py"

#: (file, function) -> (sites, why the text carries no case content).
SITES: dict[tuple[str, str], tuple[int, str]] = {}


def _parse(source: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
        return ast.parse(source)


def sites(source: str) -> Counter:
    """``{function qualname: sites}`` for one module's source.

    Raises ``AssertionError`` on an import of the name under an alias.
    """
    found: Counter = Counter()
    if _NAME not in source:  # necessary for every shape below; the walk decides
        return found
    tree = _parse(source)
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

    def scope_of(node: ast.AST) -> str:
        names = []
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
        return ".".join(reversed(names)) or "<module>"

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert not (alias.name == _NAME and alias.asname), (
                    f"line {node.lineno}: {_NAME} imported as {alias.asname}; "
                    "an alias hides its sites from this census"
                )
        elif isinstance(node, ast.Name) and node.id == _NAME:
            if isinstance(node.ctx, ast.Load):
                found[scope_of(node)] += 1
        elif isinstance(node, ast.Attribute) and node.attr == _NAME:
            if isinstance(node.ctx, ast.Load):
                found[scope_of(node)] += 1
    return found


def scan(root: Path) -> tuple[Counter, int]:
    """``{(file, function): sites}`` over ``root/faultmaven``, and the file count."""
    found: Counter = Counter()
    files = 0
    for path in sorted((root / "faultmaven").rglob("*.py")):
        files += 1
        rel = path.relative_to(root).as_posix()
        for scope, count in sites(path.read_text(encoding="utf-8")).items():
            found[(rel, scope)] = count
    return found, files


def undeclared(found: Counter) -> dict:
    return {k: n for k, n in found.items() if k not in SITES}


@lru_cache(maxsize=1)
def _scan() -> tuple[Counter, int]:
    return scan(_ROOT)


def test_the_scan_covers_the_package_and_finds_the_definition():
    """A positive control: the scan reads the package and reaches the definer."""
    found, files = _scan()
    assert files > 400, f"scanned {files} files under {_PACKAGE}"
    definer = (_ROOT / _DEFINER).read_text(encoding="utf-8")
    assert f"def {_NAME}(" in definer
    assert set(SITES) <= set(found), "a declared site was not found"


def test_every_site_is_declared():
    found, _ = _scan()
    declared = {key: value[0] for key, value in SITES.items()}
    new = undeclared(found)
    recounted = {
        k: (declared[k], n)
        for k, n in found.items()
        if k in declared and declared[k] != n
    }
    assert not new, (
        f"new {_NAME} sites — each is a claim that the text carries no case "
        "content; declare it in SITES with the reason:\n  "
        + "\n  ".join(f"{f}::{s} ({n})" for (f, s), n in sorted(new.items()))
    )
    assert not recounted, f"the number of sites changed (declared, found): {recounted}"


def test_every_declared_site_has_a_reason():
    for key, (count, reason) in SITES.items():
        assert count > 0 and reason.strip(), key


@pytest.mark.parametrize(
    "snippet",
    [
        f"from m import {_NAME}\ndef f():\n    return {_NAME}('Say hello')",
        f"def f(mc):\n    return mc.{_NAME}('x')",
        f"from functools import partial\ndef f():\n    return partial({_NAME}, 'x')",
        f"def f():\n    g = {_NAME}\n    return g('x')",
        f"def f(items):\n    return list(map({_NAME}, items))",
    ],
)
def test_every_shape_is_collected(snippet):
    assert sum(sites(snippet).values()) == 1, snippet


def test_an_aliased_import_fails():
    with pytest.raises(AssertionError, match="alias hides"):
        sites(f"from m import {_NAME} as fixed\n")


def test_a_planted_site_fails_the_census(tmp_path):
    """A positive control on the whole check, over a planted package."""
    package = tmp_path / "faultmaven"
    package.mkdir()
    (package / "planted.py").write_text(
        f"from x import {_NAME}\n\ndef send():\n    return {_NAME}('fixed')\n",
        encoding="utf-8",
    )
    found, files = scan(tmp_path)
    assert files == 1
    assert undeclared(found) == {("faultmaven/planted.py", "send"): 1}


def test_a_recounted_site_fails_the_census(monkeypatch):
    """A positive control on the recount path: a declared site whose count moved."""
    site = ("faultmaven/planted.py", "send")
    this = sys.modules[__name__]
    monkeypatch.setattr(this, "SITES", {site: (1, "fixed text")})
    monkeypatch.setattr(this, "_scan", lambda: (Counter({site: 2}), 500))
    with pytest.raises(AssertionError, match="number of sites changed"):
        test_every_site_is_declared()
