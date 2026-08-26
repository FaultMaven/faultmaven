# Cryptographic Key Management Guide

**Document Version**: 1.0
**Last Updated**: 2026-01-23
**Owner**: DevOps & Security Teams

---

## Table of Contents

1. [Overview](#overview)
2. [Key Types and Use Cases](#key-types-and-use-cases)
3. [Development Environment](#development-environment)
4. [Testing Environment](#testing-environment)
5. [Production Environment](#production-environment)
6. [Key Generation](#key-generation)
7. [Key Storage](#key-storage)
8. [Key Rotation](#key-rotation)
9. [Security Best Practices](#security-best-practices)
10. [Incident Response](#incident-response)

---

## Overview

FaultMaven uses RSA asymmetric encryption (RS256) for JWT token signing in OAuth 2.0 flows. Proper key management is critical for security. This document provides guidelines for generating, storing, rotating, and protecting cryptographic keys across all environments.

**Key Principles**:
- Never commit private keys to version control
- Separate keys by environment (dev, staging, production)
- Rotate keys regularly
- Use dynamic generation for tests
- Monitor and audit key usage

---

## Key Types and Use Cases

### JWT Signing Keys (RS256)

**Purpose**: Sign and verify OAuth 2.0 JWT access and refresh tokens

**Algorithm**: RSA-SHA256 (RS256)
**Key Size**:
- Development/Testing: 2048 bits minimum
- Production: 4096 bits recommended

**Components**:
- **Private Key**: Signs JWTs (server-side only, keep secret)
- **Public Key**: Verifies JWTs (can be distributed to clients)

**Environment Variables**:
```bash
JWT_PRIVATE_KEY=<PEM-encoded private key>
JWT_PUBLIC_KEY=<PEM-encoded public key>

# Alternative: File paths
JWT_PRIVATE_KEY_PATH=/path/to/private.pem
JWT_PUBLIC_KEY_PATH=/path/to/public.pem
```

---

## Development Environment

### Auto-Generated Keys

In development, FaultMaven automatically generates ephemeral RSA keys if none are configured. This provides a frictionless developer experience while maintaining security.

**How it works**:
1. On startup, `AuthService` checks for `JWT_PRIVATE_KEY` or `JWT_PRIVATE_KEY_PATH`
2. If not found, generates a new 2048-bit RSA key pair
3. Logs a warning: `"Generated development RSA keys - NOT FOR PRODUCTION"`
4. Uses generated keys for the session only (not persisted)

**When to use**:
- Local development on your laptop
- Quick testing without OAuth integration
- Initial project setup

**When NOT to use**:
- Production deployments
- Shared development environments
- CI/CD pipelines
- Any environment where tokens must persist across restarts

### Manual Key Generation for Development

If you need persistent keys in development (e.g., to test token persistence across restarts):

```bash
# Generate keys
python scripts/generate_oauth_keys.py > dev_keys.txt

# Add to .env (NOT .env.example)
JWT_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
```

**Important**:
- Development keys should NEVER be used in production
- Add `dev_keys.txt` to `.gitignore`
- Do not commit `.env` file

---

## Testing Environment

### Dynamic Key Generation (Recommended)

Tests should generate ephemeral keys at runtime to avoid hardcoding secrets.

**Example** (pytest fixture):
```python
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def test_rsa_keys():
    """Generate ephemeral RSA key pair for testing.

    Keys are generated fresh for each test session and never persisted.
    This prevents hardcoded secrets from being committed to version control.
    """
    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )

    # Serialize to PEM format
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode('utf-8')

    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode('utf-8')

    return private_pem, public_pem


def test_jwt_generation(test_rsa_keys):
    """Test JWT generation with ephemeral keys."""
    private_key, public_key = test_rsa_keys

    service = RS256JWTTokenGenerator(
        private_key=private_key,
        public_key=public_key,
        ...
    )

    token = await service.generate_access_token(user)
    assert token is not None
```

### What NOT to Do

**❌ NEVER hardcode test keys**:
```python
# WRONG - Do not do this!
TEST_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEAr0+UcafV6BzHPkDETQ11...
-----END RSA PRIVATE KEY-----"""
```

**Why**: Hardcoded keys can be accidentally committed to version control, leading to security incidents.

### CI/CD Testing

For CI/CD pipelines, use environment variables or dynamic generation:

**GitHub Actions Example**:
```yaml
- name: Run tests
  env:
    JWT_PRIVATE_KEY: ${{ secrets.TEST_JWT_PRIVATE_KEY }}
    JWT_PUBLIC_KEY: ${{ secrets.TEST_JWT_PUBLIC_KEY }}
  run: pytest tests/
```

Or use dynamic generation (no secrets needed):
```yaml
- name: Run tests with dynamic keys
  run: |
    export GENERATE_TEST_KEYS=true
    pytest tests/
```

---

## Production Environment

### Key Generation

**Generate production keys on a secure machine** (not your laptop):

```bash
# SSH into secure bastion host or use secure workstation
ssh secure-admin@bastion.faultmaven.ai

# Generate 4096-bit keys for production
python3 scripts/generate_oauth_keys.py --key-size 4096 > prod_keys.txt

# Immediately store in secret manager (see below)
# Then securely delete the file
shred -vfz -n 10 prod_keys.txt
```

### Key Storage

**NEVER store production keys in**:
- Version control (git)
- Unencrypted files on disk
- Email or chat messages
- Developer laptops
- Container images

**DO store production keys in**:
- AWS Secrets Manager
- Google Secret Manager
- Azure Key Vault
- HashiCorp Vault
- Kubernetes Secrets (encrypted at rest)

**AWS Secrets Manager Example**:
```bash
# Store private key
aws secretsmanager create-secret \
    --name faultmaven/production/jwt-private-key \
    --description "JWT signing private key for production" \
    --secret-string file://private_key.pem \
    --region us-east-1

# Store public key
aws secretsmanager create-secret \
    --name faultmaven/production/jwt-public-key \
    --description "JWT verification public key for production" \
    --secret-string file://public_key.pem \
    --region us-east-1
```

**Load keys in application**:
```python
import boto3
import json

def load_production_keys():
    """Load JWT keys from AWS Secrets Manager."""
    client = boto3.client('secretsmanager', region_name='us-east-1')

    # Load private key
    private_response = client.get_secret_value(
        SecretId='faultmaven/production/jwt-private-key'
    )
    private_key = private_response['SecretString']

    # Load public key
    public_response = client.get_secret_value(
        SecretId='faultmaven/production/jwt-public-key'
    )
    public_key = public_response['SecretString']

    return private_key, public_key
```

### Environment-Specific Keys

**CRITICAL**: Use different keys for each environment.

| Environment | Key Size | Rotation | Storage |
|-------------|----------|----------|---------|
| Development | 2048-bit | Never (ephemeral) | Auto-generated or .env |
| Staging | 2048-bit | Quarterly | AWS Secrets Manager |
| Production | 4096-bit | Quarterly | AWS Secrets Manager |

**Why separate keys?**:
- Prevents staging tokens from being valid in production
- Limits blast radius if keys are compromised
- Allows testing key rotation without affecting production

---

## Key Generation

### Using the FaultMaven Key Generator

```bash
# Basic usage (2048-bit keys)
python scripts/generate_oauth_keys.py

# Production usage (4096-bit keys)
python scripts/generate_oauth_keys.py --key-size 4096

# Output to file
python scripts/generate_oauth_keys.py > keys_$(date +%Y%m%d_%H%M%S).txt
```

**Output**:
- Private key (PEM format)
- Public key (PEM format)
- .env format (escaped newlines)
- Usage instructions

### Manual Generation (OpenSSL)

```bash
# Generate private key (4096-bit)
openssl genrsa -out private_key.pem 4096

# Extract public key
openssl rsa -in private_key.pem -pubout -out public_key.pem

# Verify keys
openssl rsa -in private_key.pem -check
```

### Key Format Verification

**Valid PEM formats**:

**Private Key** (TraditionalOpenSSL format):
```
-----BEGIN RSA PRIVATE KEY-----
MIIJKAIBAAKCAgEA...
-----END RSA PRIVATE KEY-----
```

**Private Key** (PKCS#8 format - also supported):
```
-----BEGIN PRIVATE KEY-----
MIIJQwIBADANBgkqhkiG9w0BAQEFAASC...
-----END PRIVATE KEY-----
```

**Public Key**:
```
-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8A...
-----END PUBLIC KEY-----
```

---

## Key Rotation

### When to Rotate Keys

**Mandatory Rotation**:
- Suspected key compromise
- Employee with key access leaves company
- Compliance requirement (e.g., PCI-DSS)
- After security incident

**Scheduled Rotation**:
- Production: Every 90 days (quarterly)
- Staging: Every 180 days
- Development: As needed or never (ephemeral)

### Rotation Procedure

**Step 1: Generate New Keys**
```bash
# On secure machine
python3 scripts/generate_oauth_keys.py --key-size 4096 > new_keys.txt
```

**Step 2: Store New Keys with Version**
```bash
# AWS Secrets Manager with version tagging
aws secretsmanager put-secret-value \
    --secret-id faultmaven/production/jwt-private-key \
    --secret-string file://new_private_key.pem \
    --version-stages AWSCURRENT ROTATION_2026_Q1
```

**Step 3: Update Application Configuration**
```bash
# Update environment variables or configuration
# For Kubernetes, update secrets
kubectl create secret generic jwt-keys \
    --from-file=private.pem=new_private_key.pem \
    --from-file=public.pem=new_public_key.pem \
    --dry-run=client -o yaml | kubectl apply -f -

# Rolling restart of application pods
kubectl rollout restart deployment/faultmaven-api
```

**Step 4: Deprecation Period**
- Keep old public key available for 24 hours
- Allow tokens signed with old key to be verified
- Log usage of old key for monitoring

**Step 5: Revoke Old Key**
```bash
# After 24 hours, remove old key
# Mark old secret as deprecated
aws secretsmanager update-secret-version-stage \
    --secret-id faultmaven/production/jwt-private-key \
    --version-stage AWSCURRENT \
    --move-to-version-id <new-version-id>
```

**Step 6: Revoke Active Tokens (Optional)**
- If immediate revocation needed, add old key JTIs to revocation store
- Force users to re-authenticate

---

## Security Best Practices

### DO ✅

1. **Use asymmetric encryption (RS256)** instead of symmetric (HS256) for production
   - Private key stays on server
   - Public key can be distributed

2. **Generate keys on secure machines**
   - Use bastion hosts or secure admin workstations
   - Never generate production keys on developer laptops

3. **Use secret management systems**
   - AWS Secrets Manager, Vault, Google Secret Manager
   - Encrypted at rest and in transit
   - Audit logs for access

4. **Implement key rotation**
   - Quarterly for production
   - Automated rotation scripts
   - Version and track keys

5. **Monitor key usage**
   - Log all token signatures and verifications
   - Alert on unusual patterns
   - Track key age

6. **Separate keys by environment**
   - Dev, staging, production must have different keys
   - Never reuse keys across environments

7. **Use dynamic generation for tests**
   - pytest fixtures for ephemeral keys
   - No hardcoded test secrets

### DON'T ❌

1. **Never commit keys to version control**
   - Use `.gitignore` for `.env`, `*.pem`, `*.key`
   - Use pre-commit hooks to detect secrets

2. **Never hardcode keys in source code**
   - Use environment variables
   - Use configuration management

3. **Never share keys via insecure channels**
   - No email, Slack, or chat
   - Use encrypted secret sharing tools

4. **Never reuse keys across environments**
   - Production key ≠ Staging key ≠ Dev key

5. **Never use weak key sizes**
   - Minimum 2048-bit for dev/test
   - 4096-bit for production

6. **Never skip key rotation**
   - Set calendar reminders
   - Automate rotation process

---

## Incident Response

### If a Private Key is Compromised

**Immediate Actions** (within 1 hour):

1. **Assess Scope**
   - Determine which key was exposed (dev, staging, production)
   - Identify when and where the exposure occurred
   - Check if key was used to sign production tokens

2. **Revoke Compromised Key**
   - Generate new key pair immediately
   - Update production systems with new key
   - Add old key JTIs to revocation store

3. **Invalidate Tokens**
   - Revoke all tokens signed with compromised key
   - Force all users to re-authenticate
   - Clear Redis revocation cache if needed

4. **Notify Stakeholders**
   - Security team
   - Engineering leadership
   - Compliance team (if production)

**Short-Term Actions** (within 24 hours):

5. **Clean Git History**
   - Use `git filter-repo` to remove key from history
   - Force push to all branches
   - Notify team of history rewrite

6. **Audit Access Logs**
   - Check who had access to the key
   - Review recent token signatures
   - Look for unauthorized token creation

7. **Document Incident**
   - Create incident report
   - Root cause analysis
   - Lessons learned

**Long-Term Actions** (within 1 week):

8. **Improve Detection**
   - Add secret scanning to CI/CD
   - Deploy monitoring and alerting
   - Regular security audits

9. **Training**
   - Security awareness training for developers
   - Key management best practices
   - Incident response drills

10. **Process Improvements**
    - Update onboarding documentation
    - Enhance pre-commit hooks
    - Automate key rotation

### Past incident: RSA key in git history (2026-01-23)

A 2048-bit RSA private key was committed to
`tests/unit/modules/auth/domain/services/test_jwt_token_generator.py` in
`1e50943d1` and removed from the tip the same day in `1a234c8de`. The write-up
that used to be linked here was deleted on 2026-07-07; this section replaces it,
because two of its conclusions were wrong.

**The blob is still reachable from `main`, in a public repo.** The write-up
recorded the incident as REMEDIATED with history cleaned. History was never
rewritten. It also named two commits as carrying the key; neither does, and it
did not name the one that does.

**The key was test-only. Verified 2026-08-26, by measurement rather than
inference:**

- It appears in exactly one blob out of ~17,000 in the object database, and in
  none of the sibling repositories.
- Its SPKI-SHA256 is `035888a6…`. The live cluster's
  `faultmaven-secrets.JWT_PRIVATE_KEY` is `65e8b68a…` — a different key.
- RS256 signing keys have no hardcoded default anywhere:
  `AuthService._load_keys` resolves `JWT_PRIVATE_KEY`, then
  `JWT_PRIVATE_KEY_PATH`, then an ephemeral runtime pair, and every RS256 field
  defaults to `None`.

Nothing was rotated, and nothing needs to be: rotation severs a trust
relationship, and this key never had one. It was never in a JWKS, never
configured as a verifier, and grants no access. Secret-scanning alert #15 is
resolved as `used_in_tests` on that basis.

**Why it self-certified.** The remediation script that accompanied the incident
verified its own work by grepping `git log --all --pretty=format: --name-only`
for the key. That command emits *paths*, never file contents, so the check could
never match and always reported success — it would have reported clean even if
the rewrite had done nothing, which is what happened. The script was removed in
the same change as this note. If a history rewrite is ever genuinely required,
write a fresh one and verify it against blob contents (`git cat-file`), not
against the path listing.

---

## References

- [OWASP Cryptographic Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)
- [NIST SP 800-57: Key Management](https://csrc.nist.gov/publications/detail/sp/800-57-part-1/rev-5/final)
- [OAuth 2.0 Security Best Current Practice](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-security-topics)
- [JWT Best Practices](https://datatracker.ietf.org/doc/html/rfc8725)
- [AWS Secrets Manager Best Practices](https://docs.aws.amazon.com/secretsmanager/latest/userguide/best-practices.html)

---

## Appendix: Quick Reference

### Key Generation
```bash
python scripts/generate_oauth_keys.py --key-size 4096
```

### Environment Variables
```bash
JWT_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
```

### Rotation Schedule
- Development: Never (ephemeral)
- Staging: Quarterly (90 days)
- Production: Quarterly (90 days)

### Emergency Contacts
- Security Team: security@faultmaven.ai
- On-Call Engineer: PagerDuty
- Compliance: compliance@faultmaven.ai

---

**Document Owner**: DevOps Team
**Review Frequency**: Quarterly
**Last Review**: 2026-01-23
**Next Review**: 2026-04-23
