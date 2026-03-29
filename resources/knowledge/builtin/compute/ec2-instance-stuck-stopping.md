---
id: ec2-instance-stuck-stopping
title: "AWS EC2 Instance Stuck in Stopping State: Diagnosis and Resolution"
domain: compute
service: aws-ec2
symptom_class:
  - service_unavailable
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - ec2
  - stopping
  - stuck
  - force-stop
  - termination
difficulty: intermediate
---

# AWS EC2 Instance Stuck in Stopping State: Diagnosis and Resolution

## Problem Definition

This runbook applies to EBS-backed Amazon EC2 instances in any AWS region. You need the AWS CLI v2 configured with credentials that have `ec2:DescribeInstances`, `ec2:StopInstances`, `ec2:TerminateInstances`, and `ec2:CreateSnapshot` permissions, or equivalent console access. Instance store-backed instances cannot be stopped, only terminated, so this runbook does not apply to them.

An instance remains in the `stopping` state for more than 15 minutes after a stop or terminate request. The instance cannot be started, modified, or terminated through normal means. While no charges accrue during the `stopping` state, the instance is unavailable, and its resources (Elastic IPs, EBS volumes) remain attached and potentially locked.

When you issue a stop command, AWS sends an ACPI shutdown signal to the instance, waits for the OS to flush EBS volume caches and metadata, then transitions the instance to `stopped`. The process stalls when the underlying host computer encounters a hardware or hypervisor fault, the guest OS is hung (a process ignoring SIGTERM, waiting on unresponsive NFS I/O, or stuck in a kernel deadlock), or EBS volume detach cannot complete due to a pending write backlog.

**Typical error presentation:**

```
$ aws ec2 describe-instances --instance-ids i-0abc123def456789 \
    --query 'Reservations[0].Instances[0].State.Name'
"stopping"
```

The instance has been in `stopping` for 15+ minutes with no progress in the console.

## Diagnostic Steps

### Step 1: Confirm Instance State and How Long It Has Been Stopping

**What this checks:** Whether the instance is genuinely stuck and how long ago the stop was initiated.

```bash
aws ec2 describe-instances --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].{State:State.Name,StateReason:StateTransitionReason,LaunchTime:LaunchTime}' \
  --output table
```

**Expected output:** The `State` field shows `stopping`. The `StateTransitionReason` may contain a timestamp such as `User initiated (2026-03-26 10:00:00 GMT)`.

**What the finding means:** An instance stuck for 15+ minutes will not recover on its own. Proceed to force stop (Mitigation Option 1). If it has been less than 15 minutes, wait and re-check before taking action.

### Step 2: Verify Root Device Type Is EBS

**What this checks:** Whether this instance can be stopped at all. Only EBS-backed instances support stop/start.

```bash
aws ec2 describe-instances --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].{InstanceType:InstanceType,RootDeviceType:RootDeviceType,EbsOptimized:EbsOptimized}' \
  --output table
```

**Expected output:** `RootDeviceType` is `ebs`.

**What the finding means:** If `RootDeviceType` is `instance-store`, the instance cannot be stopped. Termination is the only option for instance store-backed instances stuck in `shutting-down`.

### Step 3: Check EBS Volume Health

**What this checks:** Whether the attached EBS volumes have impaired status, which can prevent clean shutdown.

```bash
# List attached volume IDs
VOLS=$(aws ec2 describe-instances --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[*].Ebs.VolumeId' --output text)

# Check each volume
aws ec2 describe-volume-status --volume-ids $VOLS \
  --query 'VolumeStatuses[*].{VolumeId:VolumeId,Status:VolumeStatus.Status}' --output table
```

**Expected output:** All volumes show `Status: ok`.

**What the finding means:** A volume with `impaired` status indicates underlying storage issues that may be contributing to the stuck state. This is an AWS infrastructure problem, not something you can fix from the OS side.

### Step 4: Check for AWS Scheduled Events

**What this checks:** Whether AWS has scheduled maintenance or retirement events for the underlying host.

```bash
aws ec2 describe-instance-status --instance-ids i-0abc123def456789 \
  --include-all-instances \
  --query 'InstanceStatuses[0].{SystemStatus:SystemStatus.Status,InstanceStatus:InstanceStatus.Status,Events:Events}' \
  --output json
```

**Expected output:** No events listed, or events such as `instance-retirement` or `system-maintenance`.

**What the finding means:** Scheduled events like `instance-retirement` confirm the underlying host has a problem. The instance was likely unhealthy before the stop was initiated. Force stop and replacement are the correct path.

### Step 5: Review Console Output for Shutdown Clues

**What this checks:** Whether the OS left any messages indicating what is blocking shutdown.

```bash
aws ec2 get-console-output --instance-id i-0abc123def456789 --latest --output text
```

**Expected output:** Shutdown messages, filesystem sync operations, or service stop failures.

**What the finding means:** Messages like `A stop job is running for...` or `Waiting for NFS mount` identify the OS-level process blocking shutdown. This informs the Root Cause Resolution after recovery.

### Step 6: Check Pre-Stop CloudWatch Metrics

**What this checks:** Whether the instance was under heavy load before the stop was issued.

```bash
aws cloudwatch get-metric-statistics --namespace AWS/EC2 \
  --metric-name CPUUtilization --dimensions Name=InstanceId,Value=i-0abc123def456789 \
  --start-time $(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 --statistics Average --output table
```

**Expected output:** CPU utilization percentages over the last 2 hours.

**What the finding means:** Sustained high CPU (>90%) or high EBS write activity before the stop suggests the OS was busy and may have been slow to respond to the ACPI shutdown signal. This is consistent with a hung shutdown rather than a host failure.

## Mitigation

### Option 1: Force Stop the Instance

The primary resolution. Force stop sends a hard shutdown signal, bypassing the graceful OS shutdown sequence.

- **Risk:** Medium. File system caches may not be flushed, which can cause data inconsistency on EBS volumes. Run `fsck` on the root volume after restarting.
- **Command:**
  ```bash
  aws ec2 stop-instances --instance-ids i-0abc123def456789 --force
  ```
  To bypass the OS shutdown signal entirely (immediate power-off):
  ```bash
  aws ec2 stop-instances --instance-ids i-0abc123def456789 --force --skip-os-shutdown
  ```
- **Verify:**
  ```bash
  aws ec2 wait instance-stopped --instance-ids i-0abc123def456789 && \
  aws ec2 describe-instances --instance-ids i-0abc123def456789 \
    --query 'Reservations[0].Instances[0].State.Name' --output text
  ```
  Output should be `stopped`.
- **Duration:** Usually completes within 1-5 minutes. If no change after 10 minutes, proceed to Option 2.

### Option 2: Force Terminate and Replace

Use when force stop fails and the instance is expendable or replaceable from an AMI.

- **Risk:** High. The instance is permanently destroyed. EBS volumes with `DeleteOnTermination=true` are deleted. Take snapshots of all critical volumes first.
- **Command:**
  ```bash
  # Snapshot all attached volumes first
  for VOL in $(aws ec2 describe-instances --instance-ids i-0abc123def456789 \
    --query 'Reservations[0].Instances[0].BlockDeviceMappings[*].Ebs.VolumeId' --output text); do
    aws ec2 create-snapshot --volume-id $VOL --description "Pre-termination snapshot $(date +%Y%m%d)"
  done

  # Terminate
  aws ec2 terminate-instances --instance-ids i-0abc123def456789
  ```
- **Verify:**
  ```bash
  aws ec2 describe-instances --instance-ids i-0abc123def456789 \
    --query 'Reservations[0].Instances[0].State.Name' --output text
  ```
  State should transition to `shutting-down` and then `terminated`. AWS guarantees eventual termination.
- **Duration:** Usually 1-10 minutes. AWS will force termination within a few hours if the instance stalls in `shutting-down`.

### Option 3: Create Replacement Instance from AMI or Snapshots

Use when the workload must be restored immediately and the original instance is unrecoverable.

- **Risk:** Medium. Data is only as recent as the latest snapshot or AMI. Configuration must be re-applied if not captured in the AMI.
- **Command:**
  ```bash
  # Create an AMI from the stuck instance (--no-reboot since instance is stuck)
  AMI_ID=$(aws ec2 create-image --instance-id i-0abc123def456789 \
    --name "recovery-$(date +%Y%m%d-%H%M)" --no-reboot --query 'ImageId' --output text)
  echo "AMI created: $AMI_ID"

  # Wait for AMI to become available, then launch replacement
  aws ec2 wait image-available --image-ids $AMI_ID
  aws ec2 run-instances --image-id $AMI_ID --instance-type m5.large \
    --key-name my-key --subnet-id subnet-abc123 --security-group-ids sg-abc123
  ```
- **Verify:**
  ```bash
  aws ec2 describe-instance-status --instance-ids i-NEW_INSTANCE_ID \
    --query 'InstanceStatuses[0].{System:SystemStatus.Status,Instance:InstanceStatus.Status}' --output table
  ```
  Both status checks should report `ok`.
- **Duration:** 5-15 minutes for AMI creation, 3-5 minutes for instance launch.

## Root Cause Resolution

**If** the console output shows shutdown blocked by a specific service (e.g., `A stop job is running for My Database Service`) **then** configure systemd to enforce a shutdown timeout for that service:

```bash
# After restarting the instance
sudo mkdir -p /etc/systemd/system/myservice.service.d/
cat <<'EOF' | sudo tee /etc/systemd/system/myservice.service.d/timeout.conf
[Service]
TimeoutStopSec=30
EOF
sudo systemctl daemon-reload
```

To set a global default for all services:

```bash
sudo mkdir -p /etc/systemd/system.conf.d/
cat <<'EOF' | sudo tee /etc/systemd/system.conf.d/shutdown-timeout.conf
[Manager]
DefaultTimeoutStopSec=30s
EOF
sudo systemctl daemon-reload
```

**If** the console output shows NFS or network mount timeout during shutdown **then** add the `_netdev` mount option so network filesystems unmount before network interfaces go down:

```bash
# In /etc/fstab, change:
#   server:/share /mnt/nfs nfs defaults 0 0
# To:
#   server:/share /mnt/nfs nfs defaults,_netdev,timeo=30,retrans=3 0 0
```

**If** the instance was stuck due to an AWS host failure (system status check was `impaired` before stop, or scheduled retirement event existed) **then** this is an AWS infrastructure issue. After recovery, use spread placement to reduce single-host exposure:

```bash
aws ec2 create-placement-group --group-name spread-production \
  --strategy spread --spread-level host
```

**If** the instance has recurring stuck-stopping episodes and volume health is degraded **then** investigate the root volume performance:

```bash
# After restarting, check filesystem health
sudo fsck -n /dev/xvda1

# Check EBS I/O queue depth over the past day
aws cloudwatch get-metric-statistics --namespace AWS/EBS \
  --metric-name VolumeQueueLength --dimensions Name=VolumeId,Value=vol-0abc123def456789 \
  --start-time $(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 --statistics Average --output table
```

A sustained `VolumeQueueLength` above 1 indicates I/O contention. Upgrade to gp3 or io2 volumes for better baseline performance.

## Verification

After force stopping and restarting the instance (or launching a replacement):

```bash
# Confirm instance is running and passing both status checks
aws ec2 describe-instance-status --instance-ids i-0abc123def456789 \
  --query 'InstanceStatuses[0].{State:InstanceState.Name,System:SystemStatus.Status,Instance:InstanceStatus.Status}' \
  --output table
```

Both `System` and `Instance` status checks should report `ok`.

```bash
# Check for filesystem corruption (run on the instance after restart)
sudo dmesg | grep -iE 'error|corrupt|fsck|ext4-fs'
sudo journalctl -b -p err --no-pager | head -30
```

No filesystem corruption messages should appear. If they do, run `sudo fsck /dev/xvda1` (after unmounting or from a rescue instance).

```bash
# Verify EBS volume health post-recovery
aws ec2 describe-volume-status --volume-ids vol-0abc123def456789 \
  --query 'VolumeStatuses[0].VolumeStatus.Status' --output text
```

Output should be `ok`.

## Prevention

### Configure Graceful Shutdown Timeouts

Set systemd `DefaultTimeoutStopSec=30s` globally. Review all services in `/etc/systemd/system/` and ensure none have unbounded `ExecStop` directives that perform lengthy operations such as database dumps, remote API calls, or log shipping.

### Enable EC2 Auto Recovery

Auto Recovery migrates the instance to a healthy host when system status checks fail, preserving the instance ID, private IP, Elastic IP, and EBS volumes:

```bash
aws ec2 modify-instance-attribute --instance-id i-0abc123def456789 \
  --attribute autoRecovery --value enabled
```

### Automate EBS Snapshots

Use Amazon Data Lifecycle Manager (DLM) for automated daily snapshots so you can quickly launch a replacement instance if the original becomes unrecoverable:

```bash
aws dlm create-lifecycle-policy --description "Daily EC2 snapshots, 7-day retention" \
  --state ENABLED \
  --execution-role-arn arn:aws:iam::123456789012:role/AWSDataLifecycleManagerDefaultRole \
  --policy-details file://dlm-policy.json
```

### Monitor Instance State Transitions

Set up an EventBridge rule to alert when instances enter `stopping` or `shutting-down` states. Combine with a Lambda function or SNS topic to notify the operations team if the state persists for more than 10 minutes:

```bash
aws events put-rule --name ec2-stuck-stopping-alert \
  --event-pattern '{"source":["aws.ec2"],"detail-type":["EC2 Instance State-change Notification"],"detail":{"state":["stopping","shutting-down"]}}'
```

### Avoid Long-Running Shutdown Scripts

Review `/etc/rc0.d/`, `/etc/rc6.d/` (SysV), and systemd `ExecStop` directives. Remove or optimize shutdown scripts that perform large data transfers, remote API calls, or synchronous log shipping.

## Sources

- [AWS EC2: Troubleshoot Stopping Your Instance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/TroubleshootingInstancesStopping.html) - Official guide for instances stuck in stopping state, including force stop and `--skip-os-shutdown` procedures.
- [AWS EC2: Stop and Start Instances](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Stop_Start.html) - Instance lifecycle documentation covering stop, start, and force stop behaviors.
- [AWS EC2: Instance Stop Methods](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-stop-methods.html) - Detailed reference for force stop, skip-os-shutdown, and console vs CLI methods.
- [AWS EBS: Volume Status Checks](https://docs.aws.amazon.com/ebs/latest/userguide/monitoring-volume-status.html) - EBS volume health monitoring and impaired volume recovery.
