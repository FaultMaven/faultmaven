---
id: "mongo-replicaset-election"
title: "MongoDB Replica-Set Election Storms Disrupting Writes"
domain: database
service: mongodb
symptom_class: [service_unavailable, replication_lag]
severity: high
scope: global
version: "1.0.1"
last_updated: "2026-08-26"
verified_by: "kb-researcher"
status: draft
tags: [replica-set, election-timeout, oplog-lag, step-down, network-partition]
difficulty: advanced
---

## Symptom Recognition

- Drivers throw `MongoServerError: not primary` / `not master` and retry writes against a node that just stepped down.
- Writes intermittently fail with `NotWritablePrimary` or `PrimarySteppedDown` (code 189) during write bursts.
- `mongod` log lines on members repeatedly show election activity, e.g.:
  - `Starting an election, since we've seen no PRIMARY in the past <electionTimeoutMillis>ms`
  - `Member is now in state PRIMARY` / `transition to PRIMARY` followed shortly by `Stepping down from primary`
  - `Scheduling priority takeover` (a higher-priority member forcing a step-down of the current primary)
  - `Couldn't get a heartbeat response ... Error connecting` / heartbeat marking a member inaccessible
- `rs.status()` shows the `PRIMARY` role oscillating between members and members flipping to `health: 0` / `stateStr: "(not reachable/healthy)"`.
- Replication lag climbs (`rs.printSecondaryReplicationInfo()` shows secondaries seconds-to-minutes behind), making them ineligible to win or sustain a primacy.

## Applicability

- MongoDB replica sets running replication protocol version 1 (`pv1`), MongoDB 4.0+ (commands valid through 8.x; `mongosh` 1.x+).
- Requires a `mongosh` session connected to the set with the `clusterMonitor` role (read diagnostics) and `clusterManager` / `clusterAdmin` for `rs.reconfig()`, `rs.stepDown()`, `rs.freeze()`.
- OS-level access to each node for `mongod` logs (default `/var/log/mongodb/mongod.log`), `ping`, and `mongostat`.
- Tools: `mongosh`, `mongostat`, `mongo` log access, network reachability tooling (`ping`, `mtr`).

## Diagnostic Steps

### Step 1: Capture current replica-set membership and election state

```bash
mongosh --quiet --eval 'rs.status().members.forEach(m => print(
  m.name, "| state="+m.stateStr, "| health="+m.health,
  "| pingMs="+(m.pingMs ?? "-"), "| lastHeartbeat="+(m.lastHeartbeat ?? "-")))'
```

Expected output: one line per member. A stable set shows exactly one `state=PRIMARY`, the rest `state=SECONDARY`, all `health=1`. Storm symptoms: members with `health=0`, `state=(not reachable/healthy)`, or a `PRIMARY` that differs from a sample taken seconds earlier.

### Step 2: Inspect election history and step-down events in the mongod log

```bash
grep -E "election|Stepping down|priority takeover|no PRIMARY|heartbeat" /var/log/mongodb/mongod.log | tail -n 40
```

Expected output: clustered timestamps showing repeated `Starting an election, since we've seen no PRIMARY in the past`, `Stepping down from primary`, or `Scheduling priority takeover` within a short window indicates a storm. A healthy set shows at most an isolated election after a planned restart.

### Step 3: Measure heartbeat round-trip and inter-node reachability

```bash
mongosh --quiet --eval 'rs.status().members.forEach(m => print(
  m.name, "pingMs="+(m.pingMs ?? "self"),
  "lastHeartbeatRecv="+(m.lastHeartbeatRecv ?? "-")))'
```

Expected output: `pingMs` per member. Replica members heartbeat every 2s and mark a peer inaccessible if no response within ~10s. Healthy intra-DC `pingMs` is single/low-double digits. Values of hundreds-to-thousands of ms, or `lastHeartbeatRecv` stale by >10s, indicate a network partition or saturation.

### Step 4: Measure replication lag against the primary

```bash
mongosh --quiet --eval 'rs.printSecondaryReplicationInfo()'
```

Expected output: per-secondary `syncedTo` timestamp and "N secs (M hrs) behind the primary". Near-zero is healthy. Tens of seconds or more means a secondary cannot keep up; lagged secondaries are ineligible to quickly become primary and amplify election churn.

### Step 5: Inspect election/priority/votes configuration and timeouts

```bash
mongosh --quiet --eval 'const c=rs.conf();
  print("electionTimeoutMillis="+c.settings.electionTimeoutMillis,
        "heartbeatTimeoutSecs="+c.settings.heartbeatTimeoutSecs,
        "catchUpTimeoutMillis="+c.settings.catchUpTimeoutMillis);
  c.members.forEach(m=>print(m.host,"priority="+m.priority,"votes="+m.votes))'
```

Expected output: cluster-wide timeouts plus `priority`/`votes` per member. Default `electionTimeoutMillis` is 10000. Multiple members with equal high `priority`, or a member with high priority but chronic lag, drives repeated priority takeovers.

### Step 6: Check oplog window and replication throughput

```bash
mongosh --quiet --eval 'db.getReplicationInfo()' && mongostat --rowcount 5
```

Expected output: `db.getReplicationInfo()` returns `timeDiff`/`timeDiffHours` (the oplog window) and `tFirst`/`tLast`. A window shorter than the longest expected secondary downtime (target ≥24h, often 72h) means secondaries fall off the oplog and require resync. `mongostat` columns (`insert/query/update`, `dirty`, `used`, `repl`) reveal write bursts and cache pressure on the primary.

## Causes

### Cause A: Network partition / packet loss isolates the primary from the voting majority
**Statement:** Intermittent network loss or latency between nodes prevents the primary from reaching a majority of voting members within `electionTimeoutMillis`, so it repeatedly steps down while a partitioned member calls a competing election.
**Chain:**
- root: lossy/high-latency link drops heartbeats between the primary and ≥1 voting member
- s1: primary cannot confirm it sees a majority of voting nodes within `electionTimeoutMillis`
- s2: primary steps down to SECONDARY and an eligible member starts an election
- D: writes fail (`not primary` / `PrimarySteppedDown`) as primacy oscillates
**Indicators:**
- root: [Step 3] `pingMs` in the hundreds-to-thousands of ms or `lastHeartbeatRecv` stale beyond ~10s for at least one member
- s1: [Step 2] log lines `Couldn't get a heartbeat response` / heartbeat marking a member inaccessible
- s2: [Step 2] log line `Stepping down from primary`
- D: [Symptom] drivers report `not primary` / code 189 during the partition window
**Interventions:**
- **remediation** (root): Restore the network path between members (fix the firewall/route/MTU/saturated link) and confirm bidirectional reachability, then re-verify heartbeat health.

  ```bash
  mtr --report --report-cycles 20 <peer-member-host>
  mongosh --quiet --eval 'rs.status().members.forEach(m=>print(m.name,"pingMs="+(m.pingMs ?? "self")))'
  ```

  **Verification:** Re-run Step 3 — all `pingMs` back to single/low-double digits and no member stuck `health=0`; Step 2 shows no new election lines.
- **defensive_fix** (s1): If transient cross-DC latency is expected, raise `electionTimeoutMillis` (and matching driver `serverSelectionTimeoutMS`) so brief blips no longer trigger failover.

  ```bash
  mongosh --quiet --eval 'const c=rs.conf(); c.settings.electionTimeoutMillis=20000; rs.reconfig(c)'
  ```

  **Verification:** `rs.conf().settings.electionTimeoutMillis` returns the new value and the election rate in Step 2 drops to zero over a sustained window.

### Cause B: Oplog lag from write bursts or slow secondary I/O makes secondaries fall behind
**Statement:** Sustained write throughput or bulk loads exceed the secondaries' apply rate (often due to slow disk flush or cache eviction contention), driving replication lag that destabilizes who can hold or win primacy.
**Chain:**
- root: write/bulk-load rate on the primary exceeds secondary apply throughput (disk flush or WiredTiger cache eviction can't keep up)
- s1: secondaries' `optimeDate` falls behind the primary (replication lag grows)
- s2: lagged secondaries are ineligible to quickly become primary, so failovers stall and re-trigger elections
- D: writes are repeatedly rejected as primacy churns and majority write concern blocks
**Indicators:**
- s1: [Step 4] `rs.printSecondaryReplicationInfo()` reports a secondary tens of seconds or more behind the primary
- root: [Step 6] `mongostat` shows high `dirty`/`used` cache and large insert/update rates on the primary
- s2: [Step 1] `rs.status()` shows secondaries flipping between `SECONDARY` and `RECOVERING`
**Interventions:**
- **remediation** (root): Relieve the apply bottleneck — provision faster/dedicated disk for secondaries, increase WiredTiger cache, and throttle/batch bulk writes (smaller `batchSize`, pacing). Re-check lag after the burst clears.

  ```bash
  mongosh --quiet --eval 'rs.printSecondaryReplicationInfo()'
  ```

  **Verification:** Re-run Step 4 — every secondary reports near 0 secs behind the primary and stays there through the next write burst.
- **mitigation** (s2): Temporarily prevent a chronically lagged secondary from calling elections by freezing it, buying time to fix I/O without it triggering more storms.

  ```bash
  mongosh --quiet --eval 'rs.freeze(120)'
  ```

  **Risk:** A frozen member won't stand for election, reducing redundancy if the current primary fails during the window. **Duration:** ≤120s as set; re-issue if needed. **Verification:** Step 2 shows no new `Starting an election` lines while frozen.

### Cause C: Equal/high member priorities trigger repeated priority takeovers
**Statement:** Two or more members are configured with equal high `priority`, so MongoDB's best-effort priority-takeover logic keeps forcing the current primary to step down in favor of another high-priority member, producing chronic flapping.
**Chain:**
- root: ≥2 members configured with equal high `priority` (or a high-priority member that intermittently becomes reachable)
- s1: the highest-priority available member schedules a priority takeover and forces the current primary down
- s2: primacy moves, then the cycle repeats whenever priorities tie or reachability flaps
- D: writes fail during each takeover as the primary steps down
**Indicators:**
- root: [Step 5] `rs.conf()` shows multiple members with the same high `priority` value
- s1: [Step 2] log line `Scheduling priority takeover`
- D: [Symptom] writes intermittently return `PrimarySteppedDown` aligned with takeover log timestamps
**Interventions:**
- **remediation** (root): Differentiate priorities so one member is the clear preferred primary; give chronically lagged or remote members lower priority (0 to bar primacy). Apply via a single `rs.reconfig()`.

  ```bash
  mongosh --quiet --eval 'const c=rs.conf();
    c.members[0].priority=5; c.members[1].priority=1; c.members[2].priority=1;
    rs.reconfig(c)'
  ```

  **Verification:** Re-run Step 5 — priorities are distinct; Step 2 shows no further `Scheduling priority takeover` lines over a sustained window.

### Cause D: Lost voting majority (member down or non-voting misconfiguration) prevents a stable primary
**Statement:** The set cannot continuously hold a majority of votes — because a voting member is down or `votes`/`priority` were misconfigured (e.g., a needed member set to `votes: 0`) — so no node can sustain primacy and elections repeat without resolution.
**Chain:**
- root: a voting member is down or `votes`/`priority` is misconfigured, so the reachable voting members can't form a stable majority
- s1: an elected primary cannot continuously confirm a voting majority and steps down
- s2: elections re-run without producing a durable primary (or the set goes read-only)
- D: writes fail because there is no stable writable primary
**Indicators:**
- root: [Step 1] one or more voting members show `health=0` / `state=(not reachable/healthy)`
- root: [Step 5] `rs.conf()` shows a required member with `votes=0` (or an even voting-member count enabling ties)
- s2: [Step 2] repeated `Starting an election, since we've seen no PRIMARY` with no lasting `transition to PRIMARY`
**Interventions:**
- **remediation** (root): Restore the down voting member (or add an arbiter/voting member to reach an odd voting count) and correct `votes`/`priority` so a majority is reachable.

  ```bash
  mongosh --quiet --eval 'const c=rs.conf();
    c.members[1].votes=1; c.members[1].priority=1; rs.reconfig(c)'
  systemctl start mongod   # bring the downed voting member back online
  ```

  **Verification:** Re-run Step 1 — all voting members `health=1`, exactly one stable `PRIMARY`; Step 2 shows no new elections after recovery.

### Cause Z: Unidentified
**Statement:** The election storm does not match any cause above; an unknown trigger (driver retry storm, clock skew, WiredTiger stall, or undiagnosed kernel/network behavior) is destabilizing primacy.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot from every member and escalate to the database SME with the bundle.

  ```bash
  mongosh --quiet --eval 'printjson(rs.status()); printjson(rs.conf()); printjson(db.serverStatus().repl); rs.printSecondaryReplicationInfo()' > /tmp/mongo_election_snapshot.txt
  tail -n 500 /var/log/mongodb/mongod.log >> /tmp/mongo_election_snapshot.txt
  ```

  **Risk:** Snapshot is read-only and safe; it does not stop the storm. **Duration:** N/A — run immediately and escalate. **Verification:** `/tmp/mongo_election_snapshot.txt` contains `rs.status`, `rs.conf`, `repl` server status, and recent logs; attach to the SME ticket.

## Prevention

- Deploy an odd number of voting members (3, 5, or 7) so a majority is always well-defined; avoid even voting counts that allow ties.
- Assign distinct member `priority` values so there is one clear preferred primary; set remote/backup/analytics members to `priority: 0`, `votes: 0` (priority>0 members must have votes>0).
- Size the oplog to cover the longest expected secondary downtime — target ≥24h, commonly 72h — so brief outages resync via the oplog instead of full resync. Monitor `db.getReplicationInfo().timeDiffHours`.
- Alert on replication lag (`rs.printSecondaryReplicationInfo()` / monitoring) when any secondary exceeds a threshold (e.g., 10s) and on election frequency (`Starting an election` in logs).
- Tune `electionTimeoutMillis` to the network: keep the default 10000ms within a low-latency DC; raise it (and driver `serverSelectionTimeoutMS`) for cross-DC links to tolerate transient latency without failover. Median time to elect a new primary should stay under ~12s with defaults.
- Provision secondaries with disk/cache parity to the primary so they can keep up under write bursts; pace bulk loads.
- Monitor inter-node `pingMs` and packet loss; place voting members on reliable, low-latency links.

## Sources

- [Replica Set Elections — MongoDB Manual](https://www.mongodb.com/docs/manual/core/replica-set-elections/) — heartbeat interval (2s) / 10s inaccessibility, `electionTimeoutMillis` (10s default) trigger, network partition → primary step-down, priority best-effort takeover.
- [Self-Managed Replica Set Configuration — MongoDB Manual](https://www.mongodb.com/docs/manual/reference/replica-configuration/) — `priority`, `votes`, `settings.electionTimeoutMillis`, `heartbeatTimeoutSecs`, `catchUpTimeoutMillis` semantics and constraints.
- [Troubleshoot Replica Sets — MongoDB Manual](https://www.mongodb.com/docs/manual/tutorial/troubleshoot-replica-sets/) — checking replication lag and member status; secondary catch-up failure causes (slow disk flush, cache eviction contention, bulk writes).
- [rs.printSecondaryReplicationInfo() — MongoDB Manual](https://www.mongodb.com/docs/manual/reference/method/rs.printSecondaryReplicationInfo) — `syncedTo` and "secs behind the primary" output; delayed/negative value caveats.
- [rs.status() / replSetGetStatus — MongoDB Manual](https://www.mongodb.com/docs/manual/reference/method/rs.status) — `stateStr`, `health`, `optimeDate`, `lastHeartbeat`, `lastHeartbeatRecv`, `pingMs` field meanings.
- [Replica Set Oplog — MongoDB Manual](https://www.mongodb.com/docs/manual/core/replica-set-oplog/) — oplog window sizing (≥24h, often 72h) and resync-vs-window behavior; `db.getReplicationInfo()`.
- [Analyze MongoDB Performance — MongoDB Manual](https://www.mongodb.com/docs/manual/administration/analyzing-mongodb-performance/) — replication-lag definition and impact on primary eligibility; performance/lock analysis context.
- [MongoDB Diagnostics FAQ — MongoDB Manual](https://www.mongodb.com/docs/manual/faq/diagnostics/) — diagnostic command landscape (`serverStatus`, replication metrics, `mongostat`).
