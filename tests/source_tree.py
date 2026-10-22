"""One parse of each ``faultmaven/`` module per process, for every scan (#2171).

About forty guards read the package as syntax trees, and each used to parse it
itself: one unit run parsed the 11.5 MB tree 75 times over. A package file's
tree now comes from one cache, keyed on its resolved path and its exact text,
read on every call: a rewritten file re-parses and two files never share a
tree. Anything outside ``faultmaven/`` (``tests/`` is 18 MB, and a tmp probe
lives for one test) gets a plain ``ast.parse``.

**The trees are shared, so they are read-only.** A caller that edits one
(``NodeTransformer``, ``node.parent = ...``) parses its own copy.

**Node identity.** A package file's tree is the same object for every pass and
every caller. ``tests/error_text_ast.py`` depends on one tree per entry point:
its passes share node-identity sets, and ``id()`` is comparable only across
nodes of one parse. Re-parsing per pass made every ``in_handler`` lookup miss,
which reported two static-message 500 arms in ``modules/case/api/routes/`` as
leaks. A file outside the package is parsed afresh on every call, so such a
caller parses once and hands the tree to each pass.
"""

from __future__ import annotations

import ast
import functools
import os
import pathlib

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "faultmaven"


def parse_file(path: str | os.PathLike[str]) -> ast.Module:
    """The tree of ``path``'s current UTF-8 text."""
    return parse_source(pathlib.Path(path).read_text(encoding="utf-8"), path)


def parse_source(source: str, path: str | os.PathLike[str]) -> ast.Module:
    """The tree of ``source``, the text of ``path``: shared when ``path`` is
    under ``faultmaven/``; a failure is never cached."""
    resolved = pathlib.Path(path).resolve()
    if resolved.is_relative_to(PACKAGE_ROOT):
        return _parse(str(resolved), source)
    return ast.parse(source, filename=str(path))


@functools.lru_cache(maxsize=None)
def _parse(filename: str, source: str) -> ast.Module:
    return ast.parse(source, filename=filename)
