# FaultMaven

**AI-Powered Troubleshooting Copilot for DevOps and SRE Teams**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Docker Hub](https://img.shields.io/badge/docker-ready-blue.svg)](https://hub.docker.com/u/faultmaven)
[![GitHub](https://img.shields.io/badge/github-FaultMaven-blue.svg)](https://github.com/FaultMaven)

---

## Overview

FaultMaven is an open-source troubleshooting copilot that helps DevOps and SRE teams diagnose, document, and resolve production incidents faster. It combines AI-powered analysis with structured troubleshooting workflows to reduce MTTR and build institutional knowledge.

**Key Features:**
- 🤖 **AI-Powered Root Cause Analysis** - Structured 5-phase SRE troubleshooting doctrine
- 📚 **Knowledge Base with RAG** - Learn from past incidents and documentation
- 🔐 **Privacy-First** - All sensitive data sanitized before AI processing
- 🐳 **Zero-Configuration Deployment** - Docker Compose for single-user setups
- 🌐 **Browser Extension** - Troubleshoot directly from your browser
- 🔄 **Session Management** - Track investigation progress across sessions

---

## 🏗️ Architecture Philosophy: Enterprise Superset Model

FaultMaven uses a unique **upstream/downstream** architecture where the public version is the foundation for everything:

### Public Version (This Repository) = The Foundation
- **Role:** Single source of truth for all core logic
- **Database:** SQLite (zero-config)
- **Auth:** Lightweight JWT (`fm-auth-service`)
- **Target:** Self-hosted, single-user/small teams, privacy-focused organizations
- **License:** Apache 2.0 (fully open source)
- **Registry:** Docker Hub (`faultmaven/*`)

### Enterprise SaaS (Proprietary) = The Extension
- **Role:** Extends public foundation with enterprise features
- **Database:** PostgreSQL (multi-tenant, scalable)
- **Auth:** Supabase (SSO, SAML)
- **Target:** Organizations requiring managed hosting, teams, compliance
- **License:** Proprietary
- **Registry:** GitHub Container Registry (GHCR)

### How They Relate

```
┌─────────────────────────────────────────┐
│   Public Repos (GitHub)                 │
│   - fm-case-service                     │
│   - fm-knowledge-service                │
│   - fm-auth-service                     │
│   └──→ Docker Hub: faultmaven/*         │
└─────────────────┬───────────────────────┘
                  │ Inherits via FROM
                  ▼
┌─────────────────────────────────────────┐
│   Private Repos (Internal)              │
│   - fm-case-service-enterprise          │
│   - fm-auth-service-enterprise          │
│   └──→ GHCR: ghcr.io/faultmaven/*       │
└─────────────────────────────────────────┘
```

**Key Principle:**
> All bug fixes and core improvements are made in PUBLIC repos first. Enterprise automatically inherits them when Docker images rebuild.

**For Users:**
- ✅ Self-hosted version is **fully functional**, not a demo
- ✅ Clear upgrade path to enterprise when you need teams/SSO
- ✅ No vendor lock-in (Apache 2.0 license)

---

## Quick Start

### Prerequisites
- Docker and Docker Compose
- 8GB RAM minimum
- Ports 8000 (API Gateway), 3000 (Dashboard) available
- **LLM API Key** (OpenAI, Anthropic, or Fireworks)

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

1. Download `faultmaven-copilot` from Chrome Web Store / Firefox Add-ons *(coming soon)*
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
│                      API Gateway (8000)                      │
│              Pluggable Auth + Capabilities API               │
└─────┬────────┬──────────┬──────────┬──────────┬─────────────┘
      │        │          │          │          │
      ▼        ▼          ▼          ▼          ▼
┌──────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────────┐
│   Auth   │ │Session │ │  Case  │ │Evidence│ │ Knowledge  │
│ Service  │ │Service │ │Service │ │Service │ │  Service   │
│  (8001)  │ │ (8002) │ │ (8003) │ │ (8005) │ │   (8004)   │
└────┬─────┘ └───┬────┘ └───┬────┘ └───┬────┘ └─────┬──────┘
     │           │          │          │            │
     │           │          │          │            │
     ▼           ▼          ▼          ▼            ▼
┌─────────┐  ┌──────┐  ┌──────┐  ┌──────┐    ┌──────────┐
│ SQLite  │  │Redis │  │SQLite│  │ File │    │ChromaDB  │
│  Auth   │  │Session  │ Cases│  │Upload│    │Vector DB │
└─────────┘  └──────┘  └──────┘  └──────┘    └──────────┘
```

### Core Services (Backend)

| Service | Port | Description | Repository | Docker Image |
|---------|------|-------------|------------|--------------|
| **API Gateway** | 8000 | Unified entry point with pluggable auth | [fm-api-gateway](https://github.com/FaultMaven/fm-api-gateway) | `faultmaven/fm-api-gateway` |
| **Auth Service** | 8001 | Lightweight JWT authentication | [fm-auth-service](https://github.com/FaultMaven/fm-auth-service) | `faultmaven/fm-auth-service` |
| **Session Service** | 8002 | Redis-backed session management | [fm-session-service](https://github.com/FaultMaven/fm-session-service) | `faultmaven/fm-session-service` |
| **Case Service** | 8003 | Troubleshooting case tracking (SQLite) | [fm-case-service](https://github.com/FaultMaven/fm-case-service) | `faultmaven/fm-case-service` |
| **Knowledge Service** | 8004 | RAG-powered knowledge base (ChromaDB) | [fm-knowledge-service](https://github.com/FaultMaven/fm-knowledge-service) | `faultmaven/fm-knowledge-service` |
| **Evidence Service** | 8005 | File upload and attachment handling | [fm-evidence-service](https://github.com/FaultMaven/fm-evidence-service) | `faultmaven/fm-evidence-service` |

### User Interfaces

| Interface | Port | Description | Repository | Docker Image |
|-----------|------|-------------|------------|--------------|
| **Browser Extension** | N/A | Chat interface for troubleshooting | [faultmaven-copilot](https://github.com/FaultMaven/faultmaven-copilot) | Chrome/Firefox Store |
| **Dashboard** | 3000 | KB management UI (Next.js) | [faultmaven-dashboard](https://github.com/FaultMaven/faultmaven-dashboard) | `faultmaven/faultmaven-dashboard` |

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
- FastAPI (async Python web framework)
- SQLAlchemy 2.0 (async ORM)
- SQLite (zero-config database)
- Redis (session storage)
- ChromaDB (vector database for RAG)
- BGE-M3 embeddings (multilingual text embeddings)

**Frontend:**
- React 19+ with TypeScript
- WXT Framework (modern WebExtension toolkit)
- Tailwind CSS (utility-first styling)

**Infrastructure:**
- Docker & Docker Compose
- Apache 2.0 License

---

## Use Cases

### 1. Production Incident Investigation
Track symptoms, hypothesis, evidence, and resolution in structured cases.

### 2. Knowledge Base Building
Store runbooks, documentation, and past incident learnings in searchable knowledge base.

### 3. Root Cause Analysis
AI-powered analysis following SRE best practices:
- Define blast radius
- Establish timeline
- Formulate hypothesis
- Validate with evidence
- Propose solution

### 4. Team Collaboration
Share troubleshooting sessions and build institutional knowledge.

---

## Development

### Repository Structure

FaultMaven is organized into **9 public repositories** in the `FaultMaven` GitHub organization:

#### Core Services (Backend)
| Repository | Docker Image | Description |
|------------|--------------|-------------|
| [fm-api-gateway](https://github.com/FaultMaven/fm-api-gateway) | `faultmaven/fm-api-gateway` | Unified entry point with pluggable auth |
| [fm-auth-service](https://github.com/FaultMaven/fm-auth-service) | `faultmaven/fm-auth-service` | Lightweight JWT authentication |
| [fm-case-service](https://github.com/FaultMaven/fm-case-service) | `faultmaven/fm-case-service` | Case management (SQLite) |
| [fm-session-service](https://github.com/FaultMaven/fm-session-service) | `faultmaven/fm-session-service` | Session tracking (Redis) |
| [fm-knowledge-service](https://github.com/FaultMaven/fm-knowledge-service) | `faultmaven/fm-knowledge-service` | RAG knowledge base (ChromaDB) |
| [fm-evidence-service](https://github.com/FaultMaven/fm-evidence-service) | `faultmaven/fm-evidence-service` | File uploads |

#### User Interfaces
| Repository | Docker Image | Description |
|------------|--------------|-------------|
| [faultmaven-copilot](https://github.com/FaultMaven/faultmaven-copilot) | N/A (Extension Store) | Browser extension (chat interface) |
| [faultmaven-dashboard](https://github.com/FaultMaven/faultmaven-dashboard) | `faultmaven/faultmaven-dashboard` | KB management UI (Next.js) |

#### Deployment & Docs
| Repository | Purpose |
|------------|---------|
| [faultmaven-deploy](https://github.com/FaultMaven/faultmaven-deploy) | Docker Compose deployment for self-hosting |
| [faultmaven-website](https://github.com/FaultMaven/faultmaven-website) | Documentation & marketing site |

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

**Important:** See [CONTRIBUTING.md](CONTRIBUTING.md) for our "Cleanse & Refactor" workflow and what belongs in public vs enterprise versions

---

## Roadmap

### ✅ Public v1.0 (Current) - Production-Ready Foundation
**Status:** Available Now | **License:** Apache 2.0

This is the **complete, self-hosted version** for individuals and teams:
- ✅ AI-powered troubleshooting (multi-LLM: OpenAI, Anthropic, Fireworks)
- ✅ Knowledge base with RAG (ChromaDB)
- ✅ Case & session management
- ✅ Browser extension (Chrome/Firefox)
- ✅ Knowledge base dashboard (Next.js)
- ✅ JWT authentication
- ✅ Docker Compose deployment (SQLite + Redis)

**Target Users:**
- Self-hosters who value privacy
- Small teams (< 10 users)
- Organizations with data residency requirements
- Developers wanting full control

---

### 🔮 Public Roadmap (Community-Driven)
**Status:** Planned | **License:** Apache 2.0

Future enhancements to the **open-source** version:
- [ ] Local LLM support (Ollama, LM Studio integration)
- [ ] Enhanced RAG performance (hybrid search, reranking)
- [ ] Kubernetes deployment manifests
- [ ] Webhook integrations (Slack, PagerDuty notifications)
- [ ] Mobile-responsive dashboard
- [ ] Multi-language support
- [ ] Plugin system for custom integrations

**Contribute:** We welcome PRs for these features! See [CONTRIBUTING.md](CONTRIBUTING.md)

---

### 🏢 Enterprise SaaS (Separate Proprietary Product)
**Status:** Private Beta | **NOT Open Source**

For organizations requiring managed hosting and advanced collaboration:
- Multi-tenancy (organizations, teams, RBAC)
- Enterprise SSO/SAML
- PostgreSQL at scale
- Advanced analytics & compliance (SOC2, HIPAA)
- 24/7 support & SLAs
- Managed infrastructure

**Learn More:** Visit [faultmaven.ai/enterprise](https://faultmaven.ai/enterprise) *(coming soon)*

**Migration Path:** Self-hosted users can upgrade to enterprise seamlessly via data export/import tools

---

## Documentation

- [Deployment Guide](https://github.com/FaultMaven/faultmaven-deploy)
- [Architecture Overview](./docs/ARCHITECTURE.md) *(coming soon)*
- [API Documentation](./docs/API.md) *(coming soon)*
- [Development Setup](./docs/DEVELOPMENT.md) *(coming soon)*

---

## Docker Images

All services are published to Docker Hub:
- [faultmaven/fm-auth-service](https://hub.docker.com/r/faultmaven/fm-auth-service)
- [faultmaven/fm-session-service](https://hub.docker.com/r/faultmaven/fm-session-service)
- [faultmaven/fm-case-service](https://hub.docker.com/r/faultmaven/fm-case-service)
- [faultmaven/fm-knowledge-service](https://hub.docker.com/r/faultmaven/fm-knowledge-service)
- [faultmaven/fm-evidence-service](https://hub.docker.com/r/faultmaven/fm-evidence-service)
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
