"""Unit tests for security shim (PII redaction).

Tests the graceful degradation behavior of the Presidio PII redaction shim.
Verifies that the shim works correctly:
- When Presidio is installed and enabled
- When Presidio is installed but disabled
- When Presidio is not installed (simulated)
- Error handling (fail-open behavior)
"""

import os
from unittest.mock import MagicMock, patch

import pytest


class TestPIIRedactorInitialization:
    """Tests for PIIRedactor initialization."""

    def test_pii_redactor_disabled_by_default(self):
        """Test PIIRedactor is inactive when ENABLE_PII_REDACTION is not set."""
        with patch.dict(os.environ, {}, clear=True):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()

            assert redactor.active is False
            assert redactor.pii_enabled is False

    def test_pii_redactor_disabled_when_false(self):
        """Test PIIRedactor when ENABLE_PII_REDACTION=false."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()

            assert redactor.active is False
            assert redactor.pii_enabled is False

    def test_pii_redactor_disabled_when_empty(self):
        """Test PIIRedactor when ENABLE_PII_REDACTION is empty."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": ""}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()

            assert redactor.active is False

    def test_pii_redactor_enabled_without_presidio(self):
        """Test PIIRedactor when enabled but Presidio not available."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.security.PRESIDIO_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import security

                redactor = security.PIIRedactor()

                assert redactor.pii_enabled is True
                assert redactor.active is False  # Can't be active without Presidio


class TestPIIRedactorRedaction:
    """Tests for PIIRedactor.redact() method."""

    def test_redact_passthrough_when_disabled(self):
        """Test redact returns original text when disabled."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()
            text = "My email is test@example.com and SSN is 123-45-6789"

            result = redactor.redact(text)

            assert result == text, "Should return original text when disabled"

    def test_redact_passthrough_without_presidio(self):
        """Test redact returns original text when Presidio not available."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.security.PRESIDIO_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import security

                redactor = security.PIIRedactor()
                text = "Contact: john@doe.com, phone: 555-123-4567"

                result = redactor.redact(text)

                assert result == text, "Should passthrough when Presidio unavailable"

    def test_redact_handles_empty_string(self):
        """Test redact handles empty string gracefully."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()

            result = redactor.redact("")

            assert result == ""

    def test_redact_handles_none_entities(self):
        """Test redact handles None entities parameter."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()
            text = "Some text with PII"

            result = redactor.redact(text, entities=None)

            assert result == text


class TestPIIRedactorStatus:
    """Tests for PIIRedactor status methods."""

    def test_get_status_disabled(self):
        """Test get_status when PII redaction is disabled."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()
            status = redactor.get_status()

            assert "presidio_available" in status
            assert "pii_redaction_enabled" in status
            assert "active" in status
            assert "initialization_error" in status
            assert status["pii_redaction_enabled"] is False
            assert status["active"] is False

    def test_get_status_enabled_without_presidio(self):
        """Test get_status when enabled but Presidio not available."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.security.PRESIDIO_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import security

                redactor = security.PIIRedactor()
                status = redactor.get_status()

                assert status["pii_redaction_enabled"] is True
                assert status["active"] is False

    def test_is_active_method(self):
        """Test is_active() method returns correct value."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()

            assert redactor.is_active() is False


class TestPIIRedactorErrorHandling:
    """Tests for PIIRedactor error handling (fail-open behavior)."""

    def test_a_system_exit_from_the_engine_degrades_instead_of_killing_the_process(
        self,
    ):
        """The guard has to hold against the failure that actually happens.

        Presidio's spaCy engine reaches ``spacy.cli.download`` for a missing
        model, which shells out to an installer and ends in
        ``sys.exit(returncode)``. ``SystemExit`` derives from ``BaseException``,
        so ``except Exception`` did not apply and the exit escaped a guard whose
        entire purpose is that a redactor which cannot start degrades to
        inactive. No FaultMaven image installs a spaCy model while
        ``presidio-analyzer`` ships in the cloud requirements, so this is the
        ordinary path for any deployment that enables redaction.

        Paired with the ``Exception`` case above so the two cannot drift: both
        must degrade, and neither may leave ``active`` true.
        """
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.security.PRESIDIO_AVAILABLE", True
            ):
                with patch(
                    "faultmaven.infrastructure.shims.security.AnalyzerEngine",
                    side_effect=SystemExit(2),
                ):
                    from faultmaven.infrastructure.shims import security

                    redactor = security.PIIRedactor()

                    assert redactor.active is False
                    assert redactor.is_active() is False
                    assert redactor._initialization_error is not None
                    # And it still answers, fail-open on the text rather than
                    # raising into whatever was being redacted.
                    assert redactor.redact("mail a@b.com") == "mail a@b.com"

    def test_an_interrupt_is_not_swallowed_by_the_degradation_guard(self):
        """The other direction: ``BaseException`` would be over-catching.

        ``KeyboardInterrupt`` and ``CancelledError`` belong to whoever is
        shutting the process down; swallowing them here would turn a Ctrl-C or a
        cancelled task into a hang. Pinned so a later widening of the except
        clause has to argue with a test rather than pass silently.
        """
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.security.PRESIDIO_AVAILABLE", True
            ):
                with patch(
                    "faultmaven.infrastructure.shims.security.AnalyzerEngine",
                    side_effect=KeyboardInterrupt(),
                ):
                    from faultmaven.infrastructure.shims import security

                    with pytest.raises(KeyboardInterrupt):
                        security.PIIRedactor()

    def test_initialization_error_handling(self):
        """Test PIIRedactor handles initialization errors gracefully."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            # Mock Presidio as available but make initialization fail
            with patch(
                "faultmaven.infrastructure.shims.security.PRESIDIO_AVAILABLE", True
            ):
                with patch(
                    "faultmaven.infrastructure.shims.security.AnalyzerEngine",
                    side_effect=Exception("Init error"),
                ):
                    from faultmaven.infrastructure.shims import security

                    # Force reimport to test initialization
                    redactor = security.PIIRedactor()

                    assert redactor.active is False
                    assert "Init error" in (redactor._initialization_error or "")

    def test_redact_error_returns_original_text(self):
        """Test that redaction errors return original text (fail-open)."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()
            # Manually set active to True to test error path
            redactor.active = True
            redactor._analyzer = MagicMock()
            redactor._analyzer.analyze.side_effect = Exception("Analysis error")

            text = "My email is test@example.com"
            result = redactor.redact(text)

            # Should return original text on error
            assert result == text


class TestModuleLevelFunctions:
    """Tests for module-level functions."""

    def test_get_pii_redaction_status_disabled(self):
        """Test get_pii_redaction_status when disabled."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import (
                get_pii_redaction_status,
            )

            status = get_pii_redaction_status()

            assert "presidio_available" in status
            assert "pii_redaction_enabled" in status
            assert "would_be_active" in status
            assert status["pii_redaction_enabled"] is False
            assert status["would_be_active"] is False

    def test_get_pii_redaction_status_enabled_without_presidio(self):
        """Test get_pii_redaction_status when enabled but Presidio unavailable."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.security.PRESIDIO_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import security

                status = security.get_pii_redaction_status()

                assert status["pii_redaction_enabled"] is True
                assert status["would_be_active"] is False

    def test_presidio_available_constant_exists(self):
        """Test that PRESIDIO_AVAILABLE constant is exported."""
        from faultmaven.infrastructure.shims.security import PRESIDIO_AVAILABLE

        # Should be a boolean
        assert isinstance(PRESIDIO_AVAILABLE, bool)


class TestEnvironmentVariableHandling:
    """Tests for environment variable edge cases."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("false", False),
            ("False", False),
            ("FALSE", False),
            ("0", False),
            ("1", False),  # Only "true" (case-insensitive) enables
            ("yes", False),  # Only "true" (case-insensitive) enables
            ("", False),
        ],
    )
    def test_enable_pii_redaction_values(self, value: str, expected: bool):
        """Test various ENABLE_PII_REDACTION values."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": value}):
            from faultmaven.infrastructure.shims.security import (
                _is_pii_redaction_enabled,
            )

            result = _is_pii_redaction_enabled()
            assert (
                result is expected
            ), f"ENABLE_PII_REDACTION={value} should be {expected}"


def _presidio_is_usable() -> bool:
    """Whether a real Presidio redactor can actually become active here.

    ``PRESIDIO_AVAILABLE`` answers "is the package importable?", which is not
    the same question and is the one these tests were asking. The package ships
    in the cloud requirements while **no FaultMaven image installs a spaCy
    model**, so importable-but-unusable is the ordinary state: the engine
    reaches for a model, fails, and the redactor degrades to inactive exactly as
    designed. Gating on the import made four tests assert ``active is True``
    against a redactor that was correctly inactive.

    Constructing one and asking is the only predicate that matches what the
    tests below need, and it costs one construction. Where the model IS present
    (a cloud image that adds it), they run for real rather than skipping.
    """
    from faultmaven.infrastructure.shims.security import PIIRedactor

    with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
        return PIIRedactor().active


class TestIntegrationWithPresidio:
    """Tests for integration with real Presidio library if available."""

    def test_pii_redactor_with_presidio_enabled(self):
        """Test PIIRedactor with Presidio enabled (if available)."""
        if not _presidio_is_usable():
            pytest.skip(
                "Presidio cannot initialise here (no spaCy model installed) — "
                "the redactor correctly degrades to inactive, which is what "
                "the unit cases above cover"
            )

        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()

            assert redactor.active is True
            assert redactor.is_active() is True

    def test_redact_email_with_presidio(self):
        """Test email redaction with real Presidio (if available)."""
        if not _presidio_is_usable():
            pytest.skip(
                "Presidio cannot initialise here (no spaCy model installed) — "
                "the redactor correctly degrades to inactive, which is what "
                "the unit cases above cover"
            )

        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()
            text = "Contact me at test@example.com"

            result = redactor.redact(text, entities=["EMAIL_ADDRESS"])

            # The email should be redacted (replaced with something else)
            assert "test@example.com" not in result
            assert len(result) > 0

    def test_redact_phone_with_presidio(self):
        """Test phone number redaction with real Presidio (if available)."""
        if not _presidio_is_usable():
            pytest.skip(
                "Presidio cannot initialise here (no spaCy model installed) — "
                "the redactor correctly degrades to inactive, which is what "
                "the unit cases above cover"
            )

        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()
            text = "Call me at 555-123-4567"

            result = redactor.redact(text, entities=["PHONE_NUMBER"])

            # The phone should be redacted
            assert "555-123-4567" not in result

    def test_redact_no_pii_found(self):
        """Test redaction when no PII is present."""
        if not _presidio_is_usable():
            pytest.skip(
                "Presidio cannot initialise here (no spaCy model installed) — "
                "the redactor correctly degrades to inactive, which is what "
                "the unit cases above cover"
            )

        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()
            text = "This is a normal text with no PII"

            result = redactor.redact(text)

            # Should return original text when no PII found
            assert result == text

    def test_get_status_with_presidio(self):
        """Test status when Presidio is actually available."""
        if not _presidio_is_usable():
            pytest.skip(
                "Presidio cannot initialise here (no spaCy model installed) — "
                "the redactor correctly degrades to inactive, which is what "
                "the unit cases above cover"
            )

        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()
            status = redactor.get_status()

            assert status["presidio_available"] is True
            assert status["pii_redaction_enabled"] is True
            assert status["active"] is True
            assert status["initialization_error"] is None


class TestPIIRedactorWithSpecificEntities:
    """Tests for PIIRedactor with specific entity types."""

    def test_redact_with_specific_entities_disabled(self):
        """Test redact with specific entities when disabled."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()
            text = "Email: test@example.com, SSN: 123-45-6789"

            result = redactor.redact(text, entities=["EMAIL_ADDRESS", "US_SSN"])

            # Should return original text when disabled
            assert result == text

    def test_redact_with_language_parameter(self):
        """Test redact with language parameter."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor = PIIRedactor()
            text = "Mon email est test@example.fr"

            result = redactor.redact(text, language="fr")

            # Should return original text when disabled
            assert result == text
