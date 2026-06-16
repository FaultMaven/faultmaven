# FaultMaven

[![License](https://img.shields.io/badge/License-FSL--1.1--ALv2-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com/)

**AI Troubleshooting Copilot for Modern Engineering.**
Fair-source, self-hostable, and optimized for cloud scale.

FaultMaven is an AI-powered troubleshooting copilot. It correlates live telemetry with runbooks, documentation, and past fixes to deliver contextual AI-driven incident investigation — answers grounded in your actual system, not generic guesses. Resolve incidents faster with a copilot that understands both your stack and your organization.

Traditional observability tools tell you **what** broke. Generic LLMs guess **why**, but can't see your infrastructure. FaultMaven bridges this gap. Where predictive AIOps platforms act like actuaries — forecasting next quarter's outage probabilities from historical patterns — FaultMaven is the ER surgeon for systems already on the table.

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
  - Cloud: OpenAI, Anthropic, Fireworks AI, Google Gemini, Groq, Cohere, HuggingFace, OpenRouter
  - Local: Ollama (no API key required)

### Step 1: Start the Stack

```bash
# Clone the repository
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven

# Configure your LLM provider
cp .env.example .env
# Edit .env: Set OPENAI_API_KEY, ANTHROPIC_API_KEY, or configure Ollama

# Start API + Dashboard (pulls pre-built images from Docker Hub)
# This AUTOMATICALLY creates the database and a default admin user.
./faultmaven.sh start
# Or: docker compose up -d
```

**What happens:**
1. Docker pulls the images (one-time, ~5.4 GB for the API — it bundles the
   embedding model and a starter Knowledge Base, so the stack runs fully
   offline with no model download at runtime) and starts the services.
2. On first start (~1 minute) the API initializes the database, runs
   migrations, loads the bundled embedding model, and seeds 59 starter
   troubleshooting runbooks into the Knowledge Base. Restarts are faster — the
   database and vectors persist under `./data`.
3. A default admin user is created: `admin` / `admin@local.faultmaven`

> **First run is slower** because of the one-time image pull. After that,
> startup is ~1 minute and needs no internet access for the model or KB.

### Step 2: Log In

1. Open **http://localhost:3333**
2. Select **Dev Login**
3. Enter username: `admin`

That's it! You are ready to go.

### Optional: Create Additional Users

If you need more accounts, you can create them via CLI:

```bash
./faultmaven.sh create-user
```

### Step 3: Install the Copilot Extension

1. Download `faultmaven-copilot.zip` from [Releases](https://github.com/FaultMaven/faultmaven-copilot/releases)
2. Extract the archive
3. **Chrome/Edge:** Open `chrome://extensions` → Enable "Developer mode" → "Load unpacked" → Select `.output/chrome-mv3/`
4. **Firefox:** Open `about:debugging#/runtime/this-firefox` → "Load Temporary Add-on" → Select any file in `.output/firefox-mv3/`
5. Click the extension icon → Settings → Set API URL to `http://localhost:8090`

### Step 4: Configure LLM Provider

Once logged in, go to **LLM Settings** in the Dashboard sidebar to verify your provider is connected:

1. Check that your configured provider shows a **Connected** status
2. Click **Test Connection** to verify the API key works
3. You can change your primary provider or update API keys directly from the Dashboard — changes take effect immediately without restarting

> **Note:** LLM settings configured through the Dashboard are persisted in the database and take precedence over `.env` values. The `.env` file provides the initial configuration on first startup.

### Step 5: Start Troubleshooting

1. Open the **Dashboard** at http://localhost:3333 to upload runbooks to your Knowledge Base
2. Navigate to any observability tool (AWS Console, Datadog, Grafana)
3. Click the **Copilot** extension icon and start troubleshooting

### Docker Management Commands

Convenient scripts for managing the Docker-based stack:

```bash
# Main CLI for Docker-based deployment
./faultmaven.sh start              # Start services
./faultmaven.sh start --demo       # Start with demo data
./faultmaven.sh health             # Check service health
./faultmaven.sh logs               # View all logs
./faultmaven.sh logs api           # View specific service logs
./faultmaven.sh restart            # Restart all services
./faultmaven.sh stop               # Stop all services
./faultmaven.sh clean              # Remove containers (PROTECTS ./data)
./faultmaven.sh clean --wipe-data  # Remove containers AND delete all data
```

### Access Points

| Component | URL | Description |
|-----------|-----|-------------|
| Dashboard | http://localhost:3333 | Web UI for KB management, case history, LLM settings |
| API | http://localhost:8090 | REST API |
| API Docs | http://localhost:8090/docs | Interactive OpenAPI documentation |

### Alternative: Local Development Setup

For contributors or debugging, you can run the components as local processes instead of Docker.

**1. Start the API Backend:**

```bash
# Start API as local process
./scripts/faultmaven-dev.sh start

# Verify it's running
./scripts/faultmaven-dev.sh health
```

The API will be available at `http://localhost:8090`

**2. Start the Dashboard (separate process):**

The dashboard is in a separate repository. To run it locally:

```bash
# Clone the dashboard repository (if not already cloned)
git clone https://github.com/FaultMaven/faultmaven-dashboard.git
cd faultmaven-dashboard

# Install dependencies
npm install
# Or: pnpm install

# Configure API endpoint (optional - defaults to http://localhost:8090)
cp .env.example .env
# Edit .env if needed: VITE_API_URL=http://localhost:8090

# Start the development server
npm run dev
# Or: pnpm dev
```

The dashboard will be available at `http://localhost:5173` (Vite dev server) and will connect to the API at `http://localhost:8090`.

> **Note:** Make sure the API backend is running before starting the dashboard. The dashboard requires the API to be available at `http://localhost:8090`.

### Local Development Management Commands

Convenient scripts for managing the local development environment:

```bash
# For contributors developing FaultMaven
./scripts/faultmaven-dev.sh start   # Start API as local process
./scripts/faultmaven-dev.sh stop    # Stop the API
./scripts/faultmaven-dev.sh restart # Restart the API
./scripts/faultmaven-dev.sh health  # Run comprehensive health checks
./scripts/faultmaven-dev.sh logs    # Stream application logs
./scripts/faultmaven-dev.sh test    # Run tests (delegates to scripts/tests.py)
```

For more detailed setup instructions, see [Development Setup](docs/development/local-setup.md).

---

## Why FaultMaven?

FaultMaven is not just a chatbot wrapper; it is a context-aware investigation engine designed to get smarter with every incident. It is a copilot, not an autopilot — every step it suggests is yours to run, review, or reject. You stay in command of the keyboard.

### 1. Deep Context Awareness

Generic chatbots can't access your logs, configs, or deployments. FaultMaven correlates your **full stack context**—connecting errors with recent changes, configuration drift, and system state to find root causes faster.

**Example:** A Kubernetes pod is crashlooping. ChatGPT gives generic advice. FaultMaven analyzes your pod logs alongside deployment YAMLs and recent changes—then identifies that a ConfigMap update 2 hours ago introduced an invalid environment variable.

### 2. Zero Context-Switching

Stop copying errors between browser tabs. The **[FaultMaven Copilot](https://github.com/FaultMaven/faultmaven-copilot)** browser extension overlays AI troubleshooting directly onto your existing tools—AWS Console, Datadog, Grafana, or localhost. No backend agents, webhooks, or complex integrations required. Because the Copilot lives in your browser, it never asks for production API keys, root credentials, or read/write access to your live systems — you supply the context it sees, nothing more.

**How it works:** FaultMaven lives in your browser, not your cluster. As you view logs in CloudWatch, traces in Datadog, or pods in the Kubernetes dashboard, the Copilot extension captures the relevant context and correlates it with your Knowledge Base in real-time.

### 3. The Knowledge Flywheel

Most troubleshooting knowledge is lost once the incident is closed. FaultMaven turns that lost data into a growing asset through a "Seed & Grow" lifecycle:

- **Seed with Runbooks:** You don't start from zero. Pre-load your existing runbooks and documentation into the Knowledge Base so the AI knows your standard operating procedures from Day 1.
- **Grow with Incidents:** As you troubleshoot, the AI learns. When a case is resolved, FaultMaven extracts the successful steps and root cause to automatically update the knowledge base.
- **Result:** Your static documentation becomes a dynamic, self-improving engine. The solution to today's incident becomes the automated fix for tomorrow's.

### 4. Opportunistic Investigation Framework

FaultMaven uses an **opportunistic investigation** approach where the agent completes tasks based on data availability rather than following rigid sequential phases.

**Core Principles:**
- **Milestone-based progress** - Track what's completed, not what phase you're in. Complete multiple milestones in one turn when data allows.
- **Linear stage flow** - Both investigation paths (MITIGATION_FIRST, ROOT_CAUSE) follow 1→2→3→4 progression.
- **Mitigation as a tool** - Quick fixes available during early stages without disrupting the investigation flow.

**Case Lifecycle:** `INQUIRY` → `INVESTIGATING` → `RESOLVED` / `CLOSED`

**Key Components:**
- **MilestoneEngine** - Tracks verification, investigation, and resolution milestones opportunistically.
- **WorkingConclusionGenerator** - Continuous progress tracking to prevent circular reasoning.
- **MemoryManager** - Hot/warm/cold memory tiers to maintain context across long investigations.

### 5. Flexible Multi-LLM Support

FaultMaven is architected to be model-agnostic, giving you the freedom to choose the best intelligence for your specific needs and budget.

It supports a wide variety of backends, including:

- **Frontier Models:** Connect to major cloud providers (OpenAI, Anthropic, Google) for complex reasoning and multimodal analysis.
- **Inference Providers:** Use high-speed inference engines (Groq, Fireworks AI) for low-latency responsiveness.
- **Local & Self-Hosted:** Run entirely on your own hardware using local runners (Ollama, vLLM) for maximum data privacy and zero API costs.
- **Model Routing:** Built-in fallback logic ensures high availability by automatically switching providers if the primary API becomes unavailable.

---

## Deployment Modes

FaultMaven runs on a single, deployment-agnostic **Core**. The same engine powers two deployment architectures — what differs is the infrastructure footprint and who operates it, not the investigation engine itself. The Core is source-available (FSL-1.1-ALv2) in every mode.

### 1. Standalone (Self-Hosted)

**Best for:** Individuals, contributors, and air-gapped environments.

Standalone is a monolithic, single-instance deployment you run on your own hardware — directly as a server process or inside a Docker container. It ships with fixed, simple defaults (SQLite, in-process FakeRedis, embedded ChromaDB) so getting started is "pick an LLM provider, paste a key, go."

- **Self-Hosted:** You own and operate the stack — the container, the database (SQLite), and the configuration via a single `.env` file.
- **Build Your Own Knowledge:** Ingest your own runbooks and build a **Personal Knowledge Base** tailored exactly to your specific needs.
- **Offline Capable:** Can run entirely offline (with local LLMs like Ollama), making it ideal for high-restriction environments.

Follow the [Quick Start](#quick-start) guide above to get up and running.

### 2. Cloud (FaultMaven-Hosted SaaS)

**Best for:** Engineering teams and enterprises requiring collaboration and institutional scale.

Cloud is a cloud-native deployment architecture — orchestrated, elastic, and scalable — operated for you as a managed SaaS. It provides immediate value out of the box with managed infrastructure and data.

> **"Cloud" describes the architecture, not the location.** The same cloud-native deployment can run in public cloud (AWS/GCP/Azure) or on-prem as a private cloud.

- **Managed Kubernetes Infrastructure:** We run the Core on a high-availability Kubernetes control plane, handling auto-scaling, encryption, and zero-downtime updates for you.
- **Team Knowledge Sharing:** Multi-tenancy adds the **team** knowledge scope — share personal runbooks across your org. (Both deployments ship with the same global runbook pack.)
- **Collaborative 3-Tier Knowledge:** The cloud platform activates the full 3-scope model:
  1. **Global:** System-wide runbooks shipped to every deployment.
  2. **Team:** Runbooks shared across your organization (Institutional Memory).
  3. **Personal:** Your private runbooks and drafts.

### Comparison

| Feature | Standalone (Self-Hosted) | Cloud (FaultMaven-Hosted) |
| ------- | ------------------------ | ------------------------- |
| **Configuration** | Single-User / Docker | Multi-User / Managed K8s |
| **Knowledge Base Start State** | Ships with the global runbook pack | Ships with the global runbook pack |
| **Knowledge Scopes** | Global + Personal | **Global + Team + Personal** (team sharing) |
| **LLM Configuration** | Dashboard-managed (single provider, BYOK) | Dashboard-managed (multi-provider fallback chain, hot-reload) |
| **Case Management** | Full (with archive) | Full (with archive + org-wide view) |
| **User Management** | Not applicable (single user) | Full CRUD, invite, roles |
| **Infrastructure** | Fixed defaults (SQLite, FakeRedis, embedded ChromaDB) | Fully Managed (Postgres, Redis, S3) |
| **Security** | Local Auth | SSO (SAML/OIDC), SOC 2 Ready |
| **Session Persistence** | **Ephemeral** (FakeRedis, resets on restart) | **Persistent** (Redis, saved across sessions) |
| **Access** | `http://localhost:3333` (localhost only) | `https://app.faultmaven.ai` |

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
|                      FaultMaven API (8090)                       |
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
| `agent` | Investigation orchestration, AI tools |
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

FaultMaven has two configuration layers:

| Layer | What it configures | How to change |
| ----- | ------------------- | ------------- |
| **Environment (`.env`)** | Infrastructure: database, auth mode, Redis, CORS, ports | Edit `.env` file, restart the server |
| **Dashboard (DB-backed)** | LLM settings: provider, API keys, fallback chain | Change via Dashboard UI, takes effect immediately |

On first startup, LLM settings are loaded from `.env`. Once you modify them through the Dashboard, the database becomes the source of truth for LLM configuration. Infrastructure settings always come from `.env`.

### Infrastructure Settings (`.env`)

| Category | Variables | Description |
|----------|-----------|-------------|
| Database | `DATABASE_URL` | SQLite (default) or PostgreSQL |
| Sessions | `REDIS_HOST`, `REDIS_URL` | FakeRedis (default) or real Redis |
| Vectors | `VECTOR_STORAGE_TYPE` | `inmemory` (default) or `chromadb` |
| Security | `JWT_SECRET_KEY`, `CORS_ALLOW_ORIGINS` | Auth and CORS settings |

See [.env.example](.env.example) for all options with detailed comments.

### LLM Provider Setup

Set your initial LLM provider in `.env`:

```env
# Select primary provider
CHAT_PROVIDER=openai  # openai, anthropic, fireworks, gemini, groq, cohere, huggingface, openrouter, local

# Add API key for your provider
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
FIREWORKS_API_KEY=fw-...
GROQ_API_KEY=gsk-...
COHERE_API_KEY=xxx
HUGGINGFACE_API_KEY=hf_...
OPENROUTER_API_KEY=sk-or-...

# Optional: Separate providers for specific tasks
MULTIMODAL_PROVIDER=gemini       # Visual evidence processing
SYNTHESIS_PROVIDER=openai        # RAG document queries
```

After the first startup, you can manage all LLM settings (provider, API keys, fallback chain) through the **Dashboard > LLM Settings** page. Changes are hot-reloaded — no server restart required.

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
| **LLM/AI** | OpenAI, Anthropic, Fireworks, Gemini, Groq, Cohere, HuggingFace, OpenRouter |
| **Database** | SQLAlchemy 2.0, SQLite (standalone), PostgreSQL (cloud), Alembic |
| **Vector DB** | ChromaDB, sentence-transformers |
| **Cache** | Redis (cloud), FakeRedis (standalone — full API parity) |
| **Auth** | JWT (PyJWT), bcrypt, RBAC |
| **Observability** | Opik tracing, Prometheus metrics, structlog |
| **Testing** | pytest, pytest-asyncio, pytest-cov |

---

## User Interfaces

FaultMaven provides two frontend interfaces. For setup instructions, see [Quick Start](#quick-start) or [Local Development Setup](docs/development/local-setup.md).

### Dashboard

**[FaultMaven Dashboard](https://github.com/FaultMaven/faultmaven-dashboard)** - Web application for proactive knowledge management:

- **Knowledge Base Management**: Upload runbooks, search indexed documents, archive and restore items
- **Case Management**: View, search, filter, and annotate past investigations. Archive resolved cases for long-term reference
- **LLM Settings**: View provider status, test connections, change primary provider, and update API keys — all changes take effect immediately without restarting

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

FSL-1.1-ALv2 (Functional Source License) — source-available, converting to Apache-2.0 two years after each release. See [LICENSE](LICENSE) for details.

---

## Support

- **Issues:** [GitHub Issues](https://github.com/FaultMaven/faultmaven/issues)
- **Discussions:** [GitHub Discussions](https://github.com/FaultMaven/faultmaven/discussions)
- **Email:** support@faultmaven.ai
