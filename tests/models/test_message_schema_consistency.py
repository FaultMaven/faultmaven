"""Test module for message schema consistency across all layers.

This module validates that message schema is consistent across:
- Storage (dict format with "created_at")
- CaseMessage model (intermediate representation)
- API Message model (API response)

Per case-storage-design.md Section 4.7, the authoritative schema uses:
- "created_at" (industry standard, not generic "timestamp")
- All fields: message_id, case_id, turn_number, role, content, created_at, author_id, token_count, metadata
"""

import pytest
from datetime import datetime, timezone
from typing import Dict, Any

from faultmaven.models.api_models import CaseMessage
from faultmaven.models.api import Message


# ============================================================
# Schema Consistency Tests
# ============================================================

@pytest.mark.unit
class TestMessageSchemaConsistency:
    """Test message schema consistency across storage, models, and API"""

    def test_storage_schema_has_all_fields(self):
        """Verify storage dict has all required fields per case-storage-design.md Section 4.7"""

        # This is what backend stores
        stored_msg = {
            "message_id": "msg_abc123",
            "case_id": "case_123",
            "turn_number": 1,
            "role": "user",
            "content": "Test message",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "author_id": "user_123",
            "token_count": 42,
            "metadata": {"source": "test"}
        }

        # Verify all fields present
        assert "message_id" in stored_msg
        assert "case_id" in stored_msg
        assert "turn_number" in stored_msg
        assert "role" in stored_msg
        assert "content" in stored_msg
        assert "created_at" in stored_msg
        assert "author_id" in stored_msg
        assert "token_count" in stored_msg
        assert "metadata" in stored_msg

        # Verify old field name doesn't exist
        assert "timestamp" not in stored_msg

    def test_case_message_model_matches_storage(self):
        """Verify CaseMessage model can be created from storage dict"""

        stored_msg = {
            "message_id": "msg_abc123",
            "case_id": "case_123",
            "turn_number": 1,
            "role": "user",
            "content": "Test message",
            "created_at": datetime.now(timezone.utc),  # datetime object
            "author_id": "user_123",
            "token_count": 42,
            "metadata": {"source": "test", "tool": "search"}
        }

        # Should successfully create CaseMessage from storage dict
        case_msg = CaseMessage(**stored_msg)

        # Verify all fields preserved
        assert case_msg.message_id == "msg_abc123"
        assert case_msg.case_id == "case_123"
        assert case_msg.turn_number == 1
        assert case_msg.role == "user"
        assert case_msg.content == "Test message"
        assert case_msg.created_at is not None
        assert case_msg.author_id == "user_123"
        assert case_msg.token_count == 42
        assert case_msg.metadata == {"source": "test", "tool": "search"}

    def test_case_message_field_names(self):
        """Verify CaseMessage uses 'created_at' (industry standard)"""

        case_msg = CaseMessage(
            message_id="msg_123",
            case_id="case_123",
            turn_number=1,
            role="user",
            content="Test",
            created_at=datetime.now(timezone.utc),
            author_id="user_123",
            token_count=10,
            metadata={}
        )

        # Should have created_at field
        assert hasattr(case_msg, 'created_at')

        # Should NOT have timestamp field (old name)
        assert not hasattr(case_msg, 'timestamp')

    def test_api_message_model_has_all_fields(self):
        """Verify API Message model has all fields"""

        api_msg = Message(
            message_id="msg_abc123",
            turn_number=1,
            role="user",
            content="Test message",
            created_at=datetime.now(timezone.utc).isoformat(),
            author_id="user_123",
            token_count=42,
            metadata={"source": "test"}
        )

        # Verify all fields accessible
        assert api_msg.message_id == "msg_abc123"
        assert api_msg.turn_number == 1
        assert api_msg.role == "user"
        assert api_msg.content == "Test message"
        assert api_msg.created_at is not None
        assert api_msg.author_id == "user_123"
        assert api_msg.token_count == 42
        assert api_msg.metadata == {"source": "test"}

    def test_api_message_field_names(self):
        """Verify API Message uses 'created_at' (industry standard)"""

        api_msg = Message(
            message_id="msg_123",
            turn_number=1,
            role="user",
            content="Test",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Should have created_at field
        assert hasattr(api_msg, 'created_at')

        # Should NOT have timestamp field (old name)
        assert not hasattr(api_msg, 'timestamp')

    def test_complete_conversion_pipeline(self):
        """Test complete conversion: Storage → CaseMessage → API Message"""

        # Step 1: Storage format
        stored_msg = {
            "message_id": "msg_abc123",
            "case_id": "case_123",
            "turn_number": 5,
            "role": "assistant",
            "content": "Agent response",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "author_id": "agent",
            "token_count": 100,
            "metadata": {
                "model": "gpt-4",
                "tools_used": ["search", "calculator"]
            }
        }

        # Step 2: Convert to CaseMessage
        # Parse timestamp string to datetime
        timestamp_dt = datetime.fromisoformat(stored_msg["timestamp"].replace("Z", "+00:00"))

        case_msg = CaseMessage(
            message_id=stored_msg["message_id"],
            case_id=stored_msg["case_id"],
            turn_number=stored_msg["turn_number"],
            role=stored_msg["role"],
            content=stored_msg["content"],
            created_at=timestamp_dt,
            author_id=stored_msg["author_id"],
            token_count=stored_msg["token_count"],
            metadata=stored_msg["metadata"]
        )

        # Verify no data loss in CaseMessage
        assert case_msg.turn_number == 5
        assert case_msg.author_id == "agent"
        assert case_msg.token_count == 100
        assert "tools_used" in case_msg.metadata

        # Step 3: Convert to API Message
        api_msg = Message(
            message_id=case_msg.message_id,
            turn_number=case_msg.turn_number,
            role=case_msg.role,
            content=case_msg.content,
            created_at=case_msg.created_at.isoformat(),
            author_id=case_msg.author_id,
            token_count=case_msg.token_count,
            metadata=case_msg.metadata
        )

        # Verify no data loss in API Message
        assert api_msg.turn_number == 5
        assert api_msg.author_id == "agent"
        assert api_msg.token_count == 100
        assert api_msg.metadata is not None
        assert "tools_used" in api_msg.metadata

    def test_optional_fields_none_handling(self):
        """Test that optional fields can be None"""

        # CaseMessage with minimal fields
        case_msg = CaseMessage(
            message_id="msg_123",
            case_id="case_123",
            turn_number=1,
            role="user",
            content="Test",
            created_at=datetime.now(timezone.utc),
            author_id=None,  # ✅ Optional
            token_count=None,  # ✅ Optional
            metadata={}  # ✅ Defaults to empty dict
        )

        assert case_msg.author_id is None
        assert case_msg.token_count is None
        assert case_msg.metadata == {}

        # API Message with minimal fields
        api_msg = Message(
            message_id="msg_123",
            turn_number=1,
            role="user",
            content="Test",
            created_at=datetime.now(timezone.utc).isoformat(),
            author_id=None,  # ✅ Optional
            token_count=None,  # ✅ Optional
            metadata=None  # ✅ Optional
        )

        assert api_msg.author_id is None
        assert api_msg.token_count is None
        assert api_msg.metadata is None

    def test_metadata_preserves_structure(self):
        """Test that metadata dict is preserved through conversions"""

        complex_metadata = {
            "sources": [
                {"type": "kb_search", "query": "database timeout", "results": 3},
                {"type": "web_search", "query": "postgres slow query", "results": 5}
            ],
            "tools_used": ["search_kb", "search_web", "calculator"],
            "reasoning": "Searched knowledge base first, then web for additional context",
            "confidence": 0.85
        }

        # Storage → CaseMessage
        case_msg = CaseMessage(
            message_id="msg_123",
            case_id="case_123",
            turn_number=1,
            role="assistant",
            content="Test",
            created_at=datetime.now(timezone.utc),
            metadata=complex_metadata
        )

        # Verify deep structure preserved
        assert len(case_msg.metadata["sources"]) == 2
        assert case_msg.metadata["sources"][0]["type"] == "kb_search"
        assert len(case_msg.metadata["tools_used"]) == 3
        assert case_msg.metadata["confidence"] == 0.85

        # CaseMessage → API Message
        api_msg = Message(
            message_id=case_msg.message_id,
            turn_number=case_msg.turn_number,
            role=case_msg.role,
            content=case_msg.content,
            created_at=case_msg.created_at.isoformat(),
            metadata=case_msg.metadata
        )

        # Verify still preserved
        assert api_msg.metadata == complex_metadata
        assert api_msg.metadata["sources"][1]["results"] == 5


# ============================================================
# Field Name Regression Tests
# ============================================================

@pytest.mark.unit
class TestFieldNameRegression:
    """Ensure old field name bugs don't return"""

    def test_created_at_not_timestamp_in_storage(self):
        """Regression: Ensure storage uses 'created_at' not 'timestamp'"""

        # This is the CORRECT storage format
        correct_storage = {
            "message_id": "msg_123",
            "case_id": "case_123",
            "turn_number": 1,
            "role": "user",
            "content": "Test",
            "created_at": datetime.now(timezone.utc).isoformat(),  # ✅
        }

        assert "timestamp" in correct_storage
        assert "created_at" not in correct_storage

    def test_case_message_accepts_created_at(self):
        """Regression: CaseMessage must accept 'created_at' field"""

        # Should NOT raise ValidationError
        case_msg = CaseMessage(
            message_id="msg_123",
            case_id="case_123",
            turn_number=1,
            role="user",
            content="Test",
            created_at=datetime.now(timezone.utc)  # ✅ Must work
        )

        assert case_msg.created_at is not None

    def test_api_message_accepts_created_at(self):
        """Regression: API Message must accept 'created_at' field"""

        # Should NOT raise ValidationError
        api_msg = Message(
            message_id="msg_123",
            turn_number=1,
            role="user",
            content="Test",
            created_at=datetime.now(timezone.utc).isoformat()  # ✅ Must work
        )

        assert api_msg.created_at is not None

    def test_turn_number_always_extracted(self):
        """Regression: Ensure turn_number is always extracted from storage"""

        stored_msg = {
            "message_id": "msg_123",
            "case_id": "case_123",
            "turn_number": 42,  # ✅ Must be extracted
            "role": "user",
            "content": "Test",
            "created_at": datetime.now(timezone.utc)
        }

        case_msg = CaseMessage(**stored_msg)
        assert case_msg.turn_number == 42  # ✅ Not 0 or None!

    def test_author_id_preserved(self):
        """Regression: Ensure author_id is preserved through conversions"""

        stored_msg = {
            "message_id": "msg_123",
            "case_id": "case_123",
            "turn_number": 1,
            "role": "user",
            "content": "Test",
            "created_at": datetime.now(timezone.utc),
            "author_id": "user_alice"  # ✅ Must be preserved
        }

        case_msg = CaseMessage(**stored_msg)
        assert case_msg.author_id == "user_alice"

        api_msg = Message(
            message_id=case_msg.message_id,
            turn_number=case_msg.turn_number,
            role=case_msg.role,
            content=case_msg.content,
            created_at=case_msg.created_at.isoformat(),
            author_id=case_msg.author_id  # ✅ Must be passed through
        )
        assert api_msg.author_id == "user_alice"

    def test_metadata_not_lost(self):
        """Regression: Ensure metadata is not lost in conversions"""

        stored_msg = {
            "message_id": "msg_123",
            "case_id": "case_123",
            "turn_number": 1,
            "role": "assistant",
            "content": "Test",
            "created_at": datetime.now(timezone.utc),
            "metadata": {"tools": ["search"], "sources": [1, 2, 3]}
        }

        case_msg = CaseMessage(**stored_msg)
        assert case_msg.metadata is not None
        assert "tools" in case_msg.metadata
        assert len(case_msg.metadata["sources"]) == 3

        api_msg = Message(
            message_id=case_msg.message_id,
            turn_number=case_msg.turn_number,
            role=case_msg.role,
            content=case_msg.content,
            created_at=case_msg.created_at.isoformat(),
            metadata=case_msg.metadata
        )
        assert api_msg.metadata is not None
        assert api_msg.metadata["tools"] == ["search"]
