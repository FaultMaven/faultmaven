---
id: "linux-disk-io-saturation"
title: "Linux Disk I/O Saturation"
domain: compute
service: linux
symptom_class: [latency, disk_full]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [iostat, iotop, blktrace, io-scheduler, iowait, nvme, ebs, sysstat]
difficulty: advanced
---

## Symptom Recognition

- `iostat -xz 1` shows `%util` at or above 90% on one or more block devices
- `await` above 20 ms on SSD or above 50 ms on HDD, with `avgqu-sz` greater than 1
- `mpstat` shows `%iowait` sustained above 20%
- `top` or `ps aux` shows multiple processes in `D` (uninterruptible sleep) state
- Application-level timeouts on database queries, log writes, or file operations with no corresponding CPU spike
- `vmstat` shows non-zero `si`/`so` columns indicating active swap I/O
- Cloud provider metrics: AWS EBS `VolumeQueueLength` above 1 or `BurstBalance` approaching 0 on gp2 volumes

## Applicability

- Linux kernel 4.18+ with any storage backend: local HDD, SSD, NVMe, AWS EBS, GCP Persistent Disk, Azure Managed Disks, SAN/NAS
- Requires root or sudo access
- Tools required: `sysstat` package (`iostat`, `pidstat`, `sar`, `mpstat`), `iotop`, optionally `blktrace`/`blkparse`/`btt` for advanced tracing
- For cloud volumes, requires cloud CLI access (`aws`, `gcloud`, or `az`) to inspect provisioned limits and modify IOPS/throughput settings

## Diagnostic Steps

### Step 1: Confirm I/O saturation and identify the bottleneck dimension

```bash
iostat -xz 1 5
```

Expected output: per-device statistics over 5 one-second intervals. Key columns: `%util` (device busy time percentage), `await` (average request latency in ms), `avgqu-sz` (average queue depth), `r/s` and `w/s` (IOPS), `rkB/s` and `wkB/s` (throughput), `r_await` and `w_await` (split read/write latency).

### Step 2: Identify which processes are generating I/O

```bash
iotop -oPa
```

Expected output: processes sorted by disk I/O, showing `DISK READ` and `DISK WRITE` bytes per second and `IO>` percentage. The `-o` flag shows only active I/O processes, `-P` shows processes not threads, `-a` accumulates totals.

### Step 3: Check for swap thrashing

```bash
vmstat 1 5
```

Expected output: columns include `si` (pages swapped in per second) and `so` (pages swapped out per second). Non-zero sustained values above 100 KB/s indicate the system is actively swapping, converting a memory problem into an I/O bottleneck.

### Step 4: Measure CPU-level I/O wait

```bash
mpstat 1 5
```

Expected output: per-CPU statistics including `%iowait`. A value above 20% combined with low `%usr` and `%sys` confirms I/O is the primary bottleneck.

### Step 5: Check the active I/O scheduler

```bash
cat /sys/block/sda/queue/scheduler
```

Expected output: available schedulers with the active one in brackets, e.g. `[mq-deadline] none kyber bfq`. For NVMe devices use `/sys/block/nvme0n1/queue/scheduler`.

### Step 6: Check device type and IOPS/throughput limits

```bash
cat /sys/block/sda/queue/rotational
lsblk -o NAME,SIZE,TYPE,ROTA,MOUNTPOINT
```

Expected output: `rotational` returns `1` for HDD, `0` for SSD/NVMe. Compare observed `r/s + w/s` from Step 1 against device-rated IOPS (gp3 EBS: 3,000 baseline; io2: up to 64,000; SATA SSD: 10,000–100,000; NVMe: 100,000–1,000,000+).

### Step 7: Identify the top I/O-generating files or directories

```bash
lsof +D /var/log | sort -k7 -rn | head -10
```

Expected output: file descriptors sorted by file offset (proxy for write volume), identifying which log paths or data directories are being written most actively.

### Step 8: Check filesystem mount options and fragmentation

```bash
df -hT
mount | grep -E 'sda|nvme|xvd'
e4defrag -c /dev/sda1 2>/dev/null
```

Expected output: filesystem type and mount options. Missing `noatime` causes a metadata write on every read. Fragmentation score above 1% on HDD adds random I/O overhead.

## Causes

### Cause A: Database write amplification saturating write IOPS

**Statement:** A database process (PostgreSQL, MySQL, MongoDB) is issuing continuous high-volume writes through WAL/redo logging, checkpointing, or compaction, saturating write IOPS capacity.

**Mechanism:** Database engines write all changes to a write-ahead log before applying them to data files, and additionally trigger periodic checkpoint I/O to flush dirty pages. When the database is misconfigured (e.g., low `checkpoint_completion_target`, high `innodb_io_capacity`), these I/O bursts are compressed into short windows that exceed the device's IOPS ceiling. The result is queue depth above 1 and `w_await` climbing while reads from application queries queue behind.

**Indicator:**

- [Step 2] `iotop` shows `postgres`, `mysqld`, or `mongod` at the top of the I/O list with sustained `DISK WRITE` above device IOPS capacity
- [Step 1] `w_await` is significantly higher than `r_await` and `%util` is above 90%

<!-- match: {"step": 2, "predicate": "contains", "target": "postgres"} -->
<!-- match: {"step": 1, "predicate": "threshold", "target": "%util", "op": ">", "value": 90} -->

**Mitigation:**

- **Risk:** Spreading checkpoint I/O over a longer window may increase WAL volume on disk temporarily; `innodb_io_capacity` reduction slows InnoDB background flush which can increase dirty page count.
- **Command:**

  ```bash
  # PostgreSQL: spread checkpoint I/O over 90% of checkpoint interval
  sudo -u postgres psql -c "ALTER SYSTEM SET checkpoint_completion_target = '0.9';"
  sudo -u postgres psql -c "SELECT pg_reload_conf();"

  # MySQL: reduce InnoDB background flush rate to match device IOPS
  mysql -e "SET GLOBAL innodb_io_capacity = 500;"
  mysql -e "SET GLOBAL innodb_io_capacity_max = 1000;"
  ```

- **Duration:** Effective immediately; tune further based on `iostat` observations over 30 minutes.

**Resolution:**

```bash
# PostgreSQL: persist in postgresql.conf
echo "checkpoint_completion_target = 0.9" | sudo tee -a /etc/postgresql/*/main/postgresql.conf
echo "wal_buffers = 64MB" | sudo tee -a /etc/postgresql/*/main/postgresql.conf
echo "effective_io_concurrency = 200" | sudo tee -a /etc/postgresql/*/main/postgresql.conf
sudo systemctl reload postgresql

# MySQL: persist in /etc/mysql/mysql.conf.d/mysqld.cnf
# innodb_io_capacity = 500
# innodb_flush_method = O_DIRECT
```

- **Impact:** Cluster-wide parameter change; affects all connections. PostgreSQL reload is online. MySQL variables are dynamic except `innodb_flush_method` which requires a restart.
- **Rollback:** `ALTER SYSTEM RESET checkpoint_completion_target; SELECT pg_reload_conf();` or restore the previous `my.cnf` value.

**Verification:** After change, `iostat -xz 1 60` should show `w_await` declining and `%util` dropping below 80% during normal write load.

### Cause B: Log flooding from debug-level logging in production

**Statement:** An application or system service is writing debug or trace logs at a rate that exceeds the filesystem's sustainable write throughput.

**Mechanism:** Debug-level logging generates an order of magnitude more log volume than info-level, and log write calls are typically synchronous from the application thread. When log volume reaches megabytes per second, the device's write throughput ceiling is reached and all writer threads block on `write()` syscalls, appearing as D-state processes and driving `w_await` up.

**Indicator:**

- [Step 7] `lsof +D /var/log` identifies a specific log file receiving high-volume writes from an application process
- [Step 2] `iotop` shows the logging process near the top with `DISK WRITE` dominated by `/var/log` paths

<!-- match: {"step": 7, "predicate": "contains", "target": "/var/log"} -->

**Mitigation:**

- **Risk:** Low. Reducing log verbosity drops observability for the duration but does not affect application correctness.
- **Command:**

  ```bash
  # Rate-limit systemd-journald immediately
  sudo mkdir -p /etc/systemd/journald.conf.d/
  sudo tee /etc/systemd/journald.conf.d/rate-limit.conf <<'EOF'
  [Journal]
  RateLimitIntervalSec=30s
  RateLimitBurst=500
  EOF
  sudo systemctl restart systemd-journald
  ```

- **Duration:** Until logging configuration is permanently updated.

**Resolution:**

```bash
# Set application log level to INFO or WARN via its config file/environment variable
# Example for a Java app: LOG_LEVEL=INFO in /etc/app/env
# Persist journald rate-limit via the .conf file created in Mitigation
```

**Verification:** `iostat -xz 1 10` shows `wkB/s` drop and `w_await` return below 5 ms.

### Cause C: Backup or snapshot job generating sustained sequential I/O

**Statement:** A backup process (rsync, tar, mysqldump, pg_dump, cloud snapshot agent) is running during peak hours and consuming the device's full read or write throughput.

**Mechanism:** Backup tools read source data sequentially at maximum speed while simultaneously writing to a destination. On a single-device system, this generates competing read (source) and write (destination) streams that together saturate throughput. Even on separate volumes, backup jobs that compress data on the fly are CPU-intensive and read the source device at full speed, causing `r_await` to spike and read-dependent application queries to queue.

**Indicator:**

- [Step 2] `iotop` shows `rsync`, `tar`, `mysqldump`, `pg_dump`, or a cloud backup agent process at the top with high `DISK READ` or `DISK WRITE`
- [Step 1] `rkB/s` or `wkB/s` is near the device's rated throughput ceiling with `%util` above 90%

<!-- match: {"step": 2, "predicate": "contains", "target": "rsync"} -->
<!-- match: {"step": 1, "predicate": "threshold", "target": "%util", "op": ">", "value": 90} -->

**Mitigation:**

- **Risk:** Low. Suspending a backup delays it but does not lose data; resume when off-peak.
- **Command:**

  ```bash
  # Pause the backup process
  kill -STOP $(pgrep -f rsync)
  # To resume: kill -CONT $(pgrep -f rsync)

  # Or run with idle I/O priority from the start
  ionice -c 3 nice -n 19 rsync -av /source /dest
  ```

- **Duration:** Resume the backup during off-peak hours (e.g. 02:00–05:00).

**Resolution:**

```bash
# Schedule backups during off-peak with idle I/O class
# In crontab: 0 2 * * * ionice -c 3 nice -n 19 /usr/local/bin/backup.sh
```

**Verification:** After pausing, `iostat -xz 1 5` shows `%util` dropping and `await` returning to baseline.

### Cause D: Swap thrashing converting memory pressure into I/O saturation

**Statement:** The system is actively paging anonymous memory to and from swap, generating random I/O that saturates the storage device independently of application workload.

**Mechanism:** When resident memory exceeds physical RAM, the kernel's page reclaim algorithm writes anonymous pages to swap (swap out) and reads them back on access (swap in). Each swap I/O is a small random read or write to the swap partition or file. At high swap rates, the device queues these random requests alongside application I/O, and `await` climbs sharply. The root cause is memory pressure — the I/O saturation is a secondary symptom.

**Indicator:**

- [Step 3] `vmstat` shows `si` or `so` above 100 (pages/sec) sustained across multiple 1-second intervals
- [Step 1] `%util` is high but neither `r/s` nor `w/s` corresponds to a known application workload

<!-- match: {"step": 3, "predicate": "threshold", "target": "si", "op": ">", "value": 100} -->

**Mitigation:**

- **Risk:** Low. Reducing swappiness nudges the kernel to prefer page cache eviction over swap, but does not eliminate swap when memory is truly exhausted.
- **Command:**

  ```bash
  sudo sysctl vm.swappiness=10
  ```

- **Duration:** Until next reboot; persist with `echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.d/99-swap.conf`.

**Resolution:**

```bash
# Identify the memory-heavy process
ps aux --sort=-%mem | head -10
# Reduce its memory footprint, add RAM, or add OOM score tuning
# See linux-oom-killer runbook for full memory pressure resolution
```

**Verification:** `vmstat 1 10` shows `si` and `so` drop to 0; `iostat` shows `%util` returning to baseline.

### Cause E: I/O scheduler mismatch reducing device throughput

**Statement:** The active I/O scheduler is suboptimal for the storage device type, causing unnecessary serialization or reordering overhead that reduces effective throughput.

**Mechanism:** Linux multi-queue I/O schedulers are tuned for different device characteristics. Using `bfq` on a fast NVMe drive introduces per-request overhead that the device does not need; using `none` on an HDD with mixed workloads allows request starvation since the device cannot reorder internally. A scheduler mismatch reduces the number of I/O requests the device can service per second, making saturation appear at lower loads than the device's rated capacity.

**Indicator:**

- [Step 5] Active scheduler is not `none` or `mq-deadline` on SSD/NVMe, or not `mq-deadline` on HDD
- [Step 6] `rotational` is `0` (SSD/NVMe) but scheduler is `bfq` or `kyber`

<!-- match: {"step": 5, "predicate": "contains", "target": "bfq"} -->

**Mitigation:**

- **Risk:** Very low. Scheduler change is live and reversible; takes effect immediately with no unmount required.
- **Command:**

  ```bash
  # For SSD/NVMe: use none (device-managed queue)
  echo "none" | sudo tee /sys/block/sda/queue/scheduler

  # For HDD or mixed database workloads: use mq-deadline
  echo "mq-deadline" | sudo tee /sys/block/sda/queue/scheduler
  ```

- **Duration:** Until next reboot; persist via udev rules.

**Resolution:**

```bash
sudo tee /etc/udev/rules.d/60-io-scheduler.rules <<'EOF'
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{queue/rotational}=="0", ATTR{queue/scheduler}="none"
ACTION=="add|change", KERNEL=="nvme[0-9]*", ATTR{queue/scheduler}="none"
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{queue/rotational}=="1", ATTR{queue/scheduler}="mq-deadline"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

- **Impact:** Host-wide; applies to all matching block devices at next udev trigger or reboot. No application restart required.
- **Rollback:** Delete `/etc/udev/rules.d/60-io-scheduler.rules` and `sudo udevadm trigger`.

**Verification:** `cat /sys/block/sda/queue/scheduler` confirms new scheduler in brackets. `iostat -xz 1 30` shows `await` improvement and `%util` decrease under the same workload.

### Cause F: Cloud EBS volume hitting provisioned IOPS or throughput ceiling

**Statement:** An AWS EBS volume has reached its provisioned IOPS or throughput limit, causing all additional I/O requests to queue and latency to climb.

**Mechanism:** EBS gp3 volumes have a default 3,000 IOPS and 125 MB/s throughput ceiling independent of instance type. When application workload (database writes, log ingestion, backups) exceeds this ceiling, the EBS subsystem queues excess requests and `VolumeQueueLength` climbs in CloudWatch. From the OS perspective, `await` rises and `%util` reaches 100% even though the physical SSD backing the EBS volume is not saturated — the limit is the provisioned quota, not hardware capacity.

**Indicator:**

- [Step 6] `lsblk` shows device is `nvme` type (EBS NVMe); Step 1 `r/s + w/s` is near 3,000 and `%util` is at 100%
- [Step 1] `await` above 5 ms on what should be a fast NVMe device indicates throttling

<!-- match: {"step": 6, "predicate": "contains", "target": "nvme"} -->
<!-- match: {"step": 1, "predicate": "threshold", "target": "await", "op": ">", "value": 5} -->

**Mitigation:**

- **Risk:** Medium. EBS volume modification triggers an optimization period (can take hours for large volumes). Cost increases linearly with IOPS provisioned.
- **Command:**

  ```bash
  # Check current volume configuration
  aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 \
    --query 'Volumes[0].{Type:VolumeType,IOPS:Iops,Throughput:Throughput}'

  # Increase IOPS and throughput on gp3 (no downtime required)
  aws ec2 modify-volume \
    --volume-id vol-0123456789abcdef0 \
    --iops 6000 \
    --throughput 250
  ```

- **Duration:** Modification applies within minutes; CloudWatch `VolumeQueueLength` should drop within 5 minutes of the new limit taking effect.

**Resolution:**

```bash
# Monitor modification progress
aws ec2 describe-volumes-modifications \
  --volume-ids vol-0123456789abcdef0 \
  --query 'VolumesModifications[0].ModificationState'
# State transitions: modifying -> optimizing -> completed
```

- **Impact:** Per-volume change; no instance restart required. Monthly cost increases by approximately $0.008 per additional provisioned IOPS (gp3 pricing).
- **Rollback:** `aws ec2 modify-volume --volume-id vol-0123456789abcdef0 --iops 3000 --throughput 125`

**Verification:** `iostat -xz 1 10` shows `await` below 2 ms and `%util` below 80% at the same workload level.

### Cause G: RAID rebuild consuming full device throughput

**Statement:** A RAID array reconstruction or resync after a disk failure or new member addition is consuming the entire I/O bandwidth of the surviving drives.

**Mechanism:** RAID rebuild reads every block from all surviving members and writes parity or mirror data. On large HDD arrays, this generates sustained sequential I/O that can saturate each member drive's throughput for hours or days. The kernel's `md` driver exposes a `sync_speed` parameter that caps rebuild throughput; its default is often uncapped, leaving no headroom for application I/O.

**Indicator:**

- [Step 1] `iostat` shows high `%util` on multiple physical drives simultaneously with similar `rkB/s`/`wkB/s` patterns
- [Symptom] `cat /proc/mdstat` shows a rebuild in progress (e.g., `[====>................]  recovery = 22.3%`)

<!-- match: {"step": 1, "predicate": "contains", "target": "recovery"} -->

**Mitigation:**

- **Risk:** Low for rate limiting; data is not at risk. Reducing rebuild speed extends the window of reduced redundancy.
- **Command:**

  ```bash
  # Check RAID status
  cat /proc/mdstat

  # Cap rebuild speed to 50 MB/s to leave bandwidth for applications
  echo 51200 | sudo tee /proc/sys/dev/raid/speed_limit_max
  echo 10240 | sudo tee /proc/sys/dev/raid/speed_limit_min
  ```

- **Duration:** Until rebuild completes; restore limits after.

**Resolution:**

```bash
# Persist speed limits via sysctl
echo 'dev.raid.speed_limit_max = 51200' | sudo tee -a /etc/sysctl.d/99-raid.conf
echo 'dev.raid.speed_limit_min = 10240' | sudo tee -a /etc/sysctl.d/99-raid.conf
sudo sysctl --system
```

**Verification:** `cat /proc/mdstat` shows rebuild progressing; `iostat -xz 1 5` shows `%util` below 80% on member devices during rebuild.

### Cause Z: Unidentified I/O source

**Statement:** The source of disk I/O saturation cannot be attributed to any specific process or configuration cause using standard diagnostic tools.

**Mechanism:** Some I/O saturation causes require kernel-level tracing to isolate — kernel drivers, DMA operations, or heavily multithreaded workloads can generate I/O that does not appear cleanly attributed in `iotop`. Advanced block-level tracing with `blktrace`/`btt` and eBPF tools (`biolatency`, `biosnoop` from BCC/bpftrace) are required to trace individual I/O requests to their originating kernel call stack.

**Indicator:**

- [Default] None of Causes A–G match; `iotop` shows no dominant process yet `%util` remains above 90%

**Mitigation:**

- **Risk:** Tracing tools are read-only and safe to run on production systems; `blktrace` adds minor overhead (less than 1% CPU on modern kernels).
- **Command:**

  ```bash
  # Block-level tracing for 10 seconds
  sudo blktrace -d /dev/sda -o /tmp/trace -w 10
  sudo blkparse -i /tmp/trace.sda.blktrace.0 | tail -50
  sudo btt -i /tmp/trace.sda.blktrace.0

  # eBPF per-process I/O latency histogram (requires bcc-tools)
  sudo biolatency -d sda 10
  sudo biosnoop | head -50
  ```

- **Duration:** Collect traces, then analyze offline.

**Resolution:** Out of runbook scope. Escalate to storage or kernel engineering with `btt` output, `biosnoop` trace, and `iostat` capture attached.

**Verification:** Resolution not deterministic from this runbook. Confirm with storage team after root cause identification.

## Prevention

Set up Prometheus `node_exporter` alerts for I/O saturation before it causes user impact:

```yaml
groups:
  - name: disk_io
    rules:
      - alert: DiskIOSaturation
        expr: rate(node_disk_io_time_seconds_total[5m]) > 0.9
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Disk {{ $labels.device }} on {{ $labels.instance }} is >90% utilized"
      - alert: DiskIOHighAwait
        expr: rate(node_disk_read_time_seconds_total[5m]) / rate(node_disk_reads_completed_total[5m]) > 0.02
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Disk read latency on {{ $labels.device }} above 20ms"
```

Separate database data, WAL/redo logs, application logs, and temp files onto distinct volumes to prevent one workload from crowding out another:

```bash
# Example mount layout for PostgreSQL host
# /dev/nvme1n1  ->  /var/lib/postgresql/data   (database data, gp3 6000 IOPS)
# /dev/nvme2n1  ->  /var/lib/postgresql/pg_wal  (WAL writes, gp3 3000 IOPS dedicated)
# /dev/nvme3n1  ->  /var/log                    (application logs)
```

Mount filesystems with `noatime,nodiratime` to eliminate metadata writes on every read:

```bash
# Example fstab entry
/dev/sda1  /  ext4  defaults,noatime,nodiratime  0 1
```

Schedule backup and maintenance jobs with idle I/O priority using `ionice`:

```bash
# Crontab entry for nightly backup
0 2 * * * ionice -c 3 nice -n 19 /usr/local/bin/backup.sh >> /var/log/backup.log 2>&1
```

For AWS EBS: use `gp3` over `gp2` — `gp3` allows independent IOPS and throughput scaling without requiring a larger disk. Provision IOPS at 20% above the measured peak to absorb burst workloads.

## Sources

- [Brendan Gregg — Linux Performance](https://www.brendangregg.com/linuxperf.html) — Comprehensive Linux performance reference covering iostat, iotop, blktrace, and I/O analysis tools; priority 1.
- [Brendan Gregg — USE Method](https://www.brendangregg.com/usemethod.html) — Utilization-Saturation-Errors methodology applied to storage devices with specific Linux commands for each metric; priority 1.
- [Linux man page: iostat(1)](https://man7.org/linux/man-pages/man1/iostat.1.html) — Authoritative reference for all iostat fields including `%util`, `await`, `avgqu-sz`, `r_await`, `w_await`; priority 1.
- [Linux man page: iotop(8)](https://man7.org/linux/man-pages/man8/iotop.8.html) — Per-process I/O monitoring tool reference; priority 1.
- [Linux man page: blktrace(8)](https://man7.org/linux/man-pages/man8/blktrace.8.html) — Block-level I/O tracing reference; priority 1.
- [Linux Kernel Docs: Block Layer Schedulers](https://www.kernel.org/doc/html/latest/block/index.html) — I/O scheduler documentation for mq-deadline, bfq, kyber, and none; priority 1.
