# FaultMaven Architecture Gap Closure Instructions

## Overview
This document provides detailed instructions for the development team to close the architectural gaps identified in the January 2025 architecture review. Each gap includes context, current state, desired state, and step-by-step implementation instructions.

---

## Gap 1: IVectorStore Interface Implementation for ChromaDB

### Current State
- ChromaDB is used directly without implementing the `IVectorStore` interface
- Located in `infrastructure/persistence/chromadb.py`
- The `IVectorStore` interface is defined but not implemented

### Desired State
- ChromaDB wrapped in a class that implements `IVectorStore` interface
- Consistent with other infrastructure components (Redis, Presidio)
- Enables easy swapping of vector store implementations

### Implementation Instructions

#### Step 1: Create ChromaDBVectorStore Class
Create a new file: `faultmaven/infrastructure/persistence/chromadb_store.py`

```python
"""
ChromaDB implementation of IVectorStore interface.

This module provides a ChromaDB-based vector store that implements
the IVectorStore interface for consistent vector database operations.
"""

from typing import List, Dict, Optional
import chromadb
from chromadb.config import Settings
from faultmaven.models.interfaces import IVectorStore
from faultmaven.infrastructure.base_client import BaseExternalClient
import os
import logging

class ChromaDBVectorStore(BaseExternalClient, IVectorStore):
    """ChromaDB implementation of the IVectorStore interface"""
    
    def __init__(self):
        """Initialize ChromaDB client with K8s service configuration"""
        super().__init__(
            client_name="chromadb_vector_store",
            service_name="ChromaDB",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=60
        )
        
        # Get ChromaDB configuration from environment
        chromadb_url = os.getenv("CHROMADB_URL", "http://chromadb.faultmaven.local:30080")
        chromadb_token = os.getenv("CHROMADB_API_KEY", "faultmaven-dev-chromadb-2025")
        
        # Initialize ChromaDB client
        self.client = chromadb.HttpClient(
            host=chromadb_url.replace("http://", "").replace(":30080", ""),
            port=30080,
            settings=Settings(
                chroma_client_auth_provider="chromadb.auth.token.TokenAuthClientProvider",
                chroma_client_auth_credentials=chromadb_token
            )
        )
        
        # Get or create collection
        self.collection_name = "faultmaven_knowledge"
        try:
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "FaultMaven knowledge base"}
            )
            self.logger.info(f"✅ Connected to ChromaDB collection: {self.collection_name}")
        except Exception as e:
            self.logger.error(f"❌ Failed to connect to ChromaDB: {e}")
            raise
    
    async def add_documents(self, documents: List[Dict]) -> None:
        """
        Add documents to the vector store.
        
        Args:
            documents: List of documents with 'id', 'content', and 'metadata' keys
        """
        async def _add_wrapper():
            ids = [doc['id'] for doc in documents]
            contents = [doc['content'] for doc in documents]
            metadatas = [doc.get('metadata', {}) for doc in documents]
            
            self.collection.add(
                ids=ids,
                documents=contents,
                metadatas=metadatas
            )
            
            self.logger.info(f"Added {len(documents)} documents to vector store")
        
        await self.call_external(
            operation_name="add_documents",
            call_func=_add_wrapper,
            timeout=30.0,
            retries=2,
            retry_delay=2.0
        )
    
    async def search(self, query: str, k: int = 5) -> List[Dict]:
        """
        Search for similar documents in the vector store.
        
        Args:
            query: Search query text
            k: Number of results to return
            
        Returns:
            List of similar documents with scores
        """
        async def _search_wrapper():
            results = self.collection.query(
                query_texts=[query],
                n_results=k,
                include=["documents", "metadatas", "distances"]
            )
            
            # Format results according to interface contract
            formatted_results = []
            for i in range(len(results['ids'][0])):
                formatted_results.append({
                    'id': results['ids'][0][i],
                    'content': results['documents'][0][i],
                    'metadata': results['metadatas'][0][i],
                    'score': 1.0 - results['distances'][0][i]  # Convert distance to similarity
                })
            
            self.logger.debug(f"Found {len(formatted_results)} similar documents")
            return formatted_results
        
        return await self.call_external(
            operation_name="search",
            call_func=_search_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def delete_documents(self, ids: List[str]) -> None:
        """Delete documents by IDs"""
        async def _delete_wrapper():
            self.collection.delete(ids=ids)
            self.logger.info(f"Deleted {len(ids)} documents from vector store")
        
        await self.call_external(
            operation_name="delete_documents",
            call_func=_delete_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0
        )
```

#### Step 2: Update the DI Container
Modify `faultmaven/container.py` to use the new ChromaDBVectorStore:

```python
# In _create_infrastructure_layer() method, add:
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore

# Initialize vector store
try:
    self.vector_store: IVectorStore = ChromaDBVectorStore()
    logging.getLogger(__name__).debug("Vector store initialized")
except Exception as e:
    logging.getLogger(__name__).warning(f"Vector store initialization failed: {e}")
    self.vector_store = None

# Add getter method:
def get_vector_store(self):
    """Get the vector store interface implementation"""
    if not self._initialized:
        self.initialize()
    return self.vector_store
```

#### Step 3: Update KnowledgeService
Modify `faultmaven/services/knowledge_service.py` to accept IVectorStore:

```python
# Update constructor to use IVectorStore type hint:
def __init__(
    self,
    knowledge_ingester: IKnowledgeIngester,
    sanitizer: ISanitizer,
    tracer: ITracer,
    vector_store: Optional[IVectorStore] = None  # Changed from None
):
    self.vector_store = vector_store
    # ... rest of initialization
```

---

## Gap 2: ISessionStore Interface Implementation for Redis

### Current State
- SessionManager in `session_management.py` uses Redis directly
- ISessionStore interface is defined but not implemented
- Session management works but violates interface contract

### Desired State
- SessionManager implements ISessionStore interface
- Consistent interface-based design
- Enables swapping session backends

### Implementation Instructions

#### Step 1: Create RedisSessionStore Class
Create a new file: `faultmaven/infrastructure/persistence/redis_session_store.py`

```python
"""
Redis implementation of ISessionStore interface.

This module provides a Redis-based session store that implements
the ISessionStore interface for consistent session management.
"""

from typing import Dict, Optional
import json
from datetime import datetime
from faultmaven.models.interfaces import ISessionStore
from faultmaven.infrastructure.persistence.redis import RedisClient

class RedisSessionStore(ISessionStore):
    """Redis implementation of the ISessionStore interface"""
    
    def __init__(self):
        """Initialize Redis session store"""
        self.redis_client = RedisClient()
        self.default_ttl = 1800  # 30 minutes default
        self.prefix = "session:"
    
    async def get(self, key: str) -> Optional[Dict]:
        """
        Get session data by key.
        
        Args:
            key: Session key to retrieve
            
        Returns:
            Session data if found, None otherwise
        """
        full_key = f"{self.prefix}{key}"
        data = await self.redis_client.get(full_key)
        
        if data:
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return None
        return None
    
    async def set(self, key: str, value: Dict, ttl: Optional[int] = None) -> None:
        """
        Set session data with optional TTL.
        
        Args:
            key: Session key to set
            value: Session data to store
            ttl: Time to live in seconds (optional)
        """
        full_key = f"{self.prefix}{key}"
        
        # Add timestamp if not present
        if 'last_activity' not in value:
            value['last_activity'] = datetime.utcnow().isoformat()
        
        serialized = json.dumps(value)
        ttl = ttl or self.default_ttl
        
        await self.redis_client.set(full_key, serialized, ex=ttl)
    
    async def delete(self, key: str) -> bool:
        """
        Delete session by key.
        
        Args:
            key: Session key to delete
            
        Returns:
            True if deleted, False if not found
        """
        full_key = f"{self.prefix}{key}"
        result = await self.redis_client.delete(full_key)
        return result > 0
    
    async def exists(self, key: str) -> bool:
        """
        Check if session exists.
        
        Args:
            key: Session key to check
            
        Returns:
            True if exists, False otherwise
        """
        full_key = f"{self.prefix}{key}"
        return await self.redis_client.exists(full_key) > 0
    
    async def extend_ttl(self, key: str, ttl: Optional[int] = None) -> bool:
        """
        Extend session TTL.
        
        Args:
            key: Session key to extend
            ttl: New TTL in seconds
            
        Returns:
            True if extended, False if not found
        """
        full_key = f"{self.prefix}{key}"
        ttl = ttl or self.default_ttl
        return await self.redis_client.expire(full_key, ttl)
```

#### Step 2: Refactor SessionManager
Update `faultmaven/session_management.py` to use ISessionStore:

```python
from faultmaven.models.interfaces import ISessionStore
from faultmaven.infrastructure.persistence.redis_session_store import RedisSessionStore

class SessionManager:
    """Session manager using ISessionStore interface"""
    
    def __init__(self, session_store: Optional[ISessionStore] = None):
        """Initialize with session store interface"""
        self.session_store = session_store or RedisSessionStore()
    
    async def create_session(self, user_id: Optional[str] = None) -> SessionContext:
        """Create new session using interface"""
        session_id = str(uuid.uuid4())
        session_data = {
            'session_id': session_id,
            'user_id': user_id,
            'created_at': datetime.utcnow().isoformat(),
            'last_activity': datetime.utcnow().isoformat(),
            'data_uploads': [],
            'investigation_history': []
        }
        
        await self.session_store.set(session_id, session_data)
        return SessionContext(**session_data)
    
    async def get_session(self, session_id: str) -> Optional[SessionContext]:
        """Get session using interface"""
        session_data = await self.session_store.get(session_id)
        if session_data:
            return SessionContext(**session_data)
        return None
    
    # Update other methods similarly...
```

#### Step 3: Update Container
Modify `faultmaven/container.py`:

```python
# In _create_infrastructure_layer() method:
from faultmaven.infrastructure.persistence.redis_session_store import RedisSessionStore

try:
    self.session_store: ISessionStore = RedisSessionStore()
    logging.getLogger(__name__).debug("Session store initialized")
except Exception as e:
    logging.getLogger(__name__).warning(f"Session store initialization failed: {e}")
    self.session_store = None

# Update _create_service_layer() method:
from faultmaven.session_management import SessionManager

self.session_service = SessionManager(session_store=self.session_store)
```

---

## Gap 3: Tool Registry Pattern

### Current State
- Tools are manually instantiated in `_create_tools_layer()`
- Hard-coded tool creation
- Difficult to add new tools dynamically

### Desired State
- Dynamic tool registration system
- Tools self-register on import
- Easy addition of new tools

### Implementation Instructions

#### Step 1: Create Tool Registry
Create a new file: `faultmaven/tools/registry.py`

```python
"""
Tool Registry for dynamic tool registration.

This module provides a registry pattern for tools to self-register,
enabling dynamic tool discovery and instantiation.
"""

from typing import Dict, List, Type, Optional
import logging
from faultmaven.models.interfaces import BaseTool

class ToolRegistry:
    """Registry for dynamically registering and managing tools"""
    
    _instance = None
    _tools: Dict[str, Type[BaseTool]] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @classmethod
    def register(cls, name: str, tool_class: Type[BaseTool]):
        """
        Register a tool class.
        
        Args:
            name: Unique name for the tool
            tool_class: Tool class implementing BaseTool
        """
        if not issubclass(tool_class, BaseTool):
            raise ValueError(f"{tool_class} must implement BaseTool interface")
        
        cls._tools[name] = tool_class
        logging.getLogger(__name__).debug(f"Registered tool: {name}")
    
    @classmethod
    def get_tool(cls, name: str) -> Optional[Type[BaseTool]]:
        """Get a registered tool class by name"""
        return cls._tools.get(name)
    
    @classmethod
    def list_tools(cls) -> List[str]:
        """List all registered tool names"""
        return list(cls._tools.keys())
    
    @classmethod
    def create_all_tools(cls, **kwargs) -> List[BaseTool]:
        """
        Create instances of all registered tools.
        
        Args:
            **kwargs: Arguments to pass to tool constructors
            
        Returns:
            List of instantiated tools
        """
        tools = []
        for name, tool_class in cls._tools.items():
            try:
                tool = tool_class(**kwargs)
                tools.append(tool)
                logging.getLogger(__name__).debug(f"Created tool: {name}")
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to create tool {name}: {e}")
        
        return tools

# Global registry instance
tool_registry = ToolRegistry()

def register_tool(name: str):
    """
    Decorator for registering tools.
    
    Usage:
        @register_tool("knowledge_base")
        class KnowledgeBaseTool(BaseTool):
            ...
    """
    def decorator(cls):
        tool_registry.register(name, cls)
        return cls
    return decorator
```

#### Step 2: Update Existing Tools
Modify `faultmaven/tools/knowledge_base.py`:

```python
from faultmaven.tools.registry import register_tool
from faultmaven.models.interfaces import BaseTool, ToolResult

@register_tool("knowledge_base")
class KnowledgeBaseTool(BaseTool):
    """Knowledge base tool with self-registration"""
    
    def __init__(self, knowledge_ingester=None):
        # Existing initialization code...
        pass
    
    # Rest of the implementation remains the same
```

Modify `faultmaven/tools/web_search.py`:

```python
from faultmaven.tools.registry import register_tool
from faultmaven.models.interfaces import BaseTool, ToolResult

@register_tool("web_search")
class WebSearchTool(BaseTool):
    """Web search tool with self-registration"""
    
    def __init__(self):
        # Existing initialization code...
        pass
    
    # Rest of the implementation remains the same
```

#### Step 3: Update Container to Use Registry
Modify `faultmaven/container.py`:

```python
def _create_tools_layer(self):
    """Create tools using the registry pattern"""
    from faultmaven.tools.registry import tool_registry
    from faultmaven.core.knowledge.ingestion import KnowledgeIngester
    
    # Import tools to trigger registration
    import faultmaven.tools.knowledge_base
    import faultmaven.tools.web_search
    
    # Create knowledge ingester for tools that need it
    try:
        ingester = KnowledgeIngester()
    except Exception as e:
        logging.getLogger(__name__).warning(f"KnowledgeIngester creation failed: {e}")
        ingester = None
    
    # Create all registered tools
    self.tools: List[BaseTool] = tool_registry.create_all_tools(
        knowledge_ingester=ingester
    )
    
    logging.getLogger(__name__).debug(
        f"Tools layer created with {len(self.tools)} tools: {tool_registry.list_tools()}"
    )
```

---

## Gap 4: Centralized Configuration Management

### Current State
- Direct `os.getenv()` calls scattered throughout codebase
- No centralized configuration validation
- Difficult to manage environment-specific configs

### Desired State
- Centralized Config class
- Type-safe configuration access
- Environment-specific configuration profiles

### Implementation Instructions

#### Step 1: Create Configuration Interface
Add to `faultmaven/models/interfaces.py`:

```python
class IConfiguration(ABC):
    """Interface for configuration management"""
    
    @abstractmethod
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key"""
        pass
    
    @abstractmethod
    def get_int(self, key: str, default: int = 0) -> int:
        """Get integer configuration value"""
        pass
    
    @abstractmethod
    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean configuration value"""
        pass
    
    @abstractmethod
    def get_str(self, key: str, default: str = "") -> str:
        """Get string configuration value"""
        pass
    
    @abstractmethod
    def validate(self) -> bool:
        """Validate configuration completeness"""
        pass
```

#### Step 2: Create Config Class
Create a new file: `faultmaven/config/config.py`

```python
"""
Centralized configuration management.

This module provides a centralized configuration system that implements
the IConfiguration interface for type-safe configuration access.
"""

import os
from typing import Any, Dict, Optional
from dataclasses import dataclass
import logging
from faultmaven.models.interfaces import IConfiguration

@dataclass
class LLMConfig:
    """LLM provider configuration"""
    provider: str
    api_key: Optional[str]
    model: str
    max_tokens: int = 1000
    temperature: float = 0.7

@dataclass
class RedisConfig:
    """Redis configuration"""
    host: str
    port: int
    password: Optional[str]
    db: int = 0

@dataclass
class ChromaDBConfig:
    """ChromaDB configuration"""
    url: str
    api_key: Optional[str]
    collection_name: str = "faultmaven_knowledge"

@dataclass
class PresidioConfig:
    """Presidio configuration"""
    analyzer_url: str
    anonymizer_url: str
    timeout: float = 10.0

@dataclass
class LoggingConfig:
    """Logging configuration"""
    level: str
    format: str
    dedupe: bool
    buffer_size: int
    flush_interval: int

class Config(IConfiguration):
    """Centralized configuration management implementing IConfiguration"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self._load_configuration()
            self._initialized = True
    
    def _load_configuration(self):
        """Load configuration from environment variables"""
        self.logger = logging.getLogger(__name__)
        
        # LLM Configuration
        self.llm = LLMConfig(
            provider=os.getenv("CHAT_PROVIDER", "openai"),
            api_key=os.getenv(f"{os.getenv('CHAT_PROVIDER', 'OPENAI').upper()}_API_KEY"),
            model=os.getenv(f"{os.getenv('CHAT_PROVIDER', 'OPENAI').upper()}_MODEL", "gpt-4o"),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1000")),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.7"))
        )
        
        # Redis Configuration
        self.redis = RedisConfig(
            host=os.getenv("REDIS_HOST", "192.168.0.111"),
            port=int(os.getenv("REDIS_PORT", "30379")),
            password=os.getenv("REDIS_PASSWORD"),
            db=int(os.getenv("REDIS_DB", "0"))
        )
        
        # ChromaDB Configuration
        self.chromadb = ChromaDBConfig(
            url=os.getenv("CHROMADB_URL", "http://chromadb.faultmaven.local:30080"),
            api_key=os.getenv("CHROMADB_API_KEY", "faultmaven-dev-chromadb-2025"),
            collection_name=os.getenv("CHROMADB_COLLECTION", "faultmaven_knowledge")
        )
        
        # Presidio Configuration
        self.presidio = PresidioConfig(
            analyzer_url=os.getenv("PRESIDIO_ANALYZER_URL", 
                                  "http://presidio-analyzer.faultmaven.local:30080"),
            anonymizer_url=os.getenv("PRESIDIO_ANONYMIZER_URL",
                                    "http://presidio-anonymizer.faultmaven.local:30080"),
            timeout=float(os.getenv("PRESIDIO_TIMEOUT", "10.0"))
        )
        
        # Logging Configuration
        self.logging = LoggingConfig(
            level=os.getenv("LOG_LEVEL", "INFO"),
            format=os.getenv("LOG_FORMAT", "json"),
            dedupe=os.getenv("LOG_DEDUPE", "true").lower() == "true",
            buffer_size=int(os.getenv("LOG_BUFFER_SIZE", "100")),
            flush_interval=int(os.getenv("LOG_FLUSH_INTERVAL", "5"))
        )
        
        # Application settings
        self.session_timeout_minutes = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
        self.enable_migration_logging = os.getenv("ENABLE_MIGRATION_LOGGING", "false").lower() == "true"
        self.skip_service_checks = os.getenv("SKIP_SERVICE_CHECKS", "false").lower() == "true"
        
        # Environment
        self.environment = os.getenv("ENVIRONMENT", "development")
        
        self.logger.info(f"Configuration loaded for environment: {self.environment}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key using dot notation"""
        keys = key.split('.')
        value = self
        
        for k in keys:
            if hasattr(value, k):
                value = getattr(value, k)
            else:
                return default
        
        return value
    
    def get_int(self, key: str, default: int = 0) -> int:
        """Get integer configuration value"""
        value = self.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    
    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean configuration value"""
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ('true', '1', 'yes', 'on')
        return bool(value)
    
    def get_str(self, key: str, default: str = "") -> str:
        """Get string configuration value"""
        value = self.get(key, default)
        return str(value) if value is not None else default
    
    def validate(self) -> bool:
        """Validate configuration completeness"""
        errors = []
        
        # Check required LLM configuration
        if not self.llm.api_key and self.llm.provider != "local":
            errors.append(f"Missing API key for LLM provider: {self.llm.provider}")
        
        # Check Redis connectivity
        if not self.redis.host:
            errors.append("Redis host not configured")
        
        # Check ChromaDB configuration
        if not self.chromadb.url:
            errors.append("ChromaDB URL not configured")
        
        # Log errors
        for error in errors:
            self.logger.error(f"Configuration error: {error}")
        
        return len(errors) == 0
    
    def to_dict(self) -> Dict[str, Any]:
        """Export configuration as dictionary (for debugging)"""
        return {
            'llm': {
                'provider': self.llm.provider,
                'model': self.llm.model,
                'has_api_key': bool(self.llm.api_key)
            },
            'redis': {
                'host': self.redis.host,
                'port': self.redis.port
            },
            'chromadb': {
                'url': self.chromadb.url,
                'collection': self.chromadb.collection_name
            },
            'presidio': {
                'analyzer_url': self.presidio.analyzer_url,
                'anonymizer_url': self.presidio.anonymizer_url
            },
            'logging': {
                'level': self.logging.level,
                'format': self.logging.format,
                'dedupe': self.logging.dedupe
            },
            'environment': self.environment
        }

# Global configuration instance
config = Config()
```

#### Step 3: Update Components to Use Config
Example update for `faultmaven/infrastructure/security/redaction.py`:

```python
from faultmaven.config.config import config

class DataSanitizer(BaseExternalClient, ISanitizer):
    def __init__(self):
        super().__init__(...)
        
        # Use centralized config instead of os.getenv
        self.analyzer_url = config.presidio.analyzer_url
        self.anonymizer_url = config.presidio.anonymizer_url
        self.request_timeout = config.presidio.timeout
        
        # Rest of initialization...
```

---

## Implementation Priority and Timeline

### Priority 1: Quick Wins (1-2 days each)
1. **IVectorStore Implementation** - Critical for interface compliance
2. **ISessionStore Implementation** - Critical for interface compliance

### Priority 2: Architecture Enhancement (3-5 days)
3. **Tool Registry Pattern** - Improves extensibility
4. **Centralized Configuration** - Improves maintainability

## Testing Requirements

For each gap closure:

1. **Unit Tests**: Create test files for new classes
   - Test interface implementation
   - Test error handling
   - Test fallback mechanisms

2. **Integration Tests**: Verify component integration
   - Test with DI container
   - Test with actual services (Docker)
   - Test graceful degradation

3. **Documentation**: Update relevant docs
   - Update architecture diagrams
   - Update interface documentation
   - Update CLAUDE.md if needed

## Validation Checklist

After implementing each gap closure:

- [ ] Interface properly implemented
- [ ] Unit tests passing (>80% coverage)
- [ ] Integration tests passing
- [ ] Container integration verified
- [ ] Documentation updated
- [ ] No regression in existing tests
- [ ] Feature flags still work
- [ ] Health check includes new components

## Success Criteria

The gaps are considered closed when:

1. All interfaces are properly implemented
2. No direct infrastructure usage outside of infrastructure layer
3. All components registered with DI container
4. Configuration centralized and validated
5. All existing tests still pass
6. Architecture compliance score reaches 100/100

---

*Document prepared by: Senior Software Architect*  
*Date: January 2025*  
*Target completion: 2-3 sprints*