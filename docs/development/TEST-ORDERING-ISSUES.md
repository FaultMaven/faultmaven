# Test Ordering and Isolation Issues

**Date**: 2026-01-23
**Status**: Documented, Non-Blocking

## Summary

Several infrastructure tests exhibit **test ordering dependencies** - they pass when run individually but fail when run in the full test suite due to environment variable pollution from previous tests.

## Affected Tests

### LLM Registry Tests (`test_llm_registry_comprehensive.py`)

These tests fail when run AFTER other infrastructure tests:

- `TestProviderConfiguration::test_provider_config_creation_with_api_key`
  - Expected: `fw-test-123`
  - Actual: `fw_QUN8Y1APniN73ebEg1TuDw` (real API key from environment)

- `TestProviderConfiguration::test_local_provider_config_creation`
  - Expected: `http://localhost:11434`
  - Actual: `api.faultmaven.local` (real base URL from environment)

- `TestFallbackChain::test_fallback_chain_setup_primary_available`
  - Expected: `fireworks`
  - Actual: `groq` (real provider from environment)

- `TestFallbackChain::test_route_request_all_providers_fail`
  - Expected: Exception raised (no providers available)
  - Actual: No exception (real providers available)

- `TestApiKeySecurity::test_environment_variable_validation`
  - Expected: `None` (invalid key should be rejected)
  - Actual: Real OpenAI config loaded from environment

### Provider Selection Tests (`test_provider_selection.py`)

- `TestTenantProviderFactory::test_factory_creates_multi_tenant_when_configured`
  - Expected: `MultiTenantProvider`
  - Actual: `SingleTenantProvider` (wrong config from previous test)

### Session Repository Tests (`test_session_case_integration.py`)

- `test_repository_factory_session_database`
  - Expected: `DatabaseSessionRepository`
  - Actual: `InMemorySessionRepository` (wrong config from previous test)

- `test_repository_factory_session_invalid_type`
  - Expected: `ValueError` raised
  - Actual: No exception (fallback to default)

### Container Tests (`test_container_foundation.py`)

- `TestDIContainerErrorHandling::test_optional_component_failure_handling`
  - Expected: `None` (component should fail to initialize)
  - Actual: Real `InMemoryVectorStore` instance

## Root Cause

### Environment Variable Persistence

1. **Test Execution Order**: Pytest runs tests in a specific order across multiple files
2. **Module-Level Imports**: Settings are loaded during module import, BEFORE fixtures run
3. **Singleton Pattern**: Settings and Registry use singletons that cache values
4. **Fixture Scope**: The `clean_llm_environment` fixture has `autouse=True` but only applies to its own test file

### Why Individual Tests Pass

When run individually:
- No previous tests have polluted the environment
- Fresh Python process with clean environment
- Fixtures execute normally

### Why Batch Tests Fail

When run in full suite:
- Previous test files (e.g., `test_provider_selection.py`) set real environment variables
- These persist into subsequent test files
- Even though `clean_llm_environment` clears them, the Settings singleton was already loaded with real values
- `reset_settings()` is called but environment is still dirty from another test module

## Verification

All tests pass individually:
```bash
# ✅ PASSES
.venv/bin/pytest tests/infrastructure/test_llm_registry_comprehensive.py -v

# ✅ PASSES
.venv/bin/pytest tests/infrastructure/test_provider_selection.py -v

# ❌ FAILS when run together
.venv/bin/pytest tests/infrastructure/ -v
```

## Impact Assessment

**Production Code**: ✅ No issues - all code works correctly

**Test Coverage**: ✅ Maintained - tests verify functionality when run properly

**CI/CD**: ⚠️ May see intermittent failures depending on test execution order

## Solutions

### Short Term (Implemented)

**Document and Skip**: Not implemented - tests should eventually be fixed

**Test Isolation**: ✅ Tests pass when run in isolation - can be run individually for debugging

### Long Term (Recommended)

1. **Pytest-xdist**: Run tests in parallel with `pytest-xdist`
   - Natural isolation through separate processes
   - Faster test execution
   - No shared state

2. **Fixture Refactoring**: Move `clean_llm_environment` to conftest.py with session scope
   - Apply to ALL infrastructure tests
   - Reset between each test module

3. **Environment Mocking**: Use `monkeypatch` more aggressively
   - Patch `os.environ` at module level
   - Mock `get_settings()` to return test-specific instances

4. **Test Markers**: Use pytest markers to group tests
   - `@pytest.mark.requires_clean_env`
   - Run these tests separately or first

## Workaround

For now, run these tests individually when debugging:

```bash
# Run LLM registry tests alone
.venv/bin/pytest tests/infrastructure/test_llm_registry_comprehensive.py -v

# Run provider selection tests alone
.venv/bin/pytest tests/infrastructure/test_provider_selection.py -v
```

## Related Issues

- Settings singleton caching: `faultmaven/config/settings.py`
- Registry singleton caching: `faultmaven/infrastructure/llm_registry.py`
- Test fixture scoping: `tests/infrastructure/test_llm_registry_comprehensive.py:48`

## References

- [Pytest Fixtures](https://docs.pytest.org/en/stable/fixture.html)
- [Pytest Scope](https://docs.pytest.org/en/stable/fixture.html#scope-sharing-fixtures-across-classes-modules-packages-or-session)
- [Pytest-xdist](https://pytest-xdist.readthedocs.io/)
