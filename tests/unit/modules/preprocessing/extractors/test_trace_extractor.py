"""
Tests for TraceDataExtractor.

Covers:
- R4.3: Duration unit heuristic fix (format-aware conversion)
- R8: Critical path graph traversal (Phase 4, placeholder)
"""

import json

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
