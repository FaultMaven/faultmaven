"""A teardown imports what a step needs inside that step (#2257, #2267).

``run_teardown`` runs every step whatever the ones before it did. An import made
while the steps are built, or in the block that runs them, sits outside every
step: if it fails, no step runs, the engine disposal and the container's
shutdown included. Twice such imports were moved into their steps by hand
(#2257, #2267), and the second pass still missed one (``run_teardown`` itself,
imported in the jobs runner's ``finally``). This scan finds every block that
calls ``run_teardown``, and every step builder passed to it, and refuses an
import in them outside a nested function.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.unit

_PACKAGE = Path(faultmaven.__file__).parent
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

#: Every module that runs a teardown. A new one fails the count test below, so
#: the scan is known to have looked at it.
_TEARDOWN_RUNNERS = {
    "bootstrap/composition.py",
    "bootstrap/lifespan.py",
    "cli/_container.py",
    "jobs/run.py",
}


def _shallow(node: ast.AST):
    """``node`` and what it contains, short of functions and classes: a step's
    body runs when the step does, and a definition's own block is scanned as a
    block of its own."""
    if isinstance(node, _NESTED):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _shallow(child)


def _own(node: ast.AST):
    """A statement's own expressions: not the statements it contains, whose
    block is scanned as a block of its own."""
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.stmt, ast.excepthandler, *_NESTED)):
            yield from _own(child)


def _runs_teardown(stmt: ast.stmt) -> list[ast.Call]:
    return [
        node
        for node in _own(stmt)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "run_teardown")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_teardown"
            )
        )
    ]


def _blocks(tree: ast.AST):
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


def imports_outside_steps(source: str) -> tuple[int, list[str]]:
    """``(teardowns found, imports outside their steps as "line: module")``."""
    tree = ast.parse(source)
    builders = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    teardowns, found = 0, []
    for block in _blocks(tree):
        calls = [call for stmt in block for call in _runs_teardown(stmt)]
        if not calls:
            continue
        teardowns += len(calls)
        scanned = list(block)
        for call in calls:
            for arg in call.args:
                if (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Name)
                    and arg.func.id in builders
                ):
                    scanned.extend(builders[arg.func.id].body)
        for stmt in scanned:
            for node in _shallow(stmt):
                if isinstance(node, ast.Import):
                    found += [f"{node.lineno}: {a.name}" for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    found.append(f"{node.lineno}: {node.module}")
    return teardowns, found


def test_no_teardown_imports_outside_its_steps():
    runners, offenders = set(), {}
    for path in sorted(_PACKAGE.rglob("*.py")):
        teardowns, found = imports_outside_steps(path.read_text())
        relative = path.relative_to(_PACKAGE).as_posix()
        if teardowns:
            runners.add(relative)
        if found:
            offenders[relative] = found
    assert runners == _TEARDOWN_RUNNERS, "a teardown the scan was not told about"
    assert offenders == {}, (
        "imports outside every teardown step: a failure there skips every "
        f"step. Import inside the step that needs it: {offenders}"
    )


def test_the_scan_finds_an_import_in_the_block_that_runs_the_teardown():
    """Positive control, in the shape the jobs runner's ``finally`` had."""
    source = """
async def run():
    try:
        pass
    finally:
        from faultmaven.infrastructure.concurrency.teardown import run_teardown

        async def _close():
            from faultmaven.infrastructure.persistence.database import close_database

            await close_database()

        await run_teardown((("Database close", _close),))
"""
    assert imports_outside_steps(source) == (
        1,
        ["6: faultmaven.infrastructure.concurrency.teardown"],
    )


def test_the_scan_finds_an_import_in_a_step_builder():
    """Positive control, in the shape ``_serving_stops`` had."""
    source = """
def _stops():
    from faultmaven.infrastructure.llm.usage_ledger import install_usage_ledger

    return (("LLM usage ledger uninstall", lambda: install_usage_ledger(None)),)


async def stop():
    await run_teardown(_stops())
"""
    assert imports_outside_steps(source) == (
        1,
        ["3: faultmaven.infrastructure.llm.usage_ledger"],
    )
