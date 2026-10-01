"""The operator account list under ``TENANT_PROVIDER=multi``.

The list spans every enterprise: account records are the service's own
operational data, read from the account store with eleven columns. The
properties pinned here are the ones that make that safe:

1. The rows come from the cross-enterprise read; roles come only from ONE read
   confined to the operator's enterprise and asked about the page's own ids, and
   every other row is ``manageable: false`` with ``roles: []``.
2. The access is recorded BEFORE anything is read, a failed or missing record
   refuses the request, and the record says which list it was — never what was
   searched for.
3. Failure direction: no account store is a 503 before recording; a failure
   after recording is one logged event and a 500 with fixed text — never the
   operator's own enterprise served as the whole.
4. Under single-tenancy nothing changes but the new fields: every row is
   manageable, nothing is recorded, and the cross-enterprise read is not called.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.routes.admin import (
    get_account_directory,
    get_user_service,
    router,
)
from faultmaven.models.interfaces_operator_audit import OperatorAction
from faultmaven.modules.auth.contracts import AccountMetadata
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

pytestmark = [pytest.mark.unit, pytest.mark.security]

_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
OWN = "ent-operator"
OTHER = "ent-other"


@contextmanager
def _tenancy(name: str):
    """Both read sites of the tenant provider, as the deployment has them."""
    with (
        patch(
            "faultmaven.api.operator_user_scope.requested_tenant_provider",
            return_value=name,
        ),
        patch(
            "faultmaven.providers.tenancy.factory.requested_tenant_provider",
            return_value=name,
        ),
    ):
        yield


def _operator() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="op-1",
        enterprise_id=OWN,
        email="operator@example.com",
        roles=["user", "platform_admin"],
        permissions=[],
    )


def _account(user_id: str, enterprise_id: str, **overrides) -> AccountMetadata:
    fields = dict(
        user_id=user_id,
        enterprise_id=enterprise_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        account_kind="individual",
        service_channel=None,
        is_active=True,
        is_email_verified=True,
        last_login_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    fields.update(overrides)
    return AccountMetadata(**fields)


def _user(user_id: str, enterprise_id: str, roles=("admin",), **overrides):
    """A ``user_repository.User``-shaped row, as the confined reads return it."""
    fields = dict(
        user_id=user_id,
        enterprise_id=enterprise_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        roles=list(roles),
        account_kind="individual",
        service_channel=None,
        is_active=True,
        is_email_verified=True,
        last_login_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def accounts():
    return [
        _account("u-own", OWN),
        _account("u-slack", OTHER, account_kind="service", service_channel="slack"),
        _account("op-1", OWN),
    ]


@pytest.fixture
def stored():
    """Every account the store holds, by id."""
    return {
        "u-own": _user("u-own", OWN, roles=("admin",)),
        "op-1": _user("op-1", OWN, roles=()),
        "u-slack": _user("u-slack", OTHER, roles=("admin",)),
    }


@pytest.fixture
def directory(accounts, stored):
    """The account store's two directory reads. ``get_many_in_enterprise``
    honours its enterprise predicate for real."""

    async def get_many_in_enterprise(enterprise_id, user_ids):
        return [
            user
            for user in stored.values()
            if user.enterprise_id == enterprise_id and user.user_id in user_ids
        ]

    directory = AsyncMock()
    directory.list_account_metadata = AsyncMock(return_value=(accounts, 9))
    directory.get_many_in_enterprise = AsyncMock(side_effect=get_many_in_enterprise)
    return directory


@pytest.fixture
def user_service(stored):
    """The single-tenant listing. Under multi it must never be reached."""

    async def list_users(enterprise_id=None, **_):
        rows = [
            user
            for user in stored.values()
            if enterprise_id is None or user.enterprise_id == enterprise_id
        ]
        return rows, len(rows)

    service = AsyncMock()
    service.list_users = AsyncMock(side_effect=list_users)
    return service


@pytest.fixture
def audit_repo():
    repo = AsyncMock()
    repo.record_access = AsyncMock(return_value=True)
    return repo


def _client(user_service, directory, audit_repo) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_platform_admin] = _operator
    app.dependency_overrides[get_user_service] = lambda: user_service
    app.dependency_overrides[get_account_directory] = lambda: directory
    app.state.operator_audit_repository = audit_repo
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def multi():
    with _tenancy(BUILTIN_MULTI):
        yield


@pytest.mark.usefixtures("multi")
class TestServedAcrossEnterprises:
    def test_rows_span_enterprises_and_only_the_operators_own_are_manageable(
        self, user_service, directory, audit_repo
    ):
        resp = _client(user_service, directory, audit_repo).get(
            "/api/v1/admin/users",
            params={"is_active": "true", "search": "u-", "limit": 3, "offset": 6},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The total counts every match in every enterprise, not the page.
        assert (body["total"], body["limit"], body["offset"]) == (9, 3, 6)
        rows = {row["user_id"]: row for row in body["users"]}
        assert [row["user_id"] for row in body["users"]] == ["u-own", "u-slack", "op-1"]

        assert rows["u-own"]["manageable"] is True
        assert rows["u-own"]["roles"] == ["admin"]
        assert rows["u-own"]["enterprise_id"] == OWN
        # An empty stored role list reads as the list's default, as on the
        # single-tenant path.
        assert rows["op-1"]["roles"] == ["member"]
        assert rows["op-1"]["manageable"] is True

        # Another enterprise's account: listed, never manageable, roles not
        # reported — although the account holds one.
        assert rows["u-slack"]["enterprise_id"] == OTHER
        assert rows["u-slack"]["manageable"] is False
        assert rows["u-slack"]["roles"] == []
        assert rows["u-slack"]["account_kind"] == "service"
        assert rows["u-slack"]["service_channel"] == "slack"

        directory.list_account_metadata.assert_awaited_once_with(
            is_active=True, search="u-", enterprise_id=None, limit=3, offset=6
        )
        # Roles came from ONE read: the operator's enterprise, the page's own
        # ids only — never the service's listing.
        directory.get_many_in_enterprise.assert_awaited_once()
        enterprise, ids = directory.get_many_in_enterprise.await_args.args
        assert enterprise == OWN
        assert set(ids) == {"u-own", "op-1"}
        user_service.list_users.assert_not_awaited()

    def test_the_enterprise_filter_reaches_the_read(
        self, user_service, directory, audit_repo
    ):
        _client(user_service, directory, audit_repo).get(
            "/api/v1/admin/users", params={"enterprise_id": OTHER}
        )
        assert (
            directory.list_account_metadata.await_args.kwargs["enterprise_id"] == OTHER
        )

    def test_a_page_with_no_own_account_never_reads_roles(
        self, user_service, directory, audit_repo
    ):
        directory.list_account_metadata = AsyncMock(
            return_value=([_account("u-x", OTHER)], 1)
        )

        resp = _client(user_service, directory, audit_repo).get("/api/v1/admin/users")

        assert resp.status_code == 200
        assert resp.json()["users"][0]["manageable"] is False
        directory.get_many_in_enterprise.assert_not_awaited()

    def test_an_own_account_gone_from_the_confined_read_is_not_manageable(
        self, user_service, directory, audit_repo, caplog
    ):
        """Listed by the cross-enterprise read, deleted before the roles read:
        served, but not as something the operator can act on."""
        directory.list_account_metadata = AsyncMock(
            return_value=([_account("u-gone", OWN)], 1)
        )

        with caplog.at_level("WARNING"):
            resp = _client(user_service, directory, audit_repo).get(
                "/api/v1/admin/users"
            )

        (row,) = resp.json()["users"]
        assert (row["manageable"], row["roles"]) == (False, [])
        assert "admin_user_list_account_vanished" in caplog.text

    def test_an_empty_search_is_no_search_for_the_read_and_the_record(
        self, user_service, directory, audit_repo
    ):
        """``?search=`` filters nothing, so it is recorded as no search too."""
        _client(user_service, directory, audit_repo).get(
            "/api/v1/admin/users", params={"search": ""}
        )

        assert directory.list_account_metadata.await_args.kwargs["search"] is None
        details = audit_repo.record_access.await_args.kwargs["details"]
        assert details["search_present"] is False

    def test_a_role_filter_is_refused_before_anything_is_read_or_recorded(
        self, user_service, directory, audit_repo
    ):
        resp = _client(user_service, directory, audit_repo).get(
            "/api/v1/admin/users", params={"role": "admin"}
        )

        assert resp.status_code == 422
        assert "own enterprise" in resp.json()["detail"]
        directory.list_account_metadata.assert_not_awaited()
        audit_repo.record_access.assert_not_awaited()
        user_service.list_users.assert_not_awaited()

    @pytest.mark.parametrize(
        "value, says",
        [
            ("e" * 37, "longer than an enterprise id can be"),
            ("   ", "must not be empty"),
            ("ent\x00x", "NUL"),
        ],
    )
    def test_an_enterprise_filter_no_enterprise_could_carry_is_refused(
        self, user_service, directory, audit_repo, value, says
    ):
        """With a message about the filter itself — nothing about cases, and
        nothing is truncated."""
        resp = _client(user_service, directory, audit_repo).get(
            "/api/v1/admin/users", params={"enterprise_id": value}
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert says in detail
        assert "case" not in detail and "truncat" not in detail
        audit_repo.record_access.assert_not_awaited()
        directory.list_account_metadata.assert_not_awaited()


@pytest.mark.usefixtures("multi")
class TestAuditComesFirst:
    def test_the_access_is_recorded_before_the_read(
        self, user_service, directory, audit_repo
    ):
        order = []
        audit_repo.record_access = AsyncMock(
            side_effect=lambda **_: order.append("audit") or True
        )
        directory.list_account_metadata = AsyncMock(
            side_effect=lambda **_: order.append("read") or ([], 0)
        )

        _client(user_service, directory, audit_repo).get("/api/v1/admin/users")

        assert order == ["audit", "read"]

    def test_the_record_names_the_account_list_and_spans_every_enterprise(
        self, user_service, directory, audit_repo
    ):
        _client(user_service, directory, audit_repo).get(
            "/api/v1/admin/users", params={"search": "alice", "is_active": "false"}
        )

        kwargs = audit_repo.record_access.await_args.kwargs
        assert kwargs["action"] is OperatorAction.LIST
        assert kwargs["details"]["surface"] == "accounts"
        assert kwargs["details"]["view"] == "metadata"
        assert kwargs["details"]["is_active_filter"] is False
        assert kwargs["target_enterprise_id"] is None

    def test_the_record_says_a_search_was_applied_never_what_it_was(
        self, user_service, directory, audit_repo
    ):
        """A search is often an email address, and the trail is append-only:
        a search text written into it could never be erased. Only whether one
        was applied is recorded — no plaintext, no hash."""
        search = "alice.smith@customer.example"
        client = _client(user_service, directory, audit_repo)

        client.get("/api/v1/admin/users", params={"search": search})
        with_search = audit_repo.record_access.await_args.kwargs
        client.get("/api/v1/admin/users")
        without_search = audit_repo.record_access.await_args.kwargs

        assert with_search["details"]["search_present"] is True
        assert without_search["details"]["search_present"] is False
        for recorded in (with_search, without_search):
            dumped = repr(recorded)
            assert "alice" not in dumped and "customer.example" not in dumped
            assert "search_filter" not in recorded["details"]
        # The read itself still received the search.
        assert directory.list_account_metadata.await_args_list[0].kwargs["search"] == (
            search
        )

    def test_an_enterprise_filter_is_the_recorded_target(
        self, user_service, directory, audit_repo
    ):
        _client(user_service, directory, audit_repo).get(
            "/api/v1/admin/users", params={"enterprise_id": OTHER}
        )

        assert audit_repo.record_access.await_args.kwargs["target_enterprise_id"] == (
            OTHER
        )

    def test_a_failed_record_refuses_the_request_before_reading(
        self, user_service, directory, audit_repo
    ):
        audit_repo.record_access = AsyncMock(side_effect=RuntimeError("db down"))

        resp = _client(user_service, directory, audit_repo).get("/api/v1/admin/users")

        assert resp.status_code == 503
        directory.list_account_metadata.assert_not_awaited()
        user_service.list_users.assert_not_awaited()

    def test_no_audit_trail_refuses_the_request_before_reading(
        self, user_service, directory
    ):
        resp = _client(user_service, directory, None).get("/api/v1/admin/users")

        assert resp.status_code == 503
        directory.list_account_metadata.assert_not_awaited()
        user_service.list_users.assert_not_awaited()


@pytest.mark.usefixtures("multi")
class TestFailureDirection:
    def test_no_account_store_is_a_503_before_recording(
        self, user_service, audit_repo, caplog
    ):
        with caplog.at_level("ERROR"):
            resp = _client(user_service, None, audit_repo).get("/api/v1/admin/users")

        assert resp.status_code == 503
        user_service.list_users.assert_not_awaited()
        # Nothing was read, so nothing is recorded as an access.
        audit_repo.record_access.assert_not_awaited()
        assert "admin_user_list_unwired" in caplog.text

    @pytest.mark.parametrize(
        "failing", ["list_account_metadata", "get_many_in_enterprise"]
    )
    def test_a_failure_after_the_record_is_one_logged_500_with_fixed_text(
        self, user_service, directory, audit_repo, caplog, failing
    ):
        """Either read failing after the access was recorded: one event,
        ``admin_user_list_read_failed``, and a 500 that carries none of the
        error. Never the single-tenant listing in its place."""
        setattr(
            directory,
            failing,
            AsyncMock(side_effect=RuntimeError("SECRET-DRIVER-DETAIL")),
        )

        with caplog.at_level("ERROR"):
            resp = _client(user_service, directory, audit_repo).get(
                "/api/v1/admin/users"
            )

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Failed to list users"
        assert "SECRET-DRIVER-DETAIL" not in resp.text
        events = [r for r in caplog.records if r.levelname == "ERROR"]
        assert [r.getMessage() for r in events] == ["admin_user_list_read_failed"]
        audit_repo.record_access.assert_awaited_once()
        user_service.list_users.assert_not_awaited()


class TestSingleTenant:
    def test_every_row_is_manageable_and_carries_kind_and_channel(
        self, user_service, directory, audit_repo, stored
    ):
        with _tenancy(BUILTIN_SINGLE):
            resp = _client(user_service, directory, audit_repo).get(
                "/api/v1/admin/users"
            )

        assert resp.status_code == 200, resp.text
        rows = {row["user_id"]: row for row in resp.json()["users"]}
        assert set(rows) == set(stored)
        assert all(row["manageable"] is True for row in rows.values())
        assert rows["u-slack"]["roles"] == ["admin"]
        assert rows["u-own"]["account_kind"] == "individual"
        assert rows["u-own"]["service_channel"] is None
        # The deployment is the tenant: no confinement, nothing recorded, and
        # the cross-enterprise read is not called even with a store wired.
        assert user_service.list_users.await_args.kwargs["enterprise_id"] is None
        directory.list_account_metadata.assert_not_awaited()
        directory.get_many_in_enterprise.assert_not_awaited()
        audit_repo.record_access.assert_not_awaited()

    def test_the_role_filter_still_works(self, user_service, directory, audit_repo):
        with _tenancy(BUILTIN_SINGLE):
            resp = _client(user_service, directory, audit_repo).get(
                "/api/v1/admin/users", params={"role": "admin"}
            )

        assert resp.status_code == 200
        assert user_service.list_users.await_args.kwargs["role"] == "admin"

    def test_an_empty_search_is_no_search(self, user_service, directory, audit_repo):
        with _tenancy(BUILTIN_SINGLE):
            _client(user_service, directory, audit_repo).get(
                "/api/v1/admin/users", params={"search": ""}
            )

        assert user_service.list_users.await_args.kwargs["search"] is None
