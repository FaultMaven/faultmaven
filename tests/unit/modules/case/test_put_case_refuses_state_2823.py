"""``PUT /cases/{case_id}`` changes the title or description only (#2823).

It used to accept ``state`` and write it raw: a value the case's validators
refuse (a terminal state, INVESTIGATING before Gate 1) answered 500, and one
they admit moved the case past the transitions (INVESTIGATING back to
INQUIRY). Now any body carrying ``state`` (or any field the request does not
declare) is refused with 422 before the case is read, and
``CaseService.update_case`` refuses any key but ``title`` and ``description``
before it reads or writes.
"""

from __future__ import annotations

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.exceptions import ValidationException
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from tests.case_builders import inquiry_case, investigating_case

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "body",
    [
        {"state": "closed"},
        {"title": "x", "state": "investigating"},
        {"state": "inquiry"},
    ],
    ids=["terminal", "with-a-title", "same-state"],
)
def test_the_real_app_refuses_state_and_changes_nothing(booted_app_client, body):
    from faultmaven.container import container

    client = booted_app_client
    login = client.post("/api/v1/auth/dev-login", json={"username": "admin"})
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    created = client.post("/api/v1/cases", json={"title": "2823"}, headers=auth)
    assert created.status_code == 201, created.text
    case_id = created.json()["case_id"]
    stored = container.get_case_service().repository.get

    def snapshot():
        case = client.portal.call(stored, case_id)
        return case.state, case.title, case.version

    before = snapshot()
    assert before[0] == CaseState.INQUIRY

    response = client.put(f"/api/v1/cases/{case_id}", json=body, headers=auth)

    assert response.status_code == 422, response.text
    [error] = response.json()["errors"]
    assert error["type"] == "extra_forbidden"
    assert error["loc"] == ["body", "state"]
    assert snapshot() == before


@pytest.fixture
async def stored_inquiry():
    yield await _stored(inquiry_case(user_id="u", enterprise_id="o", title="before"))
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


async def _stored(case):
    set_current_enterprise_id(case.enterprise_id)
    repository = InMemoryCaseRepository()
    await repository.save(case)
    return repository, case.case_id, (await repository.get(case.case_id)).version


@pytest.mark.parametrize(
    "updates",
    [
        {"state": "investigating"},
        {"title": "x", "state": "investigating"},
        {"title": "x", "priority": "high"},
    ],
    ids=["before-gate-1", "with-a-title", "unknown-key"],
)
async def test_the_service_refuses_any_key_but_title_and_description(
    stored_inquiry, updates
):
    repository, case_id, version = stored_inquiry

    with pytest.raises(ValidationException, match="only title and description"):
        await CaseService(repository).update_case(case_id, updates)

    saved = await repository.get(case_id)
    assert (saved.state, saved.title, saved.version) == (
        CaseState.INQUIRY,
        "before",
        version,
    )


async def test_the_service_refuses_moving_an_investigation_back_to_inquiry():
    """The raw write the case's validators admit (on ``main`` it succeeded,
    past ``LEGAL_TRANSITIONS``)."""
    case = investigating_case(user_id="u", enterprise_id="o", symptom="slow")
    repository, case_id, version = await _stored(case)
    try:
        with pytest.raises(ValidationException, match="state"):
            await CaseService(repository).update_case(case_id, {"state": "inquiry"})
        saved = await repository.get(case_id)
        assert (saved.state, saved.version) == (CaseState.INVESTIGATING, version)
    finally:
        set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
