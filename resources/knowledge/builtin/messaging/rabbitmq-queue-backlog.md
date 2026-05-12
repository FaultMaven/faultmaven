---
id: "rabbitmq-queue-backlog"
title: "RabbitMQ Queue Backlog and Dead-Letter Accumulation"
domain: messaging
service: rabbitmq
symptom_class: [latency, throughput_degradation]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [rabbitmq, queue, backlog, dead-letter, dlx, prefetch, consumer, quorum]
difficulty: intermediate
---

## Symptom Recognition

- `messages_ready` metric on affected queues grows steadily in the RabbitMQ Management UI (port 15672)
- `rabbitmqctl list_queues` shows queues with high `messages` count and zero or very few `consumers`
- End-to-end message latency (publish timestamp to consumer acknowledgment) grows beyond SLA threshold
- Dead-letter queue depth increases — indicating repeated consumer rejections or TTL expiry
- Prometheus metric `rabbitmq_queue_messages` grows monotonically for specific queues
- Publisher connections enter `flow` or `blocked` state if backlog triggers a memory alarm
- Consumer `basic.nack` or `basic.reject` rate increases in Management UI statistics tab
- Consumer application logs show processing errors, downstream timeouts, or unhandled exceptions
- `redeliver_details.rate` is elevated relative to `ack_details.rate` — indicating retry loops

## Applicability

Applies to RabbitMQ 3.10+ (classic queues, quorum queues, and stream queues). Requires CLI access to `rabbitmqctl` and `rabbitmq-diagnostics`, plus HTTP access to the RabbitMQ Management API (default port 15672). Consumer applications must use manual acknowledgment mode (`basic.ack` / `basic.nack` / `basic.reject`). Does not cover MQTT or STOMP protocol consumers.

## Diagnostic Steps

### Step 1: Identify queues with growing backlogs

```bash
rabbitmqctl list_queues name messages messages_ready messages_unacknowledged consumers memory \
  --sort-by messages
```

Expected output: Queues sorted by total message count. Queues with high `messages_ready` and low `consumers` are accumulating. Queues with `consumers=0` are fully stalled.

### Step 2: Check publish rate vs acknowledge rate per queue

```bash
curl -s -u guest:guest \
  "http://localhost:15672/api/queues?sort=messages&sort_reverse=true&page_size=20" | \
  python3 -c "
import sys, json
queues = json.load(sys.stdin)
for q in queues:
    pub = q.get('message_stats', {}).get('publish_details', {}).get('rate', 0)
    ack = q.get('message_stats', {}).get('ack_details', {}).get('rate', 0)
    redeliver = q.get('message_stats', {}).get('redeliver_details', {}).get('rate', 0)
    print(f\"{q['name']:50s} msgs={q.get('messages',0):>8,} consumers={q.get('consumers',0):>3} pub={pub:.1f}/s ack={ack:.1f}/s redeliver={redeliver:.1f}/s\")
"
```

Expected output: Queues where publish rate exceeds ack rate are actively accumulating. High `redeliver` rate signals a requeue loop.

### Step 3: Check consumer prefetch count per channel

```bash
rabbitmqctl list_channels name messages_unacknowledged prefetch_count consumer_count
```

Expected output: Each row shows one channel. If `messages_unacknowledged` equals `prefetch_count` for many channels, consumers are prefetch-saturated and processing is stalled. A `prefetch_count` of 1 is the throughput bottleneck indicator.

### Step 4: Inspect dead-letter queue configuration and depth

```bash
# Find queues with a dead-letter exchange configured
rabbitmqctl list_queues name arguments | grep -i "dead-letter\|x-dead-letter"

# Check depth of dead-letter queues
rabbitmqctl list_queues name messages consumers | grep -iE "dlq|dead|\.error|\.retry"

# Check cluster-level redeliver and return rates
curl -s -u guest:guest http://localhost:15672/api/overview | \
  python3 -c "
import sys, json
data = json.load(sys.stdin)
stats = data.get('message_stats', {})
print('Deliver rate:    ', stats.get('deliver_details',{}).get('rate',0))
print('Ack rate:        ', stats.get('ack_details',{}).get('rate',0))
print('Redeliver rate:  ', stats.get('redeliver_details',{}).get('rate',0))
print('Return unrouted: ', stats.get('return_unroutable_details',{}).get('rate',0))
"
```

Expected output: Dead-letter queues should have zero or negligible depth. A non-zero and growing DLQ depth indicates consumer-side processing failures. High `redeliver_details.rate` without matching `ack_details.rate` confirms a retry loop.

### Step 5: Check consumer application logs for processing errors

```bash
grep -E "error|exception|timeout|nack|reject|failed|refused|OOM" \
  /var/log/consumer/consumer.log | tail -40

grep -E "connection closed|channel closed|heartbeat missed|IOException|broken pipe" \
  /var/log/consumer/consumer.log | tail -20
```

Expected output: Specific error classes (database connection failures, downstream API timeouts, deserialization exceptions, poison pill stack traces). Connection-close errors indicate heartbeat or network issues, not application logic failures.

### Step 6: Check queue configuration for TTL, length limits, and overflow policy

```bash
# Show queue arguments including TTL, max-length, overflow, and DLX settings
rabbitmqctl list_queues name type durable arguments

# Check applied policies
rabbitmqctl list_policies

# Cross-check overflow behavior (drop-head causes silent message loss)
rabbitmqctl list_queues name arguments | grep -E "x-message-ttl|x-max-length|x-overflow"
```

Expected output: Queues with `x-overflow: drop-head` discard old messages silently when full. Queues with `x-message-ttl` shorter than consumer processing time will expire messages before they are consumed. Absence of `x-dead-letter-exchange` means rejected or expired messages are discarded permanently.

## Causes

### Cause A: Insufficient consumer instances for the message rate

**Statement:** The number of consumer processes is too low to drain the queue at the incoming publish rate, causing messages to accumulate.

**Mechanism:** RabbitMQ delivers messages round-robin across available consumers. When the aggregate consumer throughput (consumers × per-consumer ack rate) falls below the publish rate, `messages_ready` grows continuously. Without additional consumers, the backlog compounds until either publisher flow control is triggered or the queue hits its memory/disk limit.

**Indicator:**

- [Step 1] `consumers` count is low (1–2) while `messages_ready` is in the thousands or growing
- [Step 2] publish rate significantly exceeds ack rate with no sign of convergence
<!-- match: {"step": 2, "predicate": "threshold", "target": "publish_rate_minus_ack_rate", "op": ">", "value": 10} -->

**Mitigation:**

- **Risk:** Adding consumers increases broker connection count and channel count. Ensure broker `max_connections` headroom.
- **Command:**

  ```bash
  # Scale consumer deployment (Kubernetes example)
  kubectl scale deployment my-consumer --replicas=10

  # Verify new consumers registered
  rabbitmqctl list_consumers queue_name consumer_tag | grep "my-queue"
  ```

- **Duration:** Consumer registration is immediate; backlog drain time depends on depth and per-message processing time.

**Resolution:**

```bash
# Set permanent replica count via deployment manifest or autoscaling policy
kubectl apply -f consumer-hpa.yaml
```

- **Impact:** Additional pods increase broker connection/channel load cluster-wide; monitor `rabbitmq_connections_total`.
- **Rollback:** `kubectl scale deployment my-consumer --replicas=<previous>`

**Verification:** `messages_ready` on the affected queue decreases monotonically over 5–10 minutes. Ack rate in Step 2 exceeds publish rate.

### Cause B: Consumer prefetch count set too low

**Statement:** A `basic.qos` prefetch count of 1 (or similarly low value) prevents the broker from pipelining messages to consumers, capping single-consumer throughput to one round-trip latency per message.

**Mechanism:** With `prefetch_count=1`, the broker waits for the consumer to acknowledge each message before delivering the next. At 100 ms processing time, throughput is capped at 10 msg/s per consumer regardless of broker capacity. Increasing prefetch allows the broker to keep the consumer's processing pipeline full, multiplying effective throughput by the prefetch depth.

**Indicator:**

- [Step 3] `prefetch_count` is 1 for channels on the affected queue while `messages_ready` is large
<!-- match: {"step": 3, "predicate": "contains", "target": "prefetch_count\t1"} -->
- [Step 3] `messages_unacknowledged` is 1 per channel (matching the low prefetch)

**Mitigation:**

- **Risk:** Higher prefetch means more messages are in-flight in consumer memory. If the consumer crashes, those messages are redelivered (not lost if queue is durable), increasing redelivery storm risk.
- **Command:**

  ```bash
  # Update consumer application configuration (code-level change)
  # Python pika example: channel.basic_qos(prefetch_count=50)
  # Java example: channel.basicQos(50)
  # Restart consumer after config change to apply new prefetch
  ```

- **Duration:** Effective immediately after consumer restart.

**Resolution:**

```bash
# Set prefetch in consumer application code, redeploy
# Recommended: 50–100 for throughput-oriented consumers
# Use prefetch=1 only when strict per-message ordering is required
```

**Verification:** `messages_unacknowledged` per channel rises to ~prefetch_count. Ack rate in Step 2 increases proportionally. Step 3 shows new prefetch value.

### Cause C: Consumer requeue loop — nack with requeue=true without retry limit

**Statement:** Consumers issue `basic.nack` with `requeue=true` on every failure, sending messages back to the queue head indefinitely and creating a tight retry loop that starves other messages.

**Mechanism:** When a consumer requeues a message, RabbitMQ places it back at the front of the queue (for classic queues) and immediately re-delivers it to the same or another consumer. If the underlying error (database unavailability, deserialization failure) is persistent, the message cycles at the broker's delivery rate, consuming consumer CPU and preventing downstream messages from being processed. `redeliver_details.rate` approaches `deliver_details.rate`, and `messages_ready` for later messages climbs.

**Indicator:**

- [Step 2] `redeliver` rate is close to or exceeds `ack` rate
<!-- match: {"step": 2, "predicate": "threshold", "target": "redeliver_rate_vs_ack_rate", "op": ">", "value": 0.5} -->
- [Step 4] DLQ depth is zero despite consumer errors (no DLX configured, or nack sends requeue=true instead of routing to DLX)
- [Step 5] Consumer logs show the same error message repeated at high frequency

**Mitigation:**

- **Risk:** Changing nack behavior to `requeue=false` will dead-letter or discard messages immediately; ensure a DLX is configured before deploying the fix.
- **Command:**

  ```bash
  # Deploy updated consumer code that:
  # 1. Catches exceptions
  # 2. Increments x-death retry counter
  # 3. Dead-letters after N attempts via basic.nack(requeue=false)
  # Verify DLX is configured first:
  rabbitmqctl list_queues name arguments | grep "x-dead-letter-exchange"
  ```

- **Duration:** Fix takes effect immediately on consumer redeploy.

**Resolution:**

```bash
# Permanent fix: implement retry-with-backoff in consumer
# Use x-death header count to detect poison pills after N retries
# Route to permanent DLQ after max retries via basic.nack(requeue=false)
```

**Verification:** Step 2 shows `redeliver` rate drops to near zero. DLQ in Step 4 begins receiving messages (confirming the poison pill is correctly routed, not looping).

### Cause D: Dead-letter queue has no consumer — rejected messages accumulate silently

**Statement:** A dead-letter exchange is configured but the dead-letter queue has no attached consumer, causing all rejected or expired messages to pile up indefinitely.

**Mechanism:** When a consumer issues `basic.nack(requeue=false)` or a message TTL expires, RabbitMQ routes the message to the configured dead-letter exchange. If the DLQ bound to that exchange has no consumer, messages accumulate without being processed, inspected, or alerted on. Over time, the DLQ consumes broker memory and disk, and the root-cause failures remain uninvestigated.

**Indicator:**

- [Step 4] DLQ depth is non-zero and growing
<!-- match: {"step": 4, "predicate": "threshold", "target": "dlq_messages", "op": ">", "value": 0} -->
- [Step 4] DLQ row shows `consumers=0`
<!-- match: {"step": 4, "predicate": "contains", "target": "consumers\t0"} -->

**Mitigation:**

- **Risk:** Low. Attaching a consumer to the DLQ is read-only relative to the main queue.
- **Command:**

  ```bash
  # Start a dedicated DLQ inspector consumer that logs and acks each message
  # Or use the Management UI to peek at DLQ message content (Get Messages)

  # Redrive DLQ messages back to original queue using Shovel plugin (if safe):
  rabbitmqctl set_parameter shovel my-dlq-redrive \
    '{"src-protocol":"amqp091","src-uri":"amqp://","src-queue":"my-queue.dlq",
      "dest-protocol":"amqp091","dest-uri":"amqp://","dest-queue":"my-queue",
      "src-delete-after":"queue-length"}'
  ```

- **Duration:** Shovel redrive completes when DLQ depth reaches zero.

**Resolution:**

```bash
# Deploy a permanent DLQ consumer service that:
# - Logs every dead-lettered message with x-death header context
# - Applies retry logic with exponential backoff for retriable errors
# - Archives non-retriable messages and fires an alert

# Remove the temporary shovel after redrive completes:
rabbitmqctl clear_parameter shovel my-dlq-redrive
```

**Verification:** DLQ depth in Step 4 decreases. An alert fires in your monitoring system when DLQ depth exceeds 0 going forward.

### Cause E: Downstream dependency failure causing consumer processing stalls

**Statement:** An external dependency (database, API, cache) that consumers call during message processing has failed, causing all consumers to block until timeout and stop acknowledging messages.

**Mechanism:** Consumers fetch messages from the queue (counted as `messages_unacknowledged`) and begin processing. If the downstream call blocks waiting for a connection that never comes, the consumer holds the message unacknowledged until the client timeout fires. With `prefetch_count` slots fully occupied by blocked messages, no new messages are delivered. The queue drains at zero rate while the downstream outage persists.

**Indicator:**

- [Step 3] `messages_unacknowledged` equals `prefetch_count` across all channels for the affected queue
<!-- match: {"step": 3, "predicate": "threshold", "target": "unack_to_prefetch_ratio", "op": ">=", "value": 1.0} -->
- [Step 5] Consumer logs contain downstream errors: `connection refused`, `timeout`, `ECONNREFUSED`, or `upstream connect error`
<!-- match: {"step": 5, "predicate": "contains", "target": "connection refused"} -->

**Mitigation:**

- **Risk:** Low. Restarting consumer without fixing the downstream causes it to re-stall. Fix the downstream dependency first.
- **Command:**

  ```bash
  # Identify the failing downstream service
  # Check its health endpoint or connection from consumer host:
  curl -s --max-time 5 http://downstream-service/health

  # If dependency is temporary, consumers will self-recover once it returns
  # Force-close stalled consumer connections to free up queue slots:
  rabbitmqctl close_connection <conn_name> "upstream recovered"
  ```

- **Duration:** Recovery is automatic once the downstream dependency is restored.

**Resolution:**

```bash
# Permanent fix: implement circuit breaker + timeout in consumer code
# Set explicit connection timeout (e.g., 5s) for all downstream calls
# Implement fast-fail path: if circuit is open, nack+requeue=false to DLX
```

**Verification:** After downstream recovery, `messages_unacknowledged` drops from prefetch_count to normal levels. Ack rate in Step 2 recovers to match publish rate.

### Cause F: Message TTL too short — messages expire before consumers process them

**Statement:** The queue's `x-message-ttl` is configured shorter than the consumer processing time, causing messages to expire in-queue and route to the dead-letter exchange before being consumed.

**Mechanism:** RabbitMQ evaluates message TTL lazily at the head of the queue. When a message at the head expires, it is dead-lettered or discarded (if no DLX is configured). If consumer throughput is low (due to slow processing or insufficient count), a large fraction of messages expire before being consumed. The queue may appear shallow in `messages_ready` while the DLQ grows rapidly — the backlog manifests as message loss rather than depth.

**Indicator:**

- [Step 6] `x-message-ttl` is set to a value (e.g., 60000 ms) shorter than observed consumer processing time
<!-- match: {"step": 6, "predicate": "contains", "target": "x-message-ttl"} -->
- [Step 4] DLQ depth grows rapidly even when consumers are active and `messages_ready` is low

**Mitigation:**

- **Risk:** Increasing TTL keeps messages in-queue longer, potentially increasing memory pressure during bursts.
- **Command:**

  ```bash
  # Update TTL via policy (preferred — does not require queue deletion)
  rabbitmqctl set_policy ttl-fix "^my-queue$" \
    '{"message-ttl": 3600000}' --apply-to queues

  # Verify policy applied
  rabbitmqctl list_policies
  ```

- **Duration:** Policy changes apply to new messages immediately; existing messages retain their original TTL.

**Resolution:**

```bash
# Set TTL based on business SLA, not implementation convenience
# If messages must not expire, remove the TTL policy entirely:
rabbitmqctl clear_policy ttl-fix
```

- **Impact:** Removing TTL means queue can grow unbounded during consumer outages; set `x-max-length` with `reject-publish` overflow as a safety valve.
- **Rollback:** `rabbitmqctl set_policy ttl-fix "^my-queue$" '{"message-ttl": <old_value>}' --apply-to queues`

**Verification:** DLQ growth rate in Step 4 drops to near zero. Step 6 shows updated TTL. Consumer application processes messages without expiry-related dead-lettering.

### Cause G: Queue overflow set to drop-head — silent message loss masking the real backlog

**Statement:** The queue `x-overflow` policy is set to `drop-head`, causing the oldest messages to be silently discarded when the queue reaches `x-max-length`, masking the true accumulation rate.

**Mechanism:** When a queue reaches its `x-max-length` limit with `drop-head` overflow, RabbitMQ drops the oldest message from the head to make room for each new arrival. The `messages_ready` metric appears stable (bounded), but messages are being lost. Publishers see no errors and consumers see no backlog — the symptom is silent data loss, not queue depth growth. This masking prevents escalation until downstream data inconsistencies appear.

**Indicator:**

- [Step 6] `x-overflow: drop-head` is present in queue arguments or applied policy
<!-- match: {"step": 6, "predicate": "contains", "target": "drop-head"} -->
- [Step 6] `x-max-length` is set and `messages_ready` is exactly at that limit

**Mitigation:**

- **Risk:** Changing overflow to `reject-publish` will cause publishers to receive `basic.return` for rejected messages or encounter channel errors, surfacing the real backlog pressure.
- **Command:**

  ```bash
  # Change overflow to reject-publish to apply backpressure instead of losing data
  rabbitmqctl set_policy overflow-fix "^my-queue$" \
    '{"overflow": "reject-publish"}' --apply-to queues
  ```

- **Duration:** Effective immediately for new messages.

**Resolution:**

```bash
# Permanent: use reject-publish-dlx if messages must not be lost
rabbitmqctl set_policy overflow-fix "^my-queue$" \
  '{"overflow": "reject-publish-dlx", "dead-letter-exchange": "my-exchange.dlx"}' \
  --apply-to queues
```

- **Impact:** Publishers will need to handle `basic.return` callbacks or `publisher confirms` failures when the queue is full — this is the correct backpressure signal.
- **Rollback:** `rabbitmqctl clear_policy overflow-fix`

**Verification:** Step 6 shows `x-overflow: reject-publish`. Publisher applications log message-return events when queue is full, confirming backpressure is now visible.

### Cause Z: Unidentified — queue backlog root cause not determined by steps above

**Statement:** The queue backlog or dead-letter accumulation root cause could not be identified from the diagnostic steps.

**Mechanism:** Complex multi-cause scenarios (e.g., network partition causing consumer disconnects combined with TTL expiry, or a broker-level resource alarm suppressing delivery) may not reduce to a single indicator. Broker-level logs and cluster health diagnostics are required for further isolation.

**Indicator:**

- [Default] None of the above causes match the observed diagnostics

**Mitigation:**

- **Risk:** Low. Diagnostic-only steps.
- **Command:**

  ```bash
  # Check broker-level alarms (memory, disk)
  rabbitmq-diagnostics alarms

  # Check broker logs for resource warnings or cluster partitions
  journalctl -u rabbitmq-server --since "1 hour ago" | grep -iE "alarm|partition|warning|error"

  # Check cluster health
  rabbitmq-diagnostics cluster_status
  ```

- **Duration:** Diagnostic only; no change to production system.

**Resolution:** Out of runbook scope. Escalate to RabbitMQ administrator with output of all diagnostic steps and broker logs.

**Verification:** N/A — escalation path.

## Prevention

- Configure a dead-letter exchange (`x-dead-letter-exchange`) on every production queue to capture rejected and expired messages instead of discarding them silently.
- Deploy a DLQ consumer for every dead-letter queue that logs, alerts, and optionally retries messages with exponential backoff; never leave a DLQ unmonitored.
- Set `basic.qos` prefetch count to 50–100 for throughput-oriented consumers; a prefetch of 1 limits single-consumer throughput to one round-trip per message.
- Implement retry limits in consumer logic using the `x-death` header count; never use `basic.nack(requeue=true)` without a maximum retry counter.
- Set queue overflow to `reject-publish` or `reject-publish-dlx` — not `drop-head` — to surface backpressure to publishers rather than discarding messages silently.
- Monitor `rabbitmq_queue_messages_ready` per queue and alert when depth exceeds a threshold (e.g., 5 minutes of production throughput equivalent).
- Monitor dead-letter queue depth and alert on any non-zero value within 1 minute.
- Use quorum queues for durability-critical queues; they provide built-in replication and better memory management than classic mirrored queues.
- Set message TTL based on actual business SLAs; validate that consumer processing time is comfortably below the TTL value under degraded conditions.
- Implement publisher confirms to detect when the broker cannot accept messages; treat rejected publishes as a signal to pause and alert.
- Plan consumer capacity for 2–3× normal throughput to absorb burst traffic and allow catch-up after maintenance windows.
- Use separate queues per message priority with dedicated consumer pools to prevent low-priority backlogs from blocking high-priority processing.

## Sources

- [RabbitMQ Documentation — Dead Lettering](https://www.rabbitmq.com/docs/dlx) — DLX configuration, x-death headers, triggers for dead-lettering, priority 1
- [RabbitMQ Documentation — Consumer Prefetch](https://www.rabbitmq.com/docs/consumer-prefetch) — basic.qos semantics, channel vs connection prefetch, throughput impact, priority 1
- [RabbitMQ Documentation — Queue Length Limits](https://www.rabbitmq.com/docs/maxlength) — x-max-length, x-max-length-bytes, overflow modes (drop-head, reject-publish, reject-publish-dlx), priority 1
- [RabbitMQ Documentation — Quorum Queues](https://www.rabbitmq.com/docs/quorum-queues) — quorum queue durability, replication, and memory behavior vs classic queues, priority 1
- [RabbitMQ Documentation — Monitoring](https://www.rabbitmq.com/docs/monitoring) — Management API endpoints, key metrics (messages_ready, messages_unacknowledged, redeliver_details), priority 1
- [RabbitMQ Documentation — Troubleshooting](https://www.rabbitmq.com/docs/troubleshooting) — CLI diagnostics, alarms, cluster status, priority 1
