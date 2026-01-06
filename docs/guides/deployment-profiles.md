# Deployment Profiles User Guide

**Version**: 1.0
**Date**: 2026-01-06
**Related Documents**:
- [Architectural Design Principles](../architecture/architectural-design-principles.md)
- [Deployment Strategy v2](../architecture/deployment-strategy-v2.md)

---

## Overview

FaultMaven supports three deployment profiles that allow the same codebase to run in different environments with different infrastructure requirements:

- **CORE** - Community Edition (zero external dependencies)
- **TEAM** - Team Edition (PostgreSQL, Redis, basic cloud storage)
- **ENTERPRISE** - Enterprise Edition (full infrastructure stack with compliance features)

The Deployment Profile Pattern ensures **zero conditional logic** in business services by using provider substitution at startup.

---

## Quick Start

### Community Edition (CORE Profile)

Perfect for local development, demos, and community deployments.

```bash
# .env
DEPLOYMENT_PROFILE=core

# That's it! Zero configuration required.
# Uses: SQLite, in-memory sessions, local file storage
```

```bash
# Start FaultMaven
python -m faultmaven.main
```

**Features**:
- ✅ SQLite database (local file)
- ✅ In-memory session storage
- ✅ Local filesystem storage
- ✅ 3 LLM providers (OpenAI, Anthropic, Local)
- ❌ No PII redaction (Presidio)
- ❌ No distributed tracing (Opik)
- ❌ No metrics collection

---

### Team Edition (TEAM Profile)

For small teams requiring shared infrastructure.

```bash
# .env
DEPLOYMENT_PROFILE=team

# Required configuration
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/faultmaven
REDIS_HOST=localhost
REDIS_PORT=6379

# Optional: Cloud storage
STORAGE_BACKEND=s3
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret
S3_BUCKET_NAME=faultmaven-evidence
```

**Features**:
- ✅ PostgreSQL database
- ✅ Redis session storage
- ✅ S3 or MinIO file storage
- ✅ 5 LLM providers
- ✅ Basic metrics collection
- ❌ No PII redaction
- ❌ No distributed tracing

---

### Enterprise Edition (ENTERPRISE Profile)

For organizations requiring compliance, security, and observability.

```bash
# .env
DEPLOYMENT_PROFILE=enterprise

# Required configuration
DATABASE_URL=postgresql+asyncpg://user:pass@postgres:5432/faultmaven
REDIS_HOST=redis
REDIS_PORT=6379

# PII Protection (required)
PRESIDIO_URL=http://presidio:3000
PRESIDIO_ANALYZER_ENABLED=true
PRESIDIO_ANONYMIZER_ENABLED=true

# Distributed Tracing (required)
OPIK_API_KEY=your-opik-api-key
OPIK_WORKSPACE=your-workspace
TRACING_ENABLED=true

# Metrics & Observability
PROMETHEUS_ENABLED=true
METRICS_EXPORTER=prometheus_http

# Cloud Storage
STORAGE_BACKEND=s3
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret
S3_BUCKET_NAME=faultmaven-evidence

# Multi-Tenancy
TENANT_PROVIDER=multi
```

**Features**:
- ✅ PostgreSQL database
- ✅ Redis session storage
- ✅ S3 file storage
- ✅ 7 LLM providers (all supported)
- ✅ PII redaction (Presidio)
- ✅ Distributed tracing (Opik)
- ✅ Prometheus metrics
- ✅ Multi-tenant isolation

---

## Feature Matrix

| Feature | CORE | TEAM | ENTERPRISE |
|---------|------|------|------------|
| **Database** | SQLite | PostgreSQL | PostgreSQL |
| **Session Storage** | In-Memory | Redis | Redis |
| **File Storage** | Local Filesystem | S3/MinIO | S3 |
| **Vector Store** | In-Memory | ChromaDB | ChromaDB |
| **PII Redaction** | ❌ | ❌ | ✅ Presidio |
| **Distributed Tracing** | ❌ | ❌ | ✅ Opik |
| **Metrics Collection** | ❌ | ✅ Basic | ✅ Prometheus |
| **LLM Providers** | 3 | 5 | 7 |
| **Multi-Tenancy** | ❌ | ❌ | ✅ |
| **External Dependencies** | 0 | 2-3 | 6+ |

---

## Environment Variables by Profile

### CORE Profile

```bash
# Deployment profile
DEPLOYMENT_PROFILE=core

# No other configuration required!
# Defaults to SQLite, in-memory, local files
```

### TEAM Profile

```bash
# Deployment profile
DEPLOYMENT_PROFILE=team

# Database (required)
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db

# Redis (required)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0

# Storage (optional, defaults to filesystem)
STORAGE_BACKEND=s3  # or "filesystem"
S3_BUCKET_NAME=faultmaven-evidence
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret

# Vector storage (optional, defaults to ChromaDB)
VECTOR_STORAGE_TYPE=chromadb
CHROMADB_URL=http://chromadb:8000
```

### ENTERPRISE Profile

```bash
# Deployment profile
DEPLOYMENT_PROFILE=enterprise

# Database (required)
DATABASE_URL=postgresql+asyncpg://user:pass@postgres:5432/faultmaven

# Redis (required)
REDIS_HOST=redis
REDIS_PORT=6379

# PII Protection (required)
PRESIDIO_URL=http://presidio:3000
PROTECTION_ENABLED=true

# Distributed Tracing (required)
OPIK_API_KEY=your-opik-api-key
OPIK_WORKSPACE=your-workspace
TRACING_ENABLED=true

# Metrics (required)
PROMETHEUS_ENABLED=true
METRICS_EXPORTER=prometheus_http

# Storage (required)
STORAGE_BACKEND=s3
S3_BUCKET_NAME=faultmaven-evidence
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret

# Multi-Tenancy (optional)
TENANT_PROVIDER=multi
```

---

## Profile Validation

FaultMaven validates profile requirements at startup and will **fail-fast** if required dependencies are missing.

### Validation Examples

**TEAM Profile - Missing Redis**:
```
❌ TEAM profile requires REDIS_HOST or REDIS_URL environment variable
```

**ENTERPRISE Profile - Missing Presidio**:
```
❌ ENTERPRISE profile requires PRESIDIO_URL environment variable
   (or set SKIP_SERVICE_CHECKS=true for testing)
```

**Bypass Validation (Testing Only)**:
```bash
# DANGER: Only use for testing!
SKIP_SERVICE_CHECKS=true
```

---

## Switching Profiles

### From CORE to TEAM

1. Provision infrastructure:
   ```bash
   docker run -d -p 5432:5432 postgres:15
   docker run -d -p 6379:6379 redis:7
   ```

2. Update `.env`:
   ```bash
   DEPLOYMENT_PROFILE=team
   DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/faultmaven
   REDIS_HOST=localhost
   ```

3. Run migrations:
   ```bash
   alembic upgrade head
   ```

4. Restart FaultMaven

### From TEAM to ENTERPRISE

1. Deploy additional services:
   ```bash
   docker run -d -p 3000:3000 presidio
   ```

2. Update `.env`:
   ```bash
   DEPLOYMENT_PROFILE=enterprise
   PRESIDIO_URL=http://localhost:3000
   OPIK_API_KEY=your-api-key
   PROMETHEUS_ENABLED=true
   ```

3. Restart FaultMaven

---

## Provider Selection Logic

The Deployment Profile Pattern uses **provider substitution** instead of conditional logic:

### Vector Store Example

```python
# WRONG: Conditional logic in business code ❌
if deployment_mode == "local":
    vector_store = InMemoryVectorStore()
else:
    vector_store = ChromaDBVectorStore()

# CORRECT: Profile-based provider selection ✅
profile = ProfileManager.get_current_profile()
if profile == DeploymentProfile.CORE:
    vector_store = InMemoryVectorStore()
else:
    vector_store = ChromaDBVectorStore()
```

**Business services never check the profile** - they only depend on interfaces:

```python
class KnowledgeSearchService:
    def __init__(self, vector_store: IVectorStore):
        self.vector_store = vector_store  # Works with any implementation!
```

---

## Testing with Profiles

### Unit Tests

```python
from faultmaven.config.deployment_profile import ProfileManager, DeploymentProfile

def test_my_feature():
    # Override profile for testing
    ProfileManager.set_profile(DeploymentProfile.CORE)

    # Test with CORE profile
    result = my_function()
    assert result.uses_sqlite

    # Clean up
    ProfileManager.reset_profile()
```

### Integration Tests

```python
@pytest.mark.parametrize("profile", [
    DeploymentProfile.CORE,
    DeploymentProfile.TEAM,
    DeploymentProfile.ENTERPRISE,
])
def test_startup_with_profile(profile):
    ProfileManager.set_profile(profile)
    # Test startup logic...
```

---

## Troubleshooting

### Issue: "Profile validation failed"

**Cause**: Missing required environment variables for the selected profile.

**Solution**: Check the error message and add the missing variables:
```bash
# Error: "ENTERPRISE profile requires PRESIDIO_URL"
export PRESIDIO_URL=http://localhost:3000
```

**Or** use SKIP_SERVICE_CHECKS for testing:
```bash
export SKIP_SERVICE_CHECKS=true
```

### Issue: "Invalid DEPLOYMENT_PROFILE"

**Cause**: Typo in profile name.

**Valid Values**: `core`, `team`, `enterprise` (case-insensitive)

**Solution**:
```bash
# Wrong
DEPLOYMENT_PROFILE=prod  # ❌

# Correct
DEPLOYMENT_PROFILE=enterprise  # ✅
```

### Issue: Services using wrong provider

**Cause**: Profile not detected correctly.

**Check**:
1. Verify `DEPLOYMENT_PROFILE` in environment
2. Check startup logs: `🔍 DEPLOYMENT_PROFILE = CORE`
3. Restart application after changing profile

---

## Best Practices

### 1. Use Environment-Specific `.env` Files

```bash
# .env.local (CORE)
DEPLOYMENT_PROFILE=core

# .env.staging (TEAM)
DEPLOYMENT_PROFILE=team
DATABASE_URL=postgresql://...
REDIS_HOST=staging-redis

# .env.production (ENTERPRISE)
DEPLOYMENT_PROFILE=enterprise
DATABASE_URL=postgresql://...
PRESIDIO_URL=https://presidio.internal
```

### 2. Validate Before Deployment

```bash
# Dry-run validation
python -c "
from faultmaven.config.deployment_profile import ProfileManager
is_valid, errors = ProfileManager.validate_profile_requirements()
if not is_valid:
    for error in errors:
        print(f'ERROR: {error}')
    exit(1)
print('Profile validation passed!')
"
```

### 3. Document Profile in Deployment Manifests

**Docker Compose**:
```yaml
services:
  faultmaven:
    image: faultmaven:latest
    environment:
      DEPLOYMENT_PROFILE: team
      DATABASE_URL: postgresql://...
```

**Kubernetes**:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: faultmaven-config
data:
  DEPLOYMENT_PROFILE: "enterprise"
```

---

## Migration Guide

### Migrating from Legacy Configuration

**Before** (legacy):
```bash
DATABASE_URL=sqlite:///./data/faultmaven.db
SESSION_STORAGE_TYPE=inmemory
VECTOR_STORAGE_TYPE=inmemory
```

**After** (profile-based):
```bash
DEPLOYMENT_PROFILE=core
# All provider selection handled automatically!
```

**Benefits**:
- ✅ Single variable controls all providers
- ✅ Guaranteed compatible configuration
- ✅ Validated at startup
- ✅ Clear upgrade path

---

## FAQ

### Q: Can I override individual providers?

**A**: Yes! Profile provides defaults, but you can override:

```bash
DEPLOYMENT_PROFILE=core
VECTOR_STORAGE_TYPE=chromadb  # Override default in-memory
```

### Q: What happens if I don't set DEPLOYMENT_PROFILE?

**A**: Defaults to CORE (zero dependencies).

### Q: Can I create custom profiles?

**A**: Not yet. Submit a feature request if you need custom profiles.

### Q: Does changing profile require data migration?

**A**: Yes, if changing database backend (SQLite → PostgreSQL):
1. Export data from SQLite
2. Run migrations on PostgreSQL
3. Import data

---

## Support

For profile-related issues:
- **Documentation**: [Deployment Strategy v2](../architecture/deployment-strategy-v2.md)
- **GitHub Issues**: https://github.com/faultmaven/faultmaven/issues
- **Community**: https://discord.gg/faultmaven

---

**Last Updated**: 2026-01-06
**Version**: 1.0
**Status**: Active
