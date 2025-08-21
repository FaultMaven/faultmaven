"""Decision Records & Telemetry Service - Phase A Implementation

This module implements decision record emission and structured logging integration
with the existing Opik tracing system for comprehensive observability and audit trails.

Key Features:
- Decision record emission for all orchestrator decisions
- Structured logging with correlation IDs and session context
- Integration with existing Opik tracing infrastructure
- Comprehensive audit trails with retention policies
- Performance metrics and SLA monitoring
- Event emission for downstream processing

Implementation Notes:
- Leverages existing LoggingCoordinator and Opik integration
- Thread-safe decision record storage and emission
- Correlation ID propagation across service boundaries
- Structured JSON logging with rich metadata
- Configurable retention and aggregation policies
- Health monitoring and alerting integration
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4
from threading import RLock
import time

from faultmaven.models.microservice_contracts.core_contracts import DecisionRecord, TurnContext
from faultmaven.models.interfaces import ITracer
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ServiceException


class DecisionRecorder:
    """
    Decision Records & Telemetry Service
    
    Provides comprehensive decision recording and telemetry with:
    - Decision record emission and storage
    - Structured logging integration
    - Correlation ID tracking across services
    - Performance metrics and SLA monitoring
    - Event emission for analytics and monitoring
    """

    def __init__(
        self,
        tracer: Optional[ITracer] = None,
        retention_days: int = 90,
        enable_structured_logging: bool = True,
        enable_performance_tracking: bool = True,
        batch_size: int = 100,
        flush_interval_seconds: int = 60
    ):
        """
        Initialize decision recorder
        
        Args:
            tracer: Optional tracer for Opik integration
            retention_days: Days to retain decision records
            enable_structured_logging: Whether to enable structured logging
            enable_performance_tracking: Whether to track performance metrics
            batch_size: Batch size for record processing
            flush_interval_seconds: Interval for flushing batched records
        """
        self._logger = logging.getLogger(self.__class__.__name__)
        self._tracer = tracer
        self._retention_period = timedelta(days=retention_days)
        self._enable_structured_logging = enable_structured_logging
        self._enable_performance_tracking = enable_performance_tracking
        self._batch_size = batch_size
        self._flush_interval = flush_interval_seconds
        
        # Thread-safe storage
        self._storage_lock = RLock()
        
        # In-memory storage (would be replaced with persistent storage in production)
        self._decision_records = {}  # record_id -> DecisionRecord
        self._session_records = {}   # session_id -> List[record_ids]
        self._batch_queue = []       # Records waiting to be processed
        
        # Performance metrics
        self._metrics = {
            'records_created': 0,
            'records_emitted': 0,
            'records_processed': 0,
            'avg_processing_time_ms': 0.0,
            'total_processing_time_ms': 0.0,
            'correlation_ids_tracked': 0,
            'sessions_tracked': 0,
            'performance_violations': 0
        }
        
        # SLA thresholds
        self._sla_thresholds = {
            'max_processing_time_ms': 100,
            'max_emission_latency_ms': 50,
            'max_batch_processing_time_ms': 500
        }
        
        # Active correlation IDs for request tracking
        self._active_correlations = {}  # correlation_id -> metadata
        
        # Performance tracking
        if self._enable_performance_tracking:
            self._performance_data = []
            
        # Start background processing
        self._processing_task = None
        self._start_background_processing()
        
        self._logger.info("✅ Decision recorder initialized with Opik integration")

    def _start_background_processing(self):
        """Start background task for processing batched records"""
        try:
            loop = asyncio.get_event_loop()
            self._processing_task = loop.create_task(self._background_processor())
        except RuntimeError:
            # No event loop running - will process synchronously
            self._logger.debug("No event loop found - background processing disabled")
            self._processing_task = None

    async def _background_processor(self):
        """Background task to process batched records"""
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._process_batch()
                await self._cleanup_expired_records()
            except Exception as e:
                self._logger.error(f"Background processing error: {e}")

    @trace("decision_recorder_create_record")
    async def create_decision_record(
        self,
        session_id: str,
        turn_id: str,
        turn: int,
        selected_agents: List[str],
        routing_rationale: str,
        features: Dict[str, float],
        confidence: Dict[str, Any],
        budget_allocated: Dict[str, Any],
        budget_used: Dict[str, Any],
        latency_ms: int,
        agent_latencies: Optional[Dict[str, int]] = None,
        agent_results: Optional[Dict[str, Any]] = None,
        final_response: str = "",
        status: str = "completed",
        errors: Optional[List[Dict[str, Any]]] = None,
        correlation_id: Optional[str] = None
    ) -> str:
        """
        Create comprehensive decision record
        
        Args:
            session_id: Session identifier
            turn_id: Turn identifier
            turn: Turn number in session
            selected_agents: List of agents selected for processing
            routing_rationale: Explanation of agent selection
            features: Feature vector used for confidence calculation
            confidence: Confidence scoring results
            budget_allocated: Budget allocated for turn
            budget_used: Actual budget consumption
            latency_ms: Total turn processing time
            agent_latencies: Per-agent execution times
            agent_results: Results from each selected agent
            final_response: Final response to user
            status: Turn completion status
            errors: Any errors encountered
            correlation_id: Optional correlation ID for request tracking
            
        Returns:
            Decision record ID
        """
        start_time = time.time()
        
        try:
            # Generate record ID and correlation ID if not provided
            record_id = str(uuid4())
            if not correlation_id:
                correlation_id = str(uuid4())
            
            # Create decision record
            decision_record = DecisionRecord(
                record_id=record_id,
                session_id=session_id,
                turn_id=turn_id,
                turn=turn,
                selected_agents=selected_agents,
                routing_rationale=routing_rationale,
                features=features,
                confidence=confidence,
                budget_allocated=self._convert_budget_dict(budget_allocated),
                budget_used=self._convert_budget_dict(budget_used),
                latency_ms=latency_ms,
                agent_latencies=agent_latencies or {},
                agent_results=agent_results or {},
                final_response=final_response,
                status=status,
                errors=errors or [],
                started_at=datetime.utcnow(),
                completed_at=datetime.utcnow() if status == "completed" else None
            )
            
            # Store record
            with self._storage_lock:
                self._decision_records[record_id] = decision_record
                
                # Track by session
                if session_id not in self._session_records:
                    self._session_records[session_id] = []
                self._session_records[session_id].append(record_id)
                
                # Add to batch queue for processing
                self._batch_queue.append({
                    'record': decision_record,
                    'correlation_id': correlation_id,
                    'created_at': datetime.utcnow()
                })
            
            # Track correlation ID
            self._track_correlation_id(correlation_id, {
                'session_id': session_id,
                'turn_id': turn_id,
                'record_id': record_id,
                'created_at': datetime.utcnow()
            })
            
            # Emit structured log
            if self._enable_structured_logging:
                await self._emit_structured_log(decision_record, correlation_id)
            
            # Emit to Opik tracer if available
            if self._tracer:
                await self._emit_to_opik(decision_record, correlation_id)
            
            # Update metrics
            processing_time_ms = (time.time() - start_time) * 1000
            self._update_creation_metrics(processing_time_ms)
            
            # Check SLA compliance
            if processing_time_ms > self._sla_thresholds['max_processing_time_ms']:
                self._metrics['performance_violations'] += 1
                self._logger.warning(
                    f"Decision record creation exceeded SLA: {processing_time_ms:.1f}ms"
                )
            
            self._logger.debug(
                f"Created decision record {record_id} for session {session_id}, "
                f"turn {turn} in {processing_time_ms:.1f}ms"
            )
            
            return record_id
            
        except Exception as e:
            self._logger.error(f"Failed to create decision record: {e}")
            raise ServiceException(f"Decision record creation failed: {str(e)}") from e

    def _convert_budget_dict(self, budget_dict: Dict[str, Any]) -> Any:
        """Convert budget dictionary to Budget object if needed"""
        from faultmaven.models.microservice_contracts.core_contracts import Budget
        
        if isinstance(budget_dict, Budget):
            return budget_dict
        
        # Convert dict to Budget object
        return Budget(
            time_ms=budget_dict.get('time_ms', 0),
            token_budget=budget_dict.get('token_budget', 0),
            call_budget=budget_dict.get('call_budget', 0),
            time_used=budget_dict.get('time_used', 0),
            tokens_used=budget_dict.get('tokens_used', 0),
            calls_used=budget_dict.get('calls_used', 0)
        )

    async def _emit_structured_log(self, record: DecisionRecord, correlation_id: str):
        """Emit structured log entry for the decision record"""
        try:
            # Create structured log entry
            log_entry = {
                "event_type": "decision_record",
                "correlation_id": correlation_id,
                "record_id": record.record_id,
                "session_id": record.session_id,
                "turn_id": record.turn_id,
                "turn": record.turn,
                "timestamp": record.started_at.isoformat(),
                "routing": {
                    "selected_agents": record.selected_agents,
                    "rationale": record.routing_rationale[:200],  # Truncate for logging
                    "agent_count": len(record.selected_agents)
                },
                "confidence": {
                    "raw_score": record.confidence.get('raw_score', 0.0),
                    "calibrated_score": record.confidence.get('calibrated_score', 0.0),
                    "band": record.confidence.get('band', 'unknown')
                },
                "performance": {
                    "total_latency_ms": record.latency_ms,
                    "agent_latencies": record.agent_latencies,
                    "budget_utilization": self._calculate_budget_utilization(
                        record.budget_allocated, record.budget_used
                    )
                },
                "outcome": {
                    "status": record.status,
                    "response_length": len(record.final_response),
                    "error_count": len(record.errors)
                },
                "metadata": {
                    "feature_count": len(record.features),
                    "agent_result_count": len(record.agent_results)
                }
            }
            
            # Log as structured JSON
            self._logger.info(
                "Decision record created",
                extra={
                    "structured_data": log_entry,
                    "correlation_id": correlation_id
                }
            )
            
        except Exception as e:
            self._logger.error(f"Failed to emit structured log: {e}")

    def _calculate_budget_utilization(self, allocated: Any, used: Any) -> Dict[str, float]:
        """Calculate budget utilization percentages"""
        try:
            return {
                "time_utilization": (used.time_used / max(allocated.time_ms, 1)) * 100,
                "token_utilization": (used.tokens_used / max(allocated.token_budget, 1)) * 100,
                "call_utilization": (used.calls_used / max(allocated.call_budget, 1)) * 100
            }
        except Exception:
            return {"time_utilization": 0.0, "token_utilization": 0.0, "call_utilization": 0.0}

    async def _emit_to_opik(self, record: DecisionRecord, correlation_id: str):
        """Emit decision record to Opik tracing system"""
        try:
            if not self._tracer:
                return
            
            # Create Opik trace entry
            with self._tracer.trace("decision_record") as span:
                span.set_attribute("correlation_id", correlation_id)
                span.set_attribute("record_id", record.record_id)
                span.set_attribute("session_id", record.session_id)
                span.set_attribute("turn", record.turn)
                span.set_attribute("selected_agents", json.dumps(record.selected_agents))
                span.set_attribute("status", record.status)
                span.set_attribute("latency_ms", record.latency_ms)
                
                # Add confidence metrics
                if record.confidence:
                    for key, value in record.confidence.items():
                        if isinstance(value, (int, float, str)):
                            span.set_attribute(f"confidence.{key}", value)
                
                # Add feature vector
                for feature, value in record.features.items():
                    span.set_attribute(f"feature.{feature}", value)
                
                # Add budget utilization
                budget_util = self._calculate_budget_utilization(
                    record.budget_allocated, record.budget_used
                )
                for metric, value in budget_util.items():
                    span.set_attribute(f"budget.{metric}", value)
                
                span.set_status("ok" if record.status == "completed" else "error")
                
                self._logger.debug(f"Emitted decision record to Opik: {record.record_id}")
                
        except Exception as e:
            self._logger.error(f"Failed to emit to Opik: {e}")

    def _track_correlation_id(self, correlation_id: str, metadata: Dict[str, Any]):
        """Track correlation ID for request tracing"""
        with self._storage_lock:
            self._active_correlations[correlation_id] = metadata
            self._metrics['correlation_ids_tracked'] += 1
            
            # Track unique sessions
            session_id = metadata.get('session_id')
            if session_id and session_id not in self._session_records:
                self._metrics['sessions_tracked'] += 1

    def _update_creation_metrics(self, processing_time_ms: float):
        """Update metrics for record creation"""
        self._metrics['records_created'] += 1
        self._metrics['total_processing_time_ms'] += processing_time_ms
        
        # Update average processing time
        count = self._metrics['records_created']
        current_avg = self._metrics['avg_processing_time_ms']
        self._metrics['avg_processing_time_ms'] = (current_avg * (count - 1) + processing_time_ms) / count

    async def _process_batch(self):
        """Process batched records"""
        if not self._batch_queue:
            return
        
        start_time = time.time()
        
        with self._storage_lock:
            # Get batch to process
            batch = self._batch_queue[:self._batch_size]
            self._batch_queue = self._batch_queue[self._batch_size:]
        
        if not batch:
            return
        
        try:
            # Process batch (in production, this would write to persistent storage)
            for item in batch:
                record = item['record']
                correlation_id = item['correlation_id']
                
                # Emit analytics events
                await self._emit_analytics_event(record, correlation_id)
                
                self._metrics['records_processed'] += 1
            
            # Update metrics
            processing_time_ms = (time.time() - start_time) * 1000
            
            if processing_time_ms > self._sla_thresholds['max_batch_processing_time_ms']:
                self._metrics['performance_violations'] += 1
                self._logger.warning(
                    f"Batch processing exceeded SLA: {processing_time_ms:.1f}ms for {len(batch)} records"
                )
            
            self._logger.debug(f"Processed batch of {len(batch)} records in {processing_time_ms:.1f}ms")
            
        except Exception as e:
            self._logger.error(f"Batch processing failed: {e}")

    async def _emit_analytics_event(self, record: DecisionRecord, correlation_id: str):
        """Emit analytics event for downstream processing"""
        try:
            # Create analytics event
            event = {
                "event_type": "troubleshooting_turn",
                "correlation_id": correlation_id,
                "timestamp": record.started_at.isoformat(),
                "session_id": record.session_id,
                "turn": record.turn,
                "agents_used": len(record.selected_agents),
                "processing_time_ms": record.latency_ms,
                "confidence_score": record.confidence.get('calibrated_score', 0.0),
                "confidence_band": record.confidence.get('band', 'unknown'),
                "budget_efficiency": {
                    "time_efficiency": (record.budget_used.time_used / max(record.budget_allocated.time_ms, 1)),
                    "token_efficiency": (record.budget_used.tokens_used / max(record.budget_allocated.token_budget, 1)),
                    "call_efficiency": (record.budget_used.calls_used / max(record.budget_allocated.call_budget, 1))
                },
                "outcome": record.status,
                "error_occurred": len(record.errors) > 0
            }
            
            # In production, this would be sent to analytics pipeline
            self._logger.debug(f"Analytics event: {event['event_type']} for session {record.session_id}")
            self._metrics['records_emitted'] += 1
            
        except Exception as e:
            self._logger.error(f"Failed to emit analytics event: {e}")

    async def _cleanup_expired_records(self):
        """Clean up expired decision records"""
        cutoff_time = datetime.utcnow() - self._retention_period
        
        with self._storage_lock:
            # Find expired records
            expired_records = []
            for record_id, record in self._decision_records.items():
                if record.started_at < cutoff_time:
                    expired_records.append(record_id)
            
            # Remove expired records
            for record_id in expired_records:
                record = self._decision_records.pop(record_id, None)
                if record:
                    # Remove from session tracking
                    session_records = self._session_records.get(record.session_id, [])
                    if record_id in session_records:
                        session_records.remove(record_id)
                        if not session_records:
                            self._session_records.pop(record.session_id, None)
            
            # Cleanup expired correlations
            expired_correlations = []
            for corr_id, metadata in self._active_correlations.items():
                if metadata.get('created_at', datetime.utcnow()) < cutoff_time:
                    expired_correlations.append(corr_id)
            
            for corr_id in expired_correlations:
                self._active_correlations.pop(corr_id, None)
            
            if expired_records or expired_correlations:
                self._logger.debug(
                    f"Cleaned up {len(expired_records)} expired records and "
                    f"{len(expired_correlations)} expired correlations"
                )

    @trace("decision_recorder_get_records")
    async def get_decision_records(
        self,
        session_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        limit: int = 100,
        include_details: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get decision records with optional filtering
        
        Args:
            session_id: Optional session ID filter
            correlation_id: Optional correlation ID filter
            limit: Maximum number of records to return
            include_details: Whether to include full record details
            
        Returns:
            List of decision record summaries or full records
        """
        try:
            records = []
            
            with self._storage_lock:
                if session_id:
                    # Get records for specific session
                    record_ids = self._session_records.get(session_id, [])
                    for record_id in record_ids[-limit:]:  # Get most recent
                        if record_id in self._decision_records:
                            record = self._decision_records[record_id]
                            records.append(self._format_record_response(record, include_details))
                
                elif correlation_id:
                    # Find record by correlation ID (simplified lookup)
                    correlation_metadata = self._active_correlations.get(correlation_id)
                    if correlation_metadata:
                        record_id = correlation_metadata.get('record_id')
                        if record_id and record_id in self._decision_records:
                            record = self._decision_records[record_id]
                            records.append(self._format_record_response(record, include_details))
                
                else:
                    # Get all records (limited)
                    all_records = list(self._decision_records.values())
                    all_records.sort(key=lambda r: r.started_at, reverse=True)
                    
                    for record in all_records[:limit]:
                        records.append(self._format_record_response(record, include_details))
            
            return records
            
        except Exception as e:
            self._logger.error(f"Failed to get decision records: {e}")
            return []

    def _format_record_response(self, record: DecisionRecord, include_details: bool) -> Dict[str, Any]:
        """Format decision record for API response"""
        base_info = {
            "record_id": record.record_id,
            "session_id": record.session_id,
            "turn_id": record.turn_id,
            "turn": record.turn,
            "timestamp": record.started_at.isoformat(),
            "status": record.status,
            "latency_ms": record.latency_ms,
            "agents_used": len(record.selected_agents),
            "confidence_score": record.confidence.get('calibrated_score', 0.0),
            "confidence_band": record.confidence.get('band', 'unknown')
        }
        
        if include_details:
            base_info.update({
                "selected_agents": record.selected_agents,
                "routing_rationale": record.routing_rationale,
                "features": record.features,
                "confidence": record.confidence,
                "budget_allocated": record.budget_allocated.dict() if record.budget_allocated else {},
                "budget_used": record.budget_used.dict() if record.budget_used else {},
                "agent_latencies": record.agent_latencies,
                "agent_results": record.agent_results,
                "final_response": record.final_response,
                "errors": record.errors,
                "completed_at": record.completed_at.isoformat() if record.completed_at else None
            })
        
        return base_info

    async def get_metrics(self) -> Dict[str, Any]:
        """Get comprehensive telemetry metrics"""
        try:
            with self._storage_lock:
                active_sessions = len(self._session_records)
                total_records = len(self._decision_records)
                active_correlations = len(self._active_correlations)
                batch_queue_size = len(self._batch_queue)
            
            # Calculate performance metrics
            sla_compliance_rate = 100.0
            if self._metrics['records_created'] > 0:
                violations = self._metrics['performance_violations']
                sla_compliance_rate = ((self._metrics['records_created'] - violations) / 
                                     self._metrics['records_created']) * 100
            
            processing_efficiency = 0.0
            if self._metrics['records_created'] > 0:
                processing_efficiency = (self._metrics['records_processed'] / 
                                       self._metrics['records_created']) * 100
            
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "service": "decision_recorder",
                "records": {
                    "created": self._metrics['records_created'],
                    "processed": self._metrics['records_processed'],
                    "emitted": self._metrics['records_emitted'],
                    "active_count": total_records,
                    "batch_queue_size": batch_queue_size
                },
                "performance": {
                    "avg_processing_time_ms": self._metrics['avg_processing_time_ms'],
                    "sla_violations": self._metrics['performance_violations'],
                    "sla_compliance_rate": sla_compliance_rate,
                    "processing_efficiency": processing_efficiency
                },
                "tracking": {
                    "correlation_ids_tracked": self._metrics['correlation_ids_tracked'],
                    "active_correlations": active_correlations,
                    "sessions_tracked": self._metrics['sessions_tracked'],
                    "active_sessions": active_sessions
                },
                "configuration": {
                    "retention_days": self._retention_period.days,
                    "batch_size": self._batch_size,
                    "flush_interval_seconds": self._flush_interval,
                    "structured_logging_enabled": self._enable_structured_logging,
                    "performance_tracking_enabled": self._enable_performance_tracking,
                    "opik_integration_enabled": self._tracer is not None
                }
            }
            
        except Exception as e:
            self._logger.error(f"Failed to get metrics: {e}")
            return {"error": str(e)}

    async def health_check(self) -> Dict[str, Any]:
        """Get service health status"""
        try:
            # Calculate health metrics
            sla_compliance_rate = 100.0
            if self._metrics['records_created'] > 0:
                violations = self._metrics['performance_violations']
                sla_compliance_rate = ((self._metrics['records_created'] - violations) / 
                                     self._metrics['records_created']) * 100
            
            avg_latency = self._metrics['avg_processing_time_ms']
            
            # Determine service status
            status = "healthy"
            errors = []
            
            if avg_latency > self._sla_thresholds['max_processing_time_ms']:
                status = "degraded"
                errors.append(f"High average processing time: {avg_latency:.1f}ms")
            
            if sla_compliance_rate < 95:
                status = "degraded"
                errors.append(f"Low SLA compliance: {sla_compliance_rate:.1f}%")
            
            with self._storage_lock:
                batch_queue_size = len(self._batch_queue)
                
            if batch_queue_size > self._batch_size * 5:  # Large backlog
                status = "degraded"
                errors.append(f"Large batch queue: {batch_queue_size} records")
            
            return {
                "service": "decision_recorder",
                "status": status,
                "timestamp": datetime.utcnow().isoformat(),
                "version": "1.0.0",
                "metrics": {
                    "records_created": self._metrics['records_created'],
                    "avg_processing_time_ms": avg_latency,
                    "sla_compliance_rate": sla_compliance_rate,
                    "batch_queue_size": batch_queue_size,
                    "active_correlations": len(self._active_correlations)
                },
                "integrations": {
                    "opik_enabled": self._tracer is not None,
                    "structured_logging_enabled": self._enable_structured_logging,
                    "background_processing_enabled": self._processing_task is not None
                },
                "errors": errors
            }
            
        except Exception as e:
            return {
                "service": "decision_recorder",
                "status": "unhealthy", 
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e)
            }

    async def ready_check(self) -> bool:
        """Check if service is ready to handle requests"""
        try:
            # Service is ready if basic components are initialized
            return True
        except Exception:
            return False

    # Utility methods for advanced telemetry features

    async def get_session_analytics(self, session_id: str) -> Dict[str, Any]:
        """Get analytics for a specific session"""
        try:
            with self._storage_lock:
                record_ids = self._session_records.get(session_id, [])
                
            if not record_ids:
                return {"session_id": session_id, "record_count": 0}
            
            # Analyze session records
            session_records = []
            for record_id in record_ids:
                if record_id in self._decision_records:
                    session_records.append(self._decision_records[record_id])
            
            if not session_records:
                return {"session_id": session_id, "record_count": 0}
            
            # Calculate session analytics
            total_turns = len(session_records)
            total_latency = sum(r.latency_ms for r in session_records)
            avg_latency = total_latency / total_turns
            
            confidence_scores = [r.confidence.get('calibrated_score', 0.0) for r in session_records]
            avg_confidence = sum(confidence_scores) / len(confidence_scores)
            
            # Agent usage analysis
            agent_usage = {}
            for record in session_records:
                for agent in record.selected_agents:
                    agent_usage[agent] = agent_usage.get(agent, 0) + 1
            
            # Status distribution
            status_distribution = {}
            for record in session_records:
                status = record.status
                status_distribution[status] = status_distribution.get(status, 0) + 1
            
            return {
                "session_id": session_id,
                "record_count": total_turns,
                "time_span": {
                    "start": session_records[0].started_at.isoformat(),
                    "end": session_records[-1].started_at.isoformat()
                },
                "performance": {
                    "total_latency_ms": total_latency,
                    "avg_latency_ms": avg_latency,
                    "avg_confidence_score": avg_confidence
                },
                "agent_usage": agent_usage,
                "status_distribution": status_distribution,
                "confidence_trend": confidence_scores
            }
            
        except Exception as e:
            self._logger.error(f"Failed to get session analytics: {e}")
            return {"session_id": session_id, "error": str(e)}

    async def get_correlation_trace(self, correlation_id: str) -> Dict[str, Any]:
        """Get complete trace for a correlation ID"""
        try:
            correlation_metadata = self._active_correlations.get(correlation_id)
            if not correlation_metadata:
                return {"correlation_id": correlation_id, "found": False}
            
            record_id = correlation_metadata.get('record_id')
            if record_id and record_id in self._decision_records:
                record = self._decision_records[record_id]
                
                return {
                    "correlation_id": correlation_id,
                    "found": True,
                    "metadata": correlation_metadata,
                    "decision_record": self._format_record_response(record, include_details=True),
                    "trace_complete": True
                }
            
            return {
                "correlation_id": correlation_id,
                "found": True,
                "metadata": correlation_metadata,
                "decision_record": None,
                "trace_complete": False
            }
            
        except Exception as e:
            self._logger.error(f"Failed to get correlation trace: {e}")
            return {"correlation_id": correlation_id, "error": str(e)}

    def __del__(self):
        """Cleanup background processing on destruction"""
        if self._processing_task and not self._processing_task.done():
            self._processing_task.cancel()