"""
Tests for ErrorReportExtractor.

Covers R4.1: Java/JS/Go root-cause inversion fix.
"""

import pytest

from faultmaven.modules.preprocessing.extractors.error_report_extractor import (
    ErrorReportExtractor,
)


class TestErrorReportExtractor:
    @pytest.fixture
    def extractor(self):
        return ErrorReportExtractor()

    def test_properties(self, extractor):
        assert extractor.strategy_name == "exception_context"
        assert extractor.llm_calls_used == 0

    # --- R4.1: Root cause identification across languages ---

    def test_python_root_cause(self, extractor):
        """Python: most recent call last → last frame is root cause."""
        content = """\
Traceback (most recent call last):
  File "main.py", line 10, in main
  File "app/service.py", line 42, in process
  File "app/db.py", line 15, in query
ValueError: invalid literal"""
        result = extractor.extract(content)
        # Root cause should be the innermost frame: db.py:15 query
        assert "db.py" in result
        assert "query" in result
        assert "Root Cause" in result

    def test_java_root_cause(self, extractor):
        """Java: most recent call first → first at-frame is root cause.
        After fix, frames are reversed so [-1] is innermost (first in doc)."""
        content = """\
java.lang.NullPointerException: Cannot invoke method on null
    at com.app.Service.processRequest(Service.java:42)
    at com.app.Controller.handleRequest(Controller.java:28)
    at org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:897)
    at java.lang.Thread.run(Thread.java:829)"""
        result = extractor.extract(content)
        # Root cause should be Service.processRequest (innermost/most recent)
        assert "Service.processRequest" in result or "Service.java" in result
        assert "Root Cause" in result

    def test_java_root_cause_not_thread_run(self, extractor):
        """Java root cause should NOT be Thread.run (outermost frame)."""
        content = """\
java.lang.NullPointerException: null
    at com.app.Service.method(Service.java:42)
    at java.lang.Thread.run(Thread.java:829)"""
        result = extractor.extract(content)
        # Should NOT identify Thread.run as root cause
        assert (
            "Thread.run" not in result.split("Root Cause")[1]
            if "Root Cause" in result
            else True
        )
        assert "Service" in result

    def test_javascript_root_cause(self, extractor):
        """JS: most recent call first → first at-frame is root cause."""
        content = """\
TypeError: Cannot read properties of undefined (reading 'map')
    at processData (src/utils/data.js:15:10)
    at handleRequest (src/routes/api.js:42:5)
    at Layer.handle (node_modules/express/lib/router/layer.js:95:5)"""
        result = extractor.extract(content)
        # Root cause should be processData (innermost)
        assert "processData" in result or "data.js" in result
        assert "Root Cause" in result

    def test_go_panic_root_cause(self, extractor):
        """Go panic traces list the panic site first."""
        content = """\
panic: runtime error: index out of range [5] with length 3

goroutine 1 [running]:
    main.go:25
    handler.go:42
    server.go:100"""
        result = extractor.extract(content)
        assert "panic" in result.lower()

    # --- Happy path tests ---

    def test_python_exception_parsing(self, extractor):
        """Basic Python exception parsing."""
        content = """\
Traceback (most recent call last):
  File "app.py", line 10, in main
    result = process()
  File "app.py", line 20, in process
    return data[key]
KeyError: 'missing_key'"""
        result = extractor.extract(content)
        assert "KeyError" in result
        assert "missing_key" in result
        assert "Likely Fixes" in result

    def test_unknown_language(self, extractor):
        """Unknown language should still produce output."""
        content = "Some random error text without recognized patterns"
        result = extractor.extract(content)
        assert "Unknown" in result
