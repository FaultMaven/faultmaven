# FaultMaven

**The AI-Powered Troubleshooting and Knowledge Base Platform**

*Empower software and operations engineers to diagnose incidents faster with privacy-first AI and a local knowledge base.*

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Docker Hub](https://img.shields.io/badge/docker-ready-blue.svg)](https://hub.docker.com/u/faultmaven)
[![GitHub](https://img.shields.io/badge/github-FaultMaven-blue.svg)](https://github.com/FaultMaven)

---

## Overview

FaultMaven is an open-source AI troubleshooting assistant that helps Developers, SREs, and DevOps engineers diagnose complex issues and capture distinct troubleshooting context. It combines privacy-first AI analysis with a local knowledge base to reduce personal toil and accelerate resolution.

This repository contains the core microservices that power the FaultMaven platform.

**Key Features:**
- 🤖 **AI-Powered Troubleshooting** - Get intelligent help diagnosing complex technical issues
- 💬 **Interactive Chat Interface** - Talk through problems naturally via browser extension
- 📚 **Smart Knowledge Base** - Store and search runbooks, documentation, and past solutions
- 🔐 **Privacy-First** - All sensitive data sanitized before AI processing
- 🐳 **Easy Self-Hosting** - One command deployment with Docker Compose
- 🔄 **Learns From Experience** - Captures solutions and builds institutional knowledge
- 🌐 **Multiple LLM Support** - Works with OpenAI, Anthropic Claude, Fireworks AI, and more

---

## 🏗️ Deployment Options

FaultMaven is available in two ways:

### 1. Self-Hosted (Open Source)
**Run on your own infrastructure.**

Deploy locally using Docker. All data stays on your machine.

**⚡ 2-Minute Setup:**
```bash
git clone https://github.com/FaultMaven/faultmaven-deploy.git
cd faultmaven-deploy
echo "OPENAI_API_KEY=sk-..." > .env
docker-compose up -d
```

**What's Included:**
- ✅ Complete AI troubleshooting agent (LangGraph with all 8 milestones)
- ✅ Browser extension (works with all deployment options)
- ✅ Knowledge base with semantic search (ChromaDB)
- ✅ Case tracking and investigation history
- ✅ Support for logs, traces, metrics, profiles, config, code, text, visual data
- ✅ 3-tier RAG system (Personal KB + Global KB + Case Working Memory)
- ✅ SQLite database (zero configuration, portable)
- ✅ Background job processing (Celery + Redis)
- ✅ Complete microservices architecture (9 Docker containers)

**Best For:**
- Developers and SREs learning AI troubleshooting
- Experimenting with LLMs, RAG, and agentic workflows
- Privacy-first or air-gapped environments
- Contributing to open source

**License:** Apache 2.0 (free forever)

**Status:** ✅ Available now | **[Quick Start Guide →](#quick-start)**

---

### 2. Managed SaaS
**Zero setup, managed infrastructure.**

**What's Available Now:**
- ✅ AI troubleshooting agent
- ✅ Browser extension (works with all deployment options)
- ✅ Knowledge base with semantic search
- ✅ Case tracking
- ✅ Managed infrastructure (PostgreSQL, Redis, S3)
- ✅ Web dashboard

**Current Limitations:**
- Single user workspace (no case sharing or team knowledge bases)
- Limited storage and active cases

**Note:** Core features are functional and available for use. We're actively improving them based on user feedback.

**Status:** ✅ Available now | **[Sign Up →](https://faultmaven.ai/signup)**

---

## Quick Start

### Prerequisites
- Docker and Docker Compose
- 8GB RAM minimum
- Ports 8000 (API Gateway), 3000 (Dashboard) available
- **LLM API Key** (OpenAI, Anthropic, Fireworks, etc.)

### Step 1: Clone Deployment Repository

```bash
git clone https://github.com/FaultMaven/faultmaven-deploy.git
cd faultmaven-deploy
```

### Step 2: Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and add your LLM provider credentials:

```bash
# Required: Choose ONE provider
OPENAI_API_KEY=sk-...
# OR
ANTHROPIC_API_KEY=sk-ant-...
# OR
FIREWORKS_API_KEY=fw-...

# Optional: Customize
JWT_SECRET=your-random-secret-here
API_PORT=8000
DASHBOARD_PORT=3000
```

### Step 3: Deploy All Services

```bash
docker compose up -d
```

All images will be automatically pulled from Docker Hub. No manual builds required!

### Step 4: Access FaultMaven

- **API Gateway:** http://localhost:8000
- **Dashboard:** http://localhost:3000
- **API Docs:** http://localhost:8000/docs
- **Capabilities Endpoint:** http://localhost:8000/v1/meta/capabilities

### Step 5: Install Browser Extension

1. Download `faultmaven-copilot` from [Chrome Web Store](https://chrome.google.com/webstore) / [Firefox Add-ons](https://addons.mozilla.org/)
2. Configure extension settings:
   - API Endpoint: `http://localhost:8000`
3. Start troubleshooting!

### Step 6: Verify Health

```bash
curl http://localhost:8000/health
curl http://localhost:8000/v1/meta/capabilities
```

**Next Steps:** Upload documents to the knowledge base via the dashboard at http://localhost:3000

---

## Architecture

FaultMaven uses a **microservices architecture** with the following components:

```
┌─────────────────────────────────────────────────────────────┐
│                  Browser Extension (Chat)                    │
│              + Dashboard (KB Management)                     │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTPS
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                      API Gateway (8090)                      │
│              Pluggable Auth + Capabilities API               │
└─────┬────────┬──────────┬──────────┬──────────┬──────┬──────┘
      │        │          │          │          │      │
      ▼        ▼          ▼          ▼          ▼      ▼
┌──────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────┐ ┌──────┐
│   Auth   │ │Session │ │  Case  │ │Evidence│ │ KB │ │Agent │
│ Service  │ │Service │ │Service │ │Service │ │Svc │ │ Svc  │
│  (8001)  │ │ (8002) │ │ (8003) │ │ (8005) │ │8004│ │ 8006 │
└────┬─────┘ └───┬────┘ └───┬────┘ └───┬────┘ └──┬─┘ └───┬──┘
     │           │          │          │         │       │
     │           │          │          │         │       │
     ▼           ▼          ▼          ▼         ▼       ▼
┌─────────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌────────┐ ┌─────┐
│ SQLite  │  │Redis │  │SQLite│  │ File │  │ChromaDB│ │ LLM │
│  Auth   │  │+Celery  │Milest│  │Upload│  │3-Tier │ │Multi│
└─────────┘  └──┬───┘  └──────┘  └──────┘  │RAG    │ │Prov │
                │                           └────────┘ └─────┘
                ▼
         ┌─────────────┐
         │ Job Worker  │
         │(Celery+Beat)│
         └─────────────┘
```

### Core Services (Backend)

| Service | Port | What It Does | Repository |
|---------|------|--------------|------------|
| **API Gateway** | 8090 | Main entry point for all requests | [fm-api-gateway](https://github.com/FaultMaven/fm-api-gateway) |
| **Auth Service** | 8001 | User authentication | [fm-auth-service](https://github.com/FaultMaven/fm-auth-service) |
| **Session Service** | 8002 | Manages user sessions | [fm-session-service](https://github.com/FaultMaven/fm-session-service) |
| **Case Service** | 8003 | Tracks troubleshooting investigations | [fm-case-service](https://github.com/FaultMaven/fm-case-service) |
| **Knowledge Service** | 8004 | Stores and searches documentation | [fm-knowledge-service](https://github.com/FaultMaven/fm-knowledge-service) |
| **Evidence Service** | 8005 | Handles file uploads (logs, screenshots) | [fm-evidence-service](https://github.com/FaultMaven/fm-evidence-service) |
| **Agent Service** | 8006 | Powers AI troubleshooting conversations | [fm-agent-service](https://github.com/FaultMaven/fm-agent-service) |
| **Job Worker** | - | Processes background tasks | [fm-job-worker](https://github.com/FaultMaven/fm-job-worker) |

### User Interfaces

| Interface | Port | Description | Repository | Docker Image |
|-----------|------|-------------|------------|--------------|
| **Browser Extension** | N/A | Chat interface for troubleshooting | [faultmaven-copilot](https://github.com/FaultMaven/faultmaven-copilot) | Chrome/Firefox Store |
| **Dashboard** | 3000 | KB management UI (Vite + React) | [faultmaven-dashboard](https://github.com/FaultMaven/faultmaven-dashboard) | `faultmaven/faultmaven-dashboard` |

### Deployment

| Repository | Purpose |
|------------|---------|
| [faultmaven-deploy](https://github.com/FaultMaven/faultmaven-deploy) | Docker Compose deployment for self-hosting |
| [faultmaven-website](https://github.com/FaultMaven/faultmaven-website) | Documentation & marketing site |

### Pluggable Authentication

FaultMaven uses a **capabilities-driven architecture** where the same codebase supports multiple deployment modes:

**Self-Hosted Mode:**
```yaml
# Uses lightweight fm-auth-service
auth:
  provider: fm-auth-service
  jwt_secret: your-secret
```

**Enterprise Mode:**
```yaml
# Uses Supabase for SSO
auth:
  provider: supabase
  project_url: https://your-project.supabase.co
```

The API Gateway detects the auth provider and adapts accordingly. The browser extension calls `/v1/meta/capabilities` to discover which features are available and adjusts the UI dynamically.

### Capabilities API

**Endpoint:** `GET /v1/meta/capabilities`

This endpoint allows clients (extension, dashboard) to discover available features at runtime.

**Self-Hosted Response:**
```json
{
  "deploymentMode": "self-hosted",
  "version": "1.0.0",
  "features": {
    "chat": true,
    "knowledgeBase": true,
    "cases": true,
    "organizations": false,
    "teams": false,
    "sso": false
  },
  "auth": {
    "type": "jwt",
    "loginUrl": "http://localhost:8000/auth/login"
  },
  "dashboardUrl": "http://localhost:3000"
}
```

**Enterprise SaaS Response:**
```json
{
  "deploymentMode": "enterprise",
  "version": "1.0.0",
  "features": {
    "chat": true,
    "knowledgeBase": true,
    "cases": true,
    "organizations": true,
    "teams": true,
    "sso": true
  },
  "auth": {
    "type": "supabase",
    "loginUrl": "https://api.faultmaven.ai/auth/login"
  },
  "dashboardUrl": "https://app.faultmaven.ai"
}
```

**How Clients Use It:**
- Extension calls this endpoint on startup
- Enables/disables UI features based on response
- Single codebase adapts to deployment mode

### Split UI Architecture

FaultMaven uses a **two-interface model** for optimal workflows:

**Browser Extension (faultmaven-copilot):**
- **Purpose:** Real-time troubleshooting chat
- **Features:** AI-powered conversation, evidence capture, case creation
- **Benefit:** Always available, context-aware

**Web Dashboard (faultmaven-dashboard):**
- **Purpose:** Knowledge base management
- **Features:** Upload documents, search KB, review cases, configure settings
- **Benefit:** Focused management tasks, advanced configurations

**Why split?** Better UX than cramming everything into the extension. Each interface is optimized for its specific use case.

---

## Technology Stack

**Backend:**
- **Framework:** FastAPI (Python async web framework)
- **AI:** Advanced language models for intelligent conversations
- **Database:** SQLite (self-hosted) or PostgreSQL (enterprise)
- **Cache:** Redis for sessions and background jobs
- **Search:** ChromaDB vector database for semantic knowledge search
- **LLM Support:** OpenAI GPT-4, Anthropic Claude, Fireworks AI, and more

**Frontend:**
- React 19+ with TypeScript
- WXT Framework (modern WebExtension toolkit)
- Tailwind CSS (utility-first styling)
- Next.js (dashboard)

**Infrastructure:**
- Docker & Docker Compose
- Kubernetes + Helm (enterprise)
- Apache 2.0 License

---

## Use Cases

### 1. Production Incident Investigation
Track symptoms, hypothesis, evidence, and resolution in structured cases.

### 2. Knowledge Base Building
Store runbooks, documentation, and past incident learnings in searchable knowledge base.

### 3. AI-Assisted Investigation
The AI agent helps you work through problems systematically:
- Ask questions to understand the issue
- Analyze logs, metrics, and other evidence
- Suggest potential root causes
- Recommend solutions based on similar past cases
- Help document the resolution for future reference

### 4. Team Collaboration (Enterprise Only)
Share troubleshooting sessions and build institutional knowledge across your organization.

---

## Development

### Repository Structure

FaultMaven is organized into **9 public repositories** in the `FaultMaven` GitHub organization:

#### Backend Services
| Repository | What It Does |
|------------|--------------|
| [fm-api-gateway](https://github.com/FaultMaven/fm-api-gateway) | Routes all API requests |
| [fm-auth-service](https://github.com/FaultMaven/fm-auth-service) | Handles user authentication |
| [fm-case-service](https://github.com/FaultMaven/fm-case-service) | Manages troubleshooting cases |
| [fm-session-service](https://github.com/FaultMaven/fm-session-service) | Tracks user sessions |
| [fm-knowledge-service](https://github.com/FaultMaven/fm-knowledge-service) | Powers knowledge base search |
| [fm-evidence-service](https://github.com/FaultMaven/fm-evidence-service) | Stores uploaded files |
| [fm-agent-service](https://github.com/FaultMaven/fm-agent-service) | Runs AI troubleshooting conversations |
| [fm-job-worker](https://github.com/FaultMaven/fm-job-worker) | Processes background tasks |
| [fm-core-lib](https://github.com/FaultMaven/fm-core-lib) | Shared code library |

#### User Interfaces
| Repository | What It Does |
|------------|--------------|
| [faultmaven-copilot](https://github.com/FaultMaven/faultmaven-copilot) | Browser extension for chatting with the AI |
| [faultmaven-dashboard](https://github.com/FaultMaven/faultmaven-dashboard) | Web UI for managing knowledge base |

#### Deployment
| Repository | What It Does |
|------------|--------------|
| [faultmaven-deploy](https://github.com/FaultMaven/faultmaven-deploy) | Docker Compose setup for easy deployment |
| [faultmaven-website](https://github.com/FaultMaven/faultmaven-website) | Documentation and project website |

### Contributing

We welcome contributions! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details on our development process and how to submit pull requests.

**Quick Start:**

1. Fork the repository you want to contribute to
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Test locally with `docker-compose` (see [faultmaven-deploy](https://github.com/FaultMaven/faultmaven-deploy))
5. Commit your changes (`git commit -m 'Add amazing feature'`)
6. Push to your fork (`git push origin feature/amazing-feature`)
7. Open a Pull Request

All contributions are welcome - from bug fixes to new features!

---

## Roadmap

### ✅ What's Available Now
**Status:** Available | **License:** Apache 2.0

**Current Features:**
- ✅ AI-powered troubleshooting assistant
- ✅ Browser extension (interactive chat interface)
- ✅ Knowledge base with semantic search
- ✅ Case tracking and investigation history
- ✅ Multiple LLM provider support (OpenAI, Anthropic, Fireworks, and more)
- ✅ Web dashboard for knowledge management
- ✅ Docker deployment
- ✅ Privacy-first data handling

---

### 🔨 Development Pipeline

**Advanced Features:**
- Team collaboration (case sharing, shared knowledge bases)
- Enterprise authentication (SSO, SAML, MFA, RBAC)
- Multi-tenancy (organizations, teams, workspaces)
- Advanced analytics and dashboards
- Increased storage and case limits

**Platform Enhancements:**
- Local LLM support (Ollama, LM Studio)
- Improved search and retrieval accuracy
- Kubernetes deployment (K8s manifests and Helm charts)
- Integrations (Slack, PagerDuty, ServiceNow, webhooks)
- Mobile-responsive dashboard

These features are being built alongside ongoing improvements to the core platform. User feedback directly influences development priorities.

**Want to help?** We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md)

---

## Documentation

- [Deployment Guide](https://github.com/FaultMaven/faultmaven-deploy) - Complete Docker Compose setup for self-hosting
- [Architecture Overview](./docs/ARCHITECTURE.md) - System design and service responsibilities
- [API Documentation](./docs/API.md) - Complete REST API reference with examples
- [Development Setup](./docs/DEVELOPMENT.md) - Local development guide for contributors

---

## Docker Images

All services are published to Docker Hub:
- [faultmaven/fm-auth-service](https://hub.docker.com/r/faultmaven/fm-auth-service)
- [faultmaven/fm-session-service](https://hub.docker.com/r/faultmaven/fm-session-service)
- [faultmaven/fm-case-service](https://hub.docker.com/r/faultmaven/fm-case-service)
- [faultmaven/fm-knowledge-service](https://hub.docker.com/r/faultmaven/fm-knowledge-service)
- [faultmaven/fm-evidence-service](https://hub.docker.com/r/faultmaven/fm-evidence-service)
- [faultmaven/fm-agent-service](https://hub.docker.com/r/faultmaven/fm-agent-service)
- [faultmaven/fm-job-worker](https://hub.docker.com/r/faultmaven/fm-job-worker)
- [faultmaven/fm-api-gateway](https://hub.docker.com/r/faultmaven/fm-api-gateway)

---

## Support

- **Issues**: [GitHub Issues](https://github.com/FaultMaven/FaultMaven/issues)
- **Discussions**: [GitHub Discussions](https://github.com/FaultMaven/FaultMaven/discussions)
- **Website**: *Coming soon*

---

## License

**Apache 2.0 License** - See [LICENSE](LICENSE) for full details.

### Why Apache 2.0?

- ✅ **Enterprise-friendly:** Use commercially without restrictions
- ✅ **Patent grant:** Protection against patent litigation
- ✅ **Permissive:** Fork, modify, commercialize freely
- ✅ **Compatible:** Integrate with proprietary systems
- ✅ **Trusted:** Same license as Kubernetes, TensorFlow, Android

**TL;DR:** You can use FaultMaven for anything, including building commercial products on top of it. No strings attached.

---

## Acknowledgments

Built with:
- [FastAPI](https://fastapi.tiangolo.com/)
- [ChromaDB](https://www.trychroma.com/)
- [Redis](https://redis.io/)
- [React](https://react.dev/)
- [WXT](https://wxt.dev/)

---

**FaultMaven** - Making troubleshooting faster, smarter, and more collaborative.
