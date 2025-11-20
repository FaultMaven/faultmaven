# FaultMaven Security Guide

This document outlines security best practices, vulnerability reporting procedures, and hardening guidelines for FaultMaven deployments.

## Table of Contents

- [Reporting Security Vulnerabilities](#reporting-security-vulnerabilities)
- [Security Best Practices](#security-best-practices)
- [Authentication & Authorization](#authentication--authorization)
- [Data Protection](#data-protection)
- [Network Security](#network-security)
- [Container Security](#container-security)
- [Secrets Management](#secrets-management)
- [Compliance & Auditing](#compliance--auditing)
- [Security Checklist](#security-checklist)

---

## Reporting Security Vulnerabilities

### Responsible Disclosure

If you discover a security vulnerability in FaultMaven, please report it responsibly:

**DO NOT:**
- Open a public GitHub issue
- Disclose the vulnerability publicly before it's patched
- Exploit the vulnerability

**DO:**
1. Email security findings to: **security@faultmaven.ai**
2. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if known)
   - Your contact information
3. Allow 90 days for a patch before public disclosure

### Response Timeline

- **24 hours:** Initial acknowledgment
- **7 days:** Preliminary assessment and severity rating
- **30 days:** Patch development (for high/critical issues)
- **90 days:** Public disclosure (coordinated with reporter)

### Security Advisories

Security advisories are published at:
- https://github.com/FaultMaven/FaultMaven/security/advisories

---

## Security Best Practices

### General Principles

1. **Defense in Depth:** Multiple layers of security controls
2. **Least Privilege:** Grant minimal necessary permissions
3. **Secure by Default:** Strong security out of the box
4. **Fail Securely:** Failures should deny access, not grant it
5. **Keep Current:** Regularly update all components

### Production Deployment Requirements

**MUST HAVE:**
- ✅ HTTPS/TLS encryption (valid certificate)
- ✅ Strong JWT secret (32+ random characters)
- ✅ Firewall configured
- ✅ Regular security updates
- ✅ Automated backups

**SHOULD HAVE:**
- ✅ Rate limiting
- ✅ Intrusion detection (Fail2Ban)
- ✅ Log monitoring
- ✅ Network segmentation
- ✅ Regular security audits

---

## Authentication & Authorization

### JWT Token Security

**Generate Strong Secret:**
```bash
# Cryptographically secure 32-byte secret
openssl rand -hex 32

# Add to .env
JWT_SECRET=your_generated_secret_here
```

**Token Configuration:**
```bash
# .env
JWT_SECRET=<strong-random-secret>
JWT_EXPIRE_MINUTES=60          # Access token expiry
REFRESH_TOKEN_EXPIRE_DAYS=7     # Refresh token expiry
JWT_ALGORITHM=HS256              # Signing algorithm
```

**Best Practices:**
- Rotate JWT secret quarterly
- Use short expiration times (15-60 minutes)
- Implement refresh tokens
- Store tokens securely (httpOnly cookies or secure storage)
- Never log JWT tokens

### Password Security

**Password Requirements:**
```python
# Enforced in auth service
MIN_PASSWORD_LENGTH = 12
REQUIRE_UPPERCASE = True
REQUIRE_LOWERCASE = True
REQUIRE_DIGITS = True
REQUIRE_SPECIAL_CHARS = True
```

**Password Storage:**
- Passwords hashed with bcrypt (cost factor: 12)
- Never store plaintext passwords
- Never log passwords

**Password Policies:**
```bash
# Configure in .env
PASSWORD_MIN_LENGTH=12
PASSWORD_COMPLEXITY=high
PASSWORD_EXPIRY_DAYS=90         # Optional
MAX_LOGIN_ATTEMPTS=5
LOCKOUT_DURATION_MINUTES=30
```

### Multi-Factor Authentication (MFA)

**Enterprise Feature:**
- TOTP-based 2FA
- SMS verification
- Hardware tokens (YubiKey)

**Self-Hosted:**
Currently not supported. Planned for future release.

### API Key Security

**LLM API Keys:**
```bash
# Store in .env, never commit to git
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Secure .env file
chmod 600 .env
```

**Rotate Regularly:**
- Monthly rotation recommended
- Immediate rotation if compromised
- Use different keys for dev/staging/prod

---

## Data Protection

### Data Encryption

**At Rest:**
```bash
# SQLite encryption (optional)
DB_ENCRYPTION_KEY=$(openssl rand -hex 32)

# Volume encryption (recommended for production)
# Use LUKS or cloud provider encryption
```

**In Transit:**
- All API communication over HTTPS
- TLS 1.2+ required
- Strong cipher suites only

**TLS Configuration (nginx):**
```nginx
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers HIGH:!aNULL:!MD5;
ssl_prefer_server_ciphers on;

# HSTS
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

### Sensitive Data Handling

**Data Classification:**

| Type | Classification | Protection |
|------|---------------|------------|
| User passwords | Critical | Bcrypt hashed, never logged |
| JWT tokens | High | Short-lived, secure transmission |
| API keys | High | Environment variables only |
| Case data | Medium | User-scoped, encrypted in transit |
| Logs | Low | Redacted sensitive fields |

**Data Sanitization:**
```python
# Automatic PII redaction before sending to LLM
REDACT_PATTERNS = [
    r'\b\d{3}-\d{2}-\d{4}\b',        # SSN
    r'\b\d{16}\b',                    # Credit card
    r'\b[\w\.-]+@[\w\.-]+\.\w+\b',   # Email (optional)
]
```

**Data Retention:**
```bash
# Configure retention policies
CASE_RETENTION_DAYS=365           # Keep cases for 1 year
LOG_RETENTION_DAYS=90             # Keep logs for 90 days
SESSION_RETENTION_DAYS=30         # Keep sessions for 30 days
```

### Backup Security

**Encrypted Backups:**
```bash
# Encrypt backups with GPG
gpg --symmetric --cipher-algo AES256 backup.tar.gz

# Or use encrypted S3 buckets
aws s3 cp backup.tar.gz s3://bucket/ --sse AES256
```

**Access Control:**
```bash
# Restrict backup access
chmod 600 /opt/faultmaven/backups/*
chown root:root /opt/faultmaven/backups/
```

---

## Network Security

### Firewall Configuration

**Minimal Exposure:**
```bash
# UFW example
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp    # SSH (restrict to specific IPs)
sudo ufw allow 80/tcp    # HTTP (redirect to HTTPS)
sudo ufw allow 443/tcp   # HTTPS
sudo ufw enable

# Block direct access to internal services
sudo ufw deny 8000/tcp
sudo ufw deny 8001/tcp
sudo ufw deny 8002/tcp
sudo ufw deny 8003/tcp
sudo ufw deny 8004/tcp
sudo ufw deny 8005/tcp
sudo ufw deny 3000/tcp
sudo ufw deny 6379/tcp   # Redis
```

**IP Whitelisting (Optional):**
```nginx
# nginx - restrict admin endpoints
location /v1/admin/ {
    allow 10.0.0.0/8;      # Internal network
    allow 203.0.113.0/24;  # Office IP
    deny all;

    proxy_pass http://faultmaven_api;
}
```

### Rate Limiting

**API Rate Limits:**
```nginx
# /etc/nginx/nginx.conf
http {
    limit_req_zone $binary_remote_addr zone=api:10m rate=100r/m;
    limit_req_zone $binary_remote_addr zone=login:10m rate=5r/m;
    limit_req_zone $binary_remote_addr zone=upload:10m rate=10r/h;

    server {
        location /v1/auth/login {
            limit_req zone=login burst=2 nodelay;
            limit_req_status 429;
            proxy_pass http://faultmaven_api;
        }

        location /v1/ {
            limit_req zone=api burst=20 nodelay;
            proxy_pass http://faultmaven_api;
        }

        location /v1/knowledge/upload {
            limit_req zone=upload burst=2 nodelay;
            proxy_pass http://faultmaven_api;
        }
    }
}
```

**Application-Level Rate Limiting:**
```python
# Configured in each service
RATE_LIMIT_PER_MINUTE = 100
RATE_LIMIT_BURST = 20
```

### Intrusion Detection

**Fail2Ban:**
```bash
# Install
sudo apt install fail2ban -y

# Configure jail
cat > /etc/fail2ban/jail.d/faultmaven.conf <<EOF
[faultmaven-auth]
enabled = true
port = http,https
filter = faultmaven-auth
logpath = /var/log/nginx/access.log
maxretry = 5
bantime = 3600
findtime = 600

[faultmaven-api]
enabled = true
port = http,https
filter = faultmaven-api
logpath = /var/log/nginx/access.log
maxretry = 100
bantime = 600
findtime = 60
EOF

# Create filters
cat > /etc/fail2ban/filter.d/faultmaven-auth.conf <<EOF
[Definition]
failregex = ^<HOST> .* "POST /v1/auth/login HTTP.*" 401
            ^<HOST> .* "POST /v1/auth/login HTTP.*" 403
ignoreregex =
EOF

cat > /etc/fail2ban/filter.d/faultmaven-api.conf <<EOF
[Definition]
failregex = ^<HOST> .* ".*" 429
ignoreregex =
EOF

# Restart Fail2Ban
sudo systemctl restart fail2ban
```

### DDoS Protection

**CloudFlare (Recommended):**
- Free tier provides basic DDoS protection
- Web Application Firewall (WAF)
- Bot protection
- Analytics

**Self-Hosted:**
```nginx
# Connection limiting
limit_conn_zone $binary_remote_addr zone=addr:10m;
limit_conn addr 10;

# Request size limits
client_max_body_size 50M;
client_body_buffer_size 1m;
client_header_buffer_size 1k;
```

---

## Container Security

### Image Security

**Official Images Only:**
```yaml
# docker-compose.yml
services:
  gateway:
    image: faultmaven/fm-api-gateway:latest  # Official
    # NOT: random-user/fm-api-gateway        # Untrusted
```

**Image Scanning:**
```bash
# Scan for vulnerabilities (Trivy)
docker run aquasec/trivy:latest image faultmaven/fm-api-gateway:latest

# Scan all images
for img in $(docker images --format "{{.Repository}}:{{.Tag}}" | grep faultmaven); do
    docker run aquasec/trivy:latest image $img
done
```

**Regular Updates:**
```bash
# Update to latest images
docker compose pull
docker compose up -d
```

### Container Hardening

**Run as Non-Root:**
```dockerfile
# All FaultMaven images use non-root user
USER appuser
```

**Read-Only Filesystem:**
```yaml
# docker-compose.yml
services:
  gateway:
    read_only: true
    tmpfs:
      - /tmp
      - /app/temp
```

**Resource Limits:**
```yaml
services:
  agent-service:
    mem_limit: 2g
    mem_reservation: 1g
    cpus: 2.0
    pids_limit: 100
```

**Security Options:**
```yaml
services:
  gateway:
    security_opt:
      - no-new-privileges:true
      - apparmor:docker-default
    cap_drop:
      - ALL
    cap_add:
      - NET_BIND_SERVICE  # Only if needed
```

### Network Isolation

**Internal Network:**
```yaml
networks:
  frontend:
    driver: bridge
  backend:
    driver: bridge
    internal: true  # No external access

services:
  gateway:
    networks:
      - frontend
      - backend

  case-service:
    networks:
      - backend  # Not exposed to internet
```

---

## Secrets Management

### Environment Variables

**DO:**
```bash
# Use .env file (not committed to git)
echo ".env" >> .gitignore

# Secure permissions
chmod 600 .env
chown root:root .env
```

**DON'T:**
```bash
# Never hardcode secrets
JWT_SECRET=hardcoded_secret_bad  # ❌

# Never commit .env to git
git add .env  # ❌
```

### Docker Secrets (Production)

**For Swarm/Kubernetes:**
```bash
# Create secret
echo "my_secret_value" | docker secret create jwt_secret -

# Use in docker-compose.yml
services:
  gateway:
    secrets:
      - jwt_secret

secrets:
  jwt_secret:
    external: true
```

### HashiCorp Vault (Enterprise)

**For large deployments:**
```bash
# Store secrets in Vault
vault kv put secret/faultmaven/jwt jwt_secret="..."

# Retrieve in startup script
export JWT_SECRET=$(vault kv get -field=jwt_secret secret/faultmaven/jwt)
```

---

## Compliance & Auditing

### Logging

**Security Event Logging:**
```python
# Logged events
- User login (success/failure)
- Password changes
- Permission changes
- Data access
- Configuration changes
- API errors
```

**Log Security:**
```bash
# Centralized logging (optional)
# ELK Stack, Splunk, or cloud provider logging

# Log rotation
# See /etc/logrotate.d/faultmaven

# Log retention
LOG_RETENTION_DAYS=90
```

**What NOT to Log:**
- Passwords (plaintext or hashed)
- JWT tokens
- API keys
- PII (unless required for compliance)

### Audit Trail

**Database Auditing:**
```sql
-- Audit table (example)
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    user_id TEXT,
    action TEXT,
    resource TEXT,
    timestamp DATETIME,
    ip_address TEXT,
    user_agent TEXT
);
```

### Compliance

**GDPR:**
- User data export: `/v1/users/{id}/export`
- Right to deletion: `/v1/users/{id}` DELETE
- Data processing agreement
- Privacy policy

**HIPAA (if storing health data):**
- Encryption at rest and in transit
- Access controls
- Audit logging
- Business associate agreement

**SOC 2 (Enterprise):**
- Continuous monitoring
- Incident response plan
- Regular penetration testing
- Third-party audits

---

## Security Checklist

### Pre-Deployment

- [ ] Strong JWT secret generated (32+ characters)
- [ ] TLS certificate obtained and configured
- [ ] Firewall rules applied
- [ ] Rate limiting enabled
- [ ] .env file secured (chmod 600)
- [ ] Default passwords changed
- [ ] Unnecessary ports closed
- [ ] Docker images scanned for vulnerabilities

### Post-Deployment

- [ ] HTTPS enforced (HTTP redirects to HTTPS)
- [ ] Security headers configured
- [ ] Fail2Ban or similar IDS active
- [ ] Automated backups configured
- [ ] Log monitoring enabled
- [ ] Incident response plan documented
- [ ] Security contacts established

### Monthly

- [ ] Review access logs for anomalies
- [ ] Update Docker images
- [ ] Review user permissions
- [ ] Test backup restoration
- [ ] Scan for vulnerabilities

### Quarterly

- [ ] Rotate JWT secret
- [ ] Rotate API keys
- [ ] Review and update security policies
- [ ] Conduct security training
- [ ] Penetration testing (production)

### Annually

- [ ] Full security audit
- [ ] Review compliance requirements
- [ ] Update disaster recovery plan
- [ ] Tabletop security exercise

---

## Security Contacts

**Report Vulnerabilities:**
- Email: security@faultmaven.ai
- PGP Key: https://faultmaven.ai/.well-known/pgp-key.txt

**Security Updates:**
- GitHub: https://github.com/FaultMaven/FaultMaven/security/advisories
- Mailing List: security-announce@faultmaven.ai

**Emergency Contact:**
- For critical vulnerabilities actively being exploited
- Email: security@faultmaven.ai (Priority: URGENT)

---

**Last Updated:** 2025-11-20
**Version:** 2.0
**Status:** Current
