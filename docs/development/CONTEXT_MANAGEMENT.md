# Context Management for Developers

## Overview

FaultMaven uses a **typed context system** (`QueryContext`) to pass conversation state, session information, and optimization flags between components. This replaces the previous loose dictionary-based approach with a strongly-typed Pydantic model.

## For Developers: Using QueryContext

### Basic Usage

```python
from faultmaven.models.agentic import QueryContext

# Create context with explicit fields
context = QueryContext(
    session_id="abc-123",
    case_id="case-456",
    conversation_history="User: What's wrong?\nAssistant: Let me help...",
    same_provider_for_response=True
)

# Pass to classification
classification = await classification_engine.classify_query(
    query="Why is my pod crashing?",
    context=context
)
```

### Required vs Optional Fields

| Field | Required | Type | Purpose |
|-------|----------|------|---------|
| `session_id` | **YES** (for classification) | `str` | Identifies the user session |
| `case_id` | No | `Optional[str]` | Links query to specific case |
| `conversation_history` | No | `str` | Previous conversation for context-aware classification |
| `same_provider_for_response` | No | `bool` | Optimization flag to skip redundant LLM calls |
| `user_metadata` | No | `Dict[str, Any]` | Additional user-specific data |

### Validation Methods

```python
# Check if context is valid for classification
if context.validate_for_classification():
    # Has required session_id
    await classify_query(query, context)

# Check if conversation history exists
if context.has_conversation_context():
    # Conversation history is non-empty
    logger.info("Using conversation context for classification")
```

### Common Patterns

#### 1. Creating Context from API Request

```python
async def submit_case_query(
    case_id: str,
    query_request: QueryRequest,
    session_id: str,
    conversation_history: str = ""
):
    # Build QueryContext
    context = QueryContext(
        session_id=session_id,
        case_id=case_id,
        conversation_history=conversation_history,
        same_provider_for_response=check_same_provider()
    )

    # Use in classification
    classification = await classification_engine.classify_query(
        query=query_request.query,
        context=context
    )
```

#### 2. Empty Context (Defaults)

```python
# If no context available, create empty QueryContext
context = QueryContext()  # All fields use defaults

# This is better than passing None because it ensures type safety
# and provides default values
```

#### 3. Adding Custom Metadata

```python
context = QueryContext(
    session_id="abc-123",
    user_metadata={
        "user_expertise": "advanced",
        "preferred_domain": "kubernetes",
        "previous_issues": ["oom-killed", "crashloop"]
    }
)
```

## For Admins: Configuration

### Conversation Thresholds

All conversation-related thresholds are now centralized in `config/settings.py` under `ConversationThresholds`:

```python
# In .env file
MAX_CLARIFICATIONS=3                    # Max clarification requests before escalation
MAX_CONVERSATION_TURNS=20               # Max conversation turns to track
MAX_CONVERSATION_TOKENS=4000            # Max tokens for conversation history

# Token budgets for prompt assembly
CONTEXT_TOKEN_BUDGET=4000               # Budget for entire context
SYSTEM_PROMPT_MAX_TOKENS=500            # Max tokens for system prompt
PATTERN_TEMPLATE_MAX_TOKENS=300         # Max tokens for pattern templates

# Classification confidence thresholds
PATTERN_CONFIDENCE_THRESHOLD=0.7        # Trigger LLM when pattern confidence < 0.7
CONFIDENCE_OVERRIDE_THRESHOLD=0.4       # Force clarification when confidence < 0.4
SELF_CORRECTION_MIN_CONFIDENCE=0.4      # Lower bound for self-correction prompt
SELF_CORRECTION_MAX_CONFIDENCE=0.7      # Upper bound for self-correction prompt
```

### How to Adjust Thresholds

1. **More Clarifications**: Increase `MAX_CLARIFICATIONS` if users complain about forced escalation
2. **Reduce LLM Calls**: Increase `PATTERN_CONFIDENCE_THRESHOLD` to rely more on pattern matching
3. **Improve Accuracy**: Decrease `PATTERN_CONFIDENCE_THRESHOLD` to use LLM more often
4. **Context Budget**: Adjust `MAX_CONVERSATION_TOKENS` if hitting context limits

### Accessing in Code

```python
from faultmaven.config.settings import get_settings

settings = get_settings()

# Access thresholds
max_clarifications = settings.thresholds.max_clarifications
confidence_threshold = settings.thresholds.pattern_confidence_threshold

# Use in logic
if clarification_count >= settings.thresholds.max_clarifications:
    return escalate_to_human()
```

## For Support Engineers: Troubleshooting

### Debugging Context Issues

#### Problem: Classification returning UNKNOWN intent

**Check**:
1. Is `session_id` provided?
   ```python
   if not context.validate_for_classification():
       logger.error("Invalid context: missing session_id")
   ```

2. Is conversation history being passed?
   ```python
   if not context.has_conversation_context():
       logger.warning("No conversation history - classification may be inaccurate")
   ```

#### Problem: Same provider optimization not working

**Check**:
1. Verify `same_provider_for_response` flag is set:
   ```python
   logger.info(f"Same provider optimization: {context.same_provider_for_response}")
   ```

2. Check provider configuration:
   ```bash
   # In .env
   CHAT_PROVIDER=fireworks
   CLASSIFIER_PROVIDER=fireworks  # Must match for optimization
   ```

#### Problem: Too many LLM classification calls (cost)

**Solution**: Increase pattern confidence threshold
```bash
# In .env
PATTERN_CONFIDENCE_THRESHOLD=0.8  # Higher = fewer LLM calls
```

**Monitor**:
```python
# Check classification method in logs
logger.info(f"Classification method: {result['classification_method']}")
# Values: pattern_only, pattern_only_same_provider_optimization, llm_enhanced
```

#### Problem: Conversation context not being used

**Check logs**:
```python
# Look for this in classification_engine logs
if context.has_conversation_context():
    logger.debug(f"Using conversation context: {len(context.conversation_history)} chars")
else:
    logger.debug("No conversation context available")
```

### Common Error Messages

| Error | Cause | Fix |
|-------|-------|-----|
| `"Invalid context: missing session_id"` | QueryContext created without session_id | Ensure `session_id` is always provided |
| `"No conversation history - classification may be inaccurate"` | Empty conversation_history | This is a warning - context-dependent queries may be misclassified |
| `"Skipping LLM classification - same provider"` | Optimization in effect | Normal - this saves costs when same provider handles both tasks |

### Monitoring Metrics

Track these in your observability platform:

```python
# Classification method distribution
classification_method: "pattern_only" | "pattern_only_same_provider_optimization" | "llm_enhanced"

# Context validation
context_valid: bool
has_conversation_history: bool

# Token usage
conversation_tokens: int
system_prompt_tokens: int
total_prompt_tokens: int
```

## Breaking Changes from Previous Implementation

### What Changed

1. **No more dict-based context**
   - ❌ Before: `context = {"session_id": "abc", "case_id": "123"}`
   - ✅ Now: `context = QueryContext(session_id="abc", case_id="123")`

2. **Required type for classify_query()**
   - ❌ Before: `context: Optional[Union[Dict, QueryContext]]`
   - ✅ Now: `context: Optional[QueryContext]`

3. **No `to_dict()` method**
   - ❌ Before: `context_dict = context.to_dict()`
   - ✅ Now: Direct access: `context.session_id`, `context.case_id`

### Migration Guide

If you have existing code passing dicts:

```python
# OLD CODE (will break)
context = {
    "session_id": session_id,
    "case_id": case_id,
    "conversation_history": history
}
classification = await engine.classify_query(query, context)

# NEW CODE (correct)
from faultmaven.models.agentic import QueryContext

context = QueryContext(
    session_id=session_id,
    case_id=case_id,
    conversation_history=history
)
classification = await engine.classify_query(query, context)
```

## Best Practices

### 1. Always Validate Before Critical Operations

```python
if not context.validate_for_classification():
    raise ValueError("Cannot classify without valid session_id")
```

### 2. Use Type Hints

```python
def my_function(context: QueryContext) -> QueryClassification:
    # IDE will autocomplete QueryContext fields
    session_id = context.session_id  # Type-safe
```

### 3. Log Context State for Debugging

```python
logger.debug(
    f"Context state: session={context.session_id}, "
    f"case={context.case_id}, "
    f"has_history={context.has_conversation_context()}, "
    f"same_provider={context.same_provider_for_response}"
)
```

### 4. Centralize Context Creation

```python
# Good: Single factory function
def create_query_context(
    session_id: str,
    case_id: Optional[str] = None,
    conversation_history: str = ""
) -> QueryContext:
    return QueryContext(
        session_id=session_id,
        case_id=case_id,
        conversation_history=conversation_history,
        same_provider_for_response=check_same_provider()
    )

# Use throughout codebase
context = create_query_context(session_id, case_id, history)
```

## Performance Considerations

### Token Estimation

Use the new token estimation utility for accurate token counting:

```python
from faultmaven.utils.token_estimation import estimate_tokens, estimate_prompt_tokens

# Estimate tokens for conversation history
tokens = estimate_tokens(
    context.conversation_history,
    provider="fireworks",
    model="llama-v3p1-405b-instruct"
)

# Check against budget
if tokens > settings.thresholds.max_conversation_tokens:
    # Truncate or summarize conversation history
    context.conversation_history = truncate_history(context.conversation_history)
```

### Same-Provider Optimization

When `same_provider_for_response=True`, the system skips LLM classification and lets the response LLM determine intent while generating the answer. This:
- Saves 1 LLM API call per query
- Reduces latency (single round trip)
- Reduces cost (fewer tokens)

**When to enable**:
```python
# Enable when CHAT_PROVIDER == CLASSIFIER_PROVIDER
from faultmaven.config.settings import get_settings

settings = get_settings()
same_provider = (settings.llm.provider == "fireworks")  # Both use same provider

context = QueryContext(
    session_id=session_id,
    same_provider_for_response=same_provider
)
```

## Related Documentation

- [Token Estimation Guide](./TOKEN_ESTIMATION.md)
- [Configuration Reference](./CONFIGURATION.md)
- [Classification System](../architecture/QUERY_CLASSIFICATION_AND_PROMPT_ENGINEERING.md)
