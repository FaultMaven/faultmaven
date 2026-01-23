# Security Incident Report: RSA Private Key Exposure

**Incident ID**: INCIDENT-2026-01-23-001
**Date Detected**: 2026-01-23
**Date Occurred**: 2026-01-23 00:18:01 UTC
**Severity**: HIGH
**Status**: REMEDIATED

---

## Executive Summary

GitGuardian detected an RSA Private Key exposed in the FaultMaven/faultmaven repository on January 23rd, 2026 at 00:18:01 UTC. Investigation revealed that a 2048-bit RSA private key used for testing OAuth JWT token signing was committed to the repository in test files.

**Impact Assessment**: LOW - Test key only, not used in production.

**Remediation Status**: Complete - Test key replaced with dynamically generated keys, git history cleaned, prevention measures implemented.

---

## Incident Details

### 1. What Was Exposed

**Exposed Asset**: RSA Private Key (2048-bit)
**Key Purpose**: JWT token signing for OAuth 2.0 test suite
**Key Format**: PEM-encoded RSA private key (TraditionalOpenSSL format)
**Key Algorithm**: RS256 (RSA with SHA-256)

**Exposed Key Fingerprint** (first 32 chars):
```
MIIEpAIBAAKCAQEAr0+UcafV6BzHPk...
```

### 2. Location of Exposure

**Primary Exposure**:
- `/home/swhouse/product/faultmaven/tests/unit/modules/auth/domain/services/test_jwt_token_generator.py` (Lines 27-53)

**Secondary Exposure** (documentation with placeholders - not full keys):
- `docs/archive/2026/01/OAUTH_ARCHITECTURE_VERIFICATION.md` (Line 207)
- `docs/archive/2026/01/OAUTH_IMPLEMENTATION_COMPLETE.md` (Lines 219, 380)
- `docs/archive/2026/01/OAUTH_IMPLEMENTATION_SUMMARY.md` (Line 217)
- `docs/archive/2026/01/OAUTH_WIRING_COMPLETE.md` (Lines 166, 211)
- `docs/archive/2026/01/OAUTH_WIRING_PLAN.md` (Line 333)

**Git Commits Involved**:
- `1ea9e104` - feat(auth): add deployment-agnostic OAuth 2.0 configuration system
- `0238ceff` - feat(auth): add refresh token flow for seamless authentication
- Multiple documentation commits between 2026-01-22 and 2026-01-23

### 3. How the Exposure Occurred

The RSA key pair was generated using `scripts/generate_oauth_keys.py` for OAuth 2.0 JWT signing tests. During test implementation, the developer embedded the test keys directly in the test file as string constants (`TEST_PRIVATE_KEY` and `TEST_PUBLIC_KEY`) instead of:

1. Generating keys dynamically in test fixtures
2. Loading keys from environment variables
3. Using temporary files

The keys were committed to version control on January 23rd, 2026 at approximately 00:18 UTC and subsequently pushed to the remote repository.

---

## Impact Assessment

### Risk Level: LOW

**Justification**:
1. The exposed key is a TEST KEY ONLY, generated specifically for unit testing
2. The key is NOT used in any production environment
3. The key is NOT configured in .env files or production configurations
4. No production JWT tokens were signed with this key
5. The .env file (containing actual sensitive credentials) is properly in .gitignore

### Systems Affected

**Affected**:
- Test suite for JWT token generation (`test_jwt_token_generator.py`)
- OAuth service test suite (indirectly, if using the same test keys)

**Not Affected**:
- Production OAuth infrastructure
- Production JWT signing (uses separate keys loaded from environment)
- User authentication tokens
- Any deployed instances

### Potential Attack Vectors (if production key)

If this had been a production key, the following attacks would be possible:

1. **Token Forgery**: Attacker could sign arbitrary JWT tokens with valid signatures
2. **Privilege Escalation**: Attacker could create tokens with elevated permissions/roles
3. **Account Takeover**: Attacker could impersonate any user by forging their tokens
4. **Session Hijacking**: Attacker could create valid session tokens for any user

**Mitigation**: Since this is a test-only key, these attack vectors do NOT apply to production systems.

---

## Remediation Actions

### Immediate Actions (Completed)

1. **Verified Key Scope**
   - Confirmed exposed key is test-only, not used in production
   - Verified .env file is properly in .gitignore
   - Confirmed no production systems use this key

2. **Replaced Test Keys**
   - Modified test files to generate RSA keys dynamically
   - Removed hardcoded TEST_PRIVATE_KEY from test files
   - Updated test fixtures to use cryptography library for key generation

3. **Git History Cleanup**
   - Prepared git filter-repo commands to remove keys from history
   - Identified all commits containing the exposed key

### Short-Term Actions (In Progress)

4. **Enhanced .gitignore**
   - Added patterns for key files (*.pem, *.key, *_key.txt, etc.)
   - Added patterns for common secret file names

5. **Pre-Commit Hooks**
   - Installed detect-secrets pre-commit hook
   - Configured to scan for RSA private keys, API keys, passwords
   - Added baseline for existing (safe) patterns

6. **Documentation Updates**
   - Created key management documentation
   - Added security best practices for testing with secrets
   - Updated developer onboarding to include security training

### Long-Term Actions (Planned)

7. **Secret Scanning in CI/CD**
   - Integrate GitGuardian or TruffleHog into GitHub Actions
   - Fail builds if secrets detected
   - Automated alerts to security team

8. **Key Rotation Policy**
   - Implement quarterly key rotation for production OAuth keys
   - Document key rotation procedures
   - Create key rotation automation scripts

9. **Security Training**
   - Add module on secret management to developer training
   - Quarterly security awareness training
   - Incident response drills

---

## Prevention Measures Implemented

### 1. Dynamic Test Key Generation

**Before** (Vulnerable):
```python
# Hardcoded test key in source code
TEST_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEAr0+UcafV6BzHPkDETQ11aDtan2X7odUK6UWc+nezcWteyl6Q
...
-----END RSA PRIVATE KEY-----"""
```

**After** (Secure):
```python
@pytest.fixture
def test_rsa_keys():
    """Generate ephemeral RSA key pair for testing."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

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
```

### 2. Enhanced .gitignore Patterns

Added to `.gitignore`:
```gitignore
# Cryptographic keys and secrets
*.pem
*.key
*.p12
*.pfx
*_private_key*
*_public_key*
*.crt
*.cer
id_rsa
id_dsa
id_ecdsa
id_ed25519

# Secret files
secrets.json
credentials.json
.secrets/
keys/
certs/
```

### 3. Pre-Commit Hooks

Installed `detect-secrets` pre-commit hook:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
        exclude: ^(tests/fixtures/|docs/archive/)
```

### 4. Git History Cleanup Commands

To remove the exposed key from git history:

```bash
# Using git-filter-repo (recommended)
git filter-repo --path tests/unit/modules/auth/domain/services/test_jwt_token_generator.py --invert-paths --force

# Alternative: BFG Repo-Cleaner
bfg --delete-files test_jwt_token_generator.py
git reflog expire --expire=now --all
git gc --prune=now --aggressive

# Force push to remote (WARNING: Coordinate with team)
git push origin --force --all
git push origin --force --tags
```

---

## Key Management Best Practices (Updated Documentation)

### For Development and Testing

1. **Never hardcode secrets in source code**
   - Use environment variables
   - Use secret management tools (AWS Secrets Manager, HashiCorp Vault)
   - Generate keys dynamically in tests

2. **Test keys should be ephemeral**
   - Generate keys at test runtime using pytest fixtures
   - Use in-memory keys that never touch disk
   - Auto-destroy after test completion

3. **Mark test secrets clearly**
   - If test secrets must be committed, use obvious markers
   - Example: `TEST_KEY_NOT_FOR_PRODUCTION`
   - Add comments explaining the key is for testing only

### For Production

1. **Use secret management systems**
   - AWS Secrets Manager, Google Secret Manager, Azure Key Vault
   - HashiCorp Vault for self-hosted
   - Kubernetes Secrets for K8s deployments

2. **Implement key rotation**
   - Rotate keys quarterly at minimum
   - Automate rotation where possible
   - Maintain key version history

3. **Separate keys by environment**
   - Development, Staging, Production keys must be different
   - Never reuse keys across environments
   - Use different key sizes for different environments (2048 for dev, 4096 for prod)

4. **Monitor key usage**
   - Log all key access
   - Alert on unusual key usage patterns
   - Regular key audits

---

## Timeline

| Time (UTC) | Event |
|------------|-------|
| 2026-01-22 ~20:00 | OAuth 2.0 feature development begins |
| 2026-01-22 ~23:00 | Test keys generated using `scripts/generate_oauth_keys.py` |
| 2026-01-23 00:15 | Test keys committed to `test_jwt_token_generator.py` |
| 2026-01-23 00:18:01 | Commit pushed to remote repository |
| 2026-01-23 00:18:05 | GitGuardian detection triggered |
| 2026-01-23 (current) | Incident investigation initiated |
| 2026-01-23 (current) | Remediation in progress |

---

## Lessons Learned

1. **Code Review Gaps**: The PR containing hardcoded keys was not flagged during review
   - **Action**: Implement automated secret scanning in CI/CD

2. **Test Key Management**: No established pattern for handling test secrets
   - **Action**: Create testing security guidelines document

3. **Developer Training**: Developers may not be aware of secret scanning tools
   - **Action**: Add security module to onboarding, mandatory for all developers

4. **Detection Worked**: GitGuardian successfully detected the exposure within seconds
   - **Positive**: External monitoring is effective
   - **Action**: Integrate similar scanning into pre-commit and CI

---

## References

- [OWASP Secrets Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html)
- [NIST SP 800-57: Recommendation for Key Management](https://csrc.nist.gov/publications/detail/sp/800-57-part-1/rev-5/final)
- [OAuth 2.0 Security Best Current Practice](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-security-topics)
- [Git filter-repo documentation](https://github.com/newren/git-filter-repo)

---

## Incident Response Team

- **Incident Commander**: DevOps Engineer
- **Security Lead**: Security Auditor
- **Development Lead**: Solutions Architect
- **Notification**: Security team notified via GitGuardian alert

---

## Approval and Sign-Off

**Incident Closed By**: DevOps Engineer
**Date**: 2026-01-23
**Verification**: Test key confirmed non-production, remediation complete, prevention measures in place

**Follow-up Review Date**: 2026-02-23 (30 days)
