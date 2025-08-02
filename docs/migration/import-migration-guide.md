# Import Migration Guide

This guide shows how to update imports after the FaultMaven backend refactoring.

## Quick Reference

### Models
```python
# Before
from faultmaven.models import DataType, SessionContext, AgentState

# After (no change - backward compatible)
from faultmaven.models import DataType, SessionContext, AgentState
```

### API Endpoints
```python
# Before
from faultmaven.api.data_ingestion import router
from faultmaven.api.kb_management import router
from faultmaven.api.query_processing import router
from faultmaven.api.sessions import router

# After
from faultmaven.api.v1.routes.data import router
from faultmaven.api.v1.routes.knowledge import router
from faultmaven.api.v1.routes.agent import router
from faultmaven.api.v1.routes.session import router
```

### Core Components
```python
# Before
from faultmaven.agent.core_agent import FaultMavenAgent
from faultmaven.agent.doctrine import Phase, TroubleshootingDoctrine
from faultmaven.data_processing.classifier import DataClassifier
from faultmaven.data_processing.log_processor import LogProcessor
from faultmaven.knowledge_base.ingestion import KnowledgeIngester

# After
from faultmaven.core.agent.agent import FaultMavenAgent
from faultmaven.core.agent.doctrine import Phase, TroubleshootingDoctrine
from faultmaven.core.processing.classifier import DataClassifier
from faultmaven.core.processing.log_analyzer import LogProcessor
from faultmaven.core.knowledge.ingestion import KnowledgeIngester
```

### Infrastructure
```python
# Before
from faultmaven.llm.router import LLMRouter
from faultmaven.security.redaction import DataSanitizer
from faultmaven.observability.tracing import trace, init_opik_tracing

# After
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.infrastructure.observability.tracing import trace, init_opik_tracing
```

### Tools
```python
# Before
from faultmaven.agent.tools.knowledge_base import KnowledgeBaseTool
from faultmaven.agent.tools.web_search import WebSearchTool

# After
from faultmaven.tools.knowledge_base import KnowledgeBaseTool
from faultmaven.tools.web_search import WebSearchTool
```

### Services (New)
```python
# New service layer imports
from faultmaven.services.agent_service import AgentService
from faultmaven.services.data_service import DataService
from faultmaven.services.knowledge_service import KnowledgeService
from faultmaven.services.session_service import SessionService
```

### Dependency Injection (New)
```python
# New dependency injection
from faultmaven.container import ApplicationContainer
from faultmaven.api.v1.dependencies import get_agent_service, get_current_session
```

## Migration Steps

1. **Update test imports**: Tests should import from the new module locations
2. **Update main.py**: The FastAPI app now imports from `api.v1.routes`
3. **Use services**: New code should use the service layer instead of direct domain access
4. **Dependency injection**: Use the container for service instantiation

## Backward Compatibility

The `models` module maintains backward compatibility by re-exporting all models from the original location. This allows gradual migration without breaking existing code.