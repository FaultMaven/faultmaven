---
id: "ec2-instance-unreachable"
title: "AWS EC2 Instance Unreachable via SSH or RDP"
domain: compute
service: aws-ec2
symptom_class: [connection_refused, timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [aws, ec2, ssh, rdp, security-group, network-acl, routing, key-pair, ssm]
difficulty: intermediate
---

## Symptom Recognition

- `ssh: connect to host ... port 22: Connection timed out` when connecting to instance
- `ssh: connect to host ... port 22: Connection refused`
- `Permission denied (publickey)` returned immediately after TCP handshake succeeds
- `WARNING: UNPROTECTED PRIVATE KEY FILE!` followed by `Permission denied (publickey)`
- `No route to host` or `Network error: Connection timed out` in PuTTY/SSH clients
- RDP connection to port 3389 times out or is refused on Windows instances
- Instance shows `running` state in EC2 console but remote session cannot be established
- AWS EC2 instance status checks show `ok`/`ok` yet the instance remains unreachable over the network

## Applicability

Applies to all Amazon EC2 Linux and Windows instances in any AWS region and VPC configuration. Requires AWS CLI v2 with `ec2:Describe*`, `ec2:DescribeSecurityGroupRules`, `ec2:GetConsoleOutput`, and `ssm:DescribeInstanceInformation` permissions. The SSH private key (.pem) for the instance key pair must be available for key-pair-related causes. For Windows instances, substitute SSH (port 22) with RDP (port 3389) throughout. AMI-specific default usernames: `ec2-user` (Amazon Linux, RHEL, SUSE, Oracle, Fedora), `ubuntu` (Ubuntu), `admin` (Debian), `rocky` (Rocky Linux), `bitnami` (Bitnami).

## Diagnostic Steps

### Step 1: Verify instance state and status checks

```bash
aws ec2 describe-instance-status \
  --instance-ids i-0abc123def456789 \
  --query 'InstanceStatuses[0].{State:InstanceState.Name,System:SystemStatus.Status,Instance:InstanceStatus.Status}' \
  --output table
```

Expected output: `State: running`, `System: ok`, `Instance: ok`. If the instance is not running, start it. If status is `impaired`, proceed to Step 6.

### Step 2: Confirm the instance has a routable public IP address

```bash
aws ec2 describe-instances \
  --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].{PublicIp:PublicIpAddress,PrivateIp:PrivateIpAddress,SubnetId:SubnetId,VpcId:VpcId}' \
  --output table
```

Expected output: `PublicIp` field contains a non-null IP address. A null value means the instance is in a private subnet or has no Elastic IP.

### Step 3: Check security group inbound rules for SSH/RDP access

```bash
SG_IDS=$(aws ec2 describe-instances \
  --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].SecurityGroups[*].GroupId' \
  --output text)

for SG in $SG_IDS; do
  echo "=== Security Group: $SG ==="
  aws ec2 describe-security-group-rules \
    --filters "Name=group-id,Values=$SG" \
    --query 'SecurityGroupRules[?IsEgress==`false`].{Port:FromPort,ToPort:ToPort,Protocol:IpProtocol,Source:CidrIpv4}' \
    --output table
done
```

Expected output: A rule with `Port: 22` (or `3389` for RDP), `Protocol: tcp`, and a `Source` CIDR that includes your current public IP address.

### Step 4: Verify network ACL allows SSH traffic (inbound and outbound ephemeral)

```bash
SUBNET=$(aws ec2 describe-instances \
  --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].SubnetId' \
  --output text)

NACL=$(aws ec2 describe-network-acls \
  --filters "Name=association.subnet-id,Values=$SUBNET" \
  --query 'NetworkAcls[0].NetworkAclId' \
  --output text)

echo "=== Inbound Rules ==="
aws ec2 describe-network-acls --network-acl-ids "$NACL" \
  --query 'NetworkAcls[0].Entries[?Egress==`false`].{Rule:RuleNumber,Action:RuleAction,Protocol:Protocol,Port:PortRange,CIDR:CidrBlock}' \
  --output table

echo "=== Outbound Rules ==="
aws ec2 describe-network-acls --network-acl-ids "$NACL" \
  --query 'NetworkAcls[0].Entries[?Egress==`true`].{Rule:RuleNumber,Action:RuleAction,Protocol:Protocol,Port:PortRange,CIDR:CidrBlock}' \
  --output table
```

Expected output: Inbound — an ALLOW rule for TCP port 22 from your source CIDR with a lower rule number than any DENY for the same range. Outbound — an ALLOW rule for TCP ports 1024–65535 to your CIDR.

### Step 5: Confirm route table has an internet gateway route

```bash
SUBNET=$(aws ec2 describe-instances \
  --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].SubnetId' \
  --output text)

aws ec2 describe-route-tables \
  --filters "Name=association.subnet-id,Values=$SUBNET" \
  --query 'RouteTables[0].Routes[*].{Destination:DestinationCidrBlock,Target:GatewayId,State:State}' \
  --output table
```

Expected output: A route with `Destination: 0.0.0.0/0`, `Target: igw-xxxxxxxx`, and `State: active`.

### Step 6: Read console output for OS-level boot and sshd errors

```bash
aws ec2 get-console-output \
  --instance-id i-0abc123def456789 \
  --latest \
  --output text | tail -80
```

Expected output: Boot sequence ending with a login prompt or `sshd` start confirmation such as `Started OpenBSD Secure Shell server`. Kernel panics, filesystem mount failures, or `sshd: error` lines indicate OS-level causes.

### Step 7: Test SSH verbosely to isolate handshake failure point

```bash
ssh -vvv -i /path/to/key.pem \
  -o ConnectTimeout=15 \
  -o StrictHostKeyChecking=no \
  ec2-user@<EC2_PUBLIC_IP> 2>&1 | head -80
```

Expected output: Verbose lines progressing through key exchange and authentication. Stalling at `Connecting to...` means network-level block. Reaching `Permission denied (publickey)` means the network is reachable but auth fails.

### Step 8: Verify key pair name and private key file permissions

```bash
# Confirm which key pair the instance expects
aws ec2 describe-instances \
  --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].KeyName' \
  --output text

# Check local key file permissions (must be 400 or 600)
ls -la /path/to/key.pem
```

Expected output: Key name matches the file you are using. File permissions show `-r--------` (400) or `-rw-------` (600). Permissions of `0777` or group/other readable trigger `WARNING: UNPROTECTED PRIVATE KEY FILE!` and SSH ignores the key.

## Causes

### Cause A: Security group missing inbound SSH/RDP rule for source IP

**Statement:** The EC2 instance's security group has no inbound rule permitting TCP port 22 (or 3389) from the operator's current public IP address.

**Mechanism:** Security groups act as stateful firewalls at the instance level; traffic not explicitly allowed by an inbound rule is silently dropped at the hypervisor before reaching the instance. Because security group changes are applied dynamically without reboot, the instance appears running but all connection attempts time out since no TCP SYN-ACK is returned.

**Indicator:**

- [Step 3] No rule with `Port: 22` exists, or the `Source` CIDR does not contain the operator's current IP
- [Symptom] Connection times out (not refused) — the packet is dropped, not rejected

<!-- match: {"step": 3, "predicate": "absent", "target": "22"} -->

**Mitigation:**

- **Risk:** Adding `0.0.0.0/0` as source exposes the port to the internet; scope rule to operator IP.
- **Command:**

  ```bash
  MY_IP=$(curl -s https://checkip.amazonaws.com)
  SG_ID=$(aws ec2 describe-instances \
    --instance-ids i-0abc123def456789 \
    --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' \
    --output text)
  aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --protocol tcp \
    --port 22 \
    --cidr "${MY_IP}/32"
  ```

- **Duration:** Immediate; security group changes apply within seconds.

**Resolution:**

```bash
# Permanent: create a dedicated bastion/admin security group scoped to corporate CIDR
aws ec2 create-security-group \
  --group-name admin-ssh-access \
  --description "SSH from corporate network only" \
  --vpc-id vpc-abc123
aws ec2 authorize-security-group-ingress \
  --group-id sg-NEWGROUP \
  --protocol tcp --port 22 \
  --cidr 203.0.113.0/24
# Reference this SG from instance SGs via source-sg rule instead of CIDR
```

- **Impact:** Cluster-wide if multiple instances reference the same SG; no restart required.
- **Rollback:** `aws ec2 revoke-security-group-ingress --group-id sg-ID --protocol tcp --port 22 --cidr <cidr>`

**Verification:** Run `ssh -o ConnectTimeout=10 -i key.pem ec2-user@<IP> echo OK` — should print `OK` within 5 seconds.

### Cause B: Network ACL DENY rule blocks SSH before security group is evaluated

**Statement:** A subnet-level network ACL has a DENY rule with a lower rule number than the ALLOW rule for SSH, or is missing the outbound ephemeral port ALLOW required for response traffic.

**Mechanism:** NACLs are stateless and evaluated in ascending rule-number order; the first matching rule wins. Unlike security groups, NACLs must explicitly allow both inbound TCP/22 and outbound ephemeral ports (1024–65535) for the response packets. A misplaced DENY rule or a missing outbound rule causes the TCP handshake to be silently dropped or the SSH response stream to be cut, manifesting as a connection timeout identical to a security group miss.

**Indicator:**

- [Step 4] Inbound rules show a DENY with rule number lower than the ALLOW for port 22, OR outbound rules have no ALLOW for ports 1024–65535
- [Symptom] Timeout persists even after confirming security group allows port 22

<!-- match: {"step": 4, "predicate": "contains", "target": "DENY"} -->

**Mitigation:**

- **Risk:** Modifying NACLs affects all instances in the subnet simultaneously.
- **Command:**

  ```bash
  # Add inbound ALLOW for port 22 at a rule number lower than any DENY
  aws ec2 create-network-acl-entry \
    --network-acl-id acl-abc123 \
    --ingress \
    --rule-number 90 \
    --protocol tcp \
    --rule-action allow \
    --cidr-block 203.0.113.5/32 \
    --port-range From=22,To=22

  # Add outbound ALLOW for ephemeral ports
  aws ec2 create-network-acl-entry \
    --network-acl-id acl-abc123 \
    --egress \
    --rule-number 90 \
    --protocol tcp \
    --rule-action allow \
    --cidr-block 203.0.113.5/32 \
    --port-range From=1024,To=65535
  ```

- **Duration:** Immediate; NACL changes apply within seconds.

**Resolution:** **Same as Mitigation.** For long-term management, document NACL rules in IaC (Terraform/CloudFormation) to prevent unreviewed manual changes.

**Verification:** Re-run Step 4 and confirm no lower-numbered DENY precedes the ALLOW. Then attempt SSH connection — it should complete within 10 seconds.

### Cause C: Subnet route table missing internet gateway route (private subnet)

**Statement:** The instance's subnet has no route for `0.0.0.0/0` pointing to an internet gateway, making the subnet private and the instance unreachable from the internet even if a public IP is assigned.

**Mechanism:** A public IP assigned to an EC2 instance is only reachable if the subnet's route table directs internet-bound traffic to an internet gateway (IGW). Without this route, traffic from external clients reaches the VPC boundary but has no path to the instance; the instance's return traffic also has no internet-facing path, causing complete TCP handshake failure and connection timeout.

**Indicator:**

- [Step 5] No route with `Destination: 0.0.0.0/0` and `Target: igw-*` exists in the route table
- [Step 2] Instance has a public IP assigned (non-null `PublicIp`) yet connections time out

<!-- match: {"step": 5, "predicate": "absent", "target": "igw-"} -->

**Mitigation:**

- **Risk:** Making a subnet public exposes all instances in it to internet traffic filtered only by security groups; review all SG rules before adding IGW route.
- **Command:**

  ```bash
  # Use SSM Session Manager as immediate workaround (no IGW needed)
  aws ssm start-session --target i-0abc123def456789
  ```

- **Duration:** SSM workaround is immediate if SSM agent is running and IAM role is attached.

**Resolution:**

```bash
# Attach an IGW if none exists for the VPC
IGW=$(aws ec2 create-internet-gateway --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --internet-gateway-id "$IGW" --vpc-id vpc-abc123

# Add the default route to the subnet's route table
RTB=$(aws ec2 describe-route-tables \
  --filters "Name=association.subnet-id,Values=subnet-abc123" \
  --query 'RouteTables[0].RouteTableId' --output text)
aws ec2 create-route \
  --route-table-id "$RTB" \
  --destination-cidr-block 0.0.0.0/0 \
  --gateway-id "$IGW"
```

- **Impact:** Subnet-wide; all instances in the subnet gain internet reachability (filtered by their SGs). Requires no instance restart.
- **Rollback:** `aws ec2 delete-route --route-table-id rtb-ID --destination-cidr-block 0.0.0.0/0`

**Verification:** Re-run Step 5 — confirm `0.0.0.0/0 → igw-*` route appears with `State: active`. Then attempt SSH.

### Cause D: Instance has no public IP address (non-Elastic IP lost on stop/start)

**Statement:** The instance was stopped and restarted, causing its non-Elastic public IP to be released and not reassigned, leaving it with only a private IP.

**Mechanism:** EC2 assigns ephemeral public IPs only to instances launched in subnets with auto-assign public IP enabled; these IPs are released on stop (not on reboot). After a stop/start cycle, a new public IP may or may not be assigned depending on subnet settings and available addresses. Instances without a public IP are only reachable via VPN, Direct Connect, bastion host, or SSM Session Manager.

**Indicator:**

- [Step 2] `PublicIp` field is null in `describe-instances` output
- [Symptom] Instance was reachable before a stop/start operation and is now unreachable

<!-- match: {"step": 2, "predicate": "absent", "target": "PublicIp"} -->

**Mitigation:**

- **Risk:** Low; allocating and associating an Elastic IP is non-disruptive.
- **Command:**

  ```bash
  # Immediate access via SSM (no public IP required)
  aws ssm start-session --target i-0abc123def456789
  ```

- **Duration:** Immediate.

**Resolution:**

```bash
# Allocate and associate an Elastic IP for stable persistent addressing
EIP_ALLOC=$(aws ec2 allocate-address \
  --domain vpc \
  --query 'AllocationId' \
  --output text)
aws ec2 associate-address \
  --instance-id i-0abc123def456789 \
  --allocation-id "$EIP_ALLOC"
echo "Elastic IP allocated: $EIP_ALLOC"
```

- **Impact:** Single instance. EIPs have a small hourly charge when not associated; associate immediately.
- **Rollback:** `aws ec2 disassociate-address --association-id eipassoc-ID && aws ec2 release-address --allocation-id eipalloc-ID`

**Verification:** Re-run Step 2 — `PublicIp` should now show the Elastic IP. Attempt SSH to that IP.

### Cause E: Wrong SSH key file or wrong AMI username

**Statement:** The SSH connection fails at the authentication stage because the private key file does not match the instance's authorized key or the AMI-specific username is incorrect.

**Mechanism:** EC2 instances store the public key in `/home/<user>/.ssh/authorized_keys` for the AMI's default user at launch time. If the wrong `.pem` file is used, the SSH client sends a public key that does not match any authorized key and authentication fails immediately. Similarly, SSH does not attempt password fallback by default, so a wrong username (e.g., using `ec2-user` on an Ubuntu instance instead of `ubuntu`) results in an immediate `Permission denied (publickey)`.

**Indicator:**

- [Step 7] Verbose output shows `Permission denied (publickey)` after key exchange succeeds (network path is fine)
- [Step 8] Key pair name in console does not match the filename of the `.pem` file being used

<!-- match: {"step": 7, "predicate": "contains", "target": "Permission denied (publickey)"} -->

**Mitigation:**

- **Risk:** Low; trying the correct key/username does not modify the instance.
- **Command:**

  ```bash
  # Confirm key pair name expected by instance
  aws ec2 describe-instances \
    --instance-ids i-0abc123def456789 \
    --query 'Reservations[0].Instances[0].KeyName' \
    --output text

  # Try correct AMI username (ubuntu for Ubuntu, admin for Debian, ec2-user for Amazon Linux)
  ssh -vvv -i /path/to/correct-key.pem ubuntu@<EC2_PUBLIC_IP>
  ```

- **Duration:** Immediate.

**Resolution:**

```bash
# If original key is lost, recover access via rescue instance procedure:
# 1. Stop the instance
aws ec2 stop-instances --instance-ids i-0abc123def456789
aws ec2 wait instance-stopped --instance-ids i-0abc123def456789

# 2. Detach root volume and attach to a rescue instance
ROOT_VOL=$(aws ec2 describe-instances \
  --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' \
  --output text)
aws ec2 detach-volume --volume-id "$ROOT_VOL"
aws ec2 attach-volume --volume-id "$ROOT_VOL" \
  --instance-id i-RESCUE --device /dev/sdf

# 3. On rescue instance: mount volume and replace authorized_keys
# sudo mkdir /mnt/rescue && sudo mount /dev/xvdf1 /mnt/rescue
# ssh-keygen -t rsa -b 4096 -f ~/.ssh/new-key
# sudo cp ~/.ssh/new-key.pub /mnt/rescue/home/ec2-user/.ssh/authorized_keys
# sudo chmod 600 /mnt/rescue/home/ec2-user/.ssh/authorized_keys
# sudo chmod 700 /mnt/rescue/home/ec2-user/.ssh
# sudo umount /mnt/rescue

# 4. Reattach to original instance and start
aws ec2 detach-volume --volume-id "$ROOT_VOL"
aws ec2 attach-volume --volume-id "$ROOT_VOL" \
  --instance-id i-0abc123def456789 --device /dev/xvda
aws ec2 start-instances --instance-ids i-0abc123def456789
```

**Verification:** `ssh -i new-key.pem ec2-user@<IP> echo Connected` — returns `Connected`.

### Cause F: Private key file has insecure permissions (SSH ignores it)

**Statement:** The SSH client refuses to use the private key file because its filesystem permissions allow read access to group or other users, triggering the `UNPROTECTED PRIVATE KEY FILE` error.

**Mechanism:** OpenSSH enforces strict key file permissions as a security control; if the `.pem` file is readable by anyone other than the owner (permissions wider than 0600), SSH silently ignores the key and authentication fails with `Permission denied (publickey)`. The warning message explicitly states the permissions and the file path.

**Indicator:**

- [Step 7] Output contains `WARNING: UNPROTECTED PRIVATE KEY FILE!` and shows permissions like `0644` or `0777`
- [Step 8] `ls -la` on the key file shows group or other read/write bits set

<!-- match: {"step": 7, "predicate": "contains", "target": "UNPROTECTED PRIVATE KEY FILE"} -->

**Mitigation:**

- **Risk:** None; restricting file permissions has no functional side effects.
- **Command:**

  ```bash
  chmod 400 /path/to/key.pem
  ssh -i /path/to/key.pem ec2-user@<EC2_PUBLIC_IP>
  ```

- **Duration:** Immediate.

**Resolution:** **Same as Mitigation.**

**Verification:** `ls -la /path/to/key.pem` shows `-r--------` (400). SSH connects without the unprotected key warning.

### Cause G: SSH daemon not running or crashed inside the OS

**Statement:** The SSH daemon (`sshd`) failed to start or crashed inside the guest OS, so the instance accepts TCP connections at the network layer but immediately closes them, producing a `Connection refused` error.

**Mechanism:** `sshd` listens on port 22; if it is stopped, crashed, or not enabled, the OS kernel returns a TCP RST to any incoming SYN on port 22, which manifests as `Connection refused` (not timeout). This is distinct from network-layer drops, which produce timeouts. The root cause may be a configuration syntax error in `/etc/ssh/sshd_config`, a failed systemd unit, or the SSH package being uninstalled.

**Indicator:**

- [Step 7] Connection is immediately refused (`Connection refused` within 1 second, not a timeout)
- [Step 6] Console output shows `sshd.service: Main process exited`, `Failed to start OpenBSD Secure Shell server`, or missing sshd startup lines

<!-- match: {"step": 7, "predicate": "contains", "target": "Connection refused"} -->

**Mitigation:**

- **Risk:** Medium; requires stopping the instance and mounting the volume to a rescue instance if SSM is unavailable.
- **Command:**

  ```bash
  # Immediate workaround: use SSM Session Manager if SSM agent is running
  aws ssm start-session --target i-0abc123def456789
  # Then inside the session:
  # sudo systemctl start sshd
  # sudo systemctl enable sshd
  ```

- **Duration:** Immediate if SSM is available; rescue procedure takes 15–20 minutes.

**Resolution:**

```bash
# If SSM is unavailable, use rescue instance to fix sshd config:
# After mounting root volume to /mnt/rescue on rescue instance:
# sudo sshd -t -f /mnt/rescue/etc/ssh/sshd_config
# Fix any reported syntax errors, then unmount and reattach

# Verify sshd_config key settings:
# PubkeyAuthentication yes
# AuthorizedKeysFile .ssh/authorized_keys
# PermitRootLogin no
```

**Verification:** After fix, attempt SSH — connection should complete without `Connection refused`. Alternatively, confirm via SSM: `sudo systemctl status sshd` shows `active (running)`.

### Cause H: Root filesystem full, blocking sshd session spawning

**Statement:** The instance's root filesystem has reached 100% capacity, preventing `sshd` from writing temporary files needed to spawn a new session, causing all new SSH connections to fail immediately.

**Mechanism:** `sshd` writes temporary files and relies on PAM or shell init scripts that may write to `/tmp` or `/var`; when the root filesystem is full, these writes fail with `ENOSPC`, causing the session setup to abort. The daemon itself continues to run and accepts the TCP connection, but the session immediately closes — this can appear as either `Connection closed` or a rapid disconnect after authentication.

**Indicator:**

- [Step 6] Console output shows `No space left on device` or similar filesystem error messages
- [Step 7] Authentication succeeds (key accepted) but session immediately closes without a shell prompt

<!-- match: {"step": 6, "predicate": "contains", "target": "No space left on device"} -->

**Mitigation:**

- **Risk:** Medium; requires access via SSM or rescue instance to free space.
- **Command:**

  ```bash
  # Use SSM Session Manager to access the instance without SSH
  aws ssm start-session --target i-0abc123def456789
  # Inside SSM session, identify and remove large files:
  # df -h /
  # sudo du -sh /var/log/* | sort -rh | head -10
  # sudo journalctl --vacuum-size=500M
  # sudo truncate -s 0 /var/log/syslog
  ```

- **Duration:** Immediate via SSM; 15–30 minutes via rescue instance.

**Resolution:**

```bash
# After freeing space, verify filesystem usage is below 85%:
df -h /

# Prevent recurrence: enable log rotation and set disk alarm
aws cloudwatch put-metric-alarm \
  --alarm-name ec2-disk-full-i-0abc123def456789 \
  --metric-name DiskSpaceUtilization \
  --namespace System/Linux \
  --statistic Average \
  --period 300 \
  --threshold 85 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 2 \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:alerts
```

**Verification:** `df -h /` shows less than 85% usage. SSH connection completes successfully and drops to a shell prompt.

### Cause Z: Unidentified cause

**Statement:** The EC2 instance remains unreachable after ruling out security groups, NACLs, routing, public IP, key pair, sshd status, and disk space.

**Mechanism:** Less common causes include OS-level firewalls (`iptables`/`firewalld`) blocking port 22 independently of security groups, instance CPU saturation preventing sshd from accepting connections, a corrupted `/home/<user>/.ssh/authorized_keys` file with wrong ownership or permissions, or an underlying EC2 host hardware failure (system status check impaired).

**Indicator:**

- [Default] All prior diagnostic steps returned expected results but SSH still fails

**Mitigation:**

- **Risk:** Low. Using SSM Session Manager provides shell access without SSH for further investigation without making changes.
- **Command:**

  ```bash
  # Access via SSM to investigate further
  aws ssm start-session --target i-0abc123def456789

  # Inside SSM: check OS firewall
  # sudo iptables -L INPUT -n --line-numbers
  # sudo firewall-cmd --list-all

  # Check authorized_keys ownership and permissions
  # ls -la ~/.ssh/authorized_keys

  # Check CPU load
  # uptime
  # top -bn1 | head -5
  ```

- **Duration:** Immediate if SSM agent is running; if SSM is also unavailable, stop/start the instance to migrate to a new host (resolves host hardware failures).

**Resolution:** Out of runbook scope. If SSM is unavailable and the instance is unresponsive, open an AWS Support case referencing the instance ID, AZ, and console output from Step 6. For persistent host issues, stop/start the instance to migrate to a new EC2 host: `aws ec2 stop-instances --instance-ids i-0abc123def456789 && aws ec2 wait instance-stopped --instance-ids i-0abc123def456789 && aws ec2 start-instances --instance-ids i-0abc123def456789`.

**Verification:** SSH connection completes end-to-end: `ssh -i key.pem ec2-user@<IP> 'echo OK; uptime'` returns `OK` and uptime line.

## Prevention

**Use AWS Systems Manager Session Manager as primary instance access method.** Session Manager provides interactive shell access without inbound ports, key pairs, or bastion hosts. Attach the `AmazonSSMManagedInstanceCore` IAM policy to the instance role; SSM agent is pre-installed on Amazon Linux 2/2023 and Windows AMIs. This eliminates the entire class of security group and key pair connectivity failures.

```bash
# Verify SSM agent is reachable before closing SSH access
aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=i-0abc123def456789" \
  --query 'InstanceInformationList[0].PingStatus' \
  --output text
# Should return: Online
```

**Use Elastic IPs for instances that are stopped and restarted.** Non-Elastic public IPs are released on every stop cycle. Allocate an Elastic IP and associate it at launch time for any instance requiring a stable address.

**Enforce SSH key hygiene with EC2 Instance Connect.** EC2 Instance Connect injects a one-time ephemeral public key for each session, eliminating long-lived key pair files that can be lost or compromised. Enable with `aws ec2-instance-connect send-ssh-public-key` and configure the security group to allow port 22 from the EC2 Instance Connect IP ranges only.

**Monitor disk usage with CloudWatch Agent.** The built-in EC2 `StatusCheckFailed` metric does not detect full filesystems. Install the CloudWatch Agent to collect `disk_used_percent` and create an alarm at 85% to alert before SSH is impacted.

```bash
# CloudWatch alarm for disk usage (requires CloudWatch Agent)
aws cloudwatch put-metric-alarm \
  --alarm-name "ec2-disk-85pct" \
  --metric-name "disk_used_percent" \
  --namespace "CWAgent" \
  --dimensions Name=InstanceId,Value=i-0abc123def456789 Name=path,Value=/ \
  --statistic Average \
  --period 300 \
  --threshold 85 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1
```

**Manage security group changes through IaC.** Use Terraform or AWS CloudFormation to manage security group rules. This prevents accidental rule deletion during security audits and provides a change history for diagnosing sudden connectivity loss.

**Set up a `StatusCheckFailed` CloudWatch alarm.** Create an alarm on the `StatusCheckFailed_System` metric with automatic recovery action (`arn:aws:automate:<region>:ec2:recover`) to migrate impaired instances to healthy hosts automatically.

## Sources

- [AWS EC2: Troubleshoot Connecting to Your Linux Instance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/TroubleshootingInstancesConnecting.html) — Primary source: all common connection error messages, security group verification steps, key pair recovery procedure, AMI username table, NACL requirements.
- [AWS EC2: Troubleshooting Overview](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/troubleshooting.html) — EC2 troubleshooting topic index; supplemental context on status checks and instance health.
- [AWS Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) — Session Manager requirements, IAM policy needs, CLI usage, and port-free access model used throughout mitigation and prevention sections.
