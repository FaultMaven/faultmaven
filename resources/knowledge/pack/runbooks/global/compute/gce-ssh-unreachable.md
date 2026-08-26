---
id: "gce-ssh-unreachable"
title: "GCE Instance Unreachable via SSH (connection refused / timeout / publickey)"
domain: compute
service: gcp-compute
symptom_class: [connection_refused, timeout]
severity: high
scope: global
version: "1.0.1"
last_updated: "2026-08-26"
verified_by: "kb-researcher"
status: draft
tags: [ssh, permission-denied-publickey, os-login, 35-235-240-0-20, no-space-left-on-device, emergency-mode]
difficulty: advanced
---

## Symptom Recognition

- Client-side: `ssh: connect to host <ip> port 22: Connection refused`
- Client-side: `ssh: connect to host <ip> port 22: Operation timed out` / `Connection timed out`
- Client-side: `Permission denied (publickey).`
- `gcloud compute ssh` retries then fails: `Could not connect, retrying...`
- Serial console boot log contains: `emergency mode` or `No space left on device`
- `gcloud compute ssh VM_NAME --troubleshoot` reports a failed firewall, IAM, OS Login, or guest-agent check.

## Applicability

- Google Compute Engine Linux VMs (Debian, Ubuntu, RHEL/CentOS, Rocky, SUSE) with the Google guest environment installed.
- Required access: IAM roles `roles/compute.viewer` (read), `roles/compute.instanceAdmin.v1` (stop/start/resize), and `roles/iap.tunnelResourceAccessor` plus `roles/compute.osLogin` when connecting through IAP / OS Login.
- Tools: `gcloud` CLI (Google Cloud SDK) authenticated to the project; a local SSH client.
- Run all commands with the instance's zone in scope, e.g. `gcloud config set compute/zone ZONE` or append `--zone=ZONE`.

## Diagnostic Steps

### Step 1: Run the built-in SSH troubleshooter

```bash
gcloud compute ssh VM_NAME --zone=ZONE --troubleshoot --tunnel-through-iap
```

Expected output: a per-check report (network connectivity, firewall rules, IAM permissions, OS Login, guest agent). Each check is reported as passing or failing with a remediation hint. (Drop `--tunnel-through-iap` if you connect over a public/internal IP rather than IAP.)

### Step 2: Inspect ingress firewall rules for TCP port 22

```bash
gcloud compute firewall-rules list --format="table(name,network,direction,sourceRanges.list(),allowed[].map().firewall_rule().list())" \
  | grep "tcp:22"
```

Expected output: at least one INGRESS `allow` rule for `tcp:22` whose source ranges cover your client (e.g. a public CIDR) or the IAP range `35.235.240.0/20`. No matching line means port 22 ingress is not permitted.

### Step 3: Read the serial console boot log

```bash
gcloud compute instances get-serial-port-output VM_NAME --zone=ZONE
```

Expected output: kernel and systemd boot messages. Scan for `emergency mode`, `No space left on device`, `google-guest-agent`, and `sshd` startup lines.

### Step 4: Grep the serial log for known failure markers

```bash
gcloud compute instances get-serial-port-output VM_NAME --zone=ZONE | grep -E "emergency mode|No space left on device"
```

Expected output: empty if the OS booted cleanly with disk space; any match pinpoints a boot/full-disk fault.

### Step 5: Verify the OS Login setting on the instance and project

```bash
gcloud compute instances describe VM_NAME --zone=ZONE \
  --format="value(metadata.items.filter('key:enable-oslogin').extract('value'))"
gcloud compute project-info describe \
  --format="value(commonInstanceMetadata.items.filter('key:enable-oslogin').extract('value'))"
```

Expected output: `TRUE` if OS Login is enabled at the instance or project level, blank/`FALSE` otherwise. This determines whether metadata SSH keys are honored.

## Causes

### Cause A: Ingress firewall rule does not allow TCP 22 from the client (or IAP range)
**Statement:** No VPC ingress firewall rule permits `tcp:22` from the connecting source — either the client's public CIDR for direct SSH or `35.235.240.0/20` when tunneling through IAP — so the TCP handshake never completes.
**Chain:**
- root: no INGRESS allow rule covers `tcp:22` from the client/IAP source range
- s1: SYN packets to port 22 are dropped at the VPC firewall
- D: client reports connection refused/timeout, SSH unreachable
**Indicators:**
- root: [Step 2] no `tcp:22` ingress allow rule whose source ranges include the client CIDR or `35.235.240.0/20`
- s1: [Step 1] troubleshooter flags the firewall/network-connectivity check as failing
- D: [Symptom] `Operation timed out` / `Connection refused` on port 22
**Interventions:**
- **remediation** (root): create an ingress rule allowing port 22 from the IAP forwarding range (preferred) or your client CIDR.

  ```bash
  gcloud compute firewall-rules create allow-ssh-ingress-from-iap \
    --direction=INGRESS \
    --action=allow \
    --rules=tcp:22 \
    --source-ranges=35.235.240.0/20 \
    --network=VPC_NETWORK
  ```

  **Verification:** re-run Step 2 and confirm the new rule appears for `tcp:22`; then `gcloud compute ssh VM_NAME --zone=ZONE --tunnel-through-iap` succeeds.

### Cause B: OS Login is enabled but the connection presents a metadata-based SSH key
**Statement:** OS Login is enabled on the project or instance, which makes the guest reject SSH keys stored in instance/project metadata, so a metadata-keyed login is denied with publickey.
**Chain:**
- root: OS Login is enabled while the login attempt uses a metadata-stored SSH key
- s1: guest agent ignores `~/.ssh/authorized_keys` derived from metadata and requires an OS Login identity
- s2: the presented public key is not accepted by `sshd`
- D: client receives `Permission denied (publickey).`
**Indicators:**
- root: [Step 5] `enable-oslogin` resolves to `TRUE` at instance or project level
- s1: [Step 1] troubleshooter flags the OS Login / IAM permissions check as failing
- D: [Symptom] `Permission denied (publickey).`
**Interventions:**
- **remediation** (root): grant the user an OS Login IAM role and connect through OS Login (let `gcloud` provision the key) instead of a metadata key.

  ```bash
  gcloud compute instances add-iam-policy-binding VM_NAME --zone=ZONE \
    --member="user:USER_EMAIL" \
    --role="roles/compute.osLogin"
  gcloud compute ssh VM_NAME --zone=ZONE --tunnel-through-iap
  ```

  **Verification:** re-run Step 1; the OS Login check passes and the SSH session opens without `publickey` errors.
- **defensive_fix** (s1): if metadata keys are intentionally required for this VM, disable OS Login on the instance so metadata keys are honored again.

  ```bash
  gcloud compute instances add-metadata VM_NAME --zone=ZONE \
    --metadata enable-oslogin=FALSE
  ```

  **Verification:** re-run Step 5 and confirm the instance value is `FALSE`; the metadata-keyed SSH connection succeeds.

### Cause C: Boot disk is full, blocking authorized_keys provisioning
**Statement:** The VM's boot disk has no free space, so the guest environment cannot write the session public key into `~/.ssh/authorized_keys` and the login fails.
**Chain:**
- root: boot disk is 100% full ("No space left on device")
- s1: guest environment cannot create/update `~/.ssh/authorized_keys` for the session
- s2: `sshd` has no matching authorized key for the connecting user
- D: SSH login fails (publickey / could-not-connect)
**Indicators:**
- root: [Step 4] serial log contains `No space left on device`
- s1: [Step 3] serial log shows guest-agent / `authorized_keys` write failures
- D: [Symptom] `Permission denied (publickey).` or `Could not connect, retrying...`
**Interventions:**
- **remediation** (root): stop the VM, grow the boot disk, then start it and (if the image does not auto-grow) extend the partition and filesystem.

  ```bash
  gcloud compute instances stop VM_NAME --zone=ZONE
  gcloud compute disks resize BOOT_DISK_NAME --zone=ZONE --size=DISK_SIZE
  gcloud compute instances start VM_NAME --zone=ZONE
  # On the VM, if the filesystem did not auto-grow:
  sudo growpart /dev/sda 1
  sudo resize2fs /dev/sda1
  ```

  **Verification:** after restart run `df -h` on the VM and confirm free space on `/`; re-run Step 4 and confirm `No space left on device` no longer appears; SSH succeeds.
- **mitigation** (s1): attach a startup script that frees space (e.g. clear logs) so the VM can boot and accept SSH, then remove the script.

  ```bash
  gcloud compute instances add-metadata VM_NAME --zone=ZONE \
    --metadata startup-script='#! /bin/bash
  journalctl --vacuum-size=50M
  find /var/log -type f -name "*.gz" -delete'
  gcloud compute instances reset VM_NAME --zone=ZONE
  ```

  **Risk:** deletes log/archive data, which may destroy diagnostic evidence; an aggressive script could remove needed files. **Duration:** remove the `startup-script` metadata immediately after the next successful login. **Verification:** SSH in, run `df -h` to confirm reclaimed space, then `gcloud compute instances remove-metadata VM_NAME --zone=ZONE --keys=startup-script`.

### Cause D: Guest agent or sshd is not running after boot
**Statement:** The `google-guest-agent` service (which provisions SSH keys) or the `sshd` daemon failed to start, so the VM does not accept SSH connections even though the network path is open.
**Chain:**
- root: `google-guest-agent.service` (or `sshd`) is stopped/failed on the VM
- s1: SSH keys are not provisioned and/or no daemon is listening on port 22
- D: client sees `Connection refused` or `Could not connect, retrying...`
**Indicators:**
- root: [Step 3] serial log shows `google-guest-agent` or `sshd` failing to start / inactive
- s1: [Step 1] troubleshooter flags the guest-agent / VM-status check as failing
- D: [Symptom] `Connection refused` or `Could not connect, retrying...`
**Interventions:**
- **remediation** (root): via the serial console (or a rescue boot), enable and start the guest agent and sshd, then confirm they are active.

  ```bash
  sudo systemctl enable --now google-guest-agent.service
  sudo systemctl enable --now sshd.service
  sudo systemctl status google-guest-agent.service sshd.service
  ```

  **Verification:** `systemctl status` reports both services `active (running)`; re-run Step 1 and confirm the guest-agent check passes; SSH connects.
- **mitigation** (s1): if the services cannot be recovered in place, reset the instance to force a clean boot of the guest environment.

  ```bash
  gcloud compute instances reset VM_NAME --zone=ZONE
  ```

  **Risk:** a hard reset interrupts running workloads and can lose in-memory state. **Duration:** one-time recovery action. **Verification:** after reset, re-run Step 4 (no `emergency mode`) and Step 1; SSH succeeds.

### Cause E: VM boots into emergency mode (filesystem/fstab fault)
**Statement:** A filesystem error or a bad `/etc/fstab` entry drops the VM into systemd emergency mode before networking and sshd come up, leaving it unreachable over SSH.
**Chain:**
- root: corrupted filesystem or invalid `/etc/fstab` entry halts normal boot
- s1: systemd enters emergency mode; `sshd` and networking never start
- D: SSH connection times out / is refused
**Indicators:**
- root: [Step 4] serial log contains `emergency mode`
- s1: [Step 3] serial log shows boot halting before `sshd` / network targets reached
- D: [Symptom] `Operation timed out` / `Connection refused` on port 22
**Interventions:**
- **remediation** (root): detach the boot disk and attach it to a temporary rescue VM (or use the GCE Rescue tool) to fix `/etc/fstab` and run a filesystem check, then reattach.

  ```bash
  gcloud compute instances stop VM_NAME --zone=ZONE
  gcloud compute instances detach-disk VM_NAME --zone=ZONE --disk=BOOT_DISK_NAME
  gcloud compute instances attach-disk RESCUE_VM --zone=ZONE \
    --disk=BOOT_DISK_NAME --device-name=recovery
  # On RESCUE_VM: fsck the disk and correct /etc/fstab, e.g.
  #   sudo fsck -y /dev/disk/by-id/google-recovery-part1
  #   sudo mount /dev/disk/by-id/google-recovery-part1 /mnt && edit /mnt/etc/fstab
  ```

  **Verification:** reattach the disk to the original VM, start it, re-run Step 4 (`emergency mode` absent), and confirm SSH succeeds.

### Cause Z: Unidentified
**Statement:** None of the known causes above match the collected evidence; the root cause is not yet identified.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the Compute Engine SME / Google Cloud Support.

  ```bash
  gcloud compute instances describe VM_NAME --zone=ZONE > vm-describe.txt
  gcloud compute instances get-serial-port-output VM_NAME --zone=ZONE > serial.log
  gcloud compute firewall-rules list > firewall-rules.txt
  gcloud compute ssh VM_NAME --zone=ZONE --troubleshoot --tunnel-through-iap > ssh-troubleshoot.txt 2>&1
  ```

  **Risk:** snapshot is read-only and safe; escalation may add latency. **Duration:** until SME review. **Verification:** the four artifacts are attached to the case and the SME confirms receipt.

## Prevention

- Standardize SSH access on IAP TCP forwarding: create one ingress rule allowing `tcp:22` from `35.235.240.0/20` and remove broad `0.0.0.0/0` port-22 rules so identity (not network) governs access.
- Enable OS Login project-wide and grant `roles/compute.osLogin` via IAM rather than distributing metadata SSH keys, keeping key/identity management centralized.
- Alert on boot-disk utilization (Ops Agent / Monitoring `disk/used_percent`) at 80% and 90% so full-disk lockouts are caught before they block SSH; size boot disks with headroom for logs.
- Add a Monitoring uptime/health signal and alert on instances entering non-running or repeated-reset states so guest-agent/emergency-mode boot faults surface proactively.
- Keep the Google guest environment package up to date in your images so `google-guest-agent` key provisioning stays reliable.

## Sources

- [Troubleshooting ssh errors](https://docs.cloud.google.com/compute/docs/troubleshooting/troubleshooting-ssh-errors) — core SSH troubleshooter usage (`--troubleshoot`, `--tunnel-through-iap`), serial console access, OS Login vs metadata key conflict, full-disk authorized_keys failure, `.ssh` permission requirements, exact error strings (`Permission denied (publickey)`, `Could not connect, retrying...`), `systemctl status/restart sshd.service`, `google-guest-agent.service` enable/start, emergency-mode and firewall `tcp:22` grep checks.
- [General tips for using Compute Engine](https://docs.cloud.google.com/compute/docs/troubleshooting/general-tips) — interactive serial-console access, the direct-internet-access conditions (external IP plus a default route to the internet gateway), and `gcloud compute ssh --ssh-key-file`.
- [Troubleshooting disk full resize](https://docs.cloud.google.com/compute/docs/troubleshooting/troubleshooting-disk-full-resize) — full boot disk detection (`No space left on device`), `gcloud compute instances stop/start`, `gcloud compute disks resize --size`, `growpart`/`resize2fs`/`df -h`, startup-script recovery.
- [Network access](https://docs.cloud.google.com/compute/docs/connect/ssh-best-practices/network-access) — IAP firewall best practice and the `35.235.240.0/20` IAP TCP-forwarding source range; `gcloud compute firewall-rules create ... --source-ranges=35.235.240.0/20`.
- [Using tcp forwarding](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding) — IAP TCP forwarding source range and the requirement to allow `35.235.240.0/20` ingress to port 22.
