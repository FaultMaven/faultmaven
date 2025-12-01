# FaultMaven Frequently Asked Questions (FAQ)

Common questions about FaultMaven deployment, usage, and features.

## Table of Contents

- [General](#general)
- [Deployment & Installation](#deployment--installation)
- [Costs & Pricing](#costs--pricing)
- [Features & Capabilities](#features--capabilities)
- [Technical](#technical)
- [Security & Privacy](#security--privacy)
- [Enterprise vs Self-Hosted](#enterprise-vs-self-hosted)
- [Troubleshooting](#troubleshooting)

---

## General

### What is FaultMaven?

FaultMaven is an AI-powered troubleshooting copilot that helps you investigate and resolve technical issues faster. It combines:
- **Browser extension** for real-time troubleshooting assistance
- **Knowledge base** for storing runbooks and documentation
- **Case management** for tracking investigations
- **AI agents** powered by GPT-4, Claude, or Fireworks models

### How does FaultMaven work?

1. You describe a problem in the browser extension
2. FaultMaven searches your knowledge base for relevant documentation
3. An AI agent analyzes the issue and provides troubleshooting steps
4. You can upload logs, screenshots, and configs for deeper analysis
5. All investigation history is saved as a "case" for future reference

### Is FaultMaven open source?

**Partially.** The self-hosted version is Apache 2.0 licensed and open source. Enterprise features (multi-tenancy, SSO, teams) are proprietary.

See: [Enterprise Superset Model](https://github.com/FaultMaven/faultmaven#architecture-philosophy-enterprise-superset-model)

### Who is FaultMaven for?

- DevOps engineers
- Site reliability engineers (SREs)
- System administrators
- Support teams
- Individual developers troubleshooting production issues

---

## Deployment & Installation

### How do I install FaultMaven?

**Self-Hosted (Docker Compose):**
```bash
git clone https://github.com/FaultMaven/faultmaven-deploy.git
cd faultmaven-deploy
cp .env.example .env
# Add your LLM API key to .env
docker compose up -d
```

See: [Quick Start Guide](https://github.com/FaultMaven/faultmaven-deploy#quick-start)

### What are the system requirements?

**Minimum (< 10 users):**
- 2 CPU cores
- 4GB RAM
- 20GB storage
- Docker 24.0+

**Recommended (production):**
- 4 CPU cores
- 8GB RAM
- 100GB SSD storage
- Load balancer
- SSL certificate

See: [Deployment Guide](DEPLOYMENT.md)

### Can I run FaultMaven on Raspberry Pi?

Not officially supported. FaultMaven images are built for amd64/arm64, but resource requirements exceed most Raspberry Pi capabilities.

For experimentation, try on a Raspberry Pi 4 (8GB RAM) or Pi 5, but expect performance issues.

### How do I update FaultMaven?

```bash
cd faultmaven-deploy
docker compose pull
docker compose up -d
```

All data persists in Docker volumes, so updates are safe.

### Can I deploy on Kubernetes?

Yes, for enterprise deployments. Helm charts are in development.

Current recommendation: Use `kompose` to convert docker-compose.yml to Kubernetes manifests.

---

## Costs & Pricing

### Is FaultMaven free?

**Self-Hosted:** Free and open source (Apache 2.0).
**Enterprise:** Paid subscription with additional features.

However, you **must provide your own LLM API key**, which has usage costs.

### What does the LLM API cost?

Costs vary by provider:

| Provider | Model | Cost per Request* |
|----------|-------|------------------|
| OpenAI | GPT-4 Turbo | ~$0.03 - $0.10 |
| OpenAI | GPT-3.5 Turbo | ~$0.002 - $0.01 |
| Anthropic | Claude 3 Opus | ~$0.05 - $0.15 |
| Anthropic | Claude 3 Sonnet | ~$0.01 - $0.05 |
| Anthropic | Claude 3 Haiku | ~$0.001 - $0.005 |
| Fireworks | Mixtral 8x7B | ~$0.001 - $0.005 |

*Approximate; varies by conversation length

**Cost Estimate:**
- Light usage (10 requests/day): $5-15/month
- Medium usage (100 requests/day): $50-150/month
- Heavy usage (1000 requests/day): $500-1500/month

### Can I use local LLMs?

Yes. FaultMaven supports local LLM providers:
- Ollama
- LM Studio
- LocalAI
- vLLM

Local LLMs keep all data on your machine. See the deployment guide for configuration.

### What about the infrastructure costs?

**Self-Hosted:**
- VPS/Cloud VM: $10-50/month (DigitalOcean, Linode, etc.)
- Or free if running on existing hardware

**Enterprise:**
- Starts at $99/user/month (includes infrastructure and support)

---

## Features & Capabilities

### What LLM providers are supported?

- ✅ OpenAI (GPT-4, GPT-4 Turbo, GPT-3.5)
- ✅ Anthropic (Claude 3 Opus, Sonnet, Haiku)
- ✅ Fireworks AI (Mixtral, Llama, etc.)
- ✅ Google (Gemini)
- ✅ Groq
- ✅ Local LLMs (Ollama, LM Studio, LocalAI, vLLM)
- 🚧 Azure OpenAI - planned

### Can I upload my own documentation?

Yes! Upload PDFs, Markdown, or text files to the knowledge base. FaultMaven will:
1. Extract text
2. Create embeddings
3. Index for semantic search
4. Retrieve relevant snippets during troubleshooting

Supported formats: PDF, TXT, MD (more formats planned)

### How does the knowledge base work?

FaultMaven uses **Retrieval-Augmented Generation (RAG)**:

1. Documents are split into chunks
2. Embeddings generated with Sentence Transformers (BGE-M3)
3. Stored in ChromaDB (vector database)
4. During chat, relevant chunks are retrieved and sent to LLM
5. LLM generates responses grounded in your documentation

### Can I search for similar past cases?

Yes. The case list shows all previous investigations. You can:
- Filter by status (open, resolved, closed)
- Search by title/description
- View conversation history
- Link related cases

### Does FaultMaven integrate with other tools?

**Currently:** No direct integrations.

**Planned:**
- Slack/Discord notifications
- Jira issue creation
- PagerDuty integration
- Webhook support for custom integrations

### Can multiple users share a knowledge base?

**Self-Hosted:** No. Each user has their own knowledge base.

**Enterprise:** Yes. Organizations can share team knowledge bases with role-based access control.

---

## Technical

### What databases does FaultMaven use?

**Self-Hosted:**
- SQLite for cases, users, metadata
- Redis for sessions and task queue
- ChromaDB for knowledge base (vector embeddings)

**Enterprise:**
- PostgreSQL (replaces SQLite)
- Redis Cluster
- ChromaDB or Pinecone

### What ports does FaultMaven use?

| Service | Port | Purpose |
|---------|------|---------|
| API Gateway | 8000 | Public API |
| Dashboard | 3000 | Web UI |
| Auth Service | 8001 | Internal only |
| Session Service | 8002 | Internal only |
| Case Service | 8003 | Internal only |
| Knowledge Service | 8004 | Internal only |
| Evidence Service | 8005 | Internal only |
| Redis | 6379 | Internal only |
| ChromaDB | 8000 | Internal only |

Only ports 8000 and 3000 should be exposed externally (via reverse proxy).

### Can I use FaultMaven offline?

**Partially.** You can:
- ✅ Access local knowledge base
- ✅ View past cases
- ❌ Get AI responses (requires internet for LLM API)

Future: Local LLM support will enable fully offline operation.

### How is data stored?

All data is stored in Docker volumes:
- `case-data` - Cases and conversations (SQLite)
- `auth-data` - Users and credentials (SQLite, bcrypt hashed passwords)
- `knowledge-data` - Document embeddings (ChromaDB)
- `evidence-data` - Uploaded files (filesystem)
- `redis-data` - Sessions and queues (Redis)

### Can I migrate data to a new server?

Yes. Backup volumes, transfer to new server, and restore:

```bash
# Old server
docker run --rm -v case-data:/data -v $(pwd):/backup alpine tar czf /backup/cases.tar.gz -C /data .

# Transfer backup to new server
scp cases.tar.gz newserver:/opt/faultmaven/

# New server
docker run --rm -v case-data:/data -v $(pwd):/backup alpine tar xzf /backup/cases.tar.gz -C /data
```

See: [Deployment Guide - Backup](DEPLOYMENT.md#backup-strategy)

---

## Security & Privacy

### Is my data secure?

**Yes.** Security measures:
- Passwords hashed with bcrypt
- JWT-based authentication
- HTTPS/TLS encryption in transit
- Data isolation per user
- No telemetry or tracking in self-hosted version

See: [Security Guide](SECURITY.md)

### Where is my data stored?

**Self-Hosted:** On your server, in Docker volumes. You have full control.

**Enterprise:** On FaultMaven's infrastructure (encrypted, SOC 2 compliant). Can be deployed in your VPC for additional security.

### Is my data sent to OpenAI/Anthropic?

**Yes, but...**

When you ask a question, FaultMaven sends:
- Your message
- Relevant knowledge base snippets
- Conversation history

**Automatic PII redaction** removes:
- Email addresses (optional)
- SSNs
- Credit card numbers
- API keys

See: [Security Guide - Data Protection](SECURITY.md#data-protection)

### Can I use FaultMaven with HIPAA/GDPR data?

**Self-Hosted:** Potentially, with proper configuration:
- Enable database encryption
- Use HIPAA-compliant LLM provider (OpenAI BAA, Azure OpenAI)
- Implement additional access controls
- Consult legal counsel

**Enterprise:** Supports HIPAA and GDPR compliance with BAA available.

### How do I report a security vulnerability?

Email: **security@faultmaven.ai**

See: [Security Guide - Responsible Disclosure](SECURITY.md#reporting-security-vulnerabilities)

---

## Enterprise vs Self-Hosted

### What's the difference?

| Feature | Self-Hosted | Enterprise |
|---------|-------------|------------|
| **Core Features** |
| AI troubleshooting | ✅ | ✅ |
| Knowledge base | ✅ | ✅ |
| Case management | ✅ | ✅ |
| Browser extension | ✅ | ✅ |
| **Collaboration** |
| Multi-user | ❌ | ✅ |
| Organizations | ❌ | ✅ |
| Teams | ❌ | ✅ |
| Shared knowledge bases | ❌ | ✅ |
| **Authentication** |
| JWT login | ✅ | ✅ |
| SSO/SAML | ❌ | ✅ |
| LDAP/AD | ❌ | ✅ |
| **Infrastructure** |
| Deployment | Self-managed | Managed |
| Backups | Self-managed | Automated |
| Updates | Manual | Automatic |
| Support | Community | 24/7 SLA |
| **Compliance** |
| HIPAA | Self-configure | ✅ |
| SOC 2 | ❌ | ✅ |
| GDPR | Self-configure | ✅ |
| **Pricing** |
| Cost | Free + LLM API | $99/user/month |

### Can I upgrade from self-hosted to enterprise?

Yes. Contact sales@faultmaven.ai for migration assistance. Your data can be imported into the enterprise platform.

### Can I get enterprise features on self-hosted?

No. Enterprise features are proprietary. However, you can contribute to self-hosted features via GitHub.

---

## Troubleshooting

### Services won't start

**Check:**
```bash
# Logs
docker compose logs

# Disk space
df -h

# Memory
free -h

# .env file
cat .env | grep -E "API_KEY|JWT_SECRET"
```

See: [Troubleshooting Guide](TROUBLESHOOTING.md#service-startup-issues)

### Extension won't connect

**Common causes:**
1. API URL incorrect (check extension settings)
2. API not running (`curl http://localhost:8000/health`)
3. CORS issues (check browser console)
4. Firewall blocking connection

See: [Troubleshooting Guide - API Connection](TROUBLESHOOTING.md#api-connection-issues)

### Knowledge base search returns nothing

**Possible reasons:**
1. No documents uploaded yet
2. Documents still processing (check job worker logs)
3. ChromaDB not running
4. Search query too specific

See: [Troubleshooting Guide - Knowledge Base](TROUBLESHOOTING.md#knowledge-base-issues)

### Slow AI responses

**Potential causes:**
1. LLM provider API slowness (check status pages)
2. Large knowledge base (many documents to search)
3. Server resource constraints

**Solutions:**
- Switch LLM provider temporarily
- Increase server resources
- Reduce max_tokens in settings

### Where can I get help?

1. **Documentation:** https://github.com/FaultMaven/faultmaven/tree/main/docs
2. **GitHub Issues:** https://github.com/FaultMaven/faultmaven/issues
3. **Discussions:** https://github.com/FaultMaven/faultmaven/discussions
4. **Email:** support@faultmaven.ai (enterprise only)

---

## Contributing

### How can I contribute?

We welcome contributions!

1. **Code:** Submit PRs for bug fixes and features
2. **Documentation:** Improve guides and examples
3. **Issues:** Report bugs and request features
4. **Community:** Help others in Discussions

See: [Contributing Guide](https://github.com/FaultMaven/faultmaven/blob/main/CONTRIBUTING.md)

### What's the roadmap?

**Upcoming Features:**
- Local LLM support (Ollama)
- Slack/Discord integrations
- Jira integration
- Advanced analytics
- Multi-language support
- Voice input
- Mobile app

See: [GitHub Projects](https://github.com/orgs/FaultMaven/projects)

### Can I request features?

Yes! Open a feature request: https://github.com/FaultMaven/faultmaven/issues/new

We prioritize based on:
- Community votes (👍 on issues)
- Implementation effort
- Strategic alignment

---

## Still Have Questions?

**Community Support:**
- GitHub Discussions: https://github.com/FaultMaven/faultmaven/discussions
- Stack Overflow: Tag with `faultmaven`

**Commercial Support:**
- Email: support@faultmaven.ai
- Enterprise: Includes 24/7 support with SLA

**Documentation:**
- [Architecture](ARCHITECTURE.md)
- [API Reference](API.md)
- [Development Guide](DEVELOPMENT.md)
- [Deployment Guide](DEPLOYMENT.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Security](SECURITY.md)

---

**Last Updated:** 2025-11-20
**Version:** 2.0
**Status:** Current
