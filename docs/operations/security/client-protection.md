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
- Per-session limits: one pair of buckets for writes, one for cheap reads (below)
- Per-endpoint limits: Specific limits for high-cost operations
- Global limits: 1000 requests/minute across all clients, keyed on client address

**Implementation**: Redis-backed sliding window rate limiter. The window counts
**requests**, not time buckets — one sorted-set entry per request inside it, so a
limit of L admits exactly L requests however they distribute across seconds. See
[rate-limiting-sliding-window.md](../../architecture/security/rate-limiting-sliding-window.md)
for the algorithm and its invariants.

**Configuration** (the defaults on the canonical settings path; the production
preset is tighter — see the table below):
```python
RATE_LIMITS = {
    "global": {"requests": 1000, "window": 60},
    "per_session": {"requests": 20, "window": 60},
    "per_session_hourly": {"requests": 100, "window": 3600},
    "per_session_read": {"requests": 240, "window": 60},
    "per_session_read_hourly": {"requests": 3000, "window": 3600},
    "title_generation": {"requests": 1, "window": 300},  # 5 minutes
    "agent_query": {"requests": 5, "window": 60}
}
```

#### 1a. Reads and writes are metered separately

A session holds **two independent pairs** of per-session buckets, and every
request is charged to exactly one pair:

| Traffic | Buckets | Why |
|---------|---------|-----|
| Cheap reads — `GET` / `HEAD` / `OPTIONS` outside the list below | `per_session_read`, `per_session_read_hourly` | Ordinary SPA navigation: loading a case, its messages, its files. Cheap to serve, and issued in bursts. |
| Writes, plus the read endpoints listed below | `per_session`, `per_session_hourly` | Consumes LLM or embedding compute. This is the quota that protection exists for. |

Both pairs are bounded by `global` on top, which is keyed on the client address
rather than the session.

**Why a second pair rather than an exemption.** A blanket per-session limit of
10 requests/minute refused normal navigation (fm#994) — a case view issues well
over ten GETs. Exempting reads from the per-minute bucket alone does not fix it:
they stay charged to the shared *hourly* bucket, so a burst still exhausts the
session, now for up to an hour and for its `POST` turns as well as its reads.
Separate pairs are what makes the failure modes independent — a read flood can
only refuse reads.

**Why the exception list.** The HTTP verb is a proxy for cost, and for a few
endpoints it is the wrong one. These `GET`s each run a query embedding (BGE-M3,
behind a process-wide lock) and a vector similarity search per call, so they are
metered as writes:

- `GET /api/v1/cases/{case_id}/report-recommendations`
- `GET /api/v1/reports/recommendations/{case_id}`
- `GET /api/v1/knowledge/documents/{document_id}/snippet`

The list lives in `EXPENSIVE_READ_PATTERNS` in
`faultmaven/api/middleware/rate_limiting.py`. A hand-maintained list of
exceptions rots in the permissive direction — an endpoint added later inherits
"cheap" by saying nothing — so
`tests/unit/api/middleware/test_rate_limit_read_cost_classification.py` guards it
by **reachability rather than by inventory**. For every read route on every
mounted router it asks whether the handler can reach an embedder or vector store,
through a declared dependency or an import inside the handler body. Seven of the
sixty-one can, and only those carry a recorded verdict; the other fifty-four are
proved cheap on each run rather than listed.

What that means in practice:

- Adding an ordinary read endpoint costs nothing — no list to update.
- Adding one that touches the vector store **fails the test** until someone
  records whether it embeds. Four of the seven flagged today hold a
  `KnowledgeService` but only read rows and counts; they are recorded cheap, with
  the reason.
- A pattern that stops matching any live route fails too — a rename would
  otherwise demote an expensive endpoint to the roomy bucket in silence.
- `MOUNTS` in that test is checked against `main.py`'s own `include_router` calls
  by AST, so a router mounted there but missing from the probe is a failure
  rather than a blind spot.

**Shipped values**:

| Bucket | settings path | development | production |
|--------|--------------|-------------|------------|
| `global` | 1000 / 60s | 5000 / 60s | 500 / 60s |
| `per_session` | 20 / 60s | 50 / 60s | 10 / 60s |
| `per_session_hourly` | 100 / 3600s | 500 / 3600s | 50 / 3600s |
| `per_session_read` | 240 / 60s | 600 / 60s | 120 / 60s |
| `per_session_read_hourly` | 3000 / 3600s | 6000 / 3600s | 1200 / 3600s |

**A missing read bucket narrows, it never widens.** The limiter allows anything
it holds no configuration for, so routing reads at an unconfigured bucket would
leave them unmetered. The middleware therefore requires *both* read keys to be
present before it applies the split; with either missing it charges reads to the
write buckets — the pre-split behaviour — and logs why. Presence is the test,
not `enabled`: an operator who ships `per_session_read` with `enabled: false` has
asked for reads not to be metered separately, which is a different statement from
never having been told the bucket exists.

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

**`Retry-After` is the caller's own remaining wait**, not a flat window
duration: it counts down to the moment that client's oldest in-window request
ages out. There is no penalty escalation.

**Headers Added** (by the limiter that enforced, and by nothing else):
- `X-RateLimit-Limit`: Current limit
- `X-RateLimit-Remaining`: Requests remaining
- `X-RateLimit-Reset`: Window reset time

A response that carries none of these was not rate-limit-checked, or was
checked by a limiter with nothing to report (a disabled limit, or a check that
failed open). That absence is deliberate — no layer substitutes a default.

### Client identity: what a limit is keyed on

The `global` limit is the only one that applies to unauthenticated traffic, and
it is keyed on the client's address. That address is resolved by one shared
rule, in `faultmaven/api/middleware/client_ip.py`:

> **`X-Forwarded-For` is honoured only when the socket peer is a configured
> trusted proxy, and never otherwise. `X-Real-IP` is never honoured at all.**

`X-Real-IP` carries a single value and no chain, so nothing distinguishes what
a trusted proxy wrote from what a caller sent. It is read only as a signal that
a proxy may be present (for the warning below), never to pick the address.

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

These are read on every deployment:

```bash
# Whether the protection middleware is installed at all. Default false —
# with it unset, rate limiting, deduplication and timeouts are all absent.
BASIC_PROTECTION_ENABLED=true

# Degrade policy for rate limiting and deduplication when Redis is
# unreachable. Governs nothing else — in particular not PII redaction.
# The production preset pins this closed and does not read the key.
PROTECTION_RATE_LIMIT_FAIL_OPEN=true

# Proxies whose X-Forwarded-For may be believed when deciding which client a
# limit applies to. Addresses or CIDRs, comma-separated. Empty (the default)
# means no forwarding header is honoured and limits key on the socket peer.
# Kubernetes: set this to the pod CIDR. X-Real-IP is never believed.
PROTECTION_TRUSTED_PROXIES=
```

**The `RATE_LIMIT_*`, `DEDUP_*` and `TIMEOUT_*` keys are not operator knobs.**
`load_protection_settings` reads them only from `_load_from_environment`, which
runs when `get_settings()` itself raises — a broken validator, or very early
init. On a healthy deployment the settings path supplies the values above from
code and never consults the environment, so setting these changes nothing. They
exist so that a process which has already lost its settings still starts from
the deployment's intended numbers:

```bash
RATE_LIMITING_ENABLED=true
RATE_LIMIT_GLOBAL=1000:60                 # requests:window_seconds
RATE_LIMIT_PER_SESSION=20:60
RATE_LIMIT_PER_SESSION_HOURLY=100:3600
RATE_LIMIT_PER_SESSION_READ=240:60
RATE_LIMIT_PER_SESSION_READ_HOURLY=3000:3600
RATE_LIMIT_TITLE_GENERATION=1:300
```

A malformed value falls back to the default shown rather than to no limit.

Changing the limits a deployment actually runs on means changing the preset in
`faultmaven/config/protection.py`. Promoting these keys onto the settings path is
tracked as a TODO in `_load_from_environment`.

### Per-endpoint rate limits

`RateLimitMiddleware.endpoint_configs` reserves a hook for limits attached to a
single path. **No endpoint currently uses it** — the two entries present declare
limit types that the dispatch path does not read, and neither carries a
`special_handling` callable. The `title_generation` and `agent_query` entries in
`RATE_LIMITS` are likewise configured but never checked — no code path calls
`check_rate_limit` with either type. Per-session cost control is done by the
read/write split above, not here.

There is no progressive penalty ladder. A refused client is told how long its
own window actually takes to free quota, and nothing longer: repeat offenders
are not punished with escalating waits.

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
