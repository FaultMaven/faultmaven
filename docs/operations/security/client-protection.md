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

**Implementation**: Redis-backed sliding window rate limiter. The window counts
**requests**, not time buckets — one sorted-set entry per request inside it, so a
limit of L admits exactly L requests however they distribute across seconds. See
[rate-limiting-sliding-window.md](../../architecture/security/rate-limiting-sliding-window.md)
for the algorithm and its invariants.

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
    - Sliding window algorithm (counts requests, not seconds)
    - Multiple limit types (global, per-session, per-endpoint)
    - Graceful degradation when Redis unavailable
    """
```

**`Retry-After` is currently a flat window duration.** The penalty multipliers
below are computed but never escalate — the violation counter they key off is
read before its `INCR` has resolved, so every refusal takes the multiplier for a
first violation. Escalation is tracked in #926.

**Headers Added**:
- `X-RateLimit-Limit`: Current limit
- `X-RateLimit-Remaining`: Requests remaining
- `X-RateLimit-Reset`: Window reset time

### Client identity: what a limit is keyed on

The `global` limit is the only one that applies to unauthenticated traffic, and
it is keyed on the client's address. That address is resolved by one shared
rule, in `faultmaven/api/middleware/client_ip.py`:

> **`X-Forwarded-For` and `X-Real-IP` are honoured only when the socket peer is
> a configured trusted proxy, and never otherwise.**

Both halves matter, and getting either wrong breaks the limit:

| Policy | Failure |
|--------|---------|
| Always trust the headers | The limited party picks its own key. Rotating the header draws a fresh quota per request, so there is no limit at all. |
| Never trust the headers | Behind an ingress every client resolves to the ingress address and shares one bucket, so one caller exhausts everybody's quota. |

When the peer is trusted, the `X-Forwarded-For` chain is walked **from the
right** and the first hop that is not itself a trusted proxy wins. The
right-hand end was appended by infrastructure we trust; the left-hand end is
whatever the caller sent. That is what makes a forged prefix inert — a caller
sending `X-Forwarded-For: 1.2.3.4` is still keyed on the address the ingress
appended. Hops that do not parse as an address are skipped rather than used, so
attacker-supplied text cannot enter a Redis key.

Configure it with `PROTECTION_TRUSTED_PROXIES` (see below). **The default is
empty**, which means no header is believed and every limit keys on the socket
peer. That is correct for a standalone install with no proxy in front of it,
and it is the safe direction for everything else: the worst case is a limit
that is too coarse, never one that can be evaded.

Entries are parsed **strictly**: write the network address (`10.244.0.0/16`),
not a host inside it (`10.244.226.134/16`). The lenient parse would round the
second form outward to the first and trust 65,536 addresses on a typo, so a
malformed entry is dropped with an ERROR instead — a mistake must narrow trust,
never widen it. A bare address (`10.42.0.7`) is accepted and means that host
alone.

**Kubernetes deployments must set this.** Until they do, all external traffic
shares one `global` bucket, and one caller crossing the limit refuses traffic
for everyone. It is not silent, and it is reported twice: the production preset
warns at startup when the value is empty, and the resolver warns at request
time (throttled to one every 5 minutes) whenever forwarding headers arrive from
an unlisted address.

In practice the value is the **cluster pod CIDR**, not the two current ingress
controller pod IPs — those change on every reschedule, and a stale list
silently collapses all traffic onto one bucket again, which is the failure this
setting exists to prevent. The residual is worth naming rather than hiding:
trusting the pod CIDR makes *any pod that can reach the API* able to set its own
forwarding header, so an in-cluster caller can still rotate its own `global`
key. That is bounded by the `allow-api-ingress` NetworkPolicy, which already
restricts who can connect to the ingress controller, first-party FaultMaven
pods, the Slack agent and monitoring — and the dashboard genuinely does proxy
`/api`, so trusting it is correct rather than merely tolerated. This setting
defends against the open internet, not against a compromised in-cluster
workload; that boundary is the NetworkPolicy's job.

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

# Proxies whose X-Forwarded-For / X-Real-IP may be believed when deciding
# which client a limit applies to. Addresses or CIDRs, comma-separated.
# Empty (the default) means no forwarding header is honoured and limits key
# on the socket peer. Kubernetes: set this to the ingress pod range.
PROTECTION_TRUSTED_PROXIES=

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
    "/api/v1/cases/{case_id}/queries": 5,
    "/api/v1/data/upload": 10,
    "/api/v1/sessions/": 20,
    "title_generation": 1,  # Special case: 1 per 5 minutes
}

# Progressive penalty multipliers. Configured but not yet reached: the
# violation counter is always read as 1, so the effective multiplier is 1.0
# and `Retry-After` is the window duration plus jitter (#926).
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

1. **Redis Unavailable**: the limiter walks a ladder — the shared application
   Redis client, then a client built by the central factory, then (only when
   `PROTECTION_RATE_LIMIT_FAIL_OPEN=true`, the default) an in-process FakeRedis
   stand-in, which still enforces every limit but **per replica**, so the
   effective ceiling is the configured limit times the replica count. There is
   no separate in-memory limiter. With `PROTECTION_RATE_LIMIT_FAIL_OPEN=false`
   the limiter refuses the stand-in and requests are answered `503` instead
   (liveness and readiness probes are exempt — a 503'd probe kills the pod).
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
