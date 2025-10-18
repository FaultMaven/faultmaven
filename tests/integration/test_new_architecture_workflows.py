"""
Integration tests for new architecture workflows.

Tests coverage:
- End-to-end workflows with DI container
- Settings -> Container -> Services flow
- Error handling across architectural layers
- Interface compliance in real scenarios
- Cross-layer communication patterns
- Real-world usage scenarios
"""

import os
import tempfile
from unittest.mock import Mock, patch, MagicMock, AsyncMock, call
from typing import Dict, Any, List, Optional
import pytest
import asyncio
import logging
from datetime import datetime, timedelta

from faultmaven.container import DIContainer
from faultmaven.config.settings import get_settings, reset_settings, FaultMavenSettings
from faultmaven.infrastructure.llm.providers.registry import get_registry, reset_registry


# Import models and interfaces with fallback
try:
    from faultmaven.models.interfaces import ILLMProvider, ITracer, ISanitizer, BaseTool, IVectorStore, ISessionStore
    from faultmaven.models.interfaces_case import ICaseStore, ICaseService
    from faultmaven.models.api import QueryRequest, AgentResponse, ViewState, User, Case, ResponseType
    INTERFACES_AVAILABLE = True
except ImportError:
    # Create mock interfaces for testing
    ILLMProvider = Mock
    ITracer = Mock
    ISanitizer = Mock
    BaseTool = Mock
    IVectorStore = Mock
    ISessionStore = Mock
    ICaseStore = Mock
    ICaseService = Mock
    
    # Mock API models
    QueryRequest = Mock
    AgentResponse = Mock
    ViewState = Mock
    User = Mock
    Case = Mock
    ResponseType = Mock
    INTERFACES_AVAILABLE = False


@pytest.fixture(autouse=True)
def reset_architecture_before_test():
    """Reset all architecture components before each test."""
    # Reset container singleton
    DIContainer._instance = None
    reset_settings()
    reset_registry()
    
    # Set test environment variables
    os.environ['SKIP_SERVICE_CHECKS'] = 'true'
    os.environ['ENVIRONMENT'] = 'development'
    
    yield
    
    # Reset after test
    DIContainer._instance = None
    reset_settings()
    reset_registry()


@pytest.fixture
def integration_env():
    """Provide a comprehensive integration test environment."""
    original_env = os.environ.copy()
    
    # Set up comprehensive test environment
    test_env = {
        'SKIP_SERVICE_CHECKS': 'true',
        'ENVIRONMENT': 'development',
        'DEBUG': 'true',
        'HOST': '127.0.0.1',
        'PORT': '8000',
        
        # LLM Configuration
        'CHAT_PROVIDER': 'fireworks',
        'FIREWORKS_API_KEY': 'fw-test-key-123',
        'FIREWORKS_MODEL': 'accounts/fireworks/models/llama-v3p1-8b-instruct',
        'OPENAI_API_KEY': 'sk-openai-fallback-456',
        'LOCAL_LLM_URL': 'http://localhost:11434',
        'LOCAL_LLM_MODEL': 'llama2:7b',
        
        # Database Configuration
        'REDIS_HOST': 'localhost',
        'REDIS_PORT': '6379',
        'REDIS_DB': '1',
        'CHROMADB_URL': 'http://localhost:8000',
        
        # Security Configuration
        'CORS_ALLOW_ORIGINS': '["http://localhost:3000", "chrome-extension://*"]',
        'RATE_LIMIT_ENABLED': 'true',
        'RATE_LIMIT_REQUESTS_PER_MINUTE': '100',
        
        # Protection Configuration
        'PROTECTION_ENABLED': 'true',
        'PRESIDIO_ANALYZER_URL': 'http://localhost:5001',
        'PRESIDIO_ANONYMIZER_URL': 'http://localhost:5002',
        
        # Session Configuration
        'SESSION_TIMEOUT_MINUTES': '30',
        'SESSION_HEARTBEAT_INTERVAL_SECONDS': '25',
        
        # Observability
        'OPIK_PROJECT_NAME': 'faultmaven-integration-test',
        'TRACING_ENABLED': 'true',
        'METRICS_ENABLED': 'true',
        
        # Features
        'USE_DI_CONTAINER': 'true',
        'USE_REFACTORED_SERVICES': 'true',
        'USE_REFACTORED_API': 'true',
    }
    
    os.environ.clear()
    os.environ.update(test_env)
    
    yield test_env
    
    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def mock_infrastructure_components():
    """Mock infrastructure components for integration testing."""
    components = {}
    
    # Mock LLM Provider
    llm_provider = Mock(spec=ILLMProvider)
    llm_provider.generate = AsyncMock(return_value=Mock(
        content="This is a mock LLM response for troubleshooting your issue. Let me analyze the problem step by step.",
        model="mock-model",
        confidence=0.9,
        usage={"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150}
    ))
    llm_provider.is_available = Mock(return_value=True)
    llm_provider.get_supported_models = Mock(return_value=["mock-model"])
    components['llm_provider'] = llm_provider
    
    # Mock Sanitizer
    sanitizer = Mock(spec=ISanitizer)
    sanitizer.sanitize = AsyncMock(return_value="Sanitized user query without PII")
    sanitizer.is_sensitive = Mock(return_value=False)
    components['sanitizer'] = sanitizer
    
    # Mock Tracer
    tracer = Mock(spec=ITracer)
    tracer.start_trace = Mock(return_value=Mock(trace_id="trace-123"))
    tracer.end_trace = Mock()
    tracer.add_event = Mock()
    components['tracer'] = tracer
    
    # Mock Vector Store
    vector_store = Mock(spec=IVectorStore)
    vector_store.search = AsyncMock(return_value=[
        {
            "document": "Database connection troubleshooting guide",
            "metadata": {"source": "docs/database.md", "confidence": 0.9},
            "score": 0.95
        }
    ])
    vector_store.add_documents = AsyncMock(return_value=True)
    components['vector_store'] = vector_store
    
    # Mock Session Store
    session_store = Mock(spec=ISessionStore)
    session_store.get_session = AsyncMock(return_value=Mock(
        session_id="test-session-123",
        user_id="test-user-456",
        created_at=datetime.utcnow(),
        last_activity=datetime.utcnow(),
        data={}
    ))
    session_store.create_session = AsyncMock(return_value="test-session-123")
    session_store.update_session = AsyncMock(return_value=True)
    session_store.delete_session = AsyncMock(return_value=True)
    components['session_store'] = session_store
    
    # Mock Case Store
    case_store = Mock(spec=ICaseStore)
    case_store.create_case = AsyncMock(return_value=True)
    case_store.get_case = AsyncMock(return_value=Mock(
        case_id="test-case-789",
        title="Database Connection Issue",
        status="active",
        created_at=datetime.utcnow(),
        messages=[]
    ))
    case_store.update_case = AsyncMock(return_value=True)
    case_store.add_message_to_case = AsyncMock(return_value=True)
    components['case_store'] = case_store
    
    # Mock Tools
    knowledge_tool = Mock(spec=BaseTool)
    knowledge_tool.execute = AsyncMock(return_value="Found relevant documentation about database connection issues")
    knowledge_tool.get_schema.return_value = {
        "name": "knowledge_base",
        "description": "Search knowledge base for relevant information",
        "parameters": {
            "query": {"type": "string", "description": "Search query"}
        }
    }
    
    web_search_tool = Mock(spec=BaseTool)
    web_search_tool.execute = AsyncMock(return_value="Found online resources about similar issues")
    web_search_tool.get_schema.return_value = {
        "name": "web_search",
        "description": "Search the web for additional information",
        "parameters": {
            "query": {"type": "string", "description": "Search query"}
        }
    }
    
    components['tools'] = [knowledge_tool, web_search_tool]
    
    return components


class TestSettingsContainerServicesFlow:
    """Test the Settings -> Container -> Services flow."""
    
    def test_settings_to_container_initialization(self, integration_env):
        """Test settings properly initialize the container."""
        # Reset settings cache to ensure we get fresh settings from integration_env
        reset_settings()

        # Get settings (should initialize from environment)
        settings = get_settings()
        
        # Verify settings loaded correctly
        assert settings.server.environment.value == 'development'
        assert settings.server.debug == True
        assert settings.llm.provider.value == 'fireworks'
        assert settings.llm.fireworks_api_key.get_secret_value() == 'fw-test-key-123'
        assert settings.database.redis_host == 'localhost'
        assert settings.session.timeout_minutes == 30
        assert settings.features.use_di_container == True
        
        # Initialize container with these settings
        container = DIContainer()

        with patch('faultmaven.infrastructure.llm.router.LLMRouter', Mock(return_value=Mock())), \
             patch('faultmaven.infrastructure.security.redaction.DataSanitizer', Mock(return_value=Mock())), \
             patch('faultmaven.infrastructure.observability.tracing.OpikTracer', Mock(return_value=Mock())):
            container.initialize()
        
        # Verify container initialized with correct settings
        assert container.settings is not None
        assert container.settings.server.environment.value == 'development'
        assert container._initialized == True
    
    def test_container_to_services_dependency_injection(self, integration_env, mock_infrastructure_components):
        """Test container properly injects dependencies into services."""
        container = DIContainer()
        
        # Mock service classes to track dependency injection
        agent_service_calls = []
        data_service_calls = []
        
        def mock_agent_service(*args, **kwargs):
            agent_service_calls.append({"args": args, "kwargs": kwargs})
            service = Mock()
            service.process_query = AsyncMock(return_value=Mock(
                content="Agent response",
                response_type="ANSWER",
                view_state=Mock()
            ))
            return service
        
        def mock_data_service(*args, **kwargs):
            data_service_calls.append({"args": args, "kwargs": kwargs})
            return Mock()
        
        with patch('faultmaven.infrastructure.llm.router.LLMRouter', Mock(return_value=mock_infrastructure_components['llm_provider'])), \
             patch('faultmaven.infrastructure.security.redaction.DataSanitizer', Mock(return_value=mock_infrastructure_components['sanitizer'])), \
             patch('faultmaven.infrastructure.observability.tracing.OpikTracer', Mock(return_value=mock_infrastructure_components['tracer'])), \
             patch('faultmaven.services.agentic.orchestration.agent_service.AgentService', mock_agent_service), \
             patch('faultmaven.services.data_service.DataService', mock_data_service):
            container.initialize()
        
        # Verify services were called with dependencies
        assert len(agent_service_calls) > 0
        agent_call = agent_service_calls[0]
        
        # AgentService should receive LLM provider, sanitizer, tracer, and tools
        assert len(agent_call["kwargs"]) > 0 or len(agent_call["args"]) > 0
        
        # Test service retrieval
        agent_service = container.get_agent_service()
        assert agent_service is not None
    
    def test_settings_changes_affect_container_initialization(self, integration_env):
        """Test that settings changes affect container initialization."""
        # Initialize with first settings
        container1 = DIContainer()
        
        with patch('faultmaven.infrastructure.llm.router.LLMRouter') as mock_llm_router:
            container1.initialize()

            # Check settings were used
            assert container1.settings.llm.provider.value == 'fireworks'
        
        # Reset and change settings
        DIContainer._instance = None
        reset_settings()
        
        # Change environment
        os.environ['CHAT_PROVIDER'] = 'openai'
        os.environ['OPENAI_MODEL'] = 'gpt-4-turbo'
        
        # Initialize new container
        container2 = DIContainer()
        
        with patch('faultmaven.infrastructure.llm.router.LLMRouter'):
            container2.initialize()

            # Should reflect new settings
            assert container2.settings.llm.provider.value == 'openai'
            assert container1 is not container2
    
    def test_configuration_validation_across_layers(self, integration_env):
        """Test configuration validation across architectural layers."""
        # Test invalid configuration
        os.environ['SESSION_TIMEOUT_MINUTES'] = '2'  # Too short
        os.environ['SESSION_HEARTBEAT_INTERVAL_SECONDS'] = '150'  # Greater than timeout
        
        # Settings validation should catch this
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as exc_info:
            get_settings()
        
        assert "Heartbeat interval" in str(exc_info.value)
    
    def test_feature_flags_control_container_behavior(self, integration_env):
        """Test that feature flags control container behavior."""
        # Test with DI container disabled
        os.environ['USE_DI_CONTAINER'] = 'false'
        
        settings = get_settings()
        assert settings.features.use_di_container == False
        
        # Test with refactored services disabled
        os.environ['USE_REFACTORED_SERVICES'] = 'false'
        reset_settings()
        
        settings = get_settings()
        assert settings.features.use_refactored_services == False


class TestEndToEndWorkflows:
    """Test end-to-end workflows with full architecture integration."""
    
    @pytest.mark.asyncio
    async def test_complete_troubleshooting_workflow(self, integration_env, mock_infrastructure_components):
        """Test a complete troubleshooting workflow from query to response."""
        # Set up container with all components
        container = DIContainer()
        
        # Mock complete service chain
        agent_service = Mock()
        session_service = Mock()
        case_service = Mock()
        
        # Configure agent service behavior
        mock_response = Mock()
        mock_response.content = "I've analyzed your database connection issue. Here's what I found..."
        mock_response.response_type = ResponseType.ANSWER if INTERFACES_AVAILABLE else "ANSWER"
        mock_response.view_state = Mock(
            session_id="test-session-123",
            user=Mock(user_id="test-user", email="test@example.com", name="Test User"),
            active_case=Mock(case_id="test-case-789", title="Database Connection Issue"),
            messages=[],
            uploaded_data=[]
        )
        mock_response.sources = []
        agent_service.process_query = AsyncMock(return_value=mock_response)
        
        # Configure session service
        session_service.get_or_create_session = AsyncMock(return_value="test-session-123")
        session_service.update_session_activity = AsyncMock(return_value=True)
        
        # Configure case service
        case_service.get_or_create_case_for_session = AsyncMock(return_value="test-case-789")
        case_service.add_message_to_case = AsyncMock(return_value=True)
        
        with patch.multiple(
            'faultmaven.container',
            # Infrastructure
            LLMRouter=Mock(return_value=mock_infrastructure_components['llm_provider']),
            DataSanitizer=Mock(return_value=mock_infrastructure_components['sanitizer']),
            OpikTracer=Mock(return_value=mock_infrastructure_components['tracer']),
            ChromaDBStore=Mock(return_value=mock_infrastructure_components['vector_store']),
            RedisSessionStore=Mock(return_value=mock_infrastructure_components['session_store']),
            RedisCaseStore=Mock(return_value=mock_infrastructure_components['case_store']),
            KnowledgeBaseTool=Mock(return_value=mock_infrastructure_components['tools'][0]),
            WebSearchTool=Mock(return_value=mock_infrastructure_components['tools'][1]),
            # Services
            AgentService=Mock(return_value=agent_service),
            SessionService=Mock(return_value=session_service),
            CaseService=Mock(return_value=case_service),
            DataService=Mock(return_value=Mock()),
            KnowledgeService=Mock(return_value=Mock())
        ):
            container.initialize()
        
        # Simulate complete workflow
        # 1. Get services from container
        agent = container.get_agent_service()
        session = container.get_session_service()
        case = container.get_case_service()
        
        # 2. Create session
        session_id = await session.get_or_create_session("test-user")
        assert session_id == "test-session-123"
        
        # 3. Create/get case
        case_id = await case.get_or_create_case_for_session(session_id, "Database Connection Issue")
        assert case_id == "test-case-789"
        
        # 4. Process query through agent
        query = "My application can't connect to the database"
        response = await agent.process_query(query, session_id, case_id)
        
        # 5. Verify response
        assert response is not None
        assert "database connection issue" in response.content.lower()
        assert response.view_state.session_id == "test-session-123"
        
        # 6. Verify cross-service interactions
        session.update_session_activity.assert_called()
        case.add_message_to_case.assert_called()
    
    @pytest.mark.asyncio
    async def test_data_upload_and_analysis_workflow(self, integration_env, mock_infrastructure_components):
        """Test data upload and analysis workflow."""
        container = DIContainer()
        
        # Mock data service with analysis capabilities
        data_service = Mock()
        data_service.process_uploaded_data = AsyncMock(return_value=Mock(
            data_id="upload-123",
            classification="log_file",
            summary="Database connection errors detected",
            insights={"error_count": 15, "patterns": ["connection timeout", "authentication failed"]}
        ))
        
        # Mock agent service to use analyzed data
        agent_service = Mock()
        agent_service.process_query = AsyncMock(return_value=Mock(
            content="Based on your uploaded logs, I can see connection timeout issues...",
            response_type="ANSWER",
            view_state=Mock(uploaded_data=[
                Mock(id="upload-123", name="app.log", type="log_file", processing_status="completed")
            ])
        ))
        
        with patch.multiple(
            'faultmaven.container',
            # Infrastructure
            LLMRouter=Mock(return_value=mock_infrastructure_components['llm_provider']),
            DataSanitizer=Mock(return_value=mock_infrastructure_components['sanitizer']),
            OpikTracer=Mock(return_value=mock_infrastructure_components['tracer']),
            ChromaDBStore=Mock(return_value=mock_infrastructure_components['vector_store']),
            RedisSessionStore=Mock(return_value=mock_infrastructure_components['session_store']),
            RedisCaseStore=Mock(return_value=mock_infrastructure_components['case_store']),
            KnowledgeBaseTool=Mock(return_value=mock_infrastructure_components['tools'][0]),
            # Services
            DataService=Mock(return_value=data_service),
            AgentService=Mock(return_value=agent_service),
            SessionService=Mock(return_value=Mock()),
            CaseService=Mock(return_value=Mock()),
            KnowledgeService=Mock(return_value=Mock())
        ):
            container.initialize()
        
        # Simulate data workflow
        data = container.get_data_service()
        agent = container.get_agent_service()
        
        # 1. Upload and process data
        file_content = "2024-01-01 12:00:00 ERROR Database connection timeout"
        upload_result = await data.process_uploaded_data(
            file_content, "app.log", "test-session-123", "test-case-789"
        )
        
        # 2. Query with context of uploaded data
        response = await agent.process_query(
            "What's causing the connection issues?",
            "test-session-123",
            "test-case-789"
        )
        
        # 3. Verify data was processed and used
        assert upload_result.classification == "log_file"
        assert "connection timeout" in response.content.lower()
        assert len(response.view_state.uploaded_data) > 0
    
    @pytest.mark.asyncio
    async def test_knowledge_base_integration_workflow(self, integration_env, mock_infrastructure_components):
        """Test knowledge base integration workflow."""
        container = DIContainer()
        
        # Mock knowledge service
        knowledge_service = Mock()
        knowledge_service.search_documents = AsyncMock(return_value=[
            {
                "content": "Database Connection Troubleshooting Guide",
                "metadata": {"source": "kb/database-troubleshooting.md"},
                "relevance": 0.95
            }
        ])
        knowledge_service.ingest_document = AsyncMock(return_value="doc-123")
        
        # Mock agent service to use knowledge base
        agent_service = Mock()
        agent_service.process_query = AsyncMock(return_value=Mock(
            content="Based on our knowledge base, here are the troubleshooting steps...",
            sources=[
                Mock(type="knowledge_base", content="Database troubleshooting guide", confidence=0.95)
            ]
        ))
        
        with patch.multiple(
            'faultmaven.container',
            # Infrastructure
            LLMRouter=Mock(return_value=mock_infrastructure_components['llm_provider']),
            DataSanitizer=Mock(return_value=mock_infrastructure_components['sanitizer']),
            OpikTracer=Mock(return_value=mock_infrastructure_components['tracer']),
            ChromaDBStore=Mock(return_value=mock_infrastructure_components['vector_store']),
            RedisSessionStore=Mock(return_value=mock_infrastructure_components['session_store']),
            KnowledgeBaseTool=Mock(return_value=mock_infrastructure_components['tools'][0]),
            # Services
            KnowledgeService=Mock(return_value=knowledge_service),
            AgentService=Mock(return_value=agent_service),
            SessionService=Mock(return_value=Mock()),
            CaseService=Mock(return_value=Mock()),
            DataService=Mock(return_value=Mock())
        ):
            container.initialize()
        
        # Simulate knowledge workflow
        knowledge = container.get_knowledge_service()
        agent = container.get_agent_service()
        
        # 1. Search knowledge base
        results = await knowledge.search_documents("database connection issues")
        assert len(results) > 0
        assert results[0]["relevance"] > 0.9
        
        # 2. Query agent (should use knowledge base)
        response = await agent.process_query(
            "How do I fix database connection issues?",
            "test-session-123",
            "test-case-789"
        )
        
        # 3. Verify knowledge was used
        assert "knowledge base" in response.content.lower()
        assert len(response.sources) > 0
    
    @pytest.mark.asyncio
    async def test_multi_session_case_continuity_workflow(self, integration_env, mock_infrastructure_components):
        """Test case continuity across multiple sessions."""
        container = DIContainer()
        
        # Mock session and case services
        session_service = Mock()
        case_service = Mock()
        
        # Session 1
        session_service.get_or_create_session = AsyncMock(side_effect=["session-1", "session-2"])
        
        # Case service maintains case continuity
        case_service.get_or_create_case_for_session = AsyncMock(return_value="case-123")
        case_service.get_case_conversation_context = AsyncMock(return_value="Previous context: User reported DB issues")
        case_service.add_message_to_case = AsyncMock(return_value=True)
        
        # Agent service uses case context
        agent_service = Mock()
        agent_service.process_query = AsyncMock(side_effect=[
            Mock(content="I'll help you with the database issue. Can you provide logs?"),
            Mock(content="Thanks for the logs. Continuing from our previous conversation...")
        ])
        
        with patch.multiple(
            'faultmaven.container',
            # Infrastructure
            LLMRouter=Mock(return_value=mock_infrastructure_components['llm_provider']),
            DataSanitizer=Mock(return_value=mock_infrastructure_components['sanitizer']),
            RedisSessionStore=Mock(return_value=mock_infrastructure_components['session_store']),
            RedisCaseStore=Mock(return_value=mock_infrastructure_components['case_store']),
            # Services
            SessionService=Mock(return_value=session_service),
            CaseService=Mock(return_value=case_service),
            AgentService=Mock(return_value=agent_service),
            DataService=Mock(return_value=Mock()),
            KnowledgeService=Mock(return_value=Mock())
        ):
            container.initialize()
        
        # Get services
        session = container.get_session_service()
        case = container.get_case_service()
        agent = container.get_agent_service()
        
        # Session 1: Initial query
        session_id_1 = await session.get_or_create_session("user-123")
        case_id = await case.get_or_create_case_for_session(session_id_1, "Database Issues")
        response_1 = await agent.process_query("Database won't connect", session_id_1, case_id)
        
        # Session 2: Continuation
        session_id_2 = await session.get_or_create_session("user-123")
        # Same case should be used
        case_id_2 = await case.get_or_create_case_for_session(session_id_2, "Database Issues")
        
        # Get context from previous session
        context = await case.get_case_conversation_context(case_id_2)
        assert "Previous context" in context
        
        # Continue conversation
        response_2 = await agent.process_query("Here are the logs you requested", session_id_2, case_id_2)
        assert "Continuing from our previous conversation" in response_2.content


class TestErrorHandlingAcrossLayers:
    """Test error handling and recovery across architectural layers."""
    
    def test_settings_error_propagation(self, integration_env):
        """Test error propagation from settings layer."""
        # Cause settings validation error
        os.environ['SESSION_TIMEOUT_MINUTES'] = 'invalid'
        
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            get_settings()
    
    def test_container_initialization_error_handling(self, integration_env):
        """Test container error handling during initialization."""
        container = DIContainer()
        
        # Mock infrastructure creation to fail
        with patch.object(container, '_create_infrastructure_layer', side_effect=Exception("Infrastructure failed")):
            with pytest.raises(Exception) as exc_info:
                container.initialize()
            
            assert "Infrastructure failed" in str(exc_info.value)
            assert not container._initialized
            assert not container._initializing
    
    def test_service_layer_error_recovery(self, integration_env, mock_infrastructure_components):
        """Test service layer error recovery."""
        container = DIContainer()
        
        # Mock agent service to fail initially then succeed
        call_count = 0
        def failing_then_working_service(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Service initialization failed")
            
            service = Mock()
            service.process_query = AsyncMock(return_value=Mock(content="Recovery successful"))
            return service
        
        with patch.multiple(
            'faultmaven.container',
            # Infrastructure (working)
            LLMRouter=Mock(return_value=mock_infrastructure_components['llm_provider']),
            DataSanitizer=Mock(return_value=mock_infrastructure_components['sanitizer']),
            # Service (initially failing)
            AgentService=failing_then_working_service,
            SessionService=Mock(return_value=Mock()),
            CaseService=Mock(return_value=Mock()),
            DataService=Mock(return_value=Mock()),
            KnowledgeService=Mock(return_value=Mock())
        ):
            # First initialization attempt should fail
            with pytest.raises(Exception):
                container.initialize()
            
            # Reset for retry
            container._initialized = False
            container._initializing = False
            
            # Second attempt should succeed
            container.initialize()
            assert container._initialized
    
    @pytest.mark.asyncio
    async def test_cross_service_error_handling(self, integration_env, mock_infrastructure_components):
        """Test error handling between services."""
        container = DIContainer()
        
        # Mock services with different failure modes
        session_service = Mock()
        session_service.get_or_create_session = AsyncMock(side_effect=Exception("Session creation failed"))
        
        agent_service = Mock()
        agent_service.process_query = AsyncMock(return_value=Mock(
            content="Handled gracefully despite session error",
            response_type="ERROR"
        ))
        
        with patch.multiple(
            'faultmaven.container',
            # Infrastructure
            LLMRouter=Mock(return_value=mock_infrastructure_components['llm_provider']),
            DataSanitizer=Mock(return_value=mock_infrastructure_components['sanitizer']),
            # Services
            SessionService=Mock(return_value=session_service),
            AgentService=Mock(return_value=agent_service),
            CaseService=Mock(return_value=Mock()),
            DataService=Mock(return_value=Mock()),
            KnowledgeService=Mock(return_value=Mock())
        ):
            container.initialize()
        
        # Test cross-service error handling
        session = container.get_session_service()
        agent = container.get_agent_service()
        
        # Session creation fails
        with pytest.raises(Exception):
            await session.get_or_create_session("user-123")
        
        # Agent should still work independently
        response = await agent.process_query("test query", None, None)
        assert response.response_type == "ERROR"
    
    def test_infrastructure_dependency_failure_handling(self, integration_env):
        """Test handling of infrastructure dependency failures."""
        container = DIContainer()
        
        # Mock some infrastructure to fail, others to succeed
        def failing_llm_router(*args, **kwargs):
            raise Exception("LLM Router connection failed")
        
        working_sanitizer = Mock()
        working_sanitizer.sanitize = AsyncMock(return_value="sanitized")
        
        with patch.multiple(
            'faultmaven.container',
            LLMRouter=failing_llm_router,
            DataSanitizer=Mock(return_value=working_sanitizer),
            # Mock other components to succeed
            OpikTracer=Mock(return_value=Mock()),
            ChromaDBStore=Mock(return_value=Mock()),
            RedisSessionStore=Mock(return_value=Mock()),
            AgentService=Mock(return_value=Mock()),
            SessionService=Mock(return_value=Mock()),
            CaseService=Mock(return_value=Mock()),
            DataService=Mock(return_value=Mock()),
            KnowledgeService=Mock(return_value=Mock())
        ):
            # Initialization should handle partial failures
            try:
                container.initialize()
                
                # Some services might still be available
                sanitizer = container.get_sanitizer()
                assert sanitizer is not None
                
            except Exception as e:
                # Or the entire initialization might fail gracefully
                assert "LLM Router connection failed" in str(e)


class TestInterfaceComplianceInRealScenarios:
    """Test interface compliance in real usage scenarios."""
    
    @patch('faultmaven.container.INTERFACES_AVAILABLE', True)
    def test_interface_contract_enforcement(self, integration_env, mock_infrastructure_components):
        """Test that interface contracts are enforced."""
        container = DIContainer()
        
        # Mock infrastructure with proper interface compliance
        llm_provider = Mock(spec=ILLMProvider)
        llm_provider.generate = AsyncMock()
        llm_provider.is_available = Mock(return_value=True)
        
        sanitizer = Mock(spec=ISanitizer)
        sanitizer.sanitize = AsyncMock()
        
        tracer = Mock(spec=ITracer)
        tracer.start_trace = Mock()
        
        with patch.multiple(
            'faultmaven.container',
            LLMRouter=Mock(return_value=llm_provider),
            DataSanitizer=Mock(return_value=sanitizer),
            OpikTracer=Mock(return_value=tracer),
            ChromaDBStore=Mock(return_value=Mock(spec=IVectorStore)),
            RedisSessionStore=Mock(return_value=Mock(spec=ISessionStore)),
            AgentService=Mock(return_value=Mock()),
            SessionService=Mock(return_value=Mock()),
            CaseService=Mock(return_value=Mock()),
            DataService=Mock(return_value=Mock()),
            KnowledgeService=Mock(return_value=Mock())
        ):
            container.initialize()
        
        # Verify interfaces are properly implemented
        retrieved_llm = container.get_llm_provider()
        assert hasattr(retrieved_llm, 'generate')
        assert hasattr(retrieved_llm, 'is_available')
        
        retrieved_sanitizer = container.get_sanitizer()
        assert hasattr(retrieved_sanitizer, 'sanitize')
    
    @pytest.mark.asyncio
    async def test_async_interface_compliance(self, integration_env, mock_infrastructure_components):
        """Test async interface compliance across services."""
        container = DIContainer()
        
        # Ensure all async methods are properly implemented
        async_methods_called = []
        
        def track_async_call(method_name):
            async def async_method(*args, **kwargs):
                async_methods_called.append(method_name)
                return Mock()
            return async_method
        
        # Mock with proper async interfaces
        llm_provider = Mock(spec=ILLMProvider)
        llm_provider.generate = track_async_call('llm_generate')
        
        sanitizer = Mock(spec=ISanitizer)
        sanitizer.sanitize = track_async_call('sanitizer_sanitize')
        
        session_store = Mock(spec=ISessionStore)
        session_store.get_session = track_async_call('session_get')
        session_store.create_session = track_async_call('session_create')
        
        with patch.multiple(
            'faultmaven.container',
            LLMRouter=Mock(return_value=llm_provider),
            DataSanitizer=Mock(return_value=sanitizer),
            RedisSessionStore=Mock(return_value=session_store),
            OpikTracer=Mock(return_value=Mock()),
            ChromaDBStore=Mock(return_value=Mock()),
            AgentService=Mock(return_value=Mock()),
            SessionService=Mock(return_value=Mock()),
            CaseService=Mock(return_value=Mock()),
            DataService=Mock(return_value=Mock()),
            KnowledgeService=Mock(return_value=Mock())
        ):
            container.initialize()
        
        # Test async interface compliance
        llm = container.get_llm_provider()
        sanitizer_service = container.get_sanitizer()
        session_store_service = container.get_session_store()
        
        # All should be awaitable
        await llm.generate("test prompt")
        await sanitizer_service.sanitize("test data")
        await session_store_service.get_session("test-session")
        await session_store_service.create_session("test-user")
        
        # Verify all async methods were called
        assert 'llm_generate' in async_methods_called
        assert 'sanitizer_sanitize' in async_methods_called
        assert 'session_get' in async_methods_called
        assert 'session_create' in async_methods_called
    
    def test_interface_mock_compatibility(self, integration_env):
        """Test that mock implementations are compatible with interfaces."""
        container = DIContainer()
        
        # Create mock implementations that satisfy interfaces
        if INTERFACES_AVAILABLE:
            mock_llm = Mock(spec=ILLMProvider)
            mock_sanitizer = Mock(spec=ISanitizer)
            mock_tracer = Mock(spec=ITracer)
            
            # Verify mocks have required interface methods
            assert hasattr(mock_llm, 'generate')
            assert hasattr(mock_llm, 'is_available')
            assert hasattr(mock_sanitizer, 'sanitize')
            assert hasattr(mock_tracer, 'start_trace')
        else:
            # In test environments without interfaces, mocks should still work
            mock_llm = Mock()
            mock_sanitizer = Mock()
            mock_tracer = Mock()
        
        # Inject mocks
        container._llm_provider = mock_llm
        container._sanitizer = mock_sanitizer
        container._tracer = mock_tracer
        
        # Verify mocks are retrievable and usable
        assert container.get_llm_provider() is mock_llm
        assert container.get_sanitizer() is mock_sanitizer
        assert container.get_tracer() is mock_tracer


class TestCrossLayerCommunicationPatterns:
    """Test communication patterns across architectural layers."""
    
    @pytest.mark.asyncio
    async def test_request_response_pattern(self, integration_env, mock_infrastructure_components):
        """Test request-response pattern across layers."""
        container = DIContainer()
        
        # Track calls across layers
        layer_calls = []
        
        def track_layer_call(layer_name, component_name):
            def wrapper(*args, **kwargs):
                layer_calls.append(f"{layer_name}:{component_name}")
                return Mock() if not asyncio.iscoroutinefunction(lambda: None) else AsyncMock()
            return wrapper
        
        # Mock components to track layer interactions
        with patch.multiple(
            'faultmaven.container',
            # Infrastructure layer
            LLMRouter=track_layer_call('infrastructure', 'llm_router'),
            DataSanitizer=track_layer_call('infrastructure', 'sanitizer'),
            OpikTracer=track_layer_call('infrastructure', 'tracer'),
            # Service layer
            AgentService=track_layer_call('service', 'agent'),
            SessionService=track_layer_call('service', 'session'),
            CaseService=track_layer_call('service', 'case')
        ):
            container.initialize()
        
        # Verify layers were created in correct order
        assert 'infrastructure:llm_router' in layer_calls
        assert 'infrastructure:sanitizer' in layer_calls
        assert 'service:agent' in layer_calls
        
        # Infrastructure should be created before services
        llm_index = layer_calls.index('infrastructure:llm_router')
        agent_index = layer_calls.index('service:agent')
        assert llm_index < agent_index
    
    @pytest.mark.asyncio
    async def test_event_driven_communication(self, integration_env, mock_infrastructure_components):
        """Test event-driven communication patterns."""
        container = DIContainer()
        
        # Mock event tracking
        events_fired = []
        
        def mock_tracer_with_events():
            tracer = Mock()
            tracer.start_trace = Mock(side_effect=lambda name: events_fired.append(f"trace_start:{name}"))
            tracer.end_trace = Mock(side_effect=lambda trace: events_fired.append("trace_end"))
            tracer.add_event = Mock(side_effect=lambda event: events_fired.append(f"event:{event}"))
            return tracer
        
        with patch.multiple(
            'faultmaven.container',
            LLMRouter=Mock(return_value=mock_infrastructure_components['llm_provider']),
            DataSanitizer=Mock(return_value=mock_infrastructure_components['sanitizer']),
            OpikTracer=mock_tracer_with_events,
            AgentService=Mock(return_value=Mock()),
            SessionService=Mock(return_value=Mock()),
            CaseService=Mock(return_value=Mock()),
            DataService=Mock(return_value=Mock()),
            KnowledgeService=Mock(return_value=Mock())
        ):
            container.initialize()
        
        # Test event propagation through tracer
        tracer = container.get_tracer()
        
        tracer.start_trace("test_operation")
        tracer.add_event("operation_started")
        tracer.end_trace(None)
        
        # Verify events were fired
        assert "trace_start:test_operation" in events_fired
        assert "event:operation_started" in events_fired
        assert "trace_end" in events_fired
    
    def test_dependency_chain_communication(self, integration_env, mock_infrastructure_components):
        """Test communication through dependency chains."""
        container = DIContainer()
        
        # Track dependency usage
        dependency_usage = []
        
        def mock_agent_service_with_dependency_tracking(**kwargs):
            # Track what dependencies were injected
            for key, value in kwargs.items():
                dependency_usage.append(key)
            
            service = Mock()
            service.dependencies = kwargs
            return service
        
        with patch.multiple(
            'faultmaven.container',
            # Infrastructure
            LLMRouter=Mock(return_value=mock_infrastructure_components['llm_provider']),
            DataSanitizer=Mock(return_value=mock_infrastructure_components['sanitizer']),
            OpikTracer=Mock(return_value=mock_infrastructure_components['tracer']),
            KnowledgeBaseTool=Mock(return_value=mock_infrastructure_components['tools'][0]),
            # Services
            AgentService=mock_agent_service_with_dependency_tracking,
            SessionService=Mock(return_value=Mock()),
            CaseService=Mock(return_value=Mock()),
            DataService=Mock(return_value=Mock()),
            KnowledgeService=Mock(return_value=Mock())
        ):
            container.initialize()
        
        # Verify agent service received dependencies
        agent_service = container.get_agent_service()
        
        # Should have received infrastructure dependencies
        assert hasattr(agent_service, 'dependencies')
        # The specific dependency names depend on the implementation


if __name__ == "__main__":
    pytest.main([__file__, "-v"])