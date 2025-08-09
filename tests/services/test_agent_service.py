"""Comprehensive test suite for AgentService - Phase 3 Testing

This test module validates the AgentService which uses interface-based
dependency injection for better testability and maintainability.

All dependencies are mocked via interfaces to ensure true unit testing isolation.

Test Coverage:
- Happy path scenarios
- Input validation
- Error handling and propagation
- Interface interaction verification  
- Async operation testing
- Performance validation
"""

import pytest
import uuid
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from typing import Any, Dict, List

from faultmaven.services.agent_service import AgentService
from faultmaven.models import QueryRequest, TroubleshootingResponse
from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer, ToolResult


class TestAgentService:
    """Comprehensive test suite for AgentService"""

    @pytest.fixture
    def mock_llm_provider(self):
        """Mock LLM provider interface"""
        mock = Mock(spec=ILLMProvider)
        mock.generate = AsyncMock(return_value="Test LLM response")
        return mock

    @pytest.fixture
    def mock_tools(self):
        """Mock tools list with proper interface implementation"""
        tool = Mock(spec=BaseTool)
        tool.execute = AsyncMock(return_value=ToolResult(
            success=True,
            data="Tool execution result",
            error=None
        ))
        tool.get_schema = Mock(return_value={
            "name": "test_tool",
            "description": "Test tool for unit testing",
            "parameters": {"type": "object"}
        })
        return [tool]

    @pytest.fixture
    def mock_tracer(self):
        """Mock tracer interface with context manager"""
        mock = Mock(spec=ITracer)
        from contextlib import contextmanager
        
        # Track calls manually
        mock._trace_calls = []
        
        @contextmanager
        def mock_trace(operation):
            mock._trace_calls.append(operation)
            yield None
        
        mock.trace = mock_trace
        
        # Add helper method to check calls
        def assert_called_with(operation):
            assert operation in mock._trace_calls, f"Expected tracer to be called with '{operation}', but calls were: {mock._trace_calls}"
        
        mock.trace.assert_called_with = assert_called_with
        return mock

    @pytest.fixture
    def mock_sanitizer(self):
        """Mock sanitizer interface"""
        mock = Mock(spec=ISanitizer)
        def smart_sanitize(x):
            if isinstance(x, str) and "SANITIZED:" not in x:
                return f"SANITIZED: {x}"
            elif isinstance(x, list):
                return [smart_sanitize(item) if isinstance(item, str) else item for item in x]
            elif isinstance(x, dict):
                return {k: smart_sanitize(v) if isinstance(v, str) else v for k, v in x.items()}
            else:
                return x
        mock.sanitize = Mock(side_effect=smart_sanitize)
        return mock

    @pytest.fixture
    def mock_logger(self):
        """Mock logger for testing"""
        logger = Mock()
        logger.debug = Mock()
        logger.info = Mock()
        logger.error = Mock()
        logger.warning = Mock()
        return logger

    @pytest.fixture
    def agent_service(self, mock_llm_provider, mock_tools, mock_tracer, mock_sanitizer):
        """AgentService instance with mocked dependencies"""
        return AgentService(
            llm_provider=mock_llm_provider,
            tools=mock_tools,
            tracer=mock_tracer,
            sanitizer=mock_sanitizer
        )

    @pytest.fixture
    def valid_query_request(self):
        """Valid QueryRequest for testing"""
        return QueryRequest(
            query="Database connection timeout errors in production",
            session_id="test_session_123",
            context={"environment": "production", "service": "api"}
        )

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_process_query_success(
        self, agent_service, valid_query_request, mock_llm_provider, 
        mock_sanitizer, mock_tracer, mock_tools
    ):
        """Test successful query processing with proper interface interactions"""
        # Arrange
        mock_agent_result = {
            'findings': [
                {
                    'type': 'error',
                    'message': 'Database connection timeout detected',
                    'severity': 'high',
                    'confidence': 0.9,
                    'source': 'log_analysis'
                },
                {
                    'type': 'performance',
                    'message': 'Response time degradation observed',
                    'severity': 'medium',
                    'confidence': 0.7,
                    'source': 'metrics_analysis'
                }
            ],
            'recommendations': [
                'Increase connection timeout configuration',
                'Monitor database connection pool usage'
            ],
            'confidence': 0.85,
            'root_cause': 'Database connection pool exhaustion',
            'estimated_mttr': "15 minutes",
            'next_steps': ['Check database connection pool settings']
        }

        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            mock_agent_class.return_value = mock_agent

            # Act
            result = await agent_service.process_query(valid_query_request)

            # Assert - Response structure
            assert isinstance(result, TroubleshootingResponse)
            assert result.session_id == "test_session_123"
            assert len(result.findings) == 2
            assert result.confidence_score == 0.85
            assert result.status == "completed"
            assert result.root_cause == "Database connection pool exhaustion"
            assert len(result.recommendations) == 2
            assert len(result.next_steps) == 1

            # Assert - Interface interactions
            mock_sanitizer.sanitize.assert_called()
            # Note: BaseService handles tracing internally through unified logger
            mock_agent_class.assert_called_once()
            mock_agent.run.assert_called_once_with(
                query=f"SANITIZED: {valid_query_request.query}",
                session_id=valid_query_request.session_id,
                tools=mock_tools,
                context=valid_query_request.context
            )

            # Note: BaseService handles logging internally
            # No direct logger assertions needed

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_process_query_empty_query_error(self, agent_service):
        """Test error handling for empty query"""
        # Arrange
        invalid_request = QueryRequest(query="", session_id="test_session")

        # Act & Assert - BaseService wraps validation errors in RuntimeError
        with pytest.raises(RuntimeError, match="Service operation failed.*Validation failed.*Query cannot be empty"):
            await agent_service.process_query(invalid_request)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_process_query_empty_session_id_error(self, agent_service):
        """Test error handling for empty session ID"""
        # Arrange
        invalid_request = QueryRequest(query="test query", session_id="")

        # Act & Assert - BaseService wraps validation errors in RuntimeError
        with pytest.raises(RuntimeError, match="Service operation failed.*Validation failed.*Session ID cannot be empty"):
            await agent_service.process_query(invalid_request)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_process_query_whitespace_only_query_error(self, agent_service):
        """Test error handling for whitespace-only query"""
        # Arrange
        invalid_request = QueryRequest(query="   \n\t   ", session_id="test_session")

        # Act & Assert - BaseService wraps validation errors in RuntimeError
        with pytest.raises(RuntimeError, match="Service operation failed.*Validation failed.*Query cannot be empty"):
            await agent_service.process_query(invalid_request)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_process_query_agent_execution_error(
        self, agent_service, valid_query_request
    ):
        """Test error handling when agent execution fails"""
        # Arrange
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(side_effect=RuntimeError("Agent execution failed"))
            mock_agent_class.return_value = mock_agent

            # Act & Assert - BaseService wraps exceptions in RuntimeError
            with pytest.raises(RuntimeError, match="Service operation failed"):
                await agent_service.process_query(valid_query_request)

            # Note: BaseService handles error logging internally

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_process_query_handles_non_list_findings(
        self, agent_service, valid_query_request, mock_sanitizer
    ):
        """Test handling of malformed agent result with non-list findings"""
        # Arrange
        mock_agent_result = {
            'findings': "Not a list",  # Invalid format
            'recommendations': ['Test recommendation'],
            'confidence': 0.5
        }

        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            mock_agent_class.return_value = mock_agent

            # Act
            result = await agent_service.process_query(valid_query_request)

            # Assert - Should handle gracefully
            assert isinstance(result, TroubleshootingResponse)
            assert result.findings == []  # Should be empty due to invalid format
            assert len(result.recommendations) == 1

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_process_query_handles_non_dict_findings(
        self, agent_service, valid_query_request
    ):
        """Test handling of findings that are not dictionaries"""
        # Arrange
        mock_agent_result = {
            'findings': ["String finding", 123, None],  # Mixed invalid types
            'recommendations': ['Test recommendation'],
            'confidence': 0.5
        }

        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            mock_agent_class.return_value = mock_agent

            # Act
            result = await agent_service.process_query(valid_query_request)

            # Assert - Should convert non-dict findings
            assert isinstance(result, TroubleshootingResponse)
            assert len(result.findings) == 3
            
            # Check converted findings structure
            for finding in result.findings:
                assert isinstance(finding, dict)
                assert 'type' in finding
                assert 'message' in finding
                assert 'severity' in finding

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_process_query_sanitizer_interaction(
        self, agent_service, valid_query_request, mock_sanitizer
    ):
        """Test proper interaction with sanitizer interface"""
        # Arrange
        sanitized_query = "SANITIZED: Database connection timeout errors in production"
        mock_sanitizer.sanitize.return_value = sanitized_query

        mock_agent_result = {
            'findings': [{'type': 'test', 'message': 'test finding'}],
            'recommendations': ['test recommendation'],
            'confidence': 0.8
        }

        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            mock_agent_class.return_value = mock_agent

            # Act
            result = await agent_service.process_query(valid_query_request)

            # Assert - Sanitizer called for input and output
            assert mock_sanitizer.sanitize.call_count >= 3  # Query, findings, recommendations
            
            # Verify query was sanitized before passing to agent
            mock_agent.run.assert_called_once()
            call_args = mock_agent.run.call_args[1]
            assert call_args['query'] == sanitized_query

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_analyze_findings_success(
        self, agent_service, mock_tracer, mock_sanitizer
    ):
        """Test successful findings analysis"""
        # Arrange
        findings = [
            {
                'type': 'error',
                'message': 'Database error',
                'severity': 'critical',
                'confidence': 0.9
            },
            {
                'type': 'error', 
                'message': 'Another database error',
                'severity': 'high',
                'confidence': 0.8
            },
            {
                'type': 'performance',
                'message': 'Slow response time',
                'severity': 'medium',
                'confidence': 0.7
            },
            {
                'type': 'security',
                'message': 'Suspicious activity',
                'severity': 'critical',
                'confidence': 0.95
            }
        ]
        session_id = "test_session_123"

        # Act
        result = await agent_service.analyze_findings(findings, session_id)

        # Assert - Structure
        assert isinstance(result, dict)
        assert result['total_findings'] == 4
        assert result['session_id'] == f"SANITIZED: {session_id}"
        assert 'analysis_timestamp' in result
        
        # Assert - Findings by type
        assert result['findings_by_type']['error'] == 2
        assert result['findings_by_type']['performance'] == 1
        assert result['findings_by_type']['security'] == 1

        # Assert - Severity distribution
        severity_dist = result['severity_distribution']
        assert severity_dist['critical'] == 2
        assert severity_dist['high'] == 1
        assert severity_dist['medium'] == 1

        # Assert - Patterns identified
        patterns = result['patterns_identified']
        assert len(patterns) == 2  # performance_issues, security_concerns (error_clustering needs 3+ errors)
        pattern_types = [p['pattern'] for p in patterns]
        assert 'performance_issues' in pattern_types  
        assert 'security_concerns' in pattern_types

        # Assert - Critical issues
        critical_issues = result['critical_issues']
        assert len(critical_issues) == 3  # 2 critical + 1 high

        # Assert - Interface interactions
        mock_sanitizer.sanitize.assert_called()
        # Note: BaseService handles tracing and logging internally

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_analyze_findings_empty_list(self, agent_service, mock_sanitizer):
        """Test analyzing empty findings list"""
        # Arrange
        findings = []
        session_id = "test_session"

        # Act
        result = await agent_service.analyze_findings(findings, session_id)

        # Assert
        assert result['total_findings'] == 0
        assert result['findings_by_type'] == {}
        assert result['patterns_identified'] == []
        assert result['critical_issues'] == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_analyze_findings_error_handling(
        self, agent_service, mock_sanitizer
    ):
        """Test error handling in findings analysis"""
        # Arrange
        findings = [{'invalid': 'data'}]  # Invalid finding structure
        session_id = "test_session"
        
        # Mock sanitizer to raise exception
        mock_sanitizer.sanitize.side_effect = RuntimeError("Sanitization failed")

        # Act & Assert - BaseService wraps exceptions in RuntimeError
        with pytest.raises(RuntimeError, match="Service operation failed"):
            await agent_service.analyze_findings(findings, session_id)

        # Note: BaseService handles error logging internally

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_get_investigation_status_success(
        self, agent_service, mock_tracer, mock_sanitizer
    ):
        """Test successful investigation status retrieval"""
        # Arrange
        investigation_id = "inv_123"
        session_id = "session_456"

        # Act
        result = await agent_service.get_investigation_status(investigation_id, session_id)

        # Assert
        assert isinstance(result, dict)
        assert result['investigation_id'] == f"SANITIZED: {investigation_id}"
        assert result['session_id'] == f"SANITIZED: {session_id}"
        assert result['status'] == "SANITIZED: completed"
        assert result['progress'] == 100.0
        assert result['phase'] == "SANITIZED: completed"
        assert 'last_updated' in result

        # Assert - Interface interactions
        mock_sanitizer.sanitize.assert_called()  # Just verify it was called
        # Note: BaseService handles tracing internally

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_get_investigation_status_error(
        self, agent_service, mock_sanitizer
    ):
        """Test error handling in investigation status retrieval"""
        # Arrange
        investigation_id = "inv_123"
        session_id = "session_456"
        
        # Mock sanitizer to raise exception
        mock_sanitizer.sanitize.side_effect = RuntimeError("Sanitization failed")

        # Act & Assert - BaseService wraps exceptions in RuntimeError
        with pytest.raises(RuntimeError, match="Service operation failed"):
            await agent_service.get_investigation_status(investigation_id, session_id)

        # Note: BaseService handles error logging internally

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_cancel_investigation_success(
        self, agent_service, mock_tracer
    ):
        """Test successful investigation cancellation"""
        # Arrange
        investigation_id = "inv_123"
        session_id = "session_456"

        # Act
        result = await agent_service.cancel_investigation(investigation_id, session_id)

        # Assert
        assert result is True

        # Note: BaseService handles tracing and logging internally

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_cancel_investigation_error(
        self, agent_service, mock_tracer
    ):
        """Test error handling in investigation cancellation"""
        # Arrange
        investigation_id = "inv_123"
        session_id = "session_456"
        
        # This test needs to simulate internal operation failure
        # Since cancel_investigation doesn't use external dependencies that can fail,
        # we'll patch the internal method to raise an exception
        with patch.object(agent_service, '_execute_investigation_cancellation', side_effect=RuntimeError("Internal cancellation failure")):
            # Act & Assert - BaseService wraps exceptions in RuntimeError
            with pytest.raises(RuntimeError, match="Service operation failed"):
                await agent_service.cancel_investigation(investigation_id, session_id)

            # Note: BaseService handles error logging internally

    @pytest.mark.unit
    def test_group_findings_by_type(self, agent_service):
        """Test private method for grouping findings by type"""
        # Arrange
        findings = [
            {'type': 'error', 'message': 'Error 1'},
            {'type': 'error', 'message': 'Error 2'}, 
            {'type': 'warning', 'message': 'Warning 1'},
            {'type': 'info', 'message': 'Info 1'},
            {'invalid': 'finding'},  # Should be handled gracefully
            'not_a_dict'  # Should be handled gracefully
        ]

        # Act
        result = agent_service._group_findings_by_type(findings)

        # Assert
        assert len(result['error']) == 2
        assert len(result['warning']) == 1
        assert len(result['info']) == 1
        assert len(result['unknown']) == 1  # Invalid finding should be grouped as 'unknown'

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_identify_patterns(self, agent_service):
        """Test pattern identification in findings"""
        # Arrange - Create findings that should trigger pattern detection
        findings_by_type = {
            'error': [{'message': 'error'} for _ in range(5)],  # Should trigger error clustering
            'performance': [{'message': 'slow'}],  # Should trigger performance pattern
            'security': [{'message': 'suspicious'}]  # Should trigger security pattern
        }

        # Act
        patterns = await agent_service._identify_patterns(findings_by_type)

        # Assert
        assert len(patterns) == 3
        pattern_types = [p['pattern'] for p in patterns]
        assert 'error_clustering' in pattern_types
        assert 'performance_issues' in pattern_types
        assert 'security_concerns' in pattern_types

        # Check specific pattern details
        error_pattern = next(p for p in patterns if p['pattern'] == 'error_clustering')
        assert error_pattern['count'] == 5
        assert error_pattern['severity'] == 'high'

    @pytest.mark.unit
    def test_calculate_severity_distribution(self, agent_service):
        """Test severity distribution calculation"""
        # Arrange
        findings = [
            {'severity': 'critical'},
            {'severity': 'critical'},
            {'severity': 'high'},
            {'severity': 'medium'},
            {'severity': 'low'},
            {'severity': 'info'},
            {'severity': 'unknown'},  # Should not be counted
            {'no_severity': 'field'}   # Should default to info
        ]

        # Act
        distribution = agent_service._calculate_severity_distribution(findings)

        # Assert
        assert distribution['critical'] == 2
        assert distribution['high'] == 1
        assert distribution['medium'] == 1
        assert distribution['low'] == 1
        assert distribution['info'] == 2  # One explicit + one default (no_severity field)

    @pytest.mark.unit
    def test_extract_critical_issues(self, agent_service):
        """Test extraction of critical issues from findings"""
        # Arrange
        findings = [
            {
                'type': 'error',
                'message': 'Critical database failure',
                'severity': 'critical',
                'source': 'logs',
                'timestamp': '2024-01-01T12:00:00Z',
                'confidence': 0.95
            },
            {
                'type': 'security',
                'message': 'High priority security breach',
                'severity': 'high', 
                'source': 'security_scanner',
                'timestamp': '2024-01-01T12:01:00Z',
                'confidence': 0.88
            },
            {
                'type': 'info',
                'message': 'Normal operation',
                'severity': 'info',
                'source': 'monitor',
                'timestamp': '2024-01-01T12:02:00Z',
                'confidence': 0.70
            }
        ]

        # Act
        critical_issues = agent_service._extract_critical_issues(findings)

        # Assert
        assert len(critical_issues) == 2  # Only critical and high severity
        
        # Check first critical issue
        assert critical_issues[0]['type'] == 'error'
        assert critical_issues[0]['severity'] == 'critical'
        assert critical_issues[0]['confidence'] == 0.95

        # Check second critical issue
        assert critical_issues[1]['type'] == 'security'
        assert critical_issues[1]['severity'] == 'high'
        assert critical_issues[1]['confidence'] == 0.88

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.performance
    async def test_process_query_performance(
        self, agent_service, valid_query_request
    ):
        """Test query processing completes within acceptable time"""
        # Arrange
        mock_agent_result = {
            'findings': [{'type': 'info', 'message': 'test'}],
            'recommendations': ['test'],
            'confidence': 0.8
        }

        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            mock_agent_class.return_value = mock_agent

            # Act & Assert - Should complete within 5 seconds
            start_time = datetime.utcnow()
            result = await agent_service.process_query(valid_query_request)
            end_time = datetime.utcnow()
            
            processing_time = (end_time - start_time).total_seconds()
            assert processing_time < 5.0, f"Processing took {processing_time} seconds, expected < 5.0"
            assert isinstance(result, TroubleshootingResponse)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_concurrent_query_processing(
        self, agent_service, mock_llm_provider, mock_tools, mock_tracer, mock_sanitizer
    ):
        """Test handling of concurrent query processing"""
        # Arrange
        requests = [
            QueryRequest(query=f"Query {i}", session_id=f"session_{i}")
            for i in range(3)
        ]
        
        mock_agent_result = {
            'findings': [{'type': 'info', 'message': 'concurrent test'}],
            'recommendations': ['test'],
            'confidence': 0.8
        }

        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            mock_agent_class.return_value = mock_agent

            # Act - Process queries concurrently
            import asyncio
            results = await asyncio.gather(*[
                agent_service.process_query(req) for req in requests
            ])

            # Assert
            assert len(results) == 3
            for i, result in enumerate(results):
                assert isinstance(result, TroubleshootingResponse)
                assert result.session_id == f"session_{i}"

    @pytest.mark.unit
    def test_validation_request_with_none_values(self, agent_service):
        """Test request validation with None values"""
        # Test None query - Pydantic will raise ValidationError, not ValueError
        with pytest.raises(Exception):  # Could be ValidationError or ValueError
            QueryRequest(query=None, session_id="test")
            
        # Test None session_id - Pydantic will raise ValidationError, not ValueError  
        with pytest.raises(Exception):  # Could be ValidationError or ValueError
            QueryRequest(query="test", session_id=None)