# File: faultmaven/models/interfaces.py
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List, ContextManager
from pydantic import BaseModel

# Tool interfaces
class ToolResult(BaseModel):
    """Result of a tool execution.
    
    Attributes:
        success: Whether the tool execution was successful
        data: The data returned by the tool
        error: Error message if execution failed
    """
    success: bool
    data: Any
    error: Optional[str] = None

class BaseTool(ABC):
    """Abstract base class for all agent tools.
    
    This interface defines the contract that all tools must implement
    to be used within the FaultMaven agent system. Tools provide specific
    capabilities like knowledge base search, web search, data analysis,
    and system interactions that extend the agent's problem-solving abilities.
    
    Tool Requirements:
        - Deterministic execution with consistent results
        - Graceful error handling with informative messages
        - Async execution support for I/O operations
        - Schema-based parameter validation
        
    Integration Notes:
        - Tools are automatically registered via the tool registry
        - Agent selects appropriate tools based on schemas and context
        - Tool outputs are sanitized before LLM processing
        - Execution timeouts prevent hanging operations
    """
    
    @abstractmethod
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Execute the tool with the given parameters.
        
        This method performs the tool's core functionality using the
        provided parameters. Implementations should handle parameter
        validation, execute the operation, and return structured results.
        
        Args:
            params: Dictionary of parameters for tool execution. Parameters
                    must conform to the tool's schema definition. Common
                    parameter patterns:
                    - 'query': Search or analysis query string
                    - 'data': Input data for processing
                    - 'options': Configuration options for execution
                    - 'timeout': Maximum execution time in seconds
                    
        Returns:
            ToolResult containing the execution outcome:
            - success: Boolean indicating if execution succeeded
            - data: Tool output data (search results, analysis, etc.)
            - error: Error message if execution failed (None if successful)
            
        Raises:
            ToolExecutionException: When tool execution fails critically
            ValidationException: When parameters don't match schema
            TimeoutException: When execution exceeds configured timeout
            
        Example:
            Knowledge base search tool:
            
            >>> params = {
                'query': 'database connection timeout',
                'limit': 5,
                'document_type': 'troubleshooting'
            }
            >>> result = await kb_tool.execute(params)
            >>> result.success
            True
            >>> len(result.data['results'])
            3
            
            Web search tool:
            
            >>> params = {
                'query': 'Redis connection pool exhausted',
                'max_results': 10
            }
            >>> result = await web_tool.execute(params)
            >>> if result.success:
                print(f"Found {len(result.data)} results")
            Found 8 results
            
            Error handling:
            
            >>> params = {'invalid_param': 'value'}
            >>> result = await tool.execute(params)
            >>> result.success
            False
            >>> 'required parameter' in result.error
            True
            
        Note:
            Parameter validation should occur before expensive operations.
            Long-running operations should support cancellation.
            Output data is automatically sanitized for PII before LLM processing.
            Tools should be stateless for thread safety.
        """
        pass
    
    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """Get the tool's schema definition for parameter validation.
        
        This method returns a JSON Schema that describes the tool's
        parameters, capabilities, and usage patterns. The agent uses
        this schema for tool selection and parameter validation.
        
        Returns:
            Dictionary containing the tool's schema in JSON Schema format:
            {
                'name': str,           # Tool identifier
                'description': str,    # Human-readable description
                'parameters': {        # JSON Schema for parameters
                    'type': 'object',
                    'properties': {...},
                    'required': [...]
                },
                'examples': [...],     # Usage examples
                'capabilities': [...]  # List of tool capabilities
            }
            
        Example:
            Knowledge base tool schema:
            
            >>> schema = kb_tool.get_schema()
            >>> schema['name']
            'knowledge_base_search'
            >>> schema['description']
            'Search the knowledge base for troubleshooting information'
            >>> 'query' in schema['parameters']['properties']
            True
            >>> 'required' in schema['parameters']
            True
            
            Web search tool schema:
            
            >>> schema = web_tool.get_schema()
            >>> schema['capabilities']
            ['web_search', 'real_time_info', 'external_resources']
            >>> len(schema['examples'])
            3
            
        Note:
            Schema should be static and deterministic.
            Examples in schema help the agent understand tool usage.
            Parameter descriptions should be clear and specific.
            Schema validation is performed automatically before execution.
        """
        pass

# Infrastructure interfaces
class ILLMProvider(ABC):
    """Interface for Large Language Model providers.
    
    This interface abstracts interactions with different LLM providers (OpenAI, 
    Anthropic, Fireworks, etc.) allowing the system to switch between providers 
    without code changes. Implementations must handle provider-specific authentication,
    rate limiting, and error handling.
    
    Thread Safety:
        Implementations must be thread-safe for concurrent request handling.
        
    Performance Notes:
        Methods should implement appropriate timeout and retry mechanisms.
        Consider caching strategies for improved performance.
    """
    
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate text response using the configured LLM provider.
        
        This method sends a prompt to the LLM provider and returns the generated
        response. The implementation handles provider-specific request formatting,
        authentication, error handling, and response parsing.
        
        Args:
            prompt: The input text prompt for generation. Must be non-empty string.
                   Length limits vary by provider (typically 8K-32K tokens).
            **kwargs: Provider-specific parameters such as:
                     - temperature: Response randomness (0.0-2.0)
                     - max_tokens: Maximum response length
                     - model: Specific model to use (overrides default)
                     - timeout: Request timeout in seconds
                     
        Returns:
            Generated text response from the LLM provider. Response length
            depends on provider limits and max_tokens parameter. Empty responses
            indicate generation failure or filtering.
            
        Raises:
            LLMProviderException: When provider request fails (auth, network, etc.)
            ValidationException: When prompt is invalid or too long
            TimeoutException: When request exceeds configured timeout
            RateLimitException: When provider rate limits are exceeded
            
        Example:
            Basic text generation:
            
            >>> provider = OpenAIProvider()
            >>> response = await provider.generate("Explain quantum computing")
            >>> print(len(response))
            1247
            
            Advanced generation with parameters:
            
            >>> response = await provider.generate(
                "Write a Python function for sorting", 
                temperature=0.1,
                max_tokens=500
            )
            >>> "def " in response
            True
            
        Note:
            Implementations should sanitize prompts to remove PII before sending
            to external providers. Rate limiting and cost management should be
            handled transparently.
        """
        pass

class ITracer(ABC):
    """Interface for distributed tracing and observability systems.
    
    This interface provides observability into system operations through
    distributed tracing capabilities. Implementations integrate with tracing
    systems like Jaeger, Zipkin, or Opik to provide request flow visibility,
    performance monitoring, and debugging capabilities.
    
    Tracing Requirements:
        - Minimal performance overhead (<1% of request time)
        - Context propagation across async boundaries
        - Structured span data with standardized tags
        
    Integration Notes:
        - Works with correlation IDs from logging system
        - Supports sampling for high-volume environments
        - Graceful degradation when tracing unavailable
    """
    
    @abstractmethod
    def trace(self, operation: str) -> ContextManager:
        """Create a trace context for monitoring an operation.
        
        This method creates a tracing span for monitoring the performance and
        execution flow of a specific operation. The span captures timing,
        success/failure status, and relevant metadata for observability.
        
        Args:
            operation: Name of the operation being traced. Should follow
                      hierarchical naming convention like "service.method" or
                      "layer.component.action". Examples: "agent.process_query",
                      "data.classify", "llm.generate_response".
                      
        Returns:
            Context manager that automatically handles span lifecycle.
            When entering context, starts the span with timestamp.
            When exiting context, finalizes span with duration and status.
            Exception handling records errors in span metadata.
            
        Raises:
            TracingException: When tracing system is unavailable and failover fails
            ConfigurationException: When tracing configuration is invalid
            
        Example:
            Basic operation tracing:
            
            >>> tracer = OpikTracer()
            >>> with tracer.trace("user.authenticate") as span:
                # Operation code here
                result = authenticate_user(credentials)
                span.set_attribute("user_id", result.user_id)
                span.set_attribute("auth_method", "oauth")
            
            Async operation tracing:
            
            >>> async def process_data():
                with tracer.trace("data.processing") as span:
                    data = await fetch_data()
                    span.set_attribute("record_count", len(data))
                    return await process_records(data)
            
            Error handling with tracing:
            
            >>> with tracer.trace("external.api_call") as span:
                try:
                    response = await external_api.call()
                    span.set_attribute("status_code", response.status)
                except APIException as e:
                    span.set_attribute("error", str(e))
                    span.set_status("error")
                    raise
            
        Note:
            Spans are automatically linked to parent spans when nested.
            Correlation IDs from request context are automatically included.
            High-frequency operations should use sampling to reduce overhead.
        """
        pass

class ISanitizer(ABC):
    """Interface for data sanitization and PII redaction services.
    
    This interface provides privacy-first data processing capabilities, ensuring
    that sensitive information is properly redacted before external processing.
    Implementations must handle various PII types including emails, phone numbers,
    credit cards, SSNs, and custom sensitive patterns.
    
    Privacy Requirements:
        - All sensitive data must be consistently redacted
        - Redaction must be reversible for authorized users only
        - Original data must never be logged or exposed
        
    Performance Requirements:
        - Sanitization should complete within 100ms for typical documents
        - Memory usage should remain constant regardless of document size
    """
    
    @abstractmethod
    def sanitize(self, data: Any) -> Any:
        """Sanitize data by removing or redacting sensitive information.
        
        This method processes input data to identify and redact personally
        identifiable information (PII) and other sensitive content. The
        sanitization process preserves data structure and utility while
        ensuring privacy compliance.
        
        Args:
            data: Input data to be sanitized. Supported types include:
                  - str: Text content with potential PII
                  - dict: Structured data with nested content
                  - list: Collections of sanitizable items
                  - bytes: Binary content (limited support)
                  Complex objects are serialized before processing.
                  
        Returns:
            Sanitized version of input data with same structure but PII
            replaced with redaction markers (e.g., "[EMAIL_REDACTED]",
            "[PHONE_REDACTED]"). Redaction markers preserve data type and
            approximate length for utility preservation.
            
        Raises:
            SanitizationException: When sanitization process fails
            UnsupportedDataTypeException: When data type cannot be processed
            ConfigurationException: When sanitization rules are invalid
            
        Example:
            Text sanitization:
            
            >>> sanitizer = PresidioSanitizer()
            >>> result = sanitizer.sanitize("Contact john@example.com for help")
            >>> print(result)
            'Contact [EMAIL_REDACTED] for help'
            
            Structured data sanitization:
            
            >>> data = {"user": "jane@company.com", "age": 25, "notes": "Call 555-1234"}
            >>> result = sanitizer.sanitize(data)
            >>> result["user"]
            '[EMAIL_REDACTED]'
            >>> result["age"]
            25
            >>> result["notes"]
            'Call [PHONE_REDACTED]'
            
        Note:
            Sanitization rules can be configured per-environment. Development
            environments may use less aggressive sanitization for debugging.
            Production environments must use comprehensive PII detection.
        """
        pass

class IVectorStore(ABC):
    """Interface for vector database operations.
    
    This interface abstracts vector database operations for knowledge base 
    storage and retrieval using semantic similarity search. Implementations
    should handle document chunking, embedding generation, and efficient
    similarity queries for RAG (Retrieval-Augmented Generation) workflows.
    
    Performance Requirements:
        - Sub-second search response times for typical queries
        - Support for concurrent read/write operations
        - Efficient memory usage for large document collections
        
    Scalability Notes:
        - Should handle collections with 100K+ documents
        - Automatic index optimization for query performance
        - Batch operations for bulk document processing
    """
    
    @abstractmethod
    async def add_documents(self, documents: List[Dict]) -> None:
        """Add documents to the vector store with automatic embedding generation.
        
        This method processes documents by generating embeddings using the
        configured embedding model and storing them in the vector database.
        Documents are automatically chunked if they exceed size limits.
        
        Args:
            documents: List of document dictionaries to add. Each document must
                      contain at minimum:
                      - 'id': Unique document identifier (str)
                      - 'content': Document text content (str)
                      - 'metadata': Optional metadata dict with additional fields
                      
        Raises:
            VectorStoreException: When document addition fails
            ValidationException: When document format is invalid
            EmbeddingException: When embedding generation fails
            
        Example:
            Basic document addition:
            
            >>> docs = [
                {
                    "id": "doc_001",
                    "content": "This is a troubleshooting guide for database issues.",
                    "metadata": {"type": "troubleshooting", "category": "database"}
                }
            ]
            >>> await vector_store.add_documents(docs)
            
        Note:
            Large documents are automatically chunked into smaller segments.
            Embeddings are generated using the configured BGE-M3 model.
            Duplicate document IDs will update existing documents.
        """
        pass
    
    @abstractmethod
    async def search(self, query: str, k: int = 5) -> List[Dict]:
        """Search for semantically similar documents using vector similarity.
        
        This method generates an embedding for the query text and performs
        similarity search against stored document embeddings. Results are
        ranked by cosine similarity score.
        
        Args:
            query: Search query text. Should be a natural language question
                   or description of the information needed.
            k: Number of most similar results to return. Must be positive
               integer, typically between 1-20 for optimal performance.
               
        Returns:
            List of similar documents ordered by relevance (highest first).
            Each result contains:
            - 'id': Document identifier
            - 'content': Relevant document text chunk
            - 'metadata': Document metadata including similarity score
            - 'score': Similarity score (0.0-1.0, higher is more similar)
            
        Raises:
            VectorStoreException: When search operation fails
            EmbeddingException: When query embedding generation fails
            
        Example:
            Basic similarity search:
            
            >>> results = await vector_store.search("database connection errors", k=3)
            >>> for result in results:
                print(f"Score: {result['score']:.3f}, ID: {result['id']}")
            Score: 0.892, ID: doc_001
            Score: 0.745, ID: doc_015
            Score: 0.621, ID: doc_032
            
            Accessing result content:
            
            >>> top_result = results[0]
            >>> print(top_result['content'][:100])
            'This troubleshooting guide covers common database connection issues including timeout errors...'
            
        Note:
            Search uses semantic similarity, not exact keyword matching.
            Results include chunk-level content for large documents.
            Metadata filtering can be implemented in concrete classes.
        """
        pass
    
    @abstractmethod
    async def delete_documents(self, ids: List[str]) -> None:
        """Delete documents from the vector store by their identifiers.
        
        This method removes documents and their associated embeddings from
        the vector database. For large documents that were chunked, all
        chunks are removed.
        
        Args:
            ids: List of document identifiers to delete. Must be strings
                 matching existing document IDs in the store.
                 
        Raises:
            VectorStoreException: When deletion operation fails
            ValidationException: When document IDs are invalid format
            
        Example:
            Delete multiple documents:
            
            >>> await vector_store.delete_documents(["doc_001", "doc_002"])
            
            Delete single document:
            
            >>> await vector_store.delete_documents(["outdated_guide"])
            
        Note:
            Non-existent document IDs are silently ignored.
            Deletion is permanent and cannot be undone.
            For large collections, batch deletions are more efficient.
        """
        pass

class ISessionStore(ABC):
    """Interface for session storage operations.
    
    This interface abstracts session storage operations for maintaining user
    session state in a distributed system. Implementations should provide
    reliable session persistence with automatic expiration, supporting
    concurrent access and high availability scenarios.
    
    Performance Requirements:
        - Sub-10ms response times for session operations
        - Support for thousands of concurrent sessions
        - Automatic cleanup of expired sessions
        
    Reliability Requirements:
        - Atomic operations for session updates
        - Consistent session state across distributed nodes
        - Graceful handling of storage failures
    """
    
    @abstractmethod
    async def get(self, key: str) -> Optional[Dict]:
        """Retrieve session data by session identifier.
        
        This method fetches session data from the persistent store,
        automatically handling deserialization and TTL validation.
        
        Args:
            key: Session identifier (typically UUID). Must be non-empty
                 string following session ID format (e.g., UUID v4).
                 
        Returns:
            Dictionary containing session data if session exists and
            hasn't expired, None if session not found or expired.
            Session data includes:
            - 'session_id': Session identifier
            - 'user_id': Associated user identifier (optional)
            - 'created_at': Session creation timestamp (ISO format)
            - 'last_activity': Last access timestamp (ISO format)
            - 'data_uploads': List of uploaded data references
            - 'investigation_history': List of troubleshooting steps
            
        Raises:
            SessionStoreException: When storage operation fails
            ValidationException: When session key format is invalid
            
        Example:
            Retrieve existing session:
            
            >>> session_data = await session_store.get("550e8400-e29b-41d4-a716-446655440000")
            >>> if session_data:
                print(f"User: {session_data.get('user_id')}")
                print(f"Created: {session_data['created_at']}")
            User: user_123
            Created: 2025-01-15T10:30:00Z
            
            Handle missing session:
            
            >>> session_data = await session_store.get("nonexistent-session")
            >>> session_data is None
            True
            
        Note:
            Expired sessions are automatically removed and return None.
            Session access does not update the last_activity timestamp.
            Use extend_ttl() to refresh session expiration.
        """
        pass
    
    @abstractmethod
    async def set(self, key: str, value: Dict, ttl: Optional[int] = None) -> None:
        """Store session data with automatic expiration.
        
        This method persists session data to the storage backend with
        automatic serialization and TTL management. Updates to existing
        sessions preserve the session structure.
        
        Args:
            key: Session identifier. Must be unique string, typically UUID.
            value: Session data dictionary containing session state.
                   Required fields:
                   - 'session_id': Must match the key parameter
                   - 'created_at': Session creation timestamp
                   Optional fields:
                   - 'user_id': Associated user identifier
                   - 'last_activity': Last access timestamp
                   - 'data_uploads': List of data references
                   - 'investigation_history': Troubleshooting history
            ttl: Time to live in seconds. If None, uses default session
                 timeout from configuration (typically 30 minutes).
                 
        Raises:
            SessionStoreException: When storage operation fails
            ValidationException: When session data format is invalid
            SerializationException: When data cannot be serialized
            
        Example:
            Create new session:
            
            >>> session_data = {
                'session_id': '550e8400-e29b-41d4-a716-446655440000',
                'user_id': 'user_123',
                'created_at': '2025-01-15T10:30:00Z',
                'last_activity': '2025-01-15T10:30:00Z',
                'data_uploads': [],
                'investigation_history': []
            }
            >>> await session_store.set(session_data['session_id'], session_data, ttl=1800)
            
            Update existing session:
            
            >>> session_data['last_activity'] = '2025-01-15T11:00:00Z'
            >>> session_data['data_uploads'].append('data_upload_456')
            >>> await session_store.set(session_data['session_id'], session_data)
            
        Note:
            Setting a session with existing key overwrites previous data.
            TTL countdown starts from the time this method is called.
            Session data is automatically serialized (typically as JSON).
        """
        pass
    
    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Remove session from storage.
        
        This method permanently deletes session data from the storage
        backend. Once deleted, session data cannot be recovered.
        
        Args:
            key: Session identifier to delete. Must be valid session ID.
            
        Returns:
            True if session was successfully deleted, False if session
            did not exist or was already deleted.
            
        Raises:
            SessionStoreException: When deletion operation fails
            
        Example:
            Delete existing session:
            
            >>> deleted = await session_store.delete("550e8400-e29b-41d4-a716-446655440000")
            >>> deleted
            True
            
            Attempt to delete non-existent session:
            
            >>> deleted = await session_store.delete("nonexistent-session")
            >>> deleted
            False
            
        Note:
            Deletion is idempotent - multiple calls with same key are safe.
            This operation cannot be undone.
            Consider using TTL expiration for temporary session removal.
        """
        pass
    
    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if session exists without retrieving data.
        
        This method efficiently checks session existence without the
        overhead of retrieving and deserializing session data.
        
        Args:
            key: Session identifier to check. Must be valid session ID format.
            
        Returns:
            True if session exists and hasn't expired, False otherwise.
            
        Raises:
            SessionStoreException: When existence check fails
            
        Example:
            Check session existence:
            
            >>> exists = await session_store.exists("550e8400-e29b-41d4-a716-446655440000")
            >>> if exists:
                session_data = await session_store.get(key)
                # Process session data
            
            Validate session before operations:
            
            >>> if await session_store.exists(session_id):
                await session_store.extend_ttl(session_id, 3600)
            else:
                # Create new session
                await create_new_session(session_id)
            
        Note:
            This is more efficient than get() when only existence matters.
            Expired sessions return False.
            Result may change between exists() and subsequent get() calls.
        """
        pass
    
    @abstractmethod
    async def extend_ttl(self, key: str, ttl: Optional[int] = None) -> bool:
        """Extend session expiration time.
        
        This method refreshes the session TTL without modifying session
        data, effectively extending the session lifetime. Useful for
        keeping active sessions alive.
        
        Args:
            key: Session identifier to extend. Must be existing session ID.
            ttl: New TTL in seconds from now. If None, uses default
                 session timeout from configuration.
                 
        Returns:
            True if TTL was successfully extended, False if session
            does not exist or has already expired.
            
        Raises:
            SessionStoreException: When TTL extension fails
            ValidationException: When TTL value is invalid
            
        Example:
            Extend session by default timeout:
            
            >>> extended = await session_store.extend_ttl("550e8400-e29b-41d4-a716-446655440000")
            >>> extended
            True
            
            Extend session by specific duration:
            
            >>> # Extend for 2 hours
            >>> extended = await session_store.extend_ttl(session_id, ttl=7200)
            >>> extended
            True
            
            Handle expired session:
            
            >>> extended = await session_store.extend_ttl("expired-session")
            >>> extended
            False
            
        Note:
            TTL extension does not modify session data or last_activity.
            This operation is atomic and safe for concurrent access.
            Use this for session keep-alive functionality.
        """
        pass


class IConfiguration(ABC):
    """Interface for centralized configuration management.
    
    This interface provides type-safe access to application configuration
    with validation, defaults, and environment-specific overrides.
    Implementations should handle environment variables, configuration
    files, and runtime validation.
    
    Design Principles:
        - Type safety with automatic conversion
        - Validation at startup with clear error messages
        - Environment-specific configuration support
        - Immutable configuration once loaded
        
    Security Notes:
        - Sensitive values should be masked in logs
        - Configuration validation should prevent injection attacks
        - Access control for different configuration sections
    """
    
    @abstractmethod
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key with optional default.
        
        This method retrieves configuration values using dot notation
        for nested keys (e.g., 'database.host'). Values are returned
        in their stored type without conversion.
        
        Args:
            key: Configuration key using dot notation for nested access.
                 Examples: 'redis.host', 'llm.provider', 'logging.level'
            default: Default value returned if key is not found.
                     Type should match expected configuration value type.
                     
        Returns:
            Configuration value if found, otherwise the default value.
            Return type matches the stored configuration type.
            
        Example:
            Basic configuration access:
            
            >>> config = ConfigurationManager()
            >>> redis_host = config.get('redis.host', 'localhost')
            >>> print(redis_host)
            'redis.production.local'
            
            Nested configuration access:
            
            >>> llm_config = config.get('llm', {})
            >>> print(llm_config['provider'])
            'openai'
            
        Note:
            This method does not perform type conversion.
            Use get_int(), get_bool(), get_str() for type-safe access.
            Dot notation supports arbitrary nesting depth.
        """
        pass
    
    @abstractmethod
    def get_int(self, key: str, default: int = 0) -> int:
        """Get integer configuration value with type conversion.
        
        This method retrieves configuration values and converts them
        to integers, handling string representations and type validation.
        
        Args:
            key: Configuration key for the integer value.
            default: Default integer value if key not found or conversion fails.
                     Must be a valid integer.
                     
        Returns:
            Integer value from configuration or default if conversion fails.
            
        Raises:
            ConfigurationException: When key exists but cannot be converted to int
            
        Example:
            Port configuration:
            
            >>> port = config.get_int('redis.port', 6379)
            >>> isinstance(port, int)
            True
            >>> port
            6379
            
            Timeout configuration:
            
            >>> timeout = config.get_int('session.timeout_minutes', 30)
            >>> timeout > 0
            True
            
        Note:
            Conversion handles string representations (e.g., '8080' -> 8080).
            Boolean True/False converts to 1/0 respectively.
            Invalid values return the default without raising exceptions.
        """
        pass
    
    @abstractmethod
    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean configuration value with type conversion.
        
        This method retrieves configuration values and converts them
        to booleans, handling various string representations.
        
        Args:
            key: Configuration key for the boolean value.
            default: Default boolean value if key not found or conversion fails.
                     
        Returns:
            Boolean value from configuration or default if conversion fails.
            Conversion rules:
            - Strings: 'true', '1', 'yes', 'on' -> True (case insensitive)
            - Strings: 'false', '0', 'no', 'off' -> False (case insensitive)
            - Numbers: 0 -> False, non-zero -> True
            - Boolean: returned as-is
            
        Example:
            Feature flag configuration:
            
            >>> debug_mode = config.get_bool('debug.enabled', False)
            >>> isinstance(debug_mode, bool)
            True
            
            Environment-specific settings:
            
            >>> use_ssl = config.get_bool('redis.ssl', False)
            >>> if use_ssl:
                print('Using SSL connection')
            
        Note:
            String conversion is case-insensitive.
            Empty strings and None values return the default.
            Invalid values return the default without raising exceptions.
        """
        pass
    
    @abstractmethod
    def get_str(self, key: str, default: str = "") -> str:
        """Get string configuration value with type conversion.
        
        This method retrieves configuration values and converts them
        to strings, handling various data types safely.
        
        Args:
            key: Configuration key for the string value.
            default: Default string value if key not found.
                     
        Returns:
            String representation of configuration value or default.
            Conversion rules:
            - None values return the default
            - All other types converted using str()
            
        Example:
            Database connection string:
            
            >>> db_host = config.get_str('database.host', 'localhost')
            >>> isinstance(db_host, str)
            True
            
            API endpoint configuration:
            
            >>> api_url = config.get_str('llm.api_url')
            >>> api_url.startswith('https://')
            True
            
        Note:
            Numbers and booleans are converted to their string representation.
            This method always returns a string (never None).
            Empty configuration values return empty string unless default provided.
        """
        pass
    
    @abstractmethod
    def validate(self) -> bool:
        """Validate configuration completeness and correctness.
        
        This method performs comprehensive validation of all configuration
        sections, checking for required values, valid formats, and
        constraint compliance. Should be called at application startup.
        
        Returns:
            True if all configuration is valid and complete,
            False if validation errors are found.
            
        Validation Checks:
            - Required configuration keys are present
            - Values meet format requirements (URLs, ports, etc.)
            - Numeric values are within acceptable ranges
            - API keys and credentials are properly formatted
            - Cross-section consistency (e.g., matching timeouts)
            
        Example:
            Startup validation:
            
            >>> config = ConfigurationManager()
            >>> if not config.validate():
                print('Configuration validation failed!')
                sys.exit(1)
            >>> print('Configuration is valid')
            Configuration is valid
            
            Development environment check:
            
            >>> try:
                config = ConfigurationManager(validate_on_init=True)
            except ConfigurationException as e:
                print(f'Config error: {e}')
                # Handle configuration error
            
        Note:
            Validation errors are logged with specific details.
            This method should be idempotent (safe to call multiple times).
            Failed validation typically indicates deployment/environment issues.
        """
        pass


# Data processing interfaces
class IDataClassifier(ABC):
    """Interface for data type classification.
    
    This interface abstracts data classification operations for automatic
    identification of data types in troubleshooting uploads. Implementations
    should analyze content patterns, file extensions, and metadata to
    determine appropriate processing strategies.
    
    Classification Categories:
        - Log files (application, system, web server)
        - Configuration files (JSON, YAML, INI, XML)
        - Code files (Python, JavaScript, SQL, etc.)
        - Data files (CSV, JSON, database dumps)
        - Documentation (text, markdown, etc.)
        
    Performance Requirements:
        - Classification should complete within 100ms for typical files
        - Memory usage should be constant regardless of file size
        - Support for files up to 100MB in size
    """
    
    @abstractmethod
    async def classify(self, content: str, filename: Optional[str] = None) -> 'DataType':
        """Classify data content and return appropriate DataType.
        
        This method analyzes file content and optional filename to determine
        the most appropriate data type classification. The classification
        influences downstream processing and analysis strategies.
        
        Args:
            content: The data content to classify. Should be string representation
                     of the file content. Binary files should be converted to
                     appropriate string representation (e.g., base64, hex dump).
            filename: Optional filename hint for classification. Used for
                      extension-based classification and disambiguation.
                      Examples: 'app.log', 'config.json', 'script.py'
                      
        Returns:
            DataType enum value representing the classified type.
            Common return values:
            - DataType.LOG_FILE: Application or system logs
            - DataType.CONFIG_FILE: Configuration files
            - DataType.CODE_FILE: Source code files
            - DataType.DATA_FILE: Structured data files
            - DataType.UNKNOWN: Unrecognizable content
            
        Raises:
            ClassificationException: When classification process fails
            ValidationException: When content format is invalid
            
        Example:
            Log file classification:
            
            >>> content = "2025-01-15 10:30:00 [ERROR] Database connection failed"
            >>> data_type = await classifier.classify(content, "app.log")
            >>> data_type
            DataType.LOG_FILE
            
            Configuration file classification:
            
            >>> content = '{"database": {"host": "localhost", "port": 5432}}'
            >>> data_type = await classifier.classify(content, "config.json")
            >>> data_type
            DataType.CONFIG_FILE
            
            Content-based classification without filename:
            
            >>> content = "SELECT * FROM users WHERE active = 1;"
            >>> data_type = await classifier.classify(content)
            >>> data_type
            DataType.CODE_FILE
            
        Note:
            Classification uses both content analysis and filename hints.
            Content analysis takes precedence over filename extensions.
            Large files are analyzed using representative samples.
            Classification confidence scores may be available in metadata.
        """
        pass

class ILogProcessor(ABC):
    """Interface for log processing and analysis operations.
    
    This interface abstracts log analysis and insight extraction for
    troubleshooting workflows. Implementations should parse log entries,
    extract patterns, identify errors, and generate actionable insights
    for debugging and root cause analysis.
    
    Processing Capabilities:
        - Error pattern detection and categorization
        - Performance metric extraction (response times, throughput)
        - Timeline reconstruction for incident analysis
        - Anomaly detection and trend analysis
        - Stack trace parsing and error correlation
        
    Performance Requirements:
        - Process up to 10MB log files within 30 seconds
        - Memory-efficient streaming for large files
        - Parallel processing for multi-file analysis
    """
    
    @abstractmethod
    async def process(self, content: str, data_type: Optional['DataType'] = None) -> Dict[str, Any]:
        """Process log content and extract actionable insights.
        
        This method analyzes log content to identify errors, patterns,
        performance issues, and other insights relevant for troubleshooting.
        Processing strategy adapts based on the detected or provided data type.
        
        Args:
            content: The log content to process. Should be text-based log data
                     with entries separated by newlines. Binary logs should
                     be converted to text representation before processing.
            data_type: Optional data type hint for processing optimization.
                       If provided, enables type-specific parsing rules.
                       Examples: DataType.LOG_FILE, DataType.CONFIG_FILE
                       
        Returns:
            Dictionary containing extracted insights and metadata:
            {
                'summary': {
                    'total_entries': int,  # Total number of log entries
                    'error_count': int,    # Number of error-level entries
                    'warning_count': int,  # Number of warning-level entries
                    'time_range': {        # Log time span
                        'start': 'ISO timestamp',
                        'end': 'ISO timestamp'
                    }
                },
                'errors': [             # List of identified errors
                    {
                        'timestamp': 'ISO timestamp',
                        'level': 'ERROR|CRITICAL',
                        'message': 'Error description',
                        'category': 'database|network|auth|...',
                        'stack_trace': 'Optional stack trace',
                        'frequency': int  # Occurrence count
                    }
                ],
                'patterns': [           # Detected patterns
                    {
                        'pattern': 'Pattern description',
                        'frequency': int,
                        'confidence': float,  # 0.0-1.0
                        'impact': 'low|medium|high'
                    }
                ],
                'recommendations': [    # Actionable recommendations
                    {
                        'type': 'fix|investigation|monitoring',
                        'priority': 'low|medium|high|critical',
                        'description': 'Recommendation text',
                        'related_errors': ['error_id_1', 'error_id_2']
                    }
                ]
            }
            
        Raises:
            LogProcessingException: When log processing fails
            ValidationException: When log format is unrecognizable
            TimeoutException: When processing exceeds time limits
            
        Example:
            Application log processing:
            
            >>> log_content = '''
            2025-01-15 10:30:00 [INFO] Application started
            2025-01-15 10:30:15 [ERROR] Database connection failed: timeout
            2025-01-15 10:30:16 [ERROR] Database connection failed: timeout
            2025-01-15 10:30:30 [WARN] Retrying database connection
            '''
            >>> result = await processor.process(log_content, DataType.LOG_FILE)
            >>> result['summary']['error_count']
            2
            >>> result['errors'][0]['category']
            'database'
            
            Web server log processing:
            
            >>> access_log = '''
            192.168.1.1 - - [15/Jan/2025:10:30:00 +0000] "GET /api/health" 200 85
            192.168.1.2 - - [15/Jan/2025:10:30:01 +0000] "POST /api/login" 401 45
            '''
            >>> result = await processor.process(access_log)
            >>> len(result['patterns'])
            1
            >>> result['recommendations'][0]['type']
            'investigation'
            
        Note:
            Processing is adaptive based on detected log format.
            Large logs are processed in chunks to manage memory usage.
            Error correlation identifies related failure patterns.
            Recommendations are prioritized by impact and confidence.
        """
        pass

class IStorageBackend(ABC):
    """Interface for data storage operations.
    
    This interface abstracts storage backend operations for persisting
    user data, analysis results, and temporary files. Implementations
    should provide reliable data persistence with appropriate access
    controls and data lifecycle management.
    
    Storage Requirements:
        - Atomic operations for data consistency
        - Support for various data types (text, binary, structured)
        - Configurable retention policies
        - Encryption at rest for sensitive data
        
    Performance Requirements:
        - Sub-second response times for typical operations
        - Support for concurrent access
        - Efficient storage utilization
    """
    
    @abstractmethod
    async def store(self, key: str, data: Any) -> None:
        """Store data with given key and automatic serialization.
        
        This method persists data to the storage backend with automatic
        serialization based on data type. Supports text, binary, and
        structured data with appropriate encoding.
        
        Args:
            key: Unique identifier for the data. Should follow naming
                 convention like 'session_id/data_type/timestamp'.
                 Examples: 'user123/upload/20250115_103000',
                          'session456/analysis/results'
            data: Data to be stored. Supported types:
                  - str: Text data (UTF-8 encoded)
                  - bytes: Binary data (stored as-is)
                  - dict/list: JSON-serializable structures
                  - Custom objects with __dict__ method
                  
        Raises:
            StorageException: When storage operation fails
            SerializationException: When data cannot be serialized
            ValidationException: When key format is invalid
            
        Example:
            Store text analysis results:
            
            >>> analysis_results = {
                'insights': ['Database timeout detected', 'Connection pool exhausted'],
                'severity': 'high',
                'timestamp': '2025-01-15T10:30:00Z'
            }
            >>> await storage.store('session123/analysis/log_analysis', analysis_results)
            
            Store uploaded file content:
            
            >>> file_content = "Error log content here..."
            >>> await storage.store('session123/uploads/error.log', file_content)
            
        Note:
            Storing with existing key overwrites previous data.
            Large data objects are automatically compressed.
            Storage keys should be session-scoped for data isolation.
        """
        pass
    
    @abstractmethod
    async def retrieve(self, key: str) -> Optional[Any]:
        """Retrieve data by key with automatic deserialization.
        
        This method fetches data from the storage backend and automatically
        deserializes it to the original data type based on stored metadata.
        
        Args:
            key: Unique identifier for the data to retrieve.
                 Must match a key from a previous store() operation.
                 
        Returns:
            Retrieved data in its original type if found, None if key
            does not exist or data has expired. Data type matches
            what was originally stored:
            - str for text data
            - bytes for binary data
            - dict/list for JSON structures
            
        Raises:
            StorageException: When retrieval operation fails
            DeserializationException: When stored data cannot be deserialized
            
        Example:
            Retrieve analysis results:
            
            >>> results = await storage.retrieve('session123/analysis/log_analysis')
            >>> if results:
                print(f"Severity: {results['severity']}")
                print(f"Insights: {len(results['insights'])}")
            Severity: high
            Insights: 2
            
            Retrieve uploaded file:
            
            >>> file_content = await storage.retrieve('session123/uploads/error.log')
            >>> if file_content:
                lines = file_content.split('\n')
                print(f"File has {len(lines)} lines")
            File has 1247 lines
            
            Handle missing data:
            
            >>> missing_data = await storage.retrieve('nonexistent/key')
            >>> missing_data is None
            True
            
        Note:
            Data expiration is handled automatically based on TTL policies.
            Deserialization preserves original data types and structure.
            Access control may restrict data retrieval based on session context.
        """
        pass

class IKnowledgeIngester(ABC):
    """Interface for knowledge base document ingestion operations.
    
    This interface abstracts knowledge ingestion operations for building
    and maintaining a searchable knowledge base. Implementations should
    handle document processing, chunking, embedding generation, and
    indexing for efficient retrieval in RAG workflows.
    
    Processing Pipeline:
        1. Document validation and preprocessing
        2. Content chunking for optimal retrieval
        3. Embedding generation using BGE-M3 model
        4. Vector storage and indexing
        5. Metadata enrichment and categorization
        
    Document Types:
        - Troubleshooting guides and runbooks
        - API documentation and references
        - Configuration templates and examples
        - Error catalogs and solution databases
        - Best practices and methodology guides
    """
    
    @abstractmethod
    async def ingest_document(
        self, 
        title: str, 
        content: str, 
        document_type: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Ingest document into knowledge base with full processing pipeline.
        
        This method processes a document through the complete ingestion
        pipeline including validation, chunking, embedding generation,
        and storage. The document becomes immediately searchable.
        
        Args:
            title: Document title for display and search. Should be descriptive
                   and include key terms for discoverability.
                   Examples: "Database Connection Troubleshooting Guide",
                            "Redis Configuration Best Practices"
            content: Full document content in text format. Supports markdown,
                     plain text, and structured formats. Large documents
                     are automatically chunked for optimal retrieval.
            document_type: Document category for filtering and organization.
                          Standard types: 'troubleshooting', 'configuration',
                          'api_reference', 'best_practices', 'runbook'
            metadata: Optional metadata dictionary containing:
                      - 'author': Document author/contributor
                      - 'version': Document version identifier
                      - 'tags': List of searchable tags
                      - 'source_url': Original document URL
                      - 'last_updated': Last modification timestamp
                      - 'difficulty': 'beginner|intermediate|advanced'
                      
        Returns:
            Unique document identifier assigned by the system.
            Format: 'doc_' followed by alphanumeric ID (e.g., 'doc_a1b2c3d4')
            This ID is used for updates, deletions, and reference tracking.
            
        Raises:
            IngestionException: When document processing fails
            ValidationException: When document format is invalid
            DuplicateDocumentException: When document already exists
            EmbeddingException: When embedding generation fails
            
        Example:
            Ingest troubleshooting guide:
            
            >>> content = '''
# Database Connection Issues

## Symptoms
- Connection timeout errors
- "Connection refused" messages

## Solutions
1. Check database service status
2. Verify connection parameters
3. Review firewall settings
'''
            >>> metadata = {
                'author': 'SRE Team',
                'tags': ['database', 'networking', 'timeout'],
                'difficulty': 'intermediate'
            }
            >>> doc_id = await ingester.ingest_document(
                title="Database Connection Troubleshooting",
                content=content,
                document_type="troubleshooting",
                metadata=metadata
            )
            >>> print(f"Document ingested: {doc_id}")
            Document ingested: doc_a1b2c3d4
            
            Ingest configuration template:
            
            >>> config_content = '''
# Redis Configuration Template
redis:
  host: localhost
  port: 6379
  timeout: 30s
'''
            >>> doc_id = await ingester.ingest_document(
                "Redis Configuration Template",
                config_content,
                "configuration"
            )
            
        Note:
            Large documents are automatically chunked for optimal retrieval.
            Embeddings are generated asynchronously for better performance.
            Document processing may take several seconds for large content.
            Duplicate content detection prevents redundant storage.
        """
        pass
    
    @abstractmethod
    async def update_document(
        self, 
        document_id: str, 
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Update existing document with new content and metadata.
        
        This method updates an existing document, regenerating embeddings
        and updating the search index. The document remains searchable
        throughout the update process.
        
        Args:
            document_id: Unique identifier of the document to update.
                         Must be a valid ID from a previous ingest_document() call.
            content: New document content replacing the existing content.
                     Content formatting and chunking rules apply as in ingestion.
            metadata: Optional metadata updates. Only provided fields are updated;
                      existing metadata is preserved for unspecified fields.
                      Set field to None to remove it from metadata.
                      
        Raises:
            DocumentNotFoundException: When document_id does not exist
            IngestionException: When content processing fails
            ValidationException: When new content format is invalid
            EmbeddingException: When embedding regeneration fails
            
        Example:
            Update document content:
            
            >>> updated_content = '''
# Database Connection Issues (Updated)

## New Section: Performance Optimization
- Connection pooling configuration
- Query timeout tuning

## Symptoms
- Connection timeout errors (updated diagnostics)
'''
            >>> await ingester.update_document(
                "doc_a1b2c3d4",
                updated_content,
                metadata={'version': '2.0', 'last_updated': '2025-01-15'}
            )
            
            Update only metadata:
            
            >>> await ingester.update_document(
                "doc_a1b2c3d4",
                existing_content,  # Keep existing content
                metadata={'tags': ['database', 'performance', 'timeout']}
            )
            
        Note:
            Updates trigger complete reprocessing including new embeddings.
            Search results reflect updated content immediately after processing.
            Document version history is maintained for audit purposes.
            Large content updates may take time for embedding regeneration.
        """
        pass
    
    @abstractmethod
    async def delete_document(self, document_id: str) -> None:
        """Delete document from knowledge base permanently.
        
        This method removes a document and all associated data including
        embeddings, chunks, and metadata from the knowledge base.
        The operation cannot be undone.
        
        Args:
            document_id: Unique identifier of the document to delete.
                         Must be a valid ID from previous ingestion.
                         
        Raises:
            DocumentNotFoundException: When document_id does not exist
            DeletionException: When deletion operation fails
            
        Example:
            Delete obsolete document:
            
            >>> await ingester.delete_document("doc_a1b2c3d4")
            
            Batch deletion with error handling:
            
            >>> obsolete_docs = ["doc_old1", "doc_old2", "doc_old3"]
            >>> for doc_id in obsolete_docs:
                try:
                    await ingester.delete_document(doc_id)
                    print(f"Deleted: {doc_id}")
                except DocumentNotFoundException:
                    print(f"Already deleted: {doc_id}")
            
        Note:
            Deletion is immediate and cannot be undone.
            Related chunks and embeddings are automatically cleaned up.
            Search index is updated to remove deleted content.
            References to deleted documents in other systems may become invalid.
        """
        pass