"""A lost version race on ``PUT /cases/{case_id}`` or the close route answers
409 ``CASE_VERSION_CONFLICT``, nothing written; ``PUT`` writes the case as
it stands when written (#2825).

On ``main`` the PUT's race answered 500 ``UPDATE_CASE_ERROR`` on both write
paths (the metadata write's ``ConflictError`` and the statement edit's
``StaleCaseException`` fell to the route's broad arm), and the close route's
409 went out unlabelled. The write path was chosen on the access check's
read, so a case Gate 1 opened meanwhile got its description written raw past
the problem statement's three stores, and one closed meanwhile was written
and answered 200.

Every test runs on the real app and store. The races are real: before each
attempt a real write bumps the stored version, so the repository's own
version guard refuses the attempt.
"""

from __future__ import annotations

import uuid

import pytest

from faultmaven.exceptions import CASE_TERMINAL, CASE_VERSION_CONFLICT
from faultmaven.modules.case.contracts import (
    CaseState,
    CausalNode,
    NodeType,
    ProblemVerification,
    StatementRecordKind,
)

pytestmark = pytest.mark.unit

STATEMENT = "checkout p99 latency doubled since the 14:00 deploy"
EDITED = "checkout p99 latency doubled since the 14:05 deploy"


@pytest.fixture
def api(booted_app_client):
    """The client, an authorization header and the app's case service."""
    from faultmaven.main import app

    client = booted_app_client
    login = client.post("/api/v1/auth/dev-login", json={"username": "admin"})
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    return client, auth, app.state.case_service


def _create(api) -> str:
    client, auth, _ = api
    created = client.post("/api/v1/cases", json={"title": "2825"}, headers=auth)
    assert created.status_code == 201, created.text
    return created.json()["case_id"]


def _load(api, case_id):
    client, _, service = api
    return client.portal.call(service.repository.get, case_id)


async def _open_investigation(repository, save, case_id: str) -> None:
    """Gate 1, as the engine's transition writes it: the confirmed statement
    in the description, the verification record and the PROBLEM node."""
    case = await repository.get(case_id)
    case.inquiry.proposed_problem_statement = STATEMENT
    case.inquiry.problem_statement_confirmed = True
    case.description = STATEMENT
    case.state = CaseState.INVESTIGATING
    case.problem_verification = ProblemVerification(symptom_statement=STATEMENT)
    problem = CausalNode(
        node_id=f"cn_{uuid.uuid4().hex[:12]}",
        statement=STATEMENT,
        node_type=NodeType.PROBLEM,
        generated_at_turn=1,
    )
    case.causal_nodes = {problem.node_id: problem}
    await save(case)


async def _bump(repository, save, case_id: str) -> None:
    """A concurrent writer: a real save of the stored case, version + 1."""
    await save(await repository.get(case_id))


def _losing(api, monkeypatch, name: str) -> None:
    """Every call of the repository's ``name`` loses its version race: a real
    write lands first, and the real guard refuses the stale attempt. The
    metadata write's title-only call carries no version and is not raced."""
    _, _, service = api
    repository = service.repository
    real, save = getattr(repository, name), repository.save

    async def racing(*args, **kwargs):
        case_id = args[0] if isinstance(args[0], str) else args[0].case_id
        if name == "save" or kwargs.get("expected_version") is not None:
            await _bump(repository, save, case_id)
        return await real(*args, **kwargs)

    monkeypatch.setattr(repository, name, racing)


def _assert_version_conflict(response, case_id: str) -> None:
    assert response.status_code == 409, response.text
    assert response.headers["x-error-code"] == CASE_VERSION_CONFLICT
    body = response.json()
    assert body["conflict_reason"] == "concurrent_update"
    assert body["resource_id"] == case_id
    assert "x-expected-version" not in response.headers


def test_a_lost_metadata_race_answers_409_and_writes_nothing(api, monkeypatch):
    """Channel 1: a description edit outside an investigation (500 on main)."""
    client, auth, _ = api
    case_id = _create(api)
    before = _load(api, case_id)
    _losing(api, monkeypatch, "update_metadata_fields")

    response = client.put(
        f"/api/v1/cases/{case_id}",
        json={"title": "renamed", "description": EDITED},
        headers=auth,
    )

    _assert_version_conflict(response, case_id)
    after = _load(api, case_id)
    assert (after.title, after.description) == (before.title, before.description)


def test_a_lost_statement_edit_race_answers_409_and_writes_nothing(api, monkeypatch):
    """Channel 2: the versioned statement edit during an investigation, with
    an edit ``edit_statement_refusal`` accepts (500 on main)."""
    client, auth, service = api
    case_id = _create(api)
    repository = service.repository
    client.portal.call(_open_investigation, repository, repository.save, case_id)
    _losing(api, monkeypatch, "save")

    response = client.put(
        f"/api/v1/cases/{case_id}", json={"description": EDITED}, headers=auth
    )

    _assert_version_conflict(response, case_id)
    after = _load(api, case_id)
    assert after.description == after.problem_verification.symptom_statement
    assert after.description == STATEMENT


def test_a_lost_close_race_answers_409_case_version_conflict(api, monkeypatch):
    """The close route's identical race, labelled (unlabelled on main)."""
    client, auth, _ = api
    case_id = _create(api)
    _losing(api, monkeypatch, "save")

    response = client.post(f"/api/v1/cases/{case_id}/close", headers=auth)

    _assert_version_conflict(response, case_id)
    assert _load(api, case_id).state == CaseState.INQUIRY


def _after_the_access_read(api, monkeypatch, move) -> None:
    """Run ``move`` once, right after the service's own access read returns
    (the second: the route's gate is the first). Both have passed on the
    case as it was; only the write sees it moved."""
    _, _, service = api
    real = service.require_case
    reads = []

    async def require_case(case_id, *args, **kwargs):
        case = await real(case_id, *args, **kwargs)
        reads.append(case_id)
        if len(reads) == 2:
            await move(case_id, case.user_id)
        return case

    monkeypatch.setattr(service, "require_case", require_case)


def test_a_case_gate_1_opens_meanwhile_gets_the_statement_edit(api, monkeypatch):
    """On main the description was written raw: the verification record and
    the PROBLEM node kept the old statement."""
    client, auth, service = api
    case_id = _create(api)
    repository = service.repository
    save = repository.save

    async def gate_1(case_id, _user_id):
        await _open_investigation(repository, save, case_id)

    _after_the_access_read(api, monkeypatch, gate_1)

    response = client.put(
        f"/api/v1/cases/{case_id}", json={"description": EDITED}, headers=auth
    )

    assert response.status_code == 200, response.text
    after = _load(api, case_id)
    [problem] = after.causal_nodes.values()
    assert after.description == EDITED
    assert after.problem_verification.symptom_statement == EDITED
    assert problem.statement == EDITED
    assert after.problem_verification.statement_history[-1].kind == (
        StatementRecordKind.EDITED
    )


def test_a_case_closed_meanwhile_is_refused_case_terminal(api, monkeypatch):
    """On main the edit was written to the closed case and answered 200."""
    client, auth, service = api
    case_id = _create(api)

    async def close(case_id, user_id):
        await service.close_case(case_id, user_id)

    _after_the_access_read(api, monkeypatch, close)

    response = client.put(
        f"/api/v1/cases/{case_id}",
        json={"title": "renamed", "description": EDITED},
        headers=auth,
    )

    assert response.status_code == 409, response.text
    assert response.headers["x-error-code"] == CASE_TERMINAL
    assert response.json()["conflict_reason"] == "already_closed"
    after = _load(api, case_id)
    assert after.state == CaseState.CLOSED
    assert after.title != "renamed"
    assert after.description != EDITED


def test_unchanged_a_title_edit_and_the_terminal_gate(api):
    """Pinned: a title-only PUT is written, and a closed case is refused at
    the route's gate with ``CASE_TERMINAL``."""
    client, auth, _ = api
    case_id = _create(api)
    path = f"/api/v1/cases/{case_id}"

    renamed = client.put(path, json={"title": "renamed"}, headers=auth)
    assert renamed.status_code == 200, renamed.text
    assert _load(api, case_id).title == "renamed"

    closed = client.post(f"{path}/close", headers=auth)
    assert closed.status_code == 200, closed.text
    refused = client.put(path, json={"title": "again"}, headers=auth)
    assert refused.status_code == 409, refused.text
    assert refused.headers["x-error-code"] == CASE_TERMINAL
    assert _load(api, case_id).title == "renamed"
