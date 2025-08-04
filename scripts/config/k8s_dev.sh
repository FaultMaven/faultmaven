#!/bin/bash
# K8s Development Environment Configuration - Stable Credentials
# Source this file to configure FaultMaven for K8s cluster connectivity

# Redis K8s Service Configuration - Stable across pod restarts
export REDIS_HOST="redis.faultmaven.local"
export REDIS_PORT="30379"
export REDIS_PASSWORD="faultmaven-dev-redis-2025"  # Never changes

# ChromaDB K8s Service Configuration (when available)
# export CHROMADB_URL="http://chromadb.faultmaven.local:30180"

# Opik Observability (already configured)
export OPIK_USE_LOCAL="true"
export OPIK_LOCAL_URL="http://opik.faultmaven.local:30080"

echo "✅ K8s development environment configured with stable credentials"
echo "   Redis: ${REDIS_HOST}:${REDIS_PORT} (stable password)"
echo "   Opik: ${OPIK_LOCAL_URL}"
echo ""
echo "Usage: source scripts/config/k8s_dev.sh && ./run_faultmaven_dev.sh"