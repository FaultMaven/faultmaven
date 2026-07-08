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
version: "2.0.0"
last_updated: "2026-06-25"
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

**Statement:** A single application process consumes 100% of one or more CPU cores continuously because an infinite loop, unbounded retry, or bug prevents it from yielding.

**Chain:**
- root: application code contains an infinite loop, unbounded retry, or missing sleep/backoff so the process never yields the CPU.
- s1: the process never calls a blocking syscall or sleep, so the kernel scheduler gives it every available quantum on its core(s).
- s2: other processes on those cores are starved of CPU time and the process holds a stable high CPU percentage across samples.
- D: CPU saturation manifests as described in Symptom Recognition.

**Indicators:**
- s2: [Step 2] same PID appears at or near 100% `%CPU` across all `pidstat` sample intervals.
- root: [Step 5] `perf report` shows a single flat call chain with no blocking points (tight loop).

**Interventions:**
- **mitigation** (s1): lower priority or pin the process to two cores so other workloads get CPU time.

  ```bash
  renice +10 -p <PID>
  # Or restrict to two cores to isolate impact
  taskset -cp 0,1 <PID>
  ```

  **Risk:** Keeps the runaway process running; it continues consuming available idle CPU and the root cause is unfixed. **Duration:** Safe for hours; investigate and deploy a code fix within 4 hours. **Verification:** `mpstat` shows other workloads regaining CPU time; the renice'd process no longer starves co-located processes.
- **remediation** (root): profile to find the looping code path, then fix the loop/retry/backoff in the application and deploy.

  ```bash
  # Profile the process to identify the looping code path
  perf record -F 99 -p <PID> -g -- sleep 30
  perf report --stdio --sort=dso,symbol | head -40
  ```

  **Verification:** After the fix is deployed, `pidstat -u -p <PID> 1 10` shows CPU usage returned to expected baseline and does not approach 100% on any sample.

---

### Cause B: User-space CPU saturation from aggregate application load

**Statement:** The host is under-provisioned for current traffic, so multiple application processes collectively saturate all CPU cores without any single process misbehaving.

**Chain:**
- root: the host's CPU capacity is under-provisioned for the current request rate (a capacity problem, not an application bug).
- s1: total request rate exceeds what the CPUs can service, so every core runs high `%usr` with minimal `%idle`.
- s2: load average climbs above CPU count and run queue depth rises as work backs up.
- D: CPU saturation manifests as described in Symptom Recognition.

**Indicators:**
- s2: [Step 1] run queue `r` > CPU count across all `vmstat` intervals.
- s1: [Step 3] `mpstat` shows all CPUs with `%usr` > 70% and `%idle` < 10%, with no single dominant process in Step 2.

**Interventions:**
- **mitigation** (s1): defer batch/non-critical workloads by lowering their priority to free CPU for serving traffic.

  ```bash
  # Identify non-critical batch processes and lower their priority
  ps -eo pid,user,%cpu,comm --sort=-%cpu | grep -E "cron|batch|backup|report"
  renice +15 -p <BATCH_PID>
  ```

  **Risk:** Deferring non-critical work reduces CPU competition immediately; primary serving traffic is unaffected. **Duration:** Safe until capacity is scaled; scale compute within 24 hours. **Verification:** `mpstat` shows `%idle` headroom returning on serving cores while batch jobs run slower.
- **remediation** (root): horizontally scale by adding instances behind the load balancer. Ensure node capacity exists or trigger cluster-autoscaler; rollback by scaling back to the original replica count.

  ```bash
  # Horizontal scale: add instances behind the load balancer
  # Kubernetes example — increase replica count
  kubectl scale deployment <name> --replicas=<N+K>
  ```

  **Verification:** After scaling, `uptime` shows load average below CPU count per node, and `mpstat` shows `%idle` > 20% headroom on all cores.

---

### Cause C: High kernel CPU time from lock contention (futex storms)

**Statement:** Lock contention in multi-threaded application code drives a futex syscall storm that consumes excessive `%sys` CPU time.

**Chain:**
- root: multi-threaded application code contends on a shared mutex or spin-lock.
- s1: threads repeatedly call `futex(FUTEX_WAIT/FUTEX_WAKE)`, so the kernel spends CPU managing the futex queue rather than running application logic.
- s2: `mpstat` shows high `%sys`, `futex` tops syscall profiles, and the process's involuntary context switches climb.
- D: CPU saturation manifests as described in Symptom Recognition.

**Indicators:**
- s2: [Step 3] `mpstat` aggregate `%sys` > 20% sustained.
- s1: [Step 6] `perf report` on `syscalls:sys_enter` shows `futex` in top entries.

**Interventions:**
- **mitigation** (s1): profile the futex syscalls to confirm and localize the contention before changing code.

  ```bash
  perf record -e syscalls:sys_enter_futex -p <PID> -g -- sleep 10
  perf report --stdio | head -30
  ```

  **Risk:** `perf record` has less than 1% overhead at 99 Hz; `strace` has significant overhead, so use it for at most 30 seconds on production. **Duration:** Profiling is safe indefinitely; deploy the code fix within the same incident window. **Verification:** profile pinpoints the contended futex/lock to target in the fix.
- **remediation** (root): reduce lock contention — narrow critical sections, use read-write or lock-free structures, reduce shared state — then deploy.

  ```bash
  # Java: capture thread dump to identify contended locks
  jstack <PID> > /tmp/threaddump.txt
  grep -A5 "BLOCKED" /tmp/threaddump.txt | head -40
  ```

  **Verification:** After deploying the fix, `mpstat` aggregate `%sys` drops below 5%, and `perf stat -a -- sleep 5` shows IPC improved and context-switch rate decreased.

---

### Cause D: CPU saturation from interrupt storm on a single core

**Statement:** A high-frequency hardware interrupt (typically from a network interface) is pinned to a single CPU core, saturating it while other cores remain underutilized.

**Chain:**
- root: IRQ affinity pins a single NIC queue's interrupts to one core (default CPU 0 or via `irqbalance`).
- s1: under high packet rates that single queue's interrupts saturate the one core with `%irq`/`%soft` work.
- s2: `mpstat` shows one CPU at 100% with high `%irq`/`%soft` while others idle, and `/proc/interrupts` shows one IRQ line incrementing rapidly in one column.
- D: CPU saturation manifests as described in Symptom Recognition.

**Indicators:**
- s2: [Step 3] `mpstat -P ALL` shows one CPU with `%irq` + `%soft` > 50% while other CPUs show < 10% utilization.
- s2: [Step 8] single IRQ line increments by >50,000 per 5-second interval in one CPU column.

**Interventions:**
- **mitigation** (s1): stop `irqbalance` and manually spread the NIC IRQs across cores.

  ```bash
  systemctl stop irqbalance
  # Spread NIC IRQs across cores (adjust eth0 to actual interface)
  grep eth0 /proc/interrupts | awk '{print $1}' | tr -d ':' | while read irq; do
      echo ff > /proc/irq/$irq/smp_affinity
  done
  ```

  **Risk:** Manually setting affinity may cause brief packet reordering on the affected NIC. **Duration:** Persists until reboot; re-enable `irqbalance` after verifying the spread is stable. **Verification:** `mpstat -P ALL` shows interrupt load redistributing off the single core.
- **remediation** (root): enable multi-queue support on the NIC for a sustained fix. Requires driver support; a brief link flap is possible; rollback with `ethtool -L eth0 combined 1` and `systemctl restart irqbalance`.

  ```bash
  # Enable multi-queue support on the NIC for sustained fix
  ethtool -L eth0 combined $(nproc)
  systemctl enable --now irqbalance
  ```

  **Verification:** `mpstat -P ALL 1 5` shows interrupt load spread across multiple CPUs with no single core above 50% from `%irq`/`%soft`.

---

### Cause E: Container CPU throttling from under-provisioned cgroup limit

**Statement:** A cgroup CPU quota set too low for the workload throttles a containerized process, causing application latency while the host CPU still has headroom.

**Chain:**
- root: the container's cgroup CPU quota (`cpu.cfs_quota_us / cpu.cfs_period_us`) is set lower than its actual workload needs.
- s1: the container exhausts its quota within a scheduling period, so the CFS bandwidth controller throttles all its threads until the next period.
- s2: `nr_throttled` increments in `cpu.stat` and container response latency spikes, even though host `mpstat` shows `%idle > 20%`.
- D: container latency/throttling manifests as described in Symptom Recognition.

**Indicators:**
- s2: [Step 7] `nr_throttled` count is non-zero and increasing across repeated reads of `cpu.stat`.
- s2: [Step 3] host-level `mpstat` shows available `%idle` > 20% (host is NOT saturated).

**Interventions:**
- **mitigation** (s1): raise the cgroup CPU limit directly to stop throttling immediately. Ensure the host has headroom.

  ```bash
  # cgroup v2: raise limit to 150% of one CPU
  echo "150000 100000" > /sys/fs/cgroup/cpu_limit/cpu.max
  ```

  **Risk:** Increasing the limit lets this container consume more host CPU; ensure the host has headroom. **Duration:** Safe indefinitely; adjust the deployment manifest as the durable fix. **Verification:** `nr_throttled` stops incrementing on the next read of `cpu.stat`.
- **remediation** (root): raise the CPU limit in the deployment manifest so it persists across pod recreation. This triggers a rolling restart; rollback by patching back the original limit.

  ```bash
  # Kubernetes: increase CPU limits on the deployment
  kubectl patch deployment <name> -p \
    '{"spec":{"template":{"spec":{"containers":[{"name":"<container>","resources":{"limits":{"cpu":"2000m"}}}]}}}}'
  ```

  **Verification:** After increasing the limit, `nr_throttled` stops incrementing on the next check of `cpu.stat`, and application latency returns to baseline.

---

### Cause F: Memory-bound workload misdiagnosed as CPU saturation

**Statement:** Cache misses stall the CPU pipeline rather than compute-bound work, so the workload appears CPU-saturated when the real bottleneck is memory bandwidth.

**Chain:**
- root: the application's working set exceeds the CPU's last-level cache (LLC).
- s1: every memory access stalls the CPU pipeline waiting for DRAM, so the CPU is 100% utilized but completes very few instructions per cycle (IPC < 0.5).
- s2: scaling CPU cores or raising CPU limits does not help; throughput stays low while `%usr` looks high.
- D: apparent CPU saturation manifests as described in Symptom Recognition.

**Indicators:**
- s1: [Step 4] `perf stat -a -- sleep 5` shows IPC below 0.5 combined with LLC cache-miss rate above 10%.
- s2: [Step 2] process shows high `%usr` CPU in `pidstat` but application throughput remains low.

**Interventions:**
- **mitigation** (s1): pin the process to a NUMA node local to its memory for an immediate locality improvement.

  ```bash
  numactl --cpunodebind=0 --membind=0 <command>
  ```

  **Risk:** No workload change beyond NUMA pinning; improvement is bounded by data layout, not a full fix. **Duration:** Safe indefinitely; treat as a stopgap while optimizing locality. **Verification:** `perf stat` shows IPC rising and remote-memory access dropping after pinning.
- **remediation** (root): optimize for cache locality — reduce working-set size, prefer structure-of-arrays, add prefetch hints; for JVM, tune GC to reduce heap fragmentation.

  ```bash
  perf stat -e cache-misses,cache-references,instructions,cycles -p <PID> -- sleep 10
  ```

  **Verification:** After optimization, `perf stat` shows IPC > 1.0 and LLC cache-miss rate below 5%, with application throughput returning to expected levels.

---

### Cause Z: Unidentified

**Statement:** CPU saturation is confirmed but the specific cause cannot be determined from the diagnostic steps in this runbook.

**Chain:**
- root: the underlying cause of the confirmed CPU saturation is not identified by any Cause A–F pattern in this runbook.
- D: CPU saturation manifests as described in Symptom Recognition.

**Indicators:**
- root: [Default] all Cause A–F indicators were checked but none matched the observed pattern.

**Interventions:**
- **mitigation** (D): capture a comprehensive system-wide diagnostic snapshot and escalate to the system or application owner (SME).

  ```bash
  # Collect a comprehensive system-wide profile for escalation
  perf record -F 99 -ag -- sleep 60
  perf report --stdio > /tmp/perf-report-$(hostname)-$(date +%Y%m%d%H%M).txt
  sar -u 1 300 > /tmp/sar-cpu-$(hostname)-$(date +%Y%m%d%H%M).txt
  ```

  **Risk:** Capturing profiling data has low overhead (< 1% at 99 Hz); escalation may require a maintenance window. **Duration:** Profile capture takes 60 seconds; escalate immediately with the captured files. **Verification:** SME receives `perf report` output, `sar` data, and `mpstat`/`pidstat` snapshots and confirms the handoff.

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
