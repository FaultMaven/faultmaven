# Environment Variables Guide

## Where are environment variables set?

Environment variables are defined in **`.env` file** in the project root directory.

### File Structure

```
FaultMaven/
├── .env                    # Active configuration (gitignored - never commit!)
├── .env.example            # Template with all available variables and defaults
├── .env.backup             # Backup (if exists)
└── faultmaven/
    └── config/
        └── settings.py     # Loads and validates environment variables
```

### Files Explained

1. **`.env`** - Your actual configuration
   - Contains real API keys, passwords, settings
   - **GITIGNORED** - Never committed to version control
   - Created by copying `.env.example` and filling in values

2. **`.env.example`** - Template and documentation
   - Shows all available environment variables
   - Includes comments explaining each variable
   - Safe to commit (contains no secrets)
   - Used by new developers to create their `.env`

## When are they loaded?

Environment variables are loaded **at application startup** in this sequence:

### 1. Startup Sequence

```
Application Start
    ↓
get_settings() called (first time)
    ↓
load_dotenv(override=True)  ← Loads .env file
    ↓
FaultMavenSettings.__init__() ← Pydantic reads env vars
    ↓
Validation & Type Checking
    ↓
Settings instance created (singleton)
    ↓
Application continues with loaded settings
```

### 2. Code Location

**File**: `faultmaven/config/settings.py`

```python
def get_settings() -> FaultMavenSettings:
    """Get global settings instance (singleton pattern)"""
    global _settings_instance
    if _settings_instance is None:
        # Load .env file BEFORE creating settings
        from dotenv import load_dotenv
        load_dotenv(override=True)  # ← HERE: .env loaded

        # Create settings instance (reads env vars)
        _settings_instance = FaultMavenSettings()  # ← HERE: Env vars read

    return _settings_instance
```

### 3. Singleton Pattern

Settings are loaded **once** when first accessed, then reused:

```python
# First call - loads .env and creates settings
settings = get_settings()  # Loads .env, reads env vars

# Subsequent calls - returns same instance
settings = get_settings()  # No reload, returns cached instance
```

**Important**: Changes to `.env` require application restart to take effect.

## How Pydantic Loads Environment Variables

### Automatic Loading

Pydantic's `BaseSettings` automatically:
1. Looks for environment variables matching field names
2. Uses `env="VARIABLE_NAME"` to map non-standard names
3. Applies type validation (int, float, bool, str)
4. Uses defaults if env var not set

### Example

```python
class ConversationThresholds(BaseSettings):
    max_clarifications: int = Field(default=3, env="MAX_CLARIFICATIONS")
    #                                ↑          ↑
    #                             default    env var name
```

**Loading sequence**:
1. Check for `MAX_CLARIFICATIONS` env var
2. If found: validate it's an int, use it
3. If not found: use default value (3)
4. If invalid type: raise validation error

## Configuration Priority

Settings are loaded in this priority order (highest to lowest):

1. **Environment variables** (`.env` file)
2. **Default values** (in `settings.py`)
3. **System environment** (OS-level env vars, if `override=False`)

Currently using `override=True`, so `.env` always wins.

## ConversationThresholds Variables

All these are **optional** - application works with defaults if not set.

### In `.env` file:

```bash
# Conversation Limits
MAX_CLARIFICATIONS=3                    # Default: 3
MAX_CONVERSATION_TURNS=20               # Default: 20
MAX_CONVERSATION_TOKENS=4000            # Default: 4000

# Token Budgets
CONTEXT_TOKEN_BUDGET=4000               # Default: 4000
SYSTEM_PROMPT_MAX_TOKENS=500            # Default: 500
PATTERN_TEMPLATE_MAX_TOKENS=300         # Default: 300

# Classification Thresholds
PATTERN_CONFIDENCE_THRESHOLD=0.7        # Default: 0.7
CONFIDENCE_OVERRIDE_THRESHOLD=0.4       # Default: 0.4
SELF_CORRECTION_MIN_CONFIDENCE=0.4      # Default: 0.4
SELF_CORRECTION_MAX_CONFIDENCE=0.7      # Default: 0.7
```

### In code:

```python
from faultmaven.config.settings import get_settings

settings = get_settings()

# Access thresholds
max_clarifications = settings.thresholds.max_clarifications
# ↑ reads MAX_CLARIFICATIONS from .env or uses default (3)
```

## Setting Up Your Environment

### For Development (First Time)

```bash
# 1. Copy example to create your .env
cp .env.example .env

# 2. Edit .env with your values
nano .env  # or vim, code, etc.

# 3. Add your API keys
OPENAI_API_KEY=sk-...
FIREWORKS_API_KEY=...

# 4. Adjust thresholds (optional)
MAX_CLARIFICATIONS=5
PATTERN_CONFIDENCE_THRESHOLD=0.8

# 5. Start application
./run_faultmaven.sh
```

### For Production

```bash
# Use environment-specific files
.env.development
.env.staging
.env.production

# Load appropriate file
export ENV_FILE=.env.production
python -m faultmaven.main
```

Or use **environment variables directly** (Docker/K8s):

```yaml
# docker-compose.yml or k8s deployment
environment:
  - MAX_CLARIFICATIONS=5
  - PATTERN_CONFIDENCE_THRESHOLD=0.8
  - OPENAI_API_KEY=${OPENAI_API_KEY}  # From secrets
```

## Validation and Type Safety

### Type Validation

Pydantic validates types automatically:

```bash
# In .env
MAX_CLARIFICATIONS=abc  # Invalid!
```

```python
# Startup error:
ValidationError: 1 validation error for ConversationThresholds
max_clarifications
  value is not a valid integer (type=type_error.integer)
```

### Range Validation

Some fields have validators:

```python
class ConversationThresholds(BaseSettings):
    pattern_confidence_threshold: float = Field(
        default=0.7,
        env="PATTERN_CONFIDENCE_THRESHOLD",
        ge=0.0,  # ≥ 0.0
        le=1.0   # ≤ 1.0
    )
```

**If you set**: `PATTERN_CONFIDENCE_THRESHOLD=1.5`

**Error**: `value must be <= 1.0`

## Runtime Changes

### Can I change env vars at runtime?

**No** - Settings are loaded once at startup (singleton pattern).

**To apply changes**:
1. Edit `.env` file
2. Restart application

```bash
# Edit .env
vim .env

# Restart
./run_faultmaven.sh  # Stops and restarts
```

### For development hot-reload

Use `uvicorn` with `--reload`:

```bash
uvicorn faultmaven.main:app --reload
# Changes to .env require manual restart even with --reload
```

## Troubleshooting

### Problem: Changes to .env not taking effect

**Cause**: Application not restarted

**Fix**:
```bash
# Stop application (Ctrl+C)
# Start again
./run_faultmaven.sh
```

### Problem: "Field required" error on startup

**Cause**: Required field missing from `.env`

**Fix**: Add the required variable to `.env`

```bash
# Error says: Field required for 'openai_api_key'
# Add to .env:
OPENAI_API_KEY=sk-your-key-here
```

### Problem: Using wrong defaults

**Cause**: Variable in `.env` but typo in name

**Example**:
```bash
# In .env
MAX_CLARIFICATONS=5  # Typo! Should be MAX_CLARIFICATIONS
```

**Result**: Uses default (3) instead of 5

**Fix**: Check spelling exactly matches `env="..."` in settings.py

### Problem: Boolean values not working

**Cause**: Pydantic is strict about boolean parsing

**Correct**:
```bash
ENABLE_INTELLIGENT_PROMPTS=true   # lowercase
ENABLE_INTELLIGENT_PROMPTS=false
ENABLE_INTELLIGENT_PROMPTS=1      # also works
ENABLE_INTELLIGENT_PROMPTS=0
```

**Incorrect**:
```bash
ENABLE_INTELLIGENT_PROMPTS=True   # Capital T
ENABLE_INTELLIGENT_PROMPTS=yes    # Not recognized
```

## Environment Variable Naming Convention

FaultMaven follows these conventions:

1. **ALL_CAPS** - Environment variable names
2. **SNAKE_CASE** - Multiple words separated by underscores
3. **Prefixes** - Group related variables
   - `OPENAI_*` - OpenAI settings
   - `REDIS_*` - Redis settings
   - `ENABLE_*` - Feature flags
   - `MAX_*` - Limit thresholds

## Security Best Practices

### ❌ Never commit .env

```bash
# .gitignore should have:
.env
.env.local
.env.*.local
```

### ✅ Use .env.example

```bash
# Document all variables (without secrets)
# Commit .env.example to repo
git add .env.example
git commit -m "Update env template"
```

### ✅ Rotate secrets regularly

```bash
# Change API keys periodically
# Update .env with new keys
# Restart application
```

### ✅ Use different secrets per environment

```bash
# Development
OPENAI_API_KEY=sk-dev-key-...

# Production
OPENAI_API_KEY=sk-prod-key-...  # Different key!
```

## Complete List of ConversationThresholds Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAX_CLARIFICATIONS` | int | 3 | Max clarification requests before escalation |
| `MAX_CONVERSATION_TURNS` | int | 20 | Max conversation turns to track |
| `MAX_CONVERSATION_TOKENS` | int | 4000 | Max tokens for conversation history |
| `CONTEXT_TOKEN_BUDGET` | int | 4000 | Total token budget (system+user+history) |
| `SYSTEM_PROMPT_MAX_TOKENS` | int | 500 | Max tokens for system prompt (warning) |
| `PATTERN_TEMPLATE_MAX_TOKENS` | int | 300 | Max tokens for pattern templates |
| `PATTERN_CONFIDENCE_THRESHOLD` | float | 0.7 | Trigger LLM when pattern confidence < this |
| `CONFIDENCE_OVERRIDE_THRESHOLD` | float | 0.4 | Force clarification when confidence < this |
| `SELF_CORRECTION_MIN_CONFIDENCE` | float | 0.4 | Lower bound for self-correction prompt |
| `SELF_CORRECTION_MAX_CONFIDENCE` | float | 0.7 | Upper bound for self-correction prompt |

## Related Documentation

- [Context Management Guide](./CONTEXT_MANAGEMENT.md)
- [Token Estimation Guide](./TOKEN_ESTIMATION.md)
- [Configuration Reference](../architecture/SYSTEM_ARCHITECTURE.md)

## Quick Reference: ConversationThresholds Variables

All environment variables for the ConversationThresholds system:

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAX_CLARIFICATIONS` | int | 3 | Max clarification requests before escalation |
| `MAX_CONVERSATION_TURNS` | int | 20 | Max conversation turns to track |
| `MAX_CONVERSATION_TOKENS` | int | 4000 | Max tokens for conversation history |
| `CONTEXT_TOKEN_BUDGET` | int | 4000 | Total token budget (system+user+history) |
| `SYSTEM_PROMPT_MAX_TOKENS` | int | 500 | Max tokens for system prompt (warning) |
| `PATTERN_TEMPLATE_MAX_TOKENS` | int | 300 | Max tokens for pattern templates |
| `PATTERN_CONFIDENCE_THRESHOLD` | float | 0.7 | Trigger LLM when pattern confidence < this |
| `CONFIDENCE_OVERRIDE_THRESHOLD` | float | 0.4 | Force clarification when confidence < this |
| `SELF_CORRECTION_MIN_CONFIDENCE` | float | 0.4 | Lower bound for self-correction prompt |
| `SELF_CORRECTION_MAX_CONFIDENCE` | float | 0.7 | Upper bound for self-correction prompt |

### Status

- ✅ All variables added to `.env` with defaults
- ✅ All variables added to `.env.example` with documentation
- ✅ All variables defined in `ConversationThresholds` class (settings.py)
- ✅ Duplicates removed from `FeatureSettings`
- ✅ Production ready - no migration needed (defaults work)

### Configuration in Code

```python
from faultmaven.config.settings import get_settings

settings = get_settings()

# Access thresholds
max_clarifications = settings.thresholds.max_clarifications  # 3
pattern_confidence = settings.thresholds.pattern_confidence_threshold  # 0.7
```

### Files Modified

1. **`faultmaven/config/settings.py`** - Added ConversationThresholds class
2. **`.env`** - Added all threshold variables with defaults
3. **`.env.example`** - Added all threshold variables with documentation

See [Infrastructure Improvements](../../releases/INFRASTRUCTURE_IMPROVEMENTS_2025-10-04.md) for full details.
