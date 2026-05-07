"""
Integration test configuration and fixtures.

Provides shared fixtures for integration tests that interact with the
full application stack via docker-compose. Enhanced with Phase 2
integration testing capabilities including memory, planning, reasoning,
knowledge, and orchestration system testing.
"""

import os

# CRITICAL: Import root conftest mocks FIRST before any other imports
# This ensures _ctypes and _sqlite3 mocks are loaded before chromadb/protobuf imports
# The import chain: CaseService -> persistence -> chromadb -> protobuf -> ctypes -> _ctypes
# Must happen BEFORE any faultmaven imports that might trigger this chain
import sys

# Add parent directory to path to import root conftest mocks
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
# Now safe to import other modules
import asyncio
import io
import time
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator, Dict
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

# Import root conftest to load _ctypes and _sqlite3 mocks BEFORE any other imports
import conftest as root_conftest  # noqa: F401
import httpx
import pytest
import pytest_asyncio

# Conditional Redis import - only available in enterprise edition
try:
    import redis.asyncio as redis

    REDIS_AVAILABLE = True
except ImportError:
    redis = None
    REDIS_AVAILABLE = False

# WorkflowContext removed - using dict instead
from faultmaven.exceptions import ServiceException, ValidationException
from faultmaven.models.interfaces import (
    ILLMProvider,
    ISanitizer,
    ITracer,
    IVectorStore,
)
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService as SessionService,
)

# Conditional RedisSessionStore import - only available in enterprise edition
if REDIS_AVAILABLE:
    from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
        RedisSessionStore,
    )
else:
    RedisSessionStore = None

# Legacy services/domain/* was removed; use extracted module path.
# This import triggers: CaseService -> persistence -> chromadb -> protobuf -> ctypes
# But _ctypes mock is already loaded from root conftest above
from faultmaven.modules.case.domain.services.case_service import CaseService

from .mock_servers import MockServerManager

# Configure pytest-asyncio to fix deprecation warnings
pytest_asyncio.asyncio_default_fixture_loop_scope = "function"
pytest_asyncio.asyncio_default_test_loop_scope = "function"

# Test configuration
BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8090")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
REDIS_URL = (
    f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}"
    if REDIS_PASSWORD
    else f"redis://{REDIS_HOST}:{REDIS_PORT}"
)
TIMEOUT = 30.0  # seconds


@pytest_asyncio.fixture(scope="function")
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client for making requests to the API."""
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=TIMEOUT, follow_redirects=True
    ) as client:
        yield client


@pytest_asyncio.fixture(scope="function")
async def test_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Alias for http_client for backwards compatibility with test_evidence_flow.py."""
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=TIMEOUT, follow_redirects=True
    ) as client:
        yield client


@pytest.fixture
def mock_authenticated_user():
    """Create a mock authenticated user for tests."""
    from faultmaven.modules.auth.contracts import UserDTO

    return UserDTO(
        user_id="test_user_123",
        username="test_user",
        email="test@example.com",
        display_name="Test User",
        is_active=True,
        roles=["user"],
    )


@pytest.fixture
def auth_headers() -> Dict[str, str]:
    """Provide mock authentication headers for integration tests.

    For actual integration tests against a running service, you would need
    to either:
    1. Generate a real JWT token from the auth service
    2. Use a test user token
    3. Mock the authentication dependency

    For now, providing placeholder headers that tests can use.
    Tests should mock the require_authentication dependency.
    """
    return {
        "Authorization": "Bearer test_token_placeholder",
        "Content-Type": "application/json",
    }


@pytest.fixture
def mock_case_service():
    """Create a mock case service for tests."""
    from unittest.mock import AsyncMock, MagicMock

    from faultmaven.modules.case.contracts import Case, CaseStatus

    service = AsyncMock()

    # Store created cases to maintain state across create_case and get_case calls
    created_cases = {}

    # Mock create_case to return a simple case
    async def create_case_mock(title=None, description=None, owner_id=None, **kwargs):
        case_id = f"case_{uuid4().hex[:12]}"
        case = Case(
            case_id=case_id,
            title=title or "Test Case",
            description=description or "Test Description",
            user_id=owner_id or "test_user_123",
            organization_id="test_org_123",
            status=CaseStatus.INQUIRY,
            current_turn=0,
            message_count=0,
        )
        # Store case for later retrieval
        created_cases[case_id] = case
        return case

    service.create_case = create_case_mock

    # Mock get_case to return the case (with evidence if it exists)
    async def get_case_mock(case_id, user_id):
        # Return the stored case if it exists, otherwise create a new one
        if case_id in created_cases:
            return created_cases[case_id]
        else:
            case = Case(
                case_id=case_id,
                title="Test Case",
                description="Test Description",
                user_id=user_id,
                organization_id="test_org_123",
                status=CaseStatus.INVESTIGATING,
                current_turn=1,
                message_count=1,
            )
            created_cases[case_id] = case
            return case

    service.get_case = get_case_mock

    return service


@pytest.fixture
def mock_session_service():
    """Create a mock session service for tests."""
    from unittest.mock import AsyncMock

    service = AsyncMock()
    return service


@pytest.fixture
def mock_data_service():
    """Create a mock data preprocessing service for tests."""
    from unittest.mock import AsyncMock

    service = AsyncMock()

    # Mock ingest_data to return preprocessed data
    async def ingest_data_mock(content, session_id, file_name, file_size, context):
        data_id = f"data_{uuid4().hex[:12]}"

        # Determine data type from file name
        data_type = "logs"
        category = "SYMPTOM_EVIDENCE"

        if (
            ".yaml" in file_name
            or ".json" in file_name
            or "config" in file_name.lower()
        ):
            data_type = "configuration"
            category = "CONTEXTUAL_EVIDENCE"
        elif "metrics" in file_name.lower():
            data_type = "metrics"
            category = "SYMPTOM_EVIDENCE"

        return {
            "data_id": data_id,
            "data_type": data_type,
            "content": content,
            "file_size": file_size,
            "insights": {
                "brief_summary": f"Test {data_type} ingested from {file_name}"
            },
            "classification": {"type": category, "confidence": 0.95},
        }

    service.ingest_data = ingest_data_mock

    return service


@pytest.fixture
def mock_investigation_service(mock_case_service):
    """Create a mock investigation service for tests."""
    from unittest.mock import AsyncMock

    from faultmaven.modules.case.contracts import CaseStatus

    service = AsyncMock()

    # Mock process_turn to return a TurnResponse
    async def process_turn_mock(case_id, user_id, payload):
        import hashlib as _hashlib

        from faultmaven.models.api_models import AttachmentResult, TurnResponse
        from faultmaven.modules.case.contracts import (
            Evidence,
            EvidenceCategory,
            EvidenceForm,
            EvidenceSourceType,
            UploadedFile,
        )

        attachments_processed = []

        # Simulate side effect: add UploadedFile + Evidence if payload has attachments
        if hasattr(payload, "has_attachments") and payload.has_attachments:
            case = await mock_case_service.get_case(case_id, user_id)
            if case:
                for attachment in payload.attachments:
                    filename = attachment.filename
                    content_ref = f"s3://bucket/{filename}"

                    # Determine source_type from filename
                    source_type = EvidenceSourceType.LOGS
                    data_type = "logs"
                    if any(
                        ext in filename.lower()
                        for ext in [".yaml", ".yml", ".json", "config"]
                    ):
                        source_type = EvidenceSourceType.CONFIGURATION
                        data_type = "configuration"
                    elif "metrics" in filename.lower():
                        source_type = EvidenceSourceType.METRICS
                        data_type = "metrics"

                    # Create UploadedFile for the raw file. content_hash and
                    # storage_ref live here now (file-intrinsic metadata);
                    # Evidence references the file via source_file_id FK.
                    file_id = f"file_{uuid4().hex[:12]}"
                    uploaded_file = UploadedFile(
                        file_id=file_id,
                        filename=filename,
                        size_bytes=len(attachment.content),
                        uploaded_at_turn=1,
                        upload_source="file_upload",
                        storage_ref=content_ref,
                        content_hash=_hashlib.sha256(b"Test content").hexdigest(),
                    )
                    case.uploaded_files.append(uploaded_file)

                    # Duplicate detection now reads from evidence.extract
                    # (was preprocessed_content before the rename)
                    is_duplicate = any(
                        ev.extract == "Test content" for ev in case.evidence
                    )
                    is_irrelevant = "random" in filename.lower()

                    category = EvidenceCategory.SYMPTOM_EVIDENCE
                    summary = "Log analysis: connection timeout error found"
                    primary_purpose = "symptom_verified"

                    if is_duplicate:
                        category = EvidenceCategory.REJECTED
                        summary = "Duplicate file: already processed this content"
                        primary_purpose = "duplicate_ignored"
                    elif is_irrelevant:
                        category = EvidenceCategory.REJECTED
                        summary = (
                            "Irrelevant file: content not related to investigation"
                        )
                        primary_purpose = "irrelevant_ignored"

                    # Create Evidence linked to the upload via source_file_id
                    ev = Evidence(
                        evidence_id=f"ev_{uuid4().hex[:12]}",
                        extract="Test content",
                        category=category,
                        source_type=source_type,
                        form=EvidenceForm.DOCUMENT,
                        summary=summary,
                        collected_at_turn=1,
                        collected_by="system",
                        source_file_id=file_id,
                        primary_purpose=primary_purpose,
                    )
                    case.evidence.append(ev)

                    attachments_processed.append(
                        AttachmentResult(
                            evidence_id=ev.evidence_id,
                            filename=filename,
                            source_type=data_type,
                            file_size=len(attachment.content),
                            processing_status="completed",
                        )
                    )

        return TurnResponse(
            agent_response="Test response",
            turn_number=1,
            milestones_completed=[],
            case_status=CaseStatus.INVESTIGATING,
            progress_made=True,
            attachments_processed=attachments_processed,
        )

    service.process_turn = process_turn_mock

    return service


@pytest.fixture
def mock_case_vector_store():
    """Create a mock case vector store for tests."""
    from unittest.mock import AsyncMock

    store = AsyncMock()

    # Mock add_documents
    async def add_documents_mock(case_id, documents):
        return True

    store.add_documents = add_documents_mock

    return store


@pytest.fixture
def mock_case_repository():
    """Create a mock case repository for tests."""
    from unittest.mock import AsyncMock

    repo = AsyncMock()
    return repo


@pytest.fixture
def mock_services_for_integration_tests(
    mock_authenticated_user,
    mock_case_service,
    mock_session_service,
    mock_data_service,
    mock_investigation_service,
    mock_case_vector_store,
    mock_case_repository,
):
    """Mock all services for integration tests (opt-in).

    This fixture overrides all service dependencies and sets up app.state with
    mock services, allowing integration tests to run without actual service
    implementations.

    To use this fixture, tests should explicitly request it as a parameter.
    Tests that need to test authentication failures should NOT use this fixture.
    """
    from starlette.testclient import TestClient

    from faultmaven.api.dependencies import get_api_case_service
    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.api.v1.dependencies import (
        get_case_vector_store,
        get_data_service,
        get_investigation_service,
    )
    from faultmaven.main import app
    from faultmaven.modules.case.api.routes import (
        _di_get_case_service_dependency,
        _di_get_session_service_dependency,
        get_case_repository,
        get_case_service,
    )

    # Mock authentication
    async def get_mock_user():
        return mock_authenticated_user

    # Mock service dependencies
    async def get_mock_case_service():
        return mock_case_service

    async def get_mock_session_service():
        return mock_session_service

    async def get_mock_data_service():
        return mock_data_service

    async def get_mock_investigation_service():
        return mock_investigation_service

    async def get_mock_case_vector_store():
        return mock_case_vector_store

    async def get_mock_case_repository():
        return mock_case_repository

    # Override all dependencies BEFORE creating TestClient
    app.dependency_overrides[require_authentication] = get_mock_user
    app.dependency_overrides[get_case_service] = get_mock_case_service
    app.dependency_overrides[get_api_case_service] = get_mock_case_service
    app.dependency_overrides[_di_get_case_service_dependency] = get_mock_case_service
    app.dependency_overrides[_di_get_session_service_dependency] = (
        get_mock_session_service
    )
    app.dependency_overrides[get_data_service] = get_mock_data_service
    app.dependency_overrides[get_investigation_service] = get_mock_investigation_service
    app.dependency_overrides[get_case_vector_store] = get_mock_case_vector_store
    app.dependency_overrides[get_case_repository] = get_mock_case_repository

    # Set app.state for services that access request.app.state directly
    app.state.session_service = mock_session_service
    app.state.case_service = mock_case_service

    # Create TestClient ONCE with all overrides configured
    # Using base_url helps with some edge cases
    test_client = TestClient(app, base_url="http://testserver")

    yield test_client

    # Clean up after test
    test_client.close()
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def redis_client() -> AsyncGenerator:
    """Create a Redis client for testing.

    Requires enterprise edition with redis installed.
    Marked as enterprise-only fixture.
    """
    if not REDIS_AVAILABLE:
        pytest.skip("Redis not available - requires enterprise edition")
    client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD if REDIS_PASSWORD else None,
        db=0,
        decode_responses=True,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
    )
    try:
        # Test connection
        await client.ping()
        yield client
    finally:
        try:
            await client.aclose()
        except Exception:
            pass  # Ignore cleanup errors


@pytest_asyncio.fixture
async def clean_redis(redis_client) -> None:
    """Clean Redis before each test.

    Requires enterprise edition with redis installed.
    """
    if not REDIS_AVAILABLE or redis_client is None:
        return
    try:
        await redis_client.flushdb()
        yield
    finally:
        # Clean up after test
        try:
            await redis_client.flushdb()
        except Exception:
            pass  # Ignore cleanup errors


@pytest.fixture
def sample_log_content() -> str:
    """Sample log content for testing data ingestion."""
    return """
2024-01-15 14:30:25.123 [ERROR] DatabaseConnectionError: 
Connection timeout after 30 seconds
    at ConnectionPool.getConnection(ConnectionPool.java:245)
    at DataService.executeQuery(DataService.java:89)
    at UserController.getUserData(UserController.java:156)
    at RequestHandler.handleRequest(RequestHandler.java:78)
    
2024-01-15 14:30:25.456 [WARN] RetryAttempt: Retrying connection (attempt 1/3)
2024-01-15 14:30:26.789 [ERROR] DatabaseConnectionError: 
Connection timeout after 30 seconds
    at ConnectionPool.getConnection(ConnectionPool.java:245)
    at DataService.executeQuery(DataService.java:89)
    at UserController.getUserData(UserController.java:156)
    at RequestHandler.handleRequest(RequestHandler.java:78)
    
2024-01-15 14:30:27.012 [WARN] RetryAttempt: Retrying connection (attempt 2/3)
2024-01-15 14:30:28.345 [FATAL] SystemShutdown: 
Maximum retry attempts exceeded. Shutting down service.
"""


@pytest.fixture
def sample_kb_document() -> str:
    """Sample knowledge base document with unique searchable content."""
    return """
# Database Connection Troubleshooting Guide

## Overview
This guide helps diagnose and resolve database connection issues.

## Common Symptoms
- Connection timeout errors
- "Too many connections" errors
- Authentication failures

## Diagnostic Steps

### Step 1: Check Connection Pool
The magenta platypus swims at dawn through the connection pool,
verifying that pool size is adequate for current load.

### Step 2: Verify Network Connectivity
- Test ping to database server
- Check firewall rules
- Verify DNS resolution

### Step 3: Authentication
- Verify username/password
- Check SSL certificate validity
- Review authentication logs

## Solutions

### Increase Connection Pool Size
Modify connection pool configuration to handle higher loads.

### Implement Connection Retry Logic
Add exponential backoff for connection attempts.

### Monitor Connection Health
Set up alerts for connection pool exhaustion.

## Related Issues
- Performance degradation
- Memory leaks in connection handling
- Database server resource exhaustion
"""


@pytest.fixture
def sample_query_request() -> Dict[str, Any]:
    """Sample query request for testing troubleshooting."""
    return {
        "query": "What does the magenta platypus do?",
        "context": {"environment": "production", "service": "user-service"},
        "priority": "high",
    }


@pytest_asyncio.fixture
async def test_session(
    http_client: httpx.AsyncClient, clean_redis: None
) -> Dict[str, Any]:
    """Create a test session for use in tests."""
    response = await http_client.post("/api/v1/sessions")
    assert response.status_code == 200

    session_data = response.json()
    assert "session_id" in session_data

    return session_data


@pytest_asyncio.fixture
async def agent_test_session(
    http_client: httpx.AsyncClient,
    clean_redis: None,
    mock_servers: MockServerManager,
) -> Dict[str, Any]:
    """Create a test session with agent capabilities enabled."""
    # Create session
    response = await http_client.post("/api/v1/sessions")
    assert response.status_code == 200

    session_data = response.json()
    assert "session_id" in session_data

    # Upload some test data to the session
    test_log_content = """
2024-01-15 14:30:25.123 [ERROR] DatabaseConnectionError: 
Connection timeout after 30 seconds
    at ConnectionPool.getConnection(ConnectionPool.java:245)
    at DataService.executeQuery(DataService.java:89)
    at UserController.getUserData(UserController.java:156)
    at RequestHandler.handleRequest(RequestHandler.java:78)
    
2024-01-15 14:30:25.456 [WARN] RetryAttempt: Retrying connection (attempt 1/3)
2024-01-15 14:30:28.345 [FATAL] SystemShutdown: 
Maximum retry attempts exceeded. Shutting down service.
"""

    # Upload data to session
    files = {"file": ("database_error.log", test_log_content, "text/plain")}
    data = {
        "session_id": session_data["session_id"],
        "title": "Database Connection Failure",
        "description": "Production database connection timeout issue",
    }

    upload_response = await http_client.post("/api/v1/data/", files=files, data=data)
    assert upload_response.status_code == 200

    return session_data


@pytest.fixture
def mock_file_upload() -> Dict[str, Any]:
    """Mock file upload data for testing."""
    return {
        "file": ("test.log", "sample log content", "text/plain"),
        "title": "Test Log File",
        "document_type": "troubleshooting_guide",
        "tags": "database,connection,error",
    }


def wait_for_service(url: str, timeout: float = 30.0) -> bool:
    """Wait for a service to be ready."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = httpx.get(url, timeout=5.0)
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.TimeoutException):
            time.sleep(1.0)
    return False


async def wait_for_redis(redis_url: str, timeout: float = 5.0) -> bool:
    """Wait for Redis to be ready.

    Requires enterprise edition with redis installed.
    """
    if not REDIS_AVAILABLE:
        return False
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            client = redis.from_url(redis_url)
            await client.ping()
            await client.aclose()
            return True
        except Exception:
            time.sleep(1.0)
    return False


@pytest_asyncio.fixture(scope="function")
async def mock_servers() -> AsyncGenerator[MockServerManager, None]:
    """
    Start and manage mock API servers for the test session.

    This fixture starts mock servers for:
    - LLM APIs (Fireworks, OpenRouter, Ollama)
    - Web Search APIs (Google Custom Search, Tavily)

    The servers run on dynamically allocated ports to avoid conflicts.
    """
    manager = MockServerManager()

    try:
        # Start all mock servers
        await manager.start_all()

        print(f"Mock servers started on ports: {manager.get_ports()}")

        # Set environment variables to point to mock servers
        original_env = {}
        mock_env = {
            "FIREWORKS_API_KEY": "mock_fireworks_key",
            "OPENROUTER_API_KEY": "mock_openrouter_key",
            "WEB_SEARCH_API_KEY": "mock_web_search_key",
            "WEB_SEARCH_ENGINE_ID": "mock_search_engine_id",
            # Override API endpoints to use mock servers
            "FIREWORKS_API_BASE": manager.get_llm_base_url(),
            "OPENROUTER_API_BASE": manager.get_llm_base_url(),
            "OLLAMA_API_BASE": manager.get_llm_base_url(),
            "WEB_SEARCH_API_ENDPOINT": f"{manager.get_web_search_base_url()}/customsearch/v1",
            "TAVILY_API_ENDPOINT": f"{manager.get_web_search_base_url()}/search",
        }

        # Backup original environment and set mock values
        for key, value in mock_env.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value

        yield manager

    finally:
        # Restore original environment
        for key, original_value in original_env.items():
            if original_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original_value

        # Stop all mock servers with proper cleanup
        try:
            await manager.stop_all()
            # Give servers time to shutdown cleanly
            await asyncio.sleep(0.2)
        except Exception as e:
            print(f"Warning: Error stopping mock servers: {e}")


@pytest_asyncio.fixture
async def mock_llm_responses(mock_servers: MockServerManager) -> Dict[str, Any]:
    """
    Fixture providing control over mock LLM responses.

    This can be used to customize responses for specific tests.
    """
    return {
        "troubleshooting_enabled": True,
        "hypothesis_enabled": True,
        "confidence_threshold": 0.8,
        "response_delay": 0.1,
    }


@pytest_asyncio.fixture
async def mock_web_search_responses(mock_servers: MockServerManager) -> Dict[str, Any]:
    """
    Fixture providing control over mock web search responses.

    This can be used to customize search results for specific tests.
    """
    return {
        "search_enabled": True,
        "max_results": 3,
        "response_delay": 0.1,
        "custom_results": {},
    }


@pytest_asyncio.fixture(scope="function", autouse=True)
async def wait_for_services():
    """Wait for all required services to be ready before running tests."""
    import os

    # Skip service checks if in test mode without real services
    if os.environ.get("SKIP_SERVICE_CHECKS", "false").lower() == "true":
        print("Skipping service checks (SKIP_SERVICE_CHECKS=true)")
        return

    # Wait for the backend API
    if not wait_for_service(f"{BASE_URL}/health"):
        pytest.skip(f"Backend API not ready at {BASE_URL}")

    # Wait for Redis (only if available - enterprise edition)
    if REDIS_AVAILABLE and not await wait_for_redis(REDIS_URL):
        pytest.skip(f"Redis not ready at {REDIS_URL}")
    elif not REDIS_AVAILABLE:
        # Skip Redis-dependent setup in community edition
        pass

    print("All services are ready")


def create_test_file(content: str, filename: str = "test.log") -> io.BytesIO:
    """Create a test file object for upload."""
    file_obj = io.BytesIO(content.encode("utf-8"))
    file_obj.name = filename
    return file_obj


@pytest.fixture
def log_file_upload(sample_log_content: str) -> Dict[str, Any]:
    """Create a log file upload for testing."""
    return {
        "file": create_test_file(sample_log_content, "test.log"),
        "description": "Test log file for integration testing",
    }


@pytest.fixture
def kb_document_upload(sample_kb_document: str) -> Dict[str, Any]:
    """Create a knowledge base document upload for testing."""
    return {
        "file": create_test_file(sample_kb_document, "troubleshooting.md"),
        "title": "Database Connection Troubleshooting",
        "document_type": "troubleshooting_guide",
        "tags": "database,connection,troubleshooting",
    }


# Phase 2 Integration Test Fixtures


@pytest.fixture
async def mock_vector_store_integration():
    """Comprehensive mock vector store for Phase 2 integration testing"""
    vector_store = Mock()

    # Enhanced search functionality with realistic responses
    async def mock_search(query, k=10, **kwargs):
        # Simulate different response types based on query content
        if "database" in query.lower():
            return [
                {
                    "id": "db_doc_1",
                    "content": "Database connection pool optimization strategies for high-throughput applications",
                    "metadata": {
                        "source": "database-optimization.md",
                        "type": "guide",
                        "complexity": "advanced",
                    },
                    "score": 0.95,
                },
                {
                    "id": "db_doc_2",
                    "content": "Troubleshooting PostgreSQL connection timeout issues in production environments",
                    "metadata": {
                        "source": "postgres-troubleshooting.md",
                        "type": "troubleshooting",
                        "complexity": "intermediate",
                    },
                    "score": 0.88,
                },
            ]
        elif "performance" in query.lower():
            return [
                {
                    "id": "perf_doc_1",
                    "content": "API performance optimization techniques for microservices architecture",
                    "metadata": {
                        "source": "api-performance.md",
                        "type": "guide",
                        "complexity": "advanced",
                    },
                    "score": 0.92,
                }
            ]
        else:
            return [
                {
                    "id": f"generic_doc_{i}",
                    "content": f"Generic troubleshooting document {i} related to: {query[:50]}",
                    "metadata": {
                        "source": f"generic-{i}.md",
                        "type": "reference",
                        "complexity": "basic",
                    },
                    "score": 0.7 - (i * 0.1),
                }
                for i in range(min(k, 3))
            ]

    vector_store.search = AsyncMock(side_effect=mock_search)
    return vector_store


@pytest.fixture
async def mock_llm_provider_integration():
    """Sophisticated mock LLM provider for Phase 2 integration testing"""
    llm = Mock()

    # Counter to track usage for realistic learning simulation
    call_count = 0
    interaction_history = []

    async def mock_generate(prompt, context=None, **kwargs):
        nonlocal call_count, interaction_history
        call_count += 1

        # Store interaction for learning simulation
        interaction_history.append(
            {
                "prompt": prompt,
                "context": context,
                "timestamp": datetime.utcnow(),
                "call_number": call_count,
            }
        )

        # Simulate different response types based on prompt content
        if "troubleshoot" in prompt.lower() or "diagnose" in prompt.lower():
            return {
                "response": f"Based on the symptoms described, this appears to be a database connectivity issue. Recommended approach: systematic analysis of connection pool and network configuration",
                "confidence": 0.85 + (call_count % 10) / 100,
                "reasoning": f"Analysis based on {len(interaction_history)} previous interactions and domain expertise",
                "issue_type": "database_connectivity",
                "recommendations": [
                    "Immediate: Check system logs for error patterns",
                    "Short-term: Implement monitoring for early detection",
                    "Long-term: Review architecture for resilience",
                ],
            }
        elif "plan" in prompt.lower() or "strategy" in prompt.lower():
            return {
                "response": f"Strategic plan for addressing this issue: systematic troubleshooting approach with phased implementation",
                "confidence": 0.80 + (call_count % 15) / 100,
                "reasoning": "Strategic planning based on best practices and similar cases",
                "plan_type": "systematic_approach",
                "phases": [
                    "Assessment and scoping",
                    "Root cause analysis",
                    "Solution implementation",
                    "Validation and monitoring",
                ],
            }
        else:
            return {
                "response": f"AI analysis of the provided information: {prompt[:100]}...",
                "confidence": 0.70,
                "reasoning": "General analysis without specific domain expertise",
                "analysis_type": "general",
            }

    llm.generate = AsyncMock(side_effect=mock_generate)
    return llm


@pytest.fixture
def sample_complex_workflow_context():
    """Complex workflow context for comprehensive integration testing"""
    return {
        "session_id": "integration-test-session-complex",
        "case_id": "integration-test-case-complex",
        "user_id": "integration-test-user-complex",
        "problem_description": "Complex multi-system issue affecting database performance, API response times, and user authentication across microservices architecture",
        "initial_context": {
            "affected_services": ["user-api", "auth-service", "database-cluster"],
            "environment": "production",
            "infrastructure": "kubernetes",
            "database_type": "postgresql",
            "monitoring_alerts": [
                "High database connection count",
                "API response time SLA breach",
                "Authentication failure rate spike",
            ],
            "recent_changes": [
                "Database schema migration deployed 2 hours ago",
                "Auth service scaling policy updated yesterday",
                "New rate limiting rules activated this morning",
            ],
            "business_impact": {
                "severity": "high",
                "affected_users": 15000,
                "revenue_impact": "moderate",
                "customer_complaints": 47,
            },
        },
        "priority_level": "critical",
        "domain_expertise": "expert",
        "time_constraints": 1800,  # 30 minutes
        "available_tools": [
            "enhanced_knowledge_search",
            "knowledge_discovery",
            "web_search",
            "log_analysis",
        ],
    }


@pytest.fixture
def workflow_test_scenarios():
    """Predefined workflow scenarios for integration testing"""
    return [
        {
            "name": "Database Performance Issue",
            "problem_description": "Database queries are timing out and connection pool is exhausted",
            "context": {
                "service": "user-api",
                "database": "postgresql",
                "environment": "production",
                "symptoms": ["timeouts", "connection_pool_exhaustion", "high_latency"],
            },
            "priority": "high",
            "expected_phases": [
                "define_blast_radius",
                "establish_timeline",
                "formulate_hypothesis",
                "validate_hypothesis",
                "propose_solution",
            ],
            "expected_insights": ["database_performance", "connection_management"],
        },
        {
            "name": "Security Incident",
            "problem_description": "Unauthorized access attempts detected in authentication logs",
            "context": {
                "service": "auth-service",
                "issue_type": "security_breach",
                "environment": "production",
                "symptoms": [
                    "unauthorized_access",
                    "suspicious_logs",
                    "authentication_failures",
                ],
            },
            "priority": "critical",
            "expected_phases": [
                "define_blast_radius",
                "establish_timeline",
                "formulate_hypothesis",
                "validate_hypothesis",
                "propose_solution",
                "verification",
            ],
            "expected_insights": ["security_analysis", "access_patterns"],
        },
    ]


@pytest.fixture
def memory_test_interactions():
    """Pre-defined interactions for memory integration testing"""
    return [
        {
            "session_id": "memory-test-session-1",
            "user_input": "Database connection pool exhausted errors",
            "ai_response": "Increase max_connections and pool_size parameters to resolve connection exhaustion",
            "context": {
                "issue_type": "database_connection",
                "resolution": "parameter_tuning",
                "success": True,
                "resolution_time": 450,
            },
        },
        {
            "session_id": "memory-test-session-2",
            "user_input": "API response times degraded after deployment",
            "ai_response": "Deployment introduced inefficient queries - optimized query performance",
            "context": {
                "issue_type": "performance_degradation",
                "trigger": "deployment",
                "resolution": "query_optimization",
                "success": True,
                "improvement": "60% faster",
            },
        },
    ]


@pytest.fixture
async def integration_test_metrics():
    """Metrics collection for integration testing"""
    metrics = {
        "start_time": time.time(),
        "operations": [],
        "errors": [],
        "performance_data": {},
    }

    def record_operation(operation_type, duration, success=True, metadata=None):
        metrics["operations"].append(
            {
                "type": operation_type,
                "duration": duration,
                "success": success,
                "metadata": metadata or {},
                "timestamp": time.time(),
            }
        )

    def record_error(error_type, error_message, context=None):
        metrics["errors"].append(
            {
                "type": error_type,
                "message": error_message,
                "context": context or {},
                "timestamp": time.time(),
            }
        )

    def get_summary():
        total_time = time.time() - metrics["start_time"]
        total_operations = len(metrics["operations"])
        successful_operations = len(
            [op for op in metrics["operations"] if op["success"]]
        )

        return {
            "total_time": total_time,
            "total_operations": total_operations,
            "successful_operations": successful_operations,
            "success_rate": (
                successful_operations / total_operations if total_operations > 0 else 0
            ),
            "avg_operation_time": (
                sum(op["duration"] for op in metrics["operations"]) / total_operations
                if total_operations > 0
                else 0
            ),
            "total_errors": len(metrics["errors"]),
            "throughput": total_operations / total_time if total_time > 0 else 0,
        }

    metrics["record_operation"] = record_operation
    metrics["record_error"] = record_error
    metrics["get_summary"] = get_summary

    return metrics


# Performance testing utilities
class PerformanceTimer:
    """Utility class for measuring performance in integration tests"""

    def __init__(self, name):
        self.name = name
        self.start_time = None
        self.end_time = None

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_time = time.time()

    @property
    def duration(self):
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return None


@pytest.fixture
def performance_timer():
    """Performance timer utility for integration tests"""
    return PerformanceTimer


# Async utilities for integration testing
class AsyncTestUtilities:
    """Utilities for async integration testing"""

    @staticmethod
    async def run_concurrent_tasks(tasks, max_concurrency=10):
        """Run tasks with controlled concurrency"""
        semaphore = asyncio.Semaphore(max_concurrency)

        async def run_with_semaphore(task):
            async with semaphore:
                return await task

        return await asyncio.gather(
            *[run_with_semaphore(task) for task in tasks], return_exceptions=True
        )

    @staticmethod
    async def measure_async_performance(async_func, *args, **kwargs):
        """Measure performance of async function"""
        start_time = time.time()
        try:
            result = await async_func(*args, **kwargs)
            success = True
            error = None
        except Exception as e:
            result = None
            success = False
            error = str(e)
        end_time = time.time()

        return {
            "result": result,
            "duration": end_time - start_time,
            "success": success,
            "error": error,
        }


@pytest.fixture
def async_test_utils():
    """Async testing utilities for integration tests"""
    return AsyncTestUtilities


# Architectural Compliance Test Fixtures


@pytest_asyncio.fixture
async def session_service() -> SessionService:
    """Create SessionService with real Redis for integration testing

    Note: Uses RedisSessionStore which creates its own Redis client from .env.
    Does NOT clean Redis - tests work with existing data to simulate
    production environment where FLUSHDB may be disabled.

    Requires enterprise edition with redis installed.
    """
    if not REDIS_AVAILABLE or RedisSessionStore is None:
        pytest.skip("RedisSessionStore not available - requires enterprise edition")

    # Create RedisSessionStore - it will use create_redis_client() from .env
    try:
        session_store = RedisSessionStore()
    except Exception as e:
        pytest.skip(f"Redis not reachable - skipping: {e}")

    # Verify Redis connectivity before proceeding
    try:
        client = redis.from_url(REDIS_URL)
        await client.ping()
        await client.aclose()
    except Exception as e:
        pytest.skip(f"Redis not reachable at {REDIS_URL} - skipping: {e}")

    # Create SessionService with session store
    service = SessionService(
        session_store=session_store,
        max_sessions_per_user=10,
        inactive_threshold_hours=24,
    )

    return service


@pytest_asyncio.fixture
async def case_service() -> CaseService:
    """Create CaseService with real dependencies for integration testing

    Note: Uses services from dependency injection container.
    Does NOT clean Redis - tests work with existing data.
    """
    from faultmaven.container import container

    # Ensure container is initialized (idempotent - safe to call multiple times)
    await container.initialize()

    # Return the case service from the container
    service = container.get_case_service()
    if service is None:
        raise RuntimeError(
            "case_service is None - container initialization may have failed"
        )
    return service


@pytest.fixture
def inmemory_session_repository():
    """Create in-memory investigation session repository for testing.

    Used for testing InMemory repository implementation without database.
    Each test gets a fresh repository instance.
    """
    from faultmaven.infrastructure.persistence.investigation_session_repository import (
        InMemoryInvestigationSessionRepository,
    )
    from faultmaven.infrastructure.persistence.repository_factory import (
        reset_inmemory_investigation_session_repository,
    )

    # Reset singleton to ensure fresh state
    reset_inmemory_investigation_session_repository()

    # Return the singleton instance
    return InMemoryInvestigationSessionRepository()
