"""
Integration test for User KB full flow

Tests:
1. Upload document to user KB
2. Query using answer_from_user_kb tool
3. List documents
4. Delete document
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from io import BytesIO


@pytest.mark.integration
class TestUserKBFlow:
    """Test complete User KB workflow"""

    @pytest.fixture
    def mock_user_kb_store(self):
        """Mock UserKBVectorStore"""
        store = AsyncMock()
        store.add_documents = AsyncMock()
        store.search = AsyncMock(return_value=[
            {
                'id': 'doc-123',
                'content': 'Database timeout troubleshooting: Check connection pool settings.',
                'metadata': {'title': 'DB Timeout Runbook', 'category': 'database'},
                'score': 0.95
            }
        ])
        store.list_documents = AsyncMock(return_value=[
            {
                'id': 'doc-123',
                'metadata': {
                    'title': 'DB Timeout Runbook',
                    'category': 'database',
                    'filename': 'db_timeout.md'
                },
                'created_at': '2025-10-22T10:00:00Z'
            }
        ])
        store.delete_document = AsyncMock()
        store.get_document_count = AsyncMock(return_value=1)
        return store

    @pytest.fixture
    def mock_preprocessing_service(self):
        """Mock PreprocessingService"""
        from faultmaven.models.api import PreprocessedData, ExtractionMetadata, DataType

        service = AsyncMock()
        service.preprocess = AsyncMock(return_value=PreprocessedData(
            content="Database timeout troubleshooting guide with connection pool settings.",
            metadata=ExtractionMetadata(
                data_type=DataType.UNSTRUCTURED_TEXT,
                processing_time_ms=50.0,
                llm_calls_used=0
            ),
            original_size=1024,
            processed_size=256
        ))
        return service

    @pytest.fixture
    def mock_container(self, mock_user_kb_store, mock_preprocessing_service):
        """Mock container with user KB dependencies"""
        container = MagicMock()
        container.user_kb_vector_store = mock_user_kb_store
        container.preprocessing_service = mock_preprocessing_service
        return container

    def test_upload_user_kb_document(self, mock_container):
        """Test uploading a document to user KB"""
        with patch('faultmaven.api.v1.routes.user_kb.container', mock_container):
            from faultmaven.main import app
            client = TestClient(app)

            # Create test file
            file_content = b"# Database Timeout Troubleshooting\n\nCheck connection pool settings."
            files = {'file': ('db_timeout.md', BytesIO(file_content), 'text/markdown')}
            data = {
                'title': 'DB Timeout Runbook',
                'category': 'database',
                'tags': 'postgresql,timeout,performance',
                'description': 'Runbook for database timeout issues'
            }

            # Mock authentication
            with patch('faultmaven.api.v1.routes.user_kb.require_authentication') as mock_auth:
                from faultmaven.models.auth import DevUser
                mock_auth.return_value = DevUser(user_id='test_user', username='testuser')

                # Upload document
                response = client.post(
                    '/api/v1/users/test_user/kb/documents',
                    files=files,
                    data=data
                )

                assert response.status_code == 201
                result = response.json()
                assert result['status'] == 'success'
                assert result['title'] == 'DB Timeout Runbook'
                assert result['category'] == 'database'
                assert result['data_type'] == 'UNSTRUCTURED_TEXT'
                assert 'document_id' in result

                # Verify store was called
                mock_container.user_kb_vector_store.add_documents.assert_called_once()

    def test_list_user_kb_documents(self, mock_container):
        """Test listing user's KB documents"""
        with patch('faultmaven.api.v1.routes.user_kb.container', mock_container):
            from faultmaven.main import app
            client = TestClient(app)

            # Mock authentication
            with patch('faultmaven.api.v1.routes.user_kb.require_authentication') as mock_auth:
                from faultmaven.models.auth import DevUser
                mock_auth.return_value = DevUser(user_id='test_user', username='testuser')

                # List documents
                response = client.get('/api/v1/users/test_user/kb/documents')

                assert response.status_code == 200
                result = response.json()
                assert result['status'] == 'success'
                assert result['user_id'] == 'test_user'
                assert result['total_count'] == 1
                assert len(result['documents']) == 1
                assert result['documents'][0]['metadata']['title'] == 'DB Timeout Runbook'

    def test_delete_user_kb_document(self, mock_container):
        """Test deleting a document from user KB"""
        with patch('faultmaven.api.v1.routes.user_kb.container', mock_container):
            from faultmaven.main import app
            client = TestClient(app)

            # Mock authentication
            with patch('faultmaven.api.v1.routes.user_kb.require_authentication') as mock_auth:
                from faultmaven.models.auth import DevUser
                mock_auth.return_value = DevUser(user_id='test_user', username='testuser')

                # Delete document
                response = client.delete('/api/v1/users/test_user/kb/documents/doc-123')

                assert response.status_code == 204

                # Verify delete was called
                mock_container.user_kb_vector_store.delete_document.assert_called_once_with(
                    user_id='test_user',
                    doc_id='doc-123'
                )

    def test_get_user_kb_stats(self, mock_container):
        """Test getting user KB statistics"""
        with patch('faultmaven.api.v1.routes.user_kb.container', mock_container):
            from faultmaven.main import app
            client = TestClient(app)

            # Mock authentication
            with patch('faultmaven.api.v1.routes.user_kb.require_authentication') as mock_auth:
                from faultmaven.models.auth import DevUser
                mock_auth.return_value = DevUser(user_id='test_user', username='testuser')

                # Get stats
                response = client.get('/api/v1/users/test_user/kb/stats')

                assert response.status_code == 200
                result = response.json()
                assert result['status'] == 'success'
                assert result['user_id'] == 'test_user'
                assert result['total_documents'] == 1
                assert 'categories' in result

    def test_access_control_upload(self, mock_container):
        """Test that users can only upload to their own KB"""
        with patch('faultmaven.api.v1.routes.user_kb.container', mock_container):
            from faultmaven.main import app
            client = TestClient(app)

            file_content = b"Test content"
            files = {'file': ('test.md', BytesIO(file_content), 'text/markdown')}

            # Mock authentication as different user
            with patch('faultmaven.api.v1.routes.user_kb.require_authentication') as mock_auth:
                from faultmaven.models.auth import DevUser
                mock_auth.return_value = DevUser(user_id='other_user', username='otheruser')

                # Try to upload to different user's KB
                response = client.post(
                    '/api/v1/users/test_user/kb/documents',
                    files=files
                )

                assert response.status_code == 403
                assert 'Access denied' in response.json()['detail']

    def test_access_control_list(self, mock_container):
        """Test that users can only list their own KB"""
        with patch('faultmaven.api.v1.routes.user_kb.container', mock_container):
            from faultmaven.main import app
            client = TestClient(app)

            # Mock authentication as different user
            with patch('faultmaven.api.v1.routes.user_kb.require_authentication') as mock_auth:
                from faultmaven.models.auth import DevUser
                mock_auth.return_value = DevUser(user_id='other_user', username='otheruser')

                # Try to list different user's KB
                response = client.get('/api/v1/users/test_user/kb/documents')

                assert response.status_code == 403
                assert 'Access denied' in response.json()['detail']
