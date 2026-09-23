"""``faultmaven_operator_case_reads_total`` — the evidence fm#1613 waits on.

faultmaven-dashboard#178 stopped the only client calling the operator case
endpoints in a standalone deployment, which leaves their standalone arm serving
nobody. Removing it is fm#1613, and
``docs/development/api-contract-changes.md`` is explicit about what that needs:

    Count arrivals of the old form — a Prometheus counter on requests in the
    legacy shape — and contract when it reads zero across a full deploy cycle of
    every client. "Nobody should still be using it" is not evidence.

A grep over client source is the disallowed kind of argument, because deployed
clients are what matter: self-hosted installs pin ``FM_IMAGE_TAG``, so Dashboard
images predating #178 are in the field and still call these routes in standalone.

So this suite asserts the counter measures the right POPULATION. Two properties
carry the whole thing:

* every read that is **served** is counted, on all three surfaces; and
* nothing **refused** is counted — otherwise a standalone reading of "not zero"
  could be entirely refusals, and the removal would be blocked by its own gate.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.operator_audit import get_operator_audit_repository
from faultmaven.api.operator_grants import get_operator_grant_repository
from faultmaven.api.routes import admin_cases
from faultmaven.api.routes.admin_cases import (
    OPERATOR_READ_DEPLOYMENTS,
    OPERATOR_READ_SURFACES,
    get_case_service,
    router,
)
from faultmaven.models.api_models import CaseMessagesResponse
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.case.domain.models import Case, CaseState

CASE_ID = "case_a1b2c3d4e5f6"

LIST_PATH = "/api/v1/admin/cases"
DETAIL_PATH = f"/api/v1/admin/cases/{CASE_ID}"
TRANSCRIPT_PATH = f"/api/v1/admin/cases/{CASE_ID}/messages"


def _operator() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="op-1",
        enterprise_id="org-operator-own",
        email="operator@example.com",
        roles=["user", "admin", "platform_admin"],
        permissions=[],
    )


def _case() -> Case:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Case(
        case_id=CASE_ID,
        user_id="someone-else",
        # Required on every case row (ADR-017 D1): the isolation tenant.
        enterprise_id="ent_1",
        title="A case",
        description="d",
        state=CaseState.INQUIRY,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def counter(monkeypatch):
    """Stand in for the module-level counter.

    The shim returns a no-op metric unless Prometheus is importable AND
    ENABLE_METRICS is on, so the real object exposes nothing to read in a unit
    test. Patching it keeps the assertions about the CALL — which labels, how
    many times — which is what the population question actually is.
    """
    fake = MagicMock()
    monkeypatch.setattr(admin_cases, "operator_case_reads_total", fake)
    return fake


def _labels(counter) -> list[dict]:
    """The label sets the route asked for, in order."""
    return [call.kwargs for call in counter.labels.call_args_list]


@pytest.fixture
def case_service():
    service = AsyncMock()
    service.list_all_cases = AsyncMock(return_value=([], 0))
    service.get_case = AsyncMock(return_value=_case())
    service.get_case_team_ids = AsyncMock(return_value=[])
    service.get_case_messages_enhanced = AsyncMock(
        return_value=CaseMessagesResponse(
            messages=[], total_count=0, retrieved_count=0, has_more=False
        )
    )
    return service


@pytest.fixture
def audit_repo():
    repo = AsyncMock()
    repo.record_access = AsyncMock(return_value=True)
    return repo


@pytest.fixture
def grant_repo():
    return AsyncMock()


@pytest.fixture
def client(audit_repo, grant_repo, case_service):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_platform_admin] = _operator
    app.dependency_overrides[get_operator_audit_repository] = lambda: audit_repo
    app.dependency_overrides[get_operator_grant_repository] = lambda: grant_repo
    app.dependency_overrides[get_case_service] = lambda: case_service
    return TestClient(app)


@pytest.fixture
def cloud(monkeypatch):
    """Cloud posture for BOTH readers of deployment mode.

    `list_all_cases` reads `admin_cases.get_settings` for the metadata/full split;
    `resolved_deployment_mode` and the grant gate read `operator_grants`'. Patching
    one and not the other produces a half-cloud deployment no real install can be
    in, and the label would then disagree with the arm actually served.
    """
    from faultmaven.api import operator_grants

    real = operator_grants.get_settings()

    class _CloudSettings:
        is_cloud = True
        deployment_mode = "cloud"

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(operator_grants, "get_settings", _CloudSettings)
    monkeypatch.setattr(admin_cases, "get_settings", _CloudSettings)


@pytest.mark.unit
class TestEveryServedReadIsCounted:
    """All three surfaces, or the standalone total is an undercount."""

    def test_the_list(self, client, counter):
        assert client.get(LIST_PATH).status_code == 200
        assert _labels(counter) == [{"surface": "list", "deployment": "standalone"}]

    def test_the_case_detail(self, client, counter):
        assert client.get(DETAIL_PATH).status_code == 200
        assert _labels(counter) == [
            {"surface": "case_detail", "deployment": "standalone"}
        ]

    def test_the_transcript(self, client, counter):
        assert client.get(TRANSCRIPT_PATH).status_code == 200
        assert _labels(counter) == [
            {"surface": "transcript", "deployment": "standalone"}
        ]

    def test_it_increments_once_per_read(self, client, counter):
        client.get(LIST_PATH)
        client.get(LIST_PATH)
        assert counter.labels.return_value.inc.call_count == 2


@pytest.mark.unit
class TestCloudIsTheDenominator:
    """Without cloud rows, zero is indistinguishable from an unwired counter.

    This is the property that makes the standalone reading trustworthy: a counter
    that never fires looks exactly like an arm nobody uses, and fm#1613 would then
    be decided on the strength of a bug.
    """

    def test_cloud_reads_are_labeled_cloud(self, client, counter, cloud):
        assert client.get(LIST_PATH).status_code == 200
        assert _labels(counter) == [{"surface": "list", "deployment": "cloud"}]
        # The other half of the closed vocabulary below: this is where the
        # `cloud` member is shown to be emitted by a real route rather than only
        # declared in the tuple.
        assert _labels(counter)[0]["deployment"] in OPERATOR_READ_DEPLOYMENTS


@pytest.mark.unit
class TestNothingRefusedIsCounted:
    """A refusal is not an arrival of the old form.

    If refusals counted, a standalone deployment could read "not zero" while
    serving nobody — and the removal would be blocked by the very gate that proves
    it is safe.
    """

    def test_the_multi_tenant_refusal(self, client, counter, monkeypatch):
        from faultmaven.providers.tenancy.factory import BUILTIN_MULTI

        monkeypatch.setattr(
            admin_cases, "requested_tenant_provider", lambda: BUILTIN_MULTI
        )

        assert client.get(LIST_PATH).status_code == 403
        counter.labels.assert_not_called()

    @pytest.mark.parametrize("path", [DETAIL_PATH, TRANSCRIPT_PATH])
    def test_a_cloud_content_read_with_no_grant(
        self, client, counter, cloud, grant_repo, path
    ):
        grant_repo.find_live_grant = AsyncMock(return_value=None)

        assert client.get(path).status_code == 403
        counter.labels.assert_not_called()


@pytest.mark.unit
class TestTheLabelVocabularyIsClosed:
    """A surface spelled at a call site but absent from the tuple mints a new
    series silently, and the standalone total is then computed over a population
    that quietly changed shape."""

    def test_every_label_the_routes_emit_is_declared(self, client, counter):
        # Standalone posture: it reaches all three surfaces without a grant, and
        # the `cloud` member is covered by the denominator test above. Running
        # this sweep under `cloud` would need a live grant per content path,
        # which is a different test's subject.
        for path in (LIST_PATH, DETAIL_PATH, TRANSCRIPT_PATH):
            client.get(path)

        emitted = _labels(counter)
        assert emitted, "no labels emitted — the sweep would pass vacuously"
        for labels in emitted:
            assert labels["surface"] in OPERATOR_READ_SURFACES
            assert labels["deployment"] in OPERATOR_READ_DEPLOYMENTS

    def test_all_three_declared_surfaces_are_reachable(self, client, counter):
        """The tuple does not carry a member no route can emit."""
        for path in (LIST_PATH, DETAIL_PATH, TRANSCRIPT_PATH):
            client.get(path)

        assert {labels["surface"] for labels in _labels(counter)} == set(
            OPERATOR_READ_SURFACES
        )


@pytest.mark.unit
class TestTheAuditRowStillCarriesTheSurface:
    """`surface` moved from the callers' `details` into a parameter so the trail
    and the metric cannot spell it differently. The trail must not have lost it."""

    @pytest.mark.parametrize(
        "path,expected",
        [(DETAIL_PATH, "case_detail"), (TRANSCRIPT_PATH, "transcript")],
    )
    def test_it_is_recorded(self, client, counter, audit_repo, path, expected):
        client.get(path)

        details = audit_repo.record_access.await_args.kwargs["details"]
        assert details["surface"] == expected

    def test_the_transcript_keeps_its_other_details(self, client, counter, audit_repo):
        client.get(f"{TRANSCRIPT_PATH}?limit=25&offset=50")

        details = audit_repo.record_access.await_args.kwargs["details"]
        assert details["limit"] == 25
        assert details["offset"] == 50
