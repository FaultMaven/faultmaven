"""Event Bus Interface for FaultMaven Microservice Architecture

This module defines the event bus abstraction that supports both in-process
and distributed messaging patterns. The interface enables seamless transition
from monolithic to microservice architecture without code changes.

Design Principles:
- Unified interface for both in-memory and Kafka implementations
- Reliable message delivery with dead letter queue support
- Schema validation and message serialization
- Backpressure handling and flow control
- Comprehensive observability and monitoring
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Callable, AsyncIterator
from enum import Enum
from dataclasses import dataclass
from datetime import datetime


class MessageStatus(Enum):
    """Message processing status enumeration."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class DeliveryGuarantee(Enum):
    """Message delivery guarantee levels."""
    AT_MOST_ONCE = "at_most_once"      # Fire and forget
    AT_LEAST_ONCE = "at_least_once"    # May duplicate
    EXACTLY_ONCE = "exactly_once"      # Exactly once delivery


@dataclass
class EventMessage:
    """Standard event message format."""
    event_id: str
    event_type: str
    source_service: str
    timestamp: datetime
    payload: Dict[str, Any]
    correlation_id: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    ttl_seconds: Optional[int] = None
    headers: Optional[Dict[str, str]] = None


@dataclass
class SubscriptionConfig:
    """Event subscription configuration."""
    topic: str
    consumer_group: str
    handler: Callable[[EventMessage], Any]
    delivery_guarantee: DeliveryGuarantee = DeliveryGuarantee.AT_LEAST_ONCE
    batch_size: int = 1
    max_wait_time_ms: int = 5000
    auto_commit: bool = True
    retry_policy: Optional[Dict[str, Any]] = None


@dataclass
class TopicConfig:
    """Topic configuration for event bus."""
    name: str
    partitions: int = 1
    replication_factor: int = 1
    retention_ms: int = 604800000  # 7 days
    cleanup_policy: str = "delete"
    schema_validation: bool = True
    dead_letter_enabled: bool = True


class IEventBus(ABC):
    """Event Bus Interface for microservice communication.
    
    This interface abstracts event-driven communication between services,
    supporting both in-process (memory-based) and distributed (Kafka-based)
    implementations. The interface provides reliable message delivery,
    schema validation, and comprehensive monitoring capabilities.
    
    Key Features:
    - Unified interface for local and distributed messaging
    - Schema-based message validation
    - Dead letter queue support for failed messages
    - Backpressure handling and flow control
    - Message ordering guarantees where needed
    - Comprehensive metrics and monitoring
    
    Implementation Modes:
    - In-Process: Memory-based for monolithic deployments
    - Distributed: Kafka-based for microservice deployments
    - Hybrid: Mixed mode supporting gradual migration
    
    Message Topics (from blueprint):
    - agent_requests: Requests to specialist agents
    - agent_responses: Responses from agents back to orchestrator
    - decision_records: Orchestrator decision records for observability
    - learning_events: Events for the learning agent batch processing
    - confidence_updates: Confidence model updates and calibration
    - policy_decisions: Policy evaluation results and confirmations
    - loop_detections: Loop/stall detection events for monitoring
    """
    
    @abstractmethod
    async def initialize(self, config: Dict[str, Any]) -> bool:
        """Initialize event bus with configuration.
        
        Args:
            config: Event bus configuration including:
                   - mode: 'in_process' or 'distributed'
                   - connection_params: Connection parameters for distributed mode
                   - topic_configs: Topic configurations
                   - retry_policies: Retry and dead letter policies
                   - monitoring_config: Metrics and monitoring setup
                   
        Returns:
            True if initialization successful, False otherwise
            
        Configuration Examples:
            In-Process Mode:
            {
                'mode': 'in_process',
                'buffer_size': 10000,
                'max_memory_mb': 100
            }
            
            Distributed Mode:
            {
                'mode': 'distributed', 
                'kafka_brokers': ['localhost:9092'],
                'schema_registry_url': 'http://localhost:8081',
                'consumer_group_prefix': 'faultmaven'
            }
        """
        pass
    
    @abstractmethod
    async def create_topic(self, config: TopicConfig) -> bool:
        """Create topic with specified configuration.
        
        Args:
            config: Topic configuration including partitions, retention, etc.
            
        Returns:
            True if topic created successfully or already exists
            
        Notes:
            - In-process mode creates in-memory topic structures
            - Distributed mode creates Kafka topics with proper configuration
            - Topic creation is idempotent (safe to call multiple times)
        """
        pass
    
    @abstractmethod
    async def publish(
        self, 
        topic: str, 
        message: EventMessage,
        partition_key: Optional[str] = None,
        delivery_guarantee: DeliveryGuarantee = DeliveryGuarantee.AT_LEAST_ONCE
    ) -> bool:
        """Publish event message to specified topic.
        
        Args:
            topic: Topic name for message publication
            message: EventMessage to publish
            partition_key: Optional key for message partitioning/ordering
            delivery_guarantee: Required delivery guarantee level
            
        Returns:
            True if message published successfully
            
        Raises:
            TopicNotFoundException: When topic doesn't exist
            MessageValidationException: When message fails schema validation
            PublishException: When publish operation fails
            BackpressureException: When system is under too much load
            
        Implementation Notes:
            - Messages are validated against topic schema if configured
            - Partition key ensures messages go to same partition for ordering
            - Delivery guarantees affect acknowledgment requirements
            - Backpressure handling prevents system overload
        """
        pass
    
    @abstractmethod
    async def subscribe(
        self, 
        config: SubscriptionConfig
    ) -> str:
        """Subscribe to topic with message handler.
        
        Args:
            config: Subscription configuration including topic, handler, and options
            
        Returns:
            Subscription identifier for management operations
            
        Raises:
            TopicNotFoundException: When topic doesn't exist
            SubscriptionException: When subscription setup fails
            
        Handler Contract:
            The handler function should:
            - Accept EventMessage as single parameter
            - Return None for success, raise exception for failure
            - Be idempotent (safe to call multiple times with same message)
            - Handle partial failures gracefully
            
        Example:
            async def handle_agent_request(message: EventMessage):
                agent_id = message.payload['agent_id']
                request = message.payload['request']
                # Process agent request...
                
            subscription_id = await bus.subscribe(SubscriptionConfig(
                topic='agent_requests',
                consumer_group='orchestrator',
                handler=handle_agent_request
            ))
        """
        pass
    
    @abstractmethod
    async def unsubscribe(self, subscription_id: str) -> bool:
        """Unsubscribe from topic using subscription identifier.
        
        Args:
            subscription_id: Subscription identifier from subscribe()
            
        Returns:
            True if unsubscribe successful
            
        Notes:
            - Gracefully handles in-flight messages
            - Commits processed message offsets
            - Cleans up subscription resources
        """
        pass
    
    @abstractmethod
    async def publish_batch(
        self, 
        topic: str, 
        messages: List[EventMessage],
        partition_key: Optional[str] = None
    ) -> List[bool]:
        """Publish batch of messages for improved performance.
        
        Args:
            topic: Topic name for batch publication
            messages: List of EventMessage objects to publish
            partition_key: Optional partition key for all messages
            
        Returns:
            List of success status for each message (True/False)
            
        Notes:
            - More efficient than individual publish calls
            - Partial failures are supported (some succeed, some fail)
            - Failed messages can be retried individually
            - Batch size limits apply to prevent memory issues
        """
        pass
    
    @abstractmethod
    async def get_message_status(self, event_id: str) -> MessageStatus:
        """Get processing status of specific message.
        
        Args:
            event_id: Message identifier from EventMessage
            
        Returns:
            Current message processing status
            
        Notes:
            - Useful for tracking critical messages
            - Status tracking duration is limited by retention policy
            - Dead letter messages retain status longer for investigation
        """
        pass
    
    @abstractmethod
    async def replay_messages(
        self, 
        topic: str, 
        start_time: datetime, 
        end_time: Optional[datetime] = None,
        partition: Optional[int] = None
    ) -> AsyncIterator[EventMessage]:
        """Replay messages from topic within time range.
        
        Args:
            topic: Topic to replay messages from
            start_time: Start time for message replay
            end_time: Optional end time (defaults to now)
            partition: Optional specific partition to replay
            
        Yields:
            EventMessage objects in chronological order
            
        Use Cases:
            - Disaster recovery and data reconstruction
            - Debugging and troubleshooting
            - Re-processing after bug fixes
            - Audit trail investigation
            
        Notes:
            - Replay respects topic retention policies
            - Large replay operations may impact performance
            - Consider using partition filtering for better performance
        """
        pass
    
    @abstractmethod
    async def get_topic_metrics(self, topic: str) -> Dict[str, Any]:
        """Get metrics for specific topic.
        
        Args:
            topic: Topic name
            
        Returns:
            Topic metrics including:
            - message_count: Total messages in topic
            - message_rate: Messages per second
            - consumer_lag: Consumer processing lag
            - error_rate: Message processing error rate
            - partition_distribution: Message distribution across partitions
            - dead_letter_count: Messages in dead letter queue
            
        Example:
            metrics = await bus.get_topic_metrics('agent_requests')
            print(f"Message rate: {metrics['message_rate']} msg/s")
            print(f"Consumer lag: {metrics['consumer_lag']} messages")
        """
        pass
    
    @abstractmethod
    async def get_health_status(self) -> Dict[str, Any]:
        """Get overall event bus health status.
        
        Returns:
            Health status including:
            - status: healthy/degraded/unhealthy
            - connection_status: Connection to underlying infrastructure
            - topic_health: Per-topic health status
            - consumer_health: Consumer group health
            - error_rates: Recent error rates by topic
            - resource_utilization: Memory and CPU usage
            
        Health Indicators:
            - Connection availability and latency
            - Message processing rates and backlogs
            - Error rates and dead letter queue growth
            - Resource utilization and capacity
        """
        pass
    
    @abstractmethod
    async def flush(self, timeout_ms: int = 30000) -> bool:
        """Flush all pending messages with timeout.
        
        Args:
            timeout_ms: Maximum wait time in milliseconds
            
        Returns:
            True if all messages flushed successfully within timeout
            
        Notes:
            - Ensures all published messages are delivered
            - Useful during graceful shutdown
            - Timeout prevents indefinite blocking
            - May not guarantee processing by consumers
        """
        pass
    
    @abstractmethod
    async def shutdown(self) -> bool:
        """Gracefully shutdown event bus and cleanup resources.
        
        Returns:
            True if shutdown completed successfully
            
        Shutdown Process:
            - Stop accepting new messages
            - Flush pending messages
            - Complete in-flight message processing
            - Unsubscribe all consumers
            - Close connections and cleanup resources
            
        Notes:
            - Should be called during application shutdown
            - May take time to complete gracefully
            - Force shutdown after timeout to prevent hanging
        """
        pass


# Event Bus Factory for creating appropriate implementation
class EventBusFactory:
    """Factory for creating event bus implementations based on configuration."""
    
    @staticmethod
    async def create_event_bus(config: Dict[str, Any]) -> IEventBus:
        """Create event bus implementation based on configuration.
        
        Args:
            config: Configuration specifying event bus type and parameters
            
        Returns:
            IEventBus implementation (InProcessEventBus or KafkaEventBus)
            
        Configuration:
            {
                'mode': 'in_process' | 'distributed',
                # Additional mode-specific configuration...
            }
        """
        mode = config.get('mode', 'in_process')
        
        if mode == 'in_process':
            from .implementations.in_process_event_bus import InProcessEventBus
            bus = InProcessEventBus()
        elif mode == 'distributed':
            from .implementations.kafka_event_bus import KafkaEventBus
            bus = KafkaEventBus()
        else:
            raise ValueError(f"Unknown event bus mode: {mode}")
        
        await bus.initialize(config)
        return bus


# Standard Event Types for FaultMaven
class FaultMavenEvents:
    """Standard event types used throughout FaultMaven microservices."""
    
    # Orchestrator Events
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    AGENT_SELECTED = "agent.selected"
    BUDGET_EXCEEDED = "budget.exceeded"
    
    # Agent Events
    AGENT_REQUEST = "agent.request"
    AGENT_RESPONSE = "agent.response"
    AGENT_TIMEOUT = "agent.timeout"
    AGENT_ERROR = "agent.error"
    
    # Confidence Events
    CONFIDENCE_SCORED = "confidence.scored"
    CONFIDENCE_MODEL_UPDATED = "confidence.model.updated"
    CONFIDENCE_CALIBRATION = "confidence.calibration"
    
    # Policy Events
    POLICY_EVALUATED = "policy.evaluated"
    ACTION_APPROVED = "action.approved"
    ACTION_DENIED = "action.denied"
    
    # Learning Events
    OUTCOME_RECORDED = "outcome.recorded"
    PATTERN_LEARNED = "pattern.learned"
    KNOWLEDGE_UPDATED = "knowledge.updated"
    
    # Loop Detection Events  
    LOOP_DETECTED = "loop.detected"
    RECOVERY_INITIATED = "recovery.initiated"
    
    # System Events
    SERVICE_STARTED = "service.started"
    SERVICE_STOPPED = "service.stopped"
    HEALTH_CHECK_FAILED = "health.check.failed"