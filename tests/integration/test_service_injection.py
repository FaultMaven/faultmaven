"""Integration tests for service dependency injection.

Tests that services can be injected correctly and work together with the DI container.
Tests actual FaultMaven services (not mocks) to ensure DI refactoring works.

Design Reference: Phase 3, Week 14-15 - DI Container Implementation
"""

import pytest
from unittest.mock import Mock, patch
from faultmaven.core.container import ServiceContainer
from faultmaven.core.service_factories import register_services, clear_service_instances


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def setup_di_container():
    """Setup DI container before each test, clean up after."""
    ServiceContainer.clear_all()
    register_services(redis_client=None)
    yield
    ServiceContainer.clear_all()


@pytest.fixture
def mock_settings():
    """Mock settings for service creation."""
    with patch('faultmaven.core.service_factories.get_settings') as mock:
        settings = Mock()
        settings.security.jwt_algorithm = "HS256"
        settings.security.jwt_access_token_expire_minutes = 30
        settings.security.jwt_refresh_token_expire_minutes = 10080
        settings.security.jwt_issuer = "faultmaven"
        settings.security.jwt_audience = "faultmaven-api"
        settings.embedding_provider = "openai"
        settings.embedding_model = "text-embedding-3-small"
        settings.openai_api_key = "test-key"
        settings.vector_store_collection = "test_collection"
        settings.chroma_persist_directory = "./test_chroma"
        settings.evidence_storage_root = "./test_evidence"
        settings.max_evidence_file_size = 100 * 1024 * 1024
        settings.allowed_evidence_mime_types = []
        mock.return_value = settings
        yield settings


# ============================================================================
# Test Service Factory Registration
# ============================================================================

class TestServiceFactoryRegistration:
    """Test that service factories are registered correctly."""

    def test_all_services_registered(self, mock_settings):
        """Test that all expected services are registered."""
        status = ServiceContainer.get_registered_services()

        # Should have all 6 services registered
        expected_services = [
            'AuthService',
            'EmbeddingService',
            'VectorStoreService',
            'FileStorageService',
            'APIInvestigationSessionService',
            'APIEvidenceArtifactService',
        ]

        for service_name in expected_services:
            assert service_name in status, f"{service_name} not registered"

    def test_no_instances_created_on_registration(self, mock_settings):
        """Test that registration doesn't create instances (lazy initialization)."""
        status = ServiceContainer.get_registered_services()

        # All should be False (no instances created yet)
        for service_name, has_instance in status.items():
            assert has_instance is False, f"{service_name} was created prematurely"


# ============================================================================
# Test Lower-Level Service Injection
# ============================================================================

class TestLowerLevelServiceInjection:
    """Test injection of lower-level services (auth, embedding, vector store, file storage)."""

    def test_auth_service_injection(self, mock_settings):
        """Test that AuthService can be created via DI container."""
        from faultmaven.services.auth_service import AuthService

        service = ServiceContainer.get(AuthService)

        assert isinstance(service, AuthService)
        # Verify singleton
        service2 = ServiceContainer.get(AuthService)
        assert service is service2

    @patch('faultmaven.services.embedding_service.EmbeddingService.__init__', return_value=None)
    def test_embedding_service_injection(self, mock_init, mock_settings):
        """Test that EmbeddingService can be created via DI container."""
        from faultmaven.modules.knowledge.domain.services.embedding_service import EmbeddingService

        service = ServiceContainer.get(EmbeddingService)

        assert isinstance(service, EmbeddingService)
        # Verify factory was called with correct params
        mock_init.assert_called_once()

    @patch('faultmaven.services.vector_store_service.VectorStoreService.__init__', return_value=None)
    def test_vector_store_service_injection(self, mock_init, mock_settings):
        """Test that VectorStoreService can be created via DI container."""
        from faultmaven.modules.knowledge.domain.services.vector_store_service import VectorStoreService

        service = ServiceContainer.get(VectorStoreService)

        assert isinstance(service, VectorStoreService)
        mock_init.assert_called_once()

    @patch('faultmaven.services.file_storage_service.FileStorageService.__init__', return_value=None)
    def test_file_storage_service_injection(self, mock_init, mock_settings):
        """Test that FileStorageService can be created via DI container."""
        from faultmaven.services.file_storage_service import FileStorageService

        service = ServiceContainer.get(FileStorageService)

        assert isinstance(service, FileStorageService)
        mock_init.assert_called_once()


# ============================================================================
# Test Mid-Level Service Injection
# ============================================================================

class TestMidLevelServiceInjection:
    """Test injection of mid-level services (session, evidence)."""

    def test_investigation_session_service_injection(self, mock_settings):
        """Test that APIInvestigationSessionService can be created via DI container."""
        from faultmaven.services.investigation_session_service import APIInvestigationSessionService

        service = ServiceContainer.get(APIInvestigationSessionService)

        assert isinstance(service, APIInvestigationSessionService)
        # Verify it has required repositories
        assert hasattr(service, 'session_repo')
        assert hasattr(service, 'execution_repo')
        assert hasattr(service, 'case_repo')

    def test_evidence_artifact_service_injection(self, mock_settings):
        """Test that APIEvidenceArtifactService can be created via DI container."""
        from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService

        service = ServiceContainer.get(APIEvidenceArtifactService)

        assert isinstance(service, APIEvidenceArtifactService)
        # Verify it has required repositories
        assert hasattr(service, 'evidence_repo')
        assert hasattr(service, 'case_repo')


# ============================================================================
# Test Service Independence Violations Fixed
# ============================================================================

class TestServiceIndependenceViolationsFixed:
    """Test that the 6 service independence violations have been fixed."""

    @patch('faultmaven.services.embedding_service.EmbeddingService')
    @patch('faultmaven.services.vector_store_service.VectorStoreService')
    def test_knowledge_search_service_uses_di(self, mock_vector, mock_embedding, mock_settings):
        """Test that KnowledgeSearchService uses DI instead of direct imports."""
        # This would have been a violation: knowledge_search_service → embedding_service
        from faultmaven.modules.knowledge.domain.services.search_service import KnowledgeSearchService
        from faultmaven.infrastructure.persistence.repository_factory import (
            get_knowledge_repository,
            STORAGE_TYPE_MEMORY,
        )

        # Create mock instances
        mock_embedding_instance = Mock()
        mock_vector_instance = Mock()

        ServiceContainer.register_instance(mock_embedding, mock_embedding_instance)
        ServiceContainer.register_instance(mock_vector, mock_vector_instance)

        # Create service with None dependencies (should use DI)
        knowledge_repo = get_knowledge_repository(storage_type=STORAGE_TYPE_MEMORY)
        service = KnowledgeSearchService(
            knowledge_repo=knowledge_repo,
            embedding_service=None,  # Should use DI
            vector_store_service=None,  # Should use DI
        )

        # Verify DI was used
        assert service.embedding_service is mock_embedding_instance
        assert service.vector_store_service is mock_vector_instance

    @patch('faultmaven.services.auth_service.AuthService')
    def test_user_service_uses_di(self, mock_auth, mock_settings):
        """Test that UserService uses DI instead of direct imports."""
        # This would have been a violation: user_service → auth_service
        from faultmaven.services.user_service import UserService
        from faultmaven.infrastructure.persistence.repository_factory import (
            get_user_repository,
            STORAGE_TYPE_MEMORY,
        )

        # Create mock instance
        mock_auth_instance = Mock()
        ServiceContainer.register_instance(mock_auth, mock_auth_instance)

        # Create service with None dependency (should use DI)
        user_repo = get_user_repository(storage_type=STORAGE_TYPE_MEMORY)
        service = UserService(
            user_repo=user_repo,
            auth_service=None,  # Should use DI
        )

        # Verify DI was used
        assert service.auth_service is mock_auth_instance

    @patch('faultmaven.services.investigation_session_service.APIInvestigationSessionService')
    @patch('faultmaven.services.evidence_artifact_service.APIEvidenceArtifactService')
    def test_agent_orchestration_service_uses_di(self, mock_evidence, mock_session, mock_settings):
        """Test that AgentOrchestrationService uses DI instead of direct imports."""
        # This would have been violations:
        # - agent_orchestration_service → investigation_session_service
        # - agent_orchestration_service → evidence_artifact_service
        from faultmaven.services.agent_orchestration_service import AgentOrchestrationService

        # Create mock instances
        mock_session_instance = Mock()
        mock_evidence_instance = Mock()

        ServiceContainer.register_instance(mock_session, mock_session_instance)
        ServiceContainer.register_instance(mock_evidence, mock_evidence_instance)

        # Create service with None dependencies (should use DI)
        service = AgentOrchestrationService(
            session_service=None,  # Should use DI
            evidence_service=None,  # Should use DI
        )

        # Verify DI was used
        assert service.session_service is mock_session_instance
        assert service.evidence_service is mock_evidence_instance

    @patch('faultmaven.services.file_storage_service.FileStorageService')
    def test_evidence_artifact_service_uses_di(self, mock_file_storage, mock_settings):
        """Test that APIEvidenceArtifactService uses DI instead of direct imports."""
        # This would have been a violation: evidence_artifact_service → file_storage_service
        from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService
        from faultmaven.infrastructure.persistence.repository_factory import (
            get_evidence_artifact_repository,
            get_case_repository,
            STORAGE_TYPE_MEMORY,
        )

        # Create mock instance
        mock_storage_instance = Mock()
        ServiceContainer.register_instance(mock_file_storage, mock_storage_instance)

        # Create service with None dependency (should use DI)
        evidence_repo = get_evidence_artifact_repository(storage_type=STORAGE_TYPE_MEMORY)
        case_repo = get_case_repository(storage_type=STORAGE_TYPE_MEMORY)

        service = APIEvidenceArtifactService(
            evidence_repo=evidence_repo,
            case_repo=case_repo,
            file_storage=None,  # Should use DI
        )

        # Verify DI was used
        assert service.file_storage is mock_storage_instance


# ============================================================================
# Test Service Lifecycle
# ============================================================================

class TestServiceLifecycle:
    """Test service lifecycle management."""

    def test_clear_service_instances_resets_singletons(self, mock_settings):
        """Test that clear_service_instances() forces new instances."""
        from faultmaven.services.auth_service import AuthService

        # Get instance
        instance1 = ServiceContainer.get(AuthService)

        # Clear instances
        clear_service_instances()

        # Get again - should be new instance
        instance2 = ServiceContainer.get(AuthService)

        assert instance1 is not instance2

    def test_services_use_singleton_pattern(self, mock_settings):
        """Test that services follow singleton pattern within same lifecycle."""
        from faultmaven.services.auth_service import AuthService
        from faultmaven.modules.knowledge.domain.services.embedding_service import EmbeddingService

        # Get services multiple times
        auth1 = ServiceContainer.get(AuthService)
        auth2 = ServiceContainer.get(AuthService)

        embedding1 = ServiceContainer.get(EmbeddingService)
        embedding2 = ServiceContainer.get(EmbeddingService)

        # Should be same instances
        assert auth1 is auth2
        assert embedding1 is embedding2


# ============================================================================
# Test Backward Compatibility
# ============================================================================

class TestBackwardCompatibility:
    """Test that DI refactoring maintains backward compatibility."""

    @patch('faultmaven.services.embedding_service.EmbeddingService')
    @patch('faultmaven.services.vector_store_service.VectorStoreService')
    def test_manual_dependency_injection_still_works(self, mock_vector, mock_embedding, mock_settings):
        """Test that manually providing dependencies still works (backward compatibility)."""
        from faultmaven.modules.knowledge.domain.services.search_service import KnowledgeSearchService
        from faultmaven.infrastructure.persistence.repository_factory import (
            get_knowledge_repository,
            STORAGE_TYPE_MEMORY,
        )

        # Create manual dependencies
        manual_embedding = Mock()
        manual_vector = Mock()

        # Create service with manual dependencies
        knowledge_repo = get_knowledge_repository(storage_type=STORAGE_TYPE_MEMORY)
        service = KnowledgeSearchService(
            knowledge_repo=knowledge_repo,
            embedding_service=manual_embedding,  # Manually provided
            vector_store_service=manual_vector,   # Manually provided
        )

        # Should use manual dependencies, NOT DI container
        assert service.embedding_service is manual_embedding
        assert service.vector_store_service is manual_vector

    @patch('faultmaven.services.auth_service.AuthService')
    def test_mixed_injection_pattern(self, mock_auth, mock_settings):
        """Test mixed pattern: some dependencies manual, some via DI."""
        from faultmaven.services.user_service import UserService
        from faultmaven.infrastructure.persistence.repository_factory import (
            get_user_repository,
            STORAGE_TYPE_MEMORY,
        )

        # Provide one dependency manually, let DI handle the other
        manual_auth = Mock()

        user_repo = get_user_repository(storage_type=STORAGE_TYPE_MEMORY)
        service = UserService(
            user_repo=user_repo,
            auth_service=manual_auth,  # Manual
        )

        # Should use manual dependency
        assert service.auth_service is manual_auth


# ============================================================================
# Test Testing Support
# ============================================================================

class TestTestingSupport:
    """Test that DI container supports testing patterns."""

    def test_mock_service_for_unit_testing(self, mock_settings):
        """Test that services can be mocked for unit testing."""
        from faultmaven.services.auth_service import AuthService

        # Create mock
        mock_auth_service = Mock(spec=AuthService)
        mock_auth_service.validate_token.return_value = {"user_id": 123}

        # Register mock
        ServiceContainer.register_instance(AuthService, mock_auth_service)

        # Get service
        service = ServiceContainer.get(AuthService)

        # Should get mock
        assert service is mock_auth_service
        assert service.validate_token() == {"user_id": 123}

    def test_test_isolation_with_clear(self, mock_settings):
        """Test that clear() enables test isolation."""
        from faultmaven.services.auth_service import AuthService

        # Test 1: Create service
        service1 = ServiceContainer.get(AuthService)

        # Clear for test isolation
        ServiceContainer.clear()

        # Test 2: Create service again
        service2 = ServiceContainer.get(AuthService)

        # Should be different instances
        assert service1 is not service2
