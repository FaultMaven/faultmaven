---
id: kafka-broker-failure
title: "Kafka Broker Failure: Under-Replicated Partitions, Leader Election, and ISR Shrinkage"
domain: messaging
service: kafka
symptom_class:
  - service-unavailable
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kafka
  - broker
  - under-replicated-partitions
  - leader-election
  - isr
  - availability
difficulty: advanced
---

# Kafka Broker Failure

## Problem Definition

Applies to Apache Kafka 2.8+ (ZooKeeper and KRaft mode) and Confluent Platform 6.0+. Requires access to Kafka CLI tools (`kafka-metadata.sh`, `kafka-topics.sh`, `kafka-log-dirs.sh`), broker JMX metrics, and broker log directories. For KRaft-mode clusters, `kafka-metadata.sh` replaces ZooKeeper commands.

A Kafka broker failure occurs when one or more brokers in a cluster become unresponsive, shut down unexpectedly, or cannot serve partition leader or follower responsibilities. When a broker goes offline, partitions it led become temporarily unavailable until leader election completes, and partitions it replicated stop receiving updates, causing the In-Sync Replica (ISR) set to shrink.

**Symptoms and errors:**

- `UnderReplicatedPartitions` JMX metric rises above 0 across remaining brokers
- `ActiveControllerCount` drops to 0 briefly during controller failover (ZooKeeper mode) or shows leader election activity in KRaft logs
- Producers receive `NotLeaderOrFollowerException`, `NetworkException`, or `TimeoutException` for partitions previously led by the failed broker
- Consumers see `coordinator load in progress` or `not coordinator` errors if the failed broker hosted the group coordinator
- `OfflinePartitionsCount` on the controller is non-zero, indicating partitions with no available leader
- Broker process disappears from `jps` output or systemd shows `inactive (dead)` status
- `IsrShrinksPerSec` metric spikes as followers on the failed broker leave ISR sets
- Log directory errors: `KafkaStorageException` or `IOException` in broker logs indicating disk failure

**Common causes:**

- Out of memory (OOM) kill by the Linux kernel due to heap misconfiguration or GC pressure
- Disk failure or I/O saturation causing `KafkaStorageException` on log directories
- Network partition isolating the broker from the rest of the cluster
- Unclean shutdown during rolling upgrade or maintenance
- JVM crash due to a bug or native memory exhaustion
- ZooKeeper session expiry caused by GC pauses exceeding `zookeeper.session.timeout.ms`
- Resource exhaustion: too many partitions on a single broker exceeding file descriptor limits

## Diagnostic Steps

### Step 1: Identify the Failed Broker

Determines which broker is offline and when it was last seen by the cluster.

```bash
# List all brokers registered in the cluster (KRaft mode)
kafka-metadata.sh --snapshot /var/kafka-logs/__cluster_metadata-0/00000000000000000000.log \
  --broker-list

# ZooKeeper mode: list registered brokers
kafka-broker-api-versions.sh --bootstrap-server kafka1:9092,kafka2:9092,kafka3:9092 2>&1 | \
  grep -E "^[a-z].*:9092"

# Check broker process status on the suspected failed node
systemctl status kafka
journalctl -u kafka --since "30 minutes ago" --no-pager | tail -50
```

**Expected output:** The failed broker will be absent from the registered broker list. The systemd status will show `inactive (dead)` or `failed`. Journal logs will show the final error before shutdown.

**What this means:** If the broker is absent from the cluster but the process is running locally, it indicates a network partition or ZooKeeper/KRaft connectivity issue rather than a process crash.

### Step 2: Check Under-Replicated and Offline Partitions

Identifies the scope of impact — how many partitions lost leaders or have degraded replication.

```bash
# List all under-replicated partitions
kafka-topics.sh --bootstrap-server kafka1:9092 \
  --describe --under-replicated-partitions

# List partitions with no available leader (offline)
kafka-topics.sh --bootstrap-server kafka1:9092 \
  --describe --unavailable-partitions

# Check partition count per broker to assess load distribution
kafka-topics.sh --bootstrap-server kafka1:9092 --describe | \
  grep -oP 'Leader: \K[0-9]+' | sort | uniq -c | sort -rn
```

**Expected output:** Under-replicated partitions will list the failed broker ID in the `Replicas` column but not in the `Isr` column. Offline partitions will show `Leader: none` — these are completely unavailable.

**What this means:** Under-replicated partitions are still serving reads and writes (the leader is alive on another broker). Offline partitions have no leader and are completely unavailable. Offline partitions occur when all replicas were on the failed broker (replication factor too low) or when `unclean.leader.election.enable=false` and no ISR member is available.

### Step 3: Check Broker Logs for Root Cause

Examines the broker log to determine why it went offline.

```bash
# Check the last 100 lines of the Kafka server log on the failed broker
tail -100 /var/log/kafka/server.log

# Search for fatal errors
grep -E "FATAL|ERROR|OutOfMemoryError|KafkaStorageException|IOException" \
  /var/log/kafka/server.log | tail -30

# Check for OOM kill in kernel logs
dmesg | grep -i "oom\|killed process" | tail -10
journalctl -k --since "1 hour ago" | grep -i "oom\|killed"
```

**Expected output:** The broker log should show the triggering event — `OutOfMemoryError`, `KafkaStorageException`, or connection errors to ZooKeeper/KRaft controller. The kernel log will show `Out of memory: Kill process` with the Kafka process PID if it was OOM-killed.

**What this means:** `OutOfMemoryError` indicates JVM heap exhaustion — check heap settings and partition count. `KafkaStorageException` indicates disk failure. ZooKeeper connection loss indicates network issues or GC pauses exceeding the session timeout.

### Step 4: Check Disk Health on the Failed Broker

Verifies whether disk I/O issues caused or contributed to the failure.

```bash
# Check disk space on Kafka log directories
df -h /var/kafka-logs

# Check disk I/O errors in kernel messages
dmesg | grep -i "error\|fault\|failed" | grep -i "sd\|nvme\|disk" | tail -10

# Check I/O utilization
iostat -xz 1 3

# Verify all log directories are accessible
kafka-log-dirs.sh --bootstrap-server kafka1:9092 --broker-list <failed-broker-id> --describe
```

**Expected output:** `df -h` should show available space. `iostat` should show `%util` below 90%. `dmesg` should have no disk errors. If `kafka-log-dirs.sh` shows `LogDirResult` with error fields, that log directory has failed.

**What this means:** A disk at 100% utilization prevents the broker from writing segment files and causes `KafkaStorageException`. I/O errors in `dmesg` indicate hardware failure requiring disk replacement. If using JBOD with multiple log directories, only the failed directory is impacted (Kafka 1.1+).

### Step 5: Check Controller and Leader Election Status

Verifies that the controller is active and leader election completed for affected partitions.

```bash
# Check which broker is the active controller
kafka-metadata.sh --snapshot /var/kafka-logs/__cluster_metadata-0/00000000000000000000.log \
  --controller

# ZooKeeper mode: check controller
echo dump | nc localhost 2181 | grep controller

# Check ISR shrink/expand rates (JMX or Prometheus)
# kafka.server:type=ReplicaManager,name=IsrShrinksPerSec
# kafka.server:type=ReplicaManager,name=IsrExpandsPerSec
# kafka.controller:type=KafkaController,name=OfflinePartitionsCount
```

**Expected output:** Exactly one broker should be the active controller. After the failed broker goes offline, `IsrShrinksPerSec` should spike then return to 0 as the cluster stabilizes. `OfflinePartitionsCount` should return to 0 once leader election completes.

**What this means:** If `OfflinePartitionsCount` remains non-zero after several minutes, leader election failed for some partitions. This happens when the failed broker was the only replica (replication factor = 1) or all ISR members are offline and `unclean.leader.election.enable=false`.

## Mitigation

### Option 1: Restart the Failed Broker

**Risk:** Low if the broker was cleanly shut down or OOM-killed. The broker will rejoin the cluster and begin fetching missed data from leaders. Causes temporary replication traffic spike.

**Command:**

```bash
# If OOM-killed, increase heap before restart
export KAFKA_HEAP_OPTS="-Xms6g -Xmx6g"
systemctl start kafka
```

**Verify:**

```bash
# Confirm broker is registered
kafka-broker-api-versions.sh --bootstrap-server kafka1:9092 2>&1 | grep "<broker-hostname>"

# Confirm under-replicated partitions are decreasing
watch -n 5 'kafka-topics.sh --bootstrap-server kafka1:9092 --describe --under-replicated-partitions | wc -l'
```

**Duration:** Broker startup takes 30-120 seconds. ISR catch-up depends on data volume — minutes for light workloads, hours for heavily lagging replicas.

### Option 2: Reassign Partitions Away from the Failed Broker

**Risk:** Medium. If the broker cannot be recovered quickly, reassigning partitions to healthy brokers restores full replication. Generates significant inter-broker replication traffic.

**Command:**

```bash
# Generate a reassignment plan excluding the failed broker (ID=3)
kafka-reassign-partitions.sh --bootstrap-server kafka1:9092 \
  --topics-to-move-json-file topics.json \
  --broker-list "1,2,4" \
  --generate

# Execute the reassignment with throttle to avoid saturating the network
kafka-reassign-partitions.sh --bootstrap-server kafka1:9092 \
  --reassignment-json-file reassignment.json \
  --execute --throttle 50000000
```

**Verify:**

```bash
kafka-reassign-partitions.sh --bootstrap-server kafka1:9092 \
  --reassignment-json-file reassignment.json \
  --verify
```

**Duration:** Minutes to hours depending on data volume. A 50 MB/s throttle moves approximately 180 GB/hour.

### Option 3: Enable Unclean Leader Election (Emergency Only)

**Risk:** High. Enables leader election from out-of-sync replicas, which causes data loss for messages not yet replicated. Use only when availability is more critical than data consistency and offline partitions cannot wait for broker recovery.

**Command:**

```bash
# Enable unclean leader election for a specific topic
kafka-configs.sh --bootstrap-server kafka1:9092 \
  --entity-type topics --entity-name critical-topic \
  --alter --add-config unclean.leader.election.enable=true
```

**Verify:**

```bash
# Confirm the topic has a leader again
kafka-topics.sh --bootstrap-server kafka1:9092 --describe --topic critical-topic

# IMPORTANT: Revert after recovery
kafka-configs.sh --bootstrap-server kafka1:9092 \
  --entity-type topics --entity-name critical-topic \
  --alter --delete-config unclean.leader.election.enable
```

**Duration:** Immediate. Leader election occurs within seconds of the config change.

### Option 4: Replace the Broker with a New Instance

**Risk:** Medium. If hardware is permanently failed, provision a new broker with the same `broker.id`. Kafka will replicate data to the new broker automatically.

**Command:**

```bash
# On the new broker, set the same broker.id in server.properties
# broker.id=3
systemctl start kafka

# Trigger preferred leader election to rebalance leaders
kafka-leader-election.sh --bootstrap-server kafka1:9092 \
  --election-type PREFERRED --all-topic-partitions
```

**Verify:**

```bash
kafka-topics.sh --bootstrap-server kafka1:9092 --describe --under-replicated-partitions
# Should return no output once catch-up completes
```

**Duration:** Broker startup is immediate. Full data replication depends on volume — plan for hours on large clusters.

## Root Cause Resolution

**If** the broker was OOM-killed → Increase JVM heap size (`KAFKA_HEAP_OPTS`) to 6-8 GB. Each partition consumes approximately 10-20 MB of heap. Reduce partition counts on overloaded brokers or add brokers to the cluster. Set `vm.overcommit_memory=0` on the host to prevent kernel overcommit.

**If** a disk failure caused `KafkaStorageException` → Replace the failed disk. If using JBOD with multiple log directories (`log.dirs`), the broker can continue serving partitions on remaining directories. Enable SMART monitoring and set up disk health alerts. Consider RAID or multiple `log.dirs` for fault tolerance.

**If** a network partition isolated the broker → Investigate network infrastructure (switches, firewalls, NIC bonding). Check `zookeeper.session.timeout.ms` (default 18s) or KRaft heartbeat intervals. Increase session timeout if GC pauses cause false disconnections, but keep it short enough to detect genuine failures.

**If** GC pauses caused ZooKeeper session expiry → Use G1GC with `-XX:MaxGCPauseMillis=20`. Increase `zookeeper.session.timeout.ms` beyond the maximum observed GC pause. Enable GC logging with `-Xlog:gc*:file=/var/log/kafka/gc.log` to monitor pause times.

**If** the broker had too many partitions → Redistribute partitions across brokers using `kafka-reassign-partitions.sh`. Follow the limit of 4,000 partitions per broker (ZooKeeper mode) or 14,000 (KRaft mode). Consolidate over-partitioned topics.

**If** an unclean shutdown during rolling upgrade left corrupted state → Check for incomplete log segment recovery in `server.log`. Ensure rolling upgrades use `controlled.shutdown.enable=true` (default). Verify `controlled.shutdown.max.retries` and wait for ISR migration before shutting down each broker.

## Verification

After recovery, confirm full cluster health:

```bash
# 1. All brokers are registered
kafka-broker-api-versions.sh --bootstrap-server kafka1:9092 2>&1 | \
  grep -c ":9092"
# Should match the expected broker count

# 2. Zero under-replicated partitions
kafka-topics.sh --bootstrap-server kafka1:9092 --describe --under-replicated-partitions
# Should return no output

# 3. Zero offline partitions
kafka-topics.sh --bootstrap-server kafka1:9092 --describe --unavailable-partitions
# Should return no output

# 4. ISR sets are fully populated for critical topics
kafka-topics.sh --bootstrap-server kafka1:9092 --describe --topic <critical-topic>
# Replicas and Isr columns should list the same broker IDs

# 5. Producer and consumer connectivity test
kafka-console-producer.sh --bootstrap-server kafka1:9092 --topic test-topic <<< "health-check-$(date +%s)"
kafka-console-consumer.sh --bootstrap-server kafka1:9092 --topic test-topic \
  --from-beginning --max-messages 1 --timeout-ms 10000
```

## Prevention

- **Set replication factor to 3** for all production topics to tolerate single broker failures without data loss
- **Set `min.insync.replicas=2`** with `acks=all` to ensure writes are durable across at least 2 replicas before acknowledgment
- **Keep `unclean.leader.election.enable=false`** (default since Kafka 0.11) to prevent data loss from out-of-sync leader election
- **Enable `controlled.shutdown.enable=true`** to ensure leaders are migrated before a broker shuts down during maintenance
- **Distribute partitions evenly** across brokers and racks using `broker.rack` configuration and rack-aware replica assignment
- **Monitor `UnderReplicatedPartitions`** with alerting — any value above 0 for more than 5 minutes warrants investigation
- **Monitor `OfflinePartitionsCount`** on the controller — this should always be 0 in a healthy cluster
- **Limit partitions per broker** to 4,000 (ZooKeeper mode) or 14,000 (KRaft mode) to avoid resource exhaustion
- **Configure JVM heap** to 6 GB for most brokers with G1GC and `-XX:MaxGCPauseMillis=20`
- **Use JBOD with multiple log directories** so that a single disk failure does not take down the entire broker
- **Set up disk space alerts** at 70% and 85% thresholds on Kafka log directories
- **Automate preferred leader election** on a schedule to prevent leader imbalance after broker restarts

## Sources

- [Apache Kafka Documentation — Broker Configs](https://kafka.apache.org/documentation/#brokerconfigs)
- [Apache Kafka Documentation — Operations](https://kafka.apache.org/documentation/#operations)
- [Apache Kafka Documentation — Replication](https://kafka.apache.org/documentation/#replication)
- [Confluent Documentation — Kafka Post-Deployment and Broker Recovery](https://docs.confluent.io/platform/current/kafka/post-deployment.html)
- [Apache Kafka KIP-500 — Replace ZooKeeper with KRaft](https://cwiki.apache.org/confluence/display/KAFKA/KIP-500)
