"""
FaultMaven Logging Configuration

Provides enhanced logging configuration using structlog with JSON formatting,
request context injection, deduplication, and OpenTelemetry integration.
"""

import logging
import os
from typing import Dict, Any, Optional
import structlog
from opentelemetry import trace


class LoggingConfig:
    """
    Configuration for logging system from environment variables.
    
    This class reads logging configuration from environment variables
    and provides type-safe access to configuration values with sensible
    defaults.
    """
    
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
    LOG_FORMAT = os.getenv('LOG_FORMAT', 'json').lower()
    LOG_DEDUPE = os.getenv('LOG_DEDUPE', 'true').lower() == 'true'
    LOG_BUFFER_SIZE = int(os.getenv('LOG_BUFFER_SIZE', '100'))
    LOG_FLUSH_INTERVAL = float(os.getenv('LOG_FLUSH_INTERVAL', '5'))
    LOG_HUMAN_READABLE = os.getenv('LOG_HUMAN_READABLE', 'false').lower() == 'true'
    
    @classmethod
    def get_log_level(cls) -> int:
        """
        Convert string log level to logging constant.
        
        Returns:
            Logging level constant (logging.DEBUG, logging.INFO, etc.)
        """
        levels = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'ERROR': logging.ERROR,
            'CRITICAL': logging.CRITICAL
        }
        return levels.get(cls.LOG_LEVEL, logging.INFO)


class FaultMavenLogger:
    """
    Enhanced logger configuration with deduplication and structured logging.
    
    This class configures structlog with processors for request context injection,
    deduplication, trace context, and JSON formatting. It ensures consistent
    log structure across all application components.
    """
    
    def __init__(self):
        """Initialize the logger configuration."""
        self.config = LoggingConfig()
        self.configure_structlog()
        
    def configure_structlog(self) -> None:
        """
        Configure structlog with comprehensive processors.
        
        Sets up a processor chain that handles:
        - Log level filtering
        - Logger name and level addition
        - Timestamp formatting
        - Exception information
        - Request context injection
        - Field deduplication
        - OpenTelemetry trace context
        - JSON output formatting
        """
        # Configure standard library logging with environment-based level
        logging.basicConfig(
            format="%(message)s",
            level=self.config.get_log_level(),
        )
        
        # Build processor list based on configuration
        processors = [
            # Standard processors
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            
            # Always add request context
            self.add_request_context,
        ]
        
        # Add deduplication processor if enabled
        if self.config.LOG_DEDUPE:
            processors.append(self.deduplicate_fields)
        
        # Always add trace context
        processors.append(self.add_trace_context)
        
        # Add appropriate renderer based on format
        if self.config.LOG_FORMAT == 'json':
            processors.append(structlog.processors.JSONRenderer())
        elif self.config.LOG_FORMAT == 'console':
            processors.append(structlog.dev.ConsoleRenderer())
        else:
            # Default to JSON for production, but allow human-readable for development
            if self.config.LOG_HUMAN_READABLE:
                processors.append(structlog.dev.ConsoleRenderer())
            else:
                processors.append(structlog.processors.JSONRenderer())
        
        # Configure structlog with dynamic processor list
        structlog.configure(
            processors=processors,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
    
    @staticmethod
    def add_request_context(logger, method_name: str, event_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add request context without duplication.
        
        This processor injects request-scoped context into log entries,
        ensuring consistent correlation tracking across all log messages
        within a request.
        
        Args:
            logger: Logger instance
            method_name: Log method name
            event_dict: Event dictionary to process
            
        Returns:
            Enhanced event dictionary with request context
        """
        # Import here to avoid circular imports
        from faultmaven.infrastructure.logging.coordinator import request_context
        
        ctx = request_context.get()
        if ctx:
            # Only add if not already present to prevent duplication
            if 'correlation_id' not in event_dict:
                event_dict['correlation_id'] = ctx.correlation_id
            if 'session_id' not in event_dict and ctx.session_id:
                event_dict['session_id'] = ctx.session_id
            if 'user_id' not in event_dict and ctx.user_id:
                event_dict['user_id'] = ctx.user_id
            if 'case_id' not in event_dict and ctx.case_id:
                event_dict['case_id'] = ctx.case_id
            if 'agent_phase' not in event_dict and ctx.agent_phase:
                event_dict['agent_phase'] = ctx.agent_phase
                
        return event_dict
    
    @staticmethod
    def deduplicate_fields(logger, method_name: str, event_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Remove duplicate fields from log entries.
        
        This processor ensures that each field appears only once in the log entry,
        preventing cluttered logs with repeated information.
        
        Args:
            logger: Logger instance
            method_name: Log method name
            event_dict: Event dictionary to process
            
        Returns:
            Deduplicated event dictionary
        """
        seen = set()
        deduped = {}
        
        for key, value in event_dict.items():
            if key not in seen:
                deduped[key] = value
                seen.add(key)
                
        return deduped
    
    @staticmethod
    def add_trace_context(logger, method_name: str, event_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add OpenTelemetry trace context to log entries.
        
        This processor injects distributed tracing information into logs,
        enabling correlation between logs and traces in observability systems.
        
        Args:
            logger: Logger instance
            method_name: Log method name
            event_dict: Event dictionary to process
            
        Returns:
            Event dictionary with trace context
        """
        span = trace.get_current_span()
        if span and span.is_recording():
            span_context = span.get_span_context()
            # Add trace information only if not already present
            if 'trace_id' not in event_dict:
                event_dict['trace_id'] = format(span_context.trace_id, '032x')
            if 'span_id' not in event_dict:
                event_dict['span_id'] = format(span_context.span_id, '016x')
            
        return event_dict


# Singleton configuration instance
_logger_config: Optional[FaultMavenLogger] = None


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Get a configured logger instance.
    
    Factory function that ensures consistent logger configuration across
    the application. Uses singleton pattern to avoid reconfiguring structlog
    multiple times.
    
    Args:
        name: Logger name, typically module or class name
        
    Returns:
        Configured structlog BoundLogger instance
    
    Example:
        >>> logger = get_logger(__name__)
        >>> logger.info("Operation completed", operation="test", duration=0.123)
    """
    global _logger_config
    if _logger_config is None:
        _logger_config = FaultMavenLogger()
    
    return structlog.get_logger(name)