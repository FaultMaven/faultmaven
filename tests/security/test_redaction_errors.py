from unittest.mock import MagicMock, patch

import pytest

from faultmaven.security.redaction import DataSanitizer


def test_sanitizer_handles_analyzer_exception():
    """
    Test that if the Presidio analyzer fails, the sanitizer returns the original text.
    """
    with patch("presidio_analyzer.AnalyzerEngine") as mock_analyzer:
        # Configure the mock to raise an exception when 'analyze' is called
        mock_analyzer.return_value.analyze.side_effect = Exception(
            "Analyzer engine failed"
        )

        sanitizer = DataSanitizer()

        # The sanitizer should catch the exception and return the original text
        original_text = "This is a test with a potential secret."
        sanitized_text = sanitizer.sanitize(original_text)

        assert sanitized_text == original_text
