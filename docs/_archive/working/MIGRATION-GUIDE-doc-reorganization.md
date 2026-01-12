# Documentation Migration Guide

**Purpose**: Step-by-step commands for reorganizing FaultMaven documentation
**Prerequisite**: Approval of audit recommendations
**Estimated Time**: 2-3 days for file moves, 1-2 weeks total with testing

---

## Pre-Migration Checklist

- [ ] Read full audit report: `AUDIT-documentation-public-repo-strategy.md`
- [ ] Approval obtained from engineering leadership
- [ ] Backup current documentation: `git tag docs-backup-$(date +%Y%m%d)`
- [ ] Create branch: `git checkout -b docs/reorganization-for-open-source`
- [ ] Link checker baseline: `lychee docs/ > link-check-before.txt`

---

## Step 1: Create Internal Documentation Repository

### Option A: Separate Repository (Recommended)

```bash
cd /home/swhouse/product

# Create new repository
mkdir faultmaven-doc-internal
cd faultmaven-doc-internal
git init
echo "# FaultMaven Internal Documentation" > README.md
echo "Internal engineering documentation for FaultMaven platform." >> README.md
echo "" >> README.md
echo "**Audience**: Internal engineers, architects, product team" >> README.md

# Create directory structure
mkdir -p architecture
mkdir -p infrastructure
mkdir -p observability
mkdir -p security
mkdir -p planning
mkdir -p database/schema
mkdir -p decisions

# Initial commit
git add .
git commit -m "Initial commit: Internal documentation repository structure"

# Push to GitHub (if remote configured)
# git remote add origin git@github.com:FaultMaven/faultmaven-doc-internal.git
# git push -u origin main
```

### Option B: Subdirectory in Existing Repo

```bash
cd /home/swhouse/product/faultmaven
mkdir -p .internal-docs
echo ".internal-docs/" >> .gitignore  # Don't commit to public repo
```

**Recommendation**: Use separate repository for clean separation and access control.

---

## Step 2: Move Architecture Documentation

### 2.1 Move All Architecture Files (94 files)

```bash
cd /home/swhouse/product/faultmaven

# Move architecture directory to internal repo
# (Preserves git history)
git mv docs/architecture/* ../faultmaven-doc-internal/architecture/

# Verify move
ls ../faultmaven-doc-internal/architecture/ | wc -l  # Should show ~94 files

# Commit in faultmaven repo
git commit -m "docs: Move architecture documentation to internal repository

All 94 architecture files moved to faultmaven-doc-internal for internal team use.
Public repository will have simplified 3-page architecture overview.

See: AUDIT-documentation-public-repo-strategy.md"
```

### 2.2 Create Simplified Public Architecture Overview

```bash
cd /home/swhouse/product/faultmaven

# Create new simplified overview
cat > docs/architecture/README.md << 'EOF'
# FaultMaven Architecture

**For detailed architecture documentation, internal team members see: [faultmaven-doc-internal](https://github.com/FaultMaven/faultmaven-doc-internal)**

This directory contains high-level architecture overview for open-source contributors.

## Quick Links

- [Architecture Overview](overview.md) - How FaultMaven works (3-page intro)
- [Clean Architecture](clean-architecture.md) - Dependency injection and interface patterns

## Need More Details?

Internal engineering documentation is available in the private `faultmaven-doc-internal` repository:
- Complete system design documents
- Architecture decision records (ADRs)
- Migration strategies
- Deployment architecture
EOF

# Commit
git add docs/architecture/README.md
git commit -m "docs: Add simplified architecture index for public repo"
```

---

## Step 3: Move Logging & Observability Documentation

```bash
cd /home/swhouse/product/faultmaven

# Move logging documentation
git mv docs/logging/* ../faultmaven-doc-internal/observability/

# Verify
ls ../faultmaven-doc-internal/observability/ | wc -l  # Should show ~7 files

# Commit
git commit -m "docs: Move logging documentation to internal repository

Logging architecture, implementation guides, and policies moved to internal docs.
Public repository focuses on user-facing documentation."
```

---

## Step 4: Move Security Implementation Details

```bash
cd /home/swhouse/product/faultmaven

# Move security implementation docs (keep RBAC and client-protection public)
git mv docs/security/implementation-guide.md ../faultmaven-doc-internal/security/
git mv docs/security/comprehensive-protection-implementation-guide.md ../faultmaven-doc-internal/security/
git mv docs/security/pii-sanitization-configuration.md ../faultmaven-doc-internal/security/
git mv docs/security/KNOWLEDGE_BASE_USER_SCOPING_ISSUE.md ../faultmaven-doc-internal/security/

# Keep public (user-facing):
# - docs/security/role-based-access-control.md
# - docs/security/client-protection.md

# Update security README
cat > docs/security/README.md << 'EOF'
# FaultMaven Security

User-facing security documentation for FaultMaven.

## Available Guides

- [Role-Based Access Control](role-based-access-control.md) - User permissions and roles
- [Client Protection](client-protection.md) - Security features for end users

## For Internal Team

Security implementation details are in [faultmaven-doc-internal/security](https://github.com/FaultMaven/faultmaven-doc-internal/tree/main/security):
- PII sanitization configuration
- Security implementation guides
- Vulnerability analysis and fixes
EOF

git add docs/security/README.md
git commit -m "docs: Move security implementation details to internal repository

Keep user-facing RBAC and client protection docs public.
Move implementation details to internal docs."
```

---

## Step 5: Move Infrastructure Documentation

```bash
cd /home/swhouse/product/faultmaven

# Move internal infrastructure docs
git mv docs/infrastructure/redis-architecture-guide.md ../faultmaven-doc-internal/infrastructure/
git mv docs/infrastructure/KB_METADATA_PERSISTENCE.md ../faultmaven-doc-internal/infrastructure/
git mv docs/infrastructure/opik-setup.md ../faultmaven-doc-internal/infrastructure/

# Keep public: Local-LLM-Setup.md (user-facing)

# Update infrastructure README
cat > docs/infrastructure/README.md << 'EOF'
# FaultMaven Infrastructure

User-facing infrastructure documentation for local development.

## Available Guides

- [Local LLM Setup](Local-LLM-Setup.md) - Running local LLM models with FaultMaven

## For Enterprise Deployments

Enterprise infrastructure documentation is available in:
- Internal docs: [faultmaven-doc-internal/infrastructure](https://github.com/FaultMaven/faultmaven-doc-internal/tree/main/infrastructure)
- Operations: [faultmaven-enterprise-infra](https://github.com/FaultMaven/faultmaven-enterprise-infra)
EOF

git add docs/infrastructure/README.md
git commit -m "docs: Move internal infrastructure docs to internal repository

Keep Local-LLM-Setup.md public for users running local models.
Move Redis, Opik, and persistence architecture to internal docs."
```

---

## Step 6: Move Schema & Database Documentation

```bash
cd /home/swhouse/product/faultmaven

# Move database schemas
git mv docs/schema/* ../faultmaven-doc-internal/database/schema/

# Remove empty schema directory
rmdir docs/schema

git commit -m "docs: Move database schemas to internal repository

Database schemas are internal implementation details.
Removed from public repository."
```

---

## Step 7: Move Planning & Strategy Documents

```bash
cd /home/swhouse/product/faultmaven

# Move evolution strategy and planning docs
git mv docs/FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md ../faultmaven-doc-internal/planning/
git mv ARCHITECTURE_ANALYSIS.md ../faultmaven-doc-internal/planning/
git mv PR46_IMPLEMENTATION_PLAN.md ../faultmaven-doc-internal/planning/

# Move bugfixes analysis
git mv docs/bugfixes/* ../faultmaven-doc-internal/planning/bugfixes/
rmdir docs/bugfixes

git commit -m "docs: Move platform evolution strategy and planning docs to internal repository

Platform evolution strategy, architecture analysis, and PR implementation plans
are internal engineering documents. Moved to faultmaven-doc-internal."
```

---

## Step 8: Move Operational Runbooks to Enterprise Infra

```bash
cd /home/swhouse/product/faultmaven

# Create or use existing enterprise-infra repository
# Assuming faultmaven-enterprise-infra exists:

# Move all runbooks
git mv docs/runbooks/* ../faultmaven-enterprise-infra/runbooks/

# Remove empty runbooks directory
rmdir docs/runbooks

git commit -m "docs: Move operational runbooks to faultmaven-enterprise-infra

Kubernetes, PostgreSQL, Redis, and networking runbooks are for enterprise
operations. Moved to faultmaven-enterprise-infra repository."
```

---

## Step 9: Archive Obsolete Documentation

```bash
cd /home/swhouse/product/faultmaven

# Create archive directory for 2025
mkdir -p docs/archive/2025

# Move obsolete/temporary docs
git mv docs/recycle/* docs/archive/2025/recycle/
git mv docs/tools/planned/* docs/archive/2025/tools-planned/

# Note: architecture/archive and architecture/_temp already moved in Step 2

# Remove empty directories
rmdir docs/recycle
rmdir docs/tools/planned

git commit -m "docs: Archive obsolete and temporary documentation

Moved recycle/, tools/planned/ to docs/archive/2025/.
Keep git history but clean up active documentation."
```

---

## Step 10: Simplify README.md

```bash
cd /home/swhouse/product/faultmaven

# Create backup
cp README.md README.md.backup

# Manual edits required - use text editor
# Key changes:
# 1. Remove lines 119-134 (Enterprise Edition infrastructure details)
# 2. Remove lines 191-205 (Deployment profile discussion)
# 3. Add "FaultMaven Cloud" option in Quick Start section
# 4. Simplify "What's Included" section

# Example Quick Start section to add:

cat > README-quickstart-addition.md << 'EOF'
## Quick Start

### Option 1: Install Locally (5 minutes)

Get FaultMaven running on your machine with zero external dependencies:

```bash
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env
# Edit .env - add your LLM API key (OPENAI_API_KEY, ANTHROPIC_API_KEY, or FIREWORKS_API_KEY)
python -m faultmaven
```

Visit **http://localhost:8000** - you're running FaultMaven!

**Interactive API Docs**: http://localhost:8000/docs

---

### Option 2: Use FaultMaven Cloud ☁️

Skip the installation and get started in 60 seconds:

👉 **[Sign Up for FaultMaven Cloud](https://faultmaven.com/signup)**

**What you get**:
- ✅ Fully managed infrastructure
- ✅ Enterprise security and compliance
- ✅ Team collaboration features
- ✅ Automatic updates and scaling
- ✅ No installation required

**Perfect for**: Teams, enterprise users, production troubleshooting

---
EOF

# Manually integrate the above into README.md
# Then commit

git add README.md
git commit -m "docs: Simplify README for open-source users

- Add FaultMaven Cloud as prominent Option 2
- Remove deployment neutrality discussion
- Remove enterprise infrastructure details
- Focus on two clear user paths: local install OR cloud"
```

---

## Step 11: Simplify .env.example

```bash
cd /home/swhouse/product/faultmaven

# Create backup
cp .env.example .env.example.backup

# Create simplified version
cat > .env.example << 'EOF'
# =============================================================================
# FaultMaven Configuration
# =============================================================================
# Copy this file to .env and configure for your environment.
# See docs/development/ENVIRONMENT_VARIABLES.md for details.

# -----------------------------------------------------------------------------
# LLM Provider (REQUIRED - choose one)
# -----------------------------------------------------------------------------
# FaultMaven supports multiple LLM providers. Configure at least one.

CHAT_PROVIDER=openai  # Options: openai, anthropic, fireworks, gemini, groq, huggingface, local

# OpenAI Configuration (if using openai)
OPENAI_API_KEY=sk-your-openai-key-here
OPENAI_MODEL=gpt-4o

# Anthropic Configuration (if using anthropic)
# ANTHROPIC_API_KEY=sk-ant-your-anthropic-key-here
# ANTHROPIC_MODEL=claude-3-5-sonnet-20241022

# Fireworks AI Configuration (if using fireworks)
# FIREWORKS_API_KEY=fw-your-fireworks-key-here
# FIREWORKS_MODEL=llama-v3p1-8b-instruct

# Local LLM Configuration (if using local)
# LOCAL_LLM_URL=http://localhost:11434
# LOCAL_LLM_MODEL=llama2

# -----------------------------------------------------------------------------
# Optional: Web Search (for investigation tool)
# -----------------------------------------------------------------------------
# TAVILY_API_KEY=tvly-your-tavily-api-key

# -----------------------------------------------------------------------------
# Application Settings
# -----------------------------------------------------------------------------
LOG_LEVEL=INFO  # Options: DEBUG, INFO, WARNING, ERROR
MAX_UPLOAD_SIZE_MB=50
SESSION_TIMEOUT_MINUTES=30

# -----------------------------------------------------------------------------
# Database (local development uses SQLite by default)
# -----------------------------------------------------------------------------
# DATABASE_URL=sqlite:///./data/faultmaven.db  # Default - no change needed

# -----------------------------------------------------------------------------
# Development Settings
# -----------------------------------------------------------------------------
# ENABLE_RELOAD=true  # Auto-reload on code changes (development only)
# CORS_ORIGINS=http://localhost:3000,http://localhost:8080  # Frontend URLs

# =============================================================================
# Need more configuration options?
# See docs/development/ENVIRONMENT_VARIABLES.md for complete reference
# =============================================================================
EOF

git add .env.example
git commit -m "docs: Simplify .env.example for users

Reduce from 50+ variables to 10-15 user-facing settings:
- LLM provider configuration (required)
- Optional web search API key
- Basic application settings

Remove enterprise-only variables:
- Opik tracing configuration
- Presidio PII redaction
- Redis Sentinel
- PostgreSQL connection pooling
- Prometheus metrics

See ENVIRONMENT_VARIABLES.md for complete reference."
```

---

## Step 12: Update Documentation Index

```bash
cd /home/swhouse/product/faultmaven

# Rewrite docs/README.md
cat > docs/README.md << 'EOF'
# FaultMaven Documentation

Welcome to FaultMaven documentation! This index helps you find what you need quickly.

---

## For Users

### Getting Started
- 🚀 **[Quick Start Guide](QUICKSTART.md)** - Install and run FaultMaven in 5 minutes
- 📖 **[User Guide](getting-started/user-guide.md)** - Core concepts, API usage, workflows
- ☁️ **[FaultMaven Cloud](getting-started/cloud-setup.md)** - Sign up and get started in 60 seconds *(coming soon)*

### Using FaultMaven
- 🔌 **[API Documentation](#)** - REST API reference *(coming soon)*
- 🛠️ **[Tools](tools/)** - Knowledge base search, web search, log analysis
- 📚 **[Features](features/)** - Phase-based retrieval, evidence store
- ❓ **[Troubleshooting](#)** - Common issues and solutions *(coming soon)*

---

## For Developers

### Contributing
- 🤝 **[Contributing Guide](CONTRIBUTING.md)** - How to contribute to FaultMaven
- 📜 **[Code of Conduct](CODE_OF_CONDUCT.md)** - Community standards

### Development
- 💻 **[Development Setup](development/)** - Environment setup, adding LLM providers
- 🧪 **[Testing](testing/)** - Test patterns, architecture testing
- 🔌 **[Plugin Development](#)** - Create LLM providers, storage backends, tools *(coming soon)*

### Architecture
- 🏗️ **[Architecture Overview](architecture/overview.md)** - How FaultMaven works *(coming soon)*
- 🧩 **[Clean Architecture](architecture/clean-architecture.md)** - DI patterns, interfaces *(coming soon)*

---

## For Internal Team

**Need detailed internal documentation?**

Internal engineering documentation is available in separate repositories:

- **Architecture & Design**: [faultmaven-doc-internal](https://github.com/FaultMaven/faultmaven-doc-internal)
  - Complete system design documents
  - Architecture decision records (ADRs)
  - Deployment neutrality architecture
  - Evolution strategies and planning

- **Operations & Infrastructure**: [faultmaven-enterprise-infra](https://github.com/FaultMaven/faultmaven-enterprise-infra)
  - Kubernetes runbooks
  - Production deployment guides
  - Observability setup (Opik, Prometheus)
  - Enterprise security configuration

---

## Documentation Organization

| Directory | Purpose | Audience |
|-----------|---------|----------|
| `getting-started/` | Installation, quick start, user guide | Users, new contributors |
| `development/` | Development setup, adding providers | Contributors, developers |
| `testing/` | Testing standards and patterns | Contributors, QA engineers |
| `tools/` | Tool documentation (KB, web search) | Users, developers |
| `features/` | Feature documentation | Users, product team |
| `architecture/` | High-level architecture overview | Contributors, architects |
| `how-to/` | Integration guides | Users, developers |
| `security/` | User-facing security features | Users, security-conscious teams |
| `infrastructure/` | Local LLM setup | Users running local models |

---

## Quick Links

- 🌐 **[FaultMaven Website](https://faultmaven.com)**
- 💬 **[GitHub Discussions](https://github.com/FaultMaven/faultmaven/discussions)**
- 🐛 **[Report Issues](https://github.com/FaultMaven/faultmaven/issues)**
- 📧 **[Contact Support](mailto:support@faultmaven.ai)**

---

**Last Updated**: 2026-01-04
**Documentation Version**: 3.0 (Post-reorganization)
EOF

git add docs/README.md
git commit -m "docs: Reorganize documentation index for open-source users

Focus on user-facing documentation:
- Getting Started (installation, cloud, user guide)
- Using FaultMaven (API, tools, features)
- Contributing (development, testing, plugins)
- Architecture (high-level overview only)

Link to internal repositories for detailed engineering docs."
```

---

## Step 13: Commit Internal Repository Changes

```bash
cd /home/swhouse/product/faultmaven-doc-internal

# Stage all moved files
git add .

# Commit
git commit -m "docs: Import internal documentation from faultmaven public repo

Moved 180+ internal engineering documents from public repository:
- 94 architecture files
- 7 logging/observability files
- 4 security implementation files
- 3 infrastructure files
- 3 planning/strategy files
- 3 database schema files

Rationale:
These documents contain internal engineering decisions, deployment strategies,
and architecture details not relevant to open-source users.

See faultmaven repo: AUDIT-documentation-public-repo-strategy.md"

# Push to remote (if configured)
# git push origin main
```

---

## Step 14: Commit Enterprise Infrastructure Changes

```bash
cd /home/swhouse/product/faultmaven-enterprise-infra

# Stage all moved files
git add .

# Commit
git commit -m "docs: Import operational runbooks from faultmaven public repo

Moved 40+ operational runbooks from public repository:
- Kubernetes troubleshooting (15+ files)
- PostgreSQL operations
- Redis operations
- Networking troubleshooting

Rationale:
These runbooks are for enterprise operations and SRE teams,
not relevant to local development users.

See faultmaven repo: AUDIT-documentation-public-repo-strategy.md"

# Push to remote (if configured)
# git push origin main
```

---

## Step 15: Final Verification

### 15.1 Link Checking

```bash
cd /home/swhouse/product/faultmaven

# Run link checker
lychee docs/ > link-check-after.txt

# Compare before/after
diff link-check-before.txt link-check-after.txt

# Fix any broken links
```

### 15.2 File Count Verification

```bash
cd /home/swhouse/product/faultmaven

# Count markdown files
find . -name "*.md" | wc -l

# Expected: ~80-100 files (down from 331)

# List documentation files
find docs/ -name "*.md" | sort

# Expected directories:
# - docs/getting-started/
# - docs/development/
# - docs/testing/
# - docs/tools/
# - docs/features/
# - docs/how-to/
# - docs/security/
# - docs/infrastructure/
# - docs/architecture/ (minimal - overview only)
# - docs/archive/
```

### 15.3 Grep for Internal Concepts

```bash
cd /home/swhouse/product/faultmaven

# Check for remaining mentions of internal concepts
grep -r "deployment neutrality" docs/
grep -r "OODA" docs/
grep -r "milestone-based" docs/

# Expected: Zero or minimal results (only in changelog/history)
```

### 15.4 Test Local Installation

```bash
cd /home/swhouse/product/faultmaven

# Create fresh virtual environment
python -m venv test-env
source test-env/bin/activate

# Install from current branch
pip install -e .

# Verify startup (should work with simplified .env)
cp .env.example .env
# Add test API key
echo "OPENAI_API_KEY=sk-test-key" >> .env

# Try to start (should not crash)
timeout 10s python -m faultmaven || echo "Startup test complete"

# Clean up
deactivate
rm -rf test-env
```

---

## Step 16: Create Pull Request

```bash
cd /home/swhouse/product/faultmaven

# Push branch
git push origin docs/reorganization-for-open-source

# Create PR description
cat > PR-description.md << 'EOF'
# Documentation Reorganization for Open Source Launch

## Summary

This PR reorganizes FaultMaven documentation to focus on **user-facing content** for open-source launch. Moved 220+ internal engineering documents to separate repositories.

## Changes

### Moved to Internal Repository (`faultmaven-doc-internal`)
- 94 architecture files (internal design decisions)
- 7 logging/observability files
- 4 security implementation files
- 3 infrastructure files (Redis, Opik, persistence)
- 3 planning/strategy files
- 3 database schema files

**Total**: 180 files moved to internal

### Moved to Enterprise Infrastructure (`faultmaven-enterprise-infra`)
- 40+ operational runbooks (Kubernetes, PostgreSQL, Redis, networking)

### Simplified for Users
- **README.md**: Added "FaultMaven Cloud" option, removed deployment neutrality discussion
- **.env.example**: Reduced from 50+ to 10-15 user-facing variables
- **docs/README.md**: Reorganized index focused on user needs

### Archived
- 26 obsolete/temporary documents moved to `docs/archive/2025/`

## Result

- **Before**: 331 documentation files (54% internal, 12% ops)
- **After**: ~85 documentation files (100% user-facing)

## Testing

- [x] Link checker passed
- [x] Local installation works with simplified .env
- [x] No broken cross-references
- [x] Git history preserved

## Related Documents

- Full audit: `docs/working/AUDIT-documentation-public-repo-strategy.md`
- Executive summary: `docs/working/EXECUTIVE-SUMMARY-doc-audit.md`
- Migration guide: `docs/working/MIGRATION-GUIDE-doc-reorganization.md`

## Reviewers

@engineering-lead @tech-writer @product-manager
EOF

# Open PR on GitHub or create via CLI
# gh pr create --title "Documentation reorganization for open-source launch" --body-file PR-description.md
```

---

## Rollback Procedure (If Needed)

If issues discovered after migration:

```bash
cd /home/swhouse/product/faultmaven

# Restore from backup tag
git checkout docs-backup-$(date +%Y%m%d)

# Or revert specific commits
git revert HEAD~10..HEAD  # Last 10 commits

# Or restore specific files
git checkout HEAD~10 -- docs/architecture/

# Push rollback
git push origin main
```

---

## Post-Migration Tasks

### Create New User-Facing Documents (Week 3-4)

**Priority documents to create**:

1. `/docs/architecture/overview.md` - Simplified 3-page architecture
2. `/docs/user-guide/use-case-examples.md` - Real-world examples
3. `/docs/plugins/creating-llm-providers.md` - Plugin development guide
4. `/docs/troubleshooting/common-issues.md` - User-level debugging
5. `/docs/getting-started/cloud-setup.md` - FaultMaven Cloud guide

**Templates available in**: `docs/working/` (to be created by tech writer)

---

## Success Criteria

Migration complete when:

- [ ] All 180 internal docs moved to `faultmaven-doc-internal`
- [ ] All 40 ops runbooks moved to `faultmaven-enterprise-infra`
- [ ] README.md simplified (no deployment neutrality)
- [ ] .env.example reduced to 10-15 variables
- [ ] Link checker passes with 0 broken links
- [ ] New contributor can set up in <30 minutes
- [ ] No "deployment neutrality" mentions in public docs
- [ ] File count: ~85 public docs (down from 331)

---

## Timeline

| Day | Task | Hours | Status |
|-----|------|-------|--------|
| Day 1 | Steps 1-5: Create internal repo, move arch/logging/security/infra | 6 hours | ⏳ |
| Day 2 | Steps 6-9: Move schema/planning, runbooks, archive | 4 hours | ⏳ |
| Day 3 | Steps 10-12: Simplify README, .env, update index | 6 hours | ⏳ |
| Day 4 | Steps 13-14: Commit to internal/enterprise repos | 2 hours | ⏳ |
| Day 5 | Steps 15-16: Verification, PR creation | 4 hours | ⏳ |

**Total**: 3-5 days for file migration

---

## Support

**Questions or issues during migration?**

- Review full audit: `docs/working/AUDIT-documentation-public-repo-strategy.md`
- Contact: Tech Writing Team
- Slack: #faultmaven-docs

---

**Document Version**: 1.0
**Last Updated**: 2026-01-04
**Owner**: Documentation Team
