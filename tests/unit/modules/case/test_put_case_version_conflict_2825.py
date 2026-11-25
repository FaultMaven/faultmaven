"""A lost version race on ``PUT /cases/{case_id}`` or the close route answers
409 ``CASE_VERSION_CONFLICT``, nothing written; ``PUT`` decides each fresh
load as the case then stands: its driver, and for a description its state
(#2825).

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
from datetime import datetime, timezone

import pytest

from faultmaven.config.tenant_context import (
    get_current_enterprise_id,
    set_current_enterprise_id,
)
from faultmaven.exceptions import (
    CASE_NOT_DRIVER,
    CASE_TERMINAL,
    CASE_VERSION_CONFLICT,
    ConflictError,
)
from faultmaven.modules.case.contracts import (
    CaseState,
    CausalNode,
    NodeType,
    ProblemVerification,
    StatementRecordKind,
)
from faultmaven.modules.case.domain.models.evidence import is_default_case_title
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from tests.case_builders import make_case

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
    _assert_statement(_load(api, case_id), EDITED)


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


def _assert_statement(case, text: str) -> None:
    [problem] = case.causal_nodes.values()
    assert case.description == text
    assert case.problem_verification.symptom_statement == text
    assert problem.statement == text
    assert case.problem_verification.statement_history[-1].kind == (
        StatementRecordKind.EDITED
    )


def test_gate_1_committing_inside_the_scoped_write_hands_off_on_the_retry(
    api, monkeypatch
):
    """The scoped write loses its ``expected_version`` race to Gate 1, which
    commits between the load and the UPDATE; the retry's fresh load finds an
    investigation and the edit becomes the statement edit."""
    client, auth, service = api
    case_id = _create(api)
    repository = service.repository
    real, save = repository.update_metadata_fields, repository.save
    raced = []

    async def gate_1_first(case_id, **kwargs):
        if kwargs.get("expected_version") is not None and not raced:
            raced.append(case_id)
            await _open_investigation(repository, save, case_id)
        return await real(case_id, **kwargs)

    monkeypatch.setattr(repository, "update_metadata_fields", gate_1_first)

    response = client.put(
        f"/api/v1/cases/{case_id}", json={"description": EDITED}, headers=auth
    )

    assert response.status_code == 200, response.text
    assert raced == [case_id]
    _assert_statement(_load(api, case_id), EDITED)


@pytest.mark.parametrize(
    ("investigating", "lands_at_load"),
    [(False, 1), (True, 2)],
    ids=["metadata", "statement"],
)
def test_a_hand_off_after_the_access_read_refuses_the_write(
    api, monkeypatch, investigating, lands_at_load
):
    """ADR-020 D4 on both write paths: from the write's ``lands_at_load``-th
    load on, the case is driven by someone else; 403 ``CASE_NOT_DRIVER``,
    nothing written. On the metadata path the hand-off meets
    ``_write_metadata``'s own load (it answered 200 and wrote the edit); on
    the statement path ``_write_metadata``'s load still shows the caller
    driving and hands off, so the retry mutator's fresh load is what refuses
    it."""
    client, auth, service = api
    case_id = _create(api)
    repository = service.repository
    if investigating:
        client.portal.call(_open_investigation, repository, repository.save, case_id)
    before = _load(api, case_id)
    real_get = repository.get
    handed_off, loads = [], []

    async def get(case_id):
        case = await real_get(case_id)
        if handed_off:
            loads.append(case_id)
            if len(loads) >= lands_at_load and case is not None:
                case.driver_id = "someone-else"
        return case

    async def hand_off(case_id, _user_id):
        handed_off.append(case_id)

    monkeypatch.setattr(repository, "get", get)
    _after_the_access_read(api, monkeypatch, hand_off)

    response = client.put(
        f"/api/v1/cases/{case_id}",
        json={"title": "renamed", "description": EDITED},
        headers=auth,
    )

    assert response.status_code == 403, response.text
    assert response.headers["x-error-code"] == CASE_NOT_DRIVER
    assert len(loads) == lands_at_load
    after = client.portal.call(real_get, case_id)
    assert (after.title, after.description, after.version) == (
        before.title,
        before.description,
        before.version,
    )


async def test_a_title_alone_names_a_resolved_placeholder_case():
    """The title route names a resolved case still holding its placeholder
    (``cases.py``: "Naming it is the one write still owed"); the service
    writes that title, a description it would refuse."""
    case = make_case(
        user_id="u",
        title="Case-260101-7",
        state=CaseState.RESOLVED,
        description="Checkout API returned 502 after the v2.1.3 deploy",
        created_at=datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
        resolved_at=datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
        closed_at=datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
    )
    previous = get_current_enterprise_id()
    set_current_enterprise_id(case.enterprise_id)
    try:
        repository = InMemoryCaseRepository()
        await repository.save(case)
        service = CaseService(repository)

        assert await service.update_case(case.case_id, {"title": "Checkout 502s"}, "u")
        assert (await repository.get(case.case_id)).title == "Checkout 502s"

        with pytest.raises(ConflictError) as refused:
            await service.update_case(case.case_id, {"description": EDITED}, "u")
        assert refused.value.error_code == CASE_TERMINAL
        assert (await repository.get(case.case_id)).description != EDITED
    finally:
        set_current_enterprise_id(previous)


async def _make_titleable(repository, save, case_id: str, resolved: bool) -> None:
    """A placeholder case with a problem statement (the title gate's
    substance), resolved here or left for the close route."""
    case = await repository.get(case_id)
    case.inquiry.proposed_problem_statement = STATEMENT
    case.description = STATEMENT
    if resolved:
        now = datetime.now(timezone.utc)
        fields = {"state": CaseState.RESOLVED, "resolved_at": now, "closed_at": now}
        case = type(case).model_validate({**dict(case), **fields})
    await save(case)


@pytest.mark.parametrize("terminal", ["resolved", "closed"])
def test_the_title_route_names_a_terminal_placeholder_case(api, terminal):
    """``POST /cases/{case_id}/title`` on the real app and store: a resolved or
    closed case still holding its placeholder is named, its version kept (a
    title alone carries no version)."""
    client, auth, service = api
    created = client.post("/api/v1/cases", json={}, headers=auth)
    assert created.status_code == 201, created.text
    case_id = created.json()["case_id"]
    repository = service.repository
    client.portal.call(
        _make_titleable, repository, repository.save, case_id, terminal == "resolved"
    )
    if terminal == "closed":
        closed = client.post(f"/api/v1/cases/{case_id}/close", headers=auth)
        assert closed.status_code == 200, closed.text
    before = _load(api, case_id)
    assert before.state.is_terminal
    assert is_default_case_title(before.title), before.title

    response = client.post(f"/api/v1/cases/{case_id}/title", headers=auth)

    assert response.status_code == 200, response.text
    after = _load(api, case_id)
    assert after.title == response.json()["title"]
    assert not is_default_case_title(after.title)
    assert (after.state, after.version) == (before.state, before.version)
