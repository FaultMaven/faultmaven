# Release Notes

## 2025-10-04: Infrastructure Improvements

**Type**: Internal Infrastructure Enhancements
**Impact**: Developers, Admins, Support Engineers

### What Changed

**5 major infrastructure improvements** that make FaultMaven more robust, maintainable, and cost-effective:

1. ✅ **Typed Context System** - QueryContext replaces loose dictionaries
2. ✅ **Accurate Token Estimation** - Provider-specific tokenizers for cost optimization
3. ✅ **Centralized Configuration** - ConversationThresholds with environment variables
4. ✅ **Enhanced Validation** - Early error detection in prompt assembly
5. ✅ **Improved Documentation** - Standardized docstrings across all modules

### Documentation

- **[Full Release Notes](./INFRASTRUCTURE_IMPROVEMENTS_2025-10-04.md)** - Complete details and migration guide
- **[Context Management Guide](../development/CONTEXT_MANAGEMENT.md)** - How to use QueryContext
- **[Token Estimation Guide](../development/TOKEN_ESTIMATION.md)** - Accurate token counting
- **[Environment Variables Guide](../development/ENVIRONMENT_VARIABLES.md)** - Configuration reference

### Impact

- Developer Productivity: ⭐⭐⭐⭐⭐ (5/5)
- System Reliability: ⭐⭐⭐⭐ (4/5)
- Operational Efficiency: ⭐⭐⭐⭐⭐ (5/5)
- User-Facing Features: ⭐ (1/5) - Internal only

### Breaking Changes

Since this is pre-release software, backward compatibility was removed for cleaner design:

**Required**: Use `QueryContext` instead of dicts when passing context to classification engine.

```python
# ✅ New way
from faultmaven.models.agentic import QueryContext
context = QueryContext(session_id="abc", case_id="123")

# ❌ Old way (no longer supported)
context = {"session_id": "abc", "case_id": "123"}
```

---

## Future Releases

Release notes for future versions will be added here.
