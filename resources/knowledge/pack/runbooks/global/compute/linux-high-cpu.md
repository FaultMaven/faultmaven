---
id: linux-high-cpu
title: "Linux High CPU Utilization"
domain: compute
service: linux
symptom_class:
  - cpu_saturation
  - latency
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - linux
  - cpu
  - perf
  - mpstat
  - pidstat
  - runaway-process
  - kernel-cpu
  - ebpf
  - cgroup
difficulty: intermediate
---

## Symptom Recognition

- Load average (from `uptime` or `/proc/loadavg`) sustained above the number of CPU cores — 1-minute load > `nproc` output.
- `mpstat -P ALL 1 5` shows one or more CPUs with `%idle` below 10% sustained over multiple samples.
- `vmstat 1 5` `r` column (run queue depth) consistently exceeds the CPU count.
- Application response time SLA breaches, health check timeouts, or request queuing observed in application metrics.
- Alerts fire: `node_load1 > count(node_cpu_seconds_total{mode="idle"})` (Prometheus), CloudWatch `CPUUtilization > 90%`, or equivalent.
- `top` or `ps aux --sort=-%cpu` shows one or more processes consuming 100%+ CPU continuously.
- Container CPU throttling: `container_cpu_cfs_throttled_seconds_total` rate increasing; `nr_throttled` in cgroup `cpu.stat` incrementing.

## Applicability

- Linux kernel 4.18 or newer on any architecture (x86_64, ARM64, RISC-V).
- Requires `sysstat` package for `mpstat`, `pidstat`, `sar`; `procps` for `top`, `ps`, `vmstat`.
- `perf` profiling requires `linux-tools-$(uname -r)` (Debian/Ubuntu) or `perf` (RHEL/Fedora) matching the running kernel; kernel profiling requires root or `CAP_SYS_ADMIN`.
- eBPF tools (`bpftrace`, BCC suite) require kernel 4.18+ with BPF enabled and the BCC or bpftrace packages installed.
- Cgroup v2 CPU controller stats (`cpu.stat`) available on kernel 4.15+ with unified hierarchy; cgroup v1 paths differ slightly.
- For containerized workloads, host-level `top`/`mpstat` shows aggregate consumption; per-container breakdown requires `docker stats` or Kubernetes `kubectl top`.

## Diagnostic Steps

### Step 1: Assess overall CPU state with load average and run queue

```bash
uptime && nproc
```

Expected output: load averages for 1/5/15 minutes and total CPU core count. If 1-minute load exceeds `nproc`, the system is CPU-saturated.

```bash
vmstat 1 5
```

Expected output: five rows; the `r` column shows run queue depth. Values consistently above the CPU count confirm saturation. The `cs` column shows context switches per second.

### Step 2: Identify top CPU-consuming processes

```bash
top -bn1 -o %CPU | head -30
```

Expected output: process list sorted by CPU percentage descending. Note the PID, user, and command for the top consumers.

```bash
pidstat -u 1 5
```

Expected output: per-process CPU utilization sampled each second for 5 seconds, with `%usr` and `%system` columns separated. More precise than `top` for transient spikes.

### Step 3: Break down CPU time by mode (user vs kernel vs iowait)

```bash
mpstat -P ALL 1 5
```

Expected output: per-CPU rows showing `%usr`, `%sys`, `%iowait`, `%irq`, `%soft`, `%idle`. High `%usr` across all CPUs = application saturation. High `%sys` = kernel overhead. One CPU at 100% while others idle = single-threaded bottleneck or IRQ affinity. High `%iowait` = I/O-blocked, not CPU-bound.

### Step 4: Profile the hottest functions system-wide

```bash
perf top -g
```

Expected output: live flame view of hottest functions across all CPUs, including both user-space and kernel symbols. Press `q` to exit. Wide entries identify the hot call paths.

```bash
perf stat -a -- sleep 5
```

Expected output: system-wide hardware counter summary — IPC (instructions per cycle), cache-misses, branch-mispredictions, context-switches. IPC below 0.5 with high LLC cache-misses suggests memory-bound workload, not genuine CPU saturation.

### Step 5: Capture a per-process CPU profile

```bash
perf record -F 99 -p <PID> -g -- sleep 30
perf report --stdio | head -60
```

Expected output: function-level CPU profile sorted by percentage of samples, with call chains. Wide plateaus at a specific function identify the hot code path causing CPU consumption.

### Step 6: Investigate kernel CPU time (if %sys > 20%)

```bash
perf record -e syscalls:sys_enter -a -g -- sleep 10
perf report --stdio | head -40
```

Expected output: syscall entry points ranked by frequency. Common offenders: `futex` (lock contention), `epoll_wait` (event-loop spin), repeated `read`/`write` (I/O-intensive path).

```bash
strace -c -p <PID> 2>&1 | head -30
```

Expected output: syscall count and time summary for a running process. Use briefly on production; strace adds significant overhead.

### Step 7: Check for cgroup CPU throttling

```bash
cat /sys/fs/cgroup/cpu.stat 2>/dev/null || cat /sys/fs/cgroup/cpu/cpu.stat 2>/dev/null
```

Expected output: cgroup CPU statistics including `nr_throttled` and `throttled_time`. Incrementing `nr_throttled` means the container is hitting its CPU limit — the process needs more CPU quota, not that the host is overloaded.

```bash
cat /proc/<PID>/sched | grep -E "nr_switches|nr_involuntary_switches|se.sum_exec_runtime"
```

Expected output: scheduler statistics for the process. A high ratio of involuntary to voluntary context switches indicates the process is being preempted (CPU contention), not yielding voluntarily.

### Step 8: Check interrupt distribution across CPUs

```bash
cat /proc/interrupts | head -5; sleep 5; cat /proc/interrupts | head -5
```

Expected output: two snapshots of interrupt counters. A single IRQ line incrementing rapidly in one CPU column indicates an interrupt storm pinned to one core.

## Causes

### Cause A: Runaway application process

**Statement:** A single application process is consuming 100% of one or more CPU cores continuously due to an infinite loop, unbounded retry, or bug that prevents yielding.

**Mechanism:** The process never calls a blocking syscall or sleep, so the kernel scheduler gives it every available quantum on the core(s) it runs on. Other processes on those cores are starved of CPU time. The process appears at the top of `top` and `pidstat` output with a stable high percentage across multiple samples.

**Indicator:**

- [Step 2] same PID appears at or near 100% `%CPU` across all `pidstat` sample intervals
- [Step 5] `perf report` shows a single flat call chain with no blocking points (tight loop)

<!-- match: {"step": 2, "predicate": "threshold", "target": "cpu_pct", "op": ">", "value": 95} -->

**Mitigation:**

- **Risk:** Lowering priority keeps the process running but allows other workloads to get CPU time. Does not fix the root cause; the runaway process continues consuming available idle CPU.
- **Command:**

  ```bash
  renice +10 -p <PID>
  # Or restrict to two cores to isolate impact
  taskset -cp 0,1 <PID>
  ```

- **Duration:** Safe for hours. Investigate and deploy a code fix within 4 hours.

**Resolution:**

```bash
# Profile the process to identify the looping code path
perf record -F 99 -p <PID> -g -- sleep 30
perf report --stdio --sort=dso,symbol | head -40
```

Fix the identified infinite loop, unbounded retry, or missing sleep/backoff in the application code and deploy the corrected version.

**Verification:** After the fix is deployed, `pidstat -u -p <PID> 1 10` shows CPU usage returned to expected baseline and does not approach 100% on any sample.

---

### Cause B: User-space CPU saturation from aggregate application load

**Statement:** Multiple application processes collectively saturate all CPU cores because the host is under-provisioned for the current traffic volume.

**Mechanism:** No single process misbehaves; the total request rate exceeds what the CPU can service at acceptable latency. Load average climbs above CPU count, run queue depth rises, and every core shows high `%usr` with minimal `%idle`. This is a capacity problem, not an application bug.

**Indicator:**

- [Step 1] run queue `r` > CPU count across all `vmstat` intervals
- [Step 3] `mpstat` shows all CPUs with `%usr` > 70% and `%idle` < 10%, no single dominant process in Step 2

<!-- match: {"step": 1, "predicate": "threshold", "target": "vmstat_r", "op": ">", "value": 8} -->

**Mitigation:**

- **Risk:** Deferring batch or non-critical workloads reduces CPU competition immediately; primary serving traffic is unaffected.
- **Command:**

  ```bash
  # Identify non-critical batch processes and lower their priority
  ps -eo pid,user,%cpu,comm --sort=-%cpu | grep -E "cron|batch|backup|report"
  renice +15 -p <BATCH_PID>
  ```

- **Duration:** Safe until capacity is scaled. Scale compute within 24 hours.

**Resolution:**

```bash
# Horizontal scale: add instances behind the load balancer
# Kubernetes example — increase replica count
kubectl scale deployment <name> --replicas=<N+K>
```

- **Impact:** Increases pod count cluster-wide. Ensure node capacity exists or trigger cluster-autoscaler.
- **Rollback:**

  ```bash
  kubectl scale deployment <name> --replicas=<original-N>
  ```

**Verification:** After scaling, `uptime` shows load average below CPU count per node, and `mpstat` shows `%idle` > 20% headroom on all cores.

---

### Cause C: High kernel CPU time from lock contention (futex storms)

**Statement:** Excessive `%sys` CPU time is caused by lock contention in multi-threaded application code, driving a futex syscall storm.

**Mechanism:** Threads compete on a shared mutex or spin-lock, repeatedly calling `futex(FUTEX_WAIT/FUTEX_WAKE)`. The kernel spends CPU time managing the futex queue rather than executing application logic. `mpstat` shows high `%sys`, `perf` syscall profiling shows `futex` at the top, and the process's `nr_involuntary_switches` is disproportionately high.

**Indicator:**

- [Step 3] `mpstat` aggregate `%sys` > 20% sustained
- [Step 6] `perf report` on `syscalls:sys_enter` shows `futex` in top entries

<!-- match: {"step": 6, "predicate": "contains", "target": "futex"} -->

**Mitigation:**

- **Risk:** Profiling with `perf record` has less than 1% overhead at 99 Hz. `strace` has significant overhead; use for at most 30 seconds on production.
- **Command:**

  ```bash
  perf record -e syscalls:sys_enter_futex -p <PID> -g -- sleep 10
  perf report --stdio | head -30
  ```

- **Duration:** Profiling is safe indefinitely. Deploy the code fix within the same incident window.

**Resolution:**

```bash
# Java: capture thread dump to identify contended locks
jstack <PID> > /tmp/threaddump.txt
grep -A5 "BLOCKED" /tmp/threaddump.txt | head -40
```

Reduce lock contention by narrowing critical sections, using read-write locks, switching to lock-free data structures, or reducing shared state between threads. Deploy the fix.

**Verification:** After deploying the fix, `mpstat` aggregate `%sys` drops below 5%, and `perf stat -a -- sleep 5` shows IPC improved and context-switch rate decreased.

---

### Cause D: CPU saturation from interrupt storm on a single core

**Statement:** A high-frequency hardware interrupt (typically from a network interface) is pinned to a single CPU core, saturating it while other cores remain underutilized.

**Mechanism:** Linux assigns IRQ affinity by default to CPU 0 or via `irqbalance`. Under high packet rates, a single NIC queue's interrupts can saturate one core. `mpstat` shows one CPU at 100% with high `%irq` or `%soft` while others are idle. `/proc/interrupts` shows rapid increment of one IRQ line in a single CPU column.

**Indicator:**

- [Step 3] `mpstat -P ALL` shows one CPU with `%irq` + `%soft` > 50% while other CPUs show < 10% utilization
- [Step 8] single IRQ line increments by >50,000 per 5-second interval in one CPU column

<!-- match: {"step": 3, "predicate": "threshold", "target": "cpu_irq_soft_pct", "op": ">", "value": 50} -->

**Mitigation:**

- **Risk:** Temporarily stopping `irqbalance` and manually setting affinity may cause brief packet reordering on the affected NIC.
- **Command:**

  ```bash
  systemctl stop irqbalance
  # Spread NIC IRQs across cores (adjust eth0 to actual interface)
  grep eth0 /proc/interrupts | awk '{print $1}' | tr -d ':' | while read irq; do
      echo ff > /proc/irq/$irq/smp_affinity
  done
  ```

- **Duration:** Persists until reboot. Re-enable `irqbalance` after verifying spread is stable.

**Resolution:**

```bash
# Enable multi-queue support on the NIC for sustained fix
ethtool -L eth0 combined $(nproc)
systemctl enable --now irqbalance
```

- **Impact:** Changes NIC queue count; requires driver support. Brief link flap possible.
- **Rollback:**

  ```bash
  ethtool -L eth0 combined 1
  systemctl restart irqbalance
  ```

**Verification:** `mpstat -P ALL 1 5` shows interrupt load spread across multiple CPUs with no single core above 50% from `%irq`/`%soft`.

---

### Cause E: Container CPU throttling from under-provisioned cgroup limit

**Statement:** A containerized process is being CPU-throttled by a cgroup quota that is too low for its actual workload, causing application latency while the host CPU has available headroom.

**Mechanism:** The Linux CFS bandwidth controller enforces the CPU quota (`cpu.cfs_quota_us / cpu.cfs_period_us`). When the container exhausts its quota within a scheduling period, all its threads are throttled until the next period. The host CPU is not saturated (`mpstat` shows `%idle > 20%`), but the container's response latency spikes and `nr_throttled` increments in cgroup `cpu.stat`.

**Indicator:**

- [Step 7] `nr_throttled` count is non-zero and increasing across repeated reads of `cpu.stat`
- [Step 3] host-level `mpstat` shows available `%idle` > 20% (host is NOT saturated)

<!-- match: {"step": 7, "predicate": "contains", "target": "nr_throttled"} -->

**Mitigation:**

- **Risk:** Increasing the cgroup CPU limit allows this container to consume more host CPU. Ensure the host has headroom.
- **Command:**

  ```bash
  # cgroup v2: raise limit to 150% of one CPU
  echo "150000 100000" > /sys/fs/cgroup/cpu_limit/cpu.max
  ```

- **Duration:** Safe indefinitely; adjust the deployment manifest as a durable fix.

**Resolution:**

```bash
# Kubernetes: increase CPU limits on the deployment
kubectl patch deployment <name> -p \
  '{"spec":{"template":{"spec":{"containers":[{"name":"<container>","resources":{"limits":{"cpu":"2000m"}}}]}}}}'
```

- **Impact:** Rolling restart of all pods in the deployment. CPU limit change takes effect on pod recreation.
- **Rollback:**

  ```bash
  kubectl patch deployment <name> -p \
    '{"spec":{"template":{"spec":{"containers":[{"name":"<container>","resources":{"limits":{"cpu":"<original>"}}}]}}}}'
  ```

**Verification:** After increasing the limit, `nr_throttled` stops incrementing on the next check of `cpu.stat`, and application latency returns to baseline.

---

### Cause F: Memory-bound workload misdiagnosed as CPU saturation

**Statement:** High CPU utilization is caused by cache misses stalling the CPU rather than compute-bound work, making the workload appear CPU-saturated when the bottleneck is actually memory bandwidth.

**Mechanism:** When the application's working set exceeds the CPU's LLC (last-level cache), every memory access stalls the CPU pipeline waiting for DRAM. The CPU is technically 100% utilized but completes very few instructions per cycle (IPC < 0.5). Scaling CPU cores or increasing CPU limits will not help; the fix requires optimizing data locality or increasing memory bandwidth.

**Indicator:**

- [Step 4] `perf stat -a -- sleep 5` shows IPC below 0.5 combined with LLC cache-miss rate above 10%
- [Step 2] process shows high `%usr` CPU in `pidstat` but application throughput remains low

<!-- match: {"step": 4, "predicate": "threshold", "target": "ipc", "op": "<", "value": 0.5} -->

**Mitigation:**

- **Risk:** No immediate mitigation changes the workload; profiling is diagnostic only.
- **Command:**

  ```bash
  perf stat -e cache-misses,cache-references,instructions,cycles -p <PID> -- sleep 10
  ```

- **Duration:** Profiling only; no production impact.

**Resolution:**

```bash
# Pin process to NUMA node local to its memory for immediate improvement
numactl --cpunodebind=0 --membind=0 <command>
```

Optimize application data structures for cache locality: reduce working set size, use structure-of-arrays instead of array-of-structs, or apply prefetch hints. For JVM workloads, tune GC to reduce heap fragmentation.

**Verification:** After optimization, `perf stat` shows IPC > 1.0 and LLC cache-miss rate below 5%, with application throughput returning to expected levels.

---

### Cause Z: Unidentified CPU saturation cause

**Statement:** CPU saturation is confirmed but the specific cause cannot be determined from the diagnostic steps in this runbook.

**Mechanism:** [Default]

**Indicator:**

- [Default] all Cause A–F indicators were checked but none matched the observed pattern

**Mitigation:**

- **Risk:** Capturing profiling data has low overhead (< 1% at 99 Hz). Escalation may require a maintenance window.
- **Command:**

  ```bash
  # Collect a comprehensive system-wide profile for escalation
  perf record -F 99 -ag -- sleep 60
  perf report --stdio > /tmp/perf-report-$(hostname)-$(date +%Y%m%d%H%M).txt
  sar -u 1 300 > /tmp/sar-cpu-$(hostname)-$(date +%Y%m%d%H%M).txt
  ```

- **Duration:** Profile capture takes 60 seconds. Escalate immediately with the captured files.

**Resolution:** Out of runbook scope. Escalate to the system or application owner with `perf report` output, `sar` data, and `mpstat`/`pidstat` snapshots for deeper analysis.

**Verification:** Resolution is out of scope; confirmed by escalation team.

## Prevention

- Configure Prometheus alerting on load average ratio and CPU idle headroom:

  ```yaml
  # Alert: sustained CPU saturation (load > 1.5x CPU count for 5 minutes)
  # expr: node_load1 > count(node_cpu_seconds_total{mode="idle"}) * 1.5
  # Alert: low CPU idle headroom (> 90% utilization for 10 minutes)
  # expr: rate(node_cpu_seconds_total{mode="idle"}[5m]) < 0.1
  # Alert: container CPU throttling
  # expr: rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0.05
  ```

- Set CPU `requests` and `limits` on all Kubernetes containers. Requests drive scheduling; limits enforce cgroup quotas. Review limits quarterly against `container_cpu_cfs_throttled_seconds_total` to catch under-provisioned containers before they cause latency:

  ```yaml
  resources:
    requests:
      cpu: "500m"
    limits:
      cpu: "2000m"
  ```

- Enable continuous profiling with low-overhead tools (`py-spy` for Python, `async-profiler` for JVM, `perf` for native) to detect CPU regressions in pre-production load tests before they reach production.
- Install and enable `irqbalance` on all multi-core hosts to prevent interrupt storms pinned to a single CPU core:

  ```bash
  systemctl enable --now irqbalance
  ```

- Implement capacity planning: track weekly CPU utilization trends. Add compute capacity when sustained utilization exceeds 70% during peak hours. Prefer horizontal scaling (more instances) over vertical scaling for stateless services.

## Sources

- [Brendan Gregg — Linux Performance](https://www.brendangregg.com/linuxperf.html) — comprehensive reference for Linux CPU performance tools (top, mpstat, pidstat, perf, vmstat, eBPF). Priority 1.
- [Brendan Gregg — USE Method](https://www.brendangregg.com/usemethod.html) — Utilization-Saturation-Errors methodology; CPU checklist (utilization: `mpstat`, saturation: `vmstat r`, errors: `perf stat`). Priority 1.
- [Brendan Gregg — eBPF Tracing Tools](https://www.brendangregg.com/ebpf.html) — BCC and bpftrace tools including `runqlat`, `cpudist`, `profile` for CPU off-CPU and run-queue latency analysis. Priority 1.
- [Linux man page: mpstat(1)](https://man7.org/linux/man-pages/man1/mpstat.1.html) — per-CPU utilization breakdown (%usr, %sys, %iowait, %irq, %soft, %idle). Priority 1.
- [Linux man page: pidstat(1)](https://man7.org/linux/man-pages/man1/pidstat.1.html) — per-process and per-thread CPU utilization with user/system breakdown. Priority 1.
- [Linux man page: vmstat(8)](https://man7.org/linux/man-pages/man8/vmstat.8.html) — run-queue depth (`r`), context switches (`cs`), and block I/O wait indicators. Priority 1.
- [Linux kernel docs: cgroup-v2 CPU controller](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html) — `cpu.max`, `cpu.stat` (`nr_throttled`, `throttled_time`), CFS bandwidth enforcement. Priority 1.
