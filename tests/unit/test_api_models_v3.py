"""Test module for v3.1.0 API models validation."""

import pytest
from pydantic import ValidationError
from typing import List

from faultmaven.models.api import (
    ResponseType,
    SourceType,
    DataType,
    ProcessingStatus,
    Source,
    PlanStep,
    UploadedData,
    ViewState,
    QueryRequest,
    AgentResponse,
    ErrorDetail,
    ErrorResponse,
    StandardErrorResponse,
    User,
    Case
)


class TestEnums:
    """Test enum value validation."""
    
    def test_response_type_enum_values(self):
        """Test ResponseType enum has expected values."""
        assert ResponseType.ANSWER == "ANSWER"
        assert ResponseType.PLAN_PROPOSAL == "PLAN_PROPOSAL"
        assert ResponseType.CLARIFICATION_REQUEST == "CLARIFICATION_REQUEST"
        assert ResponseType.CONFIRMATION_REQUEST == "CONFIRMATION_REQUEST"
        assert ResponseType.SOLUTION_READY == "SOLUTION_READY"
        assert ResponseType.NEEDS_MORE_DATA == "NEEDS_MORE_DATA"
        assert ResponseType.ESCALATION_REQUIRED == "ESCALATION_REQUIRED"
        assert ResponseType.VISUAL_DIAGRAM == "VISUAL_DIAGRAM"
        assert ResponseType.COMPARISON_TABLE == "COMPARISON_TABLE"

        # Test all values are present - updated to include all current enum values (9 total)
        expected_values = {
            "ANSWER", "PLAN_PROPOSAL", "CLARIFICATION_REQUEST", "CONFIRMATION_REQUEST",
            "SOLUTION_READY", "NEEDS_MORE_DATA", "ESCALATION_REQUIRED", "VISUAL_DIAGRAM", "COMPARISON_TABLE"
        }
        actual_values = {rt.value for rt in ResponseType}
        assert actual_values == expected_values
    
    def test_source_type_enum_values(self):
        """Test SourceType enum has expected values."""
        assert SourceType.KNOWLEDGE_BASE == "knowledge_base"
        assert SourceType.LOG_FILE == "log_file"
        assert SourceType.WEB_SEARCH == "web_search"
        assert SourceType.DOCUMENTATION == "documentation"
        assert SourceType.PREVIOUS_ANALYSIS == "previous_analysis"
        assert SourceType.USER_PROVIDED == "user_provided"
        
        # Test all values are present
        expected_values = {
            "knowledge_base", "log_file", "web_search", "documentation", 
            "previous_analysis", "user_provided"
        }
        actual_values = {st.value for st in SourceType}
        assert actual_values == expected_values


class TestSourceModel:
    """Test Source model validation."""
    
    def test_source_creation_valid(self):
        """Test valid Source creation."""
        source = Source(
            type=SourceType.KNOWLEDGE_BASE,
            content="This is content from the knowledge base..."
        )
        
        assert source.type == SourceType.KNOWLEDGE_BASE
        assert source.content == "This is content from the knowledge base..."
        assert source.confidence is None
        assert source.metadata is None
    
    def test_source_type_validation(self):
        """Test Source type field validation."""
        # Test with invalid type
        with pytest.raises(ValidationError):
            Source(
                type="invalid_type",
                content="test content"
            )
    
    def test_source_required_fields(self):
        """Test Source required fields validation."""
        # Missing type
        with pytest.raises(ValidationError):
            Source(content="test content")
        
        # Missing content
        with pytest.raises(ValidationError):
            Source(type=SourceType.LOG_FILE)
    
    def test_source_optional_fields(self):
        """Test Source optional fields."""
        source = Source(
            type=SourceType.KNOWLEDGE_BASE,
            content="Test content",
            confidence=0.85,
            metadata={"source_file": "test.md", "line_number": 42}
        )
        
        assert source.confidence == 0.85
        assert source.metadata["source_file"] == "test.md"
        assert source.metadata["line_number"] == 42


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
        from faultmaven.models.api import DataType, ProcessingStatus
        
        data = UploadedData(
            id="data_123",
            name="error.log",
            type=DataType.LOG_FILE,
            size_bytes=2048,
            upload_timestamp="2024-01-01T12:00:00Z",
            processing_status=ProcessingStatus.COMPLETED
        )
        
        assert data.id == "data_123"
        assert data.name == "error.log"
        assert data.type == DataType.LOG_FILE
        assert data.size_bytes == 2048
        assert data.upload_timestamp == "2024-01-01T12:00:00Z"
        assert data.processing_status == ProcessingStatus.COMPLETED
        assert data.processing_summary is None
        assert data.confidence_score is None
    
    def test_uploaded_data_required_fields(self):
        """Test UploadedData required fields validation."""
        from faultmaven.models.api import DataType, ProcessingStatus
        
        # Missing id
        with pytest.raises(ValidationError):
            UploadedData(
                name="test.log",
                type=DataType.LOG_FILE,
                size_bytes=1024,
                upload_timestamp="2024-01-01T12:00:00Z",
                processing_status=ProcessingStatus.PENDING
            )
        
        # Missing name
        with pytest.raises(ValidationError):
            UploadedData(
                id="123",
                type=DataType.LOG_FILE,
                size_bytes=1024,
                upload_timestamp="2024-01-01T12:00:00Z",
                processing_status=ProcessingStatus.PENDING
            )
        
        # Missing type
        with pytest.raises(ValidationError):
            UploadedData(
                id="123",
                name="test.log",
                size_bytes=1024,
                upload_timestamp="2024-01-01T12:00:00Z",
                processing_status=ProcessingStatus.PENDING
            )
        
        # Missing size_bytes
        with pytest.raises(ValidationError):
            UploadedData(
                id="123",
                name="test.log",
                type=DataType.LOG_FILE,
                upload_timestamp="2024-01-01T12:00:00Z",
                processing_status=ProcessingStatus.PENDING
            )
        
        # Missing upload_timestamp
        with pytest.raises(ValidationError):
            UploadedData(
                id="123",
                name="test.log",
                type=DataType.LOG_FILE,
                size_bytes=1024,
                processing_status=ProcessingStatus.PENDING
            )
        
        # Missing processing_status
        with pytest.raises(ValidationError):
            UploadedData(
                id="123",
                name="test.log",
                type=DataType.LOG_FILE,
                size_bytes=1024,
                upload_timestamp="2024-01-01T12:00:00Z"
            )
    
    def test_uploaded_data_optional_fields(self):
        """Test UploadedData optional fields."""
        from faultmaven.models.api import DataType, ProcessingStatus
        
        data = UploadedData(
            id="data_456",
            name="config.json",
            type=DataType.CONFIG_FILE,
            size_bytes=512,
            upload_timestamp="2024-01-01T12:00:00Z",
            processing_status=ProcessingStatus.COMPLETED,
            processing_summary="Successfully parsed configuration",
            confidence_score=0.95
        )
        
        assert data.processing_summary == "Successfully parsed configuration"
        assert data.confidence_score == 0.95


class TestViewStateModel:
    """Test ViewState model validation."""
    
    def test_view_state_creation_valid(self):
        """Test valid ViewState creation."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        case = Case(
            case_id="case_456",
            title="Database Connection Issue"
        )
        
        uploaded_data = [
            UploadedData(
                id="1",
                name="log1.txt",
                type=DataType.LOG_FILE,
                size_bytes=1024,
                upload_timestamp="2024-01-01T12:00:00Z",
                processing_status=ProcessingStatus.COMPLETED
            ),
            UploadedData(
                id="2",
                name="log2.txt",
                type=DataType.LOG_FILE,
                size_bytes=2048,
                upload_timestamp="2024-01-01T12:01:00Z",
                processing_status=ProcessingStatus.COMPLETED
            )
        ]
        
        view_state = ViewState(
            session_id="session_123",
            user=user,
            active_case=case,
            uploaded_data=uploaded_data
        )
        
        assert view_state.session_id == "session_123"
        assert view_state.user.user_id == "user_123"
        assert view_state.active_case.case_id == "case_456"
        assert len(view_state.uploaded_data) == 2
        assert view_state.uploaded_data[0].id == "1"
        assert view_state.show_case_selector == True  # default value
        assert view_state.show_data_upload == True  # default value
    
    def test_view_state_empty_uploaded_data(self):
        """Test ViewState with empty uploaded_data list."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user,
            uploaded_data=[]
        )
        
        assert len(view_state.uploaded_data) == 0
        assert view_state.active_case is None  # default value
        assert len(view_state.cases) == 0  # default empty list
        assert len(view_state.messages) == 0  # default empty list
    
    def test_view_state_required_fields(self):
        """Test ViewState required fields validation."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        # Missing session_id
        with pytest.raises(ValidationError):
            ViewState(
                user=user
            )
        
        # Missing user
        with pytest.raises(ValidationError):
            ViewState(
                session_id="session_123"
            )
    
    def test_view_state_optional_fields(self):
        """Test ViewState optional fields."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        case1 = Case(case_id="case_1", title="Issue 1")
        case2 = Case(case_id="case_2", title="Issue 2")
        
        memory_context = {
            "conversation_history": ["greeting", "question"],
            "user_preferences": {"language": "english"}
        }
        
        planning_state = {
            "current_phase": "analysis",
            "completed_steps": ["gather_info", "classify_issue"],
            "next_steps": ["analyze_logs", "propose_solution"]
        }
        
        view_state = ViewState(
            session_id="session_123",
            user=user,
            active_case=case1,
            cases=[case1, case2],
            messages=[{"type": "user", "content": "Hello"}],
            show_case_selector=False,
            show_data_upload=False,
            loading_state="Processing request...",
            memory_context=memory_context,
            planning_state=planning_state
        )
        
        assert view_state.active_case.case_id == "case_1"
        assert len(view_state.cases) == 2
        assert len(view_state.messages) == 1
        assert view_state.show_case_selector == False
        assert view_state.show_data_upload == False
        assert view_state.loading_state == "Processing request..."
        assert view_state.memory_context == memory_context
        assert view_state.planning_state == planning_state
    
    def test_view_state_new_fields_defaults(self):
        """Test that new memory_context and planning_state fields default to None."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user
        )
        
        # New fields should default to None
        assert view_state.memory_context is None
        assert view_state.planning_state is None
    
    def test_view_state_new_fields_validation(self):
        """Test validation of new memory_context and planning_state fields."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        # Test with various data types
        view_state = ViewState(
            session_id="session_123",
            user=user,
            memory_context={"key": "value", "nested": {"data": [1, 2, 3]}},
            planning_state={"phase": "test", "items": [], "active": True}
        )
        
        assert isinstance(view_state.memory_context, dict)
        assert isinstance(view_state.planning_state, dict)
        assert view_state.memory_context["nested"]["data"] == [1, 2, 3]
        assert view_state.planning_state["active"] is True


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
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        case = Case(
            case_id="case_456",
            title="Database Connection Issue"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user,
            active_case=case,
            uploaded_data=[]
        )
        
        sources = [
            Source(
                type=SourceType.KNOWLEDGE_BASE,
                content="Database troubleshooting steps..."
            )
        ]
        
        response = AgentResponse(
            content="The database error is caused by connection timeout.",
            response_type=ResponseType.ANSWER,
            session_id=view_state.session_id,
            case_id=case.case_id,
            view_state=view_state,
            sources=sources,
            plan=None
        )
        
        assert response.schema_version == "3.1.0"
        assert response.content == "The database error is caused by connection timeout."
        assert response.response_type == ResponseType.ANSWER
        assert response.view_state.active_case.case_id == "case_456"
        assert len(response.sources) == 1
        assert response.plan is None
    
    def test_agent_response_creation_valid_plan_proposal(self):
        """Test valid AgentResponse creation for PLAN_PROPOSAL type."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        case = Case(
            case_id="case_456",
            title="Database Connection Issue"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user,
            active_case=case,
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
            session_id=view_state.session_id,
            case_id="case_456",
            view_state=view_state,
            sources=[],
            plan=plan
        )
        
        assert response.response_type == ResponseType.PLAN_PROPOSAL
        assert len(response.plan) == 3
        assert response.plan[0].description == "Step 1: Check database connection"
    
    def test_agent_response_default_values(self):
        """Test AgentResponse default values."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user
        )
        
        response = AgentResponse(
            content="Test content",
            response_type=ResponseType.ANSWER,
            session_id=view_state.session_id,
            case_id="test_case_id",
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
        (ResponseType.SOLUTION_READY, None, True),
        (ResponseType.NEEDS_MORE_DATA, None, True),
        (ResponseType.ESCALATION_REQUIRED, None, True),
        (ResponseType.VISUAL_DIAGRAM, None, True),
        (ResponseType.COMPARISON_TABLE, None, True),
        (ResponseType.PLAN_PROPOSAL, [PlanStep(description="test")], True),
        (ResponseType.PLAN_PROPOSAL, None, False),  # Should fail
        (ResponseType.ANSWER, [PlanStep(description="test")], False),  # Should fail
        (ResponseType.CLARIFICATION_REQUEST, [PlanStep(description="test")], False),  # Should fail
        (ResponseType.CONFIRMATION_REQUEST, [PlanStep(description="test")], False),  # Should fail
        (ResponseType.SOLUTION_READY, [PlanStep(description="test")], False),  # Should fail
        (ResponseType.NEEDS_MORE_DATA, [PlanStep(description="test")], False),  # Should fail
        (ResponseType.ESCALATION_REQUIRED, [PlanStep(description="test")], False),  # Should fail
        (ResponseType.VISUAL_DIAGRAM, [PlanStep(description="test")], False),  # Should fail
        (ResponseType.COMPARISON_TABLE, [PlanStep(description="test")], False)  # Should fail
    ])
    def test_agent_response_plan_consistency_validator(self, response_type, plan, should_pass):
        """Test AgentResponse plan consistency validation."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user
        )
        
        if should_pass:
            response = AgentResponse(
                content="Test content",
                response_type=response_type,
                session_id=view_state.session_id,
                case_id="test_case_id",
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
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user
        )
        
        # Schema version can now accept any string value, but defaults to 3.1.0
        response_with_custom_version = AgentResponse(
            schema_version="2.0.0",  # This should now be accepted
            content="Test content",
            response_type=ResponseType.ANSWER,
            session_id=view_state.session_id,
            case_id="test_case_id",
            view_state=view_state
        )
        assert response_with_custom_version.schema_version == "2.0.0"
        
        # Create without specifying schema_version - should default to 3.1.0
        response = AgentResponse(
            content="Test content",
            response_type=ResponseType.ANSWER,
            session_id=view_state.session_id,
            case_id="test_case_id",
            view_state=view_state
        )
        assert response.schema_version == "3.1.0"
    
    def test_agent_response_required_fields(self):
        """Test AgentResponse required fields validation."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user
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
        """Test valid ErrorResponse creation with proper ErrorDetail structure."""
        error_response = ErrorResponse(
            error=ErrorDetail(
                code="VALIDATION_ERROR",
                message="Input validation failed"
            )
        )
        
        assert error_response.schema_version == "3.1.0"
        assert error_response.error.code == "VALIDATION_ERROR"
        assert error_response.error.message == "Input validation failed"
    
    def test_error_response_schema_version_enforcement(self):
        """Test ErrorResponse has schema_version field."""
        error_response = ErrorResponse(
            error=ErrorDetail(
                code="TEST_ERROR",
                message="Test error message"
            )
        )
        
        # ErrorResponse has schema_version field
        assert hasattr(error_response, 'schema_version')
        assert error_response.schema_version == "3.1.0"
        assert error_response.error.code == "TEST_ERROR"
        assert error_response.error.message == "Test error message"
    
    def test_error_response_required_fields(self):
        """Test ErrorResponse required fields validation."""
        # Missing error field entirely
        with pytest.raises(ValidationError):
            ErrorResponse()
        
        # Valid ErrorResponse
        error_response = ErrorResponse(
            error=ErrorDetail(
                code="TEST_ERROR",
                message="Test message"
            )
        )
        assert error_response.error.code == "TEST_ERROR"
        assert error_response.error.message == "Test message"
        
    
    def test_standard_error_response_creation_valid(self):
        """Test valid StandardErrorResponse creation."""
        error_response = StandardErrorResponse(
            detail="Input validation failed",
            error_type="VALIDATION_ERROR",
            correlation_id="corr-123",
            timestamp="2024-01-01T12:00:00Z"
        )
        
        assert error_response.detail == "Input validation failed"
        assert error_response.error_type == "VALIDATION_ERROR"
        assert error_response.correlation_id == "corr-123"
        assert error_response.timestamp == "2024-01-01T12:00:00Z"


class TestModelSerialization:
    """Test model serialization and deserialization."""
    
    def test_agent_response_serialization(self):
        """Test AgentResponse JSON serialization."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        case = Case(
            case_id="case_456",
            title="Serialization Test"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user,
            active_case=case,
            uploaded_data=[
                UploadedData(
                    id="1",
                    name="test.log",
                    type=DataType.LOG_FILE,
                    size_bytes=1024,
                    upload_timestamp="2024-01-01T12:00:00Z",
                    processing_status=ProcessingStatus.COMPLETED
                )
            ]
        )
        
        sources = [
            Source(
                type=SourceType.KNOWLEDGE_BASE,
                content="Test content from knowledge base"
            )
        ]
        
        plan = [
            PlanStep(description="Test step 1"),
            PlanStep(description="Test step 2")
        ]
        
        response = AgentResponse(
            content="Test response content",
            response_type=ResponseType.PLAN_PROPOSAL,
            session_id=view_state.session_id,
            case_id="test_case_id",
            view_state=view_state,
            sources=sources,
            plan=plan
        )
        
        # Test serialization
        json_data = response.dict()
        
        assert json_data["schema_version"] == "3.1.0"
        assert json_data["content"] == "Test response content"
        assert json_data["response_type"] == "PLAN_PROPOSAL"
        assert json_data["view_state"]["session_id"] == "session_123"
        assert len(json_data["sources"]) == 1
        assert len(json_data["plan"]) == 2
        
        # Test deserialization
        reconstructed = AgentResponse(**json_data)
        assert reconstructed.content == response.content
        assert reconstructed.response_type == response.response_type
        assert reconstructed.view_state.active_case.case_id == response.view_state.active_case.case_id
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
        """Test ErrorResponse JSON serialization with proper structure."""
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
    
    def test_standard_error_response_serialization(self):
        """Test StandardErrorResponse JSON serialization."""
        error_response = StandardErrorResponse(
            detail="Test error message",
            error_type="TEST_ERROR",
            correlation_id="corr-789",
            timestamp="2024-01-01T12:00:00Z"
        )
        
        # Test serialization
        json_data = error_response.dict()
        
        assert json_data["detail"] == "Test error message"
        assert json_data["error_type"] == "TEST_ERROR"
        assert json_data["correlation_id"] == "corr-789"
        assert json_data["timestamp"] == "2024-01-01T12:00:00Z"
        
        # Test deserialization
        reconstructed = StandardErrorResponse(**json_data)
        assert reconstructed.detail == error_response.detail
        assert reconstructed.error_type == error_response.error_type
        assert reconstructed.correlation_id == error_response.correlation_id
        assert reconstructed.timestamp == error_response.timestamp


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    def test_empty_strings_validation(self):
        """Test validation with empty strings."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user,
            loading_state=""  # Empty string should be valid
        )
        
        # Empty content should be valid
        response = AgentResponse(
            content="",
            response_type=ResponseType.ANSWER,
            session_id=view_state.session_id,
            case_id="test_case_id",
            view_state=view_state
        )
        
        assert response.content == ""
        assert response.view_state.loading_state == ""
    
    def test_large_data_validation(self):
        """Test validation with large data sets."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User"
        )
        
        # Large number of sources
        sources = [
            Source(
                type=SourceType.KNOWLEDGE_BASE,
                content=f"This is content from document {i} " * 10
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
            user=user
        )
        
        response = AgentResponse(
            content="Large response test",
            response_type=ResponseType.PLAN_PROPOSAL,
            session_id=view_state.session_id,
            case_id="test_case_id",
            view_state=view_state,
            sources=sources,
            plan=plan
        )
        
        assert len(response.sources) == 100
        assert len(response.plan) == 50
    
    def test_unicode_and_special_characters(self):
        """Test validation with unicode and special characters."""
        user = User(
            user_id="user_123",
            email="test@example.com",
            name="Test User with émojis 🚀"
        )
        
        case = Case(
            case_id="case_456",
            title="Test with special chars: @#$%^&*()"
        )
        
        view_state = ViewState(
            session_id="session_123",
            user=user,
            active_case=case,
            loading_state="Processing with émojis 🚀"
        )
        
        response = AgentResponse(
            content="Response with unicode: 测试中文, émojis: 🔧🐛, and symbols: ←→↑↓",
            response_type=ResponseType.ANSWER,
            session_id=view_state.session_id,
            case_id=case.case_id,
            view_state=view_state
        )
        
        assert "🚀" in response.view_state.user.name
        assert "测试中文" in response.content
        assert "🔧🐛" in response.content
        assert "@#$%^&*()" in response.view_state.active_case.title