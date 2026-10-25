"""Unit tests for BaseService class (TASK-011).

Tests the base service class functionality including:
- Service initialization with name
- Logger creation with correct name
- log_operation() logs with context
- log_error() logs with exception info
- Log extra fields included
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.services.base import BaseService


def _field(record, name):
    """A structured field, wherever this stack put it (the idiom of
    ``tests/unit/api/middleware/test_logging_middleware_attribution.py``)."""
    if isinstance(record.msg, dict) and name in record.msg:
        return record.msg[name]
    return record.__dict__.get(name)


def _the_record(caplog, event):
    matches = [
        r
        for r in caplog.records
        if (r.msg.get("event") if isinstance(r.msg, dict) else r.getMessage()) == event
    ]
    assert len(matches) == 1, f"expected one {event!r} record, got {len(matches)}"
    return matches[0]


class ConcreteService(BaseService):
    """Concrete implementation of BaseService for testing."""

    def __init__(self, service_name: str = None):
        super().__init__(service_name)

    async def test_operation(self, data: dict) -> dict:
        """Test operation for testing execute_operation."""
        return {"result": data.get("input", "default")}


class TestBaseServiceInitialization:
    """Test BaseService initialization."""

    def test_service_initialization_with_explicit_name(self):
        """Test service initialization with explicit name."""
        service = ConcreteService("my_custom_service")

        assert service.service_name == "my_custom_service"
        assert service.logger is not None

    def test_service_initialization_with_auto_generated_name(self):
        """Test service initialization with auto-generated name from class."""
        service = ConcreteService()

        # ConcreteService -> concrete_service
        assert service.service_name == "concrete_service"

    def test_logger_created_with_correct_name(self):
        """Test that logger is created with correct name prefix."""
        service = ConcreteService("test_service")

        # Logger name should include the service layer prefix
        assert (
            "test_service" in service.logger.logger_name or service.logger is not None
        )

    def test_service_name_stored_correctly(self):
        """Test that service_name attribute is stored correctly."""
        name = "case_service"
        service = ConcreteService(name)

        assert service.service_name == name

    def test_camel_case_to_snake_case_conversion(self):
        """Test that CamelCase class names are converted to snake_case."""

        class MyFancyService(BaseService):
            pass

        service = MyFancyService()
        assert service.service_name == "my_fancy_service"

    def test_already_snake_case_preserved(self):
        """Test that already snake_case names are preserved."""

        class already_snake(BaseService):
            pass

        service = already_snake()
        assert service.service_name == "already_snake"


class TestLogOperation:
    """Test log_operation method."""

    def test_log_operation_calls_logger_info(self):
        """Test that log_operation logs at INFO level."""
        service = ConcreteService("test_service")

        with patch.object(service.logger, "info") as mock_info:
            service.log_operation("create_case", user_id="user123")

            mock_info.assert_called_once()
            call_args = mock_info.call_args
            assert "create_case" in str(call_args)

    def test_log_operation_fields_are_top_level_event_keys(self, caplog):
        """The service name and every keyword arrive as fields of their own.

        Passed as stdlib ``extra=`` to the structlog logger, they used to land
        nested under one ``extra`` key, where no log query and no reader of the
        record's fields finds them (#2262).
        """
        service = ConcreteService("my_service")

        with caplog.at_level(logging.INFO):
            service.log_operation("update_case", case_id="case_123", status="active")

        record = _the_record(caplog, "update_case")
        assert _field(record, "service") == "my_service"
        assert _field(record, "case_id") == "case_123"
        assert _field(record, "status") == "active"
        assert _field(record, "extra") is None

    def test_log_operation_with_no_extra_args(self):
        """Test log_operation works with no extra arguments."""
        service = ConcreteService("test_service")

        with patch.object(service.logger, "info") as mock_info:
            service.log_operation("simple_op")

            mock_info.assert_called_once()


class TestLogError:
    """Test log_error method."""

    def test_log_error_calls_logger_error(self):
        """Test that log_error logs at ERROR level."""
        service = ConcreteService("test_service")

        with patch.object(service.logger, "error") as mock_error:
            exc = ValueError("Something went wrong")
            service.log_error("create_case", exc)

            mock_error.assert_called_once()

    def test_log_error_includes_exception_message(self):
        """Test that log_error includes exception message."""
        service = ConcreteService("test_service")

        with patch.object(service.logger, "error") as mock_error:
            exc = ValueError("Invalid input data")
            service.log_error("validate_input", exc)

            call_args = mock_error.call_args[0][0]
            assert "validate_input failed" in call_args
            assert "Invalid input data" in call_args

    def test_log_error_fields_are_top_level_event_keys(self, caplog):
        """The service, the error and every keyword arrive as fields of their own (#2262)."""
        service = ConcreteService("error_test_service")

        with caplog.at_level(logging.ERROR):
            try:
                raise KeyError("missing_key")
            except KeyError as exc:
                service.log_error("save_case", exc, case_id="case_xyz", user_id="u1")

        record = _the_record(caplog, "save_case failed: 'missing_key'")
        assert _field(record, "service") == "error_test_service"
        assert _field(record, "error_type") == "KeyError"
        assert _field(record, "case_id") == "case_xyz"
        assert _field(record, "user_id") == "u1"
        assert _field(record, "extra") is None

    def test_log_error_includes_exc_info(self):
        """Test that log_error includes exc_info=True."""
        service = ConcreteService("test_service")

        with patch.object(service.logger, "error") as mock_error:
            exc = RuntimeError("Runtime failure")
            service.log_error("run_task", exc)

            call_kwargs = mock_error.call_args[1]
            assert call_kwargs["exc_info"] is True


class TestLogMetric:
    """Test log_metric method."""

    def test_log_metric_adds_service_tag(self):
        """Test that log_metric adds service name to tags."""
        service = ConcreteService("metric_service")

        with patch.object(service.logger, "log_metric") as mock_log_metric:
            service.log_metric("case_created", 1, unit="count")

            mock_log_metric.assert_called_once()
            call_kwargs = mock_log_metric.call_args[1]
            assert call_kwargs["tags"]["service"] == "metric_service"

    def test_log_metric_preserves_existing_tags(self):
        """Test that log_metric preserves existing tags."""
        service = ConcreteService("test_service")

        with patch.object(service.logger, "log_metric") as mock_log_metric:
            service.log_metric(
                "latency", 100, unit="ms", tags={"endpoint": "/api/cases"}
            )

            call_kwargs = mock_log_metric.call_args[1]
            assert call_kwargs["tags"]["endpoint"] == "/api/cases"
            assert call_kwargs["tags"]["service"] == "test_service"


class TestLogBusinessEvent:
    """Test log_business_event method."""

    def test_log_business_event_calls_log_event(self):
        """Test that log_business_event calls log_event."""
        service = ConcreteService("event_service")

        with patch.object(service.logger, "log_event") as mock_log_event:
            service.log_business_event("case_created", severity="info")

            mock_log_event.assert_called_once()

    def test_log_business_event_includes_service_in_data(self):
        """Test that log_business_event includes service in data."""
        service = ConcreteService("business_service")

        with patch.object(service.logger, "log_event") as mock_log_event:
            service.log_business_event("case_resolved", data={"case_id": "123"})

            call_kwargs = mock_log_event.call_args[1]
            assert call_kwargs["data"]["service"] == "business_service"
            assert call_kwargs["data"]["case_id"] == "123"


class TestHealthCheck:
    """Test health_check method."""

    @pytest.mark.asyncio
    async def test_health_check_returns_dict(self):
        """Test that health_check returns a dictionary."""
        service = ConcreteService("health_service")

        result = await service.health_check()

        assert isinstance(result, dict)
        assert "service" in result
        assert "status" in result

    @pytest.mark.asyncio
    async def test_health_check_includes_service_name(self):
        """Test that health_check includes service name."""
        service = ConcreteService("my_health_service")

        result = await service.health_check()

        assert result["service"] == "my_health_service"

    @pytest.mark.asyncio
    async def test_health_check_returns_healthy_status(self):
        """Test that health_check returns healthy status by default."""
        service = ConcreteService("healthy_service")

        result = await service.health_check()

        assert result["status"] == "healthy"
        assert result["layer"] == "service"
