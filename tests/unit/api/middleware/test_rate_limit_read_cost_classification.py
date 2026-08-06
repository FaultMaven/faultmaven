"""Only reads that can reach the embedder need a cost verdict (fm#994).

The rate limiter meters cheap reads in a roomy pair of per-session buckets so
SPA navigation does not compete with POST turns. That is safe only while "cheap"
is true of what it covers: a GET that embeds a query and searches the vector
store costs what a write costs, and giving it the roomy bucket gives it the
roomiest per-session ceiling in the system. The middleware names the exceptions
in ``EXPENSIVE_READ_PATTERNS``, and a hand-maintained list of exceptions rots
silently in the permissive direction — an endpoint added later inherits "cheap"
by saying nothing.

The guard is a reachability property rather than a route inventory. For every
read route on every mounted router, this asks whether the handler can reach the
embedder at all — through a declared dependency, or through an import inside the
handler body, which is how ``report-recommendations`` gets at
``RunbookKnowledgeBase``. Routes that cannot are cheap by construction and need
no verdict; only the ones that can are listed below.

That is what makes it small. Seven of the sixty-one read routes can reach the
embedder. The other fifty-four are not enumerated here — they are *proved* cheap
each run, so adding one costs nothing, and adding one that touches the vector
store fails this test until someone decides what it costs.

Reachability is deliberately coarser than truth: four of the seven hold a
``KnowledgeService`` but only read counts and rows with it. They are listed as
cheap, with the reason, rather than excluded by narrowing the probe — a probe
tuned until it flags exactly today's three answers would stop flagging tomorrow's
fourth.
"""

import ast
import importlib
import inspect
import re
import textwrap

import pytest

from faultmaven.api.middleware.rate_limiting import (
    EXPENSIVE_READ_PATTERNS,
    READ_ONLY_METHODS,
    is_cheap_read,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


# Every router mounted in ``main.py``, with the prefix it is mounted under.
# Enumerated from the routers rather than from the assembled app so this stays a
# unit test with no module-level ``main.app`` singleton, and rather than from
# ``app.openapi()``, whose output is not deterministic. Kept honest by
# ``test_the_mount_table_matches_main``.
MOUNTS = (
    ("/api/v1", "faultmaven.modules.agent.api.routes"),
    ("/api/v1", "faultmaven.modules.auth.api.auth"),
    ("/api/v1", "faultmaven.modules.auth.api.oauth"),
    ("/api/v1", "faultmaven.modules.auth.api.session"),
    ("/api/v1", "faultmaven.modules.auth.api.sso"),
    ("/api/v1", "faultmaven.modules.auth.api.teams"),
    ("/api/v1", "faultmaven.modules.case.api.routes"),
    ("/api/v1", "faultmaven.modules.knowledge.api.conversion_routes"),
    ("/api/v1", "faultmaven.modules.knowledge.api.routes"),
    ("/api/v1", "faultmaven.modules.report.api.routes"),
    ("", "faultmaven.api.routes.admin"),
    ("", "faultmaven.api.routes.admin_cases"),
    ("", "faultmaven.api.routes.admin_config"),
    ("", "faultmaven.api.routes.admin_grants"),
    ("", "faultmaven.api.routes.sessions"),
)

# Dependency providers and modules that put an embedder or a vector store within
# a handler's reach. Substring matches, on purpose: a new provider named
# ``get_case_vector_store`` should trip this without being added here.
_DEPENDENCY_MARKERS = (
    "vector_store",
    "knowledge_service",
    "recommendation_service",
    "rec_service",
    "runbook",
    "embedding",
    "embedder",
)
_IMPORT_MARKERS = (
    "infrastructure.knowledge",
    "runbook_kb",
    "vector_store",
    "embedding",
    "chromadb",
    "recommendation_service",
)

# The reads that can reach the embedder, and what each actually costs.
# ``True`` means the roomy read bucket is correct for it.
EMBEDDER_REACHABLE_READS = {
    # Embeds a query and runs a similarity search. Metered as a write.
    "/api/v1/cases/{case_id}/report-recommendations": False,
    "/api/v1/reports/recommendations/{case_id}": False,
    # ``get_semantic_snippet`` — embeds the query to locate the chunk.
    "/api/v1/knowledge/documents/{document_id}/snippet": False,
    # Hold a KnowledgeService but never embed with it: ``list_documents``,
    # ``get_document``, ``get_knowledge_stats`` and ``get_search_analytics``
    # read rows and counts. Verified by inspecting those methods for embedding
    # and similarity calls.
    "/api/v1/knowledge/documents": True,
    "/api/v1/knowledge/documents/{document_id}": True,
    "/api/v1/knowledge/stats": True,
    "/api/v1/knowledge/analytics/search": True,
}

_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _dependency_names(route):
    """Every callable in a route's dependency tree, by name."""
    names, stack = set(), [route.dependant]
    while stack:
        dependant = stack.pop()
        if dependant.call is not None:
            names.add(getattr(dependant.call, "__name__", "") or "")
        stack.extend(getattr(dependant, "dependencies", []))
    return {name for name in names if name}


def _handler_imports(endpoint):
    """Modules imported anywhere in the handler, including inside its body.

    Function-local imports are the shape this exists for:
    ``report-recommendations`` reaches ``RunbookKnowledgeBase`` through one, so a
    scan of module-level imports alone would miss it.

    **This half of the probe is redundant today** — every route it flags is
    already flagged by its declared dependencies, so neutralising it changes no
    verdict and no route-level test notices. It is kept because the shape it
    catches is real and present (a handler that constructs a knowledge base it
    never declared), and it is exercised directly by
    ``test_the_probe_sees_an_import_inside_a_handler_body`` rather than left to
    be proved by a live route that happens to need it.
    """
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    except (OSError, TypeError, SyntaxError):  # pragma: no cover - defensive
        return set()

    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _read_routes():
    """(path, can_reach_embedder) for every read route on every mounted router."""
    routes = {}
    for prefix, module_name in MOUNTS:
        router = importlib.import_module(module_name).router
        for route in router.routes:
            methods = getattr(route, "methods", None) or set()
            if not methods & READ_ONLY_METHODS:
                continue

            reachable = any(
                marker in name.lower()
                for name in _dependency_names(route)
                for marker in _DEPENDENCY_MARKERS
            ) or any(
                marker in module.lower()
                for module in _handler_imports(route.endpoint)
                for marker in _IMPORT_MARKERS
            )
            routes[prefix + route.path] = reachable
    return routes


def _concrete(path):
    """Substitute path params, so a route template can be run through matching."""
    return _PATH_PARAM.sub("x", path)


def test_every_read_that_can_reach_the_embedder_has_a_verdict():
    """A read route that touches the vector store must be classified on purpose."""
    reachable = {path for path, hit in _read_routes().items() if hit}

    unclassified = reachable - set(EMBEDDER_REACHABLE_READS)
    assert not unclassified, (
        "these read routes can reach the embedder or vector store and have no "
        "cost verdict. For each: if it embeds a query or runs a similarity "
        "search, add it to EXPENSIVE_READ_PATTERNS in the middleware and record "
        "it False here; if it only reads rows or counts, record it True with the "
        f"reason. {sorted(unclassified)}"
    )

    departed = set(EMBEDDER_REACHABLE_READS) - reachable
    assert not departed, (
        "these routes carry a verdict but can no longer reach the embedder — "
        f"either they were renamed or the probe stopped seeing them: {sorted(departed)}"
    )


@pytest.mark.parametrize(
    "path,expected_cheap", sorted(EMBEDDER_REACHABLE_READS.items())
)
def test_the_middleware_agrees_with_each_verdict(path, expected_cheap):
    assert is_cheap_read("GET", _concrete(path)) is expected_cheap, (
        f"{path} is recorded as {'cheap' if expected_cheap else 'expensive'} but "
        "the middleware meters it the other way"
    )


def test_reads_that_cannot_reach_the_embedder_are_cheap():
    """The fifty-four are proved, not listed — so adding one costs nothing.

    An expensive pattern that matched one of these would quietly move ordinary
    navigation onto the quota that protects LLM compute, which is the fm#994
    symptom with extra steps.
    """
    unreachable = [path for path, hit in _read_routes().items() if not hit]
    assert len(unreachable) > 40, "the probe stopped resolving routes"

    misclassified = [p for p in unreachable if not is_cheap_read("GET", _concrete(p))]
    assert not misclassified, (
        "these reads cannot reach the embedder but are metered as writes: "
        f"{sorted(misclassified)}"
    )


@pytest.mark.parametrize("pattern", EXPENSIVE_READ_PATTERNS, ids=lambda p: p.pattern)
def test_every_expensive_pattern_still_matches_a_live_route(pattern):
    """A pattern that matches nothing is a demotion to cheap, spelled quietly."""
    matches = [path for path in _read_routes() if pattern.match(_concrete(path))]
    assert matches, (
        f"{pattern.pattern} matches no registered route — the endpoint it guards "
        "was renamed or removed, and requests to its new path are now metered as "
        "cheap reads"
    )


def test_the_mount_table_matches_main():
    """A router mounted in main but missing here is a blind spot in the probe.

    Parsed rather than imported: ``main`` builds the app at module scope, and the
    probe must not depend on that singleton to know what is mounted.
    """
    source = inspect.getsource(importlib.import_module("faultmaven.main"))
    tree = ast.parse(source)

    aliases = {}  # local router name -> module it came from
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = "faultmaven" + "." * 0 + node.module
            if node.level:  # relative import, e.g. ``from .api.routes.admin``
                module = "faultmaven." + node.module
            for alias in node.names:
                if alias.asname and alias.asname.endswith("_router"):
                    aliases[alias.asname] = module

    mounted = {}
    dynamic = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app"
        ):
            continue
        arg = node.args[0] if node.args else None
        if not isinstance(arg, ast.Name):
            dynamic.append(ast.dump(arg) if arg else "<none>")
            continue
        prefix = ""
        for keyword in node.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                prefix = keyword.value.value
        if arg.id in aliases:
            mounted[aliases[arg.id]] = prefix

    assert len(dynamic) <= 1, (
        "a router is mounted from an expression this test cannot resolve; if it "
        f"serves session traffic it is outside the probe: {dynamic}"
    )

    assert mounted == dict((m, p) for p, m in MOUNTS), (
        "MOUNTS has drifted from main.py — routers mounted there but missing "
        "here are never probed for embedder reachability.\n"
        f"  only in main: {sorted(set(mounted) - {m for _, m in MOUNTS})}\n"
        f"  only in MOUNTS: {sorted({m for _, m in MOUNTS} - set(mounted))}\n"
        f"  prefix disagreements: "
        f"{sorted(m for m, p in mounted.items() if dict((x, y) for y, x in MOUNTS).get(m) != p)}"
    )


def test_the_probe_sees_an_import_inside_a_handler_body():
    """The half of the probe no live route currently needs, proved on its own.

    Every route the import scan would flag is already flagged by its declared
    dependencies, so neutralising the scan changes no verdict and no other test
    in this module notices. A detector that cannot be observed to work is not a
    verified one — so it is exercised here directly, on the shape it exists for.
    """

    async def handler():
        from faultmaven.infrastructure.knowledge.runbook_kb import (  # noqa: F401
            RunbookKnowledgeBase,
        )

        return RunbookKnowledgeBase

    found = _handler_imports(handler)
    assert "faultmaven.infrastructure.knowledge.runbook_kb" in found
    assert any(marker in m.lower() for m in found for marker in _IMPORT_MARKERS), (
        "the scan saw the import but no marker matches it, so a handler that "
        "constructs a knowledge base it never declared would still read as cheap"
    )


def test_the_probe_reports_nothing_for_a_handler_that_imports_nothing():
    """The negative half: the scan must not flag every handler it can parse."""

    async def handler(case_id: str):
        return {"case_id": case_id}

    assert _handler_imports(handler) == set()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_verbs_are_never_cheap_reads(method):
    assert not is_cheap_read(method, "/api/v1/cases/x/ui")


@pytest.mark.parametrize("method", ["get", "Get", "HEAD", "options"])
def test_the_verb_check_is_case_insensitive(method):
    """ASGI hands the method up verbatim; a lowercase one must not be a write."""
    assert is_cheap_read(method, "/api/v1/cases/x/ui")
