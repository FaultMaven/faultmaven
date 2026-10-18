"""Every chromadb HTTP client in ``faultmaven/`` is built by one builder (#1462, #2056).

``build_http_client`` is where the shared credential (#1173) and the request
timeout (#2056) are applied. A client built anywhere else gets neither:
chromadb's own session has no timeout, so a stalled server would hold its
caller again, and a site that skips ``chroma_token_auth_kwargs`` sends no
token. There were two such sites, each applying the credential by hand,
and nothing failed when a third appeared.

The scan walks every ``.py`` under ``faultmaven/`` and flags each shape that
names an HTTP client or its server API:

- an attribute ``.HttpClient`` / ``.AsyncHttpClient`` / ``.CloudClient``,
  whatever the module is called locally;
- a ``from chromadb… import`` of those names, of the ``FastAPI`` /
  ``AsyncFastAPI`` server classes, or of ``*``;
- a string constant exactly equal to one of those client names, or to the
  dotted ``chroma_api_impl`` paths, as ``getattr(chromadb, "HttpClient")`` or
  ``Settings(chroma_api_impl="chromadb.api.fastapi.FastAPI")`` spell it;
- the keyword ``chroma_api_impl=``.

The only allowed site is inside ``build_http_client`` in
``infrastructure/chroma_client.py``. The positive control requires exactly one
site there, so the test fails if the scan goes blind as well as if a second
door opens.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[3]
_PACKAGE = _REPO / "faultmaven"
_DOOR_FILE = "faultmaven/infrastructure/chroma_client.py"
_DOOR_FUNCTION = "build_http_client"

_CLIENT_NAMES = frozenset({"HttpClient", "AsyncHttpClient", "CloudClient"})
_SERVER_API_NAMES = frozenset({"FastAPI", "AsyncFastAPI"})
_API_IMPL_PATHS = frozenset(
    {"chromadb.api.fastapi.FastAPI", "chromadb.api.async_fastapi.AsyncFastAPI"}
)

Site = Tuple[int, Optional[str], str]  # (line, enclosing function, shape)


class _Scanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self.sites: List[Site] = []
        self._functions: List[str] = []

    def _flag(self, node: ast.AST, shape: str) -> None:
        enclosing = self._functions[-1] if self._functions else None
        self.sites.append((getattr(node, "lineno", 0), enclosing, shape))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _CLIENT_NAMES:
            self._flag(node, f"attribute .{node.attr}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module == "chromadb" or module.startswith("chromadb."):
            for alias in node.names:
                if (
                    alias.name in _CLIENT_NAMES
                    or alias.name in _SERVER_API_NAMES
                    or alias.name == "*"
                ):
                    self._flag(node, f"from {module} import {alias.name}")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and (
            node.value in _CLIENT_NAMES or node.value in _API_IMPL_PATHS
        ):
            self._flag(node, f"constant {node.value!r}")

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg == "chroma_api_impl":
            self._flag(node, "keyword chroma_api_impl=")
        self.generic_visit(node)


def _scan_source(source: str) -> List[Site]:
    scanner = _Scanner()
    scanner.visit(ast.parse(source))
    return scanner.sites


def _scan_package() -> Tuple[List[Tuple[str, Site]], List[str]]:
    hits: List[Tuple[str, Site]] = []
    scanned: List[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        relative = path.relative_to(_REPO).as_posix()
        scanned.append(relative)
        for site in _scan_source(path.read_text(encoding="utf-8")):
            hits.append((relative, site))
    return hits, scanned


def test_the_only_http_client_construction_is_the_builder() -> None:
    hits, scanned = _scan_package()

    # Where the rule can be violated, so a scan that stopped looking fails:
    # the two former construction sites, and the door itself.
    assert len(scanned) > 500
    for must_scan in (
        _DOOR_FILE,
        "faultmaven/container/providers/infrastructure.py",
        "faultmaven/modules/knowledge/domain/services/ingestion.py",
    ):
        assert must_scan in scanned

    outside = [
        (path, site)
        for path, site in hits
        if not (path == _DOOR_FILE and site[1] == _DOOR_FUNCTION)
    ]
    assert not outside, (
        "chromadb HTTP clients must be built by "
        f"chroma_client.build_http_client, which applies the credential and the "
        f"request timeout (#1462, #2056). Other sites: {outside}"
    )

    # Positive control: the scan sees the one door it allows.
    door = [site for path, site in hits if path == _DOOR_FILE]
    assert len(door) == 1, door
    assert door[0][1] == _DOOR_FUNCTION
    assert door[0][2] == "attribute .HttpClient"


@pytest.mark.parametrize(
    "plant",
    [
        "import chromadb\nchromadb.HttpClient(host=h)",
        "from chromadb import HttpClient",
        "from chromadb import HttpClient as HC\nHC(h)",
        "import chromadb as c\nc.HttpClient(h)",
        'import chromadb\ngetattr(chromadb, "HttpClient")(h)',
        "import chromadb\nchromadb.AsyncHttpClient(h)",
        "import chromadb\nchromadb.CloudClient()",
        "import chromadb\nfrom chromadb.config import Settings\n"
        'chromadb.Client(Settings(chroma_api_impl="chromadb.api.fastapi.FastAPI"))',
        "from chromadb.api.fastapi import FastAPI",
        "from chromadb.api.async_fastapi import AsyncFastAPI",
        "from chromadb import *\nHttpClient(h)",
        'from chromadb.config import Settings\nSettings(**{"chroma_api_impl": '
        '"chromadb.api.fastapi.FastAPI"})',
        "def build_http_client():\n    import chromadb\n    chromadb.HttpClient(h)",
    ],
)
def test_each_shape_of_a_second_door_is_flagged(plant: str) -> None:
    """Parsed from a string, so each plant is scanned as a file outside the
    door. The last one is a function NAMED like the door in another file: the
    allowance is the file and the function together."""
    sites = _scan_source(plant)

    assert sites, f"missed: {plant!r}"


@pytest.mark.parametrize(
    "innocent",
    [
        '"""Builds an HttpClient through the door."""',
        "# chromadb.HttpClient(host=h) is built elsewhere\nx = 1",
        "import chromadb\nchromadb.PersistentClient(path=p)",
        'message = "the HttpClient arm"',
        "import chromadb.api.fastapi\n" "isinstance(c, chromadb.api.fastapi.FastAPI)",
    ],
)
def test_mentions_that_build_nothing_are_not_flagged(innocent: str) -> None:
    assert _scan_source(innocent) == []
