"""Tests for CaseService - case lifecycle, access control, conversation management."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import ServiceException, ValidationException
from faultmaven.models.api_models import CaseListFilter, CaseMessage, CaseSearchRequest
from faultmaven.modules.case.domain.models import Case, CaseState, MessageType
from faultmaven.modules.case.domain.services.case_service import CaseService


def _make_case(
    user_id="user_123",
    state=CaseState.INQUIRY,
    current_turn=1,
    messages=None,
    **kwargs,
):
    """Helper to build a Case with valid defaults."""
    case = Case(
        title=kwargs.get("title", "Test Case"),
        user_id=user_id,
        organization_id=kwargs.get("organization_id", "org_default"),
        description=kwargs.get("description", "test description"),
    )
    # Use object.__setattr__ to bypass Pydantic cross-field validators
    object.__setattr__(case, "state", state)
    object.__setattr__(case, "current_turn", current_turn)
    if messages is not None:
        object.__setattr__(case, "messages", messages)
    return case


@pytest.fixture
def mock_repo():
    repo = AsyncMock()
    repo.save = AsyncMock(side_effect=lambda case: case)
    repo.get = AsyncMock(return_value=None)
    repo.delete = AsyncMock(return_value=True)
    repo.list = AsyncMock(return_value=([], 0))
    repo.search = AsyncMock(return_value=([], 0))
    repo.add_message = AsyncMock(return_value=True)
    repo.get_messages = AsyncMock(return_value=[])
    repo.update_activity_timestamp = AsyncMock()
    repo.update_metadata_fields = AsyncMock(return_value=True)
    repo.get_analytics = AsyncMock(return_value={})
    repo.cleanup_expired = AsyncMock(return_value=0)
    repo.count_user_cases_on_date = AsyncMock(return_value=0)
    return repo


@pytest.fixture
def mock_session_store():
    store = AsyncMock()
    store.get = AsyncMock(return_value=None)
    store.set = AsyncMock()
    store.increment_counter = AsyncMock(return_value=1)
    return store


@pytest.fixture
def service(mock_repo, mock_session_store):
    return CaseService(
        case_repository=mock_repo,
        session_store=mock_session_store,
        max_cases_per_user=50,
    )


# ============================================================
# create_case
# ============================================================


class TestCreateCase:
    """Test case creation: validation, title auto-generation, case limits."""

    @pytest.mark.asyncio
    async def test_creates_case_with_explicit_title(self, service, mock_repo):
        mock_repo.list.return_value = ([], 0)
        case = await service.create_case(
            title="API Outage",
            description="Services down",
            owner_id="user_123",
        )
        assert case.title == "API Outage"
        assert case.user_id == "user_123"
        assert case.description == "Services down"
        mock_repo.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_empty_owner_id(self, service):
        with pytest.raises(ValidationException, match="Owner ID is required"):
            await service.create_case(title="Test", owner_id="")

    @pytest.mark.asyncio
    async def test_rejects_none_owner_id(self, service):
        with pytest.raises(ValidationException, match="Owner ID is required"):
            await service.create_case(title="Test", owner_id=None)

    @pytest.mark.asyncio
    async def test_enforces_case_limit(self, service, mock_repo):
        # Return max_cases_per_user active cases
        active_cases = [_make_case(state=CaseState.INQUIRY) for _ in range(50)]
        mock_repo.list.return_value = (active_cases, 50)

        with pytest.raises(ValidationException, match="maximum case limit"):
            await service.create_case(title="One more", owner_id="user_123")

    @pytest.mark.asyncio
    async def test_resolved_cases_dont_count_toward_limit(self, service, mock_repo):
        # 50 resolved cases should NOT block creation
        resolved = [_make_case(state=CaseState.RESOLVED) for _ in range(50)]
        mock_repo.list.return_value = (resolved, 50)

        case = await service.create_case(title="New case", owner_id="user_123")
        assert case.title == "New case"

    @pytest.mark.asyncio
    async def test_rejects_title_over_200_chars(self, service, mock_repo):
        mock_repo.list.return_value = ([], 0)
        with pytest.raises(ValidationException, match="200 characters"):
            await service.create_case(title="X" * 201, owner_id="user_123")

    @pytest.mark.asyncio
    async def test_auto_generates_title_when_none(self, service, mock_repo):
        mock_repo.list.return_value = ([], 0)
        case = await service.create_case(title=None, owner_id="user_123")
        assert case.title.startswith("Case-")

    @pytest.mark.asyncio
    async def test_stamps_source_from_argument(self, service, mock_repo):
        # Origin (ADR-012) is stamped at creation; the route derives it from the
        # creator's account_kind and passes it here.
        mock_repo.list.return_value = ([], 0)
        slack = await service.create_case(title="T", owner_id="u", source="slack")
        assert slack.source == "slack"
        default = await service.create_case(title="T2", owner_id="u")
        assert default.source == "copilot"

    @pytest.mark.asyncio
    async def test_adds_initial_message(self, service, mock_repo):
        mock_repo.list.return_value = ([], 0)
        case = await service.create_case(
            title="Test",
            owner_id="user_123",
            initial_message="My service is down",
        )
        assert len(case.messages) == 1
        assert case.messages[0]["content"] == "My service is down"
        assert case.messages[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_links_session_on_create(
        self, service, mock_repo, mock_session_store
    ):
        mock_repo.list.return_value = ([], 0)
        await service.create_case(
            title="Test",
            owner_id="user_123",
            session_id="sess_abc",
        )
        mock_session_store.set.assert_awaited()


# ============================================================
# get_case
# ============================================================


class TestGetCase:
    """Test case retrieval with access control."""

    @pytest.mark.asyncio
    async def test_returns_case_for_owner(self, service, mock_repo):
        case = _make_case(user_id="user_123")
        mock_repo.get.return_value = case
        result = await service.get_case("case_abc123abc123", user_id="user_123")
        assert result == case

    @pytest.mark.asyncio
    async def test_denies_access_to_non_owner(self, service, mock_repo):
        case = _make_case(user_id="user_123")
        mock_repo.get.return_value = case
        result = await service.get_case("case_abc123abc123", user_id="other_user")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_case(self, service, mock_repo):
        mock_repo.get.return_value = None
        result = await service.get_case("case_nonexistent1")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_id(self, service):
        result = await service.get_case("")
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_access_check_when_no_user_id(self, service, mock_repo):
        case = _make_case(user_id="user_123")
        mock_repo.get.return_value = case
        result = await service.get_case("case_abc123abc123", user_id=None)
        assert result == case


# ============================================================
# update_case
# ============================================================


class TestUpdateCase:
    """Test case updates with validation and access control."""

    @pytest.mark.asyncio
    async def test_metadata_only_updates_skip_versioned_save(self, service, mock_repo):
        """Title/description go through the scoped metadata path — no OCC.

        Writing cosmetic labels through ``save(case)`` would bump
        ``cases.version`` and stale-conflict a concurrent turn save.
        """
        case = _make_case()
        mock_repo.get.return_value = case
        result = await service.update_case(
            "case_abc123abc123",
            {"title": "New Title", "description": "Updated"},
        )
        assert result is True
        mock_repo.update_metadata_fields.assert_awaited_once_with(
            "case_abc123abc123", title="New Title", description="Updated"
        )
        mock_repo.save.assert_not_awaited()

    @staticmethod
    def _make_closed_case():
        """Build a CLOSED case with all closure-required fields set.

        Pydantic cross-field validators require CLOSED status to carry
        ``closed_at`` and ``closure_reason``. We bypass them via
        ``object.__setattr__`` for test setup.
        """
        case = _make_case()
        future = datetime.now(timezone.utc) + timedelta(seconds=1)
        object.__setattr__(case, "state", CaseState.CLOSED)
        object.__setattr__(case, "closed_at", future)
        object.__setattr__(case, "closure_reason", "inquiry_only")
        return case

    @pytest.mark.asyncio
    async def test_state_updates_go_through_versioned_save(self, service, mock_repo):
        """Status / closure_reason still use the versioned save with OCC retry."""
        mock_repo.get.return_value = self._make_closed_case()
        result = await service.update_case(
            "case_abc123abc123",
            {"closure_reason": "closed_after_investigation"},
        )
        assert result is True
        mock_repo.save.assert_awaited()
        mock_repo.update_metadata_fields.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mixed_updates_use_versioned_save(self, service, mock_repo):
        """If any state field is present, the whole update is versioned."""
        mock_repo.get.return_value = self._make_closed_case()
        result = await service.update_case(
            "case_abc123abc123",
            {"title": "New", "closure_reason": "closed_after_investigation"},
        )
        assert result is True
        mock_repo.save.assert_awaited()
        mock_repo.update_metadata_fields.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_empty_case_id(self, service):
        with pytest.raises(ValidationException, match="Case ID cannot be empty"):
            await service.update_case("", {"title": "X"})

    @pytest.mark.asyncio
    async def test_rejects_empty_updates(self, service):
        with pytest.raises(ValidationException, match="Updates cannot be empty"):
            await service.update_case("case_abc123abc123", {})

    @pytest.mark.asyncio
    async def test_returns_false_for_missing_case(self, service, mock_repo):
        mock_repo.get.return_value = None
        result = await service.update_case("case_abc123abc123", {"title": "X"})
        assert result is False


# ============================================================
# add_message_to_case
# ============================================================


class TestAddMessageToCase:
    """Test message addition: deduplication, turn numbering, persistence."""

    @pytest.mark.asyncio
    async def test_adds_message_successfully(self, service, mock_repo):
        case = _make_case(current_turn=0, messages=[])
        mock_repo.get.return_value = case
        msg = CaseMessage(
            message_id="msg_test123",
            case_id="case_abc123abc123",
            turn_number=0,
            role="user",
            content="Help with my issue",
            created_at=datetime.now(timezone.utc),
        )
        result = await service.add_message_to_case("case_abc123abc123", msg)
        assert result is True
        mock_repo.add_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deduplicates_identical_messages(self, service, mock_repo):
        existing_msg = {
            "role": "user",
            "content": "Help with my issue",
        }
        case = _make_case(current_turn=1, messages=[existing_msg])
        mock_repo.get.return_value = case
        msg = CaseMessage(
            message_id="msg_test456",
            case_id="case_abc123abc123",
            turn_number=1,
            role="user",
            content="Help with my issue",
            created_at=datetime.now(timezone.utc),
        )
        result = await service.add_message_to_case("case_abc123abc123", msg)
        assert result is True
        # Should NOT call add_message because it's a duplicate
        mock_repo.add_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_increments_turn_for_user_message(self, service, mock_repo):
        case = _make_case(current_turn=2, messages=[])
        mock_repo.get.return_value = case
        msg = CaseMessage(
            message_id="msg_test789",
            case_id="case_abc123abc123",
            turn_number=0,
            role="user",
            content="New question",
            created_at=datetime.now(timezone.utc),
        )
        await service.add_message_to_case("case_abc123abc123", msg)
        assert case.current_turn == 3  # incremented from 2

    @pytest.mark.asyncio
    async def test_rejects_missing_case(self, service, mock_repo):
        mock_repo.get.return_value = None
        msg = CaseMessage(
            message_id="msg_test000",
            case_id="case_nonexistent1",
            turn_number=0,
            role="user",
            content="Hello",
            created_at=datetime.now(timezone.utc),
        )
        with pytest.raises(ValidationException, match="not found"):
            await service.add_message_to_case("case_nonexistent1", msg)


# ============================================================
# get_or_create_case_for_session
# ============================================================


class TestGetOrCreateCaseForSession:
    """Test session-to-case resolution: reuse existing or create new."""

    @pytest.mark.asyncio
    async def test_returns_existing_case_from_session(
        self, service, mock_repo, mock_session_store
    ):
        case = _make_case(user_id="user_123")
        mock_session_store.get.return_value = case.case_id
        mock_repo.get.return_value = case

        result = await service.get_or_create_case_for_session(
            session_id="sess_abc", user_id="user_123"
        )
        assert result == case.case_id

    @pytest.mark.asyncio
    async def test_creates_new_case_when_no_existing(
        self, service, mock_repo, mock_session_store
    ):
        mock_session_store.get.return_value = None
        mock_repo.list.return_value = ([], 0)

        result = await service.get_or_create_case_for_session(
            session_id="sess_new", user_id="user_123"
        )
        assert result.startswith("case_")

    @pytest.mark.asyncio
    async def test_creates_new_when_force_new(
        self, service, mock_repo, mock_session_store
    ):
        mock_repo.list.return_value = ([], 0)
        result = await service.get_or_create_case_for_session(
            session_id="sess_abc", user_id="user_123", force_new=True
        )
        # Should NOT check session store when force_new=True
        assert result.startswith("case_")

    @pytest.mark.asyncio
    async def test_rejects_empty_session_id(self, service):
        with pytest.raises(ValidationException, match="Session ID cannot be empty"):
            await service.get_or_create_case_for_session(session_id="")


# ============================================================
# hard_delete_case
# ============================================================


class TestHardDeleteCase:
    """Test case deletion: access control, idempotency, Working Memory cleanup."""

    @pytest.mark.asyncio
    async def test_deletes_case_as_owner(self, service, mock_repo):
        case = _make_case(user_id="user_123")
        mock_repo.get.return_value = case
        result = await service.hard_delete_case("case_abc123abc123", user_id="user_123")
        assert result is True
        mock_repo.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idempotent_for_missing_case(self, service, mock_repo):
        mock_repo.get.return_value = None
        result = await service.hard_delete_case("case_nonexistent1", user_id="user_123")
        assert result is True  # Idempotent behavior

    @pytest.mark.asyncio
    async def test_cleans_up_vector_store(self, service, mock_repo):
        case = _make_case(user_id="user_123")
        mock_repo.get.return_value = case
        mock_vector_store = AsyncMock()
        service.case_vector_store = mock_vector_store

        await service.hard_delete_case("case_abc123abc123", user_id="user_123")
        mock_vector_store.delete_case_collection.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_empty_case_id(self, service):
        with pytest.raises(ValidationException, match="Case ID cannot be empty"):
            await service.hard_delete_case("")


# ============================================================
# get_case_conversation_context
# ============================================================


class TestGetConversationContext:
    """Test conversation context formatting for LLM consumption."""

    @pytest.mark.asyncio
    async def test_formats_conversation_for_llm(self, service, mock_repo):
        mock_repo.get_messages.return_value = [
            {
                "role": "user",
                "content": "My service is crashing",
                "created_at": "2024-01-01T12:00:00+00:00",
            },
            {
                "role": "assistant",
                "content": "Let me investigate the crash logs. " + "X" * 300,
                "created_at": "2024-01-01T12:01:00+00:00",
            },
            {
                "role": "user",
                "content": "Here are the logs",
                "created_at": "2024-01-01T12:02:00+00:00",
            },
        ]
        context = await service.get_case_conversation_context("case_abc123abc123")
        assert "Previous conversation" in context
        assert "User: My service is crashing" in context
        # Agent responses should be truncated at 200 chars
        assert "..." in context
        assert "Current query:" in context

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_messages(self, service, mock_repo):
        mock_repo.get_messages.return_value = []
        context = await service.get_case_conversation_context("case_abc123abc123")
        assert context == ""

    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_case_id(self, service):
        context = await service.get_case_conversation_context("")
        assert context == ""


# ============================================================
# list_user_cases
# ============================================================


class TestListUserCases:
    """Test case listing with filters: empty cases, archived, status."""

    @pytest.mark.asyncio
    async def test_returns_case_summaries(self, service, mock_repo):
        cases = [_make_case(current_turn=1)]
        mock_repo.list.return_value = (cases, 1)
        result = await service.list_user_cases("user_123")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_excludes_empty_cases_by_default(self, service, mock_repo):
        empty_case = _make_case(current_turn=0)
        active_case = _make_case(current_turn=3)
        mock_repo.list.return_value = ([empty_case, active_case], 2)

        filters = CaseListFilter(include_empty=False)
        result = await service.list_user_cases("user_123", filters=filters)
        assert len(result) == 1

    # test_excludes_archived_cases_by_default removed: archive feature dropped
    # in the schema redesign (commit 7b5a1b93). Will be reintroduced as a
    # deliberate epic with retention policy, scheduled archival, and list-view
    # filter UI. Reintroduce the test alongside that feature.

    @pytest.mark.asyncio
    async def test_rejects_empty_user_id(self, service):
        with pytest.raises(ValidationException, match="User ID cannot be empty"):
            await service.list_user_cases("")


# ============================================================
# search_cases
# ============================================================


class TestSearchCases:
    """Test case search with user filtering."""

    @pytest.mark.asyncio
    async def test_search_returns_matching_cases(self, service, mock_repo):
        case = _make_case(user_id="user_123", current_turn=1)
        mock_repo.search.return_value = ([case], 1)
        request = CaseSearchRequest(query="API outage")
        result = await service.search_cases(request, user_id="user_123")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_search_scopes_by_user_via_repository(self, service, mock_repo):
        # Scoping is delegated to the repository's SQL (WHERE user_id = …), not
        # a Python post-filter — filtering after the repo's LIMIT would drop the
        # caller's own matches and could leak other users' cases. Assert the
        # user_id (and limit) reach repository.search.
        case_a = _make_case(user_id="user_a", current_turn=1)
        mock_repo.search.return_value = ([case_a], 1)

        request = CaseSearchRequest(query="test")
        result = await service.search_cases(request, user_id="user_a")

        assert len(result) == 1
        assert mock_repo.search.await_args.kwargs["user_id"] == "user_a"
        assert mock_repo.search.await_args.kwargs["limit"] == request.limit

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_error(self, service, mock_repo):
        mock_repo.search.side_effect = RuntimeError("DB error")
        request = CaseSearchRequest(query="test")
        result = await service.search_cases(request)
        assert result == []


# ============================================================
# link_session_to_case
# ============================================================


class TestLinkSessionToCase:
    """Test session-case linking."""

    @pytest.mark.asyncio
    async def test_links_successfully(self, service, mock_repo, mock_session_store):
        case = _make_case()
        mock_repo.get.return_value = case
        result = await service.link_session_to_case("sess_abc", "case_abc123abc123")
        assert result is True
        mock_repo.update_activity_timestamp.assert_awaited()
        mock_session_store.set.assert_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_for_missing_case(self, service, mock_repo):
        mock_repo.get.return_value = None
        result = await service.link_session_to_case("sess_abc", "case_nonexistent1")
        assert result is False

    @pytest.mark.asyncio
    async def test_rejects_missing_ids(self, service):
        with pytest.raises(ValidationException):
            await service.link_session_to_case("", "case_abc123abc123")
        with pytest.raises(ValidationException):
            await service.link_session_to_case("sess_abc", "")


# ============================================================
# list_all_cases (admin cross-tenant read, ADR-012 D9)
# ============================================================


class TestListAllCases:
    """Cross-tenant admin listing: unscoped read, error propagation."""

    @pytest.mark.asyncio
    async def test_lists_across_all_users_unscoped(self, service, mock_repo):
        cases = [
            _make_case(user_id="copilot_user"),
            _make_case(user_id="slack-agent"),
        ]
        mock_repo.list = AsyncMock(return_value=(cases, 2))

        summaries, total = await service.list_all_cases(CaseListFilter())

        # user_id=None → repository drops its per-user WHERE clause.
        assert mock_repo.list.await_args.kwargs["user_id"] is None
        assert total == 2
        assert {s.user_id for s in summaries} == {"copilot_user", "slack-agent"}

    @pytest.mark.asyncio
    async def test_propagates_repository_error(self, service, mock_repo):
        # A DB failure must surface (5xx), not be masked as an empty list —
        # this is a diagnostic admin view.
        mock_repo.list = AsyncMock(side_effect=RuntimeError("db down"))

        with pytest.raises(RuntimeError):
            await service.list_all_cases(CaseListFilter())


# ============================================================
# Case read allowlist — owned ∪ shared-to-my-teams (ADR-013 §D4 / U9)
# ============================================================


class TestCaseReadAllowlist:
    """The ``shared-to-my-teams`` arm of case reads.

    Behavior-neutral until case shares exist (U10): with no team_service /
    share_repository (standalone) the arm resolves empty and every read stays
    owner-only.
    """

    def _shared_service(self, mock_repo, *, teams, shared_ids):
        team_service = AsyncMock()
        team_service.list_all_user_team_ids = AsyncMock(return_value=teams)
        share_repo = AsyncMock()
        share_repo.list_resource_ids = AsyncMock(return_value=shared_ids)
        service = CaseService(
            case_repository=mock_repo,
            team_service=team_service,
            share_repository=share_repo,
        )
        return service, team_service, share_repo

    @pytest.mark.asyncio
    async def test_resolve_empty_without_deps(self, service):
        """Standalone: no team_service/share_repository → empty allowlist."""
        assert await service._resolve_shared_case_ids("user_123") == []

    @pytest.mark.asyncio
    async def test_resolve_empty_without_user(self, mock_repo):
        svc, _, share_repo = self._shared_service(
            mock_repo, teams=["team_1"], shared_ids=["case_x"]
        )
        assert await svc._resolve_shared_case_ids(None) == []
        share_repo.list_resource_ids.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_empty_when_no_teams(self, mock_repo):
        svc, _, share_repo = self._shared_service(
            mock_repo, teams=[], shared_ids=["case_x"]
        )
        assert await svc._resolve_shared_case_ids("user_123") == []
        share_repo.list_resource_ids.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_queries_case_shares(self, mock_repo):
        svc, team_service, share_repo = self._shared_service(
            mock_repo, teams=["team_1", "team_2"], shared_ids=["case_x", "case_y"]
        )
        result = await svc._resolve_shared_case_ids("user_123")
        assert result == ["case_x", "case_y"]
        team_service.list_all_user_team_ids.assert_awaited_once_with("user_123")
        share_repo.list_resource_ids.assert_awaited_once_with(
            resource_type="case", scope_type="team", scope_ids=["team_1", "team_2"]
        )

    @pytest.mark.asyncio
    async def test_resolve_degrades_on_error(self, mock_repo):
        """A share-lookup failure narrows visibility (fail-closed), never raises."""
        team_service = AsyncMock()
        team_service.list_all_user_team_ids = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        share_repo = AsyncMock()
        svc = CaseService(
            case_repository=mock_repo,
            team_service=team_service,
            share_repository=share_repo,
        )
        assert await svc._resolve_shared_case_ids("user_123") == []

    @pytest.mark.asyncio
    async def test_list_threads_shared_ids_to_repo(self, mock_repo):
        svc, _, _ = self._shared_service(
            mock_repo, teams=["team_1"], shared_ids=["case_shared"]
        )
        await svc.list_user_cases("user_123", CaseListFilter())
        assert mock_repo.list.await_args.kwargs["shared_case_ids"] == ["case_shared"]

    @pytest.mark.asyncio
    async def test_search_threads_shared_ids_to_repo(self, mock_repo):
        svc, _, _ = self._shared_service(
            mock_repo, teams=["team_1"], shared_ids=["case_shared"]
        )
        await svc.search_cases(CaseSearchRequest(query="db"), "user_123")
        assert mock_repo.search.await_args.kwargs["shared_case_ids"] == ["case_shared"]

    @pytest.mark.asyncio
    async def test_get_case_allows_team_shared_non_owner(self, mock_repo):
        """A non-owner may read a case shared to one of their teams."""
        case = _make_case(user_id="owner_b")
        mock_repo.get = AsyncMock(return_value=case)
        svc, _, _ = self._shared_service(
            mock_repo, teams=["team_1"], shared_ids=[case.case_id]
        )
        result = await svc.get_case(case.case_id, user_id="user_123")
        assert result is case

    @pytest.mark.asyncio
    async def test_get_case_denies_unshared_non_owner(self, mock_repo):
        case = _make_case(user_id="owner_b")
        mock_repo.get = AsyncMock(return_value=case)
        svc, _, _ = self._shared_service(
            mock_repo, teams=["team_1"], shared_ids=["some_other_case"]
        )
        assert await svc.get_case(case.case_id, user_id="user_123") is None

    @pytest.mark.asyncio
    async def test_get_case_owner_short_circuits_without_share_lookup(self, mock_repo):
        """Owner access must not pay for a share resolution."""
        case = _make_case(user_id="user_123")
        mock_repo.get = AsyncMock(return_value=case)
        svc, team_service, _ = self._shared_service(
            mock_repo, teams=["team_1"], shared_ids=[]
        )
        result = await svc.get_case(case.case_id, user_id="user_123")
        assert result is case
        team_service.list_all_user_team_ids.assert_not_awaited()


# ============================================================
# Case team-share write path + share-creation defaults (U10)
# ============================================================


class TestCaseShareCreation:
    """The write counterpart of the read allowlist (ADR-013 §D3/§D4).

    A Slack-originated case auto-shares to the owner's workspace Team at
    creation; a Copilot case stays personal-until-shared. The mechanism is
    Cloud-only (``team_service`` unwired in standalone) and never blocks case
    creation.
    """

    def _share_write_service(self, mock_repo, mock_session_store, *, teams):
        team_service = AsyncMock()
        team_service.list_all_user_team_ids = AsyncMock(return_value=teams)
        share_repo = AsyncMock()
        service = CaseService(
            case_repository=mock_repo,
            session_store=mock_session_store,
            max_cases_per_user=50,
            team_service=team_service,
            share_repository=share_repo,
        )
        return service, team_service, share_repo

    @pytest.mark.asyncio
    async def test_slack_case_auto_shares_to_workspace_team(
        self, mock_repo, mock_session_store
    ):
        mock_repo.list.return_value = ([], 0)
        svc, team_service, share_repo = self._share_write_service(
            mock_repo, mock_session_store, teams=["team_ws"]
        )
        case = await svc.create_case(
            title="Prod incident", owner_id="slack_svc", source="slack"
        )
        team_service.list_all_user_team_ids.assert_awaited_once_with("slack_svc")
        share_repo.share.assert_awaited_once_with(
            resource_type="case",
            resource_id=case.case_id,
            scope_type="team",
            scope_id="team_ws",
            organization_id=case.organization_id,
            created_by="slack_svc",
        )

    @pytest.mark.asyncio
    async def test_slack_case_shares_to_every_owner_team(
        self, mock_repo, mock_session_store
    ):
        mock_repo.list.return_value = ([], 0)
        svc, _, share_repo = self._share_write_service(
            mock_repo, mock_session_store, teams=["team_a", "team_b"]
        )
        await svc.create_case(title="T", owner_id="slack_svc", source="slack")
        shared_teams = {
            call.kwargs["scope_id"] for call in share_repo.share.await_args_list
        }
        assert shared_teams == {"team_a", "team_b"}

    @pytest.mark.asyncio
    async def test_slack_case_with_no_team_stays_owner_only(
        self, mock_repo, mock_session_store, caplog
    ):
        """A Slack owner resolving to zero teams stays owner-only and is logged
        (cloud misconfiguration), never silently dropped."""
        mock_repo.list.return_value = ([], 0)
        svc, _, share_repo = self._share_write_service(
            mock_repo, mock_session_store, teams=[]
        )
        with caplog.at_level("WARNING"):
            await svc.create_case(title="T", owner_id="slack_svc", source="slack")
        share_repo.share.assert_not_awaited()
        assert "resolved no workspace Team" in caplog.text

    @pytest.mark.asyncio
    async def test_copilot_case_is_personal_until_shared(
        self, mock_repo, mock_session_store
    ):
        mock_repo.list.return_value = ([], 0)
        svc, team_service, share_repo = self._share_write_service(
            mock_repo, mock_session_store, teams=["team_ws"]
        )
        await svc.create_case(title="T", owner_id="u", source="copilot")
        share_repo.share.assert_not_awaited()
        team_service.list_all_user_team_ids.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_source_case_is_not_shared(
        self, mock_repo, mock_session_store
    ):
        """The default (copilot) source takes the personal path."""
        mock_repo.list.return_value = ([], 0)
        svc, _, share_repo = self._share_write_service(
            mock_repo, mock_session_store, teams=["team_ws"]
        )
        await svc.create_case(title="T", owner_id="u")
        share_repo.share.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slack_case_no_op_in_standalone(self, service, mock_repo):
        """Standalone: no team_service wired → a Slack case is created but not
        shared, and nothing raises."""
        mock_repo.list.return_value = ([], 0)
        case = await service.create_case(
            title="T", owner_id="slack_svc", source="slack"
        )
        assert case.source == "slack"
        # ``service`` fixture has no share_repository/team_service — no crash.

    @pytest.mark.asyncio
    async def test_auto_share_failure_does_not_block_creation(
        self, mock_repo, mock_session_store
    ):
        mock_repo.list.return_value = ([], 0)
        svc, team_service, share_repo = self._share_write_service(
            mock_repo, mock_session_store, teams=["team_ws"]
        )
        share_repo.share = AsyncMock(side_effect=RuntimeError("db down"))
        case = await svc.create_case(title="T", owner_id="slack_svc", source="slack")
        # Case still created despite the share failure (fail-safe under-share).
        assert case.source == "slack"

    @pytest.mark.asyncio
    async def test_share_case_with_team_no_op_without_repo(self, service):
        """The write helper is inert when no share repository is wired."""
        await service._share_case_with_team(
            case_id="c1", team_id="t1", organization_id="org", created_by="u"
        )  # must not raise

    @pytest.mark.asyncio
    async def test_hard_delete_cascades_shares(self, mock_repo, mock_session_store):
        case = _make_case(user_id="owner")
        mock_repo.get = AsyncMock(return_value=case)
        mock_repo.delete = AsyncMock(return_value=True)
        svc, _, share_repo = self._share_write_service(
            mock_repo, mock_session_store, teams=[]
        )
        await svc.hard_delete_case(case.case_id, user_id="owner")
        share_repo.delete_for_resource.assert_awaited_once_with("case", case.case_id)

    @pytest.mark.asyncio
    async def test_hard_delete_without_share_repo(self, service, mock_repo):
        """Standalone hard-delete (no share repository) must not raise."""
        case = _make_case(user_id="owner")
        mock_repo.get = AsyncMock(return_value=case)
        mock_repo.delete = AsyncMock(return_value=True)
        assert await service.hard_delete_case(case.case_id, user_id="owner") is True
