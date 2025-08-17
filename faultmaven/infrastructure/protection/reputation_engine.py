# File: faultmaven/infrastructure/protection/reputation_engine.py

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict

from faultmaven.models.behavioral import (
    ReputationScore, ReputationEvent, Violation, ReputationLevel, 
    Trend, ClientProfile, BehaviorProfile
)
from faultmaven.models.interfaces import ISessionStore


class AccessLevel:
    """Access level configuration based on reputation"""
    def __init__(self, level: str, rate_multiplier: float, priority: int, restrictions: List[str]):
        self.level = level
        self.rate_multiplier = rate_multiplier  # 1.0 = normal, >1.0 = more access, <1.0 = less access
        self.priority = priority  # Higher = better priority
        self.restrictions = restrictions


class RecoveryPlan:
    """Recovery plan for improving reputation"""
    def __init__(self, current_score: int, target_score: int, estimated_time: timedelta, 
                 required_actions: List[str], milestones: List[Dict[str, Any]]):
        self.current_score = current_score
        self.target_score = target_score
        self.estimated_time = estimated_time
        self.required_actions = required_actions
        self.milestones = milestones


class ReputationEngine:
    """
    Persistent client reputation management system
    
    Features:
    - Multi-factor scoring (compliance, efficiency, stability, reliability)
    - Temporal decay for reputation recovery
    - Persistent storage with Redis backend
    - Recovery path calculation
    - Access level determination
    """

    def __init__(self, session_store: Optional[ISessionStore] = None):
        self.logger = logging.getLogger(__name__)
        self.session_store = session_store
        
        # Configuration
        self.decay_rate = 0.05  # Daily reputation recovery rate
        self.violation_penalties = {
            "low": -5,
            "medium": -15,
            "high": -30,
            "critical": -50
        }
        self.positive_rewards = {
            "compliance": 2,
            "efficiency": 1,
            "stability": 1,
            "good_behavior": 3
        }
        
        # Access level configurations
        self.access_levels = {
            ReputationLevel.TRUSTED: AccessLevel("trusted", 1.5, 5, []),
            ReputationLevel.NORMAL: AccessLevel("normal", 1.0, 3, []),
            ReputationLevel.SUSPICIOUS: AccessLevel("suspicious", 0.7, 2, ["enhanced_monitoring"]),
            ReputationLevel.RESTRICTED: AccessLevel("restricted", 0.3, 1, ["enhanced_monitoring", "limited_endpoints"]),
            ReputationLevel.BLOCKED: AccessLevel("blocked", 0.0, 0, ["access_denied"])
        }
        
        # In-memory cache for frequently accessed reputations
        self._reputation_cache: Dict[str, ReputationScore] = {}
        self._cache_ttl = timedelta(minutes=15)
        self._cache_timestamps: Dict[str, datetime] = {}
        
        # Score calculation weights
        self.score_weights = {
            "compliance": 0.3,
            "efficiency": 0.2,
            "stability": 0.2,
            "reliability": 0.3
        }
        
        self.logger.info("ReputationEngine initialized with persistent storage")

    async def calculate_reputation(self, client_id: str) -> ReputationScore:
        """
        Calculate comprehensive reputation score for a client
        
        Args:
            client_id: Client identifier (session_id, user_id, or fingerprint)
            
        Returns:
            Complete reputation score
        """
        try:
            # Check cache first
            cached_score = await self._get_cached_reputation(client_id)
            if cached_score:
                return cached_score
            
            # Load existing reputation or create new
            reputation = await self._load_reputation(client_id)
            if not reputation:
                reputation = await self._create_new_reputation(client_id)
            
            # Update reputation with latest calculations
            await self._update_reputation_scores(reputation)
            
            # Apply temporal decay for recovery
            await self._apply_temporal_decay(reputation)
            
            # Update trend analysis
            await self._update_reputation_trend(reputation)
            
            # Save updated reputation
            await self._save_reputation(reputation)
            
            # Cache the result
            await self._cache_reputation(reputation)
            
            return reputation
            
        except Exception as e:
            self.logger.error(f"Error calculating reputation for {client_id}: {e}")
            # Return default neutral reputation
            return await self._create_new_reputation(client_id)

    async def update_reputation(self, client_id: str, event: ReputationEvent):
        """
        Update reputation based on a specific event
        
        Args:
            client_id: Client identifier
            event: Event that affects reputation
        """
        try:
            # Get current reputation
            reputation = await self.calculate_reputation(client_id)
            
            # Add the event
            reputation.reputation_events.append(event)
            
            # Apply the impact
            old_score = reputation.overall_score
            
            if event.event_type == "violation":
                await self._apply_violation_penalty(reputation, event)
            elif event.event_type == "compliance":
                await self._apply_positive_reward(reputation, event, "compliance")
            elif event.event_type == "efficiency":
                await self._apply_positive_reward(reputation, event, "efficiency")
            elif event.event_type == "good_behavior":
                await self._apply_positive_reward(reputation, event, "good_behavior")
            
            # Update component scores based on event type
            await self._update_component_scores(reputation, event)
            
            # Recalculate overall score
            reputation.overall_score = await self._calculate_overall_score(reputation)
            
            # Ensure score stays within bounds
            reputation.overall_score = max(0, min(100, reputation.overall_score))
            
            # Update metadata
            reputation.last_updated = datetime.utcnow()
            
            # Log significant changes
            score_change = reputation.overall_score - old_score
            if abs(score_change) > 5:
                self.logger.info(f"Reputation change for {client_id}: {old_score} -> {reputation.overall_score} ({score_change:+.1f})")
            
            # Save updated reputation
            await self._save_reputation(reputation)
            
            # Clear cache to force recalculation
            await self._invalidate_cache(client_id)
            
        except Exception as e:
            self.logger.error(f"Error updating reputation for {client_id}: {e}")

    async def get_access_level(self, reputation: ReputationScore) -> AccessLevel:
        """
        Get access level configuration based on reputation
        
        Args:
            reputation: Client reputation score
            
        Returns:
            Access level configuration
        """
        return self.access_levels[reputation.reputation_level]

    async def reputation_recovery_path(self, client_id: str) -> RecoveryPlan:
        """
        Calculate recovery path for improving reputation
        
        Args:
            client_id: Client identifier
            
        Returns:
            Detailed recovery plan
        """
        try:
            reputation = await self.calculate_reputation(client_id)
            current_score = reputation.overall_score
            
            # Determine target score (next reputation level)
            if current_score < 30:
                target_score = 30  # To reach RESTRICTED
            elif current_score < 50:
                target_score = 50  # To reach SUSPICIOUS
            elif current_score < 70:
                target_score = 70  # To reach NORMAL
            elif current_score < 90:
                target_score = 90  # To reach TRUSTED
            else:
                target_score = 100  # Maximum score
            
            # Calculate required improvement
            required_improvement = target_score - current_score
            
            # Estimate time based on natural decay and positive actions
            daily_natural_recovery = self.decay_rate * (100 - current_score)
            daily_possible_improvement = daily_natural_recovery + 10  # Assume some positive actions
            
            estimated_days = max(1, math.ceil(required_improvement / daily_possible_improvement))
            estimated_time = timedelta(days=estimated_days)
            
            # Generate required actions
            required_actions = []
            if reputation.compliance_score < 80:
                required_actions.append("Follow rate limits consistently")
                required_actions.append("Avoid policy violations")
            
            if reputation.reliability_score < 80:
                required_actions.append("Reduce error-generating requests")
                required_actions.append("Use valid request formats")
            
            if reputation.efficiency_score < 80:
                required_actions.append("Optimize resource usage")
                required_actions.append("Reduce unnecessary requests")
            
            if reputation.stability_score < 80:
                required_actions.append("Maintain consistent behavior patterns")
                required_actions.append("Avoid sudden usage spikes")
            
            # Create milestones
            milestones = []
            milestone_scores = []
            
            # Add intermediate milestones
            score_gap = target_score - current_score
            if score_gap > 20:
                milestone_scores = [current_score + score_gap // 3, current_score + 2 * score_gap // 3]
            else:
                milestone_scores = []
            
            milestone_scores.append(target_score)
            
            for i, milestone_score in enumerate(milestone_scores):
                milestone_days = math.ceil((milestone_score - current_score) / daily_possible_improvement)
                milestones.append({
                    "score": milestone_score,
                    "estimated_days": milestone_days,
                    "description": f"Reach reputation score of {milestone_score}"
                })
            
            return RecoveryPlan(
                current_score=current_score,
                target_score=target_score,
                estimated_time=estimated_time,
                required_actions=required_actions,
                milestones=milestones
            )
            
        except Exception as e:
            self.logger.error(f"Error calculating recovery path for {client_id}: {e}")
            return RecoveryPlan(0, 50, timedelta(days=30), ["Maintain good behavior"], [])

    async def get_reputation_statistics(self) -> Dict[str, Any]:
        """Get overall reputation system statistics"""
        try:
            stats = {
                "total_clients": len(self._reputation_cache),
                "reputation_distribution": defaultdict(int),
                "average_score": 0.0,
                "trends": defaultdict(int)
            }
            
            scores = []
            for reputation in self._reputation_cache.values():
                level = reputation.reputation_level
                stats["reputation_distribution"][level.value] += 1
                stats["trends"][reputation.reputation_trend.value] += 1
                scores.append(reputation.overall_score)
            
            if scores:
                stats["average_score"] = sum(scores) / len(scores)
            
            return stats
            
        except Exception as e:
            self.logger.error(f"Error getting reputation statistics: {e}")
            return {}

    async def _get_cached_reputation(self, client_id: str) -> Optional[ReputationScore]:
        """Get reputation from cache if valid"""
        if client_id in self._reputation_cache:
            cache_time = self._cache_timestamps.get(client_id)
            if cache_time and datetime.utcnow() - cache_time < self._cache_ttl:
                return self._reputation_cache[client_id]
        
        return None

    async def _cache_reputation(self, reputation: ReputationScore):
        """Cache reputation score"""
        self._reputation_cache[reputation.client_id] = reputation
        self._cache_timestamps[reputation.client_id] = datetime.utcnow()

    async def _invalidate_cache(self, client_id: str):
        """Invalidate cache for client"""
        if client_id in self._reputation_cache:
            del self._reputation_cache[client_id]
        if client_id in self._cache_timestamps:
            del self._cache_timestamps[client_id]

    async def _load_reputation(self, client_id: str) -> Optional[ReputationScore]:
        """Load reputation from persistent storage"""
        try:
            if not self.session_store:
                return None
            
            key = f"reputation:{client_id}"
            data = await self.session_store.get(key)
            
            if data:
                # Deserialize reputation data
                reputation_data = json.loads(data) if isinstance(data, str) else data
                
                # Convert datetime strings back to datetime objects
                if 'first_scored' in reputation_data:
                    reputation_data['first_scored'] = datetime.fromisoformat(reputation_data['first_scored'])
                if 'last_updated' in reputation_data:
                    reputation_data['last_updated'] = datetime.fromisoformat(reputation_data['last_updated'])
                if 'last_violation' in reputation_data and reputation_data['last_violation']:
                    reputation_data['last_violation'] = datetime.fromisoformat(reputation_data['last_violation'])
                if 'last_positive_event' in reputation_data and reputation_data['last_positive_event']:
                    reputation_data['last_positive_event'] = datetime.fromisoformat(reputation_data['last_positive_event'])
                
                # Convert violation and event lists
                violations = []
                for v_data in reputation_data.get('historical_violations', []):
                    if 'timestamp' in v_data:
                        v_data['timestamp'] = datetime.fromisoformat(v_data['timestamp'])
                    violations.append(Violation(**v_data))
                reputation_data['historical_violations'] = violations
                
                events = []
                for e_data in reputation_data.get('reputation_events', []):
                    if 'timestamp' in e_data:
                        e_data['timestamp'] = datetime.fromisoformat(e_data['timestamp'])
                    events.append(ReputationEvent(**e_data))
                reputation_data['reputation_events'] = events
                
                return ReputationScore(**reputation_data)
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error loading reputation for {client_id}: {e}")
            return None

    async def _save_reputation(self, reputation: ReputationScore):
        """Save reputation to persistent storage"""
        try:
            if not self.session_store:
                return
            
            # Serialize reputation data
            reputation_data = reputation.dict()
            
            # Convert datetime objects to ISO strings
            if reputation_data.get('first_scored'):
                reputation_data['first_scored'] = reputation_data['first_scored'].isoformat()
            if reputation_data.get('last_updated'):
                reputation_data['last_updated'] = reputation_data['last_updated'].isoformat()
            if reputation_data.get('last_violation'):
                reputation_data['last_violation'] = reputation_data['last_violation'].isoformat()
            if reputation_data.get('last_positive_event'):
                reputation_data['last_positive_event'] = reputation_data['last_positive_event'].isoformat()
            
            # Convert violations and events
            for violation in reputation_data.get('historical_violations', []):
                if 'timestamp' in violation:
                    violation['timestamp'] = violation['timestamp'].isoformat()
            
            for event in reputation_data.get('reputation_events', []):
                if 'timestamp' in event:
                    event['timestamp'] = event['timestamp'].isoformat()
            
            key = f"reputation:{reputation.client_id}"
            serialized_data = json.dumps(reputation_data)
            
            # Store with 30-day TTL
            await self.session_store.set(key, serialized_data, ttl=30 * 24 * 3600)
            
        except Exception as e:
            self.logger.error(f"Error saving reputation for {reputation.client_id}: {e}")

    async def _create_new_reputation(self, client_id: str) -> ReputationScore:
        """Create new reputation score with default values"""
        now = datetime.utcnow()
        return ReputationScore(
            client_id=client_id,
            overall_score=75,  # Start with neutral-good score
            compliance_score=75,
            efficiency_score=75,
            stability_score=75,
            reliability_score=75,
            reputation_trend=Trend.STABLE,
            first_scored=now,
            last_updated=now
        )

    async def _update_reputation_scores(self, reputation: ReputationScore):
        """Update component scores based on recent events"""
        now = datetime.utcnow()
        recent_window = timedelta(days=7)  # Consider events from last 7 days
        
        recent_events = [
            event for event in reputation.reputation_events
            if now - event.timestamp < recent_window
        ]
        
        # Calculate component scores based on recent events
        if recent_events:
            # Compliance score - based on violations
            violation_events = [e for e in recent_events if e.event_type == "violation"]
            compliance_penalty = sum(abs(e.impact) for e in violation_events)
            reputation.compliance_score = max(0, min(100, 100 - compliance_penalty))
            
            # Efficiency score - based on efficiency events
            efficiency_events = [e for e in recent_events if e.event_type == "efficiency"]
            efficiency_boost = sum(max(0, e.impact) for e in efficiency_events)
            reputation.efficiency_score = max(0, min(100, 50 + efficiency_boost))
            
            # Reliability score - based on error patterns
            error_events = [e for e in recent_events if "error" in e.event_type.lower()]
            error_penalty = sum(abs(e.impact) for e in error_events)
            reputation.reliability_score = max(0, min(100, 100 - error_penalty))
            
            # Stability score - based on consistency
            stability_events = [e for e in recent_events if e.event_type in ["good_behavior", "compliance"]]
            stability_boost = sum(max(0, e.impact) for e in stability_events)
            reputation.stability_score = max(0, min(100, 50 + stability_boost))

    async def _apply_temporal_decay(self, reputation: ReputationScore):
        """Apply temporal decay for reputation recovery"""
        now = datetime.utcnow()
        
        # Apply daily decay if no updates in the last day
        if now - reputation.last_updated > timedelta(days=1):
            days_since_update = (now - reputation.last_updated).days
            
            # Apply decay rate per day
            for _ in range(days_since_update):
                # Recovery is stronger for lower scores
                recovery_rate = self.decay_rate * (100 - reputation.overall_score) / 100
                improvement = recovery_rate * 100
                
                reputation.overall_score = min(100, reputation.overall_score + improvement)
                reputation.compliance_score = min(100, reputation.compliance_score + improvement * 0.5)
                reputation.efficiency_score = min(100, reputation.efficiency_score + improvement * 0.3)
                reputation.stability_score = min(100, reputation.stability_score + improvement * 0.4)
                reputation.reliability_score = min(100, reputation.reliability_score + improvement * 0.3)

    async def _update_reputation_trend(self, reputation: ReputationScore):
        """Update reputation trend based on recent score changes"""
        # Look at score changes over time
        recent_events = reputation.reputation_events[-10:]  # Last 10 events
        
        if len(recent_events) < 3:
            reputation.reputation_trend = Trend.STABLE
            return
        
        # Calculate trend based on event impacts
        recent_impacts = [event.impact for event in recent_events]
        
        positive_events = sum(1 for impact in recent_impacts if impact > 0)
        negative_events = sum(1 for impact in recent_impacts if impact < 0)
        
        if positive_events > negative_events * 1.5:
            reputation.reputation_trend = Trend.IMPROVING
        elif negative_events > positive_events * 1.5:
            reputation.reputation_trend = Trend.DECLINING
        else:
            # Check for volatility
            impact_variance = sum((impact - sum(recent_impacts)/len(recent_impacts))**2 for impact in recent_impacts)
            if impact_variance > 100:  # High variance threshold
                reputation.reputation_trend = Trend.VOLATILE
            else:
                reputation.reputation_trend = Trend.STABLE

    async def _apply_violation_penalty(self, reputation: ReputationScore, event: ReputationEvent):
        """Apply penalty for violations"""
        violation_data = event.metadata
        severity = violation_data.get('severity', 'medium')
        
        penalty = self.violation_penalties.get(severity, -10)
        
        # Create violation record
        violation = Violation(
            violation_type=violation_data.get('violation_type', 'unknown'),
            severity=severity,
            description=event.description,
            timestamp=event.timestamp,
            session_id=violation_data.get('session_id', 'unknown')
        )
        
        reputation.historical_violations.append(violation)
        reputation.last_violation = event.timestamp
        
        # Apply penalty with diminishing returns for repeated violations
        violation_count = len(reputation.historical_violations)
        diminishing_factor = 1.0 / (1.0 + violation_count * 0.1)
        
        adjusted_penalty = penalty * diminishing_factor
        reputation.overall_score = max(0, reputation.overall_score + adjusted_penalty)

    async def _apply_positive_reward(self, reputation: ReputationScore, event: ReputationEvent, reward_type: str):
        """Apply positive reward for good behavior"""
        reward = self.positive_rewards.get(reward_type, 1)
        
        # Diminishing returns for excessive positive events
        recent_positive = sum(1 for e in reputation.reputation_events[-20:] 
                            if e.impact > 0)
        
        diminishing_factor = 1.0 / (1.0 + recent_positive * 0.05)
        adjusted_reward = reward * diminishing_factor
        
        reputation.overall_score = min(100, reputation.overall_score + adjusted_reward)
        reputation.last_positive_event = event.timestamp

    async def _update_component_scores(self, reputation: ReputationScore, event: ReputationEvent):
        """Update component scores based on event type"""
        impact = abs(event.impact) * 0.1  # Scale impact for component scores
        
        if event.event_type == "violation":
            reputation.compliance_score = max(0, reputation.compliance_score - impact)
        elif event.event_type == "efficiency":
            reputation.efficiency_score = min(100, reputation.efficiency_score + impact)
        elif event.event_type == "compliance":
            reputation.compliance_score = min(100, reputation.compliance_score + impact)
        elif event.event_type == "good_behavior":
            reputation.stability_score = min(100, reputation.stability_score + impact)

    async def _calculate_overall_score(self, reputation: ReputationScore) -> int:
        """Calculate overall score from component scores"""
        weighted_score = (
            reputation.compliance_score * self.score_weights["compliance"] +
            reputation.efficiency_score * self.score_weights["efficiency"] +
            reputation.stability_score * self.score_weights["stability"] +
            reputation.reliability_score * self.score_weights["reliability"]
        )
        
        return int(round(weighted_score))

    async def cleanup_old_reputations(self):
        """Clean up old reputation data"""
        try:
            cutoff_time = datetime.utcnow() - timedelta(days=60)  # Keep 60 days of data
            
            # Clean up cache
            to_remove = []
            for client_id, timestamp in self._cache_timestamps.items():
                if timestamp < cutoff_time:
                    to_remove.append(client_id)
            
            for client_id in to_remove:
                await self._invalidate_cache(client_id)
            
            if to_remove:
                self.logger.info(f"Cleaned up {len(to_remove)} old reputation cache entries")
                
        except Exception as e:
            self.logger.error(f"Error during reputation cleanup: {e}")