"""Every chromadb HTTP client in ``faultmaven/`` is built by one builder (#1462, #2056).

``build_http_client`` is where the shared credential (#1173) and the request
timeout (#2056) are applied. A client built anywhere else gets neither:
chromadb's own session has no timeout, so a stalled server would hold its
caller again, and a site that skips ``chroma_token_auth_kwargs`` sends no
token. There were two such sites, each applying the credential by hand,
and nothing failed when a third appeared.

The scan walks every ``.py`` under ``faultmaven/`` and flags each shape that
names an HTTP client or its server API:

- an attribute ``.HttpClient`` / ``.AsyncHttpClient`` / ``.CloudClient``, or
  ``.FastAPI`` / ``.AsyncFastAPI``, whatever the module is called locally;
- a ``from chromadb… import`` of those names, of the ``fastapi`` /
  ``async_fastapi`` modules, or of ``*``;
- a string constant exactly equal to one of the client names, or to the
  dotted ``chroma_api_impl`` paths, as ``getattr(chromadb, "HttpClient")`` or
  ``Settings(chroma_api_impl="chromadb.api.fastapi.FastAPI")`` spell it;
- the keyword ``chroma_api_impl=``, unless its value is a string literal that
  is not an HTTP implementation (``create_persistent_client`` pins the local
  one, so the environment cannot choose).

Three sites are allowed, all in ``infrastructure/chroma_client.py``, each
pinned to its function AND its shape: the construction in
``build_http_client`` (``.HttpClient``), and the type test in
``is_http_client`` (``.FastAPI`` and ``.AsyncFastAPI``, so an
``AsyncHttpClient`` is refused too). The positive control requires exactly one
of each, so the test fails if the scan goes blind as well as if a second door
opens.

**What a source scan cannot see has a run-time guard.** A client name built
at run time, settings cloned from another client, a ``chroma_api_impl``
assigned from a variable, or chromadb's ``Settings`` reading
``CHROMA_API_IMPL`` from the environment all reach no AST. Every such client
that reaches a vector store is refused there by
``chroma_client.blocking_executor_for``, which accepts an HTTP client only
if ``build_http_client`` marked it (``test_chroma_http_client.py``). This
scan remains the guard for the credential, which a client used outside a
store would also skip.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from tests.source_tree import parse_file

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[3]
_PACKAGE = _REPO / "faultmaven"
_DOOR_FILE = "faultmaven/infrastructure/chroma_client.py"
#: (file, enclosing function, shape): the only sites allowed.
_ALLOWED = frozenset(
    {
        (_DOOR_FILE, "build_http_client", "attribute .HttpClient"),
        (_DOOR_FILE, "is_http_client", "attribute .FastAPI"),
        (_DOOR_FILE, "is_http_client", "attribute .AsyncFastAPI"),
    }
)

_CLIENT_NAMES = frozenset({"HttpClient", "AsyncHttpClient", "CloudClient"})
_SERVER_API_NAMES = frozenset({"FastAPI", "AsyncFastAPI"})
_SERVER_API_MODULES = frozenset({"fastapi", "async_fastapi"})
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
        if node.attr in _CLIENT_NAMES or node.attr in _SERVER_API_NAMES:
            self._flag(node, f"attribute .{node.attr}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module == "chromadb" or module.startswith("chromadb."):
            for alias in node.names:
                if (
                    alias.name in _CLIENT_NAMES
                    or alias.name in _SERVER_API_NAMES
                    or alias.name in _SERVER_API_MODULES
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
            value = node.value
            pinned_local = (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value not in _API_IMPL_PATHS
            )
            if not pinned_local:
                self._flag(node, "keyword chroma_api_impl=")
        self.generic_visit(node)


def _scan_source(source: str | Path) -> List[Site]:
    scanner = _Scanner()
    scanner.visit(parse_file(source) if isinstance(source, Path) else ast.parse(source))
    return scanner.sites


def _is_allowed(path: str, site: Site) -> bool:
    return (path, site[1], site[2]) in _ALLOWED


def _scan_package() -> Tuple[List[Tuple[str, Site]], List[str]]:
    hits: List[Tuple[str, Site]] = []
    scanned: List[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        relative = path.relative_to(_REPO).as_posix()
        scanned.append(relative)
        for site in _scan_source(path):
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

    outside = [(path, site) for path, site in hits if not _is_allowed(path, site)]
    assert not outside, (
        "chromadb HTTP clients must be built by "
        f"chroma_client.build_http_client, which applies the credential and the "
        f"request timeout (#1462, #2056). Other sites: {outside}"
    )

    # Positive control: the scan sees each allowed site, once.
    door = sorted((path, site[1], site[2]) for path, site in hits)
    assert door == sorted(_ALLOWED), door


@pytest.mark.parametrize(
    "path,site,allowed",
    [
        (_DOOR_FILE, (1, "build_http_client", "attribute .HttpClient"), True),
        (_DOOR_FILE, (1, "is_http_client", "attribute .FastAPI"), True),
        (_DOOR_FILE, (1, "is_http_client", "attribute .AsyncFastAPI"), True),
        # Another function in the door's own file.
        (_DOOR_FILE, (1, "chroma_token_auth_kwargs", "attribute .HttpClient"), False),
        # The right function, another shape.
        (_DOOR_FILE, (1, "is_http_client", "attribute .HttpClient"), False),
        (_DOOR_FILE, (1, "build_http_client", "keyword chroma_api_impl="), False),
        (_DOOR_FILE, (1, "build_http_client", "attribute .AsyncFastAPI"), False),
        # A function named like the door, in another file.
        ("faultmaven/x.py", (1, "build_http_client", "attribute .HttpClient"), False),
        # Module level of the door's file.
        (_DOOR_FILE, (1, None, "attribute .HttpClient"), False),
    ],
)
def test_the_allowance_is_the_file_the_function_and_the_shape(
    path: str, site: Site, allowed: bool
) -> None:
    assert _is_allowed(path, site) is allowed


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
        # The server API by attribute or alias (#2056 review): building one
        # around a System is a client with no door.
        "import chromadb.api.fastapi\nchromadb.api.fastapi.FastAPI(system)",
        "import chromadb.api.fastapi as f\nf.FastAPI(system)",
        "from chromadb.api import fastapi",
        "from chromadb.api import async_fastapi as af",
        "def is_http_client(c):\n    import chromadb.api.fastapi\n"
        "    return isinstance(c, chromadb.api.fastapi.FastAPI)",
        # A chroma_api_impl that is not a literal: it may name the HTTP one.
        "from chromadb.config import Settings\nSettings(chroma_api_impl=impl)",
    ],
)
def test_each_shape_of_a_second_door_is_flagged(plant: str) -> None:
    """Parsed from a string, so each plant is scanned as a file outside the
    door. Two are functions NAMED like the allowed ones, in another file: the
    allowance is the file, the function and the shape together."""
    sites = _scan_source(plant)

    assert sites, f"missed: {plant!r}"


@pytest.mark.parametrize(
    "innocent",
    [
        '"""Builds an HttpClient through the door."""',
        "# chromadb.HttpClient(host=h) is built elsewhere\nx = 1",
        "import chromadb\nchromadb.PersistentClient(path=p)",
        'message = "the HttpClient arm"',
        # create_persistent_client's pin: a literal, and not an HTTP impl.
        "from chromadb.config import Settings\n"
        'Settings(chroma_api_impl="chromadb.api.rust.RustBindingsAPI")',
        # The ``fastapi`` web framework, not chromadb's server API.
        "from fastapi import FastAPI\napp = FastAPI()",
    ],
)
def test_mentions_that_build_nothing_are_not_flagged(innocent: str) -> None:
    assert _scan_source(innocent) == []
