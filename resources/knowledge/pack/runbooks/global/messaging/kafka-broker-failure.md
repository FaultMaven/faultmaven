---
id: "kafka-broker-failure"
title: "Kafka Broker Failure and Partition Unavailability"
domain: messaging
service: kafka
symptom_class: [service_unavailable]
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [kafka, broker, under-replicated-partitions, leader-election, isr, kraft, zookeeper]
difficulty: advanced
---

## Symptom Recognition

- `UnderReplicatedPartitions` JMX metric (`kafka.server:type=ReplicaManager,name=UnderReplicatedPartitions`) is non-zero on surviving brokers
- `OfflinePartitionsCount` JMX metric (`kafka.controller:type=KafkaController,name=OfflinePartitionsCount`) is non-zero on the active controller
- `ActiveControllerCount` (`kafka.controller:type=KafkaController,name=ActiveControllerCount`) briefly drops to 0 during controller re-election
- Producers receive `NotLeaderOrFollowerException`, `NetworkException`, or `TimeoutException` for partitions whose leader was on the failed broker
- Consumers see `coordinator load in progress` or `not coordinator` errors when the failed broker hosted the group coordinator
- `IsrShrinksPerSec` (`kafka.server:type=ReplicaManager,name=IsrShrinksPerSec`) spikes then returns to 0 as the cluster re-stabilises
- Broker process absent from `jps` output; systemd status shows `inactive (dead)` or `failed`
- `KafkaStorageException` or `IOException` appearing in the broker's `server.log` before process exit

## Applicability

Applies to Apache Kafka 2.8+ (ZooKeeper and KRaft mode) and Confluent Platform 6.0+. Requires SSH access to broker hosts and Kafka CLI tools (`kafka-topics.sh`, `kafka-broker-api-versions.sh`, `kafka-log-dirs.sh`, `kafka-leader-election.sh`, `kafka-reassign-partitions.sh`, `kafka-configs.sh`). For KRaft-mode clusters, `kafka-metadata.sh` replaces ZooKeeper CLI commands for cluster state inspection. JMX access or a Prometheus/Grafana stack is needed for metric-based Causes.

## Diagnostic Steps

### Step 1: Identify the failed broker

```bash
# List all brokers registered in the cluster (ZooKeeper mode)
kafka-broker-api-versions.sh \
  --bootstrap-server kafka1:9092,kafka2:9092,kafka3:9092 2>&1 \
  | grep -E "id [0-9]+"

# KRaft mode: inspect cluster metadata snapshot
kafka-metadata.sh \
  --snapshot /var/kafka-logs/__cluster_metadata-0/00000000000000000000.log \
  --broker-list

# Check process and service state on the suspected host
systemctl status kafka
jps | grep Kafka
```

Expected output: the failed broker ID is absent from the registered-broker list; `systemctl status kafka` shows `inactive (dead)` or `failed`; `jps` returns no Kafka process.

### Step 2: Check under-replicated and offline partitions

```bash
# Under-replicated partitions (leader alive, follower missing)
kafka-topics.sh --bootstrap-server kafka1:9092 \
  --describe --under-replicated-partitions

# Offline partitions (no leader available)
kafka-topics.sh --bootstrap-server kafka1:9092 \
  --describe --unavailable-partitions

# Leader distribution across brokers
kafka-topics.sh --bootstrap-server kafka1:9092 --describe \
  | grep -oP 'Leader: \K[0-9]+' | sort | uniq -c | sort -rn
```

Expected output: under-replicated lines list the failed broker ID in `Replicas` but not in `Isr`; offline partition lines show `Leader: none`; leader distribution shows the failed broker absent.

### Step 3: Inspect broker logs for the failure trigger

```bash
# Last 100 lines of the Kafka server log on the failed host
tail -100 /var/log/kafka/server.log

# Search for fatal signals
grep -E "FATAL|OutOfMemoryError|KafkaStorageException|IOException|broker leaving" \
  /var/log/kafka/server.log | tail -30

# Check for OOM-kill in kernel ring buffer
dmesg | grep -i "oom\|killed process" | tail -10
journalctl -k --since "1 hour ago" | grep -i "oom\|killed"
```

Expected output: `OutOfMemoryError` in server.log indicates JVM heap exhaustion; `KafkaStorageException` indicates a disk failure; `dmesg` shows `Out of memory: Kill process <pid> (java)` for kernel OOM-kills.

### Step 4: Assess disk health on the failed broker

```bash
# Filesystem utilisation on Kafka log directories
df -h /var/kafka-logs

# Kernel-level disk errors
dmesg | grep -iE "error|fault|failed" | grep -iE "sd|nvme|disk" | tail -10

# I/O saturation
iostat -xz 1 3

# Per-log-directory health reported by the broker
kafka-log-dirs.sh \
  --bootstrap-server kafka1:9092 \
  --broker-list <failed-broker-id> --describe 2>&1 \
  | grep -i "error\|offline"
```

Expected output: `df -h` shows available space; `%util` in `iostat` below 90%; no disk errors in `dmesg`; `kafka-log-dirs.sh` returns `LogDirResult` with an `error` field if the directory has failed.

### Step 5: Check controller status and ISR metrics

```bash
# Active controller (ZooKeeper mode)
echo dump | nc localhost 2181 | grep controller

# Active controller (KRaft mode)
kafka-metadata.sh \
  --snapshot /var/kafka-logs/__cluster_metadata-0/00000000000000000000.log \
  --controller

# Poll ISR shrink/expand and offline partition counters (JMX via curl+jolokia or jmxterm)
# kafka.server:type=ReplicaManager,name=IsrShrinksPerSec
# kafka.server:type=ReplicaManager,name=IsrExpandsPerSec
# kafka.controller:type=KafkaController,name=OfflinePartitionsCount
```

Expected output: exactly one broker reports as active controller; `IsrShrinksPerSec` spikes then returns to 0 as the cluster stabilises; `OfflinePartitionsCount` returns to 0 once leader election completes for all partitions.

### Step 6: Count partitions per broker to assess overload risk

```bash
# Total partition replicas assigned to each broker
kafka-topics.sh --bootstrap-server kafka1:9092 --describe \
  | grep -oP 'Replicas: \K[0-9,]+' \
  | tr ',' '\n' | sort | uniq -c | sort -rn

# Check file-descriptor limit vs open files
ssh <failed-broker-host> "cat /proc/\$(pgrep -f kafka.Kafka)/limits | grep 'open files'"
ssh <failed-broker-host> "ls /proc/\$(pgrep -f kafka.Kafka)/fd 2>/dev/null | wc -l"
```

Expected output: ZooKeeper-mode clusters should show ≤4,000 partition replicas per broker; KRaft-mode ≤14,000. Open file count approaching the `Max open files` limit indicates file-descriptor exhaustion.

## Causes

### Cause A: JVM Heap Exhaustion (OOM-Kill)

**Statement:** The broker's JVM ran out of heap memory, causing the Linux kernel to send SIGKILL to the process.

**Mechanism:** Kafka brokers with misconfigured heap (`KAFKA_HEAP_OPTS`) or an unusually large partition count exhaust the JVM heap and trigger full GC. When GC cannot free sufficient memory the JVM throws `OutOfMemoryError`; if the Java process RSS exceeds the cgroup or host memory limit the kernel OOM-killer intervenes. Either path terminates the broker process abruptly.

**Indicator:**

- [Step 3] `OutOfMemoryError` present in `/var/log/kafka/server.log`
- [Step 3] `Out of memory: Kill process` matching the Kafka PID in `dmesg` output

<!-- match: {"step": 3, "predicate": "contains", "target": "OutOfMemoryError"} -->

**Mitigation:**

- **Risk:** Restarting without increasing heap causes an immediate repeat OOM; follower catch-up after restart generates replication traffic spike.
- **Command:**

  ```bash
  # Edit the systemd override or kafka-env.sh to raise heap
  systemctl edit kafka
  # Add under [Service]:
  # Environment="KAFKA_HEAP_OPTS=-Xms6g -Xmx6g"
  systemctl daemon-reload
  systemctl start kafka
  ```

- **Duration:** Restart takes 30–120 s; ISR catch-up minutes to hours depending on lag volume.

**Resolution:**

```bash
# Durable fix: set heap in kafka-env.sh or systemd drop-in, add G1GC flags
# In /opt/kafka/bin/kafka-env.sh or equivalent:
# export KAFKA_HEAP_OPTS="-Xms6g -Xmx6g"
# export KAFKA_JVM_PERFORMANCE_OPTS="-XX:+UseG1GC -XX:MaxGCPauseMillis=20 \
#   -XX:InitiatingHeapOccupancyPercent=35 -XX:+ExplicitGCInvokesConcurrent \
#   -Xlog:gc*:file=/var/log/kafka/gc.log:time,tags:filecount=10,filesize=100m"
systemctl restart kafka
```

- **Impact:** Requires broker restart (rolling — one broker at a time in a healthy cluster). Cluster-wide: if heap is set too high it can cause swap pressure on the host.
- **Rollback:** Revert heap value in `kafka-env.sh` / systemd drop-in and `systemctl restart kafka`.

**Verification:** After restart confirm `UnderReplicatedPartitions` returns to 0 within 10 minutes. Monitor `jstat -gcutil <pid> 5s` to confirm GC overhead stays below 5%.

---

### Cause B: Disk Full or I/O Error on Log Directory

**Statement:** The broker's Kafka log directory filesystem reached 100% utilisation or a hardware-level I/O error made the directory unwritable, causing `KafkaStorageException`.

**Mechanism:** Kafka continuously appends messages to log segment files. When the underlying filesystem is full or returns I/O errors, segment writes fail with `KafkaStorageException`. The broker marks the affected log directory as offline (JBOD mode) or shuts down entirely (single log directory). Partitions whose data lives on that directory become unavailable.

**Indicator:**

- [Step 3] `KafkaStorageException` present in `/var/log/kafka/server.log`
- [Step 4] `df -h` shows 100% utilisation on `/var/kafka-logs`
- [Step 4] `dmesg` contains disk error strings (`I/O error`, `failed command`)

<!-- match: {"step": 3, "predicate": "contains", "target": "KafkaStorageException"} -->
<!-- match: {"step": 4, "predicate": "threshold", "target": "disk_use_pct", "op": ">=", "value": 100} -->

**Mitigation:**

- **Risk:** Deleting log segments causes data loss if retention is still needed. Expanding the volume may not be instant on cloud providers.
- **Command:**

  ```bash
  # Identify largest topic log directories
  du -sh /var/kafka-logs/*/ | sort -rh | head -20

  # Reduce retention on high-volume topics to free space (immediate)
  kafka-configs.sh --bootstrap-server kafka1:9092 \
    --entity-type topics --entity-name <topic-name> \
    --alter --add-config retention.ms=3600000
  ```

- **Duration:** Log cleaner purges old segments within minutes of the retention change; verify with `du`.

**Resolution:**

```bash
# Durable: expand the volume or add a JBOD log directory, then restore retention
# 1. Expand EBS / add disk, mount at /var/kafka-logs2
# 2. Add to server.properties:  log.dirs=/var/kafka-logs,/var/kafka-logs2
# 3. Restart broker
# 4. Restore topic retention to original value after space reclaimed:
kafka-configs.sh --bootstrap-server kafka1:9092 \
  --entity-type topics --entity-name <topic-name> \
  --alter --delete-config retention.ms
```

- **Impact:** Adding a `log.dirs` entry requires broker restart; Kafka automatically rebalances new partitions across available directories.
- **Rollback:** Remove the new directory from `log.dirs` and restart (only safe if no partitions have been assigned to it yet).

**Verification:** `df -h /var/kafka-logs` shows ≥20% free; `kafka-log-dirs.sh` returns no `error` fields; `UnderReplicatedPartitions` returns to 0.

---

### Cause C: Network Partition Isolating the Broker

**Statement:** A network failure severed the broker's connectivity to other brokers and to ZooKeeper (or the KRaft quorum), causing the cluster to treat it as dead even though the process was still running.

**Mechanism:** Kafka brokers maintain heartbeats to ZooKeeper (ZooKeeper mode) or to the KRaft quorum controller (KRaft mode). If the broker cannot reach its peers within `zookeeper.session.timeout.ms` (default 18 s) or the KRaft equivalent `replica.lag.time.max.ms`, the cluster removes the broker from the ISR and triggers leader election without the isolated broker actually crashing. Network partitions can be caused by switch failures, NIC bonding misconfiguration, or firewall rule changes.

**Indicator:**

- [Step 1] Broker process is running (`jps` shows the PID, `systemctl status kafka` shows active) but absent from the registered broker list
- [Step 3] `server.log` contains `ZooKeeper session expired` or `Lost connection to ZooKeeper` or KRaft `Disconnected from controller quorum`

<!-- match: {"step": 3, "predicate": "contains", "target": "ZooKeeper session expired"} -->

**Mitigation:**

- **Risk:** Reconnecting the isolated broker while its data is stale may trigger ISR divergence for any writes that committed to only the ISR without the isolated broker.
- **Command:**

  ```bash
  # Verify connectivity from the isolated broker to peer brokers
  nc -zv kafka2 9092
  nc -zv zookeeper1 2181

  # Test MTU and packet loss
  ping -c 20 -s 8192 kafka2
  ```

- **Duration:** Once network is restored the broker re-registers within one ZooKeeper session timeout cycle (~18 s).

**Resolution:**

```bash
# Restore network path (switch/firewall/NIC fix — environment-specific)
# Then restart the broker to force a clean reconnection:
systemctl restart kafka
# Verify re-registration:
kafka-broker-api-versions.sh --bootstrap-server kafka1:9092 2>&1 | grep "<broker-hostname>"
```

- **Impact:** Broker restart is safe in a cluster with RF≥2 and min.insync.replicas properly set.
- **Rollback:** Not applicable — network fix is the only path.

**Verification:** `kafka-broker-api-versions.sh` shows the broker re-registered; `UnderReplicatedPartitions` returns to 0 as the recovered broker re-joins all ISR sets.

---

### Cause D: GC Pause Exceeding ZooKeeper Session Timeout

**Statement:** A prolonged stop-the-world GC pause caused the broker to miss ZooKeeper heartbeats, triggering a session expiry and broker eviction while the process was still alive.

**Mechanism:** During a GC pause the JVM halts all application threads, including the ZooKeeper client heartbeat thread. If the pause exceeds `zookeeper.session.timeout.ms` (default 18 s), ZooKeeper expires the ephemeral session, removes the broker from the cluster metadata, and triggers leader election. The broker then sees its session is gone and shuts itself down. This is common when the heap is too large for G1GC or when CMS is used on multi-GB heaps.

**Indicator:**

- [Step 3] `server.log` contains `ZooKeeper session expired` or `Expiring session` immediately following GC log entries
- [Step 3] GC log at `/var/log/kafka/gc.log` shows pause times exceeding 10 s

<!-- match: {"step": 3, "predicate": "contains", "target": "Expiring session"} -->

**Mitigation:**

- **Risk:** Increasing `zookeeper.session.timeout.ms` delays genuine failure detection. Switching GC flags requires a restart.
- **Command:**

  ```bash
  # Check worst-case GC pause in gc.log
  grep -oP 'Pause.*? \K[0-9]+\.[0-9]+ms' /var/log/kafka/gc.log | sort -n | tail -5

  # Restart with G1GC and a pause target (edit kafka-env.sh first)
  systemctl restart kafka
  ```

- **Duration:** GC tuning takes effect immediately after restart.

**Resolution:**

```bash
# In kafka-env.sh, replace CMS/ParallelGC with G1GC:
# export KAFKA_JVM_PERFORMANCE_OPTS="-XX:+UseG1GC -XX:MaxGCPauseMillis=20 \
#   -XX:InitiatingHeapOccupancyPercent=35 \
#   -Xlog:gc*:file=/var/log/kafka/gc.log:time,tags:filecount=10,filesize=100m"
# In server.properties, increase session timeout if cluster topology warrants it:
# zookeeper.session.timeout.ms=30000
systemctl restart kafka
```

- **Impact:** Broker restart required. `zookeeper.session.timeout.ms` change delays failure detection by the delta (e.g., 18 s → 30 s adds 12 s to detection time).
- **Rollback:** Revert `kafka-env.sh` GC flags and `server.properties` timeout value; restart.

**Verification:** Monitor `/var/log/kafka/gc.log` for 24 h post-change — GC pause times should stay below `MaxGCPauseMillis`. No further `ZooKeeper session expired` entries.

---

### Cause E: Partition Count Overload and File-Descriptor Exhaustion

**Statement:** The broker hosted too many partition replicas relative to its OS file-descriptor limit, causing the broker to fail when it could not open new log segment files.

**Mechanism:** Each Kafka partition replica requires at least two open file descriptors (the active log segment and its index file). When the number of partition replicas on a broker approaches the OS `nofile` limit (often 65535 by default but may be lower), the broker fails to open new segment files, logs `Too many open files` exceptions, and eventually shuts down. The theoretical maximum is approximately 4,000 replicas per broker in ZooKeeper mode (due to ZooKeeper watch limits) and 14,000 in KRaft mode (memory-bound).

**Indicator:**

- [Step 6] Partition replica count on the failed broker is near or above 4,000 (ZooKeeper) or 14,000 (KRaft)
- [Step 3] `server.log` contains `Too many open files`

<!-- match: {"step": 3, "predicate": "contains", "target": "Too many open files"} -->

**Mitigation:**

- **Risk:** Reassigning partitions generates significant inter-broker replication traffic; throttle appropriately.
- **Command:**

  ```bash
  # Raise OS file-descriptor limit for the Kafka process (immediate, no restart)
  # Add to /etc/security/limits.d/kafka.conf:
  # kafka soft nofile 1000000
  # kafka hard nofile 1000000
  # Or for systemd-managed service:
  systemctl edit kafka
  # Add under [Service]: LimitNOFILE=1000000
  systemctl daemon-reload && systemctl restart kafka
  ```

- **Duration:** Limit change takes effect at next process start.

**Resolution:**

```bash
# After raising the FD limit, reassign excess partitions to other brokers:
kafka-reassign-partitions.sh --bootstrap-server kafka1:9092 \
  --topics-to-move-json-file topics.json \
  --broker-list "1,2,4" --generate > reassignment.json

kafka-reassign-partitions.sh --bootstrap-server kafka1:9092 \
  --reassignment-json-file reassignment.json \
  --execute --throttle 50000000

kafka-reassign-partitions.sh --bootstrap-server kafka1:9092 \
  --reassignment-json-file reassignment.json --verify
```

- **Impact:** Partition reassignment is non-disruptive (leaders remain live on other brokers) but consumes 50 MB/s inter-broker bandwidth per the throttle setting.
- **Rollback:** Revert the reassignment JSON with broker IDs swapped if needed; re-execute.

**Verification:** `kafka-topics.sh --describe` shows the formerly overloaded broker with fewer replicas; `ls /proc/<pid>/fd | wc -l` is well below `LimitNOFILE`.

---

### Cause F: Unclean Shutdown or Corrupted Log Segment During Rolling Upgrade

**Statement:** A broker that was not cleanly shut down during a rolling upgrade left partially written log segments, causing log recovery to fail or take excessively long on restart.

**Mechanism:** Kafka marks broker shutdown as "clean" when `controlled.shutdown.enable=true` and the controller successfully migrates all partition leaders away before the JVM exits. An unclean shutdown (SIGKILL, power loss, or `controlled.shutdown.max.retries` exhausted) leaves the `.log` suffix files without a corresponding `.timeindex` or `.index`, forcing the broker into a potentially lengthy log recovery scan on next startup. In extreme cases, a corrupt segment causes `CorruptRecordException` and prevents the broker from coming online.

**Indicator:**

- [Step 1] Broker was last stopped without `controlled.shutdown.enable=true` or during a failed rolling upgrade
- [Step 3] `server.log` contains `CorruptRecordException` or `Recovering unflushed producer state`

<!-- match: {"step": 3, "predicate": "contains", "target": "CorruptRecordException"} -->

**Mitigation:**

- **Risk:** Deleting a corrupt segment causes data loss for messages in that segment. Only proceed if the segment is confirmed corrupt and data loss is acceptable or recoverable from producers.
- **Command:**

  ```bash
  # Allow log recovery to complete (may take several minutes for large logs)
  systemctl start kafka
  # Watch for recovery progress in server.log
  journalctl -u kafka -f | grep -i "recover\|Loading\|corrupt"
  ```

- **Duration:** Log recovery typically takes 1–10 minutes per log directory; corrupt-segment deletion is near-instant.

**Resolution:**

```bash
# Durable: ensure controlled shutdown is enabled in server.properties:
# controlled.shutdown.enable=true
# controlled.shutdown.max.retries=3
# controlled.shutdown.retry.backoff.ms=5000
# For rolling upgrades, wait for UnderReplicatedPartitions=0 before shutting down each broker:
watch -n 5 'kafka-topics.sh --bootstrap-server kafka1:9092 \
  --describe --under-replicated-partitions | wc -l'
```

- **Impact:** `controlled.shutdown.enable` is a per-broker `server.properties` change; restart required to apply if not already set.
- **Rollback:** Not applicable — enabling controlled shutdown has no negative effects.

**Verification:** Broker starts and joins the cluster with `UnderReplicatedPartitions` returning to 0; no `CorruptRecordException` in `server.log` for new segments.

---

### Cause Z: Unidentified Broker Failure

**Statement:** The broker went offline for a reason not deterministically identified by the preceding diagnostic steps.

**Mechanism:** Not applicable — root cause remains unknown. [Default]

**Indicator:**

- [Default] None of Causes A–F indicators match. Broker offline confirmed by Step 1; under-replicated partitions confirmed by Step 2; Steps 3–6 yield no conclusive evidence.

**Mitigation:**

- **Risk:** Restarting a broker whose failure cause is unknown may result in repeat failure.
- **Command:**

  ```bash
  # Collect a full diagnostic bundle before attempting restart
  # 1. Capture broker thread dump if process still running
  kill -3 $(pgrep -f kafka.Kafka) && journalctl -u kafka --since "2 hours ago" > /tmp/kafka-diagnostics.txt
  # 2. Capture heap histogram if OOM suspected
  jmap -histo $(pgrep -f kafka.Kafka) >> /tmp/kafka-diagnostics.txt 2>&1
  # 3. Attempt restart and observe logs
  systemctl start kafka
  journalctl -u kafka -f
  ```

- **Duration:** Diagnostic collection takes 2–5 minutes; restart outcome determines next step.

**Resolution:** Out of runbook scope — escalate to Kafka cluster owner with the diagnostic bundle from `/tmp/kafka-diagnostics.txt` and the full `server.log` from the time of failure.

**Verification:** `UnderReplicatedPartitions` returns to 0 after restart; no recurrence within the monitoring window.

## Prevention

- Set replication factor to 3 for all production topics to tolerate a single broker failure without data loss or partition unavailability.
- Set `min.insync.replicas=2` with `acks=all` (producers) so writes are acknowledged only after at least 2 replicas confirm, preventing silent data loss during ISR shrinkage.
- Keep `unclean.leader.election.enable=false` (default since Kafka 0.11) to prevent out-of-sync replicas from becoming leaders and causing data loss.
- Enable `controlled.shutdown.enable=true` (default) and confirm it is not overridden in environment-specific configs before any rolling maintenance.
- Alert on `UnderReplicatedPartitions > 0` for more than 5 minutes — any sustained non-zero value indicates a broker is absent from one or more ISR sets.
- Alert on `OfflinePartitionsCount > 0` on the active controller — offline partitions are immediately impacting producers and consumers.
- Alert on `ActiveControllerCount != 1` — a cluster without a controller cannot handle leader election.
- Configure JVM heap at 6 GB with G1GC (`-XX:+UseG1GC -XX:MaxGCPauseMillis=20`) for most broker workloads; raise to 8 GB for brokers with >2,000 partition replicas.
- Set `LimitNOFILE=1000000` in the systemd service unit; keep partition replicas per broker below 4,000 (ZooKeeper mode) or 14,000 (KRaft mode).
- Monitor disk utilisation; alert at 70% and 85% on Kafka log directories. Use JBOD (`log.dirs` with multiple paths) so a single disk failure does not bring down the entire broker.
- Run `kafka-leader-election.sh --election-type PREFERRED --all-topic-partitions` after any broker restart to rebalance partition leaders.
- Enable SMART monitoring on broker disks and integrate alerts with your on-call system.

## Sources

- [Apache Kafka Documentation — Operations](https://kafka.apache.org/documentation/#operations) — broker recovery procedures, partition reassignment, preferred leader election, controlled shutdown configuration
- [Confluent Platform Monitoring Guide](https://docs.confluent.io/platform/current/kafka/monitoring.html) — JMX metric paths for UnderReplicatedPartitions, OfflinePartitionsCount, ActiveControllerCount, IsrShrinksPerSec, and alerting thresholds
- [Confluent Platform Post-Deployment Operations](https://docs.confluent.io/platform/current/kafka/post-deployment.html) — broker replacement procedures, ISR catch-up, partition reassignment throttling, JBOD configuration
