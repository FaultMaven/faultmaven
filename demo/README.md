# FaultMaven Demo Mode

This directory contains sample data for demonstrating FaultMaven's capabilities without connecting to production infrastructure.

## What's Included

### Sample Runbooks (`runbooks/`)

- **payment-service-troubleshooting.md** - A complete troubleshooting guide for a fictional payment service, including common issues like OOMKilled, database connection exhaustion, and API timeouts.

### Sample Evidence (`evidence/`)

A pre-configured incident scenario: **Payment Service OOMKilled**

- **pod-logs.txt** - Application logs showing memory pressure and OOM errors
- **deployment.yaml** - Kubernetes deployment with problematic memory limits (256Mi, recently reduced from 512Mi)
- **kubectl-describe-pod.txt** - Pod description showing OOMKilled status and restart count

## Running Demo Mode

### Option 1: Docker Compose Profile

```bash
# Start FaultMaven with demo data pre-loaded
docker compose --profile demo up
```

### Option 2: Manual Seeding

```bash
# Start FaultMaven normally
docker compose up -d

# Seed demo data
python -m demo.seed_demo_data
```

## The Demo Scenario

**Incident:** Payment service pods in production are repeatedly crashing with `OOMKilled` status.

**Evidence provided:**
1. Pod logs showing memory warnings escalating to OOM errors
2. Kubernetes deployment YAML showing memory limit was recently reduced from 512Mi to 256Mi
3. `kubectl describe pod` output showing 3 restarts due to OOMKilled

**Expected Resolution:**

When you ask FaultMaven "Why is the payment service crashing?", it will:

1. Analyze the pod logs and identify the OOM pattern
2. Cross-reference with the runbook (which recommends 512Mi for production)
3. Find the deployment YAML comment showing the memory was recently reduced
4. Recommend increasing memory limit back to 512Mi

## Try It Out

After seeding the demo data:

1. Open the Dashboard at http://localhost:3333
2. Go to **Case History** to see the demo case
3. Open the case and ask: "Why is the payment service crashing?"
4. Watch FaultMaven correlate the evidence with your runbooks

## Cleaning Up

Demo data is stored in the same database as regular data. To reset:

```bash
# Stop and remove volumes
docker compose down -v

# Restart fresh
docker compose up -d
```
