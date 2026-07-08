---
id: "pubsub-subscription-backlog"
title: "GCP Pub/Sub Subscription Backlog: Undelivered Messages Growing"
domain: messaging
service: gcp-pubsub
symptom_class: [latency, throughput_degradation]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [num-undelivered-messages, oldest-unacked-message-age, ack-deadline, flow-control, ordering-key]
difficulty: advanced
---

## Symptom Recognition

- `subscription/num_undelivered_messages` rising and not draining (backlog count climbing).
- `subscription/oldest_unacked_message_age` increasing in seconds, trending toward the subscription's message retention duration.
- `subscription/backlog_bytes` growing over time.
- End-to-end delivery latency increasing; consumers process events minutes/hours late.
- Monitoring alert: "oldest unacked message age exceeded threshold" or backlog SLO breach.
- Cloud Monitoring "Delivery latency health score" chart flags contributing factors (expired acks, high redelivery, too few open StreamingPull connections).

## Applicability

- Service: Google Cloud Pub/Sub pull or StreamingPull subscriptions (push subscriptions share the ack-deadline and flow-control causes).
- Access: IAM roles `roles/pubsub.viewer` (describe/metrics) and `roles/pubsub.editor` (update subscription, seek). `roles/monitoring.viewer` to read metrics.
- Tools: `gcloud` CLI (Cloud SDK), Cloud Monitoring (Metrics Explorer), the subscriber client library config (flow control / lease management).
- Region/project context known: `PROJECT_ID`, `SUBSCRIPTION_ID`, `TOPIC_ID`.

## Diagnostic Steps

### Step 1: Inspect subscription configuration

```bash
gcloud pubsub subscriptions describe SUBSCRIPTION_ID \
  --project=PROJECT_ID
```

Expected output: shows `ackDeadlineSeconds`, `messageRetentionDuration`, `enableMessageOrdering`, `enableExactlyOnceDelivery`, `deadLetterPolicy`, and `retryPolicy`. Note the ack deadline and whether ordering/exactly-once are enabled.

### Step 2: Read the backlog and delivery metrics

```bash
gcloud monitoring time-series list \
  --project=PROJECT_ID \
  --filter='metric.type="pubsub.googleapis.com/subscription/num_undelivered_messages" AND resource.label.subscription_id="SUBSCRIPTION_ID"'
```

Expected output: time series for `subscription/num_undelivered_messages`. Repeat with `subscription/oldest_unacked_message_age` and `subscription/expired_ack_deadlines_count` to see whether the backlog count, the age, and the ack-expiry rate are all climbing.

### Step 3: Sample undelivered messages without acking

```bash
gcloud pubsub subscriptions pull SUBSCRIPTION_ID \
  --project=PROJECT_ID \
  --limit=10 \
  --format='value(message.orderingKey, message.publishTime)'
```

Expected output: up to 10 messages with their `orderingKey` and `publishTime`. Many rows sharing one `orderingKey` (or old `publishTime`s) indicates a hot ordering key or stalled delivery. (Omit `--auto-ack` so you do not consume the backlog.)

### Step 4: Check open StreamingPull connections and ack latency

```bash
gcloud monitoring time-series list \
  --project=PROJECT_ID \
  --filter='metric.type="pubsub.googleapis.com/subscription/open_streaming_pulls" AND resource.label.subscription_id="SUBSCRIPTION_ID"'
```

Expected output: count of open StreamingPull streams. A value at/near zero (or far fewer than your subscriber replica count) while backlog grows indicates too little subscriber capacity. Cross-reference `subscription/ack_latencies` for slow processing.

## Causes

### Cause A: Ack deadline too short for processing time

**Statement:** The subscription's `ackDeadlineSeconds` is shorter than the subscriber's actual per-message processing time, so messages are not acked before the deadline expires and Pub/Sub redelivers them, inflating the backlog with repeated deliveries.
**Chain:**
- root: ack deadline shorter than real processing time
- s1: messages exceed the deadline and ack-deadline expirations accumulate
- s2: Pub/Sub redelivers expired messages, multiplying outstanding/duplicate work
- D: undelivered-message backlog and oldest-unacked age grow
**Indicators:**
- root: [Step 1] `ackDeadlineSeconds` is low (e.g. 10) relative to known processing latency
- s1: [Step 2] `subscription/expired_ack_deadlines_count` is non-zero and rising
- s2: [Symptom] same messages reappear; delivery-latency health score flags expired acks
**Interventions:**
- **remediation** (root): raise the subscription ack deadline to comfortably exceed p99 processing time (max 600s), and rely on the client library to auto-extend the lease for in-flight work.

  ```bash
  gcloud pubsub subscriptions update SUBSCRIPTION_ID \
    --project=PROJECT_ID \
    --ack-deadline=120
  ```

  **Verification:** re-run Step 2; `subscription/expired_ack_deadlines_count` falls to ~0 and `num_undelivered_messages` begins draining.
- **defensive_fix** (s1): ensure the subscriber uses lease management (modifyAckDeadline / client auto-extension) so long-running messages keep extending their deadline instead of expiring; keep flow control aligned (see Cause B).

  ```bash
  gcloud pubsub subscriptions describe SUBSCRIPTION_ID \
    --project=PROJECT_ID \
    --format='value(ackDeadlineSeconds)'
  ```

  **Verification:** ack latencies stay below the (extended) deadline; expired-ack count stays flat under load.

### Cause B: Flow control limit set too high for subscriber capacity

**Statement:** The subscriber's flow-control `max_outstanding_messages` lets it pull more messages than it can process and ack within the deadline, so leases expire and redelivered duplicates overflow the subscriber, exacerbating the backlog.
**Chain:**
- root: max_outstanding_messages exceeds real subscriber throughput
- s1: subscriber holds more outstanding messages than it can ack in time
- s2: deadlines expire and duplicates are redelivered, overflowing the subscriber
- D: backlog and oldest-unacked age keep growing
**Indicators:**
- root: [Step 4] `subscription/ack_latencies` high while `open_streaming_pulls` is low — subscriber saturated
- s1: [Step 2] `subscription/expired_ack_deadlines_count` rising alongside duplicate deliveries
- s2: [Symptom] subscriber CPU/memory pegged; same messages delivered repeatedly
**Interventions:**
- **remediation** (root): reduce `max_outstanding_messages` (and outstanding bytes) in the subscriber's flow-control config to match measured per-instance throughput, so the client pulls only what it can ack in time. Example (Python client):

  ```python
  from google.cloud import pubsub_v1
  flow_control = pubsub_v1.types.FlowControl(max_messages=100, max_bytes=10 * 1024 * 1024)
  subscriber.subscribe(subscription_path, callback=cb, flow_control=flow_control)
  ```

  **Verification:** re-run Step 2; expired-ack count drops and `num_undelivered_messages` trends down as duplicates stop.
- **defensive_fix** (s1): scale out subscriber replicas / maintain multiple open StreamingPull connections so total capacity exceeds publish rate.

  ```bash
  gcloud monitoring time-series list \
    --project=PROJECT_ID \
    --filter='metric.type="pubsub.googleapis.com/subscription/open_streaming_pulls" AND resource.label.subscription_id="SUBSCRIPTION_ID"'
  ```

  **Verification:** `subscription/open_streaming_pulls` rises with replica count and backlog drains.

### Cause C: Slow or under-provisioned subscribers

**Statement:** The aggregate subscriber fleet processes messages slower than the publish rate (insufficient replicas, regressed code, or resource exhaustion), so messages accumulate faster than they are acked.
**Chain:**
- root: subscriber processing throughput below publish throughput
- s1: messages arrive faster than they are acked
- s2: unacked messages accumulate in the subscription
- D: num_undelivered_messages and oldest_unacked_message_age rise together
**Indicators:**
- root: [Step 4] high `subscription/ack_latencies` and few `open_streaming_pulls` vs. replica count
- s1: [Step 2] both `num_undelivered_messages` and `oldest_unacked_message_age` increase simultaneously
- s2: [Symptom] subscriber CPU/memory/network saturated; recent deploy correlates with onset
**Interventions:**
- **remediation** (root): scale subscriber capacity up to exceed publish throughput (more replicas/threads); use Pub/Sub backlog metrics as the autoscaling signal so capacity tracks load.

  ```bash
  gcloud monitoring time-series list \
    --project=PROJECT_ID \
    --filter='metric.type="pubsub.googleapis.com/subscription/num_undelivered_messages" AND resource.label.subscription_id="SUBSCRIPTION_ID"'
  ```

  **Verification:** re-run Step 2; `num_undelivered_messages` decreases and `oldest_unacked_message_age` stops climbing.
- **mitigation** (s2): if the backlog is unprocessable junk (bad deploy, poison data) and you must restore freshness, seek the subscription forward to a recent timestamp to drop old undelivered messages.

  ```bash
  gcloud pubsub subscriptions seek SUBSCRIPTION_ID \
    --project=PROJECT_ID \
    --time=2026-06-24T12:00:00Z
  ```

  **Risk:** permanently discards all unacked messages published before the seek time (data loss). **Duration:** one-time recovery action; do not repeat routinely. **Verification:** `oldest_unacked_message_age` drops to near zero immediately after seek.

### Cause D: Hot ordering key causing head-of-line blocking

**Statement:** Message ordering is enabled and one ordering key concentrates throughput, so a single slow or nacked message blocks all later messages for that key (and forces redelivery of subsequent already-acked messages), serializing delivery and stalling the backlog for that key.
**Chain:**
- root: traffic skewed onto one ordering key with ordering enabled
- s1: messages for that key must deliver strictly in sequence (1 MBps per-key cap)
- s2: a stuck/nacked message blocks and redelivers all later messages for the key
- D: oldest_unacked_message_age rises and backlog for that key cannot drain
**Indicators:**
- root: [Step 1] `enableMessageOrdering: true` on the subscription
- s1: [Step 3] pulled sample shows many messages sharing one `orderingKey`
- s2: [Step 2] `subscription/oldest_unacked_message_age` rising while overall throughput is low — hot-key signal
**Interventions:**
- **remediation** (root): redistribute publish traffic across many distinct ordering keys (higher-cardinality key) so per-key serialization no longer bottlenecks the fleet; if strict order is unnecessary, publish without ordering keys to a non-ordered subscription.

  ```bash
  gcloud pubsub subscriptions create SUBSCRIPTION_ID_UNORDERED \
    --project=PROJECT_ID \
    --topic=TOPIC_ID
  ```

  **Verification:** re-run Step 3 — pulled messages span many ordering keys; `oldest_unacked_message_age` declines as per-key blocking clears.
- **defensive_fix** (s2): stop nacking the blocking message — ack it (after routing failures to a dead-letter topic) so the ordered key can advance instead of redelivering the whole sequence.

  ```bash
  gcloud pubsub subscriptions update SUBSCRIPTION_ID \
    --project=PROJECT_ID \
    --dead-letter-topic=DLQ_TOPIC_ID \
    --max-delivery-attempts=5
  ```

  **Verification:** failing messages land in the dead-letter topic after the attempt cap; the ordering key resumes draining and backlog for it falls.

### Cause Z: Unidentified

**Statement:** Backlog growth persists after ruling out ack-deadline expiry, flow-control limits, subscriber capacity, and hot ordering keys; the root cause is not yet identified from available signals.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the Pub/Sub SME / on-call.

  ```bash
  gcloud pubsub subscriptions describe SUBSCRIPTION_ID --project=PROJECT_ID > sub_describe.txt
  for m in num_undelivered_messages oldest_unacked_message_age expired_ack_deadlines_count ack_latencies open_streaming_pulls; do
    gcloud monitoring time-series list --project=PROJECT_ID \
      --filter="metric.type=\"pubsub.googleapis.com/subscription/${m}\" AND resource.label.subscription_id=\"SUBSCRIPTION_ID\"" \
      >> sub_metrics.txt
  done
  gcloud pubsub subscriptions pull SUBSCRIPTION_ID --project=PROJECT_ID --limit=20 \
    --format='value(message.orderingKey, message.publishTime)' >> sub_sample.txt
  ```

  **Risk:** the unacked sample pull briefly increases outstanding count for those messages. **Duration:** one-time capture. **Verification:** `sub_describe.txt`, `sub_metrics.txt`, and `sub_sample.txt` are attached to the incident before escalation.

## Prevention

- Set `ackDeadlineSeconds` to comfortably exceed p99 processing time and enable client-side lease auto-extension for long-running work.
- Tune subscriber flow control (`max_outstanding_messages`, max outstanding bytes) to measured per-instance throughput; never leave it unbounded.
- Autoscale subscribers on `subscription/num_undelivered_messages` and/or `subscription/oldest_unacked_message_age` so capacity tracks publish rate.
- Alert on `subscription/oldest_unacked_message_age` approaching the message retention duration and on rising `subscription/expired_ack_deadlines_count`.
- Use ordering keys only when strictly required, with high key cardinality to avoid hot-key head-of-line blocking.
- Configure a dead-letter topic with `--max-delivery-attempts` (5–100) so poison messages are quarantined instead of looping the backlog.

## Sources

- [Troubleshooting](https://docs.cloud.google.com/pubsub/docs/troubleshooting) — top-level Pub/Sub troubleshooting entry point (confirmed backlog diagnostics live in the pull-troubleshooting/monitoring subpages).
- [Pull troubleshooting](https://docs.cloud.google.com/pubsub/docs/pull-troubleshooting) — primary source: causes of growing pull backlog (ack-deadline expiry, flow control, slow subscribers, redelivery/nacks, ordering/exactly-once), metric names, and the delivery-latency health score.
- [Monitor subscription](https://docs.cloud.google.com/pubsub/docs/monitor-subscription) — backlog/delivery metric list and monitoring guidance (oldest unacked, unacked-by-region, delivery latency health score).
- [Ordering](https://docs.cloud.google.com/pubsub/docs/ordering) — ordering-key throughput (1 MBps/key), head-of-line blocking on redelivery, hot-key detection via oldest_unacked_message_age, `--enable-message-ordering` immutability.
- [Lease management](https://docs.cloud.google.com/pubsub/docs/lease-management) — ack-deadline auto-extension based on p99 ack latency.
- [Update](https://docs.cloud.google.com/sdk/gcloud/reference/pubsub/subscriptions/update) — `gcloud pubsub subscriptions update` flags (`--ack-deadline`, `--dead-letter-topic`, `--max-delivery-attempts`, `--enable-exactly-once-delivery`).
- [Describe](https://docs.cloud.google.com/sdk/gcloud/reference/pubsub/subscriptions/describe) — `gcloud pubsub subscriptions describe` syntax.
- [Pull](https://docs.cloud.google.com/sdk/gcloud/reference/pubsub/subscriptions/pull) — `gcloud pubsub subscriptions pull` syntax and flags.
- [Dead letter topics](https://docs.cloud.google.com/pubsub/docs/dead-letter-topics) — dead-letter policy (`--max-delivery-attempts` range 5–100) for poison-message quarantine.
