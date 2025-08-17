# File: faultmaven/infrastructure/protection/protection_coordinator.py

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from faultmaven.models.behavioral import (
    BehaviorProfile, BehaviorScore, ReputationScore, ReputationEvent,
    ClientProfile, RiskLevel, ReputationLevel, ProtectionDecision,
    BehaviorAnalysisResult, AnomalyResult
)
from faultmaven.models.interfaces import ISessionStore
from faultmaven.models.protection import SystemMetrics

from .behavioral_analyzer import BehavioralAnalyzer
from .anomaly_detector import AnomalyDetectionSystem, ModelFeedback
from .reputation_engine import ReputationEngine
from .smart_circuit_breaker import SmartCircuitBreaker, CircuitConfig, Request, Response


@dataclass
class ProtectionConfig:
    """Intelligent protection system configuration"""
    # Behavioral analysis
    enable_behavioral_analysis: bool = True
    behavioral_analysis_window: int = 3600  # seconds
    behavioral_pattern_threshold: float = 0.8
    
    # ML anomaly detection
    enable_ml_detection: bool = True
    ml_model_path: str = "/tmp/faultmaven_ml_models"
    ml_training_enabled: bool = True
    ml_online_learning: bool = True
    
    # Reputation system
    enable_reputation_system: bool = True
    reputation_decay_rate: float = 0.05
    reputation_recovery_threshold: float = 0.1
    
    # Smart circuit breakers
    enable_smart_circuit_breakers: bool = True
    circuit_failure_threshold: int = 5
    circuit_timeout_seconds: int = 60
    
    # System integration
    monitoring_interval: int = 300  # seconds
    cleanup_interval: int = 3600  # seconds


class ProtectionCoordinator:
    """
    Intelligent Protection System Coordinator
    
    Integrates and orchestrates all intelligent protection components:
    - Behavioral Analysis Engine
    - ML-based Anomaly Detection
    - Client Reputation System
    - Smart Circuit Breakers
    - Adaptive Protection Mechanisms
    """

    def __init__(self, config: ProtectionConfig = None, session_store: Optional[ISessionStore] = None):
        self.config = config or ProtectionConfig()
        self.session_store = session_store
        self.logger = logging.getLogger(__name__)
        
        # Initialize components
        self.behavioral_analyzer = None
        self.anomaly_detector = None
        self.reputation_engine = None
        self.circuit_breakers: Dict[str, SmartCircuitBreaker] = {}
        
        # System state
        self.system_metrics = SystemMetrics()
        self.active_protections: Dict[str, Any] = {}
        self.protection_statistics = {
            "requests_analyzed": 0,
            "anomalies_detected": 0,
            "reputation_updates": 0,
            "circuit_breaker_actions": 0,
            "false_positives": 0,
            "true_positives": 0
        }
        
        # Background tasks
        self._monitoring_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
        # Client profiles cache
        self._client_profiles: Dict[str, ClientProfile] = {}
        self._profile_cache_ttl = timedelta(minutes=30)
        self._profile_timestamps: Dict[str, datetime] = {}

    async def initialize(self):
        """Initialize all intelligent protection components"""
        try:
            self.logger.info("Initializing Intelligent Protection System")
            
            # Initialize behavioral analyzer
            if self.config.enable_behavioral_analysis:
                self.behavioral_analyzer = BehavioralAnalyzer(self.session_store)
                self.logger.info("Behavioral analyzer initialized")
            
            # Initialize ML anomaly detector
            if self.config.enable_ml_detection:
                self.anomaly_detector = AnomalyDetectionSystem(
                    model_path=self.config.ml_model_path,
                    enable_online_learning=self.config.ml_online_learning
                )
                self.logger.info("ML anomaly detector initialized")
            
            # Initialize reputation engine
            if self.config.enable_reputation_system:
                self.reputation_engine = ReputationEngine(self.session_store)
                self.logger.info("Reputation engine initialized")
            
            # Initialize smart circuit breakers
            if self.config.enable_smart_circuit_breakers:
                await self._initialize_circuit_breakers()
                self.logger.info("Smart circuit breakers initialized")
            
            # Start background tasks
            await self._start_background_tasks()
            
            self.logger.info("Intelligent Protection System fully initialized")
            
        except Exception as e:
            self.logger.error(f"Error initializing intelligent protection system: {e}")
            raise

    async def analyze_request(self, session_id: str, request_data: dict) -> ProtectionDecision:
        """
        Comprehensive request analysis using all intelligent protection components
        
        Args:
            session_id: Session identifier
            request_data: Request details for analysis
            
        Returns:
            Protection decision with recommendations
        """
        try:
            self.protection_statistics["requests_analyzed"] += 1
            
            # Get or create client profile
            client_profile = await self._get_client_profile(session_id)
            
            # Behavioral analysis
            behavior_score = None
            if self.behavioral_analyzer:
                behavior_score = await self.behavioral_analyzer.analyze_request_pattern(
                    session_id, request_data
                )
                
                # Update client profile with behavior
                if client_profile.behavior_profile:
                    client_profile.behavior_profile.last_updated = datetime.utcnow()
                    client_profile.behavior_profile.current_risk_level = behavior_score.risk_level
            
            # ML anomaly detection
            anomaly_results = []
            if (self.anomaly_detector and client_profile.behavior_profile and 
                client_profile.behavior_profile.behavior_vectors):
                
                # Use latest behavior vector for anomaly detection
                latest_vector = client_profile.behavior_profile.behavior_vectors[-1]
                anomaly_result = await self.anomaly_detector.detect_anomalies(latest_vector)
                
                if anomaly_result.overall_score > 0.3:  # Significant anomaly
                    anomaly_results.append(anomaly_result)
                    self.protection_statistics["anomalies_detected"] += 1
            
            # Reputation-based decision
            reputation_factor = 1.0
            if self.reputation_engine and client_profile.reputation_score:
                reputation_level = client_profile.reputation_score.reputation_level
                if reputation_level == ReputationLevel.BLOCKED:
                    return ProtectionDecision(
                        decision_id=f"rep_block_{session_id}_{int(datetime.utcnow().timestamp())}",
                        session_id=session_id,
                        allow_request=False,
                        applied_restrictions=["reputation_block"],
                        risk_assessment=RiskLevel.CRITICAL,
                        confidence=0.9,
                        explanation="Client reputation is blocked",
                        decision_timestamp=datetime.utcnow()
                    )
                elif reputation_level == ReputationLevel.RESTRICTED:
                    reputation_factor = 0.3
                elif reputation_level == ReputationLevel.SUSPICIOUS:
                    reputation_factor = 0.7
                elif reputation_level == ReputationLevel.TRUSTED:
                    reputation_factor = 1.2
            
            # Circuit breaker check
            circuit_decision = await self._check_circuit_breakers(request_data, client_profile)
            if not circuit_decision.allow_request:
                return circuit_decision
            
            # Combine all factors for final decision
            final_decision = await self._make_final_decision(
                session_id, behavior_score, anomaly_results, 
                client_profile, reputation_factor, request_data
            )
            
            # Update client profile based on decision
            await self._update_client_profile(client_profile, final_decision)
            
            return final_decision
            
        except Exception as e:
            self.logger.error(f"Error in intelligent protection request analysis: {e}")
            # Return safe default decision
            return ProtectionDecision(
                decision_id=f"error_{session_id}_{int(datetime.utcnow().timestamp())}",
                session_id=session_id,
                allow_request=True,
                risk_assessment=RiskLevel.MEDIUM,
                confidence=0.0,
                explanation="Error in protection analysis, defaulting to allow",
                decision_timestamp=datetime.utcnow()
            )

    async def process_response(self, session_id: str, request_data: dict, response_data: dict):
        """
        Process response for learning and adaptation
        
        Args:
            session_id: Session identifier
            request_data: Original request data
            response_data: Response details
        """
        try:
            # Update behavioral model
            if self.behavioral_analyzer:
                interaction = {**request_data, **response_data}
                await self.behavioral_analyzer.update_behavior_model(session_id, interaction)
            
            # Update circuit breakers
            if self.circuit_breakers:
                response = Response(
                    status_code=response_data.get('status_code', 200),
                    response_time=response_data.get('response_time', 0.0),
                    error_type=response_data.get('error_type')
                )
                
                # Update relevant circuit breakers
                endpoint = request_data.get('endpoint', 'default')
                if endpoint in self.circuit_breakers:
                    client_profile = await self._get_client_profile(session_id)
                    await self.circuit_breakers[endpoint].update_metrics(response, client_profile)
            
            # Update reputation based on response
            if self.reputation_engine:
                await self._update_reputation_from_response(session_id, response_data)
            
            # Provide feedback to ML models
            if self.anomaly_detector and self.config.ml_online_learning:
                await self._provide_ml_feedback(session_id, request_data, response_data)
                
        except Exception as e:
            self.logger.error(f"Error processing response: {e}")

    async def get_client_risk_assessment(self, session_id: str) -> Dict[str, Any]:
        """Get comprehensive risk assessment for a client"""
        try:
            client_profile = await self._get_client_profile(session_id)
            
            risk_assessment = {
                "session_id": session_id,
                "overall_risk": RiskLevel.LOW,
                "risk_factors": [],
                "protective_factors": [],
                "recommendations": [],
                "confidence": 0.0
            }
            
            # Behavioral risk factors
            if client_profile.behavior_profile:
                if client_profile.behavior_profile.current_risk_level != RiskLevel.LOW:
                    risk_assessment["risk_factors"].append(
                        f"Behavioral risk: {client_profile.behavior_profile.current_risk_level.value}"
                    )
                    risk_assessment["overall_risk"] = max(
                        risk_assessment["overall_risk"], 
                        client_profile.behavior_profile.current_risk_level
                    )
            
            # Reputation factors
            if client_profile.reputation_score:
                rep_level = client_profile.reputation_score.reputation_level
                if rep_level in [ReputationLevel.BLOCKED, ReputationLevel.RESTRICTED]:
                    risk_assessment["risk_factors"].append(f"Low reputation: {rep_level.value}")
                    risk_assessment["overall_risk"] = RiskLevel.HIGH
                elif rep_level == ReputationLevel.TRUSTED:
                    risk_assessment["protective_factors"].append("Trusted reputation")
            
            # Recent anomalies
            if self.behavioral_analyzer:
                recent_anomalies = await self.behavioral_analyzer.detect_anomalies(session_id)
                if len(recent_anomalies) > 2:
                    risk_assessment["risk_factors"].append(f"{len(recent_anomalies)} recent anomalies detected")
                    risk_assessment["overall_risk"] = max(risk_assessment["overall_risk"], RiskLevel.MEDIUM)
            
            # Generate recommendations
            if risk_assessment["risk_factors"]:
                risk_assessment["recommendations"].extend([
                    "Enhanced monitoring recommended",
                    "Consider stricter rate limits"
                ])
            
            if len(risk_assessment["risk_factors"]) > 2:
                risk_assessment["recommendations"].append("Manual review recommended")
            
            # Calculate confidence based on data availability
            confidence_factors = []
            if client_profile.behavior_profile and client_profile.behavior_profile.total_requests > 10:
                confidence_factors.append(0.4)
            if client_profile.reputation_score:
                confidence_factors.append(0.4)
            if client_profile.total_sessions > 5:
                confidence_factors.append(0.2)
            
            risk_assessment["confidence"] = sum(confidence_factors)
            
            return risk_assessment
            
        except Exception as e:
            self.logger.error(f"Error getting risk assessment: {e}")
            return {"session_id": session_id, "overall_risk": RiskLevel.MEDIUM, "confidence": 0.0}

    async def get_system_status(self) -> Dict[str, Any]:
        """Get comprehensive system status"""
        try:
            status = {
                "phase2_enabled": True,
                "components": {},
                "statistics": self.protection_statistics.copy(),
                "active_protections": len(self.active_protections),
                "client_profiles": len(self._client_profiles)
            }
            
            # Component status
            if self.behavioral_analyzer:
                status["components"]["behavioral_analyzer"] = {
                    "enabled": True,
                    "profiles_tracked": len(self.behavioral_analyzer._behavior_profiles)
                }
            
            if self.anomaly_detector:
                status["components"]["anomaly_detector"] = await self.anomaly_detector.get_model_status()
            
            if self.reputation_engine:
                status["components"]["reputation_engine"] = {
                    "enabled": True,
                    "cache_size": len(self.reputation_engine._reputation_cache)
                }
            
            if self.circuit_breakers:
                circuit_status = {}
                for name, breaker in self.circuit_breakers.items():
                    circuit_status[name] = await breaker.get_status()
                status["components"]["circuit_breakers"] = circuit_status
            
            return status
            
        except Exception as e:
            self.logger.error(f"Error getting system status: {e}")
            return {"phase2_enabled": False, "error": str(e)}

    async def shutdown(self):
        """Graceful shutdown of intelligent protection system"""
        try:
            self.logger.info("Shutting down Intelligent Protection System")
            
            # Signal shutdown to background tasks
            self._shutdown_event.set()
            
            # Wait for background tasks to complete
            if self._monitoring_task and not self._monitoring_task.done():
                await asyncio.wait_for(self._monitoring_task, timeout=10.0)
            
            if self._cleanup_task and not self._cleanup_task.done():
                await asyncio.wait_for(self._cleanup_task, timeout=10.0)
            
            # Save ML models if available
            if self.anomaly_detector:
                await self.anomaly_detector._save_models()
            
            self.logger.info("Intelligent Protection System shutdown complete")
            
        except Exception as e:
            self.logger.error(f"Error during shutdown: {e}")

    async def _initialize_circuit_breakers(self):
        """Initialize smart circuit breakers for different endpoints"""
        circuit_config = CircuitConfig(
            failure_threshold=self.config.circuit_failure_threshold,
            timeout=timedelta(seconds=self.config.circuit_timeout_seconds)
        )
        
        # Create circuit breakers for high-impact endpoints
        high_impact_endpoints = [
            "agent_query", "troubleshoot", "data_upload", 
            "title_generation", "session_management"
        ]
        
        for endpoint in high_impact_endpoints:
            breaker = SmartCircuitBreaker(f"cb_{endpoint}", circuit_config)
            
            # Set up event callbacks
            breaker.on_state_change = self._on_circuit_state_change
            breaker.on_failure = self._on_circuit_failure
            
            self.circuit_breakers[endpoint] = breaker

    async def _get_client_profile(self, session_id: str) -> ClientProfile:
        """Get or create client profile"""
        # Check cache first
        if session_id in self._client_profiles:
            timestamp = self._profile_timestamps.get(session_id)
            if timestamp and datetime.utcnow() - timestamp < self._profile_cache_ttl:
                return self._client_profiles[session_id]
        
        # Create or load profile
        now = datetime.utcnow()
        
        # Load behavior profile if available
        behavior_profile = None
        if self.behavioral_analyzer:
            behavior_profile = await self.behavioral_analyzer.get_behavior_profile(session_id)
        
        # Load reputation if available
        reputation_score = None
        if self.reputation_engine:
            reputation_score = await self.reputation_engine.calculate_reputation(session_id)
        
        # Create client profile
        profile = ClientProfile(
            client_id=session_id,
            session_ids=[session_id],
            behavior_profile=behavior_profile,
            reputation_score=reputation_score,
            first_seen=now,
            last_activity=now,
            total_sessions=1,
            active_sessions=1
        )
        
        # Update derived fields
        if reputation_score:
            profile.current_reputation_level = reputation_score.reputation_level
        
        if behavior_profile:
            profile.current_risk_level = behavior_profile.current_risk_level
        
        # Cache the profile
        self._client_profiles[session_id] = profile
        self._profile_timestamps[session_id] = now
        
        return profile

    async def _check_circuit_breakers(self, request_data: dict, client_profile: ClientProfile) -> ProtectionDecision:
        """Check circuit breakers for the request"""
        endpoint = request_data.get('endpoint', 'default')
        
        if endpoint in self.circuit_breakers:
            breaker = self.circuit_breakers[endpoint]
            
            # Create request object
            request = Request(
                session_id=client_profile.client_id,
                endpoint=endpoint,
                method=request_data.get('method', 'GET'),
                timestamp=datetime.utcnow(),
                payload_size=request_data.get('payload_size', 0)
            )
            
            # Check circuit breaker decision
            decision = await breaker.should_allow_request(request, client_profile)
            
            if decision.action.value != "allow":
                self.protection_statistics["circuit_breaker_actions"] += 1
                
                return ProtectionDecision(
                    decision_id=f"cb_{endpoint}_{client_profile.client_id}_{int(datetime.utcnow().timestamp())}",
                    session_id=client_profile.client_id,
                    allow_request=decision.action.value == "allow",
                    applied_restrictions=[f"circuit_breaker_{decision.action.value}"],
                    risk_assessment=RiskLevel.HIGH,
                    confidence=decision.confidence,
                    explanation=f"Circuit breaker {decision.action.value}: {decision.reason}",
                    decision_timestamp=datetime.utcnow()
                )
        
        # Default: allow through circuit breakers
        return ProtectionDecision(
            decision_id=f"cb_allow_{client_profile.client_id}_{int(datetime.utcnow().timestamp())}",
            session_id=client_profile.client_id,
            allow_request=True,
            risk_assessment=RiskLevel.LOW,
            confidence=1.0,
            explanation="Circuit breaker check passed",
            decision_timestamp=datetime.utcnow()
        )

    async def _make_final_decision(self, session_id: str, behavior_score: Optional[BehaviorScore],
                                 anomaly_results: List[AnomalyResult], client_profile: ClientProfile,
                                 reputation_factor: float, request_data: dict) -> ProtectionDecision:
        """Make final protection decision based on all factors"""
        
        decision_factors = {}
        risk_level = RiskLevel.LOW
        confidence = 0.0
        applied_restrictions = []
        explanation_parts = []
        
        # Behavioral analysis factor
        if behavior_score:
            behavior_factor = behavior_score.overall_behavior_score
            decision_factors["behavior"] = behavior_factor
            confidence += behavior_score.confidence * 0.3
            
            if behavior_score.risk_level != RiskLevel.LOW:
                risk_level = max(risk_level, behavior_score.risk_level)
                explanation_parts.append(f"Behavioral risk: {behavior_score.risk_level.value}")
        
        # Anomaly detection factor
        if anomaly_results:
            max_anomaly_score = max(result.overall_score for result in anomaly_results)
            decision_factors["anomaly"] = 1.0 - max_anomaly_score  # Invert for decision factor
            confidence += 0.25
            
            if max_anomaly_score > 0.7:
                risk_level = RiskLevel.HIGH
                applied_restrictions.append("anomaly_detected")
                explanation_parts.append("High anomaly score detected")
            elif max_anomaly_score > 0.5:
                risk_level = max(risk_level, RiskLevel.MEDIUM)
        
        # Reputation factor
        decision_factors["reputation"] = reputation_factor
        confidence += 0.25
        
        if reputation_factor < 0.5:
            applied_restrictions.append("low_reputation")
            explanation_parts.append("Low client reputation")
        
        # System load factor
        load_factor = await self._get_system_load_factor()
        decision_factors["system_load"] = 1.0 - load_factor
        confidence += 0.2
        
        if load_factor > 0.8:
            applied_restrictions.append("high_system_load")
            explanation_parts.append("High system load")
        
        # Calculate final decision
        overall_factor = 1.0
        for factor in decision_factors.values():
            overall_factor *= factor
        
        allow_request = overall_factor > 0.3  # 30% threshold for blocking
        
        # Adjust for high-risk scenarios
        if risk_level == RiskLevel.CRITICAL:
            allow_request = False
            applied_restrictions.append("critical_risk")
        elif risk_level == RiskLevel.HIGH and overall_factor < 0.6:
            allow_request = False
            applied_restrictions.append("high_risk")
        
        # Generate explanation
        if explanation_parts:
            explanation = "Protection decision based on: " + ", ".join(explanation_parts)
        else:
            explanation = "Normal operation - all protection checks passed"
        
        return ProtectionDecision(
            decision_id=f"final_{session_id}_{int(datetime.utcnow().timestamp())}",
            session_id=session_id,
            allow_request=allow_request,
            applied_restrictions=applied_restrictions,
            decision_factors=decision_factors,
            risk_assessment=risk_level,
            confidence=min(confidence, 1.0),
            explanation=explanation,
            decision_timestamp=datetime.utcnow()
        )

    async def _update_client_profile(self, client_profile: ClientProfile, decision: ProtectionDecision):
        """Update client profile based on protection decision"""
        client_profile.last_activity = datetime.utcnow()
        client_profile.current_risk_level = decision.risk_assessment
        
        # Update monitoring flags based on decision
        if decision.applied_restrictions:
            for restriction in decision.applied_restrictions:
                if restriction not in client_profile.monitoring_flags:
                    client_profile.monitoring_flags.append(restriction)
        
        # Update access restrictions
        if not decision.allow_request:
            restriction_reason = f"blocked_at_{decision.decision_timestamp.isoformat()}"
            if restriction_reason not in client_profile.access_restrictions:
                client_profile.access_restrictions.append(restriction_reason)

    async def _update_reputation_from_response(self, session_id: str, response_data: dict):
        """Update reputation based on response data"""
        status_code = response_data.get('status_code', 200)
        response_time = response_data.get('response_time', 0.0)
        
        # Create reputation event based on response
        if status_code >= 500:
            # Server error - negative event
            event = ReputationEvent(
                event_type="error_generation",
                impact=-5,
                timestamp=datetime.utcnow(),
                session_id=session_id,
                description=f"Generated server error: {status_code}",
                metadata={"status_code": status_code, "response_time": response_time}
            )
        elif status_code >= 400:
            # Client error - minor negative
            event = ReputationEvent(
                event_type="client_error",
                impact=-2,
                timestamp=datetime.utcnow(),
                session_id=session_id,
                description=f"Client error: {status_code}",
                metadata={"status_code": status_code}
            )
        elif response_time < 1000:  # Fast response
            # Good behavior - positive event
            event = ReputationEvent(
                event_type="good_behavior",
                impact=1,
                timestamp=datetime.utcnow(),
                session_id=session_id,
                description="Fast, successful response",
                metadata={"response_time": response_time}
            )
        else:
            # Normal successful response
            return  # No reputation impact
        
        await self.reputation_engine.update_reputation(session_id, event)
        self.protection_statistics["reputation_updates"] += 1

    async def _provide_ml_feedback(self, session_id: str, request_data: dict, response_data: dict):
        """Provide feedback to ML models for online learning"""
        status_code = response_data.get('status_code', 200)
        
        # Determine if this was a true positive or false positive
        if status_code >= 500:
            # Server error suggests our detection was correct if we flagged it
            outcome = "true_positive"
        elif status_code == 200:
            # Success suggests false positive if we flagged it
            outcome = "false_positive"
        else:
            return  # Unclear outcome
        
        # Create feedback
        feedback = ModelFeedback(
            prediction_id=f"{session_id}_{int(datetime.utcnow().timestamp())}",
            actual_outcome=outcome,
            confidence=0.7  # Medium confidence in this simple heuristic
        )
        
        await self.anomaly_detector.update_online(feedback)

    async def _get_system_load_factor(self) -> float:
        """Get current system load factor (0.0 to 1.0)"""
        # Simplified implementation - in production, this would integrate with system monitoring
        try:
            # Base on number of active clients and circuit breaker states
            active_clients = len(self._client_profiles)
            load_factor = min(active_clients / 100.0, 0.5)  # Scale to max 100 clients = 50% load
            
            # Add circuit breaker load
            open_circuits = sum(1 for cb in self.circuit_breakers.values() if cb.state.value == "open")
            circuit_load = open_circuits / max(len(self.circuit_breakers), 1) * 0.3
            
            return min(load_factor + circuit_load, 1.0)
            
        except Exception:
            return 0.5  # Default moderate load

    async def _start_background_tasks(self):
        """Start background monitoring and cleanup tasks"""
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _monitoring_loop(self):
        """Background monitoring loop"""
        while not self._shutdown_event.is_set():
            try:
                # Update system metrics
                await self._update_system_metrics()
                
                # Adjust circuit breaker thresholds
                for breaker in self.circuit_breakers.values():
                    await breaker.adjust_thresholds(self.system_metrics)
                
                # Log statistics
                self.logger.debug(f"Intelligent protection statistics: {self.protection_statistics}")
                
                await asyncio.sleep(self.config.monitoring_interval)
                
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(60)  # Wait before retrying

    async def _cleanup_loop(self):
        """Background cleanup loop"""
        while not self._shutdown_event.is_set():
            try:
                # Cleanup old behavioral data
                if self.behavioral_analyzer:
                    await self.behavioral_analyzer.cleanup_old_data()
                
                # Cleanup old reputation data
                if self.reputation_engine:
                    await self.reputation_engine.cleanup_old_reputations()
                
                # Cleanup client profile cache
                await self._cleanup_client_profiles()
                
                self.logger.debug("Intelligent protection cleanup completed")
                
                await asyncio.sleep(self.config.cleanup_interval)
                
            except Exception as e:
                self.logger.error(f"Error in cleanup loop: {e}")
                await asyncio.sleep(600)  # Wait 10 minutes before retrying

    async def _cleanup_client_profiles(self):
        """Clean up old client profiles from cache"""
        cutoff_time = datetime.utcnow() - self._profile_cache_ttl * 2  # Double TTL for cleanup
        
        to_remove = []
        for session_id, timestamp in self._profile_timestamps.items():
            if timestamp < cutoff_time:
                to_remove.append(session_id)
        
        for session_id in to_remove:
            if session_id in self._client_profiles:
                del self._client_profiles[session_id]
            if session_id in self._profile_timestamps:
                del self._profile_timestamps[session_id]
        
        if to_remove:
            self.logger.debug(f"Cleaned up {len(to_remove)} old client profiles")

    async def _update_system_metrics(self):
        """Update system metrics for adaptive behavior"""
        # This is a simplified implementation
        # In production, this would integrate with actual system monitoring
        
        active_requests = sum(1 for cb in self.circuit_breakers.values() if cb.metrics.total_requests > 0)
        failed_requests = sum(cb.metrics.failed_requests for cb in self.circuit_breakers.values())
        total_requests = sum(cb.metrics.total_requests for cb in self.circuit_breakers.values())
        
        error_rate = failed_requests / max(total_requests, 1)
        health_score = max(0.0, 1.0 - error_rate)
        
        self.system_metrics = SystemMetrics(
            overall_health_score=health_score,
            cpu_usage=50.0,  # Placeholder
            memory_usage=60.0,  # Placeholder
            active_connections=active_requests,
            error_rate=error_rate,
            timestamp=datetime.utcnow()
        )

    async def _on_circuit_state_change(self, old_state, new_state):
        """Handle circuit breaker state changes"""
        self.logger.info(f"Circuit breaker state changed: {old_state.value} -> {new_state.value}")
        self.protection_statistics["circuit_breaker_actions"] += 1

    async def _on_circuit_failure(self, request, response):
        """Handle circuit breaker failures"""
        if response and response.is_failure:
            self.logger.warning(f"Circuit breaker failure detected: {response.status_code}")
            # Could trigger additional protective actions here