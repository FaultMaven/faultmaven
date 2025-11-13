# FaultMaven Migration & Conversion Execution Plan

**Version**: 1.0
**Date**: 2025-11-13
**Author**: FaultMaven Team
**Status**: Ready for Execution

---

## Executive Summary

This document provides a comprehensive execution plan for:
1. **Repository Migration**: `sterlanyu/FaultMaven` → `FaultMaven/faultmaven` (public) + `FaultMaven/faultmaven-enterprise` (private)
2. **Architecture Conversion**: Monolithic Python application → Microservice-based scalable SaaS platform
3. **Synchronization Strategy**: Artifact-based bidirectional sync with automated dependency management

**Timeline**: 12 weeks
**Effort**: 2-3 full-time engineers
**Risk Level**: Medium (architecture is ready, execution is structured)

---

## Table of Contents

1. [Pre-Migration Assessment](#1-pre-migration-assessment)
2. [Phase 0: Preparation (Week 1-2)](#2-phase-0-preparation-week-1-2)
3. [Phase 1: Public Repository Creation (Week 3-4)](#3-phase-1-public-repository-creation-week-3-4)
4. [Phase 2: Package Publishing (Week 5-6)](#4-phase-2-package-publishing-week-5-6)
5. [Phase 3: Private Repository Setup (Week 7-8)](#5-phase-3-private-repository-setup-week-7-8)
6. [Phase 4: Microservices Conversion (Week 9-10)](#6-phase-4-microservices-conversion-week-9-10)
7. [Phase 5: Deployment & Validation (Week 11-12)](#7-phase-5-deployment--validation-week-11-12)
8. [Post-Migration Operations](#8-post-migration-operations)
9. [Rollback Procedures](#9-rollback-procedures)
10. [Success Metrics](#10-success-metrics)

---

## 1. Pre-Migration Assessment

### 1.1 Current State Inventory

**Source Repository**: `https://github.com/sterlanyu/FaultMaven`

**Codebase Statistics**:
- **Lines of Code**: ~25,000+ Python
- **Test Coverage**: 71% (1425+ tests)
- **Architecture**: Clean Architecture with DI container
- **Services**: 14 service classes across 4 layers
- **Dependencies**: 7 LLM providers, Redis, PostgreSQL, ChromaDB, Presidio

**Enterprise Code Already Present**:
- ✅ User management with SSO (`infrastructure/persistence/user_repository.py`)
- ✅ RBAC implementation
- ✅ Multi-tenant architecture foundations
- ✅ Authentication system

### 1.2 Destination Repositories

**Public Repository**: `https://github.com/FaultMaven/faultmaven`
- **Purpose**: Open-source core engine
- **License**: Apache 2.0
- **Visibility**: Public
- **Content**: Core algorithms, interfaces, tools, basic infrastructure

**Private Repository**: `https://github.com/FaultMaven/faultmaven-enterprise`
- **Purpose**: Enterprise SaaS platform
- **License**: Proprietary
- **Visibility**: Private
- **Content**: Microservices, enterprise features, K8s infrastructure, tiered storage

### 1.3 Migration Goals

| Goal | Success Criteria |
|------|------------------|
| **Clean Separation** | No enterprise code in public repo, no accidental IP exposure |
| **Bidirectional Sync** | Public improvements flow to private automatically via Renovate |
| **Minimal Disruption** | Current development continues with <2 week slowdown |
| **Production Ready** | Microservices deployed to K8s within 12 weeks |
| **Community Launch** | Public repo achieves 100+ GitHub stars in first 3 months |

---

## 2. Phase 0: Preparation (Week 1-2)

**Objective**: Stabilize codebase, classify all code, complete in-flight work

### Week 1: Audit & Classification

#### Day 1-2: Automated Code Classification

**Script**: `scripts/classify_codebase.py`

```python
#!/usr/bin/env python3
"""
Classify FaultMaven codebase into public vs. private components
Output: classification_report.json
"""

import os
import re
from pathlib import Path
from typing import Dict, List

# Classification rules
PUBLIC_PATTERNS = [
    r"faultmaven/core/investigation/.*",
    r"faultmaven/core/processing/.*",
    r"faultmaven/core/knowledge/.*",
    r"faultmaven/core/confidence/.*",
    r"faultmaven/infrastructure/llm/.*",
    r"faultmaven/infrastructure/security/redaction\.py",
    r"faultmaven/models/(interfaces|case|session|api_models)\.py",
    r"faultmaven/tools/.*",
    r"faultmaven/container\.py",
]

PRIVATE_PATTERNS = [
    r"faultmaven/infrastructure/auth/.*",
    r"faultmaven/infrastructure/persistence/user_repository\.py",
    r"faultmaven/api/v1/routes/auth\.py",
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
    """Classify a file as public, private, or shared"""
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
        # Skip hidden, cache, and build directories
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['__pycache__', 'venv', '.venv', 'node_modules']]

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

    # Write report
    with open("classification_report.json", "w") as f:
        json.dump(classification, f, indent=2)

    # Print summary
    print("=== FaultMaven Code Classification ===")
    print(f"Public:  {len(classification['public'])} files")
    print(f"Private: {len(classification['private'])} files")
    print(f"Shared:  {len(classification['shared'])} files")
    print(f"Unknown: {len(classification['unknown'])} files")
    print("\nUnknown files require manual review:")
    for f in classification['unknown'][:20]:
        print(f"  - {f}")
```

**Action Items**:
- [ ] Run classification script
- [ ] Review all "unknown" files manually
- [ ] Update classification rules based on review
- [ ] Generate final `PUBLIC_BOUNDARY.md` document

**Deliverable**: `classification_report.json` + `PUBLIC_BOUNDARY.md`

---

#### Day 3-4: Complete In-Flight Work

**Objective**: Finish all active features to create a clean breakpoint

**Tasks**:
- [ ] Complete OODA Framework v3.2.0 integration (check `git log` for status)
- [ ] Finalize Redis→PostgreSQL migration
- [ ] Resolve 28 TODO/FIXME items in codebase
- [ ] Merge all open PRs or close them

**Command**:
```bash
# Find all TODOs
grep -r "TODO\|FIXME\|XXX\|HACK" faultmaven/ > todos.txt

# Review open PRs
gh pr list --state open

# Check migration status
grep -r "redis\|Redis" faultmaven/ | grep -i "TODO\|migrate"
```

**Deliverable**: Clean main branch with no in-flight features

---

#### Day 5: Stakeholder Alignment

**Meeting Agenda**:
1. Present migration plan (this document)
2. Review timeline and resource allocation
3. Discuss risk mitigation
4. Get sign-off on public/private boundaries
5. Assign team roles

**Team Roles**:
- **Migration Lead**: Owns overall execution
- **Public Repo Lead**: Manages open-source community
- **Private Repo Lead**: Manages enterprise platform
- **DevOps Lead**: K8s infrastructure and CI/CD
- **QA Lead**: Integration testing and validation

**Deliverable**: Signed-off migration plan with assigned owners

---

### Week 2: Repository Setup & Documentation

#### Day 6-7: Create GitHub Organization & Repositories

**GitHub Organization**: `FaultMaven`

**Actions**:
```bash
# 1. Create GitHub organization (via web UI)
# https://github.com/organizations/new
# Name: FaultMaven
# Email: team@faultmaven.com
# Plan: Free (for public) + Teams (for private)

# 2. Create public repository
gh repo create FaultMaven/faultmaven \
  --public \
  --description "AI-powered troubleshooting copilot for SRE teams" \
  --homepage "https://faultmaven.com" \
  --enable-issues \
  --enable-wiki=false

# 3. Create private repository
gh repo create FaultMaven/faultmaven-enterprise \
  --private \
  --description "FaultMaven Enterprise SaaS Platform" \
  --enable-issues \
  --enable-wiki=false

# 4. Set up team permissions
gh api orgs/FaultMaven/teams -f name="Core Team" -f privacy="closed"
gh api orgs/FaultMaven/teams/core-team/repos/FaultMaven/faultmaven -X PUT -f permission="admin"
gh api orgs/FaultMaven/teams/core-team/repos/FaultMaven/faultmaven-enterprise -X PUT -f permission="admin"
```

**Repository Settings**:

Public (`FaultMaven/faultmaven`):
- [x] Issues enabled
- [x] Projects disabled (use GitHub Projects at org level)
- [x] Wiki disabled (use docs/ folder)
- [x] Discussions enabled (community support)
- [x] Branch protection on `main` (require PR reviews, CI passing)
- [x] CODEOWNERS file for automatic review assignments

Private (`FaultMaven/faultmaven-enterprise`):
- [x] Issues enabled
- [x] Require signed commits
- [x] Branch protection on `main` (require 2 approvals, CI passing)
- [x] CODEOWNERS file

**Deliverable**: Empty repositories ready for migration

---

#### Day 8-9: Prepare Migration Scripts

**Script 1**: `scripts/extract_public_code.sh`

```bash
#!/bin/bash
set -e

# Extract public code from sterlanyu/FaultMaven to FaultMaven/faultmaven
# Preserves git history for public files only

SOURCE_REPO="https://github.com/sterlanyu/FaultMaven.git"
TARGET_DIR="faultmaven-public-extraction"

echo "=== Extracting Public Code from FaultMaven ==="

# 1. Clone source repository
git clone "$SOURCE_REPO" "$TARGET_DIR"
cd "$TARGET_DIR"

# 2. Load classification
PUBLIC_FILES=$(jq -r '.public[]' ../classification_report.json)

# 3. Create a new orphan branch with only public files
git checkout --orphan public-core
git rm -rf .

# 4. Cherry-pick public files from main
git checkout main -- $(echo "$PUBLIC_FILES" | tr '\n' ' ')

# 5. Reorganize into packages structure
mkdir -p packages/faultmaven-core/src
mkdir -p packages/faultmaven-models/src
mkdir -p packages/faultmaven-prompts/src
mkdir -p packages/faultmaven-llm/src
mkdir -p packages/faultmaven-tools/src
mkdir -p packages/faultmaven-security/src

# Move files to appropriate packages
mv faultmaven/core/investigation packages/faultmaven-core/src/faultmaven_core/
mv faultmaven/core/processing packages/faultmaven-core/src/faultmaven_core/
mv faultmaven/core/knowledge packages/faultmaven-core/src/faultmaven_core/
mv faultmaven/core/confidence packages/faultmaven-core/src/faultmaven_core/

mv faultmaven/models/interfaces.py packages/faultmaven-models/src/faultmaven_models/
mv faultmaven/models/case.py packages/faultmaven-models/src/faultmaven_models/
mv faultmaven/models/session.py packages/faultmaven-models/src/faultmaven_models/

mv faultmaven/prompts packages/faultmaven-prompts/src/faultmaven_prompts/templates/

mv faultmaven/infrastructure/llm packages/faultmaven-llm/src/faultmaven_llm/

mv faultmaven/tools packages/faultmaven-tools/src/faultmaven_tools/

mv faultmaven/infrastructure/security/redaction.py packages/faultmaven-security/src/faultmaven_security/

# 6. Remove enterprise-specific code patterns
find . -type f -name "*.py" -exec sed -i '/sso_provider/d' {} \;
find . -type f -name "*.py" -exec sed -i '/billing/d' {} \;
find . -type f -name "*.py" -exec sed -i '/subscription/d' {} \;

# 7. Commit
git add .
git commit -m "feat: extract public core from FaultMaven monolith

Extracted components:
- Core investigation engine
- Data processing
- Knowledge base
- LLM routing
- Agent tools
- Basic security (PII redaction)

Source: sterlanyu/FaultMaven
License: Apache 2.0"

echo "✅ Public code extracted to branch 'public-core'"
echo "Next: Push to FaultMaven/faultmaven"
```

**Script 2**: `scripts/setup_private_repo.sh`

```bash
#!/bin/bash
set -e

# Initialize private enterprise repository structure

REPO_DIR="faultmaven-enterprise"

echo "=== Setting up FaultMaven Enterprise Repository ==="

mkdir -p "$REPO_DIR"
cd "$REPO_DIR"

git init
git remote add origin https://github.com/FaultMaven/faultmaven-enterprise.git

# Create directory structure
mkdir -p services/{investigation-service,knowledge-service,llm-router-service,security-service,analytics-service,gateway-service}
mkdir -p infrastructure/{k8s/{base,overlays/{dev,staging,prod}},helm,terraform/{aws,gcp,azure},storage}
mkdir -p shared-libs/{auth,monitoring,contracts,testing}
mkdir -p vendored/prompts
mkdir -p docs/enterprise

# Create versions.yaml
cat > versions.yaml << 'EOF'
# FaultMaven Shared Artifacts Versions
# Managed by Renovate and repository_dispatch

faultmaven:
  # Python packages
  packages:
    core: "1.0.0"
    models: "1.0.0"
    prompts: "1.0.0"
    llm: "1.0.0"
    tools: "1.0.0"
    security: "1.0.0"

  # Container images
  images:
    base: "ghcr.io/faultmaven/faultmaven/base:v1.0.0"
    tools: "ghcr.io/faultmaven/faultmaven/tools:v1.0.0"

  # Schemas and contracts
  schemas:
    version: "1.0.0"
    url: "https://github.com/FaultMaven/faultmaven/releases/download/v1.0.0/schemas-1.0.0.tgz"

  # Compatibility matrix
  compatibility:
    min_python: "3.11"
    max_python: "3.12"
    redis: ">=5.0,<6.0"
    postgresql: ">=15,<17"
EOF

# Create Renovate config
cat > renovate.json << 'EOF'
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended", ":semanticCommits"],
  "packageRules": [
    {
      "matchPackagePatterns": ["^faultmaven-"],
      "groupName": "faultmaven shared packages",
      "schedule": ["before 6am on Monday"]
    }
  ]
}
EOF

# Create README
cat > README.md << 'EOF'
# FaultMaven Enterprise Platform

**Private Repository** - Enterprise SaaS platform built on FaultMaven open-source core.

## Architecture

Microservices-based architecture consuming public FaultMaven packages:
- Investigation Service
- Knowledge Service
- LLM Router Service
- Security Service
- Analytics Service
- API Gateway

## Quick Start

See `docs/enterprise/getting-started.md`
EOF

git add .
git commit -m "chore: initialize enterprise repository structure"

echo "✅ Enterprise repository initialized"
```

**Deliverable**: Tested migration scripts ready for execution

---

#### Day 10: Documentation Preparation

**Documents to Create**:

1. **`PUBLIC_BOUNDARY.md`** (detailed classification rules)
2. **`MIGRATION_CHECKLIST.md`** (day-by-day tasks)
3. **`CONTRIBUTING.md`** (for public repo)
4. **`UPSTREAM_POLICY.md`** (for private repo)
5. **`ARCHITECTURE.md`** (microservices design)

**Template: `PUBLIC_BOUNDARY.md`**:

```markdown
# FaultMaven Public/Private Boundary Definition

This document defines what code belongs in the public open-source repository vs. the private enterprise platform.

## Public Repository (Apache 2.0)

### ✅ Include

**Core Algorithms**:
- Investigation engine (OODA loop, milestone detection, phases)
- Data processing (log analysis, pattern learning)
- Knowledge base (RAG, ingestion, retrieval)
- Confidence scoring

**Infrastructure**:
- Multi-LLM routing (7 providers)
- Basic PII redaction (Presidio integration)
- Observability (Opik integration)
- Caching strategies

**Interfaces & Models**:
- All interface contracts (`ILLMProvider`, `ISanitizer`, `ITracer`, `BaseTool`)
- Domain models (Case, Session, Evidence)
- API models (request/response schemas)

**Tools**:
- Knowledge base tool
- Web search tool
- Evidence analysis tool

**Utilities**:
- DI container foundation
- Configuration management
- Basic logging

### ❌ Exclude (Keep Private)

**Enterprise Features**:
- User management (SSO, SAML, OAuth)
- RBAC (roles, permissions, policies)
- Multi-tenancy (organizations, workspaces)
- Billing & subscriptions
- Usage metering
- Advanced analytics

**Infrastructure**:
- K8s manifests
- Helm charts
- Terraform configurations
- Advanced monitoring (SLA tracking, alerting)
- Tiered storage (hot/warm/cold)
- Advanced protection (ML-based anomaly detection)

**Integrations**:
- Enterprise SSO providers (Okta, Azure AD)
- Payment gateways (Stripe, etc.)
- Enterprise logging (Datadog, Splunk)

## Review Process

All files must be classified before migration. Uncertain cases escalate to Migration Lead.
```

**Deliverable**: Complete documentation package

---

## 3. Phase 1: Public Repository Creation (Week 3-4)

**Objective**: Extract public code, create package structure, publish v1.0.0

### Week 3: Code Extraction & Restructuring

#### Day 11-12: Execute Public Code Extraction

```bash
# Execute extraction script
cd /path/to/migration
./scripts/extract_public_code.sh

# Verify extraction
cd faultmaven-public-extraction
ls -la packages/

# Expected structure:
# packages/
# ├── faultmaven-core/
# ├── faultmaven-models/
# ├── faultmaven-prompts/
# ├── faultmaven-llm/
# ├── faultmaven-tools/
# └── faultmaven-security/
```

**Manual Review**:
- [ ] Verify no enterprise code leaked (search for SSO, billing, tenant)
- [ ] Check all imports resolve correctly
- [ ] Verify no hardcoded secrets or API keys
- [ ] Review git history (ensure no sensitive commits)

**Clean History** (if needed):
```bash
# Remove sensitive commits from history
git filter-repo --path faultmaven/infrastructure/auth --invert-paths
git filter-repo --replace-text secrets.txt  # Replace any leaked secrets
```

---

#### Day 13-14: Create Package Configurations

**For each package**, create `pyproject.toml`:

**Example: `packages/faultmaven-core/pyproject.toml`**:

```toml
[project]
name = "faultmaven-core"
version = "1.0.0"
description = "FaultMaven core investigation and diagnostic engine"
readme = "README.md"
requires-python = ">=3.11"
license = {text = "Apache-2.0"}
authors = [
    {name = "FaultMaven Team", email = "team@faultmaven.com"}
]
keywords = ["sre", "troubleshooting", "ai", "diagnostics", "devops"]
classifiers = [
    "Development Status :: 4 - Beta",
    "Intended Audience :: Developers",
    "Intended Audience :: System Administrators",
    "License :: OSI Approved :: Apache Software License",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: System :: Monitoring",
    "Topic :: System :: Systems Administration",
]

dependencies = [
    "faultmaven-models>=1.0.0,<2.0.0",
    "faultmaven-prompts>=1.0.0,<2.0.0",
    "pydantic>=2.7",
    "redis>=5.0",
    "asyncio>=3.4",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov>=4.1",
    "pytest-asyncio>=0.23",
    "black>=24.0",
    "mypy>=1.8",
    "ruff>=0.1",
]

[project.urls]
Homepage = "https://faultmaven.com"
Documentation = "https://docs.faultmaven.com"
Repository = "https://github.com/FaultMaven/faultmaven"
Issues = "https://github.com/FaultMaven/faultmaven/issues"
Changelog = "https://github.com/FaultMaven/faultmaven/blob/main/CHANGELOG.md"

[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
asyncio_mode = "auto"

[tool.black]
line-length = 100
target-version = ["py311"]

[tool.mypy]
python_version = "3.11"
strict = true
warn_return_any = true
warn_unused_configs = true
```

**Create for all 6 packages**:
- [ ] faultmaven-core
- [ ] faultmaven-models
- [ ] faultmaven-prompts
- [ ] faultmaven-llm
- [ ] faultmaven-tools
- [ ] faultmaven-security

---

#### Day 15: Create Container Configurations

**`containers/base/Dockerfile`**:

```dockerfile
FROM python:3.11-slim

# Metadata
LABEL org.opencontainers.image.source="https://github.com/FaultMaven/faultmaven"
LABEL org.opencontainers.image.description="FaultMaven base container image"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Install FaultMaven packages (from PyPI after publication)
# For now, copy local packages
COPY packages/ /tmp/packages/

RUN pip install --no-cache-dir \
    /tmp/packages/faultmaven-models \
    /tmp/packages/faultmaven-prompts \
    /tmp/packages/faultmaven-core \
    /tmp/packages/faultmaven-llm \
    /tmp/packages/faultmaven-tools \
    /tmp/packages/faultmaven-security

# After PyPI publication, replace above with:
# RUN pip install --no-cache-dir \
#     faultmaven-core==1.0.0 \
#     faultmaven-models==1.0.0 \
#     faultmaven-prompts==1.0.0 \
#     faultmaven-llm==1.0.0 \
#     faultmaven-tools==1.0.0 \
#     faultmaven-security==1.0.0

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import faultmaven_core; print('healthy')"

# Default command (override in service containers)
CMD ["python"]
```

**`containers/tools/Dockerfile`** (lightweight, tools only):

```dockerfile
FROM python:3.11-alpine

LABEL org.opencontainers.image.source="https://github.com/FaultMaven/faultmaven"
LABEL org.opencontainers.image.description="FaultMaven tools container (lightweight)"

WORKDIR /app

RUN pip install --no-cache-dir \
    faultmaven-models==1.0.0 \
    faultmaven-tools==1.0.0

CMD ["python"]
```

---

### Week 4: Testing & Initial Commit

#### Day 16-17: Integration Testing

**Create test suite** for extracted packages:

```bash
# Create tests directory
mkdir -p packages/faultmaven-core/tests

# Run tests
cd packages/faultmaven-core
pytest tests/ -v --cov=src/faultmaven_core --cov-report=html

# Verify all tests pass
# Fix any import errors or missing dependencies
```

**Test each package independently**:
- [ ] faultmaven-models (unit tests)
- [ ] faultmaven-prompts (template loading)
- [ ] faultmaven-core (investigation engine)
- [ ] faultmaven-llm (provider integration)
- [ ] faultmaven-tools (tool execution)
- [ ] faultmaven-security (PII redaction)

---

#### Day 18-19: Push to Public Repository

```bash
# Add remote
cd faultmaven-public-extraction
git remote add public https://github.com/FaultMaven/faultmaven.git

# Create main branch
git checkout -b main

# Push
git push -u public main

# Create v1.0.0 tag
git tag -a v1.0.0 -m "FaultMaven v1.0.0 - Initial public release

First public release of FaultMaven core engine:
- Investigation engine with OODA framework
- Multi-LLM routing (7 providers)
- Knowledge base with RAG
- PII redaction
- Agent tools

Extracted from private monolith with clean architecture."

git push public v1.0.0
```

**Verification**:
- [ ] Repository visible at `https://github.com/FaultMaven/faultmaven`
- [ ] All files present
- [ ] No enterprise code leaked
- [ ] License file correct (Apache 2.0)
- [ ] README.md renders correctly

---

#### Day 20: Documentation & Community Setup

**Create essential docs**:

1. **`README.md`** (public-facing):
```markdown
# FaultMaven

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![PyPI](https://img.shields.io/pypi/v/faultmaven-core)](https://pypi.org/project/faultmaven-core/)

**AI-Powered Troubleshooting Copilot for SRE Teams**

FaultMaven is an open-source investigation engine that helps SRE and DevOps teams diagnose complex system issues using AI-powered analysis.

## Features

- 🧠 **Advanced Investigation Engine**: OODA-based diagnostic framework
- 🤖 **Multi-LLM Support**: 7 providers (Fireworks, OpenAI, Anthropic, Gemini, etc.)
- 📚 **Knowledge Base**: RAG-powered document search
- 🔒 **Privacy-First**: Built-in PII redaction
- 🛠️ **Extensible Tools**: Plugin system for custom capabilities

## Quick Start

```bash
pip install faultmaven-core faultmaven-models faultmaven-llm
```

See [Documentation](https://docs.faultmaven.com) for usage examples.

## Architecture

FaultMaven uses Clean Architecture with interface-based design:
- **Core**: Investigation engine, data processing
- **Infrastructure**: LLM routing, storage, security
- **Tools**: Agent capabilities (search, analysis)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)

## License

Apache 2.0 - See [LICENSE](LICENSE)
```

2. **`CONTRIBUTING.md`**
3. **`CODE_OF_CONDUCT.md`**
4. **`SECURITY.md`**

**Set up GitHub features**:
- [ ] Enable GitHub Discussions
- [ ] Create issue templates
- [ ] Add CODEOWNERS file
- [ ] Set up branch protection
- [ ] Configure GitHub Actions

---

## 4. Phase 2: Package Publishing (Week 5-6)

**Objective**: Publish packages to PyPI, container images to GHCR, set up CI/CD

### Week 5: PyPI Publication

#### Day 21-22: PyPI Account Setup

**Steps**:
```bash
# 1. Create PyPI account (https://pypi.org/account/register/)
# Use team email: team@faultmaven.com

# 2. Enable 2FA

# 3. Create API token
# Name: "FaultMaven GitHub Actions"
# Scope: Entire account (or per-project after first upload)

# 4. Add token to GitHub Secrets
gh secret set PYPI_TOKEN --body "pypi-..." --repo FaultMaven/faultmaven
```

---

#### Day 23-25: Build & Publish Packages

**Manual first publication**:

```bash
# Install build tools
pip install build twine

# Build each package
cd packages/faultmaven-models
python -m build
twine check dist/*
twine upload dist/*

# Repeat for all 6 packages (in dependency order):
# 1. faultmaven-models (no dependencies)
# 2. faultmaven-prompts (no dependencies)
# 3. faultmaven-core (depends on models, prompts)
# 4. faultmaven-llm (depends on models)
# 5. faultmaven-tools (depends on models)
# 6. faultmaven-security (depends on models)
```

**Verify publication**:
```bash
# Test installation from PyPI
python -m venv test-venv
source test-venv/bin/activate
pip install faultmaven-core==1.0.0
python -c "import faultmaven_core; print('✅ Success')"
```

---

### Week 6: Container Images & CI/CD

#### Day 26-27: Publish Container Images

**Build and push**:

```bash
# Login to GHCR
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin

# Build base image
cd containers/base
docker build -t ghcr.io/faultmaven/faultmaven/base:v1.0.0 .
docker push ghcr.io/faultmaven/faultmaven/base:v1.0.0
docker tag ghcr.io/faultmaven/faultmaven/base:v1.0.0 ghcr.io/faultmaven/faultmaven/base:latest
docker push ghcr.io/faultmaven/faultmaven/base:latest

# Build tools image
cd containers/tools
docker build -t ghcr.io/faultmaven/faultmaven/tools:v1.0.0 .
docker push ghcr.io/faultmaven/faultmaven/tools:v1.0.0
```

**Verify**:
```bash
docker pull ghcr.io/faultmaven/faultmaven/base:v1.0.0
docker run --rm ghcr.io/faultmaven/faultmaven/base:v1.0.0 python -c "import faultmaven_core; print('✅ Works')"
```

---

#### Day 28-30: Set Up CI/CD Pipeline

**`.github/workflows/release.yml`**:

```yaml
name: Release All Packages
on:
  push:
    tags: ["v*.*.*"]

permissions:
  contents: write
  packages: write
  id-token: write

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

      - name: Build package
        working-directory: packages/${{ matrix.package }}
        run: |
          pip install build
          python -m build

      - name: Publish to PyPI
        working-directory: packages/${{ matrix.package }}
        run: |
          pip install twine
          twine upload dist/*
        env:
          TWINE_USERNAME: __token__
          TWINE_PASSWORD: ${{ secrets.PYPI_TOKEN }}

  publish-images:
    needs: prepare
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    strategy:
      matrix:
        image: [base, tools]
    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

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
          tags: |
            ghcr.io/faultmaven/faultmaven/${{ matrix.image }}:v${{ needs.prepare.outputs.version }}
            ghcr.io/faultmaven/faultmaven/${{ matrix.image }}:latest
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

**Test release workflow**:
```bash
# Create test tag
git tag -a v1.0.1-test -m "Test release workflow"
git push origin v1.0.1-test

# Monitor workflow
gh run watch

# Cleanup
git tag -d v1.0.1-test
git push origin :refs/tags/v1.0.1-test
```

---

## 5. Phase 3: Private Repository Setup (Week 7-8)

**Objective**: Create enterprise repository, set up Renovate, implement adapters

### Week 7: Repository Structure

#### Day 31-32: Initialize Private Repository

```bash
# Run setup script
./scripts/setup_private_repo.sh

# Add remote
cd faultmaven-enterprise
git remote add origin https://github.com/FaultMaven/faultmaven-enterprise.git

# Initial push
git push -u origin main
```

**Verify structure**:
```
faultmaven-enterprise/
├── versions.yaml
├── renovate.json
├── services/
│   ├── investigation-service/
│   ├── knowledge-service/
│   ├── llm-router-service/
│   ├── security-service/
│   ├── analytics-service/
│   └── gateway-service/
├── infrastructure/
│   ├── k8s/
│   ├── helm/
│   ├── terraform/
│   └── storage/
├── shared-libs/
│   ├── auth/
│   ├── monitoring/
│   ├── contracts/
│   └── testing/
└── docs/enterprise/
```

---

#### Day 33-34: Set Up Renovate

**Enable Renovate**:
1. Install Renovate GitHub App on organization
2. Configure for `faultmaven-enterprise` repo
3. Test with manual trigger

**Renovate will automatically**:
- Detect `faultmaven-*` packages in `versions.yaml`
- Create weekly PRs for updates
- Group FaultMaven packages together

**Test Renovate**:
```bash
# Manually trigger Renovate
gh workflow run renovate.yml

# Check for PR
gh pr list
```

---

#### Day 35-37: Set Up Repository Dispatch Receiver

**`.github/workflows/sync-public.yml`**:

```yaml
name: Sync from Public Release
on:
  repository_dispatch:
    types: [public-release]
  workflow_dispatch:
    inputs:
      version:
        description: 'Version to sync'
        required: true

jobs:
  update-versions:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup yq
        uses: mikefarah/yq@master

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
          yq -i ".faultmaven.images.base = \"ghcr.io/faultmaven/faultmaven/base:v${VERSION}\"" versions.yaml
          yq -i ".faultmaven.images.tools = \"ghcr.io/faultmaven/faultmaven/tools:v${VERSION}\"" versions.yaml

      - name: Create Pull Request
        uses: peter-evans/create-pull-request@v6
        with:
          branch: chore/bump-faultmaven-${{ steps.version.outputs.version }}
          title: "chore(deps): bump FaultMaven to ${{ steps.version.outputs.version }}"
          body: |
            Automated sync from public release
            Version: ${{ steps.version.outputs.version }}
          labels: dependencies,automated
```

**Test**:
```bash
# Manual trigger
gh workflow run sync-public.yml -f version=1.0.0

# Verify PR created
gh pr list
```

---

### Week 8: Shared Libraries

#### Day 38-40: Create Enterprise Shared Libraries

**1. Enterprise Auth Library** (`shared-libs/auth/`)

**`shared-libs/auth/pyproject.toml`**:
```toml
[project]
name = "faultmaven-enterprise-auth"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
    "faultmaven-models>=1.0.0",
    "pydantic>=2.7",
    "python-jose[cryptography]>=3.3",
    "passlib[bcrypt]>=1.7",
    "python-multipart>=0.0.6",
]
```

**`shared-libs/auth/src/faultmaven_enterprise_auth/auth_context.py`**:
```python
from typing import Optional
from pydantic import BaseModel

class EnterpriseAuthContext(BaseModel):
    """Enterprise authentication context"""
    user_id: str
    org_id: str
    email: str
    roles: list[str]
    permissions: list[str]

    def can_access_case(self, case_id: str) -> bool:
        """Check if user can access case"""
        # TODO: Implement RBAC logic
        return True
```

**2. Enterprise Monitoring Library** (`shared-libs/monitoring/`)

**`shared-libs/monitoring/src/faultmaven_enterprise_monitoring/tracer.py`**:
```python
from faultmaven_models.interfaces import ITracer
from typing import Optional, Dict, Any

class EnterpriseTracer(ITracer):
    """Enterprise tracer with advanced monitoring"""

    def __init__(self, service_name: str):
        self.service_name = service_name

    def trace(self, name: str, metadata: Optional[Dict[str, Any]] = None):
        """Create trace span"""
        # TODO: Implement DataDog/New Relic integration
        pass
```

**Install locally**:
```bash
cd shared-libs/auth
pip install -e .

cd ../monitoring
pip install -e .
```

---

## 6. Phase 4: Microservices Conversion (Week 9-10)

**Objective**: Build first microservice (Investigation Service) as template

### Week 9: Investigation Service

#### Day 41-43: Create Investigation Service

**Directory structure**:
```
services/investigation-service/
├── pyproject.toml
├── src/
│   ├── main.py
│   ├── adapters/
│   │   ├── investigation_adapter.py
│   │   └── llm_adapter.py
│   ├── routes/
│   │   └── investigations.py
│   └── config.py
├── tests/
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
    # Public packages
    "faultmaven-core==1.0.0",
    "faultmaven-models==1.0.0",
    "faultmaven-prompts==1.0.0",
    "faultmaven-llm==1.0.0",

    # Service framework
    "fastapi>=0.109",
    "uvicorn[standard]>=0.27",
    "pydantic-settings>=2.1",

    # Enterprise libraries
    "faultmaven-enterprise-auth @ file:../../shared-libs/auth",
    "faultmaven-enterprise-monitoring @ file:../../shared-libs/monitoring",
]
```

**`services/investigation-service/src/main.py`**:
```python
from fastapi import FastAPI, Depends
from faultmaven_enterprise_auth import get_auth_context, EnterpriseAuthContext
from .adapters.investigation_adapter import EnterpriseInvestigationCoordinator
from .routes import investigations

app = FastAPI(
    title="Investigation Service",
    version="0.1.0",
    description="FaultMaven investigation microservice"
)

# Include routes
app.include_router(investigations.router, prefix="/api/v1")

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "investigation"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

**`services/investigation-service/src/adapters/investigation_adapter.py`**:
```python
"""
Enterprise adapter wrapping public faultmaven-core
"""
from typing import Optional
from faultmaven_core.investigation import InvestigationCoordinator as PublicCoordinator
from faultmaven_models.interfaces import ILLMProvider, ITracer
from faultmaven_enterprise_auth import EnterpriseAuthContext
from faultmaven_enterprise_monitoring import EnterpriseTracer

class EnterpriseInvestigationCoordinator:
    """Enterprise wrapper with RBAC, monitoring, compliance"""

    def __init__(
        self,
        llm_provider: ILLMProvider,
        auth_context: EnterpriseAuthContext,
        tracer: Optional[ITracer] = None,
    ):
        self._tracer = tracer or EnterpriseTracer("investigation-service")
        self._auth = auth_context
        self._coordinator = PublicCoordinator(
            llm_provider=llm_provider,
            tracer=self._tracer,
        )

    async def investigate(self, case_id: str) -> dict:
        """Enterprise investigation with auth and monitoring"""
        # Auth check
        if not await self._auth.can_access_case(case_id):
            raise PermissionError(f"Access denied to case {case_id}")

        # Call public core
        with self._tracer.trace("investigation"):
            result = await self._coordinator.investigate(case_id)

        # Enterprise logging, compliance, etc.
        await self._log_audit_trail(case_id, result)

        return result

    async def _log_audit_trail(self, case_id: str, result: dict):
        """Enterprise compliance logging"""
        # TODO: Log to enterprise audit system
        pass
```

**`services/investigation-service/src/routes/investigations.py`**:
```python
from fastapi import APIRouter, Depends
from faultmaven_enterprise_auth import get_auth_context, EnterpriseAuthContext
from ..adapters.investigation_adapter import EnterpriseInvestigationCoordinator

router = APIRouter(tags=["investigations"])

def get_coordinator(
    auth: EnterpriseAuthContext = Depends(get_auth_context)
) -> EnterpriseInvestigationCoordinator:
    # TODO: Get LLM provider from DI
    return EnterpriseInvestigationCoordinator(
        llm_provider=...,  # From container
        auth_context=auth,
    )

@router.post("/investigations/{case_id}")
async def investigate(
    case_id: str,
    auth: EnterpriseAuthContext = Depends(get_auth_context),
    coordinator: EnterpriseInvestigationCoordinator = Depends(get_coordinator),
):
    return await coordinator.investigate(case_id)
```

**`services/investigation-service/Dockerfile`**:
```dockerfile
FROM ghcr.io/faultmaven/faultmaven/base:v1.0.0

WORKDIR /app

# Copy shared libs
COPY shared-libs/ /app/shared-libs/
RUN pip install -e /app/shared-libs/auth -e /app/shared-libs/monitoring

# Copy service code
COPY services/investigation-service/pyproject.toml ./
COPY services/investigation-service/src/ ./src/

RUN pip install -e .

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

#### Day 44-45: Kubernetes Manifests

**`services/investigation-service/k8s/deployment.yaml`**:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: investigation-service
  namespace: faultmaven
  labels:
    app: investigation-service
    version: v1
spec:
  replicas: 3
  selector:
    matchLabels:
      app: investigation-service
  template:
    metadata:
      labels:
        app: investigation-service
        version: v1
    spec:
      containers:
      - name: investigation-service
        image: ghcr.io/faultmaven/investigation-service:latest
        ports:
        - containerPort: 8000
          name: http
        env:
        - name: SERVICE_NAME
          value: "investigation-service"
        - name: LOG_LEVEL
          value: "INFO"
        # Add secrets for LLM API keys
        envFrom:
        - secretRef:
            name: llm-credentials
        resources:
          requests:
            cpu: 500m
            memory: 1Gi
          limits:
            cpu: 2000m
            memory: 4Gi
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: investigation-service
  namespace: faultmaven
spec:
  selector:
    app: investigation-service
  ports:
  - port: 80
    targetPort: 8000
    name: http
  type: ClusterIP
```

**Test locally**:
```bash
cd services/investigation-service

# Build
docker build -t investigation-service:test .

# Run
docker run -p 8000:8000 investigation-service:test

# Test
curl http://localhost:8000/health
```

---

### Week 10: Additional Microservices

#### Day 46-50: Create Remaining Services

Using the investigation service as a template, create:

1. **Knowledge Service** (Day 46)
   - Wraps `faultmaven-core` knowledge base
   - Adds enterprise document management
   - Multi-tenant knowledge isolation

2. **LLM Router Service** (Day 47)
   - Wraps `faultmaven-llm`
   - Adds usage metering
   - Organization-specific provider selection

3. **Security Service** (Day 48)
   - Wraps `faultmaven-security`
   - Enterprise PII policies
   - Compliance logging

4. **Analytics Service** (Day 49)
   - Enterprise analytics dashboard
   - Usage metrics
   - SLA monitoring

5. **API Gateway** (Day 50)
   - Kong or similar
   - Authentication
   - Rate limiting
   - Request routing

**Each service follows same pattern**:
```
services/<service-name>/
├── pyproject.toml        # Depends on public packages
├── src/
│   ├── main.py
│   ├── adapters/         # Enterprise wrappers
│   └── routes/
├── k8s/
│   ├── deployment.yaml
│   └── service.yaml
└── Dockerfile            # FROM ghcr.io/faultmaven/faultmaven/base:v1.0.0
```

---

## 7. Phase 5: Deployment & Validation (Week 11-12)

**Objective**: Deploy to K8s, validate end-to-end, production readiness

### Week 11: Infrastructure Setup

#### Day 51-53: K8s Cluster Setup

**Create cluster** (choose cloud provider):

**AWS (EKS)**:
```bash
cd infrastructure/terraform/aws

terraform init
terraform plan
terraform apply

# Configure kubectl
aws eks update-kubeconfig --name faultmaven-cluster --region us-west-2
```

**Or GCP (GKE)**:
```bash
cd infrastructure/terraform/gcp

terraform init
terraform apply

gcloud container clusters get-credentials faultmaven-cluster --region us-central1
```

**Create namespaces**:
```bash
kubectl create namespace faultmaven
kubectl create namespace faultmaven-dev
kubectl create namespace faultmaven-staging
```

**Install infrastructure**:
```bash
# PostgreSQL
helm install postgresql bitnami/postgresql \
  --namespace faultmaven \
  --set auth.database=faultmaven

# Redis
helm install redis bitnami/redis \
  --namespace faultmaven

# Ingress controller
helm install nginx-ingress ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace
```

---

#### Day 54-55: Deploy Services

**Deploy all microservices**:
```bash
# Apply all K8s manifests
kubectl apply -f services/investigation-service/k8s/
kubectl apply -f services/knowledge-service/k8s/
kubectl apply -f services/llm-router-service/k8s/
kubectl apply -f services/security-service/k8s/
kubectl apply -f services/analytics-service/k8s/
kubectl apply -f services/gateway-service/k8s/

# Verify
kubectl get pods -n faultmaven
kubectl get services -n faultmaven
```

**Configure secrets**:
```bash
kubectl create secret generic llm-credentials \
  --from-literal=OPENAI_API_KEY=$OPENAI_API_KEY \
  --from-literal=ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  --namespace faultmaven
```

---

#### Day 56-57: Set Up Monitoring

**Install Prometheus + Grafana**:
```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace

# Access Grafana
kubectl port-forward -n monitoring svc/prometheus-grafana 3000:80
```

**Configure dashboards**:
- Service health (pod status, restarts)
- Request metrics (latency, throughput)
- LLM usage (tokens, costs)
- Error rates

---

### Week 12: Testing & Launch

#### Day 58-59: Integration Testing

**Test scenarios**:

1. **End-to-End Investigation**:
```bash
# Create case via API
curl -X POST https://api.faultmaven.com/v1/cases \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"title":"Test case","description":"Test"}'

# Submit evidence
curl -X POST https://api.faultmaven.com/v1/cases/$CASE_ID/evidence \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@logs.txt"

# Run investigation
curl -X POST https://api.faultmaven.com/v1/investigations/$CASE_ID \
  -H "Authorization: Bearer $TOKEN"

# Verify result
curl https://api.faultmaven.com/v1/investigations/$CASE_ID \
  -H "Authorization: Bearer $TOKEN"
```

2. **Multi-Tenant Isolation**:
```bash
# Create 2 users in different orgs
# Verify user A cannot access user B's cases
```

3. **LLM Failover**:
```bash
# Disable primary provider
# Verify automatic failover to secondary
```

4. **Scale Testing**:
```bash
# Load test with 100 concurrent requests
k6 run load-test.js
```

**Pass criteria**:
- [ ] All API endpoints return 200
- [ ] Investigation completes successfully
- [ ] Multi-tenant isolation works
- [ ] Failover works
- [ ] Performance meets SLA (p95 < 2s)

---

#### Day 60: Public Launch

**Launch checklist**:

**Public Repository**:
- [ ] README complete with examples
- [ ] Documentation published (docs.faultmaven.com)
- [ ] PyPI packages published (v1.0.0)
- [ ] Container images published
- [ ] GitHub Discussions enabled
- [ ] Twitter/LinkedIn announcement prepared
- [ ] Submit to Hacker News, Reddit

**Private Repository**:
- [ ] All microservices deployed to production
- [ ] Monitoring configured
- [ ] Incident response plan documented
- [ ] Backup/restore tested
- [ ] On-call rotation set up

**Migration Complete**:
- [ ] Public repo has 0 enterprise code
- [ ] Private repo consuming public packages
- [ ] Renovate creating update PRs
- [ ] Repository dispatch working
- [ ] Team trained on new workflow

---

## 8. Post-Migration Operations

### 8.1 Ongoing Synchronization

**Weekly Cadence**:

**Monday Morning** (automated):
- Renovate checks for public package updates
- Creates grouped PR if new versions available
- CI runs integration tests

**Tuesday** (manual):
- Team reviews Renovate PR
- Merge if tests pass

**As Needed** (event-driven):
- Public release → repository dispatch → auto PR in private repo

### 8.2 Upstream Contributions

**Process**:

1. Developer creates feature in private repo
2. Adds label `candidate-upstream` if applicable
3. Automated workflow:
   - Sanitizes enterprise code
   - Creates PR in public repo (draft)
4. Human reviews and finalizes
5. Merge public PR first
6. Next sync brings it back to private repo (validates round-trip)

### 8.3 Versioning Strategy

**Public Packages**:
- SemVer: `major.minor.patch`
- Breaking changes = major bump
- Features = minor bump
- Fixes = patch bump
- Deprecation policy: N+2 versions supported

**Private Services**:
- Independent versioning per service
- Pin to specific public package versions
- Blue/green deployments for zero downtime

### 8.4 Monitoring & Alerts

**Key Metrics**:

**Public Repo**:
- GitHub stars, forks, contributors
- PyPI download counts
- Issue response time
- PR merge time

**Private Platform**:
- Service uptime (99.9% SLA)
- Request latency (p95 < 2s)
- Error rate (< 0.1%)
- LLM costs per investigation

**Alerts**:
- Service down (PagerDuty)
- High error rate (Slack)
- Cost spike (email)
- Security vulnerability (GitHub Security)

---

## 9. Rollback Procedures

### 9.1 Public Package Rollback

**Scenario**: Published package has critical bug

**Steps**:
```bash
# 1. Yank broken version from PyPI
twine upload --skip-existing  # Re-upload works as yank
pip install yank
yank faultmaven-core 1.2.3

# 2. Publish hotfix
git tag -a v1.2.4 -m "Hotfix for critical bug"
git push origin v1.2.4

# 3. Notify users
gh release create v1.2.4 --notes "Critical hotfix"
```

**Private repo protection**:
- Versions pinned in `versions.yaml`
- Renovate creates PR (doesn't auto-merge)
- Manual review before updating

### 9.2 Microservice Rollback

**K8s Rollout Rollback**:
```bash
# Rollback to previous version
kubectl rollout undo deployment/investigation-service -n faultmaven

# Verify
kubectl rollout status deployment/investigation-service -n faultmaven
```

**Database Migration Rollback**:
```bash
# Alembic downgrade
alembic downgrade -1

# Or restore from backup
./scripts/restore-db.sh backup-2025-11-12.sql
```

### 9.3 Full Migration Rollback

**Worst case**: Abort entire migration

**Steps**:
1. Keep `sterlanyu/FaultMaven` monolith running
2. Redirect traffic back to monolith
3. Archive `FaultMaven/*` repositories
4. Retrospective on what went wrong

**Rollback criteria**:
- [ ] >50% of critical features broken
- [ ] Data loss occurred
- [ ] Team cannot maintain new architecture
- [ ] Business impact exceeds acceptable threshold

---

## 10. Success Metrics

### 10.1 Technical Metrics

**Migration Success** (Week 12):
- [ ] 100% public code separated (no enterprise leaks)
- [ ] All 1425+ tests passing
- [ ] 6 packages published to PyPI
- [ ] 6 microservices deployed to K8s
- [ ] Renovate creating update PRs
- [ ] CI/CD pipelines green

**Performance** (Week 12+):
- [ ] P95 latency < 2s (same as monolith)
- [ ] 99.9% uptime
- [ ] Zero data loss
- [ ] LLM costs within budget

### 10.2 Community Metrics

**Public Repo** (3 months):
- [ ] 100+ GitHub stars
- [ ] 10+ external contributors
- [ ] 1000+ PyPI downloads/month
- [ ] 5+ community PRs merged
- [ ] Active GitHub Discussions

### 10.3 Business Metrics

**Enterprise Platform** (6 months):
- [ ] 10+ enterprise customers
- [ ] 99.9% SLA maintained
- [ ] <1% churn rate
- [ ] Feature parity with monolith
- [ ] Team velocity maintained

---

## Appendices

### A. Team Roles & Responsibilities

| Role | Responsibilities | Time Commitment |
|------|------------------|-----------------|
| **Migration Lead** | Overall execution, decisions, unblocking | 100% (12 weeks) |
| **Public Repo Lead** | Open-source community, documentation | 50% (6 weeks) |
| **Private Repo Lead** | Microservices architecture, deployment | 100% (12 weeks) |
| **DevOps Lead** | K8s, CI/CD, infrastructure | 100% (12 weeks) |
| **QA Lead** | Testing, validation, monitoring | 50% (6 weeks) |

### B. Risk Register

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| **Enterprise code leaks to public** | Low | Critical | Automated scanning, manual review |
| **Migration takes longer than 12 weeks** | Medium | High | Buffer weeks, reduce scope |
| **Performance degradation** | Medium | High | Load testing, monitoring, caching |
| **Team resistance to new workflow** | Medium | Medium | Training, documentation, support |
| **Public repo gets no traction** | Medium | Low | Marketing, community engagement |
| **Renovate breaks production** | Low | High | Manual review, staging environment |

### C. Communication Plan

**Weekly Sync** (Fridays):
- Progress update
- Blockers discussion
- Next week planning

**Stakeholder Updates** (Bi-weekly):
- Executive summary
- Metrics dashboard
- Risk updates

**Team Communication**:
- Slack: Daily updates, quick questions
- GitHub Issues: Technical discussions
- Pull Requests: Code review, feedback

### D. Resources & References

**Documentation**:
- [Clean Architecture](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)
- [Microservices Patterns](https://microservices.io/patterns/index.html)
- [Kubernetes Best Practices](https://kubernetes.io/docs/concepts/configuration/overview/)
- [Python Packaging](https://packaging.python.org/en/latest/)

**Tools**:
- [Renovate](https://docs.renovatebot.com/)
- [GitHub Actions](https://docs.github.com/en/actions)
- [Helm](https://helm.sh/docs/)
- [Terraform](https://www.terraform.io/docs)

---

## Conclusion

This migration plan transforms FaultMaven from a monolithic application into a modern, scalable SaaS platform while simultaneously launching an open-source community project.

**Key Principles**:
1. **Clean Separation**: Public core vs. enterprise features
2. **Artifact-Based Sync**: Low coupling, explicit versioning
3. **Automated Operations**: Renovate, CI/CD, monitoring
4. **Graceful Migration**: Phased approach, rollback plans
5. **Community First**: Open-source success drives enterprise adoption

**Timeline Summary**:
- **Weeks 1-2**: Preparation
- **Weeks 3-6**: Public repository & publishing
- **Weeks 7-8**: Private repository setup
- **Weeks 9-10**: Microservices conversion
- **Weeks 11-12**: Deployment & launch

**Success Criteria**: Clean public repo, automated sync, microservices in production, thriving community.

---

**Document Version**: 1.0
**Last Updated**: 2025-11-13
**Next Review**: After Phase 0 completion

**Questions?** Contact Migration Lead or create issue in `FaultMaven/faultmaven-enterprise`
