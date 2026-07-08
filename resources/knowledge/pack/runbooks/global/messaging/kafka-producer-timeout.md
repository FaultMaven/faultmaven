---
id: kafka-producer-timeout
title: "Kafka Producer Timeout and Message Loss"
domain: messaging
service: kafka
symptom_class:
  - timeout
  - data_loss
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

## Symptom Recognition

- Producer callback or future throws `org.apache.kafka.common.errors.TimeoutException` with message `Expiring N record(s) for topic-partition: delivery.timeout.ms expired`.
- Producer logs show `NetworkException`, `DisconnectException`, or `Connection to node -1 could not be established. Broker may not be available` on repeated attempts.
- `NotLeaderOrFollowerException` appears after a broker restart or leader election, indicating stale partition metadata.
- `RecordTooLargeException` when a message exceeds the producer `max.request.size` or the topic-level `max.message.bytes`.
- `BufferExhaustedException` when the producer's `buffer.memory` is full and `max.block.ms` is exceeded — the calling thread blocks and then fails.
- JMX metric `kafka.producer:type=producer-metrics,client-id=*` `record-error-rate` is non-zero or rising; `record-send-rate` drops to zero.
- `batch-size-avg` near `batch.size` indicates batches are filling before `linger.ms` elapses — the producer cannot send fast enough for the produce rate.
- `buffer-available-bytes` trending toward zero and `waiting-threads > 0` confirm buffer exhaustion under sustained load.
- Silent message loss with `acks=0` or `acks=1` — the broker acknowledges but fails before replication; no exception is raised by the producer.

## Applicability

- Apache Kafka 2.6+ brokers and Java `kafka-clients` 2.6+ (or librdkafka 1.6+ equivalent). Configuration defaults referenced below match Apache Kafka 3.x / Confluent Platform 7.x.
- Shell access to a host with `kafka-broker-api-versions.sh`, `kafka-configs.sh`, `kafka-run-class.sh`, and `kafka-console-producer.sh` on the `PATH`.
- Read access to producer application logs (typically `/var/log/<app>/producer.log` or container stdout) and to the producer configuration file or JVM system properties.
- Network reachability from the diagnostic host to broker listeners (typically port `9092` PLAINTEXT or `9093` TLS) for connectivity checks.
- JMX exposure on the producer JVM or a Prometheus JMX exporter scrape to observe producer-side metrics.
- Permission to restart the producer application to apply configuration changes.

## Diagnostic Steps

### Step 1: Identify the exact exception from producer logs

```bash
grep -E "TimeoutException|NetworkException|DisconnectException|NotLeaderOrFollower|RecordTooLarge|BufferExhausted" \
  /var/log/<app>/producer.log | tail -40
grep -E "Connection to node|could not be established|Bootstrap broker" \
  /var/log/<app>/producer.log | tail -20
```

Expected output: error lines with timestamps, broker node IDs, and topic-partition references. `TimeoutException` with text `delivery.timeout.ms expired` confirms message buffering exceeded the delivery window. `NetworkException` or `DisconnectException` points to broker connectivity loss. `BufferExhaustedException` confirms the internal send buffer is exhausted.

### Step 2: Review producer configuration for durability and timeout settings

```bash
grep -E "acks|retries|delivery\.timeout|request\.timeout|linger|batch\.size|buffer\.memory|max\.block|max\.request\.size|enable\.idempotence|max\.in\.flight" \
  /etc/<app>/producer.properties 2>/dev/null
# Or from running JVM
jcmd $(pgrep -of <app>) VM.system_properties 2>/dev/null \
  | grep -E "acks|retries|delivery\.timeout|request\.timeout|linger|batch\.size|buffer\.memory"
```

Expected output: each configured key with its value. Production-safe defaults: `acks=all`, `enable.idempotence=true`, `retries=2147483647`, `delivery.timeout.ms=120000` (2 min), `request.timeout.ms=30000`, `linger.ms=5`, `batch.size=16384`, `buffer.memory=33554432` (32 MB). Flag `acks=0`, `acks=1`, `retries=0`, or `delivery.timeout.ms` shorter than `linger.ms + request.timeout.ms` as configuration faults.

### Step 3: Test TCP connectivity and DNS resolution to brokers

```bash
# Replace kafka1/kafka2/kafka3 with your broker hostnames
for HOST in kafka1 kafka2 kafka3; do
  nc -zv $HOST 9092 2>&1
  nslookup $HOST 2>&1 | tail -3
done
# Verify advertised listeners via Kafka API
kafka-broker-api-versions.sh --bootstrap-server kafka1:9092 2>&1 | head -5
# Check TLS if SSL listener is in use
openssl s_client -connect kafka1:9093 -brief 2>&1 | head -10
```

Expected output: `nc` reports `succeeded` or `Connection to <host> 9092 port [tcp/*] succeeded!` for each broker. DNS resolves to the expected IP. `kafka-broker-api-versions.sh` returns a broker API version table, confirming authentication and TCP connectivity. TLS check should show a valid certificate chain — `Verify return code: 0 (ok)`.

### Step 4: Check broker-side overload and topic message-size limits

```bash
# Broker request handler idle percentage (JMX: kafka.server:type=KafkaRequestHandlerPool,name=RequestHandlerAvgIdlePercent)
# Values below 0.30 indicate the broker is overloaded and queuing produce requests.
grep -E "ERROR|WARN" /var/log/kafka/server.log | \
  grep -i "produce\|timeout\|too.large\|reject" | tail -20
# Topic-level max.message.bytes
kafka-configs.sh --bootstrap-server kafka1:9092 \
  --entity-type topics --entity-name <topic> --describe \
  | grep max.message.bytes
```

Expected output: no `ERROR` / `WARN` lines about produce rejections in a healthy cluster. `max.message.bytes` defaults to 1048588 bytes (~1 MB) per topic. A `RequestHandlerAvgIdlePercent` below 0.30 (30%) signals broker overload; values above 0.50 are healthy.

### Step 5: Inspect producer buffer and batching metrics

```bash
# Prometheus JMX exporter — adjust host/port for your deployment
curl -s http://<producer-host>:9404/metrics \
  | grep -E 'kafka_producer_(record_error_rate|record_send_rate|batch_size_avg|record_queue_time_avg|buffer_available_bytes|waiting_threads|request_latency_avg|records_per_request_avg)' \
  | head -20
```

Expected output: `record_error_rate` at zero, `record_send_rate` positive, `buffer_available_bytes` well above zero, `waiting_threads` at zero. `record_queue_time_avg` should approximate `linger.ms`. `waiting_threads > 0` confirms threads are blocked waiting for buffer space. `batch_size_avg` near `batch.size` with high `linger.ms` indicates the producer cannot flush batches fast enough.

### Step 6: Run an end-to-end produce-consume health check

```bash
TEST_MSG="health-check-$(date +%s)"
# Produce with acks=all to confirm delivery path is working
kafka-console-producer.sh --bootstrap-server kafka1:9092 \
  --topic <topic> \
  --producer-property acks=all <<< "$TEST_MSG"
# Consume from end of log to confirm receipt within 10 s
kafka-console-consumer.sh --bootstrap-server kafka1:9092 \
  --topic <topic> --offset latest --partition 0 \
  --max-messages 1 --timeout-ms 10000 2>&1
```

Expected output: `kafka-console-producer.sh` exits cleanly without error. `kafka-console-consumer.sh` prints the test message and exits with `Processed 1 messages`. Failure at this step (timeout or exception) confirms the delivery path is broken, not a monitoring gap.

## Causes

### Cause A: `delivery.timeout.ms` too short for network latency and broker processing time

**Statement:** The producer's `delivery.timeout.ms` is set below `linger.ms` plus `request.timeout.ms`, leaving no retry headroom, so transient network delays expire messages in the buffer before delivery.

**Chain:**
- root: `delivery.timeout.ms` is configured below `linger.ms + request.timeout.ms`, the lifecycle ceiling for a record.
- s1: messages expire before the first request completes, making retries structurally impossible.
- s2: higher per-request latency (cross-region, broker load, `min.insync.replicas=2`) consumes the already-too-small budget.
- D: records expire in the buffer and the producer raises `TimeoutException` (points at Symptom Recognition).

**Indicators:**
- s1: [Step 1] log contains `Expiring N record(s) for topic-partition: delivery.timeout.ms expired`
- root: [Step 2] `delivery.timeout.ms` value is less than or equal to `request.timeout.ms` (no retry budget remains after one request)
- s2: [Step 5] `record_error_rate > 0` and `request_latency_avg` is high relative to `request.timeout.ms`

**Interventions:**
- **remediation** (root): set `delivery.timeout.ms=300000` and `request.timeout.ms=60000` durably in producer config (constraint: `delivery.timeout.ms >= linger.ms + request.timeout.ms`).

  ```bash
  # Durable fix: add these to producer.properties (or application config) and deploy
  # delivery.timeout.ms=300000
  # request.timeout.ms=60000
  # linger.ms=5
  # Verify constraint: delivery.timeout.ms (300000) >= linger.ms (5) + request.timeout.ms (60000) = 60005 ✓
  kubectl rollout restart deployment/<app>
  ```

  **Verification:** After restart, Step 5 shows `record_error_rate = 0` for 15 minutes; Step 1 grep returns no new `delivery.timeout.ms expired` lines; Step 6 end-to-end health check passes.
- **mitigation** (root): raise `delivery.timeout.ms` to 5 minutes and `request.timeout.ms` to 60s to give 5–6 retry attempts immediately.

  ```bash
  # Set delivery.timeout.ms to 5 minutes (300000 ms) — gives 5–6 retry attempts at default request.timeout.ms=30s
  # Constraint: delivery.timeout.ms >= linger.ms + request.timeout.ms
  kubectl set env deployment/<app> \
    KAFKA_PRODUCER_DELIVERY_TIMEOUT_MS=300000 \
    KAFKA_PRODUCER_REQUEST_TIMEOUT_MS=60000
  kubectl rollout restart deployment/<app>
  ```

  **Risk:** Increasing `delivery.timeout.ms` extends the window during which failed messages occupy buffer memory; size `buffer.memory` to hold the extra in-flight messages. **Duration:** Immediate after producer restart; re-evaluate after confirming `record_error_rate` returns to zero. **Verification:** Step 5 shows `record_error_rate = 0`; Step 1 grep returns no new `delivery.timeout.ms expired` lines.

### Cause B: `acks=0` or `acks=1` — silent message loss on broker failure

**Statement:** The producer uses `acks=0` or `acks=1`, so messages are acknowledged before replication completes and are silently lost when the leader broker fails before replicas receive the data.

**Chain:**
- root: producer is configured with `acks=0` (fire-and-forget) or `acks=1` (leader-only acknowledgement).
- s1: a record is acknowledged before followers fetch it, so it is unreplicated when the leader crashes.
- s2: no `TimeoutException` is raised — the callback reports success while the record is gone.
- D: consumers silently miss messages (points at Symptom Recognition).

**Indicators:**
- root: [Step 2] `acks=0` or `acks=1` is present in producer configuration
- root: [Step 2] `enable.idempotence` is false or absent (default before Kafka 3.0)
- D: [Symptom] consumer lag and offset growth do not match expected producer send count — loss detected only by comparing producer send-total with consumer receive-total

**Interventions:**
- **remediation** (root): set `acks=all`, `enable.idempotence=true`, `retries=2147483647`, `max.in.flight.requests.per.connection=5` and `min.insync.replicas=2` on the topic for durable delivery.

  ```bash
  # Producer: durable delivery baseline
  kubectl set env deployment/<app> \
    KAFKA_PRODUCER_ACKS=all \
    KAFKA_PRODUCER_ENABLE_IDEMPOTENCE=true \
    KAFKA_PRODUCER_RETRIES=2147483647 \
    KAFKA_PRODUCER_MAX_IN_FLIGHT_REQUESTS_PER_CONNECTION=5
  kubectl rollout restart deployment/<app>
  # Topic: require 2 in-sync replicas before acknowledging
  kafka-configs.sh --bootstrap-server kafka1:9092 \
    --entity-type topics --entity-name <topic> \
    --alter --add-config min.insync.replicas=2
  ```

  **Verification:** Run Step 6 end-to-end health check; kill one broker and confirm the test message is still consumed — no loss. Step 5 shows `record_error_rate = 0`.

### Cause C: Broker unreachable — network partition, firewall, or DNS failure

**Statement:** The producer cannot establish or maintain TCP connections to broker advertised listeners because of a firewall rule, security group, DNS misconfiguration, or network partition.

**Chain:**
- root: a firewall rule, security group, DNS error, network partition, or wrong `advertised.listeners` blocks TCP to a broker listener.
- s1: the producer raises `NetworkException` for partitions whose leader is on the unreachable broker.
- s2: if all brokers are unreachable, the metadata fetch cannot complete and every send raises `TimeoutException`.
- D: produce requests fail with connectivity errors (points at Symptom Recognition).

**Indicators:**
- s1: [Step 3] `nc -zv` fails for one or more brokers — `Connection refused` or `timed out`
- s1: [Step 1] log contains `Connection to node -1 could not be established`
- s1: [Step 3] `kafka-broker-api-versions.sh` returns no output or `Error connecting to node`

**Interventions:**
- **remediation** (root): correct `advertised.listeners` to an address the producer can reach, then roll the brokers.

  ```bash
  # For Kubernetes: verify advertised.listeners matches the external DNS/IP
  kubectl exec -n kafka kafka-0 -- kafka-configs.sh \
    --bootstrap-server localhost:9092 \
    --entity-type brokers --entity-name 0 --describe \
    | grep advertised.listeners
  # Correct advertised.listeners in the StatefulSet env or Helm values, then roll:
  kubectl rollout restart statefulset/kafka -n kafka
  ```

  **Verification:** Step 3 shows `nc` succeeds for all brokers; `kafka-broker-api-versions.sh` returns the API version table; Step 6 end-to-end health check passes.
- **mitigation** (s1): confirm per-broker port reachability and trace the network path to localize the block before changing production firewall rules.

  ```bash
  # From the producer host, confirm connectivity to each broker port
  for HOST in kafka1 kafka2 kafka3; do
    timeout 5 bash -c "echo >/dev/tcp/$HOST/9092" 2>&1 \
      && echo "$HOST:9092 OPEN" || echo "$HOST:9092 BLOCKED"
  done
  # Trace route to confirm the network path
  traceroute kafka1
  ```

  **Risk:** Adjusting firewall or security group rules is environment-wide; validate with a staging producer before modifying production rules. **Duration:** Diagnostic; fix network/firewall rules per your infrastructure provider. **Verification:** Step 3 shows `nc` succeeds for all brokers.

### Cause D: SSL/TLS handshake failure or certificate expiry

**Statement:** The producer cannot complete the TLS handshake to broker SSL listeners because of an expired certificate, untrusted CA, or hostname mismatch, causing all connection attempts to fail.

**Chain:**
- root: the broker certificate is expired, its CA is missing from the producer truststore, or the SAN does not match the hostname under `ssl.endpoint.identification.algorithm=https`.
- s1: the TLS handshake fails with `SSLHandshakeException` before any Kafka protocol bytes are exchanged.
- s2: every produce request on the affected connection fails with `NetworkException`; `record-error-rate` rises and `record-send-rate` drops to zero.
- D: all connection attempts to the SSL listener fail (points at Symptom Recognition).

**Indicators:**
- s1: [Step 1] log contains `SSLHandshakeException` or `PKIX path building failed` or `certificate_expired`
- root: [Step 3] `openssl s_client -connect kafka1:9093` output contains `Verify return code:` not equal to `0 (ok)`, or `notAfter` date is in the past
- root: [Step 2] `security.protocol=SSL` or `SASL_SSL` is configured

**Interventions:**
- **remediation** (root): import the new CA or broker certificate into the producer truststore and restart the producer.

  ```bash
  # Import the new CA or broker cert into the producer truststore
  keytool -import -noprompt -alias kafka-ca \
    -file /path/to/ca.crt \
    -keystore /etc/<app>/kafka.client.truststore.jks \
    -storepass changeit
  # Restart the producer to load the new truststore
  kubectl rollout restart deployment/<app>
  ```

  **Verification:** Step 3 `openssl s_client` shows `Verify return code: 0 (ok)` and `notAfter` date is in the future; Step 6 end-to-end health check passes.
- **mitigation** (root): check broker certificate expiry and the producer truststore contents to confirm the failing trust anchor before rotating.

  ```bash
  # Check broker certificate expiry
  echo | openssl s_client -connect kafka1:9093 -servername kafka1 2>/dev/null \
    | openssl x509 -noout -dates
  # Check producer truststore
  keytool -list -keystore /etc/<app>/kafka.client.truststore.jks \
    -storepass changeit | grep -A2 "kafka"
  ```

  **Risk:** Rotating certificates causes a brief connectivity window during which the new certificate is being propagated; roll brokers one at a time. **Duration:** Diagnostic; rotate certificates before `notAfter` date. **Verification:** `openssl x509 -noout -dates` shows `notAfter` in the future.

### Cause E: `buffer.memory` exhausted under sustained high throughput

**Statement:** The producer's send buffer reaches `buffer.memory` capacity because the application produces faster than brokers can acknowledge, causing `max.block.ms` to expire and raising `BufferExhaustedException`.

**Chain:**
- root: the application produces faster than brokers acknowledge, so all `buffer.memory` (default 32 MB) is occupied by record batches awaiting send.
- s1: a `send()` call with no free buffer blocks the calling thread for up to `max.block.ms` (default 60000 ms).
- s2: the buffer does not free within that window, so `BufferExhaustedException` is thrown before the record is even enqueued.
- D: the calling thread blocks then fails on produce (points at Symptom Recognition).

**Indicators:**
- s2: [Step 1] log contains `BufferExhaustedException`
- s1: [Step 5] `buffer_available_bytes` is near zero and `waiting_threads > 0`
- root: [Step 5] `record_queue_time_avg` is much higher than `linger.ms`, indicating batches are waiting for broker acknowledgment rather than just for batch accumulation

**Interventions:**
- **remediation** (root): double `buffer.memory` to 64 MB and enable `lz4` compression to reduce bytes per record and relieve sustained buffer pressure.

  ```bash
  # Enable compression to reduce bytes per record — often 3–5x reduction for text/JSON
  # buffer.memory=67108864 ; compression.type=lz4
  kubectl set env deployment/<app> \
    KAFKA_PRODUCER_BUFFER_MEMORY=67108864 \
    KAFKA_PRODUCER_COMPRESSION_TYPE=lz4
  kubectl rollout restart deployment/<app>
  ```

  **Verification:** Step 5 shows `buffer_available_bytes > 0` and `waiting_threads = 0` for 15 minutes; Step 1 grep returns no new `BufferExhaustedException` lines.
- **mitigation** (s1): double `buffer.memory` to 64 MB and extend `max.block.ms` to 120s to absorb the burst while the durable fix is prepared.

  ```bash
  # Double buffer.memory to 64 MB and extend max.block.ms
  # buffer.memory=67108864 ; max.block.ms=120000
  kubectl set env deployment/<app> \
    KAFKA_PRODUCER_BUFFER_MEMORY=67108864 \
    KAFKA_PRODUCER_MAX_BLOCK_MS=120000
  kubectl rollout restart deployment/<app>
  ```

  **Risk:** Raising `buffer.memory` increases JVM heap requirements; add `buffer.memory / 2` of headroom to JVM `-Xmx`. If the producer genuinely generates data faster than the network can carry it, more buffer only delays the failure. **Duration:** Immediate after restart; monitor for 15 minutes to confirm `waiting_threads` drops to zero. **Verification:** Step 5 shows `waiting_threads = 0`.

### Cause F: Message exceeds `max.request.size` or topic `max.message.bytes`

**Statement:** One or more messages exceed the producer `max.request.size` (default 1 MB) or the topic-level `max.message.bytes`, causing the broker to reject them with `RecordTooLargeException`.

**Chain:**
- root: a record is larger than the producer `max.request.size` or the topic `max.message.bytes` / broker `message.max.bytes`.
- s1: the producer rejects oversized records locally, or the broker returns `RecordTooLargeException` in the produce response.
- s2: Kafka does not split messages, so delivery cannot succeed until the record shrinks or both limits are raised.
- D: the broker rejects the message with `RecordTooLargeException` (points at Symptom Recognition).

**Indicators:**
- s1: [Step 1] log contains `RecordTooLargeException`
- root: [Step 4] `kafka-configs.sh ... --describe` shows `max.message.bytes` lower than the actual message size
- root: [Step 2] `max.request.size` is at the default 1048576 and application produces binary or JSON payloads that may exceed this

**Interventions:**
- **remediation** (root): raise the topic `max.message.bytes`, producer `max.request.size`, and broker `replica.fetch.max.bytes` together (or prefer `compression.type=lz4`).

  ```bash
  # Raise the topic and broker replica-fetch limits so replicas can copy large records
  kafka-configs.sh --bootstrap-server kafka1:9092 \
    --entity-type topics --entity-name <topic> \
    --alter --add-config max.message.bytes=5242880
  kafka-configs.sh --bootstrap-server kafka1:9092 \
    --entity-type brokers --entity-name 0 \
    --alter --add-config replica.fetch.max.bytes=5242880
  # Raise the producer limit and restart
  kubectl set env deployment/<app> KAFKA_PRODUCER_MAX_REQUEST_SIZE=5242880
  kubectl rollout restart deployment/<app>
  # Alternatively prefer compression: compression.type=lz4 reduces JSON/text 3-5x without schema changes
  ```

  **Verification:** Step 6 end-to-end health check passes with a message near the new size limit; Step 1 grep returns no new `RecordTooLargeException` lines.
- **mitigation** (s1): raise the topic `max.message.bytes` and producer `max.request.size` to admit the oversized payload while a compression/schema fix is evaluated.

  ```bash
  # Raise the topic limit (immediate, no broker restart required)
  kafka-configs.sh --bootstrap-server kafka1:9092 \
    --entity-type topics --entity-name <topic> \
    --alter --add-config max.message.bytes=5242880
  # Raise the producer limit and restart
  kubectl set env deployment/<app> KAFKA_PRODUCER_MAX_REQUEST_SIZE=5242880
  kubectl rollout restart deployment/<app>
  ```

  **Risk:** Raising `max.message.bytes` and `max.request.size` increases memory pressure on both sides — brokers allocate per-message receive buffers; producers allocate per-batch send buffers. Confirm broker `replica.fetch.max.bytes` is also raised or replicas cannot replicate the oversized records. **Duration:** Topic config change is immediate; producer restart takes effect on rollout. **Verification:** Step 1 grep returns no new `RecordTooLargeException` lines.

### Cause G: Broker overloaded — request handler threads exhausted

**Statement:** The broker's request handler thread pool is fully saturated, causing produce requests to queue beyond `request.timeout.ms` and triggering `TimeoutException` on the producer.

**Chain:**
- root: high partition counts, large requests, or sustained throughput saturate the broker's shared `num.io.threads` pool (default 8).
- s1: produce requests queue in the network layer and `RequestHandlerAvgIdlePercent` falls below 0.30 (30% idle).
- s2: queue delay plus processing time exceeds `request.timeout.ms`, so the producer retries against the same overloaded pool.
- s3: retries are also delayed until `delivery.timeout.ms` is exceeded and messages expire.
- D: the producer raises `TimeoutException` while TCP connectivity is healthy (points at Symptom Recognition).

**Indicators:**
- s1: [Step 4] broker JMX `RequestHandlerAvgIdlePercent < 0.30` (30%)
- s2: [Step 5] `request_latency_avg` is high (>500 ms for a local cluster) — brokers are slow to respond
- D: [Step 1] `TimeoutException` appears in producer logs but Step 3 confirms TCP connectivity is healthy

**Interventions:**
- **remediation** (root): raise broker `num.io.threads` dynamically (no restart on Kafka 2.4+) to add request-handler capacity.

  ```bash
  # Raise broker io threads dynamically (no restart needed on Kafka 2.4+)
  kafka-configs.sh --bootstrap-server kafka1:9092 \
    --entity-type brokers --entity-name 0 \
    --alter --add-config num.io.threads=16
  # Verify the change is applied
  kafka-configs.sh --bootstrap-server kafka1:9092 \
    --entity-type brokers --entity-name 0 --describe \
    | grep num.io.threads
  ```

  **Verification:** Broker JMX `RequestHandlerAvgIdlePercent` rises above 0.50; Step 5 `request_latency_avg` drops to <100 ms; Step 1 grep shows no new `TimeoutException` lines.
- **mitigation** (s2): raise the producer's `request.timeout.ms` and `delivery.timeout.ms` so requests survive the current broker load until capacity is added.

  ```bash
  # Immediate: raise producer timeout to survive current broker load
  kubectl set env deployment/<app> \
    KAFKA_PRODUCER_REQUEST_TIMEOUT_MS=60000 \
    KAFKA_PRODUCER_DELIVERY_TIMEOUT_MS=300000
  kubectl rollout restart deployment/<app>
  ```

  **Risk:** Increasing `num.io.threads` allocates more kernel threads; verify broker host CPU headroom. Adding partitions or brokers changes topology and may require consumer group rebalances. **Duration:** Stop-gap. Durable fix requires broker capacity increase. **Verification:** Step 1 grep shows no new `TimeoutException` lines.

### Cause H: `linger.ms` misconfiguration adding unnecessary latency

**Statement:** `linger.ms` is set too high for the workload's latency requirements, so records sit in the batch buffer waiting for accumulation, consuming `delivery.timeout.ms` budget before the first send attempt.

**Chain:**
- root: `linger.ms` is larger than the workload's latency SLO (e.g. 500 ms for a 200 ms target).
- s1: each `send()` waits up to `linger.ms` in the batch buffer before transit even begins.
- s2: accumulation delay consumes most of the `delivery.timeout.ms` budget, leaving no room for retries.
- D: the first send attempt arrives just as `delivery.timeout.ms` expires (points at Symptom Recognition).

**Indicators:**
- s1: [Step 5] `record_queue_time_avg` is significantly higher than expected end-to-end latency budget
- root: [Step 2] `linger.ms` value is larger than the application's latency SLO (e.g. `linger.ms=500` for a 200 ms end-to-end target)
- s1: [Step 5] `batch_size_avg` is much smaller than `batch.size` — batches flush before filling, so `linger.ms` is not the bottleneck and could be increased for throughput

**Interventions:**
- **defensive_fix** (root): reduce `linger.ms` to 0–5 ms (with matching `batch.size`) for latency-sensitive workloads; raise it with `batch.size` for throughput-oriented ones.

  ```bash
  # For latency-sensitive workloads: reduce linger.ms to 0-5 ms
  # linger.ms=5 ; batch.size=16384
  # For throughput-oriented workloads: increase linger.ms and batch.size together
  # linger.ms=20 ; batch.size=65536 ; compression.type=lz4
  kubectl set env deployment/<app> \
    KAFKA_PRODUCER_LINGER_MS=5 \
    KAFKA_PRODUCER_BATCH_SIZE=16384
  kubectl rollout restart deployment/<app>
  ```

  **Verification:** Step 5 shows `record_queue_time_avg` approximates the new `linger.ms`; end-to-end produce-to-consumer latency measured via Step 6 falls within the application's SLO.

### Cause Z: Unidentified

**Statement:** Diagnostic steps confirm producers are failing to deliver messages but no Cause A–H indicator matched the gathered evidence; a less common origin (authorization, producer fence, ISR shrinkage, leadership storm) is likely.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic bundle and escalate to the Kafka platform on-call SME.

  ```bash
  # Collect a diagnostic bundle before escalation
  grep -E "TimeoutException|NetworkException|DisconnectException|AuthorizationException|ProducerFenced|NotEnoughReplicas|RecordTooLarge|BufferExhausted" \
    /var/log/<app>/producer.log | tail -100 > /tmp/producer-errors.txt
  kafka-broker-api-versions.sh --bootstrap-server kafka1:9092 > /tmp/broker-api.txt 2>&1
  kafka-configs.sh --bootstrap-server kafka1:9092 \
    --entity-type topics --entity-name <topic> --describe > /tmp/topic-config.txt
  tar czf /tmp/kafka-producer-bundle-$(date +%s).tgz \
    /tmp/producer-errors.txt /tmp/broker-api.txt /tmp/topic-config.txt
  ```

  **Risk:** Skipping messages by resetting to latest offset is destructive and bypasses business logic that depends on those records; acceptable only if data is reproducible or explicitly disposable. **Duration:** Minutes — collect, hand off, then escalate to the Kafka platform on-call with the affected topic, producer `client.id`, exception types, and the regression time window. **Verification:** Hand-off acknowledged by the receiving engineer; incident ticket opened with artefacts attached and a follow-up owner assigned.

## Prevention

- Always configure `acks=all`, `enable.idempotence=true`, and `retries=2147483647` for production topics; `acks=0` or `acks=1` is acceptable only for explicitly disposable telemetry streams.
- Set `min.insync.replicas=2` on all production topics — `acks=all` with `min.insync.replicas=1` gives no additional durability over `acks=1`.
- Set `delivery.timeout.ms=300000` (5 min) and `request.timeout.ms=60000` (1 min) as baseline production defaults; tune per workload but never set `delivery.timeout.ms < linger.ms + request.timeout.ms`.
- Monitor `record-error-rate` with an alert at >0 sustained for 2 minutes; monitor `buffer-available-bytes` with a warning when it drops below 20% of `buffer.memory`.
- Monitor `request-latency-avg` per producer; broker-side latency above 200 ms for a local cluster signals broker overload before it cascades to client timeouts.
- Enable `compression.type=lz4` on all producers for text/JSON workloads — typically 3–5x compression with low CPU cost, reducing buffer pressure and network bandwidth.
- Implement a producer error callback that logs failed messages with topic, partition, offset attempt, exception type, and timestamp for post-incident forensics.
- For Kubernetes deployments: verify `advertised.listeners` on each broker resolves to an address the producer can reach; test connectivity with `kafka-broker-api-versions.sh` from the producer pod before launch.
- Renew TLS certificates at least 30 days before expiry; alert on broker certificate `notAfter` within 30 days using a cron-based `openssl s_client` check or cert-manager certificate monitoring.
- Load-test producer behaviour under broker failure in staging: kill one broker and verify `acks=all` producers complete delivery without loss; kill two brokers on a 3-broker cluster and verify `min.insync.replicas=2` causes `NotEnoughReplicasException` rather than silent loss.
- Set `max.in.flight.requests.per.connection=5` with `enable.idempotence=true` to maintain ordering guarantees while allowing pipelining for throughput.

## Sources

- [Apache Kafka Documentation — Producer Configs](https://kafka.apache.org/documentation/#producerconfigs) — Priority 1. Authoritative defaults and descriptions for `delivery.timeout.ms=120000`, `request.timeout.ms=30000`, `linger.ms=0`, `batch.size=16384`, `buffer.memory=33554432`, `max.block.ms=60000`, `acks=all`, `retries=2147483647` (idempotence default), `max.in.flight.requests.per.connection=5`, `enable.idempotence=true`, `max.request.size=1048576`, `compression.type=none`, `connections.max.idle.ms=540000`.
- [Apache Kafka Documentation — Operations](https://kafka.apache.org/documentation/#operations) — Priority 1. Broker JMX metrics: `RequestHandlerAvgIdlePercent`, `TotalTimeMs` for produce requests, `UnderReplicatedPartitions`, `ISRShrinks`. Operational guidance on rolling restarts and dynamic broker configuration.
- [Confluent Platform — Kafka Monitoring](https://docs.confluent.io/platform/current/kafka/monitoring.html) — Priority 1. Producer-side JMX metrics: `record-error-rate`, `record-send-rate`, `batch-size-avg`, `record-queue-time-avg`, `buffer-available-bytes`, `waiting-threads`, `request-latency-avg`, `records-per-request-avg`. Metric naming conventions, threshold guidance, and Prometheus JMX exporter metric paths.
