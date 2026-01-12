# Token Estimation for Developers and Admins

## Overview

FaultMaven provides **accurate token estimation** using official provider tokenizers instead of rough character-based estimates. This is critical for:
- **Cost optimization** - Avoid unnecessary token usage
- **Context management** - Stay within model context limits
- **Performance monitoring** - Track token usage patterns
- **Budget enforcement** - Prevent runaway costs

## For Developers: Using Token Estimation

### Basic Usage

```python
from faultmaven.utils.token_estimation import estimate_tokens

# Estimate tokens for a string
tokens = estimate_tokens(
    text="Hello, how can I help you today?",
    provider="openai"  # or "anthropic", "fireworks", "local"
)
# Returns: 8
```

### Provider-Specific Estimation

Different providers use different tokenizers with varying token counts for the same text:

```python
text = "Kubernetes pod crashed with OOMKilled error"

# OpenAI/GPT models (tiktoken)
openai_tokens = estimate_tokens(text, provider="openai", model="gpt-4")
# Uses cl100k_base encoding

# Anthropic/Claude models (official tokenizer)
anthropic_tokens = estimate_tokens(text, provider="anthropic", model="claude-3-sonnet-20240229")
# Uses Anthropic's official tokenizer

# Fireworks AI (tiktoken - most models are OpenAI-compatible)
fireworks_tokens = estimate_tokens(text, provider="fireworks", model="llama-v3p1-405b-instruct")
# Uses tiktoken with OpenAI encoding

# Local/unsupported providers (fallback)
local_tokens = estimate_tokens(text, provider="local")
# Uses character-based heuristic: len(text) // 4
```

### Estimating Complete Prompts

For full prompt assembly with system prompt, user message, and conversation history:

```python
from faultmaven.utils.token_estimation import estimate_prompt_tokens

breakdown = estimate_prompt_tokens(
    system_prompt="You are FaultMaven, an expert SRE...",
    user_message="Why is my pod crashing?",
    conversation_history="User: Hello\nAssistant: Hi there...",
    provider="fireworks",
    model="llama-v3p1-405b-instruct"
)

print(breakdown)
# {
#     "system": 45,
#     "user": 8,
#     "history": 12,
#     "total": 65
# }
```

### Integration with Context Management

Check conversation history against token budget:

```python
from faultmaven.utils.token_estimation import estimate_tokens
from faultmaven.config.settings import get_settings

settings = get_settings()

# Estimate tokens for conversation history
history_tokens = estimate_tokens(
    conversation_history,
    provider=settings.llm.provider,
    model=settings.llm.get_model()
)

# Check against budget
if history_tokens > settings.thresholds.max_conversation_tokens:
    # Truncate or summarize
    logger.warning(
        f"Conversation history exceeds budget: {history_tokens} > "
        f"{settings.thresholds.max_conversation_tokens}"
    )
    conversation_history = truncate_history(conversation_history, max_tokens)
```

### Real-World Example: Intelligent Query Processor

```python
from faultmaven.utils.token_estimation import estimate_prompt_tokens
from faultmaven.prompts import get_tiered_prompt

# Build prompt components
system_prompt = get_tiered_prompt(response_type, complexity)
user_message = sanitized_query
conversation_history = format_conversation_history(messages)

# Estimate total tokens
token_breakdown = estimate_prompt_tokens(
    system_prompt=system_prompt,
    user_message=user_message,
    conversation_history=conversation_history,
    provider="fireworks"
)

# Log for monitoring
logger.info(
    f"Prompt token usage: {token_breakdown['total']} tokens "
    f"(system={token_breakdown['system']}, "
    f"user={token_breakdown['user']}, "
    f"history={token_breakdown['history']})"
)

# Track in metrics
self.token_tracker.record_usage(
    prompt_tokens=token_breakdown['total'],
    response_type=response_type.value,
    complexity=complexity,
    breakdown=token_breakdown
)
```

## For Admins: Configuration and Monitoring

### Token Budget Configuration

Set token budgets in `.env`:

```bash
# Maximum tokens for conversation history
MAX_CONVERSATION_TOKENS=4000

# Total context budget (system + user + history)
CONTEXT_TOKEN_BUDGET=4000

# Component limits
SYSTEM_PROMPT_MAX_TOKENS=500
PATTERN_TEMPLATE_MAX_TOKENS=300
```

### Monitoring Token Usage

Track these metrics in your observability platform:

#### 1. Per-Request Metrics

```python
{
    "prompt_tokens": {
        "system": 210,
        "user": 15,
        "history": 340,
        "total": 565
    },
    "response_type": "ANSWER",
    "complexity": "simple",
    "provider": "fireworks"
}
```

#### 2. Aggregate Statistics

```python
# By response type
"by_response_type": {
    "ANSWER": {
        "count": 150,
        "average": 245,
        "min": 80,
        "max": 520
    },
    "PLAN_PROPOSAL": {
        "count": 45,
        "average": 890,
        "min": 320,
        "max": 1850
    }
}

# By complexity
"by_complexity": {
    "simple": {
        "count": 120,
        "average": 310,
        "min": 80,
        "max": 620
    },
    "complex": {
        "count": 75,
        "average": 1240,
        "min": 580,
        "max": 2100
    }
}
```

### Cost Optimization Strategies

#### 1. Tiered Prompts (81% Reduction Achieved)

System automatically uses minimal prompts for simple queries:

```python
# ANSWER responses: 30 tokens (minimal prompt)
# Simple troubleshooting: 90 tokens (brief prompt)
# Complex troubleshooting: 210 tokens (standard prompt)
```

#### 2. Pattern Templates (87% Reduction Achieved)

Compact pattern directives replace verbose examples:

```python
# Old: ~1,500 tokens per example
# New: ~200 tokens per pattern
```

#### 3. Same-Provider Optimization

Skip redundant LLM classification when same provider handles both tasks:

```bash
# Enable when both use same provider
CHAT_PROVIDER=fireworks
CLASSIFIER_PROVIDER=fireworks  # or just use local classification
```

Savings: **1 LLM call per query** (classification call eliminated)

#### 4. Conversation History Truncation

Implement smart truncation when history exceeds budget:

```python
from faultmaven.utils.token_estimation import estimate_tokens

def truncate_conversation_history(
    history: str,
    max_tokens: int,
    provider: str
) -> str:
    """Truncate conversation history to fit token budget"""
    current_tokens = estimate_tokens(history, provider)

    if current_tokens <= max_tokens:
        return history

    # Keep most recent messages
    messages = history.split("\n\n")
    messages.reverse()  # Most recent first

    truncated = []
    total_tokens = 0

    for msg in messages:
        msg_tokens = estimate_tokens(msg, provider)
        if total_tokens + msg_tokens <= max_tokens:
            truncated.insert(0, msg)
            total_tokens += msg_tokens
        else:
            break

    return "\n\n".join(truncated)
```

## For Support Engineers: Troubleshooting

### Problem: Token Estimation Inaccurate

**Symptoms**:
- Hitting context limits unexpectedly
- Cost higher than estimated
- "Context length exceeded" errors

**Check**:

1. Verify tokenizer libraries are installed:
   ```bash
   pip show tiktoken anthropic
   ```

2. Check logs for fallback warnings:
   ```
   WARNING: tiktoken not installed - falling back to character-based estimation
   WARNING: anthropic not installed - falling back to character-based estimation
   ```

3. Install missing dependencies:
   ```bash
   pip install tiktoken>=0.5.0 anthropic>=0.25.0
   ```

### Problem: High Token Usage

**Diagnosis**:

```python
# Check token breakdown in logs
logger.info(f"Token breakdown: {token_breakdown}")

# Look for:
# - Large conversation_history tokens → implement truncation
# - Large system_prompt tokens → verify tiered prompts are enabled
# - Pattern templates being added → check if needed for simple queries
```

**Solutions**:

1. **Reduce conversation history**:
   ```bash
   # In .env
   MAX_CONVERSATION_TOKENS=2000  # Reduce from 4000
   ```

2. **Enable tiered prompts** (if disabled):
   ```bash
   ENABLE_TIERED_PROMPTS=true
   ```

3. **Disable pattern templates for simple queries** (already implemented):
   - Pattern templates skip ANSWER response type automatically

### Problem: Hitting Context Limits

**Error**: `openai.error.InvalidRequestError: This model's maximum context length is 128000 tokens`

**Fix**:

1. Check total token usage:
   ```python
   total_tokens = estimate_prompt_tokens(...)["total"]
   logger.error(f"Total tokens: {total_tokens} exceeds model limit")
   ```

2. Implement aggressive truncation:
   ```python
   # Reserve tokens for response
   max_prompt_tokens = model_context_limit - expected_response_tokens

   # Truncate conversation history
   if total_tokens > max_prompt_tokens:
       history = truncate_conversation_history(
           history,
           max_tokens=max_prompt_tokens - system_tokens - user_tokens
       )
   ```

## Performance Impact

### Tokenizer Performance

Token estimation is **fast** due to LRU caching:

```python
# First call: Initialize encoder (~50ms)
tokens = estimate_tokens(text, provider="openai")

# Subsequent calls: Use cached encoder (~1ms)
tokens = estimate_tokens(text2, provider="openai")
```

**Cache size**: 10 encoders (sufficient for typical usage)

### Best Practices

1. **Estimate once per prompt assembly**:
   ```python
   # Good: Estimate after assembly
   prompt = assemble_intelligent_prompt(...)
   tokens = estimate_tokens(prompt, provider)

   # Bad: Estimate multiple times
   tokens = estimate_tokens(part1) + estimate_tokens(part2) + ...
   ```

2. **Use appropriate provider**:
   ```python
   # Good: Match actual provider
   settings = get_settings()
   tokens = estimate_tokens(text, provider=settings.llm.provider)

   # Bad: Hardcoded provider
   tokens = estimate_tokens(text, provider="openai")  # But using Anthropic!
   ```

3. **Cache results when possible**:
   ```python
   # For static content
   SYSTEM_PROMPT_TOKENS = estimate_tokens(SYSTEM_PROMPT, provider)

   # Reuse
   total_tokens = SYSTEM_PROMPT_TOKENS + user_tokens + history_tokens
   ```

## Token Estimation Accuracy

### Comparison: Character-Based vs Tokenizer

Test string: `"Kubernetes pod crashed with OOMKilled error in production"`

| Method | Tokens | Accuracy |
|--------|--------|----------|
| Character-based (`len(text)//4`) | 13 | ±20% error |
| tiktoken (OpenAI) | 11 | Exact |
| Anthropic tokenizer | 12 | Exact |

**Recommendation**: Always use provider-specific tokenizers for production.

### Fallback Behavior

When tokenizer not available:

```python
# Fallback uses: max(1, len(text) // 4)
# This is conservative (slightly overestimates)
# Acceptable for development, not recommended for production
```

## Related Configuration

### Environment Variables

```bash
# LLM Provider (determines which tokenizer to use)
CHAT_PROVIDER=fireworks        # openai, anthropic, fireworks, local, cohere

# Model Selection (affects tokenization)
FIREWORKS_MODEL=accounts/fireworks/models/llama-v3p1-405b-instruct
OPENAI_MODEL=gpt-4o
ANTHROPIC_MODEL=claude-3-sonnet-20240229

# Token Budgets
MAX_CONVERSATION_TOKENS=4000
CONTEXT_TOKEN_BUDGET=4000
SYSTEM_PROMPT_MAX_TOKENS=500
PATTERN_TEMPLATE_MAX_TOKENS=300

# Feature Flags
ENABLE_TIERED_PROMPTS=true     # Use minimal prompts for simple queries
ENABLE_PATTERN_TEMPLATES=true  # Use compact pattern templates
```

### Code Configuration

```python
from faultmaven.config.settings import get_settings

settings = get_settings()

# Access token budgets
max_tokens = settings.thresholds.max_conversation_tokens
context_budget = settings.thresholds.context_token_budget

# Access LLM configuration
provider = settings.llm.provider
model = settings.llm.get_model()
```

## API Reference

### `estimate_tokens(text, provider, model)`

Estimate token count for text using provider-specific tokenizer.

**Parameters**:
- `text` (str): Input text to tokenize
- `provider` (str): LLM provider ("openai", "anthropic", "fireworks", "local")
- `model` (Optional[str]): Specific model for accurate encoding

**Returns**: `int` - Number of tokens

### `estimate_prompt_tokens(system_prompt, user_message, conversation_history, provider, model)`

Estimate tokens for complete prompt assembly with breakdown.

**Parameters**:
- `system_prompt` (str): System instructions
- `user_message` (str): Current user query
- `conversation_history` (str): Previous conversation
- `provider` (str): LLM provider
- `model` (Optional[str]): Specific model

**Returns**: `Dict[str, int]` with keys: `system`, `user`, `history`, `total`

## Related Documentation

- [Context Management](./CONTEXT_MANAGEMENT.md)
- [Configuration Reference](./CONFIGURATION.md)
- [Prompt Engineering](../architecture/QUERY_CLASSIFICATION_AND_PROMPT_ENGINEERING.md)
