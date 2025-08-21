"""Event Bus Implementation Modules

This package contains the concrete implementations of the IEventBus interface
for both in-process and distributed deployment modes.

Implementations:
- InProcessEventBus: Memory-based implementation for monolithic deployments
- KafkaEventBus: Kafka-based implementation for distributed microservices
"""

from .in_process_event_bus import InProcessEventBus
from .kafka_event_bus import KafkaEventBus

__all__ = ['InProcessEventBus', 'KafkaEventBus']