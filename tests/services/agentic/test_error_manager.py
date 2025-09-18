"""
Unit tests for ErrorFallbackManager - comprehensive error handling and recovery.

This module tests the error management system that provides error classification,
recovery strategies, circuit breaker patterns, and health monitoring.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
import asyncio
from datetime import datetime, timedelta

from faultmaven.services.agentic.safety.error_manager import ErrorFallbackManager
from faultmaven.models.agentic import (
    ErrorContext, ErrorType, ErrorSeverity, RecoveryStrategy, CircuitBreakerState,
    HealthStatus, AlertLevel, ErrorClassification, RecoveryResult, FallbackConfig
)

# Define missing model for tests
from dataclasses import dataclass

@dataclass
class ComponentHealthStatus:
    """Component health status for testing"""
    component: str
    status: str
    response_time: float
    last_check: datetime


class TestErrorFallbackManager:
    """Test suite for Error Handling & Fallback Manager."""
    
    @pytest.fixture
    def mock_health_checker(self):
        """Mock health checker for system monitoring."""
        mock = AsyncMock()
        mock.check_component_health.return_value = ComponentHealthStatus(
            component='test_component',
            status='healthy',
            response_time=0.1,
            last_check=datetime.now()
        )
        mock.get_system_metrics.return_value = {
            'cpu_usage': 0.3,
            'memory_usage': 0.6,
            'error_rate': 0.02
        }
        return mock

    @pytest.fixture
    def mock_alert_manager(self):
        """Mock alert manager for notifications."""
        mock = AsyncMock()
        mock.send_alert.return_value = True
        mock.escalate_alert.return_value = True
        return mock

    @pytest.fixture
    def error_manager(self, mock_health_checker, mock_alert_manager):
        """Create error manager with mocked dependencies."""
        return ErrorFallbackManager(
            health_checker=mock_health_checker,
            alert_manager=mock_alert_manager
        )

    @pytest.mark.asyncio
    async def test_init_error_manager(self, error_manager):
        """Test error manager initialization."""
        assert error_manager.health_checker is not None
        assert error_manager.alert_manager is not None
        assert hasattr(error_manager, 'circuit_breakers')
        assert hasattr(error_manager, 'error_history')
        assert hasattr(error_manager, 'fallback_strategies')

    @pytest.mark.asyncio
    async def test_handle_error_transient(self, error_manager):
        """Test handling of transient errors with automatic recovery."""
        error_context = ErrorContext(
            session_id='test-session',
            operation='llm_provider_call',
            error_type=ErrorType.TIMEOUT_ERROR,
            severity=ErrorSeverity.LOW,
            component='llm_provider',
            message='Request timeout after 30 seconds',
            metadata={'attempt': 1, 'max_retries': 3}
        )
        
        result = await error_manager.handle_error(error_context)
        
        assert isinstance(result, dict)
        assert result['recovered'] == True
        assert result['strategy'] == 'retry'
        assert result['attempts'] >= 1

    @pytest.mark.asyncio
    async def test_handle_error_critical(self, error_manager, mock_alert_manager):
        """Test handling of critical errors with alerting."""
        error_context = ErrorContext(
            session_id='test-session',
            operation='workflow_execution',
            error_type=ErrorType.SYSTEM_ERROR,
            severity=ErrorSeverity.CRITICAL,
            component='workflow_engine',
            message='Core system component failure',
            metadata={'stack_trace': 'Error trace...'}
        )
        
        result = await error_manager.handle_error(error_context)
        
        # Should trigger alerts for critical errors
        mock_alert_manager.send_alert.assert_called()
        
        # Should attempt recovery
        assert 'recovery_attempted' in result
        assert result['severity'] == 'critical'

    @pytest.mark.asyncio
    async def test_error_classification(self, error_manager):
        """Test error classification by type and severity."""
        test_cases = [
            (ValueError("Invalid input"), ErrorType.VALIDATION_ERROR, ErrorSeverity.MEDIUM),
            (ConnectionError("Network unavailable"), ErrorType.NETWORK_ERROR, ErrorSeverity.HIGH),
            (TimeoutError("Operation timeout"), ErrorType.TIMEOUT_ERROR, ErrorSeverity.LOW),
            (RuntimeError("System malfunction"), ErrorType.SYSTEM_ERROR, ErrorSeverity.CRITICAL)
        ]
        
        for exception, expected_type, expected_severity in test_cases:
            classification = await error_manager.classify_error(exception)
            
            assert isinstance(classification, ErrorClassification)
            assert classification.error_type == expected_type
            assert classification.severity == expected_severity

    @pytest.mark.asyncio
    async def test_circuit_breaker_closed(self, error_manager):
        """Test circuit breaker in closed state (normal operation)."""
        component = 'test_service'
        
        # Circuit should start closed
        state = await error_manager.get_circuit_breaker_state(component)
        assert state == CircuitBreakerState.CLOSED
        
        # Should allow operation
        allowed = await error_manager.is_operation_allowed(component)
        assert allowed == True

    @pytest.mark.asyncio
    async def test_circuit_breaker_open(self, error_manager):
        """Test circuit breaker opening after error threshold."""
        component = 'failing_service'
        
        # Simulate multiple failures to trip circuit breaker
        for _ in range(5):  # Exceed threshold
            error_context = ErrorContext(
                error_type=ErrorType.SYSTEM_ERROR,
                severity=ErrorSeverity.HIGH,
                component=component,
                message='Service failure',
                metadata={}
            )
            await error_manager.handle_error(error_context)
        
        # Circuit should now be open
        state = await error_manager.get_circuit_breaker_state(component)
        assert state == CircuitBreakerState.OPEN
        
        # Should block operations
        allowed = await error_manager.is_operation_allowed(component)
        assert allowed == False

    @pytest.mark.asyncio
    async def test_circuit_breaker_half_open(self, error_manager):
        """Test circuit breaker half-open state and recovery."""
        component = 'recovering_service'
        
        # Trip circuit breaker
        for _ in range(5):
            error_context = ErrorContext(
                error_type=ErrorType.SYSTEM_ERROR,
                severity=ErrorSeverity.HIGH,
                component=component,
                message='Service failure',
                metadata={}
            )
            await error_manager.handle_error(error_context)
        
        # Simulate time passage for half-open transition
        with patch('datetime.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime.now() + timedelta(minutes=5)
            
            # Should transition to half-open
            await error_manager.update_circuit_breaker_states()
            state = await error_manager.get_circuit_breaker_state(component)
            assert state == CircuitBreakerState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_recovery_strategy_retry(self, error_manager):
        """Test retry recovery strategy."""
        error_context = ErrorContext(
            error_type=ErrorType.TIMEOUT_ERROR,
            severity=ErrorSeverity.LOW,
            component='api_client',
            message='Temporary network issue',
            metadata={'retryable': True}
        )
        
        # Mock the operation to succeed on retry
        async def mock_operation():
            return {'success': True, 'data': 'recovered'}
        
        result = await error_manager.execute_with_retry(mock_operation, max_attempts=3)
        
        assert result['success'] == True
        assert 'data' in result

    @pytest.mark.asyncio
    async def test_recovery_strategy_fallback(self, error_manager):
        """Test fallback recovery strategy."""
        error_context = ErrorContext(
            error_type=ErrorType.SYSTEM_ERROR,
            severity=ErrorSeverity.HIGH,
            component='primary_llm',
            message='Primary LLM service down',
            metadata={'fallback_available': True}
        )
        
        # Configure fallback
        fallback_config = FallbackConfig(
            fallback_component='secondary_llm',
            fallback_strategy='switch_provider',
            degraded_mode=True
        )
        
        result = await error_manager.execute_fallback(error_context, fallback_config)
        
        assert result['fallback_used'] == True
        assert result['fallback_component'] == 'secondary_llm'

    @pytest.mark.asyncio
    async def test_health_monitoring_integration(self, error_manager, mock_health_checker):
        """Test integration with health monitoring system."""
        component = 'monitored_service'
        
        # Check component health
        health = await error_manager.check_component_health(component)
        
        assert health.component == component
        mock_health_checker.check_component_health.assert_called_with(component)

    @pytest.mark.asyncio
    async def test_error_aggregation(self, error_manager):
        """Test error aggregation and pattern detection."""
        # Generate multiple similar errors
        for i in range(10):
            error_context = ErrorContext(
                error_type=ErrorType.VALIDATION_ERROR,
                severity=ErrorSeverity.MEDIUM,
                component='data_processor',
                message=f'Validation error {i}',
                metadata={'pattern': 'input_validation'}
            )
            await error_manager.handle_error(error_context)
        
        # Should detect error pattern
        patterns = await error_manager.detect_error_patterns()
        
        assert len(patterns) > 0
        assert any(p['pattern_type'] == 'validation_errors' for p in patterns)

    @pytest.mark.asyncio
    async def test_alert_escalation(self, error_manager, mock_alert_manager):
        """Test alert escalation for repeated critical errors."""
        component = 'critical_service'
        
        # Generate multiple critical errors
        for _ in range(3):
            error_context = ErrorContext(
                error_type=ErrorType.SYSTEM_ERROR,
                severity=ErrorSeverity.CRITICAL,
                component=component,
                message='Critical system failure',
                metadata={}
            )
            await error_manager.handle_error(error_context)
        
        # Should escalate alert after threshold
        mock_alert_manager.escalate_alert.assert_called()

    @pytest.mark.asyncio
    async def test_error_context_enrichment(self, error_manager):
        """Test enrichment of error context with additional metadata."""
        basic_error = ValueError("Basic validation error")
        
        enriched_context = await error_manager.enrich_error_context(
            basic_error, 
            component='validator',
            additional_context={'user_id': 'user123', 'request_id': 'req456'}
        )
        
        assert isinstance(enriched_context, ErrorContext)
        assert enriched_context.component == 'validator'
        assert 'user_id' in enriched_context.metadata
        assert 'request_id' in enriched_context.metadata

    @pytest.mark.asyncio
    async def test_graceful_degradation(self, error_manager):
        """Test graceful degradation when multiple components fail."""
        # Simulate cascade failure
        components = ['service_a', 'service_b', 'service_c']
        
        for component in components:
            error_context = ErrorContext(
                error_type=ErrorType.SYSTEM_ERROR,
                severity=ErrorSeverity.HIGH,
                component=component,
                message=f'{component} failure',
                metadata={}
            )
            await error_manager.handle_error(error_context)
        
        # Should activate degraded mode
        degraded_mode = await error_manager.is_degraded_mode_active()
        assert degraded_mode == True
        
        # Should provide limited functionality
        available_features = await error_manager.get_available_features()
        assert len(available_features) < error_manager.get_full_feature_set()

    @pytest.mark.asyncio
    async def test_error_recovery_metrics(self, error_manager):
        """Test collection of error recovery metrics."""
        # Generate various errors with different outcomes
        error_scenarios = [
            (ErrorType.TIMEOUT_ERROR, True),   # Recoverable
            (ErrorType.VALIDATION_ERROR, True),  # Recoverable
            (ErrorType.SYSTEM_ERROR, False),   # Not recoverable
        ]
        
        for error_type, recoverable in error_scenarios:
            error_context = ErrorContext(
                error_type=error_type,
                severity=ErrorSeverity.MEDIUM,
                component='test_component',
                message='Test error',
                metadata={'recoverable': recoverable}
            )
            await error_manager.handle_error(error_context)
        
        # Check recovery metrics
        metrics = await error_manager.get_recovery_metrics()
        
        assert 'total_errors' in metrics
        assert 'recovery_rate' in metrics
        assert 'recovery_time' in metrics
        assert metrics['total_errors'] >= 3

    @pytest.mark.asyncio
    async def test_error_suppression(self, error_manager):
        """Test error suppression to prevent spam."""
        # Generate duplicate errors rapidly
        error_context = ErrorContext(
            error_type=ErrorType.NETWORK_ERROR,
            severity=ErrorSeverity.MEDIUM,
            component='api_client',
            message='Connection refused',
            metadata={}
        )
        
        # First error should be handled normally
        result1 = await error_manager.handle_error(error_context)
        assert result1['suppressed'] == False
        
        # Rapid duplicate should be suppressed
        result2 = await error_manager.handle_error(error_context)
        assert result2['suppressed'] == True

    def test_error_severity_calculation(self, error_manager):
        """Test error severity calculation based on multiple factors."""
        factors = {
            'component_criticality': 'high',
            'error_frequency': 'low',
            'impact_scope': 'limited',
            'recovery_difficulty': 'easy'
        }
        
        severity = error_manager.calculate_error_severity(ErrorType.SYSTEM_ERROR, factors)
        assert severity in [s for s in ErrorSeverity]

    def test_recovery_strategy_selection(self, error_manager):
        """Test selection of appropriate recovery strategy."""
        error_scenarios = [
            (ErrorType.TIMEOUT_ERROR, RecoveryStrategy.RETRY),
            (ErrorType.SYSTEM_ERROR, RecoveryStrategy.FALLBACK),
            (ErrorType.VALIDATION_ERROR, RecoveryStrategy.SKIP),
            (ErrorType.SYSTEM_ERROR, RecoveryStrategy.ALERT_AND_STOP)
        ]
        
        for error_type, expected_strategy in error_scenarios:
            strategy = error_manager.select_recovery_strategy(error_type)
            assert strategy == expected_strategy

    def test_circuit_breaker_configuration(self, error_manager):
        """Test circuit breaker configuration and thresholds."""
        component = 'configurable_service'
        
        # Test default configuration
        config = error_manager.get_circuit_breaker_config(component)
        assert 'failure_threshold' in config
        assert 'timeout_duration' in config
        assert 'recovery_timeout' in config
        
        # Test custom configuration
        custom_config = {
            'failure_threshold': 10,
            'timeout_duration': 300,
            'recovery_timeout': 60
        }
        
        error_manager.configure_circuit_breaker(component, custom_config)
        updated_config = error_manager.get_circuit_breaker_config(component)
        
        assert updated_config['failure_threshold'] == 10
        assert updated_config['timeout_duration'] == 300

    @pytest.mark.asyncio
    async def test_error_correlation(self, error_manager):
        """Test correlation of related errors across components."""
        # Generate related errors
        related_errors = [
            ErrorContext(
                error_type=ErrorType.SYSTEM_ERROR,
                severity=ErrorSeverity.HIGH,
                component='database',
                message='Connection pool exhausted',
                metadata={'correlation_id': 'issue_123'}
            ),
            ErrorContext(
                error_type=ErrorType.SYSTEM_ERROR,
                severity=ErrorSeverity.HIGH,
                component='api_service',
                message='Database operation failed',
                metadata={'correlation_id': 'issue_123'}
            )
        ]
        
        for error in related_errors:
            await error_manager.handle_error(error)
        
        # Should identify correlation
        correlations = await error_manager.identify_error_correlations()
        
        assert len(correlations) > 0
        assert any(c['correlation_id'] == 'issue_123' for c in correlations)