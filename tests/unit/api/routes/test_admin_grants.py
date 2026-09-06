"""Break-glass grant lifecycle (ADR-012 D9, #815).

Two things are under test here, and they are different in kind:

- ``OperatorAccessGrant.is_live`` — the single definition of "this grant
  authorises a read". It is swept across the whole input space rather than
  spot-checked, because every caller in the system delegates to it and a gap
  here is a gap in the gate.
- The grant endpoints — that a justification cannot be trivially satisfied, a
  window cannot be widened, and revocation cannot move the record of when
  access ended.
"""

from datetime import datetime, timedelta, timezone
from itertools import product
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.operator_grants import get_operator_grant_repository
from faultmaven.api.routes.admin_grants import router
from faultmaven.models.api_models import (
    MAX_GRANT_TTL_MINUTES,
    MIN_GRANT_REASON_LENGTH,
)
from faultmaven.models.interfaces_operator_grant import (
    APPROVED_STATES,
    GrantApprovalState,
    OperatorAccessGrant,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

CASE_ID = "case_a1b2c3d4e5f6"
GRANT_ENTERPRISE = "ent-tenant-under-investigation"
GOOD_REASON = "customer reports the investigation is stuck; ticket SUP-4821"
BASE = "/api/v1/admin/grants"


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
        target_enterprise_id=GRANT_ENTERPRISE,
        reason=GOOD_REASON,
        created_at=now,
        expires_at=now + timedelta(minutes=60),
        approval_state=GrantApprovalState.AUTO_APPROVED,
    )
    defaults.update(overrides)
    return OperatorAccessGrant(**defaults)


@pytest.fixture
def grant_repo():
    repo = AsyncMock()
    repo.create_grant = AsyncMock(side_effect=lambda g: g)
    repo.list_grants = AsyncMock(return_value=([], 0))
    repo.revoke_grant = AsyncMock(
        return_value=_grant(revoked_at=datetime.now(timezone.utc))
    )
    return repo


@pytest.fixture
def client(grant_repo):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_platform_admin] = _operator
    app.dependency_overrides[get_operator_grant_repository] = lambda: grant_repo
    return TestClient(app)


@pytest.mark.unit
@pytest.mark.security
class TestLivenessCoversTheWholeInputSpace:
    """``is_live`` is the gate. Sweep it, do not sample it.

    Approval state, revocation and expiry are three independent axes, and the
    predicate must be the conjunction of all three. Testing one live example and
    one expired one would pass just as happily on a predicate that ignored
    revocation entirely.
    """

    @pytest.mark.parametrize(
        "state,revoked,expired",
        list(product(list(GrantApprovalState), [False, True], [False, True])),
    )
    def test_live_iff_approved_and_unrevoked_and_unexpired(
        self, state, revoked, expired
    ):
        now = datetime.now(timezone.utc)
        grant = _grant(
            approval_state=state,
            revoked_at=now - timedelta(minutes=5) if revoked else None,
            expires_at=(
                now - timedelta(minutes=1) if expired else now + timedelta(minutes=60)
            ),
        )

        expected = (state in APPROVED_STATES) and not revoked and not expired
        assert grant.is_live(now=now) is expected

    def test_expiry_is_exclusive_at_the_boundary(self):
        """A grant is not live at the instant it expires.

        The boundary has to fall somewhere, and it falls closed: at exactly
        ``expires_at`` the window is over.
        """
        now = datetime.now(timezone.utc)
        assert _grant(expires_at=now).is_live(now=now) is False
        assert _grant(expires_at=now + timedelta(seconds=1)).is_live(now=now) is True

    def test_a_naive_expiry_is_read_as_utc(self):
        """SQLite hands back timestamps with no tzinfo.

        Comparing those against an aware ``now`` raises ``TypeError`` — inside an
        authorization check, which must never fail on a technicality where the
        outcome could be read as "allow". Treating a naive value as the UTC it
        was written as keeps the comparison meaningful.
        """
        now = datetime.now(timezone.utc)
        naive_future = (now + timedelta(minutes=30)).replace(tzinfo=None)
        naive_past = (now - timedelta(minutes=30)).replace(tzinfo=None)

        assert _grant(expires_at=naive_future).is_live(now=now) is True
        assert _grant(expires_at=naive_past).is_live(now=now) is False


@pytest.mark.unit
@pytest.mark.security
class TestAJustificationIsRequired:
    @pytest.mark.parametrize(
        "reason",
        [
            pytest.param("", id="empty"),
            pytest.param(".", id="single-char"),
            pytest.param("debugging", id="too-short"),
            pytest.param(" " * 40, id="whitespace-only"),
            pytest.param("  x  " + " " * 40, id="whitespace-padded-to-length"),
        ],
    )
    def test_a_substanceless_reason_is_rejected(self, client, grant_repo, reason):
        """Including one that only clears the length floor with whitespace.

        A floor measured before stripping would let "x" plus forty spaces
        through, which is the exact degenerate value the floor exists to stop.
        """
        resp = client.post(
            BASE,
            json={
                "case_id": CASE_ID,
                "enterprise_id": GRANT_ENTERPRISE,
                "reason": reason,
            },
        )

        assert resp.status_code == 422
        grant_repo.create_grant.assert_not_awaited()

    def test_a_real_reason_is_accepted_and_stored_stripped(self, client, grant_repo):
        resp = client.post(
            BASE,
            json={
                "case_id": CASE_ID,
                "enterprise_id": GRANT_ENTERPRISE,
                "reason": f"  {GOOD_REASON}  ",
            },
        )

        assert resp.status_code == 201
        assert grant_repo.create_grant.await_args.args[0].reason == GOOD_REASON

    def test_the_floor_is_the_declared_one(self):
        """Pin the constant the API documents to the one it enforces."""
        assert MIN_GRANT_REASON_LENGTH == 20


@pytest.mark.unit
@pytest.mark.security
class TestTheWindowIsBounded:
    def test_a_ttl_beyond_the_ceiling_is_rejected(self, client, grant_repo):
        resp = client.post(
            BASE,
            json={
                "case_id": CASE_ID,
                "enterprise_id": GRANT_ENTERPRISE,
                "reason": GOOD_REASON,
                "ttl_minutes": MAX_GRANT_TTL_MINUTES + 1,
            },
        )

        assert resp.status_code == 422
        grant_repo.create_grant.assert_not_awaited()

    def test_a_zero_or_negative_ttl_is_rejected(self, client):
        for ttl in (0, -60):
            resp = client.post(
                BASE,
                json={
                    "case_id": CASE_ID,
                    "enterprise_id": GRANT_ENTERPRISE,
                    "reason": GOOD_REASON,
                    "ttl_minutes": ttl,
                },
            )
            assert resp.status_code == 422, ttl

    def test_the_grant_expires_after_the_requested_window(self, client, grant_repo):
        resp = client.post(
            BASE,
            json={
                "case_id": CASE_ID,
                "enterprise_id": GRANT_ENTERPRISE,
                "reason": GOOD_REASON,
                "ttl_minutes": 30,
            },
        )

        assert resp.status_code == 201
        grant = grant_repo.create_grant.await_args.args[0]
        window = grant.expires_at - grant.created_at
        assert window == timedelta(minutes=30)

    def test_there_is_no_extend_endpoint(self, client):
        """Widening a window must not be reachable.

        An extendable grant converges on a standing one. Needing longer means a
        new grant, with a fresh reason and a fresh audit row — enforced here, and
        again by the database trigger that pins ``expires_at``.
        """
        resp = client.post(f"{BASE}/grant-1/extend", json={"ttl_minutes": 240})
        assert resp.status_code == 404


@pytest.mark.unit
@pytest.mark.security
class TestGrantCreationTouchesNoTenantData:
    def test_no_case_lookup_happens(self, client, grant_repo):
        """Validating the pair would make this endpoint an existence oracle.

        An operator could otherwise probe whether a case id exists in a tenant
        they hold no grant for. A wrong pair instead fails closed at the read.
        """
        client.post(
            BASE,
            json={
                "case_id": "case_does_not_exist",
                "organization_id": "org-not-real",
                "reason": GOOD_REASON,
            },
        )
        # The repository the route holds is the grant store only; assert the
        # route asked it for nothing but the write.
        grant_repo.find_live_grant.assert_not_awaited()
        grant_repo.get_grant.assert_not_awaited()

    def test_the_grant_records_who_asked(self, client, grant_repo):
        client.post(
            BASE,
            json={
                "case_id": CASE_ID,
                "enterprise_id": GRANT_ENTERPRISE,
                "reason": GOOD_REASON,
            },
        )
        grant = grant_repo.create_grant.await_args.args[0]
        assert grant.operator_user_id == "op-1"
        assert grant.operator_username == "operator@example.com"
        # Auto-approved today; the seam for customer approval is the state, not
        # a different code path.
        assert grant.approval_state is GrantApprovalState.AUTO_APPROVED


@pytest.mark.unit
@pytest.mark.security
class TestRevocation:
    def test_an_unknown_grant_is_a_404(self, client, grant_repo):
        grant_repo.revoke_grant = AsyncMock(return_value=None)
        resp = client.post(f"{BASE}/nope/revoke")
        assert resp.status_code == 404

    def test_revoking_someone_elses_grant_is_allowed(self, client, grant_repo):
        """Shortening access is the safe direction.

        Requiring ownership would let a grant outlive the only person able to
        withdraw it.
        """
        grant_repo.revoke_grant = AsyncMock(
            return_value=_grant(
                operator_user_id="some-other-operator",
                revoked_at=datetime.now(timezone.utc),
            )
        )

        resp = client.post(f"{BASE}/grant-1/revoke")

        assert resp.status_code == 200
        assert resp.json()["is_live"] is False
        assert grant_repo.revoke_grant.await_args.kwargs["revoked_by"] == "op-1"

    def test_an_over_long_grant_id_is_rejected(self, client, grant_repo):
        resp = client.post(f"{BASE}/{'g' * 40}/revoke")
        assert resp.status_code == 422
        grant_repo.revoke_grant.assert_not_awaited()


@pytest.mark.unit
class TestListing:
    def test_the_listing_is_not_scoped_to_the_caller(self, client, grant_repo):
        """Who holds access to a tenant is the question this surface answers.

        An operator who could only see their own grants could not review
        anyone else's, which is the whole point of the review path.
        """
        client.get(BASE)
        assert grant_repo.list_grants.await_args.kwargs["operator_user_id"] is None

    def test_filters_pass_through(self, client, grant_repo):
        client.get(f"{BASE}?case_id={CASE_ID}&live_only=true")
        kwargs = grant_repo.list_grants.await_args.kwargs
        assert kwargs["target_case_id"] == CASE_ID
        assert kwargs["live_only"] is True
