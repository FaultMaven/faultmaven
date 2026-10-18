"""`delete_report` answers no status whose body is an exception's text (#1859).

The route used to carry ``except ServiceException as e: raise
HTTPException(status_code=403, detail=str(e))``. Nothing its ``try`` calls
raises ``ServiceException`` (the repository raises its own
``RepositoryException``; the case gate raises ``HTTPException``), so the arm was
dead, and had it fired it would have answered 403 with a wrapper's text: SQL,
``host:port``. The arm is deleted; the ``except Exception`` arm after it logs
the error and answers a static 500.

Driven through FastAPI, with the exception handlers registered as
``faultmaven/main.py`` registers them, so the assertion is on the body a client
receives rather than on the ``HTTPException`` the route raises.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
)
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_case_repository, get_case_service
from faultmaven.exceptions import ServiceException
from faultmaven.modules.report.api import routes

REPORT_ID = "report_1234"
INTERNAL_TEXT = "SELECT * FROM reports WHERE id='x' — db.internal:5432"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.include_router(routes.router)

    user = MagicMock()
    user.user_id = "user_owner"
    repository = MagicMock()
    repository.get_report = AsyncMock(side_effect=ServiceException(INTERNAL_TEXT))
    repository.delete_report = AsyncMock(return_value=True)

    app.dependency_overrides[require_authentication] = lambda: user
    app.dependency_overrides[get_case_repository] = lambda: repository
    app.dependency_overrides[get_case_service] = lambda: MagicMock()
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.security
def test_a_service_exception_inside_delete_is_a_static_500(client):
    resp = client.delete(f"/reports/{REPORT_ID}")

    assert resp.status_code == 500
    assert resp.json() == {"detail": "Failed to delete report"}
    assert "db.internal" not in resp.text
    assert "SELECT" not in resp.text
