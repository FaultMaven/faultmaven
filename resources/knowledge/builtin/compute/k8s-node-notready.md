---
id: "k8s-node-notready"
title: "Kubernetes Node NotReady"
domain: compute
service: kubernetes
symptom_class: [node_failure]
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
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

**Statement:** The kubelet systemd service exited or was killed, causing the node to stop sending heartbeats to the API server.

**Mechanism:** The kubelet is the primary node agent responsible for posting Node status updates and renewing the kube-node-lease Lease object every 10 seconds. When the kubelet process terminates (due to a panic, OOM kill, configuration error, or a failed upgrade), heartbeats stop immediately. After `node-monitor-grace-period` (default 40 seconds) the node controller marks `Ready: Unknown` and taints the node.

**Indicator:**

- [Step 4] `systemctl status kubelet` shows `Active: failed` or `Active: inactive (dead)`.
- [Step 4] Journal contains `kubelet.service: Main process exited` or `failed with result 'exit-code'`.

<!-- match: {"step": 4, "predicate": "contains", "target": "Main process exited"} -->

**Mitigation:**

- **Risk:** Low. Running containers continue while kubelet is down; restarting kubelet reconnects without stopping containers.
- **Command:**

  ```bash
  sudo systemctl restart kubelet
  sudo systemctl status kubelet
  ```

- **Duration:** Kubelet reconnects and node transitions to Ready within 30–60 seconds.

**Resolution:**

```bash
# Identify why kubelet exited and fix root cause
sudo journalctl -u kubelet -n 500 --no-pager | grep -i "error\|panic\|fatal"
# For config errors: validate kubelet config
sudo kubelet --config /var/lib/kubelet/config.yaml --dry-run 2>&1 | head -20
```

- **Impact:** Single-node. Restarting kubelet causes a brief reporting gap but does not evict pods.
- **Rollback:** If kubelet fails to start after a bad config change, restore the previous config from backup and restart.

**Verification:** `kubectl get node <node-name>` shows `Ready` within 60 seconds. `kubectl get lease <node-name> -n kube-node-lease -o jsonpath='{.spec.renewTime}'` is within the last 15 seconds.

---

### Cause B: Container runtime (containerd) crashed or is unresponsive

**Statement:** The containerd runtime crashed or became unresponsive, causing the kubelet to lose the CRI socket connection and report NotReady.

**Mechanism:** The kubelet communicates with the container runtime exclusively through the CRI (Container Runtime Interface) socket. When containerd crashes or hangs, the kubelet cannot list pods, create containers, or retrieve PLEG (Pod Lifecycle Event Generator) events. The kubelet logs a PLEG health failure and stops reporting a healthy node status. The node control plane marks it NotReady.

**Indicator:**

- [Step 5] `systemctl status containerd` shows `Active: failed` or `Active: inactive`.
- [Step 4] Kubelet journal contains `"PLEG is not healthy"` or `"failed to connect to containerd"`.
- [Step 5] `crictl ps` fails with `"connect: no such file or directory"` on the socket path.

<!-- match: {"step": 4, "predicate": "contains", "target": "PLEG is not healthy"} -->

**Mitigation:**

- **Risk:** Medium. Restarting containerd briefly disrupts all containers on the node; containers with restart policies recover automatically.
- **Command:**

  ```bash
  sudo systemctl restart containerd
  sudo systemctl restart kubelet
  ```

- **Duration:** Containers are briefly disrupted; node should return Ready within 2–5 minutes.

**Resolution:**

```bash
# If containerd repeatedly fails, check for corrupted metadata
sudo systemctl stop kubelet
sudo systemctl stop containerd
sudo rm -f /var/lib/containerd/io.containerd.metadata.v1.bolt/meta.db
sudo systemctl start containerd
sudo systemctl start kubelet
```

- **Impact:** Single-node. Deleting `meta.db` forces containerd to rediscover running containers; containers already running on the host are not killed but may be temporarily invisible.
- **Rollback:** Restore `meta.db` from a pre-deletion backup, or simply restart containerd again (it will rebuild the metadata from running container state).

**Verification:** `sudo crictl ps` lists running containers. `kubectl get node <node-name>` transitions to `Ready`. PLEG health error disappears from kubelet journal.

---

### Cause C: Node disk pressure (filesystem full or near-full)

**Statement:** The node's root or data filesystem reached the kubelet eviction threshold, triggering DiskPressure and eventually preventing kubelet from writing state files.

**Mechanism:** The kubelet continuously polls filesystem utilization. When `nodefs.available` drops below `evictionHard.nodefs.available` (default 10%) or `imagefs.available` falls below its threshold (default 15%), the kubelet sets `DiskPressure: True`, evicts pods consuming ephemeral storage, and prunes unused container images. If the disk fills completely (100%), kubelet cannot write its own state files or container logs and becomes unresponsive, causing the node to transition to NotReady.

**Indicator:**

- [Step 1] `kubectl describe node` shows `DiskPressure: True`.
- [Step 6] `df -h` shows filesystem at 90%+ utilization on `/`, `/var`, or the containerd data directory.

<!-- match: {"step": 1, "predicate": "contains", "target": "DiskPressure"} -->
<!-- match: {"step": 6, "predicate": "threshold", "target": "disk_usage_pct", "op": ">", "value": 0.9} -->

**Mitigation:**

- **Risk:** Low to medium. Pruning images and exited containers is safe; removing log files requires care.
- **Command:**

  ```bash
  sudo crictl rmi --prune
  sudo crictl rm $(sudo crictl ps -a -q --state exited)
  sudo journalctl --vacuum-size=500M
  sudo du -sh /var/log/* | sort -rh | head -10
  ```

- **Duration:** DiskPressure condition clears within 60 seconds of the kubelet's next housekeeping cycle once disk is freed.

**Resolution:**

```bash
# Prevent recurrence: configure log rotation in kubelet config
# /var/lib/kubelet/config.yaml
# containerLogMaxSize: "50Mi"
# containerLogMaxFiles: 3
sudo systemctl restart kubelet
```

- **Impact:** Single-node. Log rotation limits apply to new container log files only.
- **Rollback:** Remove `containerLogMaxSize`/`containerLogMaxFiles` from kubelet config and restart kubelet.

**Verification:** `df -h` shows utilization below eviction threshold. `kubectl describe node <node-name> | grep DiskPressure` shows `False`. Node transitions to `Ready`.

---

### Cause D: Node memory pressure (OOM or MemoryPressure eviction)

**Statement:** The node exhausted available memory, causing the Linux OOM killer to terminate the kubelet or containerd process, or triggering the kubelet's MemoryPressure eviction path.

**Mechanism:** The kubelet monitors `memory.available` via cgroups. When available memory drops below `evictionHard.memory.available` (default 100Mi), the kubelet sets `MemoryPressure: True` and begins evicting BestEffort and Burstable pods. If the Linux kernel OOM killer fires first and kills the kubelet or containerd process, the node immediately loses heartbeat and transitions to NotReady without triggering the kubelet eviction path.

**Indicator:**

- [Step 1] `kubectl describe node` shows `MemoryPressure: True` or `MemoryPressure: Unknown`.
- [Step 6] `free -h` shows near-zero `available` memory.
- [Step 6] `dmesg | grep -i "oom"` shows `oom-kill` entries naming `kubelet` or `containerd`.

<!-- match: {"step": 1, "predicate": "contains", "target": "MemoryPressure"} -->
<!-- match: {"step": 6, "predicate": "contains", "target": "oom-kill"} -->

**Mitigation:**

- **Risk:** Low. Restarting processes after OOM kill is safe; identify the memory consumer before pods respawn and repeat the OOM cycle.
- **Command:**

  ```bash
  sudo systemctl restart kubelet
  sudo systemctl restart containerd
  kubectl top pods --all-namespaces --sort-by=memory | head -20
  ```

- **Duration:** Node returns Ready within 60–90 seconds after service restart, but memory pressure may persist if the root consumer is not removed.

**Resolution:**

```bash
# Set memory limits on workloads identified in kubectl top output
kubectl set resources deployment/<name> --limits=memory=512Mi --requests=memory=256Mi -n <namespace>

# Reserve memory for system and kubelet in /var/lib/kubelet/config.yaml:
# systemReserved:
#   memory: "256Mi"
# kubeReserved:
#   memory: "256Mi"
# evictionHard:
#   memory.available: "200Mi"
```

- **Impact:** Deployment-level (limits) or node-wide (reserved resources). Reserved resources reduce allocatable capacity on the node.
- **Rollback:** Remove or raise limits in deployment spec; reduce `systemReserved`/`kubeReserved` values in kubelet config and restart kubelet.

**Verification:** `free -h` shows healthy `available` memory. `kubectl describe node <node-name> | grep MemoryPressure` shows `False`. `dmesg | grep oom` produces no new entries after fix.

---

### Cause E: Network partition between node and API server

**Statement:** A firewall rule change, routing failure, or physical network fault severed connectivity between the node and the Kubernetes API server, preventing kubelet heartbeats from reaching the control plane.

**Mechanism:** The kubelet connects to the API server over HTTPS (port 6443) to renew the kube-node-lease Lease object every 10 seconds and to post node status updates. If the TCP path is blocked or lost, the kubelet queues updates locally and retries with exponential backoff but cannot reach the server. The node controller, not receiving any heartbeat, marks `Ready: Unknown` after the grace period. The kubelet itself may be fully operational on the node and all local containers continue running normally.

**Indicator:**

- [Step 7] `curl -k https://<api-server-ip>:6443/healthz` times out or returns `connection refused`.
- [Step 7] `ping <api-server-ip>` fails or has high packet loss.
- [Step 4] Kubelet journal contains `"context deadline exceeded"` or `"connection refused"` when contacting the API server.

<!-- match: {"step": 7, "predicate": "contains", "target": "context deadline exceeded"} -->
<!-- match: {"step": 4, "predicate": "contains", "target": "connection refused"} -->

**Mitigation:**

- **Risk:** Medium. Modifying firewall or routing rules can affect other traffic; verify before applying.
- **Command:**

  ```bash
  # Check iptables rules on the node
  sudo iptables -L -n | grep 6443
  # Check routing
  ip route show
  traceroute <api-server-ip>
  ```

- **Duration:** Node returns Ready within 60 seconds of restoring network connectivity.

**Resolution:**

```bash
# If an iptables rule is blocking port 6443, remove it:
sudo iptables -D OUTPUT -d <api-server-ip> -p tcp --dport 6443 -j DROP
# If a routing change broke the path, restore the route:
sudo ip route add <api-server-subnet> via <gateway-ip>
```

- **Impact:** Node-level or network-level depending on root cause. Firewall changes may need coordination with network team.
- **Rollback:** Re-add dropped iptables rule; remove restored route if it caused other traffic issues.

**Verification:** `curl -k https://<api-server-ip>:6443/healthz` returns `ok`. Node transitions to `Ready` within 60 seconds of kubelet reconnecting.

---

### Cause F: Expired kubelet client certificate

**Statement:** The kubelet's TLS client certificate expired, preventing it from authenticating to the API server even though it is running and the network is reachable.

**Mechanism:** The kubelet uses a client certificate (stored at `/var/lib/kubelet/pki/kubelet-client-current.pem`) to authenticate to the Kubernetes API server. If certificate auto-rotation is disabled or the CSR approval controller is not running, this certificate expires after its validity period (typically 1 year for kubeadm clusters). Once expired, all API server calls fail with `x509: certificate has expired or not yet valid`, halting heartbeats even though the kubelet process continues running.

**Indicator:**

- [Step 8] `openssl x509 -in /var/lib/kubelet/pki/kubelet-client-current.pem -noout -dates` shows `notAfter` in the past.
- [Step 4] Kubelet journal contains `"x509: certificate has expired or not yet valid"`.

<!-- match: {"step": 4, "predicate": "contains", "target": "x509: certificate has expired"} -->
<!-- match: {"step": 8, "predicate": "contains", "target": "notAfter"} -->

**Mitigation:**

- **Risk:** Low. Renewing certificates on a kubeadm cluster does not disrupt running workloads.
- **Command:**

  ```bash
  sudo kubeadm certs renew all
  sudo systemctl restart kubelet
  ```

- **Duration:** Node returns Ready within 60 seconds after certificate renewal and kubelet restart.

**Resolution:**

```bash
# Enable auto-rotation in kubelet config to prevent recurrence:
# /var/lib/kubelet/config.yaml
# rotateCertificates: true

# Verify the CSR approval controller is active:
kubectl get csr --sort-by='.metadata.creationTimestamp' | tail -5
# Auto-approved CSRs are handled by the controller-manager with:
# --cluster-signing-cert-file and --cluster-signing-key-file set
```

- **Impact:** All nodes in the cluster benefit from enabling `rotateCertificates: true`; change requires kubelet config update and restart on each node.
- **Rollback:** Remove `rotateCertificates: true` from kubelet config and restart kubelet if rotation causes unexpected CSR storms.

**Verification:** `sudo openssl x509 -in /var/lib/kubelet/pki/kubelet-client-current.pem -noout -dates` shows `notAfter` well in the future. Node transitions to `Ready`.

---

### Cause G: Node PID pressure (process ID exhaustion)

**Statement:** The number of running processes on the node approached or exceeded the kernel `pid_max` limit, causing the kubelet to report PIDPressure and preventing new process creation.

**Mechanism:** The kubelet monitors process count via `/proc/sys/kernel/pid_max` and the cgroup PID controller. When processes exceed the kubelet's `evictionHard.pid.available` threshold (default 1000 remaining PIDs), the kubelet sets `PIDPressure: True` and evicts pods. At extreme exhaustion, the kubelet itself cannot fork new processes to check container status, causing it to stall and stop sending heartbeats. PID leaks typically originate from zombie processes or runaway forking workloads.

**Indicator:**

- [Step 1] `kubectl describe node` shows `PIDPressure: True`.
- [Step 6] `ps aux | wc -l` is within 10% of `cat /proc/sys/kernel/pid_max`.

<!-- match: {"step": 1, "predicate": "contains", "target": "PIDPressure"} -->
<!-- match: {"step": 6, "predicate": "threshold", "target": "pid_count_pct_of_pid_max", "op": ">", "value": 0.9} -->

**Mitigation:**

- **Risk:** Low. Identifying and killing leaked processes is safe; killing zombie parents is higher risk.
- **Command:**

  ```bash
  # Find top process-owning users
  ps -eo user | sort | uniq -c | sort -rn | head -10
  # Find zombie processes
  ps aux | awk '{if ($8 == "Z") print $0}'
  # Kill the parent of zombie processes
  kill -9 <parent-pid>
  ```

- **Duration:** PIDPressure clears within 60 seconds after process count drops below the eviction threshold.

**Resolution:**

```bash
# Set PID limits per namespace to prevent runaway pods:
kubectl create resourcequota pid-quota --hard=pods=100,count/pods=100 -n <namespace>

# Reserve PIDs for system processes in kubelet config:
# /var/lib/kubelet/config.yaml
# systemReserved:
#   pid: "1000"
# evictionHard:
#   pid.available: "500"
```

- **Impact:** Namespace-level quota limits affect all pods in that namespace; kubelet reserved PIDs reduce schedulable capacity.
- **Rollback:** Delete the resourcequota; remove PID reserved fields from kubelet config and restart kubelet.

**Verification:** `ps aux | wc -l` is substantially below `pid_max`. `kubectl describe node <node-name> | grep PIDPressure` shows `False`. Node transitions to `Ready`.

---

### Cause Z: Unidentified cause

**Statement:** The node is NotReady but the cause does not match any of the specific patterns in this runbook.

**Mechanism:** Less common causes include kernel panic requiring hard reboot, hardware failure (disk I/O error, NIC fault), cloud provider instance health failure (EC2 system status check, GCE live migration failure), severe clock skew causing TLS failures or lease expiration, or a Node Problem Detector event for a kernel bug. These require deeper OS-level or infrastructure-layer investigation.

**Indicator:**

- [Default] None of the above Cause A–G indicators match.

**Mitigation:**

- **Risk:** Medium. Draining the node safely migrates workloads before further investigation.
- **Command:**

  ```bash
  kubectl cordon <node-name>
  kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --timeout=120s
  ```

- **Duration:** Drain completes in 2–10 minutes depending on pod count and grace periods.

**Resolution:** Out of runbook scope — escalate to infrastructure team. Gather: `sudo dmesg | tail -100`, `sudo journalctl -k -n 200`, cloud provider instance status page, and `kubectl cluster-info dump --namespaces kube-system`.

**Verification:** After drain and cordon, workloads reschedule to other nodes. The affected node is investigated or replaced before uncordoning.

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
