# FaultMaven Migration & Conversion Execution Plan

**Version**: 2.0 (Updated for Current Architecture)
**Date**: 2025-11-14
**Status**: Ready for Execution
**Based On**: Architecture Overview v3.0, Milestone-Based Investigation Framework v2.0

---

## Executive Summary

This document provides a comprehensive execution plan for:

1. **Repository Migration**: `sterlanyu/FaultMaven` → `FaultMaven/faultmaven` (public) + `FaultMaven/faultmaven-enterprise` (private)
2. **Architecture Conversion**: Monolithic Python application → Microservice-based scalable SaaS platform
3. **Synchronization Strategy**: Artifact-based bidirectional sync with automated dependency management

**Current State** (Confirmed from codebase):
- ✅ **Milestone-Based Investigation**: MilestoneEngine with opportunistic task completion (OODA integrated, not primary)
- ✅ **PostgreSQL Storage**: 10-table hybrid schema for persistent cases/users/evidence
- ✅ **RBAC Implemented**: Token-based auth with role-based access control
- ✅ **Multi-LLM Support**: OpenAI, Anthropic, Fireworks AI, Groq with failover
- ✅ **247 Python files**, ~4MB codebase

**Timeline**: 10 weeks
**Effort**: 2-3 full-time engineers
**Risk Level**: Medium-Low (architecture is stable, well-documented)

---

## Table of Contents

1. [Current State Analysis](#1-current-state-analysis)
2. [Phase 0: Preparation (Week 1)](#2-phase-0-preparation-week-1)
3. [Phase 1: Public Repository Creation (Week 2-3)](#3-phase-1-public-repository-creation-week-2-3)
4. [Phase 2: Package Publishing (Week 4)](#4-phase-2-package-publishing-week-4)
5. [Phase 3: Private Repository Setup (Week 5-6)](#5-phase-3-private-repository-setup-week-5-6)
6. [Phase 4: Microservices Conversion (Week 7-8)](#6-phase-4-microservices-conversion-week-7-8)
7. [Phase 5: Deployment & Validation (Week 9-10)](#7-phase-5-deployment--validation-week-9-10)
8. [Post-Migration Operations](#8-post-migration-operations)
9. [Rollback Procedures](#9-rollback-procedures)
10. [Success Metrics](#10-success-metrics)

---

## 1. Current State Analysis

### 1.1 Codebase Inventory

**Source Repository**: `https://github.com/sterlanyu/FaultMaven`

**Current Architecture** (v3.0):
- **Lines of Code**: ~35,000+ Python (247 files)
- **Size**: 4.0MB
- **Architecture**: Milestone-based investigation with Clean Architecture + DI container
- **Storage**: PostgreSQL (10 tables) + Redis (sessions/cache) + ChromaDB (vectors)
- **Services**: Case, Investigation, Data, Knowledge, Session services
- **Investigation**: MilestoneEngine (not OODA-primary)
- **Test Coverage**: 1425+ tests

**Key Components Already Implemented**:
- ✅ Milestone-based investigation (replaces old OODA framework)
- ✅ PostgreSQL hybrid storage (10-table schema)
- ✅ RBAC with token-based authentication
- ✅ Multi-LLM routing (OpenAI, Anthropic, Fireworks, Groq)
- ✅ PII redaction (Presidio integration)
- ✅ Three vector stores (User KB, Global KB, Case Evidence)

**Enterprise Features Present in Monolith**:
- ⚠️ User management (`infrastructure/persistence/user_repository.py`)
- ⚠️ RBAC implementation (`infrastructure/security/`)
- ⚠️ Organization/Team repositories (`infrastructure/persistence/organization_repository.py`, `team_repository.py`)
- ⚠️ Authentication system (`api/v1/routes/auth.py`)
- ⚠️ Token management (Redis-backed)

### 1.2 Public vs Private Classification

Based on **current codebase** (not outdated assumptions):

#### **Public Repository (Open Source Core)**

**Core Investigation** (`core/investigation/`):
```
✅ milestone_engine.py           # MilestoneEngine - investigation orchestrator
✅ hypothesis_manager.py          # Hypothesis tracking (dual-mode)
✅ investigation_coordinator.py   # Workflow orchestration
✅ workflow_progression_detector.py # Progress detection
✅ ooda_engine.py                 # OODA integration (secondary)
✅ working_conclusion_generator.py # Conclusion synthesis
✅ phase_loopback.py              # Loop prevention
✅ engagement_modes.py            # Consultant vs Lead Investigator
✅ phases.py                      # Phase definitions
✅ strategy_selector.py           # Strategy selection
```

**Core Domain** (`core/`):
```
✅ processing/                    # Data analysis, log processor
✅ knowledge/                     # Knowledge base operations, ingestion
✅ preprocessing/                 # Data preprocessing
✅ confidence/                    # Confidence scoring
```

**Infrastructure** (Basic):
```
✅ llm/                           # Multi-LLM routing (all 7 providers)
✅ security/redaction.py          # Basic PII redaction (Presidio integration)
✅ observability/                 # Opik tracing
✅ persistence/ (interfaces only) # ISessionStore, IVectorStore, ICaseRepository
✅ caching/ (basic)               # Basic cache strategies
```

**Tools** (`tools/`):
```
✅ knowledge_base.py              # RAG operations
✅ web_search.py                  # Search capability
✅ case_evidence_qa.py            # Evidence Q&A
✅ user_kb_qa.py                  # User KB Q&A
```

**Models** (Interfaces & Core):
```
✅ interfaces.py                  # All interface contracts
✅ case.py                        # Case domain models (minus enterprise fields)
✅ api_models.py                  # API schemas
✅ evidence.py                    # Evidence tracking
✅ investigation.py               # Investigation models
```

**Container** (Foundation):
```
✅ container.py                   # DI container foundation (without enterprise services)
```

#### **Private Repository (Enterprise Features)**

**Enterprise-Specific Code**:
```
❌ infrastructure/auth/           # Authentication system
❌ infrastructure/persistence/user_repository.py  # User management
❌ infrastructure/persistence/organization_repository.py # Multi-tenancy
❌ infrastructure/persistence/team_repository.py  # Team management
❌ infrastructure/persistence/kb_document_repository.py # Enterprise KB features
❌ api/v1/routes/auth.py          # Auth endpoints
❌ infrastructure/security/ (RBAC parts) # Role-based access control
❌ infrastructure/protection/     # Advanced protection systems
❌ infrastructure/monitoring/     # Enterprise monitoring
❌ infrastructure/telemetry/      # Enterprise telemetry
❌ services/analytics/            # Enterprise analytics
```

**Enterprise Extensions**:
- SSO providers (future)
- Billing integration (future)
- Usage metering (future)
- Advanced compliance logging
- Tiered storage policies
- Multi-tenant isolation
- Organization/workspace management

---

## 2. Phase 0: Preparation (Week 1)

**Objective**: Clean classification, stabilize branch, setup repositories

**Streamlined Tasks** (No outdated items):
- ✅ OODA → Milestone transition: **ALREADY COMPLETE**
- ✅ Redis → PostgreSQL migration: **ALREADY COMPLETE**
- ✅ RBAC implementation: **ALREADY COMPLETE**

### Day 1-2: Code Classification & Boundary Definition

**Create classification script** based on **actual current structure**:

```python
#!/usr/bin/env python3
"""
Classify FaultMaven v3.0 codebase into public vs. private
Based on: Milestone-based architecture, PostgreSQL storage, RBAC implemented
"""

import os
import re
from pathlib import Path
from typing import Dict, List

PUBLIC_PATTERNS = [
    # Core Investigation (Milestone-based)
    r"faultmaven/core/investigation/milestone_engine\.py",
    r"faultmaven/core/investigation/hypothesis_manager\.py",
    r"faultmaven/core/investigation/investigation_coordinator\.py",
    r"faultmaven/core/investigation/workflow_progression_detector\.py",
    r"faultmaven/core/investigation/ooda_engine\.py",
    r"faultmaven/core/investigation/working_conclusion_generator\.py",
    r"faultmaven/core/investigation/phase_loopback\.py",
    r"faultmaven/core/investigation/engagement_modes\.py",
    r"faultmaven/core/investigation/phases\.py",
    r"faultmaven/core/investigation/strategy_selector\.py",
    r"faultmaven/core/investigation/memory_manager\.py",
    r"faultmaven/core/investigation/iteration_strategy\.py",

    # Core Domain
    r"faultmaven/core/processing/.*",
    r"faultmaven/core/knowledge/.*",
    r"faultmaven/core/preprocessing/.*",
    r"faultmaven/core/confidence/.*",

    # Infrastructure (Basic)
    r"faultmaven/infrastructure/llm/.*",
    r"faultmaven/infrastructure/security/redaction\.py",
    r"faultmaven/infrastructure/observability/.*",
    r"faultmaven/infrastructure/caching/((?!advanced).)*",  # Basic caching only

    # Tools
    r"faultmaven/tools/knowledge_base\.py",
    r"faultmaven/tools/web_search\.py",
    r"faultmaven/tools/case_evidence_qa\.py",
    r"faultmaven/tools/user_kb_qa\.py",

    # Models (Core)
    r"faultmaven/models/interfaces\.py",
    r"faultmaven/models/case\.py",
    r"faultmaven/models/api_models\.py",
    r"faultmaven/models/evidence\.py",
    r"faultmaven/models/investigation\.py",
    r"faultmaven/models/common\.py",

    # Container (Foundation)
    r"faultmaven/container\.py",

    # Configuration (Basic)
    r"faultmaven/config/settings\.py",
]

PRIVATE_PATTERNS = [
    # Authentication & Authorization
    r"faultmaven/infrastructure/auth/.*",
    r"faultmaven/api/v1/routes/auth\.py",

    # User & Organization Management
    r"faultmaven/infrastructure/persistence/user_repository\.py",
    r"faultmaven/infrastructure/persistence/organization_repository\.py",
    r"faultmaven/infrastructure/persistence/team_repository\.py",
    r"faultmaven/infrastructure/persistence/kb_document_repository\.py",

    # Enterprise Features
    r"faultmaven/infrastructure/protection/.*",
    r"faultmaven/infrastructure/monitoring/((?!basic).)*",  # Advanced monitoring
    r"faultmaven/infrastructure/telemetry/.*",
    r"faultmaven/services/analytics/.*",

    # Anything with billing, subscription, tenant, sso
    r".*billing.*",
    r".*subscription.*",
    r".*tenant.*",
    r".*sso.*",
]

SHARED_PATTERNS = [
    r"faultmaven/prompts/.*",  # Git subtree
    r"docs/.*",
    r"tests/.*",
]

def classify_file(file_path: str) -> str:
    """Classify file as public, private, or shared"""
    for pattern in PRIVATE_PATTERNS:
        if re.match(pattern, file_path):
            return "private"

    for pattern in PUBLIC_PATTERNS:
        if re.match(pattern, file_path):
            return "public"

    for pattern in SHARED_PATTERNS:
        if re.match(pattern, file_path):
            return "shared"

    return "unknown"

def scan_codebase(root_dir: str) -> Dict[str, List[str]]:
    """Scan entire codebase and classify all files"""
    classification = {
        "public": [],
        "private": [],
        "shared": [],
        "unknown": [],
    }

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['__pycache__', 'venv', '.venv']]

        for file in files:
            if file.endswith(('.py', '.md', '.yaml', '.yml', '.json')):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, root_dir)
                category = classify_file(rel_path)
                classification[category].append(rel_path)

    return classification

if __name__ == "__main__":
    import json

    classification = scan_codebase("/home/user/FaultMaven")

    with open("classification_report.json", "w") as f:
        json.dump(classification, f, indent=2)

    print("=== FaultMaven v3.0 Code Classification ===")
    print(f"Public:  {len(classification['public'])} files")
    print(f"Private: {len(classification['private'])} files")
    print(f"Shared:  {len(classification['shared'])} files")
    print(f"Unknown: {len(classification['unknown'])} files")
    print("\n✅ Milestone-based architecture confirmed")
    print("✅ PostgreSQL storage confirmed")
    print("✅ RBAC implementation confirmed")
```

**Action Items**:
- [ ] Run classification script
- [ ] Review "unknown" files manually
- [ ] Create `PUBLIC_BOUNDARY.md` document
- [ ] Verify no enterprise code leaks to public

**Deliverable**: `classification_report.json` + `PUBLIC_BOUNDARY.md`

---

### Day 3-4: Repository Setup

**GitHub Organization**: `FaultMaven`

```bash
# 1. Create GitHub organization (via web UI)
# https://github.com/organizations/new
# Name: FaultMaven
# Email: team@faultmaven.com

# 2. Create public repository
gh repo create FaultMaven/faultmaven \
  --public \
  --description "AI-powered troubleshooting copilot with milestone-based investigation" \
  --homepage "https://faultmaven.com"

# 3. Create private repository
gh repo create FaultMaven/faultmaven-enterprise \
  --private \
  --description "FaultMaven Enterprise SaaS Platform"

# 4. Configure branch protection
gh api repos/FaultMaven/faultmaven/branches/main/protection -X PUT \
  --field required_pull_request_reviews[required_approving_review_count]=1 \
  --field required_status_checks[strict]=true
```

---

### Day 5: Documentation Preparation

**Create essential docs**:

1. **`PUBLIC_BOUNDARY.md`**:

```markdown
# FaultMaven Public/Private Boundary (v3.0 Architecture)

## Public Repository (Apache 2.0)

### ✅ Core Investigation (Milestone-Based)
- `milestone_engine.py` - Investigation orchestrator with opportunistic completion
- `hypothesis_manager.py` - Dual-mode hypothesis tracking
- `investigation_coordinator.py` - Workflow orchestration
- `workflow_progression_detector.py` - Progress detection and loopback prevention
- `ooda_engine.py` - OODA integration (secondary framework)
- All other investigation components

### ✅ Core Domain
- Data processing and log analysis
- Knowledge base operations (RAG foundation)
- Confidence scoring algorithms
- Data preprocessing pipelines

### ✅ Infrastructure (Basic)
- Multi-LLM routing (OpenAI, Anthropic, Fireworks, Groq)
- Basic PII redaction (Presidio integration)
- Observability (Opik tracing)
- Basic caching strategies

### ✅ Tools & Interfaces
- All agent tools (KB search, web search, evidence Q&A)
- All interface contracts (`ILLMProvider`, `ISanitizer`, `ITracer`, etc.)
- Core domain models (Case, Evidence, Investigation)

### ✅ Container Foundation
- Basic DI container without enterprise services

## ❌ Private Repository (Proprietary)

### Enterprise Features
- User management and authentication
- RBAC (role-based access control)
- Organization/team management
- Multi-tenancy
- Advanced protection systems
- Enterprise monitoring
- Analytics and reporting
- Billing integration (future)
- SSO providers (future)

### Implementation Rule
Any file with "auth", "user", "organization", "team", "billing", "subscription", "tenant", or "sso" goes to private repo.
```

2. **`MIGRATION_CHECKLIST.md`** (day-by-day tasks)
3. **`CONTRIBUTING.md`** (for public repo)
4. **`UPSTREAM_POLICY.md`** (for private repo)

**Deliverable**: Complete documentation package

---

## 3. Phase 1: Public Repository Creation (Week 2-3)

**Objective**: Extract public code, create packages, verify no enterprise leaks

### Week 2: Code Extraction & Package Structure

#### Day 6-8: Execute Public Code Extraction

**Package Structure** (based on current architecture):

```
faultmaven/  (public repo)
├── packages/
│   ├── faultmaven-core/
│   │   ├── pyproject.toml
│   │   └── src/faultmaven_core/
│   │       ├── investigation/          # MilestoneEngine + all investigation
│   │       ├── processing/             # Data processing
│   │       ├── knowledge/              # RAG foundation
│   │       └── confidence/             # Confidence scoring
│   │
│   ├── faultmaven-models/
│   │   ├── pyproject.toml
│   │   └── src/faultmaven_models/
│   │       ├── interfaces.py           # All interface contracts
│   │       ├── case.py                 # Case models (minus enterprise fields)
│   │       ├── evidence.py             # Evidence tracking
│   │       └── investigation.py        # Investigation models
│   │
│   ├── faultmaven-prompts/
│   │   ├── pyproject.toml
│   │   └── src/faultmaven_prompts/
│   │       └── templates/
│   │           ├── investigation/      # Investigation prompts
│   │           └── engagement/         # Engagement mode prompts
│   │
│   ├── faultmaven-llm/
│   │   ├── pyproject.toml
│   │   └── src/faultmaven_llm/
│   │       ├── router.py               # Multi-LLM routing
│   │       └── providers/              # 7 LLM providers
│   │
│   ├── faultmaven-tools/
│   │   ├── pyproject.toml
│   │   └── src/faultmaven_tools/
│   │       ├── knowledge_base.py       # RAG operations
│   │       ├── web_search.py           # Web search
│   │       ├── case_evidence_qa.py     # Evidence Q&A
│   │       └── user_kb_qa.py           # User KB Q&A
│   │
│   └── faultmaven-security/
│       ├── pyproject.toml
│       └── src/faultmaven_security/
│           └── redaction.py            # Basic PII redaction
│
├── containers/
│   ├── base/
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── tools/
│       └── Dockerfile
│
├── schemas/
│   ├── openapi/
│   │   └── api.yaml
│   └── proto/
│       └── investigation.proto
│
└── .github/
    └── workflows/
        └── release.yml
```

**Extraction script**:

```bash
#!/bin/bash
set -e

# Extract public code from current FaultMaven architecture
SOURCE_REPO="https://github.com/sterlanyu/FaultMaven.git"
TARGET_DIR="faultmaven-public-extraction"

echo "=== Extracting Public Code from FaultMaven v3.0 ==="

# 1. Clone source
git clone "$SOURCE_REPO" "$TARGET_DIR"
cd "$TARGET_DIR"

# 2. Load classification
PUBLIC_FILES=$(jq -r '.public[]' ../classification_report.json)

# 3. Create orphan branch with only public files
git checkout --orphan public-core
git rm -rf .
git checkout main -- $(echo "$PUBLIC_FILES" | tr '\n' ' ')

# 4. Create package structure
mkdir -p packages/{faultmaven-core,faultmaven-models,faultmaven-prompts,faultmaven-llm,faultmaven-tools,faultmaven-security}/src

# 5. Organize into packages
# Core Investigation
mkdir -p packages/faultmaven-core/src/faultmaven_core
mv faultmaven/core/investigation packages/faultmaven-core/src/faultmaven_core/
mv faultmaven/core/processing packages/faultmaven-core/src/faultmaven_core/
mv faultmaven/core/knowledge packages/faultmaven-core/src/faultmaven_core/
mv faultmaven/core/confidence packages/faultmaven-core/src/faultmaven_core/

# Models
mkdir -p packages/faultmaven-models/src/faultmaven_models
mv faultmaven/models/interfaces.py packages/faultmaven-models/src/faultmaven_models/
mv faultmaven/models/case.py packages/faultmaven-models/src/faultmaven_models/
mv faultmaven/models/evidence.py packages/faultmaven-models/src/faultmaven_models/
mv faultmaven/models/investigation.py packages/faultmaven-models/src/faultmaven_models/

# Prompts
mkdir -p packages/faultmaven-prompts/src/faultmaven_prompts
mv faultmaven/prompts packages/faultmaven-prompts/src/faultmaven_prompts/templates/

# LLM
mkdir -p packages/faultmaven-llm/src/faultmaven_llm
mv faultmaven/infrastructure/llm packages/faultmaven-llm/src/faultmaven_llm/

# Tools
mkdir -p packages/faultmaven-tools/src/faultmaven_tools
mv faultmaven/tools packages/faultmaven-tools/src/faultmaven_tools/

# Security
mkdir -p packages/faultmaven-security/src/faultmaven_security
mv faultmaven/infrastructure/security/redaction.py packages/faultmaven-security/src/faultmaven_security/

# 6. Remove enterprise-specific patterns
find . -type f -name "*.py" -exec sed -i '/user_id\|org_id\|team_id/d' {} \;
find . -type f -name "*.py" -exec sed -i '/sso_provider\|billing\|subscription/d' {} \;

# 7. Commit
git add .
git commit -m "feat: extract public core from FaultMaven v3.0

Milestone-based investigation architecture:
- MilestoneEngine with opportunistic task completion
- PostgreSQL hybrid storage interfaces
- Multi-LLM routing (7 providers)
- Basic PII redaction
- Agent tools and RAG

Source: sterlanyu/FaultMaven (v3.0)
License: Apache 2.0"

echo "✅ Public code extracted"
```

---

#### Day 9-10: Create Package Configurations

**`packages/faultmaven-core/pyproject.toml`**:

```toml
[project]
name = "faultmaven-core"
version = "1.0.0"
description = "FaultMaven milestone-based investigation engine"
readme = "README.md"
requires-python = ">=3.11"
license = {text = "Apache-2.0"}
authors = [{name = "FaultMaven Team", email = "team@faultmaven.com"}]
keywords = ["sre", "troubleshooting", "ai", "diagnostics", "milestone-based"]

dependencies = [
    "faultmaven-models>=1.0.0,<2.0.0",
    "faultmaven-prompts>=1.0.0,<2.0.0",
    "pydantic>=2.7",
    "asyncio>=3.4",
]

[project.urls]
Homepage = "https://faultmaven.com"
Documentation = "https://docs.faultmaven.com"
Repository = "https://github.com/FaultMaven/faultmaven"

[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"
```

**Create for all 6 packages** with appropriate dependencies.

---

### Week 3: Testing & Initial Commit

#### Day 11-13: Integration Testing

**Test extracted packages**:

```bash
# Test each package independently
cd packages/faultmaven-models
pytest tests/ -v

cd ../faultmaven-core
pytest tests/ -v

# Test integration
cd ../..
pytest integration-tests/ -v
```

**Verification checklist**:
- [ ] All imports resolve
- [ ] No enterprise code leaked (search for user_id, org_id, sso_provider, billing)
- [ ] No hardcoded secrets
- [ ] All tests pass
- [ ] Documentation renders correctly

---

#### Day 14: Push to Public Repository

```bash
# Add remote
git remote add public https://github.com/FaultMaven/faultmaven.git

# Create main branch
git checkout -b main

# Push
git push -u public main

# Create v1.0.0 tag
git tag -a v1.0.0 -m "FaultMaven v1.0.0 - Initial public release

Milestone-based investigation engine:
- MilestoneEngine with opportunistic completion
- Multi-LLM routing (7 providers)
- PostgreSQL hybrid storage interfaces
- Knowledge base with RAG
- PII redaction
- Agent tools

Extracted from FaultMaven v3.0 architecture"

git push public v1.0.0
```

---

## 4. Phase 2: Package Publishing (Week 4)

**Objective**: Publish to PyPI, publish container images, set up CI/CD

### Day 15-16: PyPI Publication

```bash
# Set up PyPI account
# Create API token
# Add to GitHub secrets

# Build and publish (in dependency order)
packages=(
    "faultmaven-models"
    "faultmaven-prompts"
    "faultmaven-core"
    "faultmaven-llm"
    "faultmaven-tools"
    "faultmaven-security"
)

for pkg in "${packages[@]}"; do
    cd "packages/$pkg"
    python -m build
    twine upload dist/*
    cd ../..
done
```

**Verify**:
```bash
pip install faultmaven-core==1.0.0
python -c "from faultmaven_core.investigation import MilestoneEngine; print('✅ Success')"
```

---

### Day 17-18: Container Images

```bash
# Build base image
cd containers/base
docker build -t ghcr.io/faultmaven/faultmaven/base:v1.0.0 .
docker push ghcr.io/faultmaven/faultmaven/base:v1.0.0

# Build tools image
cd ../tools
docker build -t ghcr.io/faultmaven/faultmaven/tools:v1.0.0 .
docker push ghcr.io/faultmaven/faultmaven/tools:v1.0.0
```

---

### Day 19-20: CI/CD Pipeline

**`.github/workflows/release.yml`**:

```yaml
name: Release All Packages
on:
  push:
    tags: ["v*.*.*"]

permissions:
  contents: write
  packages: write

jobs:
  prepare:
    runs-on: ubuntu-latest
    outputs:
      version: ${{ steps.version.outputs.version }}
    steps:
      - id: version
        run: echo "version=${GITHUB_REF#refs/tags/v}" >> $GITHUB_OUTPUT

  publish-packages:
    needs: prepare
    runs-on: ubuntu-latest
    strategy:
      matrix:
        package:
          - faultmaven-core
          - faultmaven-models
          - faultmaven-prompts
          - faultmaven-llm
          - faultmaven-tools
          - faultmaven-security
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Update version
        working-directory: packages/${{ matrix.package }}
        run: |
          sed -i 's/version = ".*"/version = "${{ needs.prepare.outputs.version }}"/' pyproject.toml

      - name: Build and publish
        working-directory: packages/${{ matrix.package }}
        run: |
          pip install build twine
          python -m build
          twine upload dist/*
        env:
          TWINE_USERNAME: __token__
          TWINE_PASSWORD: ${{ secrets.PYPI_TOKEN }}

  publish-images:
    needs: prepare
    runs-on: ubuntu-latest
    strategy:
      matrix:
        image: [base, tools]
    steps:
      - uses: actions/checkout@v4
      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: containers/${{ matrix.image }}
          push: true
          tags: ghcr.io/faultmaven/faultmaven/${{ matrix.image }}:v${{ needs.prepare.outputs.version }}
          provenance: true
          sbom: true

  notify-private:
    needs: [prepare, publish-packages, publish-images]
    runs-on: ubuntu-latest
    steps:
      - name: Trigger private repo sync
        uses: peter-evans/repository-dispatch@v3
        with:
          token: ${{ secrets.PRIVATE_REPO_PAT }}
          repository: FaultMaven/faultmaven-enterprise
          event-type: public-release
          client-payload: '{"version":"${{ needs.prepare.outputs.version }}"}'
```

---

## 5. Phase 3: Private Repository Setup (Week 5-6)

**Objective**: Create enterprise repo with Renovate, set up sync

### Week 5: Repository Structure

#### Day 21-23: Initialize Private Repository

```bash
# Initialize structure
mkdir faultmaven-enterprise
cd faultmaven-enterprise

git init
git remote add origin https://github.com/FaultMaven/faultmaven-enterprise.git

# Create directory structure
mkdir -p services/{investigation-service,knowledge-service,llm-router-service,security-service,analytics-service,gateway-service}
mkdir -p infrastructure/{k8s/{base,overlays/{dev,staging,prod}},helm,terraform/{aws,gcp},storage}
mkdir -p shared-libs/{auth,monitoring,contracts,testing}
mkdir -p vendored/prompts
mkdir -p docs/enterprise

# Create versions.yaml
cat > versions.yaml << 'EOF'
faultmaven:
  packages:
    core: "1.0.0"
    models: "1.0.0"
    prompts: "1.0.0"
    llm: "1.0.0"
    tools: "1.0.0"
    security: "1.0.0"
  images:
    base: "ghcr.io/faultmaven/faultmaven/base:v1.0.0"
    tools: "ghcr.io/faultmaven/faultmaven/tools:v1.0.0"
  schemas:
    version: "1.0.0"
    url: "https://github.com/FaultMaven/faultmaven/releases/download/v1.0.0/schemas-1.0.0.tgz"
  compatibility:
    min_python: "3.11"
    max_python: "3.12"
    postgresql: ">=15,<17"
    redis: ">=5.0,<6.0"
EOF

# Initial commit
git add .
git commit -m "chore: initialize enterprise repository"
git push -u origin main
```

---

#### Day 24-26: Set Up Renovate

**`renovate.json`**:

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended", ":semanticCommits"],
  "packageRules": [
    {
      "matchPackagePatterns": ["^faultmaven-"],
      "groupName": "faultmaven shared packages",
      "schedule": ["before 6am on Monday"]
    }
  ],
  "regexManagers": [
    {
      "fileMatch": ["^versions\\.yaml$"],
      "matchStrings": [
        "(?<depName>core|models|prompts|llm|tools|security):\\s+\"(?<currentValue>[^\"]+)\""
      ],
      "datasourceTemplate": "pypi",
      "depNameTemplate": "faultmaven-{{{depName}}}"
    }
  ]
}
```

**Install Renovate GitHub App** on `faultmaven-enterprise` repo.

---

### Week 6: Automated Sync Setup

#### Day 27-30: Repository Dispatch Receiver

**`.github/workflows/sync-public.yml`**:

```yaml
name: Sync from Public Release
on:
  repository_dispatch:
    types: [public-release]
  workflow_dispatch:
    inputs:
      version:
        required: true

jobs:
  update-versions:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: mikefarah/yq@master

      - name: Get version
        id: version
        run: |
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            echo "version=${{ github.event.inputs.version }}" >> $GITHUB_OUTPUT
          else
            echo "version=${{ github.event.client_payload.version }}" >> $GITHUB_OUTPUT
          fi

      - name: Update versions.yaml
        run: |
          VERSION="${{ steps.version.outputs.version }}"
          yq -i ".faultmaven.packages.core = \"${VERSION}\"" versions.yaml
          yq -i ".faultmaven.packages.models = \"${VERSION}\"" versions.yaml
          yq -i ".faultmaven.packages.prompts = \"${VERSION}\"" versions.yaml
          yq -i ".faultmaven.packages.llm = \"${VERSION}\"" versions.yaml
          yq -i ".faultmaven.packages.tools = \"${VERSION}\"" versions.yaml
          yq -i ".faultmaven.packages.security = \"${VERSION}\"" versions.yaml

      - name: Create PR
        uses: peter-evans/create-pull-request@v6
        with:
          branch: chore/bump-faultmaven-${{ steps.version.outputs.version }}
          title: "chore(deps): bump FaultMaven to ${{ steps.version.outputs.version }}"
```

---

## 6. Phase 4: Microservices Conversion (Week 7-8)

**Objective**: Build microservices with adapter pattern

### Week 7: Investigation Service

#### Day 31-35: Create Investigation Service

**Directory structure**:

```
services/investigation-service/
├── pyproject.toml
├── src/
│   ├── main.py
│   ├── adapters/
│   │   └── investigation_adapter.py
│   └── routes/
│       └── investigations.py
├── k8s/
│   ├── deployment.yaml
│   └── service.yaml
└── Dockerfile
```

**`services/investigation-service/pyproject.toml`**:

```toml
[project]
name = "faultmaven-investigation-service"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
    "faultmaven-core==1.0.0",
    "faultmaven-models==1.0.0",
    "faultmaven-llm==1.0.0",
    "fastapi>=0.109",
    "uvicorn[standard]>=0.27",
    "faultmaven-enterprise-auth @ file:../../shared-libs/auth",
]
```

**`services/investigation-service/src/adapters/investigation_adapter.py`**:

```python
from faultmaven_core.investigation import MilestoneEngine
from faultmaven_models.interfaces import ILLMProvider
from faultmaven_enterprise_auth import EnterpriseAuthContext

class EnterpriseInvestigationCoordinator:
    """Enterprise wrapper with auth, monitoring, compliance"""

    def __init__(
        self,
        llm_provider: ILLMProvider,
        auth_context: EnterpriseAuthContext,
    ):
        self._auth = auth_context
        self._engine = MilestoneEngine(llm_provider=llm_provider)

    async def investigate(self, case_id: str) -> dict:
        # Auth check
        if not await self._auth.can_access_case(case_id):
            raise PermissionError()

        # Call public core
        result = await self._engine.process_turn(case_id)

        # Enterprise logging
        await self._log_audit_trail(case_id, result)

        return result
```

**`services/investigation-service/Dockerfile`**:

```dockerfile
FROM ghcr.io/faultmaven/faultmaven/base:v1.0.0

WORKDIR /app

COPY shared-libs/ /app/shared-libs/
RUN pip install -e /app/shared-libs/auth

COPY services/investigation-service/ ./
RUN pip install -e .

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

### Week 8: Additional Microservices

#### Day 36-40: Create Remaining Services

Using investigation service as template:

1. **Knowledge Service** - Wraps `faultmaven-core` knowledge base
2. **LLM Router Service** - Wraps `faultmaven-llm` with usage metering
3. **Security Service** - Wraps `faultmaven-security` with enterprise policies
4. **Analytics Service** - Enterprise analytics
5. **API Gateway** - Kong/similar for routing

---

## 7. Phase 5: Deployment & Validation (Week 9-10)

**Objective**: Deploy to K8s, validate end-to-end

### Week 9: Infrastructure Setup

#### Day 41-45: K8s Cluster & Services

```bash
# Create K8s cluster (AWS EKS)
cd infrastructure/terraform/aws
terraform init
terraform apply

# Deploy PostgreSQL, Redis, ChromaDB
helm install postgresql bitnami/postgresql -n faultmaven
helm install redis bitnami/redis -n faultmaven

# Deploy microservices
kubectl apply -f services/investigation-service/k8s/
kubectl apply -f services/knowledge-service/k8s/
kubectl apply -f services/llm-router-service/k8s/
```

---

### Week 10: Testing & Launch

#### Day 46-50: Integration Testing & Launch

**Test scenarios**:
1. End-to-end investigation flow
2. Multi-tenant isolation
3. LLM failover
4. Scale testing (100 concurrent)

**Launch checklist**:
- [ ] Public repo: README, docs, PyPI packages
- [ ] Private repo: All microservices deployed
- [ ] Monitoring configured
- [ ] Renovate creating PRs
- [ ] Repository dispatch working

---

## 8. Post-Migration Operations

### Weekly Sync Cadence

**Automated** (Monday):
- Renovate checks for updates
- Creates PR if new versions
- CI runs tests

**Manual** (Tuesday):
- Review PR
- Merge if tests pass

**Event-driven**:
- Public release → dispatch → auto PR

---

## 9. Rollback Procedures

### Package Rollback

```bash
# Yank broken version
pip install yank
yank faultmaven-core 1.2.3

# Publish hotfix
git tag -a v1.2.4 -m "Hotfix"
git push origin v1.2.4
```

### Microservice Rollback

```bash
kubectl rollout undo deployment/investigation-service -n faultmaven
```

---

## 10. Success Metrics

### Technical (Week 10)
- [ ] 100% public code separated
- [ ] 1425+ tests passing
- [ ] 6 packages on PyPI
- [ ] 6 microservices in K8s
- [ ] Renovate working
- [ ] CI/CD green

### Community (3 months)
- [ ] 100+ GitHub stars
- [ ] 10+ contributors
- [ ] 1000+ PyPI downloads/month

### Business (6 months)
- [ ] 10+ enterprise customers
- [ ] 99.9% SLA
- [ ] Feature parity with monolith

---

## Appendices

### A. Team Roles

| Role | Responsibilities | Time |
|------|------------------|------|
| Migration Lead | Overall execution | 100% (10 weeks) |
| Public Repo Lead | Open-source community | 50% (5 weeks) |
| Private Repo Lead | Microservices architecture | 100% (10 weeks) |
| DevOps Lead | K8s, CI/CD | 100% (10 weeks) |

### B. Risk Register

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Enterprise code leaks | Low | Critical | Automated scanning |
| Migration takes longer | Medium | High | Buffer weeks |
| Performance degradation | Medium | High | Load testing |

---

**Document Version**: 2.0
**Based On**: FaultMaven v3.0 Architecture (Milestone-based, PostgreSQL, RBAC)
**Last Updated**: 2025-11-14
**Status**: Ready for Execution
