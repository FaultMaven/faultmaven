# Rate Limiting — Sliding Window Semantics

**Status:** Current
**Component:** `faultmaven/infrastructure/protection/rate_limiter.py` (`RedisRateLimiter.check_rate_limits`)
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

Check algorithm (atomic, one Lua script). A request is usually subject to
several windows at once — an address-keyed `global` limit plus a session-keyed
per-minute and hourly pair — and **all of them are decided in one script call**:

1. **Pass one, per window.** Prune every entry whose score is less than or equal
   to `now − window`. The bound is *inclusive*, so an entry scored exactly one
   window ago is outside the window: the window is the half-open interval
   `(now − window, now]`. Then count the entries still in the set, and read the
   oldest survivor's score (`ZRANGE key 0 0 WITHSCORES`) — before any insert.
   Note the first window, in the order the caller passed them, whose
   `count >= limit`.
2. **Pass two.** If no window refused, insert the new member with the new score
   into **every** window and refresh each key's TTL (`window + 60`). If any
   window refused, insert nothing anywhere.

**All-or-nothing is a correctness property, not an optimisation** (fm#985 item
8). Checked one window at a time, each admitted window inserted before the next
had a chance to refuse — so a request the hourly bucket turned away had already
consumed a unit of `global` and of the per-minute bucket, for a response nobody
received. Behind a NAT that is how one throttled client fills the shared
address-keyed window with entries for requests that were never served. Deciding
them together makes a refusal free, and collapses three serial round trips into
one.

The **order** the windows are passed is precedence order (`global`, per-minute,
hourly). At most one result is refused — the first — so a refused client is
named the same limit it was named when these were three sequential checks.

The reply is `{blocked_position, count₁, oldest₁, count₂, oldest₂, …}`.
`blocked_position` is the 1-based index of the first window that refused, or `0`
when every window admitted. Each `oldest` is an empty string when that window
held nothing — an empty string rather than nil, because a nil inside a Lua table
truncates the array and the caller would receive a short reply it could not
distinguish from a different contract.

The script is multi-key by construction, so it assumes a single Redis keyspace:
the windows are keyed on different identities and would not share a hash slot
under Redis Cluster. FaultMaven runs standalone Redis with replicas; a move to
Cluster would need these keys hash-tagged onto one slot.

## Honest client signalling

Everything the client is told is derived from its window's `oldest` score. The oldest entry
ages out exactly one window after it arrived, so **`oldest_score + window` is
the instant the next unit of quota frees**:

- `reset_time` is that instant, on **both** the allowed and the blocked path.
  It therefore names the same moment across a client's whole window instead of
  marching forward with `now` on every request.
- `Retry-After` on a 429 is `max(1, ceil(oldest_score + window − now))`. Not a
  whole window: a client refused one second before its quota frees is told to
  wait one second. Floored at 1 because a sub-second answer reads as "retry
  immediately".
- When the window is empty, both fall back to `now + window` — the truth in
  that case, since the request that just went in is the entry that will age
  out.
- `oldest_score` is **clamped to `now`** before the window is added:
  `min(oldest_score, now) + window`. Scores are wall-clock and shared across
  replicas (see [Time source](#time-source)), so a host whose clock runs ahead
  can write an entry scored in *this* host's future. Unclamped, the derived wait
  would exceed the window itself — an answer no sliding window of that width can
  honestly produce. The clamp bounds `Retry-After` at one full window and leaves
  the measured value untouched whenever the clocks agree, which is every entry a
  host wrote itself.

  The clamp **bounds** the error; it does not remove it. A future-dated entry
  genuinely ages out at `oldest_score + window` — that is the score the prune
  compares against, on whichever replica next reads the key — so the clamped
  answer is *early* by the skew amount, and a client that obeys it can be
  refused again on arrival. That re-refusal carries a freshly measured wait,
  itself bounded by one window, so the client converges rather than looping
  unboundedly. Early-and-bounded is the direction to be wrong in: the
  alternative is a single wait longer than the window it describes, which no
  client can reconcile with the `X-RateLimit-Reset` beside it. NTP-scale skew
  makes the error sub-second; a replica minutes out of sync is a monitoring
  problem this clamp only keeps from becoming a client-facing one.

Both numbers come from a single `frees_at`, computed once per check, so
`reset_time` and `Retry-After` cannot name different instants. The formula lives
in one place — `infrastructure/protection/window_math.py` (`quota_frees_at`,
`retry_after_seconds`) — and both enforcers call it: the Redis check path and
the in-memory OAuth/SSO limiter in `modules/auth/api/rate_limiting.py`. It had
been hand-expanded at each site, so a correction to one was a silent divergence
from the other.

`Retry-After` carries **no jitter and no cap**. The value is already
per-client — each client's oldest entry arrived at its own time — so herd
de-synchronization is a property of the data rather than something randomness
has to add. The former 300-second cap actively caused the herd it was meant to
prevent: an hourly limit's genuine wait was truncated to five minutes, so every
capped client returned at five minutes to be refused again.

Response headers:

- A **429** carries `Retry-After`, `X-RateLimit-Limit`,
  `X-RateLimit-Remaining: 0` and `X-RateLimit-Reset` (the same instant as
  `Retry-After`, expressed as a timestamp).
- A **served** response carries `X-RateLimit-Limit/Remaining/Reset` for
  whichever checked limit has the **least remaining quota** — a client that
  respects the tightest limit it is under respects all of them. These are
  emitted from the `RateLimitResult`s the checks already produced, so they cost
  no additional Redis round trip, and they are emitted whether or not the
  request carried a session id. `global` is the only limit covering
  unauthenticated traffic, so gating the headers on a session used to leave
  precisely those callers with nothing to pace against.
- Both also carry **`X-RateLimit-Policy`**, naming which bucket the numbers
  describe, in the same token the configuration uses (`global`,
  `per_session_read_hourly`, …). An enforcer that owns several buckets under
  one limit type qualifies the token itself: the OAuth limiter publishes
  `oauth:/token`, `oauth:/authorize` and so on, because those six endpoints
  share `LimitType.OAUTH` with limits from 5 to 20 and a client pacing against
  a bare `oauth` would see the limit move underneath it. Five buckets can produce the same
  `Limit`/`Remaining` pair, and on a 429 the response body carries counts and a
  wait but no limit type — so without this header a refused client knows how
  long to wait and not what it hit, which is the difference between "slow this
  endpoint down" and "back off everywhere".
- Results reporting `limit == 0` are skipped: that is a disabled limit, and
  also what a check reports after failing open on a Redis error. Publishing it
  would tell every client it has no quota at all.

"Least remaining quota" compares **absolute** remaining counts across windows of
different sizes, deliberately. Window size governs when quota refills, not how
much of it is left: a client with 50 requests left in an hourly bucket may send
50 more right now whatever its per-minute bucket says, so the smallest remaining
count is the binding constraint on the next request. `X-RateLimit-Policy` and
`X-RateLimit-Reset` together say which window it belongs to and when it refills.

All five header names are in the `cors_expose_headers` default. A header a
cross-origin caller cannot read is a header not sent, and the responses that
carry them are exactly the ones a browser client must act on.

**Only the component that enforced the limit writes these headers.** For the
general limits that is `RateLimitMiddleware`; for the auth endpoints it is the
OAuth limiter dependencies in `modules/auth/api/rate_limiting.py`, which attach
the full quartet to the 429 they raise and, on an allowed request, append their
`RateLimitResult` to `request.state.rate_limit_results` — the inbox
`RateLimitMiddleware` opens for inner enforcers and merges with its own results
— so the served response advertises the OAuth limit whenever it is the tightest
one the caller is under
(it usually is — 5–10/min against the general limits' hundreds). Nothing else in
the **request-protection stack** sets or defaults a rate-limit header, and
`X-RateLimit-Window` is not emitted at all. Two writers can only agree by
coincidence: the outer one wins, so a correlation layer with no knowledge of the
enforcement would silently replace a measured wait with a constant. Whatever a
limiter did not measure is simply absent — a client that reads no `Retry-After`
backs off on its own policy, which is strictly better than backing off on a
fabricated one.

Two enforcers can both be on one request — the middleware for the general
limits, an OAuth limiter dependency inside it — so "one writer" is enforced on
the way out, by `_add_rate_limit_headers` **yielding**:

- It returns immediately on a **429**. Remaining quota beside a refusal is a
  contradiction the client resolves wrongly (`Remaining: 994` next to "you are
  being rate limited" reads as "not this limit, carry on"). The middleware's own
  refusals never reach it — those short-circuit in `dispatch` — so a 429 arriving
  through `call_next` was written by an inner enforcer that has already said what
  it measured.
- It returns immediately when the response **already carries
  `X-RateLimit-Limit`**. The inner enforcer wrote the limit *it* enforced; the
  middleware holds results for the general limits only, and stamping them over
  would replace a measured tighter quota with a roomier one nobody hit. It does
  not attempt to reconcile the two: "tightest wins" is computable only over
  results it holds, and an already-written header is not one of them.

Neither condition is redundant. A refusal can arrive without any `X-RateLimit-*`
(a non-limit 429), and an allowed response can carry an inner enforcer's headers
with no refusal anywhere.

The invariant is scoped to that stack, not to the process. One other authority
writes `Retry-After`: `api/exception_handlers.py` (`_llm_http`) stamps a
heuristic backoff hint — 60, 30 or 10 seconds by mapped condition — on the
responses it *synthesises* for LLM-provider errors (429/503/504/500). That is a
separate concern from request protection: nothing was rate-limited here by
FaultMaven, and the number is an in-house guess about an upstream provider's
recovery rather than a measurement of one of our windows. Passing the
*provider's own* `Retry-After` through, so that hint stops being a guess, belongs
to the fm#509 cluster and is tracked there.

The invariant that fm#920 restored: after N back-to-back requests against a
limit of L, exactly `min(N, L)` are allowed and the set holds `min(N, L)`
entries — regardless of how the requests distribute across wall-clock seconds.

## Memory bound

A window key holds at most `limit` entries, never `window` entries: the set
grows only on allowed requests, and step 3 refuses without inserting once the
limit is reached. Each entry costs a 32-character member plus a float score,
which with sorted-set overhead lands around 90–120 bytes, so a saturated key
costs roughly `limit × 100` bytes. The key's TTL is `window + 60`, so a key stops
costing anything shortly after its last request — a minute later for the
per-minute windows, an hour later for the hourly ones.

The largest key is no longer `global`. Since reads were given their own
per-session buckets (fm#994), production's ceilings are:

| Limit | Production | Keyed on | Saturated key | Held for |
|-------|-----------|----------|---------------|----------|
| `global` | 500 / 60s | client address | ~50 KB | ~2 min |
| `per_session` | 10 / 60s | session | ~1 KB | ~2 min |
| `per_session_hourly` | 50 / 3600s | session | ~5 KB | ~1 hr |
| `per_session_read` | 120 / 60s | session | ~12 KB | ~2 min |
| `per_session_read_hourly` | 1200 / 3600s | session | **~120 KB** | ~1 hr |

`per_session_read_hourly` is both the largest and the one there is one of *per
session*, so it is the term that scales with concurrency: a thousand sessions
that each saturate it cost on the order of 120 MB, held for an hour past their
last request. That is a ceiling and not a typical cost — entries accumulate only
on allowed requests, so a session that issues fifty reads holds fifty entries,
not twelve hundred. It is worth watching when sizing Redis for a deployment with
many simultaneous sessions, and it is the first number to lower if that sizing
becomes a problem.

## The defect this replaced

The previous implementation used `int(time.time())` as **both score and
member**. ZADD on an existing member updates its score instead of inserting,
so all requests within the same wall-clock second collapsed into one element
and `ZCARD` could never exceed the number of distinct seconds in the window.
Consequences:

- Any limit with `requests > window` (production `global` 500/60s,
  development 5000/60s) was unreachable —
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

The signalling contract carries its own obligations
(`tests/unit/infrastructure/test_rate_limiter_honest_signalling.py`,
`tests/unit/api/middleware/test_rate_limit_client_signalling.py`): a client
refused near the window's edge is told to wait seconds rather than a window; an
hourly limit's wait is not truncated; `reset_time` is the same instant on the
allowed and the blocked path and does not move with the clock; a served
response with no session id still advertises the `global` limit; and the header
path performs no Redis call of its own.

Mutation checks (each must turn at least one test red): reverting the member
to the integer second; skipping the `ZREMRANGEBYSCORE` prune; inserting on
the blocked path; making the prune bound exclusive; reverting the script to a
three-element return; restoring `now + window` on the blocked path; removing the
skew clamp from `quota_frees_at`; re-deriving `X-RateLimit-Reset` on a 429 from
`time.time() + retry_after`; dropping the `X-RateLimit-*` headers from the OAuth
429; closing the outgoing client before installing its replacement in `_adopt`;
awaiting the close instead of dispatching it; removing either early return from
`_add_rate_limit_headers`; writing `Retry-After` unconditionally in
`_create_rate_limit_response`, or restoring a `or 60` / `or 3600` default at
**any** of the three raise sites — the state a default fires on is unreachable
in production, but the "unmeasured is absent" contract is what makes it
*testable*, so both spellings of the regression turn a test red. The raise-site
mutation is swept per site rather than checked once: a limit's fallback is only
reachable through the site that raises for it, and since the read/write split
four limit types share the two session sites, so a single-site guard left most
of that surface unguarded. Registering a middleware after CORS.
