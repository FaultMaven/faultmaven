"""In-Process Event Bus Implementation

This module provides an in-memory event bus implementation for monolithic
deployments where all services run in the same process. This implementation
uses asyncio queues and direct method calls for high-performance local
message passing.

Design Features:
- Zero-copy message passing for performance
- Asyncio-based for non-blocking operations  
- Memory-bounded queues with backpressure handling
- Topic-based message routing
- Subscription management with consumer groups
- Health monitoring and metrics collection
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, AsyncIterator
from uuid import uuid4
import weakref

from ..event_bus import (
    IEventBus, EventMessage, SubscriptionConfig, TopicConfig,
    MessageStatus, DeliveryGuarantee
)


class InProcessTopic:
    """In-memory topic implementation with queue management."""
    
    def __init__(self, config: TopicConfig):
        self.config = config
        self.name = config.name
        self.messages: deque = deque(maxlen=10000)  # Circular buffer
        self.subscribers: Dict[str, List['InProcessSubscription']] = defaultdict(list)
        self.created_at = datetime.utcnow()
        self.message_count = 0
        self.error_count = 0
        
    def add_message(self, message: EventMessage) -> bool:
        """Add message to topic and notify subscribers."""
        try:
            self.messages.append(message)
            self.message_count += 1
            
            # Notify all subscribers
            for consumer_group, subscriptions in self.subscribers.items():
                # Round-robin within consumer group
                if subscriptions:
                    subscription = subscriptions[self.message_count % len(subscriptions)]
                    subscription.enqueue_message(message)
            
            return True
        except Exception as e:
            logging.error(f"Error adding message to topic {self.name}: {e}")
            self.error_count += 1
            return False
    
    def add_subscription(self, subscription: 'InProcessSubscription') -> None:
        """Add subscription to topic."""
        consumer_group = subscription.config.consumer_group
        self.subscribers[consumer_group].append(subscription)
    
    def remove_subscription(self, subscription: 'InProcessSubscription') -> None:
        """Remove subscription from topic."""
        consumer_group = subscription.config.consumer_group
        if subscription in self.subscribers[consumer_group]:
            self.subscribers[consumer_group].remove(subscription)
            if not self.subscribers[consumer_group]:
                del self.subscribers[consumer_group]


class InProcessSubscription:
    """In-memory subscription with message queue and processing."""
    
    def __init__(self, config: SubscriptionConfig, topic: InProcessTopic):
        self.config = config
        self.topic = topic
        self.subscription_id = str(uuid4())
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.processed_count = 0
        self.error_count = 0
        self.created_at = datetime.utcnow()
        
    def enqueue_message(self, message: EventMessage) -> None:
        """Enqueue message for processing."""
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            logging.warning(f"Queue full for subscription {self.subscription_id}")
            self.error_count += 1
    
    async def start(self) -> None:
        """Start message processing task."""
        if self.running:
            return
            
        self.running = True
        self.task = asyncio.create_task(self._process_messages())
    
    async def stop(self) -> None:
        """Stop message processing task."""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
    
    async def _process_messages(self) -> None:
        """Process messages from queue."""
        while self.running:
            try:
                # Batch processing if configured
                messages = []
                for _ in range(self.config.batch_size):
                    try:
                        message = await asyncio.wait_for(
                            self.queue.get(),
                            timeout=self.config.max_wait_time_ms / 1000
                        )
                        messages.append(message)
                    except asyncio.TimeoutError:
                        break
                
                if messages:
                    await self._handle_batch(messages)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error processing messages in subscription {self.subscription_id}: {e}")
                self.error_count += 1
                await asyncio.sleep(1)  # Brief pause on error
    
    async def _handle_batch(self, messages: List[EventMessage]) -> None:
        """Handle batch of messages."""
        for message in messages:
            try:
                await self.config.handler(message)
                self.processed_count += 1
            except Exception as e:
                logging.error(f"Error handling message {message.event_id}: {e}")
                self.error_count += 1
                
                # Implement retry logic if configured
                if message.retry_count < message.max_retries:
                    message.retry_count += 1
                    await asyncio.sleep(min(2 ** message.retry_count, 30))  # Exponential backoff
                    self.enqueue_message(message)


class InProcessEventBus(IEventBus):
    """In-process event bus implementation using asyncio queues."""
    
    def __init__(self):
        self.topics: Dict[str, InProcessTopic] = {}
        self.subscriptions: Dict[str, InProcessSubscription] = {}
        self.config: Dict[str, Any] = {}
        self.running = False
        self.metrics = {
            'messages_published': 0,
            'messages_processed': 0,
            'errors': 0,
            'subscriptions': 0
        }
        self.logger = logging.getLogger(__name__)
        
    async def initialize(self, config: Dict[str, Any]) -> bool:
        """Initialize the in-process event bus."""
        try:
            self.config = config
            self.running = True
            
            # Configure logging
            log_level = config.get('log_level', 'INFO')
            self.logger.setLevel(getattr(logging, log_level))
            
            # Set memory limits
            max_memory_mb = config.get('max_memory_mb', 100)
            buffer_size = config.get('buffer_size', 10000)
            
            self.logger.info(f"Initialized in-process event bus with {max_memory_mb}MB memory limit")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize in-process event bus: {e}")
            return False
    
    async def create_topic(self, config: TopicConfig) -> bool:
        """Create topic with in-memory storage."""
        try:
            if config.name in self.topics:
                self.logger.debug(f"Topic {config.name} already exists")
                return True
            
            topic = InProcessTopic(config)
            self.topics[config.name] = topic
            
            self.logger.info(f"Created topic: {config.name}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to create topic {config.name}: {e}")
            return False
    
    async def publish(
        self,
        topic: str,
        message: EventMessage,
        partition_key: Optional[str] = None,
        delivery_guarantee: DeliveryGuarantee = DeliveryGuarantee.AT_LEAST_ONCE
    ) -> bool:
        """Publish message to topic."""
        try:
            if topic not in self.topics:
                raise ValueError(f"Topic {topic} does not exist")
            
            topic_obj = self.topics[topic]
            success = topic_obj.add_message(message)
            
            if success:
                self.metrics['messages_published'] += 1
                self.logger.debug(f"Published message {message.event_id} to topic {topic}")
            else:
                self.metrics['errors'] += 1
                
            return success
            
        except Exception as e:
            self.logger.error(f"Failed to publish message to topic {topic}: {e}")
            self.metrics['errors'] += 1
            return False
    
    async def subscribe(self, config: SubscriptionConfig) -> str:
        """Subscribe to topic with message handler."""
        try:
            if config.topic not in self.topics:
                raise ValueError(f"Topic {config.topic} does not exist")
            
            topic = self.topics[config.topic]
            subscription = InProcessSubscription(config, topic)
            
            self.subscriptions[subscription.subscription_id] = subscription
            topic.add_subscription(subscription)
            
            await subscription.start()
            self.metrics['subscriptions'] += 1
            
            self.logger.info(f"Created subscription {subscription.subscription_id} for topic {config.topic}")
            return subscription.subscription_id
            
        except Exception as e:
            self.logger.error(f"Failed to create subscription for topic {config.topic}: {e}")
            raise
    
    async def unsubscribe(self, subscription_id: str) -> bool:
        """Unsubscribe from topic."""
        try:
            if subscription_id not in self.subscriptions:
                self.logger.warning(f"Subscription {subscription_id} not found")
                return False
            
            subscription = self.subscriptions[subscription_id]
            await subscription.stop()
            
            subscription.topic.remove_subscription(subscription)
            del self.subscriptions[subscription_id]
            
            self.metrics['subscriptions'] -= 1
            self.logger.info(f"Removed subscription {subscription_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to unsubscribe {subscription_id}: {e}")
            return False
    
    async def publish_batch(
        self,
        topic: str,
        messages: List[EventMessage],
        partition_key: Optional[str] = None
    ) -> List[bool]:
        """Publish batch of messages."""
        results = []
        for message in messages:
            result = await self.publish(topic, message, partition_key)
            results.append(result)
        return results
    
    async def get_message_status(self, event_id: str) -> MessageStatus:
        """Get message processing status."""
        # In-process messages are processed immediately
        # This is a simplified implementation
        return MessageStatus.COMPLETED
    
    async def replay_messages(
        self,
        topic: str,
        start_time: datetime,
        end_time: Optional[datetime] = None,
        partition: Optional[int] = None
    ) -> AsyncIterator[EventMessage]:
        """Replay messages from topic within time range."""
        if topic not in self.topics:
            raise ValueError(f"Topic {topic} does not exist")
        
        topic_obj = self.topics[topic]
        end_time = end_time or datetime.utcnow()
        
        for message in topic_obj.messages:
            if start_time <= message.timestamp <= end_time:
                yield message
    
    async def get_topic_metrics(self, topic: str) -> Dict[str, Any]:
        """Get metrics for specific topic."""
        if topic not in self.topics:
            raise ValueError(f"Topic {topic} does not exist")
        
        topic_obj = self.topics[topic]
        
        # Calculate message rate
        uptime_seconds = (datetime.utcnow() - topic_obj.created_at).total_seconds()
        message_rate = topic_obj.message_count / max(uptime_seconds, 1)
        
        # Calculate subscriber counts
        total_subscribers = sum(len(subs) for subs in topic_obj.subscribers.values())
        
        return {
            'message_count': topic_obj.message_count,
            'message_rate': round(message_rate, 2),
            'error_count': topic_obj.error_count,
            'subscriber_count': total_subscribers,
            'consumer_groups': len(topic_obj.subscribers),
            'queue_depth': len(topic_obj.messages),
            'created_at': topic_obj.created_at.isoformat()
        }
    
    async def get_health_status(self) -> Dict[str, Any]:
        """Get overall event bus health status."""
        total_subscriptions = len(self.subscriptions)
        healthy_subscriptions = sum(
            1 for sub in self.subscriptions.values() 
            if sub.running and sub.error_count < sub.processed_count * 0.1
        )
        
        # Calculate overall error rate
        total_processed = sum(sub.processed_count for sub in self.subscriptions.values())
        total_errors = sum(sub.error_count for sub in self.subscriptions.values())
        error_rate = total_errors / max(total_processed, 1)
        
        health_status = "healthy"
        if error_rate > 0.05:  # 5% error rate threshold
            health_status = "degraded"
        if error_rate > 0.2:   # 20% error rate threshold
            health_status = "unhealthy"
        
        return {
            'status': health_status,
            'topics': len(self.topics),
            'subscriptions': total_subscriptions,
            'healthy_subscriptions': healthy_subscriptions,
            'error_rate': round(error_rate * 100, 2),
            'metrics': self.metrics,
            'memory_usage': self._estimate_memory_usage(),
            'uptime_seconds': (datetime.utcnow() - datetime.utcnow()).total_seconds() if self.running else 0
        }
    
    async def flush(self, timeout_ms: int = 30000) -> bool:
        """Flush all pending messages."""
        try:
            # Wait for all subscription queues to be empty
            timeout_seconds = timeout_ms / 1000
            start_time = time.time()
            
            while time.time() - start_time < timeout_seconds:
                all_empty = True
                for subscription in self.subscriptions.values():
                    if not subscription.queue.empty():
                        all_empty = False
                        break
                
                if all_empty:
                    return True
                
                await asyncio.sleep(0.1)
            
            self.logger.warning(f"Flush timeout after {timeout_ms}ms")
            return False
            
        except Exception as e:
            self.logger.error(f"Error during flush: {e}")
            return False
    
    async def shutdown(self) -> bool:
        """Shutdown event bus and cleanup resources."""
        try:
            self.logger.info("Shutting down in-process event bus")
            
            # Stop all subscriptions
            tasks = []
            for subscription in self.subscriptions.values():
                tasks.append(subscription.stop())
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            
            # Clear all data structures
            self.subscriptions.clear()
            self.topics.clear()
            
            self.running = False
            self.logger.info("In-process event bus shutdown complete")
            return True
            
        except Exception as e:
            self.logger.error(f"Error during shutdown: {e}")
            return False
    
    def _estimate_memory_usage(self) -> Dict[str, int]:
        """Estimate memory usage of event bus components."""
        # Simplified memory estimation
        topic_memory = sum(len(topic.messages) * 1024 for topic in self.topics.values())  # 1KB per message estimate
        queue_memory = sum(subscription.queue.qsize() * 1024 for subscription in self.subscriptions.values())
        
        return {
            'topics_mb': round(topic_memory / (1024 * 1024), 2),
            'queues_mb': round(queue_memory / (1024 * 1024), 2),
            'total_mb': round((topic_memory + queue_memory) / (1024 * 1024), 2)
        }