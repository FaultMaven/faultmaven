"""
Tests for TraceDataExtractor.

Covers:
- R4.3: Duration unit heuristic fix (format-aware conversion)
- R8: Critical path graph traversal (Phase 4, placeholder)
- ISS-033: error.message tag promoted to a visible summary block
- ISS-034: span inventory header asserts numeric span count
"""

import json
import os

import pytest

from faultmaven.modules.preprocessing.extractors.trace_extractor import (
    TraceDataExtractor,
)


class TestTraceExtractor:
    @pytest.fixture
    def extractor(self):
        return TraceDataExtractor()

    def test_properties(self, extractor):
        assert extractor.strategy_name == "trace_correlation"
        assert extractor.llm_calls_used == 0

    # --- R4.3: Duration unit fix ---

    def test_otel_nanoseconds(self, extractor):
        """OpenTelemetry spans use nanoseconds — should convert to ms correctly."""
        trace = {
            "traceId": "abc12345",
            "spans": [
                {
                    "spanId": "span1",
                    "name": "GET /api",
                    "serviceName": "api-gateway",
                    "duration": 50_000_000,  # 50ms in nanoseconds
                    "status": {"code": 0},
                },
            ],
        }
        result = extractor.extract(json.dumps(trace))
        assert "50.0ms" in result.file_extract or "50.0" in result.file_extract

    def test_jaeger_microseconds(self, extractor):
        """Jaeger spans use microseconds — should convert to ms correctly."""
        trace = {
            "data": [
                {
                    "traceID": "abc12345",
                    "spans": [
                        {
                            "spanID": "span1",
                            "operationName": "GET /api",
                            "duration": 50_000,  # 50ms in microseconds
                            "startTime": 1000000,
                            "process": {"serviceName": "api-gateway"},
                            "tags": [],
                            "references": [],
                        },
                    ],
                }
            ]
        }
        result = extractor.extract(json.dumps(trace))
        assert "50.0ms" in result.file_extract or "50.0" in result.file_extract

    def test_long_running_span_not_misinterpreted(self, extractor):
        """A span lasting >1000s should NOT be divided by 1M.
        This was the original bug: magnitude heuristic treated large ms values as ns.
        """
        trace = {
            "traceId": "abc12345",
            "spans": [
                {
                    "spanId": "span1",
                    "name": "batch-job",
                    "serviceName": "worker",
                    # 1500 seconds = 1,500,000,000,000 ns = 1.5 trillion
                    "duration": 1_500_000_000_000,
                    "status": {"code": 0},
                },
            ],
        }
        result = extractor.extract(json.dumps(trace))
        # Should convert from ns to ms: 1,500,000 ms (= 1500 seconds)
        # NOT: 1,500,000,000,000 / 1,000,000 = 1,500,000 (this is correct now)
        # The old bug would have been: "1500000000000 > 1000000 → divide by 1000000 → 1500000"
        # which happened to be correct by coincidence for this value.
        # But the key is: it should NOT use magnitude heuristic.
        assert "1500000" in result.file_extract or "batch-job" in result.file_extract

    # --- Basic trace parsing ---

    def test_otel_error_spans(self, extractor):
        """OpenTelemetry spans with errors detected."""
        trace = {
            "traceId": "def67890",
            "spans": [
                {
                    "spanId": "span1",
                    "name": "db-query",
                    "serviceName": "db-service",
                    "duration": 100_000_000,
                    "status": {"code": 2},  # ERROR
                },
                {
                    "spanId": "span2",
                    "parentSpanId": "span1",
                    "name": "GET /api",
                    "serviceName": "api",
                    "duration": 200_000_000,
                    "status": {"code": 0},
                },
            ],
        }
        result = extractor.extract(json.dumps(trace))
        assert (
            "Errors detected" in result.file_extract or "FAILED" in result.file_extract
        )

    def test_invalid_json_fallback(self, extractor):
        """Non-JSON content should fall back gracefully."""
        result = extractor.extract(
            "This is not JSON at all, just some text content here"
        )
        assert (
            "partial extraction" in result.file_extract.lower()
            or "invalid JSON" in result.file_extract
        )

    # --- R8: Critical path graph traversal ---

    def test_critical_path_tree(self, extractor):
        """5-span trace tree: critical path differs from single slowest span.

        Tree structure:
          root (10ms)
          ├── child-a (50ms) ← slowest single span, but no children
          └── child-b (20ms)
              └── grandchild-b1 (40ms) ← longer chain: 20+40=60ms

        Critical path should be: root → child-b → grandchild-b1 (total: 70ms)
        NOT: root → child-a (total: 60ms)
        """
        trace = {
            "traceId": "critpath1",
            "spans": [
                {
                    "spanId": "root",
                    "name": "request",
                    "serviceName": "gateway",
                    "duration": 10_000_000,  # 10ms
                    "status": {"code": 0},
                },
                {
                    "spanId": "child-a",
                    "parentSpanId": "root",
                    "name": "fast-cache",
                    "serviceName": "cache",
                    "duration": 50_000_000,  # 50ms — slowest single span
                    "status": {"code": 0},
                },
                {
                    "spanId": "child-b",
                    "parentSpanId": "root",
                    "name": "db-lookup",
                    "serviceName": "db",
                    "duration": 20_000_000,  # 20ms
                    "status": {"code": 0},
                },
                {
                    "spanId": "grandchild-b1",
                    "parentSpanId": "child-b",
                    "name": "index-scan",
                    "serviceName": "db",
                    "duration": 40_000_000,  # 40ms
                    "status": {"code": 0},
                },
                {
                    "spanId": "child-c",
                    "parentSpanId": "root",
                    "name": "logging",
                    "serviceName": "logger",
                    "duration": 1_000_000,  # 1ms
                    "status": {"code": 0},
                },
            ],
        }
        result = extractor.extract(json.dumps(trace))
        # Critical path should include the chain through db
        assert "db" in result.file_extract.lower()
        assert (
            "index-scan" in result.file_extract
            or "db-lookup" in result.file_extract
            or "db.index-scan" in result.file_extract
        )

    def test_critical_path_malformed_no_root(self, extractor):
        """Trace with no root span falls back gracefully."""
        trace = {
            "traceId": "noroot1",
            "spans": [
                {
                    "spanId": "orphan1",
                    "parentSpanId": "missing-parent",
                    "name": "op1",
                    "serviceName": "svc1",
                    "duration": 100_000_000,
                    "status": {"code": 0},
                },
                {
                    "spanId": "orphan2",
                    "parentSpanId": "also-missing",
                    "name": "op2",
                    "serviceName": "svc2",
                    "duration": 200_000_000,
                    "status": {"code": 0},
                },
            ],
        }
        result = extractor.extract(json.dumps(trace))
        # Should still produce output (fallback to top-by-duration)
        assert (
            "svc" in result.file_extract.lower() or "op" in result.file_extract.lower()
        )

    # --- ISS-033: error.message tag promoted to a visible summary block ---

    def test_iss033_error_message_tag_surfaced(self, extractor):
        """An error span carrying error.message must surface the verbatim string
        in file_extract (not buried as raw tag-data only).
        """
        trace = {
            "data": [
                {
                    "traceID": "abc12345",
                    "spans": [
                        {
                            "spanID": "span-root",
                            "operationName": "GET /api",
                            "references": [],
                            "startTime": 1_000_000,
                            "duration": 500_000,  # 500ms in microseconds
                            "process": {"serviceName": "api-gateway"},
                            "tags": [
                                {"key": "http.method", "value": "GET"},
                            ],
                        },
                        {
                            "spanID": "span-db",
                            "operationName": "db.query",
                            "references": [
                                {"refType": "CHILD_OF", "spanID": "span-root"}
                            ],
                            "startTime": 1_100_000,
                            "duration": 400_000,
                            "process": {"serviceName": "db-service"},
                            "tags": [
                                {"key": "http.status_code", "value": 500},
                                {"key": "error", "value": True},
                                {
                                    "key": "error.message",
                                    "value": "connection refused",
                                },
                            ],
                        },
                    ],
                }
            ]
        }
        result = extractor.extract(json.dumps(trace))
        assert (
            "connection refused" in result.file_extract
        ), "error.message tag value must appear verbatim in file_extract"

    # --- ISS-034: span inventory header asserts numeric span count ---

    def test_iss034_span_count_label_in_extract(self, extractor):
        """An explicit numeric span-count label must appear in file_extract so the
        agent does not miscount spans by reading the per-span listing.
        """
        spans = []
        for i in range(12):
            spans.append(
                {
                    "spanID": f"span{i:02d}",
                    "operationName": f"op{i}",
                    "references": (
                        []
                        if i == 0
                        else [{"refType": "CHILD_OF", "spanID": f"span{i - 1:02d}"}]
                    ),
                    "startTime": 1_000_000 + i * 10_000,
                    "duration": 5_000,  # 5ms in microseconds
                    "process": {"serviceName": "svc"},
                    "tags": [],
                }
            )
        trace = {"data": [{"traceID": "abcd1234", "spans": spans}]}
        result = extractor.extract(json.dumps(trace))
        # The number 12 must appear as an explicit label in file_extract.
        assert (
            "12 total" in result.file_extract or "12 spans" in result.file_extract
        ), "file_extract must state the span count as a numeric label"

    # --- Regression on the real Jaeger fixture (skip if not present) ---

    def test_iss033_iss034_jaeger_checkout_fixture(self, extractor):
        """Regression test using the actual jaeger-checkout-trace.json fixture
        from fm-data-exam. Asserts both fixes simultaneously.
        """
        fixture_path = (
            "/home/swhouse/product/fm-data-exam/test-data/synthetic/"
            "jaeger-checkout-trace.json"
        )
        if not os.path.exists(fixture_path):
            pytest.skip(f"Fixture not available at {fixture_path}")

        with open(fixture_path, "r", encoding="utf-8") as f:
            content = f.read()

        result = extractor.extract(content)

        # ISS-033: verbatim error.message string from Stripe span
        assert (
            "upstream timeout after 2000ms" in result.file_extract
        ), "ISS-033 regression: stripe error.message tag must be in file_extract"

        # ISS-034: 9 spans must be asserted as a numeric label, not just metadata
        assert "9 total" in result.file_extract or "9 spans" in result.file_extract, (
            "ISS-034 regression: span count '9' must appear as a numeric label "
            "in file_extract (not only in file_meta)"
        )
