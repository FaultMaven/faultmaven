"""Simplified API Test Configuration

Minimal test fixtures that can handle missing dependencies gracefully.
"""

from unittest.mock import Mock, AsyncMock
from typing import Optional, Any, Dict

# Import test doubles
try:
    from tests.test_doubles import (
        LightweightLLMProvider,
        LightweightSanitizer,
        LightweightStorageBackend,
        LightweightDataClassifier,
        LightweightLogProcessor
    )
    TEST_DOUBLES_AVAILABLE = True
except ImportError:
    TEST_DOUBLES_AVAILABLE = False
    LightweightLLMProvider = Mock
    LightweightSanitizer = Mock
    LightweightStorageBackend = Mock
    LightweightDataClassifier = Mock 
    LightweightLogProcessor = Mock

# Try pytest imports with fallback
try:
    import pytest
    import pytest_asyncio
    PYTEST_AVAILABLE = True
except ImportError:
    PYTEST_AVAILABLE = False
    
    # Create mock pytest decorators
    class MockPytest:
        @staticmethod
        def fixture(*args, **kwargs):
            def decorator(func):
                return func
            return decorator
        
        class mark:
            @staticmethod
            def asyncio(func):
                return func
    
    pytest = MockPytest()

# Try FastAPI/httpx imports with fallback
try:
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    FastAPI = Mock
    AsyncClient = Mock
    ASGITransport = Mock

# Only define fixtures if all dependencies are available
if PYTEST_AVAILABLE and FASTAPI_AVAILABLE:
    
    @pytest.fixture(scope="session")
    def event_loop():
        """Create an event loop for the session."""
        import asyncio
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def test_doubles():
        """Provide lightweight test doubles for external dependencies."""
        return {
            'llm_provider': LightweightLLMProvider(),
            'data_sanitizer': LightweightSanitizer(),
            'storage_backend': LightweightStorageBackend(),
            'data_classifier': LightweightDataClassifier(),
            'log_processor': LightweightLogProcessor()
        }

    @pytest.fixture
    async def simple_app():
        """Create a FastAPI app with actual API routes for testing."""
        app = FastAPI(title="Test API")
        
        # Add a simple test endpoint
        @app.get("/health")
        async def health():
            return {"status": "ok"}
        
        # Include the actual FaultMaven API routes with dependency overrides
        try:
            from faultmaven.api.v1.routes import agent, data, knowledge, session
            from faultmaven.api.v1.dependencies import get_agent_service, get_data_service, get_knowledge_service, get_session_service
            
            app.include_router(agent.router, prefix="/api/v1", tags=["agent"])
            app.include_router(data.router, prefix="/api/v1", tags=["data"]) 
            app.include_router(knowledge.router, prefix="/api/v1", tags=["knowledge"])
            app.include_router(session.router, prefix="/api/v1", tags=["session"])
            
            # Override dependencies with test doubles
            # Shared state for data service to persist across calls
            data_service_state = {
                'upload_counter': 0,
                'deleted_items': set(),
                'uploaded_items': {}
            }
            
            async def mock_agent_service():
                from unittest.mock import Mock, AsyncMock
                from faultmaven.models import TroubleshootingResponse, AgentResponse, ViewState, UploadedData, Source, SourceType, ResponseType
                mock_service = Mock()
                # Counter for unique investigation IDs
                import uuid
                
                # Make process_query an async mock that returns a TroubleshootingResponse object
                async def mock_process_query(*args, **kwargs):
                    # Handle invalid input gracefully
                    try:
                        # Extract query details from the request
                        if args:
                            query_request = args[0]
                            if query_request is None:
                                raise ValidationException("Request cannot be None")
                            session_id = getattr(query_request, 'session_id', 'test_session_12345')
                            query = getattr(query_request, 'query', 'default query')
                            context = getattr(query_request, 'context', {}) or {}
                        else:
                            # Handle direct parameter calls
                            session_id = kwargs.get('session_id', 'test_session_12345')
                            query = kwargs.get('query', 'default query')
                            context = kwargs.get('context', {}) or {}
                    except Exception as e:
                        # If we can't extract data, this is likely a validation error
                        raise ValidationException(f"Invalid request format: {e}")
                    
                    # Implement validation logic to match real AgentService behavior
                    from faultmaven.exceptions import ValidationException
                    
                    # Validate query
                    if not query or not query.strip():
                        raise ValidationException("Query cannot be empty")
                    
                    # Validate session_id
                    if not session_id or not session_id.strip():
                        raise ValidationException("Session ID cannot be empty")
                    
                    # Check for non-existent session (return 404)
                    # Allow test session IDs and specific test patterns, reject only "non_existent_session"
                    if session_id == "non_existent_session":
                        raise FileNotFoundError(f"Session {session_id} not found")
                    # Allow any session that starts with "test_session" or other valid patterns
                    
                    investigation_id = f"test_{uuid.uuid4().hex[:8]}"
                    
                    # Create realistic findings based on query content
                    findings = []
                    recommendations = []
                    confidence_score = 0.8
                    
                    query_lower = query.lower()
                    if 'database' in query_lower or 'connection' in query_lower:
                        findings.extend([
                            {"type": "error", "message": "Database connection failed", "severity": "high", "timestamp": "2024-01-01T12:00:00Z"},
                            {"type": "warning", "message": "Connection pool exhaustion detected", "severity": "medium", "timestamp": "2024-01-01T12:00:01Z"}
                        ])
                        recommendations.extend([
                            "Check database connection pool configuration",
                            "Increase connection pool size if needed",
                            "Monitor connection usage patterns"
                        ])
                    elif 'memory' in query_lower or 'performance' in query_lower:
                        findings.extend([
                            {"type": "warning", "message": "High memory usage detected", "severity": "medium", "timestamp": "2024-01-01T12:00:01Z"},
                            {"type": "info", "message": "CPU utilization within normal range", "severity": "low", "timestamp": "2024-01-01T12:00:02Z"}
                        ])
                        recommendations.extend([
                            "Review memory allocation settings",
                            "Monitor system resources",
                            "Consider memory optimization"
                        ])
                    else:
                        # Generic findings for other queries
                        findings.extend([
                            {"type": "info", "message": "System analysis completed", "severity": "low", "timestamp": "2024-01-01T12:00:00Z"}
                        ])
                        recommendations.extend([
                            "Review system logs for additional details",
                            "Monitor ongoing performance"
                        ])
                    
                    # Adjust confidence based on context
                    if context.get('priority') == 'critical':
                        confidence_score = min(0.9, confidence_score + 0.1)
                    
                    # Record the query operation in the session
                    await _record_session_query_operation(
                        session_id, query, investigation_id, context, confidence_score
                    )
                    
                    # Create v3.1.0 AgentResponse for process_query method
                    view_state = ViewState(
                        session_id=session_id,
                        case_id=investigation_id,
                        running_summary=f"Investigation for: {query}",
                        uploaded_data=[
                            UploadedData(id="test_data_1", name="test_log.log", type="log_file")
                        ]
                    )
                    
                    sources = [
                        Source(
                            type=SourceType.KNOWLEDGE_BASE,
                            name="troubleshooting_guide.md",
                            snippet="Database connection troubleshooting steps..."
                        )
                    ]
                    
                    # Generate content based on findings and recommendations
                    content = "Analysis Results:\n"
                    if findings:
                        content += "Key findings: " + "; ".join([f["message"] for f in findings]) + "\n"
                    if recommendations:
                        content += "Recommendations: " + "; ".join(recommendations[:2])
                    
                    return AgentResponse(
                        content=content,
                        response_type=ResponseType.ANSWER,
                        view_state=view_state,
                        sources=sources
                    )
                mock_service.process_query = mock_process_query
                
                # Add methods for investigation endpoints
                async def mock_get_investigation_results(investigation_id, session_id=None, *args, **kwargs):
                    # Return comprehensive findings that match what process_query produces
                    return TroubleshootingResponse(
                        investigation_id=investigation_id,
                        session_id=session_id or "test_session_12345",
                        status="completed",
                        findings=[
                            {"type": "error", "message": "Database connection failed", "severity": "high", "timestamp": "2024-01-01T12:00:00Z"},
                            {"type": "warning", "message": "Connection pool exhaustion detected", "severity": "medium", "timestamp": "2024-01-01T12:00:01Z"}
                        ],
                        recommendations=[
                            "Check database connection pool configuration",
                            "Increase connection pool size if needed",
                            "Monitor connection usage patterns"
                        ],
                        confidence_score=0.8,
                        created_at="2024-01-01T12:00:00Z"
                    )
                    
                async def mock_list_session_investigations(*args, **kwargs):
                    return [
                        {"investigation_id": "inv_1", "query": "Database issue", "status": "completed", "created_at": "2024-01-01T12:00:00Z"},
                        {"investigation_id": "inv_2", "query": "Memory issue", "status": "completed", "created_at": "2024-01-01T12:01:00Z"},
                        {"investigation_id": "inv_3", "query": "Network issue", "status": "completed", "created_at": "2024-01-01T12:02:00Z"},
                        {"investigation_id": "inv_4", "query": "CPU issue", "status": "completed", "created_at": "2024-01-01T12:03:00Z"}
                    ]
                
                mock_service.get_investigation_results = mock_get_investigation_results  
                mock_service.list_session_investigations = mock_list_session_investigations
                return mock_service
            
            async def mock_data_service():
                from unittest.mock import Mock
                from faultmaven.models import UploadedData, DataType
                import uuid
                import time
                
                mock_service = Mock()
                
                # Use shared state from closure
                nonlocal data_service_state
                
                # Make ingest_data an async mock that returns an UploadedData object
                async def mock_ingest_data(*args, **kwargs):
                    data_service_state['upload_counter'] += 1
                    upload_counter = data_service_state['upload_counter']
                    uploaded_items = data_service_state['uploaded_items']
                    
                    # Extract filename from kwargs to determine data type
                    filename = kwargs.get('filename') or 'unknown'
                    file_size = kwargs.get('file_size', 0)
                    session_id = kwargs.get('session_id', 'test_session_12345')
                    content = kwargs.get('content', '')
                    
                    # Determine data type based on filename and content
                    if filename.endswith('.json') or 'config' in filename.lower():
                        data_type = DataType.CONFIG_FILE
                        detected_type = 'config'
                    elif filename.endswith('.log') or 'ERROR' in content or 'WARN' in content:
                        data_type = DataType.LOG_FILE
                        detected_type = 'log_file'
                    elif filename.endswith('.csv') or 'timestamp,cpu_usage' in content:
                        data_type = DataType.METRICS_DATA
                        detected_type = 'text'
                    elif 'Traceback' in content or 'Exception:' in content:
                        data_type = DataType.STACK_TRACE
                        detected_type = 'text'
                    else:
                        data_type = DataType.UNKNOWN
                        detected_type = 'unknown'
                    
                    # Determine error count based on content
                    error_count = 0
                    if content:
                        error_count = content.count('ERROR') + content.count('Exception')
                    
                    data_id = f"data_{upload_counter}_{int(time.time() * 1000)}"
                    
                    # Return a dict with the old format that tests expect (not the new v3.1.0 UploadedData model)
                    # This bypasses FastAPI response model validation since we removed response_model
                    uploaded_data = {
                        "data_id": data_id,
                        "session_id": session_id,
                        "data_type": detected_type,
                        "content": content or "2024-01-01 12:00:01 INFO Default test content",
                        "file_name": filename,
                        "file_size": file_size or len(content or ''),
                        "processing_status": "completed",
                        "insights": {
                            "error_count": error_count,
                            "error_rate": error_count / max(1, len(content.split('\n')) if content else 1),
                            "confidence_score": 0.8 if content else 0.5,
                            "processing_time_ms": 100 + (upload_counter * 10),
                            "patterns_detected": ["connection_failure", "timeout"] if error_count > 0 else [],
                            "recommendations": ["Check network connectivity", "Review timeout settings"] if error_count > 0 else []
                        }
                    }
                    
                    # Store the uploaded data
                    uploaded_items[data_id] = uploaded_data
                    
                    # Record the data upload operation in the session
                    await _record_session_data_upload_operation(
                        session_id, data_id, filename, file_size or len(content or ''), {}
                    )
                    
                    return uploaded_data
                mock_service.ingest_data = mock_ingest_data
                
                # Add methods for data analysis and other operations
                async def mock_analyze_data(*args, **kwargs):
                    from faultmaven.models import DataInsightsResponse
                    data_id = args[0] if args else "data_123"
                    return DataInsightsResponse(
                        data_id=data_id,
                        data_type=DataType.LOG_FILE,
                        confidence_score=0.85,
                        processing_time_ms=150,
                        insights={
                            "error_count": 3,
                            "patterns_detected": ["database_error", "connection_timeout"],
                            "recommendations": ["Check database connection", "Increase timeout values"]
                        }
                    )
                
                async def mock_get_data(*args, **kwargs):
                    data_id = args[0] if args else "data_123"
                    deleted_items = data_service_state['deleted_items']
                    uploaded_items = data_service_state['uploaded_items']
                    
                    # Check if the item was deleted
                    if data_id in deleted_items:
                        # Raise FileNotFoundError to trigger 404 in the API
                        raise FileNotFoundError(f"Data {data_id} not found (deleted)")
                        
                    # Return stored data if available, otherwise create default data
                    if data_id in uploaded_items:
                        return uploaded_items[data_id]
                    
                    return UploadedData(
                        data_id=data_id,
                        session_id="test_session_12345",
                        data_type=DataType.LOG_FILE,
                        content="2024-01-01 12:00:01 ERROR Sample error content",
                        file_name="sample.log",
                        file_size=512,
                        processing_status="completed",
                        insights={
                            "processed": True,
                            "error_count": 1,
                            "confidence_score": 0.8,
                            "processing_time_ms": 120
                        }
                    )
                
                async def mock_delete_data(*args, **kwargs):
                    data_id = args[0] if args else None
                    if data_id:
                        data_service_state['deleted_items'].add(data_id)
                        # Remove from uploaded_items if it exists
                        data_service_state['uploaded_items'].pop(data_id, None)
                    return True
                
                async def mock_get_session_data(session_id, *args, **kwargs):
                    """Mock get session data method for performance tests."""
                    # Return a list of UploadedData objects to match real service implementation
                    uploaded_items = data_service_state['uploaded_items']
                    session_data = []
                    
                    # Find all data items for this session
                    for data_id, uploaded_data in uploaded_items.items():
                        if uploaded_data.session_id == session_id:
                            session_data.append(uploaded_data)
                    
                    # If no real data exists, create a test item for the session
                    if not session_data:
                        test_data = UploadedData(
                            data_id=f"data_{session_id}_1",
                            session_id=session_id,
                            data_type=DataType.LOG_FILE,
                            content="Test log data for session",
                            file_name="test.log",
                            file_size=1024,
                            processing_status="completed",
                            insights={
                                "error_count": 0,
                                "processing_time_ms": 50,
                                "confidence_score": 0.8
                            }
                        )
                        session_data.append(test_data)
                    
                    return session_data
                
                mock_service.analyze_data = mock_analyze_data
                mock_service.get_data = mock_get_data
                mock_service.delete_data = mock_delete_data
                mock_service.get_session_data = mock_get_session_data
                return mock_service
            
            # Create persistent knowledge storage outside function to survive dependency injection calls
            from datetime import datetime, timezone
            _persistent_knowledge_documents = {
                "doc_sample_1": {
                    "document_id": "doc_sample_1",
                    "title": "Database Connection Troubleshooting Guide",
                    "document_type": "troubleshooting",
                    "category": "database",
                    "tags": ["database", "connection", "timeout", "pool"],
                    "description": "Guide for database connection issues",
                    "content": "Database connection troubleshooting includes checking pool configuration...",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "status": "completed",
                    "metadata": {
                        "title": "Database Connection Troubleshooting Guide",
                        "document_type": "troubleshooting",
                        "category": "database",
                        "tags": ["database", "connection", "timeout", "pool"],
                        "description": "Guide for database connection issues"
                    }
                }
            }
            _persistent_knowledge_jobs = {}
            _persistent_knowledge_stats = {
                "total_documents": 1,
                "document_types": {"troubleshooting": 1},
                "categories": {"database": 1},
                "total_chunks": 5,
                "avg_chunk_size": 512,
                "storage_used": 100
            }
            
            # Function to clear persistent knowledge state for test isolation
            def _clear_persistent_knowledge_state():
                _persistent_knowledge_documents.clear()
                _persistent_knowledge_jobs.clear()
                _persistent_knowledge_stats.clear()
            
            async def mock_knowledge_service():
                from unittest.mock import Mock
                import uuid
                from datetime import datetime, timezone
                
                mock_service = Mock()
                
                # Use persistent storage that survives across multiple dependency injection calls
                test_documents = _persistent_knowledge_documents
                test_jobs = _persistent_knowledge_jobs
                test_stats = _persistent_knowledge_stats
                
                # Add async methods that knowledge service tests expect - using correct method names from actual service
                async def mock_upload_document(content, title, document_type, category=None, tags=None, source_url=None, description=None, *args, **kwargs):
                    document_id = f"doc_{uuid.uuid4().hex[:8]}"
                    job_id = f"job_{uuid.uuid4().hex[:8]}"
                    
                    # Create document record
                    document = {
                        "document_id": document_id,
                        "title": title,
                        "document_type": document_type,
                        "category": category or "general",
                        "tags": tags if isinstance(tags, list) else (tags.split(",") if isinstance(tags, str) else []),
                        "description": description,
                        "content": content,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "status": "completed",
                        "metadata": {
                            "title": title,
                            "document_type": document_type,
                            "category": category or "general",
                            "tags": tags if isinstance(tags, list) else (tags.split(",") if isinstance(tags, str) else []),
                            "description": description
                        }
                    }
                    test_documents[document_id] = document
                    
                    # Create job record
                    job = {
                        "job_id": job_id,
                        "document_id": document_id,
                        "status": "completed",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "processing_results": {
                            "chunks_created": 5,
                            "embeddings_generated": 5,
                            "processing_time_ms": 150
                        }
                    }
                    test_jobs[job_id] = job
                    
                    # Update stats
                    test_stats["total_documents"] += 1
                    doc_type = document_type or "unknown"
                    test_stats["document_types"][doc_type] = test_stats["document_types"].get(doc_type, 0) + 1
                    cat = category or "general"
                    test_stats["categories"][cat] = test_stats["categories"].get(cat, 0) + 1
                    test_stats["total_chunks"] += 5
                    test_stats["storage_used"] += len(content) if content else 0
                    
                    return {
                        "job_id": job_id,
                        "document_id": document_id,
                        "status": "completed",
                        "metadata": document["metadata"]
                    }
                
                async def mock_get_job_status(job_id, *args, **kwargs):
                    job = test_jobs.get(job_id)
                    if job:
                        return job
                    # Return None for non-existent jobs (API will handle 404)
                    return None
                
                async def mock_search_documents(query, document_type=None, tags=None, limit=10, *args, **kwargs):
                    # Validate search parameters
                    if not query or not query.strip():
                        raise ValidationException("Query parameter is required for search")
                    if limit and (limit < 1 or limit > 100):
                        raise ValidationException("Limit must be between 1 and 100")
                        
                    # Simple mock search - return relevant documents
                    results = []
                    search_terms = query.lower().split()
                    
                    for doc_id, doc in test_documents.items():
                        # Simple relevance scoring based on title/content matching
                        score = 0.0
                        content_lower = (doc.get("content", "") or "").lower()
                        title_lower = doc.get("title", "").lower()
                        
                        for term in search_terms:
                            if term in title_lower:
                                score += 0.8
                            if term in content_lower:
                                score += 0.3
                        
                        # Apply filters
                        if document_type and doc.get("document_type") != document_type:
                            continue
                        if tags:
                            doc_tags = doc.get("tags", [])
                            if not any(tag in doc_tags for tag in tags):
                                continue
                        
                        # Only include if has some relevance
                        if score > 0.0:
                            result = {
                                "document_id": doc_id,
                                "content": doc.get("content", "")[:200] + "..." if doc.get("content") else "",
                                "similarity_score": min(score, 1.0),
                                "metadata": doc.get("metadata", {})
                            }
                            results.append(result)
                    
                    # Sort by similarity score
                    results.sort(key=lambda x: x["similarity_score"], reverse=True)
                    results = results[:limit]
                    
                    return {
                        "results": results,
                        "query": query,
                        "total_results": len(results)
                    }
                
                async def mock_get_document(document_id, *args, **kwargs):
                    doc = test_documents.get(document_id)
                    if doc:
                        return doc
                    # Return None for non-existent documents (API will handle 404)
                    return None
                
                async def mock_list_documents(*args, **kwargs):
                    documents = list(test_documents.values())
                    return {
                        "documents": documents,
                        "total_count": len(documents)
                    }
                
                async def mock_update_document_metadata(document_id, *args, **kwargs):
                    # Extract updates from kwargs since API calls with **update_data
                    updates = {k: v for k, v in kwargs.items() if k not in ['document_id']}
                    doc = test_documents.get(document_id)
                    if doc:
                        doc.update(updates)
                        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
                        return doc
                    return None
                
                async def mock_delete_document(document_id, *args, **kwargs):
                    if document_id in test_documents:
                        del test_documents[document_id]
                        test_stats["total_documents"] = max(0, test_stats["total_documents"] - 1)
                        return {"success": True, "document_id": document_id}
                    return {"success": False, "error": "Document not found"}
                
                async def mock_bulk_update_documents(document_ids, updates, *args, **kwargs):
                    updated_count = 0
                    for doc_id in document_ids:
                        if doc_id in test_documents:
                            test_documents[doc_id].update(updates)
                            test_documents[doc_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
                            updated_count += 1
                    return {
                        "success": True,
                        "updated_count": updated_count
                    }
                
                async def mock_bulk_delete_documents(document_ids, *args, **kwargs):
                    deleted_count = 0
                    for doc_id in document_ids:
                        if doc_id in test_documents:
                            del test_documents[doc_id]
                            deleted_count += 1
                    test_stats["total_documents"] = max(0, test_stats["total_documents"] - deleted_count)
                    return {
                        "success": True,
                        "deleted_count": deleted_count
                    }
                
                async def mock_get_knowledge_stats(*args, **kwargs):
                    return test_stats.copy()
                
                async def mock_get_search_analytics(*args, **kwargs):
                    return {
                        "popular_queries": ["database connection", "timeout issues", "network problems"],
                        "search_volume": 150,
                        "avg_response_time": 0.3,
                        "hit_rate": 0.85,
                        "category_distribution": test_stats["categories"]
                    }
                
                # Assign methods to mock service with correct method names from actual service
                mock_service.upload_document = mock_upload_document
                mock_service.get_job_status = mock_get_job_status
                mock_service.search_documents = mock_search_documents
                mock_service.get_document = mock_get_document
                mock_service.list_documents = mock_list_documents
                mock_service.update_document_metadata = mock_update_document_metadata
                mock_service.delete_document = mock_delete_document
                mock_service.bulk_update_documents = mock_bulk_update_documents
                mock_service.bulk_delete_documents = mock_bulk_delete_documents
                mock_service.get_knowledge_stats = mock_get_knowledge_stats
                mock_service.get_search_analytics = mock_get_search_analytics
                
                # Store references for integration
                mock_service._test_documents = test_documents
                mock_service._test_jobs = test_jobs
                mock_service._test_stats = test_stats
                mock_service._clear_persistent_state = _clear_persistent_knowledge_state
                return mock_service
            
            # Create persistent session storage outside function to survive dependency injection calls
            # This ensures sessions persist across multiple FastAPI dependency injections within the same test
            _persistent_test_sessions = {}
            _persistent_session_stats = {}
            
            # Function to clear persistent state for test isolation
            def _clear_persistent_session_state():
                _persistent_test_sessions.clear()
                _persistent_session_stats.clear()
            
            # Session operation recording functions that can be called from other mock services
            async def _record_session_query_operation(session_id, query, investigation_id, context=None, confidence_score=0.0):
                from datetime import datetime, timezone
                session = _persistent_test_sessions.get(session_id)
                if session:
                    investigation_record = {
                        "action": "query_processed",
                        "investigation_id": investigation_id,
                        "query": query,
                        "context": context or {},
                        "confidence_score": confidence_score,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                    session.case_history.append(investigation_record)
                    session.last_activity = datetime.now(timezone.utc)
                    return True
                return False
            
            async def _record_session_data_upload_operation(session_id, data_id, filename, file_size=0, metadata=None):
                from datetime import datetime, timezone
                session = _persistent_test_sessions.get(session_id)
                if session:
                    # Add to data uploads list
                    if data_id not in session.data_uploads:
                        session.data_uploads.append(data_id)
                    
                    # Add to investigation history for tracking
                    upload_record = {
                        "action": "data_uploaded",
                        "data_id": data_id,
                        "filename": filename,
                        "file_size": file_size,
                        "metadata": metadata or {},
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                    session.case_history.append(upload_record)
                    session.last_activity = datetime.now(timezone.utc)
                    return True
                return False
            
            async def _record_session_heartbeat_operation(session_id):
                from datetime import datetime, timezone
                session = _persistent_test_sessions.get(session_id)
                if session:
                    # Add heartbeat to investigation history for counting
                    heartbeat_record = {
                        "action": "heartbeat",
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                    session.case_history.append(heartbeat_record)
                    session.last_activity = datetime.now(timezone.utc)
                    return True
                return False
            
            async def mock_session_service():
                from unittest.mock import Mock
                from faultmaven.models import SessionContext
                from datetime import datetime, timezone
                import uuid
                
                mock_service = Mock()
                
                # Use persistent storage that survives across multiple dependency injection calls
                test_sessions = _persistent_test_sessions
                session_stats = _persistent_session_stats
                
                # Add async methods that session service tests expect
                async def mock_create_session(user_id=None, *args, **kwargs):
                    session_id = f"test_session_{uuid.uuid4().hex[:8]}"
                    session = SessionContext(
                        session_id=session_id,
                        user_id=user_id or "test_user",
                        created_at=datetime.now(timezone.utc),
                        last_activity=datetime.now(timezone.utc),
                        data_uploads=[],
                        case_history=[],
                        agent_state={
                            "session_id": session_id,
                            "user_query": "",
                            "current_phase": "initial",
                            "investigation_context": {},
                            "findings": [],
                            "recommendations": [],
                            "confidence_score": 0.0,
                            "tools_used": []
                        }
                    )
                    test_sessions[session_id] = session
                    return session
                    
                async def mock_get_session(session_id, *args, **kwargs):
                    session = test_sessions.get(session_id)
                    if session:
                        return session
                    # Return None for non-existent sessions (API will return 404)
                    return None
                    
                async def mock_list_sessions(user_id=None, status=None, limit=None, offset=None, *args, **kwargs):
                    sessions = []
                    for session in test_sessions.values():
                        # Apply user_id filter
                        if user_id is not None and session.user_id != user_id:
                            continue
                        # Apply status filter (assume all sessions are "active" for tests)
                        if status is not None and status != "active":
                            continue
                        sessions.append(session)
                    
                    # The route expects just a list of sessions, not a dictionary
                    # The route will handle pagination itself
                    return sessions
                    
                async def mock_delete_session(session_id, *args, **kwargs):
                    if session_id in test_sessions:
                        del test_sessions[session_id]
                        return True
                    return False
                
                async def mock_update_last_activity(session_id, *args, **kwargs):
                    session = test_sessions.get(session_id)
                    if session:
                        # Record the heartbeat operation
                        await _record_session_heartbeat_operation(session_id)
                        return True
                    return False
                
                async def mock_cleanup_session_data(session_id, *args, **kwargs):
                    session = test_sessions.get(session_id)
                    if session:
                        data_uploads_count = len(session.data_uploads)
                        case_history_count = len(session.case_history)
                        
                        # Clear session data but keep session active
                        session.data_uploads.clear()
                        session.case_history.clear()
                        session.agent_state = None
                        
                        return {
                            "success": True,
                            "session_id": session_id,
                            "status": "cleaned",
                            "message": "Session data cleaned successfully",
                            "cleaned_items": {
                                "data_uploads": data_uploads_count,
                                "case_history": case_history_count,
                                "temp_files": 0
                            }
                        }
                    return {
                        "success": False,
                        "error": "Session not found"
                    }
                
                # Add operation tracking methods for realistic session behavior
                async def mock_record_query_operation(session_id, query, investigation_id, context=None, confidence_score=0.0, *args, **kwargs):
                    session = test_sessions.get(session_id)
                    if session:
                        investigation_record = {
                            "action": "query_processed",
                            "investigation_id": investigation_id,
                            "query": query,
                            "context": context or {},
                            "confidence_score": confidence_score,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                        session.case_history.append(investigation_record)
                        session.last_activity = datetime.now(timezone.utc)
                        return True
                    return False
                
                async def mock_record_data_upload_operation(session_id, data_id, filename, file_size=0, metadata=None, *args, **kwargs):
                    session = test_sessions.get(session_id)
                    if session:
                        # Add to data uploads list
                        if data_id not in session.data_uploads:
                            session.data_uploads.append(data_id)
                        
                        # Add to investigation history for tracking
                        upload_record = {
                            "action": "data_uploaded",
                            "data_id": data_id,
                            "filename": filename,
                            "file_size": file_size,
                            "metadata": metadata or {},
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                        session.case_history.append(upload_record)
                        session.last_activity = datetime.now(timezone.utc)
                        return True
                    return False
                
                # Legacy methods for backward compatibility
                async def mock_heartbeat(session_id, *args, **kwargs):
                    await _record_session_heartbeat_operation(session_id)
                    return {"status": "updated", "last_activity": datetime.now(timezone.utc).isoformat(), "session_id": session_id}
                
                async def mock_update_session(*args, **kwargs):
                    return True
                
                async def mock_get_session_stats(session_id, *args, **kwargs):
                    """Mock get session stats method for performance tests."""
                    session = test_sessions.get(session_id)
                    if not session:
                        return None
                    
                    # Count different types of operations
                    query_operations = len([h for h in session.case_history if h.get("action") == "query_processed"])
                    upload_operations = len([h for h in session.case_history if h.get("action") == "data_uploaded"])
                    heartbeat_operations = len([h for h in session.case_history if h.get("action") == "heartbeat"])
                    
                    return {
                        "session_id": session_id,
                        "total_requests": len(session.case_history),  # Count ALL operations
                        "data_uploads": len(session.data_uploads),
                        "created_at": session.created_at.isoformat(),
                        "last_activity": session.last_activity.isoformat(),
                        "status": "active",
                        "statistics": {
                            "total_investigations": query_operations,
                            "total_data_uploads": len(session.data_uploads),
                            "latest_confidence_score": 0.8,
                            "session_duration_minutes": int(
                                (session.last_activity - session.created_at).total_seconds() / 60
                            ),
                        },
                        "operations_history": session.case_history,  # For test compatibility
                        "memory_usage": 1024,  # Mock memory usage
                        "processing_time": 0.5  # Mock processing time
                    }
                
                mock_service.create_session = mock_create_session
                mock_service.get_session = mock_get_session
                mock_service.list_sessions = mock_list_sessions
                mock_service.delete_session = mock_delete_session
                mock_service.update_last_activity = mock_update_last_activity
                mock_service.cleanup_session_data = mock_cleanup_session_data
                mock_service.record_query_operation = mock_record_query_operation
                mock_service.record_data_upload_operation = mock_record_data_upload_operation
                mock_service.heartbeat = mock_heartbeat
                mock_service.update_session = mock_update_session
                mock_service.get_session_stats = mock_get_session_stats
                
                # Create session_manager mock for direct access in session endpoints
                from unittest.mock import AsyncMock
                mock_session_manager = Mock()
                
                async def mock_add_case_history(session_id, record, *args, **kwargs):
                    """Mock add_case_history for session endpoints."""
                    session = test_sessions.get(session_id)
                    if session:
                        session.case_history.append(record)
                        session.last_activity = datetime.now(timezone.utc)
                        return True
                    return False
                
                mock_session_manager.add_case_history = mock_add_case_history
                mock_service.session_manager = mock_session_manager
                
                # Store references to test data and utility functions for integration
                mock_service._test_sessions = test_sessions
                mock_service._clear_persistent_state = _clear_persistent_session_state
                return mock_service
            
            # Apply dependency overrides
            app.dependency_overrides[get_agent_service] = mock_agent_service
            app.dependency_overrides[get_data_service] = mock_data_service  
            app.dependency_overrides[get_knowledge_service] = mock_knowledge_service
            app.dependency_overrides[get_session_service] = mock_session_service
            
            # Add proper error handlers to ensure proper HTTP status codes
            from fastapi import HTTPException, Request
            from fastapi.responses import JSONResponse
            from fastapi.exceptions import RequestValidationError
            from faultmaven.exceptions import ValidationException
            import json
            
            @app.exception_handler(ValidationException)
            async def validation_exception_handler(request: Request, exc: ValidationException):
                return JSONResponse(
                    status_code=422,
                    content={"detail": str(exc)}
                )
                
            @app.exception_handler(FileNotFoundError)
            async def file_not_found_exception_handler(request: Request, exc: FileNotFoundError):
                return JSONResponse(
                    status_code=404,
                    content={"detail": str(exc)}
                )
                
            @app.exception_handler(RequestValidationError)
            async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
                return JSONResponse(
                    status_code=422,
                    content={"detail": exc.errors()}
                )
                
            # Add middleware to handle content-type validation
            @app.middleware("http")
            async def content_type_validation_middleware(request: Request, call_next):
                # Check if this is a POST/PUT request to API endpoints
                if request.method in ["POST", "PUT"] and request.url.path.startswith("/api/"):
                    content_type = request.headers.get("content-type", "")
                    # For JSON endpoints, ensure proper content type
                    if ("/agent/query" in request.url.path or "/agent/troubleshoot" in request.url.path or 
                        "/knowledge/search" in request.url.path):
                        if content_type and not content_type.startswith("application/json") and not content_type.startswith("multipart/form-data"):
                            return JSONResponse(
                                status_code=422,
                                content={"detail": "Content-Type must be application/json"}
                            )
                
                response = await call_next(request)
                return response
            
        except ImportError:
            # If routes can't be imported, add mock endpoints
            @app.post("/api/v1/agent/query")
            async def mock_agent_query():
                return {"status": "mock_response"}
            
            @app.post("/api/v1/data/upload") 
            async def mock_data_upload():
                return {"status": "mock_response"}
            
        return app

    @pytest.fixture
    async def client(simple_app):
        """Create async HTTP client for testing."""
        transport = ASGITransport(app=simple_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    @pytest.fixture
    def test_session():
        """Provide a test session ID."""
        return "test_session_12345"

    @pytest.fixture
    def response_validator():
        """Provide response validation utilities."""
        class ResponseValidator:
            def assert_valid_response(self, data: Dict[str, Any]):
                assert isinstance(data, dict)
                
            def assert_valid_upload_response(self, data: Dict[str, Any]):
                """Validate data upload response structure."""
                assert isinstance(data, dict)
                required_fields = [
                    "data_id", "session_id", "data_type", "processing_status", "insights"
                ]
                
                for field in required_fields:
                    assert field in data, f"Missing required field: {field}"
                
                assert isinstance(data["insights"], dict)
                assert "processing_time_ms" in data["insights"]
                
            def assert_valid_troubleshooting_response(self, data: Dict[str, Any]):
                """Validate troubleshooting response structure."""
                assert isinstance(data, dict)
                required_fields = [
                    "session_id", "investigation_id", "status", 
                    "findings", "recommendations", "confidence_score"
                ]
                
                for field in required_fields:
                    assert field in data, f"Missing required field: {field}"
                
                assert isinstance(data["findings"], list)
                assert isinstance(data["recommendations"], list) 
                assert 0.0 <= data["confidence_score"] <= 1.0
            
            def assert_valid_session_response(self, data: Dict[str, Any]):
                """Validate session response structure."""
                assert isinstance(data, dict)
                required_fields = ["session_id", "created_at"]
                
                for field in required_fields:
                    assert field in data, f"Missing required field: {field}"
        
        return ResponseValidator()

    @pytest.fixture
    def performance_tracker():
        """Provide performance tracking utilities."""
        class PerformanceTracker:
            def __init__(self):
                self.timings = {}
            
            def time_request(self, operation_name: str):
                from contextlib import contextmanager
                import time
                
                @contextmanager
                def timer():
                    start_time = time.time()
                    try:
                        yield
                    finally:
                        duration = time.time() - start_time
                        self.timings[operation_name] = duration
                
                return timer()
            
            def assert_performance_target(self, operation_name: str, target_seconds: float):
                duration = self.timings.get(operation_name, 0)
                assert duration <= target_seconds, f"Operation {operation_name} took {duration}s, expected <= {target_seconds}s"
            
            def get_summary(self):
                """Get performance summary."""
                return {
                    "total_operations": len(self.timings),
                    "total_time": sum(self.timings.values()),
                    "average_time": sum(self.timings.values()) / len(self.timings) if self.timings else 0,
                    "operations": self.timings.copy()
                }
        
        return PerformanceTracker()

    @pytest.fixture
    def sample_log_file():
        """Provide sample log file for testing."""
        content = b"""2024-01-01 12:00:01 INFO Application started
2024-01-01 12:00:02 ERROR Database connection failed
2024-01-01 12:00:03 WARN Retrying connection
2024-01-01 12:00:04 INFO Connection restored
"""
        return ("app.log", content, "text/plain")

    @pytest.fixture
    def sample_error_file():
        """Provide sample error trace for testing."""
        content = b"""Traceback (most recent call last):
  File "main.py", line 42, in process_request
    result = database.query(sql)
  File "db.py", line 15, in query
    return connection.execute(sql)
ConnectionError: Database connection timeout after 30 seconds
"""
        return ("error.trace", content, "text/plain")
    
    @pytest.fixture
    def sample_document():
        """Provide sample document for knowledge base testing."""
        content = b"""# Database Connection Troubleshooting Guide

## Overview
This guide covers common database connection issues and their solutions.

## Common Issues

### Connection Pool Exhaustion
When the connection pool is exhausted, applications may experience timeouts.

**Symptoms:**
- Connection timeout errors
- Slow application response
- Database connection refused errors

**Solutions:**
1. Increase connection pool size
2. Implement connection pooling best practices
3. Monitor connection usage patterns
4. Add connection retry logic

### Network Connectivity Problems
Network issues can cause intermittent database connections.

**Troubleshooting Steps:**
1. Check network connectivity between application and database
2. Verify firewall rules allow database connections
3. Test DNS resolution for database hostname
4. Monitor network latency and packet loss

### Authentication Issues
Incorrect credentials or expired certificates can prevent connections.

**Common Causes:**
- Expired passwords
- Invalid usernames
- Certificate problems
- Permission issues

## Best Practices
- Always use connection pooling
- Implement proper error handling
- Monitor database performance
- Regular security audits
"""
        return ("database_troubleshooting.md", content, "text/markdown")

else:
    # If dependencies aren't available, create empty placeholder fixtures
    def test_doubles():
        return {}
    
    def simple_app():
        return Mock()
    
    def client():
        return Mock()
    
    def test_session():
        return "test_session"
    
    def response_validator():
        return Mock()
    
    def performance_tracker():
        return Mock()
    
    def sample_log_file():
        return ("test.log", b"test content", "text/plain")
    
    def sample_error_file():
        return ("test.err", b"test error", "text/plain")