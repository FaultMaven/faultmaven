# FaultMaven

**The AI-Powered Troubleshooting and Knowledge Base Platform**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-public%20beta-orange.svg)]()
[![Docker Hub](https://img.shields.io/badge/docker-ready-blue.svg)](https://hub.docker.com/u/faultmaven)

---

## 🎉 Public Beta Now Open: Help us Build FaultMaven

Try it free (self-hosted or Enterprise SaaS), break things, and tell us what you think. Your feedback shapes the product.

| **Enterprise SaaS (Free Beta)** | **Self-Hosted (Free Forever)** |
|:---|:---|
| [Start Your Managed Trial →](https://faultmaven.ai/signup) | [See Quick Start Guide →](#quick-start) |

[![Feedback Welcome](https://img.shields.io/badge/feedback-welcome-brightgreen.svg)](https://github.com/FaultMaven/FaultMaven/discussions)

---

## 🧪 Public Beta Program

**FaultMaven is being built WITH the community, not just FOR it.** We're opening both deployment options for public testing to validate features, gather feedback, and refine the product together.

### The Value Exchange

<table>
<tr>
<td width="50%" valign="top">

**🎁 What You Get**
- Free access to all features during the Public Beta
- Direct influence on roadmap and feature priorities
- Recognition as a founding contributor
- Direct line to the engineering team
- Early access to new capabilities as we build them
- Self-hosted version stays free forever (Apache 2.0)

</td>
<td width="50%" valign="top">

**🔍 What We Need**
- Bug reports and edge cases
- Feature requests and real-world use cases
- Usage patterns and workflow insights
- Honest feedback on what works (and what doesn't)
- Help validating team collaboration features
- Input on the long-term sustainability and roadmap

</td>
</tr>
</table>

**How to participate:**
- 🏢 **[Test Enterprise SaaS →](https://faultmaven.ai/signup)** — Zero-setup team collaboration testbed
- 🔓 **[Deploy Self-Hosted →](#quick-start)** — Free forever testbed for developers
- 💬 **[Give Feedback →](https://github.com/FaultMaven/FaultMaven/discussions)** — Shape the roadmap
- 🐛 **[Report Bugs →](https://github.com/FaultMaven/FaultMaven/issues)** — Help us improve

---

## Overview

FaultMaven is an AI-powered troubleshooting and knowledge base platform that helps software and operations engineers diagnose incidents faster. It combines intelligent AI analysis with a searchable knowledge base to capture institutional knowledge, reduce toil, and accelerate resolution.

**Key Capabilities:**
- 🤖 **AI-Powered Troubleshooting** — Intelligent help diagnosing complex technical issues
- 💬 **Interactive Chat Interface** — Talk through problems naturally via browser extension
- 📚 **Smart Knowledge Base** — Store and search runbooks, documentation, and past solutions
- 🔐 **Privacy-First** — All sensitive data sanitized before AI processing
- 🔄 **Learns From Experience** — Captures solutions and builds institutional knowledge
- 🌐 **Multiple LLM Support** — Works with OpenAI, Anthropic Claude, Fireworks AI, and more

---

## 🏗️ Deployment Options

Choose how you want to test FaultMaven:

| | **Self-Hosted (OSS)** | **Enterprise SaaS** |
|---|:---:|:---:|
| **Status** | ✅ Free beta | ✅ Free beta |
| **Setup Time** | 2 minutes (Docker) | 60 seconds (sign up) |
| **Best For** | Individual developers, tinkerers | Teams testing collaboration |
| **Team Collaboration** | ❌ Single user only | ✅ Unlimited team members |
| **Knowledge Sharing** | ❌ Local only | ✅ Organization-wide |
| **Authentication** | JWT (basic) | SSO, SAML, MFA |
| **Infrastructure** | You manage (Docker) | Fully managed |
| **Storage** | SQLite, local files | PostgreSQL, S3 |
| **Support** | Community (GitHub) | Direct engineering access |
| **Future** | **Always free** (Apache 2.0) | **Team plans** available after beta |
| | **[Quick Start →](#quick-start)** | **[Sign Up →](https://faultmaven.ai/signup)** |

---

## 🏢 Enterprise SaaS (Free Beta)

**Zero-Setup Team Collaboration Testbed**

Test team collaboration features with zero infrastructure. We're validating whether these features truly solve team troubleshooting challenges.

### What You're Testing

This is the managed platform designed for teams:
- 👥 **Team collaboration** — Share cases and knowledge bases across your organization
- 🔐 **Enterprise authentication** — SSO (Google, Okta, Azure AD), SAML, MFA
- 🏢 **Multi-tenancy** — Organizations, teams, role-based access control
- ☁️ **Managed infrastructure** — PostgreSQL, Redis Cluster, S3 storage (auto-scaling)
- 📊 **Advanced analytics** — Dashboards, trend analysis, pattern detection
- 📞 **Beta support** — Direct line to engineering team via in-app chat

### How Beta Works

- ✅ **Free for everyone** — No credit card required, no hidden costs during beta
- 🧪 **Help us validate** — Does this solve real team troubleshooting problems?
- 🏆 **Beta participants** — Recognition as Founding Contributors + special access to new features
- 💾 **Your data** — Export anytime, we'll never hold it hostage
- 🗳️ **Your input matters** — Directly shape the Enterprise feature set and roadmap

### Perfect For Testing

- Engineering teams (2+ people) exploring AI troubleshooting for incidents
- Organizations evaluating collaborative knowledge management
- Teams needing SSO/RBAC/compliance features
- Anyone who wants zero DevOps overhead

### Get Started

**[Start Enterprise Beta →](https://faultmaven.ai/signup)**

*No credit card. No risk. Export your data anytime.*

---

## 🔓 Self-Hosted (Open Source Beta)

**Free Forever Testbed for Developers & Tinkerers**

Deploy locally in 2 minutes. Test the complete AI troubleshooting platform on your own infrastructure. Open source (Apache 2.0) and will always be free.

### What You're Testing

This is the full-featured self-hosted version:
- ✅ **Complete AI agent** — Full LangGraph agent with all 8 milestones
- ✅ **All 8 data types** — Logs, traces, profiles, metrics, config, code, text, visual
- ✅ **3-tier RAG system** — Personal KB + Global KB + Case Working Memory
- ✅ **SQLite database** — Zero configuration, single file, portable
- ✅ **Local file storage** — All data stays on your machine
- ✅ **ChromaDB vector search** — Semantic knowledge base retrieval
- ✅ **Background job processing** — Celery + Redis for async tasks
- ✅ **9 Docker containers** — Complete microservices architecture
- ✅ **Multiple LLM providers** — OpenAI, Anthropic, Fireworks, and more

### Beta Terms

- ✅ **Always free** — Apache 2.0 license (no future pricing ever)
- 🔄 **Active development** — Expect frequent updates and improvements
- 🐛 **Beta quality** — Core features work well, but rough edges and bugs possible
- 🔧 **Breaking changes** — Possible during beta (we'll document and communicate)
- 💬 **Community support** — GitHub Discussions & Issues

### Perfect For Testing

- Individual developers and SREs learning AI troubleshooting
- Privacy-first or air-gapped environments
- Studying RAG architectures and agentic workflows
- Contributing to open source development
- Evaluating before Enterprise deployment

### Get Started

**[Quick Start Guide →](#quick-start)** | **[View on GitHub →](https://github.com/FaultMaven/faultmaven-deploy)**

---

## ⚠️ Beta Expectations

**What "public beta" means for FaultMaven:**

✅ **Ready for real use:**
- Core functionality is solid and tested
- Safe to use for real (non-critical) troubleshooting workflows
- AI agent, knowledge base, and case tracking all work well
- Docker deployment is stable

⚠️ **Still improving:**
- May have bugs, rough edges, and missing features
- Breaking changes possible (we'll communicate in advance)
- Not recommended for business-critical production use yet
- Browser extension in active development (4-6 weeks)

🤝 **Community-driven:**
- Your feedback directly shapes feature priorities
- We'll be transparent about bugs, roadmap, and timelines
- Beta participants directly influence the Enterprise feature set and roadmap
- Recognition for contributors and active testers

---

## 🚀 Quick Start

Deploy the self-hosted version in 2 minutes.

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

### Step 4: Access and Start Using FaultMaven

**Dashboard:** [http://localhost:3000](http://localhost:3000)
- Access the knowledge base manager
- Upload documents, runbooks, and troubleshooting guides
- Create and manage your first troubleshooting case

**API Gateway:** [http://localhost:8000](http://localhost:8000)
- API Docs: [http://localhost:8000/docs](http://localhost:8000/docs)
- Health Check: [http://localhost:8000/health](http://localhost:8000/health)
- Capabilities: [http://localhost:8000/v1/meta/capabilities](http://localhost:8000/v1/meta/capabilities)

### Step 5: Verify Everything Works

```bash
curl http://localhost:8000/health
curl http://localhost:8000/v1/meta/capabilities
```

### Step 6: Install Browser Extension (Coming Soon)

*Browser extension expected in 4-6 weeks*

Once available:
1. Download `faultmaven-copilot` from Chrome Web Store / Firefox Add-ons
2. Configure extension settings:
   - API Endpoint: `http://localhost:8000`
3. Start troubleshooting with the AI copilot!

---

## 💬 Feedback & Support

**We need your feedback to build the right product.**

### Found a Bug?
**[File an issue →](https://github.com/FaultMaven/FaultMaven/issues)**

Please include:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your deployment (self-hosted vs Enterprise)

### Have a Feature Idea?
**[Start a discussion →](https://github.com/FaultMaven/FaultMaven/discussions)**

Tell us:
- What problem you're trying to solve
- How you'd like it to work
- Why it matters to your workflow

### General Feedback
**[Share your experience →](https://github.com/FaultMaven/FaultMaven/discussions)**

We want to hear:
- What's working well
- What's frustrating
- What's missing
- How you're using it

### Success Stories
Solved a real incident with FaultMaven? **[Tell us your story →](https://github.com/FaultMaven/FaultMaven/discussions)**

### Enterprise Beta Support
If you're testing Enterprise SaaS:
- 📞 In-app chat support
- 📧 Email: beta@faultmaven.ai
- 🎯 Priority response for beta participants

---

## 🗺️ Roadmap to v1.0

### Current Status: Public Beta

**What's Working Now:**
- ✅ AI troubleshooting agent (LangGraph with 8 milestones)
- ✅ Knowledge base with semantic search (ChromaDB)
- ✅ Case tracking and investigation history
- ✅ Docker Compose deployment
- ✅ Multiple LLM providers (OpenAI, Anthropic, Fireworks, and more)
- ✅ Team collaboration (Enterprise only)
- ✅ SSO authentication (Enterprise only)
- ✅ Web dashboard for KB management

**In Active Development:**
- 🔨 Browser extension (4-6 weeks)
- 🔨 Mobile-responsive dashboard
- 🔨 Advanced analytics and dashboards
- 🔨 Kubernetes deployment manifests

**Planned for v1.0:**
- [ ] Local LLM support (Ollama, LM Studio)
- [ ] Slack and PagerDuty integrations
- [ ] Webhook notifications
- [ ] Production-ready documentation
- [ ] SOC 2 compliance (Enterprise)
- [ ] Enhanced team collaboration features

**Timeline:** v1.0 production release expected Q2 2025 (subject to beta feedback)

**Your input shapes the roadmap.** Vote on features and suggest priorities in [GitHub Discussions](https://github.com/FaultMaven/FaultMaven/discussions).

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

| Interface | Port | Description | Repository |
|-----------|------|-------------|------------|
| **Browser Extension** | N/A | Chat interface for troubleshooting (in development) | [faultmaven-copilot](https://github.com/FaultMaven/faultmaven-copilot) |
| **Dashboard** | 3000 | KB management UI (Vite + React) | [faultmaven-dashboard](https://github.com/FaultMaven/faultmaven-dashboard) |

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

---

## Technology Stack

**Backend:**
- **Framework:** FastAPI (Python async web framework)
- **AI:** LangGraph for agentic workflows
- **Database:** SQLite (self-hosted) or PostgreSQL (Enterprise)
- **Cache:** Redis for sessions and background jobs
- **Search:** ChromaDB vector database for semantic knowledge search
- **LLM Support:** OpenAI GPT-4, Anthropic Claude, Fireworks AI, and more

**Frontend:**
- React 19+ with TypeScript
- WXT Framework (modern WebExtension toolkit)
- Tailwind CSS (utility-first styling)
- Vite (dashboard build tool)

**Infrastructure:**
- Docker & Docker Compose
- Kubernetes + Helm (Enterprise)
- Apache 2.0 License (Open Source)

---

## Use Cases

### 1. AI-Assisted Incident Investigation
The AI agent helps you work through problems systematically:
- Ask questions to understand the issue
- Analyze logs, metrics, and other evidence
- Suggest potential root causes
- Recommend solutions based on similar past cases
- Help document the resolution for future reference

### 2. Knowledge Base Building
Store runbooks, documentation, and past incident learnings in a searchable knowledge base with semantic search powered by ChromaDB.

### 3. Structured Case Tracking
Track symptoms, hypothesis, evidence, and resolution in structured cases that build institutional knowledge over time.

### 4. Team Collaboration (Enterprise Only)
Share troubleshooting sessions, collaborate on incidents in real-time, and build institutional knowledge across your organization.

---

## Development

### Repository Structure

FaultMaven is organized into **multiple public repositories** in the `FaultMaven` GitHub organization:

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
- **Enterprise Beta**: beta@faultmaven.ai
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

**TL;DR:** You can use FaultMaven for anything, including building commercial products on top of it. The self-hosted version will always be free and open source. No strings attached.

---

## Acknowledgments

Built with:
- [FastAPI](https://fastapi.tiangolo.com/)
- [LangGraph](https://github.com/langchain-ai/langgraph)
- [ChromaDB](https://www.trychroma.com/)
- [Redis](https://redis.io/)
- [React](https://react.dev/)
- [WXT](https://wxt.dev/)

---

**FaultMaven** - Making troubleshooting faster, smarter, and more collaborative.
