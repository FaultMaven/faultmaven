# TASK-004: Minimal Shim Pattern Foundation

## Task Metadata
- **Phase**: Week 1, Day 4-7 (Foundation)
- **Priority**: P1 (Enables community edition)
- **Estimated Time**: 2-3 hours implementation + tests
- **Dependencies**:
  - TASK-001 (Alembic setup) ✅ Complete
  - TASK-002 (Case Repository) ✅ Complete
  - TASK-003 (Session Management) ✅ Complete
- **Assignee**: Developer (implementation + tests)
- **Test Reviewer**: Test-Engineer (TASK-004-TEST-REVIEW)
- **Architect Reviewer**: Solutions Architect (final approval)

## Objective

Implement a minimal shim pattern foundation to enable graceful degradation of enterprise dependencies. This allows FaultMaven to run with zero external dependencies in community mode while maintaining full functionality in enterprise mode.

## Context

The evolution strategy calls for FaultMaven to support both:
- **Community Edition**: Runs with SQLite, local files, no heavy dependencies
- **Enterprise Edition**: Full observability (Opik), PII redaction (Presidio), metrics (Prometheus)

We need a **shim pattern** that:
1. Detects if enterprise libraries are installed
2. Uses them if available and enabled via environment variable
3. Falls back to no-op implementations if unavailable
4. Maintains identical API so application code doesn't change

**Phase 1 (This Task):** Minimal shims for Opik (observability) and Presidio (PII redaction)
**Phase 2 (Weeks 11-14):** Complete shims for all enterprise dependencies

## Acceptance Criteria

### Functional Requirements
- [ ] Shim pattern implemented for Opik (observability/tracing)
- [ ] Shim pattern implemented for Presidio (PII redaction)
- [ ] Application code works with libraries installed
- [ ] Application code works with libraries NOT installed
- [ ] Environment variables control feature activation
- [ ] Zero crashes when dependencies missing

### Technical Requirements
- [ ] `faultmaven/infrastructure/shims/__init__.py` created
- [ ] `faultmaven/infrastructure/shims/observability.py` created
- [ ] `faultmaven/infrastructure/shims/security.py` created
- [ ] Shims export same API as original libraries
- [ ] Feature detection via try/except ImportError
- [ ] Environment variable checks (ENABLE_TRACING, ENABLE_PII_REDACTION)
- [ ] Type hints on all shim functions

### Testing Requirements (Developer Must Implement)
- [ ] Unit tests with libraries installed (80%+ coverage) - **DEVELOPER WRITES THESE**
- [ ] Unit tests with libraries NOT installed - **DEVELOPER WRITES THESE**
- [ ] Test environment variable toggling
- [ ] Test no-op behavior when disabled
- [ ] All tests pass locally before PR submission
- [ ] All tests pass in CI/CD
- [ ] Test code reviewed by test-engineer (TASK-004-TEST-REVIEW)

## Implementation Steps

### Step 1: Create Shim Package Structure

**Directory:** `faultmaven/infrastructure/shims/`

```bash
mkdir -p faultmaven/infrastructure/shims
touch faultmaven/infrastructure/shims/__init__.py
touch faultmaven/infrastructure/shims/observability.py
touch faultmaven/infrastructure/shims/security.py
```

### Step 2: Implement Observability Shim (Opik)

**File:** `faultmaven/infrastructure/shims/observability.py`

```python
"""Observability shim for distributed tracing.

Provides graceful degradation for Opik tracing:
- If Opik installed + ENABLE_TRACING=true: Use Opik
- Otherwise: No-op decorator (do nothing)

Usage:
    from faultmaven.infrastructure.shims.observability import track

    @track("function_name")
    async def my_function():
        # Function code here
        pass
"""

import os
import logging
from typing import Callable, Any
from functools import wraps

logger = logging.getLogger(__name__)

# Feature detection
try:
    from opik import track as opik_track
    OPIK_AVAILABLE = True
    logger.info("Opik tracing library available")
except ImportError:
    OPIK_AVAILABLE = False
    logger.debug("Opik not installed - tracing disabled")


def track(name: str) -> Callable:
    """
    Decorator for distributed tracing.

    If Opik is available and ENABLE_TRACING=true, uses Opik tracking.
    Otherwise, returns a no-op decorator that does nothing.

    Args:
        name: Name of the operation to track

    Returns:
        Decorator function (Opik or no-op)

    Example:
        @track("case_creation")
        async def create_case(data):
            # Your code here
            pass
    """
    # Check if tracing enabled via environment variable
    tracing_enabled = os.getenv("ENABLE_TRACING", "false").lower() == "true"

    if OPIK_AVAILABLE and tracing_enabled:
        # Use real Opik tracking
        logger.debug(f"Tracking enabled for: {name}")
        return opik_track(name)
    else:
        # Return no-op decorator
        def no_op_decorator(func: Callable) -> Callable:
            """No-op decorator that does nothing."""
            @wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                return await func(*args, **kwargs)
            return wrapper

        logger.debug(f"Tracking disabled for: {name} (OPIK_AVAILABLE={OPIK_AVAILABLE}, ENABLE_TRACING={tracing_enabled})")
        return no_op_decorator


def get_tracing_status() -> dict:
    """
    Get current tracing status for diagnostics.

    Returns:
        Dictionary with tracing configuration status
    """
    tracing_enabled = os.getenv("ENABLE_TRACING", "false").lower() == "true"
    return {
        "opik_available": OPIK_AVAILABLE,
        "tracing_enabled": tracing_enabled,
        "active": OPIK_AVAILABLE and tracing_enabled,
    }
```

### Step 3: Implement PII Redaction Shim (Presidio)

**File:** `faultmaven/infrastructure/shims/security.py`

```python
"""Security shim for PII redaction.

Provides graceful degradation for Presidio PII redaction:
- If Presidio installed + ENABLE_PII_REDACTION=true: Use Presidio
- Otherwise: Pass-through (return text unchanged)

Usage:
    from faultmaven.infrastructure.shims.security import PIIRedactor

    redactor = PIIRedactor()
    safe_text = redactor.redact("My SSN is 123-45-6789")
"""

import os
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

# Feature detection
try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
    PRESIDIO_AVAILABLE = True
    logger.info("Presidio PII redaction library available")
except ImportError:
    PRESIDIO_AVAILABLE = False
    logger.debug("Presidio not installed - PII redaction disabled")


class PIIRedactor:
    """
    PII redaction with graceful degradation.

    If Presidio is available and ENABLE_PII_REDACTION=true, performs PII redaction.
    Otherwise, passes text through unchanged.
    """

    def __init__(self):
        """Initialize PII redactor."""
        self.pii_enabled = os.getenv("ENABLE_PII_REDACTION", "false").lower() == "true"

        if PRESIDIO_AVAILABLE and self.pii_enabled:
            try:
                self.analyzer = AnalyzerEngine()
                self.anonymizer = AnonymizerEngine()
                self.active = True
                logger.info("PII redaction active (Presidio)")
            except Exception as e:
                logger.warning(f"Failed to initialize Presidio: {e}")
                self.analyzer = None
                self.anonymizer = None
                self.active = False
        else:
            self.analyzer = None
            self.anonymizer = None
            self.active = False
            logger.debug(f"PII redaction disabled (PRESIDIO_AVAILABLE={PRESIDIO_AVAILABLE}, ENABLE_PII_REDACTION={self.pii_enabled})")

    def redact(
        self,
        text: str,
        entities: Optional[List[str]] = None,
        language: str = "en"
    ) -> str:
        """
        Redact PII from text.

        Args:
            text: Text to redact PII from
            entities: Optional list of entity types to redact (e.g., ["EMAIL", "PHONE"])
            language: Language code (default: "en")

        Returns:
            Redacted text if PII redaction active, original text otherwise
        """
        if not self.active or not text:
            return text

        try:
            # Analyze text for PII
            analyzer_results = self.analyzer.analyze(
                text=text,
                entities=entities,
                language=language
            )

            # Anonymize detected PII
            anonymized_result = self.anonymizer.anonymize(
                text=text,
                analyzer_results=analyzer_results
            )

            logger.debug(f"PII redacted: {len(analyzer_results)} entities found")
            return anonymized_result.text

        except Exception as e:
            logger.error(f"PII redaction failed: {e}")
            # On error, return original text (fail-open)
            return text

    def get_status(self) -> dict:
        """
        Get current PII redaction status for diagnostics.

        Returns:
            Dictionary with PII redaction configuration status
        """
        return {
            "presidio_available": PRESIDIO_AVAILABLE,
            "pii_redaction_enabled": self.pii_enabled,
            "active": self.active,
        }
```

### Step 4: Export Shims from Package

**File:** `faultmaven/infrastructure/shims/__init__.py`

```python
"""Infrastructure shims for graceful degradation.

This package provides shim layers for optional enterprise dependencies:
- Observability (Opik): Distributed tracing
- Security (Presidio): PII redaction

Shims enable FaultMaven to run in both:
- Community mode: Zero external dependencies
- Enterprise mode: Full observability and security features

Usage:
    from faultmaven.infrastructure.shims import track, PIIRedactor

    # Tracing (no-op if Opik not installed or disabled)
    @track("my_operation")
    async def my_function():
        pass

    # PII redaction (pass-through if Presidio not installed or disabled)
    redactor = PIIRedactor()
    safe_text = redactor.redact(user_input)
"""

from .observability import track, get_tracing_status
from .security import PIIRedactor

__all__ = [
    "track",
    "get_tracing_status",
    "PIIRedactor",
]
```

### Step 5: Update Example Service to Use Shims

**File:** `faultmaven/services/case_service.py` (example usage)

```python
# Before (direct Opik import - fails if not installed):
# from opik import track

# After (shim - works with or without Opik):
from faultmaven.infrastructure.shims import track, PIIRedactor

class CaseService:
    def __init__(self, case_repository, pii_redactor: PIIRedactor = None):
        self.cases = case_repository
        self.pii_redactor = pii_redactor or PIIRedactor()

    @track("case_creation")
    async def create_case(self, title: str, description: str, user_id: str):
        """Create a new case with PII redaction and tracing."""
        # Redact PII from description
        safe_description = self.pii_redactor.redact(description)

        # Create case (tracing happens automatically via @track)
        case = await self.cases.create_case(
            title=title,
            description=safe_description,
            user_id=user_id
        )
        return case
```

### Step 6: Write Unit Tests

**File:** `tests/unit/infrastructure/shims/test_observability.py`

Required tests:
```python
import pytest
import os

@pytest.mark.unit
def test_track_decorator_with_opik_installed():
    """Test track decorator when Opik is installed and enabled"""

@pytest.mark.unit
def test_track_decorator_without_opik():
    """Test track decorator when Opik is NOT installed (no-op)"""

@pytest.mark.unit
def test_track_decorator_opik_disabled():
    """Test track decorator when ENABLE_TRACING=false"""

@pytest.mark.unit
def test_track_decorator_opik_enabled():
    """Test track decorator when ENABLE_TRACING=true"""

@pytest.mark.unit
async def test_track_decorator_preserves_function_behavior():
    """Test decorated function still works correctly"""

@pytest.mark.unit
def test_get_tracing_status():
    """Test tracing status reporting"""
```

**File:** `tests/unit/infrastructure/shims/test_security.py`

Required tests:
```python
@pytest.mark.unit
def test_pii_redactor_with_presidio_installed():
    """Test PIIRedactor when Presidio is installed and enabled"""

@pytest.mark.unit
def test_pii_redactor_without_presidio():
    """Test PIIRedactor when Presidio is NOT installed (pass-through)"""

@pytest.mark.unit
def test_pii_redactor_disabled():
    """Test PIIRedactor when ENABLE_PII_REDACTION=false"""

@pytest.mark.unit
def test_pii_redactor_enabled():
    """Test PIIRedactor when ENABLE_PII_REDACTION=true"""

@pytest.mark.unit
def test_pii_redactor_redacts_email():
    """Test email redaction when active"""

@pytest.mark.unit
def test_pii_redactor_redacts_phone():
    """Test phone number redaction when active"""

@pytest.mark.unit
def test_pii_redactor_error_handling():
    """Test PIIRedactor fails open (returns original text on error)"""

@pytest.mark.unit
def test_pii_redactor_get_status():
    """Test PII redaction status reporting"""
```

### Step 7: Write Integration Tests

**File:** `tests/integration/test_shims_integration.py`

```python
@pytest.mark.integration
async def test_case_service_with_shims_enabled():
    """Test CaseService with tracing and PII redaction enabled"""

@pytest.mark.integration
async def test_case_service_with_shims_disabled():
    """Test CaseService works when shims disabled"""

@pytest.mark.integration
def test_environment_variable_toggling():
    """Test shims respond to environment variable changes"""
```

## Files to Create/Modify

### Create
- `faultmaven/infrastructure/shims/__init__.py` (Package exports)
- `faultmaven/infrastructure/shims/observability.py` (Opik shim)
- `faultmaven/infrastructure/shims/security.py` (Presidio shim)
- `tests/unit/infrastructure/shims/test_observability.py` (Unit tests)
- `tests/unit/infrastructure/shims/test_security.py` (Unit tests)
- `tests/integration/test_shims_integration.py` (Integration tests)

### Modify
- `faultmaven/services/case_service.py` (Example usage - optional)
- `.env.example` (Add ENABLE_TRACING, ENABLE_PII_REDACTION)
- `pyproject.toml` or `requirements.txt` (Mark opik and presidio as optional)

## Testing Requirements

### Coverage Target
- **Minimum:** 80% coverage for shim code
- **Target:** 90% coverage for shim logic
- **Critical paths:** 100% coverage (feature detection, no-op fallbacks)

### Test Execution
```bash
# Run all tests
pytest

# Run shim unit tests only
pytest tests/unit/infrastructure/shims/ -v

# Run integration tests
pytest tests/integration/test_shims_integration.py -v

# Run with coverage
pytest --cov=faultmaven/infrastructure/shims --cov-report=term-missing

# Verify coverage meets target
pytest --cov=faultmaven/infrastructure/shims --cov-fail-under=80
```

### Test Scenarios
- **With dependencies installed:** Verify shims use real libraries
- **Without dependencies:** Verify shims fall back to no-op
- **Environment variables:** Test ENABLE_* toggles
- **Error handling:** Verify fail-open behavior (don't crash)

## Success Metrics

### Definition of Done
- [ ] Observability shim fully implemented
- [ ] Security (PII) shim fully implemented
- [ ] All unit tests pass (80%+ coverage)
- [ ] All integration tests pass
- [ ] Application runs with dependencies missing
- [ ] Application runs with dependencies installed
- [ ] CI/CD pipeline passes
- [ ] Code reviewed and approved
- [ ] Documentation updated
- [ ] PR merged to main

### Functionality
- Application starts: < 2 seconds (no dependency overhead)
- Shim overhead: < 1ms (negligible performance impact)
- Zero crashes when dependencies missing

### Quality
- Zero flaky tests
- Clean error messages when features disabled
- Logging indicates shim status clearly

## Environment Variables

Add to `.env.example`:
```bash
# Observability Configuration
ENABLE_TRACING=false  # Set to 'true' to enable Opik distributed tracing

# Security Configuration
ENABLE_PII_REDACTION=false  # Set to 'true' to enable Presidio PII redaction
```

## PR Template

**Title:** `[TASK-004] Implement Minimal Shim Pattern Foundation`

**Description:**
This PR implements the minimal shim pattern for graceful degradation of enterprise dependencies (Opik, Presidio). Enables FaultMaven to run in both community mode (zero dependencies) and enterprise mode (full features).

**Changes:**
- Created shim package: `faultmaven/infrastructure/shims/`
- Implemented observability shim (Opik tracing)
- Implemented security shim (Presidio PII redaction)
- Added feature detection via try/except ImportError
- Added environment variable toggles (ENABLE_TRACING, ENABLE_PII_REDACTION)
- Added 14 unit tests (90% coverage)
- Added 3 integration tests

**Testing:**
- [x] All unit tests pass (14/14) - **DEVELOPER MUST WRITE**
- [x] All integration tests pass (3/3) - **DEVELOPER MUST WRITE**
- [x] Coverage: 90% for shim layer
- [x] Tested with dependencies installed
- [x] Tested with dependencies NOT installed
- [x] CI/CD pipeline passes
- [x] Manual testing: community mode verified
- [x] Test-engineer reviewed tests (TASK-004-TEST-REVIEW)

**Checklist:**
- [ ] Shims implement graceful degradation
- [ ] No-op fallbacks work correctly
- [ ] Environment variables control features
- [ ] **Unit tests written by developer (80%+ coverage)**
- [ ] **Integration tests written by developer**
- [ ] Application runs without dependencies
- [ ] Application runs with dependencies
- [ ] Error handling tested
- [ ] Logging indicates shim status
- [ ] **Test-engineer approved test quality (TASK-004-TEST-REVIEW)**
- [ ] Solutions architect approved implementation

## Risks & Mitigation

### Risk 1: Import Errors Not Caught
**Mitigation:** Use try/except ImportError at module level. Test with dependencies removed.

### Risk 2: Feature Detection Fails
**Mitigation:** Log shim status clearly. Provide `get_status()` methods for diagnostics.

### Risk 3: Performance Overhead
**Mitigation:** No-op decorators should be lightweight (just return original function).

### Risk 4: Shim API Mismatch
**Mitigation:** Match original library API exactly. Document any deviations.

## Next Steps After Completion

1. **TASK-005:** Performance Baseline Suite (Week 1, Day 8-10)
   - Establish performance benchmarks
   - Monitor for regressions

2. **Phase 2 Shim Expansion (Weeks 11-14):**
   - Complete shims for all enterprise dependencies
   - Prometheus metrics shim
   - Redis cache shim
   - S3 storage shim

## Questions?

Before starting:
- Understand try/except ImportError pattern
- Know how to test with dependencies missing (virtual environments)
- Understand decorator patterns in Python
- Review Opik and Presidio APIs (optional dependencies)

Ask solutions-architect if unclear.

---

**Ready to start?** Review this task, implement the shim pattern, write comprehensive tests, and submit PR when all tests pass.
