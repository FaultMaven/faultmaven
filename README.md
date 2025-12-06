# FaultMaven

**FaultMaven Platform The central documentation and architecture hub for the FaultMaven ecosystem.**

FaultMaven correlates your live telemetry with your runbooks, docs, and past fixes. It delivers answers grounded in your actual system—not generic guesses. Resolve incidents faster with an AI copilot that understands both your stack and your organization.

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Docker Hub](https://img.shields.io/badge/docker-ready-blue.svg)](https://hub.docker.com/u/faultmaven)
[![GitHub](https://img.shields.io/badge/github-FaultMaven-blue.svg)](https://github.com/FaultMaven)

<!-- TODO: Add screenshot or demo GIF here showing browser extension or dashboard -->

---

## Quick Start

> **Note:** This is the **central platform repository**. To deploy FaultMaven, use our [deployment repository](https://github.com/FaultMaven/faultmaven-deploy) which orchestrates all services with Docker Compose. Want to contribute to the code? See the [Microservices Table](#repositories) below to find the specific service you need.

```bash
git clone https://github.com/FaultMaven/faultmaven-deploy.git && cd faultmaven-deploy
cp .env.example .env                      # Configure your LLM provider (see below)
./faultmaven start                        # Validates env, starts all services
```

**Access Points:**
- **Dashboard:** http://localhost:3000
- **API Gateway:** http://localhost:8090

**Default Credentials:** `admin` / `changeme123`
⚠️ **Security Warning:** Change these immediately in production. See [SECURITY.md](./docs/SECURITY.md) for setup instructions.

```bash
./faultmaven status   # Check service health
./faultmaven logs     # View logs
./faultmaven stop     # Stop services (preserves data)
```

**New to FaultMaven?**
- **[Product Overview](https://faultmaven.ai/product)** — See what FaultMaven can do
- **[Use Cases](https://faultmaven.ai/use-cases)** — Real-world troubleshooting scenarios
- **[Roadmap](https://faultmaven.ai/roadmap)** — What we're building next
- **[Beta Founders Program](https://faultmaven.ai/founders)** — Get free Enterprise Cloud access

---

## Why FaultMaven?

Traditional observability tools tell you **what** broke. Generic LLMs guess **why**, but can't see your infrastructure. FaultMaven bridges this gap.

### 1. Deep Context Awareness
Generic chatbots can't access your logs, configs, or deployments. FaultMaven auto-ingests your **full stack context**—correlating errors with recent changes, configuration drift, and system state.

**Example:** A Kubernetes pod is crashlooping. ChatGPT gives generic advice. FaultMaven ingests your pod logs, deployment YAML, and recent Git commits—then tells you the ConfigMap changed 2 hours ago.

### 2. Institutional Memory
Most troubleshooting knowledge dies in Slack threads. FaultMaven's **tiered knowledge base** ensures you never solve the same problem twice:

- **Global Knowledge Base:** Pre-loaded troubleshooting patterns for common tech stacks (Kubernetes, PostgreSQL, Redis, AWS). Think of it as a curated library of runbooks and post-mortems from the community.
- **Team Knowledge Base (Enterprise):** Automatically indexes your organization's runbooks, post-mortems, and past case resolutions.
- **Personal Context:** Your local configurations, environment variables, and deployment history.

### 3. Zero Context-Switching
Stop copying errors between browser tabs. The **[FaultMaven Copilot](https://github.com/FaultMaven/faultmaven-copilot)** browser extension overlays AI troubleshooting directly onto your existing tools—AWS Console, Datadog, Grafana, or localhost.

### 4. Continuous Learning
Every resolved case becomes institutional knowledge. FaultMaven **automatically indexes your solutions**, building a searchable history of fixes specific to your architecture. The system gets smarter about your infrastructure with every problem you solve.

---

## Self-Hosted vs. Enterprise Cloud

FaultMaven is **open core**. Run it yourself for free, or let us manage it for you.

| | **Self-Hosted** | **Enterprise Cloud** |
|---|---|---|
| **Best for** | Individual engineers, air-gapped environments, total data sovereignty | Teams needing HA, shared context, zero maintenance |
| **Pricing** | Free Forever (Apache 2.0) | Managed SaaS (Private Beta) |
| **Scope** | Individual user | Organization / multi-team |
| **Infrastructure** | Self-managed Docker Compose | Managed HA Kubernetes (AWS/GCP) |
| **Data Storage** | SQLite (local disk), local ChromaDB | Managed PostgreSQL, Redis, S3 |
| **Context Scope** | Personal knowledge base only | Global KB + Team KB + shared cases |
| **Identity** | Basic auth (single user) | SSO/SAML (Okta, Azure AD, Google) |
| **Client Integrations** | Browser Extension Overlay | Browser Extension Overlay |
| **Server Integrations** | — | Slack, PagerDuty, ServiceNow |

**Self-Hosted includes:**
- All 7 core microservices (Apache 2.0 license)
- Browser extension + web dashboard
- Multi-provider LLM support (OpenAI, Anthropic, Google, Groq)
- Local LLM support (Ollama, vLLM, LocalAI)
- Knowledge base with semantic search (RAG)
- Case tracking and evidence management

**Want Enterprise Cloud?** [Join the Beta Founders Program](https://faultmaven.ai/founders) for free access during beta.

---

## Architecture

```mermaid
flowchart TB
    subgraph Clients
        EXT[Browser Extension]
        DASH[Web Dashboard]
    end

    subgraph Gateway
        GW[API Gateway :8090]
        CAP[Capabilities API]
    end

    subgraph Core Services
        AUTH[Auth :8001]
        SESS[Session :8002]
        CASE[Case :8003]
        KB[Knowledge :8004]
        EVID[Evidence :8005]
        AGENT[Agent :8006]
    end

    subgraph Data Layer
        SQL[(SQLite/PostgreSQL)]
        REDIS[(Redis)]
        CHROMA[(ChromaDB)]
        FILES[(File Storage)]
    end

    subgraph External
        LLM[LLM Providers]
    end

    EXT --> GW
    DASH --> GW
    GW --> CAP
    GW --> AUTH
    GW --> SESS
    GW --> CASE
    GW --> KB
    GW --> EVID
    GW --> AGENT

    AUTH --> SQL
    SESS --> REDIS
    CASE --> SQL
    KB --> CHROMA
    EVID --> FILES
    AGENT --> LLM
    AGENT --> KB
```

**How it works:**

1. **Browser Extension** captures context (errors, logs, stack traces) from your current page
2. **API Gateway** routes requests and handles authentication
3. **Agent Service** orchestrates AI conversations, pulling relevant context from the Knowledge Base
4. **Knowledge Service** performs semantic search across your runbooks, past cases, and the Global KB
5. **Case Service** tracks investigations and automatically links evidence to resolutions

For detailed architecture, see [ARCHITECTURE.md](./docs/ARCHITECTURE.md).

---

## LLM Configuration

FaultMaven works with your preferred AI provider. You can start **completely free** with local models.

| Provider | Models | Best For | Cost |
|----------|--------|----------|------|
| **OpenAI** | GPT-4o, GPT-4 Turbo | Best overall quality | ~$0.01-0.03 per troubleshooting session |
| **Anthropic** | Claude 3.5 Sonnet, Claude 3 Opus | Complex reasoning, long context | ~$0.02-0.04 per session |
| **Google** | Gemini Pro | Good balance of speed/quality | ~$0.005-0.01 per session |
| **Groq** | Llama 3, Mixtral | Very fast inference | Free tier available |
| **Local (Ollama)** | Llama 3, Mistral, CodeLlama | **Air-gapped, zero cost** | Free (runs on your hardware) |

### Start Without an API Key

You can use FaultMaven **completely free** with local LLMs:

```bash
# Install Ollama (one-time setup)
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull llama3        # Download model (~4GB)

# Configure FaultMaven
cat > .env <<EOF
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=llama3
EOF

./faultmaven start
```

### Use a Cloud Provider

Get an API key from your preferred provider:
- **OpenAI:** https://platform.openai.com/api-keys
- **Anthropic:** https://console.anthropic.com/
- **Google:** https://makersuite.google.com/app/apikey
- **Groq:** https://console.groq.com/

Configure in `.env`:
```bash
LLM_PROVIDER=openai          # or: anthropic, google, groq
OPENAI_API_KEY=sk-...        # Your API key
```

**Privacy & Security:**
- **Air-Gapped Deployments:** Use Ollama or vLLM for completely offline operation
- **Data Redaction:** FaultMaven can automatically scrub PII, secrets, and sensitive data before sending to LLMs (see [SECURITY.md](./docs/SECURITY.md))

---

## Repositories

The FaultMaven ecosystem is organized into multiple repositories. Each is independently useful and can be contributed to separately.

### Core Services (Python/FastAPI Microservices)

These form the backend engine. Built with Python, FastAPI, and follow a common architecture.

| Repository | Purpose | Good First Contribution |
|------------|---------|-------------------------|
| [fm-agent-service](https://github.com/FaultMaven/fm-agent-service) | AI troubleshooting orchestration | Add new LLM provider support |
| [fm-knowledge-service](https://github.com/FaultMaven/fm-knowledge-service) | Semantic search & RAG | Improve embedding models |
| [fm-case-service](https://github.com/FaultMaven/fm-case-service) | Investigation tracking | Add export formats |
| [fm-evidence-service](https://github.com/FaultMaven/fm-evidence-service) | File/log uploads | Add new file type parsers |
| [fm-auth-service](https://github.com/FaultMaven/fm-auth-service) | Authentication | Improve session management |
| [fm-session-service](https://github.com/FaultMaven/fm-session-service) | Session persistence | Add Redis clustering |
| [fm-api-gateway](https://github.com/FaultMaven/fm-api-gateway) | Request routing, capabilities API | Add rate limiting |
| [fm-job-worker](https://github.com/FaultMaven/fm-job-worker) | Background task processing | Add new job types |

### Client Applications (TypeScript/React)

User-facing applications. Great for frontend contributors.

| Repository | Purpose | Good First Contribution |
|------------|---------|-------------------------|
| [faultmaven-copilot](https://github.com/FaultMaven/faultmaven-copilot) | Browser extension (Chrome, Firefox, Edge) | Add new site integrations |
| [faultmaven-dashboard](https://github.com/FaultMaven/faultmaven-dashboard) | Web UI for knowledge management | Improve KB search UI |

### Deployment & Infrastructure

| Repository | Purpose | Good First Contribution |
|------------|---------|-------------------------|
| [faultmaven-deploy](https://github.com/FaultMaven/faultmaven-deploy) | Docker Compose orchestration | Add Kubernetes Helm charts |
| [fm-charts](https://github.com/FaultMaven/fm-charts) | Kubernetes Helm charts | Improve resource defaults |

### Shared Libraries

| Repository | Purpose | Good First Contribution |
|------------|---------|-------------------------|
| [fm-core-lib](https://github.com/FaultMaven/fm-core-lib) | Common utilities, models, clients | Add utility functions |

**Want to contribute but not sure where to start?** Check out issues tagged [`good-first-issue`](https://github.com/search?q=org%3AFaultMaven+label%3A%22good+first+issue%22+state%3Aopen) across all repos, or ask in [GitHub Discussions](https://github.com/FaultMaven/faultmaven/discussions).

---

## Contributing

We're building FaultMaven in the open and welcome all contributions—code, documentation, bug reports, feature ideas, or just feedback.

**Ways to Contribute:**

- **Code:** Pick up a `good-first-issue` or propose a new feature
- **Documentation:** Improve guides, add examples, fix typos
- **Knowledge Base:** Contribute troubleshooting patterns for the Global KB
- **Testing:** Report bugs, test edge cases, improve test coverage
- **Community:** Answer questions in Discussions, help other users

See our [Contributing Guide](https://github.com/FaultMaven/.github/blob/main/CONTRIBUTING.md) for detailed guidelines.

```bash
# Example: Contributing to a service
git clone https://github.com/YOUR_USERNAME/fm-agent-service.git
cd fm-agent-service

# Run the full stack locally for testing
cd ../faultmaven-deploy && docker compose up -d

# Make changes, test, submit PR
```

**Not a developer?** You can still help:
- Improve documentation
- Report bugs with detailed reproduction steps
- Share your use cases and feature ideas
- Help answer questions in Discussions

---

## Documentation

### Getting Started
- **[Deployment Guide](https://github.com/FaultMaven/faultmaven-deploy)** — Complete self-hosting setup
- **[Development Guide](./docs/DEVELOPMENT.md)** — Local development environment
- **[FAQ](https://faultmaven.ai/faq)** — Frequently asked questions

### Configuration & Operations
- **[Security Guide](./docs/SECURITY.md)** — Authentication, secrets, data redaction
- **[Troubleshooting](./docs/TROUBLESHOOTING.md)** — Common issues and solutions
- **[API Reference](./docs/API.md)** — REST endpoint documentation

### Architecture & Design
- **[Architecture Overview](./docs/ARCHITECTURE.md)** — System design and data flow
- **[Roadmap](https://faultmaven.ai/roadmap)** — What we're building next

---

## Support

### Community Support (Free)
- **[GitHub Discussions](https://github.com/FaultMaven/faultmaven/discussions)** — Ask questions, share tips
- **[GitHub Issues](https://github.com/FaultMaven/faultmaven/issues)** — Report bugs, request features
- **[Website](https://faultmaven.ai)** — Product info, use cases, guides

### Enterprise Support
- **Email:** [support@faultmaven.ai](mailto:support@faultmaven.ai)
- **Enterprise SLA:** Included with Enterprise Cloud subscription

---

## Community

- **[Code of Conduct](https://github.com/FaultMaven/.github/blob/main/CODE_OF_CONDUCT.md)** — Our community standards
- **[Security Policy](https://github.com/FaultMaven/.github/blob/main/SECURITY.md)** — How to report vulnerabilities
- **[Discussions](https://github.com/FaultMaven/faultmaven/discussions)** — Questions, ideas, show & tell

---

## License

**Apache 2.0** — Use commercially, fork freely, contribute back if you'd like.

Same license as Kubernetes, TensorFlow, and Apache Kafka. We believe in open infrastructure.

---

<p align="center">
  <strong>FaultMaven</strong> — Your AI copilot for troubleshooting.<br>
  Built on the same core analysis engine. Use Self-Hosted for personal context, or Enterprise for shared team intelligence.
</p>
