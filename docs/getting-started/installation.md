# FaultMaven Installation Guide

Complete guide for installing and configuring FaultMaven.

**Note**: This guide covers both Community Edition (open source, self-host) and Enterprise Edition (additional features for production deployments). Enterprise features require separate licensing and infrastructure.

## Table of Contents

- [Installation Modes](#installation-modes)
- [Community Edition](#community-edition)
- [Enterprise Edition](#enterprise-edition)
- [Configuration](#configuration)
- [Upgrading from Community to Enterprise](#upgrading-from-community-to-enterprise)
- [Troubleshooting](#troubleshooting)

---

## Installation Modes

FaultMaven offers two installation modes optimized for different use cases:

| Feature | Community Edition | Enterprise Edition |
|---------|------------------|-------------------|
| **Install Command** | `pip install faultmaven` | `pip install faultmaven[enterprise]` |
| **Use Case** | Local development, testing, community users | Production deployments, enterprise features |
| **Database** | SQLite (local file) | SQLite + PostgreSQL support |
| **Session Management** | In-memory | In-memory + Redis support |
| **File Storage** | Local filesystem | Local + AWS S3 + Azure Blob |
| **Observability** | Basic logging | Opik tracing, Prometheus metrics |
| **PII Protection** | Disabled | Presidio PII redaction |
| **Installation Size** | ~500 MB | ~1.2 GB |
| **External Dependencies** | None (standalone) | Optional (Redis, PostgreSQL, Presidio) |

---

## Community Edition

### Quick Start

Perfect for local development, testing, and community users. Zero external dependencies required.

```bash
# Install FaultMaven
pip install faultmaven

# Start the server
faultmaven start

# Or run directly with Python
python -m faultmaven
```

### What's Included

The community edition includes all core features:

- ✅ **FastAPI REST API server**
- ✅ **Multi-LLM support** (7 providers: Fireworks, OpenAI, Anthropic, Gemini, HuggingFace, OpenRouter, Groq)
- ✅ **Agentic framework** with autonomous reasoning
- ✅ **Knowledge base** (ChromaDB with RAG)
- ✅ **SQLite database** (local file storage)
- ✅ **In-memory sessions** (no external cache needed)
- ✅ **Local file storage** (no cloud dependencies)
- ✅ **All API endpoints** (full REST API)
- ✅ **JWT authentication**
- ✅ **Case management**
- ✅ **Evidence processing**

### Configuration

Create a `.env` file in your project directory:

```bash
# Minimal configuration for community edition
CHAT_PROVIDER=fireworks
FIREWORKS_API_KEY=your_fireworks_api_key_here

# Optional: Use OpenAI instead
# CHAT_PROVIDER=openai
# OPENAI_API_KEY=your_openai_api_key_here

# Optional: Use Anthropic instead
# CHAT_PROVIDER=anthropic
# ANTHROPIC_API_KEY=your_anthropic_api_key_here
```

### Default Behavior

The community edition uses the following defaults:

```python
# Storage (no external dependencies)
database_url = "sqlite:///./data/faultmaven.db"
user_storage_type = "inmemory"
case_storage_type = "database"
# Sessions: FakeRedis (in-process, no external server needed)
# Vectors: ChromaDB PersistentClient (local, no external server needed)
vector_storage_type = "chromadb"

# Enterprise features (disabled)
opik_enabled = False
prometheus_enabled = False
tracing_enabled = False
metrics_enabled = False
protection_enabled = False
sanitize_pii = False
```

### Running the Server

```bash
# Standard startup
faultmaven start

# Or with custom port
faultmaven start --port 8080

# Or with uvicorn directly
uvicorn faultmaven.main:app --host 0.0.0.0 --port 8000
```

### API Access

Once running, access the API at:

- **API Base URL**: `http://localhost:8090`
- **API Documentation**: `http://localhost:8090/docs` (Swagger UI)
- **Health Check**: `http://localhost:8090/health`

---

## Enterprise Edition

### Installation

```bash
# Install with all enterprise features
pip install faultmaven[enterprise]
```

### What's Added

Enterprise edition adds:

- ✅ **Opik tracing** - LLM call tracing and performance monitoring
- ✅ **Prometheus metrics** - Production-grade metrics export
- ✅ **PII redaction** - Presidio-powered sensitive data protection
- ✅ **Redis sessions** - Distributed session management
- ✅ **PostgreSQL support** - Production database support
- ✅ **Cloud storage** - AWS S3 and Azure Blob support
- ✅ **Advanced observability** - Detailed tracing and monitoring

### External Dependencies

For full enterprise functionality, you'll need:

1. **Redis** (optional - for distributed sessions)
   ```bash
   # Using Docker
   docker run -d -p 6379:6379 redis:latest
   ```

2. **PostgreSQL** (optional - for production database)
   ```bash
   # Using Docker
   docker run -d -p 5432:5432 \
     -e POSTGRES_PASSWORD=password \
     -e POSTGRES_DB=faultmaven \
     postgres:15
   ```

3. **Presidio** (optional - for PII redaction)
   ```bash
   # Using Docker Compose (recommended)
   # See: https://github.com/microsoft/presidio
   ```

### Configuration

Create a `.env` file with enterprise settings:

```bash
# LLM Provider
CHAT_PROVIDER=fireworks
FIREWORKS_API_KEY=your_fireworks_api_key_here

# Database Configuration
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/faultmaven

# Redis Configuration
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=your_redis_password  # Optional

# Sessions: Real Redis auto-selected when REDIS_HOST is set

# Observability
OPIK_ENABLED=true
OPIK_API_KEY=your_opik_api_key  # Optional - for Comet Opik cloud
PROMETHEUS_ENABLED=true
TRACING_ENABLED=true
METRICS_ENABLED=true

# PII Protection
PROTECTION_ENABLED=true
SANITIZE_PII=true
PRESIDIO_ANALYZER_URL=http://localhost:5001
PRESIDIO_ANONYMIZER_URL=http://localhost:5002

# Cloud Storage (AWS S3)
STORAGE_BACKEND=s3
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
S3_BUCKET=faultmaven-evidence

# Or Cloud Storage (Azure Blob)
# STORAGE_BACKEND=azure
# AZURE_STORAGE_CONNECTION_STRING=your_connection_string
# AZURE_CONTAINER_NAME=faultmaven-evidence
```

### Running in Production

```bash
# Using Docker Compose (recommended)
docker-compose -f docker-compose.prod.yml up -d

# Or with uvicorn (manual)
uvicorn faultmaven.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 4 \
  --log-level info
```

### Kubernetes Deployment

For Kubernetes deployments, see:
- [Helm Charts](../../helm/README.md)
- [Kubernetes Deployment Guide](../operations/KUBERNETES_DEPLOYMENT.md)

---

## Configuration

### Environment Variables Reference

#### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `CHAT_PROVIDER` | `fireworks` | LLM provider: `fireworks`, `openai`, `anthropic`, `gemini`, `groq` |
| `FIREWORKS_API_KEY` | - | Fireworks AI API key |
| `OPENAI_API_KEY` | - | OpenAI API key |
| `ANTHROPIC_API_KEY` | - | Anthropic API key |
| `HOST` | `0.0.0.0` | Server host |
| `PORT` | `8000` | Server port |

#### Database Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///./data/faultmaven.db` | Database connection URL |
| `USER_STORAGE_TYPE` | `inmemory` | User storage: `inmemory`, `postgresql` |
| `CASE_STORAGE_TYPE` | `database` | Case storage: `database` (SQLite/PostgreSQL), `inmemory` (testing only) |
| `REDIS_HOST` | _(unset)_ | Sessions: FakeRedis when unset, real Redis when set |
| `VECTOR_STORAGE_TYPE` | `inmemory` | Vector storage: `inmemory`, `chromadb` |

#### Enterprise Features (Enterprise Edition Only)

| Variable | Default | Description |
|----------|---------|-------------|
| `OPIK_ENABLED` | `false` | Enable Opik LLM tracing |
| `PROMETHEUS_ENABLED` | `false` | Enable Prometheus metrics |
| `TRACING_ENABLED` | `false` | Enable distributed tracing |
| `METRICS_ENABLED` | `false` | Enable metrics collection |
| `PROTECTION_ENABLED` | `false` | Enable PII protection |
| `SANITIZE_PII` | `false` | Enable PII sanitization |

#### Redis Configuration (Enterprise Edition)

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_HOST` | `192.168.0.111` | Redis host |
| `REDIS_PORT` | `30379` | Redis port |
| `REDIS_PASSWORD` | - | Redis password (optional) |

#### Security Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `JWT_ALGORITHM` | `RS256` | JWT signing algorithm |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token expiration |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token expiration |

---

## Upgrading from Community to Enterprise

### Step 1: Install Enterprise Dependencies

```bash
# Upgrade installation to include enterprise features
pip install --upgrade faultmaven[enterprise]
```

### Step 2: Set Up External Services

```bash
# Start Redis (for distributed sessions)
docker run -d -p 6379:6379 --name faultmaven-redis redis:latest

# Start PostgreSQL (for production database)
docker run -d -p 5432:5432 \
  -e POSTGRES_PASSWORD=password \
  -e POSTGRES_DB=faultmaven \
  --name faultmaven-postgres \
  postgres:15
```

### Step 3: Migrate Data (Optional)

If you have existing SQLite data:

```bash
# Export SQLite data
python -m faultmaven.scripts.export_data --output data_export.json

# Update .env to use PostgreSQL
echo "DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/faultmaven" >> .env

# Import data to PostgreSQL
python -m faultmaven.scripts.import_data --input data_export.json
```

### Step 4: Update Configuration

Add enterprise settings to `.env`:

```bash
# Enable enterprise features
OPIK_ENABLED=true
PROMETHEUS_ENABLED=true
TRACING_ENABLED=true
METRICS_ENABLED=true
PROTECTION_ENABLED=true
SANITIZE_PII=true

# Redis for sessions (real Redis auto-selected when host configured)
REDIS_HOST=localhost
REDIS_PORT=6379

# Use PostgreSQL for storage
USER_STORAGE_TYPE=postgresql
CASE_STORAGE_TYPE=postgresql
```

### Step 5: Restart Server

```bash
# Restart with new configuration
faultmaven restart

# Or with Docker Compose
docker-compose restart faultmaven
```

---

## Troubleshooting

### Common Issues

#### Issue: `ModuleNotFoundError: No module named 'opik'`

**Cause**: Trying to use enterprise features without enterprise dependencies.

**Solution**:
```bash
pip install faultmaven[enterprise]
```

#### Issue: `Cannot connect to Redis`

**Cause**: Redis not running or wrong connection settings.

**Solution**:
```bash
# Check Redis is running
docker ps | grep redis

# Test connection
redis-cli -h localhost -p 6379 ping
# Should return: PONG

# Update .env with correct settings
REDIS_HOST=localhost
REDIS_PORT=6379
```

#### Issue: `SQLite database locked`

**Cause**: Multiple processes accessing SQLite database.

**Solution**:
```bash
# For production, use PostgreSQL instead
pip install faultmaven[enterprise]

# Update .env
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/faultmaven
```

#### Issue: `PII redaction not working`

**Cause**: Presidio services not running or `PROTECTION_ENABLED=false`.

**Solution**:
```bash
# Check configuration
grep PROTECTION_ENABLED .env
grep SANITIZE_PII .env

# Should be:
PROTECTION_ENABLED=true
SANITIZE_PII=true

# Start Presidio services (if using local Presidio)
docker-compose up -d presidio-analyzer presidio-anonymizer
```

### Verification Checklist

After installation, verify your setup:

```bash
# 1. Check installed packages
pip list | grep faultmaven

# 2. Verify dependencies
pip check

# 3. Test server startup
faultmaven start --test

# 4. Check health endpoint
curl http://localhost:8090/health

# 5. Verify API documentation
curl http://localhost:8090/docs
```

### Getting Help

- **GitHub Issues**: [https://github.com/FaultMaven/faultmaven/issues](https://github.com/FaultMaven/faultmaven/issues)
- **Documentation**: [https://github.com/FaultMaven/faultmaven/blob/main/README.md](https://github.com/FaultMaven/faultmaven/blob/main/README.md)
- **Community**: [Discussions](https://github.com/FaultMaven/faultmaven/discussions)

---

## Additional Resources

- [Architecture Overview](../architecture/ARCHITECTURE.md)
- [API Documentation](../api/API_REFERENCE.md)
- [Configuration Reference](../reference/CONFIGURATION.md)
- [Deployment Guide](../operations/DEPLOYMENT.md)
- [Security Best Practices](../security/SECURITY_BEST_PRACTICES.md)
