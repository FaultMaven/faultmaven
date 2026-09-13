# Postmortem: Checkout API outage, 2026-08-14

## Impact

Checkout requests failed for 42 minutes (14:02–14:44 UTC). p99 latency on
`POST /checkout` rose from 180ms to 9.4s; 31% of requests returned HTTP 503.

## Symptom as observed

The `CheckoutLatencyHigh` alert fired at 14:02. Application logs filled with:

```
ERROR: remaining connection slots are reserved for non-replication superuser connections
psycopg2.OperationalError: FATAL: sorry, too many clients already
```

`pg_stat_activity` showed 100 of 100 connections in use.

## Investigation

```sql
SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
SELECT pid, state, query_start, query FROM pg_stat_activity
 WHERE state = 'idle in transaction' ORDER BY query_start;
```

47 sessions were `idle in transaction`, the oldest 26 minutes old.

## Contributing factors

Deploy `2f81c` at 13:58 raised the per-pod SQLAlchemy pool size from 10 to 40
without lowering the replica count, so 6 pods could demand 240 connections
against a `max_connections` of 100. The pool ceiling was the immediate trigger.

Separately, a code path added in the same deploy opened a transaction before an
external HTTP call and only committed after it returned. When the downstream
payment provider slowed, those transactions stayed open, so connections were
held far longer than the query itself needed.

`idle_in_transaction_session_timeout` was unset on the database, so PostgreSQL
never reclaimed the stranded sessions on its own — they accumulated until the
pool was exhausted.

## Resolution

Rolled back `2f81c` at 14:41; latency recovered by 14:44.

## Action items

- Cap aggregate pool size in the Helm chart (pool_size × replicas < max_connections).
- Move the external call outside the transaction boundary.
- Set `idle_in_transaction_session_timeout = '60s'`.
