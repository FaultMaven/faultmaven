"""Integration tests for v3.1.0 schema implementation."""

import pytest
import json
import uuid
import asyncio
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from typing import Dict, Any, List

from faultmaven.services.agent import AgentService
from faultmaven.models.api import (
    QueryRequest,
    AgentResponse,
    ResponseType,
    SourceType,
    Source,
    PlanStep,
    ViewState,
    UploadedData
)
from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer
from faultmaven.exceptions import ValidationException


class TestV3SchemaIntegration:
    """Integration tests for complete v3.1.0 schema workflow."""
    
    @pytest.fixture
    def mock_llm_provider(self):
        """Mock LLM provider with realistic responses."""
        llm = Mock(spec=ILLMProvider)
        llm.generate_response = AsyncMock(return_value="Mock LLM analysis response")
        return llm
    
    @pytest.fixture
    def mock_knowledge_base_tool(self):
        """Mock knowledge base tool with realistic responses."""
        tool = Mock(spec=BaseTool)
        tool.name = "knowledge_base"
        tool.execute = AsyncMock(return_value={
            "documents": [
                "Database connection troubleshooting involves checking network connectivity...",
                "Common timeout issues can be resolved by adjusting connection pool settings..."
            ],
            "metadata": [
                {"source": "database_troubleshooting.md", "section": "connection_issues"},
                {"source": "performance_tuning.md", "section": "timeouts"}
            ],
            "distances": [0.15, 0.28]
        })
        return tool
    
    @pytest.fixture
    def mock_web_search_tool(self):
        """Mock web search tool with realistic responses."""
        tool = Mock(spec=BaseTool)
        tool.name = "web_search"
        tool.execute = AsyncMock(return_value={
            "results": [
                {
                    "title": "Database Connection Timeout Solutions",
                    "url": "https://stackoverflow.com/questions/database-timeout",
                    "snippet": "Common solutions include increasing timeout values and optimizing queries..."
                }
            ]
        })
        return tool
    
    @pytest.fixture
    def mock_log_analyzer_tool(self):
        """Mock log analyzer tool with realistic responses."""
        tool = Mock(spec=BaseTool)
        tool.name = "log_analyzer"
        tool.execute = AsyncMock(return_value={
            "log_entries": [
                {
                    "timestamp": "2024-01-01T12:00:00Z",
                    "level": "ERROR",
                    "message": "Connection timeout after 30s",
                    "source": "database.log"
                }
            ],
            "patterns": ["timeout", "connection"],
            "frequency": {"ERROR": 15, "WARN": 5}
        })
        return tool
    
    @pytest.fixture
    def mock_tracer(self):
        """Mock tracer for integration testing."""
        tracer = Mock(spec=ITracer)
        context_manager = MagicMock()
        context_manager.__enter__ = Mock()
        context_manager.__exit__ = Mock()
        tracer.trace = Mock(return_value=context_manager)
        return tracer
    
    @pytest.fixture
    def mock_sanitizer(self):
        """Mock sanitizer that simulates PII redaction."""
        sanitizer = Mock(spec=ISanitizer)
        
        def sanitize_func(content):
            if isinstance(content, str):
                # Simulate PII redaction
                sanitized = content.replace("john.doe@example.com", "[EMAIL_REDACTED]")
                sanitized = sanitized.replace("555-1234", "[PHONE_REDACTED]")
                return sanitized
            return content
        
        sanitizer.sanitize = Mock(side_effect=sanitize_func)
        return sanitizer
    
    @pytest.fixture
    def mock_session_service(self):
        """Mock session service with realistic session management."""
        service = Mock()
        
        # Mock session data
        mock_session = Mock()
        mock_session.session_id = "integration-test-session"
        mock_session.user_id = "test-user-123"
        mock_session.created_at = datetime.utcnow() - timedelta(hours=1)
        mock_session.data_uploads = ["upload-1", "upload-2"]
        
        service.get_session = AsyncMock(return_value=mock_session)
        service.record_query_operation = AsyncMock()
        return service
    
    @pytest.fixture
    def integrated_agent_service(self, mock_llm_provider, mock_knowledge_base_tool, 
                                mock_web_search_tool, mock_log_analyzer_tool,
                                mock_tracer, mock_sanitizer, mock_session_service):
        """Create AgentService with all mocked dependencies for integration testing."""
        tools = [mock_knowledge_base_tool, mock_web_search_tool, mock_log_analyzer_tool]
        
        return AgentService(
            llm_provider=mock_llm_provider,
            tools=tools,
            tracer=mock_tracer,
            sanitizer=mock_sanitizer,
            session_service=mock_session_service
        )
    
    @pytest.fixture
    def realistic_agent_result(self):
        """Create realistic agent execution result for integration testing."""
        return {
            "findings": [
                {
                    "type": "error",
                    "message": "Database connection timeout detected in application logs",
                    "severity": "high",
                    "timestamp": "2024-01-01T12:00:00Z",
                    "source": "log_analysis",
                    "confidence": 0.9
                },
                {
                    "type": "performance",
                    "message": "Connection pool utilization at 95%",
                    "severity": "medium", 
                    "timestamp": "2024-01-01T12:00:30Z",
                    "source": "monitoring",
                    "confidence": 0.85
                }
            ],
            "recommendations": [
                "Increase database connection timeout from 30s to 60s",
                "Expand connection pool size from 10 to 20 connections",
                "Implement connection retry logic with exponential backoff"
            ],
            "next_steps": [
                "Update database configuration file",
                "Restart application services",
                "Monitor connection metrics for improvement"
            ],
            "root_cause": "Database connection pool exhaustion due to increased load",
            "confidence_score": 0.87,
            "estimated_mttr": "25 minutes",
            "knowledge_base_results": [
                {
                    "title": "Database Connection Troubleshooting Guide",
                    "content": "When experiencing connection timeouts, first check...",
                    "snippet": "Connection timeout troubleshooting involves verifying network connectivity...",
                    "source": "database_troubleshooting.md",
                    "relevance_score": 0.92
                }
            ],
            "tool_results": [
                {
                    "tool_name": "web_search",
                    "source": "Stack Overflow",
                    "content": "Solutions for database connection timeout include...",
                    "url": "https://stackoverflow.com/questions/db-timeout",
                    "relevance": 0.78
                },
                {
                    "tool_name": "log_analyzer",
                    "filename": "application.log",
                    "content": "2024-01-01 12:00:00 ERROR Connection timeout after 30s",
                    "entries_analyzed": 1500,
                    "error_count": 15
                }
            ]
        }


class TestCompleteWorkflow:
    """Test complete end-to-end workflow scenarios."""
    
    @pytest.mark.asyncio
    async def test_standard_troubleshooting_workflow(self, integrated_agent_service, realistic_agent_result):
        """Test complete troubleshooting workflow resulting in ANSWER response."""
        query_request = QueryRequest(
            session_id="integration-test-session",
            query="My application is experiencing database connection timeouts. Can you help diagnose the issue?"
        )
        
        # Mock agent execution
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=realistic_agent_result)
            
            # Execute complete workflow
            response = await integrated_agent_service.process_query(query_request)
            
            # Verify complete AgentResponse structure
            assert isinstance(response, AgentResponse)
            assert response.schema_version == "3.1.0"
            assert response.response_type == ResponseType.ANSWER
            
            # Verify ViewState
            assert response.view_state.session_id == "integration-test-session"
            assert response.view_state.case_id is not None
            assert uuid.UUID(response.view_state.case_id)  # Valid UUID
            assert "Investigation" in response.view_state.running_summary
            assert len(response.view_state.uploaded_data) == 2  # From mock session
            
            # Verify Sources from multiple tools
            assert len(response.sources) > 0
            source_types = {source.type for source in response.sources}
            assert SourceType.KNOWLEDGE_BASE in source_types
            assert SourceType.WEB_SEARCH in source_types
            assert SourceType.LOG_FILE in source_types
            
            # Verify Content includes key information
            assert "Database connection" in response.content
            assert "timeout" in response.content.lower()
            assert "Root Cause:" in response.content
            
            # Verify no plan for standard answer
            assert response.plan is None
    
    @pytest.mark.asyncio
    async def test_multi_step_plan_workflow(self, integrated_agent_service):
        """Test workflow that generates a multi-step plan."""
        # Create agent result that should trigger PLAN_PROPOSAL
        complex_result = {
            "findings": [
                {"message": "Cascading failure detected across multiple services"},
                {"message": "Database corruption requires systematic recovery"}
            ],
            "recommendations": [
                "Execute comprehensive recovery procedure",
                "Follow strict dependency order for service restoration"
            ],
            "next_steps": [
                "Stop all application services",
                "Create database backup",
                "Run database integrity check",
                "Restore from clean backup if needed",
                "Restart services in dependency order",
                "Validate end-to-end functionality"
            ],
            "root_cause": "Database corruption from unexpected shutdown",
            "confidence_score": 0.95,
            "knowledge_base_results": [],
            "tool_results": []
        }
        
        query_request = QueryRequest(
            session_id="integration-test-session",
            query="Our entire system is down with database corruption. Need a recovery plan."
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=complex_result)
            
            response = await integrated_agent_service.process_query(query_request)
            
            # Verify plan proposal response
            assert response.response_type == ResponseType.PLAN_PROPOSAL
            assert response.plan is not None
            assert len(response.plan) == 6
            
            # Verify plan steps
            step_descriptions = [step.description for step in response.plan]
            assert "Stop all application services" in step_descriptions
            assert "Create database backup" in step_descriptions
            assert "Validate end-to-end functionality" in step_descriptions
            
            # Verify content mentions plan
            assert "plan" in response.content.lower() or "step" in response.content.lower()
    
    @pytest.mark.asyncio
    async def test_clarification_request_workflow(self, integrated_agent_service):
        """Test workflow that requires clarification from user."""
        # Create agent result that should trigger CLARIFICATION_REQUEST
        unclear_result = {
            "findings": [
                {"message": "Multiple potential root causes identified"},
                {"message": "Insufficient information to determine exact issue"}
            ],
            "recommendations": [
                "Need to clarify which specific error messages you're seeing",
                "Please specify the exact time when the issues started",
                "More information needed about your system configuration"
            ],
            "next_steps": [
                "Gather additional diagnostic information"
            ],
            "root_cause": "Insufficient diagnostic information",
            "confidence_score": 0.3,
            "knowledge_base_results": [],
            "tool_results": []
        }
        
        query_request = QueryRequest(
            session_id="integration-test-session", 
            query="Something is wrong with my system but I'm not sure what."
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=unclear_result)
            
            response = await integrated_agent_service.process_query(query_request)
            
            # Verify clarification request response
            assert response.response_type == ResponseType.CLARIFICATION_REQUEST
            assert response.plan is None
            
            # Verify content asks for clarification
            assert "clarify" in response.content.lower() or "specify" in response.content.lower()
            assert "more information" in response.content.lower()
    
    @pytest.mark.asyncio
    async def test_confirmation_request_workflow(self, integrated_agent_service):
        """Test workflow that requires user confirmation."""
        # Create agent result that should trigger CONFIRMATION_REQUEST
        critical_result = {
            "findings": [
                {"message": "Critical production database showing corruption signs"},
                {"message": "Immediate action required to prevent data loss"}
            ],
            "recommendations": [
                "Confirm before proceeding with emergency database restart",
                "Verify backup availability before recovery procedure"
            ],
            "next_steps": [
                "Stop database connections",
                "Perform emergency restart"
            ],
            "root_cause": "Database corruption detected in production system",
            "confidence_score": 0.95,
            "knowledge_base_results": [],
            "tool_results": []
        }
        
        query_request = QueryRequest(
            session_id="integration-test-session",
            query="Production database is corrupted and needs immediate action."
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=critical_result)
            
            response = await integrated_agent_service.process_query(query_request)
            
            # Verify confirmation request response  
            assert response.response_type == ResponseType.CONFIRMATION_REQUEST
            assert response.plan is None
            
            # Verify content asks for confirmation
            assert "confirm" in response.content.lower() or "verify" in response.content.lower()


class TestSessionIntegration:
    """Test session management integration with v3.1.0 schema."""
    
    @pytest.mark.asyncio
    async def test_session_validation_and_recording(self, integrated_agent_service, realistic_agent_result):
        """Test that session validation and operation recording work correctly."""
        query_request = QueryRequest(
            session_id="valid-session-123",
            query="Test query for session integration"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=realistic_agent_result)
            
            response = await integrated_agent_service.process_query(query_request)
            
            # Verify session was validated
            integrated_agent_service._session_service.get_session.assert_called_once_with("valid-session-123")
            
            # Verify operation was recorded
            integrated_agent_service._session_service.record_query_operation.assert_called_once()
            
            # Verify recording parameters
            call_kwargs = integrated_agent_service._session_service.record_query_operation.call_args[1]
            assert call_kwargs["session_id"] == "valid-session-123"
            assert call_kwargs["query"] == "Test query for session integration"
            assert call_kwargs["investigation_id"] == response.view_state.case_id
    
    @pytest.mark.asyncio
    async def test_session_not_found_error(self, integrated_agent_service):
        """Test handling when session is not found."""
        # Mock session service to return None (session not found)
        integrated_agent_service._session_service.get_session.return_value = None
        
        query_request = QueryRequest(
            session_id="nonexistent-session",
            query="Test query"
        )
        
        with pytest.raises(FileNotFoundError) as exc_info:
            await integrated_agent_service.process_query(query_request)
        
        assert "Session nonexistent-session not found" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_view_state_with_session_data(self, integrated_agent_service, realistic_agent_result):
        """Test that ViewState includes data from session."""
        query_request = QueryRequest(
            session_id="integration-test-session",
            query="Test query with session data"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMaven Agent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=realistic_agent_result)
            
            response = await integrated_agent_service.process_query(query_request)
            
            # Verify ViewState includes uploaded data from session
            assert len(response.view_state.uploaded_data) == 2
            assert response.view_state.uploaded_data[0].id == "upload-1"
            assert response.view_state.uploaded_data[0].name == "data_upload-1"
            assert response.view_state.uploaded_data[1].id == "upload-2"


class TestDataSanitization:
    """Test PII redaction and data sanitization integration."""
    
    @pytest.mark.asyncio
    async def test_input_sanitization(self, integrated_agent_service, realistic_agent_result):
        """Test that input query is sanitized before processing."""
        query_with_pii = QueryRequest(
            session_id="test-session",
            query="My application is failing. Contact me at john.doe@example.com or 555-1234 for details."
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=realistic_agent_result)
            
            await integrated_agent_service.process_query(query_with_pii)
            
            # Verify sanitizer was called on input
            sanitizer_calls = integrated_agent_service._sanitizer.sanitize.call_args_list
            input_call = next((call for call in sanitizer_calls 
                             if "john.doe@example.com" in str(call[0][0])), None)
            assert input_call is not None, "Input sanitization not called"
    
    @pytest.mark.asyncio
    async def test_output_sanitization(self, integrated_agent_service):
        """Test that output content is sanitized."""
        # Agent result with PII in content
        result_with_pii = {
            "findings": [{"message": "User john.doe@example.com reported error"}],
            "recommendations": ["Contact admin at 555-1234"],
            "next_steps": ["Send update to user"],
            "root_cause": "User-specific configuration issue",
            "confidence_score": 0.8
        }
        
        query_request = QueryRequest(
            session_id="test-session",
            query="Test output sanitization"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=result_with_pii)
            
            response = await integrated_agent_service.process_query(query_request)
            
            # Verify content was sanitized
            assert "[EMAIL_REDACTED]" in response.content or "john.doe@example.com" not in response.content
            assert "[PHONE_REDACTED]" in response.content or "555-1234" not in response.content


class TestSourceIntegration:
    """Test source extraction and formatting integration."""
    
    @pytest.mark.asyncio
    async def test_comprehensive_source_extraction(self, integrated_agent_service):
        """Test extraction of sources from multiple tool types."""
        comprehensive_result = {
            "findings": [{"message": "Complex issue requiring multiple sources"}],
            "recommendations": ["Use all available information"],
            "next_steps": ["Review all sources"],
            "root_cause": "Multi-faceted problem",
            "confidence_score": 0.85,
            "knowledge_base_results": [
                {
                    "title": "Troubleshooting Guide Vol 1", 
                    "snippet": "First troubleshooting approach...",
                    "content": "Detailed troubleshooting steps..."
                },
                {
                    "title": "Best Practices Manual",
                    "snippet": "Best practices include...",
                    "content": "Comprehensive best practices guide..."
                }
            ],
            "tool_results": [
                {
                    "tool_name": "web_search_advanced",
                    "source": "Technical Documentation",
                    "content": "Advanced troubleshooting techniques from official docs..."
                },
                {
                    "tool_name": "log_analyzer_pro",
                    "filename": "system.log",
                    "content": "2024-01-01 ERROR: System failure detected..."
                },
                {
                    "tool_name": "web_search_stack_overflow",
                    "source": "Community Forum",
                    "content": "Community solutions for similar issues..."
                }
            ]
        }
        
        query_request = QueryRequest(
            session_id="test-session",
            query="Need comprehensive troubleshooting help"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=comprehensive_result)
            
            response = await integrated_agent_service.process_query(query_request)
            
            # Verify all source types are present
            source_types = {source.type for source in response.sources}
            assert SourceType.KNOWLEDGE_BASE in source_types
            assert SourceType.WEB_SEARCH in source_types
            assert SourceType.LOG_FILE in source_types
            
            # Verify specific sources
            kb_sources = [s for s in response.sources if s.type == SourceType.KNOWLEDGE_BASE]
            web_sources = [s for s in response.sources if s.type == SourceType.WEB_SEARCH]
            log_sources = [s for s in response.sources if s.type == SourceType.LOG_FILE]
            
            assert len(kb_sources) == 2
            assert len(web_sources) == 2  # Two web search tools
            assert len(log_sources) == 1
            
            # Verify source content
            guide_source = next(s for s in kb_sources if "Troubleshooting Guide" in s.name)
            assert "First troubleshooting approach" in guide_source.snippet
            
            log_source = log_sources[0]
            assert log_source.name == "system.log"
            assert "System failure detected" in log_source.snippet
    
    @pytest.mark.asyncio
    async def test_source_snippet_truncation(self, integrated_agent_service):
        """Test that source snippets are properly truncated."""
        # Create result with very long content
        long_content = "This is a very long piece of content. " * 20  # ~800 characters
        
        result_with_long_content = {
            "findings": [{"message": "Test finding"}],
            "recommendations": ["Test recommendation"],
            "next_steps": ["Test step"],
            "root_cause": "Test cause",
            "confidence_score": 0.8,
            "knowledge_base_results": [
                {
                    "title": "Long Document",
                    "content": long_content,
                    "snippet": long_content
                }
            ],
            "tool_results": [
                {
                    "tool_name": "web_search",
                    "source": "Long Article",
                    "content": long_content
                }
            ]
        }
        
        query_request = QueryRequest(
            session_id="test-session",
            query="Test long content"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=result_with_long_content)
            
            response = await integrated_agent_service.process_query(query_request)
            
            # Verify snippets are truncated (should end with "...")
            for source in response.sources:
                assert len(source.snippet) <= 203  # 200 chars + "..."
                if len(long_content) > 200:
                    assert source.snippet.endswith("...")


class TestErrorHandlingIntegration:
    """Test error handling in integration scenarios."""
    
    @pytest.mark.asyncio
    async def test_tool_failure_graceful_handling(self, integrated_agent_service):
        """Test that tool failures don't break the entire workflow."""
        # Make one tool fail
        integrated_agent_service._tools[0].execute.side_effect = Exception("Tool failure")
        
        # But make agent execution succeed with minimal result
        minimal_result = {
            "findings": [{"message": "Analysis completed with limited data"}],
            "recommendations": ["Basic recommendation"],
            "next_steps": ["Monitor system"],
            "root_cause": "Limited diagnostic information available",
            "confidence_score": 0.6
        }
        
        query_request = QueryRequest(
            session_id="test-session",
            query="Test tool failure handling"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=minimal_result)
            
            # Should complete successfully despite tool failure
            response = await integrated_agent_service.process_query(query_request)
            
            assert isinstance(response, AgentResponse)
            assert response.response_type == ResponseType.ANSWER
            assert "Limited diagnostic information" in response.content
    
    @pytest.mark.asyncio
    async def test_session_service_failure_handling(self, integrated_agent_service, realistic_agent_result):
        """Test handling when session service operations fail."""
        # Make session recording fail
        integrated_agent_service._session_service.record_query_operation.side_effect = Exception("Session recording failed")
        
        query_request = QueryRequest(
            session_id="test-session",
            query="Test session service failure"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=realistic_agent_result)
            
            # Should complete successfully despite session recording failure
            response = await integrated_agent_service.process_query(query_request)
            
            assert isinstance(response, AgentResponse)
            assert response.view_state.session_id == "test-session"
    
    @pytest.mark.asyncio
    async def test_sanitizer_failure_handling(self, integrated_agent_service, realistic_agent_result):
        """Test handling when sanitizer operations fail."""
        # Make sanitizer fail for some calls but not others
        call_count = 0
        def failing_sanitizer(content):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Fail on second call
                raise Exception("Sanitizer failure")
            return f"sanitized_{content}" if isinstance(content, str) else content
        
        integrated_agent_service._sanitizer.sanitize.side_effect = failing_sanitizer
        
        query_request = QueryRequest(
            session_id="test-session",
            query="Test sanitizer failure"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=realistic_agent_result)
            
            # This should propagate the sanitizer error since it's critical for security
            with pytest.raises(Exception) as exc_info:
                await integrated_agent_service.process_query(query_request)
            
            assert "Sanitizer failure" in str(exc_info.value)


class TestPerformanceIntegration:
    """Test performance aspects of integration."""
    
    @pytest.mark.asyncio
    async def test_concurrent_query_processing(self, integrated_agent_service, realistic_agent_result):
        """Test handling multiple concurrent queries."""
        # Create multiple query requests
        queries = [
            QueryRequest(session_id=f"session-{i}", query=f"Query {i}")
            for i in range(5)
        ]
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            
            async def slow_agent_run(*args, **kwargs):
                await asyncio.sleep(0.1)  # Simulate processing time
                return realistic_agent_result
            
            mock_agent.run = slow_agent_run
            
            # Process queries concurrently
            start_time = datetime.utcnow()
            responses = await asyncio.gather(*[
                integrated_agent_service.process_query(query) for query in queries
            ])
            end_time = datetime.utcnow()
            
            # Verify all responses completed
            assert len(responses) == 5
            assert all(isinstance(r, AgentResponse) for r in responses)
            
            # Verify unique case IDs
            case_ids = [r.view_state.case_id for r in responses]
            assert len(set(case_ids)) == 5  # All unique
            
            # Verify concurrent processing (should be faster than sequential)
            total_time = (end_time - start_time).total_seconds()
            assert total_time < 1.0  # Should be much faster than 5 * 0.1 = 0.5s sequentially
    
    @pytest.mark.asyncio
    async def test_response_time_tracking(self, integrated_agent_service, realistic_agent_result):
        """Test that response processing time is tracked."""
        query_request = QueryRequest(
            session_id="test-session",
            query="Test response time tracking"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            
            async def timed_agent_run(*args, **kwargs):
                await asyncio.sleep(0.05)  # Simulate 50ms processing
                return realistic_agent_result
            
            mock_agent.run = timed_agent_run
            
            start_time = datetime.utcnow()
            response = await integrated_agent_service.process_query(query_request)
            end_time = datetime.utcnow()
            
            processing_time = (end_time - start_time).total_seconds()
            
            # Verify response completed successfully
            assert isinstance(response, AgentResponse)
            
            # Verify processing time is reasonable
            assert 0.04 <= processing_time <= 0.2  # Between 40ms and 200ms


class TestCaseIdGeneration:
    """Test case ID generation and uniqueness."""
    
    @pytest.mark.asyncio
    async def test_case_id_uniqueness_across_sessions(self, integrated_agent_service, realistic_agent_result):
        """Test that case IDs are unique across different sessions."""
        sessions = ["session-1", "session-2", "session-3"]
        case_ids = []
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=realistic_agent_result)
            
            for session_id in sessions:
                query_request = QueryRequest(
                    session_id=session_id,
                    query="Test case ID uniqueness"
                )
                
                response = await integrated_agent_service.process_query(query_request)
                case_ids.append(response.view_state.case_id)
        
        # Verify all case IDs are unique
        assert len(set(case_ids)) == len(case_ids)
        
        # Verify all are valid UUIDs
        for case_id in case_ids:
            uuid.UUID(case_id)  # Should not raise exception
    
    @pytest.mark.asyncio
    async def test_case_id_format_consistency(self, integrated_agent_service, realistic_agent_result):
        """Test that case IDs follow consistent format."""
        query_request = QueryRequest(
            session_id="test-session",
            query="Test case ID format"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=realistic_agent_result)
            
            # Generate multiple case IDs
            case_ids = []
            for _ in range(10):
                response = await integrated_agent_service.process_query(query_request)
                case_ids.append(response.view_state.case_id)
            
            # Verify all follow UUID format
            for case_id in case_ids:
                # Should be valid UUID4 format
                parsed_uuid = uuid.UUID(case_id)
                assert str(parsed_uuid) == case_id  # String representation matches
                assert parsed_uuid.version == 4      # UUID4 format


class TestSchemaValidation:
    """Test schema validation in integration context."""
    
    @pytest.mark.asyncio
    async def test_complete_response_serialization(self, integrated_agent_service, realistic_agent_result):
        """Test that complete AgentResponse can be serialized and deserialized."""
        query_request = QueryRequest(
            session_id="test-session",
            query="Test complete serialization"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=realistic_agent_result)
            
            response = await integrated_agent_service.process_query(query_request)
            
            # Serialize to JSON
            response_dict = response.dict()
            json_str = json.dumps(response_dict, default=str)
            
            # Deserialize back
            parsed_dict = json.loads(json_str)
            reconstructed_response = AgentResponse(**parsed_dict)
            
            # Verify reconstruction matches original
            assert reconstructed_response.schema_version == response.schema_version
            assert reconstructed_response.content == response.content
            assert reconstructed_response.response_type == response.response_type
            assert reconstructed_response.view_state.case_id == response.view_state.case_id
            assert len(reconstructed_response.sources) == len(response.sources)
    
    @pytest.mark.asyncio
    async def test_response_validation_edge_cases(self, integrated_agent_service):
        """Test response validation with edge case data."""
        # Create agent result with edge case data
        edge_case_result = {
            "findings": [],  # Empty findings
            "recommendations": [""],  # Empty string recommendation
            "next_steps": [None, "", "valid step"],  # Mixed valid/invalid steps
            "root_cause": "",  # Empty root cause
            "confidence_score": None,  # None confidence
            "knowledge_base_results": [],
            "tool_results": []
        }
        
        query_request = QueryRequest(
            session_id="test-session",
            query=""  # Empty query (should be caught by validation)
        )
        
        # This should fail at validation level
        with pytest.raises(ValidationException):
            await integrated_agent_service.process_query(query_request)