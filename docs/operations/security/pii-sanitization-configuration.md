# PII Sanitization Configuration Guide

## Overview

FaultMaven provides case-scoped PII redaction that protects sensitive data when sending case data to external LLM providers. When enabled, PII is redacted before LLM calls and restored in user-facing responses — the LLM never sees raw PII, but the user always does.

## Configuration

Two settings control PII redaction:

```bash
# .env
PROTECTION_ENABLED=true  # Master toggle for protection features (default: false)
SANITIZE_PII=true        # Enable PII redaction (default: false)
```

Both must be enabled for full PII protection. When `SANITIZE_PII=true`:

- All prompts are redacted before LLM calls
- Tool results (search_file, deep_analysis) are redacted before returning to the LLM
- User-facing responses have placeholders reversed back to original values
- The same PII value gets the same placeholder across all files in a case

When both are `false` (default): no redaction at any layer, and **Presidio health checks are skipped at startup** — no connection attempts to external Presidio services.

### Deployment Modes

| Deployment | PROTECTION_ENABLED | SANITIZE_PII | Presidio | Behavior |
| --- | --- | --- | --- | --- |
| Local / Community | false (default) | false (default) | Not needed | Presidio health checks skipped, regex-only fallback available |
| Cloud / Enterprise | true | true | Required | Full Presidio NLP detection + regex patterns |
| Self-hosted with local LLM | false | false | Not needed | Data never leaves the network, no redaction needed |

## The Problem

FaultMaven is a SaaS product where user log data crosses a trust boundary: user data → external LLM provider. Without redaction, production logs containing emails, IPs, API keys, and hostnames are sent to third-party APIs.

However, FaultMaven is also a troubleshooting tool. Naively redacting IPs and hostnames defeats security investigations — the core use case. This creates a tension between privacy and utility.

## How It Works

Case-scoped redaction solves both problems:

1. **The LLM sees placeholders** — `<IP_ADDRESS_1>`, `<EMAIL_ADDRESS_2>` — protecting privacy
2. **Placeholders are consistent within a case** — the same IP always maps to the same placeholder across all evidence files, preserving correlation ability
3. **The user sees real values** — placeholders are reversed before the response reaches the user

### Data Flow

```text
User uploads data
    → stored raw (never redacted at rest)
    → preprocessing extracts structural index
    ↓
MilestoneEngine._process_turn_impl()
    ├─ Load CaseRedactionContext from Redis
    ├─ Context builder assembles prompt (raw content)
    ├─ Redact prompt with case-scoped registry
    ├─ Send redacted prompt to LLM
    ├─ LLM calls search_file → raw result → redact with SAME registry → return to LLM
    ├─ LLM responds with placeholders
    └─ Save registry to Redis
    ↓
InvestigationService.process_turn()
    ├─ Reverse-substitute placeholders → original values
    └─ Return to user (user sees real IPs, names, etc.)
```

## What Gets Redacted

### Pattern-Based (Regex)

| Entity | Example | Placeholder |
|--------|---------|-------------|
| API keys | `sk-1234567890abcdef` | `<API_KEY_1>` |
| AWS access keys | `AKIAIOSFODNN7EXAMPLE` | `<AWS_ACCESS_KEY_1>` |
| Database URLs | `postgresql://user:pass@host/db` | `<DATABASE_URL_1>` |
| JWT tokens | `eyJhbGciOiJIUzI1...` | `<JWT_TOKEN_1>` |

### Presidio-Based (NLP, requires K8s Presidio services)

Default entities detected (configurable via `ENTITIES_TO_PROTECT`):

| Entity | Example | Placeholder |
|--------|---------|-------------|
| Credit cards | `4111-1111-1111-1111` | `<CREDIT_CARD_1>` |
| Crypto addresses | `1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa` | `<CRYPTO_1>` |
| Email addresses | `john@example.com` | `<EMAIL_ADDRESS_1>` |
| IBAN codes | `GB29NWBK60161331926819` | `<IBAN_CODE_1>` |
| Phone numbers | `+1-555-123-4567` | `<PHONE_NUMBER_1>` |
| Medical licenses | `DEA# AB1234567` | `<MEDICAL_LICENSE_1>` |
| US bank numbers | `1234567890` | `<US_BANK_NUMBER_1>` |
| US driver licenses | `D123-456-78-901` | `<US_DRIVER_LICENSE_1>` |
| US ITIN | `9XX-XX-XXXX` | `<US_ITIN_1>` |
| US passports | `123456789` | `<US_PASSPORT_1>` |
| US SSN | `123-45-6789` | `<US_SSN_1>` |

Entities **excluded by default** (produce false positives on log data):

| Entity | Why excluded |
|--------|-------------|
| `IP_ADDRESS` | IPs are investigation evidence — redacting attacker IPs defeats security analysis. Private IPs (RFC1918) are still caught by the regex layer |
| `PERSON` | spaCy NER misclassifies month names (`Jan`, `Mar`), hostnames, and syslog fields as person names |
| `DATE_TIME` | Timestamps are essential for log correlation — redacting them destroys timeline analysis |
| `NRP` | Nationality/religious/political — irrelevant for system logs, false positives on technical terms |
| `LOCATION` | Irrelevant for system logs, false positives on server/region names |
| `URL` | URLs in logs are diagnostically important (endpoints, services) |

To re-enable any of these, add them to `ENTITIES_TO_PROTECT` in your `.env`.

## Configuration Examples

### Default: No Redaction

```bash
# Best for: Security investigations, troubleshooting
# IPs, hostnames, usernames all visible to the LLM
# No config needed — this is the default
```

### SaaS Production: Redaction Enabled

```bash
# Best for: Multi-tenant SaaS with external LLM providers
PROTECTION_ENABLED=true
SANITIZE_PII=true
```

The LLM sees `<IP_ADDRESS_1>` instead of `192.168.1.100`. The user sees `192.168.1.100` in the response.

### Self-Hosted with Local LLM

```bash
# Best for: Enterprise on-prem — data never leaves the network
LLM_PROVIDER=local
LOCAL_LLM_URL=http://localhost:11434
# No redaction needed — data stays local
```

## Advanced Configuration

### Registry TTL

The redaction registry (mapping between real values and placeholders) is persisted in Redis for cross-turn consistency. Default TTL is 7 days:

```bash
# Optional: Adjust how long redaction mappings are kept per case
# REDACTION_REGISTRY_TTL_HOURS=168    # Default: 7 days
```

After expiry, a new registry starts and placeholders may renumber. This only affects cases inactive for longer than the TTL period.

### Presidio Detection Tuning

```bash
# Confidence threshold for Presidio detections (default: 0.85)
# Higher = fewer false positives, lower = more aggressive detection
# MIN_SCORE_THRESHOLD=0.85

# Entity types to detect (comma-separated, default shown below)
# ENTITIES_TO_PROTECT=CREDIT_CARD,CRYPTO,EMAIL_ADDRESS,IBAN_CODE,PHONE_NUMBER,MEDICAL_LICENSE,US_BANK_NUMBER,US_DRIVER_LICENSE,US_ITIN,US_PASSPORT,US_SSN
```

### Presidio Services

Presidio NLP-based detection requires running Presidio Analyzer and Anonymizer services. These are only needed when protection is enabled — when both `PROTECTION_ENABLED` and `SANITIZE_PII` are `false` (the default), Presidio health checks are skipped entirely at startup and no connection attempts are made.

```bash
# K8s Ingress-based (cloud deployment)
PRESIDIO_ANALYZER_URL=http://presidio-analyzer.faultmaven.local:30080
PRESIDIO_ANONYMIZER_URL=http://presidio-anonymizer.faultmaven.local:30080

# K8s in-cluster (production)
PRESIDIO_ANALYZER_URL=http://presidio-analyzer.faultmaven.svc.cluster.local:3000
PRESIDIO_ANONYMIZER_URL=http://presidio-anonymizer.faultmaven.svc.cluster.local:3001
```

Without Presidio, only regex-based patterns are applied. Presidio adds NLP-based entity detection for the configured entity types.

## Verification

Check logs for redaction status at startup:

```text
# When protection is disabled (default / local deployment):
Skipping Presidio health checks (protection disabled)

# When protection is enabled and Presidio is reachable:
✅ Connected to K8s Presidio services

# When protection is enabled but Presidio is unreachable:
⚠️ Limited Presidio connectivity - Analyzer: False, Anonymizer: False
📝 Falling back to regex-only sanitization
```

During investigation turns:

```text
# When SANITIZE_PII=true:
🔒 LLM Router: Applying PII sanitization

# When SANITIZE_PII=false (default):
🔓 LLM Router: Skipping PII sanitization
```

## Troubleshooting

### Problem: User sees placeholders in responses

If users see `<IP_ADDRESS_1>` in agent responses, reverse-substitution may have failed. Check:

- Redis connectivity (registry persistence)
- Server logs for "Failed to load redaction registry" warnings

### Problem: Redaction removes investigation-critical data

Redaction is off by default. If you see redacted data unexpectedly:

```bash
# Check your .env — remove or set to false:
# SANITIZE_PII=true  ← this enables redaction
```

### Problem: Same IP gets different placeholders across files

This indicates the case-scoped registry is not loading from Redis. Check:

- Redis is running and accessible
- No Redis connection errors in logs
- The case hasn't been inactive longer than the registry TTL

## Related

- [Case-Scoped PII Redaction Architecture](../../architecture/security/case-scoped-pii-redaction.md) — Design document
- [Architecture Overview](../../architecture/architecture-overview.md) — System architecture

## Version History

- **v4.2.0** — Removed `IP_ADDRESS` from Presidio default entities. Public IPs are investigation evidence, not PII. Private IPs (RFC1918: 10.x, 172.16-31.x, 192.168.x) remain redacted by the regex layer.
- **v4.1.0** — Presidio settings wired to configuration: `MIN_SCORE_THRESHOLD` (default 0.85) and `ENTITIES_TO_PROTECT` are now read from settings. Removed `PERSON`, `DATE_TIME`, `NRP`, `LOCATION`, `URL` from defaults (false positives on log data). Fixed password regex corrupting compound tokens like `failed_password: 520`.
- **v4.0.0** — Case-scoped redaction: consistent placeholders across files, tool result redaction, reverse-substitution. Removed `AUTO_SANITIZE_BASED_ON_PROVIDER` (single `SANITIZE_PII` setting)
- **v3.3.0** — Changed default to off (investigation-first)
- **v3.2.0** — Added adaptive PII sanitization with auto-detect mode
- **v3.1.0** — Basic PII sanitization (always enabled)
