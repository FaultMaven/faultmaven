# Move Manifest: Batch 1 - Safe Moves

**Date**: 2026-01-04
**Branch**: `docs/reorg-batch1-safe-moves`
**Playbook Reference**: Section 6 (Immediate "Safe Moves")

## Summary

This batch moves 7 files from the public `faultmaven` repo to their appropriate destination repositories:

- **1 file** → `faultmaven-doc-internal` (Enterprise documentation)
- **6 files** → `faultmaven-enterprise-infra` (Kubernetes/platform implementation)

## Move Table

| Source Path | Tag | Destination Repo/Path | Action | Notes |
|-------------|-----|----------------------|--------|-------|
| `docs/schema/003_enterprise_user_schema.sql` | enterprise | `faultmaven-doc-internal/schema/003_enterprise_user_schema.sql` | move | Multi-tenancy, RBAC, organizations, teams |
| `docs/infrastructure/opik-setup.md` | infra | `faultmaven-enterprise-infra/docs/infrastructure/opik-setup.md` | move | NodePort 30080, cluster IP 192.168.0.111 |
| `docs/runbooks/kubernetes/README.md` | infra | `faultmaven-enterprise-infra/docs/runbooks/kubernetes/README.md` | move | K8s runbook index |
| `docs/runbooks/kubernetes/k8s-node-not-ready.md` | infra | `faultmaven-enterprise-infra/docs/runbooks/kubernetes/k8s-node-not-ready.md` | move | K8s node troubleshooting |
| `docs/runbooks/kubernetes/k8s-pod-crashloopbackoff.md` | infra | `faultmaven-enterprise-infra/docs/runbooks/kubernetes/k8s-pod-crashloopbackoff.md` | move | K8s pod troubleshooting |
| `docs/runbooks/kubernetes/k8s-pod-imagepullbackoff.md` | infra | `faultmaven-enterprise-infra/docs/runbooks/kubernetes/k8s-pod-imagepullbackoff.md` | move | K8s pod troubleshooting |
| `docs/runbooks/kubernetes/k8s-pod-oomkilled.md` | infra | `faultmaven-enterprise-infra/docs/runbooks/kubernetes/k8s-pod-oomkilled.md` | move | K8s pod troubleshooting |

## Classification Justification

### Enterprise Files (1 file)

**`003_enterprise_user_schema.sql`**:
- Contains enterprise SaaS concepts: organizations, teams, RBAC, multi-tenancy
- Implements Row-Level Security (RLS) for tenant isolation
- Keywords: `organizations`, `organization_members`, `teams`, `roles`, `permissions`, `audit`
- Classification: **enterprise** (per Section 2.2 keyword heuristics)

### Infrastructure Files (6 files)

**Kubernetes Runbooks (5 files)**:
- `k8s-node-not-ready.md`: K8s node troubleshooting
- `k8s-pod-crashloopbackoff.md`: K8s pod lifecycle issues
- `k8s-pod-imagepullbackoff.md`: K8s image registry issues
- `k8s-pod-oomkilled.md`: K8s resource limit issues
- `README.md`: Index for K8s runbooks

All files explicitly contain:
- Kubernetes-specific commands: `kubectl`, `describe pod`, `logs`, `get nodes`
- K8s resource types: pods, nodes, deployments, services
- Classification: **infra** (per Section 2.2 keyword heuristics)

**Opik Setup**:
- `opik-setup.md`: Contains `opik.faultmaven.local:30080` (NodePort), cluster IP `192.168.0.111`
- Team server setup, not local development
- Classification: **infra** (per Section 2.1 - K8s platform-specific)

## Link Hygiene Updates

### Files Updated in `faultmaven` (public repo)

1. **`alembic/versions/20251229_0412_001_baseline_schema.py`**
   - **Change**: Updated docstring comment
   - **Before**: `- docs/schema/003_enterprise_user_schema.sql (Enterprise user/teams)`
   - **After**: `- Enterprise user/teams schema (Enterprise Edition)`
   - **Reason**: Remove dead link to moved file, indicate Enterprise-only

2. **`docs/schema/README.md`**
   - **Changes**:
     - Replaced detailed 003 schema section with "Enterprise Edition" placeholder
     - Updated application order to reference "Enterprise schema (Enterprise Edition only)"
     - Removed file path references from PostgreSQL CLI, Docker, and K8s examples
   - **Reason**: Follow playbook Section 4.3 - replace hyperlinks with plain text for Enterprise-only content

### No Cross-Repo Links Created

Per playbook Section 4.3 ("Public-to-Private link trap"):
- **Did NOT** create links from public docs to private repos
- **Did** replace with plain text: "Enterprise Edition", "Enterprise schema available in Enterprise Edition"

## Pre-Flight Checks Performed

### Schema Pre-Flight (Playbook Section 6.1)

✅ **Verified no code dependencies on `docs/schema/`**:

```bash
grep -r "docs/schema" faultmaven/ --include="*.py" --include="*.sh"
```

**Result**: Only documentation comments in Alembic migration (updated accordingly)

### Destination Lint Check (Playbook Section 4.1)

⚠️ **Pending**: Need to run destination repo lint checks after committing files:

```bash
# faultmaven-doc-internal
cd /home/swhouse/product/faultmaven-doc-internal && pre-commit run --all-files

# faultmaven-enterprise-infra
cd /home/swhouse/product/faultmaven-enterprise-infra && pre-commit run --all-files
```

## Commits Required

### Repository: `faultmaven` (public)

**Branch**: `docs/reorg-batch1-safe-moves`

**Files changed**:
- Deleted: 7 files (1 schema, 1 infrastructure, 5 kubernetes runbooks)
- Modified: 2 files (alembic migration comment, schema/README.md)

**Commit message**:
```
docs: move enterprise/infra docs to appropriate repos (batch 1)

Move enterprise and infrastructure documentation out of public repo
to their appropriate destinations per documentation reorganization plan.

Moved to faultmaven-doc-internal:
- docs/schema/003_enterprise_user_schema.sql → schema/

Moved to faultmaven-enterprise-infra:
- docs/infrastructure/opik-setup.md → docs/infrastructure/
- docs/runbooks/kubernetes/* → docs/runbooks/kubernetes/

Updated references:
- alembic migration docstring (removed dead link)
- docs/schema/README.md (enterprise schema → plain text)

Refs: REPO-RESTRUCTURE-2026-01-03.md Section 6 (Safe Moves)
```

### Repository: `faultmaven-doc-internal`

**Branch**: TBD (has pending changes on main)

**Files added**:
- `schema/003_enterprise_user_schema.sql` (24KB)

**Commit message**:
```
docs: add enterprise user schema from public repo

Moved enterprise multi-tenancy schema (organizations, teams, RBAC)
from public faultmaven repo to internal documentation.

This schema implements:
- Organizations and organization members
- Teams and team membership
- RBAC with roles and permissions
- User audit logging
- Row-Level Security (RLS) for multi-tenant isolation

Source: faultmaven/docs/schema/003_enterprise_user_schema.sql
Refs: FAULTMAVEN-DOCS-CLASSIFICATION-MANIFEST-2026-01-04.md
```

### Repository: `faultmaven-enterprise-infra`

**Branch**: `docs/add-k8s-runbooks-and-opik-setup`

**Files added**:
- `docs/infrastructure/opik-setup.md` (8.8KB)
- `docs/runbooks/kubernetes/README.md` (1.4KB)
- `docs/runbooks/kubernetes/k8s-node-not-ready.md` (6.8KB)
- `docs/runbooks/kubernetes/k8s-pod-crashloopbackoff.md` (20KB)
- `docs/runbooks/kubernetes/k8s-pod-imagepullbackoff.md` (15KB)
- `docs/runbooks/kubernetes/k8s-pod-oomkilled.md` (23KB)

**Commit message**:
```
docs: add Kubernetes runbooks and Opik setup from public repo

Moved Kubernetes-specific operational runbooks and observability
setup documentation from public faultmaven repo to enterprise infra.

Added runbooks:
- K8s node troubleshooting (not ready, disk pressure, etc.)
- K8s pod troubleshooting (CrashLoopBackOff, ImagePullBackOff, OOMKilled)
- Opik observability setup (team server, NodePort 30080)

Source: faultmaven/docs/{infrastructure,runbooks/kubernetes}/
Refs: FAULTMAVEN-DOCS-CLASSIFICATION-MANIFEST-2026-01-04.md
```

## Verification Checklist

After merging all three PRs, verify:

- [ ] Public repo `faultmaven` has NO enterprise-only schema files
- [ ] Public repo `faultmaven` has NO Kubernetes/platform runbooks
- [ ] Public docs contain **Core + Local only** content
- [ ] No broken links in public `docs/schema/README.md`
- [ ] No broken links in `alembic/versions/20251229_0412_001_baseline_schema.py`
- [ ] Enterprise schema accessible in `faultmaven-doc-internal/schema/`
- [ ] K8s runbooks accessible in `faultmaven-enterprise-infra/docs/runbooks/kubernetes/`
- [ ] Opik setup accessible in `faultmaven-enterprise-infra/docs/infrastructure/`

## Quality Gates (Playbook Section 5)

### Public Repo Gate 5.1
✅ **No enterprise SaaS ops**: Removed multi-tenancy schema
✅ **No platform implementation**: Removed K8s runbooks and NodePort configs
✅ **No canonical indexes point to `docs/working/`**: Not affected by this batch

### Internal/Infra Repo Gates 5.2
✅ **Internal docs provide enterprise strategy**: Schema describes org/team model
✅ **Infra repo contains platform implementation**: K8s runbooks and cluster config

### No Duplicates 5.3
✅ **One canonical copy per document**: Files moved (not copied) using `git rm`
✅ **No redirect stubs**: Files completely removed from public repo

## Next Steps

1. Commit changes to `faultmaven` (this branch)
2. Create branch and commit to `faultmaven-enterprise-infra` (clean repo)
3. Handle `faultmaven-doc-internal` pending changes (coordinate with other work)
4. Create PRs for all three repos
5. Run destination lint checks
6. Verify all quality gates post-merge

---

**Manifest created**: 2026-01-04
**Execution status**: Files moved, references updated, ready to commit
