"""Break-glass content access — route behaviour (ADR-012 D9, #815).

The properties under test are governance properties, not plumbing:

1. Cloud content is unreachable without a **live** grant — and "live" means all
   three of approved, unrevoked and unexpired, swept independently.
2. Standalone content is served under standing access, recorded but not gated.
3. The access is recorded BEFORE any content is served, and a failed record
   refuses the request rather than serving it unaudited.
4. The audit row names the grant that authorised it, so an access can always be
   traced back to a justification.
5. The RLS scope is rebound to the granted organization — never bypassed — and
   only where rebinding is meaningful.
6. An over-long case id is rejected, never truncated into an immutable row
   naming a different case.

Both content surfaces (case detail and transcript) are swept through the same
cases: they share one gate, and a test that only covered one would let the other
drift.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.routes.admin_cases import (
    get_case_service,
    get_operator_audit_repository,
    get_operator_grant_repository,
    router,
)
from faultmaven.models.api import CaseMessagesResponse
from faultmaven.models.interfaces_operator_audit import OperatorAction
from faultmaven.models.interfaces_operator_grant import (
    GrantApprovalState,
    OperatorAccessGrant,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.case.domain.models import Case, CaseState

# A realistic case id: the domain model pins these at exactly 17 characters, so
# a toy "case-1" would fail validation before reaching the gate under test.
CASE_ID = "case_a1b2c3d4e5f6"
GRANT_ORG = "org-tenant-under-investigation"

# The two surfaces that share the break-glass gate. Every gate property is swept
# across both — the guarantee is "operator content requires a grant", not
# "the case-detail endpoint requires a grant".
CONTENT_PATHS = (
    f"/api/v1/admin/cases/{CASE_ID}",
    f"/api/v1/admin/cases/{CASE_ID}/messages",
)


def _operator() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="op-1",
        enterprise_id="org-operator-own",
        email="operator@example.com",
        roles=["user", "admin", "platform_admin"],
        permissions=[],
    )


def _grant(**overrides) -> OperatorAccessGrant:
    now = datetime.now(timezone.utc)
    defaults = dict(
        grant_id="grant-1",
        operator_user_id="op-1",
        target_case_id=CASE_ID,
        target_enterprise_id=GRANT_ORG,
        reason="customer reports the investigation is stuck; ticket SUP-4821",
        created_at=now,
        expires_at=now + timedelta(minutes=60),
        approval_state=GrantApprovalState.AUTO_APPROVED,
    )
    defaults.update(overrides)
    return OperatorAccessGrant(**defaults)


def _case() -> Case:
    now = datetime.now(timezone.utc)
    return Case(
        case_id=CASE_ID,
        user_id="tenant-user-9",
        enterprise_id=GRANT_ORG,
        title="payments API 5xx spike",
        description="a description a cloud operator must not see without a grant",
        # INQUIRY, not INVESTIGATING: the latter's cross-field validator demands
        # a confirmed problem statement, which is irrelevant to the gate under
        # test and would only add fixture noise.
        state=CaseState.INQUIRY,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def audit_repo():
    repo = AsyncMock()
    repo.record_access = AsyncMock(return_value=True)
    return repo


@pytest.fixture
def grant_repo():
    repo = AsyncMock()
    # No grant by default: the interesting state is the denied one.
    repo.find_live_grant = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def case_service():
    service = AsyncMock()
    service.get_case = AsyncMock(return_value=_case())
    service.get_case_team_ids = AsyncMock(return_value=[])
    # A real CaseMessagesResponse, not a mock: the operator transcript envelope
    # declares the same type the owner-facing endpoint serves, so a stand-in
    # would let the two shapes drift without the test noticing.
    service.get_case_messages_enhanced = AsyncMock(
        return_value=CaseMessagesResponse(
            messages=[],
            total_count=0,
            retrieved_count=0,
            has_more=False,
        )
    )
    return service


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
    """Run the gate in its Cloud posture.

    ``authorize_content_read`` is the only thing that reads deployment mode for
    the gate decision, so this patches the settings accessor it uses rather than
    mutating global settings.
    """
    from faultmaven.api import operator_grants

    real = operator_grants.get_settings()

    class _CloudSettings:
        is_cloud = True
        deployment_mode = "cloud"

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(operator_grants, "get_settings", _CloudSettings)


@pytest.mark.unit
@pytest.mark.security
class TestCloudRequiresALiveGrant:
    @pytest.mark.parametrize("path", CONTENT_PATHS)
    def test_no_grant_refuses_and_reads_nothing(
        self, client, cloud, case_service, audit_repo, path
    ):
        resp = client.get(path)

        assert resp.status_code == 403
        # The refusal must happen before any content is touched. An endpoint
        # that reads first and refuses second has already loaded the data it
        # claims to be withholding.
        case_service.get_case.assert_not_awaited()
        # A denial is not an access: it belongs in the log, not in a trail whose
        # vocabulary is 'list' | 'content_open'.
        audit_repo.record_access.assert_not_awaited()

    @pytest.mark.parametrize(
        "not_live",
        [
            pytest.param(
                {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)},
                id="expired",
            ),
            pytest.param(
                {"revoked_at": datetime.now(timezone.utc)},
                id="revoked",
            ),
            pytest.param(
                {"approval_state": GrantApprovalState.PENDING},
                id="pending-approval",
            ),
            pytest.param(
                {"approval_state": GrantApprovalState.DENIED},
                id="denied",
            ),
        ],
    )
    @pytest.mark.parametrize("path", CONTENT_PATHS)
    def test_a_grant_that_is_not_live_refuses(
        self, client, cloud, grant_repo, case_service, not_live, path
    ):
        """Sweep every independent way a grant stops authorising.

        The repository query filters on all three, but this returns the
        non-live grant anyway: the route must not rely on the SQL predicate
        being the only thing standing between an expired grant and a
        transcript. A divergence between the two must fail closed.
        """
        grant_repo.find_live_grant = AsyncMock(return_value=_grant(**not_live))

        resp = client.get(path)

        assert resp.status_code == 403
        case_service.get_case.assert_not_awaited()

    @pytest.mark.parametrize("path", CONTENT_PATHS)
    def test_the_gate_asks_for_this_case(self, client, cloud, grant_repo, path):
        """A grant over some other case must not open this one."""
        client.get(path)
        assert grant_repo.find_live_grant.await_args.kwargs["target_case_id"] == CASE_ID
        assert (
            grant_repo.find_live_grant.await_args.kwargs["operator_user_id"] == "op-1"
        )

    def test_a_live_grant_opens_the_content(self, client, cloud, grant_repo):
        grant_repo.find_live_grant = AsyncMock(return_value=_grant())

        resp = client.get(CONTENT_PATHS[0])

        assert resp.status_code == 200
        body = resp.json()
        assert body["access"] == "break_glass"
        assert body["grant"]["grant_id"] == "grant-1"
        assert body["grant"]["is_live"] is True
        assert body["case"]["title"] == "payments API 5xx spike"

    def test_a_live_grant_opens_the_transcript(self, client, cloud, grant_repo):
        grant_repo.find_live_grant = AsyncMock(return_value=_grant())

        resp = client.get(CONTENT_PATHS[1])

        assert resp.status_code == 200
        assert resp.json()["access"] == "break_glass"


@pytest.mark.unit
@pytest.mark.security
class TestStandaloneIsAuditedNotGated:
    @pytest.mark.parametrize("path", CONTENT_PATHS)
    def test_content_is_served_without_a_grant(
        self, client, grant_repo, audit_repo, path
    ):
        """Self-hosted: the operator and the data controller are the same party."""
        resp = client.get(path)

        assert resp.status_code == 200
        assert resp.json()["access"] == "standing"
        assert resp.json()["grant"] is None
        # No grant lookup at all — the gate short-circuits before it.
        grant_repo.find_live_grant.assert_not_awaited()
        # But the read is still recorded.
        audit_repo.record_access.assert_awaited_once()

    def test_the_access_is_still_recorded_as_content(self, client, audit_repo):
        client.get(CONTENT_PATHS[0])
        kwargs = audit_repo.record_access.await_args.kwargs
        assert kwargs["action"] is OperatorAction.CONTENT_OPEN
        assert kwargs["target_case_id"] == CASE_ID
        # Standing access has no justification to record; the row must not
        # invent one.
        assert kwargs["reason"] is None
        assert kwargs["grant_id"] is None


@pytest.mark.unit
@pytest.mark.security
class TestTheAuditRowNamesTheGrant:
    def test_grant_provenance_is_denormalised_onto_the_row(
        self, client, cloud, grant_repo, audit_repo
    ):
        """The audit row must stand alone as evidence.

        Referencing the grant only by id would make the trail unreadable if the
        grant row were ever lost, so the justification and the window are copied
        onto it.
        """
        grant = _grant()
        grant_repo.find_live_grant = AsyncMock(return_value=grant)

        client.get(CONTENT_PATHS[0])

        kwargs = audit_repo.record_access.await_args.kwargs
        assert kwargs["grant_id"] == grant.grant_id
        assert kwargs["reason"] == grant.reason
        assert kwargs["expires_at"] == grant.expires_at

    def test_attribution_comes_from_the_request_not_the_grants_claim(
        self, client, cloud, grant_repo, audit_repo, monkeypatch
    ):
        """An operator must not choose which tenant their audit row names.

        ``target_enterprise_id`` on a grant is written by the operator and is
        never validated against the case. Under ``single`` nothing exercises it —
        no rebind happens — so recording it verbatim would let the audited party
        misattribute their own access to a tenant they never touched, in a row
        migration 036 makes uncorrectable. The org the read actually ran under is
        recorded instead.
        """
        from faultmaven.api import operator_grants

        monkeypatch.setattr(
            operator_grants, "get_current_enterprise_id", lambda: "ent-actually-bound"
        )
        # The grant claims some other tenant entirely.
        grant_repo.find_live_grant = AsyncMock(
            return_value=_grant(target_enterprise_id="org-someone-elses")
        )

        client.get(CONTENT_PATHS[0])

        assert (
            audit_repo.record_access.await_args.kwargs["target_enterprise_id"]
            == "ent-actually-bound"
        )

    def test_under_multi_tenancy_the_grants_org_is_recorded(
        self, client, cloud, grant_repo, audit_repo, monkeypatch
    ):
        """There the claim has already been made load-bearing.

        ``bind_grant_enterprise_scope`` rebinds RLS to the named organization before the
        read, so a false claim returns no rows and 404s. Having survived that,
        the grant's org is a fact about the request rather than an assertion
        about it — and it is the only value that names the tenant reached.
        """
        from faultmaven.api import operator_grants

        monkeypatch.setattr(
            operator_grants, "requested_tenant_provider", lambda: "multi"
        )
        monkeypatch.setattr(operator_grants, "BUILTIN_MULTI", "multi")
        monkeypatch.setattr(
            operator_grants, "set_current_enterprise_id", lambda _org: None
        )
        grant_repo.find_live_grant = AsyncMock(return_value=_grant())

        client.get(CONTENT_PATHS[0])

        assert (
            audit_repo.record_access.await_args.kwargs["target_enterprise_id"]
            == GRANT_ORG
        )

    @pytest.mark.parametrize("path", CONTENT_PATHS)
    def test_audit_is_written_before_content_is_read(
        self, client, audit_repo, case_service, path
    ):
        """A crash mid-request must leave evidence of the attempt, not silence."""
        order = []
        audit_repo.record_access = AsyncMock(
            side_effect=lambda **_: order.append("audit") or True
        )
        case_service.get_case = AsyncMock(
            side_effect=lambda *a, **k: order.append("case") or _case()
        )

        client.get(path)

        assert order[:2] == ["audit", "case"]


@pytest.mark.unit
@pytest.mark.security
class TestFailsClosed:
    @pytest.mark.parametrize("path", CONTENT_PATHS)
    def test_audit_write_failure_refuses_the_request(
        self, client, audit_repo, case_service, path
    ):
        audit_repo.record_access = AsyncMock(side_effect=RuntimeError("db down"))

        resp = client.get(path)

        assert resp.status_code == 503
        case_service.get_case.assert_not_awaited()

    def test_missing_grant_store_refuses_cloud_content(
        self, cloud, audit_repo, case_service
    ):
        """Without the grant store there is no way to establish authorization."""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_platform_admin] = _operator
        app.dependency_overrides[get_operator_audit_repository] = lambda: audit_repo
        app.dependency_overrides[get_case_service] = lambda: case_service
        # app.state.operator_grant_repository deliberately absent.

        resp = TestClient(app).get(CONTENT_PATHS[0])

        assert resp.status_code == 503
        case_service.get_case.assert_not_awaited()

    def test_a_grant_naming_an_invisible_case_yields_404(
        self, client, cloud, grant_repo, case_service
    ):
        """A wrong (case, org) pair fails closed by itself.

        This is why grant creation does not validate the pair: after the rebind
        the row is simply not visible, so a mistake costs a 404 and an audit row
        and discloses nothing.
        """
        grant_repo.find_live_grant = AsyncMock(return_value=_grant())
        case_service.get_case = AsyncMock(return_value=None)

        resp = client.get(CONTENT_PATHS[0])

        assert resp.status_code == 404

    def test_transcript_is_not_served_for_an_invisible_case(
        self, client, cloud, grant_repo, case_service
    ):
        """The transcript endpoint must re-establish existence, not assume it."""
        grant_repo.find_live_grant = AsyncMock(return_value=_grant())
        case_service.get_case = AsyncMock(return_value=None)

        resp = client.get(CONTENT_PATHS[1])

        assert resp.status_code == 404
        case_service.get_case_messages_enhanced.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.security
class TestOverLongIdentifiersAreRejected:
    @pytest.mark.parametrize(
        "path_template",
        ["/api/v1/admin/cases/{}", "/api/v1/admin/cases/{}/messages"],
    )
    def test_rejected_rather_than_truncated(
        self, client, audit_repo, case_service, path_template
    ):
        """Truncation could name a different, real case in an immutable row."""
        over_long = "c" * 40

        resp = client.get(path_template.format(over_long))

        assert resp.status_code == 422
        audit_repo.record_access.assert_not_awaited()
        case_service.get_case.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.security
class TestRlsIsReboundNotBypassed:
    def test_multi_tenant_rebinds_to_the_granted_organization(
        self, client, cloud, grant_repo, monkeypatch
    ):
        """The elevated read is bound to ONE other tenant, not unbound."""
        from faultmaven.api import operator_grants

        bound = []
        monkeypatch.setattr(
            operator_grants, "set_current_enterprise_id", lambda org: bound.append(org)
        )
        monkeypatch.setattr(
            operator_grants, "requested_tenant_provider", lambda: "multi"
        )
        monkeypatch.setattr(operator_grants, "BUILTIN_MULTI", "multi")
        grant_repo.find_live_grant = AsyncMock(return_value=_grant())

        client.get(CONTENT_PATHS[0])

        assert bound == [GRANT_ORG]

    def test_single_tenant_does_not_rebind(
        self, client, cloud, grant_repo, monkeypatch
    ):
        """Under `single` every row carries the Standalone org, so rebinding to
        the grant's organization would make the read return nothing."""
        from faultmaven.api import operator_grants

        bound = []
        monkeypatch.setattr(
            operator_grants, "set_current_enterprise_id", lambda org: bound.append(org)
        )
        grant_repo.find_live_grant = AsyncMock(return_value=_grant())

        client.get(CONTENT_PATHS[0])

        assert bound == []
