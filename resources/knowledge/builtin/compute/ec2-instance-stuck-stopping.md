---
id: "ec2-instance-stuck-stopping"
title: "AWS EC2 Instance Stuck in Stopping State"
domain: compute
service: aws-ec2
symptom_class: [service_unavailable]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [aws, ec2, stopping, shutting-down, force-stop, ebs]
difficulty: intermediate
---

## Symptom Recognition

An EBS-backed EC2 instance remains in the `stopping` or `shutting-down` state for 15 or more minutes with no transition to `stopped` or `terminated`. The AWS Console shows no progress; `aws ec2 describe-instances` returns `"stopping"` without change. Normal stop, start, and terminate operations via Console or CLI have no effect. No charges accrue during `stopping`, but the instance and its attached EBS volumes and Elastic IPs remain locked and unavailable.

## Applicability

Applies to Amazon EBS-backed EC2 instances in any AWS region and any Linux or Windows AMI. Instance store-backed instances cannot be stopped — only terminated — so this runbook does not apply to them. Required IAM permissions: `ec2:DescribeInstances`, `ec2:DescribeInstanceStatus`, `ec2:DescribeVolumeStatus`, `ec2:GetConsoleOutput`, `ec2:StopInstances`, `ec2:TerminateInstances`, `ec2:CreateImage`, `ec2:CreateSnapshot`. AWS CLI v2 must be configured with valid credentials.

## Diagnostic Steps

### Step 1: Confirm Instance State and Elapsed Time

```bash
aws ec2 describe-instances --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].{State:State.Name,Reason:StateTransitionReason,RootDeviceType:RootDeviceType}' \
  --output table
```

Expected output: `State` is `stopping`, `Reason` contains a timestamp such as `User initiated (2026-05-12 10:00:00 GMT)`, `RootDeviceType` is `ebs`. If `RootDeviceType` is `instance-store`, termination is the only option.

### Step 2: Check System and Instance Status Checks

```bash
aws ec2 describe-instance-status --instance-ids i-0abc123def456789 \
  --include-all-instances \
  --query 'InstanceStatuses[0].{SystemStatus:SystemStatus.Status,InstanceStatus:InstanceStatus.Status,Events:Events}' \
  --output json
```

Expected output: `SystemStatus` and `InstanceStatus` values. A `failed` system status or a listed `instance-retirement` or `system-maintenance` event confirms underlying host failure. Absence of events suggests an OS-level hang rather than a hardware fault.

### Step 3: Check EBS Volume Health

```bash
VOLS=$(aws ec2 describe-instances --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[*].Ebs.VolumeId' \
  --output text)
aws ec2 describe-volume-status --volume-ids $VOLS \
  --query 'VolumeStatuses[*].{VolumeId:VolumeId,Status:VolumeStatus.Status}' \
  --output table
```

Expected output: All volumes show `Status: ok`. Any volume with `Status: impaired` indicates a storage-layer issue contributing to the stuck state.

### Step 4: Retrieve Console Output for Shutdown Clues

```bash
aws ec2 get-console-output --instance-id i-0abc123def456789 --latest --output text
```

Expected output: Last OS messages before the hang. Look for lines such as `A stop job is running for <service name>`, `Waiting for NFS`, `filesystem sync`, or no output at all (indicating the OS was already unresponsive).

### Step 5: Inspect Pre-Stop CPU and EBS I/O Metrics

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/EC2 \
  --metric-name CPUUtilization \
  --dimensions Name=InstanceId,Value=i-0abc123def456789 \
  --start-time $(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 --statistics Average --output table
```

Expected output: CPU utilization percentages for the last 2 hours. Sustained values above 90% before the stop was issued suggest the OS was overwhelmed and failed to respond to the ACPI shutdown signal within the timeout window.

## Causes

### Cause A: Underlying Host Hardware Failure

**Statement:** The EC2 hypervisor host has encountered an irrecoverable hardware fault that prevents it from completing the instance shutdown sequence.

**Mechanism:** When EC2 detects irreparable hardware failure, it may initiate a stop on the instance. If the physical host is unresponsive, the hypervisor cannot perform the clean EBS volume detach, leaving the instance in `stopping`. The system status check reports `failed` and AWS often schedules a retirement event for the affected host.

**Indicator:**

- [Step 2] `SystemStatus` is `failed`
- [Step 2] `Events` contains an `instance-retirement` or `system-maintenance` event

<!-- match: {"step": 2, "predicate": "contains", "target": "instance-retirement"} -->

**Mitigation:**

- **Risk:** Force stop may not succeed if the host itself is unresponsive; a second attempt with `--skip-os-shutdown` may be required.
- **Command:**

  ```bash
  aws ec2 stop-instances --instance-ids i-0abc123def456789 --force
  ```

- **Duration:** Allow 10 minutes; if not stopped, add `--skip-os-shutdown` on a second call.

**Resolution:**

```bash
aws ec2 stop-instances \
  --instance-ids i-0abc123def456789 \
  --force --skip-os-shutdown
```

- **Impact:** Force stop with skip OS shutdown is a hard power-off; EBS caches are not flushed. Run filesystem checks after restart. Single-instance blast radius.
- **Rollback:** Not applicable — this recovers a stuck instance; reverting to the stuck state is not possible.

**Verification:** Run `aws ec2 wait instance-stopped --instance-ids i-0abc123def456789` then `aws ec2 describe-instances --instance-ids i-0abc123def456789 --query 'Reservations[0].Instances[0].State.Name' --output text`. Output must be `stopped`. After restart, confirm both status checks are `ok`.

### Cause B: OS-Level Service Blocking Shutdown

**Statement:** A user-space service or process on the guest OS is not responding to the ACPI shutdown signal within the hypervisor's timeout window, preventing the instance from stopping.

**Mechanism:** When EC2 sends a stop request, it delivers an ACPI power-button event to the guest OS. The OS then runs its shutdown sequence, which stops systemd units in reverse dependency order. A single service with an unbounded `ExecStop` (such as a database dump script, NFS flush, or log-shipping agent) blocks the shutdown sequence indefinitely until the hypervisor times out. Console output captures the blocking service name.

**Indicator:**

- [Step 4] Console output contains `A stop job is running for` followed by a service name
- [Step 4] Console output contains `Waiting for NFS` or a network mount name
- [Step 5] CPU utilization was low before the stop, ruling out a compute-related timeout

<!-- match: {"step": 4, "predicate": "contains", "target": "A stop job is running for"} -->

**Mitigation:**

- **Risk:** Force stop bypasses the service's graceful shutdown; in-flight writes may be lost.
- **Command:**

  ```bash
  aws ec2 stop-instances --instance-ids i-0abc123def456789 --force
  ```

- **Duration:** Usually completes in 1–5 minutes.

**Resolution:**

```bash
# After restarting the instance, cap shutdown time for the offending service
sudo mkdir -p /etc/systemd/system/<service>.service.d/
cat <<'EOF' | sudo tee /etc/systemd/system/<service>.service.d/timeout.conf
[Service]
TimeoutStopSec=30
EOF
sudo systemctl daemon-reload
```

- **Impact:** Applies to a single systemd unit; requires daemon-reload but no restart. For all services, set `DefaultTimeoutStopSec=30s` in `/etc/systemd/system.conf.d/shutdown-timeout.conf`.
- **Rollback:** Remove the drop-in file and run `sudo systemctl daemon-reload`.

**Verification:** Stop and start the instance normally. Console output should no longer show `stop job is running` messages, and the instance should reach `stopped` within 3 minutes.

### Cause C: EBS Volume Impaired or Detach Blocked

**Statement:** An attached EBS volume is in an impaired state or has a large unflushed write backlog that prevents the hypervisor from safely detaching it during shutdown.

**Mechanism:** The EC2 stop sequence requires all attached EBS volumes to detach cleanly before the instance reaches `stopped`. If a volume is impaired due to hardware degradation on the storage backend, or if the I/O queue depth is very high (pending writes that cannot complete), the detach operation stalls. EBS volume status checks expose the impaired state; CloudWatch `VolumeQueueLength` reveals I/O saturation.

**Indicator:**

- [Step 3] Any volume shows `Status: impaired`
- [Step 5] Pre-stop `CPUUtilization` shows normal levels but EBS I/O metrics show high activity

<!-- match: {"step": 3, "predicate": "contains", "target": "impaired"} -->

**Mitigation:**

- **Risk:** Force stop does not wait for I/O to flush; impaired volumes may have corrupted data.
- **Command:**

  ```bash
  aws ec2 stop-instances --instance-ids i-0abc123def456789 --force
  ```

- **Duration:** Usually 1–5 minutes; if no change, add `--skip-os-shutdown`.

**Resolution:**

```bash
# After force stop, check EBS volume queue depth over the past 24 hours
aws cloudwatch get-metric-statistics \
  --namespace AWS/EBS \
  --metric-name VolumeQueueLength \
  --dimensions Name=VolumeId,Value=vol-0abc123def456789 \
  --start-time $(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 --statistics Average --output table
```

If sustained `VolumeQueueLength` exceeds 1, migrate to a gp3 or io2 volume type with higher provisioned IOPS to reduce queue buildup at shutdown.

- **Impact:** Volume type migration requires a snapshot, volume replacement, and instance restart. No data loss during migration if done from a stopped state.
- **Rollback:** Reattach the original volume if the migrated volume has issues.

**Verification:** After restart, confirm volume status is `ok` with `aws ec2 describe-volume-status --volume-ids <vol-id> --query 'VolumeStatuses[0].VolumeStatus.Status' --output text`. Run `sudo fsck -n /dev/xvda1` inside the instance to check filesystem integrity.

### Cause D: Instance Store Root Volume (Not Stoppable)

**Statement:** The instance has an instance store root volume, which cannot be stopped and will remain in `stopping` or `shutting-down` indefinitely if a graceful OS shutdown fails.

**Mechanism:** EC2 stop/start is only supported for EBS-backed instances. Instance store volumes are ephemeral and tied to the physical host; AWS does not support stopping them because there is no persistent root volume to preserve. Issuing a stop command on an instance store instance may leave it in `stopping` without resolution.

**Indicator:**

- [Step 1] `RootDeviceType` is `instance-store`

<!-- match: {"step": 1, "predicate": "contains", "target": "instance-store"} -->

**Mitigation:**

- **Risk:** Termination is permanent; all instance store data is lost. Ensure any needed data has been copied to S3 or EBS before proceeding.
- **Command:**

  ```bash
  aws ec2 terminate-instances --instance-ids i-0abc123def456789
  ```

- **Duration:** AWS guarantees eventual termination within a few hours even if the instance stalls in `shutting-down`.

**Resolution:** Same as Mitigation.

**Verification:** `aws ec2 describe-instances --instance-ids i-0abc123def456789 --query 'Reservations[0].Instances[0].State.Name' --output text` returns `terminated`.

### Cause Z: Unidentified

**Statement:** The instance is stuck in stopping state but the cause cannot be determined from the available diagnostic output.

**Mechanism:** Rare platform-level or hypervisor issues may produce a stuck stopping state without obvious indicators in console output, status checks, or volume health. AWS internal systems may resolve the condition automatically, or manual escalation to AWS Support may be required.

**Indicator:**

- [Default] All diagnostic steps above return normal results yet the instance remains stuck in `stopping`

**Mitigation:**

- **Risk:** Force stop may not resolve a platform-level issue; escalation to AWS Support may be needed.
- **Command:**

  ```bash
  aws ec2 stop-instances --instance-ids i-0abc123def456789 --force --skip-os-shutdown
  ```

- **Duration:** Wait 10 minutes. If still stuck, open an AWS Support case with the instance ID and steps taken.

**Resolution:** Out of runbook scope. If force stop fails after 10 minutes, submit a request to AWS re:Post or open a technical support case in the Support Center with the instance ID, region, and a description of steps taken.

**Verification:** `aws ec2 describe-instances --instance-ids i-0abc123def456789 --query 'Reservations[0].Instances[0].State.Name' --output text` returns `stopped`.

## Prevention

Configure systemd shutdown timeout globally to prevent services from blocking shutdown indefinitely:

```bash
sudo mkdir -p /etc/systemd/system.conf.d/
cat <<'EOF' | sudo tee /etc/systemd/system.conf.d/shutdown-timeout.conf
[Manager]
DefaultTimeoutStopSec=30s
EOF
sudo systemctl daemon-reload
```

For NFS or network-backed mounts, add the `_netdev` flag and a timeout in `/etc/fstab` so network filesystems unmount before network interfaces go down:

```text
server:/share /mnt/nfs nfs defaults,_netdev,timeo=30,retrans=3 0 0
```

Enable EC2 Auto Recovery so AWS migrates the instance to a healthy host on system status check failure, preserving instance ID, private IP, and EBS volumes:

```bash
aws ec2 modify-instance-attribute \
  --instance-id i-0abc123def456789 \
  --attribute autoRecovery \
  --value enabled
```

Automate daily EBS snapshots via Amazon Data Lifecycle Manager to enable rapid replacement if an instance becomes unrecoverable:

```bash
aws dlm create-lifecycle-policy \
  --description "Daily EC2 snapshots 7-day retention" \
  --state ENABLED \
  --execution-role-arn arn:aws:iam::123456789012:role/AWSDataLifecycleManagerDefaultRole \
  --policy-details file://dlm-policy.json
```

Create an EventBridge rule to alert when instances enter `stopping` state and remain there for more than 10 minutes:

```bash
aws events put-rule \
  --name ec2-stuck-stopping-alert \
  --event-pattern '{"source":["aws.ec2"],"detail-type":["EC2 Instance State-change Notification"],"detail":{"state":["stopping","shutting-down"]}}'
```

## Sources

- [AWS EC2: Troubleshoot Amazon EC2 instance stop issues](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/TroubleshootingInstancesStopping.html) — Priority 1. Official guide for stuck stopping state; force stop procedure, skip-OS-shutdown flag, replacement instance steps.
- [AWS EC2: Stop and Start Amazon EC2 Instances](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Stop_Start.html) — Priority 1. Instance stop lifecycle, graceful vs hard shutdown, EBS vs instance store distinctions.
- [AWS EC2: Methods for Stopping an Instance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-stop-methods.html) — Priority 1. Comparison table of default stop, skip-OS-shutdown, force stop, and force stop with skip OS shutdown; data impact for each method.
