---
id: "kafka-broker-failure"
title: "Kafka Broker Failure and Partition Unavailability"
domain: messaging
service: kafka
symptom_class: [service_unavailable]
severity: critical
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

**Chain:**
- root: misconfigured `KAFKA_HEAP_OPTS` or excessive partition count exhausts the JVM heap
- s1: full GC cannot reclaim enough memory; JVM throws `OutOfMemoryError` and/or RSS exceeds the cgroup/host memory limit
- s2: the kernel OOM-killer sends SIGKILL and the broker process terminates abruptly
- D: broker offline, partitions whose leaders lived on it become under-replicated or offline (Symptom Recognition)

**Indicators:**
- root: [Step 6] partition replica count on the broker is unusually high, or heap is undersized relative to load
- s1: [Step 3] `OutOfMemoryError` present in `/var/log/kafka/server.log`
- s2: [Step 3] `Out of memory: Kill process` matching the Kafka PID in `dmesg` output

**Interventions:**
- **remediation** (root): set heap in `kafka-env.sh` or a systemd drop-in and add G1GC flags so the heap is sized correctly and GC overhead stays bounded.

  ```bash
  # Durable fix: set heap in kafka-env.sh or systemd drop-in, add G1GC flags
  # In /opt/kafka/bin/kafka-env.sh or equivalent:
  # export KAFKA_HEAP_OPTS="-Xms6g -Xmx6g"
  # export KAFKA_JVM_PERFORMANCE_OPTS="-XX:+UseG1GC -XX:MaxGCPauseMillis=20 \
  #   -XX:InitiatingHeapOccupancyPercent=35 -XX:+ExplicitGCInvokesConcurrent \
  #   -Xlog:gc*:file=/var/log/kafka/gc.log:time,tags:filecount=10,filesize=100m"
  systemctl restart kafka
  ```

  **Verification:** after restart confirm `UnderReplicatedPartitions` returns to 0 within 10 minutes (re-run Step 2); monitor `jstat -gcutil <pid> 5s` to confirm GC overhead stays below 5%.
- **mitigation** (s1): raise the heap and restart the broker to clear the immediate OOM condition.

  ```bash
  # Edit the systemd override or kafka-env.sh to raise heap
  systemctl edit kafka
  # Add under [Service]:
  # Environment="KAFKA_HEAP_OPTS=-Xms6g -Xmx6g"
  systemctl daemon-reload
  systemctl start kafka
  ```

  **Risk:** restarting without increasing heap causes an immediate repeat OOM; follower catch-up after restart generates a replication traffic spike. **Duration:** restart takes 30–120 s; ISR catch-up minutes to hours depending on lag volume. **Verification:** broker re-registers (Step 1) and `UnderReplicatedPartitions` trends to 0 (Step 2).

### Cause B: Disk Full or I/O Error on Log Directory

**Statement:** The broker's Kafka log directory filesystem reached 100% utilisation or a hardware-level I/O error made the directory unwritable, causing `KafkaStorageException`.

**Chain:**
- root: the Kafka log directory filesystem fills to 100% or the disk returns hardware I/O errors
- s1: segment-file appends fail and the broker raises `KafkaStorageException`
- s2: the broker marks the affected log directory offline (JBOD) or shuts down entirely (single log directory)
- D: partitions hosted on that directory become unavailable; survivors report under-replicated/offline partitions (Symptom Recognition)

**Indicators:**
- root: [Step 4] `df -h` shows 100% utilisation on `/var/kafka-logs`, or `dmesg` contains disk error strings (`I/O error`, `failed command`)
- s1: [Step 3] `KafkaStorageException` present in `/var/log/kafka/server.log`
- s2: [Step 4] `kafka-log-dirs.sh` reports the directory with an `error`/`offline` field

**Interventions:**
- **remediation** (root): expand the volume or add a JBOD log directory, then restore the original retention once space is reclaimed.

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

  **Verification:** `df -h /var/kafka-logs` shows ≥20% free (Step 4); `kafka-log-dirs.sh` returns no `error` fields; `UnderReplicatedPartitions` returns to 0 (Step 2).
- **mitigation** (root): cut retention on the highest-volume topics so the log cleaner frees space immediately.

  ```bash
  # Identify largest topic log directories
  du -sh /var/kafka-logs/*/ | sort -rh | head -20

  # Reduce retention on high-volume topics to free space (immediate)
  kafka-configs.sh --bootstrap-server kafka1:9092 \
    --entity-type topics --entity-name <topic-name> \
    --alter --add-config retention.ms=3600000
  ```

  **Risk:** deleting log segments causes data loss if retention is still needed; expanding the volume may not be instant on cloud providers. **Duration:** the log cleaner purges old segments within minutes of the retention change; verify with `du`. **Verification:** `df -h` shows reclaimed space (Step 4).

### Cause C: Network Partition Isolating the Broker

**Statement:** A network failure severed the broker's connectivity to other brokers and to ZooKeeper (or the KRaft quorum), causing the cluster to evict it even though the process was still running.

**Chain:**
- root: a switch failure, NIC bonding misconfiguration, or firewall change severs the broker's network path to peers and ZooKeeper/KRaft
- s1: the broker misses heartbeats beyond `zookeeper.session.timeout.ms` (default 18 s) or the KRaft `replica.lag.time.max.ms` equivalent
- s2: the cluster removes the broker from ISR and triggers leader election without the broker actually crashing
- D: producers/consumers fail for the affected partitions; survivors report under-replicated partitions (Symptom Recognition)

**Indicators:**
- root: [Step 1] broker process is running (`jps` shows the PID, `systemctl status kafka` active) but absent from the registered broker list
- s1: [Step 3] `server.log` contains `ZooKeeper session expired`, `Lost connection to ZooKeeper`, or KRaft `Disconnected from controller quorum`

**Interventions:**
- **remediation** (root): restore the network path (switch/firewall/NIC fix — environment-specific), then restart the broker for a clean reconnection.

  ```bash
  # Restore network path (switch/firewall/NIC fix — environment-specific)
  # Then restart the broker to force a clean reconnection:
  systemctl restart kafka
  # Verify re-registration:
  kafka-broker-api-versions.sh --bootstrap-server kafka1:9092 2>&1 | grep "<broker-hostname>"
  ```

  **Verification:** `kafka-broker-api-versions.sh` shows the broker re-registered (Step 1); `UnderReplicatedPartitions` returns to 0 as the recovered broker re-joins all ISR sets (Step 2).
- **mitigation** (s1): confirm the connectivity break from the isolated broker so you target the right network fault before reconnecting.

  ```bash
  # Verify connectivity from the isolated broker to peer brokers
  nc -zv kafka2 9092
  nc -zv zookeeper1 2181

  # Test MTU and packet loss
  ping -c 20 -s 8192 kafka2
  ```

  **Risk:** reconnecting the isolated broker while its data is stale may trigger ISR divergence for writes that committed to only the ISR without it. **Duration:** once the network is restored the broker re-registers within one ZooKeeper session-timeout cycle (~18 s). **Verification:** `nc -zv` to peers and ZooKeeper succeeds.

### Cause D: GC Pause Exceeding ZooKeeper Session Timeout

**Statement:** A prolonged stop-the-world GC pause caused the broker to miss ZooKeeper heartbeats, triggering a session expiry and broker eviction while the process was still alive.

**Chain:**
- root: an oversized heap on G1GC, or CMS on a multi-GB heap, produces a long stop-the-world GC pause
- s1: the GC pause halts the ZooKeeper client heartbeat thread for longer than `zookeeper.session.timeout.ms` (default 18 s)
- s2: ZooKeeper expires the ephemeral session, removes the broker from metadata, and triggers leader election
- s3: the broker observes its session is gone and shuts itself down
- D: broker offline; survivors report under-replicated/offline partitions (Symptom Recognition)

**Indicators:**
- s1: [Step 3] `/var/log/kafka/gc.log` shows pause times exceeding 10 s
- s2: [Step 3] `server.log` contains `ZooKeeper session expired` or `Expiring session` immediately following GC log entries

**Interventions:**
- **remediation** (root): replace CMS/ParallelGC with G1GC and a pause target so GC pauses stay short; optionally widen the session timeout if topology warrants.

  ```bash
  # In kafka-env.sh, replace CMS/ParallelGC with G1GC:
  # export KAFKA_JVM_PERFORMANCE_OPTS="-XX:+UseG1GC -XX:MaxGCPauseMillis=20 \
  #   -XX:InitiatingHeapOccupancyPercent=35 \
  #   -Xlog:gc*:file=/var/log/kafka/gc.log:time,tags:filecount=10,filesize=100m"
  # In server.properties, increase session timeout if cluster topology warrants it:
  # zookeeper.session.timeout.ms=30000
  systemctl restart kafka
  ```

  **Verification:** monitor `/var/log/kafka/gc.log` for 24 h post-change — GC pause times stay below `MaxGCPauseMillis`; no further `ZooKeeper session expired` entries (Step 3).
- **defensive_fix** (s1): widen `zookeeper.session.timeout.ms` so a transient long pause no longer trips a session expiry while GC tuning takes effect.

  ```bash
  # Confirm worst-case GC pause, then restart with G1GC / higher timeout applied
  grep -oP 'Pause.*? \K[0-9]+\.[0-9]+ms' /var/log/kafka/gc.log | sort -n | tail -5
  systemctl restart kafka
  ```

  **Verification:** worst-case pause in `gc.log` is below the configured `zookeeper.session.timeout.ms`; no `Expiring session` entries recur (Step 3).

### Cause E: Partition Count Overload and File-Descriptor Exhaustion

**Statement:** The broker hosted too many partition replicas relative to its OS file-descriptor limit, causing it to fail when it could not open new log segment files.

**Chain:**
- root: the broker hosts too many partition replicas relative to its OS `nofile` limit (each replica needs ≥2 FDs: active log segment + index)
- s1: the broker cannot open new segment files and logs `Too many open files`
- s2: the broker shuts down once it can no longer service partitions
- D: broker offline; survivors report under-replicated/offline partitions (Symptom Recognition)

**Indicators:**
- root: [Step 6] partition replica count on the failed broker is near or above 4,000 (ZooKeeper) or 14,000 (KRaft)
- s1: [Step 3] `server.log` contains `Too many open files`

**Interventions:**
- **remediation** (root): after raising the FD limit, reassign excess partitions off the broker to bring its replica count back within bounds.

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

  **Verification:** `kafka-topics.sh --describe` shows the formerly overloaded broker with fewer replicas (Step 6); `ls /proc/<pid>/fd | wc -l` is well below `LimitNOFILE`.
- **mitigation** (s1): raise the OS file-descriptor limit so the broker can open new segments and come back up immediately.

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

  **Risk:** reassigning partitions generates significant inter-broker replication traffic; throttle appropriately. **Duration:** the limit change takes effect at next process start. **Verification:** `cat /proc/<pid>/limits | grep 'open files'` reflects the new limit and the broker stays up (Step 1).

### Cause F: Unclean Shutdown or Corrupted Log Segment During Rolling Upgrade

**Statement:** A broker that was not cleanly shut down during a rolling upgrade left partially written log segments, causing log recovery to fail or take excessively long on restart.

**Chain:**
- root: the broker was stopped without controlled shutdown (SIGKILL, power loss, or `controlled.shutdown.max.retries` exhausted), often during a failed rolling upgrade
- s1: `.log` files are left without a matching `.index`/`.timeindex`, forcing a lengthy log-recovery scan on next startup
- s2: in extreme cases a corrupt segment raises `CorruptRecordException` and prevents the broker from coming online
- D: broker fails to (re)join the cluster; partitions stay under-replicated/offline (Symptom Recognition)

**Indicators:**
- root: [Step 1] broker was last stopped without `controlled.shutdown.enable=true` or during a failed rolling upgrade
- s2: [Step 3] `server.log` contains `CorruptRecordException` or `Recovering unflushed producer state`

**Interventions:**
- **remediation** (root): ensure controlled shutdown is enabled and wait for `UnderReplicatedPartitions=0` before stopping each broker in a rolling upgrade.

  ```bash
  # Durable: ensure controlled shutdown is enabled in server.properties:
  # controlled.shutdown.enable=true
  # controlled.shutdown.max.retries=3
  # controlled.shutdown.retry.backoff.ms=5000
  # For rolling upgrades, wait for UnderReplicatedPartitions=0 before shutting down each broker:
  watch -n 5 'kafka-topics.sh --bootstrap-server kafka1:9092 \
    --describe --under-replicated-partitions | wc -l'
  ```

  **Verification:** broker starts and joins the cluster with `UnderReplicatedPartitions` returning to 0 (Step 2); no `CorruptRecordException` in `server.log` for new segments (Step 3).
- **mitigation** (s1): let log recovery complete on startup (or delete a confirmed-corrupt segment) so the broker can come online.

  ```bash
  # Allow log recovery to complete (may take several minutes for large logs)
  systemctl start kafka
  # Watch for recovery progress in server.log
  journalctl -u kafka -f | grep -i "recover\|Loading\|corrupt"
  ```

  **Risk:** deleting a corrupt segment causes data loss for messages in that segment; only proceed if the segment is confirmed corrupt and the loss is acceptable or recoverable from producers. **Duration:** log recovery typically takes 1–10 minutes per log directory; corrupt-segment deletion is near-instant. **Verification:** recovery completes and the broker registers (Step 1).

### Cause Z: Unidentified Broker Failure

**Statement:** The broker went offline for a reason not deterministically identified by the preceding diagnostic steps.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): collect a full diagnostic bundle (thread dump, heap histogram, logs), attempt a restart, and escalate to the Kafka cluster owner with the bundle and the failure-window `server.log`.

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

  **Risk:** restarting a broker whose failure cause is unknown may result in a repeat failure. **Duration:** diagnostic collection takes 2–5 minutes; the restart outcome determines the next step. **Verification:** `UnderReplicatedPartitions` returns to 0 after restart and no recurrence within the monitoring window; otherwise escalate to the Kafka cluster owner with `/tmp/kafka-diagnostics.txt` and the full `server.log`.

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
