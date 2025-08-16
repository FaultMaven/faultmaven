"""Memory Manager Implementation

This module implements the core Memory Manager that orchestrates the hierarchical
memory system for FaultMaven's intelligent conversation capabilities.

The Memory Manager coordinates between the four memory levels:
- Working Memory (immediate context)
- Session Memory (session-specific insights)
- User Memory (user preferences and patterns)
- Episodic Memory (cross-session learning)

It provides intelligent context retrieval, insight consolidation, and learning
to enhance troubleshooting conversations with memory and personalization.
"""

import json
import time
import asyncio
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime

from faultmaven.models.interfaces import (
    ILLMProvider, IVectorStore, ISessionStore, ISanitizer,
    ConversationContext, UserProfile
)
from faultmaven.core.memory.hierarchical_memory import (
    WorkingMemory, SessionMemory, UserMemory, EpisodicMemory
)
from faultmaven.exceptions import MemoryException


class MemoryManager:
    """Core Memory Manager for intelligent conversation context management
    
    This class orchestrates the hierarchical memory system to provide:
    - Context-aware conversation enhancement
    - Pattern learning and insight consolidation
    - User personalization and preference tracking
    - Cross-session knowledge accumulation
    
    Performance Targets:
    - Context retrieval: < 50ms
    - Insight consolidation: < 100ms (async)
    - Memory cleanup: automatic background process
    
    Privacy and Security:
    - All data sanitized through ISanitizer interface
    - Configurable retention policies
    - User consent for cross-session data
    """
    
    def __init__(
        self,
        llm_provider: ILLMProvider,
        vector_store: Optional[IVectorStore] = None,
        session_store: Optional[ISessionStore] = None,
        sanitizer: Optional[ISanitizer] = None
    ):
        """Initialize Memory Manager with interface dependencies
        
        Args:
            llm_provider: LLM interface for insight extraction and analysis
            vector_store: Vector storage for semantic memory retrieval
            session_store: Session storage for persistence
            sanitizer: Data sanitization interface for privacy
        """
        self._llm = llm_provider
        self._vector_store = vector_store
        self._session_store = session_store
        self._sanitizer = sanitizer
        
        # Initialize memory hierarchy
        self._working_memory = WorkingMemory(max_items=20)
        self._session_memory = SessionMemory(session_store)
        self._user_memory = UserMemory(session_store)
        self._episodic_memory = EpisodicMemory(vector_store)
        
        self._logger = logging.getLogger(__name__)
        self._consolidation_tasks: Dict[str, asyncio.Task] = {}
    
    async def retrieve_context(self, session_id: str, query: str) -> ConversationContext:
        """Retrieve comprehensive conversation context for enhanced responses
        
        This method combines information from all memory levels to provide
        rich context for generating personalized and informed responses.
        Enhanced with intelligent filtering and semantic similarity matching.
        
        Args:
            session_id: Session identifier for context scope
            query: Current user query for relevance matching
            
        Returns:
            ConversationContext with conversation history, user profile,
            relevant insights, and domain context
            
        Raises:
            MemoryException: When context retrieval fails
        """
        try:
            start_time = time.time()
            
            # Sanitize inputs
            if self._sanitizer:
                query = self._sanitizer.sanitize(query)
            
            # Enhanced parallel retrieval with intelligent filtering
            context_tasks = [
                self._get_working_context_filtered(session_id, query),
                self._get_session_context_semantic(session_id, query),
                self._get_user_context(session_id),
                self._get_episodic_context_ranked(query)
            ]
            
            working_ctx, session_ctx, user_ctx, episodic_ctx = await asyncio.gather(
                *context_tasks, return_exceptions=True
            )
            
            # Handle any exceptions in context retrieval
            for ctx in [working_ctx, session_ctx, user_ctx, episodic_ctx]:
                if isinstance(ctx, Exception):
                    self._logger.warning(f"Context retrieval error: {ctx}")
            
            # Extract user profile information
            user_profile_data = None
            if not isinstance(user_ctx, Exception) and user_ctx:
                user_profile_data = {
                    "skill_level": user_ctx.get("skill_level", "intermediate"),
                    "preferred_communication_style": user_ctx.get("preferred_communication_style", "balanced"),
                    "domain_expertise": user_ctx.get("domain_expertise", []),
                    "interaction_patterns": user_ctx.get("interaction_patterns", {})
                }
            
            # Intelligently combine conversation history with relevance scoring
            conversation_history = []
            if not isinstance(working_ctx, Exception) and working_ctx:
                history_items = working_ctx.get("items", [])
                # Score and rank history items by relevance to current query
                scored_history = await self._score_context_relevance(history_items, query, user_profile_data)
                conversation_history.extend(scored_history)
            
            # Combine and rank relevant insights with adaptive importance
            relevant_insights = []
            if not isinstance(session_ctx, Exception) and session_ctx:
                session_insights = session_ctx.get("insights", [])
                scored_insights = await self._score_insight_relevance(session_insights, query, user_profile_data)
                relevant_insights.extend(scored_insights)
            
            if not isinstance(episodic_ctx, Exception) and episodic_ctx:
                episodic_patterns = episodic_ctx.get("patterns", [])
                scored_patterns = await self._score_pattern_relevance(episodic_patterns, query, user_profile_data)
                relevant_insights.extend(scored_patterns)
            
            # Sort insights by relevance score
            relevant_insights.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
            
            # Determine enhanced domain context with confidence scoring
            domain_context = await self._determine_enhanced_domain_context(query, user_ctx if not isinstance(user_ctx, Exception) else None)
            
            # Create intelligently filtered context
            context = ConversationContext(
                session_id=session_id,
                conversation_history=conversation_history[:8],  # Top 8 most relevant items
                user_profile=user_profile_data,
                relevant_insights=relevant_insights[:6],  # Top 6 most relevant insights
                domain_context=domain_context
            )
            
            # Enhanced performance logging with context quality metrics
            retrieval_time = (time.time() - start_time) * 1000  # Convert to ms
            context_quality_score = self._calculate_context_quality(context)
            
            self._logger.info(
                f"Enhanced context retrieval completed in {retrieval_time:.2f}ms "
                f"(quality score: {context_quality_score:.2f})"
            )
            
            if retrieval_time > 50:  # Performance target
                self._logger.warning(f"Context retrieval exceeded target time: {retrieval_time:.2f}ms")
            
            return context
            
        except Exception as e:
            self._logger.error(f"Failed to retrieve context for session {session_id}: {e}")
            raise MemoryException(f"Context retrieval failed: {str(e)}")
    
    async def consolidate_insights(self, session_id: str, result: Dict[str, Any]) -> bool:
        """Consolidate insights from troubleshooting results into memory
        
        This method processes troubleshooting results to extract patterns and
        insights that can improve future interactions. Consolidation happens
        asynchronously to avoid blocking response generation.
        
        Args:
            session_id: Session identifier for context attribution
            result: Troubleshooting result with findings, solutions, and outcomes
            
        Returns:
            True if consolidation was initiated successfully
            
        Raises:
            MemoryException: When consolidation setup fails
        """
        try:
            # Sanitize input
            if self._sanitizer:
                result = self._sanitizer.sanitize(result)
            
            # Cancel any existing consolidation task for this session
            if session_id in self._consolidation_tasks:
                self._consolidation_tasks[session_id].cancel()
            
            # Start async consolidation task
            task = asyncio.create_task(
                self._perform_consolidation(session_id, result)
            )
            self._consolidation_tasks[session_id] = task
            
            self._logger.info(f"Started insight consolidation for session {session_id}")
            return True
            
        except Exception as e:
            self._logger.error(f"Failed to start consolidation for session {session_id}: {e}")
            raise MemoryException(f"Consolidation setup failed: {str(e)}")
    
    async def get_user_profile(self, session_id: str) -> UserProfile:
        """Get user profile for personalization
        
        Args:
            session_id: Session identifier for user association
            
        Returns:
            UserProfile with skill level, preferences, and interaction patterns
        """
        try:
            # Get user ID from session (simplified for now)
            user_id = f"user_{session_id}"  # In production, would extract from session
            
            # Retrieve user profile from User Memory
            profile_data = await self._user_memory.get_user_profile(user_id)
            
            return UserProfile(
                user_id=profile_data.get("user_id"),
                skill_level=profile_data.get("skill_level", "intermediate"),
                preferred_communication_style=profile_data.get("preferred_communication_style", "balanced"),
                domain_expertise=profile_data.get("domain_expertise", []),
                interaction_patterns=profile_data.get("interaction_patterns", {}),
                historical_context=profile_data.get("historical_context", {})
            )
            
        except Exception as e:
            self._logger.error(f"Failed to get user profile for session {session_id}: {e}")
            # Return default profile on error
            return UserProfile()
    
    async def update_user_profile(self, session_id: str, updates: Dict[str, Any]) -> bool:
        """Update user profile based on interaction patterns
        
        Args:
            session_id: Session identifier for user association
            updates: Profile updates including skill level, preferences, etc.
            
        Returns:
            True if update was successful
        """
        try:
            # Sanitize updates
            if self._sanitizer:
                updates = self._sanitizer.sanitize(updates)
            
            # Get user ID from session
            user_id = f"user_{session_id}"
            
            # Update user profile
            await self._user_memory.update_user_profile(user_id, updates)
            
            self._logger.info(f"Updated user profile for session {session_id}")
            return True
            
        except Exception as e:
            self._logger.error(f"Failed to update user profile for session {session_id}: {e}")
            return False
    
    async def _get_working_context_filtered(self, session_id: str, query: str) -> Dict[str, Any]:
        """Get filtered context from Working Memory with relevance scoring"""
        base_context = await self._working_memory.get_context(session_id)
        
        # Apply intelligent filtering to working memory items
        if "items" in base_context and base_context["items"]:
            filtered_items = await self._filter_working_memory_items(base_context["items"], query)
            base_context["items"] = filtered_items
        
        return base_context
    
    async def _get_session_context_semantic(self, session_id: str, query: str) -> Dict[str, Any]:
        """Get context from Session Memory with semantic relevance matching"""
        insights = await self._session_memory.get_insights(session_id)
        patterns = await self._session_memory.detect_patterns(session_id)
        
        # Filter insights by semantic relevance to query
        if insights:
            insights = await self._filter_insights_by_relevance(insights, query)
        
        return {
            "insights": insights,
            "patterns": patterns,
            "session_id": session_id,
            "semantic_filtering_applied": True
        }
    
    async def _get_user_context(self, session_id: str) -> Dict[str, Any]:
        """Get context from User Memory"""
        user_id = f"user_{session_id}"
        return await self._user_memory.get_user_profile(user_id)
    
    async def _get_episodic_context_ranked(self, query: str) -> Dict[str, Any]:
        """Get ranked context from Episodic Memory with enhanced similarity scoring"""
        patterns = await self._episodic_memory.get_relevant_patterns(query)
        
        # Apply enhanced ranking based on multiple factors
        if patterns:
            patterns = await self._rank_episodic_patterns(patterns, query)
        
        return {
            "patterns": patterns,
            "ranking_applied": True,
            "query_analyzed": len(query.split()) > 2  # Complex query indicator
        }
    
    async def _determine_domain_context(self, query: str, user_context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Determine domain context based on query and user expertise"""
        # Simple domain detection based on keywords
        domain_keywords = {
            "database": ["database", "sql", "query", "connection", "postgres", "mysql", "mongodb"],
            "network": ["network", "connection", "timeout", "dns", "firewall", "port", "tcp", "udp"],
            "application": ["application", "app", "service", "api", "endpoint", "response", "error"],
            "system": ["system", "server", "cpu", "memory", "disk", "performance", "load"],
            "security": ["security", "auth", "authentication", "authorization", "ssl", "certificate"]
        }
        
        query_lower = query.lower()
        detected_domains = []
        
        for domain, keywords in domain_keywords.items():
            if any(keyword in query_lower for keyword in keywords):
                detected_domains.append(domain)
        
        if not detected_domains:
            return None
        
        primary_domain = detected_domains[0]
        user_expertise = user_context.get("domain_expertise", []) if user_context else []
        
        return {
            "primary_domain": primary_domain,
            "detected_domains": detected_domains,
            "user_has_expertise": primary_domain in user_expertise,
            "confidence": 0.8 if len(detected_domains) == 1 else 0.6
        }
    
    async def _perform_consolidation(self, session_id: str, result: Dict[str, Any]) -> None:
        """Perform async insight consolidation"""
        try:
            consolidation_start = time.time()
            
            # Extract insights using LLM
            insights = await self._extract_insights_from_result(result)
            
            # Store insights in Session Memory
            for insight in insights:
                await self._session_memory.store_insight(
                    session_id, 
                    insight["type"], 
                    insight["data"]
                )
            
            # Update working memory with key insights
            for insight in insights[:3]:  # Top 3 insights
                await self._working_memory.add_insight(session_id, insight)
            
            # Update user profile based on successful outcomes
            if result.get("effectiveness", 0) > 0.7:  # High effectiveness
                await self._update_user_profile_from_success(session_id, result)
            
            # Store cross-session patterns in Episodic Memory
            if insights:
                pattern_data = {
                    "problem_domain": result.get("domain", "unknown"),
                    "solution_type": result.get("solution_type", "unknown"),
                    "effectiveness": result.get("effectiveness", 0.0),
                    "insights_count": len(insights),
                    "session_id": session_id
                }
                await self._episodic_memory.store_cross_session_pattern(
                    "solution_effectiveness", 
                    pattern_data
                )
            
            consolidation_time = (time.time() - consolidation_start) * 1000
            self._logger.info(f"Insight consolidation completed in {consolidation_time:.2f}ms")
            
        except Exception as e:
            self._logger.error(f"Consolidation failed for session {session_id}: {e}")
        finally:
            # Clean up task reference
            if session_id in self._consolidation_tasks:
                del self._consolidation_tasks[session_id]
    
    async def _extract_insights_from_result(self, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract insights from troubleshooting result using LLM"""
        try:
            # Prepare prompt for insight extraction
            prompt = f"""
            Analyze the following troubleshooting result and extract key insights:
            
            Result: {json.dumps(result, indent=2)}
            
            Extract insights about:
            1. Problem patterns and root causes
            2. Solution effectiveness and approaches
            3. User interaction patterns
            4. Tool usage effectiveness
            
            Return insights as JSON array with format:
            [
                {{
                    "type": "pattern_type",
                    "data": {{"key": "value"}},
                    "confidence": 0.8
                }}
            ]
            """
            
            # Use LLM to extract insights
            llm_response = await self._llm.generate_response(prompt)
            
            # Parse LLM response (simplified JSON extraction)
            try:
                # Look for JSON array in response
                import re
                json_match = re.search(r'\[.*\]', llm_response, re.DOTALL)
                if json_match:
                    insights_data = json.loads(json_match.group())
                    return insights_data
            except (json.JSONDecodeError, AttributeError):
                pass
            
            # Fallback: create basic insights from result structure
            return self._create_basic_insights(result)
            
        except Exception as e:
            self._logger.warning(f"LLM insight extraction failed: {e}")
            return self._create_basic_insights(result)
    
    def _create_basic_insights(self, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Create basic insights when LLM extraction fails"""
        insights = []
        
        # Root cause insight
        if "root_cause" in result:
            insights.append({
                "type": "root_cause_pattern",
                "data": {
                    "root_cause": result["root_cause"],
                    "confidence": result.get("confidence_score", 0.5)
                },
                "confidence": 0.7
            })
        
        # Solution effectiveness insight
        if "effectiveness" in result:
            insights.append({
                "type": "solution_effectiveness",
                "data": {
                    "effectiveness": result["effectiveness"],
                    "solution_type": result.get("solution_type", "unknown")
                },
                "confidence": 0.8
            })
        
        return insights
    
    async def _update_user_profile_from_success(self, session_id: str, result: Dict[str, Any]) -> None:
        """Update user profile based on successful troubleshooting"""
        updates = {}
        
        # Infer skill level improvement
        if result.get("complexity", "medium") == "high" and result.get("effectiveness", 0) > 0.8:
            updates["skill_level"] = "advanced"
        
        # Track domain expertise
        if "domain" in result:
            updates["domain_expertise"] = [result["domain"]]
        
        # Track successful solution preferences
        if "solution_type" in result:
            updates["interaction_patterns"] = {
                "solution_preferences": [result["solution_type"]]
            }
        
        if updates:
            await self.update_user_profile(session_id, updates)
    
    async def health_check(self) -> Dict[str, Any]:
        """Check health of memory system components"""
        health = {
            "status": "healthy",
            "components": {
                "working_memory": "healthy",
                "session_memory": "healthy", 
                "user_memory": "healthy",
                "episodic_memory": "healthy"
            },
            "metrics": {
                "active_consolidation_tasks": len(self._consolidation_tasks),
                "working_memory_items": len(self._working_memory.items),
                "cached_sessions": len(self._session_memory.cache),
                "cached_users": len(self._user_memory.user_cache)
            }
        }
        
        # Check external dependencies
        if self._vector_store:
            health["components"]["vector_store"] = "available"
        else:
            health["components"]["vector_store"] = "unavailable"
            health["status"] = "degraded"
        
        if self._session_store:
            health["components"]["session_store"] = "available"
        else:
            health["components"]["session_store"] = "unavailable"
            health["status"] = "degraded"
        
        return health
    
    # Advanced Memory Processing Methods
    
    async def _filter_working_memory_items(self, items: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        """Filter working memory items by relevance to current query"""
        query_words = set(query.lower().split())
        scored_items = []
        
        for item in items:
            # Calculate relevance score based on content similarity
            item_text = json.dumps(item).lower()
            item_words = set(item_text.split())
            
            # Jaccard similarity for keyword overlap
            overlap = len(query_words.intersection(item_words))
            union = len(query_words.union(item_words))
            jaccard_score = overlap / union if union > 0 else 0
            
            # Time decay factor (recent items more relevant)
            time_factor = 1.0
            if "timestamp" in item:
                age_hours = (time.time() - item["timestamp"]) / 3600
                time_factor = max(0.1, 1.0 - (age_hours / 24))  # Decay over 24 hours
            
            # Item type importance
            type_importance = 1.0
            if item.get("type") == "insight":
                type_importance = 1.2  # Insights are more important
            elif item.get("type") == "conversation_turn":
                type_importance = 0.9  # Conversation turns slightly less important
            
            # Combined relevance score
            relevance_score = jaccard_score * time_factor * type_importance
            
            if relevance_score > 0.1:  # Minimum threshold
                item_copy = item.copy()
                item_copy["relevance_score"] = relevance_score
                scored_items.append(item_copy)
        
        # Sort by relevance and return top items
        scored_items.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored_items[:10]  # Top 10 most relevant
    
    async def _filter_insights_by_relevance(self, insights: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        """Filter insights by semantic relevance to query"""
        if not insights:
            return []
        
        query_words = set(query.lower().split())
        relevant_insights = []
        
        for insight in insights:
            # Extract text from insight for comparison
            insight_text = ""
            if "data" in insight:
                insight_text = json.dumps(insight["data"]).lower()
            elif "message" in insight:
                insight_text = insight["message"].lower()
            
            insight_words = set(insight_text.split())
            
            # Calculate semantic relevance
            keyword_overlap = len(query_words.intersection(insight_words))
            total_keywords = len(query_words)
            
            if total_keywords > 0:
                relevance = keyword_overlap / total_keywords
                
                # Boost relevance for high-confidence insights
                confidence_boost = insight.get("confidence", 0.5)
                final_relevance = relevance * (1 + confidence_boost)
                
                if final_relevance > 0.2:  # Minimum relevance threshold
                    insight_copy = insight.copy()
                    insight_copy["relevance_score"] = final_relevance
                    relevant_insights.append(insight_copy)
        
        # Sort by relevance
        relevant_insights.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
        return relevant_insights
    
    async def _rank_episodic_patterns(self, patterns: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        """Rank episodic patterns using advanced scoring"""
        if not patterns:
            return []
        
        query_words = set(query.lower().split())
        scored_patterns = []
        
        for pattern in patterns:
            # Base similarity score from episodic memory
            base_score = pattern.get("similarity_score", 0.5)
            
            # Pattern frequency and confidence
            pattern_data = pattern.get("content", {})
            frequency_score = min(1.0, pattern_data.get("frequency", 1) / 10)  # Normalize to 0-1
            confidence_score = pattern_data.get("confidence", 0.5)
            
            # Recency factor (newer patterns may be more relevant)
            recency_score = 1.0
            if "timestamp" in pattern_data:
                age_days = (time.time() - pattern_data["timestamp"]) / (24 * 3600)
                recency_score = max(0.2, 1.0 - (age_days / 30))  # Decay over 30 days
            
            # Domain relevance (if pattern matches query domain)
            domain_score = 1.0
            pattern_type = pattern.get("pattern_type", "")
            if "database" in query.lower() and "database" in pattern_type.lower():
                domain_score = 1.3
            elif "network" in query.lower() and "network" in pattern_type.lower():
                domain_score = 1.3
            
            # Combined ranking score
            final_score = (
                base_score * 0.4 +
                frequency_score * 0.2 +
                confidence_score * 0.2 +
                recency_score * 0.1 +
                domain_score * 0.1
            )
            
            pattern_copy = pattern.copy()
            pattern_copy["ranking_score"] = final_score
            scored_patterns.append(pattern_copy)
        
        # Sort by ranking score
        scored_patterns.sort(key=lambda x: x["ranking_score"], reverse=True)
        return scored_patterns
    
    async def _score_context_relevance(self, history_items: List[Dict[str, Any]], query: str, user_profile: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Score conversation history items for relevance with user profile awareness"""
        if not history_items:
            return []
        
        query_words = set(query.lower().split())
        user_expertise = user_profile.get("domain_expertise", []) if user_profile else []
        
        scored_items = []
        for item in history_items:
            # Base content similarity
            item_text = json.dumps(item).lower()
            item_words = set(item_text.split())
            content_similarity = len(query_words.intersection(item_words)) / len(query_words) if query_words else 0
            
            # User expertise relevance
            expertise_boost = 1.0
            for domain in user_expertise:
                if domain.lower() in item_text:
                    expertise_boost = 1.2
                    break
            
            # Conversation turn recency
            turn_recency = 1.0
            if "timestamp" in item:
                minutes_ago = (time.time() - item["timestamp"]) / 60
                turn_recency = max(0.3, 1.0 - (minutes_ago / 60))  # Decay over 1 hour
            
            # Final relevance score
            relevance_score = content_similarity * expertise_boost * turn_recency
            
            if relevance_score > 0.1:
                item_copy = item.copy()
                item_copy["relevance_score"] = relevance_score
                scored_items.append(item_copy)
        
        scored_items.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored_items
    
    async def _score_insight_relevance(self, insights: List[Dict[str, Any]], query: str, user_profile: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Score insights for relevance with adaptive importance"""
        if not insights:
            return []
        
        query_words = set(query.lower().split())
        user_skill_level = user_profile.get("skill_level", "intermediate") if user_profile else "intermediate"
        
        scored_insights = []
        for insight in insights:
            # Content relevance
            insight_text = json.dumps(insight.get("data", {})).lower()
            insight_words = set(insight_text.split())
            content_score = len(query_words.intersection(insight_words)) / len(query_words) if query_words else 0
            
            # Insight confidence and type
            confidence = insight.get("confidence", 0.5)
            insight_type = insight.get("type", "")
            
            # Skill level adaptation
            skill_adaptation = 1.0
            if user_skill_level == "beginner" and "root_cause" in insight_type:
                skill_adaptation = 1.3  # Boost diagnostic insights for beginners
            elif user_skill_level == "advanced" and "solution" in insight_type:
                skill_adaptation = 1.2  # Boost solution insights for advanced users
            
            # Time relevance
            time_score = 1.0
            if "timestamp" in insight:
                hours_ago = (time.time() - insight["timestamp"]) / 3600
                time_score = max(0.2, 1.0 - (hours_ago / 24))  # Decay over 24 hours
            
            # Combined relevance
            relevance_score = content_score * confidence * skill_adaptation * time_score
            
            if relevance_score > 0.1:
                insight_copy = insight.copy()
                insight_copy["relevance_score"] = relevance_score
                scored_insights.append(insight_copy)
        
        scored_insights.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored_insights
    
    async def _score_pattern_relevance(self, patterns: List[Dict[str, Any]], query: str, user_profile: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Score episodic patterns for relevance"""
        if not patterns:
            return []
        
        user_domains = user_profile.get("domain_expertise", []) if user_profile else []
        
        scored_patterns = []
        for pattern in patterns:
            # Use existing similarity score as base
            base_score = pattern.get("similarity_score", 0.5)
            
            # Domain expertise boost
            domain_boost = 1.0
            pattern_content = json.dumps(pattern.get("content", {})).lower()
            for domain in user_domains:
                if domain.lower() in pattern_content:
                    domain_boost = 1.15
                    break
            
            # Pattern confidence from episodic memory
            pattern_confidence = pattern.get("content", {}).get("confidence", 0.5)
            
            # Final score
            relevance_score = base_score * domain_boost * pattern_confidence
            
            if relevance_score > 0.15:
                pattern_copy = pattern.copy()
                pattern_copy["relevance_score"] = relevance_score
                scored_patterns.append(pattern_copy)
        
        scored_patterns.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored_patterns
    
    async def _determine_enhanced_domain_context(self, query: str, user_context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Enhanced domain context determination with confidence scoring"""
        # Enhanced domain keywords with more comprehensive coverage
        domain_keywords = {
            "database": {
                "keywords": ["database", "sql", "query", "connection", "postgres", "mysql", "mongodb", "table", "index", "schema"],
                "weight": 1.0
            },
            "network": {
                "keywords": ["network", "connection", "timeout", "dns", "firewall", "port", "tcp", "udp", "router", "subnet"],
                "weight": 1.0
            },
            "application": {
                "keywords": ["application", "app", "service", "api", "endpoint", "response", "error", "bug", "crash"],
                "weight": 1.0
            },
            "system": {
                "keywords": ["system", "server", "cpu", "memory", "disk", "performance", "load", "process", "resource"],
                "weight": 1.0
            },
            "security": {
                "keywords": ["security", "auth", "authentication", "authorization", "ssl", "certificate", "encryption", "vulnerability"],
                "weight": 1.1  # Slightly higher weight for security issues
            },
            "cloud": {
                "keywords": ["cloud", "aws", "azure", "gcp", "kubernetes", "docker", "container", "microservice"],
                "weight": 1.0
            }
        }
        
        query_lower = query.lower()
        query_words = set(query_lower.split())
        domain_scores = {}
        
        # Calculate confidence scores for each domain
        for domain, domain_info in domain_keywords.items():
            keywords = domain_info["keywords"]
            weight = domain_info["weight"]
            
            # Count keyword matches
            matches = sum(1 for keyword in keywords if keyword in query_lower)
            keyword_overlap = len([word for word in query_words if word in keywords])
            
            if matches > 0:
                # Confidence based on keyword density and exact matches
                confidence = (matches / len(keywords) + keyword_overlap / len(query_words)) * weight
                domain_scores[domain] = min(1.0, confidence)  # Cap at 1.0
        
        if not domain_scores:
            return None
        
        # Get primary domain and user expertise
        primary_domain = max(domain_scores.keys(), key=lambda d: domain_scores[d])
        primary_confidence = domain_scores[primary_domain]
        
        user_expertise = user_context.get("domain_expertise", []) if user_context else []
        user_has_expertise = primary_domain in user_expertise
        
        # Adjust confidence based on user expertise
        if user_has_expertise:
            primary_confidence = min(1.0, primary_confidence * 1.2)
        
        return {
            "primary_domain": primary_domain,
            "domain_scores": domain_scores,
            "confidence": primary_confidence,
            "user_has_expertise": user_has_expertise,
            "multi_domain": len(domain_scores) > 1,
            "complexity_indicator": len(query_words) > 10  # Complex queries may span domains
        }
    
    def _calculate_context_quality(self, context: ConversationContext) -> float:
        """Calculate quality score for retrieved context"""
        quality_factors = []
        
        # History quality (0-1)
        history_quality = min(1.0, len(context.conversation_history) / 5)  # Target 5 items
        quality_factors.append(history_quality)
        
        # Insights quality (0-1)
        insights_quality = min(1.0, len(context.relevant_insights) / 3)  # Target 3 insights
        quality_factors.append(insights_quality)
        
        # User profile completeness (0-1)
        profile_quality = 0.0
        if context.user_profile:
            profile_fields = ["skill_level", "preferred_communication_style", "domain_expertise"]
            filled_fields = sum(1 for field in profile_fields if context.user_profile.get(field))
            profile_quality = filled_fields / len(profile_fields)
        quality_factors.append(profile_quality)
        
        # Domain context quality (0-1)
        domain_quality = 0.0
        if context.domain_context:
            domain_quality = context.domain_context.get("confidence", 0.0)
        quality_factors.append(domain_quality)
        
        # Average quality score
        return sum(quality_factors) / len(quality_factors)