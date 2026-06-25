---
id: "rabbitmq-queue-backlog"
title: "RabbitMQ Queue Backlog and Dead-Letter Accumulation"
domain: messaging
service: rabbitmq
symptom_class: [latency, throughput_degradation]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

**Chain:**
- root: aggregate consumer throughput (consumers × per-consumer ack rate) is below the publish rate
- s1: `messages_ready` grows continuously because deliveries cannot keep pace with publishes
- s2: backlog compounds until publisher flow control triggers or the queue hits its memory/disk limit
- D: queue backlog and end-to-end latency exceed SLA (Symptom Recognition)

**Indicators:**
- root: [Step 2] publish rate significantly exceeds ack rate with no sign of convergence
  <!-- match: {"step": 2, "predicate": "threshold", "target": "publish_rate_minus_ack_rate", "op": ">", "value": 10} -->
- s1: [Step 1] `consumers` count is low (1–2) while `messages_ready` is in the thousands or growing

**Interventions:**
- **remediation** (root): set a permanent replica count via deployment manifest or autoscaling policy so consumer capacity tracks load.

  ```bash
  kubectl apply -f consumer-hpa.yaml
  ```

  **Verification:** `messages_ready` on the affected queue decreases monotonically over 5–10 minutes; ack rate in Step 2 exceeds publish rate.
- **mitigation** (s1): scale the consumer deployment up immediately to drain the existing backlog.

  ```bash
  # Scale consumer deployment (Kubernetes example)
  kubectl scale deployment my-consumer --replicas=10

  # Verify new consumers registered
  rabbitmqctl list_consumers queue_name consumer_tag | grep "my-queue"
  ```

  **Risk:** Adding consumers increases broker connection and channel count cluster-wide; ensure broker `max_connections` headroom and monitor `rabbitmq_connections_total`. **Duration:** Consumer registration is immediate; keep scaled until backlog drains, then revert via `kubectl scale deployment my-consumer --replicas=<previous>`. **Verification:** ack rate in Step 2 rises above publish rate; `messages_ready` trends down.

### Cause B: Consumer prefetch count set too low

**Statement:** A `basic.qos` prefetch count of 1 (or similarly low value) prevents the broker from pipelining messages to consumers, capping single-consumer throughput to one round-trip latency per message.

**Chain:**
- root: consumer `basic.qos` prefetch count is set to 1 (or a similarly low value)
- s1: broker waits for each ack before delivering the next message, capping throughput to 1 message per round-trip
- s2: per-consumer throughput stays well below broker capacity, so `messages_ready` climbs
- D: queue backlog and latency exceed SLA (Symptom Recognition)

**Indicators:**
- root: [Step 3] `prefetch_count` is 1 for channels on the affected queue while `messages_ready` is large
  <!-- match: {"step": 3, "predicate": "contains", "target": "prefetch_count\t1"} -->
- s1: [Step 3] `messages_unacknowledged` is 1 per channel (matching the low prefetch)

**Interventions:**
- **remediation** (root): set prefetch in consumer application code and redeploy (50–100 for throughput-oriented consumers; use 1 only when strict per-message ordering is required).

  ```bash
  # Set prefetch in consumer application code, redeploy
  # Python pika: channel.basic_qos(prefetch_count=50)
  # Java:        channel.basicQos(50)
  ```

  **Verification:** `messages_unacknowledged` per channel rises to ~prefetch_count; ack rate in Step 2 increases proportionally; Step 3 shows the new prefetch value.
- **mitigation** (s1): raise prefetch via a config change and restart consumers to apply the new value immediately.

  ```bash
  # Update consumer application configuration (code-level change)
  # Set prefetch_count to 50, then restart consumer to apply
  ```

  **Risk:** Higher prefetch means more in-flight messages in consumer memory; on consumer crash they are redelivered (not lost if the queue is durable), increasing redelivery-storm risk. **Duration:** Effective immediately after consumer restart; keep as the new baseline. **Verification:** Step 3 shows the higher prefetch and `messages_unacknowledged` rising toward it.

### Cause C: Consumer requeue loop — nack with requeue=true without retry limit

**Statement:** Consumers issue `basic.nack` with `requeue=true` on every failure, sending messages back to the queue head indefinitely and creating a tight retry loop that starves other messages.

**Chain:**
- root: consumer code issues `basic.nack(requeue=true)` on every failure with no retry/attempt limit
- s1: a persistently failing message (DB unavailability, deserialization error) is requeued to the head and re-delivered immediately
- s2: the poison message cycles at broker delivery rate, burning consumer CPU and blocking downstream messages; `redeliver_details.rate` approaches `deliver_details.rate`
- D: backlog grows for later messages and latency exceeds SLA (Symptom Recognition)

**Indicators:**
- root: [Step 5] consumer logs show the same error message repeated at high frequency
- s2: [Step 2] `redeliver` rate is close to or exceeds `ack` rate
  <!-- match: {"step": 2, "predicate": "threshold", "target": "redeliver_rate_vs_ack_rate", "op": ">", "value": 0.5} -->
- s2: [Step 4] DLQ depth is zero despite consumer errors (no DLX configured, or nack requeues instead of routing to DLX)

**Interventions:**
- **remediation** (root): implement retry-with-backoff using the `x-death` header count; after N attempts route the poison pill to a permanent DLQ via `basic.nack(requeue=false)`.

  ```bash
  # Permanent fix: implement retry-with-backoff in consumer
  # Use x-death header count to detect poison pills after N retries
  # Route to permanent DLQ after max retries via basic.nack(requeue=false)
  ```

  **Verification:** Step 2 shows `redeliver` rate drops to near zero; DLQ in Step 4 begins receiving messages (poison pill routed, not looping).
- **loop_break** (s1): deploy consumer code that stops requeuing the poison message by dead-lettering after N attempts; confirm a DLX exists first so it is not discarded.

  ```bash
  # Verify DLX is configured before deploying the fix:
  rabbitmqctl list_queues name arguments | grep "x-dead-letter-exchange"
  # Deploy updated consumer: catch exceptions, increment x-death,
  # and basic.nack(requeue=false) after N attempts.
  ```

  **Verification:** Step 2 redeliver rate falls toward zero; the repeated error in Step 5 stops recurring.

### Cause D: Dead-letter queue has no consumer — rejected messages accumulate silently

**Statement:** A dead-letter exchange is configured but the dead-letter queue has no attached consumer, causing all rejected or expired messages to pile up indefinitely.

**Chain:**
- root: the dead-letter queue bound to the configured DLX has no attached consumer
- s1: every `basic.nack(requeue=false)` or TTL-expired message is routed to the DLQ but never processed
- s2: DLQ depth grows unbounded, consuming broker memory and disk while root-cause failures stay uninvestigated
- D: dead-letter accumulation and broker resource pressure appear (Symptom Recognition)

**Indicators:**
- root: [Step 4] DLQ row shows `consumers=0`
  <!-- match: {"step": 4, "predicate": "contains", "target": "consumers\t0"} -->
- s2: [Step 4] DLQ depth is non-zero and growing
  <!-- match: {"step": 4, "predicate": "threshold", "target": "dlq_messages", "op": ">", "value": 0} -->

**Interventions:**
- **remediation** (root): deploy a permanent DLQ consumer service that logs every dead-lettered message with `x-death` context, retries retriable errors with backoff, and archives + alerts on non-retriable ones.

  ```bash
  # Deploy a permanent DLQ consumer service that:
  # - Logs every dead-lettered message with x-death header context
  # - Applies retry logic with exponential backoff for retriable errors
  # - Archives non-retriable messages and fires an alert
  ```

  **Verification:** DLQ depth in Step 4 decreases; an alert fires when DLQ depth exceeds 0 going forward.
- **mitigation** (s2): drain the accumulated DLQ now — inspect messages, then redrive back to the original queue with the Shovel plugin if safe.

  ```bash
  # Start a dedicated DLQ inspector consumer that logs and acks each message
  # Or use the Management UI to peek at DLQ content (Get Messages)

  # Redrive DLQ messages back to original queue using Shovel plugin (if safe):
  rabbitmqctl set_parameter shovel my-dlq-redrive \
    '{"src-protocol":"amqp091","src-uri":"amqp://","src-queue":"my-queue.dlq",
      "dest-protocol":"amqp091","dest-uri":"amqp://","dest-queue":"my-queue",
      "src-delete-after":"queue-length"}'
  ```

  **Risk:** Redriving messages whose root cause is unfixed re-creates the original failure; only redrive once the consumer-side error is resolved. **Duration:** Shovel redrive completes when DLQ depth reaches zero; remove it afterward with `rabbitmqctl clear_parameter shovel my-dlq-redrive`. **Verification:** DLQ depth in Step 4 reaches zero and the shovel parameter is cleared.

### Cause E: Downstream dependency failure causing consumer processing stalls

**Statement:** An external dependency (database, API, cache) that consumers call during message processing has failed, causing all consumers to block until timeout and stop acknowledging messages.

**Chain:**
- root: a downstream dependency (database, API, cache) called during message processing has failed or is unreachable
- s1: consumers block on the downstream call holding messages unacknowledged until the client timeout fires
- s2: all `prefetch_count` slots fill with blocked messages so the broker delivers no new ones; the queue drains at zero rate
- D: queue backlog grows and latency exceeds SLA while the outage persists (Symptom Recognition)

**Indicators:**
- root: [Step 5] consumer logs contain downstream errors: `connection refused`, `timeout`, `ECONNREFUSED`, or `upstream connect error`
  <!-- match: {"step": 5, "predicate": "contains", "target": "connection refused"} -->
- s2: [Step 3] `messages_unacknowledged` equals `prefetch_count` across all channels for the affected queue
  <!-- match: {"step": 3, "predicate": "threshold", "target": "unack_to_prefetch_ratio", "op": ">=", "value": 1.0} -->

**Interventions:**
- **remediation** (root): add a circuit breaker plus explicit connection timeout (e.g. 5s) to all downstream calls in consumer code; on open circuit, fast-fail with `nack(requeue=false)` to the DLX.

  ```bash
  # Permanent fix: implement circuit breaker + timeout in consumer code
  # Set explicit connection timeout (e.g., 5s) for all downstream calls
  # Implement fast-fail path: if circuit is open, nack+requeue=false to DLX
  ```

  **Verification:** After downstream recovery, `messages_unacknowledged` drops from prefetch_count to normal; ack rate in Step 2 recovers to match publish rate.
- **mitigation** (s2): once the downstream is restored, force-close stalled consumer connections to free up the saturated prefetch slots.

  ```bash
  # Confirm the downstream is healthy first:
  curl -s --max-time 5 http://downstream-service/health

  # Force-close stalled consumer connections to free up queue slots:
  rabbitmqctl close_connection <conn_name> "upstream recovered"
  ```

  **Risk:** Restarting or force-closing consumers before the downstream is fixed just re-stalls them; fix the dependency first. **Duration:** Recovery is automatic once the downstream is restored; this only accelerates slot release. **Verification:** Step 3 `messages_unacknowledged` falls below `prefetch_count`; Step 2 ack rate recovers.

### Cause F: Message TTL too short — messages expire before consumers process them

**Statement:** The queue's `x-message-ttl` is configured shorter than the consumer processing time, causing messages to expire in-queue and route to the dead-letter exchange before being consumed.

**Chain:**
- root: the queue's `x-message-ttl` is set shorter than the actual consumer processing/throughput time
- s1: messages at the queue head expire and are dead-lettered (or discarded if no DLX) before a consumer reaches them
- s2: a large fraction of traffic is lost to expiry; `messages_ready` looks shallow while DLQ depth grows rapidly
- D: backlog manifests as message loss / dead-letter accumulation rather than depth (Symptom Recognition)

**Indicators:**
- root: [Step 6] `x-message-ttl` is set to a value (e.g., 60000 ms) shorter than observed consumer processing time
  <!-- match: {"step": 6, "predicate": "contains", "target": "x-message-ttl"} -->
- s2: [Step 4] DLQ depth grows rapidly even when consumers are active and `messages_ready` is low

**Interventions:**
- **remediation** (root): set TTL from the business SLA, not implementation convenience; if messages must never expire, remove the TTL policy and add `x-max-length` with `reject-publish` as a safety valve.

  ```bash
  # Set TTL based on business SLA; if messages must not expire, remove it:
  rabbitmqctl clear_policy ttl-fix
  ```

  **Verification:** DLQ growth rate in Step 4 drops to near zero; Step 6 shows the updated TTL; consumers process without expiry-related dead-lettering.
- **mitigation** (root): widen the TTL immediately via policy (no queue deletion required) so in-flight traffic stops expiring.

  ```bash
  # Update TTL via policy (preferred — does not require queue deletion)
  rabbitmqctl set_policy ttl-fix "^my-queue$" \
    '{"message-ttl": 3600000}' --apply-to queues

  # Verify policy applied
  rabbitmqctl list_policies
  ```

  **Risk:** Longer TTL keeps messages in-queue longer, raising memory pressure during bursts; existing messages keep their original TTL. **Duration:** Applies to new messages immediately; roll back with `rabbitmqctl set_policy ttl-fix "^my-queue$" '{"message-ttl": <old_value>}' --apply-to queues`. **Verification:** Step 6 shows the wider TTL and Step 4 DLQ growth slows.

### Cause G: Queue overflow set to drop-head — silent message loss masking the real backlog

**Statement:** The queue `x-overflow` policy is set to `drop-head`, causing the oldest messages to be silently discarded when the queue reaches `x-max-length`, masking the true accumulation rate.

**Chain:**
- root: the queue `x-overflow` policy is set to `drop-head` with an `x-max-length` limit
- s1: at the length limit, the broker silently drops the oldest message from the head for each new arrival
- s2: `messages_ready` appears bounded and publishers/consumers see no errors, so the real accumulation is masked as silent data loss
- D: downstream data inconsistencies surface instead of visible backlog (Symptom Recognition)

**Indicators:**
- root: [Step 6] `x-overflow: drop-head` is present in queue arguments or applied policy
  <!-- match: {"step": 6, "predicate": "contains", "target": "drop-head"} -->
- s1: [Step 6] `x-max-length` is set and `messages_ready` is exactly at that limit

**Interventions:**
- **remediation** (root): switch overflow to `reject-publish-dlx` so over-limit messages are preserved to a DLX and publishers receive correct backpressure.

  ```bash
  # Permanent: use reject-publish-dlx if messages must not be lost
  rabbitmqctl set_policy overflow-fix "^my-queue$" \
    '{"overflow": "reject-publish-dlx", "dead-letter-exchange": "my-exchange.dlx"}' \
    --apply-to queues
  ```

  **Verification:** Step 6 shows the new overflow mode; publishers handle `basic.return` / publisher-confirm failures when the queue is full, confirming backpressure is visible.
- **mitigation** (root): immediately stop the silent loss by switching overflow to `reject-publish`, surfacing the real backlog pressure to publishers.

  ```bash
  # Change overflow to reject-publish to apply backpressure instead of losing data
  rabbitmqctl set_policy overflow-fix "^my-queue$" \
    '{"overflow": "reject-publish"}' --apply-to queues
  ```

  **Risk:** Publishers will now receive `basic.return` for rejected messages or hit channel errors when the queue is full — intended, but unprepared publishers may error. **Duration:** Effective immediately for new messages; roll back with `rabbitmqctl clear_policy overflow-fix`. **Verification:** Step 6 shows `x-overflow: reject-publish`; publishers log message-return events when the queue is full.

### Cause Z: Unidentified

**Statement:** The queue backlog or dead-letter accumulation root cause could not be identified from the diagnostic steps above.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot (broker alarms, logs, cluster status) and escalate to the RabbitMQ administrator / SME.

  ```bash
  # Check broker-level alarms (memory, disk)
  rabbitmq-diagnostics alarms

  # Check broker logs for resource warnings or cluster partitions
  journalctl -u rabbitmq-server --since "1 hour ago" | grep -iE "alarm|partition|warning|error"

  # Check cluster health
  rabbitmq-diagnostics cluster_status
  ```

  **Risk:** Low — diagnostic-only commands with no change to the production system. **Duration:** One-off; escalate immediately with the captured output. **Verification:** N/A — escalation path; SME continues isolation from the snapshot.

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
