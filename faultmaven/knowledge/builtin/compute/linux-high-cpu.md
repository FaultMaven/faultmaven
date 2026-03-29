---
id: linux-high-cpu
title: "Linux High CPU Utilization: Diagnosis and Resolution"
domain: compute
service: linux
symptom_class:
  - cpu_saturation
  - latency
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - linux
  - cpu
  - load-average
  - perf
  - mpstat
  - pidstat
  - runaway-process
  - kernel-cpu
  - user-space
  - ebpf
  - context-switch
difficulty: intermediate
---

# Linux High CPU Utilization: Diagnosis and Resolution

## Problem Definition

This runbook applies to Linux systems (kernel 4.18+) with any CPU architecture (x86_64, ARM64). You need root or sudo access and the `sysstat` package (`mpstat`, `pidstat`, `sar`), `procps` (`top`, `ps`, `vmstat`), and optionally `linux-tools` or `perf-tools` for CPU profiling with `perf`. For eBPF-based tools, kernel 4.18+ with BCC or bpftrace installed is required.

One or more CPUs are at or near 100% utilization, causing increased latency for applications, degraded throughput, and potential service timeouts. Processes compete for CPU time, leading to elevated run-queue depth and scheduling delays.

Three distinct categories produce this symptom: **user-space CPU saturation** (application code consuming excessive cycles due to busy loops, inefficient algorithms, or unexpected load), **kernel-space CPU saturation** (system calls, interrupt processing, or kernel threads consuming CPU, visible as high `%sys` in top/mpstat), and **runaway processes** (a single process stuck in an infinite loop or experiencing a bug that causes unbounded CPU consumption). Each requires a different diagnostic path and resolution. CPU saturation differs from CPU utilization: a system at 80% CPU utilization with low run-queue depth is busy but healthy, while a system at 80% utilization with a run-queue depth of 20 is saturated and degraded. The USE method (Utilization, Saturation, Errors) provides the framework for distinguishing these states.

Common alert patterns: load average exceeds CPU count, CPU utilization sustained above 90%, application response time SLA breaches, health check timeouts.

## Diagnostic Steps

### Step 1: Assess Overall CPU State

**What this checks:** Whether the system is CPU-saturated and whether the problem is user-space, kernel-space, or I/O-related.

```bash
uptime
```

**Expected output:** Load averages for 1, 5, and 15 minutes.

**What the finding means:** Compare against CPU count (`nproc`). Load average exceeding CPU count indicates saturation. A rising trend (1-min > 15-min) signals an active or worsening incident.

```bash
mpstat -P ALL 1 5
```

Shows per-CPU utilization broken down by `%usr` (user-space), `%sys` (kernel), `%iowait` (blocked on I/O), `%irq`/`%soft` (hardware/software interrupts), and `%idle`. Key patterns: high `%usr` across all CPUs means application-level saturation; high `%sys` across all CPUs means kernel overhead; one CPU at 100% while others are idle means single-threaded bottleneck or IRQ affinity issue; high `%iowait` means processes are blocked on disk I/O, not CPU-bound.

```bash
vmstat 1 5
```

Check the `r` column (run queue). Values consistently exceeding the CPU count confirm CPU saturation. The `cs` column shows context switches per second; a sudden spike indicates contention.

### Step 2: Identify Top CPU-Consuming Processes

**What this checks:** Which specific processes are consuming the most CPU.

```bash
top -bn1 -o %CPU | head -30
```

**Expected output:** Process list sorted by CPU percentage.

**What the finding means:** Note the PID, user, and command for top consumers. Batch mode (`-b`) with single iteration (`-n1`) captures a snapshot without interactive mode.

```bash
pidstat -u 1 5
```

Per-process CPU utilization sampled every second for 5 seconds, broken down into `%usr` and `%system`. More precise than top for identifying transient spikes.

```bash
pidstat -t -p <PID> 1 5
```

Thread-level CPU breakdown for a specific process. Identifies which threads within a multi-threaded application are consuming CPU. Useful for Java, Go, and Python applications where specific worker threads may be stuck.

### Step 3: Determine User vs Kernel CPU Breakdown

**What this checks:** Whether CPU time is being spent in user-space application code or in kernel system calls, which determines the investigation path.

```bash
mpstat 1 5
```

**Expected output:** Aggregate CPU statistics with `%usr` and `%sys` breakdown.

**What the finding means:** If aggregate `%sys` is above 20-30%, kernel overhead is significant. Normal applications should spend the majority of CPU time in user space.

```bash
perf top -g
```

Live view of the hottest functions across the system, including both user-space and kernel functions. The `-g` flag enables call-graph display. Press `q` to exit.

```bash
perf stat -a -- sleep 5
```

System-wide hardware counter summary over 5 seconds. Reports IPC (instructions per cycle), cache misses, branch mispredictions, and context switches. Low IPC (<0.5) combined with high cache misses suggests a memory-bound workload masquerading as CPU saturation.

### Step 4: Profile the CPU-Bound Process

**What this checks:** Which specific functions and code paths are consuming CPU cycles in the target process.

```bash
perf record -F 99 -p <PID> -g -- sleep 30
perf report --stdio
```

**Expected output:** Function-level CPU profile sorted by percentage of samples, with call chains.

**What the finding means:** Wide plateaus at specific functions identify the hot code paths. For flame graph visualization:

```bash
perf record -F 99 -ag -- sleep 30
perf script | stackcollapse-perf.pl | flamegraph.pl > flamegraph.svg
```

System-wide CPU flame graph (requires Brendan Gregg's FlameGraph tools). Width represents time spent; wide plateaus indicate the hottest code paths.

### Step 5: Investigate Kernel CPU Time (If %sys Is High)

**What this checks:** Which system calls and kernel paths are consuming CPU time.

```bash
perf record -e syscalls:sys_enter -a -g -- sleep 10
perf report --stdio
```

**Expected output:** Syscall entry points ranked by frequency with call chains.

**What the finding means:** Common offenders: excessive `futex` (lock contention), `epoll_wait` (event loop spin), `read`/`write` (I/O-heavy paths).

```bash
strace -c -p <PID> -e trace=all -f 2>&1 | head -30
```

Summary of syscall counts and time for a specific process. Note: strace adds significant overhead; use on production processes only briefly.

```bash
cat /proc/interrupts | head -5; sleep 5; cat /proc/interrupts | head -5
```

Compare interrupt counts across two snapshots to identify interrupt storms. A single IRQ line incrementing rapidly can saturate one CPU core.

### Step 6: Check for Runaway or Zombie Processes

**What this checks:** Whether a process is consuming CPU continuously without productive work.

```bash
ps aux --sort=-%cpu | head -20
```

**Expected output:** Snapshot of processes sorted by CPU usage.

**What the finding means:** Compare with Step 2 output. If the same process has been consuming CPU continuously, it is likely runaway.

```bash
ps -eo pid,ppid,user,%cpu,stat,start,time,command --sort=-%cpu | head -20
```

Extended view including parent PID, start time, and cumulative CPU time. A process with high cumulative CPU time (`TIME` column) that started recently is a strong runaway candidate.

```bash
ps aux | awk '$8 ~ /^[RD]/ {print}' | head -20
```

Lists processes in Running (R) or uninterruptible sleep (D) state. Multiple R-state processes confirm CPU contention. D-state processes are waiting on I/O, not CPU-bound, but they increase load average.

### Step 7: Check CPU Scheduling and Throttling

**What this checks:** Whether containerized workloads are being CPU-throttled by cgroup limits.

```bash
cat /sys/fs/cgroup/cpu/cpu.stat 2>/dev/null || cat /sys/fs/cgroup/cpu.stat 2>/dev/null
```

**Expected output:** Cgroup CPU statistics including `nr_throttled` and `throttled_time`.

**What the finding means:** Incrementing `nr_throttled` means the container is hitting its CPU limit. This means the process needs more CPU, not that the host is overloaded.

```bash
cat /proc/<PID>/sched | grep -E "nr_switches|nr_involuntary_switches|se.sum_exec_runtime"
```

A high ratio of involuntary to voluntary context switches indicates the process is being preempted (CPU contention), not yielding voluntarily.

## Mitigation

### Option A: Renice or Limit a Runaway Process

- **Risk:** Low. Lowering priority allows other processes to get CPU time but does not fix the runaway. The process continues to consume available CPU when the system is otherwise idle.
- **Command:**

  ```bash
  # Lower scheduling priority (higher nice value = lower priority)
  renice +10 -p <PID>

  # Alternatively, restrict to specific CPU cores
  taskset -cp 0,1 <PID>
  ```

- **Verify:**

  ```bash
  top -bn1 -p <PID> | tail -1
  mpstat -P ALL 1 3
  ```

  Confirm the process priority changed (NI column in top) and that other CPUs have reduced utilization.
- **Duration:** Safe for hours. Investigate root cause within 4 hours.

### Option B: Apply cgroup CPU Limit

- **Risk:** Medium. The target process will be CPU-throttled and may process requests more slowly, increasing latency for its consumers.
- **Command:**

  ```bash
  # cgroup v2: limit process to 50% of one CPU core
  mkdir -p /sys/fs/cgroup/cpu_limit
  echo "50000 100000" > /sys/fs/cgroup/cpu_limit/cpu.max
  echo <PID> > /sys/fs/cgroup/cpu_limit/cgroup.procs

  # cgroup v1 (older systems):
  mkdir -p /sys/fs/cgroup/cpu/cpu_limit
  echo 50000 > /sys/fs/cgroup/cpu/cpu_limit/cpu.cfs_quota_us
  echo 100000 > /sys/fs/cgroup/cpu/cpu_limit/cpu.cfs_period_us
  echo <PID> > /sys/fs/cgroup/cpu/cpu_limit/tasks
  ```

- **Verify:**

  ```bash
  mpstat -P ALL 1 3
  cat /sys/fs/cgroup/cpu_limit/cpu.stat 2>/dev/null
  ```

  Confirm overall CPU utilization has dropped and `nr_throttled` is incrementing.
- **Duration:** Safe for hours to days. Remove the cgroup once the root cause is resolved.

### Option C: Kill the Runaway Process

- **Risk:** High if the process serves live traffic. Data loss if the process has unsaved state. The process may respawn immediately if managed by systemd.
- **Command:**

  ```bash
  # Graceful termination first
  kill -TERM <PID>
  sleep 5

  # Force kill if still running
  kill -9 <PID>
  ```

- **Verify:**

  ```bash
  ps -p <PID> -o pid,stat,comm
  uptime
  mpstat 1 3
  ```

  Confirm the process is gone, load average is decreasing, and CPU utilization has dropped.
- **Duration:** Immediate. If the process is managed by systemd, it will restart automatically.

### Option D: Redistribute IRQ Affinity (If Single-CPU Saturation)

- **Risk:** Low-Medium. Changing IRQ affinity on production network interfaces may cause brief packet reordering.
- **Command:**

  ```bash
  # Stop irqbalance daemon temporarily
  systemctl stop irqbalance

  # Distribute network IRQs across cores
  grep eth0 /proc/interrupts | awk '{print $1}' | tr -d ':' | while read irq; do
      echo 2 > /proc/irq/$irq/smp_affinity_list
  done
  ```

- **Verify:**

  ```bash
  mpstat -P ALL 1 5
  ```

  Confirm the previously saturated CPU has reduced interrupt load.
- **Duration:** Persists until reboot or until irqbalance is restarted.

## Root Cause Resolution

**If** Step 2 shows a single application process consuming 100% of one or more cores with high `%usr` time **then** profile the application to identify the hot code path:

```bash
perf record -F 99 -p <PID> -g -- sleep 30
perf report --stdio --sort=dso,symbol | head -40
```

Common findings: infinite loop due to a bug, inefficient regex evaluation, unbounded retry loop, or missing backpressure. Fix the application code, deploy the fix, and confirm CPU returns to normal.

**If** Step 3 shows high `%sys` (>30%) with frequent `futex` syscalls **then** the application has lock contention:

```bash
perf record -e syscalls:sys_enter_futex -p <PID> -g -- sleep 10
perf report --stdio
```

Reduce lock contention by switching to lock-free data structures, reducing critical section scope, or using read-write locks. For Java applications, analyze thread dumps with `jstack <PID>`.

**If** Step 5 shows interrupt storms on a single CPU **then** distribute interrupts or enable RSS (Receive Side Scaling):

```bash
ethtool -L eth0 combined $(nproc)
systemctl start irqbalance
```

**If** Step 7 shows container CPU throttling (`nr_throttled` incrementing) **then** the container CPU limit is too low:

```bash
# Kubernetes: increase CPU limits
kubectl patch deployment <name> -p \
  '{"spec":{"template":{"spec":{"containers":[{"name":"<container>","resources":{"limits":{"cpu":"2000m"}}}]}}}}'

# Docker: update CPU quota
docker update --cpus=2 <container_id>
```

**If** Step 2 shows many processes each consuming moderate CPU (aggregate overload) **then** the host is undersized. Scale horizontally or vertically. Immediate mitigation: defer non-critical batch workloads:

```bash
ps -eo pid,user,%cpu,comm --sort=-%cpu | grep -E "cron|batch|backup|report"
```

**If** `perf stat` in Step 3 shows low IPC (<0.5) with high LLC cache misses **then** the workload is memory-bound, not truly CPU-bound:

```bash
perf stat -e cache-misses,cache-references,instructions,cycles -p <PID> -- sleep 10
```

Optimize data structures for cache locality, reduce working set size, or use NUMA-aware memory allocation with `numactl`.

## Verification

After applying the root cause fix, confirm the issue is resolved:

```bash
# Check load average is below CPU count
uptime
nproc
```

Load average should trend downward and stabilize below the CPU count within 5-15 minutes.

```bash
# Confirm CPU utilization is at a healthy level
mpstat -P ALL 1 5
```

No individual CPU should be sustained above 90% utilization. The `%idle` column should show available headroom across all cores.

```bash
# Verify the previously offending process is behaving normally
pidstat -u -p <PID> 1 10
```

CPU usage should be at expected levels for its workload.

```bash
# Check that run queue depth has returned to normal
vmstat 1 5
```

The `r` column should be at or below the CPU count. Context switches (`cs`) should be at baseline levels.

```bash
# Monitor for 15-30 minutes to confirm stability
sar -u 1 900 | tail -20
```

Observe CPU utilization over a sustained period with no recurring spikes back to saturation levels.

## Prevention

### Configure CPU Monitoring Alerts Using the USE Method

```bash
# Prometheus node_exporter alert rules (example)
# Utilization: sustained high CPU usage
# rate(node_cpu_seconds_total{mode="idle"}[5m]) < 0.1           --> critical (>90%)
# rate(node_cpu_seconds_total{mode="idle"}[5m]) < 0.2           --> warning  (>80%)

# Saturation: run queue depth exceeding CPU count
# node_load1 > count(node_cpu_seconds_total{mode="idle"}) * 1.5 --> warning
# node_load1 > count(node_cpu_seconds_total{mode="idle"}) * 2   --> critical

# Errors: CPU throttling in containers
# rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0.1     --> warning
```

### Set CPU Resource Limits for All Containers and Services

Prevent a single workload from monopolizing host CPU:

```yaml
# Kubernetes: always set CPU requests and limits
resources:
  requests:
    cpu: "500m"
  limits:
    cpu: "2000m"
```

### Establish Application-Level CPU Baselines

Profile applications during normal load and document expected CPU utilization. Alerts should fire when usage deviates significantly from baseline, not just on absolute thresholds.

### Enable Continuous Profiling in Production

Tools such as `py-spy` (Python), `async-profiler` (Java), and `perf` (native) can run with minimal overhead (<2%) to catch CPU regressions before they become incidents:

```bash
async-profiler -d 60 -f /tmp/profile.html -e cpu <PID>
```

### Configure irqbalance for Multi-Core Systems

```bash
systemctl enable irqbalance
systemctl start irqbalance
```

### Implement Capacity Planning

Track CPU utilization trends weekly. Provision additional compute capacity when sustained utilization exceeds 70% during peak hours. Use horizontal scaling (more instances) over vertical scaling (bigger instances) where the application supports it.

### Review Container CPU Limits Quarterly

Limits set at deployment time may become insufficient as workload patterns change. Monitor `container_cpu_cfs_throttled_seconds_total` to detect under-provisioned containers before they cause latency impact.

## Sources

- [Brendan Gregg - Linux Performance](https://www.brendangregg.com/linuxperf.html) - Comprehensive reference for Linux CPU performance analysis tools including top, mpstat, pidstat, perf, and eBPF-based tools.
- [Brendan Gregg - USE Method](https://www.brendangregg.com/usemethod.html) - Utilization-Saturation-Errors methodology for systematic resource analysis.
- [Brendan Gregg - eBPF Tracing Tools](https://www.brendangregg.com/ebpf.html) - Reference for BCC and bpftrace tools including runqlat, cpudist, and profile.
- [Brendan Gregg - CPU Utilization is Wrong](https://www.brendangregg.com/blog/2017-05-09/cpu-utilization-is-wrong.html) - Why CPU utilization alone is misleading and IPC provides better insight.
- [Linux man pages: perf(1)](https://man7.org/linux/man-pages/man1/perf.1.html) - Linux profiling framework for hardware performance counters, tracepoints, and CPU sampling.
- [Linux man pages: mpstat(1)](https://man7.org/linux/man-pages/man1/mpstat.1.html) - Per-CPU utilization breakdown including user, system, iowait, IRQ, and idle time.
- [Linux man pages: pidstat(1)](https://man7.org/linux/man-pages/man1/pidstat.1.html) - Per-process and per-thread CPU utilization with user/system breakdown.
- [Linux man pages: vmstat(8)](https://man7.org/linux/man-pages/man8/vmstat.8.html) - Virtual memory and CPU statistics including run-queue depth and context switch rates.
- [Linux kernel documentation: cgroups](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html) - Authoritative reference for cgroup v2 CPU controller including cpu.max and throttling behavior.
