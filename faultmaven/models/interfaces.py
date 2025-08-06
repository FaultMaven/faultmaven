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
    """Abstract base class for all tools.
    
    This interface defines the contract that all tools must implement
    to be used within the FaultMaven agent system.
    """
    
    @abstractmethod
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Execute the tool with the given parameters.
        
        Args:
            params: Dictionary of parameters for tool execution
            
        Returns:
            ToolResult containing the execution result
        """
        pass
    
    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """Get the tool's schema definition.
        
        Returns:
            Dictionary containing the tool's schema
        """
        pass

# Infrastructure interfaces
class ILLMProvider(ABC):
    """Interface for LLM providers.
    
    This interface abstracts the interaction with different LLM providers,
    allowing the system to switch between providers without code changes.
    """
    
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate text using the LLM provider.
        
        Args:
            prompt: The input prompt for text generation
            **kwargs: Additional parameters for the LLM provider
            
        Returns:
            Generated text response
        """
        pass

class ITracer(ABC):
    """Interface for distributed tracing systems.
    
    This interface provides observability into system operations
    through distributed tracing capabilities.
    """
    
    @abstractmethod
    def trace(self, operation: str) -> ContextManager:
        """Create a trace context for an operation.
        
        Args:
            operation: Name of the operation being traced
            
        Returns:
            Context manager for the trace span
        """
        pass

class ISanitizer(ABC):
    """Interface for data sanitization services.
    
    This interface provides PII redaction and data sanitization
    capabilities to ensure privacy compliance.
    """
    
    @abstractmethod
    def sanitize(self, data: Any) -> Any:
        """Sanitize data by removing or redacting sensitive information.
        
        Args:
            data: The data to be sanitized
            
        Returns:
            Sanitized version of the data
        """
        pass

class IVectorStore(ABC):
    """Interface for vector database operations.
    
    This interface abstracts vector database operations for
    knowledge base storage and retrieval.
    """
    
    @abstractmethod
    async def add_documents(self, documents: List[Dict]) -> None:
        """Add documents to the vector store.
        
        Args:
            documents: List of document dictionaries to add
        """
        pass
    
    @abstractmethod
    async def search(self, query: str, k: int = 5) -> List[Dict]:
        """Search for similar documents in the vector store.
        
        Args:
            query: Search query text
            k: Number of results to return
            
        Returns:
            List of similar documents
        """
        pass

class ISessionStore(ABC):
    """Interface for session storage operations.
    
    This interface abstracts session storage operations for
    maintaining user session state.
    """
    
    @abstractmethod
    async def get(self, key: str) -> Optional[Dict]:
        """Get session data by key.
        
        Args:
            key: Session key to retrieve
            
        Returns:
            Session data if found, None otherwise
        """
        pass
    
    @abstractmethod
    async def set(self, key: str, value: Dict, ttl: Optional[int] = None) -> None:
        """Set session data with optional TTL.
        
        Args:
            key: Session key to set
            value: Session data to store
            ttl: Time to live in seconds (optional)
        """
        pass

# Data processing interfaces
class IDataClassifier(ABC):
    """Interface for data type classification.
    
    This interface abstracts data classification operations,
    allowing different classification strategies to be used.
    """
    
    @abstractmethod
    async def classify(self, content: str, filename: Optional[str] = None) -> 'DataType':
        """Classify data content and return DataType.
        
        Args:
            content: The data content to classify
            filename: Optional filename hint for classification
            
        Returns:
            DataType enum value representing the classified type
        """
        pass

class ILogProcessor(ABC):
    """Interface for log processing operations.
    
    This interface abstracts log analysis and insight extraction,
    allowing different processing strategies to be used.
    """
    
    @abstractmethod
    async def process(self, content: str, data_type: Optional['DataType'] = None) -> Dict[str, Any]:
        """Process log content and extract insights.
        
        Args:
            content: The log content to process
            data_type: Optional data type hint for processing
            
        Returns:
            Dictionary containing extracted insights and metadata
        """
        pass

class IStorageBackend(ABC):
    """Interface for data storage operations.
    
    This interface abstracts storage backend operations,
    allowing different storage systems to be used.
    """
    
    @abstractmethod
    async def store(self, key: str, data: Any) -> None:
        """Store data with given key.
        
        Args:
            key: Unique identifier for the data
            data: Data to be stored
        """
        pass
    
    @abstractmethod
    async def retrieve(self, key: str) -> Optional[Any]:
        """Retrieve data by key.
        
        Args:
            key: Unique identifier for the data
            
        Returns:
            Retrieved data if found, None otherwise
        """
        pass

class IKnowledgeIngester(ABC):
    """Interface for knowledge base document ingestion operations.
    
    This interface abstracts knowledge ingestion operations,
    allowing different ingestion strategies to be used.
    """
    
    @abstractmethod
    async def ingest_document(
        self, 
        title: str, 
        content: str, 
        document_type: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Ingest document and return document ID.
        
        Args:
            title: Document title
            content: Document content
            document_type: Type of document (e.g., 'manual', 'troubleshooting')
            metadata: Optional metadata for the document
            
        Returns:
            Document ID assigned to the ingested document
        """
        pass
    
    @abstractmethod
    async def update_document(
        self, 
        document_id: str, 
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Update existing document.
        
        Args:
            document_id: ID of the document to update
            content: New content for the document
            metadata: Optional metadata updates
        """
        pass
    
    @abstractmethod
    async def delete_document(self, document_id: str) -> None:
        """Delete document from knowledge base.
        
        Args:
            document_id: ID of the document to delete
        """
        pass