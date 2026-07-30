# Redis Credential Resolution

How the application decides *which* Redis to talk to and *with what credentials*.

## The invariant

**There is exactly one place Redis connection parameters are assembled:**
`RedisClientFactory._build_config` in
[`faultmaven/infrastructure/redis_client.py`](../../../faultmaven/infrastructure/redis_client.py).

Every subsystem that needs Redis either receives the container's
boot-validated client, or calls the factory. No subsystem builds a Redis URL
from environment variables of its own.

This is the invariant because credential assembly is the thing that silently
drifts. A second assembly site does not fail loudly — it produces a *valid*
URL that simply omits the password, and the subsystem then authenticates
anonymously against a Redis that requires auth. The failure surfaces as a
disabled feature, not as a boot error.

## Resolution order

`_build_config` resolves in this order and stops at the first hit:

1. An explicit `redis_url` argument.
2. `settings.database.redis_url` — the complete-URL form (`REDIS_URL`).
3. Explicit discrete arguments (`host` / `port` / `password` / `db`).
4. `settings.database` discrete fields — `REDIS_HOST`, `REDIS_PORT`,
   `REDIS_PASSWORD`, `REDIS_DB`.

A URL wins wholesale: it already carries host, port, password and database, so
the discrete fields are left unset. Cloud uses form 4 (discrete
`REDIS_PASSWORD`); standalone and Docker Compose use form 2 or FakeRedis.

**Consequence, and the reason this document exists:** because form 1
short-circuits everything, *any* caller that passes a self-assembled
`redis_url` bypasses the password lookup entirely. Passing `None` is not a
degraded case — it is the correct, deliberate way to say "resolve centrally".

## Who consumes what

| Consumer | How it gets a client |
|---|---|
| DI container (composition root) | `get_async_redis_client()` — constructs, pings, publishes as `app.state.redis_client` |
| `DeduplicationMiddleware` | `resolve_redis_client(request)` → `app.state.redis_client` |
| `RateLimitMiddleware` → `RedisRateLimiter` | `app.state.redis_client`, passed into `RedisRateLimiter.initialize()` |
| `llm_config_overrides._get_redis` | `get_async_redis_client(redis_url=settings.database.redis_url)` |

`ProtectionSettings.redis_url` is `Optional[str]` and defaults to `None`,
meaning "resolve centrally". It is populated only when an operator sets
`REDIS_URL` explicitly — the one case where a complete URL genuinely is the
configured source.

## Fail-open policy for rate limiting

Rate limiting is both a security control and a cost control, so "Redis is
unreachable" must not silently become "no limiting". The degrade ladder, most
to least preferred:

1. **Shared Redis** — the container's boot-validated client. Limits are
   global across replicas. Normal operation.
2. **Per-replica FakeRedis** — an in-process stand-in. Limits are still
   enforced, just per replica rather than globally, and the degrade is logged
   at ERROR. Reached when the shared client is unavailable at first request
   **or stops answering later** (see "Demotion" below). Accepting this rung is
   itself governed by `fail_open_on_redis_error`: an operator who has demanded
   fail-closed gets a refusal rather than a per-replica approximation.
3. **Fail open** — requests pass unlimited. Governed by
   `fail_open_on_redis_error`, sourced from `PROTECTION_RATE_LIMIT_FAIL_OPEN`
   (default `true`) on the general load paths and the development preset. The
   production loader does not read the key: it pins fail-**closed** (see
   "Production fails closed" below).

`fail_open_on_redis_error` governs *policy*, never *reporting*.
`RedisRateLimiter.initialize` returning normally always means a usable client
is attached; it raises otherwise, whatever the flag says. The flag decides what
the request path does with a limiter that has no client — it never makes a
missing client look like a working one.

`PROTECTION_RATE_LIMIT_FAIL_OPEN` governs rate-limiting degrade policy and
nothing else. It is deliberately distinct from `PROTECTION_FAIL_OPEN`, which
binds `settings.protection.fail_open` (default `false`) and governs whether
PII redaction may pass un-analyzed text to a provider when Presidio is
unavailable. The two policies are independent and their defaults differ
(redaction closed; rate limiting open on the general paths), so sharing one key
would mean an operator hardening redaction silently converts a Redis blip into a
service-wide 503 — a coupling neither policy asked for.

### Production fails closed

`get_production_protection_settings` pins `fail_open_on_redis_error=False` and
does not read `PROTECTION_RATE_LIMIT_FAIL_OPEN`. It is the only loader that
pins the policy.

Defaulting production open would rest on the claim that rung 3 is nearly
unreachable because rungs 1 and 2 enforce limits first. **That claim is false
today.** The sliding window counts seconds, not requests: the Lua script does
`ZADD key current_time current_time`, using the same integer second as both
score *and* member, so same-second requests update one member instead of adding
entries and `ZCARD` can never exceed the window's length in seconds. Every
`global` limit configured in `config/protection.py` (production 500/60,
development 5000/60, settings path 1000/60) is therefore unreachable —
measured: 5000 requests from one IP against production's 500/60 blocked none,
final `ZCARD` 6 — and `global` is the only limit that applies to
unauthenticated traffic. Under that defect rungs 1 and 2 do not enforce the
global limit at all, so fail-open is not the floor of a ladder, it is the whole
ladder.

The counting defect is tracked separately and is deliberately not fixed here.
Until it lands, production takes the 503 cliff over the hole: rate limiting is
a security *and* cost control, and the trade-off against a total API outage is
only answerable once the intermediate rungs limit anything. Revisit this pin
then — not before.

The general load paths and the development preset do honour
`PROTECTION_RATE_LIMIT_FAIL_OPEN` (default `true`), which is what removes the
hardcode; production opts out explicitly rather than by omission.

**Initialization latches in neither direction.** A failed
`RateLimitMiddleware._initialize` leaves `_initialized` false so a later
request retries, bounded by a cooldown so a persistent outage does not
generate a connection attempt per request. Latching on failure — the previous
behaviour — meant one blip on the first request after a pod started disabled
rate limiting for the pod's entire lifetime, with no path back.

Landing on rung 2 does not latch either. It counts as initialized, so once
adopted the stand-in keeps enforcing limits rather than opening a window of
unlimited traffic, but the cooldown keeps re-attempting rung 1 so the pod is
promoted back rather than running per-replica forever.

### Demotion: the ladder is entered on client *death*, not only at startup

The ladder above would be a fiction if it only ran at initialization. The
ordinary production Redis outage is not "Redis was down when the pod booted" —
it is a restart, a failover, or a lost network an hour into the pod's life,
after a perfectly successful adoption. Treating a successful adoption as
permanent meant the limiter kept a dead client, every check fell through
`check_rate_limit`'s catch-all to fail-open, and **no rung below rung 1 was ever
reached** in the shape that matters most.

So liveness is tracked on the check path:

- `RedisRateLimiter` counts **consecutive** failed checks. At
  `CHECK_FAILURE_DEMOTION_THRESHOLD` (3) it declares the client dead and bumps
  `demotion_generation`. Three because a single timeout is a blip and demoting
  on it would churn a healthy pod, while every limited request performs at
  least one check — so three consecutive failures is sub-second on any pod
  carrying traffic. The count resets on any success, so an intermittent
  one-in-N error never accumulates into a demotion.
- A failed check is **attributed to the client it was issued against**.
  `check_rate_limit` snapshots the client and an adoption epoch together and
  issues the command against that snapshot; a failure whose epoch is no longer
  current is discarded. Without that, the fix ate its own successor: when a pool
  dies under traffic the commands already on the wire hang to `socket_timeout`
  while later ones fail fast, so the fast failures demote and re-enter the
  ladder and *then* the slow ones land, cross the threshold a second time and
  declare the healthy stand-in dead — inside a freshly armed cooldown, which
  means unlimited traffic for the rest of it with a working client in hand. The
  epoch is monotonic rather than an identity test, so re-adopting the *same*
  client object after a recovery still opens a new epoch. Successes are
  attributed the same way, so a stale success cannot clear a genuine failure run
  belonging to the current client.
- `RateLimitMiddleware` holds the generation it last acted on. A change means
  "re-enter the ladder": it drops `_initialized` (so no further checks run
  against the dead client) and re-initializes. Comparing generations keeps one
  death to **one** re-entry, however many requests observe it.
- Re-entry **pings before adopting**. On a mid-life outage `app.state.redis_client`
  still holds the client that just died; re-adopting it unchecked would land
  back in the dead state on every retry and never reach the stand-in. The very
  first initialization skips the ping — the composition root has already
  validated that client and a second ping is pure cost.
- The factory rung distinguishes "standalone chose FakeRedis by design"
  (terminal) from "the factory fell back to FakeRedis after a real client died"
  (degraded, keep retrying), so recovery still promotes.
- Retries stay on the cooldown. The one immediate re-entry is the transition
  out of a healthy terminal client; everything after that waits out
  `INIT_RETRY_COOLDOWN_SECONDS`, so this is never a ping per request.

**How much traffic goes unlimited before the ladder is re-entered is a
duration, not a request count.** The threshold (3) bounds *detection*, not the
window: the window is three failing checks plus however long the re-entry
attempt takes, and against a dead pool that attempt is a ping running to
`socket_connect_timeout` (5s) or `socket_timeout` (10s). Every request arriving
in that time sees `_initialized` false, and each one used to conclude "no
limiter" independently and pass — measured at 30 of 30 in a concurrent burst,
29 of them answered before the ping even returned. **Attempts are therefore
serialised** (`_initialize` holds a lock): concurrent arrivals wait for the
in-flight attempt and are then checked against whatever it adopted. They pay
that latency once instead of passing free, and the wait is bounded because the
factory sets both socket timeouts. The fast path returns before the lock, so a
healthy limiter never contends on it.

This is a bounded window either way, and bounded is the improvement — the
behaviour it replaced was unlimited traffic for the pod's entire lifetime, with
no path back.

**The probes are resolved before any of this.** `dispatch` decides whether a
request is rate limited at all — `rate_limiting_enabled`, then `_should_bypass`
— *before* touching initialization. `/health`, the `/health/` sub-tree and
`/readiness` bypass unconditionally. Otherwise a one-second Redis blip put a
fail-closed pod inside a 30-second cooldown that answered its own liveness
probe with 503, and the kubelet killed it.

Being inside the cooldown is not itself a verdict. When a limited request finds
no client, the check is skipped outright (rather than run against `None`, which
logged one to four ERROR lines *per request* for the whole window), the
condition is logged once per window, and only then does
`fail_open_on_redis_error` decide between passing the request and answering
503.

## Privileged database credentials are not app credentials

The same "one source" principle applies one layer out, in the deployment
manifests. The API pod's environment carries only the credentials the
application actually reads. Privileged DSNs — the owner/migrator role
(DDL, RLS-exempt) and the maintenance role (`BYPASSRLS`) — live in a separate
Secret consumed only by the Job and CronJob specs that use them.

The app has no consumer for either DSN, and both are strictly stronger than
the RLS-bound app role whose isolation guarantee the application boot-verifies.
Mounting them into the API pod means a single API-pod compromise hands over
the credential that defeats tenant isolation. See
`faultmaven-enterprise-infra` → `kubernetes/apps/faultmaven/base/secrets.yaml`.

## Rejected alternative

Adding password support to each subsystem's own URL builder — rejected
because this is the third instance of the same class of bug (async client
drift, boot-gate drift, rate-limiter auth drop) and a fourth builder would
only move the next drift somewhere new; the fix has to remove the parallel
source, not correct it.
