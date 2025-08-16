"""Service Integration Tests - Phase 2: Service Layer Rebuild

This test module demonstrates service-to-service integration testing
following minimal mocking principles established in the rebuild.

Key Features:
- Real service orchestration and cross-service workflows
- Minimal external boundary mocking only
- Performance validation of integrated workflows  
- Real error propagation and handling testing
- End-to-end troubleshooting scenario validation

Architecture:
- Agent Service + Data Service integration workflows
- Real business logic coordination between services
- Actual data flow and transformation validation
- Performance characteristics of integrated operations
"""

import pytest
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, patch, AsyncMock

from faultmaven.services.agent_service import AgentService
from faultmaven.services.data_service import DataService
from faultmaven.models import QueryRequest, AgentResponse, UploadedData, DataType
from faultmaven.exceptions import ValidationException

# Import test doubles from the service test modules
from tests.services.test_agent_service import MockLLMProvider, MockTool, MockSanitizer, MockTracer
from tests.services.test_data_service import MockDataClassifier, MockLogProcessor, MockStorageBackend


class TestServiceIntegrationWorkflows:
    """Test integrated workflows between services with real business logic"""
    
    @pytest.fixture
    def test_llm_provider(self):
        """Shared LLM provider for integration tests"""
        return MockLLMProvider()
    
    @pytest.fixture
    def test_tools(self):
        """Shared tools for agent service"""
        return [
            MockTool("log_analyzer", "Analyzed logs: found 5 errors, 12 warnings"),
            MockTool("metrics_collector", "System metrics: CPU 65%, Memory 78%"),
            MockTool("knowledge_search", "Found 3 relevant solutions in knowledge base")
        ]
    
    @pytest.fixture
    def test_sanitizer(self):
        """Shared sanitizer for both services"""
        return MockSanitizer()
    
    @pytest.fixture
    def test_tracer(self):
        """Shared tracer for cross-service operations"""
        return MockTracer()
    
    @pytest.fixture
    def test_classifier(self):
        """Data classifier for data service"""
        return MockDataClassifier()
    
    @pytest.fixture
    def test_processor(self):
        """Log processor for data service"""
        return MockLogProcessor()
    
    @pytest.fixture
    def test_storage(self):
        """Storage backend for data service"""
        return MockStorageBackend()
    
    @pytest.fixture
    def agent_service(self, test_llm_provider, test_tools, test_sanitizer, test_tracer):
        """Agent service configured for integration testing"""
        return AgentService(
            llm_provider=test_llm_provider,
            tools=test_tools,
            tracer=test_tracer,
            sanitizer=test_sanitizer
        )
    
    @pytest.fixture
    def data_service(self, test_classifier, test_processor, test_sanitizer, test_tracer, test_storage):
        """Data service configured for integration testing"""
        return DataService(
            data_classifier=test_classifier,
            log_processor=test_processor,
            sanitizer=test_sanitizer,
            tracer=test_tracer,
            storage_backend=test_storage
        )
    
    @pytest.fixture
    def integrated_services(self, agent_service, data_service):
        """Both services configured for integration workflows"""
        return {
            "agent": agent_service,
            "data": data_service
        }
    
    @pytest.fixture
    def production_incident_logs(self):
        """Realistic production incident log data"""
        return """2024-01-15 14:23:15 INFO [startup] Application server starting on port 8080
2024-01-15 14:23:16 INFO [database] Database connection pool initialized: 10 connections
2024-01-15 14:24:00 WARN [database] Connection pool 70% utilized
2024-01-15 14:24:15 ERROR [database] Connection timeout: Failed to get connection from pool after 30s
2024-01-15 14:24:16 ERROR [api] Request failed: 500 Internal Server Error - Database unavailable  
2024-01-15 14:24:17 ERROR [api] Request failed: 500 Internal Server Error - Database unavailable
2024-01-15 14:24:18 ERROR [database] Connection timeout: Failed to get connection from pool after 30s
2024-01-15 14:24:19 WARN [circuit-breaker] Database circuit breaker opened - too many failures
2024-01-15 14:24:20 ERROR [api] Request failed: 503 Service Unavailable - Circuit breaker open
2024-01-15 14:24:25 INFO [monitoring] Alert triggered: High error rate detected
2024-01-15 14:24:30 ERROR [database] All connections in pool exhausted
2024-01-15 14:24:35 WARN [healthcheck] Database health check failed
2024-01-15 14:24:40 CRITICAL [system] Service degraded: Database connectivity issues"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_complete_troubleshooting_workflow(
        self, integrated_services, production_incident_logs, test_llm_provider, test_storage
    ):
        """Test complete end-to-end troubleshooting workflow"""
        agent_service = integrated_services["agent"]
        data_service = integrated_services["data"]
        session_id = "incident_session_001"
        
        # Phase 1: Data Ingestion and Analysis
        uploaded_data = await data_service.ingest_data(
            content=production_incident_logs,
            session_id=session_id,
            file_name="incident_2024-01-15.log"
        )
        
        # Validate data ingestion results (DataService returns dict for v3.1.0 compatibility)
        assert uploaded_data["data_type"] == DataType.LOG_FILE.value
        assert uploaded_data["processing_status"] == "completed"
        
        # Get detailed analysis
        data_analysis = await data_service.analyze_data(
            uploaded_data["data_id"], 
            session_id
        )
        
        # Phase 2: Troubleshooting Query Processing
        query_request = QueryRequest(
            query=f"We're experiencing database connection issues. The logs show connection timeouts and circuit breaker activation. Data ID: {uploaded_data['data_id']}",
            session_id=session_id,
            context={
                "environment": "production",
                "incident_severity": "critical",
                "data_reference": uploaded_data["data_id"],
                "log_analysis": {
                    "error_count": data_analysis.insights.get("error_count", 0),
                    "patterns": data_analysis.insights.get("patterns_found", [])
                }
            }
        )
        
        # Mock agent to return contextual response
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(return_value={
                'findings': [
                    {
                        'type': 'error',
                        'message': 'Database connection pool exhaustion detected',
                        'severity': 'critical',
                        'confidence': 0.95,
                        'source': 'log_analysis',
                        'evidence': f'Found {data_analysis.insights.get("error_count", 0)} errors in logs'
                    },
                    {
                        'type': 'performance',
                        'message': 'Circuit breaker pattern activated indicating service protection',
                        'severity': 'high',
                        'confidence': 0.88,
                        'source': 'pattern_analysis'
                    },
                    {
                        'type': 'system',
                        'message': 'Service degradation confirmed by monitoring alerts',
                        'severity': 'high',
                        'confidence': 0.92,
                        'source': 'monitoring'
                    }
                ],
                'recommendations': [
                    'Increase database connection pool size from 10 to 20 connections',
                    'Reduce connection timeout from 30s to 15s for faster failure detection',
                    'Implement connection pool monitoring and alerting',
                    'Review database server capacity and query performance'
                ],
                'confidence': 0.91,
                'root_cause': 'Database connection pool exhaustion under high load causing cascade failures',
                'estimated_mttr': '30 minutes',
                'next_steps': [
                    'Immediately increase connection pool size',
                    'Monitor connection pool utilization',
                    'Check database server resource usage',
                    'Review recent traffic patterns for load spikes'
                ]
            })
            mock_agent_class.return_value = mock_agent
            
            # Execute integrated troubleshooting
            start_time = datetime.utcnow()
            troubleshooting_response = await agent_service.process_query(query_request)
            end_time = datetime.utcnow()
            
            integration_time = (end_time - start_time).total_seconds()
        
        # Phase 3: Validate Integrated Results
        
        # Validate data analysis integration
        assert data_analysis.confidence_score > 0.7
        assert data_analysis.insights["error_count"] >= 7  # Should find multiple errors
        assert "connection_failures" in data_analysis.insights.get("patterns_found", [])
        assert "timeout_issues" in data_analysis.insights.get("patterns_found", [])
        
        # Validate troubleshooting response
        assert isinstance(troubleshooting_response, AgentResponse)
        assert troubleshooting_response.schema_version == "3.1.0"
        assert troubleshooting_response.view_state.session_id == session_id
        assert troubleshooting_response.content is not None
        assert "connection pool" in troubleshooting_response.content.lower()
        
        # Validate v3.1.0 structure
        assert troubleshooting_response.response_type is not None
        assert troubleshooting_response.view_state.case_id is not None
        assert isinstance(troubleshooting_response.sources, list)
        
        # Validate content contains relevant information
        assert "connection" in troubleshooting_response.content.lower()
        assert "timeout" in troubleshooting_response.content.lower()
        
        # Validate plan structure if it's a plan proposal
        if troubleshooting_response.response_type.value == "plan_proposal":
            assert troubleshooting_response.plan is not None
            assert len(troubleshooting_response.plan) >= 2
        
        # Validate performance characteristics
        assert integration_time < 1.0, f"Integrated workflow took {integration_time}s, expected <1.0s"
        
        # Validate cross-service data consistency
        assert troubleshooting_response.view_state.session_id == uploaded_data["session_id"]
        
        # Validate storage integration
        stored_data = await test_storage.retrieve(uploaded_data["data_id"])
        assert stored_data is not None
        assert stored_data["session_id"] == session_id
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_multi_data_source_analysis_workflow(
        self, integrated_services, test_storage
    ):
        """Test workflow with multiple data sources feeding into troubleshooting"""
        agent_service = integrated_services["agent"]
        data_service = integrated_services["data"]
        session_id = "multi_data_session"
        
        # Prepare multiple data sources
        application_logs = """2024-01-15 15:00:00 ERROR [auth] Authentication failed for user admin
2024-01-15 15:00:01 WARN [security] Multiple failed login attempts from IP 192.168.1.100
2024-01-15 15:00:02 ERROR [auth] Authentication failed for user root
2024-01-15 15:00:03 CRITICAL [security] Potential brute force attack detected"""
        
        system_metrics = """CPU Usage: 95%
Memory Usage: 89%  
Disk I/O: 1200 IOPS
Network: 450 Mbps outbound
Active Connections: 2547"""
        
        error_message = "SECURITY ALERT: Brute force attack detected - authentication failed from IP"
        
        # Ingest multiple data sources concurrently
        data_uploads = await asyncio.gather(
            data_service.ingest_data(
                content=application_logs,
                session_id=session_id,
                file_name="auth.log"
            ),
            data_service.ingest_data(
                content=system_metrics,
                session_id=session_id,
                file_name="metrics.txt"
            ),
            data_service.ingest_data(
                content=error_message,
                session_id=session_id,
                file_name="security_alert.msg"
            )
        )
        
        # Validate all uploads completed
        assert len(data_uploads) == 3
        log_data, metrics_data, error_data = data_uploads
        
        # Validate different data types were classified (DataService returns dict for v3.1.0 compatibility)
        assert log_data["data_type"] == DataType.LOG_FILE.value
        assert error_data["data_type"] == DataType.ERROR_MESSAGE.value
        
        # Analyze each data source
        analyses = await asyncio.gather(
            data_service.analyze_data(log_data["data_id"], session_id),
            data_service.analyze_data(metrics_data["data_id"], session_id),
            data_service.analyze_data(error_data["data_id"], session_id)
        )
        
        log_analysis, metrics_analysis, error_analysis = analyses
        
        # Create comprehensive troubleshooting query
        query_request = QueryRequest(
            query=f"Security incident in progress: brute force attack detected. Multiple data sources available: logs ({log_data['data_id']}), metrics ({metrics_data['data_id']}), alerts ({error_data['data_id']})",
            session_id=session_id,
            context={
                "incident_type": "security",
                "severity": "critical",
                "data_sources": {
                    "logs": {
                        "id": log_data["data_id"],
                        "error_count": log_analysis.insights.get("error_count", 0),
                        "patterns": log_analysis.insights.get("patterns_found", [])
                    },
                    "metrics": {
                        "id": metrics_data["data_id"],
                        "cpu_usage": "95%",
                        "connections": 2547
                    },
                    "alerts": {
                        "id": error_data["data_id"],
                        "alert_type": "security",
                        "severity": error_analysis.insights.get("severity", "unknown")
                    }
                }
            }
        )
        
        # Mock agent for security-focused response
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(return_value={
                'findings': [
                    {
                        'type': 'security',
                        'message': 'Brute force attack confirmed from IP 192.168.1.100',
                        'severity': 'critical',
                        'confidence': 0.98,
                        'source': 'log_analysis'
                    },
                    {
                        'type': 'performance',
                        'message': 'System under high load during attack - CPU 95%, 2547 connections',
                        'severity': 'high',
                        'confidence': 0.94,
                        'source': 'metrics_analysis'
                    },
                    {
                        'type': 'system',
                        'message': 'Authentication service overwhelmed by attack traffic',
                        'severity': 'high',
                        'confidence': 0.89,
                        'source': 'correlation_analysis'
                    }
                ],
                'recommendations': [
                    'Immediately block IP address 192.168.1.100 at firewall level',
                    'Enable rate limiting on authentication endpoints',
                    'Implement CAPTCHA for failed login attempts',
                    'Scale authentication service to handle attack load',
                    'Enable enhanced security monitoring'
                ],
                'confidence': 0.94,
                'root_cause': 'Coordinated brute force attack causing system performance degradation',
                'estimated_mttr': '15 minutes',
                'next_steps': [
                    'Block malicious IP immediately',
                    'Monitor for attack continuation from other IPs',
                    'Review authentication service scaling',
                    'Implement additional security controls'
                ]
            })
            mock_agent_class.return_value = mock_agent
            
            # Execute multi-source troubleshooting
            response = await agent_service.process_query(query_request)
        
        # Validate integrated analysis - v3.1.0 AgentResponse doesn't have confidence_score, root_cause, recommendations, findings fields
        # Check content contains expected analysis
        assert "brute force" in response.content.lower()
        
        # v3.1.0 has plan field for recommendations (if response_type is plan_proposal)
        if response.response_type.value == "plan_proposal" and response.plan:
            security_recommendations = [step for step in response.plan if "block" in step.description.lower() or "security" in step.description.lower()]
            assert len(security_recommendations) >= 1
        
        # v3.1.0 has sources field instead of findings
        assert isinstance(response.sources, list)
        
        # Validate meaningful analysis was provided
        assert len(response.content) > 50
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_error_propagation_across_services(
        self, integrated_services, test_storage
    ):
        """Test proper error handling and propagation between services"""
        agent_service = integrated_services["agent"]
        data_service = integrated_services["data"]
        session_id = "error_test_session"
        
        # Test 1: Data service error propagation
        with pytest.raises(ValidationException) as exc_info:
            await data_service.ingest_data(
                content="",  # Empty content should fail
                session_id=session_id
            )
        assert "Content cannot be empty" in str(exc_info.value)
        
        # Test 2: Agent service validation error
        invalid_request = QueryRequest(query="", session_id=session_id)
        with pytest.raises(ValidationException) as exc_info:
            await agent_service.process_query(invalid_request)
        assert "Query cannot be empty" in str(exc_info.value)
        
        # Test 3: Cross-service error handling
        # First successfully ingest data
        valid_data = await data_service.ingest_data(
            content="INFO: Test log message",
            session_id=session_id
        )
        
        # Try to access with wrong session
        wrong_session = "wrong_session_id"
        with pytest.raises(ValidationException) as exc_info:
            await data_service.analyze_data(valid_data["data_id"], wrong_session)
        assert "does not belong to session" in str(exc_info.value)
        
        # Test 4: Storage backend error simulation
        # Try to analyze non-existent data
        with pytest.raises(FileNotFoundError) as exc_info:
            await data_service.analyze_data("nonexistent_id", session_id)
        assert "Data not found" in str(exc_info.value)
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.performance
    async def test_integrated_performance_characteristics(
        self, integrated_services, production_incident_logs
    ):
        """Test performance characteristics of integrated service workflows"""
        agent_service = integrated_services["agent"]
        data_service = integrated_services["data"]
        session_id = "perf_test_session"
        
        # Test concurrent data ingestion + analysis
        data_items = [
            (f"{production_incident_logs}\nBatch item {i}", f"batch_{i}.log")
            for i in range(5)
        ]
        
        # Measure data ingestion performance
        ingestion_start = datetime.utcnow()
        upload_results = await asyncio.gather(*[
            data_service.ingest_data(content=content, session_id=session_id, file_name=filename)
            for content, filename in data_items
        ])
        ingestion_end = datetime.utcnow()
        
        ingestion_time = (ingestion_end - ingestion_start).total_seconds()
        
        # Measure analysis performance
        analysis_start = datetime.utcnow()
        analysis_results = await asyncio.gather(*[
            data_service.analyze_data(upload["data_id"], session_id)
            for upload in upload_results
        ])
        analysis_end = datetime.utcnow()
        
        analysis_time = (analysis_end - analysis_start).total_seconds()
        
        # Create troubleshooting queries based on analyses
        queries = [
            QueryRequest(
                query=f"Investigating issue based on data {upload["data_id"]}",
                session_id=session_id,
                context={
                    "data_reference": upload["data_id"],
                    "error_count": analysis.insights.get("error_count", 0)
                }
            )
            for upload, analysis in zip(upload_results, analysis_results)
        ]
        
        # Mock agent responses
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(return_value={
                'findings': [{'type': 'info', 'message': 'Analysis complete', 'severity': 'info', 'confidence': 0.8}],
                'recommendations': ['Continue monitoring'],
                'confidence': 0.8,
                'root_cause': 'Performance test execution'
            })
            mock_agent_class.return_value = mock_agent
            
            # Measure troubleshooting performance
            troubleshooting_start = datetime.utcnow()
            troubleshooting_results = await asyncio.gather(*[
                agent_service.process_query(query)
                for query in queries
            ])
            troubleshooting_end = datetime.utcnow()
        
        troubleshooting_time = (troubleshooting_end - troubleshooting_start).total_seconds()
        total_workflow_time = (troubleshooting_end - ingestion_start).total_seconds()
        
        # Validate performance characteristics
        assert ingestion_time < 1.0, f"Data ingestion took {ingestion_time}s, expected <1.0s"
        assert analysis_time < 1.5, f"Data analysis took {analysis_time}s, expected <1.5s"
        assert troubleshooting_time < 1.0, f"Troubleshooting took {troubleshooting_time}s, expected <1.0s"
        assert total_workflow_time < 3.0, f"Total workflow took {total_workflow_time}s, expected <3.0s"
        
        # Validate all operations completed successfully
        assert len(upload_results) == 5
        assert len(analysis_results) == 5
        assert len(troubleshooting_results) == 5
        
        # Validate quality wasn't sacrificed for speed
        for upload in upload_results:
            assert upload["processing_status"] == "completed"
            assert upload["data_type"] == DataType.LOG_FILE.value
        
        for analysis in analysis_results:
            assert analysis.confidence_score > 0.6
            assert analysis.processing_time_ms > 0
        
        for troubleshooting in troubleshooting_results:
            # v3.1.0 AgentResponse doesn't have confidence_score or status fields
            assert isinstance(troubleshooting, AgentResponse)
            assert troubleshooting.schema_version == "3.1.0"
            assert len(troubleshooting.content) > 0
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cross_service_sanitization_consistency(
        self, integrated_services, test_sanitizer
    ):
        """Test that sanitization is consistent across service boundaries"""
        agent_service = integrated_services["agent"]
        data_service = integrated_services["data"]
        session_id = "sanitization_test_session"
        
        # Data with PII that should be sanitized
        sensitive_logs = """2024-01-15 10:00:00 ERROR [auth] Login failed for user with password=secret123
2024-01-15 10:00:01 WARN [system] User SSN 123-45-6789 access denied
2024-01-15 10:00:02 ERROR [payment] Credit card 1234567890123456 processing failed"""
        
        # Ingest sensitive data
        uploaded_data = await data_service.ingest_data(
            content=sensitive_logs,
            session_id=session_id,
            file_name="sensitive.log"
        )
        
        # Query with sensitive information
        sensitive_query = QueryRequest(
            query=f"User with password=admin123 is having issues. Also check SSN 987-65-4321 in data {uploaded_data['data_id']}",
            session_id=session_id,
            context={
                "user_info": "Contains password=test456 and card 9999888877776666",
                "data_reference": uploaded_data["data_id"]
            }
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(return_value={
                'findings': [
                    {
                        'type': 'security',
                        'message': 'Authentication issues detected in logs',
                        'severity': 'medium',
                        'confidence': 0.85
                    }
                ],
                'recommendations': ['Review authentication logs for patterns'],
                'confidence': 0.85,
                'root_cause': 'Authentication service issues'
            })
            mock_agent_class.return_value = mock_agent
            
            # Process sensitive query
            response = await agent_service.process_query(sensitive_query)
        
        # Validate sanitization occurred
        assert test_sanitizer.call_count > 0
        
        # Check that sensitive data was processed through sanitizer
        sanitized_items = test_sanitizer.sanitized_items
        has_password_redaction = any('[REDACTED]' in item for item in sanitized_items if isinstance(item, str))
        assert has_password_redaction, "Expected password redaction in sanitized data"
        
        # Validate data service sanitization
        assert uploaded_data["processing_status"] == "completed"
        
        # Validate agent service sanitization - v3.1.0 AgentResponse doesn't have status or confidence_score
        assert isinstance(response, AgentResponse)
        assert response.schema_version == "3.1.0"
        assert len(response.content) > 0
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_data_consistency_across_services(
        self, integrated_services, test_storage
    ):
        """Test session data consistency and access control across services"""
        agent_service = integrated_services["agent"]
        data_service = integrated_services["data"]
        
        session_1 = "session_consistency_1"
        session_2 = "session_consistency_2"
        
        # Upload data to different sessions
        data_1 = await data_service.ingest_data(
            content="Session 1 log data ERROR: Test error",
            session_id=session_1,
            file_name="session1.log"
        )
        
        data_2 = await data_service.ingest_data(
            content="Session 2 log data WARN: Test warning", 
            session_id=session_2,
            file_name="session2.log"
        )
        
        # Validate session isolation in data service
        assert data_1["session_id"] == session_1
        assert data_2["session_id"] == session_2
        assert data_1["data_id"] != data_2["data_id"]
        
        # Test cross-session access prevention
        with pytest.raises(ValidationException) as exc_info:
            await data_service.analyze_data(data_1["data_id"], session_2)
        assert "does not belong to session" in str(exc_info.value)
        
        # Test valid session access
        analysis_1 = await data_service.analyze_data(data_1["data_id"], session_1)
        assert analysis_1.data_id == data_1["data_id"]
        
        # Test agent service session consistency
        query_1 = QueryRequest(
            query=f"Analyze error from data {data_1['data_id']}",
            session_id=session_1,
            context={"data_reference": data_1["data_id"]}
        )
        
        query_2 = QueryRequest(
            query=f"Analyze warning from data {data_2['data_id']}",
            session_id=session_2,
            context={"data_reference": data_2["data_id"]}
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent.run = AsyncMock(side_effect=[
                {
                    'findings': [{'type': 'error', 'message': 'Error analysis for session 1', 'severity': 'medium', 'confidence': 0.8}],
                    'recommendations': ['Fix session 1 errors'],
                    'confidence': 0.8
                },
                {
                    'findings': [{'type': 'warning', 'message': 'Warning analysis for session 2', 'severity': 'low', 'confidence': 0.7}],
                    'recommendations': ['Monitor session 2 warnings'],
                    'confidence': 0.7
                }
            ])
            mock_agent_class.return_value = mock_agent
            
            # Process queries for different sessions
            response_1 = await agent_service.process_query(query_1)
            response_2 = await agent_service.process_query(query_2)
        
        # Validate session-specific responses - v3.1.0 AgentResponse structure
        assert response_1.view_state.session_id == session_1
        assert response_2.view_state.session_id == session_2
        # v3.1.0 doesn't have findings field - check content instead
        assert "error" in response_1.content.lower()
        assert "warning" in response_2.content.lower()
        
        # Validate storage isolation
        stored_1 = await test_storage.retrieve(data_1["data_id"])
        stored_2 = await test_storage.retrieve(data_2["data_id"])
        assert stored_1["session_id"] == session_1
        assert stored_2["session_id"] == session_2