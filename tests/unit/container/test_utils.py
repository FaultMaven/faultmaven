"""Tests for container utilities.

Tests cover:
- LazyService initialization
- service_getter decorator
- check_dependencies decorator
- log_service_status decorator
"""

import pytest
from unittest.mock import MagicMock, patch

from faultmaven.container.utils import (
    LazyService,
    service_getter,
    check_dependencies,
    log_service_status,
)
from faultmaven.container.errors import ServiceUnavailableError


class TestLazyService:
    """Test LazyService wrapper."""

    def test_not_initialized_until_accessed(self):
        """Test that factory is not called until get()."""
        factory = MagicMock(return_value="instance")
        lazy = LazyService(factory)

        assert not lazy.is_initialized()
        factory.assert_not_called()

    def test_initialized_on_first_access(self):
        """Test that factory is called on first get()."""
        factory = MagicMock(return_value="instance")
        lazy = LazyService(factory)

        result = lazy.get()

        assert result == "instance"
        assert lazy.is_initialized()
        factory.assert_called_once()

    def test_cached_after_first_access(self):
        """Test that factory is only called once."""
        factory = MagicMock(return_value="instance")
        lazy = LazyService(factory)

        lazy.get()
        lazy.get()
        lazy.get()

        factory.assert_called_once()

    def test_reset_clears_instance(self):
        """Test that reset clears the cached instance."""
        factory = MagicMock(return_value="instance")
        lazy = LazyService(factory)

        lazy.get()
        assert lazy.is_initialized()

        lazy.reset()
        assert not lazy.is_initialized()

    def test_reset_allows_reinitialization(self):
        """Test that get() after reset calls factory again."""
        call_count = {"count": 0}

        def factory():
            call_count["count"] += 1
            return f"instance_{call_count['count']}"

        lazy = LazyService(factory)

        result1 = lazy.get()
        assert result1 == "instance_1"

        lazy.reset()
        result2 = lazy.get()
        assert result2 == "instance_2"

    def test_name_from_factory(self):
        """Test that name is derived from factory function."""

        def my_factory():
            return "result"

        lazy = LazyService(my_factory)
        assert lazy._name == "my_factory"

    def test_explicit_name(self):
        """Test that explicit name overrides factory name."""
        lazy = LazyService(lambda: None, name="custom_name")
        assert lazy._name == "custom_name"


class TestServiceGetterDecorator:
    """Test service_getter decorator."""

    def test_returns_existing_instance(self):
        """Test that existing instance is returned."""

        class Container:
            def __init__(self):
                self._service = "existing"

            @service_getter("_service")
            def get_service(self):
                pass

        container = Container()
        assert container.get_service() == "existing"

    def test_returns_none_when_missing(self):
        """Test that None is returned when no instance and no fallback."""

        class Container:
            def __init__(self):
                self._service = None

            @service_getter("_service")
            def get_service(self):
                pass

        container = Container()
        assert container.get_service() is None

    def test_fallback_creates_instance(self):
        """Test that fallback creates instance when missing."""

        class Container:
            def __init__(self):
                self._service = None

            @service_getter("_service", fallback=lambda: "fallback")
            def get_service(self):
                pass

        container = Container()
        result = container.get_service()

        assert result == "fallback"
        assert container._service == "fallback"

    def test_fallback_cached_after_creation(self):
        """Test that fallback instance is cached."""
        call_count = {"count": 0}

        def fallback():
            call_count["count"] += 1
            return "created"

        class Container:
            def __init__(self):
                self._service = None

            @service_getter("_service", fallback=fallback)
            def get_service(self):
                pass

        container = Container()
        container.get_service()
        container.get_service()
        container.get_service()

        assert call_count["count"] == 1

    def test_required_raises_when_missing(self):
        """Test that required=True raises when missing."""

        class Container:
            def __init__(self):
                self._service = None

            @service_getter("_service", required=True)
            def get_service(self):
                pass

        container = Container()

        with pytest.raises(ServiceUnavailableError):
            container.get_service()

    def test_required_returns_instance_when_present(self):
        """Test that required returns instance when present."""

        class Container:
            def __init__(self):
                self._service = "exists"

            @service_getter("_service", required=True)
            def get_service(self):
                pass

        container = Container()
        assert container.get_service() == "exists"


class TestCheckDependenciesDecorator:
    """Test check_dependencies decorator."""

    def test_proceeds_when_all_deps_present(self):
        """Test that method runs when dependencies are present."""

        class Container:
            def __init__(self):
                self.db = "database"
                self.cache = "cache"

            @check_dependencies("db", "cache")
            def create_service(self):
                return "service"

        container = Container()
        assert container.create_service() == "service"

    def test_returns_none_when_dep_missing(self):
        """Test that None is returned when dependency is missing."""

        class Container:
            def __init__(self):
                self.db = "database"
                self.cache = None

            @check_dependencies("db", "cache")
            def create_service(self):
                return "service"

        container = Container()
        assert container.create_service() is None

    def test_returns_none_when_dep_not_exists(self):
        """Test that None is returned when attribute doesn't exist."""

        class Container:
            def __init__(self):
                self.db = "database"

            @check_dependencies("db", "missing_attr")
            def create_service(self):
                return "service"

        container = Container()
        assert container.create_service() is None

    def test_no_deps_always_proceeds(self):
        """Test that no dependencies always proceeds."""

        class Container:
            @check_dependencies()
            def create_service(self):
                return "service"

        container = Container()
        assert container.create_service() == "service"


class TestLogServiceStatusDecorator:
    """Test log_service_status decorator."""

    def test_logs_success_on_return(self):
        """Test that success is logged when method returns."""

        class Container:
            @log_service_status("TestService")
            def create_service(self):
                return "service"

        container = Container()

        with patch("faultmaven.container.utils.logging") as mock_logging:
            mock_logger = MagicMock()
            mock_logging.getLogger.return_value = mock_logger

            result = container.create_service()

            assert result == "service"
            mock_logger.info.assert_called_once()
            assert "TestService" in mock_logger.info.call_args[0][0]

    def test_logs_warning_on_exception(self):
        """Test that warning is logged when method raises."""

        class Container:
            @log_service_status("TestService")
            def create_service(self):
                raise ValueError("Failed")

        container = Container()

        with patch("faultmaven.container.utils.logging") as mock_logging:
            mock_logger = MagicMock()
            mock_logging.getLogger.return_value = mock_logger

            with pytest.raises(ValueError):
                container.create_service()

            mock_logger.warning.assert_called_once()
            assert "TestService" in mock_logger.warning.call_args[0][0]

    def test_no_log_when_returns_none(self):
        """Test that success is not logged when method returns None."""

        class Container:
            @log_service_status("TestService")
            def create_service(self):
                return None

        container = Container()

        with patch("faultmaven.container.utils.logging") as mock_logging:
            mock_logger = MagicMock()
            mock_logging.getLogger.return_value = mock_logger

            result = container.create_service()

            assert result is None
            mock_logger.info.assert_not_called()

    def test_custom_messages(self):
        """Test custom success and failure messages."""

        class Container:
            @log_service_status(
                "TestService",
                success_msg="Custom success",
                failure_msg="Custom failure",
            )
            def create_service(self, should_fail=False):
                if should_fail:
                    raise ValueError("Failed")
                return "service"

        container = Container()

        with patch("faultmaven.container.utils.logging") as mock_logging:
            mock_logger = MagicMock()
            mock_logging.getLogger.return_value = mock_logger

            container.create_service()
            mock_logger.info.assert_called_with("Custom success")

            with pytest.raises(ValueError):
                container.create_service(should_fail=True)
            mock_logger.warning.assert_called_with("Custom failure")
