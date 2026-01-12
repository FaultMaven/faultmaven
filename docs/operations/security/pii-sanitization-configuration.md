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

### Mode 1: Auto-Detect (Recommended - Default)

Automatically enables/disables sanitization based on LLM provider:

```bash
# .env configuration
AUTO_SANITIZE_BASED_ON_PROVIDER=true  # Default
```

**Behavior:**
- `CHAT_PROVIDER=local` → **No sanitization** (preserves all data)
- `CHAT_PROVIDER=openai` → **Sanitizes PII** (protects privacy)
- `CHAT_PROVIDER=anthropic` → **Sanitizes PII**
- `CHAT_PROVIDER=fireworks` → **Sanitizes PII**

### Mode 2: Manual Control

Explicitly enable/disable sanitization regardless of provider:

```bash
# .env configuration
AUTO_SANITIZE_BASED_ON_PROVIDER=false
SANITIZE_PII=false  # Disable sanitization (use with caution!)
```

**⚠️ Warning:** Disabling sanitization with external providers exposes user data to 3rd parties.

## Configuration Examples

### Example 1: Local LLM (Ollama) - No Sanitization

```bash
# Best for: Maximum data preservation with local models
CHAT_PROVIDER=local
LOCAL_LLM_URL=http://localhost:11434
LOCAL_LLM_MODEL=llama2

# Auto mode automatically disables sanitization
AUTO_SANITIZE_BASED_ON_PROVIDER=true
```

**Result:** ✅ Zero data loss, all PII preserved (safe because data stays local)

### Example 2: External Provider - Auto Sanitization

```bash
# Best for: Production use with external APIs
CHAT_PROVIDER=openai
OPENAI_API_KEY=sk-...

# Auto mode automatically enables sanitization
AUTO_SANITIZE_BASED_ON_PROVIDER=true
```

**Result:** 🔒 PII sanitized before sending to OpenAI (protects user privacy)

### Example 3: Trusted External Provider - Manual Override

```bash
# For: Enterprise users with BAA/DPA agreements with LLM provider
CHAT_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Explicitly disable sanitization
AUTO_SANITIZE_BASED_ON_PROVIDER=false
SANITIZE_PII=false
```

**Result:** ⚠️ Full data sent to Anthropic (only use if you have legal agreements!)

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
- Use `AUTO_SANITIZE_BASED_ON_PROVIDER=true` (recommended default)
- Use local LLMs when handling highly sensitive data
- Review your LLM provider's data retention policy
- Test sanitization with sample data before production use

### ❌ DON'T:
- Disable sanitization with external providers without legal justification
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

### Problem: Sanitization removes important data

**Solution 1:** Use local LLM provider
```bash
CHAT_PROVIDER=local
```

**Solution 2:** Manually disable (if safe to do so)
```bash
AUTO_SANITIZE_BASED_ON_PROVIDER=false
SANITIZE_PII=false
```

### Problem: Data still being sent to external provider

**Solution:** Verify configuration is loaded:
```bash
# Check logs for:
🔓 PII sanitization DISABLED (using LOCAL LLM provider)
```

If you see `🔒 ENABLED`, check your `CHAT_PROVIDER` setting.

## Related Configuration

See also:
- [LLM Provider Configuration](../getting-started/configuration.md)
- [Security Best Practices](../security/best-practices.md)
- [Local LLM Setup](../how-to/setup-local-llm.md)

## Version History

- **v3.2.0** - Added adaptive PII sanitization with auto-detect mode
- **v3.1.0** - Basic PII sanitization (always enabled)
