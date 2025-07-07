"""
Integration test configuration and fixtures.

Provides shared fixtures for integration tests that interact with the
full application stack via docker-compose.
"""

import asyncio
import io
import os
import time
from typing import Any, AsyncGenerator, Dict

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as redis

from .mock_servers import MockServerManager

# Configure pytest-asyncio to fix deprecation warnings
pytest_asyncio.asyncio_default_fixture_loop_scope = "function"
pytest_asyncio.asyncio_default_test_loop_scope = "function"

# Test configuration
BASE_URL = "http://localhost:8000"
REDIS_URL = "redis://localhost:6379"
TIMEOUT = 30.0  # seconds


@pytest_asyncio.fixture(scope="function")
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client for making requests to the API."""
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=TIMEOUT, follow_redirects=True
    ) as client:
        yield client


@pytest_asyncio.fixture(scope="function")
async def redis_client() -> AsyncGenerator[redis.Redis, None]:
    """Create a Redis client for testing."""
    client = redis.Redis(
        host="localhost",
        port=6379,
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
async def clean_redis(redis_client: redis.Redis) -> None:
    """Clean Redis before each test."""
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

    upload_response = await http_client.post(
        "/api/v1/data/", files=files, data=data
    )
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


async def wait_for_redis(redis_url: str, timeout: float = 30.0) -> bool:
    """Wait for Redis to be ready."""
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
    # Wait for the backend API
    if not wait_for_service(f"{BASE_URL}/health"):
        pytest.fail(f"Backend API not ready at {BASE_URL}")

    # Wait for Redis
    if not await wait_for_redis(REDIS_URL):
        pytest.fail(f"Redis not ready at {REDIS_URL}")

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
