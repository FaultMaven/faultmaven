---
id: "linux-disk-full"
title: "Linux Disk Full - Filesystem at 100% Capacity"
domain: compute
service: linux
symptom_class: [disk_full]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: "kb-researcher"
status: draft
tags: [disk-space, inode, ext4, xfs, lsof, du, df, no-space-left, enospc, logrotate]
difficulty: intermediate
---

## Problem Definition

Applies to any Linux system (kernel 3.x+) running ext4, XFS, or tmpfs filesystems. Requires root or sudo access to run diagnostic and remediation commands. Relevant to bare-metal servers, virtual machines, and container hosts. All commands are distribution-agnostic unless noted otherwise.

The filesystem has reached 100% utilization, causing writes to fail with `No space left on device` (errno ENOSPC). Services fail to start, log rotation breaks, databases refuse writes, and temporary file creation fails. Common alert patterns include disk usage threshold exceeded (>90%), inode usage threshold exceeded, and application write errors referencing ENOSPC. Kernel messages appear in dmesg: `EXT4-fs warning: ... has reached maxsize` or `XFS: ... No space left on device`.

Three distinct failure modes produce this symptom:

- **Block space exhaustion** (most common): the filesystem has no free data blocks.
- **Inode exhaustion**: many small files consume all inodes while free block space remains. `df -h` shows available space but `df -i` shows 100% inode usage.
- **Deleted-but-open file handles**: `df` reports the disk as full but `du` shows less total usage because deleted files are still held open by running processes. The kernel cannot reclaim the blocks until the file descriptor is closed.

## Diagnostic Steps

### Step 1: Assess Overall Disk and Inode Usage

**What this checks**: Identifies which filesystem is full and whether the exhaustion is block-based or inode-based.

```bash
df -h
```

**Expected output**: One or more filesystems showing `Use%` at 100% (or above 95% for non-root users due to reserved blocks). Note the mount point for subsequent commands.

```bash
df -i
```

**Expected output**: The `IUse%` column shows inode utilization. If inode usage is at 100% but block usage is not, the problem is inode exhaustion — skip to Step 4. If both are high, address block space first.

**What the finding means**: Block exhaustion at 100% prevents all writes. Inode exhaustion at 100% prevents new file creation even with free block space. Both require different remediation paths.

### Step 2: Identify Large Files and Directories

**What this checks**: Locates the largest consumers of disk space on the affected filesystem by drilling down from the mount point.

```bash
du -xh --max-depth=1 / 2>/dev/null | sort -rh | head -20
```

**Expected output**: A sorted list of top-level directories by size. Common offenders are `/var/log`, `/var/lib/docker`, `/var/lib/mysql`, `/tmp`, and `/home`. The `-x` flag prevents crossing filesystem boundaries.

Drill into the largest directory by repeating with that path (e.g., `du -xh --max-depth=1 /var/log 2>/dev/null | sort -rh | head -20`).

```bash
find / -xdev -type f -size +100M -exec ls -lh {} \; 2>/dev/null | sort -k5 -rh | head -20
```

**Expected output**: Individual files larger than 100MB on the root filesystem. Large log files, core dumps, heap dumps, and database WAL segments are frequent offenders.

**What the finding means**: If a single directory or file dominates usage, the root cause is likely unbounded logging, failed log rotation, or an application writing large temporary files. Proceed to Mitigation Option A or Root Cause Resolution.

### Step 3: Check for Deleted-but-Open Files

**What this checks**: Identifies files that have been deleted from the directory tree (link count = 0) but are still held open by a running process. The kernel cannot reclaim the disk space until the process releases the file descriptor.

```bash
lsof +L1 2>/dev/null | awk '{print $1, $2, $7, $9}' | sort -k3 -rn | head -20
```

**Expected output**: Columns show process name, PID, file size (bytes), and file path marked `(deleted)`. Common causes: log files deleted while the application still writes to them, or logrotate running without a `copytruncate` or service reload directive.

**What the finding means**: If large deleted-but-open files appear (hundreds of MB or more), this explains why `df` shows more usage than `du`. Proceed to Mitigation Option B. If no significant deleted-but-open files appear, the problem is standard block or inode exhaustion.

### Step 4: Check Inode-Heavy Directories (If Inode Exhaustion)

**What this checks**: Identifies directories containing the most files, which is the direct cause of inode exhaustion. Only needed when Step 1 shows `IUse%` at or near 100%.

```bash
for d in /tmp /var/spool /var/cache /var/lib /var/log; do
  echo "$(find "$d" -xdev -type f 2>/dev/null | wc -l) $d"
done | sort -rn
```

**Expected output**: A count of files per directory, sorted descending. Directories with hundreds of thousands or millions of files are the inode consumers. Common culprits: PHP session files (`/var/lib/php/sessions`), mail queue (`/var/spool/postfix`), package manager metadata, and container overlay layers.

For a more thorough scan across the entire filesystem:

```bash
find / -xdev -type d -exec sh -c 'echo "$(find "$1" -maxdepth 1 -type f | wc -l) $1"' _ {} \; 2>/dev/null | sort -rn | head -20
```

**What the finding means**: The directory with the highest file count is consuming the most inodes. Removing stale files from that directory will restore inode availability. Proceed to Root Cause Resolution for inode-specific fixes.

### Step 5: Check Reserved Blocks (ext4 Only)

**What this checks**: On ext4 filesystems, 5% of blocks are reserved for root by default. Non-root processes receive ENOSPC at ~95% usage while root can still write. This is expected behavior, not a bug.

```bash
tune2fs -l /dev/sdX1 2>/dev/null | grep -i "reserved block"
```

Replace `/dev/sdX1` with the actual device from `df -h` output.

**Expected output**: Lines showing `Reserved block count` and `Reserved blocks uid`. The default reserved percentage is 5%.

For XFS filesystems, check metadata reservation instead:

```bash
xfs_info /mountpoint
```

**What the finding means**: If non-root processes fail at 95% while root still works, the reserved block mechanism is functioning normally. Reducing reserved blocks (Mitigation Option D) can provide temporary relief for data partitions.

### Step 6: Identify Recent Large Growth

**What this checks**: Finds files modified in the last 24 hours that are large enough to be the active cause of disk filling. Helps identify which process is filling the disk right now.

```bash
find / -xdev -type f -mtime -1 -size +10M -exec ls -lh {} \; 2>/dev/null | sort -k5 -rh | head -20
```

**Expected output**: Recently modified large files, sorted by size. Application debug logs, crash dumps, and database transaction logs are common hits.

```bash
journalctl --disk-usage
```

**Expected output**: Total disk usage of the systemd journal. Values above 500MB indicate journald needs size limits configured.

**What the finding means**: If a specific file is both recently modified and very large, the associated process is the active cause. Correlate the file path with the owning service to determine the root cause fix.

## Mitigation

### Option A: Truncate Identified Large Log Files

**Risk**: Application loses recent log data in the truncated file. Most applications handle in-place truncation gracefully, but some may crash or continue writing at the previous file offset, creating a sparse file with a hole.

**Command**:

```bash
truncate -s 0 /var/log/large-application.log
```

**Verify**:

```bash
df -h /var/log
```

Confirm free space increased on the target filesystem.

**Duration**: Immediate relief. Configure log rotation within 24 hours to prevent recurrence.

### Option B: Reclaim Space from Deleted-but-Open Files

**Risk**: Using the `/proc` truncation method has no service impact. Restarting the service causes brief downtime; coordinate with load balancers or use rolling restarts in production.

**Command**:

```bash
# Identify the process holding the largest deleted file
lsof +L1 2>/dev/null | sort -k7 -rn | head -5

# Method 1: Truncate the deleted file via /proc (no restart needed)
# Replace <PID> and <FD> with values from lsof output columns 2 and 4
cat /dev/null > /proc/<PID>/fd/<FD>

# Method 2: Restart the service to release all deleted file handles
systemctl restart <service-name>
```

**Verify**:

```bash
df -h /
lsof +L1 2>/dev/null | wc -l
```

Confirm free space increased and the count of deleted-but-open files decreased.

**Duration**: Immediate relief. The `/proc` truncation is safe indefinitely. Service restart impact depends on the specific service.

### Option C: Emergency Cleanup of Known Safe Targets

**Risk**: Removing package caches requires re-download if packages are needed later. Removing tmp files may break running processes that depend on them. Old journal logs and kernels are safe to remove.

**Command**:

```bash
# Clear package manager caches
apt-get clean 2>/dev/null           # Debian/Ubuntu
yum clean all 2>/dev/null           # RHEL/CentOS 7
dnf clean all 2>/dev/null           # RHEL/CentOS 8+/Fedora

# Remove old journal logs (keep last 2 days)
journalctl --vacuum-time=2d

# Remove old kernels (keep current + one previous)
# Debian/Ubuntu:
apt-get autoremove --purge -y

# Clear /tmp files older than 7 days
find /tmp -type f -atime +7 -delete 2>/dev/null
find /var/tmp -type f -atime +7 -delete 2>/dev/null
```

**Verify**:

```bash
df -h /
df -h /boot
df -h /tmp
```

Check all relevant mount points to confirm space was reclaimed.

**Duration**: Immediate relief. Package cache rebuilds over time. Schedule proper capacity management within 48 hours.

### Option D: Temporarily Reduce Reserved Blocks (ext4 Only)

**Risk**: Reduces the safety margin for root operations. If the filesystem fills completely with 0% reserved, even root cannot write, potentially making the system unrecoverable without booting from external media. Use only on data partitions, not on `/` or `/boot`.

**Command**:

```bash
# Reduce reserved blocks from 5% to 1% (can be done on a mounted filesystem)
tune2fs -m 1 /dev/sdX1
```

**Verify**:

```bash
df -h /
tune2fs -l /dev/sdX1 | grep -i "reserved block"
```

Confirm available space increased and the reserved percentage is now 1%.

**Duration**: Safe for hours to days. Restore to 5% once the root cause is resolved: `tune2fs -m 5 /dev/sdX1`.

## Root Cause Resolution

**If** Step 2 shows a single large log file growing unbounded (e.g., `/var/log/application.log` at tens of GB) --> Configure logrotate for the application:

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

# Test the configuration (dry-run, no changes made)
logrotate -d /etc/logrotate.d/application
```

**If** Step 3 shows deleted-but-open files consuming space --> Fix the log rotation configuration to use `copytruncate` (for applications that do not reopen log files on SIGHUP) or add a `postrotate` script to signal the application to reopen its log file:

```bash
cat > /etc/logrotate.d/nginx << 'LOGROTATE'
/var/log/nginx/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 www-data adm
    sharedscripts
    postrotate
        [ -f /var/run/nginx.pid ] && kill -USR1 $(cat /var/run/nginx.pid)
    endscript
}
LOGROTATE
```

**If** Step 4 shows inode exhaustion from millions of small files (e.g., PHP session files, mail queue) --> Clean stale files and configure automatic cleanup:

```bash
# Remove PHP session files older than 24 hours
find /var/lib/php/sessions -type f -mmin +1440 -delete

# For Postfix mail queue buildup
postsuper -d ALL deferred
```

**If** Step 2 shows `/var/lib/docker` consuming excessive space --> Prune unused Docker resources and configure container log limits:

```bash
# Remove unused images, containers, volumes, and build cache
docker system prune -af --volumes

# Configure log limits in /etc/docker/daemon.json to prevent recurrence
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
```

**If** Step 6 shows journald consuming excessive space --> Set persistent size limits:

```bash
sed -i 's/^#SystemMaxUse=.*/SystemMaxUse=500M/' /etc/systemd/journald.conf
sed -i 's/^#SystemKeepFree=.*/SystemKeepFree=1G/' /etc/systemd/journald.conf
systemctl restart systemd-journald
```

**If** the filesystem is genuinely too small for the workload --> Extend the volume:

```bash
# LVM: extend logical volume and resize filesystem
lvextend -L +10G /dev/mapper/vg-lv
resize2fs /dev/mapper/vg-lv         # ext4
xfs_growfs /mountpoint               # XFS (cannot shrink, only grow)
```

## Verification

After applying the root cause fix, confirm the issue is resolved:

```bash
# Check current disk usage is below threshold
df -h
df -i
```

Verify block usage is below 85% and inode usage is well below 100%.

```bash
# Confirm no deleted-but-open files are consuming significant space (>100MB)
lsof +L1 2>/dev/null | awk '$7 > 104857600 {print $1, $2, $7, $9}'
```

Output should be empty. Any remaining entries indicate processes still holding large deleted files.

```bash
# Monitor disk growth over the next hour to confirm the leak is stopped
watch -n 60 'df -h / | tail -1'
```

Observe for 30-60 minutes. Usage should remain stable or decrease. If it continues to climb, the root cause was not fully addressed — return to Diagnostic Steps.

```bash
# Verify logrotate configuration is valid (dry-run)
logrotate -d /etc/logrotate.conf
```

Dry-run should show no errors for the affected log files.

## Prevention

Configure disk usage monitoring alerts at multiple thresholds to catch issues before they become critical:

```bash
# Prometheus node_exporter alert rules (example)
# Warning at 80% usage:
#   node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.2
# Critical at 90% usage:
#   node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.1
# Inode warning at 90% usage:
#   node_filesystem_files_free / node_filesystem_files < 0.1
```

Ensure logrotate is configured for every application that writes logs:

```bash
logrotate -d /etc/logrotate.conf 2>&1 | grep "error"
```

Set systemd journal size limits in `/etc/systemd/journald.conf`:

```bash
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

Schedule regular cleanup jobs for known temporary file accumulation points using systemd-tmpfiles:

```bash
cat > /etc/tmpfiles.d/cleanup.conf << 'EOF'
d /tmp 1777 root root 7d
d /var/tmp 1777 root root 30d
EOF
```

For ext4 filesystems, set reserved blocks appropriately per partition role:

```bash
tune2fs -m 5 /dev/sdX1    # system partitions (/, /boot) — keep default 5%
tune2fs -m 1 /dev/sdX1    # data-only partitions (/data, /var/lib) — reduce to 1%
```

Implement capacity planning: track disk usage trends weekly and provision additional storage when usage exceeds 70% with a growth trajectory that would reach 90% within 30 days.

## Sources

- [Brendan Gregg - Linux Performance](https://www.brendangregg.com/linuxperf.html) - Industry reference for Linux performance diagnostics tooling including disk I/O and storage analysis.
- [Brendan Gregg - USE Method](https://www.brendangregg.com/usemethod.html) - Utilization-Saturation-Errors methodology applied to storage capacity resources.
- [Linux man pages: df(1)](https://man7.org/linux/man-pages/man1/df.1.html) - Authoritative reference for filesystem disk space reporting.
- [Linux man pages: du(1)](https://man7.org/linux/man-pages/man1/du.1.html) - Authoritative reference for disk usage estimation per directory and file.
- [Linux man pages: lsof(8)](https://man7.org/linux/man-pages/man8/lsof.8.html) - Authoritative reference for listing open files, including deleted-but-open file detection.
- [Linux man pages: tune2fs(8)](https://man7.org/linux/man-pages/man8/tune2fs.8.html) - ext4 reserved blocks configuration and filesystem parameter adjustment.
- [logrotate(8) manual](https://man7.org/linux/man-pages/man8/logrotate.8.html) - Log rotation configuration including copytruncate and postrotate directives.
- [systemd-journald.conf(5)](https://www.freedesktop.org/software/systemd/man/journald.conf.html) - Journal size limits and vacuum configuration.
- [TheLinuxCode - Fix No Space Left on Device](https://thelinuxcode.com/how-i-fix-no-space-left-on-device-on-linux-disk-inodes-logs-containers-and-partitions/) - Practical guide covering block, inode, log, container, and partition-level disk full remediation.
- [OneUptime - Fix No Space Left on Device (2026)](https://oneuptime.com/blog/post/2026-01-24-fix-no-space-left-on-device/view) - Contemporary troubleshooting guide with Docker cleanup and automated monitoring strategies.
