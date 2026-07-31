# Rate Limiting — Sliding Window Semantics

**Status:** Current
**Component:** `faultmaven/infrastructure/protection/rate_limiter.py` (`RedisRateLimiter._check_redis_rate_limit`)
**Issue:** fm#920

## What the window counts

The sliding window counts **requests**, not time buckets. Each allowed request
inserts one element into a Redis sorted set:

- **score** — the request's arrival time as a float (`time.time()`, sub-second
  precision). Scores are what the window is pruned by, and the score is the
  only record of when an entry arrived; `ZRANGE … WITHSCORES` displays it.
- **member** — a `uuid4` hex, and nothing else. Uniqueness is the member's
  whole job: it is what makes ZADD grow the set by one per request instead of
  updating an existing element. Uniqueness comes solely from the uuid, so the
  set stays correct even if the clock stands still, and the member carries no
  second encoding of the arrival time that could drift from the score.

Both values are computed in Python and passed to the Lua script as arguments.
The script stays deterministic: it never generates time or randomness itself.
The script is registered once per adopted Redis client (`register_script`), so
requests carry an EVALSHA rather than the script body, and redis-py reloads the
body only if the server answers NOSCRIPT.

Check algorithm (atomic, one Lua script):

1. **Prune.** Every entry whose score is less than or equal to `now − window`
   is removed. The bound is *inclusive*, so an entry scored exactly one window
   ago is outside the window: the window is the half-open interval
   `(now − window, now]`.
2. **Count** the entries still in the set — the number of requests inside the
   window.
3. If `count >= limit`: **blocked**; nothing is inserted (a blocked request
   must not consume quota or extend the window).
4. Otherwise insert the new member with the new score, refresh the key TTL
   (`window + 60`), and allow.

The invariant that fm#920 restored: after N back-to-back requests against a
limit of L, exactly `min(N, L)` are allowed and the set holds `min(N, L)`
entries — regardless of how the requests distribute across wall-clock seconds.

## Memory bound

A window key holds at most `limit` entries, never `window` entries: the set
grows only on allowed requests, and step 3 refuses without inserting once the
limit is reached. Each entry costs a 32-character member plus a float score,
which with sorted-set overhead lands around 90–120 bytes, so a saturated key
costs roughly `limit × 100` bytes — of the order of 50 KB for production's
`global` limit of 500. The key's TTL is `window + 60`, so a key stops costing
anything a little over a minute after its last request.

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
errors, and production was pinned to `fail_open_on_redis_error=False`.

A counting window is necessary for that premise but not sufficient: the count
is only meaningful if the *key* is sound. The `global` limit is keyed on the
client address, and that address was read from `X-Forwarded-For` with no
trusted-proxy check — so a caller could rotate the header and draw a fresh
window per request no matter how the window counted (fm#927). Both defects are
now fixed; see [Client identity](../../operations/security/client-protection.md#client-identity-what-a-limit-is-keyed-on).

The production pin nonetheless stays, as a posture decision rather than an
outstanding precondition — the reasoning is recorded on
`get_production_protection_settings`.

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

`get_rate_limit_status` (the status path, which backs the `X-RateLimit-*`
headers) derives its bound from the same shared helper as enforcement, so the
two cannot disagree on where the window begins. It is strictly read-only: a
single `ZCOUNT` from the *exclusive* form of that bound to `+inf`. Exclusive is
the exact complement of enforcement's inclusive prune — the prune removes
scores at or below the bound, so the entries it would leave are those strictly
above it. Counting rather than pruning-then-counting means reporting status
never mutates a window a concurrent check is deciding against, and the call is
safe to serve from a read replica.

## Test obligations

The pre-fix suite could not see this bug: every enforcement test used
`requests=1` — the only value where per-request and per-second counting are
indistinguishable — and every must-not-limit test used `10_000`. The guard
tests therefore must:

- use limits strictly between 1 and the window (`1 < requests < window`),
- drive more than `requests` calls at one **frozen** instant, and assert the
  exact allowed/blocked split and the final entry count — frozen rather than
  merely fast, so a stalled machine cannot let the sweep straddle a second
  boundary and weaken the property it is asserting,
- sweep several limit values, not one instance
  (`tests/unit/infrastructure/test_rate_limiter_sliding_window.py`),
- pin window *sliding*: with a mocked clock, quota consumed at t₀ is
  released once t₀ falls out of the window,
- pin which side of the edge the bound falls on: an entry scored exactly
  `window` ago is outside the window, in both the enforcement and the status
  path,
- pin the middleware path end-to-end with `1 < global_requests`:
  request k ≤ limit → 200, request k > limit → 429
  (`tests/unit/api/middleware/test_redis_middleware_wiring.py`).

Mutation checks (each must turn at least one test red): reverting the member
to the integer second; skipping the `ZREMRANGEBYSCORE` prune; inserting on
the blocked path; making the prune bound exclusive.
