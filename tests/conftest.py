import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import Mock


# NOTE: sqlite3 mock removed - Python 3.11.9 now compiled with libsqlite3-dev support
# If you see "ModuleNotFoundError: No module named '_sqlite3'", rebuild Python with:
#   sudo apt-get install -y libsqlite3-dev
#   pyenv uninstall 3.11.9 && pyenv install 3.11.9


# Mock _ctypes module for Python 3.11 compatibility when libffi is not available
# This is needed for protobuf/chromadb imports that depend on ctypes
if "_ctypes" not in sys.modules:
    _mock_ctypes = ModuleType("_ctypes")

    # Create base types that ctypes expects
    class _MockPointer:
        pass

    class _MockCData:
        pass

    class _MockCFuncPtr:
        """Mock CFuncPtr base class for function pointers"""
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, *args, **kwargs):
            return Mock()

    # Set all required attributes
    _mock_ctypes.Union = type("Union", (_MockCData,), {})
    _mock_ctypes.Structure = type("Structure", (_MockCData,), {})
    _mock_ctypes.Array = type("Array", (_MockCData,), {})
    _mock_ctypes.CFuncPtr = _MockCFuncPtr
    _mock_ctypes._Pointer = _MockPointer
    _mock_ctypes._CData = _MockCData

    # POINTER needs to return a class (not instance) with settable from_param
    # The ctypes module will assign to POINTER(c_type).from_param as a class attribute
    _pointer_cache = {}

    def _mock_POINTER(ctype):
        """Mock POINTER function that returns a pointer type class"""
        # Cache pointer types like real ctypes does
        if ctype not in _pointer_cache:
            # Create a new class that allows from_param to be set as a regular attribute
            # (not a classmethod) since ctypes assigns to it directly
            pointer_class = type(
                f"LP_{ctype.__name__ if hasattr(ctype, '__name__') else 'unknown'}",
                (_MockPointer,),
                {"_type_": ctype}
            )
            _pointer_cache[ctype] = pointer_class

        return _pointer_cache[ctype]

    _mock_ctypes.POINTER = _mock_POINTER
    _mock_ctypes.pointer = Mock()

    # Create type classes with proper __name__ for sizeof and from_param for parameter conversion
    def _make_ctype(name):
        """Factory to create mock ctypes with from_param classmethod"""
        def _from_param(cls, obj):
            return obj
        return type(name, (_MockCData,), {"from_param": classmethod(_from_param)})

    _mock_ctypes.c_int = _make_ctype("c_int")
    _mock_ctypes.c_char_p = _make_ctype("c_char_p")
    _mock_ctypes.c_void_p = _make_ctype("c_void_p")
    _mock_ctypes.c_size_t = _make_ctype("c_size_t")
    _mock_ctypes.c_wchar_p = _make_ctype("c_wchar_p")
    _mock_ctypes.c_wchar = _make_ctype("c_wchar")
    _mock_ctypes.c_char = _make_ctype("c_char")
    _mock_ctypes.c_byte = _make_ctype("c_byte")
    _mock_ctypes.c_ubyte = _make_ctype("c_ubyte")
    _mock_ctypes.c_short = _make_ctype("c_short")
    _mock_ctypes.c_ushort = _make_ctype("c_ushort")
    _mock_ctypes.c_uint = _make_ctype("c_uint")
    _mock_ctypes.c_long = _make_ctype("c_long")
    _mock_ctypes.c_ulong = _make_ctype("c_ulong")
    _mock_ctypes.c_longlong = _make_ctype("c_longlong")
    _mock_ctypes.c_ulonglong = _make_ctype("c_ulonglong")
    _mock_ctypes.c_float = _make_ctype("c_float")
    _mock_ctypes.c_double = _make_ctype("c_double")
    _mock_ctypes.c_longdouble = _make_ctype("c_longdouble")
    _mock_ctypes.c_bool = _make_ctype("c_bool")
    _mock_ctypes.c_int8 = _make_ctype("c_int8")
    _mock_ctypes.c_int16 = _make_ctype("c_int16")
    _mock_ctypes.c_int32 = _make_ctype("c_int32")
    _mock_ctypes.c_int64 = _make_ctype("c_int64")
    _mock_ctypes.c_uint8 = _make_ctype("c_uint8")
    _mock_ctypes.c_uint16 = _make_ctype("c_uint16")
    _mock_ctypes.c_uint32 = _make_ctype("c_uint32")
    _mock_ctypes.c_uint64 = _make_ctype("c_uint64")
    _mock_ctypes.pythonapi = Mock()
    _mock_ctypes.PyDLL = Mock()
    _mock_ctypes.CDLL = Mock()
    _mock_ctypes.LoadLibrary = Mock()

    # CFUNCTYPE and PYFUNCTYPE are function factories that create function prototypes
    # The returned object must be callable and accept an address
    class _MockCFunctionType:
        def __init__(self, restype, *argtypes, **kwargs):
            self.restype = restype
            self.argtypes = argtypes

        def __call__(self, address):
            """Called with function address to create actual function"""
            return Mock()

    def _mock_cfunctype(restype, *argtypes, **kwargs):
        """Mock CFUNCTYPE that returns a callable prototype"""
        return _MockCFunctionType(restype, *argtypes, **kwargs)

    def _mock_pyfunctype(restype, *argtypes, **kwargs):
        """Mock PYFUNCTYPE that returns a callable prototype"""
        return _MockCFunctionType(restype, *argtypes, **kwargs)

    _mock_ctypes.CFUNCTYPE = _mock_cfunctype
    _mock_ctypes.PYFUNCTYPE = _mock_pyfunctype

    # Create proper sizeof that returns correct sizes for different types
    def _mock_sizeof(obj):
        """Mock sizeof that returns correct sizes for ctypes"""
        type_sizes = {
            "c_char": 1,
            "c_byte": 1,
            "c_ubyte": 1,
            "c_bool": 1,
            "c_short": 2,
            "c_ushort": 2,
            "c_int": 4,
            "c_uint": 4,
            "c_long": 8,
            "c_ulong": 8,
            "c_longlong": 8,
            "c_ulonglong": 8,
            "c_float": 4,
            "c_double": 8,
            "c_void_p": 8,
            "c_char_p": 8,
            "c_wchar_p": 8,
            "c_size_t": 8,
        }
        # Try to get size from type name
        if hasattr(obj, "__name__"):
            return type_sizes.get(obj.__name__, 8)
        # Default to pointer size
        return 8

    _mock_ctypes.sizeof = _mock_sizeof
    _mock_ctypes.addressof = Mock(return_value=0)
    _mock_ctypes.byref = Mock()
    _mock_ctypes.create_string_buffer = Mock()
    _mock_ctypes.create_unicode_buffer = Mock()
    _mock_ctypes.cast = Mock()
    _mock_ctypes.get_errno = Mock(return_value=0)
    _mock_ctypes.set_errno = Mock()

    # Additional _ctypes attributes
    _mock_ctypes.ArgumentError = Exception
    _mock_ctypes.CTYPES_MAX_ARGCOUNT = 1024
    _mock_ctypes.FUNCFLAG_CDECL = 1
    _mock_ctypes.FUNCFLAG_PYTHONAPI = 2
    _mock_ctypes.FUNCFLAG_USE_ERRNO = 4
    _mock_ctypes.FUNCFLAG_USE_LASTERROR = 8
    _mock_ctypes.PyObj_FromPtr = Mock()
    _mock_ctypes.Py_DECREF = Mock()
    _mock_ctypes.Py_INCREF = Mock()
    _mock_ctypes.RTLD_GLOBAL = 256
    _mock_ctypes.RTLD_LOCAL = 0
    _mock_ctypes.SIZEOF_TIME_T = 8
    _mock_ctypes.alignment = Mock(return_value=8)
    _mock_ctypes.buffer_info = Mock()
    _mock_ctypes.call_cdeclfunction = Mock()
    _mock_ctypes.call_function = Mock()
    _mock_ctypes.dlclose = Mock()
    _mock_ctypes.dlopen = Mock()
    _mock_ctypes.dlsym = Mock()
    _mock_ctypes.resize = Mock()
    _mock_ctypes.__version__ = "1.1.0"

    # _SimpleCData is the base class for simple C data types
    # It needs from_param as a classmethod
    def _simple_from_param(cls, obj):
        return obj

    _mock_ctypes._SimpleCData = type("_SimpleCData", (_MockCData,), {
        "from_param": classmethod(_simple_from_param)
    })
    _mock_ctypes._pointer_type_cache = {}
    _mock_ctypes._memmove_addr = Mock()
    _mock_ctypes._memset_addr = Mock()
    _mock_ctypes._string_at_addr = Mock()
    _mock_ctypes._cast_addr = Mock()

    sys.modules["_ctypes"] = _mock_ctypes


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
        apm_integration=SimpleNamespace(
            get_export_statistics=lambda: {"enabled": False}
        ),
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
# Create a proper mock sklearn module with __spec__ to satisfy transformers
_mock_sklearn = ModuleType("sklearn")
_mock_sklearn.__spec__ = SimpleNamespace(
    name="sklearn",
    loader=None,
    origin=None,
    submodule_search_locations=None,
)
sys.modules.setdefault("sklearn", _mock_sklearn)
sys.modules.setdefault("sklearn.ensemble", SimpleNamespace(IsolationForest=Mock))
sys.modules.setdefault("sklearn.preprocessing", SimpleNamespace(StandardScaler=Mock))
# NOTE: chromadb stub removed - tests need real ChromaDB
# If chromadb is not installed, tests using it will fail as expected
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

# NOTE: Heavy imports moved to lazy fixtures to avoid loading ML dependencies
# during test collection. This prevents sklearn.__spec__ errors in packaging tests.
# from faultmaven.infrastructure.llm.router import LLMRouter  # Lazy import in fixture
# from faultmaven.modules.agent.tools.knowledge_base import KnowledgeBaseTool  # Lazy import
# from faultmaven.modules.agent.tools.web_search import WebSearchTool  # Lazy import

# Lightweight imports safe for test collection
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.models import DataType, SessionContext
from faultmaven.models.common import AgentStateEnum as AgentState

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
        "user_feedback": "",
    }


@pytest.fixture(scope="function")
def reset_container():
    """Reset the DI container before each test"""
    # Import here to avoid circular dependencies
    from faultmaven.container import container

    # Reset container state
    container.reset()

    # Ensure SKIP_SERVICE_CHECKS is set for tests
    os.environ["SKIP_SERVICE_CHECKS"] = "true"

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
    from faultmaven.modules.case.domain.models import Case, CaseStatus

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
    from datetime import datetime, timezone

    from faultmaven.models.api_models import CaseMessage

    return CaseMessage(
        message_id="test-msg-123",
        case_id="case_test12345678",
        turn_number=1,
        role="user",
        content="This is a test message for case persistence testing",
        created_at=datetime.now(timezone.utc),
        author_id="test-user-456",
        metadata={"test": True, "source": "pytest"},
    )


@pytest.fixture
def sample_case_participant():
    """Sample case participant for testing."""
    from datetime import datetime, timezone

    from faultmaven.models.api_models import CaseParticipant

    return CaseParticipant(
        user_id="test-collaborator-789",
        role="collaborator",
        added_at=datetime.now(timezone.utc),
        added_by="test-user-456",
    )


@pytest.fixture
def sample_case_summary():
    """Sample case summary for testing list operations."""
    from datetime import datetime, timezone

    from faultmaven.models.api_models import CaseSummary
    from faultmaven.modules.case.domain.models import CaseStatus

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
        "initial_message": "Initial problem description for testing",
    }


@pytest.fixture
def case_update_request_data():
    """Sample case update request data for API testing."""
    return {
        "title": "Updated Test Case",
        "description": "Updated description for testing",
        "status": "investigating",
        "priority": "high",
        "tags": ["updated", "important"],
    }


@pytest.fixture
def case_share_request_data():
    """Sample case share request data for API testing."""
    return {
        "user_id": "test-collaborator-789",
        "role": "collaborator",
        "message": "Please help with this case",
    }


@pytest.fixture
def case_search_request_data():
    """Sample case search request data for API testing."""
    return {
        "query": "database connection error",
        "search_in_messages": True,
        "search_in_context": True,
        "filters": {"status": "active", "priority": "high", "limit": 20, "offset": 0},
    }


@pytest.fixture
def multiple_cases():
    """Multiple sample cases for testing list and search operations."""
    from faultmaven.modules.case.domain.models import Case, CaseStatus

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
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from faultmaven.modules.case.domain.models import Case, CaseStatus

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
        },
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
