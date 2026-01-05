# FaultMaven

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/Tests-341%20passing-brightgreen)](https://github.com/FaultMaven/faultmaven)
[![Coverage](https://img.shields.io/badge/Coverage-71%25-brightgreen)](https://github.com/FaultMaven/faultmaven)
[![Architecture](https://img.shields.io/badge/Architecture-Monolith-blue)](docs/architecture/)

**AI-Powered Troubleshooting Copilot for SRE and DevOps Teams**

FaultMaven correlates your live telemetry with your runbooks, docs, and past fixes. It delivers answers grounded in your actual system—not generic guesses. Resolve incidents faster with an AI copilot that understands both your stack and your organization.

---

## 🚀 Quick Start

Deploy FaultMaven Core locally in under 5 minutes.

### Prerequisites

- **Python 3.11+** installed
- **LLM API Key** (OpenAI, Anthropic, or other [supported providers](#4-multi-llm-support))

### Installation

```bash
# 1. Clone repository
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -e .

# 4. Configure environment
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY or ANTHROPIC_API_KEY

# 5. Start FaultMaven
python -m faultmaven
```

Visit **<http://localhost:8000>** - you're running FaultMaven!

**Access Points:**

- **API**: <http://localhost:8000> - Backend REST API
- **API Docs**: <http://localhost:8000/docs> - Interactive API documentation
- **Health Check**: <http://localhost:8000/health> - Service health status

> **Troubleshooting:** See [deploy/troubleshooting/](deploy/troubleshooting/) for common issues and solutions

---

## Why FaultMaven?

Traditional observability tools tell you **what** broke. Generic LLMs guess **why**, but can't see your infrastructure. FaultMaven bridges this gap.

### 1. Deep Context Awareness

Generic chatbots can't access your logs, configs, or deployments. FaultMaven auto-ingests your **full stack context**—correlating errors with recent changes, configuration drift, and system state.

**Example:** A Kubernetes pod is crashlooping. ChatGPT gives generic advice. FaultMaven ingests your pod logs, deployment YAML, and recent Git commits—then tells you the ConfigMap changed 2 hours ago.

### 2. Institutional Memory

Most troubleshooting knowledge dies in Slack threads. FaultMaven's **knowledge base** ensures you never solve the same problem twice:

- **User Knowledge Base:** Your personal runbooks, post-mortems, and documentation
- **Case Knowledge Base:** Context from past investigations (auto-cleanup after case closure)

### 3. AI-Powered Investigation Framework

FaultMaven uses a sophisticated **investigation framework** with integrated engines:

- ✅ **MemoryManager** - Hierarchical memory management (64% token reduction)
- ✅ **WorkingConclusionGenerator** - Continuous progress tracking
- ✅ **PhaseOrchestrator** - Intelligent phase progression with loop-back detection
- ✅ **OODAEngine** - Adaptive investigation intensity (light/medium/full)

### 4. Multi-LLM Support

FaultMaven supports **7 LLM providers** with automatic fallback:

- Fireworks AI (recommended)
- OpenAI (GPT-4, GPT-3.5)
- Anthropic (Claude 3.5)
- Google Gemini
- HuggingFace
- OpenRouter
- Local (Ollama, vLLM)

---

## Architecture

FaultMaven is built as a **monolithic application** with clean separation of concerns.

```
┌──────────────────────────────────────────────────────────┐
│              Browser Extension / Dashboard               │
└─────────────────────────┬────────────────────────────────┘
                          │ HTTPS
                          ▼
┌──────────────────────────────────────────────────────────┐
│                FaultMaven Core (8000)                     │
│                                                            │
│  ┌────────────────────────────────────────────────────┐  │
│  │                  Service Layer                     │  │
│  │   (Agent, Case, Knowledge, Evidence, Session)      │  │
│  └────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────┐  │
│  │            Infrastructure Layer                    │  │
│  │   (LLM Router, Security, Persistence, Tools)       │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
    ┌────────┐          ┌─────────┐         ┌──────────┐
    │ Redis  │          │ChromaDB │         │ SQLite/  │
    │(Cache) │          │(Vectors)│         │PostgreSQL│
    └────────┘          └─────────┘         └──────────┘
```

### Key Components

- **API Layer** - FastAPI routers with dependency injection
- **Service Layer** - Business logic orchestration (Agent, Case, Knowledge, Evidence, Session)
- **Investigation Framework** - 7-phase investigation lifecycle with AI engines
- **Infrastructure** - LLM routing, security, persistence, observability
- **Tools** - Knowledge base search, web search, log analysis

See [architecture/](docs/architecture/) for detailed architecture documentation.

---

## Features

| Feature | Description | Status |
|---------|-------------|--------|
| **Monolithic Architecture** | Single deployable unit with clean boundaries | ✅ Production |
| **Multi-LLM Support** | 7 providers with automatic fallback | ✅ Production |
| **Investigation Framework** | AI-powered investigation with memory management | ✅ Production |
| **Knowledge Base (RAG)** | Semantic search with ChromaDB | ✅ Production |
| **Session Management** | Multi-session per user with device continuity | ✅ Production |
| **Evidence Management** | File upload with metadata tracking | ✅ Production |
| **Auto-Generated API Docs** | OpenAPI specs with interactive documentation | ✅ Production |
| **Token Optimization** | 64% reduction via hierarchical memory | ✅ Production |

---

## Installation Modes

FaultMaven offers two installation modes optimized for different use cases:

### Community Edition (Zero Dependencies)

Perfect for local development, testing, and community users. **No external services required.**

```bash
pip install faultmaven
```

**Included**:

- ✅ **FastAPI REST API server**
- ✅ **Multi-LLM support** (7 providers)
- ✅ **Agentic framework** with autonomous reasoning
- ✅ **Knowledge base** (ChromaDB with RAG)
- ✅ **SQLite database** (local file storage)
- ✅ **In-memory sessions** (no external cache needed)
- ✅ **Local file storage** (no cloud dependencies)
- ✅ **All API endpoints** (full REST API)
- ✅ **JWT authentication**
- ✅ **Case management**
- ✅ **Evidence processing**

### Enterprise Edition (Full Features)

Production-ready with observability, security, and distributed infrastructure.

```bash
pip install faultmaven[enterprise]
```

**Adds**:

- ✅ **Opik tracing** - LLM call tracing and performance monitoring
- ✅ **Prometheus metrics** - Production-grade metrics export
- ✅ **PII redaction** - Presidio-powered sensitive data protection
- ✅ **Redis sessions** - Distributed session management
- ✅ **PostgreSQL support** - Production database support
- ✅ **Cloud storage** - AWS S3 and Azure Blob support
- ✅ **Advanced observability** - Detailed tracing and monitoring

See **[Installation Guide](docs/installation/INSTALLATION_GUIDE.md)** for comprehensive setup instructions.

---

## Development

### Project Structure

```
faultmaven/                  # Single repository
├── faultmaven/              # Backend application
│   ├── api/                # FastAPI routers and dependencies
│   ├── services/           # Business logic orchestration
│   │   ├── domain/         # Domain services
│   │   └── agentic/        # Investigation framework
│   ├── core/               # Core domain logic
│   │   ├── agent/          # AI reasoning engine
│   │   ├── knowledge/      # Knowledge base operations
│   │   └── processing/     # Data classification
│   ├── infrastructure/     # External integrations
│   │   ├── llm/           # Multi-provider LLM routing
│   │   ├── security/      # PII redaction
│   │   └── persistence/   # Redis, ChromaDB, databases
│   ├── tools/             # Agent tools (KB, web search)
│   ├── models/            # Data models and interfaces
│   └── main.py            # FastAPI application
├── tests/                  # Test suite (341 tests)
├── docs/                   # Documentation
├── scripts/               # Utility scripts
├── alembic/               # Database migrations
└── .env.example           # Configuration template
```

### Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=faultmaven tests/

# Current status: 341/341 tests passing (100%)
# Coverage: 71%
```

### Contributing

See [Development Setup](docs/development/setup.md) for:

- Local development setup
- Development patterns
- Testing guidelines
- Code quality standards

---

## Configuration

### Environment Variables

FaultMaven uses environment variables for configuration. Create a `.env` file from the template:

```bash
cp .env.example .env
# Edit .env and add your API keys
```

**Key configuration areas:**

- **LLM Providers** - OpenAI, Anthropic, Fireworks, Gemini, etc.
- **Database** - SQLite (dev) or PostgreSQL (production)
- **Session Management** - Timeout, cleanup intervals, memory limits
- **File Upload** - Size limits, allowed MIME types
- **Vector Search** - ChromaDB configuration

See [.env.example](.env.example) for complete configuration options with detailed comments and examples.

### LLM Provider Setup

FaultMaven supports **7 LLM providers** with automatic fallback:

```env
# Primary provider
CHAT_PROVIDER="fireworks"  # fireworks, openai, anthropic, gemini, huggingface, openrouter, local

# Provider API keys
FIREWORKS_API_KEY="fw_your_api_key"           # Fireworks AI (recommended)
OPENAI_API_KEY="sk_your_openai_key"          # OpenAI GPT models
ANTHROPIC_API_KEY="sk-ant-your_key"          # Claude 3.5 Sonnet
GEMINI_API_KEY="your_google_ai_key"          # Google Gemini
HUGGINGFACE_API_KEY="hf_your_token"          # HuggingFace models
OPENROUTER_API_KEY="sk-or-your_key"          # OpenRouter multi-provider
LOCAL_LLM_URL="http://localhost:11434"       # Local/Ollama (no API key needed)
```

**Automatic Fallback Chain**: Primary → Fireworks → OpenAI → Local (based on available API keys)

For detailed configuration and adding new providers, see: [How to Add Providers](docs/development/how-to-add-providers.md)

---

## Deployment

### Docker Deployment (Recommended)

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 2. Start with Docker Compose
docker compose up -d

# 3. Verify services
docker ps
```

Database migrations run automatically on startup. No manual initialization needed!

### Local Development

```bash
# 1. Setup Python environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -e .[dev,test]

# 3. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 4. Start FaultMaven
python -m faultmaven
```

For detailed deployment instructions, see:

- **[Deployment Guide](docs/operations/deployment.md)** - Production deployment
- **[Development Setup](docs/development/setup.md)** - Local development environment

---

## User Interfaces

FaultMaven provides two complementary interfaces:

### Browser Extension (Separate Repository)

The **[FaultMaven Copilot](https://github.com/FaultMaven/faultmaven-copilot)** browser extension for reactive troubleshooting:

- Overlay AI troubleshooting on AWS Console, Datadog, Grafana
- Context-aware conversations during incidents
- File upload and evidence collection
- Multi-session support

See the [Copilot repository](https://github.com/FaultMaven/faultmaven-copilot) for installation and development.

### Dashboard (Coming Soon)

Web-based dashboard for proactive management:

- Knowledge base management
- Case history and analytics
- Configuration and settings

---

## Performance

### Metrics

- **Token Efficiency**: 64% reduction (4,500+ → ~1,600 tokens via MemoryManager)
- **Response Times** (p95):
  - Chat endpoint: <2s
  - Knowledge search: <500ms
  - Session operations: <100ms
- **Scalability**: 100-500 req/s per process (horizontal scaling via load balancer)

### Test Coverage

- **Total Tests**: 341/341 passing (100%)
- **Code Coverage**: 71%
- **Integration Tests**: Critical paths covered

---

## Documentation

**📖 [Complete Documentation Index](docs/README.md)** - Central map of all documentation

**Essential Documents:**

- **[architecture/](docs/architecture/)** - System architecture and design
- **[Development Setup](docs/development/setup.md)** - Development setup and workflows
- **[Deployment Guide](docs/operations/deployment.md)** - Production deployment guide
- **[API Documentation](docs/api/)** - Auto-generated OpenAPI specs

**Additional Resources:**

- [Testing Strategy](docs/development/testing-strategy.md) - Testing approach
- [Security Guidelines](docs/operations/security.md) - Security guidelines
- [Troubleshooting](docs/operations/troubleshooting.md) - Common issues and solutions
- [FAQ](docs/reference/faq.md) - Frequently asked questions

See [docs/README.md](docs/README.md) for complete documentation organized by role and task.

---

## License

Apache 2.0 - See [LICENSE](LICENSE) for details.

---

## How to Contribute

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for:

- **What to work on** - Priority-ordered critical gaps and high-priority tasks
- **Development setup** - How to get started
- **Testing requirements** - Coverage targets and testing strategy
- **PR guidelines** - How to submit contributions

**Quick Links**:

- [CONTRIBUTING.md](CONTRIBUTING.md) - **Start here for contribution guidelines**
- [Development Setup](docs/development/setup.md) - Development environment setup
- [architecture/](docs/architecture/) - System architecture

---

## Contact

- **Issues**: [GitHub Issues](https://github.com/FaultMaven/faultmaven/issues)
- **Discussions**: [GitHub Discussions](https://github.com/FaultMaven/faultmaven/discussions)
- **Email**: <support@faultmaven.ai>

---

**Architecture**: Monolith (Single Repository)
**Main Application**: `faultmaven/main.py`
**Default Port**: 8000
**Database**: SQLite (dev), PostgreSQL (production)
**Status**: ✅ Production Ready
