"""The shared parse behind the source scans (``tests/source_tree.py``, #2171):
a tree is reused only for the same file with the same text."""

import ast

import pytest

from tests.source_tree import PACKAGE_ROOT, parse_file

pytestmark = pytest.mark.unit


def test_the_same_text_is_parsed_once(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("def f():\n    return 1\n", encoding="utf-8")

    assert parse_file(path) is parse_file(str(path))


def test_a_rewritten_file_is_parsed_again(tmp_path):
    """Keyed on the text, not a timestamp: a same-length rewrite re-parses."""
    path = tmp_path / "module.py"
    path.write_text("x = 1\n", encoding="utf-8")
    before = parse_file(path)
    path.write_text("y = 2\n", encoding="utf-8")

    after = parse_file(path)

    assert after is not before
    assert ast.dump(after) == ast.dump(ast.parse("y = 2\n"))


def test_two_files_with_the_same_text_do_not_share_a_tree(tmp_path):
    """A scan may key on node identity across files."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")

    assert parse_file(tmp_path / "a.py") is not parse_file(tmp_path / "b.py")


def test_the_package_root_is_the_package():
    assert isinstance(parse_file(PACKAGE_ROOT / "__init__.py"), ast.Module)
