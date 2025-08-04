"""
Test 1: Session Management Integrity

Objective: To verify that the API gateway can correctly create, retrieve, and
interact with user sessions stored in the external Redis instance.

Setup: The backend API server and the Redis container must be running.
"""

import httpx
import pytest
import redis.asyncio as redis


@pytest.mark.asyncio
@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
async def test_session_create_and_retrieve(
    http_client: httpx.AsyncClient, redis_client: redis.Redis, clean_redis: None
):
    """
    Test Steps:
    1. Make a POST request to /api/v1/sessions
    2. Assert that the HTTP response is 200 OK and contains a valid session_id
    3. Use the session_id to make a GET request to
       /api/v1/sessions/{session_id}
    4. Assert that the response is 200 OK and returned session data matches
    """
    # Step 1: Create a session
    response = await http_client.post("/api/v1/sessions")

    # Step 2: Verify session creation response
    assert response.status_code == 200
    session_data = response.json()

    assert "session_id" in session_data
    assert "user_id" in session_data
    assert "created_at" in session_data
    assert "message" in session_data
    assert session_data["message"] == "Session created successfully"

    session_id = session_data["session_id"]

    # Verify session_id is a valid UUID-like string
    assert isinstance(session_id, str)
    assert len(session_id) > 0
    assert "-" in session_id  # UUID format check

    # Step 3: Retrieve the session using the session_id
    # Note: Based on the API structure, we need to access session data
    # through the Redis client directly since there's no GET endpoint
    # for individual sessions in the current API

    # Verify session exists in Redis
    session_key = f"session:{session_id}"
    session_exists = await redis_client.exists(session_key)
    assert session_exists == 1

    # Retrieve session data from Redis
    redis_session_data = await redis_client.get(session_key)
    assert redis_session_data is not None

    # Parse the session data
    import json

    session_context = json.loads(redis_session_data)

    # Step 4: Verify the session data matches what was created
    assert session_context["session_id"] == session_id
    assert "created_at" in session_context
    assert "last_activity" in session_context
    assert "data_uploads" in session_context
    assert "investigation_history" in session_context
    assert isinstance(session_context["data_uploads"], list)
    assert isinstance(session_context["investigation_history"], list)

    # Verify session data is consistent
    assert len(session_context["data_uploads"]) == 0  # New session
    assert len(session_context["investigation_history"]) == 0  # New session


@pytest.mark.asyncio
@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
async def test_session_create_with_user_id(
    http_client: httpx.AsyncClient, redis_client: redis.Redis, clean_redis: None
):
    """
    Test creating a session with a specific user ID.
    """
    # Create a session with a user ID
    response = await http_client.post(
        "/api/v1/sessions", params={"user_id": "test_user_123"}
    )

    assert response.status_code == 200
    session_data = response.json()

    assert "session_id" in session_data
    assert session_data["user_id"] == "test_user_123"

    # Verify in Redis
    session_key = f"session:{session_data['session_id']}"
    redis_session_data = await redis_client.get(session_key)
    assert redis_session_data is not None

    import json

    session_context = json.loads(redis_session_data)
    assert session_context["user_id"] == "test_user_123"


@pytest.mark.asyncio
@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
async def test_session_expiration_and_cleanup(
    http_client: httpx.AsyncClient, redis_client: redis.Redis, clean_redis: None
):
    """
    Test that sessions have proper expiration set in Redis.
    """
    # Create a session
    response = await http_client.post("/api/v1/sessions")
    assert response.status_code == 200

    session_data = response.json()
    session_id = session_data["session_id"]
    session_key = f"session:{session_id}"

    # Check that the session has an expiration time set
    ttl = await redis_client.ttl(session_key)
    assert ttl > 0  # Should have a positive TTL
    assert ttl <= 86400  # Should be <= 24 hours (86400 seconds)


@pytest.mark.asyncio
@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
async def test_multiple_sessions_independence(
    http_client: httpx.AsyncClient, redis_client: redis.Redis, clean_redis: None
):
    """
    Test that multiple sessions are independent and don't interfere.
    """
    # Create first session
    response1 = await http_client.post("/api/v1/sessions")
    assert response1.status_code == 200
    session1_data = response1.json()
    session1_id = session1_data["session_id"]

    # Create second session
    response2 = await http_client.post("/api/v1/sessions")
    assert response2.status_code == 200
    session2_data = response2.json()
    session2_id = session2_data["session_id"]

    # Verify sessions are different
    assert session1_id != session2_id

    # Verify both sessions exist in Redis
    session1_key = f"session:{session1_id}"
    session2_key = f"session:{session2_id}"

    assert await redis_client.exists(session1_key) == 1
    assert await redis_client.exists(session2_key) == 1

    # Verify session data is independent
    import json

    session1_context = json.loads(await redis_client.get(session1_key))
    session2_context = json.loads(await redis_client.get(session2_key))

    assert session1_context["session_id"] == session1_id
    assert session2_context["session_id"] == session2_id
    assert session1_context["session_id"] != session2_context["session_id"]


@pytest.mark.asyncio
@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
async def test_session_list_endpoint(http_client: httpx.AsyncClient, clean_redis: None):
    """
    Test the session listing endpoint.
    """
    # Create a few sessions
    session_ids = []
    for i in range(3):
        response = await http_client.post("/api/v1/sessions")
        assert response.status_code == 200
        session_data = response.json()
        session_ids.append(session_data["session_id"])

    # List all sessions
    response = await http_client.get("/api/v1/sessions")
    assert response.status_code == 200

    sessions_list = response.json()
    assert "sessions" in sessions_list
    assert "total" in sessions_list
    assert sessions_list["total"] == 3

    # Verify all created sessions are in the list
    returned_session_ids = [s["session_id"] for s in sessions_list["sessions"]]
    for session_id in session_ids:
        assert session_id in returned_session_ids


@pytest.mark.asyncio
@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
async def test_session_redis_connection_integrity(
    http_client: httpx.AsyncClient, redis_client: redis.Redis, clean_redis: None
):
    """
    Test that the API and Redis connection is working correctly.
    """
    # Create a session
    response = await http_client.post("/api/v1/sessions")
    assert response.status_code == 200
    session_data = response.json()
    session_id = session_data["session_id"]

    # Verify we can directly access the session via Redis
    session_key = f"session:{session_id}"
    redis_session_data = await redis_client.get(session_key)
    assert redis_session_data is not None

    # Verify session data can be modified via Redis and is consistent
    import json

    session_context = json.loads(redis_session_data)

    # Modify session data
    session_context["test_field"] = "test_value"
    await redis_client.set(session_key, json.dumps(session_context))

    # Retrieve modified data
    modified_data = await redis_client.get(session_key)
    modified_context = json.loads(modified_data)
    assert modified_context["test_field"] == "test_value"

    # Verify session still exists and is valid
    assert modified_context["session_id"] == session_id
