---
id: "rabbitmq-memory-alarm"
title: "RabbitMQ Memory Alarm and Publisher Flow Control"
domain: messaging
service: rabbitmq
symptom_class: [oom, throughput_degradation]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [rabbitmq, memory, flow-control, memory-alarm, lazy-queue, publisher-blocking, quorum-queues]
difficulty: intermediate
---

## Symptom Recognition

- RabbitMQ Management UI shows `mem_alarm` badge on the node status panel (red highlight)
- Publisher connections show state `blocking` or `blocked` in the Connections tab
- `rabbitmqctl status` output includes `{mem_alarm, true}` and `memory_used` exceeding `mem_limit`
- Publishers hang indefinitely on `basic.publish` — no exception thrown, the call blocks until memory drops
- AMQP `connection.blocked` notification received by publisher client libraries that support it
- RabbitMQ log line: `Memory high watermark set to X bytes. Current memory usage is Y bytes`
- Prometheus metric `rabbitmq_alarms_memory_used_watermark` equals 1
- Consumer throughput continues normally; end-to-end latency spikes because new messages stop arriving
- Connection-level flow control shows `flow` state in the Management UI even before the full memory alarm fires

## Applicability

Applies to RabbitMQ 3.10 and later, including 3.12+ with quorum queues and streams. Requires shell access to run `rabbitmqctl` and `rabbitmq-diagnostics`. HTTP API diagnostics require the `rabbitmq_management` plugin enabled (default port 15672). Container deployments require additional attention to memory detection (cgroup awareness).

## Diagnostic Steps

### Step 1: Confirm the memory alarm and identify affected nodes

```bash
rabbitmq-diagnostics alarms
```

Expected output: `Node rabbit@hostname has [memory] alarm set` if active. No output or `Node rabbit@hostname has no alarms` if cleared.

```bash
curl -s -u guest:guest http://localhost:15672/api/nodes | \
  python3 -c "
import sys, json
for n in json.load(sys.stdin):
    print(n['name'], 'mem_alarm='+str(n['mem_alarm']),
          'mem_used_mb='+str(round(n['mem_used']/1024**2)),
          'mem_limit_mb='+str(round(n['mem_limit']/1024**2)))
"
```

Expected output: Each node on one line with `mem_alarm=True` confirming the alarm and the used vs limit figures in MB.

### Step 2: Break down memory by subsystem

```bash
rabbitmq-diagnostics memory_breakdown
```

Expected output: A table of memory consumers — `binary`, `queue_procs`, `connection_readers`, `connection_writers`, `mnesia`, `mgmt_db`, `plugins`, and others — each with byte totals. The largest subsystem is the primary contributor.

### Step 3: Identify queues with the highest memory and backlog

```bash
rabbitmqctl list_queues name messages memory consumers --sort-by memory
```

Expected output: Queues ordered by memory descending. Queues with `messages` in the millions and `consumers` of zero are stale or abandoned. Queues with growing `messages` and non-zero `consumers` indicate consumer capacity shortfall.

### Step 4: Check publisher and consumer message rates

```bash
curl -s -u guest:guest http://localhost:15672/api/overview | \
  python3 -c "
import sys, json
d = json.load(sys.stdin)
s = d.get('message_stats', {})
print('publish_rate:', s.get('publish_details', {}).get('rate', 0))
print('deliver_rate:', s.get('deliver_details', {}).get('rate', 0))
print('ack_rate:    ', s.get('ack_details', {}).get('rate', 0))
print('total_msgs:  ', d.get('queue_totals', {}).get('messages', 0))
"
```

Expected output: `publish_rate` significantly exceeding `deliver_rate` confirms the backlog is growing. Equal rates mean backlog is stable; lower publish than deliver means backlog is draining.

```bash
rabbitmqctl list_connections name state send_pend recv_cnt | grep -E "blocking|blocked|flow"
```

Expected output: Lines showing connections in `blocking` or `blocked` state confirm publishers are affected by the alarm.

### Step 5: Inspect the memory watermark configuration

```bash
rabbitmq-diagnostics status | grep -A 3 "vm_memory_high_watermark"
rabbitmq-diagnostics environment | grep -E "total_memory|vm_memory"
```

Expected output: Watermark fraction (e.g., `0.4`) and the detected total memory. In containers, detected total memory may equal host RAM rather than the container memory limit, causing the alarm to never fire before an OOM kill occurs.

### Step 6: Check queue types and lazy/quorum configuration

```bash
rabbitmqctl list_queues name type durable arguments
rabbitmqctl list_policies
```

Expected output: `type` column shows `classic`, `quorum`, or `stream`. Classic queues without `x-queue-mode: lazy` in arguments hold all messages in RAM. Policy rows showing `ha-mode: all` indicate classic mirrored queues which multiply memory usage across mirrors.

## Causes

### Cause A: Queue backlog growth from consumer capacity shortfall

**Statement:** Publishers are producing messages faster than consumers can process them, causing messages to accumulate in queue memory until the watermark is exceeded.

**Mechanism:** Each unacknowledged message is held in RAM by the queue process until delivered and acked by a consumer. When the publish rate consistently exceeds the deliver rate, the in-memory backlog grows linearly. Classic queues in default mode do not page messages to disk until the memory alarm fires, so even a modest throughput imbalance causes rapid RAM growth on high-volume queues.

**Indicator:**

- [Step 4] `publish_rate` is materially higher than `deliver_rate`
- [Step 3] One or more queues show rapidly increasing `messages` count with non-zero `consumers`

<!-- match: {"step": 4, "predicate": "threshold", "target": "publish_rate", "op": ">", "value": "deliver_rate"} -->

**Mitigation:**

- **Risk:** Scaling consumers increases load on downstream systems; validate capacity before scaling.
- **Command:**

  ```bash
  # Scale consumer replicas (Kubernetes)
  kubectl scale deployment my-consumer --replicas=10

  # Temporarily increase consumer prefetch in application config:
  # channel.basic_qos(prefetch_count=100)
  ```

- **Duration:** Queue drains within minutes to hours depending on backlog depth and new consumer throughput.

**Resolution:**

```bash
# Set a queue length limit with backpressure to prevent unbounded future growth
rabbitmqctl set_policy queue-limits "^my-queue$" \
  '{"max-length":500000,"overflow":"reject-publish"}' \
  --apply-to queues

# Migrate high-volume queues to quorum type for disk-backed storage
# (requires consumer downtime for classic->quorum migration)
rabbitmqctl set_policy quorum-migration "^my-queue$" \
  '{"queue-type":"quorum"}' --apply-to queues
```

**Verification:** After scaling consumers, `deliver_rate` should approach or exceed `publish_rate` within one polling interval. `rabbitmq-diagnostics alarms` should show no active alarms within 1-5 minutes as the backlog drains.

---

### Cause B: Classic queues holding all messages in RAM

**Statement:** Classic queues operating in the default (non-lazy) mode buffer all messages in process memory rather than paging to disk, exhausting available RAM under any significant backlog.

**Mechanism:** Default classic queues keep the full message body in Erlang binary memory until delivered. This is efficient for zero-backlog queues but catastrophic when consumer downtime or throughput mismatch causes messages to accumulate. The `queue_procs` and `binary` subsystems will dominate the memory breakdown. Quorum queues and lazy-mode classic queues write message bodies to disk on receipt, keeping only metadata in RAM.

**Indicator:**

- [Step 2] `binary` or `queue_procs` is the largest memory subsystem
- [Step 6] Queues show `type=classic` and no `x-queue-mode: lazy` in arguments

<!-- match: {"step": 6, "predicate": "absent", "target": "x-queue-mode"} -->

**Mitigation:**

- **Risk:** Low. Enabling lazy mode pages existing messages to disk immediately, causing a spike in disk I/O. First-message delivery latency increases slightly.
- **Command:**

  ```bash
  # Apply lazy mode policy to matching queues (no restart required)
  rabbitmqctl set_policy lazy-high-volume "^(order|event|payment)\." \
    '{"queue-mode":"lazy"}' --apply-to queues --priority 10
  ```

- **Duration:** Memory reduction begins within seconds; full relief depends on queue depth (large queues may take minutes to page out).

**Resolution:**

```bash
# For new deployments, declare queues as quorum type (preferred over lazy classic)
# In application code: x-queue-type=quorum queue argument
# Via policy for existing consumers:
rabbitmqctl set_policy quorum-all ".*" \
  '{"queue-type":"quorum"}' --apply-to queues --priority 5
```

- **Impact:** Quorum queue migration is cluster-wide and requires consumers to reconnect. Classic queues cannot be converted in-place; a drain-and-redeclare is required for in-flight messages.
- **Rollback:** Delete the policy with `rabbitmqctl clear_policy quorum-all` to stop new queue declarations from using quorum type.

**Verification:** After policy application, `rabbitmq-diagnostics memory_breakdown` should show `binary` and `queue_procs` declining. Run the check every 30 seconds for 5 minutes to confirm the trend.

---

### Cause C: Watermark set too low or misconfigured in containers

**Statement:** The memory watermark is either set below the workload's natural operating point, or RabbitMQ detects the host's total RAM instead of the container limit, making the effective watermark dangerously wrong.

**Mechanism:** The default watermark of 0.4 (40% of detected RAM) is conservative for dedicated broker nodes. In containers without cgroup v2 awareness, RabbitMQ detects host RAM — a node in a 2 GB container on a 64 GB host sets a watermark of 25.6 GB, meaning the OOM killer terminates the process long before the alarm fires. Conversely, if the operator manually set the watermark too low (e.g., 0.2) on a busy cluster, legitimate workloads trigger false alarms.

**Indicator:**

- [Step 5] Detected `total_memory` is far larger than the container memory limit
- [Step 5] Watermark fraction is below 0.35 or above 0.7

<!-- match: {"step": 5, "predicate": "threshold", "target": "vm_memory_high_watermark", "op": "<", "value": 0.35} -->

**Mitigation:**

- **Risk:** Low. Raising the watermark at runtime allows more memory usage before blocking. Do not exceed 0.7 to retain OS headroom.
- **Command:**

  ```bash
  # Raise watermark at runtime (takes effect immediately, no restart)
  rabbitmqctl set_vm_memory_high_watermark 0.6

  # Or set an absolute value matching the container limit
  rabbitmqctl set_vm_memory_high_watermark absolute "1536MB"
  ```

- **Duration:** Alarm clears immediately once the new watermark exceeds current usage.

**Resolution:**

```bash
# In rabbitmq.conf — set override for container environments:
# total_memory_available_override_value = 2147483648   # 2 GB in bytes
# vm_memory_high_watermark.relative = 0.6

# Apply via ConfigMap in Kubernetes (requires pod restart):
kubectl edit configmap rabbitmq-config -n rabbitmq
# Add: total_memory_available_override_value = <container_limit_bytes>
```

- **Impact:** Requires a RabbitMQ restart when changed via config file. Runtime `set_vm_memory_high_watermark` is non-persistent and resets on restart.
- **Rollback:** `rabbitmqctl set_vm_memory_high_watermark 0.4` to restore the default.

**Verification:** After fix, `rabbitmq-diagnostics environment | grep total_memory` should show a value matching the container memory limit. `rabbitmq-diagnostics status` should show `vm_memory_high_watermark` as the expected fraction of the corrected total.

---

### Cause D: Classic mirrored queues multiplying memory across replicas

**Statement:** Classic mirrored queues with `ha-mode: all` replicate every message in RAM on every mirror node, doubling or tripling cluster memory consumption compared to a single copy.

**Mechanism:** In classic mirroring, the primary queue process forwards every message body to each mirror via Erlang inter-node messaging. Each mirror holds a full in-memory copy of the queue. A 10 GB queue on a 3-node cluster with `ha-mode: all` consumes 30 GB of aggregate RAM. This design was deprecated in RabbitMQ 3.10 in favor of quorum queues, which use Raft-based replication with disk-backed storage and do not duplicate message bodies in RAM across replicas.

**Indicator:**

- [Step 6] `rabbitmqctl list_policies` shows policies with `ha-mode: all` or `ha-mode: exactly`
- [Step 2] Memory breakdown is proportionally high across multiple nodes with similar `queue_procs` figures

<!-- match: {"step": 6, "predicate": "contains", "target": "ha-mode"} -->

**Mitigation:**

- **Risk:** Medium. Removing mirroring reduces fault tolerance until quorum queues are in place.
- **Command:**

  ```bash
  # Remove mirroring policy temporarily to reduce memory pressure
  rabbitmqctl clear_policy ha-all

  # Check which queues were mirrored
  rabbitmqctl list_queues name policy slave_pids
  ```

- **Duration:** Memory on mirror nodes is reclaimed within seconds of policy removal.

**Resolution:**

```bash
# Migrate to quorum queues — declare new quorum queues and drain old classic ones
# Step 1: Create quorum replacement queue
# In application: declare queue with x-queue-type=quorum

# Step 2: Route new publishers to quorum queue
# Step 3: Wait for classic queue to drain, then delete it
rabbitmqctl delete_queue my-classic-queue

# Step 4: Remove the ha-mode policy
rabbitmqctl clear_policy ha-all
```

- **Impact:** Cluster-wide. All consumers must reconnect to the new queue. Requires application-side queue redeclaration.
- **Rollback:** Re-apply the ha-mode policy: `rabbitmqctl set_policy ha-all ".*" '{"ha-mode":"all"}' --apply-to queues`

**Verification:** After migration, `rabbitmqctl list_queues name type` should show `quorum` for migrated queues. `rabbitmq-diagnostics memory_breakdown` across all nodes should show significantly lower `queue_procs` totals.

---

### Cause E: Erlang binary memory accumulation and fragmentation

**Statement:** Erlang runtime binary memory is not being reclaimed between GC cycles, causing the `binary` heap to grow beyond active message payloads due to fragmentation or long-lived process references.

**Mechanism:** RabbitMQ uses Erlang's reference-counted binary heap for message bodies. Long-lived connections with large prefetch counts hold references that prevent binary GC. The management plugin's per-second statistics collection retains process state snapshots. These references keep message binaries alive long after the AMQP ack, causing the `binary` subsystem in `memory_breakdown` to grow independently of queue depth. The default Erlang allocator (mseg) can also fragment memory so that freed binaries do not return to the OS.

**Indicator:**

- [Step 2] `binary` memory is the dominant subsystem and does not decrease after purging queues
- [Step 3] Queue message counts are low but overall memory remains high

<!-- match: {"step": 2, "predicate": "threshold", "target": "binary_pct_of_total", "op": ">", "value": 0.5} -->

**Mitigation:**

- **Risk:** Low. Forcing GC on all Erlang processes is safe but causes a brief CPU spike.
- **Command:**

  ```bash
  # Force GC on all Erlang processes (safe, brief CPU spike)
  rabbitmqctl eval '[garbage_collect(P) || P <- processes()].'

  # Reduce management plugin memory overhead
  rabbitmqctl eval 'application:set_env(rabbitmq_management, rates_mode, basic).'
  ```

- **Duration:** Binary memory should decrease within 30-60 seconds after forced GC.

**Resolution:**

```bash
# In rabbitmq.conf — set allocator to reduce fragmentation:
# server_additional_erl_args = +MBas aobf +MBasbcs 512

# Reduce management stats retention:
# management.rates_mode = basic
# management.sample_retention_policies.global.60 = 5
```

- **Impact:** Allocator change requires full RabbitMQ restart. `rates_mode = basic` reduces Management UI chart resolution.
- **Rollback:** Remove `server_additional_erl_args` from config and restart.

**Verification:** After GC and config changes, `rabbitmq-diagnostics memory_breakdown` should show `binary` below 30% of total. Monitor `rabbitmq_process_resident_memory_bytes` (Prometheus) over 30 minutes to confirm it is not climbing.

---

### Cause Z: Unidentified memory cause

**Statement:** Memory usage exceeds the watermark but no single subsystem, queue, or configuration problem accounts for the alarm after completing all diagnostic steps.

**Mechanism:** [Default]

**Indicator:**

- [Default] None of Causes A–E match the observed memory breakdown pattern

**Mitigation:**

- **Risk:** Low. Temporarily raising the watermark buys time for deeper investigation without dropping messages.
- **Command:**

  ```bash
  rabbitmqctl set_vm_memory_high_watermark 0.6
  ```

- **Duration:** Immediate; re-evaluate within 30 minutes.

**Resolution:** Out of runbook scope. Collect a full memory snapshot and escalate: `rabbitmq-diagnostics memory_breakdown --formatter json > /tmp/rmq-mem-$(date +%s).json`. Engage RabbitMQ support or the community mailing list with the snapshot, RabbitMQ version, and Erlang version.

**Verification:** `rabbitmq-diagnostics alarms` shows no active alarms after the watermark adjustment. Memory trend should be monitored continuously; if usage keeps climbing, the watermark raise is not a durable fix.

## Prevention

- Use quorum queues for all new queue declarations — they provide Raft replication with disk-backed message storage and better memory management than classic mirrored queues.
- Set `vm_memory_high_watermark.relative = 0.5` (or an absolute value) in `rabbitmq.conf`; keep it at or below 0.6 to retain OS and Erlang runtime headroom.
- In all container deployments, set `total_memory_available_override_value` in `rabbitmq.conf` to match the container memory limit so the watermark calculation is correct.
- Apply queue length limits (`x-max-length` or `x-max-length-bytes`) with `overflow: reject-publish` on all queues to enforce backpressure before memory pressure builds.
- Set message TTL (`x-message-ttl`) on queues where stale messages should expire rather than accumulate.
- Alert on Prometheus metric `rabbitmq_alarms_memory_used_watermark == 1` with a 1-minute `for` duration to catch alarms before they affect SLAs.
- Alert on queue depth exceeding a threshold (e.g., 100,000 messages) with no consumers as an early warning for abandoned queues.
- Set `basic.qos` prefetch count to 50–100 for consumers doing I/O-bound processing to maximize throughput without holding excessive unacked messages in RAM.
- Set `management.rates_mode = basic` in production to reduce management plugin memory overhead.
- Enable lazy queue mode on any classic queues that cannot be migrated to quorum type immediately.

## Sources

- [RabbitMQ Documentation — Memory Alarms](https://www.rabbitmq.com/docs/memory) — watermark configuration, memory breakdown subsystems, container detection, paging behavior. Priority 1.
- [RabbitMQ Documentation — Flow Control](https://www.rabbitmq.com/docs/flow-control) — credit-based flow control, connection states (blocking/blocked/flow), relationship to memory alarms. Priority 1.
- [RabbitMQ Documentation — Lazy Queues](https://www.rabbitmq.com/docs/lazy-queues) — queue-mode policy, disk paging, migration from default classic queues. Priority 1.
- [RabbitMQ Documentation — Quorum Queues](https://www.rabbitmq.com/docs/quorum-queues) — Raft replication, disk-backed storage, memory advantages over classic mirrored. Priority 1.
- [RabbitMQ Documentation — Monitoring](https://www.rabbitmq.com/docs/monitoring) — Prometheus metrics, HTTP API endpoints for memory and alarm status. Priority 1.
- [RabbitMQ Documentation — Troubleshooting](https://www.rabbitmq.com/docs/troubleshooting) — general diagnostic tooling and memory investigation. Priority 1.
