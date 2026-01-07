import sys
from types import SimpleNamespace


class _DummyAPMIntegration:
    def __init__(self, *args, **kwargs):
        pass

    def get_export_statistics(self):
        return {"enabled": False}


# Provide a minimal stub for apm_integration to avoid import-side initialization in tests
sys.modules.setdefault(
    "faultmaven.infrastructure.monitoring.apm_integration",
    SimpleNamespace(
        APMIntegration=_DummyAPMIntegration,
        apm_integration=SimpleNamespace(get_export_statistics=lambda: {"enabled": False}),
    ),
)


# Provide a minimal stub for metrics_collector referenced by performance middleware
sys.modules.setdefault(
    "faultmaven.infrastructure.monitoring.metrics_collector",
    SimpleNamespace(
        metrics_collector=SimpleNamespace(
            get_metrics_summary=lambda: {},
            get_dashboard_data=lambda window: {},
            record_performance_metric=lambda *args, **kwargs: None,
        )
    ),
)


# Provide a minimal stub for alerting referenced by performance endpoints
sys.modules.setdefault(
    "faultmaven.infrastructure.monitoring.alerting",
    SimpleNamespace(
        alert_manager=SimpleNamespace(
            get_active_alerts=lambda: [],
            get_alert_statistics=lambda: {},
        )
    ),
)

"""Shared pytest fixtures and configuration for FaultMaven tests."""

import asyncio
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

# Stub heavy dependencies to avoid import issues in tests
# These stubs prevent importing sklearn, chromadb, pypdf, etc.
sys.modules.setdefault("sklearn", SimpleNamespace())
sys.modules.setdefault("sklearn.ensemble", SimpleNamespace(IsolationForest=Mock))
sys.modules.setdefault("sklearn.preprocessing", SimpleNamespace(StandardScaler=Mock))
sys.modules.setdefault("chromadb", SimpleNamespace())
sys.modules.setdefault("pypdf", SimpleNamespace())

sys.modules.setdefault(
    "faultmaven.tools.knowledge_base",
    SimpleNamespace(
        KnowledgeBaseTool=Mock,
    ),
)
sys.modules.setdefault(
    "faultmaven.core.knowledge.ingestion",
    SimpleNamespace(
        KnowledgeIngester=Mock,
    ),
)
sys.modules.setdefault(
    "faultmaven.tools.web_search",
    SimpleNamespace(
        WebSearchTool=Mock,
    ),
)
sys.modules.setdefault(
    "faultmaven.core.processing.log_analyzer",
    SimpleNamespace(
        LogProcessor=Mock,
    ),
)

from faultmaven.modules.agent.tools.knowledge_base import KnowledgeBaseTool
from faultmaven.modules.agent.tools.web_search import WebSearchTool
# from faultmaven.services.preprocessing.classifier import DataClassifier  # May need heavy deps
# from faultmaven.core.processing.log_analyzer import LogProcessor
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.models import AgentState, DataType, SessionContext
from faultmaven.infrastructure.security.redaction import DataSanitizer
# SessionManager has been replaced by SessionService
# from faultmaven.session_management import SessionManager


def create_agent_state_dict(status=None, case_context=None, current_phase="initial"):
    """Helper to create agent state dictionary from enum status"""
    return {
        "status": status or AgentState.IDLE,
        "case_context": case_context or {},
        "current_phase": current_phase,
        "findings": [],
        "recommendations": [],
        "confidence_score": 0.0,
        "tools_used": [],
        "awaiting_user_input": False,
        "user_feedback": ""
    }


@pytest.fixture(scope="function")
def reset_container():
    """Reset the DI container before each test"""
    # Import here to avoid circular dependencies
    from faultmaven.container import container
    
    # Reset container state
    container.reset()
    
    # Ensure SKIP_SERVICE_CHECKS is set for tests
    os.environ['SKIP_SERVICE_CHECKS'] = 'true'
    
    yield container
    
    # Reset again after test
    container.reset()


@pytest.fixture(scope="function")  
def initialized_container(reset_container):
    """Provide a properly initialized container for tests"""
    try:
        reset_container.initialize()
        return reset_container
    except Exception as e:
        # If real initialization fails, create minimal mock container
        from unittest.mock import MagicMock
        mock_container = MagicMock()
        
        # Mock key service methods
        mock_container.get_session_service.return_value = MagicMock()
        mock_container.get_agent_service.return_value = MagicMock()
        mock_container.get_case_service.return_value = MagicMock()
        mock_container.get_knowledge_service.return_value = MagicMock()
        mock_container.get_data_service.return_value = MagicMock()
        
        return mock_container


@pytest.fixture
def sample_session_context():
    """Sample session context for testing."""
    return SessionContext(
        session_id="test-session-123",
        user_id="user-456",
        created_at=datetime.now(),
        last_activity=datetime.now(),
        agent_state=create_agent_state_dict(),
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
    from faultmaven.models.case import Case, CaseStatus

    return Case(
        case_id="case_test12345678",
        title="Test Case for Persistence",
        description="A sample case for testing case persistence features",
        user_id="test-user-456",
        organization_id="test-org-123",
        status=CaseStatus.CONSULTING,
    )


@pytest.fixture
def sample_case_message():
    """Sample case message for testing."""
    from faultmaven.models.api_models import CaseMessage
    from datetime import datetime, timezone

    return CaseMessage(
        message_id="test-msg-123",
        case_id="case_test12345678",
        turn_number=1,
        role="user",
        content="This is a test message for case persistence testing",
        created_at=datetime.now(timezone.utc),
        author_id="test-user-456",
        metadata={"test": True, "source": "pytest"}
    )


@pytest.fixture
def sample_case_participant():
    """Sample case participant for testing."""
    from faultmaven.models.api_models import CaseParticipant
    from datetime import datetime, timezone

    return CaseParticipant(
        user_id="test-collaborator-789",
        role="collaborator",
        added_at=datetime.now(timezone.utc),
        added_by="test-user-456"
    )


@pytest.fixture
def sample_case_summary():
    """Sample case summary for testing list operations."""
    from faultmaven.models.api_models import CaseSummary
    from faultmaven.models.case import CaseStatus
    from datetime import datetime, timezone

    return CaseSummary(
        case_id="case_test12345678",
        title="Test Case Summary",
        status=CaseStatus.CONSULTING,
        user_id="test-user-456",
        organization_id="test-org-123",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        current_turn=0,
        milestones_completed=0,
        total_milestones=8,
        is_stuck=False,
        is_terminal=False,
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
    service.get_case_messages = AsyncMock(return_value=[])
    service.resume_case_in_session = AsyncMock(return_value=False)
    service.archive_case = AsyncMock(return_value=False)
    service.list_user_cases = AsyncMock(return_value=[])
    service.search_cases = AsyncMock(return_value=[])
    service.get_case_analytics = AsyncMock(return_value={})
    service.cleanup_expired_cases = AsyncMock(return_value=0)
    service.hard_delete_case = AsyncMock(return_value=True)
    service.delete_case = AsyncMock(return_value=True)
    service.count_user_cases = AsyncMock(return_value=0)
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
    from faultmaven.models.case import Case, CaseStatus

    cases = []
    for i in range(5):
        case = Case(
            case_id=f"case_{i+1:012x}",
            title=f"Test Case {i+1}",
            description=f"Description for test case {i+1}",
            user_id=f"test-user-{i+1}",
            organization_id="test-org-123",
            status=CaseStatus.CONSULTING if i % 2 == 0 else CaseStatus.INVESTIGATING,
        )
        cases.append(case)

    return cases


@pytest.fixture
def case_with_conversation():
    """Sample case with a full conversation for testing context generation."""
    from faultmaven.models.case import Case, CaseStatus
    from datetime import datetime, timezone, timedelta
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    case_id = "case_conversation1"

    # Create messages as dicts per case-storage-design.md Section 4.7
    messages = [
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 1,
            "role": "user",
            "content": "My application is crashing when users try to login",
            "created_at": (now - timedelta(minutes=60)).isoformat(),
        },
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 1,
            "role": "assistant",
            "content": "I'll help you troubleshoot the login crashes. Can you provide the error logs?",
            "created_at": (now - timedelta(minutes=59)).isoformat(),
        },
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 2,
            "role": "user",
            "content": "Here are the application logs from the past 24 hours",
            "created_at": (now - timedelta(minutes=55)).isoformat(),
        },
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 2,
            "role": "assistant",
            "content": "I can see authentication service timeouts in the logs. Let me check the database connection pool.",
            "created_at": (now - timedelta(minutes=50)).isoformat(),
        },
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 3,
            "role": "user",
            "content": "I've restarted the auth service but the issue persists",
            "created_at": (now - timedelta(minutes=30)).isoformat(),
        },
        {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "turn_number": 3,
            "role": "assistant",
            "content": "The database connection pool seems to be exhausted. Try increasing the pool size from 10 to 50 connections.",
            "created_at": (now - timedelta(minutes=25)).isoformat(),
        }
    ]

    return Case(
        case_id=case_id,
        title="Case with Full Conversation",
        description="Testing conversation context generation",
        user_id="test-user-456",
        organization_id="test-org-123",
        status=CaseStatus.INVESTIGATING,
        messages=messages,
        message_count=len(messages),
        current_turn=3,
    )
