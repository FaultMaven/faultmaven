---
id: "linux-disk-full"
title: "Linux Disk Full"
domain: compute
service: linux
symptom_class: [disk_full]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

### Step 1: Identify the full filesystem and exhaustion type

```bash
df -h && echo "---" && df -i
```

Expected output: `Use%` at 100% for block space or `IUse%` at 100% for inodes on one or more mount points. Note the mount point and device. If inode usage is 100% but block usage is not, skip to Step 4. If both are high, address block space first.

### Step 2: Locate large files and directories

```bash
du -xh --max-depth=1 / 2>/dev/null | sort -rh | head -20
```

Expected output: Sorted list of top-level directories by size. Common offenders: `/var/log`, `/var/lib/docker`, `/var/lib/mysql`, `/tmp`, `/home`. Repeat drilling into the largest directory (e.g., `du -xh --max-depth=1 /var/log 2>/dev/null | sort -rh | head -20`).

```bash
find / -xdev -type f -size +100M -exec ls -lh {} \; 2>/dev/null | sort -k5 -rh | head -20
```

Expected output: Individual files larger than 100 MB on the affected filesystem. Large log files, core dumps, heap dumps, and database WAL segments are frequent offenders.

### Step 3: Check for deleted-but-open files

```bash
lsof +L1 2>/dev/null | awk '{print $1, $2, $7, $9}' | sort -k3 -rn | head -20
```

Expected output: Columns show process name, PID, file size in bytes, and path marked `(deleted)`. Large values in the size column (hundreds of MB or more) indicate the kernel cannot reclaim those blocks until the holding process closes or restarts.

### Step 4: Count files per directory for inode exhaustion

```bash
for d in /tmp /var/spool /var/cache /var/lib /var/log; do
  echo "$(find "$d" -xdev -type f 2>/dev/null | wc -l) $d"
done | sort -rn
```

Expected output: File count per directory, sorted descending. Directories with hundreds of thousands or millions of files are consuming inodes. Common culprits: PHP session files (`/var/lib/php/sessions`), mail queue (`/var/spool/postfix`), container overlay layers.

### Step 5: Check reserved blocks (ext4) or metadata reservation (XFS)

```bash
tune2fs -l "$(df --output=source / | tail -1)" 2>/dev/null | grep -i "reserved block"
```

Expected output: `Reserved block count` showing how many blocks are held for root. Default is 5% of total blocks. Non-root processes receive ENOSPC when usage reaches ~95%.

```bash
xfs_info / 2>/dev/null | head -5
```

Expected output: XFS geometry including `agcount`, `agsize`, and internal log size. XFS does not have the same root-reservation model as ext4.

### Step 6: Identify active growth source

```bash
find / -xdev -type f -mtime -1 -size +10M -exec ls -lh {} \; 2>/dev/null | sort -k5 -rh | head -20
```

Expected output: Files modified in the last 24 hours that are large. Correlate file path with owning service to find what is actively filling the disk.

```bash
journalctl --disk-usage
```

Expected output: Total disk usage of the systemd journal. Values above 500 MB indicate journald needs size limits configured.

## Causes

### Cause A: Unbounded application log growth

**Statement:** An application writes to a log file without enforced size limits and logrotate is absent or misconfigured, allowing the log to fill the filesystem.

**Chain:**
- root: Log rotation is absent or its config lacks a `maxsize` directive, so no policy bounds the application's log file.
- s1: The log file grows continuously with each request, error, or debug event, with no cap to truncate or rotate it.
- s2: The growing log consumes the last free blocks and the filesystem reaches 100% utilization.
- D: All write syscalls on that partition return ENOSPC, cascading failures across services (Symptom Recognition).

**Indicators:**
- s1: [Step 2] A single file under `/var/log/` or an application log directory dominates `du` output.
  <!-- match: {"step": 2, "predicate": "contains", "target": "/var/log/"} -->
- s1: [Step 6] The same file appears among recently modified large files.
- D: [Step 1] `df -h` shows `Use%` at 100% on the affected mount.

**Interventions:**
- **remediation** (root): install a logrotate policy with `maxsize` so the log is bounded and rotated.

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
- **mitigation** (s1): truncate the runaway log in place to immediately reclaim blocks.

  ```bash
  truncate -s 0 /var/log/large-application.log
  ```

  **Risk:** Truncating a log while the process holds it open creates a sparse file starting from offset 0 on next write by some applications; most modern apps handle this correctly. **Duration:** Immediate relief; configure log rotation within 24 hours to prevent recurrence. **Verification:** Re-run Step 2; the file is no longer dominant and `df -h` shows reclaimed space.

---

### Cause B: Deleted-but-open file handles

**Statement:** Files deleted from the directory tree are still held open by running processes, preventing the kernel from reclaiming their disk blocks until the process closes or restarts.

**Chain:**
- root: A process holds an open file descriptor on a log/data file that logrotate or a script has since unlinked from the directory tree.
- s1: The kernel marks the inode for deletion but cannot free its data blocks while the descriptor count is above zero.
- s2: `df` reports full utilization (blocks allocated) while `du` shows lower usage (directory entry gone) — the diagnostic fingerprint.
- D: Writes on the partition return ENOSPC because the orphaned blocks remain unreclaimable (Symptom Recognition).

**Indicators:**
- s1: [Step 3] `lsof +L1` shows one or more entries with a `(deleted)` path and a size above 100 MB.
  <!-- match: {"step": 3, "predicate": "contains", "target": "(deleted)"} -->
- s2: [Step 1] `df -h` shows `Use%` at 100% while `du` (Step 2) reports less consumed space.

**Interventions:**
- **remediation** (root): fix logrotate to signal the app to reopen its log files after rotation, so descriptors no longer pin deleted files.

  ```bash
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
- **mitigation** (s1): truncate the deleted file via `/proc/PID/fd`, or restart the holding process to release all deleted handles.

  ```bash
  # Identify PID and FD from lsof output columns 2 and 4
  lsof +L1 2>/dev/null | sort -k7 -rn | head -5

  # Method 1: truncate in-place (no restart needed)
  cat /dev/null > /proc/<PID>/fd/<FD>

  # Method 2: restart the process (releases all deleted handles)
  systemctl restart <service-name>
  ```

  **Risk:** Truncating via `/proc/PID/fd/FD` is zero-downtime. Service restart causes brief outage; coordinate with load balancers for stateful services. **Duration:** Immediate relief; `/proc` truncation is safe indefinitely. **Verification:** Re-run Step 3; the `(deleted)` entry is gone and `df -h` shows space reclaimed.

---

### Cause C: Inode exhaustion from small file accumulation

**Statement:** Millions of small files have consumed all inodes on the filesystem, preventing new file creation even though significant block space remains free.

**Chain:**
- root: An application creates many small files (PHP sessions, mail queue entries, package metadata) with no cleanup policy.
- s1: Each file consumes one inode from the fixed-size inode table (ext4: one inode per 16 KB by default), draining the inode pool while blocks stay largely free.
- s2: The inode table is fully allocated: `df -i` shows `IUse%` at 100% while `df -h` still shows free block space.
- D: Any attempt to create a file returns ENOSPC because no inode is available (Symptom Recognition).

**Indicators:**
- s1: [Step 4] One directory contains hundreds of thousands or millions of files.
- s2: [Step 1] `df -i` shows `IUse%` at 100% while `df -h` shows block space available.
  <!-- match: {"step": 1, "predicate": "contains", "target": "100%"} -->

**Interventions:**
- **remediation** (root): schedule periodic cleanup so the file-producing app no longer accumulates inodes unbounded.

  ```bash
  # Configure PHP session cleanup via cron
  echo '0 * * * * root find /var/lib/php/sessions -type f -mmin +1440 -delete' \
    > /etc/cron.d/php-session-cleanup

  # For mail queue: fix the upstream relay and flush the queue
  postfix check && postqueue -f
  ```

  **Verification:** Run `df -i` and confirm `IUse%` has dropped below 90%. Run `df -h` to confirm block usage is also acceptable.
- **mitigation** (s1): bulk-delete the accumulated small files to immediately free inodes.

  ```bash
  # PHP sessions older than 24 hours
  find /var/lib/php/sessions -type f -mmin +1440 -delete

  # Postfix deferred mail queue
  postsuper -d ALL deferred

  # Generic: remove files not accessed in 7 days from a temp directory
  find /var/spool/target -type f -atime +7 -delete
  ```

  **Risk:** Deleting session or spool files may drop active user sessions or queued messages; validate the target directory before bulk deletion. **Duration:** Immediate relief after deletion completes. **Verification:** Re-run Step 1 (`df -i`); `IUse%` has dropped below 90%.

---

### Cause D: Docker or container runtime storage accumulation

**Statement:** Docker or another container runtime has accumulated stopped containers, unused images, anonymous volumes, and build cache that is not automatically garbage-collected.

**Chain:**
- root: The container runtime has no automatic garbage-collection policy for stopped containers, unused images, anonymous volumes, and build cache.
- s1: Each `docker pull`, `docker build`, or stopped container leaves layers, writable filesystems, and volumes under `/var/lib/docker` that are never pruned.
- s2: On hosts with CI/CD or frequent rebuilds, `/var/lib/docker` grows to tens or hundreds of GB and dominates the filesystem.
- D: The host filesystem reaches capacity and writes return ENOSPC (Symptom Recognition).

**Indicators:**
- s2: [Step 2] `du` output shows `/var/lib/docker` as the dominant consumer.
  <!-- match: {"step": 2, "predicate": "contains", "target": "/var/lib/docker"} -->
- D: [Step 1] `df -h` shows `Use%` at or near 100% on the host filesystem.

**Interventions:**
- **remediation** (root): cap container log size and schedule a recurring prune so storage no longer accumulates unbounded.

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
- **mitigation** (s1): prune unused images, containers, and volumes now to reclaim space immediately.

  ```bash
  docker system prune -af --volumes
  ```

  **Risk:** `docker system prune -af --volumes` removes ALL unused images (including base images needed for future builds) and ALL anonymous volumes. Named volumes used by running services are not touched, but confirm no unnamed volumes contain data before running. **Duration:** Immediate relief; base images will be re-pulled on next deploy. **Verification:** Re-run Step 2; `/var/lib/docker` is no longer dominant and `df -h` shows reclaimed space.

---

### Cause E: systemd journal consuming excessive space

**Statement:** The systemd journal has no size cap configured and has grown to consume a significant fraction of the filesystem, particularly on hosts with verbose logging or long retention.

**Chain:**
- root: `SystemMaxUse`/`RuntimeMaxUse` are not explicitly set, so journald falls back to defaults (10% of FS or 4 GB) that are too permissive on large or small disks.
- s1: Every application and the kernel writes to the journal, which grows for gigabytes before self-capping (or never caps usefully on small disks).
- s2: The journal occupies a significant fraction of the filesystem, pushing free space toward zero.
- D: Combined with other consumers, the partition reaches capacity and writes return ENOSPC (Symptom Recognition).

**Indicators:**
- s2: [Step 6] `journalctl --disk-usage` reports a value above 1 GB.
  <!-- match: {"step": 6, "predicate": "threshold", "target": "journal_gb", "op": ">", "value": 1} -->

**Interventions:**
- **remediation** (root): set explicit `SystemMaxUse`/`SystemKeepFree` limits so the journal can never grow unbounded again.

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
- **mitigation** (s1): vacuum old journal entries to immediately reclaim space.

  ```bash
  journalctl --vacuum-time=2d
  ```

  **Risk:** Vacuum operations permanently delete journal entries older than the cutoff. Ensure log retention requirements are met before setting aggressive limits. **Duration:** Immediate relief; set permanent limits within 1 hour. **Verification:** Re-run Step 6; `journalctl --disk-usage` reports a reduced value and `df -h` shows reclaimed space.

---

### Cause F: ext4 reserved block percentage causing false ENOSPC

**Statement:** Non-root processes receive ENOSPC when filesystem usage reaches approximately 95% because ext4 reserves 5% of blocks for root by default, even on data-only partitions.

**Chain:**
- root: An ext4 data partition retains the default 5% reserved-block allocation, which is intended for root login on system partitions, not data partitions.
- s1: As usage approaches 95%, the reserved margin is reached and only root-owned processes may consume the remaining blocks.
- D: Non-root application processes see `df -h` near 95% and fail writes with ENOSPC while root writes still succeed (Symptom Recognition).

**Indicators:**
- root: [Step 5] `tune2fs -l` shows `Reserved block count` at 5% of total blocks.
  <!-- match: {"step": 5, "predicate": "contains", "target": "Reserved block count"} -->
- s1: [Step 1] `df -h` shows `Use%` at 95–96% but application writes fail with ENOSPC.

**Interventions:**
- **remediation** (root): reduce the reserved-block percentage on the data partition to 1%, then restore to 2–5% once the underlying capacity issue is resolved.

  ```bash
  # Identify device (replace /dev/sdX1 with actual device from df -h)
  tune2fs -m 1 /dev/sdX1
  # Restore after root cause is resolved:
  tune2fs -m 5 /dev/sdX1
  ```

  **Verification:** Run `df -h` and confirm `Use%` is now below 100% with available space visible. Run `tune2fs -l /dev/sdX1 | grep -i "reserved block"` to confirm the new percentage.
- **mitigation** (s1): temporarily lower reserved blocks to 1% to unblock non-root writes during the incident.

  ```bash
  tune2fs -m 1 /dev/sdX1
  ```

  **Risk:** Reducing reserved blocks on `/` or `/boot` removes the safety margin that allows root login during disk-full emergencies. Use only on data partitions (`/data`, `/var/lib`, `/mnt/data`). **Duration:** Temporary; restore to 2–5% after the root cause is addressed. **Verification:** Re-run Step 1 (`df -h`); available space is visible and non-root writes succeed.

---

### Cause Z: Unidentified

**Statement:** The filesystem is full but the cause cannot be determined from the standard diagnostic steps.

**Chain:**
- root: A less common consumer — stale NFS mount, LVM snapshot overflow, overlay2/devicemapper metadata bloat, or a bind mount masking actual consumers — is filling the filesystem and is not surfaced by Steps 1–6.
- D: The filesystem reports full and writes return ENOSPC with no clear dominant consumer (Symptom Recognition).

**Indicators:**
- [Default] None of Steps 1–6 identify a clear dominant consumer.

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot, run low-risk emergency cleanup, and escalate to the infrastructure SME.

  ```bash
  # Diagnostic snapshot for escalation
  df -h; df -i
  du -xh --max-depth=2 / 2>/dev/null | sort -rh | head -30
  lsof +L1 2>/dev/null | sort -k7 -rn | head -20

  # Low-risk emergency cleanup
  apt-get clean 2>/dev/null || yum clean all 2>/dev/null || dnf clean all 2>/dev/null
  journalctl --vacuum-time=2d
  find /tmp -type f -atime +7 -delete 2>/dev/null
  find /var/tmp -type f -atime +7 -delete 2>/dev/null
  ```

  **Risk:** Emergency cleanup of `/tmp` or package caches is low-risk on most systems; verify no active workloads depend on temp files before deletion. **Duration:** Immediate partial relief; escalate for deeper investigation. **Verification:** Re-run `df -h`. If usage remains at 100% after all emergency cleanup, escalate immediately — a live process may be filling the disk faster than space can be reclaimed.

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
