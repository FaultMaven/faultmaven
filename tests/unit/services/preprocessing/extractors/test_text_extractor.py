"""
Tests for UnstructuredTextExtractor.

Covers:
- R7.1: Word boundary matching for error keyword detection
"""

import pytest

from faultmaven.services.preprocessing.extractors.text_extractor import (
    UnstructuredTextExtractor,
)


class TestTextExtractor:
    @pytest.fixture
    def extractor(self):
        return UnstructuredTextExtractor()

    def test_properties(self, extractor):
        assert extractor.strategy_name == "direct"
        assert extractor.llm_calls_used == 0

    # --- R7.1: Word boundary matching ---

    def test_mirror_does_not_trigger(self, extractor):
        """'mirror' contains 'error' as substring — should NOT trigger error detection."""
        content = """
The database mirror is configured for high availability.
The mirror syncs every 5 minutes.
No issues were detected during the last sync cycle.
"""
        result = extractor.extract(content)
        assert "ERROR MESSAGES" not in result

    def test_terrorism_does_not_trigger(self, extractor):
        """'terrorism' contains substring — should NOT trigger error detection."""
        content = """
The report covers counter-terrorism strategies and national security.
Intelligence agencies collaborate to prevent terrorism globally.
"""
        result = extractor.extract(content)
        assert "ERROR MESSAGES" not in result

    def test_real_error_does_trigger(self, extractor):
        """'Error: connection refused' is a real error and SHOULD trigger."""
        content = """
Connecting to database server...
Error: connection refused on port 5432
Retrying in 5 seconds...
"""
        result = extractor.extract(content)
        assert "ERROR MESSAGES" in result
        assert "connection refused" in result

    def test_fatal_crash_triggers(self, extractor):
        """'fatal crash occurred' should trigger error detection."""
        content = """
System started normally at 12:00.
At 12:05 a fatal crash occurred in the main process.
Core dump saved to /tmp/core.
"""
        result = extractor.extract(content)
        assert "ERROR MESSAGES" in result
        assert "fatal" in result.lower()

    def test_failure_as_standalone_word(self, extractor):
        """'failure' as standalone word should trigger."""
        content = """
The deployment started at 10:00 AM.
A failure was detected during the health check phase.
Rolling back to previous version.
"""
        result = extractor.extract(content)
        assert "ERROR MESSAGES" in result
