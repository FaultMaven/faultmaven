---
id: kafka-consumer-lag
title: "Kafka Consumer Lag: Consumers Falling Behind Producers"
domain: messaging
service: kafka
symptom_class:
  - latency
  - throughput-degradation
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kafka
  - consumer
  - lag
  - consumer-group
  - rebalancing
  - throughput
difficulty: intermediate
---

# Kafka Consumer Lag

## Problem Definition

Applies to Apache Kafka 2.6+ consumers using the Java client or librdkafka-based clients. Requires access to Kafka CLI tools (`kafka-consumer-groups.sh`, `kafka-topics.sh`), consumer JMX metrics, and broker metrics. Consumer group management uses the `__consumer_offsets` internal topic.

Kafka consumer lag is the difference between the latest offset produced to a partition and the current committed offset of a consumer group. When consumers fall behind producers, the lag grows, meaning messages are produced faster than they are consumed and processed. Sustained lag indicates the consumer is unable to keep up with the incoming message rate, leading to increasing end-to-end latency and potential data loss if messages expire from the topic before being consumed.

**Symptoms and errors:**

- `records-lag-max` consumer JMX metric increases steadily
- `kafka.consumer:type=consumer-fetch-manager-metrics,client-id=*,topic=*,partition=*` lag metrics grow across partitions
- End-to-end message latency (produce-to-consume) increases beyond SLA thresholds
- Consumer group state shows `PreparingRebalance` or `CompletingRebalance` frequently
- `kafka-consumer-groups.sh --describe` shows increasing `LAG` column values
- Consumer logs show `poll() timeout` warnings or `max.poll.interval.ms exceeded` errors
- Consumer group members are repeatedly kicked out and re-joining (frequent rebalances)
- `CommitFailedException` when a consumer tries to commit offsets after being removed from the group

**Common causes:**

- Consumer processing logic is too slow (database writes, API calls, complex transformations) relative to the incoming message rate
- Insufficient consumer instances for the number of partitions — idle partitions accumulate lag
- Frequent consumer group rebalances causing stop-the-world pauses during which no messages are processed
- `max.poll.records` too high, causing processing to exceed `max.poll.interval.ms` and triggering rebalances
- `fetch.min.bytes` or `fetch.max.wait.ms` set too high, introducing unnecessary fetch latency
- Network issues between consumers and brokers causing slow fetches
- Unbalanced partition assignment — some consumers handle more partitions or higher-volume partitions than others
- Producer throughput spike (burst traffic) temporarily overwhelming consumer capacity
- GC pauses in the consumer JVM causing processing stalls

## Diagnostic Steps

### Step 1: Measure Current Consumer Lag

Determines the current lag per partition and identifies which partitions and consumer instances are falling behind.

```bash
# Describe the consumer group to see per-partition lag
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group

# Output columns: TOPIC, PARTITION, CURRENT-OFFSET, LOG-END-OFFSET, LAG, CONSUMER-ID, HOST, CLIENT-ID
```

**Expected output:** Each partition shows `LAG` as the difference between `LOG-END-OFFSET` and `CURRENT-OFFSET`. A healthy consumer has lag near 0 (single-digit). Lag in the thousands or millions indicates the consumer is significantly behind.

**What this means:** If lag is high on all partitions, the overall consumer throughput is insufficient. If lag is high on only some partitions, it suggests unbalanced partition assignment or partition-specific data patterns (e.g., hot keys) causing uneven processing load.

### Step 2: Check Consumer Group State and Rebalance Frequency

Determines whether the consumer group is stable or experiencing disruptive rebalances.

```bash
# Check consumer group state
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group --state

# Check consumer group members
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group --members --verbose

# Search consumer logs for rebalance events
grep -E "rebalanc|JoinGroup|SyncGroup|LeaveGroup|max.poll.interval" \
  /var/log/consumer/consumer.log | tail -30
```

**Expected output:** Group state should be `Stable`. Member list should show all expected consumer instances with partition assignments. If the state is `PreparingRebalance` or `CompletingRebalance`, a rebalance is in progress.

**What this means:** Frequent rebalances cause stop-the-world pauses where no consumer in the group processes messages. Each rebalance can take seconds to minutes, during which lag grows. The most common trigger is a consumer exceeding `max.poll.interval.ms` (default 300 seconds), which causes the broker to remove it from the group.

### Step 3: Check Consumer Processing Rate and Poll Interval

Determines whether individual consumers are processing messages fast enough to call `poll()` within the allowed interval.

```bash
# Check consumer JMX metrics (if exposed via Prometheus/JMX exporter)
# records-consumed-rate: messages processed per second
# records-lag: current lag per partition
# poll-idle-ratio-avg: fraction of time spent waiting in poll() — low values mean processing is the bottleneck

# Check if consumers are hitting max.poll.interval.ms
grep -E "max.poll.interval|CommitFailedException|member.*removed" \
  /var/log/consumer/consumer.log | tail -20
```

**Expected output:** `poll-idle-ratio-avg` above 0.5 means the consumer spends more time waiting for messages than processing — the consumer is not the bottleneck. Below 0.5 means processing dominates. `CommitFailedException` indicates the consumer was removed from the group due to slow processing.

**What this means:** A low `poll-idle-ratio-avg` confirms the consumer processing logic is the bottleneck. `CommitFailedException` means `max.poll.interval.ms` was exceeded — either reduce `max.poll.records` or optimize processing logic.

### Step 4: Check Producer Throughput for Spikes

Determines whether a sudden increase in production rate is causing the lag rather than a consumer problem.

```bash
# Check incoming message rate on the topic
kafka-run-class.sh kafka.tools.GetOffsetShell \
  --broker-list kafka1:9092 \
  --topic my-topic \
  --time -1 | awk -F: '{sum += $3} END {print "Total messages:", sum}'

# Check producer metrics via CloudWatch or Prometheus
# kafka.server:type=BrokerTopicMetrics,name=MessagesInPerSec,topic=my-topic
```

**Expected output:** Compare the current `MessagesInPerSec` with the historical baseline. A sudden spike (e.g., 2-10x normal rate) combined with growing lag indicates the consumer is correctly processing but cannot keep up with the burst.

**What this means:** If production rate spiked, the lag may be temporary and will resolve as the burst subsides — provided the consumer can process faster than the sustained rate. If the production rate is at its normal level, the consumer itself is the problem.

### Step 5: Check Partition Assignment Balance

Determines whether partitions are evenly distributed across consumer instances.

```bash
# Show partition assignments per consumer
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group | \
  awk '{print $7}' | sort | uniq -c | sort -rn

# Check number of partitions vs number of consumers
kafka-topics.sh --bootstrap-server kafka1:9092 --describe --topic my-topic | \
  head -1
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group --members | wc -l
```

**Expected output:** Each consumer should have approximately equal partitions. If the topic has 12 partitions and 3 consumers, each should have 4. If some consumers have 6 and others 2, the assignment is unbalanced.

**What this means:** Consumers with more partitions or higher-throughput partitions will lag while others sit idle. If there are more consumers than partitions, extra consumers are idle (Kafka assigns at most one consumer per partition within a group). Use a sticky or cooperative partition assignor to minimize rebalance churn.

### Step 6: Check Consumer Fetch Configuration

Determines whether fetch settings are causing unnecessary latency or limiting throughput.

```bash
# Review consumer configuration
grep -E "fetch.min.bytes|fetch.max.wait|max.poll.records|max.partition.fetch" \
  /etc/kafka/consumer.properties

# Check fetch rate and size via JMX
# kafka.consumer:type=consumer-fetch-manager-metrics,client-id=*
# fetch-rate: fetches per second
# fetch-size-avg: average bytes per fetch
# records-per-request-avg: messages per fetch
```

**Expected output:** `fetch.min.bytes=1` (default) and `fetch.max.wait.ms=500` (default) are appropriate for low-latency consumers. `max.poll.records=500` (default) limits how many records are returned per `poll()` call.

**What this means:** A high `fetch.min.bytes` forces the consumer to wait for more data per fetch, adding latency. A high `max.poll.records` increases the processing time per `poll()` call, risking `max.poll.interval.ms` timeout. `max.partition.fetch.bytes` limits how much data is fetched per partition — too low throttles throughput.

## Mitigation

### Option 1: Scale Out Consumers

**Risk:** Low. Adding consumers distributes the processing load. Requires that the topic has more partitions than current consumers. Triggers a consumer group rebalance.

**Command:**

```bash
# Start additional consumer instances (same consumer group)
# The new instances will join the group and receive partition assignments

# If consumers are in Kubernetes, scale the deployment
kubectl scale deployment my-consumer --replicas=6

# Verify new members joined the group
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group --members
```

**Verify:**

```bash
# Watch lag decrease over time
watch -n 10 'kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group | awk "{sum+=\$6} END {print \"Total lag:\", sum}"'
```

**Duration:** Rebalance takes 10-60 seconds. Lag recovery depends on the backlog size and the increase in processing capacity.

### Option 2: Reduce max.poll.records to Prevent Rebalance-Induced Lag

**Risk:** Low. Reduces the number of records per `poll()` call, giving the consumer more time to process each batch within `max.poll.interval.ms`. May slightly reduce throughput.

**Command:**

```bash
# Update consumer configuration (requires consumer restart)
# In consumer.properties or application config:
# max.poll.records=100  (down from default 500)

# Or increase max.poll.interval.ms if processing is legitimately slow
# max.poll.interval.ms=600000  (10 minutes, up from default 5 minutes)
```

**Verify:**

```bash
# After restart, confirm no more CommitFailedException or rebalance events
grep -E "CommitFailedException|rebalanc|removed" /var/log/consumer/consumer.log | tail -10

# Confirm lag is stable or decreasing
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group
```

**Duration:** Immediate after consumer restart. Effect on lag visible within minutes.

### Option 3: Increase Topic Partitions

**Risk:** Medium. Adding partitions allows more consumers to process in parallel. Messages with the same key may be redistributed to new partitions, breaking ordering for key-based consumers. Does not help if processing per message is the bottleneck.

**Command:**

```bash
# Increase partition count (cannot be decreased)
kafka-topics.sh --bootstrap-server kafka1:9092 \
  --alter --topic my-topic --partitions 24

# Then scale consumers to match
kubectl scale deployment my-consumer --replicas=24
```

**Verify:**

```bash
kafka-topics.sh --bootstrap-server kafka1:9092 --describe --topic my-topic | head -1
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group --members --verbose
```

**Duration:** Partition creation is immediate. Consumer rebalance and new partition assignment takes 10-60 seconds. Key-based ordering is permanently affected.

### Option 4: Reset Consumer Offsets to Skip Unrecoverable Backlog

**Risk:** High. Skips unprocessed messages. Use only when the backlog is too large to catch up and the messages are no longer needed (e.g., time-sensitive events that have expired).

**Command:**

```bash
# Stop all consumers in the group first, then reset
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --group my-consumer-group \
  --topic my-topic \
  --reset-offsets --to-latest --execute
```

**Verify:**

```bash
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group
# LAG should be 0 or near-0 for all partitions
```

**Duration:** Immediate. All backlogged messages are permanently skipped.

## Root Cause Resolution

**If** consumer processing logic is too slow → Profile the consumer to identify the bottleneck (database writes, API calls, serialization). Batch downstream writes where possible. Offload heavy processing to a separate thread pool while the main thread continues calling `poll()`. Use asynchronous I/O for downstream calls.

**If** frequent rebalances cause processing gaps → Switch to the `CooperativeStickyAssignor` partition assignment strategy to avoid stop-the-world rebalances. Set `session.timeout.ms=45000` and `heartbeat.interval.ms=15000` to tolerate brief GC pauses. Configure static group membership with `group.instance.id` to avoid rebalance on transient consumer restarts.

**If** insufficient consumers for the partition count → Scale consumers to match the partition count. Each partition can be consumed by at most one consumer in a group. If the topic has 12 partitions and 3 consumers, adding consumers up to 12 will linearly improve throughput.

**If** producer throughput spike caused temporary lag → If the spike is temporary, the lag will self-resolve as consumers catch up at their normal rate. If the new rate is sustained, scale consumers to match. Consider adding partitions if the sustained rate exceeds what current partitions can deliver.

**If** unbalanced partition assignment causes hot spots → Use `CooperativeStickyAssignor` for balanced assignment. If certain partitions receive more traffic due to key distribution, consider repartitioning with a different key strategy or using a custom partition assignor.

**If** GC pauses in the consumer JVM cause stalls → Tune GC settings (use G1GC or ZGC for low-pause collection). Reduce heap pressure by limiting in-memory batch sizes. Set `max.poll.records` low enough that processing completes well within `max.poll.interval.ms` even during GC events.

## Verification

After applying fixes, confirm consumer health:

```bash
# 1. Consumer lag is decreasing toward zero
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group
# LAG column should be in single digits for all partitions

# 2. Consumer group is stable (no rebalancing)
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group --state
# STATE should be "Stable"

# 3. All expected consumer instances are assigned partitions
kafka-consumer-groups.sh --bootstrap-server kafka1:9092 \
  --describe --group my-consumer-group --members --verbose
# Each consumer should have roughly equal partitions

# 4. No rebalance or commit errors in consumer logs
grep -E "CommitFailedException|rebalanc|removed|max.poll.interval" \
  /var/log/consumer/consumer.log | tail -5
# Should return no recent entries

# 5. End-to-end latency is within SLA
# Produce a timestamped message and measure time to consumption
kafka-console-producer.sh --bootstrap-server kafka1:9092 --topic my-topic <<< "latency-check-$(date +%s)"
```

## Prevention

- **Monitor consumer lag continuously** with alerting on `records-lag-max` exceeding a threshold (e.g., 1000 messages or 5 minutes of production)
- **Use `CooperativeStickyAssignor`** to minimize rebalance disruption and maintain partition stickiness across rebalances
- **Configure static group membership** with `group.instance.id` for containerized consumers to prevent rebalances on restarts
- **Set `max.poll.records` conservatively** relative to processing time — ensure a full batch can be processed within 50% of `max.poll.interval.ms`
- **Scale consumers to match partition count** — add partitions and consumers together as throughput requirements grow
- **Profile consumer processing regularly** to catch performance regressions before they cause lag
- **Separate processing from polling** using a background thread pool for heavy work while the main thread calls `poll()` and manages offsets
- **Monitor `poll-idle-ratio-avg`** — values below 0.5 indicate the consumer is processing-bound and at risk of falling behind
- **Set up lag-based autoscaling** for containerized consumers (e.g., KEDA with Kafka scaler) to automatically scale based on consumer group lag
- **Tune fetch settings** for throughput: `fetch.min.bytes=1`, `fetch.max.wait.ms=500`, `max.partition.fetch.bytes=1048576` for most workloads
- **Use consumer interceptors** to track end-to-end latency (produce timestamp to consume timestamp) as a business-level metric
- **Plan for burst capacity** by provisioning consumers to handle 2-3x normal throughput

## Sources

- [Apache Kafka Documentation — Consumer Configs](https://kafka.apache.org/documentation/#consumerconfigs)
- [Apache Kafka Documentation — Consumer Group Protocol](https://kafka.apache.org/documentation/#design_consumerposition)
- [Confluent Documentation — Monitor Consumer Lag](https://docs.confluent.io/platform/current/kafka/monitoring.html)
- [Apache Kafka KIP-429 — Incremental Cooperative Rebalancing](https://cwiki.apache.org/confluence/display/KAFKA/KIP-429)
- [Apache Kafka KIP-345 — Static Group Membership](https://cwiki.apache.org/confluence/display/KAFKA/KIP-345)
