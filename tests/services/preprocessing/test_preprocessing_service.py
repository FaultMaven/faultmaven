"""
Integration tests for PreprocessingService
Tests the complete 4-step pipeline: Classification → Extraction → Sanitization
"""

import pytest
from faultmaven.services.preprocessing.classifier import DataClassifier
from faultmaven.services.preprocessing.extractors import (
    LogsAndErrorsExtractor,
    StructuredConfigExtractor,
    MetricsAndPerformanceExtractor,
    UnstructuredTextExtractor,
    SourceCodeExtractor,
    VisualEvidenceExtractor
)
from faultmaven.services.preprocessing.preprocessing_service import PreprocessingService
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.models.api import DataType


@pytest.fixture
def preprocessing_service():
    """Create fully configured PreprocessingService"""
    classifier = DataClassifier()
    sanitizer = DataSanitizer()

    return PreprocessingService(
        classifier=classifier,
        sanitizer=sanitizer,
        logs_extractor=LogsAndErrorsExtractor(),
        config_extractor=StructuredConfigExtractor(),
        metrics_extractor=MetricsAndPerformanceExtractor(),
        text_extractor=UnstructuredTextExtractor(),
        source_code_extractor=SourceCodeExtractor(),
        visual_extractor=VisualEvidenceExtractor()
    )


class TestPreprocessingServiceIntegration:
    """Test complete preprocessing pipeline"""

    def test_logs_and_errors_pipeline(self, preprocessing_service):
        """Test complete pipeline for log files"""
        content = """2025-10-15 10:00:00 INFO Application started
2025-10-15 10:00:01 INFO Loading configuration
2025-10-15 10:00:02 INFO Database initializing
2025-10-15 10:00:03 ERROR Database connection failed at 192.168.1.100
2025-10-15 10:00:04 ERROR Connection timeout after 30s
2025-10-15 10:00:05 INFO Retrying connection
2025-10-15 10:00:06 INFO Retry attempt 1
2025-10-15 10:00:07 ERROR Connection refused
"""
        result = preprocessing_service.preprocess('application.log', content)

        assert result.metadata.data_type == DataType.LOGS_AND_ERRORS
        assert result.metadata.extraction_strategy == "crime_scene"
        assert result.metadata.llm_calls_used == 0
        assert "ERROR" in result.content
        assert "192.168.1.100" not in result.content  # IP should be redacted
        assert "[IP_ADDRESS_REDACTED]" in result.content

    def test_structured_config_pipeline(self, preprocessing_service):
        """Test complete pipeline for config files"""
        content = """{
    "database": {
        "host": "localhost",
        "password": "secret123"
    },
    "api_key": "sk-abc123xyz"
}"""
        result = preprocessing_service.preprocess('config.json', content)

        assert result.metadata.data_type == DataType.STRUCTURED_CONFIG
        assert result.metadata.extraction_strategy == "direct"
        assert result.metadata.llm_calls_used == 0
        assert "[REDACTED]" in result.content
        assert "secret123" not in result.content
        assert "sk-abc123xyz" not in result.content

    def test_metrics_pipeline(self, preprocessing_service):
        """Test complete pipeline for metrics data"""
        content = """timestamp,cpu_percent,memory_mb
2025-10-15T10:00:00,45.2,1024
2025-10-15T10:01:00,47.1,1050
2025-10-15T10:02:00,95.3,1980
"""
        result = preprocessing_service.preprocess('metrics.csv', content)

        assert result.metadata.data_type == DataType.METRICS_AND_PERFORMANCE
        assert result.metadata.extraction_strategy == "statistical"
        assert result.metadata.llm_calls_used == 0
        assert "cpu_percent" in result.content
        assert "Mean:" in result.content

    def test_unstructured_text_pipeline(self, preprocessing_service):
        """Test complete pipeline for unstructured text"""
        content = """# Troubleshooting Guide

## Issue Description
The application crashes with error code 500 when accessing user profile at john.doe@example.com.

## Steps Taken
1. Checked logs
2. Verified database connection
"""
        result = preprocessing_service.preprocess('issue.md', content)

        assert result.metadata.data_type == DataType.UNSTRUCTURED_TEXT
        assert result.metadata.extraction_strategy == "direct"
        assert result.metadata.llm_calls_used == 0
        assert "john.doe@example.com" not in result.content  # Email should be redacted
        assert "[EMAIL_REDACTED]" in result.content or "[REDACTED]" in result.content

    def test_source_code_pipeline(self, preprocessing_service):
        """Test complete pipeline for source code"""
        content = """
import os

def connect_db(password="secret123"):
    # TODO: Add connection pooling
    return db.connect(password=password)
"""
        result = preprocessing_service.preprocess('database.py', content)

        assert result.metadata.data_type == DataType.SOURCE_CODE
        assert result.metadata.extraction_strategy == "ast_parse"
        assert result.metadata.llm_calls_used == 0
        assert "connect_db" in result.content
        assert "TODO" in result.content

    def test_visual_evidence_pipeline(self, preprocessing_service):
        """Test pipeline for visual evidence (placeholder)"""
        result = preprocessing_service.preprocess('screenshot.png', 'fake_image_data')

        assert result.metadata.data_type == DataType.VISUAL_EVIDENCE
        assert result.metadata.extraction_strategy == "vision"
        assert result.metadata.llm_calls_used == 0
        assert "Phase 3" in result.content

    def test_agent_hint_overrides_classification(self, preprocessing_service):
        """Test that agent hints are respected"""
        # Content that looks like logs but agent says it's unstructured text
        content = """
Some error message here
ERROR: This is not actually a log file
Just documentation about errors
"""
        result = preprocessing_service.preprocess(
            'readme.txt',
            content,
            agent_hint=DataType.UNSTRUCTURED_TEXT
        )

        assert result.metadata.data_type == DataType.UNSTRUCTURED_TEXT
        assert result.metadata.source == "agent_hint"

    def test_browser_context_classification(self, preprocessing_service):
        """Test that browser context influences classification"""
        content = """{"data": [{"value": 123}, {"value": 456}]}"""

        result = preprocessing_service.preprocess(
            'data.json',
            content,
            browser_context="grafana dashboard"
        )

        assert result.metadata.data_type == DataType.METRICS_AND_PERFORMANCE
        assert result.metadata.source == "browser_context"

    def test_compression_metrics(self, preprocessing_service):
        """Test that preprocessing achieves compression"""
        # Large log file with repetitive content
        content = "\n".join([
            f"2025-10-15 10:00:{i:02d} INFO Normal operation" for i in range(100)
        ] + [
            "2025-10-15 10:01:00 ERROR Critical failure",
            "2025-10-15 10:01:01 FATAL System crash"
        ])

        result = preprocessing_service.preprocess('large.log', content)

        original_size = len(content)
        processed_size = len(result.content)
        compression_ratio = 1 - (processed_size / original_size)

        # Should achieve at least 50% compression via Crime Scene Extraction
        assert compression_ratio > 0.5
        assert result.metadata.original_size == original_size
        assert result.metadata.processed_size == processed_size

    def test_classification_confidence_threshold(self, preprocessing_service):
        """Test that low confidence triggers classification_failed"""
        # Ambiguous content that doesn't match any patterns
        content = "just some random text without clear structure"

        result = preprocessing_service.preprocess('unknown.txt', content)

        # Should default to UNSTRUCTURED_TEXT with low confidence
        if result.metadata.confidence < 0.60:
            assert result.metadata.classification_failed is True

    def test_zero_llm_calls_maintained(self, preprocessing_service):
        """Test that entire pipeline uses 0 LLM calls"""
        test_files = [
            ('app.log', '2025-10-15 ERROR: Test'),
            ('config.json', '{"key": "value"}'),
            ('metrics.csv', 'timestamp,value\n2025-10-15,100'),
            ('readme.md', '# Title\nContent here'),
            ('main.py', 'def main(): pass'),
            ('screenshot.png', 'fake_image_data')
        ]

        for filename, content in test_files:
            result = preprocessing_service.preprocess(filename, content)
            assert result.metadata.llm_calls_used == 0, f"Failed for {filename}"

    def test_metadata_completeness(self, preprocessing_service):
        """Test that all metadata fields are populated"""
        content = "2025-10-15 ERROR: Test error"
        result = preprocessing_service.preprocess('test.log', content)

        # Verify all required metadata fields
        assert result.metadata.data_type is not None
        assert result.metadata.extraction_strategy is not None
        assert result.metadata.llm_calls_used == 0
        assert result.metadata.confidence > 0
        assert result.metadata.source in ["user_override", "agent_hint", "browser_context", "rule_based"]
        assert result.metadata.processing_time_ms > 0

        assert result.original_size == len(content)
        assert result.processed_size == len(result.content)
        assert isinstance(result.security_flags, list)

    def test_sanitization_integrated(self, preprocessing_service):
        """Test that sanitization is applied to all data types"""
        test_cases = [
            ('app.log', '2025-10-15 ERROR at 192.168.1.100', '[IP_ADDRESS_REDACTED]'),
            ('config.json', '{"email": "user@example.com"}', '[EMAIL_REDACTED]'),
            ('readme.md', 'Contact: john.doe@company.com', '[EMAIL_REDACTED]'),
        ]

        for filename, content, expected_redaction in test_cases:
            result = preprocessing_service.preprocess(filename, content)
            # At least one form of redaction should be present
            has_redaction = (
                expected_redaction in result.content or
                '[REDACTED]' in result.content
            )
            assert has_redaction, f"No redaction found in {filename}"


class TestPreprocessingServiceEdgeCases:
    """Test edge cases and error handling"""

    def test_empty_content(self, preprocessing_service):
        """Test handling of empty content"""
        result = preprocessing_service.preprocess('empty.txt', '')

        assert result.content is not None
        assert result.metadata.llm_calls_used == 0

    def test_very_large_content(self, preprocessing_service):
        """Test handling of large content (should truncate)"""
        # Create content larger than MAX_OUTPUT_LENGTH
        large_content = "x" * 300000  # 300KB

        result = preprocessing_service.preprocess('large.txt', large_content)

        # Should be truncated
        assert len(result.content) < len(large_content)
        assert result.metadata.llm_calls_used == 0

    def test_malformed_json(self, preprocessing_service):
        """Test handling of malformed JSON"""
        content = '{"key": "value"'  # Missing closing brace

        result = preprocessing_service.preprocess('broken.json', content)

        # Should fallback to unstructured text or direct extraction
        assert result.content is not None
        assert result.metadata.llm_calls_used == 0

    def test_mixed_content_types(self, preprocessing_service):
        """Test file with mixed content (e.g., markdown with code blocks)"""
        content = """# Database Error

The following error occurred:

```python
def connect():
    raise Exception("Connection failed")
```

Error log:
2025-10-15 ERROR: Database timeout
"""
        result = preprocessing_service.preprocess('mixed.md', content)

        # Should classify as UNSTRUCTURED_TEXT
        assert result.metadata.data_type == DataType.UNSTRUCTURED_TEXT
        assert result.metadata.llm_calls_used == 0
        # Should extract both code and error messages
        assert "ERROR" in result.content or "Exception" in result.content
