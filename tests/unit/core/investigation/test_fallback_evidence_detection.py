"""Tests for LLM response post-processing in milestone_engine.

Validates that _post_process_llm_response does NOT create fallback evidence
from regex pattern matching, trusting the LLM's judgment instead.

Background: The fallback pattern detector was removed because it
second-guessed the LLM with crude regexes, creating bogus evidence from
non-data content (SSH banners, system MOTD, general conversation). The LLM
already sees every user message and can create evidence via evidence_to_add,
ask for clarification, or treat non-data as conversation.
"""

from unittest.mock import MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import _post_process_llm_response
from faultmaven.modules.case.domain.models import EvidenceCategory, EvidenceSourceType


class TestPostProcessLlmResponse:
    """Tests for the LLM response post-processor."""

    def _make_updates(self, evidence_to_add=None):
        """Create a mock updates object."""
        updates = MagicMock()
        updates.evidence_to_add = evidence_to_add
        return updates

    def _make_case(self):
        """Create a minimal mock case."""
        case = MagicMock()
        case.evidence = []
        case.current_turn = 3
        return case

    def test_no_fallback_evidence_for_ssh_banner(self):
        """SSH terminal output must NOT trigger fallback evidence creation."""
        updates = self._make_updates(evidence_to_add=[])
        case = self._make_case()
        message = (
            "❯ ssh -L 3333:localhost:3333 -L 8090:localhost:8090 swhouse@192.168.0.200\r\n"
            "Welcome to Ubuntu 25.04 (GNU/Linux 6.14.0-29-generic x86_64)\r\n"
            "\r\n"
            " * Documentation:  https://help.ubuntu.com\r\n"
            " * Management:     https://landscape.canonical.com\r\n"
            "\r\n"
            "System information as of Sun Mar 16 06:01:58 PM UTC 2026\r\n"
            "\r\n"
            "  System load:  0.29              Processes:             253\r\n"
            "  Usage of /:   3.7% of 457.39GB  Users logged in:       1\r\n"
            "  Memory usage: 8%                IPv4 address for eth0: 192.168.0.200\r\n"
            "  Swap usage:   0%\r\n"
        )

        result = _post_process_llm_response(
            updates=updates,
            user_message=message,
            case=case,
        )

        assert result.evidence_to_add == []

    def test_no_fallback_evidence_for_log_like_text(self):
        """Even real-looking log data should not trigger fallback — the LLM
        already saw it and chose not to create evidence."""
        updates = self._make_updates(evidence_to_add=[])
        case = self._make_case()
        message = (
            "2026-03-16 14:03:21 ERROR [api] Connection refused\n"
            "2026-03-16 14:03:22 WARN [pool] Pool exhausted"
        )

        result = _post_process_llm_response(
            updates=updates,
            user_message=message,
            case=case,
        )

        assert result.evidence_to_add == []

    def test_no_fallback_evidence_for_metric_like_text(self):
        """Metric-like text should not trigger fallback evidence creation."""
        updates = self._make_updates(evidence_to_add=[])
        case = self._make_case()
        message = (
            "p95: 450ms, p99: 1200ms\n"
            "cpu: 85%, memory: 72%\n"
            "latency avg: 200ms\n"
            "throughput: 1500 req/s\n"
        )

        result = _post_process_llm_response(
            updates=updates,
            user_message=message,
            case=case,
        )

        assert result.evidence_to_add == []

    def test_preserves_llm_created_evidence(self):
        """When LLM already created evidence, it should be preserved as-is."""
        existing = MagicMock()
        updates = self._make_updates(evidence_to_add=[existing])
        case = self._make_case()

        result = _post_process_llm_response(
            updates=updates,
            user_message="some message with timeout and memory 50%",
            case=case,
        )

        assert len(result.evidence_to_add) == 1
        assert result.evidence_to_add[0] is existing

    def test_handles_none_evidence_to_add(self):
        """Should handle None evidence_to_add gracefully."""
        updates = self._make_updates(evidence_to_add=None)
        case = self._make_case()

        result = _post_process_llm_response(
            updates=updates,
            user_message="test message",
            case=case,
        )

        # Should not crash; evidence_to_add stays None (no mutation)
        assert result.evidence_to_add is None

    def test_handles_missing_evidence_to_add_attr(self):
        """Should handle updates objects without evidence_to_add attr."""
        updates = MagicMock(spec=[])  # No attributes
        case = self._make_case()

        result = _post_process_llm_response(
            updates=updates,
            user_message="test message",
            case=case,
        )

        assert result is updates  # Returned unchanged
