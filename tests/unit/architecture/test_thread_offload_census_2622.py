"""Principle 14's census of thread offloads stays equal to the code (#2622).

A thread keeps neither the event loop free nor a deadline, so every module
that hands work to one has a row in the census in
``architectural-design-principles.md`` (Principle 14, Enforcement), saying
what it hands over and what keeps the rule: I/O, native code that releases
the lock, redaction, or a #2596 stage to come. Work over a user's document
moved into threads one unit at a time (#2153, #2592, #2596, #2597).

This test checks the set of files and nothing else: a module that starts or
stops calling ``to_thread``, ``run_in_executor`` or an executor's ``submit``
fails it until its row is added or removed. It does not check that a row's
kind is true; review does.
"""

from __future__ import annotations

import ast
import re

import pytest

from tests.source_tree import PACKAGE_ROOT, parse_file

pytestmark = [pytest.mark.unit]

_REPO = PACKAGE_ROOT.parent
_PRINCIPLES = (
    _REPO / "docs/architecture/core-architecture/architectural-design-principles.md"
)

#: The calls that put work on a thread.
_OFFLOADS = {"to_thread", "run_in_executor", "submit"}

_ROW = re.compile(r"^\| `(faultmaven/[^`]+\.py)` \|", re.MULTILINE)


def _offloads(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else None
            name = name or getattr(func, "id", None)
            if name in _OFFLOADS:
                return True
    return False


def _census() -> set[str]:
    text = _PRINCIPLES.read_text(encoding="utf-8")
    section = text[text.index("**The thread offload census.**") :]
    section = section[: section.index("\n---")]
    return set(_ROW.findall(section))


def test_the_census_names_every_module_that_offloads_to_a_thread():
    sources = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert len(sources) > 200, f"only {len(sources)} files under {PACKAGE_ROOT}"
    found = {
        path.relative_to(_REPO).as_posix()
        for path in sources
        if _offloads(parse_file(path))
    }
    census = _census()

    assert found - census == set(), (
        "these modules hand work to a thread and have no row in Principle 14's "
        "census: add one saying what runs there and what keeps the rule"
    )
    assert census - found == set(), (
        "these census rows name a module that no longer offloads to a thread: "
        "remove them"
    )


def test_the_scan_sees_each_form_of_offload():
    """A census that cannot see an offload is not one."""
    for source, expected in (
        ("async def f():\n    await asyncio.to_thread(g)\n", True),
        ("async def f(loop):\n    await loop.run_in_executor(None, g)\n", True),
        ("def f(pool):\n    return pool.submit(g)\n", True),
        ("async def f():\n    return await g()\n", False),
    ):
        assert _offloads(ast.parse(source)) is expected, source
