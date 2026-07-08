---
id: "k8s-node-notready"
title: "Kubernetes Node NotReady"
domain: compute
service: kubernetes
symptom_class: [node_failure]
severity: critical
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [kubernetes, nodes, kubelet, resource-pressure, containerd, certificates]
difficulty: intermediate
---

## Symptom Recognition

Node reports `NotReady` status in `kubectl get nodes` output. The node controller marks `Ready` condition `Unknown` after `node-monitor-grace-period` (default 40 seconds) without a kubelet heartbeat. The control plane simultaneously taints the node with `node.kubernetes.io/unreachable:NoExecute` and `node.kubernetes.io/unreachable:NoSchedule`. After the toleration period (default 300 seconds), pods on the affected node enter `Terminating` and are rescheduled. Specific observable signals:

- `kubectl get nodes` shows `STATUS: NotReady` for one or more nodes.
- `kubectl describe node <name>` shows `Ready: Unknown` with `Reason: NodeStatusUnknown`.
- Node events include `"Kubelet stopped posting node status"`.
- Pods on the node may show `Terminating` without progressing.
- Alertmanager fires `KubernetesNodeNotReady` alert (if configured).

## Applicability

Applies to Kubernetes 1.24+ clusters on any distribution (kubeadm, EKS, GKE, AKS, k3s, self-managed). Steps 1–3 require only `kubectl` access with `get`/`describe` rights on `nodes` and `pods`. Steps 4–8 require SSH access to the affected node and root or sudo privileges. Tools needed: `kubectl`, `systemctl`, `journalctl`, `openssl`, `crictl`, `df`, `free`, `ps`, `curl`.

## Diagnostic Steps

### Step 1: Identify NotReady nodes and review conditions

```bash
kubectl get nodes -o wide
kubectl describe node <node-name>
```

Expected output: `STATUS` column shows `NotReady`. The `Conditions` block in `describe` shows `Ready: Unknown` (kubelet silent) or `Ready: False` (kubelet reporting but unhealthy). Note last heartbeat timestamp and which pressure conditions are `True` or `Unknown`.

### Step 2: Inspect node events and applied taints

```bash
kubectl get events --field-selector involvedObject.kind=Node,involvedObject.name=<node-name> --sort-by='.lastTimestamp'
kubectl get node <node-name> -o jsonpath='{.spec.taints}' | python3 -m json.tool
```

Expected output: Events showing `"Kubelet stopped posting node status"` or `"Node <name> status is now: NodeNotReady"`. Taints include `node.kubernetes.io/unreachable:NoExecute`.

### Step 3: Check pod impact on the affected node

```bash
kubectl get pods --all-namespaces --field-selector spec.nodeName=<node-name> -o wide
kubectl get pods --all-namespaces --field-selector spec.nodeName=<node-name> | grep -E 'Terminating|Unknown'
```

Expected output: List of pods; any stuck in `Terminating` need force-deletion if the node cannot recover. Pods managed by Deployments or StatefulSets will reschedule automatically once the taint eviction timer fires.

### Step 4: Check kubelet service status on the node

```bash
sudo systemctl status kubelet
sudo journalctl -u kubelet -n 200 --no-pager
```

Expected output: `Active: active (running)` when healthy. Stopped kubelet shows `inactive (dead)` or `failed`. Journal reveals the last error: certificate expiry (`x509: certificate has expired`), API server unreachable (`connection refused`, `context deadline exceeded`), PLEG unhealthy (`PLEG is not healthy`), or container runtime failure (`failed to connect to containerd`).

### Step 5: Check container runtime health

```bash
sudo systemctl status containerd
sudo journalctl -u containerd -n 100 --no-pager
sudo crictl ps
sudo crictl info
```

Expected output: `Active: active (running)` for containerd. `crictl ps` lists running containers. A failed runtime shows errors such as `failed to connect to containerd.sock` or `CRI v1 runtime API is not implemented`. If containerd is down, the kubelet cannot manage pods and reports NotReady.

### Step 6: Check node resource usage

```bash
df -h
df -hi
free -h
ps aux | wc -l
cat /proc/sys/kernel/pid_max
uptime
dmesg | tail -30
```

Expected output: `df -h` shows filesystem utilization percentages; values above 85% on `/` or `/var` can trigger DiskPressure. `free -h` shows available memory; low `available` column triggers MemoryPressure. `ps aux | wc -l` approaching `pid_max` triggers PIDPressure. `dmesg` may reveal OOM kills or hardware errors.

### Step 7: Test network connectivity from the node to the API server

```bash
grep server /etc/kubernetes/kubelet.conf
curl -k https://<api-server-ip>:6443/healthz
nslookup kubernetes.default.svc.cluster.local
ping -c 3 <api-server-ip>
ip route show
```

Expected output: `curl` returns `ok`, `nslookup` resolves, `ping` succeeds. Failures indicate a network partition, firewall rule, or routing change between the node and the control plane. A successful ping with a failed `curl` suggests a firewall or TLS issue on port 6443.

### Step 8: Check kubelet certificate validity

```bash
sudo openssl x509 -in /var/lib/kubelet/pki/kubelet-client-current.pem -noout -dates
sudo openssl x509 -in /var/lib/kubelet/pki/kubelet.crt -noout -dates 2>/dev/null
sudo openssl x509 -in /etc/kubernetes/pki/ca.crt -noout -dates
```

Expected output: `notAfter` dates in the future for healthy certs. If `notAfter` is in the past, the certificate has expired and the kubelet cannot authenticate to the API server. The kubelet journal from Step 4 will also contain `x509: certificate has expired or not yet valid`.

## Causes

### Cause A: Kubelet process stopped or crashed

**Statement:** The kubelet systemd service exited or was killed, so the node stopped sending heartbeats to the API server.

**Chain:**
- root: The kubelet systemd service exited (panic, OOM kill, config error, or failed upgrade).
- s1: The kubelet stops posting Node status and renewing the kube-node-lease Lease every 10 seconds.
- s2: After `node-monitor-grace-period` (default 40s) the node controller marks `Ready: Unknown` and taints the node.
- D: The node reports NotReady (Symptom).

**Indicators:**
- root: [Step 4] `systemctl status kubelet` shows `Active: failed` or `Active: inactive (dead)`; journal shows `kubelet.service: Main process exited`.
- D: [Symptom] node shows `NotReady` in `kubectl get nodes`.

**Interventions:**
- **mitigation** (root): restart the kubelet to restore heartbeats without stopping running containers.

  ```bash
  sudo systemctl restart kubelet
  sudo systemctl status kubelet
  ```

  **Risk:** Low. Running containers continue while kubelet is down; restarting kubelet reconnects without stopping containers. **Duration:** Kubelet reconnects and node transitions to Ready within 30–60 seconds. **Verification:** `kubectl get node <node-name>` shows `Ready` within 60 seconds.
- **remediation** (root): find and fix the underlying exit cause so kubelet stays up.

  ```bash
  # Identify why kubelet exited and fix root cause
  sudo journalctl -u kubelet -n 500 --no-pager | grep -i "error\|panic\|fatal"
  # For config errors: validate kubelet config
  sudo kubelet --config /var/lib/kubelet/config.yaml --dry-run 2>&1 | head -20
  ```

  **Verification:** `kubectl get node <node-name>` shows `Ready`; `kubectl get lease <node-name> -n kube-node-lease -o jsonpath='{.spec.renewTime}'` is within the last 15 seconds. If a bad config caused the exit, restore the previous config from backup and restart.

---

### Cause B: Container runtime (containerd) crashed or is unresponsive

**Statement:** The containerd runtime crashed or hung, so the kubelet lost the CRI socket connection and reported NotReady.

**Chain:**
- root: containerd crashed or became unresponsive on the node.
- s1: The kubelet loses the CRI socket and cannot list pods, create containers, or get PLEG events.
- s2: The kubelet logs a PLEG health failure and stops reporting a healthy node status.
- D: The control plane marks the node NotReady (Symptom).

**Indicators:**
- root: [Step 5] `systemctl status containerd` shows `Active: failed` or `Active: inactive`; `crictl ps` fails with `connect: no such file or directory`.
- s2: [Step 4] kubelet journal contains `PLEG is not healthy` or `failed to connect to containerd`.

**Interventions:**
- **mitigation** (root): restart containerd and kubelet to re-establish the CRI connection.

  ```bash
  sudo systemctl restart containerd
  sudo systemctl restart kubelet
  ```

  **Risk:** Medium. Restarting containerd briefly disrupts all containers on the node; containers with restart policies recover automatically. **Duration:** Containers are briefly disrupted; node should return Ready within 2–5 minutes. **Verification:** `sudo crictl ps` lists running containers; node transitions to `Ready`.
- **remediation** (root): clear corrupted containerd metadata when it repeatedly fails to start.

  ```bash
  sudo systemctl stop kubelet
  sudo systemctl stop containerd
  sudo rm -f /var/lib/containerd/io.containerd.metadata.v1.bolt/meta.db
  sudo systemctl start containerd
  sudo systemctl start kubelet
  ```

  **Verification:** `sudo crictl ps` lists running containers; PLEG health error disappears from kubelet journal; node transitions to `Ready`. Deleting `meta.db` forces containerd to rediscover running containers; restore the file from backup or restart containerd again if needed.

---

### Cause C: Node disk pressure (filesystem full or near-full)

**Statement:** The node's root or data filesystem reached the kubelet eviction threshold, triggering DiskPressure and eventually preventing kubelet from writing state files.

**Chain:**
- root: A node filesystem filled toward capacity (logs, images, ephemeral data).
- s1: `nodefs.available` < `evictionHard.nodefs.available` (10%) or `imagefs.available` < threshold (15%); kubelet sets `DiskPressure: True`, evicts pods, prunes images.
- s2: At 100% full, kubelet cannot write its own state files or container logs and becomes unresponsive.
- D: The node transitions to NotReady (Symptom).

**Indicators:**
- s1: [Step 1] `kubectl describe node` shows `DiskPressure: True`.
- root: [Step 6] `df -h` shows a filesystem at 90%+ on `/`, `/var`, or the containerd data directory.

**Interventions:**
- **mitigation** (root): reclaim disk by pruning images, exited containers, and vacuuming journal logs.

  ```bash
  sudo crictl rmi --prune
  sudo crictl rm $(sudo crictl ps -a -q --state exited)
  sudo journalctl --vacuum-size=500M
  sudo du -sh /var/log/* | sort -rh | head -10
  ```

  **Risk:** Low to medium. Pruning images and exited containers is safe; removing log files requires care. **Duration:** DiskPressure clears within 60 seconds of the kubelet's next housekeeping cycle once disk is freed. **Verification:** `df -h` shows utilization below eviction threshold.
- **remediation** (root): bound container log growth via kubelet log rotation to prevent recurrence.

  ```yaml
  # /var/lib/kubelet/config.yaml
  containerLogMaxSize: "50Mi"
  containerLogMaxFiles: 3
  ```

  **Verification:** after `sudo systemctl restart kubelet`, `kubectl describe node <node-name> | grep DiskPressure` shows `False` and the node returns `Ready`. Log rotation limits apply to new container log files only; remove the fields and restart kubelet to roll back.

---

### Cause D: Node memory pressure (OOM or MemoryPressure eviction)

**Statement:** The node exhausted available memory, causing the Linux OOM killer to terminate kubelet or containerd, or triggering the kubelet's MemoryPressure eviction path.

**Chain:**
- root: A workload or process drove node memory toward exhaustion.
- s1: `memory.available` < `evictionHard.memory.available` (100Mi); kubelet sets `MemoryPressure: True` and evicts BestEffort/Burstable pods.
- s2: If the kernel OOM killer fires first, it kills the kubelet or containerd process, immediately stopping heartbeats.
- D: The node transitions to NotReady (Symptom), bypassing the eviction path if OOM-killed.

**Indicators:**
- s1: [Step 1] `kubectl describe node` shows `MemoryPressure: True` or `MemoryPressure: Unknown`.
- s2: [Step 6] `dmesg | grep -i "oom"` shows `oom-kill` entries naming `kubelet` or `containerd`; `free -h` shows near-zero `available`.

**Interventions:**
- **mitigation** (s2): restart the OOM-killed processes and identify the memory consumer before pods respawn.

  ```bash
  sudo systemctl restart kubelet
  sudo systemctl restart containerd
  kubectl top pods --all-namespaces --sort-by=memory | head -20
  ```

  **Risk:** Low. Restarting processes after OOM kill is safe; identify the memory consumer before pods respawn and repeat the OOM cycle. **Duration:** Node returns Ready within 60–90 seconds after service restart, but pressure may persist if the root consumer is not removed. **Verification:** `free -h` shows healthy `available` memory; node returns `Ready`.
- **remediation** (root): bound workload memory and reserve memory for node daemons.

  ```bash
  # Set memory limits on workloads identified in kubectl top output
  kubectl set resources deployment/<name> --limits=memory=512Mi --requests=memory=256Mi -n <namespace>
  ```

  ```yaml
  # Reserve memory for system and kubelet in /var/lib/kubelet/config.yaml:
  systemReserved:
    memory: "256Mi"
  kubeReserved:
    memory: "256Mi"
  evictionHard:
    memory.available: "200Mi"
  ```

  **Verification:** `kubectl describe node <node-name> | grep MemoryPressure` shows `False`; `dmesg | grep oom` produces no new entries after fix. Reserved resources reduce allocatable capacity; remove or raise limits and reduce reserved values to roll back.

---

### Cause E: Network partition between node and API server

**Statement:** A firewall rule change, routing failure, or physical network fault severed connectivity between the node and the Kubernetes API server, blocking kubelet heartbeats.

**Chain:**
- root: A firewall, routing, or physical fault severed the node-to-API-server TCP path (HTTPS port 6443).
- s1: The kubelet cannot renew the kube-node-lease or post status; it queues updates and retries with backoff but cannot reach the server.
- s2: The node controller receives no heartbeat and marks `Ready: Unknown` after the grace period; local containers keep running.
- D: The node reports NotReady (Symptom).

**Indicators:**
- root: [Step 7] `curl -k https://<api-server-ip>:6443/healthz` times out or returns `connection refused`; `ping` fails or shows high loss.
- s1: [Step 4] kubelet journal contains `connection refused` or `context deadline exceeded` contacting the API server.

**Interventions:**
- **mitigation** (root): inspect node firewall and routing to locate the blocked path.

  ```bash
  # Check iptables rules on the node
  sudo iptables -L -n | grep 6443
  # Check routing
  ip route show
  traceroute <api-server-ip>
  ```

  **Risk:** Medium. Modifying firewall or routing rules can affect other traffic; verify before applying. **Duration:** Node returns Ready within 60 seconds of restoring network connectivity. **Verification:** `curl -k https://<api-server-ip>:6443/healthz` returns `ok`.
- **remediation** (root): restore the blocked route or remove the offending firewall rule.

  ```bash
  # If an iptables rule is blocking port 6443, remove it:
  sudo iptables -D OUTPUT -d <api-server-ip> -p tcp --dport 6443 -j DROP
  # If a routing change broke the path, restore the route:
  sudo ip route add <api-server-subnet> via <gateway-ip>
  ```

  **Verification:** `curl -k https://<api-server-ip>:6443/healthz` returns `ok`; node transitions to `Ready` within 60 seconds of kubelet reconnecting. Re-add the dropped rule or remove the restored route to roll back; coordinate firewall changes with the network team.

---

### Cause F: Expired kubelet client certificate

**Statement:** The kubelet's TLS client certificate expired, so it cannot authenticate to the API server even though it is running and the network is reachable.

**Chain:**
- root: Certificate auto-rotation is disabled (or CSR approval is not running), so the kubelet client cert expired past its validity period.
- s1: Every API server call fails with `x509: certificate has expired or not yet valid`.
- s2: Heartbeats stop reaching the control plane even though the kubelet process keeps running.
- D: The node reports NotReady (Symptom).

**Indicators:**
- root: [Step 8] `openssl x509 -in /var/lib/kubelet/pki/kubelet-client-current.pem -noout -dates` shows `notAfter` in the past.
- s1: [Step 4] kubelet journal contains `x509: certificate has expired or not yet valid`.

**Interventions:**
- **mitigation** (root): renew the certificates and restart kubelet to restore authentication.

  ```bash
  sudo kubeadm certs renew all
  sudo systemctl restart kubelet
  ```

  **Risk:** Low. Renewing certificates on a kubeadm cluster does not disrupt running workloads. **Duration:** Node returns Ready within 60 seconds after certificate renewal and kubelet restart. **Verification:** `sudo openssl x509 -in /var/lib/kubelet/pki/kubelet-client-current.pem -noout -dates` shows `notAfter` well in the future.
- **remediation** (root): enable certificate auto-rotation and confirm the CSR approval controller is active.

  ```bash
  # Enable rotateCertificates: true in /var/lib/kubelet/config.yaml, then verify
  # the CSR approval controller is active:
  kubectl get csr --sort-by='.metadata.creationTimestamp' | tail -5
  ```

  **Verification:** node transitions to `Ready`; new CSRs are auto-approved by controller-manager (`--cluster-signing-cert-file`/`--cluster-signing-key-file` set). Remove `rotateCertificates: true` and restart kubelet if rotation causes CSR storms.

---

### Cause G: Node PID pressure (process ID exhaustion)

**Statement:** The number of running processes on the node approached or exceeded the kernel `pid_max` limit, triggering PIDPressure and preventing new process creation.

**Chain:**
- root: A PID leak (zombie processes or runaway forking workload) drove process count toward `pid_max`.
- s1: Remaining PIDs fall below `evictionHard.pid.available` (1000); kubelet sets `PIDPressure: True` and evicts pods.
- s2: At extreme exhaustion the kubelet cannot fork to check container status, stalls, and stops sending heartbeats.
- D: The node transitions to NotReady (Symptom).

**Indicators:**
- s1: [Step 1] `kubectl describe node` shows `PIDPressure: True`.
- root: [Step 6] `ps aux | wc -l` is within 10% of `cat /proc/sys/kernel/pid_max`.

**Interventions:**
- **mitigation** (root): identify and reap leaked or zombie processes to free PIDs.

  ```bash
  # Find top process-owning users
  ps -eo user | sort | uniq -c | sort -rn | head -10
  # Find zombie processes
  ps aux | awk '{if ($8 == "Z") print $0}'
  # Kill the parent of zombie processes
  kill -9 <parent-pid>
  ```

  **Risk:** Low. Identifying and killing leaked processes is safe; killing zombie parents is higher risk. **Duration:** PIDPressure clears within 60 seconds after process count drops below the eviction threshold. **Verification:** `ps aux | wc -l` is substantially below `pid_max`.
- **remediation** (root): cap pods per namespace and reserve PIDs for system processes.

  ```bash
  # Set PID limits per namespace to prevent runaway pods:
  kubectl create resourcequota pid-quota --hard=pods=100,count/pods=100 -n <namespace>
  ```

  ```yaml
  # Reserve PIDs for system processes in /var/lib/kubelet/config.yaml:
  systemReserved:
    pid: "1000"
  evictionHard:
    pid.available: "500"
  ```

  **Verification:** `kubectl describe node <node-name> | grep PIDPressure` shows `False`; node transitions to `Ready`. Delete the resourcequota and remove the reserved PID fields (then restart kubelet) to roll back.

---

### Cause Z: Unidentified

**Statement:** The node is NotReady but the cause does not match any specific pattern in this runbook.

**Chain:**
- root: An out-of-runbook condition (kernel panic, hardware fault, cloud instance health failure, severe clock skew, or an NPD-detected kernel bug) is degrading the node.
- D: The node reports NotReady (Symptom).

**Indicators:**
- root: [Default] None of the above Cause A–G indicators match.

**Interventions:**
- **mitigation** (D): cordon and drain the node, then capture a full diagnostic snapshot and escalate to the infrastructure SME.

  ```bash
  kubectl cordon <node-name>
  kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --timeout=120s
  sudo dmesg | tail -100
  sudo journalctl -k -n 200
  kubectl cluster-info dump --namespaces kube-system
  ```

  **Risk:** Medium. Draining the node safely migrates workloads before further investigation. **Duration:** Drain completes in 2–10 minutes depending on pod count and grace periods. **Verification:** workloads reschedule to other nodes after drain and cordon; the affected node is investigated or replaced before uncordoning. Also gather the cloud provider instance status page for the SME.

## Prevention

Configure Prometheus alerts to catch node health degradation before it becomes NotReady:

```yaml
groups:
  - name: kubernetes-node-health
    rules:
      - alert: KubernetesNodeNotReady
        expr: kube_node_status_condition{condition="Ready",status="true"} == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Node {{ $labels.node }} is NotReady"
      - alert: KubernetesNodeDiskPressure
        expr: kube_node_status_condition{condition="DiskPressure",status="true"} == 1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Node {{ $labels.node }} has DiskPressure"
      - alert: KubernetesNodeMemoryPressure
        expr: kube_node_status_condition{condition="MemoryPressure",status="true"} == 1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Node {{ $labels.node }} has MemoryPressure"
      - alert: KubernetesNodeDiskUsageHigh
        expr: (node_filesystem_size_bytes{mountpoint="/"} - node_filesystem_avail_bytes{mountpoint="/"}) / node_filesystem_size_bytes{mountpoint="/"} > 0.85
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Node {{ $labels.node }} disk usage above 85%"
```

Reserve system resources in kubelet configuration to prevent workloads from starving node daemons:

```yaml
# /var/lib/kubelet/config.yaml
systemReserved:
  cpu: "100m"
  memory: "256Mi"
  ephemeral-storage: "1Gi"
  pid: "1000"
kubeReserved:
  cpu: "100m"
  memory: "256Mi"
  ephemeral-storage: "1Gi"
evictionHard:
  memory.available: "200Mi"
  nodefs.available: "10%"
  imagefs.available: "15%"
  pid.available: "500"
rotateCertificates: true
containerLogMaxSize: "50Mi"
containerLogMaxFiles: 3
```

Deploy Node Problem Detector to surface hardware and kernel issues proactively:

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/node-problem-detector/master/deployment/npd.yaml
```

Use PodDisruptionBudgets and topology spread constraints to ensure single-node failures do not cause service outages:

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: app-pdb
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: my-app
```

## Sources

- [Kubernetes: Troubleshooting Clusters](https://kubernetes.io/docs/tasks/debug/debug-cluster/) — Priority 1. Official diagnostic guide for node and cluster issues; node SSH steps, kubelet checks.
- [Kubernetes: Node Status](https://kubernetes.io/docs/reference/node/node-status/) — Priority 1. Official reference for node condition types, heartbeat intervals, lease objects, and grace period timings.
- [Kubernetes: Nodes](https://kubernetes.io/docs/concepts/architecture/nodes/) — Priority 1. Node architecture, taint application on NotReady, eviction rate controls, capacity vs allocatable resources.
- [Kubernetes: Troubleshooting kubeadm](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/) — Priority 1. kubeadm-specific certificate renewal (`kubeadm certs renew all`) and kubelet config validation.
