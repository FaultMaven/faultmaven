# FaultMaven Deployment Guide

This guide provides comprehensive instructions for deploying FaultMaven in various environments, from local development to production Kubernetes clusters.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Environment Configuration](#environment-configuration)
- [Local Development Deployment](#local-development-deployment)
- [Docker Deployment](#docker-deployment)
- [Kubernetes Deployment](#kubernetes-deployment)
- [Production Considerations](#production-considerations)
- [Monitoring and Observability](#monitoring-and-observability)
- [Troubleshooting](#troubleshooting)

## Prerequisites

### System Requirements

**Minimum Requirements:**
- CPU: 2 cores
- Memory: 4GB RAM
- Storage: 10GB available space
- Network: Internet connectivity for LLM providers

**Recommended for Production:**
- CPU: 4+ cores
- Memory: 8GB+ RAM
- Storage: 50GB+ available space
- Network: High-bandwidth connection with low latency

### Software Dependencies

**Required:**
- Python 3.10+
- Docker (for containerized deployment)
- Kubernetes 1.21+ (for K8s deployment)

**External Services (Required):**
- Redis 6.0+ (session storage and caching)
- ChromaDB 0.4+ (vector database for knowledge base)
- Microsoft Presidio (PII redaction service)

**External Services (Optional):**
- Opik (LLM observability and tracing)
- Prometheus (metrics collection)
- Grafana (monitoring dashboards)

**LLM Providers (At least one required):**
- OpenAI API (GPT models)
- Anthropic API (Claude models)
- Fireworks AI (open source models)

## Environment Configuration

### Environment Variables

Create a `.env` file in the project root with the following configuration:

```bash
# === Core Application Settings ===
ENVIRONMENT=production
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=INFO

# === LLM Provider Configuration ===
# Choose your primary LLM provider
CHAT_PROVIDER=openai  # Options: openai, anthropic, fireworks

# OpenAI Configuration
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_MODEL=gpt-4o

# Anthropic Configuration (if using Claude)
ANTHROPIC_API_KEY=your_anthropic_api_key_here
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022

# Fireworks AI Configuration (if using open models)
FIREWORKS_API_KEY=your_fireworks_api_key_here
FIREWORKS_MODEL=accounts/fireworks/models/llama-v3p1-70b-instruct

# === Database Configuration ===
# Redis (Session Storage)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=your_redis_password
REDIS_DB=0

# ChromaDB (Vector Database)
CHROMADB_URL=http://localhost:8000
CHROMADB_API_KEY=your_chromadb_api_key
CHROMADB_COLLECTION=faultmaven_knowledge

# === Security Services ===
# Presidio (PII Redaction)
PRESIDIO_ANALYZER_URL=http://localhost:5001
PRESIDIO_ANONYMIZER_URL=http://localhost:5002

# === Session Management ===
SESSION_TIMEOUT_MINUTES=30
SESSION_CLEANUP_INTERVAL_MINUTES=15
SESSION_MAX_MEMORY_MB=100
SESSION_CLEANUP_BATCH_SIZE=50

# === Observability (Optional) ===
# Opik Configuration
OPIK_API_KEY=your_opik_api_key
OPIK_PROJECT_NAME=faultmaven
OPIK_WORKSPACE=your_workspace

# Local Opik (if self-hosted)
OPIK_USE_LOCAL=true
OPIK_BASE_URL=http://localhost:5173

# === Feature Flags ===
ENABLE_LEGACY_COMPATIBILITY=true
ENABLE_EXPERIMENTAL_FEATURES=false
ENABLE_PERFORMANCE_MONITORING=true
ENABLE_DETAILED_TRACING=false

# === Logging Configuration ===
LOG_FORMAT=json
LOG_DEDUPE=true
LOG_BUFFER_SIZE=100
LOG_FLUSH_INTERVAL=5
```

### Configuration Validation

Before deployment, validate your configuration:

```bash
# Activate virtual environment
source .venv/bin/activate

# Test configuration
python -c "
from faultmaven.config.configuration_manager import get_config
config = get_config()
if config.validate():
    print('✅ Configuration is valid')
else:
    print('❌ Configuration validation failed')
"
```

## Local Development Deployment

### Quick Start

1. **Clone and Setup**
   ```bash
   git clone <repository_url>
   cd FaultMaven
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   pip install -r requirements-test.txt
   ```

2. **Download ML Models**
   ```bash
   python -m spacy download en_core_web_lg
   ```

3. **Configure Environment**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

4. **Start Development Server**
   ```bash
   ./run_faultmaven.sh
   ```

5. **Verify Deployment**
   ```bash
   curl http://localhost:8000/health
   ```

### Development with External Services

For full functionality, start the required external services:

**Redis (using Docker):**
```bash
docker run -d \
  --name faultmaven-redis \
  -p 6379:6379 \
  redis:7-alpine redis-server --requirepass your_redis_password
```

**ChromaDB (using Docker):**
```bash
docker run -d \
  --name faultmaven-chromadb \
  -p 8000:8000 \
  -e CHROMA_SERVER_AUTH_CREDENTIALS_PROVIDER=chromadb.auth.token.TokenAuthCredentialsProvider \
  -e CHROMA_SERVER_AUTH_TOKEN_TRANSPORT_HEADER=X-Chroma-Token \
  -e CHROMA_AUTH_TOKEN_TRANSPORT_HEADER=your_chromadb_api_key \
  chromadb/chroma:latest
```

**Presidio (using Docker Compose):**
```bash
# Create presidio-docker-compose.yml
cat > presidio-docker-compose.yml << EOF
version: '3.8'
services:
  presidio-analyzer:
    image: mcr.microsoft.com/presidio-analyzer:latest
    ports:
      - "5001:3000"
    environment:
      - PORT=3000
  
  presidio-anonymizer:
    image: mcr.microsoft.com/presidio-anonymizer:latest
    ports:
      - "5002:3000"
    environment:
      - PORT=3000
EOF

docker-compose -f presidio-docker-compose.yml up -d
```

## Docker Deployment

### Single Container Deployment

1. **Build the Docker Image**
   ```bash
   docker build -t faultmaven:latest .
   ```

2. **Run the Container**
   ```bash
   docker run -d \
     --name faultmaven \
     -p 8000:8000 \
     --env-file .env \
     -v $(pwd)/logs:/app/logs \
     faultmaven:latest
   ```

3. **Check Container Health**
   ```bash
   docker logs faultmaven
   curl http://localhost:8000/health
   ```

### Docker Compose Deployment

Create a `docker-compose.yml` file for a complete stack:

```yaml
version: '3.8'

services:
  faultmaven:
    build: .
    ports:
      - "8000:8000"
    environment:
      - REDIS_HOST=redis
      - CHROMADB_URL=http://chromadb:8000
      - PRESIDIO_ANALYZER_URL=http://presidio-analyzer:3000
      - PRESIDIO_ANONYMIZER_URL=http://presidio-anonymizer:3000
    env_file:
      - .env
    depends_on:
      - redis
      - chromadb
      - presidio-analyzer
      - presidio-anonymizer
    volumes:
      - ./logs:/app/logs
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    command: redis-server --requirepass ${REDIS_PASSWORD:-defaultpassword}
    volumes:
      - redis-data:/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "--raw", "incr", "ping"]
      interval: 30s
      timeout: 3s
      retries: 5

  chromadb:
    image: chromadb/chroma:latest
    ports:
      - "8001:8000"
    environment:
      - CHROMA_SERVER_AUTH_CREDENTIALS_PROVIDER=chromadb.auth.token.TokenAuthCredentialsProvider
      - CHROMA_SERVER_AUTH_TOKEN_TRANSPORT_HEADER=X-Chroma-Token
      - CHROMA_AUTH_TOKEN_TRANSPORT_HEADER=${CHROMADB_API_KEY:-defaultkey}
    volumes:
      - chromadb-data:/chroma/chroma
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/heartbeat"]
      interval: 30s
      timeout: 5s
      retries: 3

  presidio-analyzer:
    image: mcr.microsoft.com/presidio-analyzer:latest
    ports:
      - "5001:3000"
    environment:
      - PORT=3000
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  presidio-anonymizer:
    image: mcr.microsoft.com/presidio-anonymizer:latest
    ports:
      - "5002:3000"
    environment:
      - PORT=3000
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  # Optional: Opik for LLM observability
  opik:
    image: comet-opik/opik:latest
    ports:
      - "5173:5173"
    environment:
      - OPIK_DATABASE_URL=postgresql://opik:opik@postgres:5432/opik
    depends_on:
      - postgres
    restart: unless-stopped

  postgres:
    image: postgres:15
    environment:
      - POSTGRES_DB=opik
      - POSTGRES_USER=opik
      - POSTGRES_PASSWORD=opik
    volumes:
      - postgres-data:/var/lib/postgresql/data
    restart: unless-stopped

volumes:
  redis-data:
  chromadb-data:
  postgres-data:

networks:
  default:
    name: faultmaven-network
```

**Deploy the Stack:**
```bash
docker-compose up -d
```

**Monitor the Deployment:**
```bash
docker-compose ps
docker-compose logs -f faultmaven
```

## Kubernetes Deployment

### Prerequisites

- Kubernetes cluster (1.21+)
- kubectl configured
- Helm 3.x (optional, for easier deployment)

### Namespace and Configuration

1. **Create Namespace**
   ```bash
   kubectl create namespace faultmaven
   ```

2. **Create ConfigMap**
   ```yaml
   # configmap.yaml
   apiVersion: v1
   kind: ConfigMap
   metadata:
     name: faultmaven-config
     namespace: faultmaven
   data:
     ENVIRONMENT: "production"
     LOG_LEVEL: "INFO"
     HOST: "0.0.0.0"
     PORT: "8000"
     CHAT_PROVIDER: "openai"
     REDIS_HOST: "faultmaven-redis"
     REDIS_PORT: "6379"
     CHROMADB_URL: "http://faultmaven-chromadb:8000"
     PRESIDIO_ANALYZER_URL: "http://faultmaven-presidio-analyzer:3000"
     PRESIDIO_ANONYMIZER_URL: "http://faultmaven-presidio-anonymizer:3000"
   ```

3. **Create Secrets**
   ```yaml
   # secrets.yaml
   apiVersion: v1
   kind: Secret
   metadata:
     name: faultmaven-secrets
     namespace: faultmaven
   type: Opaque
   stringData:
     OPENAI_API_KEY: "your_openai_api_key_here"
     ANTHROPIC_API_KEY: "your_anthropic_api_key_here"
     REDIS_PASSWORD: "your_redis_password"
     CHROMADB_API_KEY: "your_chromadb_api_key"
   ```

### Application Deployment

```yaml
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: faultmaven
  namespace: faultmaven
  labels:
    app: faultmaven
spec:
  replicas: 3
  selector:
    matchLabels:
      app: faultmaven
  template:
    metadata:
      labels:
        app: faultmaven
    spec:
      containers:
      - name: faultmaven
        image: faultmaven:latest
        ports:
        - containerPort: 8000
        envFrom:
        - configMapRef:
            name: faultmaven-config
        - secretRef:
            name: faultmaven-secrets
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 3
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5
          timeoutSeconds: 3
          failureThreshold: 2
        volumeMounts:
        - name: logs
          mountPath: /app/logs
      volumes:
      - name: logs
        emptyDir: {}
      restartPolicy: Always
---
apiVersion: v1
kind: Service
metadata:
  name: faultmaven-service
  namespace: faultmaven
spec:
  selector:
    app: faultmaven
  ports:
  - protocol: TCP
    port: 80
    targetPort: 8000
  type: ClusterIP
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: faultmaven-ingress
  namespace: faultmaven
  annotations:
    kubernetes.io/ingress.class: "nginx"
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
spec:
  tls:
  - hosts:
    - api.faultmaven.yourdomain.com
    secretName: faultmaven-tls
  rules:
  - host: api.faultmaven.yourdomain.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: faultmaven-service
            port:
              number: 80
```

### Supporting Services

**Redis Deployment:**
```yaml
# redis.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: faultmaven-redis
  namespace: faultmaven
spec:
  replicas: 1
  selector:
    matchLabels:
      app: faultmaven-redis
  template:
    metadata:
      labels:
        app: faultmaven-redis
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        args: ["redis-server", "--requirepass", "$(REDIS_PASSWORD)"]
        env:
        - name: REDIS_PASSWORD
          valueFrom:
            secretKeyRef:
              name: faultmaven-secrets
              key: REDIS_PASSWORD
        ports:
        - containerPort: 6379
        resources:
          requests:
            memory: "128Mi"
            cpu: "100m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        volumeMounts:
        - name: redis-data
          mountPath: /data
      volumes:
      - name: redis-data
        persistentVolumeClaim:
          claimName: redis-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: faultmaven-redis
  namespace: faultmaven
spec:
  selector:
    app: faultmaven-redis
  ports:
  - port: 6379
    targetPort: 6379
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: redis-pvc
  namespace: faultmaven
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
```

### Deploy to Kubernetes

```bash
# Apply all configurations
kubectl apply -f configmap.yaml
kubectl apply -f secrets.yaml
kubectl apply -f redis.yaml
kubectl apply -f deployment.yaml

# Verify deployment
kubectl get pods -n faultmaven
kubectl get services -n faultmaven
kubectl logs -f deployment/faultmaven -n faultmaven
```

## Production Considerations

### Security

1. **API Keys and Secrets**
   - Use Kubernetes secrets or external secret management
   - Rotate API keys regularly
   - Implement least-privilege access policies

2. **Network Security**
   - Use TLS/SSL for all communications
   - Implement network policies in Kubernetes
   - Restrict ingress to necessary ports only

3. **Data Protection**
   - Enable encryption at rest for all data stores
   - Implement proper backup and recovery procedures
   - Ensure PII redaction is functioning correctly

### High Availability

1. **Application Layer**
   - Deploy multiple replicas (minimum 3 for production)
   - Use anti-affinity rules to spread pods across nodes
   - Implement proper health checks and auto-healing

2. **Data Layer**
   - Deploy Redis in cluster mode or with sentinel
   - Use ChromaDB with replication
   - Implement backup strategies for persistent data

3. **Load Balancing**
   - Use ingress controllers with proper load balancing
   - Implement circuit breakers for external services
   - Configure appropriate timeout and retry policies

### Performance Optimization

1. **Resource Allocation**
   ```yaml
   resources:
     requests:
       memory: "1Gi"
       cpu: "500m"
     limits:
       memory: "4Gi"
       cpu: "2000m"
   ```

2. **Horizontal Pod Autoscaling**
   ```yaml
   apiVersion: autoscaling/v2
   kind: HorizontalPodAutoscaler
   metadata:
     name: faultmaven-hpa
     namespace: faultmaven
   spec:
     scaleTargetRef:
       apiVersion: apps/v1
       kind: Deployment
       name: faultmaven
     minReplicas: 3
     maxReplicas: 10
     metrics:
     - type: Resource
       resource:
         name: cpu
         target:
           type: Utilization
           averageUtilization: 70
     - type: Resource
       resource:
         name: memory
         target:
           type: Utilization
           averageUtilization: 80
   ```

3. **Persistent Volume Configuration**
   - Use SSD storage for better performance
   - Size volumes appropriately for growth
   - Monitor disk usage and implement cleanup policies

## Monitoring and Observability

### Health Monitoring

FaultMaven provides comprehensive health endpoints:

```bash
# Basic health check
curl http://your-domain/health

# Detailed component health
curl http://your-domain/health/dependencies

# SLA metrics
curl http://your-domain/health/sla

# Performance metrics
curl http://your-domain/metrics/performance
```

### Prometheus Integration

**ServiceMonitor for Prometheus:**
```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: faultmaven-monitor
  namespace: faultmaven
spec:
  selector:
    matchLabels:
      app: faultmaven
  endpoints:
  - port: http
    path: /metrics/prometheus
    interval: 30s
```

### Grafana Dashboard

Create monitoring dashboards for:
- Request rate and response times
- Error rates and types
- LLM provider performance
- Session metrics
- Component health status

### Log Aggregation

**Fluent Bit Configuration:**
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: fluent-bit-config
data:
  fluent-bit.conf: |
    [INPUT]
        Name tail
        Path /app/logs/*.log
        Tag faultmaven.*
        
    [OUTPUT]
        Name es
        Match faultmaven.*
        Host elasticsearch.logging.svc.cluster.local
        Port 9200
        Index faultmaven-logs
```

## Troubleshooting

### Common Issues

1. **Container Won't Start**
   ```bash
   # Check logs
   docker logs faultmaven
   kubectl logs deployment/faultmaven -n faultmaven
   
   # Check configuration
   kubectl describe pod <pod-name> -n faultmaven
   ```

2. **External Service Connection Issues**
   ```bash
   # Test Redis connectivity
   redis-cli -h redis-host -p 6379 ping
   
   # Test ChromaDB
   curl http://chromadb-host:8000/api/v1/heartbeat
   
   # Test Presidio
   curl http://presidio-host:3000/health
   ```

3. **Performance Issues**
   ```bash
   # Check resource usage
   kubectl top pods -n faultmaven
   
   # Check health endpoints
   curl http://your-domain/health/dependencies
   curl http://your-domain/metrics/performance
   ```

### Debug Mode

Enable debug mode for detailed logging:

```bash
# Environment variable
LOG_LEVEL=DEBUG

# Feature flag
ENABLE_DETAILED_TRACING=true
```

### Support

For production support:

1. Collect relevant logs and metrics
2. Check health endpoint outputs
3. Verify external service connectivity
4. Review resource utilization
5. Check configuration validity

**Health Check Command:**
```bash
# Comprehensive health check
curl -s http://your-domain/health/dependencies | jq '.'
```

This deployment guide provides a comprehensive foundation for deploying FaultMaven in various environments, from development to production-ready Kubernetes clusters.