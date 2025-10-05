---
id: k8s-node-not-ready
title: "Kubernetes - Node Not Ready"
technology: kubernetes
severity: critical
tags:
  - kubernetes
  - node
  - infrastructure
  - kubelet
difficulty: advanced
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified
---

# Kubernetes - Node Not Ready

> **Purpose**: Diagnose and resolve Kubernetes nodes in NotReady state affecting cluster capacity

## Quick Reference Card

**🔍 Symptoms:**
- Node status shows `NotReady`
- Pods cannot schedule on affected node
- Cluster capacity reduced
- `kubectl get nodes` shows NotReady state

**⚡ Common Causes:**
1. **Kubelet stopped** (40%) - Kubelet service crashed or stopped
2. **Resource exhaustion** (30%) - Node out of disk, memory, or CPU
3. **Network issues** (20%) - Node cannot communicate with API server
4. **Container runtime failure** (10%) - Docker/containerd stopped or failing

**🚀 Quick Fix:**
```bash
# Check node status
kubectl describe node <node-name>

# Restart kubelet (on the node)
sudo systemctl restart kubelet
```

**⏱️ Estimated Resolution Time:** 10-30 minutes

---

## Diagnostic Steps

### Step 1: Identify NotReady Nodes

```bash
# List all nodes with status
kubectl get nodes

# Get detailed node status
kubectl describe node <node-name>

# Check node conditions
kubectl get node <node-name> -o jsonpath='{.status.conditions[*].type}{"\n"}{.status.conditions[*].status}'
```

**Expected output:**
```
NAME         STATUS     ROLES    AGE   VERSION
node-1       Ready      master   10d   v1.28.0
node-2       NotReady   worker   10d   v1.28.0
node-3       Ready      worker   10d   v1.28.0
```

### Step 2: Check Node Conditions

```bash
# View all node conditions
kubectl describe node <node-name> | grep -A 10 Conditions

# Check specific conditions
kubectl get node <node-name> -o json | jq '.status.conditions'
```

**Key conditions:**
- `Ready`: Kubelet is healthy and ready to accept pods
- `MemoryPressure`: Node has memory pressure
- `DiskPressure`: Node has disk pressure
- `PIDPressure`: Too many processes running
- `NetworkUnavailable`: Network not configured

### Step 3: Check Kubelet Status

```bash
# SSH to the node
ssh <node-address>

# Check kubelet service
sudo systemctl status kubelet

# View kubelet logs
sudo journalctl -u kubelet -n 100 --no-pager

# Check kubelet is running
ps aux | grep kubelet
```

### Step 4: Check Resource Usage

```bash
# On the node, check resources
df -h  # Disk usage
free -h  # Memory usage
top  # CPU and process usage

# Check for full disk
df -h / /var /var/lib/docker

# Check inode usage
df -i
```

---

## Solutions

### Solution 1: Restart Kubelet

**When to use:** Kubelet service stopped or in failed state

```bash
# SSH to node
ssh <node-address>

# Restart kubelet
sudo systemctl restart kubelet

# Check status
sudo systemctl status kubelet

# Verify node becomes ready
kubectl get node <node-name> -w
```

**Time to resolution:** ~2-5 minutes

### Solution 2: Clear Disk Space

**When to use:** Node has disk pressure or full disk

```bash
# SSH to node
ssh <node-address>

# Clean up Docker/containerd images
sudo docker system prune -a -f
# OR for containerd:
sudo crictl rmi --prune

# Clean up old logs
sudo journalctl --vacuum-time=3d

# Remove unused containers
sudo docker container prune -f
# OR:
sudo crictl rmp -a

# Clear package cache
sudo apt-get clean  # Debian/Ubuntu
sudo yum clean all  # RHEL/CentOS
```

**Time to resolution:** ~10 minutes

### Solution 3: Fix Network Connectivity

**When to use:** NetworkUnavailable condition or API server unreachable

```bash
# Test API server connectivity from node
curl -k https://<api-server-ip>:6443/healthz

# Check network plugin
kubectl get pods -n kube-system | grep -E 'calico|flannel|weave'

# Restart network plugin (example for Calico)
kubectl delete pod -n kube-system -l k8s-app=calico-node

# Check node network configuration
ip addr
ip route
```

**Time to resolution:** ~15-20 minutes

### Solution 4: Restart Container Runtime

**When to use:** Container runtime (Docker/containerd) stopped

```bash
# For Docker
sudo systemctl restart docker
sudo systemctl status docker

# For containerd
sudo systemctl restart containerd
sudo systemctl status containerd

# Restart kubelet after runtime restart
sudo systemctl restart kubelet
```

**Time to resolution:** ~5 minutes

---

## Root Cause Analysis

**Why NotReady happens:**

Nodes become NotReady when kubelet cannot communicate with the API server or node conditions fail health checks.

**Common triggers:**
1. Kubelet service crashes due to resource exhaustion
2. Disk fills up preventing container operations
3. Network partition between node and control plane
4. Container runtime crashes or hangs
5. Certificate expiration for kubelet authentication

---

## Prevention

### Immediate Prevention

1. **Monitor node status**
   ```bash
   # Alert on NotReady nodes
   kubectl get nodes -o json | jq -r '.items[] | select(.status.conditions[] | select(.type=="Ready" and .status!="True")) | .metadata.name'
   ```

2. **Set up disk usage alerts**
   ```yaml
   # Prometheus alert
   - alert: NodeDiskPressure
     expr: kube_node_status_condition{condition="DiskPressure",status="true"} == 1
     for: 5m
   ```

3. **Reserve system resources**
   ```yaml
   # Kubelet config
   systemReserved:
     cpu: 100m
     memory: 1Gi
   kubeReserved:
     cpu: 100m
     memory: 1Gi
   evictionHard:
     memory.available: "500Mi"
     nodefs.available: "10%"
   ```

### Long-Term Prevention

1. **Automated disk cleanup**
   ```bash
   # Cron job for image cleanup
   0 2 * * * docker system prune -f --filter "until=72h"
   ```

2. **Node auto-repair** (GKE, EKS auto-healing)
3. **Monitoring and alerting** (Prometheus, Datadog)
4. **Regular node rotation** (immutable infrastructure)

### Best Practices

- ✅ Monitor node conditions continuously
- ✅ Set resource reservations for system
- ✅ Use node auto-repair when available
- ✅ Configure disk pressure thresholds
- ✅ Implement automated cleanup
- ✅ Monitor kubelet health
- ❌ Don't run nodes at >80% disk capacity
- ❌ Don't ignore MemoryPressure warnings

---

## Related Issues

**Related runbooks:**
- [Kubernetes - Pod CrashLoopBackOff](k8s-pod-crashloopbackoff.md)
- [Kubernetes - Pod OOMKilled](k8s-pod-oomkilled.md)

**External resources:**
- [Node Status](https://kubernetes.io/docs/concepts/architecture/nodes/#condition)
- [Troubleshooting Nodes](https://kubernetes.io/docs/tasks/debug/debug-cluster/)

---

## Version History

| Version | Date       | Author           | Changes                  |
|---------|------------|------------------|--------------------------|
| 1.0.0   | 2025-01-15 | FaultMaven Team  | Initial verified version |

---

## License & Attribution

Licensed under Apache-2.0 License. Contributions welcome via Pull Request.
