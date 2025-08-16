"""Test module for v3.1.0 API models validation."""

import pytest
from pydantic import ValidationError
from typing import List

from faultmaven.models.api import (
    ResponseType,
    SourceType,
    Source,
    PlanStep,
    UploadedData,
    ViewState,
    QueryRequest,
    AgentResponse,
    ErrorDetail,
    ErrorResponse
)


class TestEnums:
    """Test enum value validation."""
    
    def test_response_type_enum_values(self):
        """Test ResponseType enum has expected values."""
        assert ResponseType.ANSWER == "answer"
        assert ResponseType.PLAN_PROPOSAL == "plan_proposal"
        assert ResponseType.CLARIFICATION_REQUEST == "clarification_request"
        assert ResponseType.CONFIRMATION_REQUEST == "confirmation_request"
        
        # Test all values are present
        expected_values = {
            "answer", "plan_proposal", "clarification_request", "confirmation_request"
        }
        actual_values = {rt.value for rt in ResponseType}
        assert actual_values == expected_values
    
    def test_source_type_enum_values(self):
        """Test SourceType enum has expected values."""
        assert SourceType.KNOWLEDGE_BASE == "knowledge_base"
        assert SourceType.LOG_FILE == "log_file"
        assert SourceType.WEB_SEARCH == "web_search"
        
        # Test all values are present
        expected_values = {"knowledge_base", "log_file", "web_search"}
        actual_values = {st.value for st in SourceType}
        assert actual_values == expected_values


class TestSourceModel:
    """Test Source model validation."""
    
    def test_source_creation_valid(self):
        """Test valid Source creation."""
        source = Source(
            type=SourceType.KNOWLEDGE_BASE,
            name="database_runbook.md",
            snippet="This is a snippet from the knowledge base..."
        )
        
        assert source.type == SourceType.KNOWLEDGE_BASE
        assert source.name == "database_runbook.md"
        assert source.snippet == "This is a snippet from the knowledge base..."
    
    def test_source_type_validation(self):
        """Test Source type field validation."""
        # Test with invalid type
        with pytest.raises(ValidationError):
            Source(
                type="invalid_type",
                name="test.md",
                snippet="test snippet"
            )
    
    def test_source_required_fields(self):
        """Test Source required fields validation."""
        # Missing type
        with pytest.raises(ValidationError):
            Source(name="test.md", snippet="test snippet")
        
        # Missing name
        with pytest.raises(ValidationError):
            Source(type=SourceType.LOG_FILE, snippet="test snippet")
        
        # Missing snippet
        with pytest.raises(ValidationError):
            Source(type=SourceType.WEB_SEARCH, name="test.md")


class TestPlanStepModel:
    """Test PlanStep model validation."""
    
    def test_plan_step_creation_valid(self):
        """Test valid PlanStep creation."""
        step = PlanStep(description="Check database connection")
        
        assert step.description == "Check database connection"
    
    def test_plan_step_required_fields(self):
        """Test PlanStep required fields validation."""
        # Missing description
        with pytest.raises(ValidationError):
            PlanStep()


class TestUploadedDataModel:
    """Test UploadedData model validation."""
    
    def test_uploaded_data_creation_valid(self):
        """Test valid UploadedData creation."""
        data = UploadedData(
            id="data_123",
            name="error.log",
            type="log_file"
        )
        
        assert data.id == "data_123"
        assert data.name == "error.log"
        assert data.type == "log_file"
    
    def test_uploaded_data_required_fields(self):
        """Test UploadedData required fields validation."""
        # Missing id
        with pytest.raises(ValidationError):
            UploadedData(name="test.log", type="log")
        
        # Missing name
        with pytest.raises(ValidationError):
            UploadedData(id="123", type="log")
        
        # Missing type
        with pytest.raises(ValidationError):
            UploadedData(id="123", name="test.log")


class TestViewStateModel:
    """Test ViewState model validation."""
    
    def test_view_state_creation_valid(self):
        """Test valid ViewState creation."""
        uploaded_data = [
            UploadedData(id="1", name="log1.txt", type="log"),
            UploadedData(id="2", name="log2.txt", type="log")
        ]
        
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="Investigation in progress...",
            uploaded_data=uploaded_data
        )
        
        assert view_state.session_id == "session_123"
        assert view_state.case_id == "case_456"
        assert view_state.running_summary == "Investigation in progress..."
        assert len(view_state.uploaded_data) == 2
        assert view_state.uploaded_data[0].id == "1"
    
    def test_view_state_empty_uploaded_data(self):
        """Test ViewState with empty uploaded_data list."""
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="No data uploaded",
            uploaded_data=[]
        )
        
        assert len(view_state.uploaded_data) == 0
    
    def test_view_state_required_fields(self):
        """Test ViewState required fields validation."""
        # Missing session_id
        with pytest.raises(ValidationError):
            ViewState(
                case_id="case_456",
                running_summary="test",
                uploaded_data=[]
            )
        
        # Missing case_id
        with pytest.raises(ValidationError):
            ViewState(
                session_id="session_123",
                running_summary="test",
                uploaded_data=[]
            )
        
        # Missing running_summary
        with pytest.raises(ValidationError):
            ViewState(
                session_id="session_123",
                case_id="case_456",
                uploaded_data=[]
            )
        
        # Missing uploaded_data
        with pytest.raises(ValidationError):
            ViewState(
                session_id="session_123",
                case_id="case_456",
                running_summary="test"
            )


class TestQueryRequestModel:
    """Test QueryRequest model validation."""
    
    def test_query_request_creation_valid(self):
        """Test valid QueryRequest creation."""
        request = QueryRequest(
            session_id="session_123",
            query="What is causing the database errors?"
        )
        
        assert request.session_id == "session_123"
        assert request.query == "What is causing the database errors?"
    
    def test_query_request_required_fields(self):
        """Test QueryRequest required fields validation."""
        # Missing session_id
        with pytest.raises(ValidationError):
            QueryRequest(query="test query")
        
        # Missing query
        with pytest.raises(ValidationError):
            QueryRequest(session_id="session_123")


class TestAgentResponseModel:
    """Test AgentResponse model validation and business rules."""
    
    def test_agent_response_creation_valid_answer(self):
        """Test valid AgentResponse creation for ANSWER type."""
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="Analysis complete",
            uploaded_data=[]
        )
        
        sources = [
            Source(
                type=SourceType.KNOWLEDGE_BASE,
                name="db_guide.md",
                snippet="Database troubleshooting steps..."
            )
        ]
        
        response = AgentResponse(
            content="The database error is caused by connection timeout.",
            response_type=ResponseType.ANSWER,
            view_state=view_state,
            sources=sources,
            plan=None
        )
        
        assert response.schema_version == "3.1.0"
        assert response.content == "The database error is caused by connection timeout."
        assert response.response_type == ResponseType.ANSWER
        assert response.view_state.case_id == "case_456"
        assert len(response.sources) == 1
        assert response.plan is None
    
    def test_agent_response_creation_valid_plan_proposal(self):
        """Test valid AgentResponse creation for PLAN_PROPOSAL type."""
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="Plan created",
            uploaded_data=[]
        )
        
        plan = [
            PlanStep(description="Step 1: Check database connection"),
            PlanStep(description="Step 2: Restart database service"),
            PlanStep(description="Step 3: Verify connectivity")
        ]
        
        response = AgentResponse(
            content="Here's a plan to fix the database issue:",
            response_type=ResponseType.PLAN_PROPOSAL,
            view_state=view_state,
            sources=[],
            plan=plan
        )
        
        assert response.response_type == ResponseType.PLAN_PROPOSAL
        assert len(response.plan) == 3
        assert response.plan[0].description == "Step 1: Check database connection"
    
    def test_agent_response_default_values(self):
        """Test AgentResponse default values."""
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="Default test",
            uploaded_data=[]
        )
        
        response = AgentResponse(
            content="Test content",
            response_type=ResponseType.ANSWER,
            view_state=view_state
        )
        
        # Test defaults
        assert response.schema_version == "3.1.0"
        assert response.sources == []
        assert response.plan is None
    
    @pytest.mark.parametrize("response_type,plan,should_pass", [
        (ResponseType.ANSWER, None, True),
        (ResponseType.CLARIFICATION_REQUEST, None, True),
        (ResponseType.CONFIRMATION_REQUEST, None, True),
        (ResponseType.PLAN_PROPOSAL, [PlanStep(description="test")], True),
        (ResponseType.PLAN_PROPOSAL, None, False),  # Should fail
        (ResponseType.ANSWER, [PlanStep(description="test")], False),  # Should fail
        (ResponseType.CLARIFICATION_REQUEST, [PlanStep(description="test")], False),  # Should fail
        (ResponseType.CONFIRMATION_REQUEST, [PlanStep(description="test")], False)  # Should fail
    ])
    def test_agent_response_plan_consistency_validator(self, response_type, plan, should_pass):
        """Test AgentResponse plan consistency validation."""
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="Validation test",
            uploaded_data=[]
        )
        
        if should_pass:
            response = AgentResponse(
                content="Test content",
                response_type=response_type,
                view_state=view_state,
                plan=plan
            )
            # If we get here, validation passed
            assert response.response_type == response_type
            assert response.plan == plan
        else:
            with pytest.raises(ValidationError) as exc_info:
                AgentResponse(
                    content="Test content",
                    response_type=response_type,
                    view_state=view_state,
                    plan=plan
                )
            
            # Check the error message contains plan consistency information
            error_msg = str(exc_info.value)
            assert "plan" in error_msg.lower()
    
    def test_agent_response_schema_version_enforcement(self):
        """Test AgentResponse schema version is always 3.1.0."""
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="Version test",
            uploaded_data=[]
        )
        
        # Try to create with different schema version - should be overridden
        response = AgentResponse(
            schema_version="2.0.0",  # This should be ignored
            content="Test content",
            response_type=ResponseType.ANSWER,
            view_state=view_state
        )
        
        # Should always be 3.1.0
        assert response.schema_version == "3.1.0"
    
    def test_agent_response_required_fields(self):
        """Test AgentResponse required fields validation."""
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="Required fields test",
            uploaded_data=[]
        )
        
        # Missing content
        with pytest.raises(ValidationError):
            AgentResponse(
                response_type=ResponseType.ANSWER,
                view_state=view_state
            )
        
        # Missing response_type
        with pytest.raises(ValidationError):
            AgentResponse(
                content="Test content",
                view_state=view_state
            )
        
        # Missing view_state
        with pytest.raises(ValidationError):
            AgentResponse(
                content="Test content",
                response_type=ResponseType.ANSWER
            )


class TestErrorModels:
    """Test error response models."""
    
    def test_error_detail_creation_valid(self):
        """Test valid ErrorDetail creation."""
        error_detail = ErrorDetail(
            code="SESSION_NOT_FOUND",
            message="The specified session could not be found"
        )
        
        assert error_detail.code == "SESSION_NOT_FOUND"
        assert error_detail.message == "The specified session could not be found"
    
    def test_error_detail_required_fields(self):
        """Test ErrorDetail required fields validation."""
        # Missing code
        with pytest.raises(ValidationError):
            ErrorDetail(message="test message")
        
        # Missing message
        with pytest.raises(ValidationError):
            ErrorDetail(code="TEST_ERROR")
    
    def test_error_response_creation_valid(self):
        """Test valid ErrorResponse creation."""
        error_detail = ErrorDetail(
            code="VALIDATION_ERROR",
            message="Input validation failed"
        )
        
        error_response = ErrorResponse(error=error_detail)
        
        assert error_response.schema_version == "3.1.0"
        assert error_response.error.code == "VALIDATION_ERROR"
        assert error_response.error.message == "Input validation failed"
    
    def test_error_response_schema_version_enforcement(self):
        """Test ErrorResponse schema version is always 3.1.0."""
        error_detail = ErrorDetail(
            code="TEST_ERROR",
            message="Test error message"
        )
        
        # Try to create with different schema version - should be overridden
        error_response = ErrorResponse(
            schema_version="1.0.0",  # This should be ignored
            error=error_detail
        )
        
        # Should always be 3.1.0
        assert error_response.schema_version == "3.1.0"
    
    def test_error_response_required_fields(self):
        """Test ErrorResponse required fields validation."""
        # Missing error
        with pytest.raises(ValidationError):
            ErrorResponse()


class TestModelSerialization:
    """Test model serialization and deserialization."""
    
    def test_agent_response_serialization(self):
        """Test AgentResponse JSON serialization."""
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="Serialization test",
            uploaded_data=[
                UploadedData(id="1", name="test.log", type="log")
            ]
        )
        
        sources = [
            Source(
                type=SourceType.KNOWLEDGE_BASE,
                name="guide.md",
                snippet="Test snippet"
            )
        ]
        
        plan = [
            PlanStep(description="Test step 1"),
            PlanStep(description="Test step 2")
        ]
        
        response = AgentResponse(
            content="Test response content",
            response_type=ResponseType.PLAN_PROPOSAL,
            view_state=view_state,
            sources=sources,
            plan=plan
        )
        
        # Test serialization
        json_data = response.dict()
        
        assert json_data["schema_version"] == "3.1.0"
        assert json_data["content"] == "Test response content"
        assert json_data["response_type"] == "plan_proposal"
        assert json_data["view_state"]["session_id"] == "session_123"
        assert len(json_data["sources"]) == 1
        assert len(json_data["plan"]) == 2
        
        # Test deserialization
        reconstructed = AgentResponse(**json_data)
        assert reconstructed.content == response.content
        assert reconstructed.response_type == response.response_type
        assert reconstructed.view_state.case_id == response.view_state.case_id
        assert len(reconstructed.sources) == len(response.sources)
        assert len(reconstructed.plan) == len(response.plan)
    
    def test_query_request_serialization(self):
        """Test QueryRequest JSON serialization."""
        request = QueryRequest(
            session_id="session_123",
            query="Test query"
        )
        
        # Test serialization
        json_data = request.dict()
        
        assert json_data["session_id"] == "session_123"
        assert json_data["query"] == "Test query"
        
        # Test deserialization
        reconstructed = QueryRequest(**json_data)
        assert reconstructed.session_id == request.session_id
        assert reconstructed.query == request.query
    
    def test_error_response_serialization(self):
        """Test ErrorResponse JSON serialization."""
        error_response = ErrorResponse(
            error=ErrorDetail(
                code="TEST_ERROR",
                message="Test error message"
            )
        )
        
        # Test serialization
        json_data = error_response.dict()
        
        assert json_data["schema_version"] == "3.1.0"
        assert json_data["error"]["code"] == "TEST_ERROR"
        assert json_data["error"]["message"] == "Test error message"
        
        # Test deserialization
        reconstructed = ErrorResponse(**json_data)
        assert reconstructed.schema_version == error_response.schema_version
        assert reconstructed.error.code == error_response.error.code
        assert reconstructed.error.message == error_response.error.message


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    def test_empty_strings_validation(self):
        """Test validation with empty strings."""
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="",  # Empty string should be valid
            uploaded_data=[]
        )
        
        # Empty content should be valid
        response = AgentResponse(
            content="",
            response_type=ResponseType.ANSWER,
            view_state=view_state
        )
        
        assert response.content == ""
        assert response.view_state.running_summary == ""
    
    def test_large_data_validation(self):
        """Test validation with large data sets."""
        # Large number of sources
        sources = [
            Source(
                type=SourceType.KNOWLEDGE_BASE,
                name=f"document_{i}.md",
                snippet=f"This is snippet number {i}" * 10
            )
            for i in range(100)
        ]
        
        # Large plan
        plan = [
            PlanStep(description=f"Step {i}: Execute action {i}")
            for i in range(50)
        ]
        
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="Large data test",
            uploaded_data=[]
        )
        
        response = AgentResponse(
            content="Large response test",
            response_type=ResponseType.PLAN_PROPOSAL,
            view_state=view_state,
            sources=sources,
            plan=plan
        )
        
        assert len(response.sources) == 100
        assert len(response.plan) == 50
    
    def test_unicode_and_special_characters(self):
        """Test validation with unicode and special characters."""
        view_state = ViewState(
            session_id="session_123",
            case_id="case_456",
            running_summary="Test with émojis 🚀 and special chars: @#$%^&*()",
            uploaded_data=[]
        )
        
        response = AgentResponse(
            content="Response with unicode: 测试中文, émojis: 🔧🐛, and symbols: ←→↑↓",
            response_type=ResponseType.ANSWER,
            view_state=view_state
        )
        
        assert "🚀" in response.view_state.running_summary
        assert "测试中文" in response.content
        assert "🔧🐛" in response.content