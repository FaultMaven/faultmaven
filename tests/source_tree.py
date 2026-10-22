"""One parse of each ``faultmaven/`` module per process, for every scan (#2171).

About forty guards read the package as syntax trees, and each used to parse it
itself: one unit run parsed the 11.5 MB tree 75 times over. :func:`parse_file`
parses a file once and hands every later caller the same tree.

The key is the file's path and its exact text, read on every call, so a
rewritten file re-parses and two files never share a tree. The trees are
shared, so they are read-only: a caller that edits one (``NodeTransformer``,
``node.parent = ...``) parses its own copy with ``ast.parse``.

Only ``faultmaven/`` comes through here: holding ``tests/`` (18 MB) as trees
would cost each worker about 450 MB.
"""

from __future__ import annotations

import ast
import functools
import os
import pathlib
import warnings

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "faultmaven"


def parse_file(path: str | os.PathLike[str]) -> ast.Module:
    """The tree of ``path``'s current UTF-8 text; a failure is not cached."""
    filename = os.path.abspath(path)
    return _parse(filename, pathlib.Path(filename).read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=None)
def _parse(filename: str, source: str) -> ast.Module:
    # A module's own invalid escapes are the linter's finding. With one parse
    # per process they would warn in whichever test happened to parse first.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
        return ast.parse(source, filename=filename)
