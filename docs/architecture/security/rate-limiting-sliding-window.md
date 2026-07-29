# Rate Limiting — Sliding Window Semantics

**Status:** Current
**Component:** `faultmaven/infrastructure/protection/rate_limiter.py` (`RedisRateLimiter._check_redis_rate_limit`)
**Issue:** fm#920

## What the window counts

The sliding window counts **requests**, not time buckets. Each allowed request
inserts one element into a Redis sorted set:

- **score** — the request's arrival time as a float (`time.time()`, sub-second
  precision). Scores are what the window is pruned by.
- **member** — a string unique per request: the float timestamp joined with a
  `uuid4` hex (`"{now:.6f}:{uuid}"`). Uniqueness is what makes ZADD grow the
  set by one per request; the timestamp prefix exists only so a human reading
  `ZRANGE` output can see when each entry arrived.

Both values are computed in Python and passed to the Lua script as arguments.
The script stays deterministic: it never generates time or randomness itself.

Check algorithm (atomic, one Lua script):

1. `ZREMRANGEBYSCORE key -inf (now - window)` — drop entries older than the
   window.
2. `ZCARD` — the number of requests still inside the window.
3. If `count >= limit`: **blocked**; nothing is inserted (a blocked request
   must not consume quota or extend the window).
4. Otherwise insert the new member with the new score, refresh the key TTL
   (`window + 60`), and allow.

The invariant that fm#920 restored: after N back-to-back requests against a
limit of L, exactly `min(N, L)` are allowed and `ZCARD == min(N, L)` —
regardless of how the requests distribute across wall-clock seconds.

## The defect this replaced

The previous implementation used `int(time.time())` as **both score and
member**. ZADD on an existing member updates its score instead of inserting,
so all requests within the same wall-clock second collapsed into one element
and `ZCARD` could never exceed the number of distinct seconds in the window.
Consequences:

- Any limit with `requests > window` (production `global` 500/60s,
  development 5000/60s, `_load_from_settings` 1000/60s) was unreachable —
  measured: 5000 requests from one IP, 0 blocked. `global` is the only limit
  covering unauthenticated traffic, so a single-IP flood was never limited.
- Limits with `requests < window` tripped on distinct *seconds*, so every
  limit also permitted an unbounded requests-per-second burst.

This falsified the premise behind defaulting production to fail-open on Redis
errors; PR #909 pinned production `fail_open_on_redis_error=False` until this
fix landed (fm#922 tracks revisiting the pin).

**Rejected alternative:** generating the unique member inside Lua via
`redis.call('TIME')` — non-deterministic commands inside scripts complicate
replication semantics and FakeRedis Lua parity, for no gain over passing the
values in as arguments.

## Time source

Scores use wall-clock `time.time()`, not `time.monotonic()`: entries are
shared across processes and replicas through Redis, so scores must be
comparable across hosts. NTP-scale skew moves the window edge by the skew
amount — acceptable for rate limiting; per-request uniqueness never depends
on the clock (the uuid provides it even if time stands still).

`get_rate_limit_status` (the read-only status path) prunes with the same
float `now - window` bound so status and enforcement agree on the window
edge.

## Test obligations

The pre-fix suite could not see this bug: every enforcement test used
`requests=1` — the only value where per-request and per-second counting are
indistinguishable — and every must-not-limit test used `10_000`. The guard
tests therefore must:

- use limits strictly between 1 and the window (`1 < requests < window`),
- drive more than `requests` calls **without sleeping** (same wall-clock
  second), and assert the exact allowed/blocked split and final `ZCARD`,
- sweep several limit values, not one instance
  (`tests/unit/infrastructure/test_rate_limiter_sliding_window.py`),
- pin window *sliding*: with a mocked clock, quota consumed at t₀ is
  released once t₀ falls out of the window,
- pin the middleware path end-to-end with `1 < global_requests`:
  request k ≤ limit → 200, request k > limit → 429
  (`tests/unit/api/middleware/test_redis_middleware_wiring.py`).

Mutation checks (each must turn at least one test red): reverting the member
to the integer second; skipping the `ZREMRANGEBYSCORE` prune; inserting on
the blocked path.
