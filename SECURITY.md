# Security Policy

## Supported Versions

We take security seriously and provide security updates for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| 1.x     | :white_check_mark: |
| < 1.0   | :x:                |

---

## Reporting a Vulnerability

**DO NOT** open public GitHub issues for security vulnerabilities.

### How to Report

Please report security vulnerabilities by emailing:

**engineering@faultmaven.ai**

Include the following information:

1. **Description** of the vulnerability
2. **Steps to reproduce** the issue
3. **Potential impact** (what an attacker could do)
4. **Affected versions** (if known)
5. **Suggested fix** (if you have one)

### What to Expect

- **Acknowledgment:** We'll respond within 48 hours
- **Initial Assessment:** Within 5 business days, we'll provide an initial assessment
- **Updates:** We'll keep you informed as we investigate and develop a fix
- **Disclosure:** We'll coordinate public disclosure timing with you

### Security Update Process

1. We verify and investigate the report
2. We develop and test a fix
3. We prepare a security advisory
4. We release a patched version
5. We publish the security advisory (after giving users time to update)

---

## Security Best Practices for Self-Hosted Deployments

### 1. API Keys and Secrets

**Never commit secrets to Git:**
```bash
# Use .env file (NOT committed)
OPENAI_API_KEY=sk-...
JWT_SECRET=your-random-secret-here
```

**Generate strong JWT secrets:**
```bash
openssl rand -hex 32
```

### 2. Network Security

**Reverse Proxy (Recommended):**
```nginx
# Use nginx or Caddy in front of FaultMaven
server {
    listen 443 ssl;
    server_name faultmaven.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

**Firewall Rules:**
```bash
# Only expose necessary ports
# - 443 (HTTPS) - PUBLIC
# - 8000 (API) - INTERNAL ONLY
# - 3000 (Dashboard) - INTERNAL ONLY
# - 6379 (Redis) - INTERNAL ONLY
```

### 3. Docker Security

**Run containers as non-root:**
```dockerfile
# Services should use non-root users
USER appuser
```

**Use read-only filesystems where possible:**
```yaml
services:
  gateway:
    image: faultmaven/fm-api-gateway:latest
    read_only: true
    tmpfs:
      - /tmp
```

**Limit container resources:**
```yaml
services:
  case:
    image: faultmaven/fm-case-service:latest
    deploy:
      resources:
        limits:
          cpus: '1'
          memory: 1G
```

### 4. Data Security

**Encrypt volumes at rest:**
```bash
# Use encrypted filesystems for sensitive data
# - SQLite database files
# - Redis RDB snapshots
# - ChromaDB vectors
# - Uploaded files
```

**Regular backups:**
```bash
# Backup SQLite databases
docker exec fm-case-service sqlite3 /data/cases.db ".backup /backup/cases-$(date +%Y%m%d).db"

# Backup Redis
docker exec fm-redis redis-cli SAVE
docker cp fm-redis:/data/dump.rdb ./backup/redis-$(date +%Y%m%d).rdb
```

### 5. Authentication

**Change default JWT secret:**
```bash
# NEVER use default secrets in production
JWT_SECRET=$(openssl rand -hex 32)
```

**Enable HTTPS in production:**
```yaml
# Use TLS certificates
services:
  gateway:
    environment:
      - HTTPS_ENABLED=true
      - TLS_CERT_PATH=/certs/cert.pem
      - TLS_KEY_PATH=/certs/key.pem
```

### 6. Update Policy

**Keep FaultMaven updated:**
```bash
# Pull latest images
docker-compose pull

# Restart services
docker-compose up -d
```

**Subscribe to security advisories:**
- Watch this repository for security updates
- Enable GitHub security alerts
- Monitor our security mailing list (coming soon)

---

## Known Security Considerations

### LLM API Keys

FaultMaven requires LLM API keys (OpenAI, Anthropic, etc.). These keys:
- Are stored in `.env` files (never committed to Git)
- Are passed as environment variables to containers
- Should use **least privilege** (restrict API key scopes if possible)

**Recommendation:** Use separate API keys for development vs production.

### Data Privacy

FaultMaven processes sensitive troubleshooting data. By default:
- All data stays local (self-hosted)
- Only sanitized context is sent to LLM providers
- No telemetry is collected

**Verify data flows:**
```bash
# Review what's sent to LLM providers
# Check: fm-case-service/src/ai/sanitizer.py
```

### Browser Extension Permissions

The FaultMaven extension requests permissions to:
- Read page content (for evidence capture)
- Access storage (for settings)
- Make network requests (to your API)

**Review permissions before installing.**

---

## Security Features

### ✅ Input Validation

All API endpoints validate inputs using Pydantic models:
```python
class CreateCaseRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=10000)
```

### ✅ SQL Injection Protection

We use SQLAlchemy ORM with parameterized queries (prevents SQL injection):
```python
# SAFE (parameterized)
result = await db.execute(select(Case).where(Case.id == case_id))
```

### ✅ XSS Protection

- React escapes user content by default
- API responses use proper Content-Type headers
- CSP headers configured in dashboard

### ✅ Rate Limiting

API Gateway includes basic rate limiting:
```python
# 100 requests per minute per IP
@limiter.limit("100/minute")
async def create_case(...):
    pass
```

### ✅ CORS Configuration

CORS is configurable:
```yaml
services:
  gateway:
    environment:
      - CORS_ORIGINS=http://localhost:3000,https://yourdomain.com
```

---

## Vulnerability Disclosure Policy

We follow **responsible disclosure** principles:

1. **Report privately** via engineering@faultmaven.ai
2. **Allow time** for us to develop and release a fix (typically 90 days)
3. **Coordinate disclosure** timing
4. **Receive credit** in our security advisories (if desired)

We do not currently offer a bug bounty program, but we deeply appreciate security researchers who report vulnerabilities responsibly.

---

## Security Hall of Fame

We thank the following researchers for responsibly disclosing vulnerabilities:

*(No entries yet - be the first!)*

---

## Contact

For security issues: **engineering@faultmaven.ai**

For general support: See [README.md](README.md)

---

**Last Updated:** 2025-11-20
