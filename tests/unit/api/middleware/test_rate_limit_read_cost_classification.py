"""Every read endpoint is classified cheap or expensive on purpose (fm#994).

The rate limiter meters cheap reads in a roomy pair of per-session buckets so
that SPA navigation does not compete with POST turns. That is only safe while
"cheap" is true of the endpoints it covers: a GET that embeds a query and
searches the vector store costs what a write costs, and handing it the roomy
bucket hands it the roomiest per-session ceiling in the system.

The middleware states the exceptions as a small list of patterns, and a list is
exactly the kind of thing that rots — silently, and in the permissive direction,
because an endpoint added later inherits "cheap" by saying nothing. This module
closes both halves of that:

- **Nothing is unclassified.** The registered read routes are pinned against an
  explicit verdict here, so a new read endpoint fails this test until someone
  decides what it costs.
- **Nothing is classified at a path that no longer exists.** Each expensive
  pattern must still match a live route, so renaming an endpoint out from under
  a pattern is a failure rather than a quiet demotion to cheap.

The verdicts are asserted through the middleware's own ``is_cheap_read``, not
through a second reading of the patterns — a private copy of the rule could
agree with this docstring while disagreeing with what runs.
"""

import re

import pytest

from faultmaven.api.middleware.rate_limiting import (
    EXPENSIVE_READ_PATTERNS,
    READ_ONLY_METHODS,
    is_cheap_read,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


# Read routes that run an embedding and/or a vector similarity search per call.
# These are metered as writes.
EXPENSIVE_READ_ROUTES = {
    # Runbook similarity search over the knowledge base.
    "/api/v1/cases/{case_id}/report-recommendations",
    "/api/v1/reports/recommendations/{case_id}",
    # ``get_semantic_snippet`` — embeds the query to locate the chunk.
    "/api/v1/knowledge/documents/{document_id}/snippet",
}

# Read routes that resolve out of the database or the ORM cache. Cheap.
CHEAP_READ_ROUTES = {
    "/api/v1/cases",
    "/api/v1/cases/health",
    "/api/v1/cases/{case_id}",
    "/api/v1/cases/{case_id}/analytics",
    "/api/v1/cases/{case_id}/data",
    "/api/v1/cases/{case_id}/data/{data_id}",
    "/api/v1/cases/{case_id}/diff",
    "/api/v1/cases/{case_id}/evidence",
    "/api/v1/cases/{case_id}/evidence/{evidence_id}",
    "/api/v1/cases/{case_id}/messages",
    "/api/v1/cases/{case_id}/reports",
    "/api/v1/cases/{case_id}/reports/{report_id}/download",
    "/api/v1/cases/{case_id}/snapshot/{turn_number}",
    "/api/v1/cases/{case_id}/ui",
    "/api/v1/cases/{case_id}/uploaded-files",
    "/api/v1/cases/{case_id}/uploaded-files/{file_id}",
    "/api/v1/knowledge/analytics/search",
    "/api/v1/knowledge/conversions",
    "/api/v1/knowledge/conversions/by-case/{case_id}",
    "/api/v1/knowledge/conversions/{conversion_id}",
    "/api/v1/knowledge/documents",
    "/api/v1/knowledge/documents/{document_id}",
    "/api/v1/knowledge/drafts",
    "/api/v1/knowledge/stats",
    "/api/v1/knowledge/suggestions",
    "/api/v1/knowledge/suggestions/{suggestion_id}",
    "/api/v1/reports/case/{case_id}",
    "/api/v1/reports/{report_id}",
    "/api/v1/reports/{report_id}/versions",
}

CLASSIFIED_READ_ROUTES = CHEAP_READ_ROUTES | EXPENSIVE_READ_ROUTES

# The routers that own the endpoints an embedding can hide behind: cases,
# knowledge and reports. Enumerated from the routers rather than from the
# assembled app so this stays a unit test — and rather than from
# ``app.openapi()``, whose output is not deterministic.
_ROUTER_IMPORTS = (
    ("case", "faultmaven.modules.case.api.routes"),
    ("knowledge", "faultmaven.modules.knowledge.api.routes"),
    ("knowledge_conversion", "faultmaven.modules.knowledge.api.conversion_routes"),
    ("report", "faultmaven.modules.report.api.routes"),
)

_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _registered_read_routes():
    """Full paths of every read route the three routers register."""
    import importlib

    paths = set()
    for _, module_name in _ROUTER_IMPORTS:
        router = importlib.import_module(module_name).router
        for route in router.routes:
            methods = getattr(route, "methods", None) or set()
            if methods & READ_ONLY_METHODS:
                paths.add("/api/v1" + route.path)
    return paths


def _concrete(path: str) -> str:
    """Substitute path params, so a template can be run through the matcher."""
    return _PATH_PARAM.sub("x", path)


def test_every_registered_read_route_is_classified():
    """A read endpoint added later must be given a verdict, not inherit one."""
    registered = _registered_read_routes()

    unclassified = registered - CLASSIFIED_READ_ROUTES
    assert not unclassified, (
        "these read routes have no cost verdict — decide whether each runs an "
        "embedding or vector search (expensive, metered as a write) or resolves "
        "from the database (cheap), then add it to the matching set here and, if "
        "expensive, to EXPENSIVE_READ_PATTERNS in the middleware: "
        f"{sorted(unclassified)}"
    )

    departed = CLASSIFIED_READ_ROUTES - registered
    assert not departed, (
        "these routes are classified here but no longer registered; a stale "
        f"verdict hides the next rename: {sorted(departed)}"
    )


@pytest.mark.parametrize("path", sorted(EXPENSIVE_READ_ROUTES))
def test_expensive_reads_are_not_cheap(path):
    assert not is_cheap_read(
        "GET", _concrete(path)
    ), f"{path} runs an embedding but the middleware meters it as a cheap read"


@pytest.mark.parametrize("path", sorted(CHEAP_READ_ROUTES))
def test_cheap_reads_are_cheap(path):
    assert is_cheap_read("GET", _concrete(path)), (
        f"{path} is metered as a write, so ordinary navigation over it competes "
        "with POST turns for one quota — the fm#994 symptom"
    )


@pytest.mark.parametrize("pattern", EXPENSIVE_READ_PATTERNS, ids=lambda p: p.pattern)
def test_every_expensive_pattern_still_matches_a_live_route(pattern):
    """A pattern that matches nothing is a demotion to cheap, spelled quietly."""
    matches = [
        path for path in _registered_read_routes() if pattern.match(_concrete(path))
    ]
    assert matches, (
        f"{pattern.pattern} matches no registered route — the endpoint it "
        "guards was renamed or removed, and requests to its new path are now "
        "metered as cheap reads"
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_verbs_are_never_cheap_reads(method):
    assert not is_cheap_read(method, "/api/v1/cases/x/ui")


@pytest.mark.parametrize("method", ["get", "Get", "HEAD", "options"])
def test_the_verb_check_is_case_insensitive(method):
    """ASGI hands the method up verbatim; a lowercase one must not be a write."""
    assert is_cheap_read(method, "/api/v1/cases/x/ui")
