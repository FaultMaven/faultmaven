"""
Unit tests for ToolSkillBroker - dynamic capability discovery and orchestration.

This module tests the tool broker that manages agent capabilities, performs safety 
assessments, and orchestrates tool execution with performance monitoring.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
import asyncio
from datetime import datetime, timedelta

from faultmaven.services.agentic.tool_broker import ToolSkillBroker
from faultmaven.models.agentic import (
    AgentCapabilities, ToolExecutionRequest, ToolExecutionResult,
    SafetyAssessment, PerformanceMetrics, CapabilityDiscovery
)


class TestToolSkillBroker:
    """Test suite for Tool & Skill Broker."""
    
    @pytest.fixture
    def mock_knowledge_base(self):
        """Mock knowledge base for capability discovery."""
        mock = AsyncMock()
        mock.search.return_value = [
            {
                'tool_name': 'knowledge_search',
                'capabilities': ['document_retrieval', 'semantic_search'],
                'safety_rating': 'safe',
                'performance_rating': 'high'
            },
            {
                'tool_name': 'web_search',
                'capabilities': ['external_search', 'url_validation'],
                'safety_rating': 'moderate',
                'performance_rating': 'medium'
            }
        ]
        return mock

    @pytest.fixture
    def mock_health_monitor(self):
        """Mock health monitor for tool availability."""
        mock = AsyncMock()
        mock.check_tool_health.return_value = {
            'status': 'healthy',
            'response_time': 0.1,
            'success_rate': 0.95
        }
        mock.get_performance_metrics.return_value = PerformanceMetrics(
            average_response_time=0.15,
            success_rate=0.92,
            total_executions=100,
            error_count=8
        )
        return mock

    @pytest.fixture
    def tool_broker(self, mock_knowledge_base, mock_health_monitor):
        """Create tool broker with mocked dependencies."""
        return ToolSkillBroker(
            knowledge_base=mock_knowledge_base,
            health_monitor=mock_health_monitor
        )

    @pytest.mark.asyncio
    async def test_init_tool_broker(self, tool_broker):
        """Test tool broker initialization."""
        assert tool_broker.knowledge_base is not None
        assert tool_broker.health_monitor is not None
        assert hasattr(tool_broker, 'available_tools')
        assert hasattr(tool_broker, 'performance_cache')

    @pytest.mark.asyncio
    async def test_discover_capabilities_basic(self, tool_broker):
        """Test basic capability discovery."""
        query_context = "Need to search for troubleshooting information"
        
        capabilities = await tool_broker.discover_capabilities(query_context)
        
        assert isinstance(capabilities, AgentCapabilities)
        assert len(capabilities.tools) > 0
        assert 'knowledge_search' in capabilities.tools
        
        # Verify knowledge base was searched
        tool_broker.knowledge_base.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_discover_capabilities_complex_query(self, tool_broker, mock_knowledge_base):
        """Test capability discovery for complex queries."""
        # Add more comprehensive capabilities
        mock_knowledge_base.search.return_value.extend([
            {
                'tool_name': 'log_analyzer',
                'capabilities': ['log_parsing', 'anomaly_detection'],
                'safety_rating': 'safe',
                'performance_rating': 'high'
            },
            {
                'tool_name': 'system_monitor',
                'capabilities': ['resource_monitoring', 'alert_generation'],
                'safety_rating': 'safe', 
                'performance_rating': 'medium'
            }
        ])
        
        query_context = "Analyze system logs for performance issues and generate alerts"
        capabilities = await tool_broker.discover_capabilities(query_context)
        
        assert len(capabilities.tools) >= 2
        assert 'log_analyzer' in capabilities.tools
        assert len(capabilities.skills) >= 2

    @pytest.mark.asyncio
    async def test_safety_assessment_safe_tool(self, tool_broker):
        """Test safety assessment for safe tools."""
        tool_request = ToolExecutionRequest(
            tool_name='knowledge_search',
            parameters={'query': 'system performance'},
            context={'session_id': 'test-123'}
        )
        
        assessment = await tool_broker.assess_safety(tool_request)
        
        assert isinstance(assessment, SafetyAssessment)
        assert assessment.is_safe == True
        assert assessment.risk_level == 'low'

    @pytest.mark.asyncio
    async def test_safety_assessment_risky_parameters(self, tool_broker):
        """Test safety assessment with potentially risky parameters."""
        tool_request = ToolExecutionRequest(
            tool_name='web_search',
            parameters={
                'query': 'download malicious software',
                'follow_redirects': True
            },
            context={'session_id': 'test-123'}
        )
        
        assessment = await tool_broker.assess_safety(tool_request)
        
        assert assessment.is_safe == False
        assert assessment.risk_level in ['medium', 'high']
        assert len(assessment.risk_factors) > 0

    @pytest.mark.asyncio
    async def test_execute_capability_success(self, tool_broker):
        """Test successful capability execution."""
        capability_name = 'knowledge_search'
        parameters = {'query': 'troubleshooting guide', 'limit': 5}
        
        with patch.object(tool_broker, '_execute_tool') as mock_execute:
            mock_execute.return_value = ToolExecutionResult(
                success=True,
                result={'documents': ['doc1', 'doc2']},
                execution_time=0.5,
                metadata={'tokens_used': 100}
            )
            
            result = await tool_broker.execute_capability(capability_name, parameters)
            
            assert result.success == True
            assert 'documents' in result.result
            assert result.execution_time > 0

    @pytest.mark.asyncio
    async def test_execute_capability_with_safety_check(self, tool_broker):
        """Test capability execution with safety validation."""
        capability_name = 'web_search'
        parameters = {'query': 'safe search query'}
        
        with patch.object(tool_broker, 'assess_safety') as mock_safety:
            mock_safety.return_value = SafetyAssessment(
                is_safe=True,
                risk_level='low',
                risk_factors=[]
            )
            
            with patch.object(tool_broker, '_execute_tool') as mock_execute:
                mock_execute.return_value = ToolExecutionResult(
                    success=True,
                    result={'results': ['result1']},
                    execution_time=0.3,
                    metadata={}
                )
                
                result = await tool_broker.execute_capability(capability_name, parameters)
                
                # Verify safety check was performed
                mock_safety.assert_called_once()
                assert result.success == True

    @pytest.mark.asyncio
    async def test_execute_capability_blocked_by_safety(self, tool_broker):
        """Test capability execution blocked by safety assessment."""
        capability_name = 'web_search'
        parameters = {'query': 'potentially harmful query'}
        
        with patch.object(tool_broker, 'assess_safety') as mock_safety:
            mock_safety.return_value = SafetyAssessment(
                is_safe=False,
                risk_level='high',
                risk_factors=['external_content', 'unvalidated_input']
            )
            
            result = await tool_broker.execute_capability(capability_name, parameters)
            
            assert result.success == False
            assert 'safety' in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_performance_monitoring(self, tool_broker, mock_health_monitor):
        """Test performance monitoring during tool execution."""
        capability_name = 'knowledge_search'
        parameters = {'query': 'test query'}
        
        with patch.object(tool_broker, '_execute_tool') as mock_execute:
            mock_execute.return_value = ToolExecutionResult(
                success=True,
                result={'data': 'test'},
                execution_time=0.2,
                metadata={}
            )
            
            await tool_broker.execute_capability(capability_name, parameters)
            
            # Verify performance monitoring
            mock_health_monitor.check_tool_health.assert_called()

    @pytest.mark.asyncio
    async def test_adaptive_routing_based_on_performance(self, tool_broker):
        """Test adaptive routing based on tool performance."""
        # Simulate multiple tools with different performance
        with patch.object(tool_broker, 'get_tool_performance') as mock_perf:
            mock_perf.side_effect = [
                PerformanceMetrics(average_response_time=0.5, success_rate=0.8, total_executions=50, error_count=10),
                PerformanceMetrics(average_response_time=0.2, success_rate=0.95, total_executions=100, error_count=5)
            ]
            
            # Request should route to better-performing tool
            best_tool = await tool_broker.select_best_tool(['tool_a', 'tool_b'])
            
            assert best_tool == 'tool_b'  # Better performance

    @pytest.mark.asyncio
    async def test_capability_caching(self, tool_broker):
        """Test caching of capability discoveries."""
        query_context = "search for documentation"
        
        # First discovery should hit knowledge base
        capabilities1 = await tool_broker.discover_capabilities(query_context)
        
        # Second discovery should use cache
        capabilities2 = await tool_broker.discover_capabilities(query_context)
        
        assert capabilities1.tools == capabilities2.tools
        # Knowledge base should only be called once due to caching
        assert tool_broker.knowledge_base.search.call_count <= 2

    @pytest.mark.asyncio
    async def test_tool_health_monitoring(self, tool_broker, mock_health_monitor):
        """Test tool health monitoring and availability checking."""
        tool_name = 'knowledge_search'
        
        health_status = await tool_broker.check_tool_availability(tool_name)
        
        assert health_status is not None
        mock_health_monitor.check_tool_health.assert_called_with(tool_name)

    @pytest.mark.asyncio
    async def test_error_handling_tool_failure(self, tool_broker):
        """Test error handling when tool execution fails."""
        capability_name = 'failing_tool'
        parameters = {'test': 'param'}
        
        with patch.object(tool_broker, '_execute_tool') as mock_execute:
            mock_execute.side_effect = Exception("Tool execution failed")
            
            result = await tool_broker.execute_capability(capability_name, parameters)
            
            assert result.success == False
            assert 'execution failed' in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_concurrent_tool_execution(self, tool_broker):
        """Test concurrent execution of multiple tools."""
        capabilities = ['knowledge_search', 'web_search']
        parameters_list = [
            {'query': 'test1'},
            {'query': 'test2'}
        ]
        
        with patch.object(tool_broker, '_execute_tool') as mock_execute:
            mock_execute.return_value = ToolExecutionResult(
                success=True,
                result={'data': 'test'},
                execution_time=0.1,
                metadata={}
            )
            
            results = await tool_broker.execute_capabilities_concurrent(
                capabilities, parameters_list
            )
            
            assert len(results) == 2
            for result in results:
                assert result.success == True

    @pytest.mark.asyncio
    async def test_tool_discovery_filtering(self, tool_broker, mock_knowledge_base):
        """Test filtering of discovered tools based on criteria."""
        # Set up filtered search results
        mock_knowledge_base.search.return_value = [
            {
                'tool_name': 'safe_tool',
                'capabilities': ['safe_operation'],
                'safety_rating': 'safe',
                'performance_rating': 'high'
            },
            {
                'tool_name': 'risky_tool',
                'capabilities': ['risky_operation'],
                'safety_rating': 'risky',
                'performance_rating': 'high'
            }
        ]
        
        query_context = "need safe tools only"
        capabilities = await tool_broker.discover_capabilities(
            query_context, 
            safety_filter='safe_only'
        )
        
        assert 'safe_tool' in capabilities.tools
        assert 'risky_tool' not in capabilities.tools

    def test_validate_tool_parameters(self, tool_broker):
        """Test parameter validation for tool execution."""
        # Valid parameters
        valid_params = {'query': 'test', 'limit': 10}
        assert tool_broker._validate_parameters('knowledge_search', valid_params) == True
        
        # Invalid parameters (missing required)
        invalid_params = {'limit': 10}  # Missing 'query'
        assert tool_broker._validate_parameters('knowledge_search', invalid_params) == False

    def test_tool_capability_mapping(self, tool_broker):
        """Test mapping between tools and their capabilities."""
        # Test known tool capabilities
        capabilities = tool_broker.get_tool_capabilities('knowledge_search')
        
        assert isinstance(capabilities, list)
        assert len(capabilities) > 0
        assert all(isinstance(cap, str) for cap in capabilities)

    @pytest.mark.asyncio
    async def test_performance_metrics_collection(self, tool_broker):
        """Test collection of performance metrics."""
        capability_name = 'knowledge_search'
        
        # Execute multiple times to build metrics
        for _ in range(5):
            with patch.object(tool_broker, '_execute_tool') as mock_execute:
                mock_execute.return_value = ToolExecutionResult(
                    success=True,
                    result={'data': 'test'},
                    execution_time=0.1,
                    metadata={}
                )
                
                await tool_broker.execute_capability(capability_name, {'query': 'test'})
        
        # Verify metrics are collected
        metrics = await tool_broker.get_performance_metrics(capability_name)
        assert metrics is not None
        assert hasattr(metrics, 'total_executions')

    def test_tool_registration_and_deregistration(self, tool_broker):
        """Test dynamic tool registration and deregistration."""
        # Register new tool
        tool_config = {
            'name': 'new_tool',
            'capabilities': ['new_capability'],
            'safety_rating': 'safe',
            'performance_rating': 'medium'
        }
        
        tool_broker.register_tool(tool_config)
        assert 'new_tool' in tool_broker.available_tools
        
        # Deregister tool
        tool_broker.deregister_tool('new_tool')
        assert 'new_tool' not in tool_broker.available_tools