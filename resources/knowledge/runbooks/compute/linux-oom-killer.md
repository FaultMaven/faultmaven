---
id: "linux-oom-killer"
title: "Linux OOM Killer Invocation"
domain: compute
service: linux
symptom_class: [oom]
severity: critical
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [linux, oom, memory, kernel, dmesg, overcommit, cgroups, memory-leak, swap, slab]
difficulty: intermediate
---

## Symptom Recognition

- `dmesg` or `journalctl -k` shows: `Out of memory: Killed process <PID> (<name>) total-vm:<N>kB, anon-rss:<N>kB`
- `dmesg` shows: `<name> invoked oom-killer: gfp_mask=...`
- A service restarts unexpectedly with no application-level exception — systemd reports `Main process exited, code=killed, status=9/KILL`
- `systemctl status <service>` shows `Active: failed` or a restart loop with exit code 137 (128+9)
- System becomes sluggish or unresponsive before the kill event
- Monitoring alerts on available memory dropping below threshold followed by a process disappearing

## Applicability

Applies to Linux systems running kernel 4.18 or later with any memory configuration. Covers bare-metal hosts, VMs, and the host-level OOM killer for containerized workloads (cgroup-level OOM kills in containers are partially covered here; for Kubernetes-specific OOMKilled see the `k8s-oomkilled` runbook). Required access: root or sudo. Required tools: `procps` (`ps`, `free`), `util-linux` (`dmesg`), `systemd` (`journalctl`, `systemctl`), `sysstat` (`sar`, `vmstat`). Optional: `slabtop`, `valgrind`, `jmap`, Docker or containerd CLI.

## Diagnostic Steps

### Step 1: Confirm OOM kill and identify the victim

```bash
dmesg -T | grep -E "oom-killer|Out of memory|Killed process"
```

Expected output: one or more timestamped lines such as `[Tue May 12 04:23:11 2026] Out of memory: Killed process 18432 (myapp) total-vm:8192000kB, anon-rss:7800000kB, file-rss:4096kB`. An empty result means no OOM kill has occurred since last boot; also check journald:

```bash
journalctl -k --grep="oom|Killed process" --no-pager
```

### Step 2: Determine kill timeline and frequency

```bash
dmesg -T | grep "Killed process" | awk '{print $1, $2, $3, $NF}'
```

Expected output: timestamps and process names for every OOM kill since boot. Multiple kills within minutes indicate sustained pressure. Also check whether the service restarted automatically:

```bash
systemctl status <service-name> --no-pager
```

Expected output: `Active: active (running)` means it auto-restarted. `Active: failed` or `exit-code=137` means it is not running.

### Step 3: Assess current memory pressure

```bash
free -h
```

Expected output: `available` column shows usable memory (buffers+cache already subtracted). If `available` is under 200 MB and swap is at or near 0, the system is still under pressure. Also pull fine-grained fields:

```bash
grep -E "MemTotal|MemAvailable|SwapTotal|SwapFree|Committed_AS|CommitLimit|Slab" /proc/meminfo
```

Expected output: key values for committed vs limit comparison. If `Committed_AS` exceeds `CommitLimit`, allocations are failing under strict overcommit mode.

### Step 4: Identify top memory consumers

```bash
ps -eo pid,user,rss,vsz,comm --sort=-rss | head -20
```

Expected output: processes sorted by RSS (resident set — actual physical pages) descending. The top entries are the largest current consumers. Note the PID and process name for the suspected offender.

### Step 5: Check for memory growth (leak detection)

```bash
ps -eo pid,lstart,rss,comm --sort=-rss | head -10
```

Expected output: process start times alongside current RSS. A process started 8+ hours ago with RSS in the gigabytes is a strong leak candidate. Track growth with:

```bash
watch -n 30 'grep VmRSS /proc/<PID>/status'
```

Expected output: `VmRSS` value increasing monotonically over successive readings indicates a leak.

### Step 6: Inspect OOM scores

```bash
for pid in /proc/[0-9]*/; do
  pid_num=${pid//[^0-9]/}
  name=$(cat /proc/$pid_num/comm 2>/dev/null) || continue
  score=$(cat /proc/$pid_num/oom_score 2>/dev/null)
  adj=$(cat /proc/$pid_num/oom_score_adj 2>/dev/null)
  [ "${score:-0}" -gt 100 ] 2>/dev/null && echo "PID=$pid_num score=$score adj=$adj name=$name"
done | sort -t= -k2 -rn | head -20
```

Expected output: processes with the highest OOM score (most likely to be killed next). `oom_score_adj` of -1000 means the process is never killed; +1000 means it is killed first.

### Step 7: Check cgroup memory limits (containers / systemd units)

```bash
# cgroups v2 — host-level current slice
cat /sys/fs/cgroup/memory.max 2>/dev/null
cat /sys/fs/cgroup/memory.current 2>/dev/null

# Docker containers
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}" 2>/dev/null
```

Expected output: `memory.max` shows the cgroup ceiling; `memory.current` shows current usage. A container showing usage at or near its limit triggered a cgroup-level OOM kill independent of host memory state.

### Step 8: Check overcommit settings

```bash
sysctl vm.overcommit_memory vm.overcommit_ratio
grep -E "CommitLimit|Committed_AS" /proc/meminfo
```

Expected output: `vm.overcommit_memory` value of 0 (heuristic), 1 (always allow — dangerous), or 2 (strict). Under mode 2, `CommitLimit = swap + RAM × (overcommit_ratio / 100)`. If `Committed_AS > CommitLimit`, new allocations fail with ENOMEM and surviving processes may OOM-kill when trying to fault in pages.

### Step 9: Check kernel slab cache size

```bash
grep "^Slab:" /proc/meminfo
slabtop -o --sort=c | head -20
```

Expected output: `Slab:` total in kB. Values exceeding 1 GB on hosts with modest workloads indicate a slab leak. `slabtop` identifies the specific cache (e.g., `dentry`, `inode_cache`) that is growing.

## Causes

### Cause A: Application memory leak

**Statement:** A long-running process continuously allocates memory without releasing it, exhausting physical RAM and swap over time.

**Chain:**
- root: process retains references to allocated objects (unclosed handles, unbounded caches, circular references) so the allocator cannot return pages to the OS
- s1: the process's virtual and resident set sizes grow without bound across hours
- s2: growing RSS drives total memory demand past physical RAM and swap
- D: the kernel OOM killer fires and kills the leaking process ([Symptom])

**Indicators:**
- s1: [Step 5] `VmRSS` for the process increases monotonically across 30-minute intervals without stabilising
  <!-- match: {"step": 5, "predicate": "contains", "target": "VmRSS"} -->
- s1: [Step 4] the same process appears at the top of the RSS ranking for multiple hours
- D: [Step 1] dmesg identifies the same process name killed repeatedly across reboots

**Interventions:**
- **mitigation** (s1): restart the service to release leaked memory and buy time.

  ```bash
  systemctl restart <service-name>
  ```

  **Risk:** Application restart loses in-flight state; if the leak resumes after restart, the kill will recur without the root fix. **Duration:** Temporary relief — hours to days depending on leak rate. Root fix required. **Verification:** service returns to `Active: active (running)`; `VmRSS` resets to warm-up baseline.
- **remediation** (root): profile the application to find the leak site and patch it.

  ```bash
  # C/C++
  valgrind --leak-check=full --show-leak-kinds=all ./myapp

  # Java — heap dump for analysis in Eclipse MAT
  jmap -dump:live,format=b,file=/tmp/heap.hprof $(pgrep -o java)

  # Python — add to app code at startup
  # import tracemalloc; tracemalloc.start()

  # Node.js — start with inspector, attach Chrome DevTools heap profiler
  node --inspect myapp.js
  ```

  **Verification:** After patching and redeploying, run `watch -n 60 'grep VmRSS /proc/$(pgrep -o <name>)/status'` for 2 hours under production load. RSS must plateau and not grow beyond initial warm-up allocation. No new OOM entries in `dmesg -T`.

---

### Cause B: Undersized instance / steady-state demand exceeds RAM

**Statement:** The combined steady-state memory requirement of all workloads on the host permanently exceeds available physical RAM, making OOM kills inevitable under any sustained load.

**Chain:**
- root: aggregate steady-state RSS of all workloads on the host permanently exceeds physical RAM
- s1: as workloads warm up (JVM heap, page cache, connection pools), total RSS surpasses physical RAM
- s2: swap absorbs the excess but is 10–100× slower, raising latency until swap is exhausted
- D: with RAM and swap exhausted the OOM killer fires ([Symptom])

**Indicators:**
- root: [Step 4] total RSS of top 10 processes exceeds 90% of `MemTotal`
- s1: [Step 3] `available` memory is below 200 MB even at quiet hours (low traffic periods)
  <!-- match: {"step": 3, "predicate": "threshold", "target": "available_mb", "op": "<", "value": 200} -->
- s2: [Step 3] swap is more than 50% used during normal operations

**Interventions:**
- **mitigation** (s2): add a temporary swap file to absorb the excess until resize.

  ```bash
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  ```

  **Risk:** Adding swap is slower than RAM — acceptable as interim relief, harmful if relied on long-term. **Duration:** Until instance resize. Remove with `swapoff /swapfile && rm /swapfile` after resize. **Verification:** `swapon --show` lists the swapfile; OOM kills pause under current load.
- **remediation** (root): resize the instance or VM to provide at least 20% headroom above peak RSS.

  ```bash
  # Estimate required RAM
  echo "Peak RSS (kB):"
  ps -eo rss --sort=-rss | awk 'NR>1{sum+=$1} END{print sum}'
  echo "Current MemTotal (kB):"
  grep MemTotal /proc/meminfo | awk '{print $2}'
  ```

  **Verification:** After resize, `free -h` shows `available` above 20% of `MemTotal` under peak load. Swap usage stays under 10% of total swap. No OOM entries in `dmesg -T` for 24 hours post-resize.

---

### Cause C: No or insufficient swap space

**Statement:** The host has no swap configured (or swap is smaller than peak memory bursts), so transient allocation spikes that exceed RAM have no safety buffer and immediately trigger OOM kills.

**Chain:**
- root: host has no swap (or swap smaller than peak bursts), so there is no overflow store for anonymous pages
- s1: a transient allocation spike exhausts physical RAM with no buffer to evict anonymous pages into
- D: the OOM killer fires immediately rather than letting the spike subside ([Symptom])

**Indicators:**
- root: [Step 3] `SwapTotal: 0 kB` or `SwapFree` equals `SwapTotal` (swap full)
  <!-- match: {"step": 3, "predicate": "contains", "target": "SwapTotal:          0 kB"} -->
- s1: [Step 1] OOM kill occurs during a known batch job or traffic spike (transient event)

**Interventions:**
- **mitigation** (root): add a swap file now to give transient spikes a buffer.

  ```bash
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  ```

  **Risk:** HDD-backed swap causes severe latency (10–100× slower than RAM). SSD-backed swap is acceptable short-term. Never use swap as a substitute for right-sizing. **Duration:** Immediate and persistent across the session until the node reboots or swap is removed. **Verification:** `swapon --show` lists the swapfile; `free -h` shows non-zero `SwapTotal`.
- **remediation** (root): make swap persistent across reboots and tune swappiness.

  ```bash
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.d/99-swap.conf
  sudo sysctl -p /etc/sysctl.d/99-swap.conf
  ```

  **Verification:** `swapon --show` lists the swapfile with the expected size. `free -h` shows non-zero `SwapTotal`. No subsequent OOM events for the same transient workload pattern.

---

### Cause D: Cgroup memory limit hit (containerized workload)

**Statement:** A container or systemd unit hit its cgroup memory ceiling, triggering a cgroup-level OOM kill even though the host has sufficient free memory.

**Chain:**
- root: a container or systemd unit's `memory.max` cgroup ceiling is set below its actual peak RSS
- s1: the container's RSS climbs to the cgroup ceiling regardless of host-level free memory
- D: the cgroup OOM killer fires against that container's PID namespace ([Symptom])

**Indicators:**
- root: [Step 7] `memory.current` approaches `memory.max` for the target cgroup
- s1: [Step 7] `docker stats` shows a container at or above its memory limit
- D: [Step 1] dmesg contains `memory cgroup out of memory` or `oom-kill-constraint` referencing a cgroup path
  <!-- match: {"step": 7, "predicate": "contains", "target": "memory cgroup out of memory"} -->

**Interventions:**
- **mitigation** (s1): raise the cgroup limit to stop the immediate kills (only if host has headroom).

  ```bash
  # Docker
  docker update --memory 4g --memory-swap 4g <container-id>

  # systemd unit
  sudo systemctl set-property <unit>.service MemoryMax=4G
  sudo systemctl daemon-reload && sudo systemctl restart <unit>.service
  ```

  **Risk:** Increasing the limit allows the container to consume more host memory; verify there is headroom before doing so. **Duration:** Immediate; persists until next config change. **Verification:** `docker stats` shows usage below the new ceiling; no cgroup OOM kills for the container.
- **remediation** (root): set the limit to 20% above observed peak RSS, or optimise the app to reduce peak footprint.

  ```bash
  # Find peak RSS for a running container
  docker stats --no-stream --format "{{.MemUsage}}" <container-name>
  ```

  **Verification:** Run the workload pattern that previously triggered the OOM kill. `docker stats` shows memory usage stabilising below the new limit. No OOM entries for that container in `dmesg -T` or `journalctl -k`.

---

### Cause E: Kernel slab cache exhaustion

**Statement:** The kernel's internal slab allocator (dentries, inodes, network buffers) grows unboundedly on filesystem- or network-heavy workloads, consuming RAM that user-space processes cannot reclaim.

**Chain:**
- root: a filesystem- or network-heavy workload creates large numbers of kernel objects (dentries, inodes, buffers)
- s1: slab caches grow holding these objects and are not automatically evicted like user-space pages
- s2: slab consumes RAM that user-space processes cannot reclaim, lowering `MemAvailable`
- D: with reclaimable memory exhausted the OOM killer fires ([Symptom])

**Indicators:**
- root: [Step 9] `slabtop` shows `dentry`, `inode_cache`, or `ext4_inode_cache` as the largest consumers
- s1: [Step 9] `Slab:` in `/proc/meminfo` exceeds 1 GB
  <!-- match: {"step": 9, "predicate": "threshold", "target": "slab_kb", "op": ">", "value": 1048576} -->
- s2: [Step 3] `MemAvailable` is low despite few large user-space processes in Step 4

**Interventions:**
- **mitigation** (s2): raise `vfs_cache_pressure` to force the kernel to reclaim slab caches now.

  ```bash
  sudo sysctl -w vm.vfs_cache_pressure=200
  ```

  **Risk:** Raising `vfs_cache_pressure` aggressively shrinks caches which may increase filesystem latency as dentries must be reloaded from disk. **Duration:** Immediate; slab reclaim begins within seconds. Revert if latency increases. **Verification:** `grep "^Slab:" /proc/meminfo` shows a declining value within minutes.
- **remediation** (root): persist the pressure setting and investigate the workload generating excessive dentries/inodes.

  ```bash
  echo 'vm.vfs_cache_pressure=200' | sudo tee /etc/sysctl.d/99-slab.conf
  sudo sysctl -p /etc/sysctl.d/99-slab.conf
  ```

  **Verification:** `grep "^Slab:" /proc/meminfo` shows a declining value over 30 minutes. `MemAvailable` increases accordingly. No new OOM kills in `dmesg -T`.

---

### Cause F: Aggressive overcommit allowing runaway allocation

**Statement:** `vm.overcommit_memory=1` permits unlimited memory overcommit, allowing processes to commit far more virtual memory than physically exists, making OOM kills likely when pages are actually faulted in.

**Chain:**
- root: `vm.overcommit_memory=1` makes the kernel never refuse a `mmap`/`malloc` regardless of committed memory
- s1: processes over-allocate virtual memory far beyond physical RAM and the allocations succeed
- s2: when those committed pages are faulted in, no physical memory is available to back them
- D: the OOM killer fires with no prior gradual climb in monitoring ([Symptom])

**Indicators:**
- root: [Step 8] `vm.overcommit_memory = 1`
  <!-- match: {"step": 8, "predicate": "contains", "target": "vm.overcommit_memory = 1"} -->
- s1: [Step 8] `Committed_AS` is 2× or more `MemTotal`
- D: [Step 1] OOM kills occur without prior warning — no gradual memory climb visible in monitoring

**Interventions:**
- **mitigation** (root): switch to strict overcommit (mode 2) and test application behaviour before persisting.

  ```bash
  sudo sysctl -w vm.overcommit_memory=2
  sudo sysctl -w vm.overcommit_ratio=80
  ```

  **Risk:** Switching to mode 2 (strict overcommit) will cause allocations that exceed `CommitLimit` to fail immediately with `ENOMEM`. Applications that do not handle `malloc` returning NULL will crash instead of being OOM-killed; this is safer but may surface application bugs. **Duration:** Immediate; test application behaviour before making persistent. **Verification:** `sysctl vm.overcommit_memory` returns `2`; `Committed_AS` stays below `CommitLimit`.
- **remediation** (root): persist the stricter overcommit policy.

  ```bash
  echo 'vm.overcommit_memory=2' | sudo tee /etc/sysctl.d/99-overcommit.conf
  echo 'vm.overcommit_ratio=80' | sudo tee -a /etc/sysctl.d/99-overcommit.conf
  sudo sysctl -p /etc/sysctl.d/99-overcommit.conf
  ```

  **Verification:** `sysctl vm.overcommit_memory` returns `2`. `Committed_AS` stays below `CommitLimit` in `/proc/meminfo`. OOM kills cease and any process that would previously have been killed now receives `ENOMEM` at allocation time.

---

### Cause Z: Unidentified

**Statement:** The OOM kill cause cannot be determined from the available diagnostic output and does not match any of the above patterns.

**Chain:**
- root: the OOM kill arises from an unusual combination (slab growth, network buffer pressure, NUMA imbalance, HugePages misconfiguration, or a kernel bug) not matched above
- D: memory exhaustion triggers the OOM killer with no identified pattern ([Symptom])

**Indicators:**
- root: [Default] None of Causes A–F match the diagnostic output

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to kernel/platform engineering.

  ```bash
  # Full memory snapshot
  cat /proc/meminfo > /tmp/meminfo-snapshot.txt
  ps -eo pid,user,rss,vsz,comm --sort=-rss > /tmp/ps-snapshot.txt
  dmesg -T > /tmp/dmesg-snapshot.txt
  slabtop -o --sort=c >> /tmp/meminfo-snapshot.txt
  ```

  **Risk:** Low — the snapshot commands are read-only data collection steps. **Duration:** Collect snapshots for 30 minutes during a reproduction window, then escalate. **Verification:** N/A — escalation required. Escalate to kernel/platform engineering with the collected snapshots and `dmesg` output. Reference: Linux kernel OOM documentation at `https://www.kernel.org/doc/html/latest/admin-guide/mm/concepts.html`.

## Prevention

### Set systemd Memory Limits on All Services

Use `MemoryHigh` (soft throttle) and `MemoryMax` (hard cgroup limit) on every service unit. `MemoryHigh` triggers reclaim before `MemoryMax` triggers a kill, providing a warning layer:

```ini
# /etc/systemd/system/myapp.service.d/memory.conf
[Service]
MemoryHigh=1500M
MemoryMax=2G
OOMScoreAdjust=200
```

### Protect Critical System Processes

Prevent the OOM killer from selecting essential processes (sshd, init) by setting `OOMScoreAdjust=-1000` in their unit files:

```ini
# /etc/systemd/system/sshd.service.d/oom.conf
[Service]
OOMScoreAdjust=-1000
```

### Configure Swap Equal to 50–100% of RAM

Swap acts as a safety buffer for transient spikes. On servers with 8 GB+ RAM, a swap file equal to 50% of RAM is a reasonable baseline:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf
sudo sysctl -p /etc/sysctl.d/99-swap.conf
```

### Alert on Memory Pressure Before OOM

Alert at 80% used (warning) and 90% used (critical) with a sustained window to avoid false positives:

```yaml
- alert: HostMemoryPressureWarning
  expr: (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) > 0.80
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Host {{ $labels.instance }} memory usage above 80%"

- alert: HostMemoryPressureCritical
  expr: (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) > 0.90
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "Host {{ $labels.instance }} memory usage above 90% — OOM risk"
```

### Enable Memory Profiling in CI/CD

For garbage-collected runtimes (Java, Python, Node.js, Go), run a memory growth test in the CI pipeline. Fail the build if RSS grows more than 20% over a 10-minute soak test:

```bash
# Example soak test harness
start_rss=$(grep VmRSS /proc/$(pgrep -o myapp)/status | awk '{print $2}')
sleep 600
end_rss=$(grep VmRSS /proc/$(pgrep -o myapp)/status | awk '{print $2}')
growth=$(( (end_rss - start_rss) * 100 / start_rss ))
[ "$growth" -gt 20 ] && echo "FAIL: RSS grew ${growth}%" && exit 1
```

### Monitor Slab Cache Growth

Alert when slab consumes more than 15% of total RAM — an early signal of dentry/inode leaks:

```yaml
- alert: KernelSlabHighUsage
  expr: node_memory_Slab_bytes / node_memory_MemTotal_bytes > 0.15
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Kernel slab cache on {{ $labels.instance }} above 15% of RAM"
```

## Sources

- [Linux Kernel Documentation: Memory Management Concepts](https://www.kernel.org/doc/html/latest/admin-guide/mm/concepts.html) — Priority 1. OOM killer algorithm, overcommit modes, cgroup OOM handling, process selection heuristics.
- [Linux Kernel Documentation: vm sysctl parameters](https://www.kernel.org/doc/html/latest/admin-guide/sysctl/vm.html) — Priority 1. Authoritative reference for `overcommit_memory`, `overcommit_ratio`, `swappiness`, `vfs_cache_pressure`, and `oom_kill_allocating_task`.
- [Linux man pages: proc(5) — oom_score and oom_score_adj](https://man7.org/linux/man-pages/man5/proc.5.html) — Priority 1. OOM score calculation (badness heuristic), adjustment range -1000 to +1000, and interaction with cgroups.
- [systemd.resource-control(5)](https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html) — Priority 1. `MemoryMax`, `MemoryHigh`, `OOMScoreAdjust` directives for cgroup-based memory limits on systemd services.
- [Brendan Gregg — Linux Performance](https://www.brendangregg.com/linuxperf.html) — Priority 2. Memory analysis tools reference: `free`, `vmstat`, `sar`, `slabtop`, `/proc/meminfo` field interpretation, USE method for memory utilisation/saturation analysis.
