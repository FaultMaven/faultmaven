---
id: k8s-node-notready
title: "Kubernetes Node NotReady"
domain: compute
service: kubernetes
symptom_class:
  - node_failure
severity: critical
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - nodes
  - kubelet
  - cluster
  - resource-pressure
difficulty: intermediate
---

# Kubernetes Node NotReady

## Problem Definition

Applies to Kubernetes 1.24+ clusters on any distribution (kubeadm, EKS, GKE, AKS, self-managed). Requires `kubectl` access with permissions to get and describe nodes and pods. Node-level diagnostics require SSH access to the affected node. Steps 1-3 can be performed remotely via kubectl; Steps 4+ require direct node access.

A Kubernetes node in `NotReady` status indicates that the kubelet on that node has stopped posting heartbeats to the API server. The node controller marks the node as NotReady after the heartbeat timeout (default 40 seconds). The kubelet reports status via node lease objects in the `kube-node-lease` namespace (default update interval: 10 seconds) and periodic node status updates. When heartbeats stop, the node controller sets the `Ready` condition to `Unknown` and taints the node with `node.kubernetes.io/unreachable:NoExecute` and `node.kubernetes.io/unreachable:NoSchedule`. After the pod eviction timeout (default 5 minutes), pods on the NotReady node are terminated and rescheduled elsewhere.

Common root causes include the kubelet process crashing or stopping (service failure, misconfiguration, failed upgrade), resource pressure making the kubelet unresponsive (memory, disk, PID exhaustion), network partition between the node and the API server, certificate expiration (kubelet client certificate preventing API server communication), disk pressure filling the node filesystem and preventing kubelet operation, kernel panic or OS-level failure, container runtime failure (containerd or CRI-O crash), and severe clock skew causing TLS handshake failures or lease expiration.

Typical presentation:

```text
NAME              STATUS     ROLES    AGE   VERSION
kube-worker-01    NotReady   <none>   30d   v1.29.2
kube-worker-02    Ready      <none>   30d   v1.29.2
kube-worker-03    Ready      <none>   30d   v1.29.2
```

Node conditions show `Unknown` status:

```text
Conditions:
  Type             Status    LastHeartbeatTime                 Reason
  ----             ------    -----------------                 ------
  MemoryPressure   Unknown   2026-03-26T10:15:00Z              NodeStatusUnknown
  DiskPressure     Unknown   2026-03-26T10:15:00Z              NodeStatusUnknown
  PIDPressure      Unknown   2026-03-26T10:15:00Z              NodeStatusUnknown
  Ready            Unknown   2026-03-26T10:15:00Z              NodeStatusUnknown
```

## Diagnostic Steps

### Step 1: Identify NotReady Nodes and Check Conditions

**What this checks:** Which nodes are NotReady and what the last-known condition state was before heartbeats stopped.

```bash
# List all nodes and their status
kubectl get nodes -o wide

# Get detailed conditions for the affected node
kubectl describe node <node-name>
```

**Expected output:** The `Conditions` section shows the status of each condition flag.

**What the finding means:**

| Condition | Status | Meaning |
| --------- | ------ | ------- |
| Ready | Unknown | Kubelet stopped reporting (network or kubelet failure) |
| Ready | False | Kubelet is reporting but node is unhealthy |
| MemoryPressure | True | Node is low on memory |
| DiskPressure | True | Node filesystem is nearly full |
| PIDPressure | True | Node is running out of process IDs |

If all conditions show `Unknown`, the kubelet has completely stopped communicating. If `Ready = False`, the kubelet is still running but reporting an unhealthy state.

### Step 2: Check Node Events and Taints

**What this checks:** Recent events on the node and automatically applied taints that affect pod scheduling and eviction.

```bash
# Check events on the node
kubectl events --for node/<node-name>

# Check taints applied to the node
kubectl get node <node-name> -o jsonpath='{.spec.taints}' | jq .
```

**Expected output:** Events showing when the node transitioned to NotReady, and taints including `node.kubernetes.io/unreachable`.

**What the finding means:** The event timestamps show when the node went offline. The `unreachable:NoExecute` taint triggers pod eviction after the toleration timeout (default 300 seconds). The `unreachable:NoSchedule` taint prevents new pods from being scheduled.

### Step 3: Check Impact on Workloads

**What this checks:** Which pods were running on the affected node and their current state.

```bash
# Find pods that were running on the affected node
kubectl get pods --all-namespaces --field-selector spec.nodeName=<node-name> -o wide

# Check for pods in Terminating state (being evicted)
kubectl get pods --all-namespaces --field-selector spec.nodeName=<node-name> | grep Terminating
```

**Expected output:** A list of pods on the node, some potentially in `Terminating` state.

**What the finding means:** Pods managed by controllers (Deployments, StatefulSets) will be rescheduled to other nodes automatically. Standalone pods without controllers will be lost. Pods stuck in `Terminating` may need force deletion if the node does not come back.

### Step 4: Check Kubelet Status on the Node

**What this checks:** Whether the kubelet process is running and what errors it is reporting. Requires SSH access.

```bash
# Check kubelet service status
sudo systemctl status kubelet

# Check if kubelet process is running
ps aux | grep kubelet

# View recent kubelet logs
sudo journalctl -u kubelet -n 100 --no-pager

# Follow kubelet logs in real time
sudo journalctl -u kubelet -f
```

**Expected output:** The systemctl output shows whether the service is `active (running)` or `inactive (dead)` / `failed`. Journal logs show the kubelet's error messages.

**What the finding means:** If the kubelet is stopped, check why it exited (configuration error, binary crash, dependency failure). Look for error messages related to certificate expiration (`certificate has expired`), API server connectivity (`connection refused`, `context deadline exceeded`), resource issues (`failed to get node info`), or PLEG health (`PLEG is not healthy`).

### Step 5: Check Container Runtime

**What this checks:** Whether the container runtime (containerd/CRI-O) is functioning, since the kubelet depends on it.

```bash
# Check containerd status
sudo systemctl status containerd

# Check containerd logs
sudo journalctl -u containerd -n 50 --no-pager

# List running containers
sudo crictl ps

# Check runtime info
sudo crictl info
```

**Expected output:** The containerd service should be `active (running)` and `crictl ps` should list containers.

**What the finding means:** If the runtime is down, the kubelet cannot manage containers and reports NotReady. If the runtime is up but `crictl ps` fails, there may be socket or API version issues between the kubelet and runtime.

### Step 6: Check System Resources

**What this checks:** Whether the node has sufficient disk, memory, PIDs, and CPU to function.

```bash
# Disk usage
df -h
df -hi  # Check inodes too

# Memory usage
free -h

# Process count (PID pressure)
ps aux | wc -l
cat /proc/sys/kernel/pid_max

# System load
uptime
top -bn1 | head -20

# Check for kernel issues
dmesg | tail -50
sudo journalctl -k -n 50 --no-pager
```

**Expected output:** Resource utilization figures and any kernel error messages.

**What the finding means:** If disk is at 100%, the kubelet cannot write state and becomes unresponsive. If memory is exhausted, the OOM killer may have killed the kubelet or containerd. If PID count approaches `pid_max`, process creation fails. Kernel logs showing `oom-kill` or hardware errors indicate OS-level problems.

### Step 7: Check Network Connectivity to API Server

**What this checks:** Whether the node can reach the Kubernetes API server over the network.

```bash
# Get the API server endpoint from kubelet config
cat /etc/kubernetes/kubelet.conf | grep server

# Test connectivity to the API server
curl -k https://<api-server-ip>:6443/healthz

# Check DNS resolution
nslookup kubernetes.default.svc.cluster.local

# Test general network connectivity
ping -c 3 <api-server-ip>
```

**Expected output:** A successful `healthz` response (`ok`) and successful DNS resolution and ping.

**What the finding means:** If `curl` to the API server fails with connection refused or timeout, there is a network partition. If DNS fails, the node's DNS configuration is broken. Check firewall rules, routing tables, and physical/virtual network connectivity.

### Step 8: Check Certificates

**What this checks:** Whether the kubelet's TLS certificates have expired, which prevents communication with the API server.

```bash
# Check kubelet client certificate expiration
sudo openssl x509 -in /var/lib/kubelet/pki/kubelet-client-current.pem -noout -dates

# Check kubelet serving certificate
sudo openssl x509 -in /var/lib/kubelet/pki/kubelet.crt -noout -dates 2>/dev/null

# Check API server CA certificate
sudo openssl x509 -in /etc/kubernetes/pki/ca.crt -noout -dates
```

**Expected output:** Certificate validity dates showing `notBefore` and `notAfter`.

**What the finding means:** If `notAfter` is in the past, the certificate has expired and must be renewed. The kubelet will log `x509: certificate has expired` when this occurs.

## Mitigation

### Option 1: Restart the Kubelet

Use when kubelet is stopped or in a bad state but system resources are healthy.

- **Risk:** Low. Restarting the kubelet causes a brief disruption to status reporting but running containers continue to run.
- **Command:**
  ```bash
  sudo systemctl restart kubelet
  ```
- **Verify:**
  ```bash
  sudo systemctl status kubelet
  kubectl get node <node-name>
  ```
  The node should transition to `Ready` within 30-60 seconds.
- **Duration:** 30 seconds to 2 minutes.

### Option 2: Restart the Container Runtime

Use when the container runtime (containerd/CRI-O) has crashed or is unresponsive.

- **Risk:** Medium. Restarting the runtime briefly disrupts all containers on the node. Containers with proper restart policies will recover.
- **Command:**
  ```bash
  sudo systemctl restart containerd
  sudo systemctl restart kubelet
  ```
- **Verify:**
  ```bash
  sudo crictl ps
  kubectl get node <node-name>
  kubectl get pods --field-selector spec.nodeName=<node-name> --all-namespaces
  ```
  The node should become Ready and pods should return to Running.
- **Duration:** 1 to 5 minutes.

### Option 3: Cordon and Drain the Node

Use when the node is unstable and you want to safely move workloads before further investigation.

- **Risk:** Low. Draining respects PodDisruptionBudgets and gracefully terminates pods.
- **Command:**
  ```bash
  kubectl cordon <node-name>
  kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --timeout=120s
  ```
- **Verify:**
  ```bash
  kubectl get pods --all-namespaces --field-selector spec.nodeName=<node-name>
  kubectl get node <node-name>
  ```
  The node should show `SchedulingDisabled` and have no non-DaemonSet pods.
- **Duration:** 2 to 10 minutes depending on pod count and grace periods.

### Option 4: Free Disk Space

Use when `DiskPressure` is the cause of NotReady.

- **Risk:** Low to Medium depending on what is cleaned.
- **Command:**
  ```bash
  sudo crictl rmi --prune
  sudo journalctl --vacuum-size=500M
  sudo du -sh /var/log/* | sort -rh | head -10
  sudo du -sh /var/lib/containerd/* | sort -rh | head -5
  ```
- **Verify:**
  ```bash
  df -h
  kubectl describe node <node-name> | grep DiskPressure
  ```
  DiskPressure condition should return to `False` and the node should become Ready.
- **Duration:** 2 to 10 minutes.

## Root Cause Resolution

**If** kubelet logs show a panic or fatal error **then** check for recent kubelet configuration changes and revert them:

```bash
sudo cat /var/lib/kubelet/config.yaml
sudo systemctl cat kubelet
kubelet --version
```

**If** the kubelet repeatedly crashes after restart **then** check for corrupted state:

```bash
sudo systemctl stop kubelet
sudo rm -rf /var/lib/kubelet/pods/*/
sudo systemctl start kubelet
```

**If** the node has MemoryPressure **then** identify the top memory consumers and either scale them down or increase node capacity:

```bash
ps aux --sort=-%mem | head -20
kubectl top pods --all-namespaces --sort-by=memory | head -20
```

**If** the node has DiskPressure **then** clean up disk and configure proper log rotation:

```bash
sudo crictl rmi --prune
sudo crictl rm $(sudo crictl ps -a -q --state exited)
```

Configure log rotation in kubelet config: `containerLogMaxSize: "50Mi"`, `containerLogMaxFiles: 3`.

**If** the node has PIDPressure **then** identify and kill leaked processes:

```bash
ps -eo user | sort | uniq -c | sort -rn | head -10
```

**If** the node can be reached via SSH but kubelet cannot reach the API server **then** diagnose the network path:

```bash
ip route show
traceroute <api-server-ip>
sudo iptables -L -n | grep <api-server-ip>
```

Fix firewall rules, routing, or underlying network infrastructure.

**If** kubelet logs show `certificate has expired` or `x509: certificate has expired` **then** renew certificates:

```bash
# For kubeadm-managed clusters
sudo kubeadm certs renew all
sudo systemctl restart kubelet
```

If kubelet certificate auto-rotation is not working, verify the kubelet has RBAC permissions for CSR approval via the `certificates.k8s.io` API.

**If** containerd or CRI-O logs show persistent errors **then** check for corrupted state:

```bash
sudo journalctl -u containerd -n 200 | grep -i "error\|panic\|fatal"

# Reset containerd state if needed (will restart all containers)
sudo systemctl stop kubelet
sudo systemctl stop containerd
sudo rm -rf /var/lib/containerd/io.containerd.metadata.v1.bolt/meta.db
sudo systemctl start containerd
sudo systemctl start kubelet
```

## Verification

After applying a fix, confirm the node recovers and workloads resume.

```bash
# 1. Confirm node transitions to Ready
kubectl get node <node-name> -w
# Node should show Ready status
```

```bash
# 2. Verify all node conditions are healthy
kubectl get node <node-name> -o jsonpath='{.status.conditions}' | jq '.[] | {type, status}'
# Expected: Ready=True, MemoryPressure=False, DiskPressure=False, PIDPressure=False
```

```bash
# 3. Confirm pods are running on the node
kubectl get pods --all-namespaces --field-selector spec.nodeName=<node-name> -o wide
# If the node was drained, uncordon it: kubectl uncordon <node-name>
```

```bash
# 4. Verify kubelet heartbeat is active
kubectl get lease <node-name> -n kube-node-lease -o jsonpath='{.spec.renewTime}'
# renewTime should be within the last 10-40 seconds
```

## Prevention

**Monitor node health proactively.** Set up Prometheus alerts for node conditions before they become NotReady:

```yaml
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
```

**Reserve system resources.** Configure kubelet to reserve resources for system daemons, preventing workloads from consuming all node capacity:

```yaml
# kubelet config
systemReserved:
  cpu: "100m"
  memory: "256Mi"
  ephemeral-storage: "1Gi"
kubeReserved:
  cpu: "100m"
  memory: "256Mi"
  ephemeral-storage: "1Gi"
evictionHard:
  memory.available: "100Mi"
  nodefs.available: "10%"
  imagefs.available: "15%"
```

**Automate certificate rotation.** Ensure kubelet certificate auto-rotation is enabled (default in kubeadm 1.19+):

```yaml
# kubelet config
rotateCertificates: true
```

Verify the cluster has a certificate signing request (CSR) approver running.

**Implement log rotation.** Prevent disk exhaustion from container logs:

```yaml
# kubelet config
containerLogMaxSize: "50Mi"
containerLogMaxFiles: 3
```

Also configure system-level log rotation for `/var/log/kubelet.log` and other system logs via logrotate.

**Deploy Node Problem Detector.** Run the Node Problem Detector DaemonSet to detect and report hardware, kernel, and runtime issues before they cause NotReady:

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/node-problem-detector/master/deployment/npd.yaml
```

**Ensure redundant nodes.** Design clusters with enough spare capacity that a single node going NotReady does not cause service disruption. Use PodDisruptionBudgets and topology spread constraints to distribute critical workloads across nodes.

## Sources

- [Kubernetes: Troubleshooting Clusters](https://kubernetes.io/docs/tasks/debug/debug-cluster/) -- Official guide for diagnosing node and cluster issues
- [Kubernetes: Node Status](https://kubernetes.io/docs/reference/node/node-status/) -- Node condition types, heartbeat mechanism, and lease objects
- [Kubernetes: Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) -- Debugging pod scheduling failures on unhealthy nodes
- [Kubernetes: Troubleshooting kubeadm](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/) -- kubeadm-specific kubelet and certificate issues
