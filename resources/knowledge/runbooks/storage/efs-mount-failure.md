---
id: "efs-mount-failure"
title: "AWS EFS mount fails or hangs: connection timeout, access denied, and burst-credit exhaustion"
domain: storage
service: aws-efs
symptom_class: [connection_refused, timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [efs, nfs-2049, mount-nfs4, access-denied, az-mismatch, burst-credit]
difficulty: intermediate
---

## Symptom Recognition

- `mount.nfs4: Connection timed out` — `mount -t efs` or `mount -t nfs4` hangs ~30–90s then fails.
- `mount.nfs4: access denied by server while mounting 127.0.0.1:/` (EFS mount helper with TLS) or `...while mounting fs-xxxx.efs.<region>.amazonaws.com:/`.
- `mount.nfs4: Failed to resolve server fs-xxxx.efs.<region>.amazonaws.com: Name or service not known`.
- `/var/log/amazon/efs/mount.log` shows `Connection reset by peer`, `Failed to resolve`, or `Tls bound to port ... but cannot connect`.
- Mount succeeds but I/O stalls: reads/writes intermittently hang for seconds; CloudWatch `BurstCreditBalance` at/near 0 and `MeteredIOBytes` == `PermittedThroughput`.

## Applicability

- Amazon EFS (Standard / One Zone) mounted from EC2, ECS/Fargate, or EKS Linux clients.
- `amazon-efs-utils` package installed (provides `mount -t efs`, TLS, and IAM helper) — recommended over raw `mount -t nfs4`.
- Required to diagnose: `aws` CLI with `elasticfilesystem:Describe*` permissions; shell access to the client host with `nc`/`telnet`; CloudWatch read access.
- IAM-authenticated mounts require client IAM permissions `elasticfilesystem:ClientMount`/`ClientWrite` and the `-o iam` (or `-o tls`) mount option.

## Diagnostic Steps

### Step 1: Attempt the mount with verbose logging

```bash
sudo mkdir -p /mnt/efs
sudo mount -t efs -o tls,verbose fs-0123456789abcdef0:/ /mnt/efs
sudo tail -n 40 /var/log/amazon/efs/mount.log
```

Expected output: a successful mount produces no error and `mount | grep efs` shows the filesystem. On failure, the exact `mount.nfs4:` error string is printed and recorded in `mount.log`.

### Step 2: List mount targets and their AZs / security groups

```bash
aws efs describe-mount-targets --file-system-id fs-0123456789abcdef0 \
  --query 'MountTargets[].{AZ:AvailabilityZoneName,Subnet:SubnetId,IP:IpAddress,State:LifeCycleState,MT:MountTargetId}' --output table
# Compare against this client instance's AZ:
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/availability-zone
```

Expected output: a mount target in `available` state whose AZ matches the client AZ printed by the metadata call.

### Step 3: Test NFS reachability on TCP 2049 to the mount-target IP

```bash
MT_IP=$(aws efs describe-mount-targets --file-system-id fs-0123456789abcdef0 \
  --query 'MountTargets[0].IpAddress' --output text)
nc -vz -w 5 "$MT_IP" 2049 ; echo "exit=$?"
# or: telnet "$MT_IP" 2049
```

Expected output: `succeeded!` / `Connected to ...` and `exit=0` when port 2049 is open end-to-end.

### Step 4: Resolve the EFS DNS name and check VPC DNS settings

```bash
nslookup fs-0123456789abcdef0.efs.us-east-1.amazonaws.com
aws ec2 describe-vpc-attribute --vpc-id vpc-0abc --attribute enableDnsSupport \
  --query 'EnableDnsSupport.Value'
aws ec2 describe-vpc-attribute --vpc-id vpc-0abc --attribute enableDnsHostnames \
  --query 'EnableDnsHostnames.Value'
```

Expected output: DNS name resolves to the mount-target private IP from Step 3; both VPC attributes return `true`.

### Step 5: Inspect access point, IAM, and file-system policy

```bash
aws efs describe-access-points --file-system-id fs-0123456789abcdef0 \
  --query 'AccessPoints[].{AP:AccessPointId,Root:RootDirectory.Path,POSIX:PosixUser}' --output table
aws efs describe-file-system-policy --file-system-id fs-0123456789abcdef0 \
  --query 'Policy' --output text
aws sts get-caller-identity --query 'Arn' --output text
```

Expected output: the access-point root directory path exists/has a `CreationInfo`; the file-system policy contains at least one `Allow` for `elasticfilesystem:ClientMount`/`ClientWrite` matching the caller ARN with no overriding `Deny`.

### Step 6: Check burst-credit balance and permitted throughput

```bash
aws cloudwatch get-metric-statistics --namespace AWS/EFS \
  --metric-name BurstCreditBalance --period 300 --statistics Minimum \
  --dimensions Name=FileSystemId,Value=fs-0123456789abcdef0 \
  --start-time "$(date -u -d '1 hour ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
  --query 'Datapoints[].Minimum'
aws efs describe-file-systems --file-system-id fs-0123456789abcdef0 \
  --query 'FileSystems[0].ThroughputMode'
```

Expected output: `BurstCreditBalance` well above 0 for a healthy Bursting filesystem; throughput mode reported (`bursting`/`elastic`/`provisioned`).

## Causes

### Cause A: Mount-target security group blocks inbound NFS on TCP 2049
**Statement:** The mount target's security group (or the client SG / subnet NACL on the path) does not allow inbound TCP 2049 from the client's IP range, so the NFS handshake never completes.
**Chain:**
- root: SG/NACL inbound rule for TCP 2049 from the client CIDR is missing or denied
- s1: TCP connection to the mount-target IP on 2049 is dropped/never established
- D: mount hangs and fails with `mount.nfs4: Connection timed out`
**Indicators:**
- root: [Step 3] `nc -vz <mt-ip> 2049` reports timeout / non-zero exit (no rule permits the client)
- s1: [Step 1] `mount.log` / console shows `Connection timed out`
- D: [Symptom] `mount.nfs4: Connection timed out`
**Interventions:**
- **remediation** (root): authorize inbound TCP 2049 on the mount-target SG from the client's subnet/VPC CIDR.

  ```bash
  aws ec2 authorize-security-group-ingress \
    --group-id sg-MOUNTTARGET --protocol tcp --port 2049 \
    --cidr 10.0.0.0/16
  ```

  **Verification:** re-run Step 3 — `nc -vz <mt-ip> 2049` returns `exit=0`; then Step 1 mount succeeds.

### Cause B: No EFS mount target in the client's Availability Zone
**Statement:** The filesystem has no mount target in the client instance's AZ, so the AZ-local DNS name resolves to nothing (or the cross-AZ path is unusable) and the mount cannot connect.
**Chain:**
- root: no mount target exists in `available` state in the client's AZ
- s1: `fs-xxxx.efs.<region>.amazonaws.com` has no resolvable endpoint for that AZ
- s2: client cannot reach any mount-target IP on 2049
- D: mount fails with `Connection timed out` or `Failed to resolve server`
**Indicators:**
- root: [Step 2] no mount-target AZ matches the client AZ from instance metadata
- s1: [Step 4] `nslookup` of the EFS DNS name returns NXDOMAIN / no private IP
- D: [Symptom] `mount.nfs4: Failed to resolve server` or `Connection timed out`
**Interventions:**
- **remediation** (root): create a mount target in the client's AZ/subnet (wait 90s for DNS to propagate before mounting).

  ```bash
  aws efs create-mount-target --file-system-id fs-0123456789abcdef0 \
    --subnet-id subnet-CLIENTAZ --security-groups sg-MOUNTTARGET
  ```

  **Verification:** re-run Step 2 — an `available` mount target now matches the client AZ; Step 1 mount succeeds.
- **mitigation** (s2): mount against the mount-target IP directly to bypass AZ DNS while the new target propagates.

  ```bash
  sudo mount -t nfs4 -o nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2 \
    <other-az-mount-target-ip>:/ /mnt/efs
  ```

  **Risk:** cross-AZ traffic incurs data-transfer cost and is not zone-failure tolerant. **Duration:** until the in-AZ mount target reaches `available` (typically <90s). **Verification:** `mount | grep efs` shows the filesystem mounted.

### Cause C: VPC DNS disabled or custom DNS does not forward to the Amazon resolver
**Statement:** `enableDnsSupport`/`enableDnsHostnames` is off on the VPC, or a custom DNS server fails to forward `*.amazonaws.com` to the Amazon DNS (`VPC_CIDR_base+2`), so the EFS DNS name cannot resolve.
**Chain:**
- root: VPC DNS attributes are disabled or custom resolver does not forward `*.amazonaws.com`
- s1: `fs-xxxx.efs.<region>.amazonaws.com` fails to resolve on the client
- D: mount fails with `mount.nfs4: Failed to resolve server ...: Name or service not known`
**Indicators:**
- root: [Step 4] `describe-vpc-attribute` returns `false` for DNS support or hostnames
- s1: [Step 4] `nslookup` cannot resolve the EFS DNS name
- D: [Symptom] `mount.nfs4: Failed to resolve server`
**Interventions:**
- **remediation** (root): enable DNS support/hostnames on the VPC (and forward `*.amazonaws.com` on any custom resolver).

  ```bash
  aws ec2 modify-vpc-attribute --vpc-id vpc-0abc --enable-dns-support '{"Value":true}'
  aws ec2 modify-vpc-attribute --vpc-id vpc-0abc --enable-dns-hostnames '{"Value":true}'
  ```

  **Verification:** re-run Step 4 — both attributes return `true` and `nslookup` resolves the EFS DNS name; Step 1 mount succeeds.
- **mitigation** (s1): mount via the mount-target IP to bypass DNS entirely.

  ```bash
  sudo mount -t nfs4 -o nfsvers=4.1,hard,timeo=600,retrans=2 \
    <mount-target-ip>:/ /mnt/efs
  ```

  **Risk:** IP mount loses TLS/IAM and won't follow mount-target IP changes. **Duration:** until VPC/DNS is corrected. **Verification:** `mount | grep efs` shows the filesystem mounted.

### Cause D: IAM / access-point authorization denies the mount
**Statement:** The client lacks `elasticfilesystem:ClientMount`/`ClientWrite` (or the file-system policy has an applicable `Deny`, or the access-point root directory does not exist), so the server rejects the authenticated mount.
**Chain:**
- root: caller identity is not granted ClientMount/ClientWrite (policy Deny or missing access-point root dir)
- s1: EFS mount target refuses the TLS/IAM mount request
- D: mount fails with `mount.nfs4: access denied by server while mounting 127.0.0.1:/`
**Indicators:**
- root: [Step 5] file-system policy has no `Allow` for the caller ARN (or has a `Deny`), or access-point root path is absent
- s1: [Step 1] mount attempted with `-o iam`/`-o tls` still rejected; `mount.log` shows `access denied`
- D: [Symptom] `mount.nfs4: access denied by server`
**Interventions:**
- **remediation** (root): grant the client role ClientMount/ClientWrite and ensure the access-point root directory exists (or set `CreationInfo` so EFS creates it).

  ```bash
  cat > efs-client.json <<'JSON'
  {"Version":"2012-10-17","Statement":[{"Effect":"Allow",
   "Action":["elasticfilesystem:ClientMount","elasticfilesystem:ClientWrite",
             "elasticfilesystem:DescribeMountTargets"],
   "Resource":"arn:aws:elasticfilesystem:us-east-1:111122223333:file-system/fs-0123456789abcdef0"}]}
  JSON
  aws iam put-role-policy --role-name efs-client-role \
    --policy-name efs-client --policy-document file://efs-client.json
  ```

  **Verification:** re-run Step 5 (policy now allows the caller / root dir present), then Step 1 with `-o iam,tls` mounts without `access denied`.

### Cause E: Bursting burst credits exhausted (mount works, I/O stalls)
**Statement:** A Bursting-throughput filesystem has drained `BurstCreditBalance` to 0, dropping permitted throughput to the size-proportional baseline (50 KiBps/GiB), which manifests as intermittent multi-second I/O hangs after a successful mount.
**Chain:**
- root: sustained throughput exceeded baseline until `BurstCreditBalance` reached 0
- s1: `PermittedThroughput` collapses to baseline and equals `MeteredIOBytes`
- s2: NFS operations queue and stall waiting for throughput
- D: reads/writes hang intermittently for seconds (timeout symptom_class) on an already-mounted FS
**Indicators:**
- root: [Step 6] `BurstCreditBalance` Minimum at/near 0
- s1: [Step 6] throughput mode is `bursting`
- D: [Symptom] mounted FS shows intermittent I/O stalls; `MeteredIOBytes` == `PermittedThroughput`
**Interventions:**
- **remediation** (root): switch the filesystem to Elastic (or Provisioned) throughput to remove the credit ceiling.

  ```bash
  aws efs update-file-system --file-system-id fs-0123456789abcdef0 \
    --throughput-mode elastic
  ```

  **Verification:** re-run Step 6 — `ThroughputMode` is `elastic`; I/O latency normalizes and stalls stop.
- **mitigation** (s2): temporarily raise capacity via Provisioned throughput to ride out the spike.

  ```bash
  aws efs update-file-system --file-system-id fs-0123456789abcdef0 \
    --throughput-mode provisioned --provisioned-throughput-in-mibps 256
  ```

  **Risk:** Provisioned throughput is billed for the provisioned rate regardless of use; throughput-mode changes are limited to once per 24h. **Duration:** until traffic subsides or Elastic is adopted. **Verification:** I/O stalls clear; `PermittedThroughput` reflects the provisioned rate.

### Cause Z: Unidentified
**Statement:** The mount failure or hang does not match Causes A–E (e.g. NFS client kernel/module issue, stale `efs-utils`/stunnel state, regional EFS service event, or a layered firewall outside the SG/NACL path).
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the storage/AWS SME.

  ```bash
  ts=$(date -u +%Y%m%dT%H%M%SZ); out="efs-diag-$ts"; mkdir -p "$out"
  sudo cp -a /var/log/amazon/efs/ "$out/efs-logs" 2>/dev/null
  dmesg | tail -n 200 > "$out/dmesg.txt"
  mount | grep -i efs > "$out/mounts.txt"
  rpm -q amazon-efs-utils nfs-utils stunnel5 > "$out/pkgs.txt" 2>&1
  aws efs describe-file-systems --file-system-id fs-0123456789abcdef0 > "$out/fs.json"
  aws efs describe-mount-targets --file-system-id fs-0123456789abcdef0 > "$out/mt.json"
  tar czf "$out.tgz" "$out"
  ```

  **Risk:** log/diagnostic collection only; no production change. **Duration:** N/A. **Verification:** `efs-diag-*.tgz` produced and attached to the escalation ticket.

## Prevention

- Create a mount target in **every AZ** your clients run in; keep all mount-target SGs allowing inbound TCP 2049 from the workload subnets/VPC CIDR only.
- Enable `enableDnsSupport` and `enableDnsHostnames` on the VPC; forward `*.amazonaws.com` on any custom resolver. Wait 90s after creating a mount target before mounting.
- Prefer `mount -t efs -o tls,iam` (amazon-efs-utils) over raw `mount -t nfs4`; bake `amazon-efs-utils` into the AMI/container image.
- Grant clients only `elasticfilesystem:ClientMount`/`ClientWrite`/`DescribeMountTargets`; pre-create access-point root directories or set `CreationInfo`.
- Use **Elastic throughput** for spiky/unpredictable workloads; for Bursting, alarm on CloudWatch `BurstCreditBalance` (e.g. Minimum < 1 TiB-equivalent for 1h) and on `PermittedThroughput` == `MeteredIOBytes`.

## Sources

- [Troubleshooting efs mounting](https://docs.aws.amazon.com/efs/latest/ug/troubleshooting-efs-mounting.html) — exact `mount.nfs4` error strings (Connection timed out, access denied, Failed to resolve), `/var/log/amazon/efs/mount.log`, AZ-local DNS requirement.
- [Fargate unable to mount efs](https://repost.aws/knowledge-center/fargate-unable-to-mount-efs) — security-group inbound TCP 2049 rules, mount target must exist in the client AZ.
- [Ecs problems with efs dns name](https://repost.aws/knowledge-center/ecs-problems-with-efs-dns-name) — DNS resolution fallback to efs-utils, VPC DNS support/hostnames requirement, 90s propagation delay.
- [Efs access points directory access](https://repost.aws/knowledge-center/efs-access-points-directory-access) — access-point root directory and `-o iam`/`-o tls`; access denied when root dir absent.
- [Managing throughput](https://docs.aws.amazon.com/efs/latest/ug/managing-throughput.html) — throughput modes (bursting/elastic/provisioned), baseline 50 KiBps/GiB, switching to Elastic/Provisioned.
- [Efs metrics](https://docs.aws.amazon.com/efs/latest/ug/efs-metrics.html) — `BurstCreditBalance`, `PermittedThroughput`, `MeteredIOBytes` CloudWatch metrics and interpretation.
- [Efs burst credits](https://repost.aws/knowledge-center/efs-burst-credits) — burst-credit exhaustion drops throughput to baseline; recover via Provisioned/Elastic.
