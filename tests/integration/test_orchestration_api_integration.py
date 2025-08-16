"""Orchestration API Integration Tests

This module contains comprehensive integration tests for the orchestration API
endpoints, validating that all new Phase 2 API endpoints work correctly together
and integrate properly with the underlying service architecture.

Key Test Areas:
- Orchestration API endpoint functionality
- Request/response validation
- Error handling and edge cases
- API workflow integration
- Cross-endpoint data consistency
- Performance under API load
"""

import json
import time
from datetime import datetime
from typing import Dict, List, Any
from unittest.mock import Mock, AsyncMock, patch
import pytest
from fastapi.testclient import TestClient
from fastapi import status

from faultmaven.main import app
from faultmaven.api.v1.routes.orchestration import (
    WorkflowCreateRequest, StepExecuteRequest, WorkflowPauseRequest
)
from faultmaven.services.orchestration_service import OrchestrationService
from faultmaven.exceptions import ServiceException, ValidationException


@pytest.fixture
def api_client():
    """Test client for API endpoints"""
    return TestClient(app)


@pytest.fixture
def mock_orchestration_service():
    """Mock orchestration service for API testing"""
    service = Mock()
    
    # Mock successful workflow creation
    service.create_troubleshooting_workflow = AsyncMock(return_value={
        "success": True,
        "workflow_id": "test-workflow-123",
        "workflow_details": {
            "pattern": "standard_troubleshooting",
            "total_steps": 5,
            "estimated_duration": 1500,
            "initialization_time": 150.5
        },
        "current_step": {
            "step_id": "step_01_blast_radius_assessment",
            "title": "Assess Impact Scope",
            "description": "Determine affected systems"
        },
        "strategic_insights": ["High priority issue detected", "Database patterns identified"],
        "memory_enhancements": 3,
        "next_action": "Execute first step using execute_workflow_step"
    })
    
    # Mock successful step execution
    service.execute_workflow_step = AsyncMock(return_value={
        "success": True,
        "workflow_id": "test-workflow-123", 
        "step_execution": {
            "step_id": "step_01_blast_radius_assessment",
            "status": "completed",
            "findings": [
                {"type": "impact_assessment", "content": "API service affected"}
            ],
            "insights": ["Database connection pool exhausted"],
            "recommendations": ["Increase connection pool size"],
            "confidence_score": 0.85,
            "knowledge_gaps": []
        },
        "workflow_progress": {
            "current_step": 2,
            "total_steps": 5,
            "status": "in_progress",
            "progress_percentage": 20.0
        },
        "next_step": {
            "step_id": "step_02_timeline_analysis",
            "title": "Construct Event Timeline"
        },
        "adaptive_changes": [],
        "execution_time": 1200.5,
        "recommendations": {
            "immediate_actions": ["Check connection pool"],
            "next_steps": ["Analyze timeline"],
            "knowledge_gaps": []
        }
    })
    
    # Mock workflow status
    service.get_workflow_status = AsyncMock(return_value={
        "success": True,
        "workflow_id": "test-workflow-123",
        "status": "in_progress",
        "progress": {
            "current_step": 2,
            "total_steps": 5,
            "progress_percentage": 40.0,
            "steps_completed": 1
        },
        "findings_summary": {
            "total_findings": 5,
            "knowledge_items": 8,
            "execution_log_entries": 2
        },
        "performance": {
            "initialization_time": 150.5,
            "steps_completed": 1,
            "total_knowledge_retrieved": 8,
            "adaptive_changes": 0
        },
        "timeline": {
            "created_at": "2024-01-01T12:00:00",
            "estimated_completion": "2024-01-01T12:25:00",
            "current_timestamp": "2024-01-01T12:05:00"
        },
        "service_metadata": {
            "service": "orchestration_service",
            "version": "1.0.0",
            "capabilities": {
                "workflow_management": True,
                "memory_integration": True
            }
        }
    })
    
    # Mock pause workflow
    service.pause_workflow = AsyncMock(return_value={
        "success": True,
        "workflow_id": "test-workflow-123",
        "status": "paused",
        "pause_details": {
            "paused_at": "2024-01-01T12:05:00",
            "current_step": 2
        },
        "pause_reason": "Testing pause functionality",
        "resume_instructions": "Call resume_workflow() to continue execution",
        "service_message": "Workflow paused successfully"
    })
    
    # Mock resume workflow
    service.resume_workflow = AsyncMock(return_value={
        "success": True,
        "workflow_id": "test-workflow-123",
        "status": "resumed",
        "resume_details": {
            "resumed_at": "2024-01-01T12:10:00",
            "current_step": 2
        },
        "next_step": {
            "step_id": "step_02_timeline_analysis",
            "title": "Construct Event Timeline"
        },
        "progress": {
            "current_step": 2,
            "total_steps": 5
        },
        "next_action": "Execute next step using execute_workflow_step"
    })
    
    # Mock workflow recommendations
    service.get_workflow_recommendations = AsyncMock(return_value={
        "success": True,
        "workflow_id": "test-workflow-123",
        "recommendations": {
            "performance": ["Consider parallel knowledge retrieval"],
            "methodology": ["Workflow progressing well"],
            "efficiency": ["More focused questioning recommended"],
            "quality": ["High-quality findings so far"]
        },
        "optimization_score": 75.5,
        "next_optimizations": [
            "Implement caching for knowledge queries",
            "Add step parallelization"
        ]
    })
    
    # Mock list workflows
    service.list_active_workflows = AsyncMock(return_value={
        "success": True,
        "workflows": [
            {
                "workflow_id": "test-workflow-123",
                "case_id": "case-456",
                "status": "in_progress",
                "progress": 40.0,
                "created_at": "2024-01-01T12:00:00"
            }
        ],
        "summary": {
            "total_active_workflows": 1,
            "workflows_by_status": {
                "in_progress": 1,
                "paused": 0,
                "waiting_input": 0
            },
            "user_filter": None
        },
        "service_metrics": {
            "workflows_created": 10,
            "workflows_completed": 8,
            "avg_workflow_duration": 1200.0
        }
    })
    
    # Mock health check
    service.health_check = AsyncMock(return_value={
        "status": "healthy",
        "service": "orchestration_service",
        "dependencies": {
            "memory_service": "healthy",
            "planning_service": "healthy",
            "reasoning_service": "healthy",
            "knowledge_service": "healthy"
        },
        "capabilities": {
            "workflow_management": True,
            "memory_integration": True,
            "planning_integration": True
        },
        "uptime_seconds": 3600.0
    })
    
    return service


@pytest.fixture
def sample_workflow_request():
    """Sample workflow creation request"""
    return {
        "session_id": "test-session-123",
        "case_id": "test-case-456", 
        "user_id": "test-user-789",
        "problem_description": "Database connections are timing out frequently in production",
        "context": {
            "service_name": "user-api",
            "environment": "production",
            "component": "database"
        },
        "priority_level": "high",
        "domain_expertise": "intermediate",
        "time_constraints": 3600
    }


class TestOrchestrationAPIEndpoints:
    """Test orchestration API endpoints functionality"""
    
    def test_create_workflow_endpoint(self, api_client, mock_orchestration_service, sample_workflow_request):
        """Test POST /orchestration/workflows endpoint"""
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.post(
                "/orchestration/workflows",
                json=sample_workflow_request
            )
        
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        
        # Validate response structure
        assert data["success"] is True
        assert "workflow_id" in data
        assert "workflow_details" in data
        assert "current_step" in data
        assert "strategic_insights" in data
        assert "memory_enhancements" in data
        assert "next_action" in data
        
        # Validate workflow details
        workflow_details = data["workflow_details"]
        assert "pattern" in workflow_details
        assert "total_steps" in workflow_details
        assert "estimated_duration" in workflow_details
        assert workflow_details["total_steps"] > 0
        
        # Verify service was called correctly
        mock_orchestration_service.create_troubleshooting_workflow.assert_called_once()
    
    def test_execute_step_endpoint(self, api_client, mock_orchestration_service):
        """Test POST /orchestration/workflows/{workflow_id}/steps endpoint"""
        workflow_id = "test-workflow-123"
        step_request = {
            "step_inputs": {"user_input": "Issue started after deployment"},
            "user_feedback": {"helpful": True, "clarity": "good"}
        }
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.post(
                f"/orchestration/workflows/{workflow_id}/steps",
                json=step_request
            )
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        
        # Validate response structure
        assert data["success"] is True
        assert data["workflow_id"] == workflow_id
        assert "step_execution" in data
        assert "workflow_progress" in data
        assert "next_step" in data
        assert "execution_time" in data
        assert "recommendations" in data
        
        # Validate step execution details
        step_execution = data["step_execution"]
        assert "findings" in step_execution
        assert "insights" in step_execution
        assert "confidence_score" in step_execution
        assert step_execution["confidence_score"] > 0
        
        # Verify service was called correctly
        mock_orchestration_service.execute_workflow_step.assert_called_once_with(
            workflow_id=workflow_id,
            step_inputs=step_request["step_inputs"],
            user_feedback=step_request["user_feedback"]
        )
    
    def test_get_workflow_status_endpoint(self, api_client, mock_orchestration_service):
        """Test GET /orchestration/workflows/{workflow_id}/status endpoint"""
        workflow_id = "test-workflow-123"
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.get(f"/orchestration/workflows/{workflow_id}/status")
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        
        # Validate response structure
        assert data["success"] is True
        assert data["workflow_id"] == workflow_id
        assert "status" in data
        assert "progress" in data
        assert "findings_summary" in data
        assert "performance" in data
        assert "timeline" in data
        assert "service_metadata" in data
        
        # Validate progress information
        progress = data["progress"]
        assert "current_step" in progress
        assert "total_steps" in progress
        assert "progress_percentage" in progress
        assert 0 <= progress["progress_percentage"] <= 100
        
        # Verify service was called correctly
        mock_orchestration_service.get_workflow_status.assert_called_once_with(workflow_id)
    
    def test_pause_workflow_endpoint(self, api_client, mock_orchestration_service):
        """Test POST /orchestration/workflows/{workflow_id}/pause endpoint"""
        workflow_id = "test-workflow-123"
        pause_request = {"reason": "Taking a break for team meeting"}
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.post(
                f"/orchestration/workflows/{workflow_id}/pause",
                json=pause_request
            )
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        
        # Validate response structure
        assert data["success"] is True
        assert data["workflow_id"] == workflow_id
        assert data["status"] == "paused"
        assert "pause_details" in data
        assert "resume_instructions" in data
        
        # Verify service was called correctly
        mock_orchestration_service.pause_workflow.assert_called_once_with(
            workflow_id=workflow_id,
            reason=pause_request["reason"]
        )
    
    def test_resume_workflow_endpoint(self, api_client, mock_orchestration_service):
        """Test POST /orchestration/workflows/{workflow_id}/resume endpoint"""
        workflow_id = "test-workflow-123"
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.post(f"/orchestration/workflows/{workflow_id}/resume")
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        
        # Validate response structure
        assert data["success"] is True
        assert data["workflow_id"] == workflow_id
        assert data["status"] == "resumed"
        assert "resume_details" in data
        assert "next_step" in data
        assert "next_action" in data
        
        # Verify service was called correctly
        mock_orchestration_service.resume_workflow.assert_called_once_with(workflow_id)
    
    def test_get_workflow_recommendations_endpoint(self, api_client, mock_orchestration_service):
        """Test GET /orchestration/workflows/{workflow_id}/recommendations endpoint"""
        workflow_id = "test-workflow-123"
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.get(f"/orchestration/workflows/{workflow_id}/recommendations")
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        
        # Validate response structure
        assert data["success"] is True
        assert data["workflow_id"] == workflow_id
        assert "recommendations" in data
        assert "optimization_score" in data
        assert "next_optimizations" in data
        
        # Validate recommendations structure
        recommendations = data["recommendations"]
        assert "performance" in recommendations
        assert "methodology" in recommendations
        assert "efficiency" in recommendations
        assert "quality" in recommendations
        
        # Verify service was called correctly
        mock_orchestration_service.get_workflow_recommendations.assert_called_once_with(workflow_id)
    
    def test_list_workflows_endpoint(self, api_client, mock_orchestration_service):
        """Test GET /orchestration/workflows endpoint"""
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.get("/orchestration/workflows")
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        
        # Validate response structure
        assert data["success"] is True
        assert "workflows" in data
        assert "summary" in data
        assert "service_metrics" in data
        
        # Validate summary information
        summary = data["summary"]
        assert "total_active_workflows" in summary
        assert "workflows_by_status" in summary
        
        # Verify service was called correctly
        mock_orchestration_service.list_active_workflows.assert_called_once_with(None)
    
    def test_list_workflows_with_user_filter(self, api_client, mock_orchestration_service):
        """Test GET /orchestration/workflows with user_id filter"""
        user_id = "test-user-123"
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.get(f"/orchestration/workflows?user_id={user_id}")
        
        assert response.status_code == status.HTTP_200_OK
        
        # Verify service was called with user filter
        mock_orchestration_service.list_active_workflows.assert_called_once_with(user_id)
    
    def test_health_check_endpoint(self, api_client, mock_orchestration_service):
        """Test GET /orchestration/health endpoint"""
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.get("/orchestration/health")
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        
        # Validate response structure
        assert "status" in data
        assert "service" in data
        assert "dependencies" in data
        assert "capabilities" in data
        
        # Check dependencies status
        dependencies = data["dependencies"]
        for service_name in ["memory_service", "planning_service", "reasoning_service", "knowledge_service"]:
            assert service_name in dependencies
        
        # Verify service was called correctly
        mock_orchestration_service.health_check.assert_called_once()


class TestAPIRequestValidation:
    """Test API request validation and error handling"""
    
    def test_create_workflow_validation_errors(self, api_client, mock_orchestration_service):
        """Test workflow creation with invalid requests"""
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            # Test missing required fields
            response = api_client.post("/orchestration/workflows", json={})
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
            
            # Test invalid priority level
            invalid_request = {
                "session_id": "test-session",
                "case_id": "test-case", 
                "user_id": "test-user",
                "problem_description": "Test problem",
                "priority_level": "invalid_priority"
            }
            response = api_client.post("/orchestration/workflows", json=invalid_request)
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
            
            # Test problem description too short
            short_desc_request = {
                "session_id": "test-session",
                "case_id": "test-case",
                "user_id": "test-user", 
                "problem_description": "Short"
            }
            response = api_client.post("/orchestration/workflows", json=short_desc_request)
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    
    def test_step_execution_with_invalid_workflow_id(self, api_client, mock_orchestration_service):
        """Test step execution with invalid workflow ID"""
        # Mock service to raise ServiceException for invalid workflow
        mock_orchestration_service.execute_workflow_step.side_effect = ServiceException("Workflow not found")
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.post(
                "/orchestration/workflows/invalid-id/steps",
                json={"step_inputs": {}}
            )
        
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        error_data = response.json()
        assert "Service error" in error_data["detail"]
    
    def test_workflow_not_found_errors(self, api_client, mock_orchestration_service):
        """Test various endpoints with non-existent workflow"""
        # Mock service to raise ServiceException for workflow not found
        mock_orchestration_service.get_workflow_status.side_effect = ServiceException("Workflow not found")
        mock_orchestration_service.pause_workflow.side_effect = ServiceException("Workflow not found") 
        mock_orchestration_service.resume_workflow.side_effect = ServiceException("Workflow not found")
        mock_orchestration_service.get_workflow_recommendations.side_effect = ServiceException("Workflow not found")
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            # Test status endpoint
            response = api_client.get("/orchestration/workflows/non-existent-id/status")
            assert response.status_code == status.HTTP_404_NOT_FOUND
            
            # Test pause endpoint
            response = api_client.post("/orchestration/workflows/non-existent-id/pause", json={})
            assert response.status_code == status.HTTP_404_NOT_FOUND
            
            # Test resume endpoint
            response = api_client.post("/orchestration/workflows/non-existent-id/resume")
            assert response.status_code == status.HTTP_404_NOT_FOUND
            
            # Test recommendations endpoint
            response = api_client.get("/orchestration/workflows/non-existent-id/recommendations")
            assert response.status_code == status.HTTP_404_NOT_FOUND


class TestAPIWorkflowIntegration:
    """Test complete API workflow integration scenarios"""
    
    def test_complete_api_workflow_sequence(self, api_client, mock_orchestration_service, sample_workflow_request):
        """Test complete workflow through API endpoints"""
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            # 1. Create workflow
            create_response = api_client.post("/orchestration/workflows", json=sample_workflow_request)
            assert create_response.status_code == status.HTTP_201_CREATED
            
            workflow_data = create_response.json()
            workflow_id = workflow_data["workflow_id"]
            
            # 2. Check initial status
            status_response = api_client.get(f"/orchestration/workflows/{workflow_id}/status")
            assert status_response.status_code == status.HTTP_200_OK
            
            # 3. Execute first step
            step_response = api_client.post(
                f"/orchestration/workflows/{workflow_id}/steps",
                json={"step_inputs": {"observation": "Database errors in logs"}}
            )
            assert step_response.status_code == status.HTTP_200_OK
            
            # 4. Get recommendations
            rec_response = api_client.get(f"/orchestration/workflows/{workflow_id}/recommendations")
            assert rec_response.status_code == status.HTTP_200_OK
            
            # 5. Pause workflow
            pause_response = api_client.post(
                f"/orchestration/workflows/{workflow_id}/pause",
                json={"reason": "Checking with team"}
            )
            assert pause_response.status_code == status.HTTP_200_OK
            
            # 6. Resume workflow
            resume_response = api_client.post(f"/orchestration/workflows/{workflow_id}/resume")
            assert resume_response.status_code == status.HTTP_200_OK
            
            # 7. List all workflows
            list_response = api_client.get("/orchestration/workflows")
            assert list_response.status_code == status.HTTP_200_OK
            
            # 8. Check service health
            health_response = api_client.get("/orchestration/health")
            assert health_response.status_code == status.HTTP_200_OK
    
    def test_concurrent_api_requests(self, api_client, mock_orchestration_service, sample_workflow_request):
        """Test API performance with concurrent requests"""
        import concurrent.futures
        import threading
        
        def make_create_request():
            with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
                return api_client.post("/orchestration/workflows", json=sample_workflow_request)
        
        # Make 5 concurrent workflow creation requests
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(make_create_request) for _ in range(5)]
            responses = [future.result() for future in concurrent.futures.as_completed(futures)]
        
        # All requests should succeed
        for response in responses:
            assert response.status_code == status.HTTP_201_CREATED
            data = response.json()
            assert data["success"] is True
    
    def test_api_error_handling_consistency(self, api_client, mock_orchestration_service):
        """Test consistent error handling across API endpoints"""
        # Mock service to raise various exceptions
        mock_orchestration_service.create_troubleshooting_workflow.side_effect = ValidationException("Invalid input")
        mock_orchestration_service.execute_workflow_step.side_effect = ServiceException("Service error")
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            # Test validation error
            response = api_client.post("/orchestration/workflows", json={
                "session_id": "test",
                "case_id": "test", 
                "user_id": "test",
                "problem_description": "Test problem"
            })
            assert response.status_code == status.HTTP_400_BAD_REQUEST
            assert "Validation error" in response.json()["detail"]
            
            # Test service error
            response = api_client.post("/orchestration/workflows/test-id/steps", json={})
            assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
            assert "Service error" in response.json()["detail"]


class TestAPIResponseValidation:
    """Test API response format validation"""
    
    def test_workflow_creation_response_format(self, api_client, mock_orchestration_service, sample_workflow_request):
        """Test workflow creation response matches OpenAPI schema"""
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.post("/orchestration/workflows", json=sample_workflow_request)
        
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        
        # Validate required fields according to WorkflowCreateResponse model
        required_fields = [
            "success", "workflow_id", "workflow_details", "current_step",
            "strategic_insights", "memory_enhancements", "next_action"
        ]
        
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"
        
        # Validate data types
        assert isinstance(data["success"], bool)
        assert isinstance(data["workflow_id"], str)
        assert isinstance(data["workflow_details"], dict)
        assert isinstance(data["strategic_insights"], list)
        assert isinstance(data["memory_enhancements"], int)
        assert isinstance(data["next_action"], str)
    
    def test_step_execution_response_format(self, api_client, mock_orchestration_service):
        """Test step execution response matches OpenAPI schema"""
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.post("/orchestration/workflows/test-id/steps", json={})
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        
        # Validate required fields according to StepExecuteResponse model
        required_fields = [
            "success", "workflow_id", "step_execution", "workflow_progress",
            "next_step", "adaptive_changes", "execution_time", "recommendations"
        ]
        
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"
        
        # Validate data types
        assert isinstance(data["success"], bool)
        assert isinstance(data["execution_time"], (int, float))
        assert isinstance(data["adaptive_changes"], list)
        assert isinstance(data["recommendations"], dict)
    
    def test_status_response_format(self, api_client, mock_orchestration_service):
        """Test workflow status response matches OpenAPI schema"""
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            response = api_client.get("/orchestration/workflows/test-id/status")
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        
        # Validate required fields according to WorkflowStatusResponse model
        required_fields = [
            "success", "workflow_id", "status", "progress", "findings_summary",
            "performance", "timeline", "service_metadata"
        ]
        
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"
        
        # Validate nested structures
        assert "current_step" in data["progress"]
        assert "total_steps" in data["progress"]
        assert "progress_percentage" in data["progress"]
        assert isinstance(data["progress"]["progress_percentage"], (int, float))


class TestAPIPerformanceAndLoad:
    """Test API performance under load"""
    
    def test_api_response_times(self, api_client, mock_orchestration_service, sample_workflow_request):
        """Test API response times meet performance requirements"""
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            # Test workflow creation response time
            start_time = time.time()
            response = api_client.post("/orchestration/workflows", json=sample_workflow_request)
            creation_time = time.time() - start_time
            
            assert response.status_code == status.HTTP_201_CREATED
            assert creation_time < 1.0  # Should complete within 1 second
            
            # Test status retrieval response time
            start_time = time.time()
            response = api_client.get("/orchestration/workflows/test-id/status")
            status_time = time.time() - start_time
            
            assert response.status_code == status.HTTP_200_OK
            assert status_time < 0.2  # Should complete within 200ms
            
            # Test health check response time
            start_time = time.time()
            response = api_client.get("/orchestration/health")
            health_time = time.time() - start_time
            
            assert response.status_code == status.HTTP_200_OK
            assert health_time < 0.1  # Should complete within 100ms
    
    def test_api_load_handling(self, api_client, mock_orchestration_service):
        """Test API can handle multiple simultaneous requests"""
        import concurrent.futures
        
        def make_health_request():
            with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
                return api_client.get("/orchestration/health")
        
        # Make 20 concurrent health check requests
        start_time = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_health_request) for _ in range(20)]
            responses = [future.result() for future in concurrent.futures.as_completed(futures)]
        total_time = time.time() - start_time
        
        # All requests should succeed
        for response in responses:
            assert response.status_code == status.HTTP_200_OK
        
        # Should complete within reasonable time
        assert total_time < 5.0  # 20 requests in under 5 seconds
        
        # Calculate average response time
        avg_response_time = total_time / 20
        assert avg_response_time < 0.25  # Average under 250ms per request
    
    def test_api_memory_usage(self, api_client, mock_orchestration_service, sample_workflow_request):
        """Test API memory usage doesn't grow excessively"""
        import psutil
        import os
        
        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            # Make many requests
            for i in range(50):
                response = api_client.post("/orchestration/workflows", json=sample_workflow_request)
                assert response.status_code == status.HTTP_201_CREATED
        
        final_memory = process.memory_info().rss
        memory_growth = final_memory - initial_memory
        
        # Memory growth should be reasonable (less than 50MB for 50 requests)
        assert memory_growth < 50 * 1024 * 1024  # 50MB


@pytest.mark.integration
@pytest.mark.api
class TestCrossEndpointDataConsistency:
    """Test data consistency across API endpoints"""
    
    def test_workflow_data_consistency(self, api_client, mock_orchestration_service, sample_workflow_request):
        """Test workflow data remains consistent across endpoints"""
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            # Create workflow
            create_response = api_client.post("/orchestration/workflows", json=sample_workflow_request)
            create_data = create_response.json()
            workflow_id = create_data["workflow_id"]
            
            # Get status and verify consistency
            status_response = api_client.get(f"/orchestration/workflows/{workflow_id}/status")
            status_data = status_response.json()
            
            assert status_data["workflow_id"] == workflow_id
            assert status_data["success"] is True
            
            # Execute step and verify workflow ID consistency
            step_response = api_client.post(f"/orchestration/workflows/{workflow_id}/steps", json={})
            step_data = step_response.json()
            
            assert step_data["workflow_id"] == workflow_id
            assert step_data["success"] is True
            
            # Get recommendations and verify consistency
            rec_response = api_client.get(f"/orchestration/workflows/{workflow_id}/recommendations")
            rec_data = rec_response.json()
            
            assert rec_data["workflow_id"] == workflow_id
            assert rec_data["success"] is True
    
    def test_workflow_state_transitions(self, api_client, mock_orchestration_service, sample_workflow_request):
        """Test workflow state transitions are consistent across endpoints"""
        # Mock service responses for state transitions
        mock_orchestration_service.get_workflow_status = AsyncMock(side_effect=[
            # Initial status after creation
            {
                "success": True,
                "workflow_id": "test-workflow-123",
                "status": "initialized",
                "progress": {"current_step": 1, "total_steps": 5, "progress_percentage": 0}
            },
            # Status after first step
            {
                "success": True,
                "workflow_id": "test-workflow-123", 
                "status": "in_progress",
                "progress": {"current_step": 2, "total_steps": 5, "progress_percentage": 20}
            },
            # Status after pause
            {
                "success": True,
                "workflow_id": "test-workflow-123",
                "status": "paused", 
                "progress": {"current_step": 2, "total_steps": 5, "progress_percentage": 20}
            }
        ])
        
        with patch("faultmaven.api.v1.dependencies.get_orchestration_service", return_value=mock_orchestration_service):
            # Create workflow
            create_response = api_client.post("/orchestration/workflows", json=sample_workflow_request)
            workflow_id = create_response.json()["workflow_id"]
            
            # Check initial status
            status_response = api_client.get(f"/orchestration/workflows/{workflow_id}/status")
            assert status_response.json()["status"] == "initialized"
            
            # Execute step
            api_client.post(f"/orchestration/workflows/{workflow_id}/steps", json={})
            
            # Check status after step
            status_response = api_client.get(f"/orchestration/workflows/{workflow_id}/status")
            assert status_response.json()["status"] == "in_progress"
            
            # Pause workflow
            api_client.post(f"/orchestration/workflows/{workflow_id}/pause", json={})
            
            # Check status after pause
            status_response = api_client.get(f"/orchestration/workflows/{workflow_id}/status")
            assert status_response.json()["status"] == "paused"