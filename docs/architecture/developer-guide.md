# FaultMaven Developer Onboarding Guide

**Document Type**: Developer Guide  
**Last Updated**: August 2025

## Overview

Welcome to FaultMaven! This guide helps new developers quickly understand our intelligent architecture, development workflow, and best practices. FaultMaven features a sophisticated clean architecture with advanced memory management, strategic planning, and intelligent communication capabilities that make it truly intelligent and adaptive.

## Quick Start (5 Minutes)

### 1. Environment Setup

```bash
# Clone and setup
git clone <repository-url>
cd FaultMaven
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# or .venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-test.txt

# Download language model for processing
python -m spacy download en_core_web_lg
```

### 2. Basic Configuration

Create `.env` file:

```bash
# Minimal working configuration
CHAT_PROVIDER="local"
LOCAL_LLM_URL="http://localhost:5000"  # Or use real provider
REDIS_URL="redis://localhost:6379"

# Optional: Add real LLM provider
FIREWORKS_API_KEY="fw_your_key"
CHAT_PROVIDER="fireworks"
```

### 3. Start Development

```bash
# Simple startup (loads .env automatically)
./run_faultmaven.sh

# Or manual startup
python -m faultmaven.main

# Run tests to verify setup
python run_tests.py --unit
```

## Enhanced Architecture Overview

### Core Philosophy

FaultMaven uses **intelligent interface-based dependency injection** throughout, featuring:

- **Advanced Memory Management**: Hierarchical memory with semantic understanding
- **Strategic Planning**: Multi-phase planning with problem decomposition
- **Intelligent Communication**: Context-aware prompting and response optimization
- **Continuous Learning**: System improvement through conversation analysis
- **All dependencies are abstractions (interfaces)** for maximum flexibility

### Enhanced Layer Structure

```
API Layer (FastAPI routes)
    ↓ [Uses dependency injection]
Service Layer (Business logic + Intelligence)
    ↓ [Memory + Planning integration]
Core Domain (Business entities + AI reasoning)
    ↓ [Advanced communication capabilities]
Infrastructure (External integrations + Memory storage)
```

### Key Enhanced Interfaces

Located in `faultmaven/models/interfaces.py`:

```python
# Core Intelligence Interfaces
IMemoryService      # Hierarchical memory management
IPlanningService    # Strategic planning and problem decomposition
IPromptEngine       # Advanced prompting with optimization

# Infrastructure Interfaces
ILLMProvider        # AI model interaction with memory enhancement
ISanitizer          # Data cleaning/PII removal
ITracer             # Observability/tracing
IVectorStore        # Knowledge base storage with memory integration
ISessionStore       # Session management with memory preservation

# Processing Interfaces  
IDataClassifier     # Data type detection with context awareness
ILogProcessor       # Log analysis with pattern learning
IStorageBackend     # General storage

# Tool Interfaces
BaseTool            # Agent capabilities with planning integration
```

## New Architecture Features

### 1. Memory Management System

FaultMaven now features a sophisticated hierarchical memory architecture:

```python
# Memory Service Usage
from faultmaven.services.agentic.management.state_manager import IAgentStateManager

class EnhancedAgentService:
    def __init__(self, memory_service: IMemoryService):
        self._memory = memory_service
    
    async def process_query(self, request: QueryRequest) -> AgentResponse:
        # Retrieve relevant context from memory
        context = await self._memory.retrieve_context(
            request.session_id, 
            request.query
        )
        
        # Process with context awareness
        result = await self._process_with_context(request, context)
        
        # Consolidate insights back to memory
        await self._memory.consolidate_insights(
            request.session_id, 
            result
        )
        
        return result
```

**Memory Types**:
- **Working Memory**: Current conversation context (sliding window)
- **Session Memory**: Session-specific insights and learnings
- **User Memory**: Long-term user preferences and expertise patterns
- **Episodic Memory**: Past troubleshooting cases and resolutions

### 2. Strategic Planning System

The system now includes intelligent planning capabilities:

```python
# Planning Service Usage
from faultmaven.services.domain.planning_service import IPlanningService

class EnhancedAgentService:
    def __init__(self, planning_service: IPlanningService):
        self._planning = planning_service
    
    async def process_query(self, request: QueryRequest) -> AgentResponse:
        # Plan response strategy
        strategy = await self._planning.plan_response_strategy(
            request.query, 
            context
        )
        
        # Execute with strategic guidance
        result = await self._execute_with_strategy(request, strategy)
        
        return result
```

**Planning Features**:
- **Problem Decomposition**: LLM-powered problem breakdown
- **Solution Strategy**: Multi-phase solution development
- **Risk Assessment**: Comprehensive risk analysis and mitigation
- **Alternative Solutions**: Multiple approach evaluation

### 3. Advanced Prompting System

Enhanced prompting with multi-layer architecture:

```python
# Advanced Prompt Engine Usage
from faultmaven.core.prompting import AdvancedPromptEngine

class EnhancedAgentService:
    def __init__(self, prompt_engine: AdvancedPromptEngine):
        self._prompt_engine = prompt_engine
    
    async def generate_response(self, query: str, context: dict) -> str:
        # Assemble context-aware prompt
        prompt = await self._prompt_engine.assemble_prompt(
            question=query,
            response_type=ResponseType.ANSWER,
            context=context
        )
        
        # Generate optimized response
        response = await self._llm.generate(prompt)
        
        return response
```

**Prompt Features**:
- **Multi-Layer Architecture**: System, context, domain, task, safety, adaptation
- **Dynamic Optimization**: Quality-based prompt improvement
- **Context Injection**: Memory-aware prompt enhancement
- **Performance Tracking**: A/B testing and optimization

## Development Workflow

### 1. Understanding the New Architecture

Start with these key documents:
- `SYSTEM_ARCHITECTURE.md` - Complete system overview
- `COMPONENT_INTERACTIONS.md` - How components work together
- `IMPLEMENTATION_GAP_ANALYSIS.md` - What's implemented vs. planned

### 2. Working with Memory and Planning

```python
# Example: Adding memory to a new service
class MyNewService:
    def __init__(
        self,
        memory_service: IMemoryService,
        planning_service: IPlanningService
    ):
        self._memory = memory_service
        self._planning = planning_service
    
    async def process_request(self, request: MyRequest) -> MyResponse:
        # Get context from memory
        context = await self._memory.retrieve_context(
            request.session_id, 
            request.content
        )
        
        # Plan approach
        plan = await self._planning.plan_response_strategy(
            request.content, 
            context
        )
        
        # Process with intelligence
        result = await self._process_intelligently(request, context, plan)
        
        # Learn from interaction
        await self._memory.consolidate_insights(
            request.session_id, 
            result
        )
        
        return result
```

### 3. Testing with New Components

```python
# Testing memory and planning services
import pytest
from unittest.mock import Mock

class TestMyNewService:
    @pytest.fixture
    def mock_memory_service(self):
        service = Mock(spec=IMemoryService)
        service.retrieve_context.return_value = MockConversationContext()
        service.consolidate_insights.return_value = True
        return service
    
    @pytest.fixture
    def mock_planning_service(self):
        service = Mock(spec=IPlanningService)
        service.plan_response_strategy.return_value = MockStrategicPlan()
        return service
    
    async def test_service_with_intelligence(self, mock_memory_service, mock_planning_service):
        my_service = MyNewService(mock_memory_service, mock_planning_service)
        result = await my_service.process_request(MockRequest())
        
        assert result is not None
        mock_memory_service.retrieve_context.assert_called_once()
        mock_planning_service.plan_response_strategy.assert_called_once()
```

## Key Development Patterns

### 1. Memory-Aware Processing

Always consider memory context in your services:

```python
# Good: Memory-aware processing
async def process_data(self, data: str, session_id: str) -> ProcessedData:
    # Get relevant context
    context = await self._memory.retrieve_context(session_id, data)
    
    # Process with context
    result = await self._process_with_context(data, context)
    
    # Learn from processing
    await self._memory.consolidate_insights(session_id, result)
    
    return result

# Avoid: Ignoring memory context
async def process_data(self, data: str) -> ProcessedData:
    # Missing memory integration
    return await self._process(data)
```

### 2. Strategic Planning Integration

Use planning for complex operations:

```python
# Good: Planning-driven processing
async def solve_problem(self, problem: str, session_id: str) -> Solution:
    # Plan approach
    strategy = await self._planning.plan_response_strategy(problem, {})
    
    # Execute with strategy
    solution = await self._execute_strategy(problem, strategy)
    
    return solution

# Avoid: Direct problem solving
async def solve_problem(self, problem: str) -> Solution:
    # Missing strategic planning
    return await self._direct_solve(problem)
```

### 3. Context-Aware Communication

Always consider user context and expertise:

```python
# Good: Context-aware communication
async def generate_response(self, query: str, session_id: str) -> str:
    # Get user profile and context
    user_profile = await self._memory.get_user_profile(session_id)
    context = await self._memory.retrieve_context(session_id, query)
    
    # Generate appropriate response
    response = await self._generate_contextual_response(
        query, 
        user_profile, 
        context
    )
    
    return response

# Avoid: Generic responses
async def generate_response(self, query: str) -> str:
    # Missing context awareness
    return await self._generate_generic_response(query)
```

## Testing Strategy

### 1. Unit Testing

Test individual components with mocked dependencies:

```python
# Test memory service
async def test_memory_retrieval():
    mock_vector_store = Mock()
    memory_service = MemoryService(mock_vector_store)
    
    context = await memory_service.retrieve_context("session_1", "query")
    assert context is not None
    assert len(context.conversation_history) > 0

# Test planning service
async def test_planning_strategy():
    mock_llm = Mock()
    planning_service = PlanningService(mock_llm)
    
    strategy = await planning_service.plan_response_strategy("query", {})
    assert strategy is not None
    assert strategy.plan_components is not None
```

### 2. Integration Testing

Test component interactions:

```python
# Test complete workflow
async def test_intelligent_workflow():
    # Setup services
    memory_service = MemoryService(mock_vector_store)
    planning_service = PlanningService(mock_llm)
    agent_service = EnhancedAgentService(
        memory_service, 
        planning_service
    )
    
    # Test complete flow
    request = QueryRequest(session_id="test", query="test query")
    response = await agent_service.process_query(request)
    
    # Verify intelligence features
    assert response.view_state.memory_context is not None
    assert response.view_state.planning_state is not None
```

### 3. Performance Testing

Test memory and planning performance:

```python
# Test memory performance
async def test_memory_performance():
    start_time = time.time()
    context = await memory_service.retrieve_context("session_1", "query")
    end_time = time.time()
    
    # Should complete within 50ms
    assert (end_time - start_time) < 0.05
    assert context is not None

# Test planning performance
async def test_planning_performance():
    start_time = time.time()
    strategy = await planning_service.plan_response_strategy("query", {})
    end_time = time.time()
    
    # Should complete within 100ms
    assert (end_time - start_time) < 0.1
    assert strategy is not None
```

## Common Development Tasks

### 1. Adding a New Service

```python
# 1. Define interface
class IMyService(ABC):
    @abstractmethod
    async def process_request(self, request: MyRequest) -> MyResponse:
        pass

# 2. Implement service with intelligence
class MyService(IMyService):
    def __init__(
        self,
        memory_service: IMemoryService,
        planning_service: IPlanningService
    ):
        self._memory = memory_service
        self._planning = planning_service
    
    async def process_request(self, request: MyRequest) -> MyResponse:
        # Get context
        context = await self._memory.retrieve_context(
            request.session_id, 
            request.content
        )
        
        # Plan approach
        plan = await self._planning.plan_response_strategy(
            request.content, 
            context
        )
        
        # Process
        result = await self._process(request, context, plan)
        
        # Learn
        await self._memory.consolidate_insights(
            request.session_id, 
            result
        )
        
        return result

# 3. Add to DI container
def _create_service_layer(self):
    self.my_service = MyService(
        self.memory_service,
        self.planning_service
    )
```

### 2. Adding Memory Integration

```python
# Add memory to existing service
class EnhancedExistingService:
    def __init__(
        self,
        existing_dependencies: List[Any],
        memory_service: IMemoryService  # New dependency
    ):
        self._existing_deps = existing_dependencies
        self._memory = memory_service  # New memory integration
    
    async def existing_method(self, request: Request) -> Response:
        # Get context from memory
        context = await self._memory.retrieve_context(
            request.session_id, 
            request.content
        )
        
        # Use context in existing logic
        result = await self._existing_logic(request, context)
        
        # Learn from interaction
        await self._memory.consolidate_insights(
            request.session_id, 
            result
        )
        
        return result
```

### 3. Adding Planning Integration

```python
# Add planning to existing service
class EnhancedExistingService:
    def __init__(
        self,
        existing_dependencies: List[Any],
        planning_service: IPlanningService  # New dependency
    ):
        self._existing_deps = existing_dependencies
        self._planning = planning_service  # New planning integration
    
    async def existing_method(self, request: Request) -> Response:
        # Plan approach
        strategy = await self._planning.plan_response_strategy(
            request.content, 
            {}
        )
        
        # Use strategy in existing logic
        result = await self._existing_logic(request, strategy)
        
        return result
```

## Troubleshooting Common Issues

### 1. Memory Service Not Available

```python
# Check DI container initialization
container = DIContainer()
if not container.memory_service:
    print("Memory service not initialized")
    print("Check container health:", container.get_health_status())

# Verify Redis connection
redis_client = redis.Redis.from_url(REDIS_URL)
try:
    redis_client.ping()
    print("Redis connection OK")
except Exception as e:
    print(f"Redis connection failed: {e}")
```

### 2. Planning Service Errors

```python
# Check LLM provider availability
container = DIContainer()
if not container.llm_provider:
    print("LLM provider not available")
    print("Check LLM configuration in .env")

# Verify planning service health
planning_service = container.planning_service
health = await planning_service.get_health_status()
print("Planning service health:", health)
```

### 3. Performance Issues

```python
# Check memory usage
import psutil
process = psutil.Process()
print(f"Memory usage: {process.memory_info().rss / 1024 / 1024:.2f} MB")

# Check Redis performance
redis_client = redis.Redis.from_url(REDIS_URL)
start_time = time.time()
redis_client.ping()
end_time = time.time()
print(f"Redis response time: {(end_time - start_time) * 1000:.2f} ms")
```

## Next Steps

1. **Read the Architecture Documents**: Start with `SYSTEM_ARCHITECTURE.md`
2. **Explore the Codebase**: Look at existing memory and planning implementations
3. **Run the Tests**: Verify your environment is working correctly
4. **Start Small**: Add memory integration to a simple service first
5. **Ask Questions**: Use the team chat for architecture questions

## Resources

- **Architecture Documents**: `docs/architecture/`
- **Code Examples**: `faultmaven/services/` and `faultmaven/core/`
- **Testing**: `tests/` directory with comprehensive examples
- **Configuration**: `.env.example` for environment setup

Welcome to the intelligent future of FaultMaven! 🚀