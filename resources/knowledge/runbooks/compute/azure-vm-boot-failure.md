---
id: "azure-vm-boot-failure"
title: "Azure Linux VM fails to boot (kernel panic, fstab/disk errors, agent not Ready)"
domain: compute
service: azure-vm
symptom_class: [service_unavailable]
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [kernel-panic, fstab, emergency-mode, waagent, serial-console, az-vm-repair]
difficulty: advanced
---

## Symptom Recognition

- VM Agent status in the Azure portal is not **Ready**; SSH connection times out or is refused.
- Boot diagnostics / serial console log ends with a kernel panic line, e.g. `Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)` or `Kernel panic - not syncing: Attempted to kill init!`.
- Serial console shows the systemd emergency prompt: `Welcome to emergency mode!` / `You are in emergency mode.` with `Give root password for maintenance`.
- Boot log shows `Timed out waiting for device ...`, `Dependency failed for Local File Systems`, and `Failed to mount /<mountpoint>`.
- Filesystem errors in the boot log: `EXT4-fs (sda1): ... Marking fs in need of filesystem check`, `XFS (sdc1): Unmount and run xfs_repair`, `[FAILED] Failed to mount /data`.
- waagent log shows `An error occurred while retrieving the goal state` (`/var/log/waagent.log`).

## Applicability

- Azure Linux VMs (Ubuntu, RHEL, CentOS, SLES, Oracle Linux, etc.), Generation 1 and Generation 2, managed-disk based, ARM deployment model.
- Required access: an Azure role with read/write/delete on the resource group containing the target VM (Contributor or Owner at the resource group scope — VM Contributor is insufficient for `az vm repair`).
- Tools: Azure CLI 2.0.67 or later (`az --version`), the `vm-repair` CLI extension, and serial console enabled on the VM.
- Outbound port 443 from the repair VM is required for `az vm repair run`. A single repair script can run for at most 90 minutes and cannot be canceled.

## Diagnostic Steps

### Step 1: Capture the serial / boot diagnostics log

```bash
az vm boot-diagnostics enable --name myVM --resource-group myResourceGroup \
  --storage https://mystor.blob.core.windows.net/
az vm boot-diagnostics get-boot-log --name myVM --resource-group myResourceGroup | tail -n 120
```

Expected output: the last screens of the boot console. A healthy VM ends at a login prompt; a failing VM ends at a `Kernel panic`, `emergency mode`, or `Failed to mount` line.

### Step 2: Connect to the serial console and read the failure

```bash
az serial-console connect --name myVM --resource-group myResourceGroup
```

Expected output: an interactive serial console. Scroll to the end of the boot output and note the exact terminal line (`Kernel panic - not syncing: ...`, `Welcome to emergency mode!`, or a `[FAILED] Failed to mount` entry).

### Step 3: Inspect the OS disk from a repair VM (offline)

```bash
az extension add -n vm-repair
az vm repair create -g myResourceGroup -n myVM \
  --repair-username azureuser --repair-password 'P@ssw0rd1234!' --verbose
# SSH into the repair VM, then identify the attached copy of the OS disk:
sudo lsblk -f
```

Expected output: `az vm repair create` reports the new repair resource group and repair VM name; `lsblk -f` lists the attached OS disk partitions, their FSTYPE (ext4/xfs/LVM2_member), UUIDs, and mountpoints.

### Step 4: Check the Azure Linux Agent (provisioning) once the VM is reachable

```bash
sudo systemctl status walinuxagent || sudo systemctl status waagent
sudo tail -n 50 /var/log/waagent.log
curl http://168.63.129.16/?comp=versions
```

Expected output: `Active: active (running)` for a healthy agent; the `curl` to the WireServer returns an XML version list. A failing agent is `inactive`/`failed`, or `waagent.log` shows `An error occurred while retrieving the goal state`, or the `curl` hangs/fails.

## Causes

### Cause A: Bad kernel after upgrade/downgrade leaves no usable initramfs
**Statement:** A recent kernel package change installed a kernel whose initramfs is missing or incompatible, so the kernel cannot mount the root filesystem and panics at boot.
**Chain:**
- root: kernel upgrade/downgrade left the default GRUB kernel without a valid initramfs
- s1: kernel cannot locate the root block device at boot
- D: VM panics before init and never reaches a login prompt
**Indicators:**
- root: [Step 2] serial console shows the panic referencing a missing root fs / initramfs
- s1: [Step 1] boot log ends at the kernel panic banner
- D: [Symptom] VM Agent status is not Ready and SSH is unreachable
**Interventions:**
- **mitigation** (s1): boot the previous known-good kernel from the GRUB menu via the serial console.

  ```bash
  az serial-console connect --name myVM --resource-group myResourceGroup
  # At the GRUB menu, press a key to halt the countdown, select "Advanced options",
  # then choose the previous kernel entry and press Enter to boot it.
  ```

  **Risk:** boots an older kernel that may miss security fixes; default kernel is unchanged so the next reboot panics again. **Duration:** until the broken kernel is fixed or removed. **Verification:** VM reaches a login prompt; `uname -r` shows the previous kernel.
- **remediation** (root): regenerate the initramfs / reinstall the kernel on the OS disk via the repair VM, then swap it back.

  ```bash
  az vm repair run -g myResourceGroup -n myVM --run-on-repair --run-id lin-hello-world --verbose
  # Or perform it manually inside the chroot on the repair VM, e.g. on RHEL:
  #   dracut -f /boot/initramfs-$(uname -r).img $(uname -r)
  # on Ubuntu/Debian:  update-initramfs -u -k all   (and reinstall the kernel package)
  az vm repair restore -g myResourceGroup -n myVM --verbose
  ```

  **Verification:** re-run Step 1; the boot log reaches a login prompt and no `Kernel panic` line is present.

### Cause B: Invalid /etc/fstab entry blocks systemd local-fs target
**Statement:** An `/etc/fstab` entry references a device/UUID that is wrong or absent and lacks the `nofail` option, so systemd waits for the device, fails the local-filesystems target, and drops the VM into emergency mode.
**Chain:**
- root: fstab entry has a wrong UUID/device name and no `nofail` option
- s1: systemd times out waiting for the device and fails `Local File Systems`
- s2: boot is diverted into emergency mode requiring a root password
- D: VM never reaches multi-user; SSH unreachable and agent not Ready
**Indicators:**
- root: [Step 2] fstab review shows an entry pointing at a non-existent device/UUID without `nofail`
- s1: [Step 1] boot log shows the dependency failure for local filesystems
- s2: [Step 2] serial console shows the emergency-mode banner
- D: [Symptom] VM Agent status is not Ready
**Interventions:**
- **mitigation** (s2): in single-user/emergency mode over the serial console, comment out the offending line and reboot.

  ```bash
  vi /etc/fstab          # comment out (#) the failing data-disk line
  mount -a               # validate remaining entries report no errors
  reboot -f
  ```

  **Risk:** the affected filesystem is not mounted until the entry is corrected; commenting a system mount can break the OS. **Duration:** until the entry is fixed and `nofail` added. **Verification:** the VM reaches a bash prompt in the serial console and SSH succeeds.
- **defensive_fix** (root): correct the entry to use the partition UUID and add `nofail` so a missing data disk never blocks boot.

  ```bash
  blkid                  # read the real UUID of the data filesystem
  # Replace the fstab line with the UUID form, e.g.:
  #   UUID=<uuid>  /data   xfs   defaults,nofail   0  0
  mount -a
  ```

  **Verification:** re-run Step 1 after a reboot; no `Dependency failed for Local File Systems` and no emergency-mode banner appear.

### Cause C: Disk/filesystem corruption on a mounted volume
**Statement:** A filesystem on an attached disk is corrupted (ext4 journal/inode or xfs metadata damage), so the mount fails its consistency check and systemd diverts the boot into emergency mode.
**Chain:**
- root: ext4/xfs metadata on the disk is corrupted
- s1: the mount unit fails its filesystem check and cannot mount the volume
- s2: boot is diverted into emergency mode
- D: VM never reaches multi-user; SSH unreachable and agent not Ready
**Indicators:**
- root: [Step 1] boot log shows the kernel filesystem driver flagging corruption
- s1: [Step 1] a specific mount fails
- s2: [Step 2] serial console shows `You are in emergency mode.`
- D: [Symptom] VM Agent status is not Ready
**Interventions:**
- **remediation** (root): take a snapshot, then repair the unmounted filesystem (use the repair VM from Step 3 for root/`/usr`).

  ```bash
  # ext4 (run until it reports "clean"):
  fsck -y /dev/sdc1
  # xfs (check first, then repair; -L only as a last resort — it discards the log and risks data loss):
  xfs_repair -n /dev/rootvg/homelv
  xfs_repair /dev/rootvg/homelv
  ```

  **Verification:** `fsck` exits with a `clean` status (or `xfs_repair` completes without errors); after `az vm repair restore`, re-run Step 1 and confirm the VM boots to a login prompt.

### Cause D: Azure Linux Agent cannot reach the WireServer, so provisioning never completes
**Statement:** The Azure Linux Agent (waagent) is stopped or is blocked from reaching the host WireServer at 168.63.129.16 (firewall/iptables/proxy on ports 80, 443, 32526), so the VM Agent never reports Ready even though the OS itself boots.
**Chain:**
- root: waagent is not running, or outbound access to 168.63.129.16 is blocked
- s1: the agent cannot retrieve the goal state from the Fabric Controller
- D: VM Agent status stays not Ready and extensions/provisioning fail
**Indicators:**
- root: [Step 4] `waagent.log` reports a goal-state retrieval error
- root: [Step 4] WireServer connectivity probe fails
- D: [Symptom] VM Agent status in the portal is not Ready
**Interventions:**
- **remediation** (root): restart the agent and restore outbound access to the host IP.

  ```bash
  sudo systemctl restart walinuxagent || sudo systemctl restart waagent
  sudo iptables -L -n | grep -E '168.63.129.16|:80|:443|:32526'   # ensure not blocked
  curl http://168.63.129.16/?comp=versions                        # must return XML versions
  ```

  **Verification:** re-run Step 4; `systemctl status` shows `active (running)`, the `curl` succeeds, and the portal VM Agent status returns to Ready.
- **defensive_fix** (root): keep the agent current by enabling auto-update so a stale agent does not silently go Not Ready.

  ```bash
  sudo sed -i 's/^AutoUpdate.Enabled=.*/AutoUpdate.Enabled=y/' /etc/waagent.conf
  sudo systemctl restart walinuxagent || sudo systemctl restart waagent
  ```

  **Verification:** `grep AutoUpdate.Enabled /etc/waagent.conf` returns `AutoUpdate.Enabled=y` and the agent restarts cleanly.

### Cause Z: Unidentified
**Statement:** The boot failure does not match any known cause above and requires a full diagnostic capture before escalation.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a complete diagnostic snapshot and escalate to the compute/VM SME.

  ```bash
  az vm boot-diagnostics get-boot-log --name myVM --resource-group myResourceGroup > boot.log
  az vm get-instance-view --name myVM --resource-group myResourceGroup \
    --query "instanceView.statuses" -o json > instance-view.json
  az snapshot create -g myResourceGroup -n myVM-osdisk-snap \
    --source "$(az vm show -g myResourceGroup -n myVM --query storageProfile.osDisk.managedDisk.id -o tsv)"
  ```

  **Risk:** none beyond minor snapshot storage cost; no change to the running VM. **Duration:** until the SME responds. **Verification:** `boot.log`, `instance-view.json`, and the OS-disk snapshot exist and are attached to the escalation ticket.

## Prevention

- Add `nofail` to every non-critical data-disk entry in `/etc/fstab` and mount by UUID (`blkid`), so a missing or corrupt data disk never blocks boot (only `/`, `/usr`, `/var` should omit `nofail`).
- After any kernel change, reboot once in a maintenance window and verify the VM boots before relying on it; keep the previous kernel installed so GRUB can fall back.
- Enable boot diagnostics on every VM (`az vm boot-diagnostics enable`) and ensure the serial console is enabled, so failures are diagnosable without a repair VM.
- Keep `AutoUpdate.Enabled=y` in `/etc/waagent.conf` and never block outbound access to 168.63.129.16 on ports 80/443/32526.
- Schedule periodic filesystem health checks and snapshot OS disks before disruptive maintenance so a known-good restore point exists.

## Sources

- [Virtual machines](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/) — Azure VM troubleshooting hub; entry point to the Linux VM boot-failure articles.
- [Welcome virtual machines linux](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/linux/welcome-virtual-machines-linux) — "VM is not booting" index (kernel panic, fstab, filesystem, agent articles).
- [Linux kernel panic troubleshooting](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/linux/linux-kernel-panic-troubleshooting) — exact kernel-panic strings, serial-console recovery, previous-kernel boot, repair-VM/initramfs path.
- [Linux virtual machine cannot start fstab errors](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/linux/linux-virtual-machine-cannot-start-fstab-errors) — emergency-mode/dependency-failed strings, fstab edit + `nofail`, `mount -a`, `reboot -f`, ALAR `repair-button fstab`.
- [Linux recovery cannot start file system errors](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/linux/linux-recovery-cannot-start-file-system-errors) — ext4/xfs corruption strings, `lsblk -f`, `fsck -y`, `xfs_repair -n/-L`.
- [Linux azure guest agent](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/linux/linux-azure-guest-agent) — waagent status/restart, `/var/log/waagent.log` goal-state error, 168.63.129.16 WireServer `curl` probe, AutoUpdate setting.
- [Repair linux vm using azure virtual machine repair commands](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/linux/repair-linux-vm-using-azure-virtual-machine-repair-commands) — exact `az extension add -n vm-repair`, `az vm repair create/run/restore`, `az vm boot-diagnostics enable` syntax and role requirements.
