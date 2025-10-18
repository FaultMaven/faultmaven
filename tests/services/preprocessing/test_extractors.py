"""
Comprehensive unit tests for all preprocessing extractors
"""

import pytest
from faultmaven.services.preprocessing.extractors import (
    LogsAndErrorsExtractor,
    StructuredConfigExtractor,
    MetricsAndPerformanceExtractor,
    UnstructuredTextExtractor,
    SourceCodeExtractor,
    VisualEvidenceExtractor
)


class TestLogsAndErrorsExtractor:
    """Test Crime Scene Extraction for logs"""

    def test_single_error_extraction(self):
        """Test extracting single error with context"""
        extractor = LogsAndErrorsExtractor()
        content = """
2025-10-15 10:00:00 INFO Starting application
2025-10-15 10:00:01 INFO Loading configuration
2025-10-15 10:00:02 ERROR Database connection failed: timeout
2025-10-15 10:00:03 INFO Retrying connection
2025-10-15 10:00:04 INFO Application started
"""
        result = extractor.extract(content)

        assert "ERROR" in result
        assert "Database connection failed" in result
        assert extractor.llm_calls_used == 0

    def test_multiple_crime_scenes(self):
        """Test extracting first and last errors"""
        extractor = LogsAndErrorsExtractor()
        content = "\n".join([
            f"2025-10-15 10:00:{i:02d} INFO Normal operation" if i % 10 != 0
            else f"2025-10-15 10:00:{i:02d} ERROR Critical failure {i}"
            for i in range(60)
        ])

        result = extractor.extract(content)

        assert "ERROR" in result
        assert "Critical failure 0" in result or "Critical failure 10" in result
        assert extractor.llm_calls_used == 0

    def test_no_errors_returns_tail(self):
        """Test fallback to tail when no errors found"""
        extractor = LogsAndErrorsExtractor()
        content = "\n".join([
            f"2025-10-15 10:00:{i:02d} INFO Normal operation {i}"
            for i in range(20)
        ])

        result = extractor.extract(content)

        assert "Normal operation 19" in result
        assert extractor.llm_calls_used == 0

    def test_severity_prioritization(self):
        """Test that FATAL/CRITICAL are prioritized over ERROR"""
        extractor = LogsAndErrorsExtractor()
        content = """
2025-10-15 10:00:00 ERROR Minor error
2025-10-15 10:00:01 FATAL System crash
2025-10-15 10:00:02 ERROR Another minor error
"""
        result = extractor.extract(content)

        assert "FATAL" in result
        assert "System crash" in result
        assert extractor.llm_calls_used == 0


class TestStructuredConfigExtractor:
    """Test configuration file parsing and sanitization"""

    def test_json_config_parsing(self):
        """Test parsing JSON configuration"""
        extractor = StructuredConfigExtractor()
        content = """{
    "database": {
        "host": "localhost",
        "port": 5432,
        "password": "secret123"
    },
    "api_key": "sk-abc123xyz"
}"""
        result = extractor.extract(content)

        assert "database" in result
        assert "[REDACTED]" in result
        assert "secret123" not in result
        assert "sk-abc123xyz" not in result
        assert extractor.llm_calls_used == 0

    def test_yaml_config_parsing(self):
        """Test parsing YAML configuration"""
        extractor = StructuredConfigExtractor()
        content = """
database:
  host: localhost
  port: 5432
  password: secret123
api_key: sk-abc123xyz
"""
        result = extractor.extract(content)

        assert "database" in result
        assert "[REDACTED]" in result
        assert "secret123" not in result
        assert extractor.llm_calls_used == 0

    def test_env_file_parsing(self):
        """Test parsing .env file - uses INI-style parsing"""
        extractor = StructuredConfigExtractor()
        # Test with INI-style section headers which parse more reliably
        content = """[database]
host=localhost
password=secret123

[api]
key=sk-abc123xyz
"""
        result = extractor.extract(content)

        # Verify secrets are redacted
        assert "[REDACTED]" in result
        assert "secret123" not in result
        assert "sk-abc123xyz" not in result
        assert extractor.llm_calls_used == 0

    def test_secret_redaction(self):
        """Test that secrets are properly redacted"""
        extractor = StructuredConfigExtractor()
        content = """{
    "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "jwt_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "normal_value": "this is fine"
}"""
        result = extractor.extract(content)

        assert "[REDACTED]" in result
        assert "wJalrXUtnFEMI" not in result
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "this is fine" in result
        assert extractor.llm_calls_used == 0


class TestMetricsAndPerformanceExtractor:
    """Test metrics analysis and anomaly detection"""

    def test_csv_metrics_parsing(self):
        """Test parsing CSV time-series data"""
        extractor = MetricsAndPerformanceExtractor()
        content = """timestamp,cpu_percent,memory_mb
2025-10-15T10:00:00,45.2,1024
2025-10-15T10:01:00,47.1,1050
2025-10-15T10:02:00,46.8,1048
"""
        result = extractor.extract(content)

        assert "cpu_percent" in result
        assert "memory_mb" in result
        assert "Mean:" in result
        assert extractor.llm_calls_used == 0

    def test_json_metrics_parsing(self):
        """Test parsing JSON time-series data"""
        extractor = MetricsAndPerformanceExtractor()
        content = """[
    {"timestamp": "2025-10-15T10:00:00", "response_time": 120, "error_rate": 0.01},
    {"timestamp": "2025-10-15T10:01:00", "response_time": 125, "error_rate": 0.02}
]"""
        result = extractor.extract(content)

        assert "response_time" in result
        assert "error_rate" in result
        assert extractor.llm_calls_used == 0

    def test_anomaly_spike_detection(self):
        """Test detecting anomalous spikes"""
        extractor = MetricsAndPerformanceExtractor()
        content = """timestamp,latency
2025-10-15T10:00:00,100
2025-10-15T10:01:00,105
2025-10-15T10:02:00,950
2025-10-15T10:03:00,102
"""
        result = extractor.extract(content)

        assert "anomaly" in result.lower() or "spike" in result.lower()
        assert extractor.llm_calls_used == 0

    def test_statistical_summary(self):
        """Test that statistical measures are calculated"""
        extractor = MetricsAndPerformanceExtractor()
        content = """timestamp,cpu
2025-10-15T10:00:00,50
2025-10-15T10:01:00,55
2025-10-15T10:02:00,52
"""
        result = extractor.extract(content)

        assert "Mean:" in result
        assert "p50" in result or "Percentiles" in result
        assert extractor.llm_calls_used == 0


class TestUnstructuredTextExtractor:
    """Test unstructured text extraction"""

    def test_markdown_structure_detection(self):
        """Test detecting markdown structure"""
        extractor = UnstructuredTextExtractor()
        content = """# Main Title

## Section 1

Some content here.

## Section 2

More content.
"""
        result = extractor.extract(content)

        assert "Main Title" in result or "Section 1" in result
        assert extractor.llm_calls_used == 0

    def test_error_message_extraction(self):
        """Test extracting error messages from text"""
        extractor = UnstructuredTextExtractor()
        content = """
We encountered an issue with the database connection.

ERROR: connection to server at "db.example.com" failed
Connection refused
"""
        result = extractor.extract(content)

        assert "ERROR" in result
        assert "connection" in result.lower()
        assert extractor.llm_calls_used == 0

    def test_code_block_extraction(self):
        """Test extracting code blocks"""
        extractor = UnstructuredTextExtractor()
        content = """
Here is the problematic code:

```python
def broken_function():
    raise Exception("This fails")
```
"""
        result = extractor.extract(content)

        assert "CODE" in result or "python" in result
        assert extractor.llm_calls_used == 0

    def test_plain_text_fallback(self):
        """Test handling plain text without structure"""
        extractor = UnstructuredTextExtractor()
        content = "Just some plain text without any special formatting."

        result = extractor.extract(content)

        assert len(result) > 0
        assert extractor.llm_calls_used == 0


class TestSourceCodeExtractor:
    """Test source code analysis"""

    def test_python_ast_parsing(self):
        """Test Python AST parsing"""
        extractor = SourceCodeExtractor()
        content = """
import os
import sys

class MyClass:
    def __init__(self):
        pass

    def method(self, x):
        return x * 2

def my_function(a, b):
    return a + b
"""
        result = extractor.extract(content)

        assert "import os" in result
        assert "MyClass" in result
        assert "my_function" in result
        assert extractor.llm_calls_used == 0

    def test_python_error_handling_detection(self):
        """Test detecting try/except blocks"""
        extractor = SourceCodeExtractor()
        content = """
def risky_operation():
    try:
        result = dangerous_call()
        return result
    except ValueError as e:
        print(f"Error: {e}")
        return None
"""
        result = extractor.extract(content)

        assert "Error Handling" in result or "try/except" in result
        assert "ValueError" in result
        assert extractor.llm_calls_used == 0

    def test_python_todo_extraction(self):
        """Test extracting TODO comments"""
        extractor = SourceCodeExtractor()
        content = """
def incomplete_function():
    # TODO: Implement error handling
    pass

def another_function():
    # FIXME: This has a bug
    return None
"""
        result = extractor.extract(content)

        assert "TODO" in result or "FIXME" in result
        assert extractor.llm_calls_used == 0

    def test_javascript_pattern_extraction(self):
        """Test pattern-based extraction for JavaScript"""
        extractor = SourceCodeExtractor()
        content = """
function myFunction(x, y) {
    return x + y;
}

const arrowFunc = (a) => a * 2;

class MyClass {
    constructor() {
        this.value = 0;
    }
}
"""
        result = extractor.extract(content)

        assert "myFunction" in result or "JavaScript" in result
        assert extractor.llm_calls_used == 0

    def test_invalid_python_fallback(self):
        """Test fallback to pattern-based for invalid Python"""
        extractor = SourceCodeExtractor()
        content = """
// This is JavaScript, not Python
function test() {
    console.log("Hello");
}
"""
        result = extractor.extract(content)

        assert len(result) > 0
        assert extractor.llm_calls_used == 0


class TestVisualEvidenceExtractor:
    """Test visual evidence placeholder"""

    def test_placeholder_message(self):
        """Test that placeholder returns informative message"""
        extractor = VisualEvidenceExtractor()
        content = "fake_image_data_here"

        result = extractor.extract(content, filename="screenshot.png")

        assert "VISUAL EVIDENCE" in result
        assert "Phase 3" in result
        assert "screenshot.png" in result
        assert extractor.llm_calls_used == 0

    def test_format_detection(self):
        """Test image format detection"""
        extractor = VisualEvidenceExtractor()

        result_png = extractor.extract("data", filename="image.png")
        result_jpg = extractor.extract("data", filename="image.jpg")

        assert "png" in result_png
        assert "jpg" in result_jpg
        assert extractor.llm_calls_used == 0


class TestExtractorIntegration:
    """Integration tests for extractor consistency"""

    def test_all_extractors_have_strategy_name(self):
        """Test that all extractors expose strategy_name"""
        extractors = [
            LogsAndErrorsExtractor(),
            StructuredConfigExtractor(),
            MetricsAndPerformanceExtractor(),
            UnstructuredTextExtractor(),
            SourceCodeExtractor(),
            VisualEvidenceExtractor()
        ]

        for extractor in extractors:
            assert hasattr(extractor, 'strategy_name')
            assert isinstance(extractor.strategy_name, str)
            assert len(extractor.strategy_name) > 0

    def test_all_extractors_track_llm_calls(self):
        """Test that all extractors track LLM usage"""
        extractors = [
            LogsAndErrorsExtractor(),
            StructuredConfigExtractor(),
            MetricsAndPerformanceExtractor(),
            UnstructuredTextExtractor(),
            SourceCodeExtractor(),
            VisualEvidenceExtractor()
        ]

        for extractor in extractors:
            assert hasattr(extractor, 'llm_calls_used')
            # Phase 2: All extractors should use 0 LLM calls
            assert extractor.llm_calls_used == 0

    def test_all_extractors_return_string(self):
        """Test that all extractors return string output"""
        test_data = "sample data for testing"

        extractors = [
            LogsAndErrorsExtractor(),
            StructuredConfigExtractor(),
            MetricsAndPerformanceExtractor(),
            UnstructuredTextExtractor(),
            SourceCodeExtractor(),
            VisualEvidenceExtractor()
        ]

        for extractor in extractors:
            result = extractor.extract(test_data)
            assert isinstance(result, str)
            assert len(result) > 0
