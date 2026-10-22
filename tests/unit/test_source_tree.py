"""The shared parse behind the source scans (``tests/source_tree.py``, #2171):
only a package file is shared, and only for the same path and the same text."""

import ast

import pytest

from tests import source_tree
from tests.source_tree import PACKAGE_ROOT, parse_file, parse_source

pytestmark = pytest.mark.unit


def test_a_package_file_is_parsed_once():
    spelled_twice = PACKAGE_ROOT / "utils" / ".." / "__init__.py"

    assert parse_file(PACKAGE_ROOT / "__init__.py") is parse_file(spelled_twice)


def test_a_rewritten_package_file_is_read_and_parsed_again(tmp_path, monkeypatch):
    """Keyed on the text read now, not a timestamp or a stored copy."""
    monkeypatch.setattr(source_tree, "PACKAGE_ROOT", tmp_path.resolve())
    path = tmp_path / "module.py"
    path.write_text("x = 1\n", encoding="utf-8")
    before = parse_file(path)
    assert parse_file(path) is before
    path.write_text("y = 2\n", encoding="utf-8")

    after = parse_file(path)

    assert after is not before
    assert ast.dump(after) == ast.dump(ast.parse("y = 2\n"))


def test_two_files_with_the_same_text_do_not_share_a_tree():
    """A scan may key on node identity across files."""
    a, b = PACKAGE_ROOT / "_probe_a.py", PACKAGE_ROOT / "_probe_b.py"

    assert parse_source("x = 1\n", a) is not parse_source("x = 1\n", b)


def test_a_file_outside_the_package_is_not_kept(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("x = 1\n", encoding="utf-8")

    assert parse_file(path) is not parse_file(path)


@pytest.mark.parametrize("where", ["package", "outside"])
def test_a_source_files_own_warnings_still_surface(where, tmp_path):
    """The linter does not select W605, so the parse's warning is the signal,
    for a package file and for any other."""
    root = PACKAGE_ROOT if where == "package" else tmp_path
    with pytest.warns((DeprecationWarning, SyntaxWarning), match="invalid escape"):
        parse_source('x = "\\d"\n', root / "_probe_warns.py")
