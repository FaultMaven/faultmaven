# FaultMaven Knowledge Base Systems

**Version:** 2.0
**Last Updated:** 2025-10-18
**Purpose:** Comprehensive guide to FaultMaven's three-tier knowledge base architecture

---

## Table of Contents

1. [Overview](#overview)
2. [Three-Tier Knowledge Base Architecture](#three-tier-knowledge-base-architecture)
3. [Global Knowledge Base (System 2)](#global-knowledge-base-system-2)
4. [Knowledge Base Toolkit](#knowledge-base-toolkit)
5. [Knowledge Base Structure](#knowledge-base-structure)
6. [Content Creation & Sources](#content-creation--sources)
7. [Contribution Workflow](#contribution-workflow)
8. [Review Process](#review-process)
9. [Ingestion, Updates & Maintenance](#ingestion-updates--maintenance)
10. [Tools & Commands](#tools--commands)
11. [Quality Standards](#quality-standards)
12. [FAQ](#faq)

---

## Overview

FaultMaven implements a **three-tier RAG (Retrieval-Augmented Generation) architecture** with distinct knowledge base systems serving different purposes:

1. **User Knowledge Base** (System 1) - Personal runbooks and procedures
2. **Global Knowledge Base** (System 2) - System-wide troubleshooting documentation
3. **Case Evidence Store** (System 3) - Temporary case-specific evidence

This document focuses on the **Global Knowledge Base** (System 2). For complete architecture details, see [Knowledge Base Architecture](../architecture/knowledge-base-architecture.md).

---

## Three-Tier Knowledge Base Architecture

### Quick Reference

| System | Scope | Lifecycle | Purpose | Status |
|--------|-------|-----------|---------|--------|
| **User KB** | User-level | Permanent | Personal runbooks & procedures | ⏳ Planned |
| **Global KB** | System-level | Permanent | System-wide troubleshooting docs | ✅ Implemented |
| **Case Evidence Store** | Case-level | Ephemeral | Case-specific uploaded evidence | ✅ Implemented |

### When to Use Each System

**Use Global KB (this document) when:**
- Creating system-wide troubleshooting runbooks
- Contributing general technical documentation
- Building reusable troubleshooting procedures

**Use User KB (planned) when:**
- Storing personal/team-specific runbooks
- Managing private procedures and playbooks
- Building custom troubleshooting workflows

**Use Case Evidence Store when:**
- Uploading logs, configs, or error dumps for active case
- Asking questions about uploaded troubleshooting evidence
- Analyzing case-specific data

---

## Global Knowledge Base (System 2)

### What is the Global Knowledge Base?

The Global Knowledge Base is a **system-wide collection of curated troubleshooting runbooks** that enhances the AI agent's ability to help users resolve infrastructure and application issues.

**Key Characteristics:**
- 🌐 **Universal Access** - All users benefit automatically
- 🔍 **Transparent Operation** - Retrieved automatically during troubleshooting
- 📚 **Source Attribution** - Responses reference specific runbooks
- ✅ **Quality Controlled** - Team-verified, community-contributed
- 🔄 **Continuously Updated** - Regular additions and improvements
- 🏛️ **System-Wide** - Shared across all users and cases

### How It Works

```
User Question → Agent Reasoning → RAG Retrieval → Runbook Context → Enhanced Response
                                        ↓
                                Knowledge Base (ChromaDB)
                                        ↓
                                Curated Runbooks
```

**Example:**
```
User: "My Kubernetes pod keeps crashing"
  ↓
Agent retrieves: k8s-pod-crashloopbackoff.md
  ↓
Response: "Based on our Kubernetes CrashLoopBackOff runbook, this typically
          indicates [diagnosis]. Try these steps: [solutions]..."

Source: docs/runbooks/kubernetes/k8s-pod-crashloopbackoff.md
```

### Current Status

- ✅ **10 team-verified runbooks** across 4 technologies
- ✅ **123 chunks** in ChromaDB vector database
- ✅ **99.20/100 quality score** (A+ grade)
- ✅ **100% retrieval accuracy** on benchmark queries
- ✅ **<200ms search latency** (p95)

---

## Knowledge Base Toolkit

### Overview

The **FaultMaven Knowledge Base Toolkit** (`faultmaven-kb-toolkit`) is a standalone command-line tool designed to streamline knowledge base operations, automation, and content management.

**Repository:** [`faultmaven-kb-toolkit`](https://github.com/faultmaven/faultmaven-kb-toolkit)

### Key Features

🔧 **Pipeline Automation**
- Automated web scraping and documentation conversion
- AI-powered content transformation to runbook format
- Batch ingestion with validation and quality checks
- GitHub Actions integration for CI/CD workflows

📊 **Content Management**
- URL-to-runbook conversion pipeline
- Bulk operations (ingest, validate, update)
- Change detection and incremental updates
- Quality assurance and testing

🤖 **AI Conversion**
- LLM-powered documentation → runbook transformation
- Template-based standardization
- Metadata extraction and enrichment
- Multi-source content aggregation

### Quick Start

```bash
# Install the toolkit
pip install faultmaven-kb-toolkit

# Convert a URL to a runbook
kb-toolkit convert https://kubernetes.io/docs/tasks/debug/debug-pod/ \
  --output docs/runbooks/kubernetes/k8s-debug-pod.md \
  --technology kubernetes \
  --severity medium

# Validate runbooks
kb-toolkit validate docs/runbooks/

# Batch ingest to knowledge base
kb-toolkit ingest --status verified --technology all
```

### Documentation

For complete toolkit documentation, see the [`faultmaven-kb-toolkit`](https://github.com/faultmaven/faultmaven-kb-toolkit) repository:

- **[Specification](https://github.com/faultmaven/faultmaven-kb-toolkit/blob/main/docs/SPECIFICATION.md)** - Architecture and design
- **[Implementation Guide](https://github.com/faultmaven/faultmaven-kb-toolkit/blob/main/docs/IMPLEMENTATION_GUIDE.md)** - Development details
- **[Operations Manual](https://github.com/faultmaven/faultmaven-kb-toolkit/blob/main/docs/OPERATIONS.md)** - Usage and workflows
- **[GitHub Actions](https://github.com/faultmaven/faultmaven-kb-toolkit/blob/main/docs/GITHUB_ACTIONS.md)** - CI/CD automation

### When to Use the Toolkit

**Use `faultmaven-kb-toolkit` for:**
- ✅ Converting external documentation to runbooks
- ✅ Automating batch operations
- ✅ CI/CD pipeline integration
- ✅ Web scraping and content extraction
- ✅ Quality validation at scale

**Use manual processes for:**
- ❌ Single runbook creation/editing
- ❌ Quick fixes and updates
- ❌ Learning runbook structure
- ❌ Custom one-off operations

---

## Knowledge Base Structure

### 1. Directory Organization

```
docs/runbooks/
├── README.md                      # Main index and overview
├── TEMPLATE.md                    # Gold standard runbook template
├── CONTRIBUTING.md                # Contribution guidelines
├── REVIEW_GUIDELINES.md           # Curator review handbook
│
├── kubernetes/                    # Kubernetes troubleshooting
│   ├── README.md                  # K8s quick reference
│   ├── k8s-pod-crashloopbackoff.md
│   ├── k8s-pod-oomkilled.md
│   ├── k8s-pod-imagepullbackoff.md
│   └── k8s-node-not-ready.md
│
├── redis/                         # Redis troubleshooting
│   ├── README.md                  # Redis quick reference
│   ├── redis-connection-refused.md
│   └── redis-out-of-memory.md
│
├── postgresql/                    # PostgreSQL troubleshooting
│   ├── README.md                  # PostgreSQL quick reference
│   ├── postgres-connection-pool-exhausted.md
│   └── postgres-slow-queries.md
│
└── networking/                    # Networking troubleshooting
    ├── README.md                  # Networking quick reference
    ├── network-dns-resolution-failure.md
    └── network-connection-timeout.md
```

### 2. File Naming Convention

**Pattern:** `{technology}-{problem-description}.md`

**Examples:**
- ✅ `k8s-pod-crashloopbackoff.md`
- ✅ `redis-out-of-memory.md`
- ✅ `postgres-slow-queries.md`
- ❌ `kubernetes_pod_crash.md` (use hyphens, not underscores)
- ❌ `K8S-Pod-Issue.md` (use lowercase)

### 3. Runbook Structure

Every runbook follows this standard structure:

```markdown
---
id: unique-runbook-id           # Required: kebab-case identifier
title: "Technology - Problem"    # Required: Human-readable title
technology: kubernetes           # Required: Technology category
severity: high                   # Required: critical/high/medium/low
tags: [tag1, tag2, tag3]        # Required: Searchable keywords
difficulty: intermediate         # Required: beginner/intermediate/advanced
version: "1.0.0"                # Required: Semantic versioning
last_updated: "2025-01-15"      # Required: YYYY-MM-DD format
verified_by: "FaultMaven Team"  # Required: Verification authority
status: verified                # Required: verified/draft/deprecated
---

# Problem Title

> **Purpose**: One-sentence description

## Quick Reference Card
🔍 Symptoms | ⚡ Common Causes | 🚀 Quick Fix | ⏱️ Resolution Time

## Diagnostic Steps
Step-by-step investigation procedures

## Solutions
Multiple solution paths with commands

## Root Cause Analysis
Why this problem occurs

## Prevention
Tactical + Strategic prevention strategies

## Related Issues
Links to related runbooks

## Version History
Changelog table

## License & Attribution
Apache-2.0 License
```

### 4. Metadata Fields Explained

| Field | Purpose | Example |
|-------|---------|---------|
| `id` | Unique identifier for retrieval | `k8s-pod-crashloopbackoff` |
| `title` | Display name in UI | `"Kubernetes - Pod CrashLoopBackOff"` |
| `technology` | Technology category for filtering | `kubernetes` |
| `severity` | Impact assessment | `critical`, `high`, `medium`, `low` |
| `tags` | Search keywords | `[kubernetes, pod, crashloop, restart]` |
| `difficulty` | Skill level required | `beginner`, `intermediate`, `advanced` |
| `version` | Document version (semver) | `"1.0.0"` |
| `last_updated` | Last modification date | `"2025-01-15"` |
| `verified_by` | Who verified accuracy | `"FaultMaven Team"`, `"John Doe"` |
| `status` | Publication status | `verified`, `draft`, `deprecated` |

---

## Content Creation & Sources

### 1. Sources of Knowledge

Runbooks are created from **verified, authoritative sources**:

#### Primary Sources (Team-Verified)
- ✅ **Production Incidents** - Real-world troubleshooting experiences
- ✅ **Official Documentation** - Kubernetes, Redis, PostgreSQL docs
- ✅ **Engineering Runbooks** - Internal SRE/DevOps playbooks
- ✅ **Post-Mortems** - Incident retrospectives and lessons learned

#### Secondary Sources (Referenced)
- 📚 **Technical Books** - "Site Reliability Engineering" (Google)
- 🎓 **Training Materials** - Certified courses and workshops
- 🌐 **Community Knowledge** - Stack Overflow, GitHub issues
- 📊 **Vendor Guides** - Cloud provider best practices

#### Quality Requirements
- ✅ Commands must be tested and verified
- ✅ Solutions must include success criteria
- ✅ Examples must be realistic and practical
- ✅ Security considerations must be addressed

### 2. Creation Process

**Step 1: Identify Need**
- Production incident requires new runbook
- Technology gap in knowledge base
- Community request for coverage

**Step 2: Research & Draft**
- Gather information from authoritative sources
- Test all commands in safe environment
- Document diagnostic procedures
- Validate solutions work

**Step 3: Follow Template**
- Use `docs/runbooks/TEMPLATE.md` as skeleton
- Fill all required metadata fields
- Include Quick Reference Card
- Provide multiple solution paths

**Step 4: Internal Review**
- Technical accuracy check
- Command verification
- Security review
- Quality checklist completion

**Step 5: Testing**
- Simulate problem in test environment
- Follow runbook step-by-step
- Verify diagnostic commands work
- Confirm solutions resolve issue

### 3. Content Types

**Comprehensive Runbooks** (20KB+)
- Full diagnostic workflows
- Multiple solution paths
- Root cause analysis
- Prevention strategies
- Example: `k8s-pod-crashloopbackoff.md`, `k8s-pod-oomkilled.md`

**Focused Runbooks** (10-15KB)
- Specific problem scope
- 2-3 solution paths
- Essential diagnostics
- Example: `k8s-pod-imagepullbackoff.md`, `redis-connection-refused.md`

**Quick Reference** (5-10KB)
- Common scenarios
- Fast resolution
- Essential commands
- Example: Category README files

---

## Contribution Workflow

### Overview: Fork → Branch → PR → Review → Merge → Auto-Ingest

### 1. Prerequisites

**Required:**
- GitHub account
- Git installed locally
- Text editor (VS Code recommended)
- Basic Markdown knowledge

**Recommended:**
- Kubernetes/Docker for testing
- Understanding of the technology domain
- Familiarity with the problem being documented

### 2. Step-by-Step Contribution Process

#### Step 1: Fork & Clone

```bash
# Fork the repository on GitHub (click "Fork" button)

# Clone your fork
git clone https://github.com/YOUR_USERNAME/FaultMaven.git
cd FaultMaven

# Add upstream remote
git remote add upstream https://github.com/faultmaven/FaultMaven.git
```

#### Step 2: Create Feature Branch

```bash
# Update your fork
git fetch upstream
git checkout main
git merge upstream/main

# Create feature branch
git checkout -b runbook/k8s-deployment-failed
```

**Branch Naming Convention:**
- `runbook/{technology}-{problem}` - New runbook
- `update/{runbook-id}` - Update existing runbook
- `fix/{runbook-id}` - Bug fix in runbook

#### Step 3: Create/Edit Runbook

```bash
# Copy template
cp docs/runbooks/TEMPLATE.md docs/runbooks/kubernetes/k8s-deployment-failed.md

# Edit the runbook
# - Fill all YAML frontmatter fields
# - Follow structure from TEMPLATE.md
# - Test all commands
# - Include code examples
```

#### Step 4: Contributor Self-Review

Use the **Contributor Checklist** from `CONTRIBUTING.md`:

**Required Checks:**
- [ ] All YAML frontmatter fields completed
- [ ] All 5 required sections present
- [ ] Commands tested and verified
- [ ] Code examples included with explanations
- [ ] Security considerations addressed
- [ ] No sensitive data (credentials, IPs, etc.)
- [ ] Markdown formatting correct
- [ ] Links work and are accessible
- [ ] Spell-check completed
- [ ] Follows naming conventions

#### Step 5: Commit & Push

```bash
# Add your runbook
git add docs/runbooks/kubernetes/k8s-deployment-failed.md

# Commit with descriptive message
git commit -m "Add runbook: Kubernetes Deployment Failed

- Covers failed deployment diagnostics
- Includes rollout status checks
- Provides rollback procedures
- Tested on K8s 1.28+"

# Push to your fork
git push origin runbook/k8s-deployment-failed
```

#### Step 6: Create Pull Request

1. Go to your fork on GitHub
2. Click "Compare & pull request"
3. Fill PR template:

```markdown
## Runbook Information
- **Technology**: Kubernetes
- **Problem**: Deployment Failed
- **Severity**: High
- **Difficulty**: Intermediate

## Description
This runbook covers troubleshooting failed Kubernetes deployments,
including rollout issues, image problems, and resource constraints.

## Testing Performed
- [x] All commands tested on K8s 1.28
- [x] Verified rollback procedures
- [x] Tested with various failure scenarios
- [x] Commands include expected output

## Checklist
- [x] Follows TEMPLATE.md structure
- [x] All metadata fields complete
- [x] Commands tested and working
- [x] Security reviewed
- [x] No sensitive information
```

4. Submit PR and wait for review

### 3. Contribution Types

**New Runbooks**
- Add coverage for new problems
- Expand technology coverage
- Fill identified gaps

**Updates to Existing Runbooks**
- Add new solutions
- Update for new versions
- Improve clarity
- Fix errors

**Translations** (Future)
- Translate runbooks to other languages
- Maintain technical accuracy
- Update locale-specific examples

### 4. Recognition

Contributors are recognized through:
- Author credit in runbook metadata
- Contributor list in README.md
- GitHub contributor graph
- Special recognition for high-quality contributions

---

## Review Process

### Overview: Knowledge Curator 5-Step Review

All contributions are reviewed by **Knowledge Curators** - designated team members responsible for maintaining quality standards.

### 1. Curator Role & Responsibilities

**Who are Knowledge Curators?**
- Senior engineers with deep technical expertise
- Designated by FaultMaven team
- Maintain knowledge base quality

**Responsibilities:**
- Review all runbook PRs within 48 hours
- Ensure technical accuracy
- Verify security compliance
- Maintain quality standards
- Provide constructive feedback

**Current Curators:**
- FaultMaven Team (primary reviewers)

### 2. Five-Step Review Process

#### Step 1: Initial Assessment (5 minutes)

**Quick checks:**
- [ ] PR follows template
- [ ] All required sections present
- [ ] Contributor checklist completed
- [ ] No obvious security issues
- [ ] Appropriate difficulty level

**Decision:**
- ✅ Pass → Continue to Step 2
- ❌ Fail → Request changes immediately

#### Step 2: Technical Accuracy Review (15-30 minutes)

**Deep review:**
- [ ] Commands are correct and tested
- [ ] Diagnostic steps are logical
- [ ] Solutions actually work
- [ ] Root cause analysis is accurate
- [ ] Technology-specific best practices followed

**Verification:**
```bash
# Reviewer tests commands locally
kubectl get pods -A  # Verify command syntax
kubectl describe pod <pod-name>  # Test diagnostic steps
```

**Decision:**
- ✅ Accurate → Continue to Step 3
- ⚠️ Issues → Request technical corrections

#### Step 3: Safety & Security Review (10 minutes)

**Security checks:**
- [ ] No hardcoded credentials
- [ ] No sensitive IPs/hostnames
- [ ] No dangerous commands without warnings
- [ ] Privilege escalation properly documented
- [ ] Data exposure risks addressed

**Warning flags:**
- Commands with `rm -rf`
- Database operations affecting production
- Network changes impacting multiple systems
- Privilege escalation (`sudo`, `root`)

**Decision:**
- ✅ Secure → Continue to Step 4
- 🚨 Security risk → Request security fixes

#### Step 4: Quality & Completeness Review (10 minutes)

**Quality assessment:**
- [ ] Writing is clear and concise
- [ ] Examples are practical and realistic
- [ ] Code formatting is consistent
- [ ] Markdown syntax is correct
- [ ] Links are valid and accessible

**Completeness checks:**
- [ ] Quick Reference Card complete
- [ ] Multiple solution paths provided
- [ ] Prevention strategies included
- [ ] Time estimates realistic
- [ ] Version history documented

**Decision:**
- ✅ High quality → Continue to Step 5
- 📝 Quality issues → Request improvements

#### Step 5: Consistency & Integration Review (5 minutes)

**Final checks:**
- [ ] Consistent with existing runbooks
- [ ] Proper cross-references to related runbooks
- [ ] Fits within technology category
- [ ] No duplicate content
- [ ] Metadata is complete and accurate

**Integration verification:**
```bash
# Check for duplicates
grep -r "title.*Deployment Failed" docs/runbooks/

# Verify cross-references
grep -r "k8s-deployment-failed" docs/runbooks/
```

**Decision:**
- ✅ Approved → Merge PR
- 🔄 Minor issues → Approve with suggestions

### 3. Review Decision Matrix

| Criteria | Approve & Merge | Approve with Suggestions | Request Changes | Reject |
|----------|----------------|-------------------------|-----------------|--------|
| Technical Accuracy | ✅ All correct | ✅ Minor improvements | ❌ Errors found | ❌ Fundamentally wrong |
| Security | ✅ No issues | ✅ Add warnings | ❌ Security risks | ❌ Major vulnerabilities |
| Completeness | ✅ All sections | ✅ Enhance content | ❌ Missing sections | ❌ Incomplete runbook |
| Quality | ✅ Excellent | ✅ Good enough | ❌ Needs work | ❌ Poor quality |

### 4. Feedback Guidelines

**Constructive feedback examples:**

✅ **Good feedback:**
```
The diagnostic steps are great! Consider adding this command to check
for resource constraints:

kubectl top pods -n <namespace>

This will help identify if the pod is hitting CPU/memory limits.
```

❌ **Poor feedback:**
```
This is wrong. Fix it.
```

**Reviewer should:**
- Explain the issue clearly
- Provide specific examples
- Suggest improvements
- Acknowledge good work

### 5. Approval & Merge

**When approved:**
1. Curator clicks "Approve" on GitHub
2. Curator merges PR to main branch
3. Automated ingestion pipeline triggers
4. Runbook appears in knowledge base within minutes

**Post-merge:**
- Contributor notified of merge
- Runbook automatically ingested to ChromaDB
- Available for agent retrieval immediately
- Contributor credited in metadata

---

## Ingestion, Updates & Maintenance

### Overview: Automated Pipeline

```
Runbook Files (Git) → Validation → MD5 Change Detection → ChromaDB Ingestion
                          ↓                                        ↓
                    Error Reports                          123 Chunks Ready
```

### 1. Automated Ingestion Pipeline

**Location:** `faultmaven/scripts/ingest_runbooks.py`

**What it does:**
1. Discovers runbook markdown files
2. Extracts YAML frontmatter metadata
3. Validates against quality schema
4. Detects changes using MD5 hashes
5. Chunks content (1000 chars, 200 overlap)
6. Generates BGE-M3 embeddings
7. Stores in ChromaDB vector database

**Trigger events:**
- PR merged to main branch (automatic)
- Manual execution (on-demand)
- Scheduled batch runs (future)

### 2. Ingestion Process Details

#### Discovery Phase
```python
# Scans for runbook files
docs/runbooks/**/*.md

# Excludes special files
- README.md
- TEMPLATE.md
- CONTRIBUTING.md
- REVIEW_GUIDELINES.md
```

#### Validation Phase
```python
# Required metadata fields check
REQUIRED_FIELDS = [
    "id", "title", "technology", "severity",
    "tags", "difficulty", "version",
    "last_updated", "verified_by", "status"
]

# Required sections check
REQUIRED_SECTIONS = [
    "Quick Reference Card",
    "Diagnostic Steps",
    "Solutions",
    "Prevention",
    "Related Issues"
]
```

#### Change Detection
```python
# MD5 hash calculation
current_hash = md5(file_content)
previous_hash = ingestion_log[file_path]["hash"]

# Only ingest if changed
if current_hash != previous_hash:
    ingest_runbook(file_path)
```

#### Chunking & Embedding
```python
# Content splitting
chunks = split_content(
    content=runbook_text,
    chunk_size=1000,
    overlap=200
)

# Embedding generation (BGE-M3)
embeddings = model.encode(chunks)

# Store in ChromaDB
collection.add(
    embeddings=embeddings,
    documents=chunks,
    metadatas=metadata,
    ids=chunk_ids
)
```

### 3. Update Scenarios

#### Scenario 1: New Runbook Added
```bash
# PR merged with new runbook
docs/runbooks/docker/docker-build-failed.md

# Automatic ingestion
✓ File discovered
✓ Metadata extracted
✓ Validation passed
✓ 12 chunks created
✓ Embeddings generated
✓ Stored in ChromaDB
✓ Available for retrieval
```

#### Scenario 2: Existing Runbook Updated
```bash
# Update to existing file
docs/runbooks/kubernetes/k8s-pod-crashloopbackoff.md

# Change detection
✓ MD5 hash changed
✓ Previous chunks deleted
✓ New content ingested
✓ Updated embeddings
✓ Knowledge base refreshed
```

#### Scenario 3: Runbook Deprecated
```bash
# Update metadata status
status: deprecated  # in YAML frontmatter

# Ingestion behavior
✓ Status updated in ChromaDB
✓ Lower retrieval priority
✓ Marked as deprecated in responses
```

### 4. Maintenance Operations

#### Regular Maintenance (Weekly)

**Quality checks:**
```bash
# Run quality benchmark tests
pytest tests/quality/ -v

# Verify all runbooks score >90
# Check for broken links
# Update outdated commands
```

**Content review:**
```bash
# Identify runbooks needing updates
git log --since="6 months ago" docs/runbooks/

# Review technology versions
# Update deprecated solutions
# Add new best practices
```

#### Deep Maintenance (Monthly)

**Comprehensive audit:**
- Review all runbooks for accuracy
- Update commands for new versions
- Add emerging problems
- Retire obsolete content

**Performance optimization:**
```bash
# Check ChromaDB collection stats
python -c "
from faultmaven.core.knowledge.ingestion import KnowledgeIngester
from faultmaven.config.settings import get_settings

ingester = KnowledgeIngester(settings=get_settings())
stats = ingester.get_collection_stats()
print(f'Total chunks: {stats[\"total_chunks\"]}')
print(f'Document types: {stats[\"document_types\"]}')
print(f'Top tags: {stats[\"top_tags\"]}')
"
```

#### Emergency Updates

**Critical security issue:**
1. Identify affected runbooks
2. Update immediately (bypass normal PR flow if needed)
3. Force re-ingestion
4. Notify users of critical change

```bash
# Emergency re-ingestion
python -m faultmaven.scripts.ingest_runbooks --force
```

### 5. Ingestion Monitoring

**Ingestion log:**
```json
// docs/runbooks/.ingestion_log.json
{
  "kubernetes/k8s-pod-crashloopbackoff.md": {
    "hash": "a1b2c3d4e5f6...",
    "ingested_at": "2025-01-15T10:30:00Z",
    "document_id": "k8s-pod-crashloopbackoff",
    "title": "Kubernetes - Pod CrashLoopBackOff",
    "technology": "kubernetes",
    "status": "verified"
  }
}
```

**Metrics tracked:**
- Total runbooks ingested
- Chunks per runbook
- Ingestion success rate
- Validation failures
- Average ingestion time

---

## Tools & Commands

### 1. Knowledge Base Toolkit (Recommended)

**For automated operations, use the standalone toolkit:**

See [Knowledge Base Toolkit](#knowledge-base-toolkit) section above for:
- AI-powered URL-to-runbook conversion
- Batch ingestion and validation
- GitHub Actions automation
- Web scraping and content extraction

**Installation:**
```bash
pip install faultmaven-kb-toolkit
kb-toolkit --help
```

**Documentation:** [`faultmaven-kb-toolkit` repository](https://github.com/faultmaven/faultmaven-kb-toolkit)

---

### 2. Manual Ingestion Pipeline (Legacy)

**Location:** `faultmaven/scripts/ingest_runbooks.py`

> **Note:** For production use, prefer the `faultmaven-kb-toolkit` for better automation and error handling.

#### Basic Usage

```bash
# Activate virtual environment
source .venv/bin/activate

# Ingest all verified runbooks
python -m faultmaven.scripts.ingest_runbooks --status verified

# Dry-run to validate without ingesting
python -m faultmaven.scripts.ingest_runbooks --dry-run --status all

# Force re-ingest everything (ignores change detection)
python -m faultmaven.scripts.ingest_runbooks --force
```

#### Advanced Options

```bash
# Ingest specific technology
python -m faultmaven.scripts.ingest_runbooks --technology kubernetes

# Ingest all statuses (verified, draft, deprecated)
python -m faultmaven.scripts.ingest_runbooks --status all

# Combine filters
python -m faultmaven.scripts.ingest_runbooks \
  --technology redis \
  --status verified \
  --force

# Custom runbook directory
python -m faultmaven.scripts.ingest_runbooks \
  --runbook-dir /path/to/runbooks

# Skip validation (not recommended)
python -m faultmaven.scripts.ingest_runbooks --no-validate
```

#### Output Examples

**Successful ingestion:**
```
FaultMaven Runbook Ingestion Pipeline
Runbook directory: docs/runbooks

Initializing knowledge base connection...
✓ Connected to knowledge base

Discovering runbooks (technology=all, status=verified)...
Found 10 runbooks

Ingesting 10 runbooks...
  Ingesting k8s-pod-crashloopbackoff.md... ━━━━━━━━━━━━━ 100%

Ingestion Report
┏━━━━━━━━━━━━━━━━┳━━━━━━━┓
┃ Metric         ┃ Count ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━┩
│ Total Runbooks │ 10    │
│ Successful     │ 10    │
│ Failed         │ 0     │
│ Warnings       │ 0     │
└────────────────┴───────┘
```

**With validation errors:**
```
Failed Runbooks:
  • kubernetes/k8s-new-runbook.md
    - Missing required metadata field: severity
    - Missing required section: Prevention
```

### 3. Quality Testing Tools

**Location:** `tests/quality/`

#### Run Quality Tests

```bash
# Run all quality tests
pytest tests/quality/ -v

# Run with baseline report
pytest tests/quality/test_baseline_quality.py::test_generate_baseline_report -v -s

# Run specific dimension tests
pytest tests/quality/test_baseline_quality.py::TestKnowledgeBaseRelevancy -v
pytest tests/quality/test_baseline_quality.py::TestKnowledgeBaseCompleteness -v
pytest tests/quality/test_baseline_quality.py::TestKnowledgeBaseActionability -v
pytest tests/quality/test_baseline_quality.py::TestKnowledgeBaseStructure -v
```

#### Quality Score Output

```
============================================================
KNOWLEDGE BASE BASELINE QUALITY REPORT
============================================================

📊 Collection Statistics:
  Total chunks: 123
  Document types: {'runbook': 123}
  Top 5 tags: {'kubernetes': 83, 'pod': 74, 'intermediate': 69}

📈 Quality Scores by Dimension:
  Relevancy       100.00/100  (Grade: A+)  [Weight: 40%]
  Completeness    100.00/100  (Grade: A+)  [Weight: 25%]
  Actionability    96.00/100  (Grade: A+)  [Weight: 20%]
  Structure       100.00/100  (Grade: A+)  [Weight: 15%]

🎯 Overall Quality Score: 99.20/100  (Grade: A+)
```

### 4. Knowledge Base Search Tool

#### Test Search Functionality

```python
# Create test script: test_search.py
import asyncio
from faultmaven.config.settings import get_settings
from faultmaven.core.knowledge.ingestion import KnowledgeIngester

async def main():
    settings = get_settings()
    ingester = KnowledgeIngester(settings=settings)

    # Search
    results = await ingester.search(
        query="pod keeps crashing",
        n_results=5
    )

    # Display results
    for i, result in enumerate(results, 1):
        print(f"\n--- Result {i} ---")
        print(f"Title: {result['metadata'].get('title')}")
        print(f"Score: {result['relevance_score']:.3f}")
        print(f"Snippet: {result['document'][:200]}...")

asyncio.run(main())
```

```bash
# Run search test
python test_search.py
```

### 4. Collection Statistics

```python
# Get knowledge base stats
from faultmaven.core.knowledge.ingestion import KnowledgeIngester
from faultmaven.config.settings import get_settings

ingester = KnowledgeIngester(settings=get_settings())
stats = ingester.get_collection_stats()

print(f"Total chunks: {stats['total_chunks']}")
print(f"Document types: {stats['document_types']}")
print(f"Top tags: {stats['top_tags']}")
```

### 5. Validation Tools

#### Validate Single Runbook

```bash
# Create validation script
cat > validate_runbook.py << 'EOF'
from pathlib import Path
import yaml

def validate_runbook(file_path):
    with open(file_path, 'r') as f:
        content = f.read()

    # Check YAML frontmatter
    if not content.startswith('---'):
        print("❌ Missing YAML frontmatter")
        return False

    # Parse metadata
    parts = content.split('---', 2)
    metadata = yaml.safe_load(parts[1])

    # Required fields
    required = ['id', 'title', 'technology', 'severity', 'tags',
                'difficulty', 'version', 'last_updated', 'verified_by', 'status']

    for field in required:
        if field not in metadata:
            print(f"❌ Missing field: {field}")
            return False

    print("✓ Validation passed")
    return True

if __name__ == "__main__":
    import sys
    validate_runbook(sys.argv[1])
EOF

# Run validation
python validate_runbook.py docs/runbooks/kubernetes/k8s-pod-crashloopbackoff.md
```

### 6. Useful Commands Reference

#### Git Operations

```bash
# Update fork from upstream
git fetch upstream
git checkout main
git merge upstream/main
git push origin main

# Create runbook branch
git checkout -b runbook/new-problem

# Check file status
git status
git diff docs/runbooks/
```

#### File Operations

```bash
# List all runbooks
find docs/runbooks -name "*.md" -type f | grep -v README | grep -v TEMPLATE

# Count runbooks by technology
ls docs/runbooks/*/k8s-*.md | wc -l
ls docs/runbooks/*/redis-*.md | wc -l

# Find runbooks by tag
grep -r "tags:" docs/runbooks/ | grep "crashloop"

# Check runbook size
du -h docs/runbooks/kubernetes/*.md
```

#### ChromaDB Operations

```bash
# Connect to ChromaDB (via Python)
python << EOF
import chromadb
from chromadb.config import Settings

client = chromadb.HttpClient(
    host="chromadb.faultmaven.local",
    port=30080
)

collection = client.get_collection("faultmaven_kb")
print(f"Total items: {collection.count()}")
EOF
```

### 7. Troubleshooting Tools

#### Common Issues & Fixes

**Issue: Ingestion fails**
```bash
# Check ChromaDB connection
curl http://chromadb.faultmaven.local:30080/api/v1/heartbeat

# Check BGE-M3 model
python -c "from faultmaven.infrastructure.model_cache import model_cache; print(model_cache.get_bge_m3_model())"

# Run in debug mode
LOG_LEVEL=DEBUG python -m faultmaven.scripts.ingest_runbooks --dry-run
```

**Issue: Search returns no results**
```bash
# Check collection stats
python -c "
from faultmaven.core.knowledge.ingestion import KnowledgeIngester
from faultmaven.config.settings import get_settings
ingester = KnowledgeIngester(settings=get_settings())
print(ingester.get_collection_stats())
"

# Verify embeddings
python -c "
from faultmaven.infrastructure.model_cache import model_cache
model = model_cache.get_bge_m3_model()
embedding = model.encode('test query')
print(f'Embedding shape: {embedding.shape}')
"
```

**Issue: Validation errors**
```bash
# Run dry-run to see all errors
python -m faultmaven.scripts.ingest_runbooks --dry-run --status all

# Check specific runbook
python validate_runbook.py docs/runbooks/path/to/runbook.md
```

---

## Quality Standards

### 1. Quality Scoring Framework

**4-Dimension Evaluation:**

| Dimension | Weight | Target Score | Measurement |
|-----------|--------|--------------|-------------|
| **Relevancy** | 40% | 90+ | Query retrieval accuracy |
| **Completeness** | 25% | 90+ | Required sections present |
| **Actionability** | 20% | 85+ | Commands, steps, solutions |
| **Structure** | 15% | 95+ | Format compliance |
| **Overall** | 100% | **90+** | **Weighted average** |

### 2. Quality Requirements

#### Minimum Standards

**All runbooks must have:**
- ✅ All 10 YAML metadata fields
- ✅ All 5 required sections
- ✅ At least 3 diagnostic commands
- ✅ At least 2 solution paths
- ✅ Code examples with explanations
- ✅ Time estimates for resolution
- ✅ Security considerations
- ✅ Prevention strategies

#### Excellence Standards (A+ grade)

**High-quality runbooks include:**
- ✅ Comprehensive diagnostic workflow
- ✅ 3+ solution paths with tradeoffs
- ✅ Root cause analysis
- ✅ Visual diagrams (when applicable)
- ✅ Real-world examples
- ✅ Performance implications
- ✅ Cost considerations
- ✅ Links to official documentation

### 3. Content Guidelines

#### Writing Style

**DO:**
- ✅ Use clear, concise language
- ✅ Write in active voice
- ✅ Provide specific examples
- ✅ Include expected outputs
- ✅ Explain why, not just how

**DON'T:**
- ❌ Use jargon without explanation
- ❌ Assume knowledge level
- ❌ Provide untested commands
- ❌ Skip security warnings
- ❌ Ignore edge cases

#### Code Examples

**Good example:**
```bash
# Check pod status and identify restarts
kubectl get pod <pod-name> -n <namespace>

# Expected output:
# NAME              READY   STATUS             RESTARTS   AGE
# my-app-xyz123     0/1     CrashLoopBackOff   5          3m

# Explanation: 5 restarts in 3 minutes indicates a crash loop
```

**Bad example:**
```bash
kubectl get pod <pod-name>
```

### 4. Security Standards

**Required security practices:**
- ✅ Warn before destructive commands
- ✅ Document privilege requirements
- ✅ Sanitize all examples (no real credentials)
- ✅ Explain security implications
- ✅ Provide least-privilege alternatives

**Example:**
```bash
# ⚠️ WARNING: This command affects production
# Requires: cluster-admin role
# Impact: Deletes pod and triggers restart

kubectl delete pod <pod-name> -n <namespace> --force
```

### 5. Versioning Standards

**Semantic versioning:**
- `1.0.0` - Initial verified version
- `1.1.0` - Minor update (new solution added)
- `1.0.1` - Patch (typo fix, clarification)
- `2.0.0` - Major update (significant restructure)

**Version history:**
```markdown
## Version History

| Version | Date       | Author      | Changes                    |
|---------|------------|-------------|----------------------------|
| 1.1.0   | 2025-01-20 | Jane Doe    | Added network policy check |
| 1.0.0   | 2025-01-15 | John Smith  | Initial verified version   |
```

### 6. Benchmark Queries

**Test runbook retrievability:**

20+ benchmark queries ensure runbooks are discoverable:

```python
# Example queries
{
    "query": "pod keeps crashing and restarting",
    "expected_runbook_id": "k8s-pod-crashloopbackoff",
    "category": "kubernetes"
}

{
    "query": "kubernetes pod out of memory",
    "expected_runbook_id": "k8s-pod-oomkilled",
    "category": "kubernetes"
}
```

**Quality targets:**
- ✅ 100% accuracy on exact matches
- ✅ 90%+ accuracy on natural language queries
- ✅ Top-3 retrieval for related queries

---

## FAQ

### General Questions

**Q: Who can contribute to the knowledge base?**
A: Anyone! Community contributions are encouraged. All contributions go through our review process to ensure quality.

**Q: How long does review take?**
A: Knowledge Curators review PRs within 48 hours. Simple updates may be approved faster, complex runbooks may take longer.

**Q: Can I update someone else's runbook?**
A: Yes! If you have improvements or updates, submit a PR. Original author will be credited, and you'll be added as a contributor.

**Q: What happens to deprecated runbooks?**
A: They're marked as `status: deprecated` in metadata, remain searchable but with lower priority, and eventually archived after 6 months.

### Technical Questions

**Q: How does the AI agent use runbooks?**
A: When a user asks a question, the agent:
1. Generates query embeddings
2. Searches ChromaDB vector database
3. Retrieves top-k relevant runbook chunks
4. Uses context to generate response
5. Optionally cites runbook sources

**Q: How often is the knowledge base updated?**
A: Automatically on every PR merge. Manual batch updates can be triggered anytime.

**Q: What if a runbook command doesn't work?**
A: Submit an issue or PR to fix it! Include error details, your environment, and suggested fix.

**Q: How are embeddings generated?**
A: We use BGE-M3 (BAAI General Embedding Model v3) which creates 1024-dimensional embeddings optimized for semantic search.

**Q: Can I test runbooks locally?**
A: Yes! Clone the repo, activate the virtual environment, and run the ingestion pipeline against a local ChromaDB instance.

### Contribution Questions

**Q: Do I need to be an expert to contribute?**
A: No! Beginner-level runbooks are valuable too. If you've solved a problem, document it. Our review process ensures quality.

**Q: What if I don't know all the metadata fields?**
A: Copy `TEMPLATE.md` and fill what you know. Reviewers will help complete missing fields.

**Q: Can I contribute in languages other than English?**
A: Currently English only. Multi-language support is planned for future phases.

**Q: How do I get credit for my contribution?**
A: Your GitHub username appears in:
- Runbook `verified_by` field (if you're the primary author)
- Git commit history
- Contributors list in README.md

### Quality Questions

**Q: What's a good quality score?**
A:
- 90+ = Excellent (A grade)
- 80-89 = Good (B grade)
- 70-79 = Acceptable (C grade)
- <70 = Needs improvement

**Q: How is quality measured?**
A: 4-dimension framework:
- Relevancy (40%): Search accuracy
- Completeness (25%): Required sections
- Actionability (20%): Commands and solutions
- Structure (15%): Format compliance

**Q: Can a runbook be rejected?**
A: Yes, if it:
- Contains security vulnerabilities
- Has fundamentally incorrect information
- Doesn't meet minimum quality standards
- Duplicates existing content without adding value

**Q: What if my runbook gets a low score?**
A: Reviewers will provide specific feedback. Address the issues and resubmit. Low scores are learning opportunities!

### Maintenance Questions

**Q: Who maintains the knowledge base long-term?**
A: The FaultMaven team oversees it, but community contributions drive growth. We have dedicated Knowledge Curators for reviews.

**Q: How do you handle outdated content?**
A: Monthly audits identify outdated runbooks. We update or deprecate them based on technology evolution.

**Q: What if a technology version changes?**
A: Update the runbook with new version info. Keep backward compatibility notes if applicable.

**Q: How do I report a problem with a runbook?**
A:
1. Create GitHub issue with `runbook-issue` label
2. Include runbook ID and specific problem
3. Suggest fix if possible
4. Tag Knowledge Curators

---

## Getting Help

### Resources

**Documentation:**
- 📖 [Runbook Template](docs/runbooks/TEMPLATE.md)
- 📖 [Contributing Guide](docs/runbooks/CONTRIBUTING.md)
- 📖 [Review Guidelines](docs/runbooks/REVIEW_GUIDELINES.md)
- 📖 [Baseline Quality Report](docs/PHASE_0_BASELINE.md)

**Community:**
- 💬 GitHub Discussions - Ask questions
- 🐛 GitHub Issues - Report problems
- 📧 Email: knowledge@faultmaven.com
- 👥 Slack: #knowledge-base channel

**Support:**
- 🆘 Knowledge Curator on-call (48hr response)
- 📚 Example runbooks for reference
- 🎥 Video tutorials (coming soon)

### Contact

**Knowledge Base Team:**
- Lead Curator: FaultMaven Team
- Technical Contact: knowledge@faultmaven.com
- Community Manager: community@faultmaven.com

**Office Hours:**
- Weekly Wednesday 2-3pm EST
- Open Q&A and contribution help
- Zoom link: [Coming Soon]

---

## Appendix

### A. File Locations

```
FaultMaven/
├── docs/
│   ├── KNOWLEDGE_BASE_SYSTEM.md          # This document
│   ├── PHASE_0_KNOWLEDGE_BASE_PLAN.md    # Implementation plan
│   ├── PHASE_0_BASELINE.md               # Quality measurements
│   └── runbooks/                         # Runbook content
│       ├── README.md
│       ├── TEMPLATE.md
│       ├── CONTRIBUTING.md
│       ├── REVIEW_GUIDELINES.md
│       └── [technology]/
│           └── [runbook].md
│
├── faultmaven/
│   ├── core/knowledge/
│   │   └── ingestion.py                  # Ingestion engine
│   └── scripts/
│       └── ingest_runbooks.py            # Ingestion pipeline
│
└── tests/quality/
    ├── benchmark_queries.py              # Test queries
    ├── quality_scorer.py                 # Scoring engine
    └── test_baseline_quality.py          # Quality tests
```

### B. Quick Reference Commands

```bash
# INGESTION
python -m faultmaven.scripts.ingest_runbooks --status verified
python -m faultmaven.scripts.ingest_runbooks --dry-run --status all
python -m faultmaven.scripts.ingest_runbooks --technology kubernetes --force

# QUALITY TESTING
pytest tests/quality/ -v
pytest tests/quality/test_baseline_quality.py::test_generate_baseline_report -v -s

# VALIDATION
python validate_runbook.py docs/runbooks/[technology]/[runbook].md

# STATISTICS
python -c "from faultmaven.core.knowledge.ingestion import KnowledgeIngester; from faultmaven.config.settings import get_settings; ingester = KnowledgeIngester(settings=get_settings()); print(ingester.get_collection_stats())"

# GIT WORKFLOW
git checkout -b runbook/[technology]-[problem]
git add docs/runbooks/[technology]/[runbook].md
git commit -m "Add runbook: [Title]"
git push origin runbook/[technology]-[problem]
```

### C. Glossary

| Term | Definition |
|------|------------|
| **Runbook** | Step-by-step troubleshooting guide for specific problems |
| **Knowledge Base** | Collection of runbooks stored in ChromaDB |
| **RAG** | Retrieval-Augmented Generation - LLM + knowledge retrieval |
| **ChromaDB** | Vector database for semantic search |
| **BGE-M3** | Embedding model (BAAI General Embedding v3) |
| **Embedding** | Vector representation of text for similarity search |
| **Chunk** | Text segment (1000 chars) with overlap for context |
| **Knowledge Curator** | Designated reviewer for runbook quality |
| **Ingestion** | Process of adding runbooks to knowledge base |
| **Frontmatter** | YAML metadata at top of markdown file |
| **Semantic Search** | Search by meaning, not just keywords |

---

---

## Other Knowledge Base Systems

This document focuses on the **Global Knowledge Base** (System 2). For the other two systems:

### User Knowledge Base (System 1) - ⏳ Planned

**Purpose**: Personal/team runbooks and procedures

**Status**: Not yet implemented (Phase 2)

**Planned Features**:
- Per-user permanent knowledge storage (`user_{user_id}_kb` collections)
- Private runbooks and playbooks accessible across all user's cases
- Knowledge Management UI for upload/organization
- Cross-case accessibility for personal procedures

**Documentation**: See [Knowledge Base Architecture](../architecture/knowledge-base-architecture.md#system-1-user-knowledge-base-per-user-permanent-storage)

---

### Case Evidence Store (System 3) - ✅ Implemented

**Purpose**: Temporary storage for case-specific troubleshooting evidence

**Status**: Production deployed with lifecycle-based cleanup

**Key Features**:
- Case-specific collections (`case_{case_id}`)
- Evidence upload via browser extension during active investigations
- QA sub-agent for evidence questions
- Lifecycle-based cleanup (deleted when case closes/archives)
- Background orphan detection every 6 hours

**Documentation**:
- [Case Evidence Store Feature](../features/case-evidence-store.md)
- [Knowledge Base Architecture](../architecture/knowledge-base-architecture.md#system-3-case-evidence-store-ephemeral-case-specific-storage)

---

**Document Version:** 2.0
**Last Updated:** 2025-10-18
**Maintained By:** FaultMaven Team
**License:** Apache-2.0
