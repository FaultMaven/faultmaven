---
id: ec2-instance-unreachable
title: "AWS EC2 Instance Unreachable: SSH/RDP Connection Failure Diagnosis"
domain: compute
service: aws-ec2
symptom_class:
  - connection_refused
  - timeout
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - ec2
  - ssh
  - rdp
  - connectivity
  - security-group
  - network-acl
  - routing
difficulty: intermediate
---

# AWS EC2 Instance Unreachable: SSH/RDP Connection Failure Diagnosis

## Problem Definition

This runbook applies to Amazon EC2 instances (Linux or Windows) in any AWS region and VPC configuration. You need the AWS CLI v2 with `ec2:Describe*` permissions, the correct SSH private key (.pem) for the instance key pair, and knowledge of the instance's default username (e.g., `ec2-user` for Amazon Linux, `ubuntu` for Ubuntu, `admin` for Debian). For Windows instances, replace SSH references with RDP (port 3389).

An EC2 instance is running and passing status checks, but SSH (port 22) or RDP (port 3389) connections time out or are refused. The instance appears healthy in the console but you cannot establish a remote session. Common error messages include `Connection timed out`, `Connection refused`, `Permission denied (publickey)`, and `No route to host`.

The most frequent causes are: security group rules missing an inbound allow for your source IP, network ACLs blocking traffic, missing or misconfigured route table entries (no internet gateway route), incorrect key pair or username, OS-level firewall (iptables/firewalld) blocking connections, the SSH daemon not running, or a full root filesystem preventing SSH from spawning new sessions.

**Typical error presentation:**

```
$ ssh -i my-key.pem ec2-user@ec2-203-0-113-25.compute-1.amazonaws.com
ssh: connect to host ec2-203-0-113-25.compute-1.amazonaws.com port 22: Connection timed out
```

## Diagnostic Steps

### Step 1: Confirm the Instance Is Running and Passing Status Checks

**What this checks:** Whether the instance is actually in a healthy running state at the AWS level.

```bash
aws ec2 describe-instance-status --instance-ids i-0abc123def456789 \
  --query 'InstanceStatuses[0].{State:InstanceState.Name,System:SystemStatus.Status,Instance:InstanceStatus.Status}' \
  --output table
```

**Expected output:** `State: running`, `System: ok`, `Instance: ok`.

**What the finding means:** If the instance is not `running`, start it. If system status check is `impaired`, the underlying host has a problem (use EC2 Auto Recovery or stop/start to migrate to a new host). If instance status check is `impaired`, the guest OS is unhealthy (check console output in Step 6).

### Step 2: Verify the Instance Has a Public IP or Elastic IP

**What this checks:** Whether the instance is reachable from the internet. Instances in private subnets without a public IP are only reachable via VPN, Direct Connect, or a bastion host.

```bash
aws ec2 describe-instances --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].{PublicIp:PublicIpAddress,PrivateIp:PrivateIpAddress,SubnetId:SubnetId,VpcId:VpcId}' \
  --output table
```

**Expected output:** A non-null `PublicIp` field.

**What the finding means:** If `PublicIp` is null, the instance has no public address. You must either associate an Elastic IP, connect via a bastion host in a public subnet, or use AWS Systems Manager Session Manager (which does not require inbound ports).

### Step 3: Check Security Group Inbound Rules for SSH/RDP

**What this checks:** Whether the instance's security group allows inbound traffic on port 22 (SSH) or 3389 (RDP) from your IP address.

```bash
# Get security group IDs
SG_IDS=$(aws ec2 describe-instances --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].SecurityGroups[*].GroupId' --output text)

# Check inbound rules for each security group
for SG in $SG_IDS; do
  echo "=== Security Group: $SG ==="
  aws ec2 describe-security-group-rules --filters "Name=group-id,Values=$SG" \
    --query 'SecurityGroupRules[?IsEgress==`false`].{Port:FromPort,Protocol:IpProtocol,Source:CidrIpv4}' \
    --output table
done
```

**Expected output:** A rule allowing TCP port 22 (or 3389) from your source IP or CIDR range.

**What the finding means:** If no rule exists for your IP on the required port, add one. If the source is `0.0.0.0/0`, restrict it to your specific IP for security. If you recently changed ISPs or are on a VPN, your source IP may have changed.

### Step 4: Check Network ACLs on the Subnet

**What this checks:** Whether subnet-level network ACLs are blocking SSH traffic. Unlike security groups, NACLs are stateless and require explicit inbound AND outbound rules.

```bash
# Get subnet ID
SUBNET=$(aws ec2 describe-instances --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].SubnetId' --output text)

# Get NACL for the subnet
NACL=$(aws ec2 describe-network-acls --filters "Name=association.subnet-id,Values=$SUBNET" \
  --query 'NetworkAcls[0].NetworkAclId' --output text)

echo "=== Inbound Rules ==="
aws ec2 describe-network-acls --network-acl-ids $NACL \
  --query 'NetworkAcls[0].Entries[?Egress==`false`].{Rule:RuleNumber,Action:RuleAction,Protocol:Protocol,Port:PortRange,CIDR:CidrBlock}' \
  --output table

echo "=== Outbound Rules ==="
aws ec2 describe-network-acls --network-acl-ids $NACL \
  --query 'NetworkAcls[0].Entries[?Egress==`true`].{Rule:RuleNumber,Action:RuleAction,Protocol:Protocol,Port:PortRange,CIDR:CidrBlock}' \
  --output table
```

**Expected output:** Inbound: an ALLOW rule for TCP port 22 from your CIDR. Outbound: an ALLOW rule for TCP ephemeral ports (1024-65535) to your CIDR.

**What the finding means:** NACLs are evaluated in rule-number order. A DENY rule with a lower number than your ALLOW rule will block traffic. Outbound ephemeral ports must be allowed because SSH response traffic uses a random high port on the client side.

### Step 5: Verify Route Table Has an Internet Gateway Route

**What this checks:** Whether the subnet's route table has a route to the internet gateway, which is required for public IP reachability.

```bash
SUBNET=$(aws ec2 describe-instances --instance-ids i-0abc123def456789 \
  --query 'Reservations[0].Instances[0].SubnetId' --output text)

aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$SUBNET" \
  --query 'RouteTables[0].Routes[*].{Destination:DestinationCidrBlock,Target:GatewayId,State:State}' \
  --output table
```

**Expected output:** A route with `Destination: 0.0.0.0/0` and `Target: igw-xxxxxxxx` in state `active`.

**What the finding means:** If there is no `0.0.0.0/0` route pointing to an internet gateway, the subnet is private. Instances in private subnets are not reachable from the internet even with a public IP. Either add an IGW route (making it a public subnet) or use a bastion host.

### Step 6: Check Console Output for OS-Level Issues

**What this checks:** Whether the OS booted successfully and the SSH daemon started.

```bash
aws ec2 get-console-output --instance-id i-0abc123def456789 --latest --output text | tail -50
```

**Expected output:** Boot messages ending with a login prompt or `sshd` startup confirmation.

**What the finding means:** If the output shows kernel panics, filesystem mount failures, or `sshd` failing to start, the problem is inside the OS. Attach the root volume to a rescue instance to fix the configuration.

### Step 7: Test SSH Connection with Verbose Output

**What this checks:** Where in the SSH handshake the connection fails, which distinguishes network-level blocks from authentication failures.

```bash
ssh -vvv -i my-key.pem -o ConnectTimeout=10 ec2-user@EC2_PUBLIC_IP 2>&1 | head -60
```

**Expected output:** Connection establishment messages followed by key exchange.

**What the finding means:** If the output stalls at `Connecting to...`, the problem is network-level (security group, NACL, or routing). If it reaches `Permission denied (publickey)`, the network path is fine but the key or username is wrong. Check the key pair name in the console: `aws ec2 describe-instances --instance-ids i-0abc123def456789 --query 'Reservations[0].Instances[0].KeyName'`.

## Mitigation

### Option 1: Add Security Group Rule for Your IP

Use when Step 3 reveals no inbound SSH rule for your IP.

- **Risk:** Low. Adding a scoped inbound rule does not affect other traffic.
- **Command:**
  ```bash
  MY_IP=$(curl -s https://checkip.amazonaws.com)
  SG_ID=$(aws ec2 describe-instances --instance-ids i-0abc123def456789 \
    --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)
  aws ec2 authorize-security-group-ingress --group-id $SG_ID \
    --protocol tcp --port 22 --cidr ${MY_IP}/32
  ```
- **Verify:**
  ```bash
  ssh -i my-key.pem -o ConnectTimeout=10 ec2-user@EC2_PUBLIC_IP echo "Connected"
  ```
  Should print `Connected`.
- **Duration:** Immediate. Security group changes take effect within seconds.

### Option 2: Use AWS Systems Manager Session Manager

Use when network path issues cannot be quickly resolved, or when the instance is in a private subnet.

- **Risk:** Low. Session Manager uses the SSM agent already installed on most Amazon Linux and Windows AMIs. Requires the instance to have an IAM role with `AmazonSSMManagedInstanceCore` policy and outbound HTTPS (443) access.
- **Command:**
  ```bash
  aws ssm start-session --target i-0abc123def456789
  ```
- **Verify:**
  ```bash
  aws ssm describe-instance-information \
    --filters "Key=InstanceIds,Values=i-0abc123def456789" \
    --query 'InstanceInformationList[0].PingStatus' --output text
  ```
  Should return `Online`.
- **Duration:** Immediate if SSM agent is running and IAM role is attached.

### Option 3: Fix SSH Key Pair via Rescue Instance

Use when authentication fails (`Permission denied (publickey)`) and the original key pair is lost or the `authorized_keys` file is corrupted.

- **Risk:** Medium. Requires stopping the instance and detaching its root volume. Data is preserved but the instance is offline during the procedure.
- **Command:**
  ```bash
  # Stop the instance
  aws ec2 stop-instances --instance-ids i-0abc123def456789
  aws ec2 wait instance-stopped --instance-ids i-0abc123def456789

  # Detach root volume (note the device name, e.g., /dev/xvda)
  ROOT_VOL=$(aws ec2 describe-instances --instance-ids i-0abc123def456789 \
    --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)
  aws ec2 detach-volume --volume-id $ROOT_VOL

  # Attach to a rescue instance in the same AZ
  aws ec2 attach-volume --volume-id $ROOT_VOL --instance-id i-RESCUE --device /dev/sdf

  # On the rescue instance, mount and fix authorized_keys:
  # sudo mount /dev/xvdf1 /mnt/rescue
  # sudo cp ~/.ssh/authorized_keys /mnt/rescue/home/ec2-user/.ssh/authorized_keys
  # sudo chmod 600 /mnt/rescue/home/ec2-user/.ssh/authorized_keys
  # sudo chmod 700 /mnt/rescue/home/ec2-user/.ssh
  # sudo umount /mnt/rescue

  # Reattach to original instance and start
  aws ec2 detach-volume --volume-id $ROOT_VOL
  aws ec2 attach-volume --volume-id $ROOT_VOL --instance-id i-0abc123def456789 --device /dev/xvda
  aws ec2 start-instances --instance-ids i-0abc123def456789
  ```
- **Verify:**
  ```bash
  ssh -i new-key.pem ec2-user@EC2_PUBLIC_IP echo "Connected"
  ```
- **Duration:** 10-20 minutes including stop, volume detach/attach, and restart.

## Root Cause Resolution

**If** the security group was missing an SSH rule because it was removed during a security audit **then** create a dedicated bastion security group with SSH access restricted to your corporate CIDR, and reference it from application instance security groups:

```bash
aws ec2 create-security-group --group-name bastion-ssh \
  --description "SSH access from corporate network" --vpc-id vpc-abc123
aws ec2 authorize-security-group-ingress --group-id sg-BASTION \
  --protocol tcp --port 22 --cidr 10.0.0.0/8
```

**If** the instance lost its public IP after a stop/start cycle (non-Elastic IP) **then** allocate and associate an Elastic IP for stable addressing:

```bash
EIP=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
aws ec2 associate-address --instance-id i-0abc123def456789 --allocation-id $EIP
```

**If** the SSH daemon crashed or was misconfigured (console output shows sshd failure) **then** mount the root volume on a rescue instance and fix the sshd configuration:

```bash
# On rescue instance after mounting root volume to /mnt/rescue
sudo vi /mnt/rescue/etc/ssh/sshd_config
# Ensure: PermitRootLogin no, PubkeyAuthentication yes, PasswordAuthentication no
# Check for syntax errors:
sudo sshd -t -f /mnt/rescue/etc/ssh/sshd_config
```

**If** the OS-level firewall (iptables/firewalld) is blocking port 22 **then** mount the root volume on a rescue instance and disable the blocking rule or add an SSH exception:

```bash
# On rescue instance after mounting to /mnt/rescue
sudo chroot /mnt/rescue
iptables -L INPUT -n --line-numbers
# Delete the blocking rule or allow SSH:
iptables -I INPUT 1 -p tcp --dport 22 -j ACCEPT
iptables-save > /etc/sysconfig/iptables
exit
```

**If** the root filesystem is full, preventing SSH from spawning sessions **then** mount the root volume on a rescue instance and free space:

```bash
# On rescue instance after mounting to /mnt/rescue
sudo du -sh /mnt/rescue/var/log/* | sort -rh | head -10
# Truncate large log files
sudo truncate -s 0 /mnt/rescue/var/log/messages
sudo truncate -s 0 /mnt/rescue/var/log/journal/*/*.journal
```

## Verification

After applying the fix, confirm full connectivity:

```bash
# Verify SSH connection works end-to-end
ssh -i my-key.pem -o ConnectTimeout=10 ec2-user@EC2_PUBLIC_IP \
  'echo "SSH OK"; uptime; df -h / | tail -1'
```

The command should return `SSH OK`, the instance uptime, and disk usage.

```bash
# Verify both status checks are passing
aws ec2 describe-instance-status --instance-ids i-0abc123def456789 \
  --query 'InstanceStatuses[0].{System:SystemStatus.Status,Instance:InstanceStatus.Status}' \
  --output table
```

Both should report `ok`.

```bash
# Verify the security group rules include SSH access
aws ec2 describe-security-group-rules \
  --filters "Name=group-id,Values=$(aws ec2 describe-instances --instance-ids i-0abc123def456789 \
    --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)" \
  --query 'SecurityGroupRules[?IsEgress==`false` && FromPort==`22`].{Source:CidrIpv4,Description:Description}' \
  --output table
```

Should show your IP or CIDR as an allowed source.

## Prevention

### Use AWS Systems Manager Session Manager as Primary Access

Session Manager eliminates the need for inbound SSH ports entirely. Install the SSM agent (pre-installed on Amazon Linux 2/2023 and Windows AMIs) and attach the `AmazonSSMManagedInstanceCore` IAM policy to the instance role. This removes the entire class of security group and key pair connectivity issues.

### Use Elastic IPs for Persistent Addressing

Non-Elastic public IPs change on every stop/start cycle. For instances that are regularly stopped, allocate an Elastic IP to maintain a stable address.

### Audit Security Groups with AWS Config

Use the AWS Config managed rule `restricted-ssh` to detect security groups that allow SSH from `0.0.0.0/0`. Set up automated remediation to replace broad rules with IP-scoped rules.

### Monitor Instance Reachability

Create a CloudWatch alarm on the `StatusCheckFailed` metric. For application-level SSH monitoring, use Route 53 health checks on TCP port 22 or a synthetic canary that tests SSH connectivity periodically.

### Store SSH Keys Securely

Store private keys in AWS Secrets Manager or your organization's vault. Never share keys between team members. Use EC2 Instance Connect for one-time ephemeral key injection instead of long-lived key pairs.

## Sources

- [AWS EC2: Troubleshoot Connecting to Your Instance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/TroubleshootingInstancesConnecting.html) - Official guide covering SSH connection failures, security group checks, key pair issues, and NACL verification.
- [AWS EC2: Troubleshooting](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/troubleshooting.html) - Top-level EC2 troubleshooting reference.
- [AWS Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) - Alternative access method that does not require inbound ports.
- [AWS VPC: Security Group Rules](https://docs.aws.amazon.com/vpc/latest/userguide/security-group-rules.html) - Security group configuration reference.
