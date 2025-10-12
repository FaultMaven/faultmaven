# Session Management Specification
## Multi-Session Architecture and State Persistence

**Document Type:** Component Specification
**Version:** 1.0
**Last Updated:** 2025-10-11
**Status:** ✅ **IMPLEMENTED** (v3.2.0)

## Implementation Status

**Implementation Date:** 2025-10-11 (OODA integration)
**Implementation Version:** v3.2.0
**Status:** Redis-backed with OODA investigation state

**Implementation Summary:**
- ✅ Redis-backed session storage with TTL expiration
- ✅ InvestigationState persistence per session
- ✅ Session-scoped investigation isolation
- ✅ StateManager methods for investigation state CRUD
- ✅ Multi-session support (multiple concurrent investigations)

**Implementation Files:**
- State Manager: `faultmaven/services/agentic/management/state_manager.py`
- OODA Integration: `faultmaven/services/agentic/orchestration/ooda_integration.py`
- Models: `faultmaven/models/investigation.py`

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Session Architecture](#session-architecture)
3. [State Management](#state-management)
4. [Redis Storage Design](#redis-storage-design)
5. [Session Lifecycle](#session-lifecycle)
6. [Multi-Session Support](#multi-session-support)
7. [Implementation Details](#implementation-details)

---

## System Overview

### Purpose

FaultMaven's session management system provides:

1. **Persistent Investigation State** - Redis-backed storage for OODA InvestigationState
2. **Multi-Session Support** - Users can have multiple concurrent investigations
3. **Session Isolation** - Each investigation operates independently
4. **Automatic Cleanup** - TTL-based session expiration
5. **State Recovery** - Resume investigations after disconnect/reconnect

### Key Concepts

**Session**: A unique investigation context with:
- Unique session_id
- Associated InvestigationState
- Case context and messages
- TTL-based expiration

**Investigation State**: Complete OODA framework state including:
- Current phase and OODA iteration
- Hypotheses and confidence levels
- Evidence requests and collected evidence
- Hierarchical memory (hot/warm/cold/persistent)
- Anomaly frame and problem confirmation

---

## Session Architecture

### Session Identity

```python
session_id: str  # Unique identifier (UUID)
```

**Properties:**
- Globally unique across all users
- Generated client-side or server-side
- Persistent for investigation duration
- Maps to single InvestigationState

### Session Scope

Each session contains:
- **InvestigationState**: Full OODA framework state
- **Case**: Associated case object with messages
- **User Context**: User ID and authentication
- **Metadata**: Created timestamp, last updated, TTL

---

## State Management

### StateManager Interface

```python
class StateManager:
    """Manages investigation state persistence"""

    async def get_investigation_state(
        self,
        session_id: str
    ) -> Optional[InvestigationState]:
        """Retrieve investigation state for session"""

    async def update_investigation_state(
        self,
        session_id: str,
        state: InvestigationState,
        ttl: int = None
    ) -> bool:
        """Persist investigation state for session"""

    async def initialize_investigation_session(
        self,
        session_id: str,
        user_id: str = None,
        llm_provider = None
    ) -> bool:
        """Initialize new investigation session"""

    async def delete_investigation_state(
        self,
        session_id: str
    ) -> bool:
        """Remove investigation state (cleanup)"""
```

### State Persistence Flow

```
User Query
    │
    ▼
PhaseOrchestrator.process_turn()
    │
    ├─> StateManager.get_investigation_state(session_id)
    │   └─> Redis GET inv:state:{session_id}
    │
    ├─> Phase Handler Execution
    │   └─> Updates InvestigationState
    │
    └─> StateManager.update_investigation_state(session_id, state)
        └─> Redis SET inv:state:{session_id} EX {ttl}
```

---

## Redis Storage Design

### Key Structure

**Investigation State:**
```
Key: inv:state:{session_id}
Value: JSON-serialized InvestigationState
TTL: 3600 seconds (1 hour default)
```

**Session Metadata:**
```
Key: session:{session_id}:meta
Value: {
    "created_at": "2025-10-11T10:00:00Z",
    "last_updated": "2025-10-11T10:30:00Z",
    "user_id": "user_123",
    "case_id": "case_456"
}
TTL: 3600 seconds
```

### Data Serialization

**InvestigationState → Redis:**
```python
# Serialize
state_json = investigation_state.json()
redis.set(f"inv:state:{session_id}", state_json, ex=3600)

# Deserialize
state_json = redis.get(f"inv:state:{session_id}")
investigation_state = InvestigationState.parse_raw(state_json)
```

**Benefits:**
- Type-safe with Pydantic models
- Automatic validation on deserialization
- JSON format for debugging/inspection

### Storage Optimization

**Hierarchical Memory Compression:**
- Hot memory: ~500 tokens (last 2 iterations)
- Warm memory: ~300 tokens (LLM-summarized)
- Cold memory: ~100 tokens (key facts only)
- Persistent: ~100 tokens (never compressed)
- **Total: ~1,600 tokens** (64% reduction from 4,500+)

This compression happens in-memory before Redis storage, reducing:
- Storage costs
- Network transfer time
- Deserialization overhead

---

## Session Lifecycle

### 1. Session Creation

```python
# Client initiates investigation
session_id = str(uuid.uuid4())  # Generate unique ID

# Server initializes session
await state_manager.initialize_investigation_session(
    session_id=session_id,
    user_id=request.user_id
)

# Creates InvestigationState in Phase 0 (Intake)
```

### 2. Active Investigation

```python
# Each turn
for turn in investigation:
    # Retrieve state
    state = await state_manager.get_investigation_state(session_id)

    # Process turn
    response, updated_state = await orchestrator.process_turn(
        user_query=query,
        investigation_state=state
    )

    # Persist updated state
    await state_manager.update_investigation_state(
        session_id=session_id,
        state=updated_state,
        ttl=3600  # Reset TTL on each update
    )
```

### 3. Session Expiration

**TTL-Based:**
- Default: 3600 seconds (1 hour)
- Reset on each update
- Automatic Redis cleanup when TTL expires

**Manual Closure:**
```python
# User explicitly closes investigation
await state_manager.delete_investigation_state(session_id)
```

### 4. Session Recovery

```python
# User reconnects with existing session_id
state = await state_manager.get_investigation_state(session_id)

if state:
    # Resume investigation from last state
    summary = orchestrator.get_investigation_summary(state)
    return f"Resuming investigation: {summary}"
else:
    # Session expired - start new investigation
    return "Session expired. Starting new investigation."
```

---

## Multi-Session Support

### Concurrent Investigations

Users can have multiple active investigations:

```
User: user_123
├── Session: session_abc (API errors investigation)
│   └── Phase 3 (Hypothesis), 2 hypotheses active
├── Session: session_def (Database slowness investigation)
│   └── Phase 4 (Validation), testing connection pool hypothesis
└── Session: session_ghi (Recent deployment issue)
    └── Phase 1 (Blast Radius), scoping impact
```

### Session Isolation

Each session has:
- Independent InvestigationState
- Separate OODA iterations
- Isolated hypotheses and evidence
- Own memory hierarchy

**No cross-session contamination.**

### Session Switching

```python
# User switches between investigations
current_session_id = request.session_id

# Retrieve appropriate state
state = await state_manager.get_investigation_state(current_session_id)

# Continue investigation from current phase
response = await orchestrator.process_turn(
    user_query=query,
    investigation_state=state
)
```

---

## Implementation Details

### StateManager Methods (Implemented)

#### get_investigation_state()

```python
async def get_investigation_state(
    self,
    session_id: str
) -> Optional[InvestigationState]:
    """Retrieve investigation state from Redis

    Args:
        session_id: Session identifier

    Returns:
        InvestigationState or None if not found/expired
    """
    key = f"inv:state:{session_id}"
    state_json = await self.redis.get(key)

    if not state_json:
        return None

    try:
        return InvestigationState.parse_raw(state_json)
    except Exception as e:
        logger.error(f"Failed to deserialize state: {e}")
        return None
```

#### update_investigation_state()

```python
async def update_investigation_state(
    self,
    session_id: str,
    state: InvestigationState,
    ttl: int = 3600
) -> bool:
    """Persist investigation state to Redis

    Args:
        session_id: Session identifier
        state: InvestigationState to persist
        ttl: Time-to-live in seconds (default 1 hour)

    Returns:
        True if successful
    """
    key = f"inv:state:{session_id}"
    state_json = state.json()

    try:
        await self.redis.set(key, state_json, ex=ttl)
        return True
    except Exception as e:
        logger.error(f"Failed to persist state: {e}")
        return False
```

#### initialize_investigation_session()

```python
async def initialize_investigation_session(
    self,
    session_id: str,
    user_id: str = None,
    llm_provider = None
) -> bool:
    """Initialize new investigation session

    Creates initial InvestigationState in Phase 0 (Intake)

    Args:
        session_id: Unique session identifier
        user_id: Optional user identifier
        llm_provider: Optional LLM provider for orchestrator

    Returns:
        True if successful
    """
    orchestrator = PhaseOrchestrator(
        llm_provider=llm_provider or self.default_llm,
        session_id=session_id
    )

    # Create initial state
    investigation_state = await orchestrator.initialize_investigation(
        user_query="",  # Empty query for initialization
        case_diagnostic_state=None
    )

    # Persist to Redis
    return await self.update_investigation_state(
        session_id=session_id,
        state=investigation_state,
        ttl=3600
    )
```

### Integration with OODA Framework

**ooda_integration.py:**

```python
async def process_turn_with_ooda(
    user_query: str,
    case: Case,
    llm_client: ILLMProvider,
    session_id: str,
    state_manager: Optional[StateManager] = None,
) -> Tuple[StructuredLLMResponse, CaseDiagnosticState]:
    """Process turn with OODA framework

    Automatically handles:
    - State retrieval from Redis
    - OODA turn processing
    - State persistence back to Redis
    """
    # Get or initialize investigation state
    investigation_state = await _get_or_initialize_investigation_state(
        case=case,
        session_id=session_id,
        user_query=user_query,
        llm_client=llm_client,
        state_manager=state_manager,
    )

    # Process turn
    orchestrator = PhaseOrchestrator(
        llm_provider=llm_client,
        session_id=session_id,
    )

    response_text, updated_state = await orchestrator.process_turn(
        user_query=user_query,
        investigation_state=investigation_state,
        conversation_history=conversation_history,
    )

    # Persist updated state
    await state_manager.update_investigation_state(
        session_id=session_id,
        state=updated_state,
    )

    return structured_response, updated_diagnostic_state
```

---

## Configuration

### Redis Settings

```python
# From settings.py
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 0
REDIS_PASSWORD = None

# TTL Configuration
SESSION_TTL_SECONDS = 3600  # 1 hour
INVESTIGATION_STATE_TTL = 3600  # 1 hour
```

### TTL Strategy

**Short-lived sessions** (1 hour) because:
- Most investigations complete within 30-60 minutes
- Reduces Redis memory usage
- Encourages timely investigation completion
- Users can always start new session if needed

**Adjustable per environment:**
- Development: 1 hour (fast iteration)
- Staging: 2 hours (longer testing sessions)
- Production: 1 hour (optimal balance)

---

## Future Enhancements

### Session Persistence to Database

**Current:** Redis only (ephemeral)
**Future:** Persist completed investigations to PostgreSQL

```sql
CREATE TABLE investigations (
    investigation_id UUID PRIMARY KEY,
    session_id VARCHAR(255) UNIQUE,
    user_id VARCHAR(255),
    case_id VARCHAR(255),
    final_state JSONB,
    created_at TIMESTAMP,
    completed_at TIMESTAMP,
    outcome VARCHAR(50)  -- resolved, abandoned, stalled
);
```

**Benefits:**
- Long-term storage
- Investigation analytics
- Resume very old sessions
- Audit trail

### Session Clustering

Support for multiple Redis instances:
- Read replicas for scale
- Cluster mode for high availability
- Geo-distributed sessions

### Session Migration

Move sessions between servers:
- Load balancing
- Server maintenance
- Disaster recovery

---

## Security Considerations

### Session Security

1. **Session ID randomness**: UUID4 ensures unpredictability
2. **User isolation**: Sessions scoped to user_id
3. **TTL limits**: Automatic cleanup prevents indefinite storage
4. **No sensitive data in keys**: Only session_id in Redis keys

### Access Control

```python
async def get_investigation_state(
    self,
    session_id: str,
    user_id: str  # Required for authorization
) -> Optional[InvestigationState]:
    """Retrieve state only if user owns session"""

    # Verify ownership
    session_meta = await self.redis.get(f"session:{session_id}:meta")
    if session_meta["user_id"] != user_id:
        raise UnauthorizedError("Session does not belong to user")

    # Retrieve state
    return await self._get_state_from_redis(session_id)
```

---

## Summary

FaultMaven's session management system provides:

✅ **Redis-backed persistence** for OODA InvestigationState
✅ **Multi-session support** for concurrent investigations
✅ **Session isolation** preventing cross-contamination
✅ **Automatic cleanup** via TTL expiration
✅ **State recovery** for resuming investigations
✅ **Memory optimization** through hierarchical compression
✅ **Type safety** with Pydantic serialization

**Implementation Status:** Complete and integrated with OODA framework (v3.2.0)

**Key Files:**
- `faultmaven/services/agentic/management/state_manager.py`
- `faultmaven/services/agentic/orchestration/ooda_integration.py`
- `faultmaven/models/investigation.py`

---

## Client-Based Session Resumption (Multi-Device Support)

**Added from SESSION_MANAGEMENT_SPEC.md**

### Architecture: Multi-Session Per User

**Current Implementation:**
- Multiple concurrent sessions per user (one per client/device)
- Session resumption across browser restarts using persistent `client_id`
- Multi-device support with independent sessions per device
- Multi-tab sharing using same `client_id` within browser instance

### Client-Device Mapping

```
(user_id, client_id) → session_id → InvestigationState
```

**Redis Index Structure:**
```
Key: client:session:{user_id}:{client_id}
Value: session_id
TTL: 24 hours
```

### Enhanced SessionService Methods

#### create_session() with Client Resumption

```python
async def create_session(
    self,
    request: SessionCreateRequest,
    user_id: Optional[str] = None
) -> SessionResponse:
    """Create new session or resume existing session based on client_id.
    
    Features:
    - If client_id provided: Resume existing session for (user_id, client_id)
    - If no client_id: Create completely new session
    - Multi-session support: Multiple concurrent sessions per user
    - Session resumption: Same client can resume across browser restarts
    
    Returns:
        SessionResponse with session_resumed flag
    """
    if request.client_id:
        # Check for existing session
        existing_session_id = await self.store.get_session_by_client(
            user_id=user_id,
            client_id=request.client_id
        )
        
        if existing_session_id:
            # Resume existing session
            session = await self.store.get_session(existing_session_id)
            if session and session.is_active():
                return SessionResponse(
                    session_id=session.session_id,
                    session_resumed=True,
                    message="Session resumed successfully"
                )
    
    # Create new session
    session_id = str(uuid.uuid4())
    
    # Store client mapping if client_id provided
    if request.client_id:
        await self.store.store_client_session_mapping(
            user_id=user_id,
            client_id=request.client_id,
            session_id=session_id
        )
    
    return SessionResponse(
        session_id=session_id,
        session_resumed=False,
        message="New session created"
    )
```

### Enhanced ISessionStore Interface

```python
class ISessionStore(ABC):
    """Session storage interface with client-based indexing"""
    
    @abstractmethod
    async def store_client_session_mapping(
        self,
        user_id: str,
        client_id: str,
        session_id: str
    ) -> bool:
        """Store (user_id, client_id) → session_id mapping"""
    
    @abstractmethod
    async def get_session_by_client(
        self,
        user_id: str,
        client_id: str
    ) -> Optional[str]:
        """Retrieve session_id for (user_id, client_id)"""
    
    @abstractmethod
    async def cleanup_client_session_mapping(
        self,
        session_id: str
    ) -> bool:
        """Remove client mapping when session expires"""
```

### SessionCreateRequest Enhancement

```python
class SessionCreateRequest(BaseModel):
    """Enhanced session creation request"""
    client_id: Optional[str] = Field(
        None,
        description="Optional client/device identifier for session resumption"
    )
    # ... other fields
```

### SessionResponse Enhancement

```python
class SessionResponse(BaseModel):
    """Enhanced session response"""
    session_id: str
    session_resumed: bool = Field(
        default=False,
        description="True if existing session was resumed"
    )
    message: str = Field(
        default="Session created",
        description="Human-readable status message"
    )
```

---

## Background Cleanup Scheduler

**Added from SESSION_MANAGEMENT_SPEC.md**

### Automated Session Cleanup

#### cleanup_inactive_sessions()

```python
async def cleanup_inactive_sessions(
    self,
    max_age_minutes: Optional[int] = None
) -> int:
    """Clean up sessions that have exceeded their TTL.
    
    Multi-Session Implementation:
    - Handles multiple sessions per user concurrently
    - Cleans up client-session mappings atomically
    - Preserves active sessions while removing expired ones
    - Maintains (user_id, client_id) → session_id index integrity
    
    Args:
        max_age_minutes: Maximum session age (default: SESSION_TIMEOUT_MINUTES)
        
    Returns:
        Number of sessions successfully cleaned up
    """
    if max_age_minutes is None:
        max_age_minutes = self.config.SESSION_TIMEOUT_MINUTES
    
    cutoff_time = datetime.utcnow() - timedelta(minutes=max_age_minutes)
    
    # Get all sessions
    all_sessions = await self.store.list_all_sessions()
    
    cleaned_count = 0
    for session in all_sessions:
        if session.last_updated < cutoff_time:
            # Clean up investigation state
            await self.delete_investigation_state(session.session_id)
            
            # Clean up client mapping
            await self.store.cleanup_client_session_mapping(session.session_id)
            
            # Delete session
            await self.store.delete_session(session.session_id)
            
            cleaned_count += 1
    
    logger.info(f"Cleaned up {cleaned_count} expired sessions")
    return cleaned_count
```

#### Background Scheduler

```python
async def start_cleanup_scheduler(
    self,
    interval_minutes: int = 15
) -> None:
    """Start background task for periodic session cleanup.
    
    Args:
        interval_minutes: Cleanup interval (default: 15 minutes)
    """
    async def cleanup_loop():
        while self.scheduler_running:
            try:
                cleaned = await self.cleanup_inactive_sessions()
                logger.info(f"Cleanup cycle: {cleaned} sessions removed")
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
            
            await asyncio.sleep(interval_minutes * 60)
    
    self.scheduler_running = True
    self.scheduler_task = asyncio.create_task(cleanup_loop())
    logger.info(f"Cleanup scheduler started (interval: {interval_minutes}m)")

async def stop_cleanup_scheduler(self) -> None:
    """Stop the background cleanup scheduler gracefully."""
    self.scheduler_running = False
    if self.scheduler_task:
        self.scheduler_task.cancel()
        try:
            await self.scheduler_task
        except asyncio.CancelledError:
            pass
    logger.info("Cleanup scheduler stopped")
```

### Session Metrics

```python
def get_session_metrics(self) -> Dict[str, Union[int, float]]:
    """Get comprehensive session metrics for monitoring.
    
    Returns:
        - active_sessions: Current active session count
        - expired_sessions: Sessions awaiting cleanup
        - cleanup_runs: Total cleanup operations performed
        - last_cleanup_time: Timestamp of last cleanup
        - average_session_duration: Average session lifetime
        - memory_usage_mb: Estimated memory usage
    """
    return {
        "active_sessions": len(self.active_sessions),
        "expired_sessions": len(self.expired_sessions),
        "cleanup_runs": self.cleanup_counter,
        "last_cleanup_time": self.last_cleanup.isoformat(),
        "average_session_duration": self.avg_duration_minutes,
        "memory_usage_mb": self.estimate_memory_usage()
    }
```

---

## Configuration

### Session Management Configuration

```python
# Multi-session configuration
SESSION_TIMEOUT_MINUTES = int(os.getenv("SESSION_TIMEOUT_MINUTES", "60"))
SESSION_CLEANUP_INTERVAL_MINUTES = int(os.getenv("SESSION_CLEANUP_INTERVAL_MINUTES", "15"))
SESSION_MAX_MEMORY_MB = int(os.getenv("SESSION_MAX_MEMORY_MB", "100"))
SESSION_CLEANUP_BATCH_SIZE = int(os.getenv("SESSION_CLEANUP_BATCH_SIZE", "50"))

# Client-based session management
ENABLE_CLIENT_SESSION_RESUMPTION = bool(os.getenv("ENABLE_CLIENT_SESSION_RESUMPTION", "true"))
MAX_SESSIONS_PER_USER = int(os.getenv("MAX_SESSIONS_PER_USER", "10"))
CLIENT_ID_TTL_HOURS = int(os.getenv("CLIENT_ID_TTL_HOURS", "24"))
```

---

## Health Check Integration

### Enhanced Health Endpoint

```python
@app.get("/health")
async def health_check():
    """Enhanced health check including session metrics."""
    session_metrics = session_service.get_session_metrics()
    
    return {
        "status": "healthy",
        "services": {
            "session_manager": {
                "status": "healthy" if session_metrics["active_sessions"] < 1000 else "degraded",
                "metrics": session_metrics
            }
        }
    }
```

---

## Monitoring and Alerting

### Key Metrics

- Session cleanup duration and success rate
- Memory usage trends
- Active session count over time
- Cleanup scheduler health and uptime
- Client-based resumption success rate

### Alert Conditions

- Cleanup failures > 5% of operations
- Session memory usage > 100MB
- Active sessions > 1000 concurrent
- Cleanup scheduler down > 5 minutes
- Client mapping inconsistencies detected

---

**Merged from:** `docs/specifications/SESSION_MANAGEMENT_SPEC.md`
**Merge Date:** 2025-10-11
**Status:** Combined OODA investigation state management with client-based multi-session support
