"""The shared parse behind the source scans (``tests/source_tree.py``, #2171):
only a package file is shared, and only for the same path and the same text."""

import ast

import pytest

from tests.source_tree import PACKAGE_ROOT, parse_file, parse_source

pytestmark = pytest.mark.unit


def test_a_package_file_is_parsed_once():
    spelled_twice = PACKAGE_ROOT / "utils" / ".." / "__init__.py"

    assert parse_file(PACKAGE_ROOT / "__init__.py") is parse_file(spelled_twice)


def test_new_text_at_the_same_path_is_parsed_again():
    """Keyed on the text, not a timestamp: a same-length rewrite re-parses."""
    path = PACKAGE_ROOT / "_probe_2171.py"
    before = parse_source("x = 1\n", path)

    after = parse_source("y = 2\n", path)

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


def test_a_source_files_own_warnings_still_surface():
    """The linter does not select W605, so the parse's warning is the signal."""
    with pytest.warns((DeprecationWarning, SyntaxWarning), match="invalid escape"):
        parse_source('x = "\\d"\n', PACKAGE_ROOT / "_probe_warns.py")
