"""
FaultMaven Base Service Class

Provides a base class for all service layer components that includes
unified logging, error handling, and operation management patterns.
"""

from typing import Any, Callable, Dict, Optional, TypeVar, Union
import asyncio
from abc import ABC
from datetime import datetime, timezone

from faultmaven.infrastructure.logging.unified import get_unified_logger, UnifiedLogger
from faultmaven.exceptions import ValidationException, FaultMavenException


# Type variable for generic return types
T = TypeVar('T')


class BaseService(ABC):
    """
    Base class for all service layer components.
    
    This class provides common functionality for service layer components
    including unified logging, standardized error handling, and operation
    management patterns that integrate with the Phase 2 logging infrastructure.
    
    All service classes should inherit from this base class to ensure
    consistent logging and error handling patterns across the application.
    
    Attributes:
        service_name: Name of the service (derived from class name)
        logger: UnifiedLogger instance for the service layer
    """
    
    def __init__(self, service_name: Optional[str] = None):
        """
        Initialize base service with unified logging.
        
        Args:
            service_name: Optional custom service name. If not provided,
                         will use the class name in snake_case format.
        """
        # Generate service name from class name if not provided
        if service_name is None:
            # Convert CamelCase class name to snake_case
            class_name = self.__class__.__name__
            service_name = ''.join(['_' + c.lower() if c.isupper() and i > 0 
                                   else c.lower() for i, c in enumerate(class_name)])
        
        self.service_name = service_name
        self.logger = get_unified_logger(f"faultmaven.services.{service_name}", "service")
        
        # Log service initialization
        self.logger.log_event(
            event_type="system",
            event_name="service_initialized",
            severity="info",
            data={"service_name": self.service_name}
        )
    
    async def execute_operation(
        self,
        operation_name: str,
        operation_func: Callable[..., Union[T, any]],
        *args,
        validate_inputs: Optional[Callable[..., None]] = None,
        transform_result: Optional[Callable[[T], T]] = None,
        log_result: bool = True,
        **kwargs
    ) -> T:
        """
        Execute a service operation with unified logging and error handling.
        
        This method provides a standardized way to execute service operations
        with automatic logging, error handling, validation, and result transformation.
        
        Args:
            operation_name: Name of the operation being performed
            operation_func: The function to execute (can be sync or async)
            *args: Arguments to pass to the operation function
            validate_inputs: Optional function to validate inputs before execution
            transform_result: Optional function to transform the result before returning
            log_result: Whether to log operation results (default: True)
            **kwargs: Keyword arguments to pass to the operation function
            
        Returns:
            Result of the operation function, potentially transformed
            
        Raises:
            ValidationException: If input validation fails using ValidationException
            ValueError: If input validation fails using other exceptions  
            FileNotFoundError: If file operations fail
            FaultMavenException: If service operations fail (preserves original exception type)
            RuntimeError: If non-FaultMaven operation execution fails
            
        Example:
            >>> async def process_data(data: dict) -> dict:
            ...     return {"processed": data}
            >>> 
            >>> def validate_data(data: dict) -> None:
            ...     if not data:
            ...         raise ValueError("Data cannot be empty")
            >>> 
            >>> result = await self.execute_operation(
            ...     "process_user_data",
            ...     process_data,
            ...     {"user": "john"}, 
            ...     validate_inputs=lambda d: validate_data(d)
            ... )
        """
        # Log operation boundary - inbound
        self.logger.log_boundary(
            operation=operation_name,
            direction="inbound",
            data={
                "service": self.service_name,
                "args_count": len(args),
                "kwargs_keys": list(kwargs.keys()) if kwargs else []
            }
        )
        
        # Execute with unified operation logging
        async with self.logger.operation(operation_name, service=self.service_name) as ctx:
            try:
                # Input validation if provided
                if validate_inputs:
                    try:
                        if asyncio.iscoroutinefunction(validate_inputs):
                            await validate_inputs(*args, **kwargs)
                        else:
                            validate_inputs(*args, **kwargs)
                        ctx["validation"] = "passed"
                    except ValidationException as validation_error:
                        ctx["validation"] = "failed"
                        ctx["validation_error"] = str(validation_error)
                        self.logger.error(
                            f"Input validation failed for operation: {operation_name}",
                            validation_error=str(validation_error),
                            operation=operation_name,
                            service=self.service_name
                        )
                        raise  # Re-raise ValidationException as-is
                    except FileNotFoundError as validation_error:
                        ctx["validation"] = "failed"
                        ctx["validation_error"] = str(validation_error)
                        self.logger.error(
                            f"Input validation failed for operation: {operation_name}",
                            validation_error=str(validation_error),
                            operation=operation_name,
                            service=self.service_name
                        )
                        raise  # Re-raise FileNotFoundError as-is
                    except Exception as validation_error:
                        ctx["validation"] = "failed"
                        ctx["validation_error"] = str(validation_error)
                        self.logger.error(
                            f"Input validation failed for operation: {operation_name}",
                            validation_error=str(validation_error),
                            operation=operation_name,
                            service=self.service_name
                        )
                        raise ValueError(f"Validation failed: {str(validation_error)}") from validation_error
                
                # Execute the operation function
                ctx["execution_started"] = datetime.now(timezone.utc).isoformat()
                
                if asyncio.iscoroutinefunction(operation_func):
                    result = await operation_func(*args, **kwargs)
                else:
                    result = operation_func(*args, **kwargs)
                
                ctx["execution_completed"] = datetime.now(timezone.utc).isoformat()
                
                # Transform result if transformer provided
                if transform_result:
                    try:
                        if asyncio.iscoroutinefunction(transform_result):
                            result = await transform_result(result)
                        else:
                            result = transform_result(result)
                        ctx["transformation"] = "applied"
                    except Exception as transform_error:
                        ctx["transformation"] = "failed"
                        ctx["transform_error"] = str(transform_error)
                        self.logger.error(
                            f"Result transformation failed for operation: {operation_name}",
                            transform_error=str(transform_error),
                            operation=operation_name,
                            service=self.service_name
                        )
                        # Don't raise - continue with untransformed result
                        self.logger.warning(
                            f"Continuing with untransformed result for operation: {operation_name}",
                            operation=operation_name,
                            service=self.service_name
                        )
                
                # Log result information if enabled
                if log_result:
                    result_info = {
                        "type": type(result).__name__,
                        "size": len(str(result)) if result is not None else 0
                    }
                    
                    # Add specific information based on result type
                    if isinstance(result, dict):
                        result_info["keys"] = list(result.keys())
                        result_info["dict_size"] = len(result)
                    elif isinstance(result, (list, tuple)):
                        result_info["items_count"] = len(result)
                    elif isinstance(result, str):
                        result_info["string_length"] = len(result)
                    
                    ctx["result"] = result_info
                
                # Log successful operation boundary - outbound
                self.logger.log_boundary(
                    operation=operation_name,
                    direction="outbound",
                    data={
                        "service": self.service_name,
                        "success": True,
                        "result_type": type(result).__name__
                    }
                )
                
                # Log operation success event
                self.logger.log_event(
                    event_type="business",
                    event_name="operation_completed",
                    severity="info",
                    data={
                        "operation": operation_name,
                        "service": self.service_name,
                        "result_type": type(result).__name__
                    }
                )
                
                return result
                
            except ValidationException as validation_error:
                # Re-raise ValidationException directly for test compatibility
                raise
            except FileNotFoundError:
                # Re-raise FileNotFoundError exceptions without wrapping
                raise
            except FaultMavenException as fault_maven_error:
                # Re-raise all FaultMaven custom exceptions directly to preserve exception types
                # This includes ServiceException, AgentException, etc. for proper error handling
                
                # Update context with error information
                ctx["error"] = str(fault_maven_error)
                ctx["error_type"] = type(fault_maven_error).__name__
                
                # Log failed operation boundary - outbound
                self.logger.log_boundary(
                    operation=operation_name,
                    direction="outbound",
                    data={
                        "service": self.service_name,
                        "success": False,
                        "error": str(fault_maven_error),
                        "error_type": type(fault_maven_error).__name__
                    }
                )
                
                # Log operation failure event
                self.logger.log_event(
                    event_type="technical",
                    event_name="operation_failed",
                    severity="error",
                    data={
                        "operation": operation_name,
                        "service": self.service_name,
                        "error": str(fault_maven_error),
                        "error_type": type(fault_maven_error).__name__
                    }
                )
                
                # Re-raise the original FaultMaven exception to preserve type
                raise
            except Exception as operation_error:
                # Update context with error information
                ctx["error"] = str(operation_error)
                ctx["error_type"] = type(operation_error).__name__
                
                # Log failed operation boundary - outbound
                self.logger.log_boundary(
                    operation=operation_name,
                    direction="outbound",
                    data={
                        "service": self.service_name,
                        "success": False,
                        "error": str(operation_error),
                        "error_type": type(operation_error).__name__
                    }
                )
                
                # Log operation failure event
                self.logger.log_event(
                    event_type="technical",
                    event_name="operation_failed",
                    severity="error",
                    data={
                        "operation": operation_name,
                        "service": self.service_name,
                        "error": str(operation_error),
                        "error_type": type(operation_error).__name__
                    }
                )
                
                # Raise as RuntimeError with context for non-FaultMaven exceptions
                raise RuntimeError(
                    f"Service operation failed: {self.service_name}.{operation_name}: {str(operation_error)}"
                ) from operation_error
    
    def execute_operation_sync(
        self,
        operation_name: str,
        operation_func: Callable[..., T],
        *args,
        validate_inputs: Optional[Callable[..., None]] = None,
        transform_result: Optional[Callable[[T], T]] = None,
        log_result: bool = True,
        **kwargs
    ) -> T:
        """
        Synchronous version of execute_operation for non-async operations.
        
        This method provides the same functionality as execute_operation but
        for synchronous operations that don't require async/await.
        
        Args:
            operation_name: Name of the operation being performed
            operation_func: The synchronous function to execute
            *args: Arguments to pass to the operation function
            validate_inputs: Optional function to validate inputs before execution
            transform_result: Optional function to transform the result before returning
            log_result: Whether to log operation results (default: True)
            **kwargs: Keyword arguments to pass to the operation function
            
        Returns:
            Result of the operation function, potentially transformed
            
        Raises:
            ValidationException: If input validation fails using ValidationException
            ValueError: If input validation fails using other exceptions  
            FileNotFoundError: If file operations fail
            FaultMavenException: If service operations fail (preserves original exception type)
            RuntimeError: If non-FaultMaven operation execution fails
        """
        # Log operation boundary - inbound
        self.logger.log_boundary(
            operation=operation_name,
            direction="inbound",
            data={
                "service": self.service_name,
                "args_count": len(args),
                "kwargs_keys": list(kwargs.keys()) if kwargs else []
            }
        )
        
        # Execute with unified operation logging (synchronous)
        with self.logger.operation_sync(operation_name, service=self.service_name) as ctx:
            try:
                # Input validation if provided
                if validate_inputs:
                    try:
                        validate_inputs(*args, **kwargs)
                        ctx["validation"] = "passed"
                    except Exception as validation_error:
                        ctx["validation"] = "failed"
                        ctx["validation_error"] = str(validation_error)
                        self.logger.error(
                            f"Input validation failed for operation: {operation_name}",
                            validation_error=str(validation_error),
                            operation=operation_name,
                            service=self.service_name
                        )
                        raise ValueError(f"Validation failed: {str(validation_error)}") from validation_error
                
                # Execute the operation function
                ctx["execution_started"] = datetime.now(timezone.utc).isoformat()
                result = operation_func(*args, **kwargs)
                ctx["execution_completed"] = datetime.now(timezone.utc).isoformat()
                
                # Transform result if transformer provided
                if transform_result:
                    try:
                        result = transform_result(result)
                        ctx["transformation"] = "applied"
                    except Exception as transform_error:
                        ctx["transformation"] = "failed"
                        ctx["transform_error"] = str(transform_error)
                        self.logger.error(
                            f"Result transformation failed for operation: {operation_name}",
                            transform_error=str(transform_error),
                            operation=operation_name,
                            service=self.service_name
                        )
                        # Don't raise - continue with untransformed result
                        self.logger.warning(
                            f"Continuing with untransformed result for operation: {operation_name}",
                            operation=operation_name,
                            service=self.service_name
                        )
                
                # Log result information if enabled
                if log_result:
                    result_info = {
                        "type": type(result).__name__,
                        "size": len(str(result)) if result is not None else 0
                    }
                    
                    # Add specific information based on result type
                    if isinstance(result, dict):
                        result_info["keys"] = list(result.keys())
                        result_info["dict_size"] = len(result)
                    elif isinstance(result, (list, tuple)):
                        result_info["items_count"] = len(result)
                    elif isinstance(result, str):
                        result_info["string_length"] = len(result)
                    
                    ctx["result"] = result_info
                
                # Log successful operation boundary - outbound
                self.logger.log_boundary(
                    operation=operation_name,
                    direction="outbound",
                    data={
                        "service": self.service_name,
                        "success": True,
                        "result_type": type(result).__name__
                    }
                )
                
                # Log operation success event
                self.logger.log_event(
                    event_type="business",
                    event_name="operation_completed",
                    severity="info",
                    data={
                        "operation": operation_name,
                        "service": self.service_name,
                        "result_type": type(result).__name__
                    }
                )
                
                return result
                
            except ValidationException as validation_error:
                # Re-raise ValidationException directly for test compatibility
                raise
            except FileNotFoundError:
                # Re-raise FileNotFoundError exceptions without wrapping
                raise
            except FaultMavenException as fault_maven_error:
                # Re-raise all FaultMaven custom exceptions directly to preserve exception types
                # This includes ServiceException, AgentException, etc. for proper error handling
                
                # Update context with error information
                ctx["error"] = str(fault_maven_error)
                ctx["error_type"] = type(fault_maven_error).__name__
                
                # Log failed operation boundary - outbound
                self.logger.log_boundary(
                    operation=operation_name,
                    direction="outbound",
                    data={
                        "service": self.service_name,
                        "success": False,
                        "error": str(fault_maven_error),
                        "error_type": type(fault_maven_error).__name__
                    }
                )
                
                # Log operation failure event
                self.logger.log_event(
                    event_type="technical",
                    event_name="operation_failed",
                    severity="error",
                    data={
                        "operation": operation_name,
                        "service": self.service_name,
                        "error": str(fault_maven_error),
                        "error_type": type(fault_maven_error).__name__
                    }
                )
                
                # Re-raise the original FaultMaven exception to preserve type
                raise
            except Exception as operation_error:
                # Update context with error information
                ctx["error"] = str(operation_error)
                ctx["error_type"] = type(operation_error).__name__
                
                # Log failed operation boundary - outbound
                self.logger.log_boundary(
                    operation=operation_name,
                    direction="outbound",
                    data={
                        "service": self.service_name,
                        "success": False,
                        "error": str(operation_error),
                        "error_type": type(operation_error).__name__
                    }
                )
                
                # Log operation failure event
                self.logger.log_event(
                    event_type="technical",
                    event_name="operation_failed",
                    severity="error",
                    data={
                        "operation": operation_name,
                        "service": self.service_name,
                        "error": str(operation_error),
                        "error_type": type(operation_error).__name__
                    }
                )
                
                # Raise as RuntimeError with context for non-FaultMaven exceptions
                raise RuntimeError(
                    f"Service operation failed: {self.service_name}.{operation_name}: {str(operation_error)}"
                ) from operation_error
    
    def log_metric(
        self,
        metric_name: str,
        value: Union[int, float],
        unit: str = "count",
        tags: Optional[Dict[str, str]] = None,
        **extra_fields
    ) -> None:
        """
        Log a service metric with automatic service tagging.
        
        Args:
            metric_name: Name of the metric
            value: Metric value
            unit: Unit of measurement 
            tags: Optional metric tags
            **extra_fields: Additional fields
        """
        # Add service name to tags
        if tags is None:
            tags = {}
        tags["service"] = self.service_name
        
        self.logger.log_metric(
            metric_name=metric_name,
            value=value,
            unit=unit,
            tags=tags,
            **extra_fields
        )
    
    def log_business_event(
        self,
        event_name: str,
        severity: str = "info",
        data: Optional[Dict[str, Any]] = None,
        **extra_fields
    ) -> None:
        """
        Log a business event with service context.
        
        Args:
            event_name: Name of the business event
            severity: Event severity level
            data: Optional event data
            **extra_fields: Additional fields
        """
        if data is None:
            data = {}
        data["service"] = self.service_name
        
        self.logger.log_event(
            event_type="business",
            event_name=event_name,
            severity=severity,
            data=data,
            **extra_fields
        )
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Perform health check for the service.
        
        This method should be overridden by subclasses to provide
        service-specific health checking logic.
        
        Returns:
            Dictionary containing health status information
        """
        return {
            "service": self.service_name,
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "layer": "service"
        }