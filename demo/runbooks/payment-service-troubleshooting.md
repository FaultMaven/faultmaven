# Payment Service Troubleshooting Guide

## Overview

The Payment Service handles all transaction processing for the e-commerce platform. It runs as a Kubernetes deployment with 3 replicas.

## Architecture

```
                    ┌─────────────────┐
                    │   API Gateway   │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ Payment Service │ ← You are here
                    │   (3 replicas)  │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────▼────────┐ ┌──▼───┐ ┌───────▼───────┐
     │ PostgreSQL DB   │ │Redis │ │ Stripe API    │
     │ (transactions)  │ │Cache │ │ (processor)   │
     └─────────────────┘ └──────┘ └───────────────┘
```

## Common Issues

### 1. OOMKilled (Out of Memory)

**Symptoms:**
- Pods restart frequently
- `OOMKilled` status in `kubectl describe pod`
- Memory usage spikes before crash

**Root Causes:**
- Memory limit too low for transaction volume
- Memory leak in recent deployment
- Large batch processing overwhelming heap

**Resolution:**
1. Check current memory limit: `kubectl get pod <pod> -o yaml | grep -A5 resources`
2. Review recent deployments for memory changes
3. Increase memory limit if < 512Mi for production:
   ```yaml
   resources:
     limits:
       memory: 512Mi
     requests:
       memory: 256Mi
   ```
4. If leak suspected, enable heap profiling and analyze

### 2. Database Connection Exhausted

**Symptoms:**
- `Connection pool exhausted` errors in logs
- Increasing response latency
- Transactions timing out

**Root Causes:**
- Connection pool size too small
- Connections not being released (connection leak)
- Database overloaded

**Resolution:**
1. Check current pool size: `PAYMENT_DB_POOL_SIZE` env var
2. Review connection metrics in Grafana
3. Increase pool size if needed (max 20 per pod)
4. Check for unclosed connections in code

### 3. Stripe API Timeouts

**Symptoms:**
- `TimeoutError: Stripe API` in logs
- Transactions stuck in `pending` state
- 504 Gateway Timeout responses

**Root Causes:**
- Stripe service degradation
- Network issues to Stripe
- Request rate limiting

**Resolution:**
1. Check Stripe Status: https://status.stripe.com
2. Verify network connectivity from pod
3. Review rate limit headers in responses
4. Implement retry with exponential backoff

## Health Checks

### Kubernetes Probes

```yaml
livenessProbe:
  httpGet:
    path: /health/live
    port: 8080
  initialDelaySeconds: 30
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /health/ready
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 5
```

### Manual Health Check

```bash
# From within cluster
kubectl exec -it <pod> -- curl localhost:8080/health

# Expected response
{"status": "healthy", "db": "connected", "redis": "connected", "stripe": "reachable"}
```

## Metrics to Monitor

| Metric | Alert Threshold | Description |
|--------|-----------------|-------------|
| `payment_transaction_duration_seconds` | > 5s | Transaction processing time |
| `payment_error_rate` | > 1% | Failed transaction percentage |
| `payment_db_connections_active` | > 80% of pool | Database connection usage |
| `container_memory_usage_bytes` | > 80% of limit | Memory consumption |

## Escalation

1. **L1 (On-call):** Restart pod, check logs, basic troubleshooting
2. **L2 (Platform Team):** Infrastructure issues, scaling, config changes
3. **L3 (Payment Team):** Code issues, Stripe integration, business logic

## Related Documentation

- [Kubernetes Deployment Guide](./kubernetes-deployment.md)
- [Database Schema](./payment-db-schema.md)
- [Stripe Integration](./stripe-integration.md)
