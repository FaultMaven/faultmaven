# File: faultmaven/infrastructure/protection/behavioral_analyzer.py

import asyncio
import json
import logging
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
import hashlib
import numpy as np

from faultmaven.models.behavioral import (
    BehaviorProfile, BehaviorScore, BehaviorType, RiskLevel, 
    RequestPattern, TimingProfile, ErrorPattern, ResourceProfile,
    BehaviorVector, BehaviorAnalysisResult, TemporalAnomaly, AnomalyType,
    Trend
)
from faultmaven.models.interfaces import ISessionStore


class BehavioralAnalyzer:
    """
    Advanced behavioral analysis engine for client protection
    
    Features:
    - Real-time pattern analysis
    - Historical behavior comparison
    - Multi-dimensional scoring
    - Temporal pattern recognition
    - Anomaly detection integration
    """

    def __init__(self, session_store: Optional[ISessionStore] = None):
        self.logger = logging.getLogger(__name__)
        self.session_store = session_store
        
        # Configuration
        self.analysis_window = timedelta(hours=1)  # Window for pattern analysis
        self.pattern_memory = timedelta(days=7)    # How long to remember patterns
        self.min_requests_for_analysis = 5         # Minimum requests for reliable analysis
        
        # In-memory storage for real-time analysis
        self._behavior_profiles: Dict[str, BehaviorProfile] = {}
        self._request_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self._timing_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        self._error_history: Dict[str, List[ErrorPattern]] = defaultdict(list)
        
        # Pattern recognition parameters
        self.timing_outlier_threshold = 2.5  # Standard deviations for timing outliers
        self.frequency_spike_threshold = 3.0  # Multiplier for frequency spikes
        self.error_rate_threshold = 0.1      # Error rate threshold (10%)
        
        # Behavioral baseline storage
        self._session_baselines: Dict[str, Dict[str, float]] = {}
        
        self.logger.info("BehavioralAnalyzer initialized with real-time pattern recognition")

    async def analyze_request_pattern(self, session_id: str, request_data: dict) -> BehaviorScore:
        """
        Analyze a single request and update behavioral patterns
        
        Args:
            session_id: Session identifier
            request_data: Request details including endpoint, timing, errors, etc.
            
        Returns:
            BehaviorScore with analysis results
        """
        try:
            # Record the request
            await self._record_request(session_id, request_data)
            
            # Get or create behavior profile
            profile = await self._get_or_create_profile(session_id)
            
            # Update patterns
            await self._update_request_patterns(profile, request_data)
            await self._update_timing_patterns(profile, request_data)
            await self._update_error_patterns(profile, request_data)
            
            # Analyze current behavior
            behavior_score = await self._calculate_behavior_score(profile)
            
            # Update profile with latest analysis
            profile.last_updated = datetime.now(timezone.utc)
            profile.current_risk_level = behavior_score.risk_level
            profile.confidence_score = behavior_score.confidence
            
            self._behavior_profiles[session_id] = profile
            
            return behavior_score
            
        except Exception as e:
            self.logger.error(f"Error analyzing request pattern for session {session_id}: {e}")
            # Return a default safe score
            return BehaviorScore(
                session_id=session_id,
                overall_behavior_score=0.5,
                risk_level=RiskLevel.MEDIUM,
                confidence=0.0,
                analysis_timestamp=datetime.now(timezone.utc),
                analysis_window=self.analysis_window
            )

    async def detect_anomalies(self, session_id: str) -> List[TemporalAnomaly]:
        """
        Detect anomalies in client behavior patterns
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of detected anomalies
        """
        anomalies = []
        
        try:
            profile = self._behavior_profiles.get(session_id)
            if not profile or profile.total_requests < self.min_requests_for_analysis:
                return anomalies
            
            # Check for frequency anomalies
            frequency_anomalies = await self._detect_frequency_anomalies(session_id)
            anomalies.extend(frequency_anomalies)
            
            # Check for timing anomalies
            timing_anomalies = await self._detect_timing_anomalies(session_id)
            anomalies.extend(timing_anomalies)
            
            # Check for pattern anomalies
            pattern_anomalies = await self._detect_pattern_anomalies(session_id)
            anomalies.extend(pattern_anomalies)
            
            # Check for error pattern anomalies
            error_anomalies = await self._detect_error_anomalies(session_id)
            anomalies.extend(error_anomalies)
            
            self.logger.debug(f"Detected {len(anomalies)} anomalies for session {session_id}")
            
        except Exception as e:
            self.logger.error(f"Error detecting anomalies for session {session_id}: {e}")
        
        return anomalies

    async def update_behavior_model(self, session_id: str, interaction: dict):
        """
        Update the behavioral model with new interaction data
        
        Args:
            session_id: Session identifier
            interaction: Interaction details
        """
        try:
            profile = await self._get_or_create_profile(session_id)
            
            # Update behavior vectors for ML analysis
            behavior_vector = await self._extract_behavior_vector(session_id, interaction)
            profile.behavior_vectors.append(behavior_vector)
            
            # Keep only recent vectors to manage memory
            if len(profile.behavior_vectors) > 100:
                profile.behavior_vectors = profile.behavior_vectors[-50:]
            
            # Update endpoint preferences
            endpoint = interaction.get('endpoint', 'unknown')
            if endpoint != 'unknown':
                current_count = profile.endpoint_preferences.get(endpoint, 0.0)
                profile.endpoint_preferences[endpoint] = current_count + 1.0
                
                # Normalize preferences
                total_requests = sum(profile.endpoint_preferences.values())
                if total_requests > 0:
                    profile.endpoint_preferences = {
                        k: v / total_requests 
                        for k, v in profile.endpoint_preferences.items()
                    }
            
            profile.total_requests += 1
            profile.last_updated = datetime.now(timezone.utc)
            
            self._behavior_profiles[session_id] = profile
            
        except Exception as e:
            self.logger.error(f"Error updating behavior model for session {session_id}: {e}")

    async def predict_risk_level(self, session_id: str) -> RiskLevel:
        """
        Predict future risk level based on current behavioral trends
        
        Args:
            session_id: Session identifier
            
        Returns:
            Predicted risk level
        """
        try:
            profile = self._behavior_profiles.get(session_id)
            if not profile:
                return RiskLevel.LOW
            
            # Analyze trends in behavior
            recent_scores = []
            recent_anomalies = []
            
            # Get recent behavior vectors
            if profile.behavior_vectors:
                recent_vectors = profile.behavior_vectors[-10:]  # Last 10 interactions
                for vector in recent_vectors:
                    # Extract risk indicators from features
                    risk_score = self._calculate_risk_from_vector(vector)
                    recent_scores.append(risk_score)
            
            # Check for escalating patterns
            if len(recent_scores) >= 3:
                # Check if risk is increasing
                if self._is_trend_increasing(recent_scores):
                    if profile.current_risk_level == RiskLevel.LOW:
                        return RiskLevel.MEDIUM
                    elif profile.current_risk_level == RiskLevel.MEDIUM:
                        return RiskLevel.HIGH
                    elif profile.current_risk_level == RiskLevel.HIGH:
                        return RiskLevel.CRITICAL
            
            # Check recent anomalies
            recent_anomalies = await self.detect_anomalies(session_id)
            if len(recent_anomalies) > 3:
                return RiskLevel.HIGH
            elif len(recent_anomalies) > 1:
                return RiskLevel.MEDIUM
            
            return profile.current_risk_level
            
        except Exception as e:
            self.logger.error(f"Error predicting risk level for session {session_id}: {e}")
            return RiskLevel.MEDIUM

    async def get_behavior_profile(self, session_id: str) -> Optional[BehaviorProfile]:
        """Get the current behavior profile for a session"""
        return self._behavior_profiles.get(session_id)

    async def _record_request(self, session_id: str, request_data: dict):
        """Record request for historical analysis"""
        timestamp = datetime.now(timezone.utc)
        
        # Add timestamp to request data
        request_record = {
            **request_data,
            'timestamp': timestamp,
            'session_id': session_id
        }
        
        # Store in request history
        self._request_history[session_id].append(request_record)
        
        # Store timing data separately for efficient analysis
        if 'response_time' in request_data:
            timing_record = {
                'timestamp': timestamp,
                'response_time': request_data['response_time'],
                'endpoint': request_data.get('endpoint', 'unknown')
            }
            self._timing_history[session_id].append(timing_record)

    async def _get_or_create_profile(self, session_id: str) -> BehaviorProfile:
        """Get existing profile or create a new one"""
        if session_id in self._behavior_profiles:
            return self._behavior_profiles[session_id]
        
        now = datetime.now(timezone.utc)
        profile = BehaviorProfile(
            session_id=session_id,
            first_seen=now,
            last_updated=now,
            current_risk_level=RiskLevel.LOW
        )
        
        self._behavior_profiles[session_id] = profile
        return profile

    async def _update_request_patterns(self, profile: BehaviorProfile, request_data: dict):
        """Update request pattern analysis"""
        endpoint = request_data.get('endpoint', 'unknown')
        method = request_data.get('method', 'GET')
        response_time = request_data.get('response_time', 0.0)
        status_code = request_data.get('status_code', 200)
        payload_size = request_data.get('payload_size', 0)
        
        # Find or create pattern for this endpoint
        existing_pattern = None
        for pattern in profile.request_patterns:
            if pattern.endpoint == endpoint and pattern.method == method:
                existing_pattern = pattern
                break
        
        if existing_pattern:
            # Update existing pattern with moving averages
            alpha = 0.1  # Smoothing factor
            existing_pattern.avg_response_time = (
                (1 - alpha) * existing_pattern.avg_response_time + 
                alpha * response_time
            )
            existing_pattern.payload_size_avg = int(
                (1 - alpha) * existing_pattern.payload_size_avg + 
                alpha * payload_size
            )
            
            # Update error rate
            is_error = status_code >= 400
            existing_pattern.error_rate = (
                (1 - alpha) * existing_pattern.error_rate + 
                alpha * (1.0 if is_error else 0.0)
            )
            
            existing_pattern.timestamp = datetime.now(timezone.utc)
        else:
            # Create new pattern
            new_pattern = RequestPattern(
                endpoint=endpoint,
                method=method,
                frequency=1.0,  # Will be calculated later
                avg_response_time=response_time,
                error_rate=1.0 if status_code >= 400 else 0.0,
                payload_size_avg=payload_size,
                timestamp=datetime.now(timezone.utc)
            )
            profile.request_patterns.append(new_pattern)

    async def _update_timing_patterns(self, profile: BehaviorProfile, request_data: dict):
        """Update timing pattern analysis"""
        timing_history = self._timing_history[profile.session_id]
        
        if len(timing_history) < 2:
            return
        
        # Calculate request intervals
        intervals = []
        recent_requests = list(timing_history)[-10:]  # Last 10 requests
        
        for i in range(1, len(recent_requests)):
            interval = (recent_requests[i]['timestamp'] - 
                       recent_requests[i-1]['timestamp']).total_seconds()
            intervals.append(interval)
        
        if intervals:
            avg_interval = statistics.mean(intervals)
            interval_stddev = statistics.stdev(intervals) if len(intervals) > 1 else 0.0
            
            # Detect peak activity hours
            hour_counts = defaultdict(int)
            for record in recent_requests:
                hour = record['timestamp'].hour
                hour_counts[hour] += 1
            
            peak_hours = [hour for hour, count in hour_counts.items() if count > 1]
            
            # Update or create timing profile
            profile.timing_characteristics = TimingProfile(
                avg_request_interval=avg_interval,
                request_interval_stddev=interval_stddev,
                peak_activity_hours=peak_hours,
                session_duration_avg=0.0,  # Will be calculated at session end
                burst_frequency=self._calculate_burst_frequency(intervals),
                think_time_avg=avg_interval  # Simplified
            )

    async def _update_error_patterns(self, profile: BehaviorProfile, request_data: dict):
        """Update error pattern analysis"""
        status_code = request_data.get('status_code', 200)
        endpoint = request_data.get('endpoint', 'unknown')
        
        if status_code >= 400:
            error_type = f"HTTP_{status_code}"
            
            # Find existing error pattern
            existing_error = None
            for error_pattern in profile.error_patterns:
                if error_pattern.error_type == error_type:
                    existing_error = error_pattern
                    break
            
            if existing_error:
                existing_error.frequency += 1
                existing_error.last_occurrence = datetime.now(timezone.utc)
                if endpoint not in existing_error.endpoints_affected:
                    existing_error.endpoints_affected.append(endpoint)
            else:
                new_error = ErrorPattern(
                    error_type=error_type,
                    frequency=1,
                    endpoints_affected=[endpoint],
                    first_occurrence=datetime.now(timezone.utc),
                    last_occurrence=datetime.now(timezone.utc),
                    error_rate_trend=Trend.STABLE,
                    resolution_attempts=0
                )
                profile.error_patterns.append(new_error)

    async def _calculate_behavior_score(self, profile: BehaviorProfile) -> BehaviorScore:
        """Calculate comprehensive behavior score"""
        scores = {}
        risk_factors = []
        positive_indicators = []
        
        # Request pattern scoring
        request_score = await self._score_request_patterns(profile)
        scores[BehaviorType.REQUEST_PATTERN] = request_score
        
        # Timing pattern scoring
        timing_score = await self._score_timing_patterns(profile)
        scores[BehaviorType.TIMING_PATTERN] = timing_score
        
        # Error pattern scoring
        error_score = await self._score_error_patterns(profile)
        scores[BehaviorType.ERROR_PATTERN] = error_score
        
        # Resource usage scoring (if available)
        if profile.resource_usage:
            resource_score = await self._score_resource_usage(profile)
            scores[BehaviorType.RESOURCE_PATTERN] = resource_score
        
        # Calculate overall score
        overall_score = statistics.mean(scores.values()) if scores else 0.5
        
        # Determine risk level
        if overall_score >= 0.8:
            risk_level = RiskLevel.LOW
            positive_indicators.append("Consistent normal behavior patterns")
        elif overall_score >= 0.6:
            risk_level = RiskLevel.MEDIUM
        elif overall_score >= 0.4:
            risk_level = RiskLevel.HIGH
            risk_factors.append("Multiple behavioral anomalies detected")
        else:
            risk_level = RiskLevel.CRITICAL
            risk_factors.append("Severe behavioral anomalies detected")
        
        # Check for specific risk patterns
        if len(profile.error_patterns) > 5:
            risk_factors.append("High error generation rate")
        
        if profile.timing_characteristics and profile.timing_characteristics.burst_frequency > 10:
            risk_factors.append("Excessive request bursting")
        
        return BehaviorScore(
            session_id=profile.session_id,
            overall_behavior_score=overall_score,
            pattern_scores=scores,
            risk_level=risk_level,
            confidence=min(profile.total_requests / 20.0, 1.0),  # Confidence builds with data
            risk_factors=risk_factors,
            positive_indicators=positive_indicators,
            analysis_timestamp=datetime.now(timezone.utc),
            analysis_window=self.analysis_window
        )

    async def _score_request_patterns(self, profile: BehaviorProfile) -> float:
        """Score request patterns for normalcy"""
        if not profile.request_patterns:
            return 0.5  # Neutral score for no data
        
        scores = []
        
        for pattern in profile.request_patterns:
            pattern_score = 1.0
            
            # Check error rate
            if pattern.error_rate > self.error_rate_threshold:
                pattern_score *= (1.0 - pattern.error_rate)
            
            # Check response time (assuming reasonable baseline)
            if pattern.avg_response_time > 5000:  # 5 seconds
                pattern_score *= 0.7
            elif pattern.avg_response_time > 1000:  # 1 second
                pattern_score *= 0.9
            
            scores.append(pattern_score)
        
        return statistics.mean(scores)

    async def _score_timing_patterns(self, profile: BehaviorProfile) -> float:
        """Score timing patterns for normalcy"""
        if not profile.timing_characteristics:
            return 0.5
        
        timing = profile.timing_characteristics
        score = 1.0
        
        # Check for unusual burst frequency
        if timing.burst_frequency > 20:  # More than 20 bursts per hour
            score *= 0.3
        elif timing.burst_frequency > 10:
            score *= 0.6
        
        # Check for very short intervals (potential bot behavior)
        if timing.avg_request_interval < 1.0:  # Less than 1 second
            score *= 0.4
        elif timing.avg_request_interval < 5.0:  # Less than 5 seconds
            score *= 0.7
        
        return score

    async def _score_error_patterns(self, profile: BehaviorProfile) -> float:
        """Score error patterns for normalcy"""
        if not profile.error_patterns:
            return 1.0  # No errors is good
        
        # Calculate total error frequency
        total_errors = sum(error.frequency for error in profile.error_patterns)
        error_diversity = len(profile.error_patterns)
        
        # More errors and more diverse errors are worse
        if total_errors > 20:
            return 0.2
        elif total_errors > 10:
            return 0.4
        elif total_errors > 5:
            return 0.6
        
        # Diversity penalty
        diversity_penalty = min(error_diversity * 0.1, 0.3)
        return max(0.8 - diversity_penalty, 0.0)

    async def _score_resource_usage(self, profile: BehaviorProfile) -> float:
        """Score resource usage patterns"""
        if not profile.resource_usage:
            return 0.5
        
        resource = profile.resource_usage
        score = 1.0
        
        # Check CPU usage
        if resource.cpu_usage_avg > 1000:  # 1 second per request
            score *= 0.3
        elif resource.cpu_usage_avg > 500:  # 500ms per request
            score *= 0.6
        
        # Check memory usage
        if resource.memory_usage_avg > 100:  # 100MB per request
            score *= 0.4
        elif resource.memory_usage_avg > 50:  # 50MB per request
            score *= 0.7
        
        return score

    async def _detect_frequency_anomalies(self, session_id: str) -> List[TemporalAnomaly]:
        """Detect frequency-based anomalies"""
        anomalies = []
        request_history = self._request_history[session_id]
        
        if len(request_history) < 10:
            return anomalies
        
        # Calculate request frequencies over time windows
        recent_requests = list(request_history)[-50:]  # Last 50 requests
        now = datetime.now(timezone.utc)
        
        # Check for sudden spikes in the last 5 minutes
        recent_5min = [r for r in recent_requests 
                      if (now - r['timestamp']).total_seconds() <= 300]
        
        if len(recent_5min) > 20:  # More than 20 requests in 5 minutes
            anomalies.append(TemporalAnomaly(
                anomaly_type=AnomalyType.FREQUENCY_ANOMALY,
                timestamp=now,
                severity=min(len(recent_5min) / 50.0, 1.0),
                duration=timedelta(minutes=5),
                affected_patterns=["request_frequency"],
                description=f"High frequency spike: {len(recent_5min)} requests in 5 minutes"
            ))
        
        return anomalies

    async def _detect_timing_anomalies(self, session_id: str) -> List[TemporalAnomaly]:
        """Detect timing-based anomalies"""
        anomalies = []
        timing_history = self._timing_history[session_id]
        
        if len(timing_history) < 5:
            return anomalies
        
        # Calculate recent intervals
        recent_requests = list(timing_history)[-20:]
        intervals = []
        
        for i in range(1, len(recent_requests)):
            interval = (recent_requests[i]['timestamp'] - 
                       recent_requests[i-1]['timestamp']).total_seconds()
            intervals.append(interval)
        
        if len(intervals) > 3:
            mean_interval = statistics.mean(intervals)
            
            # Check for very regular timing (potential bot)
            if len(intervals) > 5:
                std_dev = statistics.stdev(intervals)
                if std_dev < 0.1 and mean_interval < 10:  # Very regular, fast requests
                    anomalies.append(TemporalAnomaly(
                        anomaly_type=AnomalyType.TIMING_ANOMALY,
                        timestamp=datetime.now(timezone.utc),
                        severity=0.8,
                        duration=timedelta(minutes=1),
                        affected_patterns=["request_timing"],
                        description=f"Highly regular timing pattern detected (std_dev: {std_dev:.3f})"
                    ))
        
        return anomalies

    async def _detect_pattern_anomalies(self, session_id: str) -> List[TemporalAnomaly]:
        """Detect pattern-based anomalies"""
        anomalies = []
        profile = self._behavior_profiles.get(session_id)
        
        if not profile or not profile.endpoint_preferences:
            return anomalies
        
        # Check for sudden changes in endpoint usage
        recent_requests = list(self._request_history[session_id])[-20:]
        recent_endpoints = defaultdict(int)
        
        for request in recent_requests:
            endpoint = request.get('endpoint', 'unknown')
            recent_endpoints[endpoint] += 1
        
        # Normalize recent usage
        total_recent = sum(recent_endpoints.values())
        if total_recent > 0:
            recent_prefs = {k: v/total_recent for k, v in recent_endpoints.items()}
            
            # Compare with historical preferences
            for endpoint, recent_pref in recent_prefs.items():
                historical_pref = profile.endpoint_preferences.get(endpoint, 0.0)
                
                # Check for significant deviation
                if abs(recent_pref - historical_pref) > 0.3:  # 30% change
                    anomalies.append(TemporalAnomaly(
                        anomaly_type=AnomalyType.PATTERN_ANOMALY,
                        timestamp=datetime.now(timezone.utc),
                        severity=abs(recent_pref - historical_pref),
                        duration=timedelta(minutes=10),
                        affected_patterns=[f"endpoint_usage_{endpoint}"],
                        description=f"Significant change in {endpoint} usage: {historical_pref:.2f} -> {recent_pref:.2f}"
                    ))
        
        return anomalies

    async def _detect_error_anomalies(self, session_id: str) -> List[TemporalAnomaly]:
        """Detect error pattern anomalies"""
        anomalies = []
        recent_requests = list(self._request_history[session_id])[-20:]
        
        # Check recent error rate
        recent_errors = [r for r in recent_requests 
                        if r.get('status_code', 200) >= 400]
        
        if len(recent_requests) > 0:
            recent_error_rate = len(recent_errors) / len(recent_requests)
            
            if recent_error_rate > 0.2:  # More than 20% error rate
                anomalies.append(TemporalAnomaly(
                    anomaly_type=AnomalyType.PATTERN_ANOMALY,
                    timestamp=datetime.now(timezone.utc),
                    severity=recent_error_rate,
                    duration=timedelta(minutes=5),
                    affected_patterns=["error_rate"],
                    description=f"High error rate detected: {recent_error_rate:.1%}"
                ))
        
        return anomalies

    async def _extract_behavior_vector(self, session_id: str, interaction: dict) -> BehaviorVector:
        """Extract behavioral features for ML analysis"""
        features = {}
        
        # Request timing features
        features['response_time'] = interaction.get('response_time', 0.0)
        features['payload_size'] = interaction.get('payload_size', 0)
        
        # Request pattern features
        recent_requests = list(self._request_history[session_id])[-10:]
        if len(recent_requests) > 1:
            intervals = []
            for i in range(1, len(recent_requests)):
                interval = (recent_requests[i]['timestamp'] - 
                           recent_requests[i-1]['timestamp']).total_seconds()
                intervals.append(interval)
            
            features['avg_interval'] = statistics.mean(intervals)
            features['interval_stddev'] = statistics.stdev(intervals) if len(intervals) > 1 else 0.0
            features['request_frequency'] = len(recent_requests) / 600.0  # requests per 10 minutes
        
        # Error features
        recent_errors = sum(1 for r in recent_requests if r.get('status_code', 200) >= 400)
        features['error_rate'] = recent_errors / max(len(recent_requests), 1)
        
        # Endpoint diversity
        endpoints = set(r.get('endpoint', 'unknown') for r in recent_requests)
        features['endpoint_diversity'] = len(endpoints)
        
        return BehaviorVector(
            features=features,
            feature_names=list(features.keys()),
            extraction_timestamp=datetime.now(timezone.utc),
            window_size=10,  # Based on last 10 requests
            confidence=min(len(recent_requests) / 10.0, 1.0)
        )

    def _calculate_burst_frequency(self, intervals: List[float]) -> float:
        """Calculate request burst frequency"""
        if len(intervals) < 3:
            return 0.0
        
        # Count intervals shorter than 2 seconds as potential bursts
        short_intervals = sum(1 for interval in intervals if interval < 2.0)
        return (short_intervals / len(intervals)) * 60.0  # Bursts per hour estimate

    def _calculate_risk_from_vector(self, vector: BehaviorVector) -> float:
        """Calculate risk score from behavior vector"""
        features = vector.features
        risk_score = 0.0
        
        # Fast response times might indicate automated behavior
        if features.get('response_time', 0) < 100:  # Less than 100ms
            risk_score += 0.2
        
        # Very frequent requests
        if features.get('request_frequency', 0) > 5:  # More than 5 per 10 minutes
            risk_score += 0.3
        
        # High error rate
        if features.get('error_rate', 0) > 0.1:  # More than 10% errors
            risk_score += 0.4
        
        # Very regular intervals (bot-like)
        if features.get('interval_stddev', 1.0) < 0.1:
            risk_score += 0.3
        
        return min(risk_score, 1.0)

    def _is_trend_increasing(self, values: List[float]) -> bool:
        """Check if a trend is consistently increasing"""
        if len(values) < 3:
            return False
        
        increasing_count = 0
        for i in range(1, len(values)):
            if values[i] > values[i-1]:
                increasing_count += 1
        
        return increasing_count / (len(values) - 1) > 0.6  # 60% of values increasing

    async def cleanup_old_data(self):
        """Clean up old behavioral data to manage memory"""
        try:
            cutoff_time = datetime.now(timezone.utc) - self.pattern_memory
            
            # Clean up old profiles
            to_remove = []
            for session_id, profile in self._behavior_profiles.items():
                if profile.last_updated < cutoff_time:
                    to_remove.append(session_id)
            
            for session_id in to_remove:
                del self._behavior_profiles[session_id]
                if session_id in self._request_history:
                    del self._request_history[session_id]
                if session_id in self._timing_history:
                    del self._timing_history[session_id]
                if session_id in self._error_history:
                    del self._error_history[session_id]
            
            if to_remove:
                self.logger.info(f"Cleaned up {len(to_remove)} old behavioral profiles")
                
        except Exception as e:
            self.logger.error(f"Error during behavioral data cleanup: {e}")