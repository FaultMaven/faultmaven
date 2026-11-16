"""``main.py`` finds the routes it declares a policy for through the flattener (#2178).

The turn, case-search and KB receipt lookups walked ``app.routes`` for a
handler. From FastAPI 0.137.0 that table holds one placeholder per included
router, so each found nothing and ``import faultmaven.main`` raised. They read
``route_enumeration.iter_served_endpoints`` now (``main._paths_served_by``,
whose flat-revert canary is in ``test_route_walk_sites_reach_included_routers``),
and the AST guard ``test_no_direct_route_table_read_2178.py`` keeps a direct
walk out.
"""

import pytest

from faultmaven import main
from faultmaven.api.middleware.route_policy import APP_STATE_POLICY_ATTR, RoutePolicy
from faultmaven.modules.case.api.routes.cases import search_cases
from faultmaven.modules.case.api.routes.conversation import submit_turn
from faultmaven.modules.knowledge.api.conversion_routes import convert_document
from faultmaven.modules.knowledge.api.routes import upload_document

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "found, handlers, expected",
    [
        ("_TURN_ROUTE_PATHS", {submit_turn}, ["/api/v1/cases/{case_id}/turns"]),
        ("_SEARCH_ROUTE_PATHS", {search_cases}, ["/api/v1/cases/search"]),
        (
            "_KB_RECEIPT_ROUTE_PATHS",
            {upload_document, convert_document},
            ["/api/v1/knowledge/documents", "/api/v1/knowledge/convert"],
        ),
    ],
)
def test_each_lookup_finds_its_served_path_and_declares_it(found, handlers, expected):
    assert getattr(main, found) == main._paths_served_by(main.app, *handlers)
    assert getattr(main, found) == expected
    policy = getattr(main.app.state, APP_STATE_POLICY_ATTR)
    for path in expected:
        assert policy[path] == RoutePolicy(never_replayed=True, never_collapsed=True)
