"""Tests for turn-based threshold in title generation endpoint.

The threshold gate raises ``ValidationException`` (mapped to HTTP 422
by the global handler in ``api/exception_handlers.py`` — see
``docs/architecture/specifications/exception-contract.md``). Tests
assert on the raised type and ``str(exc)`` rather than a wrapped HTTP
response shape; HTTP translation happens at the app boundary.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes import generate_case_title
from faultmaven.modules.case.contracts import CaseDTO


class TestTurnThreshold:
    """Test turn-based threshold for title generation"""

    @pytest.fixture
    def mock_request(self):
        """Mock FastAPI request with LLM provider"""
        request = MagicMock()
        request.app.state.llm_provider = AsyncMock()
        request.app.state.llm_provider.generate = AsyncMock(
            return_value=LLMResponse(
                content="Database Connection Issues",
                confidence=0.95,
                provider="test",
                model="test-model",
                tokens_used=10,
                response_time_ms=100,
            )
        )
        return request

    @pytest.fixture
    def mock_response(self):
        """Mock FastAPI response"""
        response = MagicMock()
        response.headers = {}
        return response

    @pytest.fixture
    def mock_user(self):
        """Mock authenticated user"""
        return UserDTO(
            user_id="user_123",
            username="testuser",
            email="test@example.com",
            display_name="Test User",
            is_active=True,
        )

    @pytest.fixture
    def mock_case(self):
        """Mock case with generic title"""
        case = MagicMock()
        case.case_id = "case_123"
        case.title = "Case-240101-1"  # Generic title
        case.description = "Test case"
        case.user_id = "user_123"
        case.status = "open"
        case.is_terminal = False
        case.message_count = 0
        return case

    @pytest.mark.asyncio
    async def test_insufficient_turns_returns_422(
        self, mock_request, mock_response, mock_user, mock_case
    ):
        """Should return 422 when turn count < 5"""
        mock_case_service = AsyncMock()
        mock_case_service.get_case = AsyncMock(return_value=mock_case)

        # Only 3 user turns (below threshold of 5)
        mock_case_service.repository.get_messages = AsyncMock(
            return_value=[
                {"role": "user", "content": "Message 1"},
                {"role": "assistant", "content": "Response 1"},
                {"role": "user", "content": "Message 2"},
                {"role": "assistant", "content": "Response 2"},
                {"role": "user", "content": "Message 3"},
            ]
        )

        with pytest.raises(ValidationException) as exc_info:
            await generate_case_title(
                case_id="case_123",
                request=mock_request,
                response=mock_response,
                request_body=None,
                force=False,
                case_service=mock_case_service,
                current_user=mock_user,
            )

        assert "Need at least 5 conversation turns" in str(exc_info.value)
        assert "currently 3 turns" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exactly_5_turns_passes_threshold(
        self, mock_request, mock_response, mock_user, mock_case
    ):
        """Should pass threshold when turn count = 5 (minimum) - test only threshold check"""
        mock_case_service = AsyncMock()
        mock_case_service.get_case = AsyncMock(return_value=mock_case)

        # Exactly 5 user turns (meets threshold)
        mock_case_service.repository.get_messages = AsyncMock(
            return_value=[
                {
                    "role": "user",
                    "content": "Database connection timeout issue with PostgreSQL server on production environment",
                },
                {"role": "assistant", "content": "Response 1"},
                {
                    "role": "user",
                    "content": "Error message shows authentication failed for postgres user",
                },
                {"role": "assistant", "content": "Response 2"},
                {
                    "role": "user",
                    "content": "I've verified the password is correct in environment variables",
                },
                {"role": "assistant", "content": "Response 3"},
                {
                    "role": "user",
                    "content": "Should I check the pg_hba.conf file for authentication settings",
                },
                {"role": "assistant", "content": "Response 4"},
                {
                    "role": "user",
                    "content": "Found the issue in authentication configuration thanks",
                },
            ]
        )

        mock_case_service.get_case_conversation_context = AsyncMock(
            return_value="""Previous conversation:
1. [14:20] User: Database connection timeout issue with PostgreSQL server on production environment
2. [14:21] Assistant: Response 1
3. [14:22] User: Error message shows authentication failed for postgres user
4. [14:23] Assistant: Response 2
5. [14:24] User: I've verified the password is correct in environment variables
6. [14:25] Assistant: Response 3
7. [14:26] User: Should I check the pg_hba.conf file for authentication settings
8. [14:27] Assistant: Response 4
9. [14:28] User: Found the issue in authentication configuration thanks"""
        )

        mock_case_service.update_case = AsyncMock(return_value=True)

        # Threshold should pass — turn count meets minimum. Title
        # generation itself may still fail downstream (e.g. LLM/fallback
        # both fail → ValidationException with a different message), but
        # the failure must NOT be the turn-threshold one.
        try:
            result = await generate_case_title(
                case_id="case_123",
                request=mock_request,
                response=mock_response,
                request_body=None,
                force=False,
                case_service=mock_case_service,
                current_user=mock_user,
            )
            # If it succeeds, verify threshold passed (title should be updated)
            # Actual title content depends on hybrid logic (extractive vs LLM)
            # so we only verify that SOME non-generic title was set
            assert (
                result.title != "Case-240101-1"
            ), "Title should be updated from generic"
            assert len(result.title) > 0, "Title should not be empty"
        except Exception as e:  # noqa: BLE001
            # Downstream failures (other 422s, persistence verification
            # 500s, etc.) are acceptable for this test — the only
            # forbidden outcome is the turn-threshold message.
            assert "conversation turns" not in str(e)

    @pytest.mark.asyncio
    async def test_more_than_5_turns_passes_threshold(
        self, mock_request, mock_response, mock_user, mock_case
    ):
        """Should pass threshold when turn count > 5 - test only threshold check"""
        mock_case_service = AsyncMock()
        mock_case_service.get_case = AsyncMock(return_value=mock_case)

        # 10 user turns (well above threshold) - use meaningful content
        messages = []
        for i in range(1, 11):
            messages.append(
                {
                    "role": "user",
                    "content": f"Database connection timeout issue on production server environment number {i}",
                }
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": f"Let me help investigate that issue",
                }
            )

        mock_case_service.repository.get_messages = AsyncMock(return_value=messages)

        mock_case_service.get_case_conversation_context = AsyncMock(
            return_value="Previous conversation:\n"
            + "\n".join(
                [
                    f"{i}. [14:{i:02d}] User: Database connection timeout issue on production server environment number {i}"
                    for i in range(1, 11)
                ]
            )
        )

        mock_case_service.update_case = AsyncMock(return_value=True)

        # Threshold should pass — turn count well above minimum.
        # Downstream 422 (LLM/fallback failure) is acceptable; the
        # turn-threshold-specific message must not appear.
        try:
            result = await generate_case_title(
                case_id="case_123",
                request=mock_request,
                response=mock_response,
                request_body=None,
                force=False,
                case_service=mock_case_service,
                current_user=mock_user,
            )
            # If it succeeds, verify threshold passed (title should be updated)
            # Actual title content depends on hybrid logic (extractive vs LLM)
            # so we only verify that SOME non-generic title was set
            assert (
                result.title != "Case-240101-1"
            ), "Title should be updated from generic"
            assert len(result.title) > 0, "Title should not be empty"
        except Exception as e:  # noqa: BLE001
            # Downstream failures are acceptable for this test — the
            # only forbidden outcome is the turn-threshold message.
            assert "conversation turns" not in str(e)

    @pytest.mark.asyncio
    async def test_only_counts_user_role_messages(
        self, mock_request, mock_response, mock_user, mock_case
    ):
        """Should only count user role messages, not assistant or system"""
        mock_case_service = AsyncMock()
        mock_case_service.get_case = AsyncMock(return_value=mock_case)

        # 3 user + 3 assistant + 2 system = only 3 user turns
        mock_case_service.repository.get_messages = AsyncMock(
            return_value=[
                {"role": "system", "content": "Case created"},
                {"role": "user", "content": "User 1"},
                {"role": "assistant", "content": "Agent 1"},
                {"role": "user", "content": "User 2"},
                {"role": "assistant", "content": "Agent 2"},
                {"role": "system", "content": "Case updated"},
                {"role": "user", "content": "User 3"},
                {"role": "assistant", "content": "Agent 3"},
            ]
        )

        with pytest.raises(ValidationException) as exc_info:
            await generate_case_title(
                case_id="case_123",
                request=mock_request,
                response=mock_response,
                request_body=None,
                force=False,
                case_service=mock_case_service,
                current_user=mock_user,
            )

        assert "currently 3 turns" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_no_messages_returns_422(
        self, mock_request, mock_response, mock_user, mock_case
    ):
        """Should return 422 when case has no messages"""
        mock_case_service = AsyncMock()
        mock_case_service.get_case = AsyncMock(return_value=mock_case)
        mock_case_service.repository.get_messages = AsyncMock(return_value=[])

        with pytest.raises(ValidationException) as exc_info:
            await generate_case_title(
                case_id="case_123",
                request=mock_request,
                response=mock_response,
                request_body=None,
                force=False,
                case_service=mock_case_service,
                current_user=mock_user,
            )

        assert "currently 0 turns" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_messages_exception_treats_as_zero_turns(
        self, mock_request, mock_response, mock_user, mock_case
    ):
        """Should treat get_messages exception as 0 turns and return 422"""
        mock_case_service = AsyncMock()
        mock_case_service.get_case = AsyncMock(return_value=mock_case)
        mock_case_service.repository.get_messages = AsyncMock(
            side_effect=Exception("Database error")
        )

        with pytest.raises(ValidationException) as exc_info:
            await generate_case_title(
                case_id="case_123",
                request=mock_request,
                response=mock_response,
                request_body=None,
                force=False,
                case_service=mock_case_service,
                current_user=mock_user,
            )

        assert "currently 0 turns" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_error_message_includes_current_count(
        self, mock_request, mock_response, mock_user, mock_case
    ):
        """Error message should include current turn count for user clarity"""
        mock_case_service = AsyncMock()
        mock_case_service.get_case = AsyncMock(return_value=mock_case)

        # 2 user turns
        mock_case_service.repository.get_messages = AsyncMock(
            return_value=[
                {"role": "user", "content": "First"},
                {"role": "assistant", "content": "Response"},
                {"role": "user", "content": "Second"},
            ]
        )

        with pytest.raises(ValidationException) as exc_info:
            await generate_case_title(
                case_id="case_123",
                request=mock_request,
                response=mock_response,
                request_body=None,
                force=False,
                case_service=mock_case_service,
                current_user=mock_user,
            )

        detail = str(exc_info.value)
        assert "Need at least 5" in detail
        assert "currently 2 turns" in detail
        assert "Continue discussing your issue" in detail
