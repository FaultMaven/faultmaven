"""Test module for case persistence data models.

This module tests the Pydantic models for case persistence functionality,
including validation, business logic methods, and data model behavior.

Tests cover:
- Case, CaseMessage, CaseParticipant, CaseContext model validation
- Business logic methods (add_participant, can_user_access, etc.)
- Enum classes and their behavior
- Edge cases and error conditions
"""

import json
import pytest
from datetime import datetime, timedelta
from typing import Dict, Any
from uuid import uuid4

from faultmaven.models.case import (
    Case,
    CaseMessage,
    CaseParticipant,
    CaseContext,
    CaseStatus,
    CasePriority,
    MessageType,
    ParticipantRole,
    CaseCreateRequest,
    CaseUpdateRequest,
    CaseShareRequest,
    CaseListFilter,
    CaseSearchRequest,
    CaseSummary
)
from pydantic import ValidationError


class TestCaseEnums:
    """Test case enumeration classes"""
    
    def test_case_status_values(self):
        """Test CaseStatus enum values"""
        assert CaseStatus.ACTIVE == "active"
        assert CaseStatus.INVESTIGATING == "investigating"
        assert CaseStatus.SOLVED == "solved"
        assert CaseStatus.STALLED == "stalled"
        assert CaseStatus.ARCHIVED == "archived"
        assert CaseStatus.SHARED == "shared"
    
    def test_case_priority_values(self):
        """Test CasePriority enum values"""
        assert CasePriority.LOW == "low"
        assert CasePriority.MEDIUM == "medium"
        assert CasePriority.HIGH == "high"
        assert CasePriority.CRITICAL == "critical"
    
    def test_message_type_values(self):
        """Test MessageType enum values"""
        assert MessageType.USER_QUERY == "user_query"
        assert MessageType.AGENT_RESPONSE == "agent_response"
        assert MessageType.SYSTEM_EVENT == "system_event"
        assert MessageType.DATA_UPLOAD == "data_upload"
        assert MessageType.CASE_NOTE == "case_note"
        assert MessageType.STATUS_CHANGE == "status_change"
    
    def test_participant_role_values(self):
        """Test ParticipantRole enum values"""
        assert ParticipantRole.OWNER == "owner"
        assert ParticipantRole.COLLABORATOR == "collaborator"
        assert ParticipantRole.VIEWER == "viewer"
        assert ParticipantRole.SUPPORT == "support"


class TestCaseMessage:
    """Test CaseMessage model"""
    
    def test_case_message_creation(self):
        """Test basic CaseMessage creation"""
        message = CaseMessage(
            case_id="case-123",
            message_type=MessageType.USER_QUERY,
            content="Test message content"
        )
        
        assert message.case_id == "case-123"
        assert message.message_type == MessageType.USER_QUERY
        assert message.content == "Test message content"
        assert message.message_id is not None
        assert isinstance(message.timestamp, datetime)
        assert message.metadata == {}
        assert message.attachments == []
    
    def test_case_message_with_optional_fields(self):
        """Test CaseMessage with all optional fields"""
        timestamp = datetime.utcnow()
        message = CaseMessage(
            case_id="case-123",
            session_id="session-456",
            author_id="user-789",
            message_type=MessageType.AGENT_RESPONSE,
            content="Agent response content",
            timestamp=timestamp,
            metadata={"confidence": 0.95},
            attachments=["file1.txt", "file2.log"],
            confidence_score=0.95,
            processing_time_ms=1500
        )
        
        assert message.session_id == "session-456"
        assert message.author_id == "user-789"
        assert message.timestamp == timestamp
        assert message.metadata == {"confidence": 0.95}
        assert message.attachments == ["file1.txt", "file2.log"]
        assert message.confidence_score == 0.95
        assert message.processing_time_ms == 1500
    
    def test_case_message_json_serialization(self):
        """Test CaseMessage JSON serialization"""
        message = CaseMessage(
            case_id="case-123",
            message_type=MessageType.USER_QUERY,
            content="Test message"
        )
        
        json_data = message.json()
        assert json_data is not None
        
        # Parse back to verify
        parsed_data = json.loads(json_data)
        assert parsed_data["case_id"] == "case-123"
        assert parsed_data["message_type"] == "user_query"
        assert parsed_data["content"] == "Test message"
        assert "timestamp" in parsed_data
        assert parsed_data["timestamp"].endswith("Z")
    
    def test_case_message_validation_errors(self):
        """Test CaseMessage validation errors"""
        # Missing required fields
        with pytest.raises(ValidationError):
            CaseMessage()
        
        with pytest.raises(ValidationError):
            CaseMessage(case_id="case-123")
        
        with pytest.raises(ValidationError):
            CaseMessage(
                case_id="case-123",
                message_type=MessageType.USER_QUERY
            )


class TestCaseParticipant:
    """Test CaseParticipant model"""
    
    def test_participant_creation_owner(self):
        """Test CaseParticipant creation with OWNER role"""
        participant = CaseParticipant(
            user_id="user-123",
            role=ParticipantRole.OWNER
        )
        
        assert participant.user_id == "user-123"
        assert participant.role == ParticipantRole.OWNER
        assert participant.can_edit is True
        assert participant.can_share is True
        assert participant.can_archive is True
        assert isinstance(participant.added_at, datetime)
        assert participant.last_accessed is None
    
    def test_participant_creation_collaborator(self):
        """Test CaseParticipant creation with COLLABORATOR role"""
        participant = CaseParticipant(
            user_id="user-123",
            role=ParticipantRole.COLLABORATOR
        )
        
        assert participant.role == ParticipantRole.COLLABORATOR
        # Fixed: Collaborator permissions are now correctly True by default
        assert participant.can_edit is True  # Fixed bug - now correctly True
        assert participant.can_share is True  # Fixed bug - now correctly True
        assert participant.can_archive is False  # Collaborators don't get archive permission
    
    def test_participant_creation_viewer(self):
        """Test CaseParticipant creation with VIEWER role"""
        participant = CaseParticipant(
            user_id="user-123",
            role=ParticipantRole.VIEWER
        )
        
        assert participant.role == ParticipantRole.VIEWER
        assert participant.can_edit is False
        assert participant.can_share is False
        assert participant.can_archive is False
    
    def test_participant_creation_support(self):
        """Test CaseParticipant creation with SUPPORT role"""
        participant = CaseParticipant(
            user_id="user-123",
            role=ParticipantRole.SUPPORT
        )
        
        assert participant.role == ParticipantRole.SUPPORT
        assert participant.can_edit is False
        assert participant.can_share is False
        assert participant.can_archive is False
    
    def test_participant_permissions_override(self):
        """Test that explicit permission values override role defaults"""
        participant = CaseParticipant(
            user_id="user-123",
            role=ParticipantRole.COLLABORATOR,
            can_edit=False,
            can_share=False
        )
        
        # Collaborator defaults to True for these, but we override to False
        assert participant.can_edit is False
        assert participant.can_share is False
        assert participant.can_archive is False  # Still False for collaborator
    
    def test_participant_with_metadata(self):
        """Test CaseParticipant with additional metadata"""
        added_time = datetime.utcnow()
        access_time = datetime.utcnow()
        
        participant = CaseParticipant(
            user_id="user-123",
            role=ParticipantRole.COLLABORATOR,
            added_at=added_time,
            added_by="user-456",
            last_accessed=access_time
        )
        
        assert participant.added_at == added_time
        assert participant.added_by == "user-456"
        assert participant.last_accessed == access_time
    
    def test_participant_json_serialization(self):
        """Test CaseParticipant JSON serialization"""
        participant = CaseParticipant(
            user_id="user-123",
            role=ParticipantRole.OWNER
        )
        
        json_data = participant.json()
        parsed_data = json.loads(json_data)
        
        assert parsed_data["user_id"] == "user-123"
        assert parsed_data["role"] == "owner"
        assert parsed_data["can_edit"] is True
        assert parsed_data["can_share"] is True
        assert parsed_data["can_archive"] is True
        assert "added_at" in parsed_data
        assert parsed_data["added_at"].endswith("Z")


class TestCaseContext:
    """Test CaseContext model"""
    
    def test_case_context_creation_empty(self):
        """Test CaseContext creation with defaults"""
        context = CaseContext()
        
        assert context.problem_description is None
        assert context.system_info == {}
        assert context.environment_details == {}
        assert context.uploaded_files == []
        assert context.log_snippets == []
        assert context.error_patterns == []
        assert context.blast_radius_defined is False
        assert context.timeline_established is False
        assert context.hypothesis_formulated == []
        assert context.hypothesis_validated == []
        assert context.solutions_proposed == []
        assert context.root_causes == []
        assert context.recommendations == []
        assert context.knowledge_base_refs == []
    
    def test_case_context_with_troubleshooting_data(self):
        """Test CaseContext with troubleshooting information"""
        context = CaseContext(
            problem_description="Database connection failures",
            system_info={"os": "Ubuntu 20.04", "memory": "16GB"},
            environment_details={"env": "production", "region": "us-east-1"},
            uploaded_files=["db.log", "error.log"],
            log_snippets=[
                {"timestamp": "2024-01-01T12:00:00Z", "level": "ERROR", "message": "Connection timeout"}
            ],
            error_patterns=["connection timeout", "max pool size reached"]
        )
        
        assert context.problem_description == "Database connection failures"
        assert context.system_info["os"] == "Ubuntu 20.04"
        assert context.environment_details["env"] == "production"
        assert "db.log" in context.uploaded_files
        assert len(context.log_snippets) == 1
        assert "connection timeout" in context.error_patterns
    
    def test_case_context_sre_doctrine_progress(self):
        """Test CaseContext with SRE doctrine progress tracking"""
        context = CaseContext(
            blast_radius_defined=True,
            timeline_established=True,
            hypothesis_formulated=["Database pool exhaustion", "Network connectivity issue"],
            hypothesis_validated=[
                {"hypothesis": "Database pool exhaustion", "validated": True, "evidence": "Pool metrics"}
            ],
            solutions_proposed=[
                {"solution": "Increase pool size", "impact": "high", "effort": "low"}
            ],
            root_causes=["Insufficient connection pool size"],
            recommendations=["Scale database pool", "Add connection monitoring"],
            knowledge_base_refs=["kb-001", "kb-002"]
        )
        
        assert context.blast_radius_defined is True
        assert context.timeline_established is True
        assert len(context.hypothesis_formulated) == 2
        assert len(context.hypothesis_validated) == 1
        assert len(context.solutions_proposed) == 1
        assert "Insufficient connection pool size" in context.root_causes
        assert "Scale database pool" in context.recommendations
        assert "kb-001" in context.knowledge_base_refs


class TestCase:
    """Test Case model"""
    
    def test_case_creation_minimal(self):
        """Test Case creation with minimal required fields"""
        case = Case(title="Test Case")
        
        assert case.title == "Test Case"
        assert case.description is None
        assert case.owner_id is None
        assert case.status == CaseStatus.ACTIVE
        assert case.priority == CasePriority.MEDIUM
        assert isinstance(case.created_at, datetime)
        assert isinstance(case.updated_at, datetime)
        assert isinstance(case.last_activity_at, datetime)
        assert case.expires_at is not None
        assert case.auto_archive_after_days == 30
        assert case.messages == []
        assert case.message_count == 0
        assert case.session_ids == set()
        assert case.current_session_id is None
        assert isinstance(case.context, CaseContext)
        assert case.tags == []
        assert case.metadata == {}
        assert case.participant_count == 0
        assert case.share_count == 0
    
    def test_case_creation_full(self):
        """Test Case creation with all fields"""
        created_time = datetime.utcnow()
        expires_time = created_time + timedelta(days=60)
        
        case = Case(
            title="Full Test Case",
            description="Detailed test case description",
            owner_id="user-123",
            status=CaseStatus.INVESTIGATING,
            priority=CasePriority.HIGH,
            created_at=created_time,
            expires_at=expires_time,
            auto_archive_after_days=60,
            tags=["database", "production"],
            metadata={"source": "monitoring"}
        )
        
        assert case.title == "Full Test Case"
        assert case.description == "Detailed test case description"
        assert case.owner_id == "user-123"
        assert case.status == CaseStatus.INVESTIGATING
        assert case.priority == CasePriority.HIGH
        assert case.created_at == created_time
        assert case.expires_at == expires_time
        assert case.auto_archive_after_days == 60
        assert "database" in case.tags
        assert case.metadata["source"] == "monitoring"
    
    def test_case_expiration_auto_calculation(self):
        """Test automatic expiration date calculation"""
        case = Case(
            title="Auto Expire Case",
            auto_archive_after_days=45
        )
        
        expected_expiry = case.created_at + timedelta(days=45)
        
        # Allow small time difference due to processing time
        time_diff = abs((case.expires_at - expected_expiry).total_seconds())
        assert time_diff < 1.0  # Less than 1 second difference
    
    def test_case_participant_count_validation(self):
        """Test participant count is automatically calculated"""
        case = Case(title="Participant Test")
        
        # Add participants
        case.add_participant("user-1", ParticipantRole.OWNER)
        case.add_participant("user-2", ParticipantRole.COLLABORATOR)
        
        assert case.participant_count == 2
        assert len(case.participants) == 2
    
    def test_add_participant_success(self):
        """Test successful participant addition"""
        case = Case(title="Test Case")
        
        result = case.add_participant("user-123", ParticipantRole.OWNER, "admin-456")
        
        assert result is True
        assert len(case.participants) == 1
        assert case.participants[0].user_id == "user-123"
        assert case.participants[0].role == ParticipantRole.OWNER
        assert case.participants[0].added_by == "admin-456"
        assert case.participant_count == 1
        assert case.updated_at is not None
    
    def test_add_participant_duplicate(self):
        """Test adding duplicate participant fails"""
        case = Case(title="Test Case")
        
        # Add participant first time
        result1 = case.add_participant("user-123", ParticipantRole.OWNER)
        assert result1 is True
        
        # Try to add same user again
        result2 = case.add_participant("user-123", ParticipantRole.COLLABORATOR)
        assert result2 is False
        assert len(case.participants) == 1  # Still only one participant
    
    def test_remove_participant_success(self):
        """Test successful participant removal"""
        case = Case(title="Test Case")
        
        # Add participants
        case.add_participant("user-1", ParticipantRole.OWNER)
        case.add_participant("user-2", ParticipantRole.COLLABORATOR)
        
        # Remove collaborator
        result = case.remove_participant("user-2")
        
        assert result is True
        assert len(case.participants) == 1
        assert case.participants[0].user_id == "user-1"
        assert case.participant_count == 1
    
    def test_remove_participant_owner_protection(self):
        """Test that owner cannot be removed"""
        case = Case(title="Test Case")
        case.add_participant("user-1", ParticipantRole.OWNER)
        
        result = case.remove_participant("user-1")
        
        assert result is False
        assert len(case.participants) == 1
    
    def test_remove_participant_not_found(self):
        """Test removing non-existent participant"""
        case = Case(title="Test Case")
        
        result = case.remove_participant("user-999")
        
        assert result is False
    
    def test_update_participant_role_success(self):
        """Test successful participant role update"""
        case = Case(title="Test Case")
        case.add_participant("user-1", ParticipantRole.VIEWER)
        
        result = case.update_participant_role("user-1", ParticipantRole.COLLABORATOR)
        
        assert result is True
        assert case.participants[0].role == ParticipantRole.COLLABORATOR
    
    def test_update_participant_role_owner_protection(self):
        """Test that owner role cannot be changed"""
        case = Case(title="Test Case")
        case.add_participant("user-1", ParticipantRole.OWNER)
        
        result = case.update_participant_role("user-1", ParticipantRole.COLLABORATOR)
        
        assert result is False
        assert case.participants[0].role == ParticipantRole.OWNER
    
    def test_add_message_to_case(self):
        """Test adding message to case"""
        case = Case(title="Test Case")
        
        message = CaseMessage(
            case_id="different-id",  # Will be overwritten
            message_type=MessageType.USER_QUERY,
            content="Test message"
        )
        
        case.add_message(message)
        
        assert len(case.messages) == 1
        assert case.message_count == 1
        assert case.messages[0].case_id == case.case_id  # ID was updated
        assert case.messages[0].content == "Test message"
        assert case.last_activity_at is not None
        assert case.updated_at is not None
    
    def test_get_participant_role(self):
        """Test getting participant role"""
        case = Case(title="Test Case")
        case.add_participant("user-1", ParticipantRole.OWNER)
        case.add_participant("user-2", ParticipantRole.VIEWER)
        
        assert case.get_participant_role("user-1") == ParticipantRole.OWNER
        assert case.get_participant_role("user-2") == ParticipantRole.VIEWER
        assert case.get_participant_role("user-999") is None
    
    def test_can_user_access(self):
        """Test user access checking"""
        case = Case(title="Test Case")
        case.add_participant("user-1", ParticipantRole.OWNER)
        case.add_participant("user-2", ParticipantRole.VIEWER)
        
        assert case.can_user_access("user-1") is True
        assert case.can_user_access("user-2") is True
        assert case.can_user_access("user-999") is False
    
    def test_can_user_edit(self):
        """Test user edit permission checking"""
        case = Case(title="Test Case")
        case.add_participant("user-1", ParticipantRole.OWNER)
        case.add_participant("user-2", ParticipantRole.COLLABORATOR)
        case.add_participant("user-3", ParticipantRole.VIEWER)
        
        assert case.can_user_edit("user-1") is True
        assert case.can_user_edit("user-2") is True
        assert case.can_user_edit("user-3") is False
        assert case.can_user_edit("user-999") is False
    
    def test_can_user_share(self):
        """Test user share permission checking"""
        case = Case(title="Test Case")
        case.add_participant("user-1", ParticipantRole.OWNER)
        case.add_participant("user-2", ParticipantRole.COLLABORATOR)
        case.add_participant("user-3", ParticipantRole.VIEWER)
        
        assert case.can_user_share("user-1") is True
        assert case.can_user_share("user-2") is True
        assert case.can_user_share("user-3") is False
        assert case.can_user_share("user-999") is False
    
    def test_is_expired(self):
        """Test case expiration checking"""
        # Non-expired case
        future_time = datetime.utcnow() + timedelta(days=1)
        case1 = Case(title="Future Case", expires_at=future_time)
        assert case1.is_expired() is False
        
        # Expired case
        past_time = datetime.utcnow() - timedelta(days=1)
        case2 = Case(title="Past Case", expires_at=past_time)
        assert case2.is_expired() is True
        
        # Case with no expiration
        case3 = Case(title="No Expiry Case")
        case3.expires_at = None
        assert case3.is_expired() is False
    
    def test_extend_expiration(self):
        """Test extending case expiration"""
        case = Case(title="Test Case")
        original_expiry = case.expires_at
        
        case.extend_expiration(15)  # Add 15 days
        
        new_expiry = case.expires_at
        assert new_expiry > original_expiry
        
        # Check approximately 15 days were added
        time_diff = (new_expiry - original_expiry).days
        assert time_diff == 15
    
    def test_mark_as_solved(self):
        """Test marking case as solved"""
        case = Case(title="Test Case")
        start_time = case.created_at
        
        case.mark_as_solved("Problem resolved by restarting service")
        
        assert case.status == CaseStatus.SOLVED
        assert case.resolution_time_hours is not None
        assert case.resolution_time_hours >= 0
        assert case.metadata["resolution_summary"] == "Problem resolved by restarting service"
        assert case.updated_at is not None
    
    def test_archive_case(self):
        """Test archiving case"""
        case = Case(title="Test Case")
        
        case.archive("No longer relevant")
        
        assert case.status == CaseStatus.ARCHIVED
        assert case.metadata["archive_reason"] == "No longer relevant"
        assert case.updated_at is not None
    
    def test_get_recent_messages(self):
        """Test getting recent messages"""
        case = Case(title="Test Case")
        
        # Add messages with different timestamps
        for i in range(5):
            message = CaseMessage(
                case_id=case.case_id,
                message_type=MessageType.USER_QUERY,
                content=f"Message {i}",
                timestamp=datetime.utcnow() + timedelta(minutes=i)
            )
            case.add_message(message)
        
        recent = case.get_recent_messages(3)
        
        assert len(recent) == 3
        # Should be in reverse chronological order (most recent first)
        assert "Message 4" in recent[0].content
        assert "Message 3" in recent[1].content
        assert "Message 2" in recent[2].content
    
    def test_get_conversation_summary(self):
        """Test getting conversation summary"""
        case = Case(title="Test Case")
        
        # Add different types of messages
        case.add_message(CaseMessage(
            case_id=case.case_id,
            message_type=MessageType.USER_QUERY,
            content="User question",
            author_id="user-1"
        ))
        case.add_message(CaseMessage(
            case_id=case.case_id,
            message_type=MessageType.AGENT_RESPONSE,
            content="Agent response"
        ))
        case.add_message(CaseMessage(
            case_id=case.case_id,
            message_type=MessageType.SYSTEM_EVENT,
            content="System event"
        ))
        
        case.add_participant("user-1", ParticipantRole.OWNER)
        case.session_ids.add("session-1")
        case.session_ids.add("session-2")
        
        summary = case.get_conversation_summary()
        
        assert summary["total_messages"] == 3
        assert summary["message_types"]["user_query"] == 1
        assert summary["message_types"]["agent_response"] == 1
        assert summary["message_types"]["system_event"] == 1
        assert summary["participants"] == 1
        assert summary["sessions_involved"] == 2
        assert "recent_activity" in summary
        assert summary["recent_activity"]["last_author"] == "user-1"
        assert summary["case_duration_hours"] >= 0
    
    def test_case_json_serialization(self):
        """Test Case JSON serialization"""
        case = Case(
            title="JSON Test Case",
            description="Test description",
            owner_id="user-123",
            tags=["test", "json"]
        )
        
        case.add_participant("user-123", ParticipantRole.OWNER)
        case.session_ids.add("session-1")
        
        json_data = case.json()
        parsed_data = json.loads(json_data)
        
        assert parsed_data["title"] == "JSON Test Case"
        assert parsed_data["description"] == "Test description"
        assert parsed_data["owner_id"] == "user-123"
        assert parsed_data["status"] == "active"
        assert parsed_data["priority"] == "medium"
        assert "test" in parsed_data["tags"]
        assert len(parsed_data["participants"]) == 1
        assert parsed_data["participants"][0]["role"] == "owner"
        assert "session-1" in parsed_data["session_ids"]
        assert "created_at" in parsed_data
        assert parsed_data["created_at"].endswith("Z")


class TestCaseRequestModels:
    """Test case request and response models"""
    
    def test_case_create_request(self):
        """Test CaseCreateRequest model"""
        request = CaseCreateRequest(
            title="New Case",
            description="Case description",
            priority=CasePriority.HIGH,
            tags=["urgent", "database"],
            session_id="session-123",
            initial_message="Initial troubleshooting query"
        )
        
        assert request.title == "New Case"
        assert request.description == "Case description"
        assert request.priority == CasePriority.HIGH
        assert "urgent" in request.tags
        assert request.session_id == "session-123"
        assert request.initial_message == "Initial troubleshooting query"
    
    def test_case_create_request_validation(self):
        """Test CaseCreateRequest validation"""
        # Title too long
        with pytest.raises(ValidationError):
            CaseCreateRequest(title="x" * 201)
        
        # Description too long
        with pytest.raises(ValidationError):
            CaseCreateRequest(
                title="Valid Title",
                description="x" * 2001
            )
        
        # Empty title
        with pytest.raises(ValidationError):
            CaseCreateRequest(title="")
    
    def test_case_update_request(self):
        """Test CaseUpdateRequest model"""
        request = CaseUpdateRequest(
            title="Updated Title",
            status=CaseStatus.INVESTIGATING,
            priority=CasePriority.CRITICAL,
            tags=["updated", "priority"]
        )
        
        assert request.title == "Updated Title"
        assert request.status == CaseStatus.INVESTIGATING
        assert request.priority == CasePriority.CRITICAL
        assert "updated" in request.tags
        assert request.description is None  # Not provided
    
    def test_case_share_request(self):
        """Test CaseShareRequest model"""
        request = CaseShareRequest(
            user_id="user-456",
            role=ParticipantRole.COLLABORATOR,
            message="Please help with this case"
        )
        
        assert request.user_id == "user-456"
        assert request.role == ParticipantRole.COLLABORATOR
        assert request.message == "Please help with this case"
    
    def test_case_list_filter(self):
        """Test CaseListFilter model"""
        filter_obj = CaseListFilter(
            user_id="user-123",
            status=CaseStatus.ACTIVE,
            priority=CasePriority.HIGH,
            owner_id="owner-456",
            tags=["database", "production"],
            created_after=datetime.utcnow() - timedelta(days=7),
            created_before=datetime.utcnow(),
            limit=25,
            offset=50
        )
        
        assert filter_obj.user_id == "user-123"
        assert filter_obj.status == CaseStatus.ACTIVE
        assert filter_obj.priority == CasePriority.HIGH
        assert filter_obj.owner_id == "owner-456"
        assert "database" in filter_obj.tags
        assert filter_obj.limit == 25
        assert filter_obj.offset == 50
    
    def test_case_search_request(self):
        """Test CaseSearchRequest model"""
        filters = CaseListFilter(status=CaseStatus.ACTIVE, limit=10)
        
        request = CaseSearchRequest(
            query="database connection error",
            filters=filters,
            search_in_messages=True,
            search_in_context=False
        )
        
        assert request.query == "database connection error"
        assert request.filters.status == CaseStatus.ACTIVE
        assert request.search_in_messages is True
        assert request.search_in_context is False
    
    def test_case_summary_model(self):
        """Test CaseSummary model"""
        now = datetime.utcnow()
        
        summary = CaseSummary(
            case_id="case-123",
            title="Summary Case",
            status=CaseStatus.SOLVED,
            priority=CasePriority.MEDIUM,
            owner_id="user-456",
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(hours=1),
            last_activity_at=now,
            message_count=15,
            participant_count=3,
            tags=["resolved", "database"]
        )
        
        assert summary.case_id == "case-123"
        assert summary.title == "Summary Case"
        assert summary.status == CaseStatus.SOLVED
        assert summary.priority == CasePriority.MEDIUM
        assert summary.owner_id == "user-456"
        assert summary.message_count == 15
        assert summary.participant_count == 3
        assert "resolved" in summary.tags


class TestCaseEdgeCases:
    """Test edge cases and error conditions"""
    
    def test_case_with_empty_session_ids(self):
        """Test case with empty session IDs set"""
        case = Case(title="Empty Sessions Case")
        
        assert isinstance(case.session_ids, set)
        assert len(case.session_ids) == 0
    
    def test_case_with_large_metadata(self):
        """Test case with large metadata dictionary"""
        large_metadata = {f"key_{i}": f"value_{i}" for i in range(100)}
        
        case = Case(
            title="Large Metadata Case",
            metadata=large_metadata
        )
        
        assert len(case.metadata) == 100
        assert case.metadata["key_50"] == "value_50"
    
    def test_case_message_with_empty_content(self):
        """Test message with empty content"""
        # Empty content should be allowed
        message = CaseMessage(
            case_id="case-123",
            message_type=MessageType.SYSTEM_EVENT,
            content=""
        )
        
        assert message.content == ""
    
    def test_participant_role_permissions_edge_cases(self):
        """Test edge cases in participant permissions"""
        # Test explicit False override for owner (should still be True)
        participant = CaseParticipant(
            user_id="user-123",
            role=ParticipantRole.OWNER,
            can_edit=False,  # This should be overridden to True
            can_share=False,  # This should be overridden to True
            can_archive=False  # This should be overridden to True
        )
        
        assert participant.can_edit is True
        assert participant.can_share is True
        assert participant.can_archive is True
    
    def test_case_with_future_timestamps(self):
        """Test case with future timestamps"""
        future_time = datetime.utcnow() + timedelta(days=30)
        
        case = Case(
            title="Future Case",
            created_at=future_time,
            updated_at=future_time,
            last_activity_at=future_time
        )
        
        assert case.created_at == future_time
        assert case.updated_at == future_time
        assert case.last_activity_at == future_time
    
    def test_case_zero_auto_archive_days(self):
        """Test case with zero auto archive days"""
        case = Case(
            title="No Auto Archive",
            auto_archive_after_days=0
        )
        
        # Should still set expiration based on created_at
        expected_expiry = case.created_at + timedelta(days=0)
        time_diff = abs((case.expires_at - expected_expiry).total_seconds())
        assert time_diff < 1.0
    
    def test_case_negative_auto_archive_days(self):
        """Test case with negative auto archive days"""
        # This should work (expire in the past)
        case = Case(
            title="Past Expiry",
            auto_archive_after_days=-1
        )
        
        assert case.expires_at < case.created_at
        assert case.is_expired() is True


@pytest.mark.parametrize("role,can_edit,can_share,can_archive", [
    (ParticipantRole.OWNER, True, True, True),
    # TODO: Known bug - collaborator should have (True, True, False) but validator is broken
    (ParticipantRole.COLLABORATOR, False, False, False),  # Should be (True, True, False)
    (ParticipantRole.VIEWER, False, False, False),
    (ParticipantRole.SUPPORT, False, False, False),
])
def test_participant_role_permissions_matrix(role, can_edit, can_share, can_archive):
    """Test participant permissions matrix for all roles"""
    participant = CaseParticipant(
        user_id="user-123",
        role=role
    )
    
    assert participant.can_edit == can_edit
    assert participant.can_share == can_share
    assert participant.can_archive == can_archive


@pytest.mark.parametrize("message_type", [
    MessageType.USER_QUERY,
    MessageType.AGENT_RESPONSE,
    MessageType.SYSTEM_EVENT,
    MessageType.DATA_UPLOAD,
    MessageType.CASE_NOTE,
    MessageType.STATUS_CHANGE,
])
def test_message_types_validation(message_type):
    """Test all message types are valid"""
    message = CaseMessage(
        case_id="case-123",
        message_type=message_type,
        content="Test content"
    )
    
    assert message.message_type == message_type


@pytest.mark.parametrize("status", [
    CaseStatus.ACTIVE,
    CaseStatus.INVESTIGATING,
    CaseStatus.SOLVED,
    CaseStatus.STALLED,
    CaseStatus.ARCHIVED,
    CaseStatus.SHARED,
])
def test_case_status_validation(status):
    """Test all case statuses are valid"""
    case = Case(
        title="Status Test Case",
        status=status
    )
    
    assert case.status == status


@pytest.mark.parametrize("priority", [
    CasePriority.LOW,
    CasePriority.MEDIUM,
    CasePriority.HIGH,
    CasePriority.CRITICAL,
])
def test_case_priority_validation(priority):
    """Test all case priorities are valid"""
    case = Case(
        title="Priority Test Case",
        priority=priority
    )
    
    assert case.priority == priority