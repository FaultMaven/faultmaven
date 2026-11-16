"""``main.py`` finds the routes it declares a policy for through the flattener (#2178).

The three lookups (the turn route, case search, and the KB upload and
conversion receipts) used to walk ``app.routes`` for a handler. From FastAPI
0.137.0 that table holds one ``_IncludedRouter`` placeholder per included
router, so each walk found nothing and ``import faultmaven.main`` raised. They
now read ``route_enumeration.iter_served_endpoints``, which flattens the table
on every FastAPI this project runs on; ``tests/unit/architecture/
test_no_direct_route_table_read_2178.py`` keeps a direct walk from returning.
"""

import pytest

from faultmaven.api.middleware.route_policy import APP_STATE_POLICY_ATTR, RoutePolicy
from faultmaven.api.route_enumeration import iter_served_endpoints

pytestmark = pytest.mark.unit

NEVER_REPLAYED = RoutePolicy(never_replayed=True, never_collapsed=True)


@pytest.mark.parametrize(
    "found, handlers, expected",
    [
        (
            "_TURN_ROUTE_PATHS",
            ("faultmaven.modules.case.api.routes.conversation:submit_turn",),
            ["/api/v1/cases/{case_id}/turns"],
        ),
        (
            "_SEARCH_ROUTE_PATHS",
            ("faultmaven.modules.case.api.routes.cases:search_cases",),
            ["/api/v1/cases/search"],
        ),
        (
            "_KB_RECEIPT_ROUTE_PATHS",
            (
                "faultmaven.modules.knowledge.api.routes:upload_document",
                "faultmaven.modules.knowledge.api.conversion_routes:convert_document",
            ),
            ["/api/v1/knowledge/documents", "/api/v1/knowledge/convert"],
        ),
    ],
)
def test_each_lookup_finds_its_served_path_and_declares_it(found, handlers, expected):
    import importlib

    from faultmaven import main

    endpoints = []
    for spec in handlers:
        module, name = spec.split(":")
        endpoints.append(getattr(importlib.import_module(module), name))
    served = [
        e.path for e in iter_served_endpoints(main.app) if e.endpoint in endpoints
    ]

    assert getattr(main, found) == served == expected
    policy = getattr(main.app.state, APP_STATE_POLICY_ATTR)
    for path in expected:
        assert policy[path] == NEVER_REPLAYED, path
