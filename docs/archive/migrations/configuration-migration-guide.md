# Configuration Migration Guide - OODA Framework v3.2.0

**Document Type:** Migration Guide
**Version:** 1.0
**Last Updated:** 2025-10-12
**Status:** Active

---

## Executive Summary

The OODA Investigation Framework v3.2.0 introduces significant architectural changes that make several configuration parameters obsolete. This guide documents what has changed, what's deprecated, and what's new.

---

## Removed Systems

### 1. Query Classification System (DEPRECATED)

**Why Removed:** OODA framework uses structured LLM outputs instead of rule-based classification.

**Deprecated Settings:**
```bash
# NO LONGER USED
LLM_CLASSIFICATION_MODE=enhancement
PATTERN_CONFIDENCE_THRESHOLD=0.7
ENABLE_MULTIDIMENSIONAL_CONFIDENCE=true
CONFIDENCE_OVERRIDE_THRESHOLD=0.4
SELF_CORRECTION_MIN_CONFIDENCE=0.4
SELF_CORRECTION_MAX_CONFIDENCE=0.7
PATTERN_WEIGHTED_SCORING=true
PATTERN_EXCLUSION_RULES=true
ENABLE_STRUCTURE_ANALYSIS=true
ENABLE_LINGUISTIC_ANALYSIS=true
ENABLE_ENTITY_ANALYSIS=true
ENABLE_CONTEXT_ANALYSIS=true
ENABLE_DISAMBIGUATION_CHECK=true
ENABLE_LLM_CLASSIFICATION=true
```

**Reason:** LLM returns structured responses (JSON schema enforcement) eliminating need for intent classification.

**Design Reference:** `docs/architecture/RESPONSE_FORMAT_INTEGRATION_SPEC.md`

---

### 2. Doctor/Patient Prompt System (REPLACED)

**Why Replaced:** Philosophy preserved, implementation replaced by OODA modes.

**Deprecated Settings:**
```bash
# NO LONGER USED
DOCTOR_PATIENT_PROMPT_VERSION=standard
ENABLE_DYNAMIC_PROMPT_VERSION=false
MINIMAL_PROMPT_THRESHOLD=50
DETAILED_PROMPT_THRESHOLD=0.7
```

**Replaced By:**
```bash
# OODA Framework Settings
DEFAULT_INVESTIGATION_STRATEGY=active_incident
DEFAULT_OODA_INTENSITY=medium
PROBLEM_SIGNAL_THRESHOLD=moderate
MAX_CONSULTANT_TURNS=5
```

**What Changed:**
- **Old:** 3 prompt versions (minimal, standard, detailed)
- **New:** 2 engagement modes (Consultant, Lead Investigator) + 7 investigation phases

**Design Reference:** `docs/architecture/investigation-phases-and-ooda-integration.md`

---

## New Configuration Sections

### 1. OODA Investigation Framework Settings

**Added Settings:**
```bash
# Investigation Strategy
DEFAULT_INVESTIGATION_STRATEGY=active_incident  # or post_mortem
DEFAULT_OODA_INTENSITY=medium                   # light|medium|full

# Memory Management (4-Tier Hierarchical)
HOT_MEMORY_TOKENS=500                           # Last 2 iterations
WARM_MEMORY_TOKENS=300                          # Iterations 3-5 (summarized)
COLD_MEMORY_TOKENS=100                          # Older (key facts)
PERSISTENT_MEMORY_TOKENS=100                    # Always accessible

# Phase Control
ENABLE_PHASE_SKIP=true                          # Allow skipping in active incidents
MIN_CONFIDENCE_TO_ADVANCE=0.70                  # Advance threshold
STALL_DETECTION_ITERATIONS=3                    # Iterations without progress

# Consultant Mode
PROBLEM_SIGNAL_THRESHOLD=moderate               # weak|moderate|strong
MAX_CONSULTANT_TURNS=5                          # Before suggesting Lead Investigator
```

**Purpose:**
- Control investigation behavior across 7 phases
- Manage memory budgets for token optimization
- Configure engagement mode transitions

---

### 2. Structured Response Configuration

**Implicitly Configured (via LLM provider):**
- Function calling (Tier 1) - Automatic when OpenAI/Anthropic used
- JSON parsing (Tier 2) - Always available
- Heuristic extraction (Tier 3) - Fallback

**No new environment variables needed** - behavior determined by:
1. LLM provider capabilities (function calling support)
2. Response schema (Pydantic models)
3. Three-tier fallback parser (automatic)

---

## Updated Settings

### 1. LLM Provider Settings

**Changed:**
```bash
# Old
CLASSIFIER_PROVIDER=local  # REMOVED - no classification layer

# New - Only these are needed
CHAT_PROVIDER=openai
OPENAI_API_KEY=xxx
OPENAI_MODEL=gpt-4o
OPENAI_API_BASE=https://api.openai.com/v1  # NEW - explicit base URL
LLM_REQUEST_TIMEOUT=30
LLM_MAX_RETRIES=3
LLM_MAX_TOKENS=4096
LLM_CONTEXT_WINDOW=128000
```

**Additions:**
- `OPENAI_API_BASE` - Explicit API base URL for each provider
- `LLM_CONTEXT_WINDOW` - Track context window size for memory management

---

### 2. Session Settings

**Unchanged but clarified:**
```bash
SESSION_TIMEOUT_MINUTES=180                     # 3 hours default
SESSION_CLEANUP_INTERVAL_MINUTES=30
SESSION_MAX_MEMORY_MB=100
SESSION_HEARTBEAT_INTERVAL_SECONDS=30
MAX_SESSIONS_PER_USER=10
```

**Note:** These work with OODA framework - no changes needed.

---

### 3. Context Management

**Changed:**
```bash
# Old - Part of classification system
ENABLE_TOKEN_AWARE_CONTEXT=true
MAX_CLARIFICATIONS=3
MAX_CONVERSATION_TURNS=20
MAX_CONVERSATION_TOKENS=4000
CONTEXT_TOKEN_BUDGET=4000

# New - Simplified for OODA
ENABLE_TOKEN_AWARE_CONTEXT=true
ENABLE_CONVERSATION_SUMMARIZATION=true
MAX_CONVERSATION_TURNS=20
MAX_CONVERSATION_TOKENS=4000

# Memory tiers managed by OODA-specific settings (see above)
```

**What Changed:**
- Removed classification-related context settings
- Memory budgets now managed by OODA 4-tier system
- `MAX_CLARIFICATIONS` no longer used (handled by phase logic)

---

## Migration Checklist

### Step 1: Backup Current Configuration
```bash
cp .env .env.backup
cp faultmaven/config/settings.py settings.py.backup
```

### Step 2: Update .env File
```bash
# Remove deprecated settings (or comment them out)
# Add new OODA settings (see .env.example.new)
# Verify LLM provider settings include base URLs
```

### Step 3: Update settings.py
```bash
# Add OODASettings class
# Mark deprecated settings as deprecated=True
# Update FeatureSettings to remove obsolete flags
```

### Step 4: Test Configuration
```bash
python -c "from faultmaven.config.settings import get_settings; s = get_settings(); print('Config loaded successfully')"
```

### Step 5: Verify OODA Framework
```bash
# Start server
./run_faultmaven.sh

# Check logs for:
# - "OODA Framework v3.2.0 initialized"
# - No warnings about deprecated settings
# - Structured response parser loaded
```

---

## Backward Compatibility

### Deprecated Settings Behavior

**Current (v3.2.0):**
- Deprecated settings are **ignored** (logged as warning)
- No impact on functionality
- Will be **removed** in v4.0.0

**Warning Example:**
```
[WARN] Deprecated setting 'LLM_CLASSIFICATION_MODE' found in .env - this setting is no longer used
[INFO] Please update your .env file to remove deprecated settings
```

### Gradual Migration

**If you want to keep old settings temporarily:**
1. Leave them in `.env` - they won't break anything
2. Add new OODA settings alongside
3. Remove deprecated settings when ready

**Recommendation:** Clean removal now to avoid confusion.

---

## Settings.py Changes Required

### Add OODASettings Class

```python
class OODASettings(BaseSettings):
    """OODA Investigation Framework configuration (v3.2.0)"""

    # Investigation Strategy
    default_strategy: str = Field(
        default="active_incident",
        env="DEFAULT_INVESTIGATION_STRATEGY",
        description="active_incident or post_mortem"
    )

    default_intensity: str = Field(
        default="medium",
        env="DEFAULT_OODA_INTENSITY",
        description="light, medium, or full"
    )

    # Memory Management (4-Tier Hierarchical)
    hot_memory_tokens: int = Field(default=500, env="HOT_MEMORY_TOKENS")
    warm_memory_tokens: int = Field(default=300, env="WARM_MEMORY_TOKENS")
    cold_memory_tokens: int = Field(default=100, env="COLD_MEMORY_TOKENS")
    persistent_memory_tokens: int = Field(default=100, env="PERSISTENT_MEMORY_TOKENS")

    # Phase Control
    enable_phase_skip: bool = Field(default=True, env="ENABLE_PHASE_SKIP")
    min_confidence_to_advance: float = Field(default=0.70, env="MIN_CONFIDENCE_TO_ADVANCE")
    stall_detection_iterations: int = Field(default=3, env="STALL_DETECTION_ITERATIONS")

    # Consultant Mode
    problem_signal_threshold: str = Field(default="moderate", env="PROBLEM_SIGNAL_THRESHOLD")
    max_consultant_turns: int = Field(default=5, env="MAX_CONSULTANT_TURNS")

    model_config = {"env_prefix": "", "extra": "ignore"}
```

### Update FaultMavenSettings

```python
class FaultMavenSettings(BaseSettings):
    # ... existing fields ...

    # Add OODA configuration
    ooda: OODASettings = Field(default_factory=OODASettings)

    # Mark deprecated sections
    # Remove ConversationThresholds (merged into OODASettings)
    # Remove PromptSettings (replaced by OODA modes)
```

### Mark Deprecated Settings

```python
class FeatureSettings(BaseSettings):
    """Feature flags and toggles"""

    # ... existing fields ...

    # DEPRECATED - Remove in v4.0.0
    llm_classification_mode: Optional[str] = Field(
        default=None,
        env="LLM_CLASSIFICATION_MODE",
        deprecated=True,
        description="DEPRECATED: No longer used in OODA framework"
    )

    enable_multidimensional_confidence: Optional[bool] = Field(
        default=None,
        env="ENABLE_MULTIDIMENSIONAL_CONFIDENCE",
        deprecated=True,
        description="DEPRECATED: No longer used in OODA framework"
    )

    # ... other deprecated settings ...
```

---

## Testing Updated Configuration

### Unit Test

```python
def test_ooda_settings_loaded():
    """Verify OODA settings load correctly"""
    from faultmaven.config.settings import get_settings

    settings = get_settings()

    assert settings.ooda.default_strategy in ["active_incident", "post_mortem"]
    assert settings.ooda.default_intensity in ["light", "medium", "full"]
    assert settings.ooda.hot_memory_tokens == 500
    assert settings.ooda.warm_memory_tokens == 300
    assert settings.ooda.cold_memory_tokens == 100
    assert settings.ooda.persistent_memory_tokens == 100
```

### Integration Test

```bash
# Start server with updated config
./run_faultmaven.sh

# Make test request
curl -X POST http://localhost:8000/api/v1/agent/query \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test-123", "query": "My API is returning 500 errors"}'

# Verify response has structured format
# Should see: suggested_actions, clarifying_questions, etc.
```

---

## Troubleshooting

### Issue: "Settings validation failed"

**Cause:** Missing required settings or invalid values

**Solution:**
```bash
# Check which settings are causing issues
python -c "from faultmaven.config.settings import get_settings; get_settings()"

# Common fixes:
# 1. Ensure CHAT_PROVIDER is set
# 2. Ensure API key for chosen provider is set
# 3. Verify REDIS_HOST and REDIS_PORT are correct
```

### Issue: "Deprecated setting warning"

**Cause:** Old settings still in .env

**Solution:**
```bash
# Safe to ignore, but clean up:
grep -E "LLM_CLASSIFICATION|DOCTOR_PATIENT|PATTERN_" .env
# Remove or comment out matching lines
```

### Issue: "OODA framework not initializing"

**Cause:** Missing OODA settings

**Solution:**
```bash
# Add to .env:
DEFAULT_INVESTIGATION_STRATEGY=active_incident
DEFAULT_OODA_INTENSITY=medium
HOT_MEMORY_TOKENS=500
WARM_MEMORY_TOKENS=300
COLD_MEMORY_TOKENS=100
PERSISTENT_MEMORY_TOKENS=100
```

---

## Related Documentation

- [OODA Investigation Framework](./investigation-phases-and-ooda-integration.md)
- [Response Format Integration](./RESPONSE_FORMAT_INTEGRATION_SPEC.md)
- [Prompt Engineering Architecture](./prompt-engineering-architecture.md)
- [Session Management](./session-management-specification.md)

---

## Document Metadata

**Version History:**
- v1.0 (2025-10-12): Initial migration guide for OODA v3.2.0

**Audience:** DevOps, Development Team, System Administrators

**Maintained By:** Architecture Team
**Review Cycle:** Before each major release
