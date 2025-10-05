---
# Runbook Metadata (YAML frontmatter for automated parsing)
id: template-runbook-id
title: "[Technology] - [Specific Problem]"
technology: kubernetes  # kubernetes|redis|postgresql|docker|networking|etc
severity: medium  # critical|high|medium|low
tags:
  - template
  - example
  - technology-name
difficulty: intermediate  # beginner|intermediate|advanced
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified  # verified|draft|deprecated
---

# [Technology] - [Specific Problem]

> **Purpose**: One-sentence description of what problem this runbook solves

## Quick Reference Card

**🔍 Symptoms:**
- Observable symptom 1 (what the user sees/experiences)
- Observable symptom 2 (error messages, behaviors)
- Observable symptom 3 (performance indicators)

**⚡ Common Causes:**
1. Most likely root cause (70% of cases) - Brief explanation
2. Second most likely cause (20% of cases) - Brief explanation
3. Less common but possible cause (10% of cases) - Brief explanation

**🚀 Quick Fix:**
```bash
# Emergency one-liner for immediate mitigation (if applicable)
kubectl scale deployment/app --replicas=0 && kubectl scale deployment/app --replicas=3
```

**⏱️ Estimated Resolution Time:** 15 minutes

---

## Diagnostic Steps

### Step 1: Verify the Problem Exists

**What to check:** Confirm the symptom is actually present and not a false alarm

**How to check:**
```bash
# Diagnostic command with clear explanation
kubectl get pods -n production | grep -i crashloopbackoff
```

**Expected output if problem exists:**
```
app-7d9f8c6b5-4xkz2   0/1     CrashLoopBackOff   5          3m
app-7d9f8c6b5-9pxl1   0/1     CrashLoopBackOff   3          2m
```

**What this tells you:**
Multiple pods are failing to start successfully, indicating a systemic issue with the deployment configuration or application code.

---

### Step 2: Identify the Root Cause

**What to check:** Examine pod events and logs to determine why it's failing

**How to check:**
```bash
# Get detailed pod information
kubectl describe pod app-7d9f8c6b5-4xkz2 -n production

# Check application logs
kubectl logs app-7d9f8c6b5-4xkz2 -n production --tail=50

# If pod already restarted, check previous logs
kubectl logs app-7d9f8c6b5-4xkz2 -n production --previous
```

**Look for:**
- `Exit Code: 137` → OOMKilled (memory exhaustion)
- `Exit Code: 1` → Application error
- `Error: ImagePullBackOff` → Registry/image issue
- `Readiness probe failed` → Application not healthy
- `CrashLoopBackOff` → Continuous failure after restarts

**Common error patterns:**
```
Error: failed to start container: Error response from daemon: OCI runtime create failed
→ Runtime configuration issue

panic: runtime error: invalid memory address
→ Application bug or missing dependency

Error: ECONNREFUSED connecting to database
→ Dependency service unavailable
```

---

### Step 3: Check Configuration and Resources

**What to check:** Verify deployment configuration and resource allocation

**How to check:**
```bash
# Check deployment configuration
kubectl get deployment app -n production -o yaml

# Check resource requests and limits
kubectl describe deployment app -n production | grep -A 5 "Limits:"

# Check environment variables and secrets
kubectl get deployment app -n production -o jsonpath='{.spec.template.spec.containers[0].env}'
```

**Look for:**
- Missing or incorrect environment variables
- Insufficient resource requests/limits
- Incorrect image tag or registry
- Missing volume mounts or secrets
- Incorrect command or entrypoint

---

## Solutions

### Solution 1: Fix Application Configuration (Most Common)

**When to use this solution:**
- Logs show application errors related to config
- Environment variables are missing or incorrect
- Application exits with code 1

**Prerequisites:**
- Access to kubectl with deployment edit permissions
- Knowledge of correct configuration values
- Understanding of application requirements

**Implementation Steps:**

1. **Backup current deployment**
   ```bash
   kubectl get deployment app -n production -o yaml > app-deployment-backup.yaml
   ```

2. **Identify the configuration issue**
   ```bash
   # Review current environment variables
   kubectl get deployment app -n production -o jsonpath='{.spec.template.spec.containers[0].env}' | jq

   # Check configmap if used
   kubectl get configmap app-config -n production -o yaml
   ```

3. **Apply the fix**
   ```bash
   # Method 1: Edit deployment directly
   kubectl edit deployment app -n production
   # Update the environment variables or configuration

   # Method 2: Apply corrected configmap
   kubectl apply -f corrected-config.yaml

   # Method 3: Set environment variable via CLI
   kubectl set env deployment/app DATABASE_URL="postgres://..." -n production
   ```

4. **Trigger redeployment**
   ```bash
   kubectl rollout restart deployment/app -n production
   ```

5. **Verify the fix**
   ```bash
   # Watch rollout status
   kubectl rollout status deployment/app -n production

   # Check pod status
   kubectl get pods -n production -l app=app

   # Verify application health
   kubectl logs -f deployment/app -n production
   ```

**Expected outcome:** Pods successfully start and remain in `Running` state

**Time to resolution:** ~5 minutes

---

### Solution 2: Increase Resource Limits

**When to use this solution:**
- Exit code is 137 (OOMKilled)
- Logs show memory-related errors
- Application requires more resources than allocated

**Implementation Steps:**

1. **Check current resource usage**
   ```bash
   kubectl top pods -n production
   ```

2. **Update resource limits**
   ```bash
   kubectl set resources deployment app -n production \
     --requests=cpu=500m,memory=512Mi \
     --limits=cpu=1000m,memory=1Gi
   ```

3. **Verify the fix**
   ```bash
   kubectl rollout status deployment/app -n production
   kubectl get pods -n production
   ```

**Expected outcome:** Pods run without being killed by OOM

**Time to resolution:** ~3 minutes

---

### Solution 3: Fix Dependency Issues

**When to use this solution:**
- Application cannot connect to dependencies (database, cache, etc.)
- Connection refused or timeout errors in logs
- Application works in other environments

**Implementation Steps:**

1. **Verify dependency availability**
   ```bash
   # Check if database pod is running
   kubectl get pods -n production -l app=database

   # Test connectivity from a debug pod
   kubectl run -it --rm debug --image=nicolaka/netshoot --restart=Never -- sh
   # Inside debug pod:
   nc -zv database-service 5432
   ```

2. **Fix service discovery**
   ```bash
   # Check service exists
   kubectl get svc database-service -n production

   # Verify DNS resolution
   kubectl run -it --rm debug --image=nicolaka/netshoot --restart=Never -- nslookup database-service
   ```

3. **Update connection strings if needed**
   ```bash
   kubectl set env deployment/app DATABASE_HOST="database-service.production.svc.cluster.local" -n production
   ```

**Expected outcome:** Application successfully connects to dependencies

---

## Root Cause Analysis

**Why this happens:**

The CrashLoopBackOff state occurs when:
1. Application fails to start successfully
2. Kubernetes attempts to restart it (exponential backoff)
3. Application continues to fail after multiple restart attempts
4. Kubernetes increases wait time between restarts to avoid resource thrashing

**Common scenarios that trigger this:**

1. **Configuration errors** (60% of cases)
   - Missing environment variables
   - Incorrect database connection strings
   - Invalid application config files

2. **Resource exhaustion** (25% of cases)
   - Insufficient memory allocation
   - CPU throttling preventing startup
   - Disk space issues

3. **Application bugs** (10% of cases)
   - Code crashes during initialization
   - Unhandled exceptions in startup code
   - Missing dependencies or libraries

4. **Infrastructure issues** (5% of cases)
   - Dependency services unavailable
   - Network connectivity problems
   - Image pull failures

---

## Prevention

### Immediate Prevention (Tactical)

1. **Monitor pod status proactively:**
   ```bash
   # Set up watch on pod status
   kubectl get pods -n production -w

   # Use monitoring alerts for CrashLoopBackOff
   ```

2. **Validate configuration before deployment:**
   ```bash
   # Dry-run deployment
   kubectl apply -f deployment.yaml --dry-run=server

   # Use kubeval or similar tools
   kubeval deployment.yaml
   ```

3. **Test in staging first:**
   ```bash
   # Deploy to staging environment
   kubectl apply -f deployment.yaml -n staging

   # Verify stability before production
   kubectl rollout status deployment/app -n staging
   ```

### Long-Term Prevention (Strategic)

1. **Implement health checks:**
   ```yaml
   livenessProbe:
     httpGet:
       path: /health
       port: 8080
     initialDelaySeconds: 30
     periodSeconds: 10
   readinessProbe:
     httpGet:
       path: /ready
       port: 8080
     initialDelaySeconds: 5
     periodSeconds: 5
   ```

2. **Use configuration management:**
   - Store configs in ConfigMaps/Secrets
   - Version control all Kubernetes manifests
   - Use Helm charts or Kustomize for templating

3. **Implement proper resource management:**
   - Set realistic requests and limits based on profiling
   - Use Vertical Pod Autoscaler for optimization
   - Monitor actual resource usage over time

4. **Set up comprehensive monitoring:**
   - Pod restart counts
   - Application error rates
   - Resource utilization metrics
   - Dependency health checks

### Best Practices

- ✅ Always set resource requests and limits
- ✅ Implement proper health checks (liveness and readiness)
- ✅ Use structured logging for easier debugging
- ✅ Test configurations in non-production first
- ✅ Version control all manifests
- ✅ Monitor pod restart counts
- ❌ Don't deploy without testing configuration
- ❌ Don't ignore warning signs from monitoring
- ❌ Don't hardcode configuration in containers
- ❌ Don't skip staging deployments

---

## Related Issues

**Related runbooks:**
- [Kubernetes - Pod OOMKilled](k8s-pod-oomkilled.md) - Memory exhaustion
- [Kubernetes - ImagePullBackOff](k8s-pod-imagepullbackoff.md) - Registry issues
- [Kubernetes - Node Not Ready](k8s-node-not-ready.md) - Infrastructure problems

**External resources:**
- [Kubernetes Debugging Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)
- [Application Introspection and Debugging](https://kubernetes.io/docs/tasks/debug/debug-application/)
- [Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)

---

## Validation & Quality Checklist

**For Reviewers (Knowledge Curators):**
- [ ] Technical accuracy verified by testing
- [ ] All commands tested in real environment
- [ ] Expected outputs match actual results
- [ ] Security best practices followed
- [ ] No dangerous commands without warnings
- [ ] All external links are valid and authoritative
- [ ] YAML frontmatter metadata is complete
- [ ] Follows TEMPLATE.md structure exactly
- [ ] Clear and actionable language used
- [ ] Appropriate for target difficulty level
- [ ] Prevention strategies are practical
- [ ] Related runbooks properly linked

**For Contributors:**
- [ ] I have tested all commands in a real environment
- [ ] I have verified this works in production-like conditions
- [ ] I have included all necessary prerequisites
- [ ] I have considered security implications
- [ ] I have linked related runbooks
- [ ] All expected outputs are accurate
- [ ] I have included prevention strategies
- [ ] YAML metadata is complete and accurate

---

## Version History

| Version | Date       | Author           | Changes                  |
|---------|------------|------------------|--------------------------|
| 1.0.0   | 2025-01-15 | FaultMaven Team  | Initial template version |

---

## License & Attribution

This runbook is part of the FaultMaven Knowledge Base.

Licensed under Apache-2.0 License. Contributions welcome via Pull Request.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for contribution guidelines.
