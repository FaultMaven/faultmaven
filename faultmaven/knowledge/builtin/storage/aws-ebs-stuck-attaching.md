---
id: aws-ebs-stuck-attaching
title: "AWS EBS Volume Stuck Attaching/Detaching — Diagnosis and Resolution"
domain: storage
service: aws-ec2
symptom_class:
  - service_unavailable
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - ebs
  - volume
  - attaching
  - detaching
  - stuck
  - ec2
difficulty: intermediate
---

# AWS EBS Volume Stuck Attaching/Detaching — Diagnosis and Resolution

## Problem Definition

Applies to all AWS accounts using EBS volumes (gp2, gp3, io1, io2, st1, sc1). Requires EC2 permissions (`ec2:DescribeVolumes`, `ec2:DescribeInstances`, `ec2:DetachVolume`, `ec2:AttachVolume`) and optionally SSH/SSM access to the instance for OS-level diagnostics. Affects all instance types and volume types.

An EBS volume becomes stuck in the `attaching` or `detaching` state when the attachment or detachment operation does not complete within the expected timeframe (typically 30-60 seconds). The volume state remains transitional indefinitely:

```
$ aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 \
    --query 'Volumes[].Attachments[].State'
["attaching"]
```

```
$ aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 \
    --query 'Volumes[].Attachments[].State'
["detaching"]
```

Common causes:

- **OS-level mount lock** — the volume is still mounted or in use by a process at the OS level, preventing the hypervisor from completing detachment.
- **Busy filesystem** — open file handles, active I/O, or a process writing to the volume prevents clean unmount.
- **Instance impairment** — the underlying EC2 instance is in a degraded state, preventing the hypervisor from processing attachment/detachment.
- **NVMe driver issues** — the NVMe driver on Nitro-based instances is unresponsive or has a stale device mapping.
- **Device name conflict** — the requested device name is already in use or conflicts with instance store volumes.
- **API rate limiting** — high volumes of concurrent attach/detach operations in the account trigger throttling, stalling individual operations.

## Diagnostic Steps

### Step 1. Check the volume and attachment state

Retrieves the current volume state, attachment state, and the associated instance to confirm the volume is genuinely stuck and identify the target instance.

```bash
aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 \
  --query 'Volumes[].{VolumeId:VolumeId,State:State,AZ:AvailabilityZone,Attachments:Attachments[].{InstanceId:InstanceId,Device:Device,State:State,AttachTime:AttachTime}}' \
  --output json
```

Expected output shows the volume state (`in-use`, `available`, or `error`) and attachment state (`attaching`, `attached`, `detaching`, `detached`). If the attachment state has been `attaching` or `detaching` for more than 5 minutes, the operation is stuck.

### Step 2. Check the instance status

Determines whether the instance itself is healthy. A failed or impaired instance cannot complete volume operations.

```bash
aws ec2 describe-instance-status --instance-ids i-0123456789abcdef0 \
  --query 'InstanceStatuses[].{State:InstanceState.Name,SystemStatus:SystemStatus.Status,InstanceStatus:InstanceStatus.Status}'
```

Expected output shows `running` state with both status checks `ok`. If either status is `impaired`, the instance has underlying issues that may prevent volume operations.

### Step 3. Check for OS-level mount and process activity (if detaching)

Identifies whether the filesystem on the volume is still mounted or whether processes have open file handles preventing clean detachment.

```bash
# SSH or SSM into the instance
# Check if the device is mounted
lsblk
mount | grep /dev/xvdf   # or /dev/nvme1n1 on Nitro instances

# Check for open file handles
lsof +D /mnt/data   # replace with mount point
fuser -vm /mnt/data
```

If `lsof` or `fuser` shows active processes, those processes must be stopped or the filesystem unmounted before detachment can complete. A busy filesystem is the most common cause of stuck detaching.

### Step 4. Check EC2 system events

Retrieves scheduled events or maintenance notifications that may affect the instance or volume.

```bash
aws ec2 describe-instance-status --instance-ids i-0123456789abcdef0 \
  --query 'InstanceStatuses[].Events'
```

Scheduled retirement, maintenance, or system reboot events can interfere with volume operations. If events are present, they may need to be resolved first.

### Step 5. Check CloudTrail for the attach/detach API call

Retrieves the original API call record to identify any errors or unusual parameters in the attach/detach request.

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=ResourceName,AttributeValue=vol-0123456789abcdef0 \
  --start-time "$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[].{Time:EventTime,Name:EventName,Event:CloudTrailEvent}' \
  --output json
```

Look for `AttachVolume` or `DetachVolume` events and check for error codes. Repeated failed attempts may indicate API-level issues.

### Step 6. Check NVMe device mapping (Nitro instances)

On Nitro-based instances, EBS volumes appear as NVMe devices. Verifies that the NVMe driver correctly maps the volume.

```bash
# On the instance
sudo nvme list
sudo nvme id-ctrl /dev/nvme1n1 | grep sn   # Serial number maps to volume ID
```

If `nvme list` does not show the expected volume or shows it in an error state, the NVMe driver may need to be reloaded or the instance may need a reboot.

## Mitigation

### Option 1: Force detach the volume

**Risk**: Force detach can cause data corruption if the volume has unflushed writes. Only use when normal detach has been stuck for more than 15 minutes and the instance is unresponsive.

**Command**:

```bash
aws ec2 detach-volume \
  --volume-id vol-0123456789abcdef0 \
  --force
```

**Verify**:

```bash
aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 \
  --query 'Volumes[].State'
```

Expected output: `"available"` (may take 1-2 minutes after force detach).

**Duration**: One-time operation. After the volume becomes available, it can be reattached.

### Option 2: Stop and start the instance (if attaching is stuck)

**Risk**: Stopping the instance causes downtime. All instance store data is lost. The instance may receive a new public IP if not using an Elastic IP. This clears the hypervisor-level attachment state.

**Command**:

```bash
aws ec2 stop-instances --instance-ids i-0123456789abcdef0
# Wait for stopped state
aws ec2 wait instance-stopped --instance-ids i-0123456789abcdef0
aws ec2 start-instances --instance-ids i-0123456789abcdef0
```

**Verify**:

```bash
aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 \
  --query 'Volumes[].Attachments[].State'
```

**Duration**: Instance restart takes 2-5 minutes. Plan for application downtime.

### Option 3: Unmount the filesystem from the OS (if detaching)

**Risk**: Killing processes with open file handles may cause data loss in those processes. Unmounting while writes are in-flight can corrupt buffered data. Sync first.

**Command**:

```bash
# On the instance
sudo sync
sudo fuser -km /mnt/data   # Kill all processes using the mount point
sudo umount /mnt/data
```

**Verify**: After unmounting, retry the detach:

```bash
aws ec2 detach-volume --volume-id vol-0123456789abcdef0
```

**Duration**: Immediate after successful unmount.

## Root Cause Resolution

**If** the filesystem was not unmounted before detach → always unmount before detaching:

```bash
# Correct detach sequence
sudo sync
sudo umount /mnt/data
aws ec2 detach-volume --volume-id vol-0123456789abcdef0
```

**If** a process holds open file handles → identify and stop the process before unmounting:

```bash
sudo lsof +D /mnt/data
sudo kill <pid>
sudo umount /mnt/data
```

**If** the instance is impaired → stop and restart the instance to clear the hypervisor state. If the instance cannot be stopped, terminate it and launch a replacement, then attach the volume to the new instance.

**If** a device name conflict prevented attachment → use a different device name:

```bash
aws ec2 attach-volume \
  --volume-id vol-0123456789abcdef0 \
  --instance-id i-0123456789abcdef0 \
  --device /dev/xvdg   # Use a device name not already in use
```

**If** the NVMe driver is stale or unresponsive → reload the driver or reboot:

```bash
sudo modprobe -r nvme
sudo modprobe nvme
# If that fails, reboot the instance
sudo reboot
```

**If** API rate limiting stalled the operation → reduce concurrent attach/detach operations and implement exponential backoff in automation scripts.

**If** the volume is stuck in `error` state after force detach → create a snapshot and restore to a new volume:

```bash
aws ec2 create-snapshot --volume-id vol-0123456789abcdef0 --description "Recovery snapshot"
# After snapshot completes
aws ec2 create-volume --snapshot-id snap-0123456789abcdef0 \
  --availability-zone us-east-1a --volume-type gp3
```

## Verification

1. Confirm the volume is in the expected state (`available` if detached, `in-use` with `attached` if reattached):

```bash
aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 \
  --query 'Volumes[].{State:State,Attachments:Attachments[].{InstanceId:InstanceId,State:State}}'
```

2. If reattached, verify the device is visible at the OS level:

```bash
lsblk
sudo file -s /dev/xvdg   # or /dev/nvme1n1
```

3. Mount the filesystem and verify data integrity:

```bash
sudo mount /dev/xvdg /mnt/data
ls -la /mnt/data
sudo xfs_repair -n /dev/xvdg   # Dry-run check for XFS
# Or for ext4:
sudo e2fsck -n /dev/xvdg
```

4. Verify the application can read and write to the volume successfully.

## Prevention

1. **Always unmount filesystems before detaching volumes**. Script the sequence: `sync` -> `umount` -> `detach-volume`.

2. **Use systemd mount units** to manage EBS volume mounts, ensuring clean unmount on instance stop/terminate:

```bash
# /etc/systemd/system/mnt-data.mount
# [Unit]
# Description=EBS Data Volume
# [Mount]
# What=/dev/xvdg
# Where=/mnt/data
# Type=xfs
# [Install]
# WantedBy=multi-user.target
```

3. **Implement health checks on volume attachment** in automation. Poll `describe-volumes` after attach/detach and raise an alert if the operation does not complete within 2 minutes.

4. **Use EBS Multi-Attach only with cluster-aware filesystems** (io1/io2). Standard filesystems (ext4, XFS) do not support concurrent writes from multiple instances.

5. **Set up CloudWatch alarms on volume status**:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name EBSVolumeStuck \
  --metric-name VolumeQueueLength \
  --namespace AWS/EBS \
  --statistic Average \
  --period 300 \
  --threshold 100 \
  --comparison-operator GreaterThanThreshold \
  --dimensions Name=VolumeId,Value=vol-0123456789abcdef0 \
  --evaluation-periods 2 \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:ops-alerts
```

6. **Avoid concurrent attach/detach operations** on the same instance. Serialize volume operations in automation scripts with proper waits between each.

7. **Use instance store volumes for ephemeral data** and EBS only for persistent data, reducing the number of volume operations needed during instance lifecycle events.

## Sources

- [Detach an Amazon EBS volume - AWS EC2 User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-detaching-volume.html)
- [Attach an Amazon EBS volume to an instance - AWS EC2 User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-attaching-volume.html)
- [Troubleshoot EBS volumes - AWS EC2 User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-troubleshooting.html)
- [Amazon EBS volume status checks - AWS EC2 User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/monitoring-volume-status.html)
- [Amazon EBS and NVMe on Linux - AWS EC2 User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nvme-ebs-volumes.html)
- [Force detach an EBS volume - AWS re:Post](https://repost.aws/knowledge-center/ebs-force-detach-volume)
