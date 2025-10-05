---
id: k8s-pod-oomkilled
title: "Kubernetes - Pod OOMKilled"
technology: kubernetes
severity: high
tags:
  - kubernetes
  - pod
  - oom
  - memory
  - resources
difficulty: intermediate
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified
---

# Kubernetes - Pod OOMKilled

> **Purpose**: Diagnose and resolve pods being killed due to out-of-memory (OOM) conditions

## Quick Reference Card

**🔍 Symptoms:**
- Pod shows `OOMKilled` status or exit code 137
- Container restarts frequently with memory errors
- Application becomes unresponsive before restart
- `kubectl describe pod` shows "OOMKilled" in events

**⚡ Common Causes:**
1. **Memory limits set too low** (50% of cases) - Container needs more memory than allocated
2. **Memory leak in application** (30% of cases) - Application gradually consumes all available memory
3. **Traffic spike** (15% of cases) - Sudden increase in requests causes memory surge
4. **Large data processing** (5% of cases) - Processing datasets larger than available memory

**🚀 Quick Fix:**
```bash
# Immediately increase memory limits
kubectl set resources deployment/<name> -n <namespace> \
  --limits=memory=2Gi --requests=memory=1Gi
```

**⏱️ Estimated Resolution Time:** 5-10 minutes (quick fix), 1-2 hours (root cause fix)

---

## Diagnostic Steps

### Step 1: Confirm OOMKilled Status

**What to check:** Verify the pod was killed due to out-of-memory

**How to check:**
```bash
# Check pod status
kubectl get pods -n <namespace>

# Look for OOMKilled in pod description
kubectl describe pod <pod-name> -n <namespace>

# Check container exit code (137 = OOMKilled)
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[*].lastState.terminated.exitCode}'
```

**Expected output if OOMKilled:**
```
# In pod events:
Events:
  Type     Reason     Age   From               Message
  ----     ------     ----  ----               -------
  Warning  BackOff    2m    kubelet            Back-off restarting failed container
  Normal   Killing    1m    kubelet            Container app failed liveness probe, will be restarted
  Warning  Failed     1m    kubelet            Error: OOMKilled

# Exit code:
137
```

**What this tells you:**
- Exit code 137 = 128 + 9 (SIGKILL signal sent by OOM killer)
- Container exceeded memory limits
- Linux OOM killer terminated the process

---

### Step 2: Check Current Memory Configuration

**What to check:** Review memory requests and limits for the pod

**How to check:**
```bash
# Get resource limits from deployment
kubectl get deployment <deployment-name> -n <namespace> -o yaml | grep -A 10 resources

# Check actual pod resource configuration
kubectl describe pod <pod-name> -n <namespace> | grep -A 5 "Limits:"

# View in JSON format for clarity
kubectl get deployment <deployment-name> -n <namespace> \
  -o jsonpath='{.spec.template.spec.containers[*].resources}'
```

**Expected output:**
```yaml
resources:
  limits:
    memory: "256Mi"  # Maximum memory before OOMKilled
    cpu: "500m"
  requests:
    memory: "128Mi"  # Minimum guaranteed memory
    cpu: "250m"
```

**What this tells you:**
- If limits are missing, pod can use unlimited memory (node capacity)
- If limits are very low (< 256Mi), likely too restrictive for most apps
- Large gap between requests and limits suggests poor capacity planning

---

### Step 3: Analyze Memory Usage Patterns

**What to check:** Understand actual memory consumption before OOMKilled

**How to check:**
```bash
# Check current memory usage (if pod is running)
kubectl top pod <pod-name> -n <namespace>

# View memory usage for all pods in namespace
kubectl top pods -n <namespace>

# Get historical metrics if metrics-server available
kubectl top pod <pod-name> -n <namespace> --containers

# If using Prometheus, query historical memory
# PromQL: container_memory_usage_bytes{pod="<pod-name>"}
```

**Expected output:**
```
NAME                    CPU(cores)   MEMORY(bytes)
myapp-7d9f8c6b5-4xkz2  45m          245Mi
```

**Analysis:**
- Memory near limit (e.g., 245Mi out of 256Mi limit) → Limits too low
- Memory steadily climbing → Likely memory leak
- Memory spikes correlate with traffic → Need dynamic scaling or higher limits

---

### Step 4: Examine Application Logs

**What to check:** Look for memory-related errors before OOMKilled

**How to check:**
```bash
# Check logs before crash
kubectl logs <pod-name> -n <namespace> --previous --tail=100

# Search for memory-related errors
kubectl logs <pod-name> -n <namespace> --previous | grep -i "memory\|oom\|heap"

# Check for allocation failures
kubectl logs <pod-name> -n <namespace> --previous | grep -i "allocation\|cannot allocate"
```

**Common memory error patterns:**
```
# Java applications
java.lang.OutOfMemoryError: Java heap space
java.lang.OutOfMemoryError: GC overhead limit exceeded

# Node.js applications
FATAL ERROR: CALL_AND_RETRY_LAST Allocation failed - JavaScript heap out of memory
<--- Last few GCs --->

# Python applications
MemoryError
memory error: std::bad_alloc

# Go applications
fatal error: runtime: out of memory
runtime: out of memory
```

---

### Step 5: Check for Memory Leaks

**What to check:** Determine if application has a memory leak

**How to check:**
```bash
# Monitor memory over time (requires running pod)
watch -n 5 'kubectl top pod <pod-name> -n <namespace>'

# Check pod uptime vs memory usage
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.startTime}'
kubectl top pod <pod-name> -n <namespace>

# If memory increases linearly with uptime → leak suspected
```

**Memory leak indicators:**
- Memory usage increases over time without decreasing
- Memory doesn't release after processing completes
- Garbage collection doesn't reclaim memory
- Restarts temporarily fix the issue

---

## Solutions

### Solution 1: Increase Memory Limits (Quick Fix)

**When to use this solution:**
- Current limits are clearly too low for workload
- Application legitimately needs more memory
- Quick mitigation required
- No evidence of memory leak

**Prerequisites:**
- kubectl access with deployment permissions
- Cluster has sufficient node memory capacity
- Understanding of application memory requirements

**Implementation Steps:**

1. **Determine appropriate memory limits**
   ```bash
   # Rule of thumb for initial sizing:
   # - Small apps: 512Mi - 1Gi
   # - Medium apps: 1Gi - 2Gi
   # - Large apps: 2Gi - 4Gi
   # - Data processing: 4Gi+

   # Set requests = 70-80% of limits
   # Example: If limits=2Gi, then requests=1.5Gi
   ```

2. **Update deployment resources**
   ```bash
   # Quick update via kubectl
   kubectl set resources deployment/<deployment-name> -n <namespace> \
     --requests=memory=1Gi,cpu=500m \
     --limits=memory=2Gi,cpu=1000m
   ```

   **Or edit deployment manually:**
   ```bash
   kubectl edit deployment <deployment-name> -n <namespace>
   ```

   Update resources section:
   ```yaml
   spec:
     containers:
     - name: app
       resources:
         requests:
           memory: "1Gi"
           cpu: "500m"
         limits:
           memory: "2Gi"
           cpu: "1000m"
   ```

3. **Monitor rollout**
   ```bash
   # Watch rollout progress
   kubectl rollout status deployment/<deployment-name> -n <namespace>

   # Monitor new pods
   kubectl get pods -n <namespace> -w

   # Check if pods stay running
   kubectl get pods -n <namespace>
   ```

4. **Verify memory usage**
   ```bash
   # Check new pods aren't being OOMKilled
   kubectl describe pod <new-pod-name> -n <namespace> | grep -i oom

   # Monitor memory usage
   kubectl top pod -n <namespace>

   # Should see memory usage well below new limits
   ```

5. **Load test (recommended)**
   ```bash
   # Send traffic to verify stability
   # Monitor memory during load
   kubectl top pod -n <namespace> -w
   ```

**Expected outcome:**
- Pods run without OOMKilled events
- Memory usage stays below 80% of limits
- No restarts due to memory issues

**Time to resolution:** ~5 minutes

**Important:** This is often the right solution if limits were set arbitrarily low initially. However, if memory usage continues to grow, investigate for memory leaks.

---

### Solution 2: Fix Memory Leak in Application

**When to use this solution:**
- Memory usage grows linearly over time
- Restarts temporarily fix the issue
- Increasing limits only delays the problem
- Application code is accessible for fixes

**Implementation Steps:**

1. **Enable memory profiling**

   **For Java applications:**
   ```bash
   # Add JVM flags to deployment
   kubectl set env deployment/<name> -n <namespace> \
     JAVA_OPTS="-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp/heapdump.hprof"
   ```

   **For Node.js applications:**
   ```bash
   # Enable heap snapshots
   kubectl set env deployment/<name> -n <namespace> \
     NODE_OPTIONS="--max-old-space-size=1536 --heapsnapshot-signal=SIGUSR2"
   ```

   **For Go applications:**
   ```bash
   # Enable pprof
   # Add to application code:
   import _ "net/http/pprof"
   go func() {
     log.Println(http.ListenAndServe("localhost:6060", nil))
   }()
   ```

2. **Collect memory profile**
   ```bash
   # Port forward to access profiling endpoint
   kubectl port-forward <pod-name> 6060:6060 -n <namespace>

   # For Go pprof:
   go tool pprof http://localhost:6060/debug/pprof/heap

   # For Java, get heap dump when OOM occurs:
   kubectl cp <pod-name>:/tmp/heapdump.hprof ./heapdump.hprof -n <namespace>
   # Analyze with jhat or Eclipse MAT

   # For Node.js:
   kubectl exec <pod-name> -n <namespace> -- kill -USR2 1
   kubectl cp <pod-name>:/tmp/heapdump.heapsnapshot ./heapdump.heapsnapshot -n <namespace>
   # Analyze with Chrome DevTools
   ```

3. **Analyze and identify leak**
   - Look for objects that accumulate over time
   - Check for unclosed connections (database, HTTP, files)
   - Review cache implementations
   - Check for event listener accumulation
   - Look for circular references

4. **Common memory leak patterns to fix:**

   **Unclosed database connections:**
   ```python
   # Bad
   def query_db():
       conn = db.connect()
       result = conn.execute("SELECT * FROM users")
       return result  # Connection never closed!

   # Good
   def query_db():
       with db.connect() as conn:
           result = conn.execute("SELECT * FROM users")
           return result  # Connection closed automatically
   ```

   **Cache without eviction:**
   ```javascript
   // Bad
   const cache = {};
   function cacheResult(key, value) {
     cache[key] = value;  // Grows forever!
   }

   // Good
   const LRU = require('lru-cache');
   const cache = new LRU({ max: 500 });
   function cacheResult(key, value) {
     cache.set(key, value);  // Old entries evicted
   }
   ```

   **Event listeners not removed:**
   ```javascript
   // Bad
   function setupHandler() {
     window.addEventListener('resize', handler);
     // Never removed!
   }

   // Good
   function setupHandler() {
     window.addEventListener('resize', handler);
     return () => window.removeEventListener('resize', handler);
   }
   ```

5. **Deploy fix and monitor**
   ```bash
   # Build and deploy fixed version
   kubectl set image deployment/<name> <container>=<image>:fixed -n <namespace>

   # Monitor memory usage over extended period
   watch -n 30 'kubectl top pod -n <namespace>'

   # Check after 24+ hours - memory should plateau
   ```

**Expected outcome:**
- Memory usage plateaus at reasonable level
- No continuous growth over time
- Pods run indefinitely without OOMKilled

**Time to resolution:** 1-2 hours (investigation) + development time

---

### Solution 3: Implement Horizontal Pod Autoscaling

**When to use this solution:**
- Memory spikes correlate with traffic increases
- Single pod cannot handle peak load
- Want to maintain low memory per pod
- Application is stateless and can scale horizontally

**Implementation Steps:**

1. **Ensure metrics-server is installed**
   ```bash
   # Check if metrics-server is running
   kubectl get deployment metrics-server -n kube-system

   # If not installed, install it
   kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
   ```

2. **Create HorizontalPodAutoscaler**
   ```bash
   # Create HPA based on memory usage
   kubectl autoscale deployment <deployment-name> -n <namespace> \
     --cpu-percent=70 \
     --memory-percent=80 \
     --min=2 \
     --max=10
   ```

   **Or create HPA manifest:**
   ```yaml
   apiVersion: autoscaling/v2
   kind: HorizontalPodAutoscaler
   metadata:
     name: myapp-hpa
     namespace: production
   spec:
     scaleTargetRef:
       apiVersion: apps/v1
       kind: Deployment
       name: myapp
     minReplicas: 2
     maxReplicas: 10
     metrics:
     - type: Resource
       resource:
         name: memory
         target:
           type: Utilization
           averageUtilization: 80  # Scale when avg memory > 80%
     - type: Resource
       resource:
         name: cpu
         target:
           type: Utilization
           averageUtilization: 70
     behavior:
       scaleDown:
         stabilizationWindowSeconds: 300  # Wait 5 min before scaling down
         policies:
         - type: Percent
           value: 50  # Scale down max 50% at a time
           periodSeconds: 60
       scaleUp:
         stabilizationWindowSeconds: 0  # Scale up immediately
         policies:
         - type: Percent
           value: 100  # Can double pods
           periodSeconds: 30
         - type: Pods
           value: 2  # Or add 2 pods
           periodSeconds: 30
         selectPolicy: Max  # Choose most aggressive
   ```

3. **Apply HPA**
   ```bash
   kubectl apply -f hpa.yaml
   ```

4. **Verify HPA is working**
   ```bash
   # Check HPA status
   kubectl get hpa -n <namespace>

   # Watch HPA in action
   kubectl get hpa -n <namespace> -w

   # Check current metrics
   kubectl describe hpa <hpa-name> -n <namespace>
   ```

5. **Test scaling behavior**
   ```bash
   # Generate load (example with hey)
   hey -z 5m -c 50 http://<service-url>

   # Watch pods scale up
   kubectl get pods -n <namespace> -w

   # After load stops, watch scale down (after stabilization window)
   ```

**Expected outcome:**
- Pods scale up when memory usage increases
- Memory per pod stays within safe limits
- Pods scale down when load decreases
- No OOMKilled events

**Time to resolution:** ~30 minutes

---

### Solution 4: Optimize Application Memory Usage

**When to use this solution:**
- Application genuinely needs less memory
- Cloud costs are a concern
- Cluster capacity is limited
- Application can be optimized

**Implementation Steps:**

1. **Profile application to find optimization opportunities**
   ```bash
   # Use profiling tools to identify memory hotspots
   # See Solution 2 for profiling setup
   ```

2. **Common optimization techniques:**

   **Reduce cache size:**
   ```python
   # Before
   cache = {}  # Unlimited cache

   # After
   from functools import lru_cache
   @lru_cache(maxsize=1000)  # Limited cache
   def expensive_function(arg):
       return result
   ```

   **Stream large files instead of loading fully:**
   ```python
   # Bad - loads entire file in memory
   def process_file(path):
       content = open(path).read()
       return process(content)

   # Good - streams file
   def process_file(path):
       with open(path) as f:
           for line in f:
               process(line)
   ```

   **Use database pagination:**
   ```python
   # Bad - loads all records
   results = db.query("SELECT * FROM large_table")

   # Good - paginated
   offset = 0
   while True:
       results = db.query(f"SELECT * FROM large_table LIMIT 1000 OFFSET {offset}")
       if not results:
           break
       process(results)
       offset += 1000
   ```

   **Optimize data structures:**
   ```javascript
   // Bad - storing entire objects
   const userCache = new Map();
   userCache.set(id, {id, name, email, address, ...allFields});

   // Good - only store what's needed
   const nameCache = new Map();
   nameCache.set(id, name);
   ```

3. **For Java applications, tune JVM:**
   ```bash
   # Set maximum heap size
   kubectl set env deployment/<name> -n <namespace> \
     JAVA_OPTS="-Xmx1536m -Xms512m -XX:+UseG1GC -XX:MaxGCPauseMillis=200"

   # Xmx should be ~75% of container memory limit
   # If limit=2Gi (2048Mi), set Xmx=1536m
   ```

4. **Deploy and verify**
   ```bash
   # Deploy optimized version
   kubectl set image deployment/<name> <container>=<image>:optimized -n <namespace>

   # Monitor memory usage
   kubectl top pod -n <namespace>

   # Should see reduced memory footprint
   ```

**Expected outcome:**
- Lower memory usage per pod
- Same functionality with less resources
- Reduced cloud costs
- Better resource utilization

**Time to resolution:** Varies by optimization complexity (days to weeks)

---

## Root Cause Analysis

**Why OOMKilled happens:**

The Linux OOM (Out of Memory) Killer is triggered when:
1. Container tries to use more memory than its limit
2. Linux kernel's cgroup enforces the limit
3. Kernel sends SIGKILL (signal 9) to the process
4. Kubernetes restarts the container

**Memory limit enforcement in Kubernetes:**
```
Application requests memory
          ↓
Exceeds container limit (cgroup limit)
          ↓
Linux OOM Killer invoked
          ↓
Process killed (exit code 137)
          ↓
Kubernetes restarts container
```

**Common scenarios:**

1. **Underprovisioned Limits** (50%)
   - Developer guessed limits without testing
   - Application requirements grew over time
   - Initial limits copied from example configs
   - Traffic patterns changed

2. **Memory Leaks** (30%)
   - Unclosed connections accumulating
   - Caches growing unbounded
   - Event listeners not removed
   - Circular references preventing GC

3. **Traffic Spikes** (15%)
   - Sudden user surge
   - Batch processing jobs
   - Retry storms
   - DDoS attacks

4. **Large Data Processing** (5%)
   - Loading entire datasets into memory
   - Not streaming large files
   - Inefficient algorithms (O(n²) memory)
   - Holding results instead of yielding

---

## Prevention

### Immediate Prevention (Tactical)

1. **Set memory limits for all containers**
   ```yaml
   resources:
     requests:
       memory: "512Mi"
     limits:
       memory: "1Gi"
   ```

2. **Monitor memory usage proactively**
   ```bash
   # Watch memory usage
   kubectl top pods -n <namespace>

   # Set up alerts
   ```

3. **Use Vertical Pod Autoscaler for recommendations**
   ```bash
   # Install VPA
   kubectl apply -f https://github.com/kubernetes/autoscaler/tree/master/vertical-pod-autoscaler

   # Create VPA in recommendation mode
   ```
   ```yaml
   apiVersion: autoscaling.k8s.io/v1
   kind: VerticalPodAutoscaler
   metadata:
     name: myapp-vpa
   spec:
     targetRef:
       apiVersion: apps/v1
       kind: Deployment
       name: myapp
     updateMode: "Off"  # Recommendation only
   ```

4. **Load test before production**
   ```bash
   # Test with realistic load in staging
   hey -z 10m -c 100 http://staging.example.com

   # Monitor memory during test
   kubectl top pod -n staging -w

   # Set limits based on peak + 30% buffer
   ```

### Long-Term Prevention (Strategic)

1. **Implement proper resource governance**
   ```yaml
   # Set namespace quotas
   apiVersion: v1
   kind: ResourceQuota
   metadata:
     name: compute-resources
     namespace: production
   spec:
     hard:
       requests.memory: "100Gi"
       limits.memory: "200Gi"
       requests.cpu: "50"
       limits.cpu: "100"
   ```

2. **Use LimitRange for defaults**
   ```yaml
   apiVersion: v1
   kind: LimitRange
   metadata:
     name: mem-limit-range
     namespace: production
   spec:
     limits:
     - default:  # Default limits if not specified
         memory: 512Mi
       defaultRequest:  # Default requests
         memory: 256Mi
       type: Container
   ```

3. **Establish monitoring and alerting**
   ```yaml
   # Prometheus alert for high memory usage
   - alert: PodMemoryUsageHigh
     expr: |
       container_memory_usage_bytes{pod!=""} /
       container_spec_memory_limit_bytes{pod!=""} > 0.9
     for: 5m
     labels:
       severity: warning
     annotations:
       summary: "Pod {{ $labels.pod }} memory usage > 90%"

   # Alert for OOMKilled
   - alert: PodOOMKilled
     expr: |
       increase(kube_pod_container_status_terminated_reason{reason="OOMKilled"}[5m]) > 0
     labels:
       severity: critical
     annotations:
       summary: "Pod {{ $labels.pod }} was OOMKilled"
   ```

4. **Implement continuous profiling**
   - Use tools like Pyroscope, Phlare, or Google Cloud Profiler
   - Continuously monitor memory allocation patterns
   - Detect memory leaks early
   - Optimize before reaching production

5. **Regular capacity planning reviews**
   - Review resource usage monthly
   - Adjust limits based on trends
   - Plan for growth
   - Right-size deployments

### Best Practices

- ✅ Always set both requests and limits
- ✅ Requests should be realistic baseline (P50-P75 usage)
- ✅ Limits should accommodate peaks (P95-P99 usage)
- ✅ Leave 20-30% headroom in limits
- ✅ Use VPA for sizing recommendations
- ✅ Monitor memory trends over time
- ✅ Profile applications for memory leaks
- ✅ Implement HPA for traffic-driven workloads
- ✅ Load test with realistic scenarios
- ✅ Set up alerting for high memory usage
- ❌ Don't omit resource limits
- ❌ Don't set limits too close to actual usage
- ❌ Don't ignore memory growth trends
- ❌ Don't deploy without load testing
- ❌ Don't use same limits for all environments

---

## Related Issues

**Related runbooks:**
- [Kubernetes - Pod CrashLoopBackOff](k8s-pod-crashloopbackoff.md) - General crash/restart issues
- [Kubernetes - Node Not Ready](k8s-node-not-ready.md) - Node-level memory pressure

**External resources:**
- [Managing Resources for Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Configure Default Memory Requests and Limits](https://kubernetes.io/docs/tasks/administer-cluster/manage-resources/memory-default-namespace/)
- [Vertical Pod Autoscaler](https://github.com/kubernetes/autoscaler/tree/master/vertical-pod-autoscaler)
- [Horizontal Pod Autoscaler](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/)

---

## Version History

| Version | Date       | Author           | Changes                  |
|---------|------------|------------------|--------------------------|
| 1.0.0   | 2025-01-15 | FaultMaven Team  | Initial verified version |

---

## License & Attribution

This runbook is part of the FaultMaven Knowledge Base.

Licensed under Apache-2.0 License. Contributions welcome via Pull Request.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for contribution guidelines.
