# Client Protection and Abuse Prevention

## Overview

This document describes FaultMaven's defense mechanisms against malicious or malfunctioning clients that could overwhelm the system through excessive requests, infinite loops, or resource abuse.

## Background

**Incident Analysis**: A frontend bug caused infinite title generation requests, leading to:
- Excessive LLM API calls and costs
- Server resource exhaustion
- Poor user experience for legitimate users
- Potential system instability

**Root Cause**: Lack of server-side protections against client misbehavior.

## Defense Strategy

### Phase 1: Immediate Protection (Critical)

#### 1. Rate Limiting Middleware
**Purpose**: Prevent rapid-fire requests that can overwhelm the system.

**Scope**:
- Per-session limits: 10 requests/minute, 100 requests/hour
- Per-endpoint limits: Specific limits for high-cost operations
- Global limits: 1000 requests/minute across all clients

**Implementation**: Redis-backed sliding window rate limiter with progressive penalties.

**Configuration**:
```python
RATE_LIMITS = {
    "global": {"requests": 1000, "window": 60},
    "per_session": {"requests": 10, "window": 60},
    "per_session_hourly": {"requests": 100, "window": 3600},
    "title_generation": {"requests": 1, "window": 300},  # 5 minutes
    "agent_query": {"requests": 5, "window": 60}
}
```

#### 2. Request Deduplication
**Purpose**: Prevent processing identical requests within short time windows.

**Scope**:
- Title generation: Block duplicates for 5 minutes per session
- Agent queries: Block exact duplicates for 30 seconds per session
- Hash-based identification: SHA-256 of (session_id + endpoint + request_body)

**Cache Strategy**: Redis with TTL-based expiration.

#### 3. Agent Execution Timeouts
**Purpose**: Prevent runaway agent processes from consuming resources indefinitely.

**Timeouts**:
- Agent execution: 60 seconds maximum
- LLM calls: 30 seconds maximum
- Individual phase execution: 45 seconds maximum

**Implementation**: asyncio.timeout with graceful cleanup.

## Technical Specifications

### Rate Limiting Middleware

```python
class RateLimitMiddleware:
    """
    Multi-level rate limiting with Redis backend
    
    Features:
    - Sliding window algorithm
    - Progressive penalties (exponential backoff)
    - Multiple limit types (global, per-session, per-endpoint)
    - Graceful degradation when Redis unavailable
    """
```

**Headers Added**:
- `X-RateLimit-Limit`: Current limit
- `X-RateLimit-Remaining`: Requests remaining
- `X-RateLimit-Reset`: Window reset time

**Response Codes**:
- `429 Too Many Requests`: Rate limit exceeded
- `503 Service Unavailable`: System overloaded

### Request Deduplication

```python
class RequestDeduplicationMiddleware:
    """
    Hash-based request deduplication
    
    Features:
    - Content-based hashing (excludes timestamps)
    - Per-endpoint TTL configuration
    - Memory-efficient storage
    - Optional response caching
    """
```

**Hash Algorithm**:
```python
def generate_request_hash(session_id: str, endpoint: str, body: str) -> str:
    # Normalize body (remove timestamps, request IDs)
    normalized = normalize_request_body(body)
    content = f"{session_id}:{endpoint}:{normalized}"
    return hashlib.sha256(content.encode()).hexdigest()
```

### Agent Execution Timeouts

```python
class AgentTimeoutManager:
    """
    Timeout management for agent operations
    
    Features:
    - Hierarchical timeouts (operation < phase < total)
    - Graceful cleanup on timeout
    - Resource monitoring
    - Timeout escalation
    """
```

**Timeout Hierarchy**:
1. **LLM Call Timeout**: 30 seconds
2. **Phase Timeout**: 45 seconds  
3. **Total Agent Timeout**: 60 seconds
4. **Emergency Shutdown**: 90 seconds (force kill)

## Configuration

### Environment Variables

```bash
# Rate Limiting
RATE_LIMIT_ENABLED=true
RATE_LIMIT_REDIS_URL=redis://localhost:6379/1
RATE_LIMIT_GLOBAL_REQUESTS=1000
RATE_LIMIT_GLOBAL_WINDOW=60

# Request Deduplication  
DEDUP_ENABLED=true
DEDUP_DEFAULT_TTL=30
DEDUP_TITLE_TTL=300

# Agent Timeouts
AGENT_TIMEOUT_ENABLED=true
AGENT_TOTAL_TIMEOUT=60
AGENT_PHASE_TIMEOUT=45
AGENT_LLM_TIMEOUT=30
```

### Rate Limit Configuration

```python
# Per-endpoint rate limits (requests per minute)
ENDPOINT_RATE_LIMITS = {
    "/api/v1/agent/query": 5,
    "/api/v1/agent/troubleshoot": 5,
    "/api/v1/data/upload": 10,
    "/api/v1/sessions/": 20,
    "title_generation": 1,  # Special case: 1 per 5 minutes
}

# Progressive penalty multipliers
PENALTY_MULTIPLIERS = {
    "first_violation": 2.0,    # 2x longer wait
    "second_violation": 4.0,   # 4x longer wait  
    "third_violation": 8.0,    # 8x longer wait
    "persistent_violation": 16.0  # 16x longer wait
}
```

## Monitoring and Alerting

### Metrics Tracked

```python
PROTECTION_METRICS = {
    "rate_limit_hits": "Counter of rate limit violations",
    "duplicate_requests": "Counter of duplicate request blocks",
    "agent_timeouts": "Counter of agent execution timeouts",
    "session_suspensions": "Counter of suspended sessions",
    "protection_overhead": "Histogram of protection processing time"
}
```

### Alert Conditions

- **High Rate Limit Violations**: >100/minute (potential attack)
- **Excessive Duplicates**: >50% duplicate rate (client bug)
- **Frequent Timeouts**: >10% timeout rate (performance issue)
- **Protection Overhead**: >10ms average (performance impact)

## Error Handling

### Graceful Degradation

1. **Redis Unavailable**: Fall back to in-memory rate limiting
2. **Timeout Service Down**: Continue with warnings
3. **High System Load**: Increase rate limit strictness

### Error Responses

```json
{
  "error": "rate_limit_exceeded",
  "message": "Too many requests. Please wait 45 seconds.",
  "retry_after": 45,
  "error_code": "RL001",
  "correlation_id": "abc123"
}
```

## Testing Strategy

### Unit Tests
- Rate limiting algorithms
- Hash generation consistency
- Timeout mechanisms
- Configuration validation

### Integration Tests  
- End-to-end protection flows
- Redis integration
- Middleware interaction
- Performance impact measurement

### Load Tests
- Rate limit effectiveness under load
- Memory usage under attack simulation
- Response time impact
- Resource cleanup verification

## Security Considerations

### Attack Vectors Addressed
- **Request flooding**: Rate limiting
- **Infinite loops**: Deduplication + timeouts
- **Resource exhaustion**: Timeouts + limits
- **Session hijacking**: Per-session limits
- **Cost attacks**: LLM call limits

### Potential Bypasses
- **IP rotation**: Mitigated by session-based limits
- **Request variation**: Mitigated by normalized hashing
- **Slow attacks**: Mitigated by hourly limits
- **Distributed attacks**: Mitigated by global limits

## Implementation Order

1. **Rate Limiting Middleware** (highest impact)
2. **Request Deduplication** (prevents exact incident)  
3. **Agent Timeouts** (prevents resource exhaustion)
4. **Integration & Testing** (ensures reliability)
5. **Monitoring & Alerting** (operational visibility)

## Future Enhancements (Phase 2+)

- Machine learning-based anomaly detection
- Behavioral analysis and scoring
- Distributed rate limiting across instances
- Advanced circuit breakers
- Client reputation system