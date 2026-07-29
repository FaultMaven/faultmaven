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
   at ERROR. Reached when the shared client is unavailable at first request.
3. **Fail open** — requests pass unlimited. Governed by
   `fail_open_on_redis_error`, sourced from `PROTECTION_FAIL_OPEN` (default
   `true`) on both the settings and environment load paths.

Rung 3 defaults to open rather than closed deliberately: failing closed turns
a Redis blip into a total API outage (503 on every request), which is a worse
failure than rung 2's per-replica limiting. Rung 2 exists precisely so rung 3
is nearly unreachable — the honest reading of "fail open" here is "after two
strictly-better degrades have already been tried".

**Initialization does not latch on failure.** A failed
`RateLimitMiddleware._initialize` leaves `_initialized` false so a later
request retries, bounded by a cooldown so a persistent outage does not
generate a connection attempt per request. Latching on failure — the previous
behaviour — meant one blip on the first request after a pod started disabled
rate limiting for the pod's entire lifetime, with no path back.

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
