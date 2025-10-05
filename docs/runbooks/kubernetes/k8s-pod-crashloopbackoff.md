---
id: k8s-pod-crashloopbackoff
title: "Kubernetes - Pod CrashLoopBackOff"
technology: kubernetes
severity: high
tags:
  - kubernetes
  - pod
  - crashloop
  - restart
  - deployment
difficulty: intermediate
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified
---

# Kubernetes - Pod CrashLoopBackOff

> **Purpose**: Diagnose and resolve pods that are continuously crashing and restarting with exponential backoff delays

## Quick Reference Card

**🔍 Symptoms:**
- Pod status shows `CrashLoopBackOff`
- Pod restarts count continuously increasing
- Application unavailable or intermittently accessible
- Events show repeated container exits

**⚡ Common Causes:**
1. **Application configuration errors** (60% of cases) - Missing environment variables, incorrect config files, bad connection strings
2. **Resource constraints** (25% of cases) - Insufficient memory/CPU, OOM kills, resource limits too low
3. **Application bugs** (10% of cases) - Crashes during startup, unhandled exceptions, missing dependencies
4. **Infrastructure issues** (5% of cases) - Unavailable dependencies, image pull failures, volume mount problems

**🚀 Quick Fix:**
```bash
# Check logs immediately for error messages
kubectl logs <pod-name> -n <namespace> --previous

# If config issue, rollback to last known good deployment
kubectl rollout undo deployment/<deployment-name> -n <namespace>
```

**⏱️ Estimated Resolution Time:** 15-30 minutes

---

## Diagnostic Steps

### Step 1: Verify CrashLoopBackOff State

**What to check:** Confirm pods are in CrashLoopBackOff and identify affected resources

**How to check:**
```bash
# List all pods with status
kubectl get pods -n <namespace>

# Check across all namespaces
kubectl get pods -A | grep CrashLoop

# Get detailed pod information
kubectl get pods -n <namespace> -o wide
```

**Expected output if problem exists:**
```
NAME                      READY   STATUS             RESTARTS   AGE
myapp-7d9f8c6b5-4xkz2    0/1     CrashLoopBackOff   5          8m
myapp-7d9f8c6b5-9pxl1    0/1     CrashLoopBackOff   3          6m
```

**What this tells you:**
- Multiple pods affected indicates deployment-level issue
- High restart count (>5) suggests persistent problem
- Recent AGE with high restarts means rapid failure

---

### Step 2: Examine Pod Events and Logs

**What to check:** Determine why the container is exiting

**How to check:**
```bash
# Get detailed pod description with events
kubectl describe pod <pod-name> -n <namespace>

# Check current container logs
kubectl logs <pod-name> -n <namespace>

# Check previous container logs (crucial for crashed containers)
kubectl logs <pod-name> -n <namespace> --previous

# Follow logs in real-time during restart
kubectl logs -f <pod-name> -n <namespace>

# If multiple containers in pod, specify container
kubectl logs <pod-name> -c <container-name> -n <namespace> --previous
```

**Look for in events:**
```
Events:
  Type     Reason     Age                   From               Message
  ----     ------     ----                  ----               -------
  Normal   Scheduled  5m                    default-scheduler  Successfully assigned default/myapp-xxx to node1
  Normal   Pulling    5m                    kubelet            Pulling image "myapp:v1.2.3"
  Normal   Pulled     5m                    kubelet            Successfully pulled image
  Normal   Created    2m (x5 over 5m)       kubelet            Created container myapp
  Normal   Started    2m (x5 over 5m)       kubelet            Started container myapp
  Warning  BackOff    1m (x10 over 4m)      kubelet            Back-off restarting failed container
```

**Common error patterns in logs:**
```bash
# Configuration errors
Error: Missing required environment variable DATABASE_URL
Error: failed to load config file: /etc/app/config.yaml: no such file or directory
panic: runtime error: invalid memory address or nil pointer dereference

# Dependency errors
Error: dial tcp 10.0.1.5:5432: connect: connection refused
Error: Failed to connect to Redis: ECONNREFUSED
Error: FATAL: database "mydb" does not exist

# Resource errors
fatal error: runtime: out of memory
OOMKilled
signal: killed

# Application errors
panic: interface conversion: interface {} is nil, not string
Error: listen tcp :8080: bind: address already in use
Exception in thread "main" java.lang.NullPointerException
```

---

### Step 3: Check Deployment Configuration

**What to check:** Review pod spec for configuration issues

**How to check:**
```bash
# Get deployment YAML
kubectl get deployment <deployment-name> -n <namespace> -o yaml

# Check environment variables
kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].env}'

# Check resource requests and limits
kubectl describe deployment <deployment-name> -n <namespace> | grep -A 10 "Limits:"

# Check configmaps
kubectl get configmap -n <namespace>
kubectl get configmap <configmap-name> -n <namespace> -o yaml

# Check secrets
kubectl get secrets -n <namespace>
```

**Look for:**
- Missing or incorrect environment variables
- Insufficient resource limits (memory/CPU)
- Missing volume mounts
- Incorrect image tag or registry
- Missing ConfigMaps or Secrets
- Wrong command or args

---

### Step 4: Check Exit Code

**What to check:** Understand how the container is failing

**How to check:**
```bash
# Get container status with exit code
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[0].lastState.terminated}'

# Alternative: from describe output
kubectl describe pod <pod-name> -n <namespace> | grep -A 5 "Last State"
```

**Common exit codes:**
```
Exit Code 0   - Clean exit (unusual for crash)
Exit Code 1   - Application error (most common)
Exit Code 2   - Misuse of shell command
Exit Code 126 - Command cannot execute (permissions)
Exit Code 127 - Command not found
Exit Code 137 - SIGKILL (OOMKilled - out of memory)
Exit Code 139 - SIGSEGV (segmentation fault)
Exit Code 143 - SIGTERM (graceful termination)
```

**What this tells you:**
- Exit code 137 → Memory issue, check resource limits
- Exit code 1 → Application error, check logs
- Exit code 127 → Wrong entrypoint/command

---

## Solutions

### Solution 1: Fix Application Configuration (Most Common)

**When to use this solution:**
- Logs show missing environment variables
- Configuration file errors
- Database connection string issues
- Exit code is 1

**Prerequisites:**
- kubectl access with deployment edit permissions
- Knowledge of correct configuration values
- Access to ConfigMaps/Secrets if needed

**Implementation Steps:**

1. **Backup current deployment**
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> -o yaml > deployment-backup-$(date +%Y%m%d-%H%M%S).yaml
   ```

2. **Identify missing or incorrect configuration**
   ```bash
   # Check current environment variables
   kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].env}' | jq

   # Check if ConfigMap exists and has correct data
   kubectl get configmap <configmap-name> -n <namespace> -o yaml
   ```

3. **Update configuration using one of these methods:**

   **Method A: Update environment variable directly**
   ```bash
   kubectl set env deployment/<deployment-name> -n <namespace> \
     DATABASE_URL="postgresql://user:pass@db-host:5432/dbname"
   ```

   **Method B: Update ConfigMap**
   ```bash
   # Edit ConfigMap
   kubectl edit configmap <configmap-name> -n <namespace>

   # Or apply updated ConfigMap
   kubectl apply -f updated-configmap.yaml
   ```

   **Method C: Edit deployment directly**
   ```bash
   kubectl edit deployment <deployment-name> -n <namespace>
   # Edit the env section, save and exit
   ```

4. **Trigger rollout (if ConfigMap updated)**
   ```bash
   # Deployment won't auto-restart for ConfigMap changes
   kubectl rollout restart deployment/<deployment-name> -n <namespace>
   ```

5. **Monitor the rollout**
   ```bash
   # Watch rollout progress
   kubectl rollout status deployment/<deployment-name> -n <namespace>

   # Watch pod status
   kubectl get pods -n <namespace> -w

   # Check logs of new pods
   kubectl logs -f deployment/<deployment-name> -n <namespace>
   ```

6. **Verify application health**
   ```bash
   # Check pod status - should show Running
   kubectl get pods -n <namespace> -l app=<app-label>

   # Test application endpoint
   kubectl port-forward deployment/<deployment-name> 8080:8080 -n <namespace>
   curl http://localhost:8080/health
   ```

**Expected outcome:** Pods reach `Running` state with 0 restarts

**Time to resolution:** ~5-10 minutes

---

### Solution 2: Increase Resource Limits

**When to use this solution:**
- Exit code is 137 (OOMKilled)
- Logs show "out of memory" errors
- Container killed repeatedly
- `kubectl describe` shows OOMKilled in events

**Implementation Steps:**

1. **Check current resource usage and limits**
   ```bash
   # Current resource limits
   kubectl describe deployment <deployment-name> -n <namespace> | grep -A 5 "Limits:"

   # Actual resource usage (if pod runs briefly)
   kubectl top pod <pod-name> -n <namespace>
   ```

2. **Determine appropriate limits**
   ```bash
   # Review historical metrics if available
   kubectl top pod <pod-name> -n <namespace> --use-protocol-buffers

   # Rule of thumb: Double current limits if OOMKilled
   # Current: 256Mi → New: 512Mi
   # Current: 512Mi → New: 1Gi
   ```

3. **Update resource requests and limits**
   ```bash
   kubectl set resources deployment <deployment-name> -n <namespace> \
     --requests=cpu=500m,memory=512Mi \
     --limits=cpu=1000m,memory=1Gi
   ```

   **Or edit manually:**
   ```bash
   kubectl edit deployment <deployment-name> -n <namespace>
   ```

   Update the resources section:
   ```yaml
   resources:
     requests:
       memory: "512Mi"
       cpu: "500m"
     limits:
       memory: "1Gi"
       cpu: "1000m"
   ```

4. **Monitor new pods**
   ```bash
   kubectl rollout status deployment/<deployment-name> -n <namespace>
   kubectl get pods -n <namespace> -w
   kubectl top pod -n <namespace> -l app=<app-label>
   ```

**Expected outcome:** Pods run without OOMKilled events

**Time to resolution:** ~3-5 minutes

---

### Solution 3: Fix Dependency Connectivity

**When to use this solution:**
- Logs show "connection refused" errors
- Cannot connect to database, cache, or other services
- Works in other environments
- Exit code is 1 with network errors

**Implementation Steps:**

1. **Verify dependency service is running**
   ```bash
   # Check if dependency pods are running
   kubectl get pods -n <namespace> -l app=database

   # Check service exists
   kubectl get svc -n <namespace>

   # Get service details
   kubectl describe svc <service-name> -n <namespace>
   ```

2. **Test connectivity from a debug pod**
   ```bash
   # Create debug pod in same namespace
   kubectl run -it --rm debug --image=nicolaka/netshoot --restart=Never -n <namespace> -- bash

   # Inside debug pod, test connectivity:
   # For databases
   nc -zv <service-name> 5432  # PostgreSQL
   nc -zv <service-name> 3306  # MySQL
   nc -zv <service-name> 6379  # Redis

   # For HTTP services
   curl http://<service-name>:<port>/health

   # Check DNS resolution
   nslookup <service-name>
   nslookup <service-name>.<namespace>.svc.cluster.local
   ```

3. **Fix connection string if needed**
   ```bash
   # Update to use Kubernetes service DNS
   kubectl set env deployment/<deployment-name> -n <namespace> \
     DATABASE_HOST="postgres-service.production.svc.cluster.local"

   # Or update full connection string
   kubectl set env deployment/<deployment-name> -n <namespace> \
     DATABASE_URL="postgresql://user:pass@postgres-service:5432/mydb"
   ```

4. **Check network policies**
   ```bash
   # List network policies
   kubectl get networkpolicy -n <namespace>

   # Check if network policy is blocking traffic
   kubectl describe networkpolicy <policy-name> -n <namespace>
   ```

5. **Verify dependency is ready**
   ```bash
   # Check dependency pod logs
   kubectl logs -f <dependency-pod-name> -n <namespace>

   # Check if dependency service has endpoints
   kubectl get endpoints <service-name> -n <namespace>
   ```

**Expected outcome:** Application successfully connects to all dependencies

**Time to resolution:** ~10-15 minutes

---

### Solution 4: Rollback to Previous Version

**When to use this solution:**
- Issue started after recent deployment
- Quick mitigation needed
- Root cause investigation can happen after rollback
- Previous version was stable

**Implementation Steps:**

1. **Check deployment history**
   ```bash
   kubectl rollout history deployment/<deployment-name> -n <namespace>
   ```

   **Output:**
   ```
   REVISION  CHANGE-CAUSE
   1         kubectl apply --filename=deployment.yaml --record=true
   2         kubectl set image deployment/myapp myapp=myapp:v1.2.3
   3         kubectl set image deployment/myapp myapp=myapp:v1.2.4
   ```

2. **Rollback to previous revision**
   ```bash
   # Rollback to immediately previous version
   kubectl rollout undo deployment/<deployment-name> -n <namespace>

   # Rollback to specific revision
   kubectl rollout undo deployment/<deployment-name> -n <namespace> --to-revision=2
   ```

3. **Monitor rollback**
   ```bash
   kubectl rollout status deployment/<deployment-name> -n <namespace>
   kubectl get pods -n <namespace> -w
   ```

4. **Verify application is working**
   ```bash
   kubectl get pods -n <namespace>
   kubectl logs deployment/<deployment-name> -n <namespace>
   ```

**Expected outcome:** Application returns to working state with previous version

**Time to resolution:** ~2-3 minutes

**Note:** This is a temporary fix. Investigate and fix the root cause before redeploying the new version.

---

## Root Cause Analysis

**Why CrashLoopBackOff happens:**

CrashLoopBackOff is Kubernetes' protective mechanism when:
1. Container exits with non-zero status (failure)
2. Kubernetes restarts the container automatically
3. Container continues to fail after multiple restart attempts
4. Kubernetes implements exponential backoff delays (10s, 20s, 40s, 80s, up to 5 minutes)

**The backoff prevents:**
- Resource thrashing
- API server overload
- Node instability
- Rapid failure loops

**Common trigger scenarios:**

1. **Configuration Deployment** (Most Common)
   - New deployment with missing environment variable
   - Updated ConfigMap with syntax error
   - Changed database credentials without updating Secret
   - Incorrect image tag pointing to broken version

2. **Resource Exhaustion**
   - Application memory requirements grew
   - Insufficient memory limits set initially
   - Memory leak in application code
   - Traffic spike causing memory pressure

3. **Dependency Changes**
   - Database upgraded with incompatible schema
   - Cache server restarted with different config
   - External API changed authentication method
   - Service discovery issues after cluster changes

4. **Code Defects**
   - Null pointer exceptions during startup
   - Uncaught exceptions in initialization code
   - Missing error handling for critical paths
   - Race conditions in multi-threaded startup

---

## Prevention

### Immediate Prevention (Tactical)

1. **Monitor pod restarts proactively**
   ```bash
   # Watch for restart increases
   kubectl get pods -n <namespace> -w

   # Check restart counts regularly
   kubectl get pods -A --field-selector=status.phase=Running --sort-by=.status.containerStatuses[0].restartCount
   ```

2. **Set up alerts for CrashLoopBackOff**
   ```yaml
   # Prometheus alert rule
   - alert: PodCrashLooping
     expr: rate(kube_pod_container_status_restarts_total[15m]) > 0
     for: 5m
     labels:
       severity: warning
     annotations:
       summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} is crash looping"
   ```

3. **Validate configuration before deploying**
   ```bash
   # Dry-run deployment to catch errors
   kubectl apply -f deployment.yaml --dry-run=server

   # Use kubeval for YAML validation
   kubeval deployment.yaml

   # Test in staging first
   kubectl apply -f deployment.yaml -n staging
   kubectl rollout status deployment/<name> -n staging
   # Wait 10 minutes, monitor, then deploy to production
   ```

4. **Always use readiness probes**
   ```yaml
   readinessProbe:
     httpGet:
       path: /ready
       port: 8080
     initialDelaySeconds: 5
     periodSeconds: 5
     failureThreshold: 3
   ```

### Long-Term Prevention (Strategic)

1. **Implement comprehensive health checks**
   ```yaml
   spec:
     containers:
     - name: app
       livenessProbe:
         httpGet:
           path: /health
           port: 8080
         initialDelaySeconds: 30
         periodSeconds: 10
         failureThreshold: 3
       readinessProbe:
         httpGet:
           path: /ready
           port: 8080
         initialDelaySeconds: 5
         periodSeconds: 5
         failureThreshold: 3
       startupProbe:
         httpGet:
           path: /startup
           port: 8080
         initialDelaySeconds: 0
         periodSeconds: 10
         failureThreshold: 30  # 5 minutes max startup time
   ```

2. **Use configuration management and GitOps**
   - Store all manifests in Git
   - Use Helm charts or Kustomize for templating
   - Implement CI/CD pipelines with validation
   - Use ArgoCD or Flux for automated deployments

3. **Implement proper resource management**
   ```yaml
   resources:
     requests:
       memory: "512Mi"
       cpu: "500m"
     limits:
       memory: "1Gi"
       cpu: "1000m"
   ```
   - Set requests based on actual usage (use VPA for guidance)
   - Set limits 50-100% higher than requests
   - Monitor and adjust based on metrics

4. **Set up comprehensive observability**
   - Centralized logging (ELK, Loki)
   - Metrics collection (Prometheus)
   - Distributed tracing (Jaeger, Tempo)
   - Alerting on key metrics
   - Dashboard for pod health

5. **Implement progressive delivery**
   - Use Canary deployments (10% → 50% → 100%)
   - Implement blue-green deployments
   - Automated rollback on failure detection
   - Use tools like Flagger or Argo Rollouts

### Best Practices

- ✅ Always set resource requests and limits
- ✅ Implement all three probe types (liveness, readiness, startup)
- ✅ Use structured logging with log levels
- ✅ Test deployments in staging first
- ✅ Version control all Kubernetes manifests
- ✅ Monitor restart counts and set alerts
- ✅ Use ConfigMaps/Secrets for configuration
- ✅ Implement graceful shutdown handling
- ✅ Have runbooks for common failure scenarios
- ✅ Use init containers for dependency checks
- ❌ Don't deploy directly to production without testing
- ❌ Don't ignore increasing restart counts
- ❌ Don't hardcode configuration in container images
- ❌ Don't skip health check implementation
- ❌ Don't deploy without understanding the changes

---

## Related Issues

**Related runbooks:**
- [Kubernetes - Pod OOMKilled](k8s-pod-oomkilled.md) - Memory exhaustion (exit code 137)
- [Kubernetes - Pod ImagePullBackOff](k8s-pod-imagepullbackoff.md) - Cannot pull container image
- [Kubernetes - Node Not Ready](k8s-node-not-ready.md) - Infrastructure node issues

**External resources:**
- [Kubernetes Debugging Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)
- [Application Introspection and Debugging](https://kubernetes.io/docs/tasks/debug/debug-application/)
- [Configure Liveness, Readiness and Startup Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
- [Resource Management for Pods and Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)

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
