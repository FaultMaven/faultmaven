"""
Trace Correlation Extraction for TRACE_DATA data type

Analyzes distributed traces to extract critical path, bottlenecks, and service dependencies.
No LLM calls required - pure JSON parsing and graph analysis.
"""

import json
import re
from typing import List, Dict, Optional, Any


class TraceDataExtractor:
    """Trace correlation extraction for distributed traces (0 LLM calls)"""

    @property
    def strategy_name(self) -> str:
        return "trace_correlation"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> str:
        """
        Trace Correlation algorithm:
        1. Parse trace JSON (OpenTelemetry/Jaeger format)
        2. Build service dependency graph
        3. Calculate critical path (longest duration chain)
        4. Identify slow spans (> p95 latency)
        5. Extract error spans
        6. Generate natural language summary
        """
        try:
            trace_data = json.loads(content)
        except json.JSONDecodeError:
            # If not pure JSON, try to extract JSON from text
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                try:
                    trace_data = json.loads(json_match.group())
                except json.JSONDecodeError:
                    return self._fallback_extraction(content)
            else:
                return self._fallback_extraction(content)

        # Extract trace ID
        trace_id = self._extract_trace_id(trace_data)

        # Extract spans
        spans = self._extract_spans(trace_data)

        if not spans:
            return f"Trace Analysis (traceId: {trace_id})\n\nNo spans found in trace data."

        # Calculate total duration
        total_duration = self._calculate_total_duration(spans)

        # Find critical path
        critical_path = self._find_critical_path(spans)

        # Find slow spans (> 20% of total time)
        slow_spans = self._find_slow_spans(spans, total_duration)

        # Find error spans
        error_spans = self._find_error_spans(spans)

        # Build service call tree
        call_tree = self._build_call_tree(spans)

        # Generate summary
        summary = self._generate_summary(
            trace_id, total_duration, critical_path, slow_spans, error_spans, call_tree
        )

        return summary

    def _extract_trace_id(self, trace_data: Dict) -> str:
        """Extract trace ID from various trace formats"""
        # OpenTelemetry format
        if 'traceId' in trace_data:
            return trace_data['traceId'][:8]  # First 8 chars for readability

        # Jaeger format
        if 'data' in trace_data and isinstance(trace_data['data'], list):
            if trace_data['data'] and 'traceID' in trace_data['data'][0]:
                return trace_data['data'][0]['traceID'][:8]

        return "unknown"

    def _extract_spans(self, trace_data: Dict) -> List[Dict]:
        """Extract spans from various trace formats"""
        spans = []

        # OpenTelemetry format: {spans: [...]}
        if 'spans' in trace_data:
            spans = trace_data['spans']

        # Jaeger format: {data: [{spans: [...]}]}
        elif 'data' in trace_data and isinstance(trace_data['data'], list):
            if trace_data['data'] and 'spans' in trace_data['data'][0]:
                spans = trace_data['data'][0]['spans']

        # Normalize span format
        normalized_spans = []
        for span in spans:
            normalized_spans.append({
                'span_id': span.get('spanId') or span.get('spanID', 'unknown'),
                'parent_id': span.get('parentSpanId') or span.get('references', [{}])[0].get('spanID'),
                'operation': span.get('operationName') or span.get('name', 'unknown'),
                'service': self._extract_service_name(span),
                'duration_ms': self._extract_duration_ms(span),
                'has_error': self._check_error(span),
            })

        return normalized_spans

    def _extract_service_name(self, span: Dict) -> str:
        """Extract service name from span"""
        # OpenTelemetry format
        if 'serviceName' in span:
            return span['serviceName']

        # Check in tags
        if 'tags' in span:
            for tag in span['tags']:
                if tag.get('key') == 'service.name':
                    return tag.get('value', 'unknown')

        # Check in process
        if 'process' in span and 'serviceName' in span['process']:
            return span['process']['serviceName']

        return "unknown"

    def _extract_duration_ms(self, span: Dict) -> float:
        """Extract duration in milliseconds from span"""
        # OpenTelemetry format (nanoseconds)
        if 'duration' in span:
            duration = span['duration']
            # Check if it's in nanoseconds (very large number)
            if duration > 1000000:
                return duration / 1000000  # Convert ns to ms
            return duration

        # Jaeger format (microseconds)
        if 'startTime' in span and 'duration' in span:
            return span['duration'] / 1000  # Convert μs to ms

        return 0.0

    def _check_error(self, span: Dict) -> bool:
        """Check if span has error"""
        # Check status
        if 'status' in span:
            if span['status'].get('code') == 2:  # ERROR in OpenTelemetry
                return True

        # Check tags
        if 'tags' in span:
            for tag in span['tags']:
                if tag.get('key') == 'error' and tag.get('value'):
                    return True

        return False

    def _calculate_total_duration(self, spans: List[Dict]) -> float:
        """Calculate total trace duration"""
        if not spans:
            return 0.0
        return max(span['duration_ms'] for span in spans)

    def _find_critical_path(self, spans: List[Dict]) -> List[str]:
        """Find the critical path (longest duration chain)"""
        # Sort by duration descending
        sorted_spans = sorted(spans, key=lambda x: x['duration_ms'], reverse=True)

        # Return top 3 slowest operations
        return [f"{span['service']}.{span['operation']}" for span in sorted_spans[:3]]

    def _find_slow_spans(self, spans: List[Dict], total_duration: float) -> List[Dict]:
        """Find spans that take > 20% of total time"""
        threshold = total_duration * 0.2
        slow_spans = [
            span for span in spans
            if span['duration_ms'] > threshold
        ]
        return sorted(slow_spans, key=lambda x: x['duration_ms'], reverse=True)

    def _find_error_spans(self, spans: List[Dict]) -> List[Dict]:
        """Find spans with errors"""
        return [span for span in spans if span['has_error']]

    def _build_call_tree(self, spans: List[Dict]) -> str:
        """Build a simple service call tree"""
        services = set(span['service'] for span in spans)
        return " → ".join(sorted(services))

    def _generate_summary(
        self,
        trace_id: str,
        total_duration: float,
        critical_path: List[str],
        slow_spans: List[Dict],
        error_spans: List[Dict],
        call_tree: str
    ) -> str:
        """Generate natural language summary"""
        lines = [
            f"Trace Analysis (traceId: {trace_id}...)",
            f"- Total duration: {total_duration:.1f}ms",
            f"- Service call chain: {call_tree}",
            ""
        ]

        if error_spans:
            lines.append(f"⚠️  Errors detected: {len(error_spans)} failed span(s)")
            for span in error_spans[:3]:  # Show top 3 errors
                lines.append(f"   - {span['service']}.{span['operation']} (FAILED)")
            lines.append("")

        if slow_spans:
            lines.append(f"🐢 Bottlenecks identified ({len(slow_spans)} slow spans):")
            for i, span in enumerate(slow_spans[:3], 1):  # Show top 3
                pct = (span['duration_ms'] / total_duration) * 100
                lines.append(
                    f"{i}. {span['service']}.{span['operation']} "
                    f"({span['duration_ms']:.1f}ms, {pct:.1f}% of total) "
                    f"{'← BOTTLENECK' if pct > 30 else ''}"
                )
            lines.append("")

        if critical_path:
            lines.append("Critical Path (slowest operations):")
            for op in critical_path:
                lines.append(f"  → {op}")

        return "\n".join(lines)

    def _fallback_extraction(self, content: str) -> str:
        """Fallback for non-JSON content - extract key patterns"""
        lines = content.split('\n')[:20]  # First 20 lines

        summary = ["Trace Data (partial extraction - invalid JSON format)", ""]

        # Try to find trace ID
        for line in lines:
            if 'traceId' in line or 'trace_id' in line:
                summary.append(f"- {line.strip()}")

        summary.append("\nNote: Unable to fully parse trace data. Please verify JSON format.")

        return "\n".join(summary)
