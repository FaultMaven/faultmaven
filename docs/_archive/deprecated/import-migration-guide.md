# Import Migration Guide

## Overview

This guide documents the migration from scattered imports to centralized configuration and dependency injection.

## Key Changes

### 1. Configuration Management

**Before:**
```python
import os
api_key = os.getenv("OPENAI_API_KEY")
```

**After:**
```python
from faultmaven.config.settings import get_settings
settings = get_settings()
api_key = settings.llm.openai_api_key
```

### 2. Service Dependencies

**Before:**
```python
from faultmaven.services.llm_service import LLMService
llm = LLMService()
```

**After:**
```python
from faultmaven.container import get_container
container = get_container()
llm = container.get_llm_provider()
```

### 3. Database Access

**Before:**
```python
from faultmaven.db import get_db
db = get_db()
```

**After:**
```python
from faultmaven.container import get_container
container = get_container()
db = container.get_service("database")
```

## Migration Steps

1. **Replace direct environment variable access** with settings
2. **Replace direct service instantiation** with container access
3. **Update import paths** to new module structure
4. **Add dependency injection** to functions/classes
5. **Update tests** to use mocked container

## Benefits

- **Centralized configuration**: Single source of truth
- **Testability**: Easy mocking and dependency injection
- **Type safety**: Proper type hints throughout
- **Maintainability**: Clear dependency graph
- **Observability**: Built-in health checks

## Rollback Plan

If issues arise, the system maintains backward compatibility through the `config_bridge`:

```python
from faultmaven.config.settings import config_bridge
value = config_bridge.get("path.to.setting", default="fallback")
```

## Testing

All migrated code should include:
- Unit tests with mocked dependencies
- Integration tests with test container
- Configuration validation tests

## Support

For questions or issues, see:
- `docs/architecture/current-architecture.md`
- `tests/unit/architecture/` for examples
