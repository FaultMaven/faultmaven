"""Test file for DataServiceRefactored - Phase 3.2

This demonstrates how the interface-based design improves testability
by allowing easy mocking of dependencies.

Note: This is a simple test example. Production tests would use pytest fixtures
and more comprehensive test scenarios.
"""

import asyncio
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock
from contextlib import contextmanager

from faultmaven.services.data_service_refactored import DataServiceRefactored
from faultmaven.models import DataType, IDataClassifier, ILogProcessor, ISanitizer, ITracer, IStorageBackend


# Mock implementations for testing
class MockDataClassifier(IDataClassifier):
    """Mock data classifier for testing"""
    
    def __init__(self, return_type: DataType = DataType.LOG_FILE):
        self.return_type = return_type
        self.classify_calls = []
    
    async def classify(self, content: str, filename: Optional[str] = None) -> DataType:
        self.classify_calls.append((content, filename))
        return self.return_type


class MockLogProcessor(ILogProcessor):
    """Mock log processor for testing"""
    
    def __init__(self, return_data: Optional[Dict[str, Any]] = None):
        self.return_data = return_data or {"error_count": 5, "warning_count": 10}
        self.process_calls = []
    
    async def process(self, content: str, data_type: Optional[DataType] = None) -> Dict[str, Any]:
        self.process_calls.append((content, data_type))
        return self.return_data.copy()


class MockSanitizer(ISanitizer):
    """Mock sanitizer for testing"""
    
    def __init__(self):
        self.sanitize_calls = []
    
    def sanitize(self, data: Any) -> Any:
        self.sanitize_calls.append(data)
        if isinstance(data, str):
            return data.replace("sensitive", "***")
        return data


class MockTracer(ITracer):
    """Mock tracer for testing"""
    
    def __init__(self):
        self.traced_operations = []
    
    def trace(self, operation: str):
        self.traced_operations.append(operation)
        
        @contextmanager
        def trace_context():
            yield
            
        return trace_context()


class MockStorageBackend(IStorageBackend):
    """Mock storage backend for testing"""
    
    def __init__(self):
        self.storage = {}
        self.store_calls = []
        self.retrieve_calls = []
    
    async def store(self, key: str, data: Any) -> None:
        self.store_calls.append((key, data))
        self.storage[key] = data
    
    async def retrieve(self, key: str) -> Optional[Any]:
        self.retrieve_calls.append(key)
        return self.storage.get(key)


async def test_ingest_data_success():
    """Test successful data ingestion"""
    print("Testing data ingestion...")
    
    # Arrange
    classifier = MockDataClassifier(DataType.ERROR_MESSAGE)
    processor = MockLogProcessor()
    sanitizer = MockSanitizer()
    tracer = MockTracer()
    storage = MockStorageBackend()
    
    service = DataServiceRefactored(
        data_classifier=classifier,
        log_processor=processor,
        sanitizer=sanitizer,
        tracer=tracer,
        storage_backend=storage,
    )
    
    # Act
    result = await service.ingest_data(
        content="Error: Database connection failed with sensitive data",
        session_id="test_session",
        file_name="error.log",
    )
    
    # Assert
    assert result.data_type == DataType.ERROR_MESSAGE
    assert result.session_id == "test_session"
    assert result.file_name == "error.log"
    assert "sensitive" not in result.content  # Sanitized
    
    # Verify interface interactions
    assert len(classifier.classify_calls) == 1
    assert len(sanitizer.sanitize_calls) == 1
    assert len(storage.store_calls) == 1
    assert "data_service_refactored_ingest_data" in tracer.traced_operations
    
    print("✅ Data ingestion test passed")


async def test_analyze_data_success():
    """Test successful data analysis"""
    print("Testing data analysis...")
    
    # Arrange
    classifier = MockDataClassifier()
    processor = MockLogProcessor({"error_count": 15, "warning_count": 5})
    sanitizer = MockSanitizer()
    tracer = MockTracer()
    storage = MockStorageBackend()
    
    service = DataServiceRefactored(
        data_classifier=classifier,
        log_processor=processor,
        sanitizer=sanitizer,
        tracer=tracer,
        storage_backend=storage,
    )
    
    # First ingest some data
    uploaded_data = await service.ingest_data(
        content="Sample log content",
        session_id="test_session",
    )
    
    # Act
    result = await service.analyze_data(
        data_id=uploaded_data.data_id,
        session_id="test_session",
    )
    
    # Assert
    assert result.data_id == uploaded_data.data_id
    assert result.confidence_score > 0
    assert result.processing_time_ms >= 0
    assert result.insights["error_count"] == 15
    
    # Verify interface interactions
    assert len(processor.process_calls) == 1
    assert len(storage.retrieve_calls) == 1
    
    print("✅ Data analysis test passed")


async def test_batch_processing():
    """Test batch processing functionality"""
    print("Testing batch processing...")
    
    # Arrange
    classifier = MockDataClassifier()
    processor = MockLogProcessor()
    sanitizer = MockSanitizer()
    tracer = MockTracer()
    storage = MockStorageBackend()
    
    service = DataServiceRefactored(
        data_classifier=classifier,
        log_processor=processor,
        sanitizer=sanitizer,
        tracer=tracer,
        storage_backend=storage,
    )
    
    # Act
    batch_items = [
        ("Log entry 1", "log1.txt"),
        ("Log entry 2", "log2.txt"),
        ("Log entry 3", None),
    ]
    
    results = await service.batch_process(
        data_items=batch_items,
        session_id="test_session",
    )
    
    # Assert
    assert len(results) == 3
    assert all(result.session_id == "test_session" for result in results)
    assert len(classifier.classify_calls) == 3
    assert len(storage.store_calls) == 3
    
    print("✅ Batch processing test passed")


async def test_error_handling():
    """Test error handling scenarios"""
    print("Testing error handling...")
    
    # Arrange
    classifier = MockDataClassifier()
    processor = MockLogProcessor()
    sanitizer = MockSanitizer()
    tracer = MockTracer()
    storage = MockStorageBackend()
    
    service = DataServiceRefactored(
        data_classifier=classifier,
        log_processor=processor,
        sanitizer=sanitizer,
        tracer=tracer,
        storage_backend=storage,
    )
    
    # Test empty content
    try:
        await service.ingest_data(content="", session_id="test_session")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "empty" in str(e).lower()
        print("✅ Empty content validation passed")
    
    # Test empty session ID
    try:
        await service.ingest_data(content="test", session_id="")
        assert False, "Should have raised ValueError"  
    except ValueError as e:
        assert "session" in str(e).lower()
        print("✅ Empty session ID validation passed")
    
    # Test analysis without storage
    service_no_storage = DataServiceRefactored(
        data_classifier=classifier,
        log_processor=processor,
        sanitizer=sanitizer,
        tracer=tracer,
        storage_backend=None,
    )
    
    try:
        await service_no_storage.analyze_data("test_id", "test_session")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "storage" in str(e).lower()
        print("✅ No storage backend validation passed")


async def test_anomaly_detection():
    """Test anomaly detection logic"""
    print("Testing anomaly detection...")
    
    # Arrange
    classifier = MockDataClassifier(DataType.LOG_FILE)
    processor = MockLogProcessor({"error_count": 150, "warning_count": 600})  # Above thresholds
    sanitizer = MockSanitizer()
    tracer = MockTracer()
    storage = MockStorageBackend()
    
    service = DataServiceRefactored(
        data_classifier=classifier,
        log_processor=processor,
        sanitizer=sanitizer,
        tracer=tracer,
        storage_backend=storage,
    )
    
    # Ingest and analyze data
    uploaded_data = await service.ingest_data(
        content="Sample log with errors",
        session_id="test_session",
    )
    
    result = await service.analyze_data(
        data_id=uploaded_data.data_id,
        session_id="test_session",
    )
    
    # Assert anomalies were detected
    assert len(result.anomalies_detected) == 2  # Error spike + warning spike
    anomaly_types = [a["type"] for a in result.anomalies_detected]
    assert "error_spike" in anomaly_types
    assert "warning_spike" in anomaly_types
    
    # Verify recommendations are generated
    assert len(result.recommendations) > 0
    
    print("✅ Anomaly detection test passed")


async def run_all_tests():
    """Run all test scenarios"""
    print("=== Running DataServiceRefactored Tests ===\n")
    
    try:
        await test_ingest_data_success()
        await test_analyze_data_success()
        await test_batch_processing()
        await test_error_handling()
        await test_anomaly_detection()
        
        print("\n🎉 All tests passed!")
        print("\nKey benefits demonstrated:")
        print("✅ Easy mocking through interface abstractions")
        print("✅ Isolated testing of business logic")
        print("✅ Comprehensive error handling validation")
        print("✅ Traceability of interface interactions")
        print("✅ Improved testability vs concrete dependencies")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(run_all_tests())