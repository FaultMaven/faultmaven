---
id: linux-oom-killer
title: "Linux OOM Killer Invocation"
domain: compute
service: linux
symptom_class:
  - oom
severity: critical
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - linux
  - oom
  - memory
  - kernel
  - dmesg
  - overcommit
  - cgroups
  - memory-leak
difficulty: intermediate
---

# Linux OOM Killer Invocation

## Problem Definition

This runbook applies to Linux systems (kernel 4.18+) with any memory configuration. You need root or sudo access and the `procps` package (`ps`, `free`), `util-linux` (`dmesg`), and `systemd` (`journalctl`). For memory profiling, language-specific tools are required: `valgrind` for C/C++, `jmap`/`jhat` for Java, `tracemalloc` for Python, or Chrome DevTools for Node.js.

The Linux kernel's Out-of-Memory (OOM) Killer has terminated one or more processes because the system exhausted available memory and swap. The killed process terminates immediately with SIGKILL (signal 9), receives no opportunity to clean up, and any in-flight work is lost. Services may fail to restart if memory pressure persists, and the system may become unstable if critical processes (init, sshd, database) are killed.

Linux allows processes to request more virtual memory than is physically available (memory overcommit). When all physical RAM and swap are exhausted and a process requests more memory, the kernel invokes the OOM Killer. It selects a process to kill based on a scoring algorithm (`oom_score`) that considers memory usage, process age, and the `oom_score_adj` value. The process with the highest score is killed to free memory.

The most frequent causes are: memory leaks in long-running applications (improper resource cleanup, unbounded caches, circular references), undersized instances where steady-state memory exceeds available RAM, insufficient or no swap space, fork bombs or runaway child processes, memory-intensive batch jobs temporarily spiking usage, cgroup memory limits being hit in containerized environments, kernel slab cache growth from filesystem-heavy workloads, and aggressive overcommit settings allowing more memory commitment than physically available.

**Typical error presentation:**

```text
[  123.456789] my-app invoked oom-killer: gfp_mask=0x6200ca(GFP_HIGHUSER_MOVABLE), order=0, oom_score_adj=0
[  123.456790] Out of memory: Killed process 12345 (my-app) total-vm:4096000kB, anon-rss:3800000kB, file-rss:1200kB
```

## Diagnostic Steps

### Step 1: Confirm OOM Kill Occurred

**What this checks:** Whether the OOM Killer was actually invoked and which process was killed.

```bash
dmesg -T | grep -i "oom\|out of memory\|killed process"
```

**Expected output:** Timestamped messages showing which process was killed, its memory usage (`anon-rss` = anonymous resident set, `total-vm` = virtual memory), and the `oom_score_adj` value.

**What the finding means:** The killed process name and memory usage at time of death are recorded. Multiple OOM kill events close together indicate persistent memory pressure. Also check journald:

```bash
journalctl -k | grep -i "oom\|killed process"
```

### Step 2: Identify What Was Killed and When

**What this checks:** The timeline and pattern of OOM kills to determine whether this is a one-time event or recurring issue.

```bash
dmesg -T | grep "Killed process" | awk '{print $1, $2, $3, $NF}'
```

**Expected output:** Timestamps and process names of all OOM kills.

**What the finding means:** Check if a critical service was killed and whether it auto-restarted:

```bash
systemctl status my-service
```

If the service was killed and restarted (via systemd `Restart=always`), it may be killed again if memory pressure persists.

### Step 3: Assess Current Memory State

**What this checks:** Whether the system is still under memory pressure or has recovered after the OOM kill.

```bash
free -h
```

**Expected output:** Memory breakdown including `total`, `used`, `free`, `available`, and swap usage.

**What the finding means:** Linux uses free memory for buffer/cache, so `free` may appear low while `available` is adequate. If `available` is near zero and swap is fully used, the system is still under pressure.

```bash
cat /proc/meminfo | grep -E "MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Slab|Committed_AS"
```

Key fields: `Committed_AS` (total memory committed, can exceed physical RAM with overcommit), `Slab` (kernel memory for caches), `SwapFree` (remaining swap).

### Step 4: Identify Top Memory Consumers

**What this checks:** Which processes are currently using the most memory, identifying potential offenders.

```bash
ps aux --sort=-%mem | head -20
```

**Expected output:** Processes sorted by memory percentage with RSS (Resident Set Size) showing actual physical memory used.

**What the finding means:** The `%MEM` and `RSS` columns identify the largest consumers. Compare against expected baselines.

```bash
ps -eo pid,user,rss,vsz,comm --sort=-rss | head -20
```

More precise sorting by RSS in kilobytes.

### Step 5: Check for Memory Leaks

**What this checks:** Whether a process's memory usage is growing over time, indicating a leak.

```bash
ps -eo pid,lstart,rss,comm --sort=-rss | head -20
```

**Expected output:** Process start times and current RSS.

**What the finding means:** A process started hours or days ago with very large RSS may be leaking. For a more precise check, track growth over time:

```bash
while true; do
  echo "$(date +%H:%M:%S) $(cat /proc/<PID>/status | grep VmRSS)"
  sleep 60
done
```

If VmRSS grows steadily without stabilizing, the process has a memory leak.

### Step 6: Check OOM Score and Adjustments

**What this checks:** Which processes are most likely to be killed next and whether any have been given OOM protection.

```bash
for pid in $(ls /proc/ | grep -E '^[0-9]+$'); do
  name=$(cat /proc/$pid/comm 2>/dev/null)
  score=$(cat /proc/$pid/oom_score 2>/dev/null)
  adj=$(cat /proc/$pid/oom_score_adj 2>/dev/null)
  if [ -n "$score" ] && [ "$score" -gt 100 ]; then
    echo "PID=$pid Name=$name Score=$score Adj=$adj"
  fi
done | sort -t= -k3 -rn | head -20
```

**Expected output:** Processes ranked by OOM score.

**What the finding means:** Processes with the highest `oom_score` are next to be killed. `oom_score_adj` ranges from -1000 (never kill) to +1000 (always kill first). Critical services like sshd should have negative adjustments.

### Step 7: Check Cgroup Memory Limits (Containers)

**What this checks:** Whether a container's cgroup memory limit was hit, which causes a cgroup-level OOM kill even if the host has free memory.

```bash
# cgroups v2
cat /sys/fs/cgroup/memory.max 2>/dev/null
cat /sys/fs/cgroup/memory.current 2>/dev/null

# Docker containers
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}"
```

**Expected output:** Memory limits and current usage for containers.

**What the finding means:** If a container's usage approaches its limit, it will be OOM-killed by the cgroup controller regardless of host memory availability. The fix is to increase the container's memory limit or optimize the application.

### Step 8: Check Overcommit Settings

**What this checks:** How the kernel handles memory allocation requests, which affects when and whether the OOM Killer activates.

```bash
sysctl vm.overcommit_memory vm.overcommit_ratio
```

**Expected output:** `vm.overcommit_memory` (0=heuristic, 1=always allow, 2=strict) and `vm.overcommit_ratio` (percentage).

**What the finding means:** Mode 0 (default) uses a heuristic that allows some overcommit. Mode 1 always allows allocations (dangerous). Mode 2 denies allocations exceeding `CommitLimit = swap + RAM * (overcommit_ratio / 100)`.

```bash
grep CommitLimit /proc/meminfo
grep Committed_AS /proc/meminfo
```

If `Committed_AS` exceeds `CommitLimit` with `overcommit_memory=0`, the OOM Killer may activate.

## Mitigation

### Option 1: Kill the Memory-Hogging Process Manually

If the OOM Killer selected the wrong process, manually kill the actual offender.

- **Risk:** Low if the target process is non-critical. The process receives SIGKILL and cannot clean up.
- **Command:**

  ```bash
  ps -eo pid,rss,comm --sort=-rss | head -5
  kill -9 <PID>
  ```

- **Verify:**

  ```bash
  free -h
  ```

  `available` memory should increase significantly.
- **Duration:** Immediate.

### Option 2: Restart the Service with Memory Limits

- **Risk:** Medium. The service restarts (brief downtime). The memory limit may cause the service to OOM again if set too low.
- **Command:**

  ```bash
  sudo systemctl edit my-service
  # Add:
  # [Service]
  # MemoryMax=2G
  # MemoryHigh=1.5G

  sudo systemctl restart my-service
  ```

- **Verify:**

  ```bash
  systemctl status my-service
  systemctl show my-service | grep Memory
  ```

  The service should be running with the configured memory limits.
- **Duration:** Service restart takes seconds to minutes.

### Option 3: Add Swap Space Temporarily

Provides breathing room when the system is critically low on memory.

- **Risk:** Medium. Swap is significantly slower than RAM (10-100x). SSD-backed swap is acceptable for short-term relief; HDD swap causes severe performance degradation.
- **Command:**

  ```bash
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  ```

- **Verify:**

  ```bash
  swapon --show
  free -h
  ```

  Swap total should show the added space.
- **Duration:** Immediate. Remove when root cause is fixed: `sudo swapoff /swapfile && sudo rm /swapfile`.

### Option 4: Protect Critical Processes from OOM Killer

Prevent the OOM Killer from targeting essential services while the root cause is investigated.

- **Risk:** Low-Medium. If the protected process is the memory hog, the OOM Killer will kill other processes instead, potentially causing cascading failures.
- **Command:**

  ```bash
  # Protect sshd (critical for remote access)
  echo -1000 > /proc/$(pgrep -o sshd)/oom_score_adj

  # Make a specific process more likely to be killed
  echo 500 > /proc/<PID-of-leaking-app>/oom_score_adj
  ```

- **Verify:**

  ```bash
  cat /proc/$(pgrep -o sshd)/oom_score_adj
  ```

  Should show `-1000`.
- **Duration:** Immediate. Persists until process restart. For permanent settings, use systemd `OOMScoreAdjust=`.

## Root Cause Resolution

**If** Step 5 shows a process's RSS growing over time without stabilizing **then** the application has a memory leak. Profile the application:

```bash
# For C/C++ applications: use Valgrind
valgrind --leak-check=full --show-leak-kinds=all ./my-app

# For Java applications: heap dump analysis
jmap -dump:live,format=b,file=/tmp/heap.hprof $(pgrep java)
# Analyze with Eclipse MAT or jhat

# For Python applications: use tracemalloc
# Add to application: import tracemalloc; tracemalloc.start()
# Then: snapshot = tracemalloc.take_snapshot()
# top_stats = snapshot.statistics('lineno')

# For Node.js: use --inspect and Chrome DevTools heap profiler
node --inspect my-app.js
```

Fix the leak in the application code, then deploy the patched version.

**If** the workload's steady-state memory usage exceeds 80% of available RAM **then** add more memory:

```bash
echo "Committed: $(grep Committed_AS /proc/meminfo | awk '{print $2/1024/1024, "GB"}')"
echo "Total RAM: $(grep MemTotal /proc/meminfo | awk '{print $2/1024/1024, "GB"}')"
```

For cloud instances, resize to a larger instance type. Target at least 20% headroom above peak usage.

**If** Step 3 shows no swap or swap fully used **then** configure persistent swap:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.d/99-swap.conf
sudo sysctl -p /etc/sysctl.d/99-swap.conf
```

**If** Step 7 shows a container hitting its cgroup memory limit **then** increase the limit or optimize the application:

```bash
# Docker: increase memory limit
docker update --memory 4g --memory-swap 4g <container-id>

# Kubernetes: increase memory limit in the pod spec
# resources:
#   limits:
#     memory: "4Gi"
#   requests:
#     memory: "2Gi"
```

**If** Step 8 shows `overcommit_memory=0` and frequent OOM kills **then** consider switching to strict overcommit to fail allocations early:

```bash
echo 'vm.overcommit_memory=2' | sudo tee -a /etc/sysctl.d/99-overcommit.conf
echo 'vm.overcommit_ratio=80' | sudo tee -a /etc/sysctl.d/99-overcommit.conf
sudo sysctl -p /etc/sysctl.d/99-overcommit.conf
```

With mode 2, processes receive `ENOMEM` (malloc returns NULL) instead of being OOM-killed. Requires applications to handle allocation failures.

**If** Step 3 shows high `Slab` usage (>1 GB) **then** increase cache pressure to reclaim kernel memory:

```bash
echo 'vm.vfs_cache_pressure=200' | sudo tee -a /etc/sysctl.d/99-cache.conf
sudo sysctl -p /etc/sysctl.d/99-cache.conf
```

Default is 100. Values above 100 cause the kernel to reclaim dentry and inode caches more aggressively.

## Verification

After applying the fix, confirm the system is stable:

```bash
# Check memory is healthy
free -h
```

`available` memory should be at least 20% of total RAM. Swap usage should be minimal (under 10% of total swap).

```bash
# Watch for new OOM events (run for 30-60 minutes during peak load)
dmesg -T --follow | grep -i "oom\|killed process"
```

No new OOM kill messages should appear.

```bash
# Verify the service is running and memory is stable
systemctl status my-service
ps -eo pid,rss,comm --sort=-rss | head -10
```

The service should be running and its RSS should be stable (not growing).

```bash
# Record memory usage for 24-hour trend analysis
sar -r 60 1440 > /tmp/memory-trend.log &
```

Review after 24 hours. Memory usage should remain stable without approaching the OOM threshold.

## Prevention

### Set Memory Limits on All Services

Use systemd `MemoryMax` and `MemoryHigh` to constrain each service. `MemoryHigh` triggers throttling before `MemoryMax` triggers a cgroup OOM kill:

```ini
# /etc/systemd/system/my-service.service.d/memory.conf
[Service]
MemoryMax=2G
MemoryHigh=1.5G
```

### Configure Swap Appropriately

A general guideline is swap equal to 50-100% of RAM for servers with 8+ GB. Swap acts as a safety net; the goal is not to use it heavily but to have it available during transient spikes.

### Protect Critical Processes

Set `OOMScoreAdjust=-1000` in systemd unit files for sshd, init, and other critical system processes:

```ini
# /etc/systemd/system/sshd.service.d/oom.conf
[Service]
OOMScoreAdjust=-1000
```

### Monitor Memory Usage with Alerts

Set up alerts at 80% (warning) and 90% (critical) memory usage:

```yaml
- alert: HostMemoryUnderPressure
  expr: (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) > 0.9
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "Host {{ $labels.instance }} memory usage above 90%"
```

### Enable Memory Leak Detection in CI/CD

For languages with garbage collection (Java, Python, Node.js, Go), run memory profiling as part of the test suite. Fail the build if memory growth exceeds a threshold during load tests.

### Use cgroups for Resource Isolation

In multi-tenant environments, use cgroups (via systemd or Kubernetes) to ensure no single workload can consume all available memory. This prevents one application's leak from causing OOM kills of other applications.

## Sources

- [Brendan Gregg - Linux Performance](https://www.brendangregg.com/linuxperf.html) - Comprehensive Linux performance analysis reference including memory diagnostics tools (free, vmstat, sar, slabtop).
- [Brendan Gregg - USE Method](https://www.brendangregg.com/usemethod.html) - Utilization-Saturation-Errors methodology for memory resource analysis.
- [Linux Kernel Documentation: OOM Killer](https://www.kernel.org/doc/html/latest/admin-guide/mm/concepts.html) - Kernel memory management concepts including overcommit and OOM handling.
- [Linux Kernel Documentation: vm.overcommit](https://www.kernel.org/doc/html/latest/admin-guide/sysctl/vm.html) - Overcommit memory settings, overcommit ratio, and CommitLimit calculation.
- [Linux man pages: proc_pid_oom_score(5)](https://man7.org/linux/man-pages/man5/proc_pid_oom_score.5.html) - OOM score calculation and the badness heuristic.
- [Linux man pages: proc_pid_oom_score_adj(5)](https://man7.org/linux/man-pages/man5/proc_pid_oom_score_adj.5.html) - OOM score adjustment values and their effect on process selection.
- [systemd.resource-control(5)](https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html) - Memory limits and cgroup configuration for systemd services.
