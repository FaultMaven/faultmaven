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
        assert "db.py" in result.file_extract
        assert "query" in result.file_extract
        assert "Root Cause" in result.file_extract

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
        assert (
            "Service.processRequest" in result.file_extract
            or "Service.java" in result.file_extract
        )
        assert "Root Cause" in result.file_extract

    def test_java_root_cause_not_thread_run(self, extractor):
        """Java root cause should NOT be Thread.run (outermost frame)."""
        content = """\
java.lang.NullPointerException: null
    at com.app.Service.method(Service.java:42)
    at java.lang.Thread.run(Thread.java:829)"""
        result = extractor.extract(content)
        # Should NOT identify Thread.run as root cause
        assert (
            "Thread.run" not in result.file_extract.split("Root Cause")[1]
            if "Root Cause" in result.file_extract
            else True
        )
        assert "Service" in result.file_extract

    def test_javascript_root_cause(self, extractor):
        """JS: most recent call first → first at-frame is root cause."""
        content = """\
TypeError: Cannot read properties of undefined (reading 'map')
    at processData (src/utils/data.js:15:10)
    at handleRequest (src/routes/api.js:42:5)
    at Layer.handle (node_modules/express/lib/router/layer.js:95:5)"""
        result = extractor.extract(content)
        # Root cause should be processData (innermost)
        assert "processData" in result.file_extract or "data.js" in result.file_extract
        assert "Root Cause" in result.file_extract

    def test_go_panic_root_cause(self, extractor):
        """Go panic traces list the panic site first."""
        content = """\
panic: runtime error: index out of range [5] with length 3

goroutine 1 [running]:
    main.go:25
    handler.go:42
    server.go:100"""
        result = extractor.extract(content)
        assert "panic" in result.file_extract.lower()

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
        assert "KeyError" in result.file_extract
        assert "missing_key" in result.file_extract
        assert "Likely Fixes" in result.file_extract

    def test_unknown_language(self, extractor):
        """Unknown language should still produce output."""
        content = "Some random error text without recognized patterns"
        result = extractor.extract(content)
        assert "Unknown" in result.file_extract


class TestMultiBlockExtraction:
    """ISS-031: extractor must surface every top-level exception in a file.

    Before the fix, ``extract()`` ran the per-language parser on the whole
    file as a single unit, so only the first exception type/message survived
    and frames from later exceptions were silently merged into the first
    block's call path. Downstream questions about additional exceptions were
    answered with a fabricated denial.
    """

    @pytest.fixture
    def extractor(self):
        return ErrorReportExtractor()

    # --- Java multi-block ---

    def test_java_two_top_level_exceptions(self, extractor):
        """Two timestamped Java ERROR blocks → two exception sections."""
        content = """\
2024-01-15 10:23:47.842 ERROR [order-service-pool-3] c.acme.order.OrderController - Failed to process order
org.springframework.dao.DataIntegrityViolationException: could not execute statement
    at com.acme.order.service.OrderService.placeOrder(OrderService.java:142)
    at com.acme.order.OrderController.handlePlaceOrder(OrderController.java:78)

2024-01-15 10:23:51.103 ERROR [http-nio-8080-exec-7] c.acme.api.RequestFilter - Unhandled exception
java.lang.NullPointerException: Cannot invoke "SearchIndex.lookup" because "this.searchIndex" is null
    at com.acme.inventory.InventoryService.search(InventoryService.java:67)
    at com.acme.inventory.InventoryController.search(InventoryController.java:43)"""
        result = extractor.extract(content)

        # Header + count.
        assert "(2 exceptions detected)" in result.file_extract
        # Per-block sections present.
        assert "### Exception 1" in result.file_extract
        assert "### Exception 2" in result.file_extract
        # Both exception types reported.
        assert "DataIntegrityViolationException" in result.file_extract
        assert "NullPointerException" in result.file_extract
        # Both root causes appear (innermost frame for each block).
        assert "OrderService.java" in result.file_extract
        assert "InventoryService.java" in result.file_extract
        # file_meta is updated.
        assert result.file_meta["exception_count"] == 2
        assert "DataIntegrityViolationException" in result.file_meta["exception_types"]
        assert "NullPointerException" in result.file_meta["exception_types"]

    def test_java_real_fixture(self, extractor):
        """Run against the real fm-data-exam Java fixture verbatim."""
        content = (
            "2024-01-15 10:23:47.842 ERROR [order-service-pool-3] c.acme.order.OrderController - Failed to process order 4729183 for customer cust_abc_192\n"
            "org.springframework.dao.DataIntegrityViolationException: could not execute statement; SQL [n/a]; constraint [orders_status_check]; nested exception is org.hibernate.exception.ConstraintViolationException: could not execute statement\n"
            "\tat org.springframework.orm.jpa.vendor.HibernateJpaDialect.convertHibernateAccessException(HibernateJpaDialect.java:301)\n"
            "\tat com.acme.order.service.OrderService.placeOrder(OrderService.java:142)\n"
            "\tat com.acme.order.OrderController.handlePlaceOrder(OrderController.java:78)\n"
            "Caused by: org.hibernate.exception.ConstraintViolationException: could not execute statement\n"
            "\tat org.hibernate.exception.internal.SQLStateConversionDelegate.convert(SQLStateConversionDelegate.java:80)\n"
            'Caused by: org.postgresql.util.PSQLException: ERROR: new row for relation "orders" violates check constraint "orders_status_check"\n'
            "\tat org.postgresql.core.v3.QueryExecutorImpl.receiveErrorResponse(QueryExecutorImpl.java:2675)\n"
            "\n"
            "2024-01-15 10:23:51.103 ERROR [http-nio-8080-exec-7] c.acme.api.RequestFilter - Unhandled exception in request to /api/v1/inventory/search\n"
            'java.lang.NullPointerException: Cannot invoke "com.acme.inventory.SearchIndex.lookup(java.util.List)" because "this.searchIndex" is null\n'
            "\tat com.acme.inventory.InventoryService.search(InventoryService.java:67)\n"
            "\tat com.acme.inventory.InventoryController.search(InventoryController.java:43)\n"
        )
        result = extractor.extract(content)

        assert result.file_meta["exception_count"] == 2
        # Both exception types listed in metadata.
        types = result.file_meta["exception_types"]
        assert "DataIntegrityViolationException" in types
        assert "NullPointerException" in types
        # Both timestamps appear in section headers.
        assert "10:23:47.842" in result.file_extract
        assert "10:23:51.103" in result.file_extract
        # NPE message survives.
        assert "this.searchIndex" in result.file_extract

    # --- Python multi-block ---

    def test_python_three_top_level_tracebacks(self, extractor):
        """3 distinct tracebacks separated by ``---`` → 3 blocks. Chained
        ``During handling of the above exception`` tracebacks belong to
        the same block as the parent traceback."""
        content = """\
Traceback (most recent call last):
  File "/app/api/routers/report.py", line 112, in generate_report
    pdf_bytes = await renderer.render(context)
  File "/opt/venv/lib/python3.11/site-packages/jinja2/loaders.py", line 215, in get_source
    raise TemplateNotFound(template)
jinja2.exceptions.TemplateNotFound: incident_summary_v2.html

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/app/api/routers/report.py", line 119, in generate_report
    raise ReportGenerationError(f"Template not found") from exc
app.exceptions.ReportGenerationError: Template not found: incident_summary_v2.html

---

Traceback (most recent call last):
  File "/app/services/chunker.py", line 48, in chunk
    tokens = self.tokenizer.encode(text)
UnicodeEncodeError: 'utf-8' codec can't encode character '\\udce2' in position 1847: surrogates not allowed

---

Traceback (most recent call last):
  File "/app/workers/embedding_worker.py", line 44, in process_job
    embeddings = await self.embed_client.embed(chunks)
  File "/opt/venv/lib/python3.11/site-packages/httpx/_client.py", line 1513, in send
    response = await self._send_with_response(request)
httpx.ConnectTimeout: timed out connecting to http://embed-service:8080 after 30.0 seconds

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/app/workers/embedding_worker.py", line 52, in process_job
    raise EmbeddingServiceError(f"Embedding service unreachable: {exc}") from exc
app.exceptions.EmbeddingServiceError: Embedding service unreachable: timed out connecting to http://embed-service:8080 after 30.0 seconds
"""
        result = extractor.extract(content)

        assert "(3 exceptions detected)" in result.file_extract
        # All three top-level exception types referenced somewhere in the
        # output (either as wrapper or root in their respective blocks).
        assert (
            "TemplateNotFound" in result.file_extract
            or "ReportGenerationError" in result.file_extract
        )
        assert "UnicodeEncodeError" in result.file_extract
        assert (
            "ConnectTimeout" in result.file_extract
            or "EmbeddingServiceError" in result.file_extract
        )
        # Three section headings.
        assert "### Exception 1" in result.file_extract
        assert "### Exception 2" in result.file_extract
        assert "### Exception 3" in result.file_extract
        assert result.file_meta["exception_count"] == 3

    def test_python_dotted_namespace_exception_recognized(self, extractor):
        """Regression: ``app.exceptions.X`` and ``jinja2.exceptions.Y``
        previously fell through the ``\\w+`` Python ``exception_line``
        regex, leaving the block with ``exception_type='Unknown'``."""
        content = """\
Traceback (most recent call last):
  File "/app/api/routers/report.py", line 119, in generate_report
    raise ReportGenerationError("nope") from exc
app.exceptions.ReportGenerationError: Template not found: incident_summary_v2.html"""
        result = extractor.extract(content)
        assert "app.exceptions.ReportGenerationError" in result.file_extract
        assert "Unknown" not in result.file_meta["exception_types"]

    # --- Single-block regression ---

    def test_single_python_block_still_works(self, extractor):
        """Single-exception files must still produce a sensible summary —
        now with an explicit ``(1 exception detected)`` count header."""
        content = """\
Traceback (most recent call last):
  File "app.py", line 10, in main
  File "app/db.py", line 15, in query
ValueError: invalid literal"""
        result = extractor.extract(content)

        assert "(1 exception detected)" in result.file_extract
        assert "ValueError" in result.file_extract
        # No section headings for single-block output.
        assert "### Exception 1" not in result.file_extract
        assert result.file_meta["exception_count"] == 1
        assert result.file_meta["exception_types"] == ["ValueError"]
        # Backward-compat fields still populated.
        assert result.file_meta["exception_type"] == "ValueError"
        assert "root_cause" in result.file_meta

    def test_single_java_block_still_works(self, extractor):
        """Java single-exception (no leading timestamp) — falls back to
        the whole-content single-block path."""
        content = """\
java.lang.NullPointerException: null
    at com.app.Service.method(Service.java:42)
    at com.app.Controller.handle(Controller.java:28)
    at java.lang.Thread.run(Thread.java:829)"""
        result = extractor.extract(content)
        assert "(1 exception detected)" in result.file_extract
        assert "NullPointerException" in result.file_extract
        assert result.file_meta["exception_count"] == 1
        assert "### Exception 1" not in result.file_extract

    # --- Edge cases ---

    def test_go_two_panics(self, extractor):
        """Two ``panic:`` lines → two blocks."""
        content = """\
panic: runtime error: index out of range [5] with length 3

goroutine 1 [running]:
    main.go:25
    handler.go:42

panic: assignment to entry in nil map

goroutine 2 [running]:
    cache.go:10
    server.go:55"""
        result = extractor.extract(content)
        assert "(2 exceptions detected)" in result.file_extract
        assert "index out of range" in result.file_extract
        assert "assignment to entry in nil map" in result.file_extract
        assert result.file_meta["exception_count"] == 2

    def test_empty_content_short_circuit(self, extractor):
        """Empty / too-short content returns the standard empty response,
        bypassing block splitting entirely."""
        result = extractor.extract("")
        assert "[No content to analyze]" in result.file_extract
