---
id: "linux-disk-full"
title: "Linux Disk Full"
domain: compute
service: linux
symptom_class: [disk_full]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [disk-space, inode, ext4, xfs, lsof, du, df, no-space-left, enospc, logrotate]
difficulty: intermediate
---

## Symptom Recognition

- `No space left on device` (errno ENOSPC) returned on any write operation
- Services fail to start or crash with write errors referencing ENOSPC
- Log rotation silently fails; logs stop rotating and grow unbounded
- Databases refuse writes: PostgreSQL logs `could not write to file`, MySQL logs `Errcode: 28`
- `df -h` shows `Use%` at 100% on one or more mount points
- `df -i` shows `IUse%` at 100% with block space still available (inode exhaustion variant)
- Kernel messages in `dmesg`: `EXT4-fs warning: ... has reached maxsize` or `XFS: ... No space left on device`
- Prometheus alert: `node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.05`
- Application health checks fail due to inability to write temp files or PID files

## Applicability

Applies to any Linux host (kernel 3.x+) running ext4, XFS, or tmpfs filesystems. Covers bare-metal servers, virtual machines, and container hosts. Requires root or sudo access for all diagnostic and remediation commands. Tools required: `df`, `du`, `lsof`, `find`, `tune2fs` (ext4), `xfs_info` (XFS), `journalctl`. Distribution-agnostic unless noted.

## Diagnostic Steps

### Step 1: Identify the Full Filesystem and Exhaustion Type

```bash
df -h && echo "---" && df -i
```

Expected output: `Use%` at 100% for block space or `IUse%` at 100% for inodes on one or more mount points. Note the mount point and device. If inode usage is 100% but block usage is not, skip to Step 4. If both are high, address block space first.

### Step 2: Locate Large Files and Directories

```bash
du -xh --max-depth=1 / 2>/dev/null | sort -rh | head -20
```

Expected output: Sorted list of top-level directories by size. Common offenders: `/var/log`, `/var/lib/docker`, `/var/lib/mysql`, `/tmp`, `/home`. Repeat drilling into the largest directory (e.g., `du -xh --max-depth=1 /var/log 2>/dev/null | sort -rh | head -20`).

```bash
find / -xdev -type f -size +100M -exec ls -lh {} \; 2>/dev/null | sort -k5 -rh | head -20
```

Expected output: Individual files larger than 100 MB on the affected filesystem. Large log files, core dumps, heap dumps, and database WAL segments are frequent offenders.

### Step 3: Check for Deleted-but-Open Files

```bash
lsof +L1 2>/dev/null | awk '{print $1, $2, $7, $9}' | sort -k3 -rn | head -20
```

Expected output: Columns show process name, PID, file size in bytes, and path marked `(deleted)`. Large values in the size column (hundreds of MB or more) indicate the kernel cannot reclaim those blocks until the holding process closes or restarts.

### Step 4: Count Files Per Directory for Inode Exhaustion

```bash
for d in /tmp /var/spool /var/cache /var/lib /var/log; do
  echo "$(find "$d" -xdev -type f 2>/dev/null | wc -l) $d"
done | sort -rn
```

Expected output: File count per directory, sorted descending. Directories with hundreds of thousands or millions of files are consuming inodes. Common culprits: PHP session files (`/var/lib/php/sessions`), mail queue (`/var/spool/postfix`), container overlay layers.

### Step 5: Check Reserved Blocks (ext4) or Metadata Reservation (XFS)

```bash
tune2fs -l "$(df --output=source / | tail -1)" 2>/dev/null | grep -i "reserved block"
```

Expected output: `Reserved block count` showing how many blocks are held for root. Default is 5% of total blocks. Non-root processes receive ENOSPC when usage reaches ~95%.

```bash
xfs_info / 2>/dev/null | head -5
```

Expected output: XFS geometry including `agcount`, `agsize`, and internal log size. XFS does not have the same root-reservation model as ext4.

### Step 6: Identify Active Growth Source

```bash
find / -xdev -type f -mtime -1 -size +10M -exec ls -lh {} \; 2>/dev/null | sort -k5 -rh | head -20
```

Expected output: Files modified in the last 24 hours that are large. Correlate file path with owning service to find what is actively filling the disk.

```bash
journalctl --disk-usage
```

Expected output: Total disk usage of the systemd journal. Values above 500 MB indicate journald needs size limits configured.

## Causes

### Cause A: Unbounded Application Log Growth

**Statement:** An application writes to a log file without enforced size limits and logrotate is absent or misconfigured, allowing the log to fill the filesystem.

**Mechanism:** When no log rotation policy exists or the logrotate configuration lacks a `maxsize` directive, log files grow continuously with each request, error, or debug event. At 100% filesystem utilization all write syscalls return ENOSPC, causing cascading failures in any service writing to that partition.

**Indicator:**

- [Step 2] A single file under `/var/log/` or an application log directory dominates `du` output
- [Step 6] The same file appears in recently modified large files

<!-- match: {"step": 2, "predicate": "contains", "target": "/var/log/"} -->

**Mitigation:**

- **Risk:** Truncating a log while the process holds it open creates a sparse file starting from offset 0 on next write by some applications; most modern apps handle this correctly.
- **Command:**

  ```bash
  truncate -s 0 /var/log/large-application.log
  ```

- **Duration:** Immediate relief. Configure log rotation within 24 hours to prevent recurrence.

**Resolution:**

```bash
cat > /etc/logrotate.d/application << 'LOGROTATE'
/var/log/application.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    maxsize 500M
}
LOGROTATE

logrotate -d /etc/logrotate.d/application
```

**Verification:** Run `df -h` on the affected partition to confirm usage below 85%. Run `watch -n 60 'df -h / | tail -1'` for 30 minutes and verify usage is stable or declining.

---

### Cause B: Deleted-but-Open File Handles

**Statement:** Files deleted from the directory tree are still held open by running processes, preventing the kernel from reclaiming their disk blocks until the process closes or restarts.

**Mechanism:** When logrotate or a script deletes a log file that an application holds open, the kernel marks the inode for deletion but cannot free the data blocks until the file descriptor count reaches zero. `df` reports full utilization because the blocks are allocated; `du` shows lower usage because the directory entry is gone. The discrepancy between `df` and `du` output is the diagnostic fingerprint.

**Indicator:**

- [Step 3] `lsof +L1` shows one or more entries with `(deleted)` path and size column above 100 MB

<!-- match: {"step": 3, "predicate": "contains", "target": "(deleted)"} -->

**Mitigation:**

- **Risk:** Truncating via `/proc/PID/fd/FD` is zero-downtime. Service restart causes brief outage; coordinate with load balancers for stateful services.
- **Command:**

  ```bash
  # Identify PID and FD from lsof output columns 2 and 4
  lsof +L1 2>/dev/null | sort -k7 -rn | head -5

  # Method 1: truncate in-place (no restart needed)
  cat /dev/null > /proc/<PID>/fd/<FD>

  # Method 2: restart the process (releases all deleted handles)
  systemctl restart <service-name>
  ```

- **Duration:** Immediate relief. `/proc` truncation is safe indefinitely.

**Resolution:**

```bash
# Fix logrotate to signal the app to reopen log files after rotation
cat > /etc/logrotate.d/app << 'LOGROTATE'
/var/log/app/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 appuser appgroup
    sharedscripts
    postrotate
        kill -USR1 $(cat /var/run/app.pid) 2>/dev/null || true
    endscript
}
LOGROTATE
```

**Verification:** Re-run `lsof +L1 2>/dev/null | awk '$7 > 104857600 {print $1, $2, $7, $9}'`. Output should be empty. Confirm `df -h` shows reclaimed space.

---

### Cause C: Inode Exhaustion from Small File Accumulation

**Statement:** Millions of small files have consumed all inodes on the filesystem, preventing new file creation even though significant block space remains free.

**Mechanism:** Each file on ext4/XFS requires one inode. The inode table is sized at filesystem creation time (ext4: one inode per 16 KB by default). Applications that create many small files — PHP sessions, mail queue entries, package manager metadata — can exhaust inodes while leaving blocks largely empty. `df -h` shows available space but `df -i` shows `IUse%` at 100%; any attempt to create a file returns ENOSPC.

**Indicator:**

- [Step 1] `df -i` shows `IUse%` at 100% while `df -h` shows block space available
- [Step 4] One directory contains hundreds of thousands or millions of files

<!-- match: {"step": 1, "predicate": "contains", "target": "100%"} -->

**Mitigation:**

- **Risk:** Deleting session or spool files may drop active user sessions or queued messages. Validate the target directory before bulk deletion.
- **Command:**

  ```bash
  # PHP sessions older than 24 hours
  find /var/lib/php/sessions -type f -mmin +1440 -delete

  # Postfix deferred mail queue
  postsuper -d ALL deferred

  # Generic: remove files not accessed in 7 days from a temp directory
  find /var/spool/target -type f -atime +7 -delete
  ```

- **Duration:** Immediate relief after deletion completes.

**Resolution:**

```bash
# Configure PHP session cleanup via cron
echo '0 * * * * root find /var/lib/php/sessions -type f -mmin +1440 -delete' \
  > /etc/cron.d/php-session-cleanup

# For mail queue: fix the upstream relay and flush the queue
postfix check && postqueue -f
```

**Verification:** Run `df -i` and confirm `IUse%` has dropped below 90%. Run `df -h` to confirm block usage is also acceptable.

---

### Cause D: Docker or Container Runtime Storage Accumulation

**Statement:** Docker or another container runtime has accumulated stopped containers, unused images, anonymous volumes, and build cache that is not automatically garbage-collected.

**Mechanism:** Each `docker pull`, `docker build`, or stopped container leaves layers, writable container filesystems, and volumes on the host filesystem under `/var/lib/docker`. Without periodic pruning, these overlay layers accumulate indefinitely. On hosts running CI/CD pipelines or frequent image rebuilds, `/var/lib/docker` can grow to tens or hundreds of GB within weeks.

**Indicator:**

- [Step 2] `du` output shows `/var/lib/docker` as the dominant consumer

<!-- match: {"step": 2, "predicate": "contains", "target": "/var/lib/docker"} -->

**Mitigation:**

- **Risk:** `docker system prune -af --volumes` removes ALL unused images (including base images needed for future builds) and ALL anonymous volumes. Named volumes used by running services are not touched, but confirm no unnamed volumes contain data before running.
- **Command:**

  ```bash
  docker system prune -af --volumes
  ```

- **Duration:** Immediate relief. Base images will be re-pulled on next deploy.

**Resolution:**

```bash
# Configure container log limits in /etc/docker/daemon.json
cat > /etc/docker/daemon.json << 'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
EOF
systemctl restart docker

# Schedule weekly prune via systemd timer or cron
echo '0 3 * * 0 root docker system prune -af --volumes >> /var/log/docker-prune.log 2>&1' \
  > /etc/cron.d/docker-prune
```

**Verification:** Run `du -xh --max-depth=1 /var/lib/docker 2>/dev/null | sort -rh | head -10`. Confirm total is substantially reduced. Run `df -h` and verify the host filesystem is below 80% usage.

---

### Cause E: systemd Journal Consuming Excessive Space

**Statement:** The systemd journal has no size cap configured and has grown to consume a significant fraction of the filesystem, particularly on hosts with verbose logging or long retention.

**Mechanism:** `systemd-journald` by default limits storage to 10% of the filesystem or 4 GB, whichever is smaller. However, if `SystemMaxUse` or `RuntimeMaxUse` is not explicitly set and the filesystem is large, the journal can grow several GB before self-capping. On embedded or small-disk systems the defaults are insufficient. Each application writing to the journal (including kernel messages) contributes.

**Indicator:**

- [Step 6] `journalctl --disk-usage` reports a value above 1 GB

<!-- match: {"step": 6, "predicate": "threshold", "target": "journal_gb", "op": ">", "value": 1} -->

**Mitigation:**

- **Risk:** Vacuum operations permanently delete journal entries older than the cutoff. Ensure log retention requirements are met before setting aggressive limits.
- **Command:**

  ```bash
  journalctl --vacuum-time=2d
  ```

- **Duration:** Immediate relief. Set permanent limits within 1 hour.

**Resolution:**

```bash
sed -i 's/^#SystemMaxUse=.*/SystemMaxUse=500M/' /etc/systemd/journald.conf
sed -i 's/^#SystemKeepFree=.*/SystemKeepFree=1G/' /etc/systemd/journald.conf
# If the lines are absent rather than commented:
grep -q '^SystemMaxUse=' /etc/systemd/journald.conf \
  || echo 'SystemMaxUse=500M' >> /etc/systemd/journald.conf
grep -q '^SystemKeepFree=' /etc/systemd/journald.conf \
  || echo 'SystemKeepFree=1G' >> /etc/systemd/journald.conf
systemctl restart systemd-journald
```

**Verification:** Run `journalctl --disk-usage` and confirm reported size is below 500 MB. Run `df -h` to confirm filesystem usage is below threshold.

---

### Cause F: ext4 Reserved Block Percentage Causing False ENOSPC

**Statement:** Non-root processes receive ENOSPC when filesystem usage reaches approximately 95% because ext4 reserves 5% of blocks for root by default, even on data-only partitions.

**Mechanism:** ext4 reserves a percentage of data blocks exclusively for root-owned processes, intended to allow root to log in and remediate even when the filesystem is full. The default reservation is 5% of total blocks. Non-root application processes see `df -h` showing 95% usage and start failing writes with ENOSPC while root processes still succeed. This is expected behavior on root partitions but is counterproductive on dedicated data partitions.

**Indicator:**

- [Step 1] `df -h` shows `Use%` at 95–96% but application writes fail with ENOSPC
- [Step 5] `tune2fs -l` shows `Reserved block count` at 5% of total blocks

<!-- match: {"step": 5, "predicate": "contains", "target": "Reserved block count"} -->

**Mitigation:**

- **Risk:** Reducing reserved blocks on `/` or `/boot` removes the safety margin that allows root login during disk-full emergencies. Use only on data partitions (`/data`, `/var/lib`, `/mnt/data`).
- **Command:**

  ```bash
  # Identify device (replace /dev/sdX1 with actual device from df -h)
  tune2fs -m 1 /dev/sdX1
  ```

- **Duration:** Temporary. Restore to 2–5% after root cause is addressed.

**Resolution:** Same as Mitigation. Restore after root cause is resolved:

```bash
tune2fs -m 5 /dev/sdX1
```

**Verification:** Run `df -h` and confirm `Use%` is now below 100% with available space visible. Run `tune2fs -l /dev/sdX1 | grep -i "reserved block"` to confirm the new percentage.

---

### Cause Z: Unidentified Disk Exhaustion

**Statement:** [Default] The filesystem is full but the cause cannot be determined from the standard diagnostic steps.

**Mechanism:** Less common causes include NFS stale mounts reporting incorrect usage, LVM snapshot overflow consuming pool space, container runtime storage drivers (overlay2, devicemapper) with metadata bloat, or bind mounts obscuring actual consumers. `du` vs `df` discrepancy without deleted-but-open files may indicate a stale NFS mount or bind mount masking large consumers.

**Indicator:**

- [Default] None of Steps 1–6 identify a clear dominant consumer

**Mitigation:**

- **Risk:** Emergency cleanup of `/tmp` or package caches is low-risk on most systems. Verify no active workloads depend on temp files before deletion.
- **Command:**

  ```bash
  # Clear package manager caches
  apt-get clean 2>/dev/null || yum clean all 2>/dev/null || dnf clean all 2>/dev/null

  # Remove old journal logs
  journalctl --vacuum-time=2d

  # Clear old temp files
  find /tmp -type f -atime +7 -delete 2>/dev/null
  find /var/tmp -type f -atime +7 -delete 2>/dev/null
  ```

- **Duration:** Immediate partial relief. Escalate for deeper investigation.

**Resolution:** Out of runbook scope. Escalate to infrastructure team with output of `df -h`, `df -i`, `du -xh --max-depth=2 / 2>/dev/null | sort -rh | head -30`, and `lsof +L1 2>/dev/null | sort -k7 -rn | head -20`.

**Verification:** After emergency cleanup, run `df -h` to confirm usage decreased. If usage remains at 100% after all emergency cleanup commands, escalate immediately — a live process may be filling the disk faster than space can be reclaimed.

## Prevention

Configure disk usage and inode monitoring alerts at multiple thresholds:

```yaml
# Prometheus alerting rules (prometheus/rules/disk.yml)
groups:
  - name: disk
    rules:
      - alert: DiskUsageWarning
        expr: node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"} < 0.20
        for: 5m
        labels:
          severity: warning
      - alert: DiskUsageCritical
        expr: node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"} < 0.10
        for: 2m
        labels:
          severity: critical
      - alert: InodeUsageWarning
        expr: node_filesystem_files_free / node_filesystem_files < 0.10
        for: 5m
        labels:
          severity: warning
```

Ensure logrotate covers every application log directory:

```bash
# Audit for log files not covered by logrotate
logrotate -d /etc/logrotate.conf 2>&1 | grep error
```

Set systemd journal size limits in `/etc/systemd/journald.conf`:

```ini
SystemMaxUse=500M
SystemKeepFree=1G
```

For Docker hosts, configure container log limits in `/etc/docker/daemon.json`:

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
```

Schedule automatic temp file cleanup via systemd-tmpfiles:

```bash
cat > /etc/tmpfiles.d/cleanup.conf << 'EOF'
d /tmp 1777 root root 7d
d /var/tmp 1777 root root 30d
EOF
```

Set reserved block percentages appropriate to partition role:

```bash
tune2fs -m 5 /dev/sdX1    # system partitions (/, /boot) — keep default 5%
tune2fs -m 1 /dev/sdX2    # data-only partitions (/data, /var/lib) — reduce to 1%
```

Implement capacity planning: track weekly disk usage trends and provision additional storage when usage exceeds 70% with a growth trajectory reaching 90% within 30 days.

## Sources

- [Linux man pages: df(1)](https://man7.org/linux/man-pages/man1/df.1.html) — Priority 1. Authoritative reference for filesystem disk space reporting, `-h` and `-i` flags.
- [Linux man pages: du(1)](https://man7.org/linux/man-pages/man1/du.1.html) — Priority 1. Authoritative reference for disk usage estimation, `-x` (single filesystem) flag.
- [Linux man pages: lsof(8)](https://man7.org/linux/man-pages/man8/lsof.8.html) — Priority 1. Authoritative reference for `+L1` deleted-but-open file detection and `/proc/PID/fd` method.
- [Linux man pages: tune2fs(8)](https://man7.org/linux/man-pages/man8/tune2fs.8.html) — Priority 1. ext4 reserved block configuration and `-m` percentage parameter.
- [logrotate(8) manual](https://man7.org/linux/man-pages/man8/logrotate.8.html) — Priority 1. `copytruncate`, `postrotate`, `maxsize` directives for log rotation.
- [systemd-journald.conf(5)](https://www.freedesktop.org/software/systemd/man/journald.conf.html) — Priority 1. `SystemMaxUse`, `SystemKeepFree`, and vacuum configuration.
- [Brendan Gregg - Linux Performance](https://www.brendangregg.com/linuxperf.html) — Priority 2. USE method applied to storage utilization, saturation, and error diagnosis.
- [Brendan Gregg - USE Method](https://www.brendangregg.com/usemethod.html) — Priority 2. Utilization-Saturation-Errors methodology for storage capacity resources.
