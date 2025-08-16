"""Hierarchical Memory System Implementation

This module implements the hierarchical memory architecture for FaultMaven's
intelligent conversation system. The architecture consists of four levels:

1. Working Memory: Current conversation context and immediate state
2. Session Memory: Session-specific insights and patterns
3. User Memory: User preferences, skill level, and interaction patterns
4. Episodic Memory: Cross-session patterns and system-wide learning

Each level provides specific capabilities for context retrieval and learning.
"""

import json
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from collections import defaultdict

from faultmaven.models.interfaces import IVectorStore, ISessionStore
from faultmaven.exceptions import MemoryException


@dataclass
class MemoryItem:
    """Base class for memory items"""
    id: str
    content: Dict[str, Any]
    timestamp: float
    importance: float = 0.5
    access_count: int = 0
    last_accessed: float = 0.0
    
    def __post_init__(self):
        if self.last_accessed == 0.0:
            self.last_accessed = self.timestamp


class WorkingMemory:
    """Working Memory - Current conversation context and immediate state
    
    Manages the immediate conversation context including:
    - Current conversation turns
    - Active problem context
    - Temporary insights and hypotheses
    - Tool execution results
    
    Performance: < 10ms access time, limited to 10-20 items
    """
    
    def __init__(self, max_items: int = 20):
        self.max_items = max_items
        self.items: List[MemoryItem] = []
        self.current_context: Dict[str, Any] = {}
        self._logger = logging.getLogger(__name__)
    
    async def add_conversation_turn(self, session_id: str, turn_data: Dict[str, Any]) -> None:
        """Add a conversation turn to working memory with adaptive importance scoring"""
        # Calculate adaptive importance based on turn content
        importance = await self._calculate_turn_importance(turn_data)
        
        item = MemoryItem(
            id=f"turn_{session_id}_{len(self.items)}",
            content={
                "type": "conversation_turn",
                "session_id": session_id,
                "data": turn_data,
                "relevance_score": 1.0,  # Most recent turns are most relevant
                "adaptive_importance": importance
            },
            timestamp=time.time(),
            importance=importance
        )
        
        self.items.append(item)
        await self._maintain_size_limit()
    
    async def add_insight(self, session_id: str, insight: Dict[str, Any]) -> None:
        """Add an insight or hypothesis to working memory with enhanced scoring"""
        # Enhanced importance calculation for insights
        base_confidence = insight.get("confidence", 0.5)
        insight_type = insight.get("type", "general")
        
        # Boost importance for critical insight types
        type_multiplier = 1.0
        if "root_cause" in insight_type.lower():
            type_multiplier = 1.3
        elif "solution" in insight_type.lower():
            type_multiplier = 1.2
        elif "error" in insight_type.lower():
            type_multiplier = 1.1
        
        adaptive_importance = min(1.0, base_confidence * type_multiplier)
        
        item = MemoryItem(
            id=f"insight_{session_id}_{int(time.time())}",
            content={
                "type": "insight",
                "session_id": session_id,
                "insight": insight,
                "confidence": base_confidence,
                "adaptive_importance": adaptive_importance,
                "insight_type": insight_type
            },
            timestamp=time.time(),
            importance=adaptive_importance
        )
        
        self.items.append(item)
        await self._maintain_size_limit()
    
    async def _calculate_turn_importance(self, turn_data: Dict[str, Any]) -> float:
        """Calculate adaptive importance for conversation turns"""
        base_importance = 0.8
        
        # Check for key indicators of importance
        if "query" in turn_data:
            query = turn_data["query"].lower()
            
            # Error indicators boost importance
            error_keywords = ["error", "failed", "broken", "issue", "problem", "crash"]
            if any(keyword in query for keyword in error_keywords):
                base_importance = min(1.0, base_importance * 1.2)
            
            # Urgency indicators
            urgency_keywords = ["urgent", "critical", "down", "outage", "emergency"]
            if any(keyword in query for keyword in urgency_keywords):
                base_importance = min(1.0, base_importance * 1.3)
            
            # Question complexity (longer queries often more important)
            if len(query.split()) > 10:
                base_importance = min(1.0, base_importance * 1.1)
        
        # Response quality affects importance
        if "response" in turn_data:
            response_data = turn_data["response"]
            if isinstance(response_data, dict):
                # High confidence responses are more important
                confidence = response_data.get("confidence_score", 0.5)
                base_importance = min(1.0, base_importance * (1 + confidence * 0.2))
        
        return base_importance
    
    async def get_context(self, session_id: str) -> Dict[str, Any]:
        """Get current context for session"""
        relevant_items = []
        for item in self.items:
            if item.content.get("session_id") == session_id:
                item.access_count += 1
                item.last_accessed = time.time()
                relevant_items.append(item.content)
        
        return {
            "session_id": session_id,
            "items": relevant_items,
            "context_size": len(relevant_items),
            "timestamp": time.time()
        }
    
    async def _maintain_size_limit(self) -> None:
        """Maintain working memory size limit by removing oldest items"""
        if len(self.items) > self.max_items:
            # Sort by importance and recency, keep most important/recent
            self.items.sort(key=lambda x: (x.importance, x.timestamp), reverse=True)
            self.items = self.items[:self.max_items]
    
    def clear_session(self, session_id: str) -> None:
        """Clear working memory for specific session"""
        self.items = [item for item in self.items 
                     if item.content.get("session_id") != session_id]


class SessionMemory:
    """Session Memory - Session-specific insights and patterns
    
    Manages session-level memory including:
    - Session insights and learnings
    - Problem-solution patterns
    - User interaction patterns within session
    - Tool effectiveness tracking
    
    Performance: < 50ms access time, session-scoped storage
    """
    
    def __init__(self, session_store: Optional[ISessionStore] = None):
        self.session_store = session_store
        self.cache: Dict[str, Dict[str, Any]] = {}
        self._logger = logging.getLogger(__name__)
    
    async def store_insight(self, session_id: str, insight_type: str, data: Dict[str, Any]) -> None:
        """Store insight for session"""
        if session_id not in self.cache:
            self.cache[session_id] = {"insights": [], "patterns": [], "metadata": {}}
        
        insight = {
            "id": f"{insight_type}_{int(time.time())}",
            "type": insight_type,
            "data": data,
            "timestamp": time.time(),
            "confidence": data.get("confidence", 0.5)
        }
        
        self.cache[session_id]["insights"].append(insight)
        
        # Persist to session store if available
        if self.session_store:
            try:
                session_data = await self.session_store.get(session_id)
                if session_data:
                    session_data["memory_insights"] = self.cache[session_id]["insights"]
                    await self.session_store.set(session_id, session_data)
            except Exception as e:
                self._logger.warning(f"Failed to persist session insight: {e}")
    
    async def get_insights(self, session_id: str, insight_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get insights for session, optionally filtered by type"""
        # Load from cache or session store
        await self._load_session_data(session_id)
        
        insights = self.cache.get(session_id, {}).get("insights", [])
        
        if insight_type:
            insights = [insight for insight in insights if insight["type"] == insight_type]
        
        # Sort by confidence and recency
        insights.sort(key=lambda x: (x["confidence"], x["timestamp"]), reverse=True)
        return insights
    
    async def detect_patterns(self, session_id: str) -> List[Dict[str, Any]]:
        """Detect patterns in session data with enhanced analysis"""
        await self._load_session_data(session_id)
        
        insights = self.cache.get(session_id, {}).get("insights", [])
        patterns = []
        
        # Group insights by type
        insight_groups = defaultdict(list)
        for insight in insights:
            insight_groups[insight["type"]].append(insight)
        
        # Detect recurring patterns with enhanced metrics
        for insight_type, group_insights in insight_groups.items():
            if len(group_insights) >= 2:  # Need at least 2 for a pattern
                # Calculate pattern strength
                confidence_scores = [i["confidence"] for i in group_insights]
                avg_confidence = sum(confidence_scores) / len(confidence_scores)
                confidence_variance = sum((c - avg_confidence) ** 2 for c in confidence_scores) / len(confidence_scores)
                
                # Pattern consistency (lower variance = more consistent)
                consistency_score = max(0.1, 1.0 - confidence_variance)
                
                # Time clustering analysis
                timestamps = [i.get("timestamp", time.time()) for i in group_insights]
                time_span = max(timestamps) - min(timestamps) if len(timestamps) > 1 else 0
                temporal_density = len(group_insights) / max(1, time_span / 3600)  # Insights per hour
                
                patterns.append({
                    "pattern_type": f"recurring_{insight_type}",
                    "frequency": len(group_insights),
                    "confidence": avg_confidence,
                    "consistency_score": consistency_score,
                    "temporal_density": temporal_density,
                    "time_span_hours": time_span / 3600,
                    "strength": avg_confidence * consistency_score * min(1.0, temporal_density),
                    "insights": [i["id"] for i in group_insights]
                })
        
        # Sort patterns by strength
        patterns.sort(key=lambda x: x["strength"], reverse=True)
        
        # Add semantic clustering patterns
        semantic_patterns = await self._detect_semantic_patterns(insights)
        patterns.extend(semantic_patterns)
        
        return patterns
    
    async def _detect_semantic_patterns(self, insights: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Detect semantic patterns in insights using content similarity"""
        if len(insights) < 3:  # Need at least 3 for semantic clustering
            return []
        
        semantic_patterns = []
        
        # Simple keyword-based semantic clustering
        keyword_clusters = defaultdict(list)
        
        for insight in insights:
            # Extract keywords from insight data
            insight_text = json.dumps(insight.get("data", {})).lower()
            words = set(insight_text.split())
            
            # Remove common words
            common_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by"}
            meaningful_words = words - common_words
            
            # Group by dominant keywords
            for word in meaningful_words:
                if len(word) > 3:  # Only consider meaningful words
                    keyword_clusters[word].append(insight)
        
        # Create patterns for clusters with multiple insights
        for keyword, clustered_insights in keyword_clusters.items():
            if len(clustered_insights) >= 2:
                avg_confidence = sum(i["confidence"] for i in clustered_insights) / len(clustered_insights)
                
                # Skip low-confidence clusters
                if avg_confidence > 0.3:
                    semantic_patterns.append({
                        "pattern_type": f"semantic_cluster_{keyword}",
                        "keyword": keyword,
                        "frequency": len(clustered_insights),
                        "confidence": avg_confidence,
                        "semantic_strength": avg_confidence * len(clustered_insights) / len(insights),
                        "insights": [i["id"] for i in clustered_insights]
                    })
        
        # Sort semantic patterns by strength
        semantic_patterns.sort(key=lambda x: x["semantic_strength"], reverse=True)
        return semantic_patterns[:3]  # Top 3 semantic patterns
    
    async def _load_session_data(self, session_id: str) -> None:
        """Load session data from store if not in cache"""
        if session_id not in self.cache and self.session_store:
            try:
                session_data = await self.session_store.get(session_id)
                if session_data and "memory_insights" in session_data:
                    self.cache[session_id] = {
                        "insights": session_data["memory_insights"],
                        "patterns": [],
                        "metadata": {}
                    }
            except Exception as e:
                self._logger.warning(f"Failed to load session data: {e}")


class UserMemory:
    """User Memory - User preferences and interaction history
    
    Manages user-level memory including:
    - User skill level and expertise domains
    - Communication style preferences
    - Historical problem patterns
    - Learning progression tracking
    
    Performance: < 100ms access time, user-scoped storage
    """
    
    def __init__(self, session_store: Optional[ISessionStore] = None):
        self.session_store = session_store
        self.user_cache: Dict[str, Dict[str, Any]] = {}
        self._logger = logging.getLogger(__name__)
    
    async def get_user_profile(self, user_id: str) -> Dict[str, Any]:
        """Get user profile with defaults for new users"""
        if user_id not in self.user_cache:
            await self._load_user_data(user_id)
        
        return self.user_cache.get(user_id, {
            "user_id": user_id,
            "skill_level": "intermediate",
            "preferred_communication_style": "balanced",
            "domain_expertise": [],
            "interaction_patterns": {
                "avg_session_length": 0,
                "common_problem_domains": [],
                "solution_preferences": []
            },
            "learning_progression": {
                "completed_guides": [],
                "skill_improvements": []
            },
            "created_at": time.time(),
            "last_updated": time.time()
        })
    
    async def update_user_profile(self, user_id: str, updates: Dict[str, Any]) -> None:
        """Update user profile with new information"""
        profile = await self.get_user_profile(user_id)
        
        # Update specific fields
        for key, value in updates.items():
            if key in ["skill_level", "preferred_communication_style"]:
                profile[key] = value
            elif key == "domain_expertise":
                # Merge domain expertise
                existing_domains = set(profile.get("domain_expertise", []))
                new_domains = set(value if isinstance(value, list) else [value])
                profile["domain_expertise"] = list(existing_domains.union(new_domains))
            elif key == "interaction_patterns":
                # Update interaction patterns
                profile["interaction_patterns"].update(value)
        
        profile["last_updated"] = time.time()
        self.user_cache[user_id] = profile
        
        # Persist to storage if available
        await self._persist_user_data(user_id, profile)
    
    async def track_interaction(self, user_id: str, interaction_data: Dict[str, Any]) -> None:
        """Track user interaction for pattern learning"""
        profile = await self.get_user_profile(user_id)
        
        # Update interaction patterns
        patterns = profile["interaction_patterns"]
        
        # Track session length
        if "session_duration" in interaction_data:
            current_avg = patterns.get("avg_session_length", 0)
            new_duration = interaction_data["session_duration"]
            patterns["avg_session_length"] = (current_avg + new_duration) / 2
        
        # Track problem domains
        if "problem_domain" in interaction_data:
            domain = interaction_data["problem_domain"]
            domains = patterns.get("common_problem_domains", [])
            if domain not in domains:
                domains.append(domain)
                patterns["common_problem_domains"] = domains
        
        # Track solution effectiveness
        if "solution_effective" in interaction_data:
            solution_type = interaction_data.get("solution_type", "unknown")
            if interaction_data["solution_effective"]:
                preferences = patterns.get("solution_preferences", [])
                if solution_type not in preferences:
                    preferences.append(solution_type)
                    patterns["solution_preferences"] = preferences
        
        await self.update_user_profile(user_id, {"interaction_patterns": patterns})
    
    async def _load_user_data(self, user_id: str) -> None:
        """Load user data from persistent storage"""
        # For now, this would load from a user profile store
        # Implementation would depend on the chosen storage backend
        pass
    
    async def _persist_user_data(self, user_id: str, profile: Dict[str, Any]) -> None:
        """Persist user data to storage"""
        # For now, this would save to a user profile store
        # Implementation would depend on the chosen storage backend
        pass


class EpisodicMemory:
    """Episodic Memory - Cross-session patterns and system-wide learning
    
    Manages system-level memory including:
    - Cross-session problem patterns
    - System-wide effectiveness metrics
    - Global user behavior patterns
    - Knowledge base optimization insights
    
    Performance: < 200ms access time, global storage with indexing
    """
    
    def __init__(self, vector_store: Optional[IVectorStore] = None):
        self.vector_store = vector_store
        self.pattern_cache: Dict[str, Any] = {}
        self._logger = logging.getLogger(__name__)
    
    async def store_cross_session_pattern(self, pattern_type: str, pattern_data: Dict[str, Any]) -> None:
        """Store patterns that emerge across multiple sessions"""
        pattern = {
            "id": f"pattern_{pattern_type}_{int(time.time())}",
            "type": pattern_type,
            "data": pattern_data,
            "timestamp": time.time(),
            "frequency": pattern_data.get("frequency", 1),
            "confidence": pattern_data.get("confidence", 0.5)
        }
        
        # Store in vector store for semantic retrieval if available
        if self.vector_store:
            try:
                document = {
                    "id": pattern["id"],
                    "content": json.dumps(pattern_data),
                    "metadata": {
                        "type": "episodic_pattern",
                        "pattern_type": pattern_type,
                        "timestamp": pattern["timestamp"],
                        "confidence": pattern["confidence"]
                    }
                }
                await self.vector_store.add_documents([document])
            except Exception as e:
                self._logger.warning(f"Failed to store pattern in vector store: {e}")
        
        # Cache locally
        if pattern_type not in self.pattern_cache:
            self.pattern_cache[pattern_type] = []
        self.pattern_cache[pattern_type].append(pattern)
    
    async def get_relevant_patterns(self, query: str, pattern_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get relevant patterns based on query similarity"""
        patterns = []
        
        # Search vector store for semantic similarity
        if self.vector_store:
            try:
                results = await self.vector_store.search(query, k=5)
                for result in results:
                    if result.get("metadata", {}).get("type") == "episodic_pattern":
                        if not pattern_type or result["metadata"].get("pattern_type") == pattern_type:
                            patterns.append({
                                "id": result["id"],
                                "content": json.loads(result["content"]),
                                "similarity_score": result.get("score", 0.0),
                                "pattern_type": result["metadata"].get("pattern_type")
                            })
            except Exception as e:
                self._logger.warning(f"Failed to search vector store: {e}")
        
        # Fallback to cache search
        if not patterns and pattern_type in self.pattern_cache:
            cache_patterns = self.pattern_cache[pattern_type]
            # Simple keyword matching for fallback
            query_words = set(query.lower().split())
            for pattern in cache_patterns:
                pattern_words = set(json.dumps(pattern["data"]).lower().split())
                similarity = len(query_words.intersection(pattern_words)) / len(query_words) if query_words else 0
                if similarity > 0.2:  # Threshold for relevance
                    patterns.append({
                        "id": pattern["id"],
                        "content": pattern["data"],
                        "similarity_score": similarity,
                        "pattern_type": pattern["type"]
                    })
        
        # Sort by similarity and confidence
        patterns.sort(key=lambda x: x["similarity_score"], reverse=True)
        return patterns[:5]  # Return top 5 most relevant
    
    async def analyze_global_patterns(self) -> Dict[str, Any]:
        """Analyze global patterns across all stored data"""
        analysis = {
            "total_patterns": 0,
            "pattern_types": defaultdict(int),
            "high_confidence_patterns": [],
            "emerging_patterns": [],
            "pattern_trends": {}
        }
        
        # Analyze cached patterns
        for pattern_type, patterns in self.pattern_cache.items():
            analysis["total_patterns"] += len(patterns)
            analysis["pattern_types"][pattern_type] = len(patterns)
            
            # Find high confidence patterns
            high_conf = [p for p in patterns if p["confidence"] > 0.8]
            analysis["high_confidence_patterns"].extend(high_conf)
            
            # Find emerging patterns (recent with increasing frequency)
            recent_patterns = [p for p in patterns 
                             if time.time() - p["timestamp"] < 7 * 24 * 3600]  # Last week
            if len(recent_patterns) > len(patterns) * 0.3:  # 30% are recent
                analysis["emerging_patterns"].append({
                    "pattern_type": pattern_type,
                    "recent_count": len(recent_patterns),
                    "total_count": len(patterns),
                    "growth_rate": len(recent_patterns) / len(patterns)
                })
        
        return analysis