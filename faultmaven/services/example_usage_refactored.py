"""Example Usage for DataServiceRefactored - Phase 3.2

This file demonstrates how to use the refactored DataService with interface dependencies.
This is for documentation and testing purposes only.

The refactored service shows how dependency injection through interfaces
improves testability and modularity.
"""

from typing import Optional
from faultmaven.services.data_service_refactored import (
    DataServiceRefactored,
    DataClassifierAdapter,
    LogProcessorAdapter,
    SimpleStorageBackend,
)
from faultmaven.models import DataType


# Example mock implementations for demonstration
class MockSanitizer:
    """Mock sanitizer for example usage"""
    
    def sanitize(self, data):
        """Simple sanitization - in real implementation would use ISanitizer"""
        if isinstance(data, str):
            return data.replace("password=secret", "password=***")
        return data


class MockTracer:
    """Mock tracer for example usage"""
    
    def trace(self, operation: str):
        """Simple trace context manager"""
        from contextlib import contextmanager
        
        @contextmanager
        def trace_context():
            print(f"Starting trace: {operation}")
            yield
            print(f"Completed trace: {operation}")
        
        return trace_context()


# Example concrete implementations (these would be replaced in Phase 4)
class ExampleDataClassifier:
    """Example classifier implementation"""
    
    async def classify(self, content: str, filename: Optional[str] = None) -> DataType:
        """Simple classification logic"""
        content_lower = content.lower()
        
        if "error" in content_lower or "exception" in content_lower:
            return DataType.ERROR_MESSAGE
        elif "traceback" in content_lower:
            return DataType.STACK_TRACE
        elif filename and ".log" in filename.lower():
            return DataType.LOG_FILE
        else:
            return DataType.UNKNOWN


class ExampleLogProcessor:
    """Example log processor implementation"""
    
    async def process(self, content: str, data_type: Optional[DataType] = None):
        """Simple processing logic"""
        lines = content.split('\n')
        error_count = sum(1 for line in lines if 'error' in line.lower())
        warning_count = sum(1 for line in lines if 'warning' in line.lower())
        
        return {
            "line_count": len(lines),
            "error_count": error_count,
            "warning_count": warning_count,
            "data_type_processed": data_type.value if data_type else "unknown",
        }


async def example_usage():
    """
    Example of how to use DataServiceRefactored with interface dependencies
    
    This shows the dependency injection pattern and how the service
    works with interface abstractions.
    """
    
    # Create concrete implementations (these would be injected in real app)
    classifier = ExampleDataClassifier()
    processor = ExampleLogProcessor()
    sanitizer = MockSanitizer()
    tracer = MockTracer()
    storage = SimpleStorageBackend()
    
    # Create adapters until Phase 4 refactoring
    classifier_adapter = DataClassifierAdapter(classifier)
    processor_adapter = LogProcessorAdapter(processor)
    
    # Create the refactored service with interface dependencies
    data_service = DataServiceRefactored(
        data_classifier=classifier_adapter,
        log_processor=processor_adapter,
        sanitizer=sanitizer,
        tracer=tracer,
        storage_backend=storage,
    )
    
    # Example usage
    print("=== DataServiceRefactored Example Usage ===")
    
    # 1. Ingest data
    sample_log = """
    2024-01-15 10:30:15 INFO Starting application
    2024-01-15 10:30:16 ERROR Database connection failed
    2024-01-15 10:30:17 WARNING Retrying connection
    2024-01-15 10:30:18 INFO Connection restored
    """
    
    uploaded_data = await data_service.ingest_data(
        content=sample_log,
        session_id="test_session_123",
        file_name="app.log",
    )
    
    print(f"✅ Ingested data: {uploaded_data.data_id}")
    print(f"   Data type: {uploaded_data.data_type.value}")
    print(f"   File size: {uploaded_data.file_size} bytes")
    
    # 2. Analyze data  
    insights = await data_service.analyze_data(
        data_id=uploaded_data.data_id,
        session_id="test_session_123",
    )
    
    print(f"✅ Analysis complete: {insights.confidence_score:.2f} confidence")
    print(f"   Processing time: {insights.processing_time_ms}ms")
    print(f"   Anomalies found: {len(insights.anomalies_detected)}")
    print(f"   Recommendations: {len(insights.recommendations)}")
    
    # 3. Batch processing
    batch_items = [
        ("Error: Connection timeout", "error1.log"),
        ("Warning: High memory usage", "warning1.log"),
    ]
    
    batch_results = await data_service.batch_process(
        data_items=batch_items,
        session_id="test_session_123",
    )
    
    print(f"✅ Batch processed: {len(batch_results)} items")
    
    return data_service


if __name__ == "__main__":
    # This example shows the interface-based design
    print("DataServiceRefactored demonstrates:")
    print("✅ Interface-based dependency injection")
    print("✅ Improved testability through abstraction")
    print("✅ Better separation of concerns")
    print("✅ Comprehensive error handling")
    print("✅ Consistent sanitization via interface")
    print("✅ Proper distributed tracing integration")
    print()
    print("Run this example with: python -m asyncio example_usage_refactored.py")