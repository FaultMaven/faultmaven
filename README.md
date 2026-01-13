# FaultMaven

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com/)

**Open-Source AI Troubleshooting Copilot for Modern Engineering**

FaultMaven correlates your live telemetry with your runbooks, docs, and past fixes. It delivers answers grounded in your actual system—not generic guesses. Resolve incidents faster with an AI copilot that understands both your stack and your organization.

Traditional observability tools tell you **what** broke. Generic LLMs guess **why**, but can't see your infrastructure. FaultMaven bridges this gap.

---

## System Components

FaultMaven consists of three components that work together:

| Component | Repository | Purpose |
|-----------|------------|---------|
| **FaultMaven API** | This repo | Backend server: investigation engine, knowledge base, AI orchestration |
| **FaultMaven Dashboard** | [faultmaven-dashboard](https://github.com/FaultMaven/faultmaven-dashboard) | Web UI: knowledge base management, case history, settings |
| **FaultMaven Copilot** | [faultmaven-copilot](https://github.com/FaultMaven/faultmaven-copilot) | Browser extension: in-context troubleshooting overlay |

**Typical usage:** The Copilot extension is your primary interface during incidents. The Dashboard manages your knowledge base and reviews past cases. Both connect to the API backend.

---

## Quick Start

Get the full FaultMaven stack running in under 5 minutes.

### Prerequisites

- **Docker** and **Docker Compose**
- **LLM Provider** (one of):
  - Cloud: OpenAI, Anthropic, Fireworks AI, Google Gemini, Groq
  - Local: Ollama (no API key required)

### Step 1: Start the Stack

```bash
# Clone the repository
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven

# Configure your LLM provider
cp .env.example .env
# Edit .env: Set OPENAI_API_KEY, ANTHROPIC_API_KEY, or configure Ollama

# Start API + Dashboard
docker compose up -d
```

### Step 2: Install the Copilot Extension

1. Download `faultmaven-copilot.zip` from [Releases](https://github.com/FaultMaven/faultmaven-copilot/releases)
2. Extract the archive
3. **Chrome/Edge:** Open `chrome://extensions` → Enable "Developer mode" → "Load unpacked" → Select `.output/chrome-mv3/`
4. **Firefox:** Open `about:debugging#/runtime/this-firefox` → "Load Temporary Add-on" → Select any file in `.output/firefox-mv3/`
5. Click the extension icon → Settings → Set API URL to `http://localhost:8000`

### Step 3: Start Troubleshooting

1. Open the **Dashboard** at http://localhost:3000 to upload runbooks to your Knowledge Base
2. Navigate to any observability tool (AWS Console, Datadog, Grafana)
3. Click the **Copilot** extension icon and start troubleshooting

### Access Points

| Component | URL | Description |
|-----------|-----|-------------|
| Dashboard | http://localhost:3000 | Web UI for KB management, case history |
| API | http://localhost:8000 | REST API |
| API Docs | http://localhost:8000/docs | Interactive OpenAPI documentation |

### Alternative: Local Development Setup

For contributors or debugging, you can run the components as local processes instead of Docker. See [Development Setup](docs/development/local-setup.md).

---

## Why FaultMaven?

FaultMaven is not just a chatbot wrapper; it is a context-aware investigation engine designed to get smarter with every incident.

### 1. Zero-Agent Context Capture

Generic chatbots can't see your infrastructure. FaultMaven's browser extension captures context directly from your screen—reading logs, configs, and dashboards as you view them. No backend agents, webhooks, or complex API integrations required.

**How it works:** FaultMaven lives in your browser, not your cluster. As you view logs in CloudWatch, traces in Datadog, or pods in the Kubernetes dashboard, the Copilot extension captures the relevant text and correlates it with your stored Knowledge Base to provide instant, grounded answers.

**Example:** A Kubernetes pod is crashlooping. ChatGPT gives generic advice. You open the pod logs in your browser—FaultMaven reads them alongside your runbooks and recent case history, then identifies that a ConfigMap change 2 hours ago introduced an invalid environment variable.

### 2. The Knowledge Flywheel

Most troubleshooting knowledge is lost once the incident is closed. FaultMaven turns that lost data into a growing asset through a "Seed & Grow" lifecycle:

- **Seed with Runbooks:** You don't start from zero. Pre-load your existing runbooks and documentation into the Knowledge Base so the AI knows your standard operating procedures from Day 1.
- **Grow with Incidents:** As you troubleshoot, the AI learns. When a case is resolved, FaultMaven extracts the successful steps and root cause to automatically update the knowledge base.
- **Result:** Your static documentation becomes a dynamic, self-improving engine. The solution to today's incident becomes the automated fix for tomorrow's.

### 3. AI-Powered Investigation Framework

FaultMaven uses a 7-phase investigation lifecycle based on the **OODA Loop** (Observe, Orient, Decide, Act) with integrated engines:

- **MemoryManager** - Hot/warm/cold memory tiers to maintain context across long investigations and reduce token usage.
- **WorkingConclusionGenerator** - Continuous progress tracking to prevent circular reasoning.
- **PhaseOrchestrator** - Intelligent phase progression with loop-back detection.
- **OODAEngine** - Adaptive investigation intensity (light/medium/full).

### 4. Flexible Multi-LLM Support

FaultMaven is architected to be model-agnostic, giving you the freedom to choose the best intelligence for your specific needs and budget.

It supports a wide variety of backends, including:

- **Frontier Models:** Connect to major cloud providers (OpenAI, Anthropic, Google) for complex reasoning and multimodal analysis.
- **Inference Providers:** Utilize high-speed inference engines (Groq, Fireworks AI) for low-latency responsiveness.
- **Local & Open Source:** Run entirely on your own hardware using local runners (Ollama, vLLM) for maximum data privacy and zero API costs.
- **Model Routing:** Built-in fallback logic ensures high availability by automatically switching providers if the primary API becomes unavailable.

---

## Editions

FaultMaven runs on a single, deployment-agnostic **Core**. This engine can be configured for different environments, giving you two distinct ways to use the platform.

### 1. FaultMaven Open Source (Local Deployment)

**Best for:** Individuals, contributors, and air-gapped environments.

In this configuration, you run the Core on your own hardware—either directly as a server process or inside a Docker container. You maintain full control over the infrastructure.

- **Self-Hosted:** You own the stack. You manage the container, the database (SQLite), and the configuration.
- **Build Your Own Knowledge:** The local environment starts with a clean slate. It includes all the capabilities to ingest your own runbooks and build a **Personal Knowledge Base** from scratch, tailored exactly to your specific needs.
- **Offline Capable:** Can run entirely offline (with local LLMs like Ollama), making it ideal for high-restriction environments.

Follow the [Quick Start](#quick-start) guide above to get up and running.

### 2. FaultMaven Cloud (SaaS)

**Best for:** Engineering teams and enterprises requiring collaboration and institutional scale.

The SaaS edition runs the Core in a distributed, production-grade configuration. It provides immediate value out of the box with managed infrastructure and data.

- **Managed Kubernetes Infrastructure:** We run the Core on a high-availability Kubernetes control plane, handling auto-scaling, encryption, and zero-downtime updates for you.
- **Pre-Built Intelligence:** Unlike the empty local state, the SaaS version comes with a **Global Knowledge Base** pre-populated with industry-standard troubleshooting guides and best practices.
- **Collaborative 3-Tier Knowledge:** The cloud platform activates the full 3-tier architecture:
  1. **Global:** Pre-built system-wide knowledge.
  2. **Team:** Shared runbooks and incident logs (Institutional Memory).
  3. **Personal:** Private notes and drafts.

### Comparison

| Feature | Open Source (Local) | Cloud (SaaS) |
|---------|---------------------|--------------|
| **Configuration** | Single-User / Docker | Multi-User / Managed K8s |
| **Knowledge Base Start State** | **Empty** (User builds it) | **Pre-Loaded** (Global KB included) |
| **Knowledge Tiers** | Personal Only | **Global + Team + Personal** |
| **Infrastructure** | User-Managed (SQLite) | Fully Managed (Postgres, S3) |
| **Security** | Local Auth | SSO (SAML/OIDC), SOC 2 Ready |
| **Access** | `localhost` | `app.faultmaven.ai` |

**Subscribe:** [https://cloud.faultmaven.ai](https://cloud.faultmaven.ai)

---

## Architecture

FaultMaven is a monolithic application with clean **Vertical Slice Architecture**. Instead of separating by technical layers (Controller, Service, Dao), we organize by Feature Modules.

```
                    Browser Extension / Dashboard
                              |
                            HTTPS
                              v
+------------------------------------------------------------------+
|                      FaultMaven API (8000)                       |
|                                                                  |
|  +------------------------------------------------------------+  |
|  |                       API Layer                            |  |
|  |   /api/v1/agent   /api/v1/cases   /api/v1/knowledge  ...   |  |
|  +------------------------------------------------------------+  |
|  |                     Service Layer                          |  |
|  |   AgentService  CaseService  KnowledgeService  AuthService |  |
|  +------------------------------------------------------------+  |
|  |                  Infrastructure Layer                      |  |
|  |   LLM Router   Persistence   Security   Observability      |  |
|  +------------------------------------------------------------+  |
+------------------------------------------------------------------+
         |                     |                     |
         v                     v                     v
    +--------+           +---------+           +----------+
    | Redis  |           |ChromaDB |           | SQLite/  |
    | (opt)  |           |(Vectors)|           | Postgres |
    +--------+           +---------+           +----------+
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
├── faultmaven/              # Main application
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
├── tests/                   # Test suite
│   ├── unit/
│   ├── integration/
│   ├── health/
│   └── performance/
├── docs/                    # Documentation
├── alembic/                 # Database migrations
├── faultmaven.sh            # CLI wrapper (start/stop/test)
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
MULTIMODAL_PROVIDER=gemini       # Visual evidence processing
SYNTHESIS_PROVIDER=openai        # RAG document queries
```

**Fallback Chain:** Primary provider -> Fireworks -> OpenAI -> Local

---

## Development

### Testing

```bash
# Run all tests
./faultmaven.sh test

# Run with coverage
./faultmaven.sh test --coverage

# Or use pytest directly
pytest tests/unit/
pytest --cov=faultmaven
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

FaultMaven provides two frontend interfaces. For setup instructions, see [Quick Start](#quick-start) or [Local Development Setup](docs/development/local-setup.md).

### Dashboard

**[FaultMaven Dashboard](https://github.com/FaultMaven/faultmaven-dashboard)** - Web application for proactive knowledge management:

- **Knowledge Base Management**: Upload runbooks, edit indexed documents, manage vectors
- **Case History**: View, search, and export past troubleshooting sessions
- **Configuration**: Manage LLM providers and system settings

### Browser Extension (Copilot)

**[FaultMaven Copilot](https://github.com/FaultMaven/faultmaven-copilot)** - Browser extension for reactive troubleshooting:

- **Context Capture**: Reads logs, stack traces, and dashboards directly from your screen
- **In-Flow Diagnostics**: Troubleshoot within AWS Console, Datadog, Grafana, and other tools
- **Knowledge Base Integration**: References your documentation in real-time
- **Session Continuity**: Maintains chat history across browser sessions

### Building from Source

For development or customization, see the respective repositories:
- [Dashboard Development](https://github.com/FaultMaven/faultmaven-dashboard#development)
- [Copilot Development](https://github.com/FaultMaven/faultmaven-copilot#development)

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
