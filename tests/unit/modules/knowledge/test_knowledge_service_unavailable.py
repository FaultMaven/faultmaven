"""A process composed without a knowledge service says so (#899).

``get_knowledge_service`` reads ``app.state.knowledge_service``, which can now
be ``None``: the container stopped substituting an in-memory stub that answered
document reads with invented content. The dependency names the condition
instead of letting each route fail on its own — a 503 an operator can act on,
rather than a 500 from ``'NoneType' object has no attribute ...`` that reads
like a bug in whichever route happened to be called first.

Deliberately NOT overriding the dependency: every other knowledge route test
substitutes a service through ``dependency_overrides``, which is exactly the
seam under test here.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.v1.auth_dependencies import get_current_user_optional


def _client_without_knowledge_service():
    from faultmaven.modules.knowledge.api.routes import router

    app = FastAPI()
    app.include_router(router)
    app.state.knowledge_service = None
    app.dependency_overrides[get_current_user_optional] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.unit
@pytest.mark.knowledge_base
def test_document_listing_answers_503_not_500():
    response = _client_without_knowledge_service().get("/knowledge/documents")

    assert response.status_code == 503
    assert response.json()["detail"] == "Knowledge base unavailable"


@pytest.mark.unit
@pytest.mark.knowledge_base
def test_the_slot_being_absent_reads_the_same_as_being_none():
    """``app.state`` with no attribute at all, not just one set to None.

    ``DIContainer.reset()`` ``delattr``s the service rather than nulling it, so
    a reset container and an uncomposed one leave app.state in two different
    shapes. Both must answer the same way.
    """
    from faultmaven.modules.knowledge.api.routes import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_optional] = lambda: None

    response = TestClient(app, raise_server_exceptions=False).get(
        "/knowledge/documents"
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Knowledge base unavailable"
