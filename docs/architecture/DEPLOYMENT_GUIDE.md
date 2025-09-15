# Enhanced FaultMaven Deployment Guide

**Document Type**: Deployment Guide  
**Last Updated**: August 2025

This guide provides comprehensive instructions for deploying FaultMaven in various environments, from local development to production Kubernetes clusters. The system now features advanced intelligent communication capabilities including memory management, strategic planning, and dynamic prompting.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Environment Configuration](#environment-configuration)
- [Local Development Deployment](#local-development-deployment)
- [Docker Deployment](#docker-deployment)
- [Kubernetes Deployment](#kubernetes-deployment)
- [Production Considerations](#production-considerations)
- [Monitoring and Observability](#monitoring-and-observability)
- [Intelligence Service Configuration](#intelligence-service-configuration)
- [Troubleshooting](#troubleshooting)

## Prerequisites

### System Requirements

**Minimum Requirements:**
- CPU: 4 cores (increased for intelligence services)
- Memory: 8GB RAM (increased for memory management)
- Storage: 20GB available space (increased for memory storage)
- Network: Internet connectivity for LLM providers

**Recommended for Production:**
- CPU: 8+ cores (for parallel intelligence processing)
- Memory: 16GB+ RAM (for large memory operations)
- Storage: 100GB+ available space (for extensive memory storage)
- Network: High-bandwidth connection with low latency

**Intelligence Service Requirements:**
- **Memory Service**: Additional 4-8GB RAM for hierarchical memory
- **Planning Service**: Additional 2-4GB RAM for strategic planning
- **Prompt Engine**: Additional 2-4GB RAM for advanced prompting

### Software Dependencies

**Required:**
- Python 3.10+
- Docker (for containerized deployment)
- Kubernetes 1.21+ (for K8s deployment)

**External Services (Required):**
- Redis 6.0+ (multi-session per user storage with client-based resumption, caching, and memory management)
- ChromaDB 0.4+ (vector database for knowledge base and memory)
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

### Enhanced Environment Variables

Create a `.env` file in the project root with the following configuration:

```bash
# === Core Application Settings ===
ENVIRONMENT=production
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=INFO

# === Intelligence Service Configuration ===
# Memory Management System
ENABLE_MEMORY_FEATURES=true
MEMORY_HIERARCHY_LEVELS=4
MEMORY_MAX_WORKING_SIZE_MB=512
MEMORY_MAX_SESSION_SIZE_MB=256
MEMORY_MAX_USER_SIZE_MB=1024
MEMORY_MAX_EPISODIC_SIZE_MB=2048
MEMORY_CONSOLIDATION_INTERVAL_MINUTES=30
MEMORY_SEMANTIC_SEARCH_ENABLED=true
MEMORY_VECTOR_DIMENSION=768

# Strategic Planning System
ENABLE_PLANNING_FEATURES=true
PLANNING_MAX_PHASES=7
PLANNING_STRATEGY_CACHE_SIZE=100
PLANNING_RISK_ASSESSMENT_ENABLED=true
PLANNING_ALTERNATIVE_SOLUTIONS_ENABLED=true
PLANNING_EXECUTION_TIMEOUT_SECONDS=300

# Advanced Prompting System
ENABLE_ADVANCED_PROMPTING=true
PROMPT_MAX_LAYERS=6
PROMPT_OPTIMIZATION_ENABLED=true
PROMPT_VERSIONING_ENABLED=true
PROMPT_A_B_TESTING_ENABLED=true
PROMPT_PERFORMANCE_TRACKING=true

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

# === Enhanced Database Configuration ===
# Redis (Session Storage, Caching, and Memory)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=your_redis_password
REDIS_DB=0
REDIS_MEMORY_DB=1
REDIS_CACHE_DB=2
REDIS_MEMORY_TTL_HOURS=168  # 7 days
REDIS_CACHE_TTL_HOURS=24    # 1 day

# ChromaDB (Vector Database for Knowledge Base and Memory)
CHROMADB_URL=http://localhost:8000
CHROMADB_API_KEY=your_chromadb_api_key
CHROMADB_COLLECTION=faultmaven_knowledge
CHROMADB_MEMORY_COLLECTION=faultmaven_memory
CHROMADB_MAX_COLLECTIONS=10

# === Security Services ===
# Presidio (PII Redaction)
PRESIDIO_ANALYZER_URL=http://localhost:5001
PRESIDIO_ANONYMIZER_URL=http://localhost:5002

# === Enhanced Session Management ===
SESSION_TIMEOUT_MINUTES=60  # Increased for intelligence features
SESSION_CLEANUP_INTERVAL_MINUTES=15
SESSION_MAX_MEMORY_MB=500   # Increased for memory storage
SESSION_CLEANUP_BATCH_SIZE=50
SESSION_MEMORY_PRESERVATION=true
SESSION_INTELLIGENCE_CONTEXT=true

# === Enhanced Observability (Optional) ===
# Opik Configuration
OPIK_API_KEY=your_opik_api_key
OPIK_PROJECT_NAME=faultmaven
OPIK_WORKSPACE=your_workspace

# Local Opik (if self-hosted)
OPIK_USE_LOCAL=true
OPIK_BASE_URL=http://localhost:5173

# === Enhanced Feature Flags ===
ENABLE_INTELLIGENT_FEATURES=true
ENABLE_MEMORY_FEATURES=true
ENABLE_PLANNING_FEATURES=true
ENABLE_ADVANCED_PROMPTING=true
ENABLE_LEGACY_COMPATIBILITY=false
ENABLE_EXPERIMENTAL_FEATURES=false
ENABLE_PERFORMANCE_MONITORING=true
ENABLE_DETAILED_TRACING=true
ENABLE_MEMORY_ANALYTICS=true
ENABLE_PLANNING_ANALYTICS=true

# === Enhanced Logging Configuration ===
LOG_FORMAT=json
LOG_DEDUPE=true
LOG_BUFFER_SIZE=200  # Increased for intelligence logging
LOG_FLUSH_INTERVAL=5
LOG_MEMORY_OPERATIONS=true
LOG_PLANNING_OPERATIONS=true
LOG_PROMPT_OPERATIONS=true
```

### Enhanced Configuration Validation

Before deployment, validate your enhanced configuration:

```bash
# Activate virtual environment
source .venv/bin/activate

# Test enhanced configuration
python -c "
from faultmaven.config.enhanced_configuration_manager import get_enhanced_config
config = get_enhanced_config()
if config.validate_intelligence_services():
    print('✅ Enhanced configuration is valid')
    print(f'Memory features: {config.memory_enabled}')
    print(f'Planning features: {config.planning_enabled}')
    print(f'Advanced prompting: {config.advanced_prompting_enabled}')
else:
    print('❌ Enhanced configuration validation failed')
    print(config.validation_errors)
"
```

## Local Development Deployment

### Enhanced Quick Start

1. **Clone and Setup**
   ```bash
   git clone <repository_url>
   cd FaultMaven
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   pip install -r requirements-test.txt
   pip install -r requirements-intelligence.txt  # New intelligence dependencies
   ```

2. **Download Enhanced ML Models**
   ```bash
   python -m spacy download en_core_web_lg
   python -m spacy download en_core_web_trf  # For advanced NLP
   python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"  # For memory embeddings
   ```

3. **Configure Enhanced Environment**
   ```bash
   cp .env.example .env
   # Edit .env with your enhanced configuration
   ```

4. **Start Enhanced Development Server**
   ```bash
   ./run_faultmaven.sh
   ```

5. **Verify Enhanced Deployment**
   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8000/health/intelligence  # New intelligence health check
   ```

### Development with Enhanced External Services

For full functionality with intelligence features, start the required external services:

**Enhanced Redis (using Docker):**
```bash
docker run -d \
  --name faultmaven-redis \
  -p 6379:6379 \
  -e REDIS_MAXMEMORY=2gb \
  -e REDIS_MAXMEMORY_POLICY=allkeys-lru \
  redis:7-alpine redis-server --requirepass your_redis_password
```

**Enhanced ChromaDB (using Docker):**
```bash
docker run -d \
  --name faultmaven-chromadb \
  -p 8000:8000 \
  -e CHROMA_SERVER_AUTH_CREDENTIALS_PROVIDER=chromadb.auth.token.TokenAuthCredentialsProvider \
  -e CHROMA_SERVER_AUTH_CREDENTIALS=your_chromadb_api_key \
  -e CHROMA_SERVER_AUTH_PROVIDER=chromadb.auth.token.TokenAuthServerProvider \
  -e CHROMA_SERVER_AUTH_CREDENTIALS_FILE=/chroma/chroma_auth.json \
  -v $(pwd)/chroma_data:/chroma/chroma \
  chromadb/chroma:latest
```

**Enhanced Presidio (using Docker Compose):**
```yaml
# docker-compose.presidio.yml
version: '3.8'
services:
  presidio-analyzer:
    image: mcr.microsoft.com/presidio-analyzer:latest
    ports:
      - "5001:3000"
    environment:
      - REDIS_HOST=redis
      - REDIS_PORT=6379
    depends_on:
      - redis
  
  presidio-anonymizer:
    image: mcr.microsoft.com/presidio-anonymizer:latest
    ports:
      - "5002:3000"
    environment:
      - REDIS_HOST=redis
      - REDIS_PORT=6379
    depends_on:
      - redis
  
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
```

## Intelligence Service Configuration

### Memory Management System Setup

Configure the hierarchical memory system:

```bash
# Memory service configuration
export MEMORY_SERVICE_CONFIG="{
  'hierarchy_levels': 4,
  'working_memory': {
    'max_size_mb': 512,
    'ttl_hours': 24,
    'consolidation_interval_minutes': 30
  },
  'session_memory': {
    'max_size_mb': 256,
    'ttl_hours': 168,
    'consolidation_interval_minutes': 60
  },
  'user_memory': {
    'max_size_mb': 1024,
    'ttl_hours': 8760,
    'consolidation_interval_minutes': 1440
  },
  'episodic_memory': {
    'max_size_mb': 2048,
    'ttl_hours': 87600,
    'consolidation_interval_minutes': 10080
  }
}"

# Initialize memory collections
python -c "
from faultmaven.services.memory_service import MemoryService
from faultmaven.infrastructure.persistence.redis_store import RedisMemoryStore
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBMemoryStore

# Initialize memory stores
redis_store = RedisMemoryStore()
chroma_store = ChromaDBMemoryStore()

# Initialize memory service
memory_service = MemoryService(redis_store, chroma_store)
memory_service.initialize_collections()

print('✅ Memory collections initialized')
"
```

### Strategic Planning System Setup

Configure the strategic planning system:

```bash
# Planning service configuration
export PLANNING_SERVICE_CONFIG="{
  'max_phases': 7,
  'strategy_cache_size': 100,
  'risk_assessment_enabled': true,
  'alternative_solutions_enabled': true,
  'execution_timeout_seconds': 300,
  'planning_models': {
    'problem_decomposition': 'gpt-4o',
    'strategy_development': 'gpt-4o',
    'risk_assessment': 'gpt-4o'
  }
}"

# Initialize planning service
python -c "
from faultmaven.services.planning_service import PlanningService
from faultmaven.infrastructure.llm.router import LLMRouter

# Initialize LLM router
llm_router = LLMRouter()

# Initialize planning service
planning_service = PlanningService(llm_router)
planning_service.initialize()

print('✅ Planning service initialized')
"
```

### Advanced Prompting System Setup

Configure the advanced prompting system:

```bash
# Prompt engine configuration
export PROMPT_ENGINE_CONFIG="{
  'max_layers': 6,
  'optimization_enabled': true,
  'versioning_enabled': true,
  'a_b_testing_enabled': true,
  'performance_tracking': true,
  'layer_configs': {
    'system': {'weight': 1.0, 'optimization': true},
    'context': {'weight': 0.8, 'optimization': true},
    'domain': {'weight': 0.9, 'optimization': true},
    'task': {'weight': 1.0, 'optimization': true},
    'safety': {'weight': 0.7, 'optimization': false},
    'adaptation': {'weight': 0.6, 'optimization': true}
  }
}"

# Initialize prompt engine
python -c "
from faultmaven.core.prompting import AdvancedPromptEngine
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.services.memory_service import MemoryService

# Initialize dependencies
llm_router = LLMRouter()
memory_service = MemoryService()

# Initialize prompt engine
prompt_engine = AdvancedPromptEngine(llm_router, memory_service)
prompt_engine.initialize()

print('✅ Advanced prompt engine initialized')
"
```

## Enhanced Docker Deployment

### Enhanced Dockerfile

Create an enhanced Dockerfile with intelligence dependencies:

```dockerfile
# Enhanced FaultMaven Dockerfile
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements
COPY requirements.txt requirements-intelligence.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r requirements-intelligence.txt

# Download enhanced ML models
RUN python -m spacy download en_core_web_lg
RUN python -m spacy download en_core_web_trf
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"

# Copy application code
COPY faultmaven/ ./faultmaven/
COPY run_faultmaven.sh ./

# Set environment variables
ENV PYTHONPATH=/app
ENV ENABLE_INTELLIGENT_FEATURES=true
ENV ENABLE_MEMORY_FEATURES=true
ENV ENABLE_PLANNING_FEATURES=true
ENV ENABLE_ADVANCED_PROMPTING=true

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run application
CMD ["./run_faultmaven.sh"]
```

### Enhanced Docker Compose

Create an enhanced docker-compose.yml with intelligence services:

```yaml
# Enhanced docker-compose.yml
version: '3.8'

services:
  faultmaven:
    build: .
    ports:
      - "8000:8000"
    environment:
      - ENABLE_INTELLIGENT_FEATURES=true
      - ENABLE_MEMORY_FEATURES=true
      - ENABLE_PLANNING_FEATURES=true
      - ENABLE_ADVANCED_PROMPTING=true
      - REDIS_HOST=redis
      - CHROMADB_URL=http://chromadb:8000
      - PRESIDIO_ANALYZER_URL=http://presidio-analyzer:3000
      - PRESIDIO_ANONYMIZER_URL=http://presidio-anonymizer:3000
    depends_on:
      - redis
      - chromadb
      - presidio-analyzer
      - presidio-anonymizer
    volumes:
      - ./logs:/app/logs
      - ./memory_data:/app/memory_data
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    command: redis-server --requirepass your_redis_password --maxmemory 2gb --maxmemory-policy allkeys-lru
    volumes:
      - redis_data:/data
    restart: unless-stopped

  chromadb:
    image: chromadb/chroma:latest
    ports:
      - "8000:8000"
    environment:
      - CHROMA_SERVER_AUTH_CREDENTIALS_PROVIDER=chromadb.auth.token.TokenAuthCredentialsProvider
      - CHROMA_SERVER_AUTH_CREDENTIALS=your_chromadb_api_key
      - CHROMA_SERVER_AUTH_PROVIDER=chromadb.auth.token.TokenAuthServerProvider
    volumes:
      - chromadb_data:/chroma/chroma
    restart: unless-stopped

  presidio-analyzer:
    image: mcr.microsoft.com/presidio-analyzer:latest
    ports:
      - "5001:3000"
    environment:
      - REDIS_HOST=redis
      - REDIS_PORT=6379
    depends_on:
      - redis
    restart: unless-stopped

  presidio-anonymizer:
    image: mcr.microsoft.com/presidio-anonymizer:latest
    ports:
      - "5002:3000"
    environment:
      - REDIS_HOST=redis
      - REDIS_PORT=6379
    depends_on:
      - redis
    restart: unless-stopped

volumes:
  redis_data:
  chromadb_data:
  memory_data:
```

## Enhanced Kubernetes Deployment

### Enhanced Kubernetes Manifests

Create enhanced Kubernetes manifests with intelligence services:

```yaml
# Enhanced deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: faultmaven
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
        env:
        - name: ENABLE_INTELLIGENT_FEATURES
          value: "true"
        - name: ENABLE_MEMORY_FEATURES
          value: "true"
        - name: ENABLE_PLANNING_FEATURES
          value: "true"
        - name: ENABLE_ADVANCED_PROMPTING
          value: "true"
        - name: REDIS_HOST
          value: "redis-service"
        - name: CHROMADB_URL
          value: "http://chromadb-service:8000"
        - name: PRESIDIO_ANALYZER_URL
          value: "http://presidio-analyzer-service:3000"
        - name: PRESIDIO_ANONYMIZER_URL
          value: "http://presidio-anonymizer-service:3000"
        resources:
          requests:
            memory: "8Gi"
            cpu: "2"
          limits:
            memory: "16Gi"
            cpu: "4"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 60
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /health/intelligence
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        volumeMounts:
        - name: memory-storage
          mountPath: /app/memory_data
        - name: logs
          mountPath: /app/logs
      volumes:
      - name: memory-storage
        persistentVolumeClaim:
          claimName: faultmaven-memory-pvc
      - name: logs
        emptyDir: {}
```

### Enhanced Service Manifests

```yaml
# Enhanced service.yaml
apiVersion: v1
kind: Service
metadata:
  name: faultmaven-service
spec:
  selector:
    app: faultmaven
  ports:
  - port: 80
    targetPort: 8000
  type: LoadBalancer
---
apiVersion: v1
kind: Service
metadata:
  name: redis-service
spec:
  selector:
    app: redis
  ports:
  - port: 6379
    targetPort: 6379
---
apiVersion: v1
kind: Service
metadata:
  name: chromadb-service
spec:
  selector:
    app: chromadb
  ports:
  - port: 8000
    targetPort: 8000
```

### Enhanced Persistent Volume Claims

```yaml
# Enhanced pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: faultmaven-memory-pvc
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 100Gi
  storageClassName: fast-ssd
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: faultmaven-logs-pvc
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 50Gi
  storageClassName: fast-ssd
```

## Enhanced Production Considerations

### Intelligence Service Scaling

Configure scaling for intelligence services:

```yaml
# Enhanced hpa.yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: faultmaven-hpa
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
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 60
      policies:
      - type: Percent
        value: 100
        periodSeconds: 15
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
      - type: Percent
        value: 10
        periodSeconds: 60
```

### Memory and Planning Service Monitoring

Configure monitoring for intelligence services:

```yaml
# Enhanced monitoring.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: faultmaven-monitoring-config
data:
  prometheus.yml: |
    global:
      scrape_interval: 15s
    scrape_configs:
    - job_name: 'faultmaven'
      static_configs:
      - targets: ['faultmaven-service:80']
      metrics_path: /metrics
      scrape_interval: 10s
    - job_name: 'faultmaven-intelligence'
      static_configs:
      - targets: ['faultmaven-service:80']
      metrics_path: /metrics/intelligence
      scrape_interval: 5s
```

## Enhanced Monitoring and Observability

### Intelligence Service Health Checks

Monitor the health of intelligence services:

```bash
# Check memory service health
curl -X GET "http://localhost:8000/health/memory" \
  -H "accept: application/json"

# Check planning service health
curl -X GET "http://localhost:8000/health/planning" \
  -H "accept: application/json"

# Check prompt engine health
curl -X GET "http://localhost:8000/health/prompt-engine" \
  -H "accept: application/json"

# Check overall intelligence health
curl -X GET "http://localhost:8000/health/intelligence" \
  -H "accept: application/json"
```

### Memory Analytics Dashboard

Access memory analytics:

```bash
# Get memory usage statistics
curl -X GET "http://localhost:8000/analytics/memory/usage" \
  -H "accept: application/json"

# Get memory performance metrics
curl -X GET "http://localhost:8000/analytics/memory/performance" \
  -H "accept: application/json"

# Get memory consolidation insights
curl -X GET "http://localhost:8000/analytics/memory/consolidation" \
  -H "accept: application/json"
```

### Planning Analytics Dashboard

Access planning analytics:

```bash
# Get planning strategy metrics
curl -X GET "http://localhost:8000/analytics/planning/strategies" \
  -H "accept: application/json"

# Get planning performance metrics
curl -X GET "http://localhost:8000/analytics/planning/performance" \
  -H "accept: application/json"

# Get planning success rates
curl -X GET "http://localhost:8000/analytics/planning/success-rates" \
  -H "accept: application/json"
```

## Enhanced Troubleshooting

### Intelligence Service Diagnostics

Diagnose intelligence service issues:

```bash
# Check intelligence service logs
kubectl logs -l app=faultmaven -c faultmaven | grep -E "(memory|planning|prompt)"

# Check intelligence service status
kubectl exec -it deployment/faultmaven -- curl -s http://localhost:8000/health/intelligence

# Check memory service connectivity
kubectl exec -it deployment/faultmaven -- python -c "
from faultmaven.services.memory_service import MemoryService
try:
    memory_service = MemoryService()
    status = memory_service.get_health_status()
    print(f'Memory service status: {status}')
except Exception as e:
    print(f'Memory service error: {e}')
"

# Check planning service connectivity
kubectl exec -it deployment/faultmaven -- python -c "
from faultmaven.services.planning_service import PlanningService
try:
    planning_service = PlanningService()
    status = planning_service.get_health_status()
    print(f'Planning service status: {status}')
except Exception as e:
    print(f'Planning service error: {e}')
"
```

### Performance Optimization

Optimize intelligence service performance:

```bash
# Monitor memory usage
kubectl top pods -l app=faultmaven

# Check memory service performance
curl -X GET "http://localhost:8000/analytics/memory/performance" \
  -H "accept: application/json" | jq '.response_time_percentiles'

# Check planning service performance
curl -X GET "http://localhost:8000/analytics/planning/performance" \
  -H "accept: application/json" | jq '.strategy_development_time'

# Optimize memory storage
curl -X POST "http://localhost:8000/admin/memory/optimize" \
  -H "accept: application/json" \
  -H "content-type: application/json" \
  -d '{"optimization_level": "aggressive"}'
```

## Conclusion

This enhanced deployment guide provides comprehensive instructions for deploying FaultMaven with advanced intelligent communication capabilities. The new memory management, strategic planning, and advanced prompting services require additional resources and configuration but provide significant improvements in system intelligence and user experience.

**Key Deployment Benefits**:
- **Intelligent Context Awareness**: All operations consider memory and planning context
- **Strategic Execution**: Planning-driven approach to problem solving
- **Continuous Learning**: System improves through conversation analysis and feedback
- **Enhanced User Experience**: Context-aware, personalized, and progressive interactions
- **Scalable Intelligence**: Horizontal scaling for intelligence services

**Next Steps**:
1. **Deploy Enhanced Services**: Use the enhanced configuration and manifests
2. **Monitor Intelligence Services**: Use the new health checks and analytics
3. **Optimize Performance**: Monitor and optimize intelligence service performance
4. **Scale as Needed**: Use HPA for automatic scaling of intelligence services

This enhanced deployment positions FaultMaven for intelligent operation in production environments while maintaining all the benefits of the existing clean architecture.