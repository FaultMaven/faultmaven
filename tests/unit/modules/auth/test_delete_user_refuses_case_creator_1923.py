"""``DELETE /api/v1/auth/users/{username}`` refuses a case's creator (#1923).

Owner ruling (2026-10-12): refuse until reassigned. The route asks how many
cases the account created BEFORE it revokes anything, and answers 409
``ACCOUNT_OWNS_CASES`` with the count and the ``fm-reassign-cases`` remedy. The
database key is the guarantee; a case created between the count and the delete
meets the key, and that refusal is the same 409 (after the revocation, which is
the documented residual). These tests pin the order with the collaborators
recorded:

1. lookup and scope → 404 (unchanged, first);
2. count → 409, nothing revoked, nothing deleted; unbound → 503;
3. revoke → 500 on failure, nothing deleted (unchanged);
4. delete → the store's typed refusal → 409, ``False`` → 500.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from faultmaven.api.operator_user_scope import OperatorUserScope
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.modules.auth.api import auth as auth_routes
from faultmaven.modules.auth.exceptions import AccountOwnsCasesError

pytestmark = [pytest.mark.unit, pytest.mark.security]

_USER = SimpleNamespace(user_id="user_123", username="doomed", enterprise_id="ent-1")


class _World:
    """The route's collaborators, each call recorded in ``order``."""

    def __init__(self, *, created=0, delete_result=True, delete_raises=None):
        self.order: list = []
        self.user_store = MagicMock()
        self.user_store.get_user_by_username = AsyncMock(return_value=_USER)

        async def _count(user_id):
            self.order.append(("count", user_id))
            return created

        async def _revoke(user_id):
            self.order.append(("revoke", user_id))

        async def _delete(user_id):
            self.order.append(("delete", user_id))
            if delete_raises is not None:
                raise delete_raises
            return delete_result

        self.user_store.delete_user = _delete
        self.user_service = SimpleNamespace(count_cases_created_by=_count)
        self.auth_service = SimpleNamespace(revoke_user_tokens=_revoke)
        self.state = SimpleNamespace(
            user_service=self.user_service, auth_service=self.auth_service
        )

    @property
    def calls(self) -> list:
        return [name for name, _ in self.order]

    async def delete(self):
        request = SimpleNamespace(app=SimpleNamespace(state=self.state))
        with patch.object(
            auth_routes, "get_user_store", AsyncMock(return_value=self.user_store)
        ):
            return await auth_routes.delete_user(
                username="doomed",
                request=request,
                operator=None,
                scope=OperatorUserScope(users=None),
            )


async def test_a_creator_is_refused_before_anything_is_revoked():
    world = _World(created=3)

    with pytest.raises(HTTPException) as refused:
        await world.delete()

    assert refused.value.status_code == 409
    assert refused.value.headers == {"x-error-code": "ACCOUNT_OWNS_CASES"}
    detail = refused.value.detail
    assert "created 3 case(s)" in detail
    assert (
        "fm-reassign-cases --enterprise-id ent-1 --from-user doomed --to-user"
    ) in detail
    # Revoke NOT called, delete NOT called: the sessions stay live and the
    # account stays, because only the count ran.
    assert world.calls == ["count"]


async def test_the_refusal_that_beats_the_count_is_the_same_409_after_a_revoke():
    """The race: the count saw none, a case appeared, and the key refused the
    delete. The account survived; its sessions were revoked once."""
    world = _World(created=0, delete_raises=AccountOwnsCasesError("user_123"))

    with pytest.raises(HTTPException) as refused:
        await world.delete()

    assert refused.value.status_code == 409
    assert refused.value.headers == {"x-error-code": "ACCOUNT_OWNS_CASES"}
    assert "sessions were revoked" in refused.value.detail
    assert "case(s)" not in refused.value.detail, "no count it could stand behind"
    assert world.calls == ["count", "revoke", "delete"]


async def test_an_account_that_created_nothing_is_deleted_after_its_revocation():
    world = _World(created=0)

    result = await world.delete()

    assert result == {
        "message": "User 'doomed' deleted successfully",
        "user_id": "user_123",
    }
    assert world.calls == ["count", "revoke", "delete"]


@pytest.mark.parametrize("unbound", ["no user service", "no case module bound"])
async def test_no_count_no_delete(unbound):
    """Fail closed: with nothing to ask, the route revokes and deletes nothing."""
    world = _World(created=0)
    if unbound == "no user service":
        world.state.user_service = None
    else:

        async def _unbound(_user_id):
            world.order.append(("count", _user_id))
            return None

        world.user_service.count_cases_created_by = _unbound

    with pytest.raises(HTTPException) as refused:
        await world.delete()

    assert refused.value.status_code == 503
    assert "revoke" not in world.calls and "delete" not in world.calls


async def test_an_unbound_user_service_counts_nothing():
    """``UserService.count_cases_created_by`` answers ``None`` until the
    composition root binds the case service — the route's 503 above."""
    from faultmaven.modules.auth.domain.services.user_service import UserService

    service = UserService(user_repo=MagicMock(), auth_service=MagicMock())
    assert await service.count_cases_created_by("user_123") is None

    counter = SimpleNamespace(count_cases_created_by=AsyncMock(return_value=2))
    service.bind_case_creator_count(counter)
    assert await service.count_cases_created_by("user_123") == 2
    counter.count_cases_created_by.assert_awaited_once_with("user_123")


async def test_a_failed_delete_is_still_a_500():
    world = _World(created=0, delete_result=False)

    with pytest.raises(HTTPException) as failed:
        await world.delete()

    assert failed.value.status_code == 500
    assert world.calls == ["count", "revoke", "delete"]


class TestTheStoreLetsTheRefusalThrough:
    """``DatabaseUserStore.delete_user`` turned every exception into ``False``,
    which the route answered as a 500 ``delete_failed``. The referential
    refusal now propagates, told apart by its type."""

    async def test_an_integrity_error_is_the_typed_refusal(self):
        repository = MagicMock()
        repository.delete = AsyncMock(
            side_effect=IntegrityError("DELETE FROM users", {}, Exception("fk"))
        )

        with pytest.raises(AccountOwnsCasesError) as refused:
            await DatabaseUserStore(repository).delete_user("user_123")

        assert refused.value.user_id == "user_123"
        assert isinstance(refused.value.__cause__, IntegrityError)

    async def test_any_other_failure_is_still_false(self):
        repository = MagicMock()
        repository.delete = AsyncMock(side_effect=RuntimeError("db down"))

        assert await DatabaseUserStore(repository).delete_user("user_123") is False
