# FaultMaven Technical Specifications
## Detailed Architecture, Design Decisions & Implementation Guidance

**Version:** 1.0
**Date:** 2025-09-30
**Related Document:** IMPLEMENTATION_PLAN.md

---

## Table of Contents

1. [Memory System Architecture](#1-memory-system-architecture)
2. [Agentic Framework Orchestration](#2-agentic-framework-orchestration)
3. [Tool Ecosystem Design](#3-tool-ecosystem-design)
4. [Prompt Engineering System](#4-prompt-engineering-system)
5. [Context Management Strategy](#5-context-management-strategy)
6. [Integration Patterns](#6-integration-patterns)
7. [Performance Optimization](#7-performance-optimization)
8. [Security & Privacy](#8-security--privacy)

---

## 1. Memory System Architecture

> **IMPORTANT - K8s Microservices Approach:**
>
> This section describes the memory system as **microservices expansion** of the existing Redis deployment on Kubernetes, NOT as monolithic Python code. FaultMaven API pods remain stateless clients that call Redis/ChromaDB microservices. For complete microservices architecture, see `MICROSERVICES_ARCHITECTURE.md`.

### 1.1 Core Modules & Classes

#### Microservices Architecture Overview

**System Design:**
```
┌──────────────────────────────────────────────────────────────┐
│  FaultMaven API Pod (Stateless Client)                       │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  IMemoryService Interface                            │   │
│  │  (Thin client calling Redis/ChromaDB microservices)  │   │
│  └──────────────────────────────────────────────────────┘   │
└────────────────────┬─────────────────────────────────────────┘
                     │
         ┌───────────┴───────────┐
         ▼                       ▼
┌──────────────────┐    ┌─────────────────┐
│ Redis Service    │    │ ChromaDB Service│
│ (K8s Pod)        │    │ (K8s Pod)       │
│                  │    │                 │
│ Tier 1: Working  │    │ Tier 4: Episodic│
│ Tier 2: Session  │    │ (Vector Store)  │
│ Tier 3: User     │    │                 │
└──────────────────┘    └─────────────────┘
```

**Key Principle:** Memory system implemented entirely through **Redis key patterns** and **ChromaDB collections**, not Python classes in FaultMaven codebase.

#### Primary Component: IMemoryService (Client Interface)

**IMemoryService** (`faultmaven/services/domain/memory_service.py`)
```python
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class IMemoryService(ABC):
    """Interface for memory operations across all four tiers (client-side)"""

    # TIER 1: Working Memory (Redis List)
    @abstractmethod
    async def add_to_working_memory(
        self,
        session_id: str,
        content: str,
        content_type: str  # "query" | "response" | "observation"
    ) -> None:
        """Add item to working memory (Redis: memory:working:{session_id})"""
        pass

    @abstractmethod
    async def get_working_memory(
        self,
        session_id: str,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Retrieve recent working memory items from Redis"""
        pass

    # TIER 2: Session Memory (Redis Hash)
    @abstractmethod
    async def store_session_insight(
        self,
        session_id: str,
        insight_id: str,
        insight: Dict[str, Any]
    ) -> None:
        """Store session-level insight (Redis: memory:session:{session_id}:insights)"""
        pass

    @abstractmethod
    async def get_session_insights(
        self,
        session_id: str
    ) -> List[Dict[str, Any]]:
        """Retrieve all session insights from Redis"""
        pass

    @abstractmethod
    async def update_session_metadata(
        self,
        session_id: str,
        metadata: Dict[str, Any]
    ) -> None:
        """Update session metadata (topic, severity, resolved status)"""
        pass

    # TIER 3: User Memory (Redis Hash + Sorted Set)
    @abstractmethod
    async def update_user_profile(
        self,
        user_id: str,
        profile_updates: Dict[str, Any]
    ) -> None:
        """Update user profile (Redis: memory:user:{user_id}:profile)"""
        pass

    @abstractmethod
    async def get_user_profile(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """Retrieve user profile from Redis"""
        pass

    @abstractmethod
    async def record_user_pattern(
        self,
        user_id: str,
        pattern: str,
        increment: int = 1
    ) -> None:
        """Increment pattern frequency (Redis Sorted Set: memory:user:{user_id}:patterns)"""
        pass

    @abstractmethod
    async def get_user_patterns(
        self,
        user_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get top user patterns by frequency"""
        pass

    # TIER 4: Episodic Memory (ChromaDB)
    @abstractmethod
    async def store_episodic_memory(
        self,
        user_id: str,
        session_summary: str,
        metadata: Dict[str, Any]
    ) -> None:
        """Store episodic memory in ChromaDB (semantic search)"""
        pass

    @abstractmethod
    async def search_episodic_memory(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Semantic search in ChromaDB episodic memory"""
        pass

    # Cross-Tier Operations
    @abstractmethod
    async def consolidate_working_to_session(
        self,
        session_id: str
    ) -> None:
        """Extract insights from working memory → session insights"""
        pass

    @abstractmethod
    async def consolidate_session_to_user(
        self,
        session_id: str,
        user_id: str
    ) -> None:
        """Extract patterns from session → user memory"""
        pass

    @abstractmethod
    async def retrieve_relevant_context(
        self,
        session_id: str,
        user_id: str,
        query: str,
        limit: int = 10
    ) -> Dict[str, Any]:
        """Retrieve all relevant context across all memory tiers (parallel)"""
        pass
```

#### Implementation: RedisMemoryService (Microservices Client)

**RedisMemoryService** (`faultmaven/services/domain/redis_memory_service.py`)
```python
from typing import List, Dict, Any, Optional
import json
from datetime import datetime
from redis.asyncio import Redis

from faultmaven.services.domain.memory_service import IMemoryService
from faultmaven.infrastructure.persistence.redis_client import create_redis_client
from faultmaven.infrastructure.persistence.chroma_client import create_chroma_client

class RedisMemoryService(IMemoryService):
    """Memory service implementation using Redis + ChromaDB microservices"""

    def __init__(self):
        self.redis: Redis = None
        self.chroma = None
        self.collection_name = "faultmaven_episodic_memory"

    async def initialize(self):
        """Connect to Redis and ChromaDB microservices on K8s"""
        self.redis = await create_redis_client()
        self.chroma = await create_chroma_client()

    # TIER 1: Working Memory
    async def add_to_working_memory(
        self,
        session_id: str,
        content: str,
        content_type: str
    ) -> None:
        """Add to Redis List, maintain max 20 items, set TTL"""
        key = f"memory:working:{session_id}"

        item = {
            "timestamp": datetime.utcnow().isoformat(),
            "content_type": content_type,
            "content": content,
            "metadata": {}
        }

        # Add to list (prepend for FIFO)
        await self.redis.lpush(key, json.dumps(item))

        # Trim to 20 items
        await self.redis.ltrim(key, 0, 19)

        # Set TTL (10 minutes = 600 seconds)
        await self.redis.expire(key, 600)

    async def get_working_memory(
        self,
        session_id: str,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Retrieve recent working memory items from Redis"""
        key = f"memory:working:{session_id}"
        items = await self.redis.lrange(key, 0, limit - 1)
        return [json.loads(item) for item in items]

    # TIER 2: Session Memory
    async def store_session_insight(
        self,
        session_id: str,
        insight_id: str,
        insight: Dict[str, Any]
    ) -> None:
        """Store insight in Redis Hash with 24h TTL"""
        key = f"memory:session:{session_id}:insights"
        await self.redis.hset(key, insight_id, json.dumps(insight))
        await self.redis.expire(key, 86400)  # 24 hours

    async def get_session_insights(
        self,
        session_id: str
    ) -> List[Dict[str, Any]]:
        """Retrieve all session insights"""
        key = f"memory:session:{session_id}:insights"
        insights_dict = await self.redis.hgetall(key)
        return [json.loads(v) for v in insights_dict.values()]

    async def update_session_metadata(
        self,
        session_id: str,
        metadata: Dict[str, Any]
    ) -> None:
        """Update session metadata"""
        key = f"memory:session:{session_id}:metadata"
        await self.redis.hset(key, mapping=metadata)
        await self.redis.expire(key, 86400)

    # TIER 3: User Memory
    async def update_user_profile(
        self,
        user_id: str,
        profile_updates: Dict[str, Any]
    ) -> None:
        """Update user profile (no TTL, persistent)"""
        key = f"memory:user:{user_id}:profile"
        await self.redis.hset(key, mapping=profile_updates)

    async def get_user_profile(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """Retrieve user profile"""
        key = f"memory:user:{user_id}:profile"
        return await self.redis.hgetall(key)

    async def record_user_pattern(
        self,
        user_id: str,
        pattern: str,
        increment: int = 1
    ) -> None:
        """Increment pattern frequency in Sorted Set"""
        key = f"memory:user:{user_id}:patterns"
        await self.redis.zincrby(key, increment, pattern)

    async def get_user_patterns(
        self,
        user_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get top patterns by frequency"""
        key = f"memory:user:{user_id}:patterns"
        # zrevrange returns [(member, score), ...]
        patterns = await self.redis.zrevrange(key, 0, limit - 1, withscores=True)
        return [{"pattern": p[0], "frequency": int(p[1])} for p in patterns]

    # TIER 4: Episodic Memory
    async def store_episodic_memory(
        self,
        user_id: str,
        session_summary: str,
        metadata: Dict[str, Any]
    ) -> None:
        """Store in ChromaDB collection"""
        collection = await self.chroma.get_or_create_collection(
            name=self.collection_name
        )

        await collection.add(
            documents=[session_summary],
            metadatas=[{
                "user_id": user_id,
                "timestamp": datetime.utcnow().isoformat(),
                **metadata
            }],
            ids=[f"{user_id}_{datetime.utcnow().timestamp()}"]
        )

    async def search_episodic_memory(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Semantic search in ChromaDB"""
        collection = await self.chroma.get_collection(
            name=self.collection_name
        )

        where_clause = {"user_id": user_id} if user_id else None

        results = await collection.query(
            query_texts=[query],
            n_results=limit,
            where=where_clause
        )

        return [
            {
                "content": doc,
                "metadata": meta,
                "distance": dist
            }
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0]
            )
        ]

    # Cross-Tier Operations
    async def consolidate_working_to_session(
        self,
        session_id: str
    ) -> None:
        """Extract insights from working memory → session insights"""
        # Get all working memory items
        working_items = await self.get_working_memory(session_id)

        # Extract insights (simplified - real implementation uses LLM)
        error_items = [
            item for item in working_items
            if "error" in item.get("content", "").lower()
        ]

        if error_items:
            insight = {
                "type": "error_pattern",
                "count": len(error_items),
                "timestamp": datetime.utcnow().isoformat(),
                "items": error_items[:5]
            }

            insight_id = f"insight_{datetime.utcnow().timestamp()}"
            await self.store_session_insight(session_id, insight_id, insight)

    async def consolidate_session_to_user(
        self,
        session_id: str,
        user_id: str
    ) -> None:
        """Extract patterns from session → user memory"""
        insights = await self.get_session_insights(session_id)

        for insight in insights:
            if insight.get("type") == "error_pattern":
                await self.record_user_pattern(
                    user_id,
                    "error_troubleshooting",
                    increment=1
                )

    async def retrieve_relevant_context(
        self,
        session_id: str,
        user_id: str,
        query: str,
        limit: int = 10
    ) -> Dict[str, Any]:
        """Retrieve all relevant context across all memory tiers (parallel)"""
        import asyncio

        # Parallel retrieval from Redis + ChromaDB
        working, session_insights, user_profile, user_patterns, episodic = await asyncio.gather(
            self.get_working_memory(session_id),
            self.get_session_insights(session_id),
            self.get_user_profile(user_id),
            self.get_user_patterns(user_id),
            self.search_episodic_memory(query, user_id)
        )

        return {
            "working_memory": working,
            "session_insights": session_insights,
            "user_profile": user_profile,
            "user_patterns": user_patterns,
            "episodic_memory": episodic
        }
```

#### Redis Key Schema (Data Storage Design)

**Tier 1: Working Memory (Redis List)**
```
Key: memory:working:{session_id}
Type: List (capped at 20 items, FIFO)
TTL: 600 seconds (10 minutes)
Value: JSON[{
  "timestamp": "2025-09-30T10:30:00Z",
  "content_type": "query" | "response" | "observation",
  "content": "...",
  "metadata": {}
}]

Example:
> LRANGE memory:working:sess_abc123 0 -1
1) "{\"timestamp\":\"2025-09-30T10:30:00Z\",\"content_type\":\"query\",\"content\":\"My Redis pod is crashing\"}"
2) "{\"timestamp\":\"2025-09-30T10:31:00Z\",\"content_type\":\"response\",\"content\":\"Let me help troubleshoot...\"}"
```

**Tier 2: Session Memory (Redis Hash)**
```
Key: memory:session:{session_id}:insights
Type: Hash
TTL: 86400 seconds (24 hours)
Fields: insight_id → JSON insight data

Example:
> HGETALL memory:session:sess_abc123:insights
"insight_001" "{\"type\":\"error_pattern\",\"count\":3,\"timestamp\":\"...\"}"
"insight_002" "{\"type\":\"resolution\",\"data\":\"...\"}"

Key: memory:session:{session_id}:metadata
Type: Hash
TTL: 86400 seconds
Fields: topic, severity, resolved, etc.

Example:
> HGETALL memory:session:sess_abc123:metadata
"topic" "kubernetes troubleshooting"
"severity" "high"
"resolved" "true"
```

**Tier 3: User Memory (Redis Hash + Sorted Set)**
```
Key: memory:user:{user_id}:profile
Type: Hash
TTL: None (persistent)
Fields: expertise_level, preferred_verbosity, tech_stack, etc.

Example:
> HGETALL memory:user:user_xyz:profile
"expertise_level" "intermediate"
"preferred_verbosity" "detailed"
"tech_stack" "kubernetes,redis,postgresql"

Key: memory:user:{user_id}:patterns
Type: Sorted Set (pattern → frequency score)
TTL: None (persistent)

Example:
> ZREVRANGE memory:user:user_xyz:patterns 0 9 WITHSCORES
1) "error_troubleshooting"
2) "15"
3) "kubernetes_networking"
4) "8"
5) "redis_oom"
6) "5"
```

**Tier 4: Episodic Memory (ChromaDB Collection)**
```
Collection: faultmaven_episodic_memory
Embedding Model: BGE-M3
Documents: Successful troubleshooting session summaries
Metadata: {
  "user_id": "...",
  "timestamp": "...",
  "issue_type": "...",
  "resolution": "...",
  "success_score": 0.95
}

Query Example:
> collection.query(query_texts=["Redis OOM error"], n_results=5)
Returns: Top 5 semantically similar past resolutions
```

#### Interface Definitions

**IMemoryService** (`faultmaven/models/interfaces.py`)
```python
class IMemoryService(ABC):
    """Service interface for memory operations"""

    @abstractmethod
    async def store_conversation_turn(
        self,
        session_id: str,
        user_query: str,
        agent_response: str,
        metadata: Dict[str, Any]
    ) -> None:
        """Store a conversation turn in memory with metadata"""
        pass

    @abstractmethod
    async def retrieve_relevant_context(
        self,
        session_id: str,
        query: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Retrieve relevant context for current query"""
        pass

    @abstractmethod
    async def consolidate_memory(
        self,
        session_id: str
    ) -> Dict[str, Any]:
        """Consolidate and summarize session memory"""
        pass

    @abstractmethod
    async def get_user_patterns(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """Get learned patterns for user"""
        pass

    @abstractmethod
    async def extract_facts(
        self,
        session_id: str
    ) -> List[str]:
        """Extract important facts from conversation"""
        pass
```

### 1.2 Key Design Decisions

#### Decision 1: Four-Tier Hierarchy vs Two-Tier
**Choice:** Four-tier (Working → Session → User → Episodic)
**Rationale:**
- **Separation of Concerns**: Each tier has distinct lifecycle and access patterns
- **Performance**: Hot data (working) separate from cold data (episodic)
- **Privacy**: User-specific data isolated for GDPR compliance
- **Scalability**: Different tiers can scale independently

**Trade-offs:**
- ✅ Pros: Better performance, clearer data lifecycle, easier privacy management
- ❌ Cons: More complexity, potential data sync issues
- **Mitigation**: Use async consolidation to maintain consistency without blocking

#### Decision 2: Vector Store for Episodic Memory
**Choice:** ChromaDB with BGE-M3 embeddings
**Rationale:**
- **Semantic Retrieval**: Find similar past incidents even with different wording
- **Scale**: Handles millions of episodes efficiently
- **Context-Aware**: Embeddings capture meaning, not just keywords

**Alternatives Considered:**
- Keyword-based search (Elasticsearch): Too literal, misses semantic similarity
- Graph database (Neo4j): Overkill for our use case, harder to maintain

#### Decision 3: Redis vs PostgreSQL for Session/User Memory
**Choice:** Redis with JSON serialization
**Rationale:**
- **Performance**: Sub-millisecond access times (target: <50ms)
- **TTL Support**: Automatic expiration of old sessions
- **Atomic Operations**: Safe concurrent access
- **Simple Schema**: No migrations needed for schema evolution

**Trade-offs:**
- ✅ Pros: Fast, simple, built-in TTL
- ❌ Cons: Less queryable than SQL, memory-bound
- **Mitigation**: Use episodic memory (ChromaDB) for complex queries

#### Decision 4: Async Consolidation vs Blocking
**Choice:** Async consolidation with background tasks
**Rationale:**
- **User Experience**: Don't block response generation
- **Resource Efficiency**: Batch consolidation operations
- **Failure Isolation**: Consolidation errors don't affect responses

**Implementation Pattern:**
```python
async def store_conversation_turn(self, session_id, query, response):
    # Immediate storage (fast)
    await self._working_memory.add_conversation_turn(session_id, {
        "query": query,
        "response": response,
        "timestamp": datetime.utcnow()
    })

    # Trigger async consolidation (non-blocking)
    asyncio.create_task(
        self._consolidate_insights(session_id, response)
    )
```

### 1.3 Design Principles & Strategies

#### Principle 1: Privacy-First Design
**Strategy:**
- All data sanitized through `ISanitizer` interface before storage
- User consent required for cross-session data in episodic memory
- Configurable retention policies per memory tier
- User can request data deletion (GDPR right to be forgotten)

**Implementation:**
```python
async def store_insight(self, session_id: str, insight: Dict):
    # Sanitize before storage
    if self._sanitizer:
        insight = self._sanitizer.sanitize(insight)

    # Check user consent for long-term storage
    user_id = await self._get_user_id(session_id)
    if not await self._has_consent(user_id, "episodic_memory"):
        # Store only in session memory (24h TTL)
        await self._session_memory.store_insight(session_id, insight)
    else:
        # Store in episodic memory for long-term learning
        await self._episodic_memory.store_episode(...)
```

#### Principle 2: Intelligent Context Filtering
**Strategy:**
- Not all memory is equally relevant - rank by importance and recency
- Use semantic similarity to filter context
- Adaptive importance scoring based on content type

**Relevance Scoring Algorithm:**
```python
async def _score_context_relevance(self, items: List, query: str) -> List:
    """Score memory items by relevance to current query"""
    scored_items = []

    for item in items:
        score = 0.0

        # Recency boost (exponential decay)
        age_hours = (datetime.utcnow() - item['timestamp']).total_seconds() / 3600
        recency_score = math.exp(-age_hours / 24)  # Half-life: 24 hours

        # Semantic similarity (if embeddings available)
        if 'embedding' in item and self._vector_store:
            query_embedding = await self._get_embedding(query)
            semantic_score = cosine_similarity(query_embedding, item['embedding'])
        else:
            semantic_score = 0.5  # Default

        # Importance boost from metadata
        importance_multiplier = item.get('importance', 0.5)

        # Composite score
        score = (0.3 * recency_score + 0.5 * semantic_score + 0.2 * importance_multiplier)

        scored_items.append({**item, 'relevance_score': score})

    return sorted(scored_items, key=lambda x: x['relevance_score'], reverse=True)
```

#### Principle 3: Performance-Driven Architecture
**Strategy:**
- Hot path (working memory) optimized for <10ms access
- Cold path (episodic memory) optimized for <200ms retrieval
- Caching layer between tiers to reduce latency

**Performance Targets:**
| Tier | Operation | Target Latency | Strategy |
|------|-----------|----------------|----------|
| Working | Read | <10ms | In-memory list |
| Working | Write | <10ms | Append-only, async consolidation |
| Session | Read | <50ms | Redis with local cache |
| Session | Write | <50ms | Redis with batching |
| User | Read | <100ms | Redis with TTL cache |
| User | Write | <100ms | Redis with async updates |
| Episodic | Read | <200ms | ChromaDB with result cache |
| Episodic | Write | <500ms | Async batch insertion |

#### Principle 4: Graceful Degradation
**Strategy:**
- System continues working even if memory tiers fail
- Fallback to stateless operation if Redis unavailable
- Degrade to keyword search if vector store unavailable

**Fallback Chain:**
```python
async def retrieve_context(self, session_id: str, query: str):
    try:
        # Try full context retrieval with all tiers
        return await self._retrieve_full_context(session_id, query)
    except MemoryTierUnavailable as e:
        logger.warning(f"Memory tier unavailable: {e.tier}")

        # Fallback 1: Skip unavailable tier
        return await self._retrieve_partial_context(
            session_id, query, skip_tiers=[e.tier]
        )
    except AllMemoryTiersUnavailable:
        logger.error("All memory tiers unavailable, using stateless mode")

        # Fallback 2: Stateless operation (no context)
        return ConversationContext(
            session_id=session_id,
            conversation_history=[],
            user_profile=None,
            relevant_insights=[],
            domain_context={}
        )
```

### 1.4 Component-Specific Considerations

#### Redis Storage Schema

**Keys:**
```
memory:working:{session_id}           → List[MemoryItem]
memory:session:{session_id}:insights  → Hash{insight_id → insight_json}
memory:session:{session_id}:patterns  → Hash{pattern_id → pattern_json}
memory:user:{user_id}:profile         → Hash{profile_fields}
memory:user:{user_id}:patterns        → Hash{pattern_type → List[Pattern]}
memory:consolidation:queue            → List[ConsolidationTask]
```

**TTL Strategy:**
- Working memory: No TTL (cleared explicitly)
- Session memory: 24-hour TTL (configurable)
- User memory: No TTL (persistent)
- Consolidation queue: 1-hour TTL (safety net)

#### ChromaDB Collection Schema

**Collection:** `episodic_memory`
```python
{
    "id": "episode_<uuid>",
    "embedding": [0.123, -0.456, ...],  # BGE-M3 embeddings (1024-dim)
    "metadata": {
        "episode_type": "troubleshooting_success",
        "problem_domain": "kubernetes",
        "solution_type": "configuration_fix",
        "user_skill_level": "intermediate",
        "timestamp": "2025-09-30T10:00:00Z",
        "confidence_score": 0.85,
        "outcome": "resolved",
        "key_facts": ["OOMKilled", "memory_limit_too_low", "increased_to_512Mi"]
    },
    "document": "User reported Kubernetes pod OOMKilled errors. Root cause: memory limits set too low at 256Mi. Solution: increased memory limits to 512Mi. Outcome: Pod stable after change."
}
```

**Indexes:**
- Vector index on `embedding` (HNSW for fast similarity search)
- Metadata filters: `problem_domain`, `solution_type`, `timestamp`

#### Memory Consolidation Pipeline

**Stages:**
1. **Extraction**: Extract key facts and insights from conversation
2. **Classification**: Classify episode type and domain
3. **Embedding**: Generate embeddings for semantic retrieval
4. **Pattern Detection**: Identify recurring patterns
5. **Storage**: Store in appropriate memory tier
6. **Cleanup**: Remove low-value entries

**LLM-Powered Extraction:**
```python
async def _extract_key_facts(self, conversation_turns: List[Dict]) -> List[str]:
    """Use LLM to extract key facts from conversation"""

    prompt = f"""Extract 3-5 key facts from this troubleshooting conversation.
Focus on:
- Problem symptoms
- Root cause identified
- Solution applied
- Outcome

Conversation:
{self._format_conversation(conversation_turns)}

Output format: List of concise facts"""

    response = await self._llm_provider.generate(
        prompt=prompt,
        temperature=0.3,  # Low temperature for factual extraction
        max_tokens=300
    )

    return self._parse_facts(response)
```

---

## 2. Agentic Framework Orchestration

### 2.1 Core Modules & Classes

#### AgentService Orchestrator

**AgentService** (`faultmaven/services/agentic/orchestration/agent_service.py`)
```python
class AgentService(BaseService):
    """Main orchestrator for 7-component agentic framework"""

    def __init__(
        self,
        # Core dependencies
        llm_provider: ILLMProvider,
        sanitizer: ISanitizer,
        tracer: ITracer,

        # 7 Agentic Components
        query_classification_engine: IQueryClassificationEngine,
        agent_state_manager: IAgentStateManager,
        tool_skill_broker: IToolSkillBroker,
        business_logic_workflow_engine: IBusinessLogicWorkflowEngine,
        guardrails_policy_layer: IGuardrailsPolicyLayer,
        response_synthesizer: IResponseSynthesizer,
        error_fallback_manager: IErrorFallbackManager,

        # Supporting services
        memory_service: IMemoryService,
        session_service: ISessionService
    ):
        # Initialize all components
        pass

    async def process_query_for_case(
        self,
        case_id: str,
        request: QueryRequest
    ) -> AgentResponse:
        """Main entry point - orchestrates all 7 components"""

        # 1. Sanitize & classify
        sanitized = self._sanitizer.sanitize(request.query)
        classification = await self.query_classification_engine.classify_query(sanitized)

        # 2. Load agent state & memory
        agent_state = await self.agent_state_manager.load_state(request.session_id)
        memory_context = await self.memory_service.retrieve_relevant_context(
            request.session_id, sanitized
        )

        # 3. Select tools
        tools = await self.tool_skill_broker.select_tools(
            classification=classification,
            agent_state=agent_state
        )

        # 4. Execute workflow
        workflow_result = await self.business_logic_workflow_engine.execute(
            query=sanitized,
            classification=classification,
            agent_state=agent_state,
            memory_context=memory_context,
            tools=tools,
            llm_provider=self._llm_provider
        )

        # 5. Validate with guardrails
        validated = await self.guardrails_policy_layer.validate(
            result=workflow_result,
            classification=classification
        )

        # 6. Synthesize response
        response = await self.response_synthesizer.synthesize(
            workflow_result=validated,
            classification=classification,
            agent_state=agent_state
        )

        # 7. Error handling (if needed)
        if workflow_result.has_errors:
            response = await self.error_fallback_manager.handle_error(
                error=workflow_result.error,
                context={"query": request.query, "case_id": case_id}
            )

        # 8. Update state & memory
        await self.agent_state_manager.save_state(
            session_id=request.session_id,
            agent_state=workflow_result.updated_state
        )
        await self.memory_service.store_conversation_turn(
            session_id=request.session_id,
            user_query=request.query,
            agent_response=response.content,
            metadata=workflow_result.metadata
        )

        return response
```

#### Workflow Engine (Five-Phase Doctrine)

**BusinessLogicWorkflowEngine** (`faultmaven/services/agentic/engines/workflow_engine.py`)
```python
class BusinessLogicWorkflowEngine(IBusinessLogicWorkflowEngine):
    """Implements five-phase SRE troubleshooting doctrine"""

    async def execute(
        self,
        query: str,
        classification: QueryClassification,
        agent_state: AgentState,
        memory_context: ConversationContext,
        tools: List[BaseTool],
        llm_provider: ILLMProvider
    ) -> WorkflowResult:
        """Execute Plan→Execute→Observe→Re-plan cycle"""

        # Determine current phase
        current_phase = agent_state.current_phase or Phase.DEFINE_BLAST_RADIUS

        # Get phase-specific configuration
        phase_config = TroubleshootingDoctrine.get_phase_config(current_phase)

        # Build phase-aware prompt
        prompt = self._build_phase_prompt(
            query=query,
            phase=current_phase,
            phase_config=phase_config,
            agent_state=agent_state,
            memory_context=memory_context,
            available_tools=[tool.name for tool in tools]
        )

        # Execute reasoning with tools
        reasoning_result = await self._execute_llm_with_tools(
            prompt=prompt,
            tools=tools,
            llm_provider=llm_provider,
            max_iterations=5
        )

        # Assess phase completion
        phase_complete = self._assess_phase_completion(
            reasoning_result=reasoning_result,
            phase=current_phase,
            phase_config=phase_config
        )

        # Determine next phase
        if phase_complete:
            next_phase = self._advance_to_next_phase(current_phase)
        else:
            next_phase = current_phase

        # Update agent state
        updated_state = agent_state.copy(deep=True)
        updated_state.current_phase = next_phase
        updated_state.phase_history.append({
            "phase": current_phase.value,
            "completed": phase_complete,
            "reasoning": reasoning_result.summary
        })

        return WorkflowResult(
            reasoning=reasoning_result.content,
            current_phase=next_phase,
            phase_complete=phase_complete,
            tools_used=reasoning_result.tools_used,
            updated_state=updated_state,
            metadata=reasoning_result.metadata
        )

    def _build_phase_prompt(
        self,
        query: str,
        phase: Phase,
        phase_config: Dict,
        agent_state: AgentState,
        memory_context: ConversationContext,
        available_tools: List[str]
    ) -> str:
        """Build phase-specific prompt with doctrine guidance"""

        prompt_parts = [
            f"# TROUBLESHOOTING PHASE: {phase.value.upper()}",
            f"\n## Objective\n{phase_config['objective']}",
            f"\n## Key Questions",
            *[f"- {q}" for q in phase_config['key_questions']],
            f"\n## Available Tools\n{', '.join(available_tools)}",
            f"\n## Success Criteria",
            *[f"- {c}" for c in phase_config['success_criteria']],
        ]

        # Add memory context
        if memory_context.relevant_insights:
            prompt_parts.append(f"\n## Relevant Past Insights")
            for insight in memory_context.relevant_insights[:3]:
                prompt_parts.append(f"- {insight['summary']}")

        # Add agent state (previous findings)
        if agent_state.findings:
            prompt_parts.append(f"\n## Previous Findings")
            for finding in agent_state.findings:
                prompt_parts.append(f"- {finding}")

        prompt_parts.extend([
            f"\n## User Query\n{query}",
            f"\n## Instructions",
            f"Based on the {phase.value} phase, analyze the query and provide specific insights.",
            f"Use available tools as needed. Be concrete and actionable.",
            f"If this phase is complete, clearly state what you've accomplished."
        ])

        return "\n".join(prompt_parts)
```

### 2.2 Key Design Decisions

#### Decision 1: Sequential vs Parallel Component Execution
**Choice:** Sequential execution with dependency resolution
**Rationale:**
- Components have clear dependencies (classification → tool selection → execution)
- Sequential flow easier to debug and trace
- Performance adequate (<2s total for most queries)

**Optimization:** Parallelize where possible (e.g., memory retrieval + state loading)

#### Decision 2: Five-Phase Doctrine vs Free-Form Reasoning
**Choice:** Structured five-phase SRE doctrine
**Rationale:**
- **Consistency**: Every troubleshooting session follows best practices
- **Completeness**: Phases ensure no steps are skipped
- **Learning**: Phase-specific patterns easier to learn from
- **User Trust**: Transparent, structured approach builds confidence

**Trade-offs:**
- ✅ Pros: Structured, complete, learnable
- ❌ Cons: May feel rigid for simple queries
- **Mitigation**: Allow phase skipping for simple/status queries

#### Decision 3: LLM-Powered vs Rule-Based Phase Transitions
**Choice:** LLM-powered assessment with validation rules
**Rationale:**
- **Flexibility**: LLM can assess nuanced completion criteria
- **Robustness**: Validation rules prevent incorrect transitions
- **Learning**: LLM gets better with examples

**Implementation:**
```python
def _assess_phase_completion(self, reasoning_result, phase, phase_config):
    """Assess if phase is complete using LLM + validation rules"""

    # LLM assessment
    llm_assessment = self._llm_assess_completion(reasoning_result, phase_config)

    # Validation rules
    rule_checks = []
    for criterion in phase_config['success_criteria']:
        rule_checks.append(
            self._check_criterion_met(criterion, reasoning_result)
        )

    # Combine: Require both LLM agreement AND all rules passed
    return llm_assessment and all(rule_checks)
```

#### Decision 4: Tool Execution: Sync vs Async
**Choice:** Async tool execution with timeout protection
**Rationale:**
- **Performance**: Don't block on slow tools
- **Reliability**: Timeout prevents hanging
- **Parallelization**: Can execute multiple tools concurrently

**Pattern:**
```python
async def _execute_llm_with_tools(self, prompt, tools, llm_provider, max_iterations=5):
    """Execute LLM reasoning loop with tool calls"""

    for iteration in range(max_iterations):
        # LLM decides which tool to use (if any)
        llm_response = await llm_provider.generate_with_functions(
            prompt=prompt,
            functions=[tool.get_schema() for tool in tools],
            temperature=0.3
        )

        if not llm_response.function_call:
            # No tool needed, reasoning complete
            return llm_response

        # Execute tool with timeout
        tool = self._get_tool_by_name(llm_response.function_call.name)
        try:
            tool_result = await asyncio.wait_for(
                tool.execute(llm_response.function_call.arguments),
                timeout=10.0  # 10s tool timeout
            )
        except asyncio.TimeoutError:
            tool_result = {"error": "Tool execution timed out"}

        # Add tool result to context
        prompt = self._append_tool_result(prompt, tool_result)

    return llm_response  # Max iterations reached
```

### 2.3 Design Principles & Strategies

#### Principle 1: Explicit State Management
**Strategy:**
- AgentState tracks all execution context
- State persisted after each interaction
- State includes phase history for debugging

**State Schema:**
```python
@dataclass
class AgentState:
    session_id: str
    case_id: str
    current_phase: Phase
    phase_history: List[Dict]  # [{phase, completed, reasoning, timestamp}]
    findings: List[str]  # Accumulated facts
    hypotheses: List[Dict]  # [{hypothesis, confidence, evidence}]
    actions_taken: List[Dict]  # [{action, result, timestamp}]
    context: Dict[str, Any]  # Flexible context storage
    metadata: Dict[str, Any]
```

#### Principle 2: Observability-Driven
**Strategy:**
- Every component call traced
- Metrics collected at each phase
- Errors captured with full context

**Tracing Pattern:**
```python
@trace("workflow_engine_execute")
async def execute(self, ...):
    # Trace includes:
    # - query classification
    # - current phase
    # - tools selected
    # - phase completion status
    # - execution time
    pass
```

#### Principle 3: Fail-Safe by Default
**Strategy:**
- Every component has a fallback
- System continues even if components fail
- Errors logged but don't crash

**Fallback Chain:**
```
Classification fails → Use default classification (general troubleshooting)
Tool execution fails → Use cached results or skip tool
Memory unavailable → Use stateless mode
LLM timeout → Return partial response with recovery instructions
```

### 2.4 Component-Specific Considerations

#### Query Classification Engine

**Multi-Dimensional Classification:**
```python
class QueryClassification:
    intent: QueryIntent  # troubleshooting | status | explanation | configuration
    complexity: ComplexityLevel  # simple | moderate | complex | expert
    domain: TechnicalDomain  # database | networking | infrastructure | etc.
    urgency: UrgencyLevel  # low | medium | high | critical
    confidence: float  # 0.0-1.0
    reasoning: str  # Why this classification
```

**Classification Strategy:**
1. **Pattern Matching (Fast Path)**: Regex patterns for common queries (<10ms)
2. **LLM Classification (Slow Path)**: For ambiguous queries (100-200ms)
3. **Hybrid**: Use patterns first, fallback to LLM if low confidence

#### Tool Selection Strategy

**Selection Criteria:**
- Query classification (intent, domain)
- Current troubleshooting phase
- Tool availability and health
- Historical effectiveness

**Example:**
```python
async def select_tools(self, classification, agent_state):
    """Select tools based on classification and phase"""

    tools = []

    # Phase-specific tools
    if agent_state.current_phase == Phase.DEFINE_BLAST_RADIUS:
        tools.extend([
            self._get_tool("knowledge_base_search"),
            self._get_tool("status_check")
        ])
    elif agent_state.current_phase == Phase.ESTABLISH_TIMELINE:
        tools.extend([
            self._get_tool("log_analysis"),
            self._get_tool("metrics_query")
        ])

    # Domain-specific tools
    if classification.domain == TechnicalDomain.DATABASE:
        tools.append(self._get_tool("database_query"))

    # Filter by health
    tools = [t for t in tools if self._is_tool_healthy(t)]

    return tools
```

---

## 3. Tool Ecosystem Design

### 3.1 Core Modules & Classes

#### Tool Interface

**BaseTool** (`faultmaven/models/interfaces.py`)
```python
class BaseTool(ABC):
    """Base interface for all agent tools"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool identifier"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for LLM"""
        pass

    @abstractmethod
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Execute tool with parameters"""
        pass

    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """Get JSON schema for LLM function calling"""
        pass

    @property
    def category(self) -> ToolCategory:
        """Tool category (search, analysis, execution)"""
        return ToolCategory.ANALYSIS

    @property
    def safety_level(self) -> SafetyLevel:
        """Safety level (safe, caution, dangerous)"""
        return SafetyLevel.SAFE
```

#### Tool Result Model

```python
@dataclass
class ToolResult:
    success: bool
    data: Dict[str, Any]
    metadata: Dict[str, Any]
    execution_time: float
    error_message: Optional[str] = None
    sources: List[Source] = field(default_factory=list)
```

#### Log Analysis Tool

**LogAnalysisTool** (`faultmaven/tools/log_analysis.py`)
```python
class LogAnalysisTool(BaseTool):
    """Analyze logs for errors, patterns, and anomalies"""

    name = "log_analysis"
    description = """Analyze log files to identify errors, warnings, patterns, and anomalies.
    Supports JSON logs, plain text logs, and structured logs.
    Use when troubleshooting issues that may be evident in logs."""

    def __init__(self, llm_provider: ILLMProvider):
        self.llm_provider = llm_provider
        self._init_patterns()

    def _init_patterns(self):
        """Initialize common log patterns"""
        self.error_patterns = [
            r'\b(ERROR|Error|error:)\b',
            r'\b(FATAL|Fatal|fatal:)\b',
            r'\b(Exception|exception)\b',
            r'\bfailed\b|\bFAILED\b',
            r'\bOOMKilled\b',
            r'\bconnection refused\b',
            r'\btimeout\b|\btimed out\b',
            r'\bcrash\b|\bCRASH\b'
        ]

        self.warning_patterns = [
            r'\b(WARN|Warning|warning:)\b',
            r'\bdeprecated\b',
            r'\bretry\b|\bretrying\b'
        ]

        self.k8s_patterns = {
            'oom_killed': r'OOMKilled',
            'crash_loop': r'CrashLoopBackOff',
            'image_pull_error': r'ImagePullBackOff|ErrImagePull',
            'readiness_failed': r'Readiness probe failed',
            'liveness_failed': r'Liveness probe failed'
        }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Analyze logs"""
        start_time = time.time()

        log_content = params.get("log_content", "")
        log_type = params.get("log_type", "auto")  # auto, json, plain, k8s

        if not log_content:
            return ToolResult(
                success=False,
                data={"error": "No log content provided"},
                metadata={},
                execution_time=time.time() - start_time
            )

        # 1. Parse logs
        parsed_logs = self._parse_logs(log_content, log_type)

        # 2. Detect errors
        errors = self._detect_errors(parsed_logs)

        # 3. Detect warnings
        warnings = self._detect_warnings(parsed_logs)

        # 4. Detect K8s-specific issues (if applicable)
        k8s_issues = self._detect_k8s_issues(parsed_logs)

        # 5. Build timeline
        timeline = self._build_timeline(parsed_logs, errors)

        # 6. Detect patterns
        patterns = self._detect_patterns(errors)

        # 7. LLM-powered analysis for deeper insights
        llm_analysis = await self._llm_analyze(
            errors=errors[:10],  # Top 10 errors
            warnings=warnings[:5],
            k8s_issues=k8s_issues,
            patterns=patterns,
            timeline=timeline
        )

        execution_time = time.time() - start_time

        return ToolResult(
            success=True,
            data={
                "summary": llm_analysis["summary"],
                "errors_found": len(errors),
                "warnings_found": len(warnings),
                "error_details": errors[:10],  # Top 10
                "k8s_issues": k8s_issues,
                "patterns": patterns,
                "timeline": timeline,
                "root_cause_hypothesis": llm_analysis.get("root_cause"),
                "recommendations": llm_analysis.get("recommendations", [])
            },
            metadata={
                "total_lines": len(parsed_logs),
                "log_type": log_type,
                "analysis_depth": "llm_enhanced"
            },
            execution_time=execution_time
        )

    def _parse_logs(self, content: str, log_type: str) -> List[Dict]:
        """Parse log content into structured format"""
        if log_type == "json" or (log_type == "auto" and content.strip().startswith('{')):
            return self._parse_json_logs(content)
        elif log_type == "k8s":
            return self._parse_k8s_logs(content)
        else:
            return self._parse_plain_logs(content)

    def _parse_json_logs(self, content: str) -> List[Dict]:
        """Parse JSON logs"""
        logs = []
        for line in content.split('\n'):
            if not line.strip():
                continue
            try:
                log_entry = json.loads(line)
                logs.append({
                    'timestamp': log_entry.get('timestamp'),
                    'level': log_entry.get('level', 'INFO'),
                    'message': log_entry.get('message', ''),
                    'raw': line
                })
            except json.JSONDecodeError:
                logs.append({
                    'timestamp': None,
                    'level': 'UNKNOWN',
                    'message': line,
                    'raw': line
                })
        return logs

    def _detect_errors(self, logs: List[Dict]) -> List[Dict]:
        """Detect error entries in logs"""
        errors = []
        for log in logs:
            message = log.get('message', '')
            for pattern in self.error_patterns:
                if re.search(pattern, message, re.IGNORECASE):
                    errors.append({
                        'timestamp': log.get('timestamp'),
                        'message': message,
                        'pattern_matched': pattern,
                        'context': log
                    })
                    break  # Only match once per log
        return errors

    def _detect_patterns(self, errors: List[Dict]) -> List[Dict]:
        """Detect repeating patterns in errors"""
        patterns = []
        error_messages = [e['message'] for e in errors]

        # Group similar errors
        from collections import Counter
        message_counts = Counter(error_messages)

        for message, count in message_counts.most_common(5):
            if count > 1:
                patterns.append({
                    'pattern': message,
                    'occurrences': count,
                    'first_seen': errors[0]['timestamp'],
                    'last_seen': errors[-1]['timestamp']
                })

        return patterns

    async def _llm_analyze(self, **kwargs) -> Dict[str, Any]:
        """Use LLM for deeper log analysis"""
        prompt = f"""Analyze these log patterns and provide insights:

Errors Found: {kwargs['errors'][:5]}
Warnings: {kwargs['warnings'][:3]}
K8s Issues: {kwargs['k8s_issues']}
Patterns: {kwargs['patterns']}
Timeline: {kwargs['timeline']}

Provide:
1. **Summary**: What went wrong (2-3 sentences)
2. **Root Cause**: Most likely root cause hypothesis
3. **Recommendations**: Specific actionable recommendations (3-5 items)

Be concise and specific. Focus on actionable insights."""

        response = await self.llm_provider.generate(
            prompt=prompt,
            temperature=0.3,
            max_tokens=500
        )

        # Parse LLM response
        return {
            "summary": self._extract_section(response, "Summary"),
            "root_cause": self._extract_section(response, "Root Cause"),
            "recommendations": self._extract_list(response, "Recommendations")
        }

    def get_schema(self) -> Dict[str, Any]:
        """Tool schema for function calling"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "log_content": {
                        "type": "string",
                        "description": "The log content to analyze"
                    },
                    "log_type": {
                        "type": "string",
                        "enum": ["auto", "json", "plain", "k8s"],
                        "description": "Log format type",
                        "default": "auto"
                    }
                },
                "required": ["log_content"]
            }
        }
```

### 3.2 Key Design Decisions

#### Decision 1: Tool Interface Design
**Choice:** Abstract BaseTool with execute() and get_schema()
**Rationale:**
- **Standardization**: All tools follow same interface
- **LLM Integration**: get_schema() provides function calling spec
- **Type Safety**: Strong typing with ToolResult

#### Decision 2: LLM-Enhanced vs Pure Rule-Based Tools
**Choice:** Hybrid approach - rules + LLM analysis
**Rationale:**
- **Speed**: Rules handle common cases quickly
- **Intelligence**: LLM handles complex patterns
- **Cost**: LLM only when needed

#### Decision 3: Sync vs Async Tool Execution
**Choice:** Async with timeout
**Rationale:**
- **Performance**: Non-blocking execution
- **Reliability**: Timeout prevents hanging
- **Composability**: Easy to run tools in parallel

### 3.3 Design Principles & Strategies

#### Principle 1: Fail-Safe Execution
**Strategy:**
- Tools never throw exceptions to caller
- Errors returned in ToolResult
- Partial results better than no results

#### Principle 2: Observable Operations
**Strategy:**
- All tool executions traced
- Execution time measured
- Results include metadata for debugging

#### Principle 3: Progressive Enhancement
**Strategy:**
- Basic functionality without LLM
- LLM adds deeper analysis
- Graceful degradation if LLM unavailable

### 3.4 Additional Tools to Implement

#### ConfigValidationTool
```python
class ConfigValidationTool(BaseTool):
    """Validate Kubernetes manifests, Docker configs, env files"""

    async def execute(self, params):
        config = params["config"]
        config_type = params["type"]  # k8s, docker, env

        # Validate syntax
        syntax_errors = self._validate_syntax(config, config_type)

        # Check common misconfigurations
        misconfigs = self._check_common_issues(config, config_type)

        # LLM-powered best practice check
        best_practice_issues = await self._llm_check_best_practices(config)

        return ToolResult(
            success=len(syntax_errors) == 0,
            data={
                "syntax_errors": syntax_errors,
                "misconfigurations": misconfigs,
                "best_practice_issues": best_practice_issues,
                "recommendations": self._generate_fixes(syntax_errors + misconfigs)
            },
            metadata={"config_type": config_type}
        )
```

#### MetricsQueryTool
```python
class MetricsQueryTool(BaseTool):
    """Query Prometheus/Grafana metrics"""

    def __init__(self, prometheus_url: str):
        self.prometheus_url = prometheus_url

    async def execute(self, params):
        query = params["query"]  # PromQL query
        time_range = params.get("time_range", "5m")

        # Query Prometheus
        metrics = await self._query_prometheus(query, time_range)

        # Detect anomalies
        anomalies = self._detect_anomalies(metrics)

        # Correlate with incidents (if available)
        correlations = await self._correlate_with_incidents(metrics, anomalies)

        return ToolResult(
            success=True,
            data={
                "metrics": metrics,
                "anomalies": anomalies,
                "correlations": correlations,
                "insights": self._generate_insights(metrics, anomalies)
            },
            metadata={"query": query, "time_range": time_range}
        )
```

---


## 4. Prompt Engineering & Classification System

> **STATUS**: ✅ IMPLEMENTED - Phase 0 Complete (2025-10-03)
>
> This section has been consolidated into a dedicated architecture document.
> For complete technical specification, see:
> **[`docs/architecture/QUERY_CLASSIFICATION_AND_PROMPT_ENGINEERING.md`](./docs/architecture/QUERY_CLASSIFICATION_AND_PROMPT_ENGINEERING.md)**

### Quick Summary

**v3.0 Response-Format-Driven Classification:**
- 16 intent taxonomy (consolidated from 20)
- 9 ResponseType formats (added visual diagrams and comparison tables)
- 47+ weighted patterns with exclusion rules
- Multi-dimensional confidence framework
- 100% test coverage (28/28 passing)

**Prompt Engineering System:**
- PromptManager with template management
- 5-phase SRE doctrine prompts
- Few-shot examples and token optimization
- Phase-specific prompt generation

---
## 5. Context Management Strategy

### 5.1 Core Modules & Classes

#### Context Builder

**ContextBuilder** (`faultmaven/core/context/context_builder.py`)
```python
class ContextBuilder:
    """Intelligent context building with token budget management"""

    def __init__(
        self,
        memory_service: IMemoryService,
        token_budget: int = 2000,
        tokenizer: Optional[Any] = None
    ):
        self.memory_service = memory_service
        self.token_budget = token_budget
        self.tokenizer = tokenizer or self._default_tokenizer()

    async def build_context(
        self,
        session_id: str,
        query: str,
        phase: Phase
    ) -> str:
        """Build optimized context within token budget"""

        # 1. Allocate token budget
        budget_allocation = {
            "recent_turns": int(self.token_budget * 0.4),  # 40%
            "key_facts": int(self.token_budget * 0.3),     # 30%
            "user_profile": int(self.token_budget * 0.15), # 15%
            "relevant_insights": int(self.token_budget * 0.15)  # 15%
        }

        # 2. Retrieve context components
        recent_turns = await self.memory_service.retrieve_relevant_context(
            session_id, query, limit=10
        )
        key_facts = await self.memory_service.extract_facts(session_id)
        user_profile = await self.memory_service.get_user_profile(session_id)
        relevant_insights = await self._get_relevant_insights(session_id, query)

        # 3. Rank and truncate within budget
        context_parts = []

        # Recent turns (most important)
        recent_text = self._format_turns(recent_turns)
        recent_text = self._truncate_to_budget(
            recent_text,
            budget_allocation["recent_turns"]
        )
        context_parts.append(f"## Recent Conversation\n{recent_text}")

        # Key facts
        if key_facts:
            facts_text = "\n".join([f"- {fact}" for fact in key_facts])
            facts_text = self._truncate_to_budget(
                facts_text,
                budget_allocation["key_facts"]
            )
            context_parts.append(f"## Key Facts\n{facts_text}")

        # User profile (if significant)
        if user_profile and user_profile.get("skill_level") != "intermediate":
            profile_text = f"User skill level: {user_profile['skill_level']}"
            if user_profile.get("domain_expertise"):
                profile_text += f"\nExpertise: {', '.join(user_profile['domain_expertise'])}"
            context_parts.append(f"## User Profile\n{profile_text}")

        # Relevant insights
        if relevant_insights:
            insights_text = "\n".join([
                f"- {insight['summary']}"
                for insight in relevant_insights[:3]
            ])
            context_parts.append(f"## Relevant Past Insights\n{insights_text}")

        return "\n\n".join(context_parts)

    def _truncate_to_budget(self, text: str, budget: int) -> str:
        """Truncate text to fit within token budget"""
        tokens = self.tokenizer.encode(text)
        if len(tokens) <= budget:
            return text

        # Truncate and add ellipsis
        truncated_tokens = tokens[:budget-3]  # Reserve 3 for "..."
        return self.tokenizer.decode(truncated_tokens) + "..."
```

### 5.2 Key Design Decisions

#### Decision 1: Fixed vs Dynamic Context Window
**Choice:** Dynamic with token budget management
**Rationale:**
- **Flexibility**: Adapt to context availability
- **Efficiency**: Don't waste tokens on padding
- **Quality**: Prioritize most relevant context

#### Decision 2: Recency vs Relevance Weighting
**Choice:** Hybrid scoring (50% relevance, 30% recency, 20% importance)
**Rationale:**
- **Balance**: Recent context usually relevant
- **Quality**: Semantic relevance crucial
- **User Value**: Important facts shouldn't be dropped

#### Decision 3: Summarization: Always vs Threshold
**Choice:** Summarize only when exceeding budget
**Rationale:**
- **Fidelity**: Avoid information loss
- **Cost**: LLM summarization adds latency
- **Simplicity**: Simpler implementation

### 5.3 Design Principles & Strategies

#### Principle 1: Token Budget Discipline
**Strategy:**
- Fixed budget per request (default: 2000 tokens)
- Proportional allocation to context types
- Strict truncation when exceeded

#### Principle 2: Relevance-First Ordering
**Strategy:**
- Most relevant context first
- Oldest context truncated first
- Critical facts always included

#### Principle 3: Progressive Enhancement
**Strategy:**
- Start with minimal context
- Add context based on availability
- Degrade gracefully if sources unavailable

---

## 6. Integration Patterns

### 6.1 Component Communication

**Message Flow:**
```
API Request
    ↓
AgentService.process_query_for_case()
    ↓
├─ 1. QueryClassificationEngine.classify_query()
├─ 2. AgentStateManager.load_state() ──┐
├─ 3. MemoryService.retrieve_context()─┤ (Parallel)
├─ 4. ToolSkillBroker.select_tools()───┘
    ↓
├─ 5. WorkflowEngine.execute()
    ↓  ├─ 5a. Build phase-specific prompt
    ↓  ├─ 5b. Execute LLM with tools
    ↓  └─ 5c. Assess phase completion
    ↓
├─ 6. GuardrailsLayer.validate()
├─ 7. ResponseSynthesizer.synthesize()
    ↓
├─ 8. AgentStateManager.save_state() ──┐
├─ 9. MemoryService.store_turn()───────┤ (Parallel)
    ↓
AgentResponse
```

### 6.2 Error Propagation

**Pattern:**
```python
try:
    result = await component.execute()
except ComponentException as e:
    # Log with full context
    logger.error(f"Component {component.name} failed", extra={
        "error": str(e),
        "session_id": session_id,
        "phase": current_phase,
        "correlation_id": correlation_id
    })

    # Attempt fallback
    result = await fallback_strategy.execute()

    # Record degradation
    metrics.record_degradation(component.name)
```

---

## 7. Performance Optimization

### 7.1 Target Metrics

| Component | Operation | Target | P99 |
|-----------|-----------|--------|-----|
| Memory | Working memory read | <10ms | <20ms |
| Memory | Context retrieval | <50ms | <100ms |
| Classification | Pattern match | <10ms | <20ms |
| Classification | LLM classify | <200ms | <500ms |
| Tools | Log analysis | <1s | <3s |
| Workflow | Phase execution | <2s | <5s |
| Overall | End-to-end | <3s | <8s |

### 7.2 Optimization Strategies

#### Caching Strategy
```python
# 1. Memory context cache (5-minute TTL)
@cache(ttl=300)
async def retrieve_context(session_id, query):
    pass

# 2. User profile cache (1-hour TTL)
@cache(ttl=3600, key="user:{user_id}")
async def get_user_profile(user_id):
    pass

# 3. LLM response cache (semantic key)
@cache(ttl=1800, key="llm:{semantic_hash}")
async def llm_generate(prompt):
    pass
```

#### Parallel Execution
```python
# Execute independent operations concurrently
memory_task = memory_service.retrieve_context(session_id, query)
state_task = state_manager.load_state(session_id)

memory_ctx, agent_state = await asyncio.gather(
    memory_task,
    state_task
)
```

---

## 8. Security & Privacy

### 8.1 PII Protection

**Strategy:**
- All user input sanitized before storage
- All LLM inputs sanitized before sending
- Sanitization via Presidio integration

**Implementation:**
```python
async def process_query(self, request: QueryRequest):
    # Sanitize immediately
    sanitized_query = self._sanitizer.sanitize(request.query)

    # Use sanitized version throughout
    classification = await self.classify(sanitized_query)
    # ...
```

### 8.2 Data Retention

**Policies:**
- Working memory: Cleared on session end
- Session memory: 24-hour TTL
- User memory: Persistent (user can delete)
- Episodic memory: Requires user consent

---

**End of Technical Specifications Document**

This document provides comprehensive technical details for implementing the FaultMaven system. Use in conjunction with IMPLEMENTATION_PLAN.md for complete guidance.