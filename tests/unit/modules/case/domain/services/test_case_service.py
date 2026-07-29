"""Tests for CaseService - case lifecycle, access control, conversation management."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id
from faultmaven.exceptions import ServiceException, ValidationException
from faultmaven.models.api_models import CaseListFilter, CaseMessage, CaseSearchRequest
from faultmaven.modules.case.domain.models import Case, CaseState, MessageType
from faultmaven.modules.case.domain.services.case_service import CaseService


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    """Reset the request-bound org contextvar after each test.

    ``create_case`` now stamps the case with ``get_current_org_id()`` (P2c), so
    a test that sets a tenant org must not leak it into the next test.
    """
    yield
    set_current_org_id(STANDALONE_ORG_ID)


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
    async def test_stamps_org_from_request_context(self, service, mock_repo):
        # P2c: the case is stamped with the request-bound org (tenant_scope
        # middleware -> config.tenant_context). In multi-tenant mode this is the
        # caller's verified JWT org; the write stamp must match it, not fall back
        # to the Standalone/default org.
        mock_repo.list.return_value = ([], 0)
        tenant_org = "11111111-1111-1111-1111-111111111111"
        set_current_org_id(tenant_org)
        case = await service.create_case(title="Scoped", owner_id="user_123")
        assert case.organization_id == tenant_org

    @pytest.mark.asyncio
    async def test_defaults_org_to_standalone(self, service, mock_repo):
        # Single-tenant / unset context -> the Standalone org (contextvar default),
        # so standalone deployments stay scoped without any per-request wiring.
        mock_repo.list.return_value = ([], 0)
        assert get_current_org_id() == STANDALONE_ORG_ID  # sanity: default binding
        case = await service.create_case(title="Default", owner_id="user_123")
        assert case.organization_id == STANDALONE_ORG_ID

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
    async def test_identical_content_from_different_author_persists(
        self, service, mock_repo
    ):
        """Dedup is per-principal: on a team-shared case, a second member
        posting the same adjacent content ("still broken", "+1") is a real
        turn, not a resubmission (#855)."""
        existing_msg = {
            "role": "user",
            "content": "still broken",
            "author_id": "user_alice",
        }
        case = _make_case(current_turn=1, messages=[existing_msg])
        mock_repo.get.return_value = case
        msg = CaseMessage(
            message_id="msg_test457",
            case_id="case_abc123abc123",
            turn_number=1,
            role="user",
            content="still broken",
            author_id="user_bob",
            created_at=datetime.now(timezone.utc),
        )
        result = await service.add_message_to_case("case_abc123abc123", msg)
        assert result is True
        mock_repo.add_message.assert_awaited_once()
        assert case.current_turn == 2  # a real turn increments

    @pytest.mark.asyncio
    async def test_identical_content_from_same_author_dedupes(self, service, mock_repo):
        existing_msg = {
            "role": "user",
            "content": "still broken",
            "author_id": "user_alice",
        }
        case = _make_case(current_turn=1, messages=[existing_msg])
        mock_repo.get.return_value = case
        msg = CaseMessage(
            message_id="msg_test458",
            case_id="case_abc123abc123",
            turn_number=1,
            role="user",
            content="still broken",
            author_id="user_alice",
            created_at=datetime.now(timezone.utc),
        )
        result = await service.add_message_to_case("case_abc123abc123", msg)
        assert result is True
        mock_repo.add_message.assert_not_awaited()
        assert case.current_turn == 1  # deduped turn does not increment

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
    async def test_returns_case_summaries_and_total(self, service, mock_repo):
        cases = [_make_case(current_turn=1)]
        mock_repo.list.return_value = (cases, 1)
        summaries, total = await service.list_user_cases("user_123")
        assert len(summaries) == 1
        # total is the repository's true match count, not the page length.
        assert total == 1

    @pytest.mark.asyncio
    async def test_total_is_repo_total_not_page_length(self, service, mock_repo):
        # Repository returns a single page (2 rows) but reports a larger total.
        page = [_make_case(current_turn=1), _make_case(current_turn=1)]
        mock_repo.list.return_value = (page, 17)
        summaries, total = await service.list_user_cases(
            "user_123", CaseListFilter(limit=2, offset=0)
        )
        assert len(summaries) == 2
        assert total == 17

    @pytest.mark.asyncio
    async def test_forwards_pagination_and_include_empty_to_repo(
        self, service, mock_repo
    ):
        # include_empty filtering is pushed into the repository query (SQL),
        # NOT applied as a Python post-filter — so the service must forward
        # limit/offset/include_empty and trust the repo's page + total.
        mock_repo.list.return_value = ([], 0)
        filters = CaseListFilter(include_empty=False, limit=25, offset=50)
        await service.list_user_cases("user_123", filters=filters)
        kwargs = mock_repo.list.await_args.kwargs
        assert kwargs["include_empty"] is False
        assert kwargs["limit"] == 25
        assert kwargs["offset"] == 50

    @pytest.mark.asyncio
    async def test_no_python_post_filter_of_empty_cases(self, service, mock_repo):
        # The repo (with include_empty pushed down) returns exactly the rows the
        # service should surface. The service must NOT re-filter empties itself
        # (a post-slice filter would disagree with the repo's total).
        active = [_make_case(current_turn=3)]
        mock_repo.list.return_value = (active, 1)
        summaries, total = await service.list_user_cases(
            "user_123", CaseListFilter(include_empty=False)
        )
        assert len(summaries) == 1
        assert total == 1

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


def _team_share_service(mock_repo, mock_session_store, *, teams):
    """CaseService wired for Cloud team sharing (team_service + share_repository)."""
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


class TestCaseTeamShareEndpoints:
    """User-initiated case→Team share/unshare (ADR-013 §D4, U12 backend).

    Owner-only, membership-checked, Cloud-only. Replaces the retired per-user
    participant share.
    """

    @pytest.mark.asyncio
    async def test_share_success_writes_share_row(self, mock_repo, mock_session_store):
        case = _make_case(user_id="owner", organization_id="org_1")
        mock_repo.get = AsyncMock(return_value=case)
        svc, team_service, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        await svc.share_case_with_team(case.case_id, "team_a", "owner")
        share_repo.share.assert_awaited_once_with(
            resource_type="case",
            resource_id=case.case_id,
            scope_type="team",
            scope_id="team_a",
            organization_id="org_1",
            created_by="owner",
        )

    @pytest.mark.asyncio
    async def test_share_rejected_for_non_owner(self, mock_repo, mock_session_store):
        case = _make_case(user_id="owner")
        mock_repo.get = AsyncMock(return_value=case)
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        with pytest.raises(ValidationException, match="owner"):
            await svc.share_case_with_team(case.case_id, "team_a", "intruder")
        share_repo.share.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_share_rejected_for_non_member_team(
        self, mock_repo, mock_session_store
    ):
        case = _make_case(user_id="owner")
        mock_repo.get = AsyncMock(return_value=case)
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        with pytest.raises(ValidationException, match="team you belong to"):
            await svc.share_case_with_team(case.case_id, "team_other", "owner")
        share_repo.share.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_share_missing_case_raises(self, mock_repo, mock_session_store):
        mock_repo.get = AsyncMock(return_value=None)
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        with pytest.raises(ValidationException, match="not found"):
            await svc.share_case_with_team("missing", "team_a", "owner")
        share_repo.share.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_share_not_available_in_standalone(self, service, mock_repo):
        """No team_service (standalone) → clear 'not available', never a no-op."""
        with pytest.raises(ValidationException, match="not available"):
            await service.share_case_with_team("c1", "team_a", "owner")

    @pytest.mark.asyncio
    async def test_unshare_success_returns_true(self, mock_repo, mock_session_store):
        case = _make_case(user_id="owner")
        mock_repo.get = AsyncMock(return_value=case)
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        share_repo.unshare = AsyncMock(return_value=True)
        assert await svc.unshare_case_from_team(case.case_id, "team_a", "owner") is True
        share_repo.unshare.assert_awaited_once_with(
            resource_type="case",
            resource_id=case.case_id,
            scope_type="team",
            scope_id="team_a",
        )

    @pytest.mark.asyncio
    async def test_unshare_returns_false_when_not_shared(
        self, mock_repo, mock_session_store
    ):
        case = _make_case(user_id="owner")
        mock_repo.get = AsyncMock(return_value=case)
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        share_repo.unshare = AsyncMock(return_value=False)
        assert (
            await svc.unshare_case_from_team(case.case_id, "team_a", "owner") is False
        )

    @pytest.mark.asyncio
    async def test_unshare_rejected_for_non_owner(self, mock_repo, mock_session_store):
        case = _make_case(user_id="owner")
        mock_repo.get = AsyncMock(return_value=case)
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        with pytest.raises(ValidationException, match="owner"):
            await svc.unshare_case_from_team(case.case_id, "team_a", "intruder")
        share_repo.unshare.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unshare_not_available_in_standalone(self, service):
        with pytest.raises(ValidationException, match="not available"):
            await service.unshare_case_from_team("c1", "team_a", "owner")


class TestCaseTeamShareReads:
    """Read-side enrichment: a case's shared team ids on the DTOs."""

    @pytest.mark.asyncio
    async def test_get_case_team_ids_filters_to_team_scope(
        self, mock_repo, mock_session_store
    ):
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=[]
        )
        # Delegates to the batched resolver (single-id map).
        share_repo.list_scopes_for_resources = AsyncMock(
            return_value={
                "c1": [
                    MagicMock(scope_type="team", scope_id="team_a"),
                    MagicMock(scope_type="organization", scope_id="org_1"),
                    MagicMock(scope_type="team", scope_id="team_b"),
                ]
            }
        )
        assert await svc.get_case_team_ids("c1") == ["team_a", "team_b"]

    @pytest.mark.asyncio
    async def test_get_case_team_ids_empty_in_standalone(self, service):
        assert await service.get_case_team_ids("c1") == []

    @pytest.mark.asyncio
    async def test_enrich_summaries_sets_shared_team_ids(
        self, mock_repo, mock_session_store
    ):
        from faultmaven.models.api_models import CaseSummary

        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=[]
        )
        share_repo.list_scopes_for_resources = AsyncMock(
            return_value={
                "c1": [MagicMock(scope_type="team", scope_id="team_a")],
            }
        )
        s1 = CaseSummary.from_case(_make_case(title="A"))
        object.__setattr__(s1, "case_id", "c1")
        s2 = CaseSummary.from_case(_make_case(title="B"))
        object.__setattr__(s2, "case_id", "c2")
        await svc._enrich_summaries_with_team_shares([s1, s2])
        assert s1.shared_team_ids == ["team_a"]
        assert s2.shared_team_ids == []  # no shares → empty

    @pytest.mark.asyncio
    async def test_enrich_summaries_best_effort_on_error(
        self, mock_repo, mock_session_store
    ):
        from faultmaven.models.api_models import CaseSummary

        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=[]
        )
        share_repo.list_scopes_for_resources = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        s1 = CaseSummary.from_case(_make_case(title="A"))
        await svc._enrich_summaries_with_team_shares([s1])  # must not raise
        assert s1.shared_team_ids == []


class TestCaseTeamFilter:
    """Filter-by-team facet on list/search (a narrowing, not the visibility arm)."""

    @pytest.mark.asyncio
    async def test_resolve_team_filter_requires_membership(
        self, mock_repo, mock_session_store
    ):
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        share_repo.list_resource_ids = AsyncMock(return_value=["c1", "c2"])
        # Member of team_a → resolves shared ids.
        assert await svc._resolve_team_filter_case_ids("u", "team_a") == ["c1", "c2"]
        # Not a member of team_x → empty (can't filter by a foreign team).
        assert await svc._resolve_team_filter_case_ids("u", "team_x") == []

    @pytest.mark.asyncio
    async def test_resolve_team_filter_empty_in_standalone(self, service):
        assert await service._resolve_team_filter_case_ids("u", "team_a") == []

    @pytest.mark.asyncio
    async def test_list_with_team_filter_passes_restrict_ids(
        self, mock_repo, mock_session_store
    ):
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        share_repo.list_resource_ids = AsyncMock(return_value=["c1"])
        share_repo.list_scopes_for_resources = AsyncMock(return_value={})
        mock_repo.list = AsyncMock(return_value=([], 0))
        await svc.list_user_cases("u", CaseListFilter(team_id="team_a"))
        assert mock_repo.list.await_args.kwargs["restrict_case_ids"] == ["c1"]

    @pytest.mark.asyncio
    async def test_list_with_empty_team_filter_short_circuits(
        self, mock_repo, mock_session_store
    ):
        """Filtering by a team with no shares (or as a non-member) returns [] and
        never queries the repository."""
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        share_repo.list_resource_ids = AsyncMock(return_value=[])
        mock_repo.list = AsyncMock(return_value=([], 0))
        assert await svc.list_user_cases("u", CaseListFilter(team_id="team_a")) == (
            [],
            0,
        )
        mock_repo.list.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_with_team_filter_passes_restrict_ids(
        self, mock_repo, mock_session_store
    ):
        svc, _, share_repo = _team_share_service(
            mock_repo, mock_session_store, teams=["team_a"]
        )
        share_repo.list_resource_ids = AsyncMock(return_value=["c1"])
        mock_repo.search = AsyncMock(return_value=([], 0))
        await svc.search_cases(
            CaseSearchRequest(query="db", team_id="team_a"), user_id="u"
        )
        assert mock_repo.search.await_args.kwargs["restrict_case_ids"] == ["c1"]


# ============================================================
# close_case (#915)
# ============================================================


class TestCloseCase:
    """User-initiated close routes through the engine's terminal executor.

    One closure rule for the REST and chat surfaces: engine-derived
    closure_reason, closed_at stamped, action-history entry — set
    atomically (the pre-#915 route mutated ``case.state`` directly and
    was rejected by the terminal-state validator).
    """

    @pytest.mark.asyncio
    async def test_closes_inquiry_case_with_derived_reason(self, service, mock_repo):
        case = _make_case(user_id="user_123", state=CaseState.INQUIRY)
        mock_repo.get.return_value = case

        closed = await service.close_case(case.case_id, "user_123")

        assert closed.state == CaseState.CLOSED
        assert closed.closed_at is not None
        assert closed.closure_reason == "inquiry_only"
        assert closed.action_history[-1].to_state == CaseState.CLOSED
        assert closed.action_history[-1].triggered_by == "user_123"
        mock_repo.save.assert_awaited()

    @pytest.mark.asyncio
    async def test_closes_investigating_case_with_derived_reason(
        self, service, mock_repo
    ):
        case = _make_case(user_id="user_123", state=CaseState.INVESTIGATING)
        mock_repo.get.return_value = case

        closed = await service.close_case(case.case_id, "user_123")

        assert closed.state == CaseState.CLOSED
        assert closed.closure_reason == "closed_after_investigation"

    @pytest.mark.asyncio
    async def test_insufficient_evidence_close_keeps_honest_reason(
        self, service, mock_repo
    ):
        from faultmaven.modules.case.domain.models import VerificationStatus

        case = _make_case(user_id="user_123", state=CaseState.INVESTIGATING)
        case.progress.verification_status = VerificationStatus.INSUFFICIENT_EVIDENCE
        mock_repo.get.return_value = case

        closed = await service.close_case(case.case_id, "user_123")

        assert closed.closure_reason == "closed_insufficient_evidence"

    @pytest.mark.asyncio
    async def test_already_terminal_case_conflicts(self, service, mock_repo):
        from faultmaven.exceptions import ConflictError

        for terminal in (CaseState.CLOSED, CaseState.RESOLVED):
            case = _make_case(user_id="user_123", state=terminal)
            mock_repo.get.return_value = case

            with pytest.raises(ConflictError):
                await service.close_case(case.case_id, "user_123")

    @pytest.mark.asyncio
    async def test_concurrent_terminal_transition_conflicts(self, service, mock_repo):
        """The retry mutator re-checks terminal state on each fresh load: a
        close that lost the race to another terminal transition conflicts
        instead of silently re-closing."""
        from faultmaven.exceptions import ConflictError

        open_case = _make_case(user_id="user_123", state=CaseState.INQUIRY)
        closed_meanwhile = _make_case(user_id="user_123", state=CaseState.CLOSED)
        # First get() serves the service's pre-check; the second serves
        # update_case_with_retry's fresh load.
        mock_repo.get.side_effect = [open_case, closed_meanwhile]

        with pytest.raises(ConflictError):
            await service.close_case(open_case.case_id, "user_123")
        mock_repo.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_owner_gets_not_found(self, service, mock_repo):
        """404-not-403 posture: existence is not disclosed to non-owners."""
        from faultmaven.exceptions import NotFoundError

        case = _make_case(user_id="someone_else")
        mock_repo.get.return_value = case

        with pytest.raises(NotFoundError):
            await service.close_case(case.case_id, "user_123")
        mock_repo.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_case_not_found(self, service, mock_repo):
        from faultmaven.exceptions import NotFoundError

        mock_repo.get.return_value = None

        with pytest.raises(NotFoundError):
            await service.close_case("case_missing000", "user_123")

    @pytest.mark.asyncio
    async def test_deleted_mid_close_maps_to_not_found(self, service, mock_repo):
        """CaseNotFoundError from the retry helper (deleted between the
        pre-check and the fresh load) is translated to the handled
        NotFoundError → 404, not an unmapped CaseException → 500."""
        from faultmaven.exceptions import NotFoundError

        open_case = _make_case(user_id="user_123", state=CaseState.INQUIRY)
        mock_repo.get.side_effect = [open_case, None]

        with pytest.raises(NotFoundError):
            await service.close_case(open_case.case_id, "user_123")

    @pytest.mark.asyncio
    async def test_occ_exhaustion_maps_to_conflict(self, service, mock_repo):
        """StaleCaseException after max retries is translated to the handled
        ConflictError → 409 (mirrors the turn route's OCC posture)."""
        from faultmaven.exceptions import ConflictError
        from faultmaven.modules.case.exceptions import StaleCaseException

        def _fresh_open(_case_id):
            return _make_case(user_id="user_123", state=CaseState.INQUIRY)

        mock_repo.get.side_effect = lambda case_id: _fresh_open(case_id)
        mock_repo.save.side_effect = StaleCaseException(
            case_id="case_x", expected_version=1, actual_version=2
        )

        with pytest.raises(ConflictError) as exc_info:
            await service.close_case("case_x", "user_123")
        assert exc_info.value.conflict_reason == "concurrent_update"
