"""Shared pytest fixtures and configuration for FaultMaven tests."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from faultmaven.tools.knowledge_base import KnowledgeBaseTool
from faultmaven.tools.web_search import WebSearchTool
from faultmaven.core.processing.classifier import DataClassifier
from faultmaven.core.processing.log_analyzer import LogProcessor
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.models import AgentState, DataType, SessionContext
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.session_management import SessionManager


@pytest.fixture
def sample_session_context():
    """Sample session context for testing."""
    return SessionContext(
        session_id="test-session-123",
        user_id="user-456",
        created_at=datetime.now(),
        last_activity=datetime.now(),
        agent_state=AgentState.IDLE,
        conversation_history=[],
        uploaded_data=[],
        insights={},
    )


@pytest.fixture
def sample_uploaded_data():
    """Sample uploaded data for testing."""
    return {
        "filename": "test.log",
        "data_type": DataType.SYSTEM_LOGS,
        "size": 1024,
        "uploaded_at": datetime.now(),
        "content": "2024-01-01 12:00:00 ERROR Test error",
    }


@pytest.fixture
def sample_processor_result():
    """Sample processor result for testing."""
    return Mock(
        summary="Test summary",
        insights={
            "error_count": 2,
            "error_rate": 0.4,
            "level_distribution": {"ERROR": 2, "INFO": 3},
            "time_range": {
                "start": "2024-01-01T12:00:00Z",
                "end": "2024-01-01T12:05:00Z",
            },
        },
        anomalies=[{"index": 5, "score": 0.9, "feature": "response_time"}],
        suggested_next_action="Investigate errors",
    )


@pytest.fixture
def mock_llm_router():
    """Mock LLM router for testing."""
    router = Mock()
    router.route.return_value = "Mocked LLM response"
    return router


@pytest.fixture
def mock_chroma_client():
    """Mock ChromaDB client for testing."""
    client = Mock()
    collection = Mock()
    client.get_collection.return_value = collection
    return client, collection


@pytest.fixture
def mock_session_manager():
    """Mock session manager for testing."""
    manager = Mock()
    manager.create_session.return_value = "test-session-id"
    manager.get_session.return_value = sample_session_context()
    manager.update_session.return_value = None
    return manager


@pytest.fixture
def mock_data_classifier():
    """Mock data classifier for testing."""
    classifier = Mock()
    classifier.classify.return_value = DataType.SYSTEM_LOGS
    return classifier


@pytest.fixture
def mock_log_processor():
    """Mock log processor for testing."""
    processor = Mock()
    processor.process.return_value = sample_processor_result()
    return processor


@pytest.fixture
def mock_data_sanitizer():
    """Mock data sanitizer for testing."""
    sanitizer = Mock()
    sanitizer.sanitize.return_value = "Sanitized content"
    sanitizer.is_sensitive.return_value = False
    return sanitizer


@pytest.fixture
def sample_log_data():
    """Sample log data for testing."""
    return """
2024-01-01 12:00:00 ERROR Database connection failed
2024-01-01 12:00:01 INFO Application started successfully
2024-01-01 12:00:02 WARN High memory usage detected
2024-01-01 12:00:03 ERROR Timeout occurred
2024-01-01 12:00:04 DEBUG Processing request
"""


@pytest.fixture
def sample_structured_logs():
    """Sample structured (JSON) logs for testing."""
    return """
{"timestamp": "2024-01-01T12:00:00Z", "level": "ERROR", "message": "DB error", "service": "api"}
{"timestamp": "2024-01-01T12:00:01Z", "level": "INFO", "message": "Request processed", "service": "api"}
{"timestamp": "2024-01-01T12:00:02Z", "level": "WARN", "message": "Slow query", "service": "db"}
{"timestamp": "2024-01-01T12:00:03Z", "level": "ERROR", "message": "Connection lost", "service": "api"}
"""


@pytest.fixture
def sample_knowledge_documents():
    """Sample knowledge base documents for testing."""
    return [
        {
            "document": "Database connection timeout troubleshooting guide",
            "metadata": {"source": "docs/troubleshooting.md", "type": "guide"},
            "distance": 0.1,
        },
        {
            "document": "How to configure connection pooling",
            "metadata": {"source": "docs/config.md", "type": "config"},
            "distance": 0.2,
        },
        {
            "document": "Common database errors and solutions",
            "metadata": {"source": "docs/errors.md", "type": "reference"},
            "distance": 0.3,
        },
    ]


@pytest.fixture
def mock_fireworks_client():
    """Mock Fireworks AI client for testing."""
    client = Mock()
    client.chat.completions.create.return_value = Mock(
        choices=[Mock(message=Mock(content="Fireworks AI response"))]
    )
    return client


@pytest.fixture
def mock_openrouter_client():
    """Mock OpenRouter client for testing."""
    client = Mock()
    client.chat.completions.create.return_value = Mock(
        choices=[Mock(message=Mock(content="OpenRouter response"))]
    )
    return client


@pytest.fixture
def mock_ollama_client():
    """Mock Ollama client for testing."""
    client = Mock()
    client.chat.completions.create.return_value = Mock(
        choices=[Mock(message=Mock(content="Ollama response"))]
    )
    return client


@pytest.fixture
def test_config():
    """Test configuration for FaultMaven."""
    return {
        "llm": {
            "fireworks": {"api_key": "test-key", "model": "test-model"},
            "openrouter": {"api_key": "test-key", "model": "test-model"},
            "ollama": {"base_url": "http://localhost:11434", "model": "llama2"},
        },
        "chromadb": {
            "persist_directory": "./test_chroma",
            "collection_name": "test_collection",
        },
        "session": {
            "timeout": 1800,  # 30 minutes for testing
            "cleanup_interval": 300,  # 5 minutes for testing
        },
        "security": {
            "secret_patterns": {
                "test_key": r"TEST_[A-Z0-9]{16}",
                "test_token": r"TEST_TOKEN_[A-Z0-9]{32}",
            }
        },
    }


# Case persistence fixtures
@pytest.fixture
def sample_case():
    """Sample case for testing case persistence functionality."""
    from faultmaven.models.case import Case, CaseStatus, CasePriority, ParticipantRole
    
    case = Case(
        case_id="test-case-123",
        title="Test Case for Persistence",
        description="A sample case for testing case persistence features",
        owner_id="test-user-456",
        status=CaseStatus.ACTIVE,
        priority=CasePriority.MEDIUM,
        tags=["test", "persistence", "sample"]
    )
    case.add_participant("test-user-456", ParticipantRole.OWNER)
    return case


@pytest.fixture
def sample_case_message():
    """Sample case message for testing."""
    from faultmaven.models.case import CaseMessage, MessageType
    
    return CaseMessage(
        message_id="test-msg-123",
        case_id="test-case-123",
        session_id="test-session-789",
        author_id="test-user-456",
        message_type=MessageType.USER_QUERY,
        content="This is a test message for case persistence testing",
        metadata={"test": True, "source": "pytest"}
    )


@pytest.fixture
def sample_case_participant():
    """Sample case participant for testing."""
    from faultmaven.models.case import CaseParticipant, ParticipantRole
    
    return CaseParticipant(
        user_id="test-collaborator-789",
        role=ParticipantRole.COLLABORATOR,
        added_by="test-user-456"
    )


@pytest.fixture
def sample_case_context():
    """Sample case context for testing."""
    from faultmaven.models.case import CaseContext
    
    return CaseContext(
        problem_description="Test problem for case persistence",
        system_info={
            "os": "Linux",
            "version": "Ubuntu 20.04",
            "memory": "16GB"
        },
        environment_details={
            "env": "test",
            "region": "local"
        },
        uploaded_files=["test.log", "error.log"],
        log_snippets=[
            {
                "timestamp": "2024-01-01T12:00:00Z",
                "level": "ERROR",
                "message": "Test error message"
            }
        ],
        error_patterns=["connection timeout", "memory allocation error"],
        blast_radius_defined=True,
        timeline_established=False,
        hypothesis_formulated=["Network connectivity issue", "Memory leak"],
        root_causes=["Insufficient connection pool"],
        recommendations=["Increase pool size", "Add monitoring"]
    )


@pytest.fixture
def sample_case_summary():
    """Sample case summary for testing list operations."""
    from faultmaven.models.case import CaseSummary, CaseStatus, CasePriority
    from datetime import datetime
    
    return CaseSummary(
        case_id="test-case-123",
        title="Test Case Summary",
        status=CaseStatus.ACTIVE,
        priority=CasePriority.MEDIUM,
        owner_id="test-user-456",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        last_activity_at=datetime.utcnow(),
        message_count=5,
        participant_count=2,
        tags=["test", "summary"]
    )


@pytest.fixture
def mock_case_store():
    """Mock case store for testing."""
    from unittest.mock import AsyncMock, Mock
    
    store = Mock()
    store.create_case = AsyncMock(return_value=True)
    store.get_case = AsyncMock(return_value=None)
    store.update_case = AsyncMock(return_value=True)
    store.delete_case = AsyncMock(return_value=True)
    store.list_cases = AsyncMock(return_value=[])
    store.search_cases = AsyncMock(return_value=[])
    store.add_message_to_case = AsyncMock(return_value=True)
    store.get_case_messages = AsyncMock(return_value=[])
    store.get_user_cases = AsyncMock(return_value=[])
    store.add_case_participant = AsyncMock(return_value=True)
    store.remove_case_participant = AsyncMock(return_value=True)
    store.update_case_activity = AsyncMock(return_value=True)
    store.cleanup_expired_cases = AsyncMock(return_value=0)
    store.get_case_analytics = AsyncMock(return_value={})
    return store


@pytest.fixture
def mock_case_service():
    """Mock case service for testing."""
    from unittest.mock import AsyncMock, Mock
    
    service = Mock()
    service.create_case = AsyncMock()
    service.get_case = AsyncMock(return_value=None)
    service.update_case = AsyncMock(return_value=False)
    service.share_case = AsyncMock(return_value=False)
    service.add_message_to_case = AsyncMock(return_value=False)
    service.get_or_create_case_for_session = AsyncMock(return_value="test-case-123")
    service.link_session_to_case = AsyncMock(return_value=False)
    service.get_case_conversation_context = AsyncMock(return_value="")
    service.resume_case_in_session = AsyncMock(return_value=False)
    service.archive_case = AsyncMock(return_value=False)
    service.list_user_cases = AsyncMock(return_value=[])
    service.search_cases = AsyncMock(return_value=[])
    service.get_case_analytics = AsyncMock(return_value={})
    service.cleanup_expired_cases = AsyncMock(return_value=0)
    return service


@pytest.fixture
def case_create_request_data():
    """Sample case create request data for API testing."""
    return {
        "title": "Test Case Creation",
        "description": "Testing case creation via API",
        "priority": "medium",
        "tags": ["api", "test"],
        "session_id": "test-session-123",
        "initial_message": "Initial problem description for testing"
    }


@pytest.fixture
def case_update_request_data():
    """Sample case update request data for API testing."""
    return {
        "title": "Updated Test Case",
        "description": "Updated description for testing",
        "status": "investigating",
        "priority": "high",
        "tags": ["updated", "important"]
    }


@pytest.fixture
def case_share_request_data():
    """Sample case share request data for API testing."""
    return {
        "user_id": "test-collaborator-789",
        "role": "collaborator",
        "message": "Please help with this case"
    }


@pytest.fixture
def case_search_request_data():
    """Sample case search request data for API testing."""
    return {
        "query": "database connection error",
        "search_in_messages": True,
        "search_in_context": True,
        "filters": {
            "status": "active",
            "priority": "high",
            "limit": 20,
            "offset": 0
        }
    }


@pytest.fixture
def multiple_cases():
    """Multiple sample cases for testing list and search operations."""
    from faultmaven.models.case import Case, CaseStatus, CasePriority, ParticipantRole
    from datetime import datetime, timedelta
    
    cases = []
    for i in range(5):
        case = Case(
            case_id=f"test-case-{i+1:03d}",
            title=f"Test Case {i+1}",
            description=f"Description for test case {i+1}",
            owner_id=f"test-user-{i+1}",
            status=CaseStatus.ACTIVE if i % 2 == 0 else CaseStatus.INVESTIGATING,
            priority=CasePriority.MEDIUM if i % 3 == 0 else CasePriority.HIGH,
            created_at=datetime.utcnow() - timedelta(days=i),
            tags=[f"tag-{i}", "test"]
        )
        case.add_participant(f"test-user-{i+1}", ParticipantRole.OWNER)
        cases.append(case)
    
    return cases


@pytest.fixture
def case_with_conversation():
    """Sample case with a full conversation for testing context generation."""
    from faultmaven.models.case import Case, CaseMessage, MessageType, ParticipantRole
    from datetime import datetime, timedelta
    
    case = Case(
        case_id="test-case-conversation",
        title="Case with Full Conversation",
        description="Testing conversation context generation",
        owner_id="test-user-456"
    )
    case.add_participant("test-user-456", ParticipantRole.OWNER)
    
    # Add conversation messages
    messages = [
        CaseMessage(
            case_id=case.case_id,
            session_id="test-session-1",
            author_id="test-user-456",
            message_type=MessageType.USER_QUERY,
            content="My application is crashing when users try to login",
            timestamp=datetime.utcnow() - timedelta(minutes=60)
        ),
        CaseMessage(
            case_id=case.case_id,
            session_id="test-session-1",
            message_type=MessageType.AGENT_RESPONSE,
            content="I'll help you troubleshoot the login crashes. Can you provide the error logs?",
            timestamp=datetime.utcnow() - timedelta(minutes=59)
        ),
        CaseMessage(
            case_id=case.case_id,
            session_id="test-session-1",
            author_id="test-user-456",
            message_type=MessageType.DATA_UPLOAD,
            content="Here are the application logs from the past 24 hours",
            timestamp=datetime.utcnow() - timedelta(minutes=55)
        ),
        CaseMessage(
            case_id=case.case_id,
            session_id="test-session-1",
            message_type=MessageType.AGENT_RESPONSE,
            content="I can see authentication service timeouts in the logs. Let me check the database connection pool.",
            timestamp=datetime.utcnow() - timedelta(minutes=50)
        ),
        CaseMessage(
            case_id=case.case_id,
            session_id="test-session-2",
            author_id="test-user-456",
            message_type=MessageType.USER_QUERY,
            content="I've restarted the auth service but the issue persists",
            timestamp=datetime.utcnow() - timedelta(minutes=30)
        ),
        CaseMessage(
            case_id=case.case_id,
            session_id="test-session-2",
            message_type=MessageType.AGENT_RESPONSE,
            content="The database connection pool seems to be exhausted. Try increasing the pool size from 10 to 50 connections.",
            timestamp=datetime.utcnow() - timedelta(minutes=25)
        )
    ]
    
    for message in messages:
        case.add_message(message)
    
    return case
