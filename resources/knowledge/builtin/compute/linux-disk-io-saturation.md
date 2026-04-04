---
id: linux-disk-io-saturation
title: "Linux Disk I/O Saturation"
domain: compute
service: linux
symptom_class:
  - latency
  - disk_full
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - linux
  - disk
  - io
  - iostat
  - iotop
  - blktrace
  - saturation
  - latency
  - scheduler
difficulty: advanced
---

# Linux Disk I/O Saturation

## Problem Definition

This runbook applies to Linux systems (kernel 4.18+) with any storage backend: local HDD/SSD/NVMe, cloud block storage (AWS EBS, GCP Persistent Disk, Azure Managed Disks), or SAN/NAS. You need root or sudo access and the `sysstat` package (`iostat`, `pidstat`, `sar`), `iotop`, and optionally `blktrace` for advanced block-level tracing. For cloud instances, you also need cloud CLI access to check storage configuration and modify IOPS/throughput settings.

The system's disk I/O subsystem is saturated, meaning the storage device cannot keep up with the volume of read and write requests. Applications experience high latency on file operations, processes enter uninterruptible sleep (D state) waiting for I/O, and overall system responsiveness degrades. Database queries slow down, log writes stall, and batch processing jobs take far longer than expected.

Disk I/O saturation occurs when the rate of I/O requests exceeds the device's throughput or IOPS capacity. Every storage device has finite limits: a SATA HDD provides approximately 100-200 IOPS, an SSD provides 10,000-100,000+ IOPS, and a cloud EBS gp3 volume provides a baseline of 3,000 IOPS. The most frequent causes are: database write-heavy workloads (WAL logging, checkpointing), log flooding from debug-level logging in production, backup or snapshot operations generating sustained I/O, swap thrashing converting a memory problem into an I/O problem, RAID rebuilds, noisy neighbors in cloud environments consuming shared storage bandwidth, and undersized storage provisioning relative to the workload's IOPS requirements.

**Typical error presentation:**

```text
$ iostat -xz 1
Device  r/s    w/s  rkB/s  wkB/s  rrqm/s  wrqm/s  %util  await  r_await  w_await
sda     50.0  200.0  400.0  3200.0  0.0    45.0    99.8   85.2   12.5     103.4
```

`%util` at 99-100% indicates the device is fully saturated. High `await` confirms requests are queuing. Processes in `D` (uninterruptible sleep) state in `top` indicate I/O-blocked tasks.

## Diagnostic Steps

### Step 1: Confirm I/O Saturation with iostat

**What this checks:** Whether the storage device is saturated and which I/O dimension (IOPS, throughput, latency) is the bottleneck.

```bash
iostat -xz 1 5
```

**Expected output:** Per-device statistics over 5 one-second intervals.

**What the finding means:** Key saturation indicators: `%util` above 90% means the device is busy almost continuously. `await` above 20ms for SSD or 50ms for HDD means requests are queuing. `avgqu-sz` (average queue size) above 1 confirms queuing. Compare `r/s + w/s` against the device's rated IOPS and `rkB/s + wkB/s` against its rated throughput to identify which limit is hit. If `r_await` is much higher than `w_await`, reads are the bottleneck (and vice versa).

### Step 2: Identify Which Processes Are Generating I/O

**What this checks:** Which specific processes are responsible for the I/O load.

```bash
iotop -oPa
```

**Expected output:** Per-process I/O sorted by disk usage. `-o` shows only processes doing I/O, `-P` shows processes (not threads), `-a` accumulates I/O since start.

**What the finding means:** The `DISK READ` and `DISK WRITE` columns show bytes per second. The `IO>` column shows percentage of time in I/O wait. The top processes in this list are causing the saturation. If `iotop` is not available, use `/proc/<pid>/io`:

```bash
for pid in $(ls /proc/ | grep -E '^[0-9]+$'); do
  io=$(cat /proc/$pid/io 2>/dev/null) || continue
  wchar=$(echo "$io" | grep write_bytes | awk '{print $2}')
  name=$(cat /proc/$pid/comm 2>/dev/null)
  [ "$wchar" -gt 1000000 ] 2>/dev/null && echo "PID=$pid Name=$name WriteBytes=$wchar"
done | sort -t= -k3 -rn | head -20
```

### Step 3: Check for Swap Thrashing

**What this checks:** Whether the I/O load is actually a memory problem manifesting as disk I/O.

```bash
vmstat 1 5
```

**Expected output:** System statistics including `si` (swap in) and `so` (swap out) columns.

**What the finding means:** Non-zero `si` and `so` values, especially sustained above 1000 KB/s, indicate the system is actively swapping. This converts a memory problem into an I/O bottleneck. Address memory pressure first (see linux-oom-killer runbook).

```bash
swapon --show
free -h
```

### Step 4: Check I/O Wait at CPU Level

**What this checks:** How much CPU time is spent waiting for I/O, confirming I/O is the bottleneck rather than CPU.

```bash
mpstat 1 5
```

**Expected output:** Per-CPU statistics including `%iowait`.

**What the finding means:** `%iowait` above 20% combined with low `%usr` and `%sys` confirms I/O is the bottleneck, not CPU. If `%usr` or `%sys` are also high, the system has both CPU and I/O contention.

### Step 5: Examine I/O Patterns with blktrace (Advanced)

**What this checks:** Block-level I/O patterns including request size, sequentiality, and merge behavior.

```bash
blktrace -d /dev/sda -o /tmp/blktrace -w 10
blkparse -i /tmp/blktrace.sda.blktrace.0 | head -100
btt -i /tmp/blktrace.sda.blktrace.0
```

**Expected output:** Detailed I/O trace showing every request at the block device level.

**What the finding means:** `btt` output shows queue depth, completion latency, and I/O size distribution. Large numbers of small random I/Os saturate IOPS; large sequential I/Os saturate throughput. This distinction determines whether you need more IOPS or more bandwidth.

### Step 6: Check the I/O Scheduler

**What this checks:** Whether the current I/O scheduler is appropriate for the storage device and workload.

```bash
cat /sys/block/sda/queue/scheduler
```

**Expected output:** Available schedulers with the active one in brackets, e.g., `[mq-deadline] none kyber bfq`.

**What the finding means:** `none`/`noop` is best for SSDs and NVMe (device handles optimization). `mq-deadline` is good for databases on SSD (deadline-based with read priority). `bfq` is best for interactive workloads on HDD. `kyber` is a low-overhead scheduler for fast SSDs with latency targets. Using `cfq` on SSD or `none` on HDD with mixed workloads can reduce throughput.

### Step 7: Check Storage Device Limits

**What this checks:** Whether the observed I/O rate has reached the hardware or provisioned limits of the storage device.

```bash
# Check device type (rotational = HDD, non-rotational = SSD)
cat /sys/block/sda/queue/rotational

# For NVMe
nvme id-ctrl /dev/nvme0 -H 2>/dev/null | grep -i "max\|throughput"

# List block devices
lsblk -o NAME,SIZE,TYPE,ROTA,MOUNTPOINT
```

**Expected output:** Device characteristics and capacity.

**What the finding means:** Compare the observed IOPS (`r/s + w/s` from iostat) against the device's rated capacity. For AWS EBS: gp3 provides 3,000 baseline IOPS (scalable to 16,000); io2 provides up to 64,000 IOPS. If you have reached the provisioned limit, increase IOPS or upgrade the volume type.

### Step 8: Check for Filesystem Issues

**What this checks:** Whether filesystem fragmentation or mount options are contributing to excessive I/O.

```bash
# Check filesystem type and mount options
df -hT
mount | grep "sda\|nvme"

# Check ext4 fragmentation
e4defrag -c /dev/sda1 2>/dev/null
```

**Expected output:** Filesystem types, mount options, and fragmentation statistics.

**What the finding means:** Missing `noatime` mount option causes metadata writes on every read. Heavy fragmentation on HDD causes more random I/O. Journaling overhead on ext4/XFS adds write amplification under heavy create/delete workloads.

## Mitigation

### Option 1: Identify and Throttle the I/O-Heavy Process

- **Risk:** Low-Medium. Throttling reduces the performance of the target process but relieves contention for other processes.
- **Command:**

  ```bash
  # Reduce I/O priority (class 3 = idle, only uses I/O when no other process needs it)
  ionice -c 3 -p <PID>

  # For systemd services: set I/O weight (lower = less priority)
  sudo systemctl set-property my-service.service IOWeight=50
  ```

- **Verify:**

  ```bash
  iostat -xz 1 3
  ```

  `%util` should decrease and `await` should improve for other processes.
- **Duration:** Immediate. Lasts until the process restarts or the setting is changed.

### Option 2: Stop Non-Critical I/O Operations

- **Risk:** Low. Delaying backups, log archival, or batch jobs temporarily reduces I/O load. These can be rescheduled.
- **Command:**

  ```bash
  # Pause a running backup
  kill -STOP <backup-PID>
  # Resume later: kill -CONT <backup-PID>

  # Stop logrotate if running
  pkill -f logrotate
  ```

- **Verify:**

  ```bash
  iostat -xz 1 3
  iotop -oPa
  ```

  I/O utilization should drop after stopping the offending operations.
- **Duration:** Immediate. Resume operations during off-peak hours.

### Option 3: Increase I/O Capacity (Cloud)

- **Risk:** Medium. Increases cost. Volume modification may take time to apply (EBS modifications can take hours for large volumes).
- **Command:**

  ```bash
  # AWS: Increase EBS volume IOPS (gp3 allows independent IOPS scaling)
  aws ec2 modify-volume --volume-id vol-0123456789abcdef0 \
    --iops 6000 --throughput 250

  # Verify modification status
  aws ec2 describe-volumes-modifications --volume-ids vol-0123456789abcdef0
  ```

- **Verify:**

  ```bash
  iostat -xz 1 5
  ```

  `%util` and `await` should decrease as the additional IOPS capacity absorbs the workload.
- **Duration:** EBS IOPS changes take effect within minutes but full optimization may take hours.

### Option 4: Switch I/O Scheduler

- **Risk:** Low. Can be done on a live system without unmounting. Effect is immediate.
- **Command:**

  ```bash
  # For SSDs: use none (noop) or mq-deadline
  echo "none" > /sys/block/sda/queue/scheduler

  # For HDDs with database workloads: use mq-deadline
  echo "mq-deadline" > /sys/block/sda/queue/scheduler
  ```

- **Verify:**

  ```bash
  cat /sys/block/sda/queue/scheduler
  iostat -xz 1 5
  ```

  The selected scheduler should show in brackets. Monitor `await` for improvement.
- **Duration:** Immediate. Make permanent via udev rules or kernel boot parameters.

## Root Cause Resolution

**If** Step 2 shows a database process (postgres, mysqld) as the top I/O consumer **then** optimize database I/O settings:

```bash
# PostgreSQL: spread checkpoint I/O and increase parallel reads
sudo -u postgres psql -c "SHOW checkpoint_completion_target;"  -- should be 0.9
sudo -u postgres psql -c "SHOW wal_buffers;"                   -- increase to 64MB
sudo -u postgres psql -c "SHOW effective_io_concurrency;"      -- set to 200 for SSD

# MySQL/InnoDB: match I/O capacity to device
mysql -e "SHOW VARIABLES LIKE 'innodb_io_capacity%';"  -- set to match device IOPS
mysql -e "SHOW VARIABLES LIKE 'innodb_flush_method';"  -- use O_DIRECT for SSD
```

**If** Step 2 shows a logging process or application writing heavily to `/var/log` **then** reduce log verbosity and rate-limit journal output:

```bash
# Identify which log files are being written
lsof +D /var/log | sort -k7 -rn | head -10

# Rate-limit journald
sudo mkdir -p /etc/systemd/journald.conf.d/
cat <<'EOF' | sudo tee /etc/systemd/journald.conf.d/rate-limit.conf
[Journal]
RateLimitIntervalSec=30s
RateLimitBurst=1000
EOF
sudo systemctl restart systemd-journald
```

**If** Step 3 shows active swapping (`si`/`so` > 0) **then** the root cause is memory pressure, not disk capacity. Reduce swappiness and address the memory issue:

```bash
sudo sysctl vm.swappiness=10
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.d/99-swap.conf
```

**If** Step 7 shows the device IOPS or throughput limit has been reached **then** upgrade the storage:

```bash
# AWS: Migrate from gp2 to gp3 with higher IOPS
aws ec2 modify-volume --volume-id vol-0123456789abcdef0 \
  --volume-type gp3 --iops 6000 --throughput 250
```

For local storage, replace HDD with SSD or add NVMe drives. For RAID arrays, add more spindles to increase aggregate IOPS.

**If** Step 6 shows a suboptimal scheduler **then** configure the correct scheduler permanently via udev:

```bash
cat <<'EOF' | sudo tee /etc/udev/rules.d/60-io-scheduler.rules
# SSDs and NVMe: use none
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{queue/rotational}=="0", ATTR{queue/scheduler}="none"
ACTION=="add|change", KERNEL=="nvme[0-9]*", ATTR{queue/scheduler}="none"
# HDDs: use mq-deadline
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{queue/rotational}=="1", ATTR{queue/scheduler}="mq-deadline"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

**If** iostat shows high `r/s` and `r_await` but low `w/s` **then** the workload is read-bound. Increase the read-ahead buffer for sequential workloads:

```bash
# Check current read-ahead (in 512-byte sectors)
blockdev --getra /dev/sda
# Increase to 2048 sectors (1 MB) for sequential reads
blockdev --setra 2048 /dev/sda
```

## Verification

After applying fixes, confirm I/O saturation is resolved:

```bash
# Check I/O utilization
iostat -xz 1 10
```

`%util` should be below 80% during normal operations. `await` should be under 5ms for SSD or under 20ms for HDD.

```bash
# Check I/O wait at CPU level
mpstat 1 5
```

`%iowait` should be below 10% during normal operations.

```bash
# Check for processes in D (uninterruptible sleep) state
ps aux | awk '$8 ~ /D/ {print $0}'
```

No processes should be persistently stuck in D state during normal operations.

```bash
# Collect I/O statistics for 24-hour trending
sar -d 60 1440 > /tmp/io-trend.log &
```

Review after 24 hours. `%util` peaks should remain below 80% and `await` should not show an upward trend.

## Prevention

### Right-Size Storage for the Workload

Provision storage based on measured IOPS and throughput requirements, not just capacity. For AWS EBS: gp3 provides 3,000 baseline IOPS (scalable to 16,000) and 125 MB/s baseline throughput (scalable to 1,000 MB/s). Use io2 for sustained high IOPS (up to 64,000 per volume). Use EBS-optimized instances for dedicated storage bandwidth.

### Separate Workloads onto Different Volumes

Place database data, WAL/logs, application logs, and temporary files on separate volumes to prevent one workload from saturating the I/O path for another:

```bash
# Example mount layout
/dev/nvme1n1  /var/lib/postgresql/data   # Database data
/dev/nvme2n1  /var/lib/postgresql/pg_wal  # WAL (high write IOPS)
/dev/nvme3n1  /var/log                    # Application logs
```

### Monitor I/O Metrics Continuously

Set up Prometheus node_exporter alerts for I/O saturation:

```yaml
- alert: DiskIOSaturation
  expr: rate(node_disk_io_time_seconds_total[5m]) > 0.9
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Disk {{ $labels.device }} on {{ $labels.instance }} is saturated (>90% utilized)"
```

### Schedule Heavy I/O During Off-Peak Hours

Run backups, database maintenance (VACUUM, OPTIMIZE), and batch processing during low-traffic periods. Use `ionice` with class 3 (idle) for background operations:

```bash
ionice -c 3 nice -n 19 /usr/local/bin/backup.sh
```

### Use Appropriate Filesystem Mount Options

Mount with `noatime,nodiratime` to reduce metadata writes. Use XFS for large files and parallel I/O on HDD. Use tmpfs for `/tmp` if the workload generates many small temporary files:

```bash
# Example fstab entry with noatime
/dev/sda1  /  ext4  defaults,noatime  0 1
```

## Sources

- [Brendan Gregg - Linux Performance](https://www.brendangregg.com/linuxperf.html) - Comprehensive Linux performance reference covering iostat, iotop, blktrace, and I/O analysis methodology.
- [Brendan Gregg - USE Method](https://www.brendangregg.com/usemethod.html) - Utilization-Saturation-Errors methodology applied to storage devices, including specific Linux commands for each metric.
- [Brendan Gregg - Poor Disk Performance](https://www.brendangregg.com/blog/2021-05-09/poor-disk-performance.html) - Practical disk performance diagnosis using iostat and blktrace.
- [Linux man pages: iostat(1)](https://man7.org/linux/man-pages/man1/iostat.1.html) - Authoritative reference for CPU and device I/O statistics reporting.
- [Linux man pages: iotop(8)](https://man7.org/linux/man-pages/man8/iotop.8.html) - Per-process I/O monitoring tool reference.
- [Linux man pages: blktrace(8)](https://man7.org/linux/man-pages/man8/blktrace.8.html) - Block-level I/O tracing tool reference.
- [Linux Kernel Documentation: Block Layer](https://www.kernel.org/doc/html/latest/block/index.html) - I/O scheduler documentation including mq-deadline, bfq, and kyber.
