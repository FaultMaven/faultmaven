# PII Sanitization Configuration Guide

## Overview

FaultMaven provides flexible PII (Personally Identifiable Information) sanitization controls to protect sensitive data when using external LLM providers while allowing full data preservation when using local/self-hosted models.

## The Problem

By default, FaultMaven sends data to **external 3rd-party LLM APIs** (OpenAI, Anthropic, Fireworks). This creates privacy risks:

- Production logs containing customer emails
- Stack traces with API keys or credentials
- System configs with internal hostnames/IPs
- Error messages with sensitive user data

**Without sanitization**, this data is sent to external providers and may be:
- Logged by the provider
- Used for model training (depending on provider terms)
- Subject to the provider's data retention policies

## The Solution

FaultMaven provides **adaptive PII sanitization** with two configuration modes:

### Mode 1: Off (Default)

PII sanitization is **off by default**. FaultMaven is a troubleshooting tool — redacting IPs, hostnames, and usernames defeats the core use case for security investigations.

```bash
# .env configuration (default — no action needed)
AUTO_SANITIZE_BASED_ON_PROVIDER=false
```

### Mode 2: Auto-Detect

Automatically enables sanitization for external providers, disables for local:

```bash
# .env configuration
AUTO_SANITIZE_BASED_ON_PROVIDER=true
```

**Behavior:**

- `LLM_PROVIDER=local` → **No sanitization** (preserves all data)
- `LLM_PROVIDER=openai` → **Sanitizes PII** (protects privacy)
- `LLM_PROVIDER=anthropic` → **Sanitizes PII**
- `LLM_PROVIDER=fireworks` → **Sanitizes PII**

### Mode 3: Manual Control

Explicitly enable sanitization regardless of provider:

```bash
# .env configuration
SANITIZE_PII=true
```

**⚠️ Note:** When `AUTO_SANITIZE_BASED_ON_PROVIDER=false` (default), the `SANITIZE_PII` setting controls sanitization directly.

## Configuration Examples

### Example 1: Security Investigation (Default)

```bash
# Best for: Troubleshooting security incidents — IPs, hostnames, usernames preserved
LLM_PROVIDER=fireworks
FIREWORKS_API_KEY=fw_...

# Default: no sanitization (no config needed)
```

**Result:** ✅ All investigation data preserved — IPs, hostnames, usernames visible to the LLM

### Example 2: External Provider with Auto-Sanitization

```bash
# Best for: Production use where privacy matters more than investigation fidelity
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...

# Enable auto-detect mode
AUTO_SANITIZE_BASED_ON_PROVIDER=true
```

**Result:** 🔒 PII sanitized before sending to OpenAI (protects user privacy)

### Example 3: Local LLM (Ollama)

```bash
# Best for: Maximum privacy — data never leaves the machine
LLM_PROVIDER=local
LOCAL_LLM_URL=http://localhost:11434
LOCAL_LLM_MODEL=llama2
```

**Result:** ✅ Zero data loss, all PII preserved (data stays local)

## What Gets Sanitized

When sanitization is enabled, FaultMaven redacts:

- **Email addresses** → `<EMAIL_ADDRESS>`
- **Phone numbers** → `<PHONE_NUMBER>`
- **Credit card numbers** → `<CREDIT_CARD>`
- **US Social Security Numbers** → `<US_SSN>`
- **IP addresses** → `<IP_ADDRESS>`
- **Person names** → `<PERSON>` (if detected)
- **Locations** → `<LOCATION>` (if detected)
- **API keys/tokens** → `<API_KEY>` (pattern-based)

## Verification

Check the logs to see sanitization status:

```
# With LOCAL provider:
🔓 PII sanitization DISABLED (using LOCAL LLM provider)

# With external provider:
🔒 PII sanitization ENABLED (using external provider: openai)
```

## Best Practices

### ✅ DO:

- Enable `AUTO_SANITIZE_BASED_ON_PROVIDER=true` when handling customer PII with external providers
- Use local LLMs when handling highly sensitive data
- Review your LLM provider's data retention policy
- Test sanitization with sample data before production use

### ❌ DON'T:

- Enable sanitization for security investigations (it redacts IPs, hostnames — the data you need)
- Assume external providers don't log data (check their policies)
- Upload customer production data without proper safeguards

## Size-Based Adaptive Processing

FaultMaven also implements size-based adaptive preprocessing:

| Data Size | Processing Tier | PII Sanitization |
|-----------|----------------|------------------|
| < 5K chars | Tier 1: Raw pass-through | Applied if enabled |
| 5K-50K | Tier 2: Augmented preprocessing | Applied if enabled |
| 50K-500K | Tier 3: Smart summarization | Applied if enabled |
| > 500K | Tier 4: Chunk-based processing | Applied if enabled |

**Note:** Sanitization is applied **after** preprocessing but **before** sending to LLM.

## Troubleshooting

### Problem: Sanitization removes important investigation data

Sanitization is off by default. If you see redacted data (`<IP_ADDRESS>`, `<PERSON>`), check for explicit opt-in:

```bash
# Check logs for:
🔒 LLM Router: Applying PII sanitization

# Fix: ensure these are not set
# AUTO_SANITIZE_BASED_ON_PROVIDER=true  ← remove or set to false
# SANITIZE_PII=true                     ← remove or set to false
```

### Problem: Want to enable sanitization for privacy

```bash
# Option 1: Auto-detect (sanitizes for external providers, skips for local)
AUTO_SANITIZE_BASED_ON_PROVIDER=true

# Option 2: Always sanitize
SANITIZE_PII=true
```

## Related Configuration

See also:
- [LLM Provider Configuration](../getting-started/configuration.md)
- [Security Best Practices](../security/best-practices.md)
- [Local LLM Setup](../how-to/setup-local-llm.md)

## Version History

- **v3.3.0** - Changed default to off (investigation-first); auto-detect is opt-in via `AUTO_SANITIZE_BASED_ON_PROVIDER=true`
- **v3.2.0** - Added adaptive PII sanitization with auto-detect mode
- **v3.1.0** - Basic PII sanitization (always enabled)
