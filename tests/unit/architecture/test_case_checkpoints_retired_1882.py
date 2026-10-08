"""Case checkpoints are retired, not deprecated (#1882, owner ruling 2026-10-08).

Nothing ever read ``case_checkpoints``, no production code ever wrote one, and
the facts a pre-transition snapshot would hold are kept where they are read:
``case_actions``, ``statement_history`` and ``turn_history``. Revision 009 drops
the table. This pins that no code path in ``faultmaven/`` can bring the
component back by halves: no name, attribute, parameter, import or string that
says "checkpoint".

Read by AST, so comments (history notes, and the unrelated "M3 checkpoint" of
the hypothesis methodology) are not symbols. The alembic history is outside the
scanned tree: the baseline creates the table and 009 drops it, as history must.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "faultmaven"


def _symbols(tree: ast.AST):
    """Every identifier and string literal in ``tree``, with its line."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.lineno, node.id
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.lineno, node.name
        elif isinstance(node, ast.arg):
            yield node.lineno, node.arg
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.value.lineno, node.arg
        elif isinstance(node, ast.alias):
            yield node.lineno, f"{node.name} {node.asname or ''}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.lineno, node.module
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _is_docstring(node: ast.AST) -> bool:
    return isinstance(node, ast.Expr) and isinstance(
        getattr(node, "value", None), ast.Constant
    )


def test_no_checkpoint_symbol_remains_in_faultmaven():
    files = sorted(SOURCE.rglob("*.py"))
    assert len(files) > 500, f"scanned the wrong tree: {SOURCE}"
    found = []
    for path in files:
        tree = ast.parse(path.read_text())
        # Docstrings are prose, like comments: drop them before scanning.
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and _is_docstring(body[0]):
                body.pop(0)
        for lineno, text in _symbols(tree):
            if "checkpoint" in text.lower():
                found.append(f"{path.relative_to(ROOT)}:{lineno}: {text[:80]!r}")
    assert found == [], "case checkpoints are retired (#1882):\n" + "\n".join(found)


def test_the_scan_sees_a_planted_symbol(tmp_path):
    """Positive control: the walk reports a name, a string and an import."""
    tree = ast.parse(
        "from x.checkpoint_service import CheckpointService\n"
        "def f(checkpoint_id):\n"
        "    return 'case_checkpoints'\n"
    )
    texts = [text for _, text in _symbols(tree) if "checkpoint" in text.lower()]
    assert len(texts) >= 4, texts
