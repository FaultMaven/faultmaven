"""
Enhanced logging configuration for FaultMaven development.

This module provides structured logging with proper level management,
request correlation, and development-friendly features.
"""

import logging
import logging.config
import os
import sys
import json
from typing import Any, Dict, Optional
import uuid
from contextvars import ContextVar

# Request correlation context
request_id_context: ContextVar[Optional[str]] = ContextVar('request_id', default=None)

class CorrelationFilter(logging.Filter):
    """Add request correlation ID to log records."""
    
    def filter(self, record):
        record.correlation_id = request_id_context.get() or 'no-request'
        return True

class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured logging in production."""
    
    def format(self, record):
        log_entry = {
            'timestamp': self.formatTime(record, self.datefmt),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
            'correlation_id': getattr(record, 'correlation_id', 'unknown')
        }
        
        # Add exception info if present
        if record.exc_info:
            log_entry['exception'] = self.formatException(record.exc_info)
            
        # Add extra fields from the log record
        for key, value in record.__dict__.items():
            if key not in log_entry and not key.startswith('_'):
                log_entry[key] = value
                
        return json.dumps(log_entry)

class DevelopmentFormatter(logging.Formatter):
    """Enhanced console formatter for development."""
    
    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green  
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[35m', # Magenta
    }
    RESET = '\033[0m'
    
    def format(self, record):
        # Add color coding for console output
        if hasattr(record, 'correlation_id'):
            correlation = f"[{record.correlation_id[:8]}]"
        else:
            correlation = "[no-req]"
            
        color = self.COLORS.get(record.levelname, '')
        reset = self.RESET if color else ''
        
        # Enhanced format with correlation, module context, and colors
        log_format = (
            f"{color}%(asctime)s {correlation} "
            f"%(levelname)-8s{reset} "
            f"%(name)-30s | %(message)s"
        )
        
        formatter = logging.Formatter(log_format, datefmt='%H:%M:%S')
        return formatter.format(record)

def setup_logging(
    log_level: str = None,
    environment: str = None,
    structured: bool = None,
    log_file: str = None
) -> None:
    """
    Configure enhanced logging for FaultMaven.
    
    Args:
        log_level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        environment: Environment type (development, production, testing)
        structured: Force structured JSON logging
        log_file: Optional log file path
    """
    # Determine configuration from environment
    log_level = log_level or os.getenv('LOG_LEVEL', 'INFO').upper()
    environment = environment or os.getenv('ENVIRONMENT', 'development').lower()
    structured = structured if structured is not None else (environment == 'production')
    log_file = log_file or os.getenv('LOG_FILE')
    
    # Validate log level
    numeric_level = getattr(logging, log_level, logging.INFO)
    
    # Create formatters
    if structured:
        formatter = StructuredFormatter()
    else:
        formatter = DevelopmentFormatter()
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(CorrelationFilter())
    root_logger.addHandler(console_handler)
    
    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(StructuredFormatter())  # Always JSON for files
        file_handler.addFilter(CorrelationFilter())
        root_logger.addHandler(file_handler)
    
    # Configure third-party loggers
    _configure_third_party_loggers(log_level)
    
    # Log configuration details
    logger = logging.getLogger(__name__)
    logger.info(f"Logging configured: level={log_level}, environment={environment}, structured={structured}")

def _configure_third_party_loggers(log_level: str):
    """Configure third-party library loggers to reduce noise."""
    
    # Reduce uvicorn access logs in development
    if log_level == 'DEBUG':
        logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    else:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    
    # Note: Presidio PII protection now handled by K8s microservice (no local library logging)
    
    # Control ChromaDB noise  
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.INFO)
    
    # Control HTTP client noise
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
    # Keep important ones visible
    logging.getLogger("faultmaven").setLevel(log_level)

def get_logger(name: str) -> logging.Logger:
    """
    Get a properly configured logger instance.
    
    Args:
        name: Logger name (usually __name__)
        
    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)

def set_request_id(request_id: str = None) -> str:
    """
    Set correlation ID for current request context.
    
    Args:
        request_id: Optional request ID, generates one if not provided
        
    Returns:
        The request ID that was set
    """
    if not request_id:
        request_id = str(uuid.uuid4())
    
    request_id_context.set(request_id)
    return request_id

def get_request_id() -> Optional[str]:
    """Get the current request correlation ID."""
    return request_id_context.get()

# Development-specific log helpers
class LogContext:
    """Context manager for enhanced logging with metadata."""
    
    def __init__(self, logger: logging.Logger, operation: str, **kwargs):
        self.logger = logger
        self.operation = operation
        self.metadata = kwargs
        self.start_time = None
        
    def __enter__(self):
        import time
        self.start_time = time.time()
        self.logger.info(f"Starting {self.operation}", extra=self.metadata)
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        import time
        duration = time.time() - self.start_time
        
        if exc_type:
            self.logger.error(
                f"Failed {self.operation} in {duration:.3f}s: {exc_val}",
                extra={**self.metadata, 'duration_seconds': duration},
                exc_info=True
            )
        else:
            self.logger.info(
                f"Completed {self.operation} in {duration:.3f}s",
                extra={**self.metadata, 'duration_seconds': duration}
            )