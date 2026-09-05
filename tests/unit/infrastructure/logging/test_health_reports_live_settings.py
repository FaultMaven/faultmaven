"""The logging health report must not advertise settings that do nothing.

``GET /health/logging`` returns a ``configuration`` block. It used to name
three knobs that configured nothing: ``deduplication`` (its processor rebuilt
a dict whose keys are unique by construction), and ``buffer_size`` /
``flush_interval`` (bound from settings, reported here, read by no buffering
code — none exists). An operator reading that block saw a buffering
configuration for a system that does not buffer.

That is the fm#1029 shape: an operator surface that disagrees with the running
system. This test keeps the block honest by requiring every key in it to name
a real field on ``LoggingSettings``.

It cannot catch a field that exists but is inert — the case here was both — so
it is a floor, not a proof. Removing a knob's settings field is the step that
gets forgotten last, which is what makes this the useful check.
"""

import pytest

from faultmaven.config.settings import LoggingSettings
from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator

# Reported key -> the LoggingSettings field it claims to reflect.
REPORTED_KEY_TO_FIELD = {
    "log_level": "level",
    "log_format": "log_output_format",
}


@pytest.mark.unit
class TestLoggingHealthReportsLiveSettings:
    def test_every_reported_key_is_mapped(self):
        """A new key added to the block must be added to the map above.

        Without this, the check below would silently skip whatever was added
        and the next inert knob would ship reported-but-dead.
        """
        reported = LoggingCoordinator().get_health_status()["configuration"]

        assert set(reported) == set(REPORTED_KEY_TO_FIELD), (
            "the health block's keys changed; update REPORTED_KEY_TO_FIELD and "
            "confirm the new key names a setting the logging system reads"
        )

    def test_every_reported_key_names_a_real_settings_field(self):
        for key, field_name in REPORTED_KEY_TO_FIELD.items():
            assert field_name in LoggingSettings.model_fields, (
                f"the health block reports {key!r} from LoggingSettings."
                f"{field_name}, which is not a field"
            )
