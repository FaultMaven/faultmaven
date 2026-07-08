---
id: "rds-connection-exhaustion"
title: "AWS RDS Connection Exhaustion: max_connections Reached"
domain: database
service: aws-rds
symptom_class: [connection_refused, latency]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [too-many-connections, error-1040, max-connections, rds-proxy, database-connections]
difficulty: intermediate
---

## Symptom Recognition

- MySQL/MariaDB clients fail with: `ERROR 1040 (HY000): Too many connections`
- PostgreSQL clients fail with: `FATAL: remaining connection slots are reserved for non-replication superuser connections`
- PostgreSQL clients may also see: `FATAL: sorry, too many clients already`
- New connections are refused while existing sessions keep working (intermittent connect failures under load).
- CloudWatch `DatabaseConnections` for the instance is flat at, or oscillating just below, the instance `max_connections` value.
- Application-side: spiking p99 query latency and connection-pool checkout timeouts immediately before connections start being refused.

## Applicability

- Engines: Amazon RDS for MySQL, MariaDB, PostgreSQL (and Aurora MySQL/PostgreSQL with the equivalent `max_connections` formulas).
- Required AWS access: `rds:DescribeDBInstances`, `rds:DescribeDBParameters`, `cloudwatch:GetMetricStatistics`, `pi:GetResourceMetrics` (if Performance Insights is enabled), and `rds:CreateDBProxy` / `rds:RegisterDBProxyTargets` for the RDS Proxy remediation.
- Required DB access: an admin DB user (MySQL needs `PROCESS`/`CONNECTION_ADMIN`; PostgreSQL needs a role that can read `pg_stat_activity`). MySQL permits `max_connections + 1` so a `CONNECTION_ADMIN` user can still log in to diagnose.
- Tools: AWS CLI v2 (`aws rds`, `aws cloudwatch`, `aws pi`), and `mysql` or `psql` clients.

## Diagnostic Steps

### Step 1: Read the configured connection limit and live connection count

```bash
# Effective max_connections for the instance's parameter group
aws rds describe-db-parameters \
  --db-parameter-group-name <your-parameter-group> \
  --query "Parameters[?ParameterName=='max_connections'].[ParameterName,ParameterValue]" \
  --output table

# Live DatabaseConnections over the last hour (Maximum per 60s)
aws cloudwatch get-metric-statistics \
  --namespace AWS/RDS \
  --metric-name DatabaseConnections \
  --dimensions Name=DBInstanceIdentifier,Value=<db-instance-id> \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Maximum --output table
```

Expected output: the configured `max_connections` value, and a time series of peak connection counts. When exhausted, the `Maximum` series sits at (or just under) the configured limit.

### Step 2: Enumerate live sessions and identify who holds the connections

```sql
-- MySQL / MariaDB
SHOW STATUS LIKE 'Threads_connected';
SHOW STATUS LIKE 'Max_used_connections';
SELECT USER, HOST, DB, COMMAND, TIME, STATE, COUNT(*) AS conns
FROM information_schema.PROCESSLIST
GROUP BY USER, HOST, DB, COMMAND, STATE
ORDER BY conns DESC;
```

```sql
-- PostgreSQL
SELECT setting::int AS max_connections FROM pg_settings WHERE name = 'max_connections';
SELECT count(*) AS total,
       count(*) FILTER (WHERE state = 'idle') AS idle,
       count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn
FROM pg_stat_activity;
SELECT usename, application_name, client_addr, state, count(*) AS conns
FROM pg_stat_activity
GROUP BY usename, application_name, client_addr, state
ORDER BY conns DESC;
```

Expected output: total session count near the limit, plus a breakdown by user/host/state. A large `idle` or `idle in transaction` count points at a pool/leak; a spread across many distinct app hosts points at fleet growth.

### Step 3: Decompose database load by wait event (Performance Insights)

```bash
# metric-queries.json:
#   [{"Metric": "db.load.avg", "GroupBy": {"Group": "db.wait_event"}}]
aws pi get-resource-metrics \
  --service-type RDS \
  --identifier <DbiResourceId> \
  --metric-queries '[{"Metric":"db.load.avg","GroupBy":{"Group":"db.wait_event"}}]' \
  --start-time "$(date -u -d '1 hour ago' +%s)" \
  --end-time "$(date -u +%s)" \
  --period-in-seconds 300 \
  --output json
```

Expected output: average active sessions (AAS) split by wait event. `Client:ClientRead` (PostgreSQL) dominating means sessions are connected but waiting on the application, not the DB. A `Lock:`/`LWLock:` wait event dominating means blocking is holding sessions open. Get `<DbiResourceId>` from `aws rds describe-db-instances --db-instance-identifier <db-instance-id> --query 'DBInstances[0].DbiResourceId' --output text`.

## Causes

### Cause A: Application connection pools oversized for the instance limit
**Statement:** The sum of `pool_max` across all running application instances exceeds the instance `max_connections`, so connection demand at peak fan-out overruns the server's hard slot limit.
**Chain:**
- root: aggregate app pool capacity (instances × pool_max) > server max_connections
- s1: at peak fan-out, app instances simultaneously open their full pools
- s2: server reaches max_connections and rejects further connects
- D: clients get `Too many connections` / `remaining connection slots are reserved`
**Indicators:**
- root: [Step 1] `DatabaseConnections` Maximum equals the configured `max_connections` value
- s1: [Step 2] connection count grouped by `client_addr`/`HOST` is spread across many distinct application hosts in `active` state
- D: [Symptom] clients log `ERROR 1040 (HY000): Too many connections` or `FATAL: remaining connection slots are reserved`
**Interventions:**
- **remediation** (root): Front the database with RDS Proxy so many client connections multiplex onto a bounded server-side pool; cap the pool with `MaxConnectionsPercent` (leave ≥30% headroom).

  ```bash
  aws rds create-db-proxy \
    --db-proxy-name <proxy-name> \
    --engine-family POSTGRESQL \
    --auth AuthScheme=SECRETS,SecretArn=<secret-arn>,IAMAuth=DISABLED \
    --role-arn <iam-role-arn> \
    --vpc-subnet-ids <subnet-1> <subnet-2>

  aws rds register-db-proxy-targets \
    --db-proxy-name <proxy-name> \
    --db-instance-identifiers <db-instance-id>

  # Bound the pool to 70% of server max_connections
  aws rds modify-db-proxy-target-group \
    --db-proxy-name <proxy-name> \
    --target-group-name default \
    --connection-pool-config MaxConnectionsPercent=70,MaxIdleConnectionsPercent=50
  ```

  **Verification:** Re-run Step 1; `DatabaseConnections` should plateau below `max_connections` even at peak, and CloudWatch `DatabaseConnections` vs `MaxDatabaseConnectionsAllowed` on the proxy should show headroom.
- **defensive_fix** (s1): Reduce per-instance `pool_max` so `instances × pool_max + admin_overhead < max_connections` (set in the app's pool config, e.g. HikariCP `maximumPoolSize`, SQLAlchemy `pool_size`), then roll the fleet.

  ```bash
  # Example: redeploy with a smaller pool size after editing app config
  kubectl rollout restart deployment/<app-deployment>
  ```

  **Verification:** Re-run Step 2; summed `Threads_connected` / `pg_stat_activity` count at peak stays comfortably below the limit.

### Cause B: Leaked / idle-in-transaction connections never returned to the pool
**Statement:** Application code paths open connections (or begin transactions) without reliably closing/committing them, so sessions accumulate in an idle state and permanently consume slots until the limit is hit.
**Chain:**
- root: connections/transactions opened without a guaranteed close/commit (leak)
- s1: sessions accumulate in `idle` / `idle in transaction` state and are never reclaimed
- s2: live slot count climbs monotonically until it reaches max_connections
- D: new connects are refused with the too-many-connections error
**Indicators:**
- root: [Step 2] high `idle in transaction` count with large `TIME`/age on those sessions
- s1: [Step 2] `idle` connection count is a large fraction of total and grows over time without matching query activity
- s2: [Step 1] `DatabaseConnections` trends upward monotonically (saw-toothless climb) rather than tracking traffic
**Interventions:**
- **remediation** (root): Fix the leak at the source — wrap acquisition in try/finally (or context managers / RAII) so every connection is closed and every transaction is committed or rolled back; add pool leak detection (e.g. HikariCP `leakDetectionThreshold`).

  ```bash
  # After patching the leak, redeploy
  kubectl rollout restart deployment/<app-deployment>
  ```

  **Verification:** Re-run Step 2 after the next peak; `idle in transaction` count stays near zero and total connections track real traffic.
- **mitigation** (s1): Reap idle and stuck sessions to immediately free slots while the fix ships. **Risk:** killing an in-flight transaction rolls it back; verify the session is truly idle first. **Duration:** one-shot; safe to repeat but it is not a fix. **Verification:** Re-run Step 1; `DatabaseConnections` drops immediately.

  ```sql
  -- PostgreSQL: terminate sessions idle in transaction > 10 min
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE state = 'idle in transaction'
    AND now() - state_change > interval '10 minutes';
  ```

### Cause C: Instance memory too small, capping max_connections below real demand
**Statement:** The DB instance class has too little memory, and because RDS derives `max_connections` from `DBInstanceClassMemory`, the formula-computed limit is genuinely below the workload's legitimate concurrent-connection need.
**Chain:**
- root: instance class memory is undersized for the workload's concurrency
- s1: the RDS formula yields a low max_connections (PostgreSQL `LEAST({DBInstanceClassMemory/9531392}, 5000)`; MySQL `{DBInstanceClassMemory/12582880}`)
- s2: legitimate peak demand exceeds that computed ceiling
- D: connects are refused at the limit even with healthy pools and no leaks
**Indicators:**
- root: [Step 1] configured `max_connections` is the default formula value (not a hand-raised override) and is low relative to demand
- s1: [Step 2] sessions are predominantly `active` (real work), not idle — demand is legitimate
- D: [Symptom] error occurs at peak load with pools correctly sized
**Interventions:**
- **remediation** (root): Scale the instance up to a class with more memory; this raises the formula-derived `max_connections` automatically without manual tuning (AWS best practice: scale up before raising the parameter).

  ```bash
  aws rds modify-db-instance \
    --db-instance-identifier <db-instance-id> \
    --db-instance-class db.r6g.2xlarge \
    --apply-immediately
  ```

  **Verification:** After the modify completes, re-run Step 1; the formula-derived `max_connections` is higher and `DatabaseConnections` peak now sits below it.
- **mitigation** (s1): Raise `max_connections` in the parameter group as a stopgap. **Risk:** AWS warns against exceeding the default formula value — each connection consumes memory and a low-memory instance can crash. **Duration:** until the scale-up is scheduled; do not run indefinitely. **Verification:** Re-run Step 1 and watch CloudWatch `FreeableMemory` stays well above zero.

  ```bash
  aws rds modify-db-parameter-group \
    --db-parameter-group-name <your-parameter-group> \
    --parameters "ParameterName=max_connections,ParameterValue=<higher-value>,ApplyMethod=immediate"
  ```

### Cause D: Sessions pinned open by long-running queries or lock contention
**Statement:** A blocking lock or a set of long-running queries holds backend sessions open far longer than normal, so connection turnover collapses and the live slot count climbs to the ceiling even though request volume is normal.
**Chain:**
- root: a blocking transaction / long-running query holds locks and pins sessions
- s1: blocked sessions stack up waiting, each consuming a connection slot
- s2: connection turnover stalls and live slot count rises to max_connections
- D: new connects are refused while blocked sessions occupy the slots
**Indicators:**
- root: [Step 3] Performance Insights `db.load.avg` is dominated by a `Lock:`/`LWLock:` wait event
- s1: [Step 2] many sessions share the same `STATE`/blocked state with large `TIME` values
- s2: [Step 1] `DatabaseConnections` climbs during the lock window while traffic is flat
**Interventions:**
- **remediation** (root): Eliminate the blocking pattern — add the missing index, shorten the transaction, or remove the long-held lock in application code; deploy the fix.

  ```bash
  kubectl rollout restart deployment/<app-deployment>
  ```

  **Verification:** Re-run Step 3; the `Lock:`/`LWLock:` wait event no longer dominates `db.load.avg` and Step 1 connection count tracks traffic again.
- **mitigation** (s1): Terminate the specific blocking session to release its locks and let queued sessions drain. **Risk:** rolls back the blocker's transaction. **Duration:** one-shot. **Verification:** Re-run Step 2; blocked sessions clear and connection count falls.

  ```sql
  -- PostgreSQL: find then terminate the blocking backend
  SELECT blocked.pid AS blocked_pid, blocking.pid AS blocking_pid
  FROM pg_stat_activity blocked
  JOIN pg_stat_activity blocking
    ON blocking.pid = ANY(pg_blocking_pids(blocked.pid));
  -- SELECT pg_terminate_backend(<blocking_pid>);
  ```

### Cause Z: Unidentified
**Statement:** The connection exhaustion does not match any known root cause above, or the available signals are insufficient to isolate a single root.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot and escalate to the database SME. **Risk:** none (read-only capture). **Duration:** N/A. **Verification:** snapshot artifacts attached to the incident.

  ```bash
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  aws rds describe-db-instances --db-instance-identifier <db-instance-id> > rds-instance-$TS.json
  aws rds describe-db-parameters --db-parameter-group-name <your-parameter-group> > rds-params-$TS.json
  aws cloudwatch get-metric-statistics --namespace AWS/RDS \
    --metric-name DatabaseConnections \
    --dimensions Name=DBInstanceIdentifier,Value=<db-instance-id> \
    --start-time "$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --period 60 --statistics Maximum Average > rds-connections-$TS.json
  aws pi get-resource-metrics --service-type RDS --identifier <DbiResourceId> \
    --metric-queries '[{"Metric":"db.load.avg","GroupBy":{"Group":"db.wait_event"}}]' \
    --start-time "$(date -u -d '3 hours ago' +%s)" --end-time "$(date -u +%s)" \
    --period-in-seconds 300 > rds-pi-$TS.json
  ```

## Prevention

- Front high-fan-out workloads with RDS Proxy and bound the pool via `MaxConnectionsPercent`, leaving ≥30% headroom for redistribution across proxy nodes.
- Size application pools by budget: `Σ(instances × pool_max) + admin_overhead < max_connections`; re-check whenever the fleet autoscales.
- CloudWatch alarm on `DatabaseConnections` at ~80% of `max_connections` (and on RDS Proxy `DatabaseConnectionsBorrowLatency`).
- Set `idle_in_transaction_session_timeout` (PostgreSQL) and tune `wait_timeout`/`interactive_timeout` (MySQL) so leaked/idle sessions are reaped automatically.
- Enable Performance Insights so `db.load.avg` by wait event is already available when an incident starts.
- Scale instance memory up before manually raising `max_connections`; do not exceed the default formula value on memory-constrained classes.

## Sources

- [Troubleshooting for Amazon RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Troubleshooting.html) — RDS connection troubleshooting context.
- [Using Amazon RDS Performance Insights](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PerfInsights.html) — DBLoad / average active sessions, wait-event dimension.
- [Increase max connections of my RDS for MySQL or PostgreSQL instance (re:Post)](https://repost.aws/knowledge-center/rds-mysql-max-connections) — `max_connections` formulas (MySQL `DBInstanceClassMemory/12582880`, PostgreSQL `LEAST({DBInstanceClassMemory/9531392}, 5000)`), scale-before-raise guidance.
- [Resolve the Too Many Connections error in a MySQL DB instance (re:Post)](https://www.repost.aws/knowledge-center/rds-mysql-too-many-connections) — `ERROR 1040` semantics, `DatabaseConnections` symptom, `SHOW PROCESSLIST`, timeout params.
- [MySQL B.3.2.5 Too many connections](https://dev.mysql.com/doc/refman/8.0/en/too-many-connections.html) — `max_connections + 1` admin reservation, `CONNECTION_ADMIN`, `SHOW PROCESSLIST` for diagnosis.
- [RDS Proxy connection considerations](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy-connections.html) — `MaxConnectionsPercent`/`MaxIdleConnectionsPercent`, ≥30% headroom guidance.
- [Monitoring RDS Proxy metrics with CloudWatch](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy.monitoring.html) — `DatabaseConnections` vs `MaxDatabaseConnectionsAllowed`, `DatabaseConnectionsBorrowLatency`.
- [create-db-proxy (AWS CLI Reference)](https://docs.aws.amazon.com/cli/latest/reference/rds/create-db-proxy.html) — `--engine-family`, `--auth`, `--role-arn`, `--vpc-subnet-ids` syntax.
- [Creating a proxy for Amazon RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy-creating.html) — `register-db-proxy-targets` association step.
- [get-resource-metrics (AWS CLI Reference)](https://docs.aws.amazon.com/cli/latest/reference/pi/get-resource-metrics.html) and [AWS CLI examples for Performance Insights](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PerfInsights.API.Examples.html) — `aws pi get-resource-metrics` with `db.load.avg` grouped by `db.wait_event`, `DbiResourceId` identifier.
