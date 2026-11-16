"""``main.py`` finds the routes it declares a policy for through the flattener (#2178).

The turn, case-search and KB receipt lookups walked ``app.routes`` for a
handler. From FastAPI 0.137.0 that table holds one placeholder per included
router, so each found nothing and ``import faultmaven.main`` raised. They read
``route_enumeration.iter_served_endpoints`` now (``main._served_path_of``,
whose flat-revert canary is in ``test_route_walk_sites_reach_included_routers``),
and the AST guard ``test_no_direct_route_table_read_2178.py`` keeps a direct
walk out.
"""

import pytest
from fastapi import FastAPI

from faultmaven import main
from faultmaven.api.middleware.route_policy import APP_STATE_POLICY_ATTR, RoutePolicy

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "found, expected",
    [
        ("_TURN_ROUTE_PATHS", ["/api/v1/cases/{case_id}/turns"]),
        ("_SEARCH_ROUTE_PATHS", ["/api/v1/cases/search"]),
        (
            "_KB_RECEIPT_ROUTE_PATHS",
            ["/api/v1/knowledge/documents", "/api/v1/knowledge/convert"],
        ),
    ],
)
def test_each_lookup_finds_its_served_path_and_declares_it(found, expected):
    assert getattr(main, found) == expected
    policy = getattr(main.app.state, APP_STATE_POLICY_ATTR)
    for path in expected:
        assert policy[path] == RoutePolicy(never_replayed=True, never_collapsed=True)


def _served():  # pragma: no cover - never dispatched
    return {}


def _unserved():  # pragma: no cover - never dispatched
    return {}


def test_each_handler_must_be_served_exactly_once():
    """Per handler, not a combined count: one served twice and another not at
    all would add up to the two the KB lookup used to expect."""
    app = FastAPI()
    app.add_api_route("/a", _served, methods=["POST"])
    app.add_api_route("/b", _served, methods=["POST"])

    with pytest.raises(RuntimeError, match="found \\['/a', '/b'\\]"):
        main._served_path_of(app, _served)
    with pytest.raises(RuntimeError, match="found \\[\\]"):
        main._served_path_of(app, _unserved)
