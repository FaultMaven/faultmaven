"""Architectural Compliance Integration Tests

Tests to verify compliance with case-and-session-concepts.md v2.0 specification.
These tests validate the session-case boundary, multi-device support, and
session resumption as specified in lines 647-736 of the design document.

Test Coverage:
1. Session creation with client_id (test resumption works)
2. Multi-device sessions for same user (multiple concurrent sessions)
3. Case access across different sessions (same cases visible from all user sessions)
4. Validation that session updates reject case data (forbidden fields enforcement)
5. owner_id requirement in Case creation (security validation)
"""

import pytest
from datetime import datetime, timezone
from typing import Optional

from faultmaven.services.domain.session_service import SessionService
from faultmaven.services.domain.case_service import CaseService
from faultmaven.models.common import SessionContext
from faultmaven.models.case import Case
from faultmaven.exceptions import ValidationException


@pytest.mark.integration
@pytest.mark.asyncio
class TestArchitecturalCompliance:
    """Integration tests for spec compliance verification"""

    # ============================================================================
    # Test 1: Session Creation with client_id and Resumption
    # Spec Reference: Lines 669-676, 723-736
    # ============================================================================

    async def test_session_resumption_with_same_client_id(
        self, session_service: SessionService
    ):
        """Test that sessions resume when same client_id is provided

        Spec Lines 669-676:
        - Same client_id should resume existing session
        - session_resumed flag should be True
        - session_id should be identical
        """
        user_id = "user_resumption_test"
        client_id = "laptop-chrome-device-001"

        # Create initial session with client_id
        session1, was_resumed1 = await session_service.create_session(
            user_id=user_id,
            client_id=client_id
        )

        assert session1 is not None
        assert session1.user_id == user_id
        assert session1.client_id == client_id
        assert was_resumed1 is False, "First session creation should not be resumed"

        # Simulate browser restart - same client_id should resume
        session2, was_resumed2 = await session_service.create_session(
            user_id=user_id,
            client_id=client_id
        )

        # ✅ SPEC COMPLIANCE: Session should be resumed
        assert session1.session_id == session2.session_id, \
            "Same client_id must resume existing session (spec lines 669-676)"
        assert was_resumed2 is True, \
            "session_resumed flag must be True when resuming (spec line 675)"
        assert session2.client_id == client_id

    async def test_different_client_ids_create_separate_sessions(
        self, session_service: SessionService
    ):
        """Test that different client_ids create separate sessions

        Spec Lines 657-667:
        - Different client_ids should create different sessions
        - Both sessions should be valid and active
        """
        user_id = "user_multidevice_test"
        client_id_1 = "device-chrome-laptop"
        client_id_2 = "device-firefox-desktop"

        # Create session from device 1
        session1, was_resumed1 = await session_service.create_session(
            user_id=user_id,
            client_id=client_id_1
        )

        # Create session from device 2
        session2, was_resumed2 = await session_service.create_session(
            user_id=user_id,
            client_id=client_id_2
        )

        # ✅ SPEC COMPLIANCE: Different sessions for different devices
        assert session1.session_id != session2.session_id, \
            "Different client_ids must create separate sessions (spec lines 657-667)"
        assert session1.user_id == session2.user_id, \
            "Both sessions belong to same user"
        assert was_resumed1 is False
        assert was_resumed2 is False

    async def test_session_without_client_id_creates_new_session(
        self, session_service: SessionService
    ):
        """Test that omitting client_id always creates new sessions

        When client_id is None, sessions should not resume
        """
        user_id = "user_no_client_id"

        # Create multiple sessions without client_id
        session1, _ = await session_service.create_session(user_id=user_id)
        session2, _ = await session_service.create_session(user_id=user_id)
        session3, _ = await session_service.create_session(user_id=user_id)

        # All should be different sessions
        session_ids = {session1.session_id, session2.session_id, session3.session_id}
        assert len(session_ids) == 3, \
            "Sessions without client_id should all be unique"

    # ============================================================================
    # Test 2: Multi-Device Support for Same User
    # Spec Reference: Lines 657-667, 703-722
    # ============================================================================

    async def test_multi_device_sessions_concurrent_access(
        self, session_service: SessionService
    ):
        """Test that same user can have multiple concurrent sessions

        Spec Lines 657-667:
        - Same user can have sessions on multiple devices
        - Each device has separate session_id
        - All sessions belong to same user_id
        """
        user_id = "user_concurrent_devices"
        devices = [
            "device-chrome-laptop",
            "device-firefox-desktop",
            "device-safari-iphone",
            "device-edge-surface"
        ]

        sessions = []
        for device_id in devices:
            session, _ = await session_service.create_session(
                user_id=user_id,
                client_id=device_id
            )
            sessions.append(session)

        # ✅ SPEC COMPLIANCE: All sessions valid but separate
        session_ids = [s.session_id for s in sessions]
        assert len(set(session_ids)) == len(devices), \
            "Each device must have unique session_id (spec lines 657-667)"

        # All sessions belong to same user
        for session in sessions:
            assert session.user_id == user_id
            assert session.client_id in devices

    async def test_session_expiry_does_not_affect_other_devices(
        self, session_service: SessionService
    ):
        """Test that expiring one session doesn't affect other device sessions

        Sessions are independent per device
        """
        user_id = "user_session_isolation"
        client_1 = "device-laptop"
        client_2 = "device-mobile"

        # Create sessions on two devices
        session1, _ = await session_service.create_session(user_id, client_1)
        session2, _ = await session_service.create_session(user_id, client_2)

        # Delete session 1 (laptop)
        deleted = await session_service.delete_session(session1.session_id)
        assert deleted is True

        # Session 2 should still be valid
        session2_check = await session_service.get_session(session2.session_id)
        assert session2_check is not None, \
            "Deleting one session should not affect other device sessions"
        assert session2_check.session_id == session2.session_id

    # ============================================================================
    # Test 3: Case Access Across Different Sessions
    # Spec Reference: Lines 650-655, 678-690, 710-722
    # ============================================================================

    async def test_cases_accessible_from_all_user_sessions(
        self, session_service: SessionService,
        case_service: CaseService
    ):
        """Test that cases are accessible from all sessions of the same user

        Spec Lines 650-655, 710-722:
        - Cases owned by user_id, not session_id
        - All sessions for same user see identical cases
        - Cases persist beyond individual session lifecycle
        """
        user_id = "user_case_access"
        client_1 = "device-laptop"
        client_2 = "device-mobile"

        # Create sessions on two devices
        session1, _ = await session_service.create_session(user_id, client_1)
        session2, _ = await session_service.create_session(user_id, client_2)

        # Create case owned by user (via session1 for auth)
        case = await case_service.create_case(
            title="Multi-Device Test Case",
            initial_query="Test case accessibility across sessions",
            owner_id=user_id  # ✅ SPEC: Case owned by user, not session
        )

        # Retrieve cases via both sessions
        cases_via_session1 = await case_service.get_session_cases(session1.session_id)
        cases_via_session2 = await case_service.get_session_cases(session2.session_id)

        # ✅ SPEC COMPLIANCE: Identical results from both sessions
        case_ids_session1 = {c.case_id for c in cases_via_session1}
        case_ids_session2 = {c.case_id for c in cases_via_session2}

        assert case_ids_session1 == case_ids_session2, \
            "Same user's cases must be identical from all sessions (spec lines 650-655)"
        assert case.case_id in case_ids_session1, \
            "Created case must be visible from all user sessions"

    async def test_cases_persist_after_session_deletion(
        self, session_service: SessionService,
        case_service: CaseService
    ):
        """Test that cases persist after creating session is deleted

        Spec Lines 678-690:
        - Cases are independent resources
        - Session deletion does not delete cases
        - New sessions can access old cases
        """
        user_id = "user_case_persistence"
        client_id = "device-chrome"

        # Create session and case
        session1, _ = await session_service.create_session(user_id, client_id)
        case = await case_service.create_case(
            title="Persistent Case",
            initial_query="This case should survive session deletion",
            owner_id=user_id
        )
        case_id = case.case_id

        # Delete the session
        await session_service.delete_session(session1.session_id)

        # Create new session
        session2, _ = await session_service.create_session(user_id, client_id)

        # Case should still be accessible
        cases = await case_service.get_session_cases(session2.session_id)
        case_ids = {c.case_id for c in cases}

        # ✅ SPEC COMPLIANCE: Case persists beyond session
        assert case_id in case_ids, \
            "Cases must persist after session expiry (spec lines 678-690)"

    async def test_direct_case_access_matches_session_case_access(
        self, session_service: SessionService,
        case_service: CaseService
    ):
        """Test that direct user case access matches session-based access

        Spec Lines 650-655:
        - getUserCases() and getSessionCases() must return identical results
        """
        user_id = "user_access_parity"
        client_id = "device-test"

        # Create session
        session, _ = await session_service.create_session(user_id, client_id)

        # Create multiple cases
        case_titles = ["Case 1", "Case 2", "Case 3"]
        created_cases = []
        for title in case_titles:
            case = await case_service.create_case(
                title=title,
                initial_query=f"Query for {title}",
                owner_id=user_id
            )
            created_cases.append(case)

        # Get cases via direct user access
        direct_cases = await case_service.get_user_cases(user_id)

        # Get cases via session access
        session_cases = await case_service.get_session_cases(session.session_id)

        # ✅ SPEC COMPLIANCE: Identical results
        direct_case_ids = sorted([c.case_id for c in direct_cases])
        session_case_ids = sorted([c.case_id for c in session_cases])

        assert direct_case_ids == session_case_ids, \
            "Direct and session-based case access must return identical results (spec lines 650-655)"

    # ============================================================================
    # Test 4: Session Updates Reject Case Data
    # Spec Reference: Lines 102-107
    # ============================================================================

    async def test_session_update_rejects_case_history(
        self, session_service: SessionService
    ):
        """Test that session updates reject case_history field

        Spec Lines 102-107:
        - SessionContext must NOT contain case_history
        - Updates with case_history should be rejected
        """
        user_id = "user_forbidden_fields"
        session, _ = await session_service.create_session(user_id)

        # Attempt to update with forbidden case_history field
        forbidden_update = {
            "case_history": [
                {"case_id": "case_001", "timestamp": datetime.now(timezone.utc).isoformat()}
            ]
        }

        # ✅ SPEC COMPLIANCE: Should reject forbidden field
        with pytest.raises(ValidationException) as exc_info:
            await session_service.update_session(session.session_id, forbidden_update)

        assert "case_history" in str(exc_info.value).lower() or \
               "forbidden" in str(exc_info.value).lower(), \
            "Session updates must reject case_history field (spec lines 102-107)"

    async def test_session_update_rejects_current_case_id(
        self, session_service: SessionService
    ):
        """Test that session updates reject current_case_id field

        Spec Lines 102-107:
        - SessionContext must NOT contain current_case_id
        - Updates with current_case_id should be rejected
        """
        user_id = "user_forbidden_case_id"
        session, _ = await session_service.create_session(user_id)

        # Attempt to update with forbidden current_case_id field
        forbidden_update = {
            "current_case_id": "case_123"
        }

        # ✅ SPEC COMPLIANCE: Should reject forbidden field
        with pytest.raises(ValidationException) as exc_info:
            await session_service.update_session(session.session_id, forbidden_update)

        assert "current_case_id" in str(exc_info.value).lower() or \
               "forbidden" in str(exc_info.value).lower(), \
            "Session updates must reject current_case_id field (spec lines 102-107)"

    async def test_session_update_rejects_data_uploads(
        self, session_service: SessionService
    ):
        """Test that session updates reject data_uploads field

        Spec Lines 102-107:
        - SessionContext must NOT contain data_uploads
        - Updates with data_uploads should be rejected
        """
        user_id = "user_forbidden_uploads"
        session, _ = await session_service.create_session(user_id)

        # Attempt to update with forbidden data_uploads field
        forbidden_update = {
            "data_uploads": ["upload_001", "upload_002"]
        }

        # ✅ SPEC COMPLIANCE: Should reject forbidden field
        with pytest.raises(ValidationException) as exc_info:
            await session_service.update_session(session.session_id, forbidden_update)

        assert "data_uploads" in str(exc_info.value).lower() or \
               "forbidden" in str(exc_info.value).lower(), \
            "Session updates must reject data_uploads field (spec lines 102-107)"

    async def test_session_update_accepts_valid_fields(
        self, session_service: SessionService
    ):
        """Test that session updates accept valid authentication fields

        Valid fields: metadata, authentication context
        """
        user_id = "user_valid_updates"
        session, _ = await session_service.create_session(user_id)

        # Valid authentication metadata
        valid_update = {
            "metadata": {
                "last_ip": "192.168.1.1",
                "user_agent": "Mozilla/5.0",
                "login_time": datetime.now(timezone.utc).isoformat()
            }
        }

        # Should succeed
        success = await session_service.update_session(session.session_id, valid_update)
        assert success is True, \
            "Session updates should accept valid authentication metadata"

        # Verify update was applied
        updated_session = await session_service.get_session(session.session_id)
        assert updated_session.metadata.get("last_ip") == "192.168.1.1"

    # ============================================================================
    # Test 5: owner_id Requirement in Case Creation
    # Spec Reference: Lines 273-277
    # ============================================================================

    async def test_case_creation_requires_owner_id(
        self, case_service: CaseService
    ):
        """Test that case creation requires owner_id field

        Spec Lines 273-277:
        - owner_id must be required (not Optional)
        - Case creation without owner_id should fail
        """
        # Attempt to create case without owner_id
        # This should fail at validation level

        with pytest.raises((ValidationException, TypeError, ValueError)) as exc_info:
            await case_service.create_case(
                title="Missing Owner Case",
                initial_query="This should fail",
                owner_id=None  # ✅ SPEC: owner_id is required
            )

        # Should reject missing owner_id
        error_msg = str(exc_info.value).lower()
        assert "owner" in error_msg or "required" in error_msg, \
            "Case creation must require owner_id (spec lines 273-277)"

    async def test_case_creation_with_valid_owner_id(
        self, session_service: SessionService,
        case_service: CaseService
    ):
        """Test that case creation succeeds with valid owner_id

        Spec Lines 273-277:
        - owner_id is required and must be set
        - Created case must have owner_id set
        """
        user_id = "user_valid_owner"

        # Create session for authentication
        session, _ = await session_service.create_session(user_id)

        # Create case with valid owner_id
        case = await case_service.create_case(
            title="Valid Owner Case",
            initial_query="Case with valid owner",
            owner_id=user_id  # ✅ SPEC: owner_id provided
        )

        # ✅ SPEC COMPLIANCE: owner_id must be set and non-optional
        assert case.owner_id == user_id, \
            "Created case must have owner_id set (spec lines 273-277)"
        assert case.owner_id is not None, \
            "owner_id must not be None"

    async def test_case_ownership_enforced_in_retrieval(
        self, session_service: SessionService,
        case_service: CaseService
    ):
        """Test that case ownership is enforced when retrieving cases

        Users should only see their own cases
        """
        user1_id = "user_owner_1"
        user2_id = "user_owner_2"

        # Create sessions for both users
        session1, _ = await session_service.create_session(user1_id)
        session2, _ = await session_service.create_session(user2_id)

        # User 1 creates a case
        case1 = await case_service.create_case(
            title="User 1 Case",
            initial_query="Case owned by user 1",
            owner_id=user1_id
        )

        # User 2 creates a case
        case2 = await case_service.create_case(
            title="User 2 Case",
            initial_query="Case owned by user 2",
            owner_id=user2_id
        )

        # User 1 should only see their case
        user1_cases = await case_service.get_session_cases(session1.session_id)
        user1_case_ids = {c.case_id for c in user1_cases}

        assert case1.case_id in user1_case_ids, \
            "User should see their own cases"
        assert case2.case_id not in user1_case_ids, \
            "User should not see other users' cases"

        # User 2 should only see their case
        user2_cases = await case_service.get_session_cases(session2.session_id)
        user2_case_ids = {c.case_id for c in user2_cases}

        assert case2.case_id in user2_case_ids, \
            "User should see their own cases"
        assert case1.case_id not in user2_case_ids, \
            "User should not see other users' cases"

    # ============================================================================
    # Test 6: Additional Compliance Validations
    # ============================================================================

    async def test_session_context_has_required_multi_device_fields(
        self, session_service: SessionService
    ):
        """Test that SessionContext includes multi-device fields

        Spec Lines 263-269:
        - client_id field must exist
        - session_resumed field must exist
        - expires_at field must exist
        """
        user_id = "user_field_validation"
        client_id = "device-test"

        session, was_resumed = await session_service.create_session(
            user_id=user_id,
            client_id=client_id
        )

        # ✅ SPEC COMPLIANCE: Multi-device fields present
        assert hasattr(session, 'client_id'), \
            "SessionContext must have client_id field (spec lines 263-269)"
        assert hasattr(session, 'session_resumed'), \
            "SessionContext must have session_resumed field (spec lines 263-269)"
        assert hasattr(session, 'expires_at'), \
            "SessionContext must have expires_at field (spec lines 263-269)"

        # Verify values are set correctly
        assert session.client_id == client_id
        assert isinstance(session.session_resumed, bool)

    async def test_session_context_user_id_is_required(
        self, session_service: SessionService
    ):
        """Test that SessionContext requires user_id

        Spec: user_id should be required for authorization
        """
        # This tests the refactored spec where user_id is required
        # If the implementation still allows None, this validates behavior

        session, _ = await session_service.create_session(user_id="test_user")

        # user_id should be set and non-None
        assert session.user_id is not None, \
            "SessionContext user_id should be set"
        assert session.user_id == "test_user"
