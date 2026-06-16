# FaultMaven Installation Guide

Complete guide for installing and configuring FaultMaven.

**Note**: This guide covers installing **FaultMaven Standalone** — the self-hosted, single-user, fair-source (FSL-1.1-ALv2) deployment you run on your own hardware with fixed simple defaults. **Cloud** is the separate cloud-native deployment (multi-tenant; run as FaultMaven-hosted SaaS or self-hosted as a private cloud), configured through Kubernetes rather than installed via this guide — see [Scaling Beyond Standalone (Cloud)](#scaling-beyond-standalone-cloud). Standalone and Cloud are the same core engine, differing only by configuration and which composed modules are present.

## Table of Contents

- [Deployment Profiles](#deployment-profiles)
- [Standalone (Default)](#standalone-default)
- [Scaling Beyond Standalone (Cloud)](#scaling-beyond-standalone-cloud)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)

---

## Deployment Profiles

FaultMaven is **one codebase**. Standalone and Cloud run the same code and schema, differing only by **configuration** and by **which composed modules are present**. You choose the deployment at install time:

- **Standalone** — self-hosted, **single-user**, with **fixed simple defaults** (SQLite, in-process FakeRedis, embedded ChromaDB, local filesystem). No scale-backend knobs are surfaced; setup is "pick an LLM provider, paste a key, go." **This guide covers Standalone.**
- **Cloud** — the cloud-native deployment architecture (multi-tenant, elastic), run as managed SaaS *or* self-hosted as a private cloud. Its production infrastructure (PostgreSQL, Redis, object storage, observability, PII redaction) is configured through **Kubernetes ConfigMaps/Secrets**. See [Scaling Beyond Standalone (Cloud)](#scaling-beyond-standalone-cloud).

| | Standalone | Cloud |
|---|---|---|
| **Operator** | Self-hosted | FaultMaven-hosted, or self-hosted private cloud |
| **Tenancy / users** | Single-tenant, single-user | Multi-tenant, multi-user |
| **Database** | SQLite (fixed) | PostgreSQL |
| **Sessions** | In-process FakeRedis (fixed) | Redis |
| **File storage** | Local filesystem (fixed) | Object storage (S3 / Azure Blob) |
| **Observability / PII** | Basic logging | Opik tracing, Prometheus, Presidio PII redaction |
| **Configuration surface** | Single slim `.env` | Kubernetes ConfigMaps + Secrets |

---

## Standalone (Default)

### Quick Start

Perfect for local development, testing, and single-operator self-hosting. Zero external dependencies required.

```bash
# Clone the repository
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven

# Configure your LLM provider
cp .env.example .env
# Edit .env: set CHAT_PROVIDER and the matching *_API_KEY

# Start with Docker (recommended)
./faultmaven.sh start
```

On first start the API auto-initializes the database, runs migrations, creates a default admin, and seeds the bundled Knowledge Base. To run as a local process instead of Docker, see [Running the Server](#running-the-server).

### What's Included

The standalone default includes all core features:

- ✅ **FastAPI REST API server**
- ✅ **Multi-LLM support** (9 providers: Anthropic, OpenAI, Gemini, Fireworks, Groq, HuggingFace, Cohere, OpenRouter, Local Ollama/vLLM)
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
# Minimal configuration for standalone
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

Standalone uses the following defaults:

```python
# Storage (no external dependencies)
database_url = "sqlite:///./data/faultmaven.db"
user_storage_type = "inmemory"
case_storage_type = "database"
# Sessions: FakeRedis (in-process, no external server needed)
# Vectors: ChromaDB PersistentClient (local, no external server needed)
vector_storage_type = "chromadb"

# Production features (disabled by default)
opik_enabled = False
prometheus_enabled = False
tracing_enabled = False
metrics_enabled = False
protection_enabled = False
sanitize_pii = False
```

### Running the Server

```bash
# Docker (recommended)
./faultmaven.sh start

# Or as a local process (development)
pip install -e ".[dev]"
./scripts/faultmaven-dev.sh start

# Or with uvicorn directly
uvicorn faultmaven.main:app --host 0.0.0.0 --port 8090
```

### API Access

Once running, access the API at:

- **API Base URL**: `http://localhost:8090`
- **API Documentation**: `http://localhost:8090/docs` (Swagger UI)
- **Health Check**: `http://localhost:8090/health`

---

## Scaling Beyond Standalone (Cloud)

Standalone runs on **fixed simple defaults** (SQLite, in-process FakeRedis, embedded ChromaDB, local filesystem) and is **single-user**. Production-grade, multi-tenant infrastructure (PostgreSQL, Redis, object storage) is provided by the **Cloud** deployment architecture.

Cloud is cloud-native: the same core engine plus the composed proprietary modules (billing, usage metering, hosted IAM/admin), configured through **Kubernetes ConfigMaps and Secrets** (not a `.env` file) and operated either as FaultMaven-hosted SaaS or self-hosted as a private cloud. The data-tier wiring (PostgreSQL, Redis, S3/Azure Blob), observability (Opik, Prometheus), and PII redaction (Presidio) live in the cloud config surface.

- **Managed SaaS:** [cloud.faultmaven.ai](https://cloud.faultmaven.ai) — no installation required.
- **Self-hosted Cloud (private cloud):** see the Kubernetes manifests and Helm charts in the **faultmaven-enterprise-infra** repository.

The settings that back this infrastructure still exist in the codebase (see [Configuration](#configuration)); they are simply **not part of the Standalone `.env` surface** and are supplied by the cloud config contract.

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

#### Cloud-Surface Settings (configured in Cloud, not the Standalone template)

These settings exist in the codebase but are **not** part of the Standalone `.env` surface — they are supplied by the cloud config contract (Kubernetes ConfigMaps/Secrets). On Standalone they stay at their off defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPIK_ENABLED` | `false` | Enable Opik LLM tracing |
| `PROMETHEUS_ENABLED` | `false` | Enable Prometheus metrics |
| `TRACING_ENABLED` | `false` | Enable distributed tracing |
| `METRICS_ENABLED` | `false` | Enable metrics collection |
| `PROTECTION_ENABLED` | `false` | Enable PII protection |
| `SANITIZE_PII` | `false` | Enable PII sanitization |

#### Redis Configuration (Cloud)

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_HOST` | — | Redis host (Cloud; set via ConfigMap/Secret) |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | — | Redis password (set via Secret) |

#### Security Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `JWT_ALGORITHM` | `RS256` | JWT signing algorithm |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token expiration |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token expiration |

---

## Troubleshooting

### Common Issues

#### Issue: `SQLite database locked`

**Cause**: More than one process is opening the Standalone SQLite database. Standalone is single-user / single-process by design.

**Solution**:
```bash
# Ensure only one FaultMaven instance is running against ./data
ps aux | grep faultmaven
```

> For PostgreSQL, Redis, PII redaction (Presidio), and Opik/Prometheus, see [Scaling Beyond Standalone (Cloud)](#scaling-beyond-standalone-cloud) — these are configured on the Cloud deployment surface.

### Verification Checklist

After installation, verify your setup:

```bash
# 1. Check service status (Docker)
./faultmaven.sh status

# 2. Check the health endpoint
curl http://localhost:8090/health

# 3. Check readiness
curl http://localhost:8090/readiness

# 4. Open the API docs
curl http://localhost:8090/docs
```

> Running as a local process instead of Docker? Use `./scripts/faultmaven-dev.sh health`.

### Getting Help

- **GitHub Issues**: [https://github.com/FaultMaven/faultmaven/issues](https://github.com/FaultMaven/faultmaven/issues)
- **Documentation**: [https://github.com/FaultMaven/faultmaven/blob/main/README.md](https://github.com/FaultMaven/faultmaven/blob/main/README.md)
- **Community**: [Discussions](https://github.com/FaultMaven/faultmaven/discussions)

---

## Additional Resources

- [Architecture Overview](../architecture/architecture-overview.md)
- [API Reference](../reference/api/)
- [Configuration Reference](../reference/configuration/)
- [Quick Start](quickstart.md)
- [Local Development Setup](local-setup.md)
