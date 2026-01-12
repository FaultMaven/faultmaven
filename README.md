# FaultMaven

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com/)

**Open-Source AI Troubleshooting Copilot for Modern Engineering**

FaultMaven helps SREs, DevOps engineers, and developers diagnose incidents faster by correlating full-stack data with a unified knowledge base. It assists with both incident resolution and root cause analysis—human-centric, with AI doing the heavy lifting on data correlation and context retrieval.

---

## Quick Start

### Prerequisites

- **Python 3.11+**
- **LLM Provider** (one of):
  - Cloud: OpenAI, Anthropic, Fireworks AI, Google Gemini, Groq
  - Local: Ollama (no API key required)

### Installation

```bash
# Clone and setup
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)

# Start server
uvicorn faultmaven.main:app --reload --host 0.0.0.0 --port 8000
```

### Access Points

| Endpoint | URL | Description |
|----------|-----|-------------|
| API | http://localhost:8000 | REST API |
| API Docs | http://localhost:8000/docs | Interactive OpenAPI documentation |
| Health | http://localhost:8000/health | Health check endpoint |

---

## What It Does

### Full-Stack Data Correlation

FaultMaven ingests logs, configs, metrics, and deployment state, then correlates them with recent changes. You provide the data; the AI finds the connections.

### Unified Knowledge Base

Two knowledge stores work together:

- **User Knowledge Base** - Runbooks, post-mortems, internal documentation (persisted)
- **Case Knowledge Base** - Context from the current investigation (session-scoped)

Both are RAG-enabled, so the AI retrieves relevant context automatically during investigations.

### Investigation Framework

FaultMaven uses a 7-phase investigation lifecycle with integrated engines:

- **MemoryManager** - Hierarchical memory with hot/warm/cold tiers (~1,600 vs 4,500+ tokens unmanaged)
- **WorkingConclusionGenerator** - Continuous progress tracking
- **PhaseOrchestrator** - Intelligent phase progression with loop-back detection
- **OODAEngine** - Adaptive investigation intensity (light/medium/full)

### Multi-LLM Support

7 LLM providers with automatic fallback:

| Provider | Models | Use Case |
|----------|--------|----------|
| OpenAI | GPT-4o, GPT-4, GPT-3.5 | General purpose |
| Anthropic | Claude 3.5 Sonnet | Complex reasoning |
| Fireworks AI | Llama 3.1 70B | Lower cost |
| Google Gemini | Gemini 1.5 Pro | Multimodal (images) |
| Groq | Llama, Mixtral | Low latency |
| HuggingFace | Various | Open-weight models |
| Local | Ollama, vLLM | Air-gapped / self-hosted |

---

## Architecture

FaultMaven is a monolithic application with clean vertical slice architecture.

```
                    Browser Extension / Dashboard
                              |
                            HTTPS
                              v
+------------------------------------------------------------------+
|                     FaultMaven API (8000)                        |
|                                                                  |
|  +------------------------------------------------------------+  |
|  |                      API Layer                              |  |
|  |   /api/v1/agent   /api/v1/cases   /api/v1/knowledge  ...   |  |
|  +------------------------------------------------------------+  |
|  |                    Service Layer                            |  |
|  |   AgentService  CaseService  KnowledgeService  AuthService  |  |
|  +------------------------------------------------------------+  |
|  |                 Infrastructure Layer                        |  |
|  |   LLM Router   Persistence   Security   Observability       |  |
|  +------------------------------------------------------------+  |
+------------------------------------------------------------------+
         |                    |                    |
         v                    v                    v
    +--------+          +---------+          +----------+
    | Redis  |          |ChromaDB |          | SQLite/  |
    | (opt)  |          |(Vectors)|          | Postgres |
    +--------+          +---------+          +----------+
```

### Modules

| Module | Description |
|--------|-------------|
| `agent` | Investigation orchestration, AI tools, OODA framework |
| `auth` | Users, sessions, organizations, teams, RBAC |
| `case` | Investigation cases and lifecycle management |
| `evidence` | File uploads, metadata, storage adapters |
| `knowledge` | Embeddings, vector search, RAG, knowledge items |
| `report` | Report generation and recommendations |

---

## Project Structure

```
faultmaven/
├── faultmaven/              # Main application (398 Python files)
│   ├── main.py              # FastAPI entry point
│   ├── api/                 # Shared API middleware, dependencies
│   ├── modules/             # Vertical slice feature modules
│   │   ├── agent/           # Investigation + AI tools
│   │   ├── auth/            # Authentication + authorization
│   │   ├── case/            # Case management
│   │   ├── evidence/        # Evidence/file handling
│   │   ├── knowledge/       # Knowledge base + RAG
│   │   └── report/          # Reporting
│   ├── config/              # Settings (single env-read point)
│   ├── container/           # Dependency injection
│   ├── infrastructure/      # Shared adapters (LLM, DB, storage)
│   ├── core/                # Core domain logic
│   └── services/            # Service layer
├── tests/                   # Test suite (142 test files)
│   ├── unit/
│   ├── integration/
│   ├── health/
│   └── performance/
├── docs/                    # Documentation
├── alembic/                 # Database migrations
├── docker-compose.yml       # Local services
├── Dockerfile               # Container image
├── pyproject.toml           # Dependencies and tools
└── .env.example             # Configuration template
```

---

## Configuration

### Environment Variables

Create a `.env` file from the template:

```bash
cp .env.example .env
```

Key configuration areas:

| Category | Variables | Description |
|----------|-----------|-------------|
| LLM | `CHAT_PROVIDER`, `OPENAI_API_KEY`, etc. | Primary LLM provider and API keys |
| Database | `DATABASE_URL` | SQLite (default) or PostgreSQL |
| Sessions | `SESSION_STORAGE_TYPE` | `inmemory` (default) or `redis` |
| Vectors | `VECTOR_STORAGE_TYPE` | `inmemory` (default) or `chromadb` |
| Security | `JWT_SECRET_KEY`, `CORS_ALLOW_ORIGINS` | Auth and CORS settings |

See [.env.example](.env.example) for all options with detailed comments.

### LLM Provider Setup

```env
# Select primary provider
CHAT_PROVIDER=openai  # openai, anthropic, fireworks, gemini, groq, local

# Add API key for your provider
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
FIREWORKS_API_KEY=fw-...

# Optional: Separate providers for specific tasks
MULTIMODAL_PROVIDER=gemini      # Visual evidence processing
SYNTHESIS_PROVIDER=openai       # RAG document queries
```

**Fallback Chain:** Primary provider -> Fireworks -> OpenAI -> Local

---

## Deployment

### Docker (Recommended)

```bash
# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Option 1: Docker Compose (includes Redis + ChromaDB)
docker-compose up

# Option 2: Standalone container
docker build -t faultmaven:local .
docker run --rm -p 8000:8000 --env-file .env faultmaven:local
```

### Local Development

```bash
# Setup environment
python -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e .[dev,test]

# Configure
cp .env.example .env

# Start server
uvicorn faultmaven.main:app --reload --host 0.0.0.0 --port 8000
```

### Deployment Options

| Environment | Database | Sessions | Vectors | Storage |
|-------------|----------|----------|---------|---------|
| Local/Dev | SQLite | In-memory | ChromaDB | Filesystem |
| Production | PostgreSQL | Redis | ChromaDB | S3/Azure Blob |

---

## Development

### Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=faultmaven

# Run specific test categories
pytest tests/unit/
pytest tests/integration/
pytest -m "not slow"
```

### Code Quality

```bash
# Linting
ruff check .

# Formatting
black .
isort .

# Type checking
mypy faultmaven/
```

### Database Migrations

```bash
# Create migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head
```

---

## Tech Stack

| Layer | Technologies |
|-------|--------------|
| **Framework** | Python 3.11+, FastAPI, Uvicorn, AsyncIO |
| **LLM/AI** | LangGraph, LangChain, OpenAI, Anthropic, Fireworks, Gemini |
| **Database** | SQLAlchemy 2.0, SQLite (local), PostgreSQL (production), Alembic |
| **Vector DB** | ChromaDB, sentence-transformers |
| **Cache** | Redis (optional), in-memory fallback |
| **Auth** | JWT (PyJWT), bcrypt, RBAC |
| **Observability** | Opik tracing, Prometheus metrics, structlog |
| **Testing** | pytest, pytest-asyncio, pytest-cov |

---

## User Interfaces

FaultMaven provides two complementary frontend interfaces (separate repositories):

### Browser Extension

**[FaultMaven Copilot](https://github.com/FaultMaven/faultmaven-copilot)** - Browser extension for reactive troubleshooting:

- Overlay AI assistance on AWS Console, Datadog, Grafana
- Context-aware conversations during incidents
- Evidence collection and file upload

### Dashboard

**[FaultMaven Dashboard](https://github.com/FaultMaven/faultmaven-dashboard)** - Web UI for:

- Knowledge base management
- Case history and analytics
- Configuration and settings

```bash
# Run dashboard locally (separate repo)
cd faultmaven-dashboard
pnpm install
VITE_API_URL=http://localhost:8000 pnpm dev
# Open http://localhost:5173
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/README.md](docs/README.md) | Documentation index |
| [docs/architecture/](docs/architecture/) | System design and ADRs |
| [docs/guides/](docs/guides/) | How-to guides |
| [docs/development/](docs/development/) | Development standards |
| [docs/operations/](docs/operations/) | Runbooks and monitoring |

---

## Contributing

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for:

- Development setup
- Coding standards
- Testing requirements
- PR guidelines

---

## License

Apache 2.0 - See [LICENSE](LICENSE) for details.

---

## Support

- **Issues:** [GitHub Issues](https://github.com/FaultMaven/faultmaven/issues)
- **Discussions:** [GitHub Discussions](https://github.com/FaultMaven/faultmaven/discussions)
- **Email:** support@faultmaven.ai
