# FaultMaven Refactoring Plan

**Document Type**: Detailed Refactoring Implementation Plan  
**Date**: 2025-01-06  
**Scope**: Complete architectural refactoring from monolithic to service-oriented architecture  
**Primary References**: 
- `refactored-architecture.md` (target architecture)
- `refactoring-lessons-learned.md` (critical insights)

## Executive Summary

This plan provides a safe, incremental refactoring strategy to transform FaultMaven from its current monolithic structure (with architectural violations) into a clean, layered architecture with strict boundaries and dependency injection. Each phase is independently verifiable and maintains backward compatibility.

## Current State Analysis

### Identified Violations
1. **API Layer Violations**: Routes directly import from Core, Infrastructure, and Tools
   - Example: `agent.py` imports `FaultMavenAgent`, `KnowledgeBaseTool`, `LLMRouter`
2. **Missing Service Layer Logic**: Business logic exists in API routes
3. **Circular Dependencies**: Core depends on concrete tools, tools depend on Core
4. **Missing Interfaces**: No interface definitions for dependency inversion
5. **Direct Instantiation**: Components directly instantiated rather than injected

### Test Suite Organization
- **Unit Tests**: `/tests/unit/`, `/tests/agent/`, `/tests/llm/`, etc.
- **Integration Tests**: `/tests/integration/`
- **API Tests**: `/tests/api/`
- **Security Tests**: `/tests/security/`

## Phase 1: Create Interface Foundation (Day 1)

**Goal**: Establish interface contracts without breaking existing code

### Step 1.1: Create Base Interfaces

**Code Modification (CodeAgent)**:
1. Create `/faultmaven/models/interfaces.py`:
```python
# File: faultmaven/models/interfaces.py
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List, ContextManager
from pydantic import BaseModel

# Tool interfaces
class ToolResult(BaseModel):
    success: bool
    data: Any
    error: Optional[str] = None

class BaseTool(ABC):
    @abstractmethod
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        pass
    
    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        pass

# Infrastructure interfaces
class ILLMProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> str:
        pass

class ITracer(ABC):
    @abstractmethod
    def trace(self, operation: str) -> ContextManager:
        pass

class ISanitizer(ABC):
    @abstractmethod
    def sanitize(self, data: Any) -> Any:
        pass

class IVectorStore(ABC):
    @abstractmethod
    async def add_documents(self, documents: List[Dict]) -> None:
        pass
    
    @abstractmethod
    async def search(self, query: str, k: int = 5) -> List[Dict]:
        pass

class ISessionStore(ABC):
    @abstractmethod
    async def get(self, key: str) -> Optional[Dict]:
        pass
    
    @abstractmethod
    async def set(self, key: str, value: Dict, ttl: Optional[int] = None) -> None:
        pass
```

**Test Modification (TestAgent)**:
1. Create `/tests/unit/test_interfaces.py`:
```python
import pytest
from faultmaven.models.interfaces import BaseTool, ILLMProvider, ITracer

def test_interfaces_exist():
    """Verify all interfaces are properly defined"""
    assert hasattr(BaseTool, 'execute')
    assert hasattr(ILLMProvider, 'generate')
    assert hasattr(ITracer, 'trace')
```

**Verification**: `pytest tests/unit/test_interfaces.py`

### Step 1.2: Create Infrastructure Interfaces

**Code Modification (CodeAgent)**:
1. Create `/faultmaven/infrastructure/interfaces.py`:
```python
# File: faultmaven/infrastructure/interfaces.py
from faultmaven.models.interfaces import ITracer, ISanitizer, ILLMProvider, IVectorStore, ISessionStore

# Re-export for infrastructure layer
__all__ = ['ITracer', 'ISanitizer', 'ILLMProvider', 'IVectorStore', 'ISessionStore']
```

**Test Modification (TestAgent)**:
1. No test changes needed - import verification only

**Verification**: `python -c "from faultmaven.infrastructure.interfaces import *"`

## Phase 2: Implement Interface Adapters (Day 1)

**Goal**: Make existing components implement interfaces without breaking functionality

### Step 2.1: Adapt LLM Router

**Code Modification (CodeAgent)**:
1. Modify `/faultmaven/infrastructure/llm/router.py`:
   - Add import: `from faultmaven.models.interfaces import ILLMProvider`
   - Update class declaration: `class LLMRouter(ILLMProvider):`
   - Ensure `generate` method signature matches interface

**Test Modification (TestAgent)**:
1. No changes needed - existing tests should pass

**Verification**: `pytest tests/llm/test_router.py`

### Step 2.2: Adapt Tools to Interface

**Code Modification (CodeAgent)**:
1. Modify `/faultmaven/tools/knowledge_base.py`:
   - Add import: `from faultmaven.models.interfaces import BaseTool, ToolResult`
   - Update class: `class KnowledgeBaseTool(BaseTool):`
   - Implement `execute` and `get_schema` methods

2. Modify `/faultmaven/tools/web_search.py`:
   - Same pattern as knowledge_base.py

**Test Modification (TestAgent)**:
1. Update `/tests/agent/tools/test_knowledge_base.py`:
   - Verify tool implements BaseTool interface
2. Update `/tests/agent/tools/test_web_search.py`:
   - Verify tool implements BaseTool interface

**Verification**: `pytest tests/agent/tools/`

### Step 2.3: Adapt Infrastructure Components

**Code Modification (CodeAgent)**:
1. Modify `/faultmaven/infrastructure/security/redaction.py`:
   - Add import: `from faultmaven.models.interfaces import ISanitizer`
   - Update class: `class DataSanitizer(ISanitizer):`

2. Modify `/faultmaven/infrastructure/observability/tracing.py`:
   - Add import: `from faultmaven.models.interfaces import ITracer`
   - Create adapter class if needed

**Test Modification (TestAgent)**:
1. Update tests to verify interface implementation

**Verification**: `pytest tests/security/ tests/infrastructure/`

## Phase 3: Create Service Layer (Day 2)

**Goal**: Introduce service layer with all business logic

### Step 3.1: Create AgentService

**Code Modification (CodeAgent)**:
1. Create `/faultmaven/services/agent_service_refactored.py`:
```python
# File: faultmaven/services/agent_service_refactored.py
from typing import List, Optional
from faultmaven.models.interfaces import BaseTool, ITracer, ISanitizer, ILLMProvider
from faultmaven.models import QueryRequest, TroubleshootingResponse

class AgentServiceRefactored:
    def __init__(
        self,
        llm_provider: ILLMProvider,
        tools: List[BaseTool],
        tracer: ITracer,
        sanitizer: ISanitizer
    ):
        self._llm = llm_provider
        self._tools = tools
        self._tracer = tracer
        self._sanitizer = sanitizer
        self._agent = None  # Will be created per request
    
    async def process_query(self, request: QueryRequest) -> TroubleshootingResponse:
        """Main business logic for query processing"""
        with self._tracer.trace("process_query"):
            # Sanitize input
            clean_query = self._sanitizer.sanitize(request.query)
            
            # Create and configure agent
            from faultmaven.core.agent.agent import FaultMavenAgent
            agent = FaultMavenAgent(llm_interface=self._llm)
            
            # Execute troubleshooting
            result = await agent.run(
                query=clean_query,
                session_id=request.session_id,
                tools=self._tools
            )
            
            # Format and sanitize response
            response = TroubleshootingResponse(
                investigation_id=result.get('investigation_id'),
                session_id=request.session_id,
                findings=self._sanitizer.sanitize(result.get('findings', [])),
                recommendations=result.get('recommendations', []),
                confidence_score=result.get('confidence', 0.0)
            )
            
            return response
```

**Test Modification (TestAgent)**:
1. Create `/tests/unit/test_agent_service_refactored.py`:
```python
import pytest
from unittest.mock import Mock, AsyncMock
from faultmaven.services.agent_service_refactored import AgentServiceRefactored

@pytest.mark.asyncio
async def test_agent_service_process_query():
    # Mock dependencies
    mock_llm = Mock()
    mock_tracer = Mock()
    mock_sanitizer = Mock()
    mock_sanitizer.sanitize.return_value = "clean query"
    
    service = AgentServiceRefactored(
        llm_provider=mock_llm,
        tools=[],
        tracer=mock_tracer,
        sanitizer=mock_sanitizer
    )
    
    # Test would go here
    assert service is not None
```

**Verification**: `pytest tests/unit/test_agent_service_refactored.py`

### Step 3.2: Create DataService Refactored

**Code Modification (CodeAgent)**:
1. Create `/faultmaven/services/data_service_refactored.py`:
   - Move all data processing logic from API routes
   - Implement with interface dependencies

**Test Modification (TestAgent)**:
1. Create corresponding test file with mocked interfaces

**Verification**: `pytest tests/unit/test_data_service_refactored.py`

### Step 3.3: Create KnowledgeService Refactored

**Code Modification (CodeAgent)**:
1. Create `/faultmaven/services/knowledge_service_refactored.py`:
   - Move knowledge base logic from API routes
   - Implement with interface dependencies

**Test Modification (TestAgent)**:
1. Create corresponding test file

**Verification**: `pytest tests/unit/test_knowledge_service_refactored.py`

## Phase 4: Refactor Core Layer (Day 2)

**Goal**: Update Core to depend only on interfaces

### Step 4.1: Update Agent to Use Interfaces

**Code Modification (CodeAgent)**:
1. Create `/faultmaven/core/agent/agent_refactored.py`:
```python
# File: faultmaven/core/agent/agent_refactored.py
from typing import List, Optional
from faultmaven.models.interfaces import ILLMProvider, BaseTool
from langgraph.graph import StateGraph

class FaultMavenAgentRefactored:
    def __init__(self, llm_interface: ILLMProvider):
        self._llm = llm_interface
        self._tools: List[BaseTool] = []
        self._graph = None
    
    def configure_tools(self, tools: List[BaseTool]):
        """Configure tools via interface"""
        self._tools = tools
        self._build_graph()
    
    async def run(self, query: str, session_id: str, tools: Optional[List[BaseTool]] = None) -> dict:
        """Execute with interface-based tools"""
        if tools:
            self.configure_tools(tools)
        
        # Agent logic here using interfaces
        result = {}
        for tool in self._tools:
            tool_result = await tool.execute({"query": query})
            # Process tool_result
        
        return result
```

**Test Modification (TestAgent)**:
1. Update `/tests/agent/test_core_agent.py`:
   - Mock BaseTool interface instead of concrete tools
   - Verify agent works with interface mocks

**Verification**: `pytest tests/agent/test_core_agent.py`

### Step 4.2: Remove Circular Dependencies

**Code Modification (CodeAgent)**:
1. Update `/faultmaven/core/agent/agent.py`:
   - Remove all imports from `faultmaven.tools`
   - Use only interface imports from `faultmaven.models.interfaces`

**Test Modification (TestAgent)**:
1. Update tests to inject mocked tools via interfaces

**Verification**: `pytest tests/agent/`

## Phase 5: Create Dependency Injection Container (Day 3)

**Goal**: Centralized dependency management

### Step 5.1: Create DI Container

**Code Modification (CodeAgent)**:
1. Create `/faultmaven/container_refactored.py`:
```python
# File: faultmaven/container_refactored.py
from typing import Optional
import os
from faultmaven.models.interfaces import ILLMProvider, ITracer, ISanitizer, BaseTool

class DIContainer:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def initialize(self):
        if self._initialized:
            return
        
        # Create infrastructure components
        from faultmaven.infrastructure.llm.router import LLMRouter
        from faultmaven.infrastructure.security.redaction import DataSanitizer
        from faultmaven.infrastructure.observability.tracing import OpikTracer
        
        self.llm_provider: ILLMProvider = LLMRouter()
        self.sanitizer: ISanitizer = DataSanitizer()
        self.tracer: ITracer = OpikTracer()
        
        # Create tools
        from faultmaven.tools.knowledge_base import KnowledgeBaseTool
        from faultmaven.tools.web_search import WebSearchTool
        from faultmaven.core.knowledge.ingestion import KnowledgeIngester
        
        ingester = KnowledgeIngester()
        self.tools: List[BaseTool] = [
            KnowledgeBaseTool(ingester),
            WebSearchTool()
        ]
        
        # Create services
        from faultmaven.services.agent_service_refactored import AgentServiceRefactored
        
        self.agent_service = AgentServiceRefactored(
            llm_provider=self.llm_provider,
            tools=self.tools,
            tracer=self.tracer,
            sanitizer=self.sanitizer
        )
        
        self._initialized = True
    
    def get_agent_service(self):
        self.initialize()
        return self.agent_service

# Global container instance
container = DIContainer()
```

**Test Modification (TestAgent)**:
1. Create `/tests/unit/test_container_refactored.py`:
   - Test container initialization
   - Test dependency injection

**Verification**: `pytest tests/unit/test_container_refactored.py`

## Phase 6: Refactor API Routes (Day 3)

**Goal**: Remove all business logic and violations from API layer

### Step 6.1: Refactor Agent Routes

**Code Modification (CodeAgent)**:
1. Create `/faultmaven/api/v1/routes/agent_refactored.py`:
```python
# File: faultmaven/api/v1/routes/agent_refactored.py
from fastapi import APIRouter, Depends
from faultmaven.models import QueryRequest, TroubleshootingResponse
from faultmaven.api.v1.dependencies import get_agent_service
from faultmaven.services.agent_service_refactored import AgentServiceRefactored

router = APIRouter(prefix="/query", tags=["query_processing"])

@router.post("/troubleshoot", response_model=TroubleshootingResponse)
async def troubleshoot(
    request: QueryRequest,
    agent_service: AgentServiceRefactored = Depends(get_agent_service)
):
    """Thin controller - only delegates to service"""
    return await agent_service.process_query(request)
```

2. Create `/faultmaven/api/v1/dependencies.py`:
```python
# File: faultmaven/api/v1/dependencies.py
from faultmaven.container_refactored import container

def get_agent_service():
    return container.get_agent_service()

def get_data_service():
    return container.get_data_service()

def get_knowledge_service():
    return container.get_knowledge_service()
```

**Test Modification (TestAgent)**:
1. Update `/tests/api/test_query_processing.py`:
   - Mock service layer instead of individual components
   - Verify API only delegates to service

**Verification**: `pytest tests/api/test_query_processing.py`

### Step 6.2: Refactor Data Routes

**Code Modification (CodeAgent)**:
1. Create `/faultmaven/api/v1/routes/data_refactored.py`:
   - Remove all business logic
   - Only validate input and delegate to DataService

**Test Modification (TestAgent)**:
1. Update API tests to mock service layer

**Verification**: `pytest tests/api/test_data_ingestion.py`

## Phase 7: Integration and Migration (Day 4)

**Goal**: Safely migrate from old to new implementation

### Step 7.1: Create Feature Flags

**Code Modification (CodeAgent)**:
1. Create `/faultmaven/config/feature_flags.py`:
```python
# File: faultmaven/config/feature_flags.py
import os

USE_REFACTORED_SERVICES = os.getenv("USE_REFACTORED_SERVICES", "false").lower() == "true"
USE_REFACTORED_API = os.getenv("USE_REFACTORED_API", "false").lower() == "true"
USE_DI_CONTAINER = os.getenv("USE_DI_CONTAINER", "false").lower() == "true"
```

**Test Modification (TestAgent)**:
1. Add tests for both old and new code paths

**Verification**: `pytest tests/`

### Step 7.2: Update Main Application

**Code Modification (CodeAgent)**:
1. Modify `/faultmaven/main.py`:
```python
# Add feature flag imports
from faultmaven.config.feature_flags import USE_REFACTORED_API

# Conditionally include routers
if USE_REFACTORED_API:
    from faultmaven.api.v1.routes import agent_refactored as agent
    from faultmaven.api.v1.routes import data_refactored as data
else:
    from faultmaven.api.v1.routes import agent
    from faultmaven.api.v1.routes import data
```

**Test Modification (TestAgent)**:
1. Test both configurations

**Verification**: `USE_REFACTORED_API=false pytest tests/` and `USE_REFACTORED_API=true pytest tests/`

## Phase 8: Architecture Validation (Day 4)

**Goal**: Ensure architectural constraints are enforced

### Step 8.1: Create Architecture Tests

**Code Modification (CodeAgent)**:
1. Create `/tests/test_architecture.py`:
```python
# File: tests/test_architecture.py
import ast
from pathlib import Path

def test_api_layer_boundaries():
    """Ensure API routes don't import from Core or Infrastructure"""
    api_files = Path("faultmaven/api").rglob("*_refactored.py")
    
    for file in api_files:
        content = file.read_text()
        tree = ast.parse(content)
        
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("faultmaven.core"), \
                    f"{file} violates: imports from core"
                assert not module.startswith("faultmaven.infrastructure"), \
                    f"{file} violates: imports from infrastructure"
                assert not module.startswith("faultmaven.tools"), \
                    f"{file} violates: imports from tools"

def test_core_independence():
    """Ensure Core doesn't depend on concrete implementations"""
    core_files = Path("faultmaven/core").rglob("*_refactored.py")
    
    for file in core_files:
        content = file.read_text()
        assert "from faultmaven.tools" not in content, \
            f"{file} has concrete tool dependency"
        assert "from faultmaven.infrastructure" not in content, \
            f"{file} has infrastructure dependency"

def test_circular_dependencies():
    """Check for circular import patterns"""
    # Implementation here
    pass
```

**Test Modification (TestAgent)**:
1. No changes needed

**Verification**: `pytest tests/test_architecture.py`

### Step 8.2: Create Validation Script

**Code Modification (CodeAgent)**:
1. Create `/scripts/validate_architecture.py`:
```python
#!/usr/bin/env python3
# File: scripts/validate_architecture.py
"""
Architecture validation script to run before commits
"""
import sys
import subprocess

def main():
    print("Running architecture validation...")
    
    # Run architecture tests
    result = subprocess.run(
        ["pytest", "tests/test_architecture.py", "-v"],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        print("❌ Architecture validation failed!")
        print(result.stdout)
        sys.exit(1)
    
    print("✅ Architecture validation passed!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

**Test Modification (TestAgent)**:
1. No changes needed

**Verification**: `python scripts/validate_architecture.py`

## Phase 9: Cleanup and Documentation (Day 5)

**Goal**: Remove old code and update documentation

### Step 9.1: Remove Old Implementations

**Code Modification (CodeAgent)**:
1. After verification, remove old files:
   - Move original files to `_deprecated/` folder
   - Rename `_refactored` files to original names
   - Update all imports

**Test Modification (TestAgent)**:
1. Update all test imports to use new file names
2. Remove tests for deprecated code

**Verification**: Full test suite: `pytest tests/ --cov=faultmaven`

### Step 9.2: Update Documentation

**Code Modification (CodeAgent)**:
1. Update `/docs/architecture/current-architecture.md`
2. Update README with new architecture
3. Create migration guide

**Test Modification (TestAgent)**:
1. No changes needed

**Verification**: Manual review

## Rollback Plan

Each phase can be rolled back independently:

1. **Phase 1-2**: No impact - interfaces are additions only
2. **Phase 3-4**: Services are separate files - can be removed
3. **Phase 5-6**: Feature flags allow instant rollback
4. **Phase 7-8**: Keep old code until fully validated
5. **Phase 9**: Git history preserves old implementation

## Success Metrics

- ✅ All existing tests pass (maintain backward compatibility)
- ✅ Architecture tests pass (enforce boundaries)
- ✅ No circular dependencies detected
- ✅ Coverage remains above 70%
- ✅ API responses unchanged
- ✅ Performance metrics stable or improved

## Risk Mitigation

1. **Parallel Implementation**: Keep old code alongside new
2. **Feature Flags**: Control rollout gradually
3. **Comprehensive Testing**: Each phase independently tested
4. **Incremental Migration**: No "big bang" changes
5. **Documentation**: Every change documented

## Timeline

- **Day 1**: Phases 1-2 (Interfaces and Adapters)
- **Day 2**: Phases 3-4 (Services and Core refactoring)
- **Day 3**: Phases 5-6 (DI Container and API refactoring)
- **Day 4**: Phases 7-8 (Integration and Validation)
- **Day 5**: Phase 9 (Cleanup and Documentation)

## Specialist Agent Instructions

### For CodeAgent
- Create new files with `_refactored` suffix initially
- Preserve all existing functionality
- Follow exact interface signatures
- Add comprehensive docstrings
- Use type hints throughout

### For TestAgent
- Update imports to use interfaces for mocking
- Create new test files for refactored components
- Ensure both old and new implementations tested
- Add architecture validation tests
- Maintain or improve coverage

## Conclusion

This plan provides a safe, incremental path from the current monolithic structure to a clean, service-oriented architecture. Each phase is independently valuable and verifiable, minimizing risk while maximizing architectural improvements.