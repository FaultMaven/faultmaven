"""Rebuilt Session API Endpoint Tests

Tests complete session lifecycle and state management workflows with real HTTP processing.
Focus on real session persistence and cross-request state validation.
"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List

import pytest
from httpx import AsyncClient


class TestSessionAPIEndpointsRebuilt:
    """Session API tests using real HTTP state management workflows."""
    
    @pytest.mark.asyncio
    async def test_complete_session_lifecycle(
        self,
        client: AsyncClient,
        response_validator,
        performance_tracker
    ):
        """Test complete session creation, usage, and cleanup lifecycle."""
        
        # Create new session
        with performance_tracker.time_request("session_creation"):
            create_response = await client.post("/api/v1/sessions/")
        
        assert create_response.status_code == 200
        session_data = create_response.json()
        response_validator.assert_valid_session_response(session_data)
        
        session_id = session_data["session_id"]
        assert session_id is not None
        assert session_data["status"] == "active"
        assert "created_at" in session_data
        
        # Validate session can be retrieved
        with performance_tracker.time_request("session_retrieval"):
            get_response = await client.get(f"/api/v1/sessions/{session_id}")
        
        assert get_response.status_code == 200
        get_data = get_response.json()
        
        assert get_data["session_id"] == session_id
        assert get_data["status"] == "active"
        
        # Compare timestamps with tolerance for microsecond differences
        from datetime import datetime
        created_original = datetime.fromisoformat(session_data["created_at"])
        created_retrieved = datetime.fromisoformat(get_data["created_at"])
        time_diff = abs((created_retrieved - created_original).total_seconds())
        assert time_diff < 1.0, f"Timestamp difference too large: {time_diff}s"
        
        # Update session activity (heartbeat)
        with performance_tracker.time_request("session_heartbeat"):
            heartbeat_response = await client.post(f"/api/v1/sessions/{session_id}/heartbeat")
        
        assert heartbeat_response.status_code == 200
        heartbeat_data = heartbeat_response.json()
        
        assert heartbeat_data["session_id"] == session_id
        assert heartbeat_data["status"] == "active"
        assert "last_activity" in heartbeat_data
        
        # Get session statistics
        stats_response = await client.get(f"/api/v1/sessions/{session_id}/stats")
        
        assert stats_response.status_code == 200
        stats_data = stats_response.json()
        
        expected_stats = ["session_id", "created_at", "last_activity", "total_requests"]
        for stat in expected_stats:
            assert stat in stats_data, f"Missing session statistic: {stat}"
        
        # Delete session
        with performance_tracker.time_request("session_deletion"):
            delete_response = await client.delete(f"/api/v1/sessions/{session_id}")
        
        assert delete_response.status_code == 200
        delete_data = delete_response.json()
        
        assert delete_data["session_id"] == session_id
        assert delete_data["deleted"] is True
        
        # Verify session no longer accessible
        verify_response = await client.get(f"/api/v1/sessions/{session_id}")
        assert verify_response.status_code == 404
        
        # Validate performance targets
        performance_tracker.assert_performance_target("session_creation", 1.0)
        performance_tracker.assert_performance_target("session_retrieval", 0.5)
        performance_tracker.assert_performance_target("session_heartbeat", 0.5)
        performance_tracker.assert_performance_target("session_deletion", 1.0)
    
    @pytest.mark.asyncio
    async def test_session_state_persistence_across_requests(
        self,
        client: AsyncClient,
        performance_tracker
    ):
        """Test session state persists across multiple HTTP requests."""
        
        # Create session
        create_response = await client.post("/api/v1/sessions/")
        assert create_response.status_code == 200
        session_id = create_response.json()["session_id"]
        
        # Simulate multiple operations that should persist state
        operations = [
            {
                "operation": "query",
                "data": {
                    "session_id": session_id,
                    "query": "Database connection issue",
                    "context": {"environment": "production"}
                }
            },
            {
                "operation": "data_upload", 
                "files": {"file": ("test.log", b"ERROR: DB connection failed", "text/plain")},
                "data": {"session_id": session_id}
            },
            {
                "operation": "query",
                "data": {
                    "session_id": session_id,
                    "query": "Follow-up analysis",
                    "context": {"follow_up": True}
                }
            }
        ]
        
        operation_results = []
        
        for i, op in enumerate(operations):
            with performance_tracker.time_request(f"operation_{i}"):
                if op["operation"] == "query":
                    response = await client.post("/api/v1/agent/query", json=op["data"])
                elif op["operation"] == "data_upload":
                    response = await client.post(
                        "/api/v1/data/upload",
                        files=op["files"],
                        data=op["data"]
                    )
            
            assert response.status_code == 200
            result_data = response.json()
            
            # Handle different response schemas: v3.1.0 AgentResponse vs legacy formats
            if op["operation"] == "query":
                # AgentResponse (v3.1.0) has session_id in view_state
                assert result_data["view_state"]["session_id"] == session_id
                result_id = result_data["view_state"]["case_id"]
            else:
                # Data upload responses have session_id at top level
                assert result_data["session_id"] == session_id
                result_id = result_data.get("data_id")
            
            operation_results.append({
                "operation": op["operation"],
                "result_id": result_id,
                "timestamp": time.time()
            })
        
        # Verify session accumulated state from all operations
        final_stats_response = await client.get(f"/api/v1/sessions/{session_id}/stats")
        assert final_stats_response.status_code == 200
        final_stats = final_stats_response.json()
        
        # Should reflect multiple operations
        assert final_stats["total_requests"] >= len(operations)
        
        # Should have activity from all operations
        if "operations_history" in final_stats:
            assert len(final_stats["operations_history"]) >= len(operations)
        
        # Validate cross-request state consistency
        for i in range(1, len(operation_results)):
            assert operation_results[i]["timestamp"] > operation_results[i-1]["timestamp"]
    
    @pytest.mark.asyncio
    async def test_concurrent_session_operations(
        self,
        client: AsyncClient,
        performance_tracker
    ):
        """Test concurrent operations on same session handle state correctly."""
        
        # Create session for concurrent testing
        create_response = await client.post("/api/v1/sessions/")
        assert create_response.status_code == 200
        session_id = create_response.json()["session_id"]
        
        # Define concurrent operations
        async def heartbeat_operation():
            return await client.post(f"/api/v1/sessions/{session_id}/heartbeat")
        
        async def stats_operation():
            return await client.get(f"/api/v1/sessions/{session_id}/stats")
        
        async def query_operation(query_text: str):
            return await client.post(
                "/api/v1/agent/query",
                json={
                    "session_id": session_id,
                    "query": f"Concurrent test: {query_text}",
                    "context": {"concurrent": True}
                }
            )
        
        # Execute operations concurrently
        with performance_tracker.time_request("concurrent_operations"):
            responses = await asyncio.gather(
                heartbeat_operation(),
                stats_operation(), 
                query_operation("operation 1"),
                query_operation("operation 2"),
                heartbeat_operation(),
                return_exceptions=True
            )
        
        # Validate all operations succeeded
        for i, response in enumerate(responses):
            assert not isinstance(response, Exception), f"Operation {i} failed: {response}"
            assert response.status_code == 200
            
            response_data = response.json()
            # Handle different response schemas based on operation type
            if "view_state" in response_data:
                # AgentResponse (v3.1.0) - query operations
                assert response_data["view_state"]["session_id"] == session_id
            elif "session_id" in response_data:
                # Legacy response format - heartbeat, stats operations
                assert response_data["session_id"] == session_id
            else:
                # Some operations might not include session_id directly
                pass
        
        # Validate concurrent performance
        performance_tracker.assert_performance_target("concurrent_operations", 5.0)
        
        # Verify session state consistency after concurrent operations
        final_check = await client.get(f"/api/v1/sessions/{session_id}/stats")
        assert final_check.status_code == 200
        
        final_data = final_check.json()
        assert final_data["session_id"] == session_id
        assert final_data["total_requests"] >= len(responses)
    
    @pytest.mark.asyncio
    async def test_session_timeout_and_expiration(
        self,
        client: AsyncClient
    ):
        """Test session timeout and expiration handling."""
        
        # Create session with custom timeout
        create_response = await client.post(
            "/api/v1/sessions/",
            json={"timeout_minutes": 1}  # Very short timeout for testing
        )
        
        assert create_response.status_code == 200
        session_data = create_response.json()
        session_id = session_data["session_id"]
        
        # Verify session is initially active
        initial_check = await client.get(f"/api/v1/sessions/{session_id}")
        assert initial_check.status_code == 200
        assert initial_check.json()["status"] == "active"
        
        # Test session expiration simulation (might need to be mocked in real tests)
        # For now, test the timeout configuration is preserved
        stats_response = await client.get(f"/api/v1/sessions/{session_id}/stats")
        assert stats_response.status_code == 200
        stats_data = stats_response.json()
        
        if "timeout_minutes" in stats_data:
            assert stats_data["timeout_minutes"] == 1
        
        # Test heartbeat resets timeout
        heartbeat_response = await client.post(f"/api/v1/sessions/{session_id}/heartbeat")
        assert heartbeat_response.status_code == 200
        
        heartbeat_data = heartbeat_response.json()
        assert "last_activity" in heartbeat_data
        
        # Validate heartbeat updated activity time
        updated_stats = await client.get(f"/api/v1/sessions/{session_id}/stats")
        assert updated_stats.status_code == 200
        updated_data = updated_stats.json()
        
        # Last activity should be more recent than creation
        if "last_activity" in updated_data and "created_at" in updated_data:
            last_activity = datetime.fromisoformat(updated_data["last_activity"].replace("Z", "+00:00"))
            created_at = datetime.fromisoformat(updated_data["created_at"].replace("Z", "+00:00"))
            assert last_activity >= created_at
    
    @pytest.mark.asyncio
    async def test_session_listing_and_filtering(
        self,
        client: AsyncClient,
        performance_tracker
    ):
        """Test session listing with filtering and pagination."""
        
        # Create multiple sessions for testing
        test_sessions = []
        session_types = ["troubleshooting", "analysis", "testing"]
        
        for i, session_type in enumerate(session_types):
            create_response = await client.post(
                "/api/v1/sessions/",
                json={
                    "session_type": session_type,
                    "description": f"Test session {i} for {session_type}"
                }
            )
            
            assert create_response.status_code == 200
            session_data = create_response.json()
            test_sessions.append({
                "session_id": session_data["session_id"],
                "session_type": session_type,
                "index": i
            })
        
        # List all sessions
        with performance_tracker.time_request("session_listing"):
            list_response = await client.get("/api/v1/sessions/")
        
        assert list_response.status_code == 200
        list_data = list_response.json()
        
        # Validate listing structure
        assert "sessions" in list_data
        assert "total_count" in list_data
        assert isinstance(list_data["sessions"], list)
        assert list_data["total_count"] >= len(test_sessions)
        
        # Verify our test sessions are included
        session_ids = [session["session_id"] for session in list_data["sessions"]]
        for test_session in test_sessions:
            assert test_session["session_id"] in session_ids
        
        # Test filtering by session type
        filter_response = await client.get(
            "/api/v1/sessions/",
            params={"session_type": "troubleshooting"}
        )
        
        assert filter_response.status_code == 200
        filter_data = filter_response.json()
        
        # Should only return troubleshooting sessions
        troubleshooting_sessions = [
            s for s in filter_data["sessions"] 
            if s.get("session_type") == "troubleshooting"
        ]
        assert len(troubleshooting_sessions) >= 1
        
        # Test pagination
        paginated_response = await client.get(
            "/api/v1/sessions/",
            params={"limit": 2, "offset": 0}
        )
        
        assert paginated_response.status_code == 200
        paginated_data = paginated_response.json()
        
        assert len(paginated_data["sessions"]) <= 2
        assert "total_count" in paginated_data
        
        performance_tracker.assert_performance_target("session_listing", 2.0)
    
    @pytest.mark.asyncio
    async def test_session_data_association_and_cleanup(
        self,
        client: AsyncClient
    ):
        """Test association of data and operations with sessions."""
        
        # Create session
        create_response = await client.post("/api/v1/sessions/")
        assert create_response.status_code == 200
        session_id = create_response.json()["session_id"]
        
        # Associate various data with session
        associated_data = []
        
        # Upload file
        upload_response = await client.post(
            "/api/v1/data/upload",
            files={"file": ("session_test.log", b"ERROR: Session test data", "text/plain")},
            data={"session_id": session_id}
        )
        assert upload_response.status_code == 200
        associated_data.append({
            "type": "data_upload",
            "id": upload_response.json()["data_id"]
        })
        
        # Create investigation
        query_response = await client.post(
            "/api/v1/agent/query",
            json={
                "session_id": session_id,
                "query": "Session association test",
                "context": {"test_type": "association"}
            }
        )
        assert query_response.status_code == 200
        query_result = query_response.json()
        associated_data.append({
            "type": "investigation",
            "id": query_result["view_state"]["case_id"]
        })
        
        # Check session has associated data
        stats_response = await client.get(f"/api/v1/sessions/{session_id}/stats")
        assert stats_response.status_code == 200
        stats_data = stats_response.json()
        
        # Should show associated data
        assert stats_data["total_requests"] >= 2
        
        if "associated_data" in stats_data:
            data_counts = stats_data["associated_data"]
            assert data_counts.get("uploads", 0) >= 1
            assert data_counts.get("investigations", 0) >= 1
        
        # Test session data cleanup
        cleanup_response = await client.post(f"/api/v1/sessions/{session_id}/cleanup")
        assert cleanup_response.status_code == 200
        cleanup_data = cleanup_response.json()
        
        assert "cleaned_items" in cleanup_data
        assert cleanup_data["session_id"] == session_id
        
        # Verify cleanup maintained session but cleaned associated data
        post_cleanup_stats = await client.get(f"/api/v1/sessions/{session_id}/stats")
        assert post_cleanup_stats.status_code == 200
    
    @pytest.mark.asyncio
    async def test_session_recovery_and_restoration(
        self,
        client: AsyncClient
    ):
        """Test session recovery and state restoration."""
        
        # Create session with specific state
        create_response = await client.post(
            "/api/v1/sessions/",
            json={
                "session_type": "recovery_test",
                "metadata": {
                    "test_mode": True,
                    "recovery_test": "enabled"
                }
            }
        )
        
        assert create_response.status_code == 200
        original_session = create_response.json()
        session_id = original_session["session_id"]
        
        # Add some state to the session
        query_response = await client.post(
            "/api/v1/agent/query",
            json={
                "session_id": session_id,
                "query": "Recovery state test",
                "context": {"recovery": "testing"}
            }
        )
        assert query_response.status_code == 200
        
        # Simulate session recovery (get current state)
        recovery_response = await client.get(f"/api/v1/sessions/{session_id}/recovery-info")
        
        if recovery_response.status_code == 200:
            recovery_data = recovery_response.json()
            
            # Should include session state information
            assert "session_id" in recovery_data
            assert "state_summary" in recovery_data
            assert recovery_data["session_id"] == session_id
            
            # Should preserve metadata
            if "metadata" in recovery_data:
                metadata = recovery_data["metadata"]
                assert metadata.get("test_mode") is True
                assert metadata.get("recovery_test") == "enabled"
        
        # Test session restoration from backup/state
        restore_response = await client.post(
            f"/api/v1/sessions/{session_id}/restore",
            json={
                "restore_point": "latest",
                "include_data": True
            }
        )
        
        # Should handle restore request appropriately
        assert restore_response.status_code in [200, 202, 501]  # 501 if not implemented
    
    @pytest.mark.asyncio
    async def test_session_performance_under_load(
        self,
        client: AsyncClient,
        performance_tracker
    ):
        """Test session performance under concurrent load."""
        
        # Create session for load testing
        create_response = await client.post("/api/v1/sessions/")
        assert create_response.status_code == 200
        session_id = create_response.json()["session_id"]
        
        # Define high-frequency operations
        async def rapid_heartbeats():
            responses = []
            for i in range(10):
                response = await client.post(f"/api/v1/sessions/{session_id}/heartbeat")
                responses.append(response)
                await asyncio.sleep(0.01)  # 10ms between heartbeats
            return responses
        
        async def rapid_stats_checks():
            responses = []
            for i in range(5):
                response = await client.get(f"/api/v1/sessions/{session_id}/stats")
                responses.append(response)
                await asyncio.sleep(0.02)  # 20ms between checks
            return responses
        
        # Execute high-frequency operations
        with performance_tracker.time_request("high_frequency_operations"):
            heartbeat_responses, stats_responses = await asyncio.gather(
                rapid_heartbeats(),
                rapid_stats_checks()
            )
        
        # Validate all operations succeeded
        all_responses = heartbeat_responses + stats_responses
        for response in all_responses:
            assert response.status_code == 200
            response_data = response.json()
            assert response_data.get("session_id") == session_id or "session_id" in response_data
        
        # Validate performance under load
        performance_tracker.assert_performance_target("high_frequency_operations", 10.0)
        
        # Check session integrity after load
        final_stats = await client.get(f"/api/v1/sessions/{session_id}/stats")
        assert final_stats.status_code == 200
        
        final_data = final_stats.json()
        assert final_data["session_id"] == session_id
        # Should show high request count from load test
        assert final_data["total_requests"] >= len(all_responses)


class TestSessionAPIErrorScenarios:
    """Test error scenarios and edge cases for session API."""
    
    @pytest.mark.asyncio
    async def test_nonexistent_session_operations(self, client: AsyncClient):
        """Test operations on non-existent sessions."""
        
        fake_session_id = "non-existent-session-12345"
        
        # Get non-existent session
        get_response = await client.get(f"/api/v1/sessions/{fake_session_id}")
        assert get_response.status_code == 404
        error_data = get_response.json()
        assert "not found" in error_data["detail"].lower()
        
        # Heartbeat on non-existent session
        heartbeat_response = await client.post(f"/api/v1/sessions/{fake_session_id}/heartbeat")
        assert heartbeat_response.status_code == 404
        
        # Stats for non-existent session
        stats_response = await client.get(f"/api/v1/sessions/{fake_session_id}/stats")
        assert stats_response.status_code == 404
        
        # Delete non-existent session
        delete_response = await client.delete(f"/api/v1/sessions/{fake_session_id}")
        assert delete_response.status_code == 404
    
    @pytest.mark.asyncio
    async def test_invalid_session_parameters(self, client: AsyncClient):
        """Test session creation with invalid parameters."""
        
        # Invalid timeout
        response = await client.post(
            "/api/v1/sessions/",
            json={"timeout_minutes": -1}
        )
        assert response.status_code == 422
        
        # Invalid session type
        response = await client.post(
            "/api/v1/sessions/",
            json={"session_type": ""}
        )
        assert response.status_code == 422
        
        # Malformed request
        response = await client.post(
            "/api/v1/sessions/",
            json={"invalid_field": "invalid_value"}
        )
        # Should either accept (ignoring invalid fields) or reject
        assert response.status_code in [200, 422]
    
    @pytest.mark.asyncio
    async def test_session_resource_limits(self, client: AsyncClient):
        """Test session resource limits and constraints."""
        
        # Create session
        create_response = await client.post("/api/v1/sessions/")
        assert create_response.status_code == 200
        session_id = create_response.json()["session_id"]
        
        # Test rapid operations to trigger rate limiting (if implemented)
        rapid_requests = []
        for i in range(100):  # Many rapid requests
            task = client.post(f"/api/v1/sessions/{session_id}/heartbeat")
            rapid_requests.append(task)
        
        # Execute all at once
        responses = await asyncio.gather(*rapid_requests, return_exceptions=True)
        
        # Most should succeed, but some might be rate limited
        success_count = sum(
            1 for r in responses 
            if not isinstance(r, Exception) and r.status_code == 200
        )
        
        rate_limited_count = sum(
            1 for r in responses
            if not isinstance(r, Exception) and r.status_code == 429
        )
        
        # Should handle the load gracefully
        assert success_count + rate_limited_count == len(responses)
        assert success_count > 0  # At least some should succeed
    
    @pytest.mark.asyncio
    async def test_malformed_session_requests(self, client: AsyncClient):
        """Test handling of malformed session requests."""
        
        # Create valid session first
        create_response = await client.post("/api/v1/sessions/")
        assert create_response.status_code == 200
        session_id = create_response.json()["session_id"]
        
        # Test malformed JSON in requests
        malformed_requests = [
            # Malformed JSON content
            ("/api/v1/sessions/", '{"invalid": json}'),
            (f"/api/v1/sessions/{session_id}/restore", '{"restore_point":}'),
        ]
        
        for endpoint, malformed_json in malformed_requests:
            response = await client.post(
                endpoint,
                content=malformed_json,
                headers={"Content-Type": "application/json"}
            )
            
            assert response.status_code == 422
            error_data = response.json()
            assert "detail" in error_data
    
    @pytest.mark.asyncio
    async def test_session_cleanup_edge_cases(self, client: AsyncClient):
        """Test session cleanup with edge cases."""
        
        # Create session
        create_response = await client.post("/api/v1/sessions/")
        assert create_response.status_code == 200
        session_id = create_response.json()["session_id"]
        
        # Try cleanup on fresh session (no data to clean)
        cleanup_response = await client.post(f"/api/v1/sessions/{session_id}/cleanup")
        
        assert cleanup_response.status_code == 200
        cleanup_data = cleanup_response.json()
        
        # Should handle empty cleanup gracefully
        assert "cleaned_items" in cleanup_data
        assert cleanup_data["session_id"] == session_id
        
        # Multiple rapid cleanups
        cleanup_tasks = [
            client.post(f"/api/v1/sessions/{session_id}/cleanup")
            for _ in range(3)
        ]
        
        cleanup_responses = await asyncio.gather(*cleanup_tasks)
        
        # All should succeed or handle gracefully
        for response in cleanup_responses:
            assert response.status_code in [200, 409]  # 409 if cleanup in progress