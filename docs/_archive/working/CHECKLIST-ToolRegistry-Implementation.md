# ToolRegistry Consolidation - Implementation Checklist

**Task**: Complete ToolRegistry implementation to fix 15 failing tests
**Design Doc**: [DESIGN-ToolRegistry-Consolidation.md](./DESIGN-ToolRegistry-Consolidation.md)
**Diagrams**: [DIAGRAM-ToolRegistry-Consolidation.md](./DIAGRAM-ToolRegistry-Consolidation.md)

---

## Pre-Implementation Verification

- [x] Read and understand current implementations
  - [x] `faultmaven/modules/agent/tools/registry.py` - Legacy class-based
  - [x] `faultmaven/modules/agent/tools/base.py` - Complete AgentToolRegistry
  - [x] `tests/unit/tools/test_tool_registry.py` - Test expectations
- [x] Identify root cause: Tests import from `registry.py` but expect `AgentToolRegistry` API
- [x] Design solution: Re-export `AgentToolRegistry` from `registry.py`
- [x] Create architecture documentation

---

## Implementation Steps

### Step 1: Backup Current State (2 minutes)

```bash
# Create backup of current implementation
cp faultmaven/modules/agent/tools/registry.py faultmaven/modules/agent/tools/registry.py.backup

# Verify tests are failing
pytest tests/unit/tools/test_tool_registry.py -v --tb=short
# Expected: 15 failures
```

**Acceptance**: Backup created, baseline test results captured

---

### Step 2: Update registry.py (10 minutes)

Replace the contents of `faultmaven/modules/agent/tools/registry.py` with:

```python
"""
Tool Registry for dynamic tool registration.

This module provides the canonical ToolRegistry implementation for agent tools.
The registry supports instance-based registration with context-aware execution.

Architecture Note:
    The implementation lives in base.py (AgentToolRegistry) and is re-exported
    here for discoverability and backward compatibility.
"""

from typing import Dict, List, Type, Optional
import logging

# Import the complete implementation
from faultmaven.modules.agent.tools.base import AgentToolRegistry as ToolRegistry
from faultmaven.modules.agent.tools.base import tool_registry

# Re-export for backward compatibility and discoverability
__all__ = ['ToolRegistry', 'tool_registry', 'register_tool']


logger = logging.getLogger(__name__)


def register_tool(name: str):
    """
    Decorator for registering tools (legacy class-based pattern).

    Note: This pattern is deprecated in favor of instance-based registration:

        # Preferred approach:
        tool_instance = MyTool(dependencies)
        tool_registry.register(tool_instance)

    Usage:
        @register_tool("knowledge_base")
        class KnowledgeBaseTool(AgentTool):
            ...

    Args:
        name: Tool name for registration

    Returns:
        Class decorator that instantiates and registers the tool
    """
    def decorator(cls):
        try:
            # Instantiate the tool class
            tool_instance = cls()
            # Register with the global registry
            tool_registry.register(tool_instance)
            logger.info(f"Registered tool via decorator: {name}")
        except Exception as e:
            logger.error(f"Failed to register tool '{name}': {e}")
            raise
        return cls
    return decorator
```

**Checklist**:
- [ ] Remove old `ToolRegistry` class definition
- [ ] Import `AgentToolRegistry` from `base.py`
- [ ] Re-export as `ToolRegistry`
- [ ] Import and re-export `tool_registry` global instance
- [ ] Update `register_tool` decorator to instantiate and register
- [ ] Add module docstring explaining architecture
- [ ] Add `__all__` for explicit exports

---

### Step 3: Run Tests (5 minutes)

```bash
# Run registry tests
pytest tests/unit/tools/test_tool_registry.py -v

# Expected result: ALL TESTS PASS (15/15)
```

**Acceptance Criteria**:
- [ ] `test_registry_register_tool` - PASS
- [ ] `test_registry_register_duplicate_raises` - PASS
- [ ] `test_registry_get_tool` - PASS
- [ ] `test_registry_get_nonexistent_tool` - PASS
- [ ] `test_registry_list_tools` - PASS
- [ ] `test_registry_get_all_tools` - PASS
- [ ] `test_registry_get_all_schemas` - PASS
- [ ] `test_registry_get_all_domain_tools` - PASS
- [ ] `test_registry_execute_tool` - PASS
- [ ] `test_registry_execute_nonexistent_tool` - PASS
- [ ] `test_registry_execute_tool_validates_params` - PASS
- [ ] `test_registry_execute_tool_handles_exception` - PASS
- [ ] `test_registry_clear` - PASS
- [ ] `test_global_registry_exists` - PASS
- [ ] `test_global_registry_has_methods` - PASS

---

### Step 4: Regression Testing (10 minutes)

```bash
# Run all tool-related tests
pytest tests/unit/tools/ -v

# Run agent module tests
pytest tests/unit/agent/ -v --tb=short

# Check for any imports of the old API
grep -r "ToolRegistry" faultmaven/ --include="*.py" | grep -v "AgentToolRegistry"
```

**Acceptance Criteria**:
- [ ] No new test failures in `tests/unit/tools/`
- [ ] No new test failures in `tests/unit/agent/`
- [ ] No broken imports in application code
- [ ] Coverage maintained above 71%

---

### Step 5: Code Quality Checks (5 minutes)

```bash
# Run type checking
mypy faultmaven/modules/agent/tools/registry.py

# Run linting
ruff check faultmaven/modules/agent/tools/registry.py

# Format code
ruff format faultmaven/modules/agent/tools/registry.py
```

**Acceptance Criteria**:
- [ ] No mypy errors
- [ ] No ruff linting errors
- [ ] Code properly formatted

---

### Step 6: Documentation Updates (10 minutes)

Update inline documentation:

```bash
# Verify docstrings are complete
python -c "from faultmaven.modules.agent.tools.registry import ToolRegistry; help(ToolRegistry)"
```

**Checklist**:
- [ ] Module docstring explains re-export pattern
- [ ] `register_tool` decorator has deprecation note
- [ ] All public methods documented
- [ ] `__all__` lists public exports

---

### Step 7: Integration Testing (Optional, 5 minutes)

If integration tests exist:

```bash
# Run integration tests
pytest tests/integration/agent/ -v -k tool
```

**Acceptance Criteria**:
- [ ] No integration test failures
- [ ] Agent workflows using tools still work

---

## Post-Implementation Verification

### Test Verification Summary

```bash
# Final comprehensive test run
pytest tests/unit/tools/test_tool_registry.py -v --cov=faultmaven.modules.agent.tools.registry --cov-report=term-missing

# Expected output:
# ============================= 15 passed in X.XXs =============================
# Coverage: 80%+ on registry module
```

**Acceptance Criteria**:
- [ ] 15/15 tests passing
- [ ] Coverage ≥ 80% on registry module
- [ ] No warnings or deprecation errors

---

### Code Review Checklist

Before submitting PR:

**Architecture**:
- [ ] Single source of truth: `AgentToolRegistry` in `base.py`
- [ ] Clean re-export from `registry.py`
- [ ] No duplicate implementations
- [ ] Backward compatibility maintained

**Testing**:
- [ ] All unit tests pass
- [ ] No regression in other modules
- [ ] Coverage maintained/improved
- [ ] Test expectations match implementation

**Code Quality**:
- [ ] No type checking errors
- [ ] No linting errors
- [ ] Proper documentation
- [ ] Clear deprecation notices

**Security**:
- [ ] No secrets in code
- [ ] Proper error handling
- [ ] Input validation preserved
- [ ] No new security vulnerabilities

---

## Rollback Plan

If issues are discovered:

### Quick Rollback (< 2 minutes)

```bash
# Restore backup
mv faultmaven/modules/agent/tools/registry.py.backup faultmaven/modules/agent/tools/registry.py

# Verify rollback
pytest tests/unit/tools/test_tool_registry.py -v
```

### Git Rollback (< 5 minutes)

```bash
# Revert commit
git log --oneline -n 5  # Find commit hash
git revert <commit-hash>

# Run tests
pytest tests/unit/tools/test_tool_registry.py -v
```

**Rollback Triggers**:
- More than 5 test failures after implementation
- Type checking errors in dependent code
- Production errors in staging environment
- Performance degradation > 10%

---

## Success Criteria

Implementation is successful when:

1. **Tests Pass**: All 15 registry tests pass
2. **No Regressions**: No new failures in other test suites
3. **Coverage Maintained**: Overall coverage ≥ 71%, registry module ≥ 80%
4. **Code Quality**: No type/lint errors
5. **Documentation**: Clear module docs and deprecation notices
6. **Backward Compatibility**: Existing code continues to work

---

## Time Estimate

| Step | Duration | Notes |
|------|----------|-------|
| Backup & Baseline | 2 min | Save current state |
| Update registry.py | 10 min | Replace with re-export |
| Run Tests | 5 min | Verify fixes |
| Regression Testing | 10 min | Check for side effects |
| Code Quality | 5 min | Type check, lint, format |
| Documentation | 10 min | Update docstrings |
| Integration Testing | 5 min | Optional if available |
| **Total** | **47 min** | **< 1 hour** |

---

## Next Steps After Implementation

1. **Create PR**:
   ```bash
   git checkout -b fix/consolidate-tool-registry
   git add faultmaven/modules/agent/tools/registry.py
   git commit -m "fix: Consolidate ToolRegistry implementations

   - Re-export AgentToolRegistry from registry.py
   - Fix 15 failing tests in test_tool_registry.py
   - Maintain backward compatibility
   - Add deprecation notice for class-based registration

   Refs: DESIGN-ToolRegistry-Consolidation.md"

   git push origin fix/consolidate-tool-registry
   ```

2. **Request Reviews**:
   - Tech Lead: Architecture review
   - Test Engineer: Test coverage review
   - Team Member: Code review

3. **Monitor After Merge**:
   - CI/CD pipeline success
   - No new errors in staging
   - Coverage reports

4. **Plan Phase 2** (Optional):
   - Migration guide for class-based → instance-based
   - Update internal code to use instance registration
   - Deprecation timeline

---

## Reference Commands

### Quick Test Run
```bash
pytest tests/unit/tools/test_tool_registry.py -v
```

### With Coverage
```bash
pytest tests/unit/tools/test_tool_registry.py -v \
  --cov=faultmaven.modules.agent.tools.registry \
  --cov-report=term-missing \
  --cov-report=html
```

### Specific Test
```bash
pytest tests/unit/tools/test_tool_registry.py::TestToolRegistry::test_registry_register_tool -v
```

### Watch Mode
```bash
ptw tests/unit/tools/test_tool_registry.py -- -v
```

---

## Contacts

- **Design**: Solutions Architect
- **Implementation**: TBD
- **Review**: Tech Lead, Test Engineer
- **Approval**: Product Manager (if breaking changes)

---

## Status Tracking

| Milestone | Status | Date | Notes |
|-----------|--------|------|-------|
| Design Complete | ✅ | 2026-01-08 | Architecture documented |
| Implementation | ⏳ | TBD | Awaiting assignment |
| Tests Passing | ⏳ | TBD | Target: 15/15 |
| Code Review | ⏳ | TBD | |
| Merged to Main | ⏳ | TBD | |
| Deployed to Prod | ⏳ | TBD | |

**Last Updated**: 2026-01-08
**Status**: Ready for Implementation
