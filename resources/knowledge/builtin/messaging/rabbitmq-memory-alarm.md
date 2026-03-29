---
id: rabbitmq-memory-alarm
title: "RabbitMQ Memory Alarm and Flow Control: Publisher Blocking and Memory Tuning"
domain: messaging
service: rabbitmq
symptom_class:
  - oom
  - throughput-degradation
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - rabbitmq
  - memory
  - flow-control
  - memory-alarm
  - lazy-queue
  - publisher-blocking
difficulty: intermediate
---

# RabbitMQ Memory Alarm and Flow Control

## Problem Definition

Applies to RabbitMQ 3.10+ (including 3.12+ with quorum queues and streams). Requires access to `rabbitmqctl`, `rabbitmq-diagnostics`, and the RabbitMQ Management UI or HTTP API (default port 15672). The node must have the `rabbitmq_management` plugin enabled for HTTP API diagnostics.

A RabbitMQ memory alarm triggers when a node's memory usage exceeds the configured memory watermark (default 40% of available RAM). When the alarm fires, RabbitMQ blocks all publishing connections — publishers can still open connections but their `basic.publish` calls will block indefinitely until memory drops below the watermark. Consumers continue to operate normally. Flow control is a related but distinct mechanism that throttles individual connections when internal credit is exhausted, typically due to slow queue processes or disk I/O.

**Symptoms and errors:**

- RabbitMQ Management UI shows `mem_alarm` in the node status panel (red highlight)
- Publisher connections show state `blocking` or `blocked` in the Connections tab
- `rabbitmqctl status` shows `{mem_alarm, true}` and `memory_used` exceeding the watermark
- Publishers hang indefinitely on `basic.publish` — no exception is thrown, the call simply blocks
- Consumer throughput is unaffected but end-to-end latency spikes because new messages stop arriving
- Connection-level flow control shows `flow` state in the Management UI even before the memory alarm
- RabbitMQ logs: `Memory high watermark set to X bytes. Current memory usage is Y bytes`
- Client-side: AMQP `connection.blocked` notification received by the publisher (if the client library supports it)
- Prometheus metric `rabbitmq_alarms_memory_used_watermark` is 1

**Common causes:**

- Queue backlog growth: consumers are slower than publishers, causing messages to accumulate in memory
- Classic mirrored queues holding all messages in RAM (not using lazy queues or quorum queues)
- High message rate with large message payloads filling memory faster than messages can be consumed or paged to disk
- Erlang process memory leak from long-lived connections with large prefetch counts
- Memory watermark set too low for the workload (e.g., 40% on a node with many queues)
- Channel-level prefetch (`basic.qos`) too high, causing RabbitMQ to hold many unacknowledged messages in memory
- Queue mirroring (`ha-mode: all`) doubling or tripling memory usage across mirrors
- Management plugin collecting excessive metrics data consuming memory
- Node running on a container with a memory limit lower than what RabbitMQ detects as available RAM

## Diagnostic Steps

### Step 1: Confirm Memory Alarm Status

Determines whether the memory alarm is currently active and which nodes are affected.

```bash
# Check node alarms
rabbitmqctl status | grep -A 5 "alarms"

# Or via diagnostics command
rabbitmq-diagnostics alarms

# Check memory usage details
rabbitmq-diagnostics memory_breakdown

# Via HTTP API
curl -s -u guest:guest http://localhost:15672/api/nodes | \
  python3 -c "
import sys, json
nodes = json.load(sys.stdin)
for n in nodes:
    print(f\"Node: {n['name']}\")
    print(f\"  mem_used: {n['mem_used'] / 1024**2:.0f} MB\")
    print(f\"  mem_limit: {n['mem_limit'] / 1024**2:.0f} MB\")
    print(f\"  mem_alarm: {n['mem_alarm']}\")
    print(f\"  disk_free_alarm: {n['disk_free_alarm']}\")
"
```

**Expected output:** `mem_alarm: true` confirms the alarm is active. `mem_used` exceeding `mem_limit` shows how far over the watermark the node is. The memory breakdown shows which subsystem (queues, connections, mnesia, binary references) is consuming the most memory.

**What this means:** If `mem_alarm` is true, all publishers on that node are blocked. If `binary` memory is high, messages are held in RAM. If `connection` memory is high, too many connections or large prefetch counts are the cause. If `queue_procs` is high, too many queues or queues with large backlogs are consuming process memory.

### Step 2: Identify the Largest Memory-Consuming Queues

Determines which queues are holding the most messages in memory and contributing to the alarm.

```bash
# List queues sorted by memory usage (top 20)
rabbitmqctl list_queues name messages memory consumers --sort-by memory | \
  tail -20

# Via HTTP API with more detail
curl -s -u guest:guest "http://localhost:15672/api/queues?sort=memory&sort_reverse=true&page_size=20" | \
  python3 -c "
import sys, json
queues = json.load(sys.stdin)
for q in queues:
    print(f\"{q['name']:50s} msgs={q.get('messages',0):>10,} mem={q.get('memory',0)/1024**2:>8.1f}MB consumers={q.get('consumers',0)}\")
"
```

**Expected output:** Queues with high `messages` count and high `memory` values are the primary contributors. Queues with zero consumers and growing message counts are the most likely cause.

**What this means:** A queue with millions of messages and no consumers is a stale or abandoned queue consuming memory needlessly. A queue with consumers but a growing backlog indicates consumer capacity is insufficient. Queues using classic mirrored mode hold all messages in RAM, while lazy queues or quorum queues page to disk.

### Step 3: Check Publisher and Consumer Rates

Determines the imbalance between incoming and outgoing message rates.

```bash
# Check message rates via the overview API
curl -s -u guest:guest http://localhost:15672/api/overview | \
  python3 -c "
import sys, json
data = json.load(sys.stdin)
rates = data.get('message_stats', {})
print(f\"Publish rate:  {rates.get('publish_details', {}).get('rate', 0):.1f} msg/s\")
print(f\"Deliver rate:  {rates.get('deliver_details', {}).get('rate', 0):.1f} msg/s\")
print(f\"Ack rate:      {rates.get('ack_details', {}).get('rate', 0):.1f} msg/s\")
print(f\"Total msgs:    {data.get('queue_totals', {}).get('messages', 0):,}\")
"

# Check connection states for flow control
rabbitmqctl list_connections name state send_pend recv_cnt | grep -E "blocking|blocked|flow"
```

**Expected output:** Publish rate significantly exceeding deliver/ack rate confirms the backlog is growing. Connections in `blocking` or `blocked` state confirm the memory alarm is affecting publishers.

**What this means:** If publish rate is 10,000 msg/s and deliver rate is 2,000 msg/s, the queue backlog grows by 8,000 msg/s. At 1 KB per message, that is approximately 8 MB/s of memory growth. The alarm will trigger quickly under this imbalance.

### Step 4: Check Memory Watermark Configuration

Determines the current memory watermark and whether it is appropriate for the workload.

```bash
# Check current memory watermark
rabbitmqctl eval 'application:get_env(rabbit, vm_memory_high_watermark).'

# Check effective memory limit
rabbitmq-diagnostics status | grep -A 3 "vm_memory_high_watermark"

# Check if running in a container and whether memory detection is correct
rabbitmq-diagnostics environment | grep -E "total_memory|vm_memory"
```

**Expected output:** Default watermark is `0.4` (40% of detected RAM). In containers, RabbitMQ may detect the host's total RAM instead of the container's memory limit if cgroup awareness is not configured.

**What this means:** If the container has a 2 GB limit but RabbitMQ detects 64 GB of host RAM, the watermark is set to 25.6 GB — far above the container limit. The OOM killer will terminate the process before the memory alarm ever fires. Set `total_memory_available_override_value` or use `vm_memory_high_watermark.absolute` in containerized deployments.

### Step 5: Check Queue Types and Durability Settings

Determines whether queues are configured for memory efficiency or holding everything in RAM.

```bash
# List queue types (classic, quorum, stream)
rabbitmqctl list_queues name type durable arguments | head -30

# Check for lazy queue mode on classic queues
rabbitmqctl list_queues name arguments | grep -i "lazy\|x-queue-mode"

# Check for mirrored queues (ha-mode policy)
rabbitmqctl list_policies
```

**Expected output:** Classic queues without `x-queue-mode: lazy` hold all messages in RAM. Quorum queues (type `quorum`) page to disk automatically. Mirrored queues (`ha-mode: all`) replicate all messages in RAM across mirrors.

**What this means:** Classic queues in default mode are the primary cause of memory alarms under backlog conditions. Migrating to quorum queues or enabling lazy mode for classic queues dramatically reduces memory usage because messages are written to disk and only loaded into RAM when delivered to consumers.

## Mitigation

### Option 1: Purge Stale or Abandoned Queues

**Risk:** High if the queue contains needed messages. Confirm the queue is truly abandoned (zero consumers, no recent activity) before purging. Irreversible.

**Command:**

```bash
# Purge a specific queue
rabbitmqctl purge_queue <queue-name>

# Or delete the queue entirely if it is abandoned
rabbitmqctl delete_queue <queue-name>

# To purge all messages from queues with zero consumers (use with caution)
rabbitmqctl list_queues name consumers messages --formatter json | \
  python3 -c "
import sys, json
for q in json.load(sys.stdin):
    if q['consumers'] == 0 and q['messages'] > 10000:
        print(f\"Candidate for purge: {q['name']} ({q['messages']:,} messages)\")
"
```

**Verify:**

```bash
# Check that memory alarm clears
rabbitmq-diagnostics alarms
# Should show no alarms
```

**Duration:** Immediate. Memory is reclaimed within seconds of purging.

### Option 2: Increase Memory Watermark Temporarily

**Risk:** Medium. Raises the threshold at which the alarm fires, allowing more memory usage. Risk of OOM kill if the node exceeds available RAM. Do not exceed 70% on a dedicated node.

**Command:**

```bash
# Increase watermark at runtime (no restart required)
rabbitmqctl set_vm_memory_high_watermark 0.6

# Or set an absolute value
rabbitmqctl set_vm_memory_high_watermark absolute "4GB"
```

**Verify:**

```bash
# Confirm alarm clears
rabbitmq-diagnostics alarms

# Confirm new watermark is in effect
rabbitmq-diagnostics status | grep -A 3 "vm_memory_high_watermark"
```

**Duration:** Immediate. The alarm clears as soon as the new watermark exceeds current usage.

### Option 3: Enable Lazy Queue Mode on High-Volume Classic Queues

**Risk:** Low. Switches classic queues to page messages to disk instead of holding them in RAM. Increases disk I/O but dramatically reduces memory usage. Consumers may see slightly higher latency for the first message in a batch.

**Command:**

```bash
# Set a policy to enable lazy mode on matching queues
rabbitmqctl set_policy lazy-queues "^(order|payment|event)\." \
  '{"queue-mode":"lazy"}' --apply-to queues

# Or apply to all queues
rabbitmqctl set_policy lazy-all ".*" \
  '{"queue-mode":"lazy"}' --apply-to queues --priority 0
```

**Verify:**

```bash
# Confirm policy is applied
rabbitmqctl list_policies

# Monitor memory usage decreasing as messages page to disk
rabbitmq-diagnostics memory_breakdown
```

**Duration:** Existing messages begin paging to disk within seconds. Full memory reduction depends on queue depth — large queues may take minutes.

### Option 4: Scale Consumer Capacity

**Risk:** Low. Adding consumers drains the queue backlog, reducing memory usage. Requires consumer application scaling.

**Command:**

```bash
# If consumers are in Kubernetes
kubectl scale deployment my-consumer --replicas=10

# Increase prefetch to improve consumer throughput
# In consumer application config: basic.qos(prefetch_count=50)
```

**Verify:**

```bash
# Monitor queue depth and memory decreasing
watch -n 5 'rabbitmqctl list_queues name messages memory consumers --sort-by memory | tail -10'
```

**Duration:** Minutes to hours depending on backlog size and consumer throughput.

## Root Cause Resolution

**If** classic queues hold all messages in RAM → Migrate to quorum queues (recommended for RabbitMQ 3.10+) or enable lazy queue mode on classic queues. Quorum queues page to disk automatically, provide replication, and handle memory more efficiently. Set a policy: `rabbitmqctl set_policy quorum "^my-queue" '{"queue-type":"quorum"}' --apply-to queues`.

**If** consumers cannot keep up with publishers → Scale consumers horizontally. Increase `basic.qos` prefetch count to 50-100 for consumers doing batch processing. Optimize consumer processing logic. If the throughput mismatch is structural, add queue length limits (`x-max-length` or `x-max-length-bytes`) with overflow behavior (`drop-head` or `reject-publish`).

**If** the memory watermark is misconfigured in containers → Set `total_memory_available_override_value` in `rabbitmq.conf` to match the container memory limit. Or use `vm_memory_high_watermark.absolute` with an explicit byte value. Example: `vm_memory_high_watermark.absolute = 1536MB` for a 2 GB container.

**If** mirrored queue policies double memory usage → Migrate from classic mirrored queues (`ha-mode`) to quorum queues, which provide replication without duplicating messages in RAM across all mirrors. Quorum queues are the recommended replacement since RabbitMQ 3.10.

**If** management plugin metrics consume excessive memory → Reduce the metrics collection interval: `management.rates_mode = none` or `management.rates_mode = basic`. Disable per-object statistics if not needed. The detailed metrics mode retains 10 minutes of per-second data, which can consume hundreds of MB on busy clusters.

**If** Erlang binary memory is not being reclaimed → Force a garbage collection: `rabbitmqctl eval '[garbage_collect(P) || P <- processes()].'`. If this is a recurring issue, set `RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS="+MBas aobf"` to use the address-order best-fit allocator, which reduces binary memory fragmentation.

## Verification

After applying fixes, confirm the node is healthy:

```bash
# 1. Memory alarm is cleared
rabbitmq-diagnostics alarms
# Should show: Node has no alarms

# 2. Memory usage is below the watermark
rabbitmq-diagnostics status | grep -E "mem_used|mem_limit"
# mem_used should be well below mem_limit

# 3. No connections are blocked
rabbitmqctl list_connections name state | grep -c "blocked"
# Should return 0

# 4. Publisher and consumer rates are balanced
curl -s -u guest:guest http://localhost:15672/api/overview | \
  python3 -c "
import sys, json
data = json.load(sys.stdin)
rates = data.get('message_stats', {})
print(f\"Publish:  {rates.get('publish_details', {}).get('rate', 0):.1f} msg/s\")
print(f\"Deliver:  {rates.get('deliver_details', {}).get('rate', 0):.1f} msg/s\")
"

# 5. Queue depths are stable or decreasing
rabbitmqctl list_queues name messages --sort-by messages | tail -10
```

## Prevention

- **Use quorum queues** instead of classic mirrored queues for all new queues — they provide built-in replication with disk-based storage and better memory management
- **Set queue length limits** with `x-max-length` or `x-max-length-bytes` and an overflow policy (`reject-publish` for backpressure, `drop-head` for bounded queues) to prevent unbounded queue growth
- **Set `vm_memory_high_watermark` to 0.4-0.6** depending on workload — never exceed 0.7 to leave headroom for Erlang runtime and OS
- **In containers, always set `total_memory_available_override_value`** to match the container memory limit so the watermark is calculated correctly
- **Monitor `rabbitmq_alarms_memory_used_watermark`** (Prometheus) and alert when it equals 1
- **Monitor queue depth** and alert when any queue exceeds a threshold (e.g., 100,000 messages) without consumers
- **Set `basic.qos` prefetch count** to 50-100 for consumers to maximize throughput without overloading consumer memory
- **Enable lazy queue mode** for classic queues that may accumulate backlogs during consumer downtime
- **Reduce management plugin overhead** by setting `management.rates_mode = basic` in production
- **Set message TTL** (`x-message-ttl`) on queues where stale messages should be discarded rather than accumulated
- **Implement publisher confirms** so publishers detect when the broker is under pressure and can apply backpressure upstream
- **Monitor Erlang binary memory** (`rabbitmq_process_resident_memory_bytes`) and set up alerting for memory growth trends

## Sources

- [RabbitMQ Documentation — Memory Alarms](https://www.rabbitmq.com/docs/memory)
- [RabbitMQ Documentation — Flow Control](https://www.rabbitmq.com/docs/flow-control)
- [RabbitMQ Documentation — Lazy Queues](https://www.rabbitmq.com/docs/lazy-queues)
- [RabbitMQ Documentation — Quorum Queues](https://www.rabbitmq.com/docs/quorum-queues)
- [RabbitMQ Documentation — Monitoring](https://www.rabbitmq.com/docs/monitoring)
