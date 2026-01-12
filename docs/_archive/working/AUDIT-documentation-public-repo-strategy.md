# FaultMaven Public Repository Documentation Audit

**Document Type**: Audit Report
**Created**: 2026-01-04
**Purpose**: Evaluate documentation for open-source public repository strategy
**Status**: DRAFT - For Review

---

## Executive Summary

This audit evaluates all documentation in the `faultmaven` repository (331 markdown files) against the user-defined principle:

> **"Deployment neutrality is OUR concern, not users'. Users should see:**
> - **How to install locally (clear steps)**
> - **Or use managed cloud (signup link)**
>
> **They should NOT be bothered with deployment neutrality - we enforce it behind the scenes."**

### Key Findings

1. **Strong User-Facing Foundation**: Installation and quick-start documentation is excellent for open-source users
2. **Internal Engineering Concepts Exposed**: Significant discussion of deployment neutrality, enterprise infrastructure, and internal architecture decisions visible to users
3. **Mixed Audience**: Documentation conflates user needs with internal engineering documentation
4. **Operational Details in Public Repo**: Kubernetes runbooks, Redis architecture, enterprise deployment patterns belong in internal docs

### Recommendation Summary

- **Keep in Public Repo**: ~85 documents (26% of total) - user-facing, development, API docs
- **Move to Internal**: ~180 documents (54% of total) - architecture decisions, deployment strategies, internal designs
- **Move to Enterprise/Infra**: ~40 documents (12% of total) - ops runbooks, infrastructure setup, enterprise features
- **Archive/Delete**: ~26 documents (8% of total) - redundant, outdated, temporary files

---

## Part 1: Documentation Inventory

### Total Documentation Count

- **Total Markdown Files**: 331 files
- **Primary Documentation Hub**: `/home/swhouse/product/faultmaven/docs/` (306 files)
- **Root Level**: 25 files (README.md, CHANGELOG.md, PR plans, etc.)

### Directory Structure

```
faultmaven/
├── README.md (29KB) - Main user-facing documentation
├── CHANGELOG.md
├── docs/
│   ├── README.md - Master documentation index
│   ├── QUICKSTART.md - 5-minute installation guide
│   ├── CONTRIBUTING.md
│   ├── CODE_OF_CONDUCT.md
│   ├── getting-started/ (1 file)
│   ├── architecture/ (94 files) ⚠️ INTERNAL
│   ├── development/ (6 files) ✅ PUBLIC
│   ├── testing/ (3 files) ✅ PUBLIC
│   ├── security/ (6 files) ⚠️ MIXED
│   ├── infrastructure/ (4 files) ⚠️ INTERNAL
│   ├── logging/ (7 files) ⚠️ INTERNAL
│   ├── runbooks/ (15+ files) ⚠️ ENTERPRISE
│   ├── tools/ (6 files) ✅ PUBLIC
│   ├── how-to/ (4 files) ✅ PUBLIC
│   ├── features/ (3 files) ✅ PUBLIC
│   ├── schema/ (3 files) ⚠️ INTERNAL
│   ├── bugfixes/ (1 file) ⚠️ INTERNAL
│   └── recycle/ (archived content)
└── [module READMEs, test READMEs, etc.]
```

---

## Part 2: Content Categorization

### ✅ Category 1: PUBLIC - User-Facing Documentation (Keep)

**Target Audience**: Open-source users, contributors, developers integrating FaultMaven

#### Installation & Getting Started
- `/README.md` ✅ **EXCELLENT** - Clear local installation, zero dependencies, enterprise upsell
- `/docs/QUICKSTART.md` ✅ **EXCELLENT** - 5-minute setup guide
- `/docs/getting-started/user-guide.md` ✅ **GOOD** - Core concepts, API usage
- `.env.example` ✅ - Configuration template

**Status**: **KEEP** - These are exactly what public repo needs

**Improvements Needed**:
- Remove "deployment neutrality" mentions in README (lines 119-134, 191-205)
- Simplify README's "What's Included" section - remove Enterprise Edition infrastructure details
- Add clear "FaultMaven Cloud" signup link as alternative to local installation

---

#### Development & Contributing
- `/docs/CONTRIBUTING.md` ✅
- `/docs/CODE_OF_CONDUCT.md` ✅
- `/docs/development/how-to-add-providers.md` ✅
- `/docs/development/ENVIRONMENT_VARIABLES.md` ✅ (simplify, remove internal flags)
- `/docs/development/DATETIME_STANDARD.md` ✅
- `/docs/development/TOKEN_ESTIMATION.md` ✅
- `/docs/development/DATABASE_MIGRATIONS.md` ✅
- `/docs/development/performance-testing.md` ✅

**Status**: **KEEP** with minor edits

**Improvements Needed**:
- Remove internal-only environment variables (observability flags, enterprise configs)
- Focus on contributor workflow, not production deployment

---

#### API Documentation
- `/docs/api/` (if exists) ✅
- API endpoint documentation in README ✅
- OpenAPI/Swagger specs ✅

**Status**: **KEEP** - Core for developers

---

#### Testing & Quality
- `/docs/testing/new-test-patterns.md` ✅
- `/docs/testing/architecture-testing-guide.md` ✅
- `/docs/testing/REBUILT_TESTING_STANDARDS.md` ✅
- `/tests/README.md` ✅

**Status**: **KEEP** - Essential for contributors

---

#### Tools & Features
- `/docs/tools/README.md` ✅
- `/docs/tools/developer-guide.md` ✅
- `/docs/tools/tool-catalog.md` ✅
- `/docs/tools/architecture/tool-interface-spec.md` ✅
- `/docs/tools/integrations/mcp-integration.md` ✅
- `/docs/features/phase-based-retrieval.md` ✅
- `/docs/features/case-evidence-store.md` ✅
- `/docs/features/platform-specific-extractors.md` ✅

**Status**: **KEEP** - User-facing features

---

#### How-To Guides (User-Focused)
- `/docs/how-to/testing-investigation-framework.md` ✅ (if user-focused)
- `/docs/how-to/knowledge-base-system.md` ✅

**Status**: **KEEP** with review

**Improvements Needed**:
- Remove operational/deployment how-tos (move to internal)

---

### ⚠️ Category 2: INTERNAL - Engineering Documentation (Move to faultmaven-doc-internal)

**Target Audience**: Internal engineers, architects, product team

#### Architecture Documentation (94 files - TOO MANY FOR PUBLIC)

**Core Issue**: The `/docs/architecture/` directory contains 94 files discussing internal design decisions, OODA loops, milestone-based investigations, data models, and engineering rationale. This is engineering documentation, NOT user documentation.

**Files to Move to Internal**:

1. **Strategic Planning Documents**
   - `/docs/FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md` ⚠️ **INTERNAL** - 1,682 lines of internal roadmap
   - `/docs/system-requirements-specification.md` ⚠️ **INTERNAL** - Internal requirements
   - `/docs/architecture/README.md` ⚠️ **INTERNAL** - Architecture index

2. **Architecture Deep-Dive** (ALL 94 files in `/docs/architecture/`)
   - `architecture-overview.md` ⚠️ **INTERNAL**
   - `ARCHITECTURE_SUMMARY.md` ⚠️ **INTERNAL**
   - `SYSTEM_DESIGN_MODULES.md` ⚠️ **INTERNAL**
   - `milestone-based-investigation-framework.md` ⚠️ **INTERNAL**
   - `case-and-session-concepts.md` ⚠️ **INTERNAL** (user guide version can stay)
   - `dependency-injection-system.md` ⚠️ **INTERNAL**
   - `authentication-design.md` ⚠️ **INTERNAL**
   - `data-storage-design.md` ⚠️ **INTERNAL**
   - `knowledge-base-architecture.md` ⚠️ **INTERNAL** (user guide version can stay)
   - All 40+ architecture specs, design docs, decision guides

3. **Infrastructure Architecture**
   - `/docs/infrastructure/redis-architecture-guide.md` ⚠️ **INTERNAL**
   - `/docs/infrastructure/KB_METADATA_PERSISTENCE.md` ⚠️ **INTERNAL**
   - `/docs/infrastructure/Local-LLM-Setup.md` ⚠️ **KEEP** (user-facing for local LLM)
   - `/docs/infrastructure/opik-setup.md` ⚠️ **INTERNAL** (enterprise observability)

4. **Internal Logging & Observability**
   - `/docs/logging/architecture.md` ⚠️ **INTERNAL**
   - `/docs/logging/implementation-guide.md` ⚠️ **INTERNAL**
   - `/docs/logging/configuration.md` ⚠️ **INTERNAL**
   - `/docs/logging/developer-guide.md` ⚠️ **INTERNAL**
   - `/docs/logging/operations-runbook.md` ⚠️ **INTERNAL**
   - `/docs/logging/testing-guide.md` ⚠️ **INTERNAL**
   - `/docs/logging/logging-policy.md` ⚠️ **INTERNAL**

5. **Security Implementation Details**
   - `/docs/security/implementation-guide.md` ⚠️ **INTERNAL**
   - `/docs/security/comprehensive-protection-implementation-guide.md` ⚠️ **INTERNAL**
   - `/docs/security/pii-sanitization-configuration.md` ⚠️ **INTERNAL**
   - `/docs/security/KNOWLEDGE_BASE_USER_SCOPING_ISSUE.md` ⚠️ **INTERNAL** (bug analysis)
   - **KEEP**: `/docs/security/role-based-access-control.md` (user-facing RBAC)
   - **KEEP**: `/docs/security/client-protection.md` (user-facing security features)

6. **Internal How-To Guides**
   - `/docs/how-to/operational-configuration.md` ⚠️ **INTERNAL** (ops config)

7. **Schema & Database**
   - `/docs/schema/` (all files) ⚠️ **INTERNAL** - database schemas for internal use

8. **Bug Fixes & Analysis**
   - `/docs/bugfixes/response-json-regression-root-cause-analysis.md` ⚠️ **INTERNAL**

9. **Root-Level Internal Documents**
   - `/ARCHITECTURE_ANALYSIS.md` ⚠️ **INTERNAL** (15KB internal analysis)
   - `/PR46_IMPLEMENTATION_PLAN.md` ⚠️ **INTERNAL** (implementation plan)

**Rationale for Moving to Internal**:
- These documents discuss HOW the system is built, not HOW to use it
- Expose internal engineering trade-offs users don't need to know
- Create confusion about what's user-facing vs internal
- Reveal deployment strategies that should be abstracted from users
- Contain architecture decision rationale for internal team alignment

---

### 🏢 Category 3: ENTERPRISE/OPS - Operational Documentation (Move to faultmaven-enterprise-infra)

**Target Audience**: DevOps, SREs, enterprise customers, cloud operations team

#### Runbooks (15+ files)
- `/docs/runbooks/kubernetes/` ⚠️ **ENTERPRISE**
  - `k8s-pod-crashloopbackoff.md`
  - `k8s-pod-oomkilled.md`
  - `k8s-pod-imagepullbackoff.md`
  - `k8s-node-not-ready.md`
- `/docs/runbooks/postgresql/` ⚠️ **ENTERPRISE**
- `/docs/runbooks/redis/` ⚠️ **ENTERPRISE**
- `/docs/runbooks/networking/` ⚠️ **ENTERPRISE**

**Rationale**:
- Kubernetes troubleshooting is for enterprise deployments, not local users
- These are operational procedures for running FaultMaven at scale
- Belong in enterprise infrastructure documentation
- Not relevant to open-source contributors running locally

---

### 🗑️ Category 4: ARCHIVE/DELETE - Redundant or Temporary (Clean Up)

#### Temporary/Working Documents
- `/docs/recycle/` - Already archived, can delete or move to internal
- `/docs/architecture/_temp/` - Temporary status reports
- `/docs/architecture/archive/` - Superseded documents
- `/docs/tools/planned/` - Future planning docs

#### Redundant Documentation
- Multiple "README.md" files in test directories (consolidate)
- Duplicate architecture documents (v1.0 vs v2.0)
- Old migration guides in `/docs/archive/migrations/`

**Action**: Move to `/docs/archive/YYYY/MM/` or delete entirely

---

## Part 3: Gap Analysis - What's Missing for Public Repo?

### Critical Gaps for User-Facing Documentation

1. **❌ Missing: "FaultMaven Cloud" Alternative**
   - README mentions enterprise features but no clear "sign up for cloud" option
   - User choice should be: "Install locally" OR "Use FaultMaven Cloud"
   - Need prominent cloud signup link in README

2. **❌ Missing: Simplified Architecture Overview (User Perspective)**
   - Current architecture docs are too detailed (94 files!)
   - Need simple "How FaultMaven Works" (2-3 pages) explaining:
     - Browser extension → API → AI reasoning
     - Knowledge base (RAG)
     - Case management workflow
     - NOT: dependency injection, OODA loops, milestone frameworks

3. **❌ Missing: Clear Plugin/Extension Guide**
   - How to build LLM provider plugins
   - How to build storage provider plugins
   - How to build tools for the agentic framework
   - Currently buried in architecture docs

4. **❌ Missing: Troubleshooting Guide (User-Level)**
   - Common setup issues
   - API connection problems
   - LLM provider errors
   - NOT: Kubernetes pod crashes, Redis cluster issues

5. **❌ Missing: Use Case Examples**
   - Example: "Debugging API latency spike"
   - Example: "Investigating Kubernetes pod crashes"
   - Example: "Root cause analysis for database deadlocks"
   - Show what FaultMaven can do, not how it's built

6. **❌ Overly Complex: ENVIRONMENT_VARIABLES.md**
   - Contains 50+ environment variables (many internal-only)
   - Users need: LLM API keys, basic config
   - Users DON'T need: Opik config, Presidio URLs, Redis Sentinel settings

---

### Gaps in Internal Documentation Strategy

1. **❌ No Clear Separation: Public vs Internal**
   - All documentation in one repository
   - No `faultmaven-doc-internal` repository structure
   - Internal architecture decisions mixed with user guides

2. **❌ Deployment Neutrality Over-Exposed**
   - `/docs/FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md` discusses deployment neutrality extensively
   - This is internal engineering concern, should be in `faultmaven-doc-internal`

3. **❌ Enterprise Features Too Prominent**
   - README discusses enterprise features (Presidio, Opik, Prometheus) in detail
   - Should be: "Need enterprise features? Contact us or use FaultMaven Cloud"

---

## Part 4: Recommended Reorganization

### New Public Repository Structure (`faultmaven`)

```
faultmaven/
├── README.md (SIMPLIFIED)
│   ├── Installation (local SQLite)
│   ├── FaultMaven Cloud (signup link)
│   ├── Quick Start (5 minutes)
│   ├── Core Features
│   └── Contributing
├── QUICKSTART.md ✅
├── CHANGELOG.md ✅
├── LICENSE ✅
├── .env.example (SIMPLIFIED - only user-facing vars)
├── docs/
│   ├── README.md (NEW - simplified doc index)
│   ├── getting-started/
│   │   ├── installation.md (local setup)
│   │   ├── cloud-setup.md (NEW - FaultMaven Cloud)
│   │   ├── quick-start.md
│   │   └── core-concepts.md (NEW - sessions, cases, queries)
│   ├── user-guide/
│   │   ├── using-the-api.md
│   │   ├── knowledge-base.md (user perspective)
│   │   ├── troubleshooting-workflows.md
│   │   └── use-case-examples.md (NEW)
│   ├── api/
│   │   ├── overview.md
│   │   ├── authentication.md
│   │   ├── cases-api.md
│   │   ├── sessions-api.md
│   │   ├── knowledge-api.md
│   │   └── openapi.yaml
│   ├── development/
│   │   ├── setup.md
│   │   ├── contributing.md
│   │   ├── adding-llm-providers.md ✅
│   │   ├── environment-variables.md (SIMPLIFIED)
│   │   ├── testing.md
│   │   └── code-standards.md
│   ├── plugins/ (NEW)
│   │   ├── creating-llm-providers.md
│   │   ├── creating-storage-backends.md
│   │   └── creating-tools.md
│   ├── troubleshooting/ (NEW - user-level)
│   │   ├── common-issues.md
│   │   ├── api-errors.md
│   │   └── llm-provider-issues.md
│   ├── architecture/ (HIGH-LEVEL ONLY)
│   │   ├── overview.md (NEW - simplified, 2-3 pages)
│   │   └── clean-architecture.md (NEW - DI, interfaces, patterns)
│   └── community/
│       ├── CODE_OF_CONDUCT.md ✅
│       ├── CONTRIBUTING.md ✅
│       └── support.md (NEW - how to get help)
```

**Total Public Documentation**: ~30-40 files (down from 331)

---

### New Internal Documentation Structure (`faultmaven-doc-internal`)

```
faultmaven-doc-internal/
├── README.md (internal team documentation index)
├── architecture/
│   ├── system-design/ (ALL 94 architecture files from public repo)
│   ├── deployment-neutrality.md ⚠️ CRITICAL
│   ├── agentic-framework-design.md
│   ├── milestone-based-investigation.md
│   ├── dependency-injection-deep-dive.md
│   └── [... all internal architecture docs]
├── infrastructure/
│   ├── redis-architecture-guide.md
│   ├── opik-integration.md
│   ├── presidio-setup.md
│   └── kubernetes-deployment.md
├── observability/
│   ├── logging-architecture.md
│   ├── logging-policy.md
│   ├── tracing-strategy.md
│   └── metrics-collection.md
├── security/
│   ├── pii-sanitization-implementation.md
│   ├── security-audit-procedures.md
│   └── vulnerability-management.md
├── planning/
│   ├── platform-evolution-strategy.md (current FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md)
│   ├── api-feature-gap-analysis.md
│   └── roadmap.md
├── database/
│   ├── schema/ (all schema files)
│   └── migration-strategy.md
└── decisions/
    ├── ADR-001-deployment-neutrality.md
    ├── ADR-002-monolith-architecture.md
    └── [... all architecture decisions]
```

**Total Internal Documentation**: ~200 files

---

### Enterprise Infrastructure Documentation (`faultmaven-enterprise-infra`)

```
faultmaven-enterprise-infra/
├── README.md (ops documentation index)
├── deployment/
│   ├── kubernetes/
│   │   ├── helm-charts/
│   │   └── deployment-guide.md
│   ├── docker-compose/
│   │   └── production-compose.yml
│   └── multi-tenant-setup.md
├── runbooks/
│   ├── kubernetes/ (ALL Kubernetes runbooks)
│   ├── postgresql/ (ALL PostgreSQL runbooks)
│   ├── redis/ (ALL Redis runbooks)
│   └── networking/ (ALL networking runbooks)
├── observability/
│   ├── opik-production-setup.md
│   ├── prometheus-configuration.md
│   └── grafana-dashboards.md
├── security/
│   ├── presidio-deployment.md
│   └── compliance/
│       ├── soc2-controls.md
│       └── gdpr-procedures.md
└── operations/
    ├── backup-recovery.md
    ├── disaster-recovery.md
    └── scaling-guide.md
```

**Total Enterprise Documentation**: ~40 files

---

## Part 5: Prioritized Action Plan

### Phase 1: CRITICAL - Cleanup for Open Source (Week 1-2)

**Priority**: P0 - Blocker for open-source launch

#### 1.1 Move Internal Documentation (2 days)

**Files to move to `faultmaven-doc-internal`**:
- All 94 files in `/docs/architecture/` (except new simplified overview)
- All 7 files in `/docs/logging/`
- 4 of 6 files in `/docs/security/` (keep RBAC, client-protection)
- 3 of 4 files in `/docs/infrastructure/` (keep Local-LLM-Setup.md)
- All files in `/docs/schema/`
- `/docs/bugfixes/`
- `/docs/FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md`
- `/ARCHITECTURE_ANALYSIS.md`
- `/PR46_IMPLEMENTATION_PLAN.md`

**Command**:
```bash
# Create faultmaven-doc-internal repository if doesn't exist
cd /home/swhouse/product
mkdir -p faultmaven-doc-internal/architecture
mkdir -p faultmaven-doc-internal/infrastructure
mkdir -p faultmaven-doc-internal/observability
mkdir -p faultmaven-doc-internal/security
mkdir -p faultmaven-doc-internal/planning

# Move architecture docs
git mv faultmaven/docs/architecture/* faultmaven-doc-internal/architecture/

# Move logging docs
git mv faultmaven/docs/logging/* faultmaven-doc-internal/observability/

# Move internal infrastructure docs
git mv faultmaven/docs/infrastructure/{redis-architecture-guide.md,KB_METADATA_PERSISTENCE.md,opik-setup.md} faultmaven-doc-internal/infrastructure/

# Move security implementation details
git mv faultmaven/docs/security/{implementation-guide.md,comprehensive-protection-implementation-guide.md,pii-sanitization-configuration.md,KNOWLEDGE_BASE_USER_SCOPING_ISSUE.md} faultmaven-doc-internal/security/

# Move planning docs
git mv faultmaven/docs/FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md faultmaven-doc-internal/planning/
git mv faultmaven/ARCHITECTURE_ANALYSIS.md faultmaven-doc-internal/planning/
git mv faultmaven/PR46_IMPLEMENTATION_PLAN.md faultmaven-doc-internal/planning/

# Move schema
git mv faultmaven/docs/schema/* faultmaven-doc-internal/database/schema/

# Move bugfixes
git mv faultmaven/docs/bugfixes/* faultmaven-doc-internal/planning/bugfixes/
```

---

#### 1.2 Move Enterprise/Ops Documentation (1 day)

**Files to move to `faultmaven-enterprise-infra`**:
- All runbooks in `/docs/runbooks/`

**Command**:
```bash
cd /home/swhouse/product
mkdir -p faultmaven-enterprise-infra/runbooks

# Move all runbooks
git mv faultmaven/docs/runbooks/* faultmaven-enterprise-infra/runbooks/
```

---

#### 1.3 Simplify README.md (1 day)

**Changes needed**:

1. **Remove deployment neutrality discussion** (lines 119-134, 191-205)
2. **Simplify "What's Included" section**:
   ```markdown
   ## Quick Start

   ### Option 1: Install Locally (5 minutes)

   ```bash
   git clone https://github.com/FaultMaven/faultmaven.git
   cd faultmaven
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .
   cp .env.example .env
   # Add your LLM API key to .env
   python -m faultmaven
   ```

   Visit http://localhost:8000 - you're running FaultMaven!

   ### Option 2: Use FaultMaven Cloud

   Skip the installation - get started in 60 seconds:
   👉 **[Sign Up for FaultMaven Cloud](https://faultmaven.com/signup)**

   - Fully managed infrastructure
   - Enterprise security and compliance
   - Team collaboration features
   - No installation required
   ```

3. **Remove enterprise features detail**:
   - Remove "Enterprise Edition" section with Opik/Presidio/PostgreSQL details
   - Replace with: "Need enterprise features? [Contact us](mailto:sales@faultmaven.ai)"

4. **Simplify architecture section**:
   - Keep high-level "How FaultMaven Works" diagram
   - Remove dependency injection, interface contracts details
   - Link to new simplified architecture overview

---

#### 1.4 Simplify .env.example (1 day)

**Current**: 50+ environment variables
**Target**: 10-15 user-facing variables

**Keep only**:
```bash
# LLM Provider (REQUIRED - choose one)
CHAT_PROVIDER=openai
OPENAI_API_KEY=sk-your-key-here
# ANTHROPIC_API_KEY=sk-ant-your-key-here
# FIREWORKS_API_KEY=fw-your-key-here

# Optional: Local LLM
# LOCAL_LLM_URL=http://localhost:11434
# LOCAL_LLM_MODEL=llama2

# Optional: Web Search
# TAVILY_API_KEY=tvly-your-key

# Application Settings
LOG_LEVEL=INFO
MAX_UPLOAD_SIZE_MB=50
```

**Remove**: All Opik, Presidio, Redis, PostgreSQL, enterprise config

---

### Phase 2: HIGH - Create New User-Facing Documentation (Week 3-4)

**Priority**: P1 - Essential for good user experience

#### 2.1 Create Simplified Architecture Overview (2 days)

**New file**: `/docs/architecture/overview.md`

**Content structure**:
```markdown
# How FaultMaven Works

A simple explanation of FaultMaven's architecture for users and contributors.

## Core Components

1. **Browser Extension** - Your troubleshooting interface
2. **FaultMaven API** - REST API backend
3. **AI Reasoning Engine** - LLM-powered investigation
4. **Knowledge Base** - RAG-powered document search
5. **Case Management** - Investigation tracking

## Data Flow

[Simple Mermaid diagram: Browser → API → Agent → Knowledge Base]

## Technology Stack

- **API**: FastAPI (Python 3.11+)
- **Database**: SQLite (local) or PostgreSQL (enterprise)
- **AI**: Multi-LLM support (OpenAI, Anthropic, Fireworks, etc.)
- **Knowledge Base**: ChromaDB with vector search

## Clean Architecture

FaultMaven uses dependency injection and interface-based design for:
- Easy testing with mocks
- Swappable LLM providers
- Pluggable storage backends
- Clear separation of concerns

[Link to development guide for details]
```

**Maximum length**: 3 pages

---

#### 2.2 Create Use Case Examples (2 days)

**New file**: `/docs/user-guide/use-case-examples.md`

**Content**:
- 3-5 real-world examples showing FaultMaven in action
- Step-by-step workflows with API calls
- Expected responses and troubleshooting tips

---

#### 2.3 Create Plugin Development Guide (2 days)

**New directory**: `/docs/plugins/`

**Files**:
- `creating-llm-providers.md`
- `creating-storage-backends.md`
- `creating-agent-tools.md`

**Purpose**: Enable community contributions

---

#### 2.4 Create Troubleshooting Guide (User-Level) (2 days)

**New file**: `/docs/troubleshooting/common-issues.md`

**Content**:
- Installation errors
- API connection problems
- LLM provider errors (API key issues, rate limits)
- Database locked errors (SQLite)
- Port conflicts

**Exclude**: Kubernetes, Redis, enterprise infrastructure

---

### Phase 3: MEDIUM - Archive/Cleanup (Week 5)

**Priority**: P2 - Nice-to-have cleanup

#### 3.1 Archive Obsolete Documents

**Move to** `/docs/archive/2025/`:
- `/docs/recycle/`
- `/docs/architecture/archive/`
- `/docs/architecture/_temp/`
- Superseded architecture documents (v1.0)

#### 3.2 Consolidate Test READMEs

**Current**: 5+ README.md files in test directories
**Target**: 1 `/tests/README.md` with links

---

### Phase 4: LOW - Polish & Refinement (Week 6)

**Priority**: P3 - Quality improvements

#### 4.1 Update Documentation Index

**Edit**: `/docs/README.md`

**New structure**:
```markdown
# FaultMaven Documentation

## For Users
- [Getting Started](getting-started/)
- [User Guide](user-guide/)
- [API Reference](api/)
- [Troubleshooting](troubleshooting/)

## For Developers
- [Development Setup](development/)
- [Contributing](community/CONTRIBUTING.md)
- [Plugin Development](plugins/)
- [Testing](development/testing.md)

## Architecture
- [Overview](architecture/overview.md) - High-level design
- [Clean Architecture](architecture/clean-architecture.md) - DI and interfaces

Need more details? Internal team members see [faultmaven-doc-internal](https://github.com/FaultMaven/faultmaven-doc-internal)
```

---

#### 4.2 Add Cloud Signup Links

**Files to update**:
- `/README.md` - Prominent cloud option
- `/docs/getting-started/cloud-setup.md` - New file
- `/docs/README.md` - Link to cloud option

---

## Part 6: Implementation Checklist

### Week 1: Move Internal Documentation

- [ ] Create `faultmaven-doc-internal` repository
- [ ] Move 94 architecture files to internal
- [ ] Move 7 logging files to internal
- [ ] Move 4 security implementation files to internal
- [ ] Move 3 infrastructure files to internal
- [ ] Move schema files to internal
- [ ] Move planning docs (evolution strategy, PRs) to internal
- [ ] Update cross-references in moved files
- [ ] Verify no broken links in public repo

### Week 2: Move Enterprise Documentation & Simplify Public

- [ ] Create `faultmaven-enterprise-infra` repository (or use existing)
- [ ] Move all runbooks to enterprise infra
- [ ] Simplify `/README.md` (remove deployment neutrality, enterprise details)
- [ ] Simplify `/.env.example` (10-15 vars only)
- [ ] Add "FaultMaven Cloud" signup links

### Week 3: Create New User Documentation

- [ ] Create `/docs/architecture/overview.md` (simplified, 3 pages)
- [ ] Create `/docs/user-guide/use-case-examples.md`
- [ ] Create `/docs/plugins/` directory with 3 guides
- [ ] Create `/docs/troubleshooting/common-issues.md`

### Week 4: Finalize & Test

- [ ] Update `/docs/README.md` with new structure
- [ ] Archive obsolete docs to `/docs/archive/2025/`
- [ ] Consolidate test READMEs
- [ ] Run link checker on all public docs
- [ ] Review with 2-3 external beta testers
- [ ] Final approval for open-source launch

---

## Part 7: Measurement Criteria

### Success Metrics

| Metric | Before | Target After | How to Measure |
|--------|--------|--------------|----------------|
| **Total public docs** | 331 files | 30-40 files | `find docs/ -name "*.md" \| wc -l` |
| **Time to first contribution** | Unknown | <30 minutes | New contributor survey |
| **"Deployment neutrality" mentions** | 15+ | 0 | `grep -r "deployment neutrality" docs/` |
| **Enterprise infrastructure docs in public** | 40+ files | 0 files | Manual audit |
| **User confusion about installation** | Unknown | <5% | User feedback |
| **Architecture doc page count** | 200+ pages | <10 pages | Word count |

### Quality Gates

Before marking audit as complete:

- [ ] No internal architecture decisions in public docs
- [ ] Clear user choice: "Install locally" OR "Use cloud"
- [ ] No Kubernetes/Redis/enterprise infrastructure in public docs
- [ ] All links working (no broken references to moved docs)
- [ ] External beta tester can install and contribute in <30 minutes
- [ ] README.md under 500 lines (currently 640 lines)

---

## Part 8: Risk Mitigation

### Risk 1: Broken Links After Moving Documentation

**Mitigation**:
- Use automated link checker before and after moves
- Update cross-references systematically
- Create redirect/archive README files pointing to new locations
- Test all documentation links in CI/CD

### Risk 2: Loss of Valuable Context

**Mitigation**:
- Don't delete anything - move to internal/archive
- Maintain git history (use `git mv`, not copy/delete)
- Create index in internal repo pointing to original locations
- Keep "why we moved this" notes in commit messages

### Risk 3: User Confusion During Transition

**Mitigation**:
- Add deprecation notices to moved docs before deletion
- Create migration guide for existing users
- Announce changes in CHANGELOG and community channels
- Provide 30-day notice before removing any public documentation

---

## Conclusion

This audit reveals that the `faultmaven` public repository contains **too much internal engineering documentation** (54%) and **too little user-facing guidance**. The core issue is mixing three distinct audiences:

1. **Open-source users** - Want: "How do I install and use FaultMaven?"
2. **Internal engineers** - Want: "How did we design this? Why these decisions?"
3. **Enterprise ops** - Want: "How do I deploy and operate at scale?"

### Recommended Immediate Actions

1. **Move 180 internal docs** to `faultmaven-doc-internal` (Week 1-2)
2. **Move 40 ops docs** to `faultmaven-enterprise-infra` (Week 2)
3. **Simplify README.md** - Remove deployment neutrality, add cloud signup (Week 2)
4. **Create 5-10 new user guides** - Use cases, plugins, troubleshooting (Week 3-4)
5. **Archive 26 obsolete docs** - Clean house (Week 5)

### Final Public Repository Vision

**Target**: 30-40 high-quality, user-focused documents organized by:
- **Getting started** (installation, cloud signup, quick start)
- **User guide** (API usage, features, workflows)
- **Development** (contributing, plugins, testing)
- **Troubleshooting** (common issues, user-level debugging)
- **Architecture** (simplified overview only, 3 pages)

**Users see**: "Install locally in 5 minutes" OR "Sign up for FaultMaven Cloud"
**Users don't see**: Deployment neutrality, OODA loops, enterprise infrastructure, internal architecture decisions

---

**Document Owner**: Tech Writing Team
**Review Date**: 2026-01-05
**Next Steps**: Review with leadership, approve action plan, begin Phase 1

---

## Appendix A: Files by Category (Full Inventory)

### PUBLIC - Keep (85 files)

**Root Level**:
- README.md (with edits)
- CHANGELOG.md
- LICENSE
- .env.example (simplified)

**Getting Started**:
- docs/QUICKSTART.md
- docs/getting-started/user-guide.md
- docs/CONTRIBUTING.md
- docs/CODE_OF_CONDUCT.md

**Development**:
- docs/development/how-to-add-providers.md
- docs/development/ENVIRONMENT_VARIABLES.md (simplified)
- docs/development/DATETIME_STANDARD.md
- docs/development/TOKEN_ESTIMATION.md
- docs/development/DATABASE_MIGRATIONS.md
- docs/development/performance-testing.md

**Testing**:
- docs/testing/new-test-patterns.md
- docs/testing/architecture-testing-guide.md
- docs/testing/REBUILT_TESTING_STANDARDS.md
- tests/README.md

**Tools & Features**:
- docs/tools/README.md
- docs/tools/developer-guide.md
- docs/tools/tool-catalog.md
- docs/tools/architecture/tool-interface-spec.md
- docs/tools/integrations/mcp-integration.md
- docs/features/phase-based-retrieval.md
- docs/features/case-evidence-store.md
- docs/features/platform-specific-extractors.md

**How-To (User-Focused)**:
- docs/how-to/testing-investigation-framework.md
- docs/how-to/knowledge-base-system.md

**Security (User-Facing)**:
- docs/security/role-based-access-control.md
- docs/security/client-protection.md

**Infrastructure (User-Facing)**:
- docs/infrastructure/Local-LLM-Setup.md

[... additional files ...]

---

### INTERNAL - Move to faultmaven-doc-internal (180 files)

**Architecture** (94 files):
- All files in docs/architecture/* (except new simplified overview)

**Logging** (7 files):
- docs/logging/*.md

**Security Implementation** (4 files):
- docs/security/implementation-guide.md
- docs/security/comprehensive-protection-implementation-guide.md
- docs/security/pii-sanitization-configuration.md
- docs/security/KNOWLEDGE_BASE_USER_SCOPING_ISSUE.md

**Infrastructure Internal** (3 files):
- docs/infrastructure/redis-architecture-guide.md
- docs/infrastructure/KB_METADATA_PERSISTENCE.md
- docs/infrastructure/opik-setup.md

**Schema** (all files):
- docs/schema/*.sql

**Planning**:
- docs/FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md
- ARCHITECTURE_ANALYSIS.md
- PR46_IMPLEMENTATION_PLAN.md

[... complete list ...]

---

### ENTERPRISE - Move to faultmaven-enterprise-infra (40 files)

**Runbooks**:
- docs/runbooks/kubernetes/*.md (15+ files)
- docs/runbooks/postgresql/*.md
- docs/runbooks/redis/*.md
- docs/runbooks/networking/*.md

[... complete list ...]

---

### ARCHIVE - Delete or Move to docs/archive/2025/ (26 files)

**Obsolete**:
- docs/recycle/*
- docs/architecture/archive/*
- docs/architecture/_temp/*
- docs/tools/planned/*

[... complete list ...]

---

**End of Audit Report**
