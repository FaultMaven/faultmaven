---
id: kafka-producer-timeout
title: "Kafka Producer Timeout and Message Loss: Acks, Batching, and Network Diagnosis"
domain: messaging
service: kafka
symptom_class:
  - timeout
  - data-loss
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kafka
  - producer
  - timeout
  - acks
  - batching
  - message-loss
  - linger
difficulty: intermediate
---

# Kafka Producer Timeout and Message Loss

## Problem Definition

Applies to Apache Kafka 2.6+ producers using the Java client (`kafka-clients`), librdkafka, or compatible clients. Requires access to producer application logs, broker logs, and Kafka CLI tools. Producer configuration is typically set in application code or `producer.properties`.

Kafka producer timeouts occur when the producer cannot deliver messages to brokers within the configured time limits. The producer's `delivery.timeout.ms` (default 120 seconds) governs the total time a message can spend in the producer buffer, waiting for batch completion, and awaiting broker acknowledgment. When this timeout is exceeded, the producer callback receives a `TimeoutException` and the message is not delivered. Depending on `acks` configuration, messages may also be silently lost if the broker acknowledges before replication completes and then fails.

**Symptoms and errors:**

- Producer callback or future receives `org.apache.kafka.common.errors.TimeoutException`
- `NetworkException` or `DisconnectException` when the broker connection drops during send
- `NotLeaderOrFollowerException` when the producer sends to a broker that no longer leads the target partition
- `RecordTooLargeException` when the message exceeds `max.request.size` or `message.max.bytes`
- `BufferExhaustedException` when the producer's internal buffer (`buffer.memory`, default 32 MB) is full and `max.block.ms` is exceeded
- `record-error-rate` JMX metric increases
- `record-send-rate` drops to zero during network partitions or broker outages
- `batch-size-avg` near `batch.size` indicates full batches — may mean `linger.ms` is too high or throughput exceeds send capacity
- Producer logs show repeated `Connection to node -1 could not be established` errors

**Common causes:**

- Broker unreachable due to network partition, broker failure, or firewall rules
- `acks=0` or `acks=1` causing silent message loss when brokers fail after acknowledgment but before replication
- `linger.ms` set too high, causing messages to wait in the batch buffer beyond acceptable latency
- `batch.size` too large for the message rate, causing batches to never fill and waiting for `linger.ms` to expire
- `request.timeout.ms` or `delivery.timeout.ms` too short for the network latency and broker processing time
- `buffer.memory` exhausted under sustained high throughput, causing backpressure
- `retries` set to 0 (pre-Kafka 2.1 default), preventing retry of transient failures
- DNS resolution failure for bootstrap servers or advertised listeners
- SSL/TLS handshake failure or certificate expiry preventing connection establishment
- Broker-side `message.max.bytes` or topic-level `max.message.bytes` lower than the producer's message size

## Diagnostic Steps

### Step 1: Identify the Specific Error from Producer Logs

Determines the exact failure type — timeout, network error, or message rejection — to guide subsequent diagnosis.

```bash
# Search producer application logs for Kafka errors
grep -E "TimeoutException|NetworkException|DisconnectException|NotLeaderOrFollower|RecordTooLarge|BufferExhausted" \
  /var/log/app/producer.log | tail -30

# Check for connection establishment failures
grep -E "Connection to node|could not be established|Bootstrap broker" \
  /var/log/app/producer.log | tail -20
```

**Expected output:** Error messages with timestamps, broker IDs, and topic-partition references. `TimeoutException` with a message like `Expiring N record(s) for topic-partition: delivery.timeout.ms expired` confirms the message sat in the buffer too long.

**What this means:** `TimeoutException` means the message could not be sent within `delivery.timeout.ms`. `NetworkException` means the connection dropped mid-request. `NotLeaderOrFollowerException` means metadata is stale. `BufferExhaustedException` means the producer is generating messages faster than they can be sent.

### Step 2: Check Producer Configuration

Reviews the critical producer settings that govern timeouts, batching, and durability.

```bash
# If using a properties file
grep -E "acks|retries|delivery.timeout|request.timeout|linger|batch.size|buffer.memory|max.block|max.request.size|enable.idempotence" \
  /etc/kafka/producer.properties

# Check effective configuration via producer JMX (if exposed)
# kafka.producer:type=producer-metrics,client-id=*
# Key metrics: batch-size-avg, record-queue-time-avg, request-latency-avg, record-send-rate
```

**Expected output:** For production durability: `acks=all`, `enable.idempotence=true`, `retries=2147483647` (or high value). For latency: `linger.ms=0-5`. For throughput: `batch.size=16384-65536`, `linger.ms=5-50`.

**What this means:** `acks=0` means the producer does not wait for any acknowledgment — messages are fire-and-forget and silently lost on broker failure. `acks=1` waits only for the leader, so messages are lost if the leader fails before replication. `linger.ms=0` sends immediately but reduces batching efficiency. `delivery.timeout.ms` must be greater than `linger.ms + request.timeout.ms`.

### Step 3: Check Broker Connectivity from the Producer

Verifies that the producer can reach the brokers and that advertised listeners resolve correctly.

```bash
# Test TCP connectivity to broker
nc -zv kafka1 9092
nc -zv kafka2 9092
nc -zv kafka3 9092

# Verify DNS resolution of bootstrap servers
nslookup kafka1
nslookup kafka2

# Check that advertised listeners match what the producer can reach
kafka-broker-api-versions.sh --bootstrap-server kafka1:9092 2>&1 | head -5

# Check for TLS issues
openssl s_client -connect kafka1:9093 -brief 2>&1 | head -10
```

**Expected output:** `nc` should report `succeeded` or `open` for each broker. DNS should resolve to the expected IP addresses. If using SSL, the certificate chain should be valid and not expired.

**What this means:** If `nc` times out, a firewall or security group is blocking the port. If DNS fails, the producer cannot discover brokers. If TLS fails, check certificate expiry, CA trust, and listener configuration (`listeners` vs `advertised.listeners` in broker config).

### Step 4: Check Broker-Side Metrics and Logs

Determines whether the broker is receiving and rejecting requests, or not receiving them at all.

```bash
# Check broker request handler utilization
# kafka.server:type=KafkaRequestHandlerPool,name=RequestHandlerAvgIdlePercent
# Values below 0.3 (30%) indicate broker is overloaded

# Check produce request latency on the broker
# kafka.network:type=RequestMetrics,name=TotalTimeMs,request=Produce

# Check broker logs for produce errors
grep -E "ERROR|WARN" /var/log/kafka/server.log | \
  grep -i "produce\|timeout\|too.large\|reject" | tail -20

# Check topic configuration for max message size
kafka-configs.sh --bootstrap-server kafka1:9092 \
  --entity-type topics --entity-name my-topic --describe | \
  grep max.message.bytes
```

**Expected output:** `RequestHandlerAvgIdlePercent` should be above 0.5 (50% idle). If it is below 0.3, the broker is overloaded and produce requests queue up, causing timeouts. Topic-level `max.message.bytes` defaults to 1 MB.

**What this means:** Low request handler idle percentage means the broker cannot keep up with incoming requests — scale brokers or reduce partition count. If `max.message.bytes` is lower than the messages being sent, the broker rejects them with `RecordTooLargeException`.

### Step 5: Check Producer Buffer and Batching Metrics

Determines whether the producer's internal buffer is saturated and whether batching is configured optimally.

```bash
# Key JMX metrics to check (via Prometheus, JMX exporter, or JConsole):
# kafka.producer:type=producer-metrics,client-id=*
#   buffer-available-bytes: remaining buffer space (should be > 0)
#   buffer-total-bytes: total buffer memory (= buffer.memory setting)
#   record-queue-time-avg: average time records spend in the buffer (ms)
#   batch-size-avg: average batch size in bytes
#   records-per-request-avg: records per produce request
#   request-latency-avg: average broker response time (ms)
#   waiting-threads: threads blocked waiting for buffer space (should be 0)
```

**Expected output:** `buffer-available-bytes` should be well above 0. `record-queue-time-avg` should be close to `linger.ms`. `waiting-threads > 0` indicates buffer exhaustion.

**What this means:** If `buffer-available-bytes` is near 0 and `waiting-threads > 0`, the producer is backpressured — increase `buffer.memory` or reduce send rate. If `record-queue-time-avg` is much higher than `linger.ms`, batches are waiting for broker responses (slow broker or network). If `batch-size-avg` is much smaller than `batch.size`, increase `linger.ms` to allow more messages per batch and improve throughput.

## Mitigation

### Option 1: Increase delivery.timeout.ms for Transient Network Issues

**Risk:** Low. Gives the producer more time to retry failed sends. Increases the window during which messages are held in memory before being declared failed.

**Command:**

```bash
# Update producer configuration (requires producer restart)
# delivery.timeout.ms=300000   (5 minutes, up from default 2 minutes)
# request.timeout.ms=60000     (60 seconds, up from default 30 seconds)

# Ensure the relationship holds: delivery.timeout.ms >= linger.ms + request.timeout.ms
```

**Verify:**

```bash
# After restart, monitor that TimeoutException rate drops
grep "TimeoutException" /var/log/app/producer.log | tail -5
# Check producer metrics: record-error-rate should decrease
```

**Duration:** Immediate after producer restart.

### Option 2: Switch to acks=all with Idempotence to Prevent Message Loss

**Risk:** Low. Increases produce latency slightly (waits for all ISR replicas) but ensures no message loss. Idempotence prevents duplicates from retries.

**Command:**

```bash
# Update producer configuration (requires producer restart)
# acks=all
# enable.idempotence=true
# retries=2147483647  (effectively infinite, set automatically with idempotence)
# max.in.flight.requests.per.connection=5  (safe with idempotence enabled)
```

**Verify:**

```bash
# Confirm no message loss by comparing producer send count with consumer receive count
# Check producer metrics: record-send-total, record-error-total
# Check topic offset growth matches producer send rate
kafka-run-class.sh kafka.tools.GetOffsetShell \
  --broker-list kafka1:9092 --topic my-topic --time -1
```

**Duration:** Immediate after producer restart. Latency increase is typically 1-5 ms per produce request.

### Option 3: Increase buffer.memory for High-Throughput Producers

**Risk:** Low. Allocates more memory to the producer buffer, allowing it to absorb bursts without backpressure. Increases JVM memory requirements.

**Command:**

```bash
# Update producer configuration (requires producer restart)
# buffer.memory=67108864   (64 MB, up from default 32 MB)
# max.block.ms=120000      (2 minutes, time to wait if buffer is full)
```

**Verify:**

```bash
# Monitor producer metrics: buffer-available-bytes should stay above 0
# waiting-threads should be 0
# BufferExhaustedException should not appear in logs
grep "BufferExhausted" /var/log/app/producer.log | tail -5
```

**Duration:** Immediate after producer restart.

### Option 4: Tune Batching for Optimal Throughput/Latency Balance

**Risk:** Low. Adjusting `linger.ms` and `batch.size` trades latency for throughput or vice versa.

**Command:**

```bash
# For low-latency (sub-millisecond send):
# linger.ms=0
# batch.size=16384

# For high-throughput (batch-oriented):
# linger.ms=20
# batch.size=65536
# compression.type=lz4   (reduces network I/O, increases CPU)
```

**Verify:**

```bash
# Check producer metrics after restart:
# batch-size-avg should increase with higher linger.ms
# record-send-rate should increase
# record-queue-time-avg should approximate linger.ms
```

**Duration:** Immediate after producer restart.

## Root Cause Resolution

**If** `acks=0` or `acks=1` is causing silent message loss → Set `acks=all` and `enable.idempotence=true`. This ensures messages are replicated to all ISR members before acknowledgment and prevents duplicates from retries. Combine with `min.insync.replicas=2` on the topic for maximum durability.

**If** `delivery.timeout.ms` is too short for the network conditions → Increase `delivery.timeout.ms` to accommodate network latency, broker processing time, and retry attempts. Ensure `delivery.timeout.ms >= linger.ms + request.timeout.ms`. For cross-region producers, set `request.timeout.ms=60000` or higher.

**If** broker unreachability causes connection failures → Fix network connectivity (security groups, firewall rules, DNS). Verify `advertised.listeners` in the broker config matches what the producer can resolve and connect to. In Kubernetes, ensure the producer uses the correct service or headless service address.

**If** `buffer.memory` exhaustion causes backpressure → Increase `buffer.memory` to handle burst traffic. If the producer consistently exceeds buffer capacity, the downstream send rate is insufficient — add partitions, enable compression (`compression.type=lz4`), or reduce message size.

**If** `linger.ms` is too high and adds unnecessary latency → Reduce `linger.ms` to 0-5 ms for latency-sensitive workloads. For throughput-oriented workloads, `linger.ms=20-50` with a larger `batch.size` is appropriate. Ensure `delivery.timeout.ms` accounts for `linger.ms`.

**If** messages exceed broker `max.message.bytes` → Increase `max.message.bytes` on the topic and `message.max.bytes` on the broker. Also increase `max.request.size` on the producer. Alternatively, compress messages or split large payloads into smaller messages.

**If** SSL/TLS handshake failures prevent connections → Check certificate expiry with `openssl s_client`. Verify the producer's truststore includes the broker's CA. Ensure `ssl.endpoint.identification.algorithm` matches the broker's certificate SANs. Rotate expired certificates.

## Verification

After applying fixes, confirm producer health:

```bash
# 1. No TimeoutException or other errors in producer logs
grep -E "TimeoutException|NetworkException|BufferExhausted|RecordTooLarge" \
  /var/log/app/producer.log | tail -5
# Should return no recent entries

# 2. Producer is sending successfully (record-error-rate = 0)
# Check JMX: kafka.producer:type=producer-metrics,client-id=*
# record-send-rate > 0, record-error-rate = 0

# 3. Messages are arriving at the topic
kafka-run-class.sh kafka.tools.GetOffsetShell \
  --broker-list kafka1:9092 --topic my-topic --time -1
# Run twice 30 seconds apart — offsets should increase

# 4. End-to-end delivery test
kafka-console-producer.sh --bootstrap-server kafka1:9092 \
  --topic my-topic \
  --producer-property acks=all <<< "health-check-$(date +%s)"
kafka-console-consumer.sh --bootstrap-server kafka1:9092 \
  --topic my-topic --from-beginning --max-messages 1 --timeout-ms 10000

# 5. Broker-side produce latency is acceptable
# kafka.network:type=RequestMetrics,name=TotalTimeMs,request=Produce
# p99 should be under 100 ms for local clusters
```

## Prevention

- **Always use `acks=all` with `enable.idempotence=true`** for production topics to guarantee exactly-once delivery semantics and prevent silent message loss
- **Set `min.insync.replicas=2`** on topics to ensure writes are durable even if one replica fails
- **Set `delivery.timeout.ms`** to accommodate the worst-case network round-trip plus retry time — 2-5 minutes is typical
- **Monitor `record-error-rate`** and alert when it exceeds 0 for more than 1 minute
- **Monitor `buffer-available-bytes`** and alert when it drops below 10% of `buffer.memory`
- **Use `compression.type=lz4`** to reduce network bandwidth and improve throughput without significant CPU overhead
- **Configure producer retries as infinite** (`retries=2147483647` or rely on idempotence defaults) and let `delivery.timeout.ms` control the overall failure window
- **Set `max.in.flight.requests.per.connection=5`** with idempotence enabled to maintain ordering and throughput
- **Use separate producer instances** for topics with different latency and durability requirements rather than sharing one producer
- **Implement a producer callback** that logs failed messages with topic, partition, offset, and error for post-incident analysis
- **Test producer behavior under broker failure** by killing a broker in a staging environment and verifying no messages are lost with `acks=all`
- **Monitor `request-latency-avg`** and `record-queue-time-avg` to detect broker slowdowns before they cause timeouts

## Sources

- [Apache Kafka Documentation — Producer Configs](https://kafka.apache.org/documentation/#producerconfigs)
- [Apache Kafka Documentation — Design: The Producer](https://kafka.apache.org/documentation/#design_overview)
- [Confluent Documentation — Kafka Producer](https://docs.confluent.io/platform/current/clients/producer.html)
- [Apache Kafka KIP-98 — Exactly Once Delivery and Transactional Messaging](https://cwiki.apache.org/confluence/display/KAFKA/KIP-98)
- [Apache Kafka Documentation — Message Delivery Semantics](https://kafka.apache.org/documentation/#semantics)
