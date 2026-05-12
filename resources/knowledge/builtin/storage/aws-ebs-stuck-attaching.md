---
id: "aws-ebs-stuck-attaching"
title: "AWS EBS Volume Stuck Attaching or Detaching"
domain: storage
service: aws-ec2
symptom_class: [service_unavailable]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [aws, ebs, volume, attaching, detaching, stuck, ec2, nvme, nitro, force-detach]
difficulty: intermediate
---

## Symptom Recognition

- `aws ec2 describe-volumes` shows attachment `State: "attaching"` or `State: "detaching"` (or `State: "busy"`) for more than 5 minutes with no transition
- Volume state remains `in-use` despite a detach API call completing without error
- Instance is inaccessible or application fails because a data volume did not attach within the expected 30–60 second window
- CloudWatch `VolumeQueueLength` spikes on the affected volume while I/O requests queue behind the stuck operation
- EC2 console shows the volume in `attaching` or `detaching` state with an `AttachTime` that does not advance

## Applicability

Applies to all AWS accounts using Amazon EBS volumes (gp2, gp3, io1, io2, st1, sc1) attached to EC2 instances. Requires IAM permissions: `ec2:DescribeVolumes`, `ec2:DescribeInstances`, `ec2:DescribeInstanceStatus`, `ec2:DetachVolume`, `ec2:AttachVolume`, `cloudtrail:LookupEvents`. OS-level steps require SSH or AWS Systems Manager Session Manager access to the instance. Affects all instance families; NVMe-specific steps apply to Nitro-based instances only (C5, M5, R5, T3, and later).

## Diagnostic Steps

### Step 1: Confirm volume state and elapsed attach time

```bash
aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 \
  --query 'Volumes[].{VolumeId:VolumeId,VolumeState:State,AZ:AvailabilityZone,Attachments:Attachments[].{InstanceId:InstanceId,Device:Device,AttachState:State,AttachTime:AttachTime}}' \
  --output json
```

Expected output: `AttachState` is `attaching`, `detaching`, or `busy`; `AttachTime` more than 5 minutes in the past confirms the operation is stuck.

### Step 2: Check EC2 instance system status

```bash
aws ec2 describe-instance-status --instance-ids i-0123456789abcdef0 \
  --query 'InstanceStatuses[].{InstanceState:InstanceState.Name,SystemCheck:SystemStatus.Status,InstanceCheck:InstanceStatus.Status,Events:Events}'
```

Expected output: `running` state with both checks `ok`. `impaired` on either check indicates the underlying host has a hardware or hypervisor-level failure that blocks volume operations.

### Step 3: Check OS-level mount and open file handles (stuck detaching)

```bash
# Via SSH or SSM Session Manager on the instance
lsblk
mount | grep -E 'xvdf|nvme1'

lsof +D /mnt/data
fuser -vm /mnt/data
```

Expected output: if `lsblk` shows the device as mounted or `lsof`/`fuser` lists active PIDs, the filesystem is still in use — detachment cannot complete until those are cleared.

### Step 4: Retrieve OS console output for shutdown or I/O errors

```bash
aws ec2 get-console-output --instance-id i-0123456789abcdef0 \
  --query 'Output' --output text | tail -100
```

Expected output: look for kernel I/O errors (`blk_update_request`, `I/O error`), NVMe timeout messages (`nvme: I/O timeout`), or shutdown stalls (`A stop job is running`) that explain why the hypervisor cannot complete the volume operation.

### Step 5: Verify NVMe device mapping on Nitro instances

```bash
# On the instance (Nitro only — C5, M5, R5, T3, etc.)
sudo nvme list
sudo nvme id-ctrl /dev/nvme1n1 | grep -i sn
```

Expected output: `nvme list` shows volumes with their serial numbers (volume ID without `vol-` prefix). A missing or error-state entry indicates the NVMe driver has lost contact with the volume controller.

### Step 6: Check CloudTrail for the original API call and errors

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=ResourceName,AttributeValue=vol-0123456789abcdef0 \
  --start-time "$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[].{Time:EventTime,Name:EventName,ErrorCode:CloudTrailEvent}' \
  --output json
```

Expected output: `AttachVolume` or `DetachVolume` events; inspect the `CloudTrailEvent` JSON for `errorCode` fields such as `AttachmentLimitExceeded`, `IncorrectInstanceState`, or `RequestExpired` that reveal the API-layer cause.

## Causes

### Cause A: Filesystem still mounted when detach was requested

**Statement:** An EBS volume cannot detach while its filesystem remains mounted on the instance, leaving the volume permanently in `busy` or `detaching` state.

**Mechanism:** When the EC2 API receives a `DetachVolume` call for a mounted filesystem, the hypervisor signals the block device to drain I/O; however, the OS-level filesystem holds an exclusive reference, so the block device never fully drains, and the attachment state hangs at `detaching`. AWS describes this `busy` sub-state explicitly in `describe-volumes` output.

**Indicator:**

- [Step 3] `mount | grep` shows the device is still mounted (e.g., `/dev/xvdf on /mnt/data type xfs`)
- [Step 3] `lsof +D /mnt/data` or `fuser -vm /mnt/data` lists active PIDs with open file handles

<!-- match: {"step": 3, "predicate": "contains", "target": "/mnt/data"} -->

**Mitigation:**

- **Risk:** Killing processes with open file handles may cause data loss in those processes; sync first to flush pending writes.
- **Command:**

  ```bash
  sudo sync
  sudo fuser -km /mnt/data
  sudo umount /mnt/data
  aws ec2 detach-volume --volume-id vol-0123456789abcdef0
  ```

- **Duration:** Immediate after successful unmount.

**Resolution:**

```bash
# Permanent: always follow this sequence before any detach operation
sudo sync
sudo umount /mnt/data
aws ec2 detach-volume --volume-id vol-0123456789abcdef0
```

**Verification:** `aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 --query 'Volumes[].State'` returns `"available"` within 60 seconds of the detach call.

---

### Cause B: Underlying EC2 instance impairment blocking hypervisor operations

**Statement:** An impaired EC2 instance (failed system or instance status check) cannot complete volume attach or detach because the hypervisor layer is degraded.

**Mechanism:** EBS attachment and detachment are orchestrated between the EBS control plane and the instance's virtualization layer (hypervisor or Nitro card). When either EC2 status check fails, the Nitro controller or Xen hypervisor cannot reliably communicate with the EBS control plane, so the volume operation remains in `attaching` or `detaching` indefinitely without a host-side acknowledgment.

**Indicator:**

- [Step 2] `SystemCheck` or `InstanceCheck` shows `impaired`
- [Step 2] `Events` lists an `instance-retirement` or `system-maintenance` event
- [Step 4] Console output shows `nvme: I/O timeout` or kernel panics

<!-- match: {"step": 2, "predicate": "contains", "target": "impaired"} -->

**Mitigation:**

- **Risk:** Stopping the instance causes downtime; instance store data is lost; EIP is retained but public IP may change if not using EIP.
- **Command:**

  ```bash
  aws ec2 stop-instances --instance-ids i-0123456789abcdef0
  aws ec2 wait instance-stopped --instance-ids i-0123456789abcdef0
  aws ec2 start-instances --instance-ids i-0123456789abcdef0
  ```

- **Duration:** Stop/start takes 2–5 minutes; plan for application downtime.

**Resolution:**

```bash
# If stop/start fails due to retirement, terminate and launch a replacement:
aws ec2 create-image --instance-id i-0123456789abcdef0 --name "recovery-ami"
aws ec2 terminate-instances --instance-ids i-0123456789abcdef0
# Then launch new instance from the AMI and reattach the volume
```

- **Impact:** Stop/start migrates the instance to a new host, clearing the hypervisor state; if retirement is the underlying cause, a new instance is required.

- **Rollback:** N/A — stopping and starting is idempotent; if the new instance has issues, terminate it and relaunch from an earlier snapshot.

**Verification:** `aws ec2 describe-instance-status --instance-ids i-0123456789abcdef0` shows both status checks `ok`; `describe-volumes` shows the volume is no longer in `attaching`/`detaching`.

---

### Cause C: NVMe driver lost contact with volume controller (Nitro instances)

**Statement:** On Nitro-based EC2 instances the NVMe driver can lose its mapping to an EBS volume after a transient Nitro card error, leaving the attachment stuck without a device node visible to the OS.

**Mechanism:** Nitro instances expose EBS volumes as NVMe block devices via the Nitro card. If the Nitro card encounters a transient error or if the driver's internal state diverges from the EBS control plane (e.g., after a live migration), the OS NVMe driver may hold a stale queue handle that never completes I/O commands, causing attach/detach API calls to hang waiting for I/O drain that never arrives.

**Indicator:**

- [Step 5] `nvme list` does not show the expected volume (identified by serial number matching the volume ID)
- [Step 4] Console output contains `nvme: I/O timeout` or `nvme nvme0: controller is down`
- [Step 1] Volume `AttachState` is `attaching` and has not progressed in more than 10 minutes on a Nitro instance type

<!-- match: {"step": 5, "predicate": "absent", "target": "vol-"} -->
<!-- match: {"step": 4, "predicate": "contains", "target": "nvme: I/O timeout"} -->

**Mitigation:**

- **Risk:** Reloading the NVMe module (`modprobe -r nvme`) will cause all NVMe devices (including the root volume on Nitro-only-NVMe instances) to disappear momentarily — only safe on instances with a non-NVMe root or via an instance reboot.
- **Command:**

  ```bash
  # Attempt driver reload if root volume is NOT NVMe:
  sudo modprobe -r nvme_core
  sudo modprobe nvme_core
  # If driver reload is unsafe, reboot the instance:
  aws ec2 reboot-instances --instance-ids i-0123456789abcdef0
  ```

- **Duration:** Reboot takes 2–3 minutes; driver reload (when safe) is immediate.

**Resolution:** Same as Mitigation.

- **Impact:** Instance reboot causes application downtime; driver reload on NVMe-only-root instances would cause an unbootable state — always prefer reboot via AWS API.

- **Rollback:** N/A — reboot is idempotent; the Nitro card initializes fresh on next boot.

**Verification:** After reboot, `sudo nvme list` on the instance shows the volume's serial number; `aws ec2 describe-volumes` shows `AttachState: attached`.

---

### Cause D: Device name conflict at attachment time

**Statement:** An `AttachVolume` call targeting a device name already in use by another volume or instance store device causes the attachment to fail or hang.

**Mechanism:** The EC2 block device layer reserves device names at the hypervisor/Nitro level before signaling success to the control plane. When the requested device name (e.g., `/dev/sdf`) is already assigned to an existing volume, instance store, or a ghost device from a previous failed attachment, the reservation fails silently and the volume remains in `attaching` with no error propagated to the calling API.

**Indicator:**

- [Step 1] Volume `AttachState` is `attaching` with `Device: /dev/sdf` and the same device is visible in `lsblk` mapped to a different volume
- [Step 6] CloudTrail `AttachVolume` event contains `errorCode: AttachmentLimitExceeded` or the `Device` parameter matches a device already listed in `describe-instances` block device mappings

<!-- match: {"step": 6, "predicate": "contains", "target": "AttachmentLimitExceeded"} -->

**Mitigation:**

- **Risk:** Canceling and re-issuing with a different device name causes a brief delay; no data risk.
- **Command:**

  ```bash
  # Cancel: force-detach the stuck volume
  aws ec2 detach-volume --volume-id vol-0123456789abcdef0 --force
  # List currently used device names on the instance
  aws ec2 describe-instances --instance-ids i-0123456789abcdef0 \
    --query 'Reservations[].Instances[].BlockDeviceMappings[].DeviceName'
  # Re-attach with a free device name
  aws ec2 attach-volume \
    --volume-id vol-0123456789abcdef0 \
    --instance-id i-0123456789abcdef0 \
    --device /dev/sdg
  ```

- **Duration:** Force detach takes up to 2 minutes; reattach takes 30–60 seconds.

**Resolution:** Same as Mitigation.

**Verification:** `aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 --query 'Volumes[].Attachments[].State'` returns `"attached"`; `lsblk` on the instance shows the new device node.

---

### Cause E: Force detach required after prolonged stuck state

**Statement:** A volume that has been stuck in `detaching` for more than 15 minutes with no OS-level unmount path available requires a forced detach via the `--force` flag, risking data corruption.

**Mechanism:** When an instance is unresponsive (stopped, terminated, or crashed) but the EBS control plane still records an active attachment, the normal detach signal cannot be acknowledged by the instance. AWS provides a `--force` option that instructs the EBS control plane to mark the attachment as detached unilaterally, without draining OS-level I/O queues; any unflushed writes to the volume's buffer cache at the time of forced detach are lost.

**Indicator:**

- [Step 1] Volume `AttachState` has been `detaching` for more than 15 minutes
- [Step 2] `InstanceState` is `stopped`, `terminated`, or `shutting-down`
- [Step 4] Console output is empty or shows a crashed/panicked OS

<!-- match: {"step": 2, "predicate": "contains", "target": "stopped"} -->
<!-- match: {"step": 1, "predicate": "contains", "target": "detaching"} -->

**Mitigation:**

- **Risk:** Force detach can cause filesystem corruption if writes were in flight; always take an EBS snapshot before force detaching a volume with unconfirmed data integrity.
- **Command:**

  ```bash
  aws ec2 create-snapshot \
    --volume-id vol-0123456789abcdef0 \
    --description "Pre-force-detach recovery snapshot"
  # After snapshot reaches completed state:
  aws ec2 detach-volume --volume-id vol-0123456789abcdef0 --force
  ```

- **Duration:** Snapshot may take minutes to hours depending on volume size; force detach itself completes within 1–2 minutes.

**Resolution:** After force detach, run filesystem check before mounting:

```bash
# After reattaching to any instance:
sudo fsck -n /dev/xvdf        # ext2/ext3/ext4 — dry run
sudo xfs_repair -n /dev/xvdf  # XFS — dry run
# If filesystem errors found, run repair (without -n flag)
```

- **Impact:** Force detach is a one-time irreversible operation on the current attachment; the volume returns to `available` and can be reattached to any instance in the same AZ.

- **Rollback:** If filesystem corruption is confirmed after force detach, restore from the pre-force-detach snapshot.

**Verification:** `aws ec2 describe-volumes --volume-ids vol-0123456789abcdef0 --query 'Volumes[].State'` returns `"available"`; filesystem check exits with code 0 after repair.

---

### Cause F: API throttling stalling concurrent attach/detach operations

**Statement:** High-volume concurrent attach or detach operations in the same AWS account trigger EC2 API rate limiting, causing individual operations to stall without a clear error.

**Mechanism:** AWS EC2 imposes per-account and per-region rate limits on `AttachVolume` and `DetachVolume` API calls. Automation scripts that issue many concurrent volume operations (e.g., during fleet scaling or volume migration) can exhaust these limits; subsequent calls are throttled and the volume transitions to `attaching` or `detaching` then waits for retry, which may never arrive if the caller does not implement exponential backoff.

**Indicator:**

- [Step 6] CloudTrail contains `errorCode: RequestLimitExceeded` or `Throttling` on `AttachVolume`/`DetachVolume` events within the same 5-minute window
- [Step 6] Multiple volumes on the same account show `attaching`/`detaching` simultaneously

<!-- match: {"step": 6, "predicate": "contains", "target": "RequestLimitExceeded"} -->
<!-- match: {"step": 6, "predicate": "contains", "target": "Throttling"} -->

**Mitigation:**

- **Risk:** No data risk; serializing operations adds latency to the fleet operation.
- **Command:**

  ```bash
  # Check how many volumes are currently in transitional states
  aws ec2 describe-volumes \
    --filters "Name=status,Values=attaching,detaching" \
    --query 'length(Volumes[])' \
    --output text
  # Serialize remaining operations with waits
  for vol in vol-aaa vol-bbb vol-ccc; do
    aws ec2 attach-volume --volume-id "$vol" \
      --instance-id i-0123456789abcdef0 --device /dev/sdf
    aws ec2 wait volume-in-use --volume-ids "$vol"
    sleep 5
  done
  ```

- **Duration:** Serialized operations add 30–60 seconds per volume; acceptable for non-emergency fleet operations.

**Resolution:** Same as Mitigation. Implement exponential backoff with jitter in all volume-management automation; target fewer than 5 concurrent attach/detach calls per region.

**Verification:** CloudTrail shows no further `Throttling` errors; all volumes transition to `in-use` or `available` within expected time.

---

### Cause Z: Unidentified cause

**Statement:** The EBS volume has been stuck in `attaching` or `detaching` for more than 15 minutes and none of the diagnostic steps have identified a specific OS-level, instance-health, NVMe driver, device-conflict, or API-throttling cause.

**Mechanism:** N/A — root cause is undetermined. AWS infrastructure-side issues (e.g., availability zone hardware events, EBS fleet issues) can also cause stuck transitions and are not diagnosable from the customer side.

**Indicator:**

- [Default] All other causes have been excluded and the volume remains stuck

**Mitigation:**

- **Risk:** Force detach risks filesystem corruption; take a snapshot first.
- **Command:**

  ```bash
  # Last-resort: force detach after snapshotting
  aws ec2 create-snapshot \
    --volume-id vol-0123456789abcdef0 \
    --description "Force-detach escalation snapshot"
  aws ec2 detach-volume --volume-id vol-0123456789abcdef0 --force
  # Open an AWS Support case with the volume ID, instance ID, and timeline
  ```

- **Duration:** One-time; open support case in parallel.

**Resolution:** Out of runbook scope — escalate to AWS Support with the volume ID, instance ID, availability zone, timeline of events, and CloudTrail event IDs.

**Verification:** AWS Support confirms volume returns to `available` state; monitor with `aws ec2 describe-volumes` until state transitions.

## Prevention

1. **Always unmount before detaching.** Automate the three-step sequence in all tooling: `sync` → `umount` → `detach-volume`. Never issue a `DetachVolume` API call while the filesystem is mounted.

2. **Use systemd mount units with `RequiredBy=` shutdown ordering** so that EBS volumes are unmounted before the instance stops:

   ```ini
   # /etc/systemd/system/mnt-data.mount
   [Unit]
   Description=EBS Data Volume
   After=local-fs.target

   [Mount]
   What=/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0123456789abcdef0
   Where=/mnt/data
   Type=xfs
   Options=defaults,nofail

   [Install]
   WantedBy=multi-user.target
   ```

3. **Poll after every attach/detach API call** using `aws ec2 wait volume-in-use` or `wait volume-available`. Raise an alert if the wait exceeds 2 minutes:

   ```bash
   timeout 120 aws ec2 wait volume-in-use --volume-ids vol-0123456789abcdef0 \
     || echo "ALERT: volume did not reach in-use within 2 minutes"
   ```

4. **Serialize volume operations in automation.** Do not issue more than 5 concurrent `AttachVolume`/`DetachVolume` calls per region; add exponential backoff with jitter on `RequestLimitExceeded` errors.

5. **Set a CloudWatch alarm on VolumeQueueLength** to detect I/O saturation that may cause detach to hang:

   ```bash
   aws cloudwatch put-metric-alarm \
     --alarm-name EBSHighQueueLength \
     --metric-name VolumeQueueLength \
     --namespace AWS/EBS \
     --statistic Average \
     --period 300 \
     --threshold 10 \
     --comparison-operator GreaterThanThreshold \
     --dimensions Name=VolumeId,Value=vol-0123456789abcdef0 \
     --evaluation-periods 2 \
     --alarm-actions arn:aws:sns:us-east-1:123456789012:ops-alerts
   ```

6. **Never use EBS Multi-Attach (io1/io2) with non-cluster-aware filesystems.** ext4 and XFS do not support concurrent writes from multiple instances; use only with cluster filesystems (GFS2, OCFS2) or for specialized applications that implement their own locking.

7. **Create pre-detach EBS snapshots for critical volumes** before any planned detach, especially during instance migrations, to enable point-in-time recovery if force detach becomes necessary.

## Sources

- [Detach an Amazon EBS volume — AWS EC2 User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-detaching-volume.html) — unmount procedure, `busy` state description, force detach guidance (Priority 1)
- [Attach an Amazon EBS volume — AWS EC2 User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-attaching-volume.html) — attachment considerations, device naming, `AttachmentLimitExceeded` (Priority 1)
- [Troubleshoot Amazon EBS volumes — AWS EBS User Guide](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-troubleshooting.html) — volume status checks, stuck attachment states (Priority 1)
- [Amazon EBS volume status checks — AWS EC2 User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/monitoring-volume-status.html) — `VolumeQueueLength`, status check failures (Priority 1)
- [Amazon EBS and NVMe on Linux — AWS EC2 User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nvme-ebs-volumes.html) — NVMe device naming, serial number to volume ID mapping (Priority 1)
