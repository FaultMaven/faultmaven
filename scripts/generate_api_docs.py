#!/usr/bin/env python3
"""Generate comprehensive API documentation from FastAPI schemas.

This script creates detailed API documentation including:
- Enhanced OpenAPI schema with examples
- Multiple output formats (JSON, YAML, Markdown)
- Real usage examples for all endpoints
- Error response documentation
- Authentication placeholders for future implementation
"""

import json
import yaml
import sys
import os
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def enhance_openapi_schema(app: FastAPI) -> Dict[str, Any]:
    """Enhance the basic OpenAPI schema with comprehensive information and examples."""
    
    schema = get_openapi(
        title="FaultMaven API",
        version="1.0.0",
        description="""
# FaultMaven API Documentation

AI-powered troubleshooting assistant for Engineers, SREs, and DevOps professionals.

## Architecture Overview

The FaultMaven API follows clean architecture principles with:

- **API Layer**: FastAPI routers handling HTTP requests with comprehensive middleware
- **Service Layer**: Business logic orchestration using dependency injection
- **Core Layer**: Domain logic including AI reasoning engine and data processing
- **Infrastructure Layer**: External service integrations (LLM providers, databases, security)

## Key Features

- **AI-Powered Troubleshooting**: Advanced reasoning engine using multiple LLM providers
- **Privacy-First Design**: Comprehensive PII redaction before external processing
- **Session Management**: Redis-backed session persistence for multi-turn conversations
- **Knowledge Base**: RAG-enabled document ingestion and retrieval using ChromaDB
- **Data Processing**: Intelligent log analysis and classification
- **Performance Monitoring**: Real-time metrics and health monitoring
- **Error Recovery**: Automatic error detection and recovery mechanisms

## Authentication

Currently, the API does not require authentication. This may change in future versions.
When implemented, authentication will use API key-based authentication.

## Rate Limiting

API requests are subject to rate limiting to ensure fair usage and system stability.
Current limits are applied at the infrastructure level.

## Error Handling

All endpoints return structured error responses with appropriate HTTP status codes.

### Standard Error Response Format

```json
{
    "detail": "Human-readable error description",
    "error_type": "ErrorType",
    "correlation_id": "uuid-here",
    "timestamp": "2025-01-15T10:30:00Z"
}
```

### Common HTTP Status Codes

- `200`: Success
- `400`: Bad Request - Invalid input data
- `401`: Unauthorized - Authentication required (future)
- `404`: Not Found - Resource not found
- `422`: Validation Error - Request data validation failed
- `429`: Too Many Requests - Rate limit exceeded
- `500`: Internal Server Error - Unexpected server error
- `503`: Service Unavailable - External service unavailable

## Data Privacy

All data submitted to the API is processed through privacy-first pipelines with:

- Comprehensive PII redaction using Microsoft Presidio
- Data sanitization before external LLM processing
- Session-based data isolation
- Configurable data retention policies

## Performance Characteristics

- **Response Time**: < 200ms for typical queries (excluding LLM processing)
- **Throughput**: Supports 100+ concurrent requests
- **Availability**: 99.9% uptime target with health monitoring
- **Scalability**: Horizontal scaling support via stateless design
        """,
        routes=app.routes,
    )
    
    # Add comprehensive endpoint examples
    schema = _add_endpoint_examples(schema)
    
    # Add detailed response schemas
    schema = _add_response_schemas(schema)
    
    # Add security schemas (placeholders)
    schema = _add_security_schemas(schema)
    
    # Add additional metadata
    schema = _add_metadata(schema)
    
    return schema


def _add_endpoint_examples(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Add comprehensive examples to all endpoints."""
    
    paths = schema.get("paths", {})
    
    # Agent troubleshooting endpoint examples
    if "/api/v1/troubleshoot" in paths:
        troubleshoot_path = paths["/api/v1/troubleshoot"]
        if "post" in troubleshoot_path:
            post_op = troubleshoot_path["post"]
            if "requestBody" in post_op:
                post_op["requestBody"]["content"]["application/json"]["examples"] = {
                    "database_connectivity_issue": {
                        "summary": "Database Connection Problem",
                        "description": "Troubleshooting a PostgreSQL connectivity issue with timeout errors",
                        "value": {
                            "query": "Our application can't connect to the PostgreSQL database. We're seeing connection timeout errors in the logs and the application is returning 500 errors to users.",
                            "session_id": "session_db_123",
                            "context": {
                                "system_type": "web_application",
                                "environment": "production",
                                "urgency": "high",
                                "affected_services": ["user-api", "order-service"],
                                "error_symptoms": ["connection_timeout", "500_errors"]
                            }
                        }
                    },
                    "performance_degradation": {
                        "summary": "API Performance Issue",
                        "description": "Investigating sudden increase in API response times",
                        "value": {
                            "query": "API response times have increased from 200ms to 2-3 seconds over the past hour. No recent deployments. CPU and memory look normal.",
                            "session_id": "session_perf_456",
                            "context": {
                                "system_type": "microservice",
                                "environment": "production",
                                "urgency": "medium",
                                "metrics": {
                                    "baseline_response_time": "200ms",
                                    "current_response_time": "2-3s",
                                    "cpu_usage": "45%",
                                    "memory_usage": "60%"
                                }
                            }
                        }
                    },
                    "kubernetes_pod_crashloop": {
                        "summary": "Kubernetes Pod CrashLoopBackOff",
                        "description": "Pod stuck in CrashLoopBackOff state",
                        "value": {
                            "query": "Pod 'user-service-7d8f9b6c4-xyz123' is in CrashLoopBackOff state. Container exits with code 1 after startup.",
                            "session_id": "session_k8s_789",
                            "context": {
                                "system_type": "kubernetes",
                                "environment": "staging",
                                "urgency": "medium",
                                "pod_details": {
                                    "namespace": "default",
                                    "pod_name": "user-service-7d8f9b6c4-xyz123",
                                    "container_name": "user-service",
                                    "exit_code": 1
                                }
                            }
                        }
                    }
                }
    
    # Data ingestion endpoint examples
    if "/api/v1/data" in paths:
        data_path = paths["/api/v1/data"]
        if "post" in data_path:
            post_op = data_path["post"]
            if "requestBody" in post_op:
                # Add file upload examples
                post_op["description"] = "Upload and process log files, configuration files, or other diagnostic data"
                post_op["requestBody"]["content"]["multipart/form-data"]["examples"] = {
                    "log_file_upload": {
                        "summary": "Application Log File",
                        "description": "Upload application logs for analysis",
                        "value": {
                            "file": "[Binary log file content]",
                            "file_type": "application_logs",
                            "description": "Production API server logs from the last 24 hours"
                        }
                    },
                    "kubernetes_yaml": {
                        "summary": "Kubernetes Configuration",
                        "description": "Upload Kubernetes YAML for configuration analysis",
                        "value": {
                            "file": "[YAML configuration content]",
                            "file_type": "kubernetes_config",
                            "description": "Deployment configuration showing resource issues"
                        }
                    }
                }
    
    # Knowledge base document ingestion examples
    if "/api/v1/knowledge/documents" in paths:
        kb_path = paths["/api/v1/knowledge/documents"]
        if "post" in kb_path:
            post_op = kb_path["post"]
            if "requestBody" in post_op:
                post_op["requestBody"]["content"]["multipart/form-data"]["examples"] = {
                    "runbook_upload": {
                        "summary": "Troubleshooting Runbook",
                        "description": "Upload team runbook for knowledge base",
                        "value": {
                            "file": "[PDF or Markdown runbook content]",
                            "document_type": "runbook",
                            "tags": ["database", "troubleshooting", "postgresql"],
                            "description": "Database troubleshooting procedures and common fixes"
                        }
                    },
                    "documentation_upload": {
                        "summary": "System Documentation",
                        "description": "Upload system architecture documentation",
                        "value": {
                            "file": "[Documentation content]",
                            "document_type": "architecture_doc",
                            "tags": ["architecture", "microservices", "system_design"],
                            "description": "Microservices architecture overview and dependencies"
                        }
                    }
                }
    
    # Session management examples
    if "/api/v1/sessions" in paths:
        sessions_path = paths["/api/v1/sessions"]
        if "post" in sessions_path:
            post_op = sessions_path["post"]
            if "requestBody" in post_op:
                post_op["requestBody"]["content"]["application/json"]["examples"] = {
                    "new_session": {
                        "summary": "Create New Session",
                        "description": "Start a new troubleshooting session",
                        "value": {
                            "timeout_minutes": 60,
                            "session_type": "troubleshooting",
                            "metadata": {
                                "environment": "production",
                                "team": "platform-team",
                                "incident_priority": "high"
                            }
                        }
                    },
                    "resume_session_with_client_id": {
                        "summary": "Resume Session with Client ID",
                        "description": "Resume existing session using client identifier for session continuity",
                        "value": {
                            "timeout_minutes": 60,
                            "session_type": "troubleshooting", 
                            "client_id": "browser-client-abc123",
                            "metadata": {
                                "environment": "production",
                                "team": "platform-team"
                            }
                        }
                    }
                }
    
    return schema


def _add_response_schemas(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Add detailed response schemas with examples."""
    
    if "components" not in schema:
        schema["components"] = {}
    if "schemas" not in schema["components"]:
        schema["components"]["schemas"] = {}
    
    # Standard error response schema
    schema["components"]["schemas"]["ErrorResponse"] = {
        "type": "object",
        "required": ["detail"],
        "properties": {
            "detail": {
                "type": "string",
                "description": "Human-readable error description"
            },
            "error_type": {
                "type": "string",
                "description": "Machine-readable error classification",
                "enum": ["ValidationError", "AuthenticationError", "AuthorizationError", 
                        "NotFoundError", "RateLimitError", "ServiceUnavailableError", "InternalServerError"]
            },
            "correlation_id": {
                "type": "string",
                "format": "uuid",
                "description": "Unique identifier for request tracing and support"
            },
            "timestamp": {
                "type": "string",
                "format": "date-time",
                "description": "Error occurrence timestamp in ISO format"
            },
            "context": {
                "type": "object",
                "description": "Additional error context for debugging"
            }
        },
        "example": {
            "detail": "Invalid session ID provided",
            "error_type": "ValidationError",
            "correlation_id": "123e4567-e89b-12d3-a456-426614174000",
            "timestamp": "2025-01-15T10:30:00Z",
            "context": {
                "session_id": "invalid_session_123",
                "validation_errors": ["Session ID format invalid"]
            }
        }
    }
    
    # Troubleshooting response schema
    schema["components"]["schemas"]["TroubleshootingResponse"] = {
        "type": "object",
        "required": ["investigation_id", "status", "session_id"],
        "properties": {
            "investigation_id": {
                "type": "string",
                "description": "Unique identifier for this troubleshooting investigation"
            },
            "status": {
                "type": "string",
                "enum": ["in_progress", "completed", "failed", "requires_input"],
                "description": "Current status of the investigation"
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Finding category",
                            "enum": ["root_cause", "contributing_factor", "symptom", "recommendation"]
                        },
                        "message": {
                            "type": "string",
                            "description": "Finding description"
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "high", "medium", "low", "info"],
                            "description": "Finding severity level"
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "AI confidence in this finding (0.0 to 1.0)"
                        },
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Supporting evidence for this finding"
                        }
                    }
                },
                "description": "List of findings from the investigation"
            },
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "Recommended action to take"
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["immediate", "high", "medium", "low"],
                            "description": "Action priority"
                        },
                        "impact": {
                            "type": "string",
                            "description": "Expected impact of this action"
                        },
                        "effort": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "description": "Estimated effort required"
                        }
                    }
                },
                "description": "Recommended actions based on findings"
            },
            "session_id": {
                "type": "string",
                "description": "Session ID for this troubleshooting session"
            },
            "reasoning_trace": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string"},
                        "reasoning": {"type": "string"},
                        "data_sources": {"type": "array", "items": {"type": "string"}}
                    }
                },
                "description": "AI reasoning process trace for transparency"
            }
        },
        "example": {
            "investigation_id": "inv_789",
            "status": "completed",
            "findings": [
                {
                    "type": "root_cause",
                    "message": "Database connection pool exhausted due to connection leak",
                    "severity": "high",
                    "confidence": 0.9,
                    "evidence": [
                        "Connection pool size: 20, Active connections: 20",
                        "No idle connections available",
                        "Long-running transactions detected"
                    ]
                }
            ],
            "recommendations": [
                {
                    "action": "Increase database connection pool size to 50",
                    "priority": "immediate",
                    "impact": "Should restore service within 5 minutes",
                    "effort": "low"
                },
                {
                    "action": "Review application code for connection leaks",
                    "priority": "high",
                    "impact": "Prevents future occurrences",
                    "effort": "medium"
                }
            ],
            "session_id": "session_db_123",
            "reasoning_trace": [
                {
                    "step": "symptom_analysis",
                    "reasoning": "HTTP 500 errors correlate with database timeout errors",
                    "data_sources": ["application_logs", "database_metrics"]
                },
                {
                    "step": "hypothesis_formation",
                    "reasoning": "Connection pool exhaustion is most likely cause given metrics",
                    "data_sources": ["connection_pool_metrics", "transaction_logs"]
                }
            ]
        }
    }
    
    # Data ingestion response schema
    schema["components"]["schemas"]["DataIngestionResponse"] = {
        "type": "object",
        "required": ["ingestion_id", "status"],
        "properties": {
            "ingestion_id": {
                "type": "string",
                "description": "Unique identifier for this data ingestion"
            },
            "status": {
                "type": "string",
                "enum": ["processing", "completed", "failed"],
                "description": "Current processing status"
            },
            "file_info": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                    "file_type": {"type": "string"},
                    "detected_format": {"type": "string"}
                },
                "description": "Information about the uploaded file"
            },
            "processing_results": {
                "type": "object",
                "properties": {
                    "lines_processed": {"type": "integer"},
                    "errors_found": {"type": "integer"},
                    "insights_extracted": {"type": "integer"},
                    "processing_time_ms": {"type": "integer"}
                },
                "description": "Results of data processing"
            }
        },
        "example": {
            "ingestion_id": "ingest_456",
            "status": "completed",
            "file_info": {
                "filename": "app.log",
                "size_bytes": 1048576,
                "file_type": "application/log",
                "detected_format": "json_logs"
            },
            "processing_results": {
                "lines_processed": 15420,
                "errors_found": 23,
                "insights_extracted": 8,
                "processing_time_ms": 2340
            }
        }
    }
    
    # Session response schema
    schema["components"]["schemas"]["SessionResponse"] = {
        "type": "object",
        "required": ["session_id", "status"],
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Unique session identifier"
            },
            "user_id": {
                "type": "string",
                "description": "Associated user identifier",
                "nullable": True
            },
            "client_id": {
                "type": "string",
                "description": "Client/device identifier for session resumption",
                "nullable": True
            },
            "status": {
                "type": "string",
                "enum": ["active", "idle", "expired"],
                "description": "Current session status"
            },
            "created_at": {
                "type": "string",
                "format": "date-time",
                "description": "Session creation timestamp"
            },
            "session_resumed": {
                "type": "boolean",
                "description": "Indicates if this was an existing session resumed",
                "nullable": True
            },
            "session_type": {
                "type": "string",
                "description": "Type of session (e.g., troubleshooting)"
            },
            "message": {
                "type": "string",
                "description": "Status message about session creation/resumption"
            },
            "metadata": {
                "type": "object",
                "description": "Session metadata and context"
            }
        },
        "examples": {
            "new_session": {
                "summary": "New Session Created",
                "value": {
                    "session_id": "session_abc123",
                    "user_id": "user_123",
                    "client_id": "browser-client-abc123",
                    "status": "active",
                    "created_at": "2025-01-15T10:00:00Z",
                    "session_resumed": False,
                    "session_type": "troubleshooting",
                    "message": "Session created successfully",
                    "metadata": {
                        "environment": "production",
                        "team": "platform-team"
                    }
                }
            },
            "resumed_session": {
                "summary": "Existing Session Resumed",
                "value": {
                    "session_id": "session_existing_456",
                    "user_id": "user_123",
                    "client_id": "browser-client-abc123",
                    "status": "active",
                    "created_at": "2025-01-15T09:30:00Z",
                    "session_resumed": True,
                    "session_type": "troubleshooting",
                    "message": "Session resumed successfully",
                    "metadata": {
                        "environment": "production",
                        "team": "platform-team",
                        "investigations_count": 2
                    }
                }
            }
        }
    }
    
    return schema


def _add_security_schemas(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Add security schemas (placeholder for future authentication)."""
    
    if "components" not in schema:
        schema["components"] = {}
    
    # Placeholder for future authentication
    schema["components"]["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "API key authentication (planned for future implementation)"
        },
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "JWT Bearer token authentication (planned for future implementation)"
        }
    }
    
    # Add common headers
    schema["components"]["headers"] = {
        "X-Correlation-ID": {
            "description": "Unique identifier for request tracing and debugging",
            "schema": {
                "type": "string",
                "format": "uuid"
            },
            "example": "550e8400-e29b-41d4-a716-446655440000"
        }
    }
    
    return schema


def _add_metadata(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Add additional metadata to the schema."""
    
    # Add contact information
    schema["info"]["contact"] = {
        "name": "FaultMaven Team",
        "email": "support@faultmaven.ai",
        "url": "https://faultmaven.ai"
    }
    
    # Add license information
    schema["info"]["license"] = {
        "name": "Proprietary",
        "url": "https://faultmaven.ai/license"
    }
    
    # Add external documentation
    schema["externalDocs"] = {
        "description": "FaultMaven Documentation",
        "url": "https://docs.faultmaven.ai"
    }
    
    # Add tags for better organization
    schema["tags"] = [
        {
            "name": "troubleshooting",
            "description": "AI-powered troubleshooting operations",
            "externalDocs": {
                "description": "Troubleshooting Guide",
                "url": "https://docs.faultmaven.ai/troubleshooting"
            }
        },
        {
            "name": "data_ingestion",
            "description": "File upload and data processing operations",
            "externalDocs": {
                "description": "Data Ingestion Guide",
                "url": "https://docs.faultmaven.ai/data-ingestion"
            }
        },
        {
            "name": "knowledge_base",
            "description": "Knowledge base and document management operations",
            "externalDocs": {
                "description": "Knowledge Base Guide",
                "url": "https://docs.faultmaven.ai/knowledge-base"
            }
        },
        {
            "name": "session_management",
            "description": "Session lifecycle and management operations"
        }
    ]
    
    return schema


def generate_markdown_docs(schema: Dict[str, Any], docs_dir: Path) -> None:
    """Generate markdown documentation from OpenAPI schema."""
    
    md_content = f"""# FaultMaven API Reference

{schema['info']['description']}

**Version:** {schema['info']['version']}  
**Base URL:** `/`  
**Generated:** {datetime.utcnow().isoformat()}Z

## Authentication

Currently, the API does not require authentication. Future versions will implement API key or JWT-based authentication.

## Endpoints

"""
    
    # Sort paths for consistent documentation
    sorted_paths = sorted(schema.get("paths", {}).items())
    
    for path, methods in sorted_paths:
        md_content += f"### `{path}`\n\n"
        
        # Sort methods (GET, POST, PUT, DELETE, etc.)
        method_order = ["get", "post", "put", "patch", "delete"]
        sorted_methods = sorted(methods.items(), key=lambda x: method_order.index(x[0]) if x[0] in method_order else 999)
        
        for method, details in sorted_methods:
            if method.startswith('x-'):  # Skip extension fields
                continue
                
            md_content += f"#### {method.upper()}\n\n"
            
            # Add summary and description
            if 'summary' in details:
                md_content += f"**{details['summary']}**\n\n"
            
            if 'description' in details:
                md_content += f"{details['description']}\n\n"
            
            # Add tags
            if 'tags' in details:
                tags_str = ", ".join(f"`{tag}`" for tag in details['tags'])
                md_content += f"**Tags:** {tags_str}\n\n"
            
            # Add parameters
            if 'parameters' in details:
                md_content += "**Parameters:**\n\n"
                for param in details['parameters']:
                    required = "✅" if param.get('required', False) else "❌"
                    md_content += f"- `{param['name']}` ({param.get('in', 'query')}) {required} - {param.get('description', 'No description')}\n"
                md_content += "\n"
            
            # Add request body examples
            if 'requestBody' in details:
                md_content += "**Request Body:**\n\n"
                request_body = details['requestBody']
                content = request_body.get('content', {})
                
                for content_type, content_details in content.items():
                    md_content += f"Content-Type: `{content_type}`\n\n"
                    
                    if 'examples' in content_details:
                        for example_name, example_data in content_details['examples'].items():
                            md_content += f"**Example: {example_data.get('summary', example_name)}**\n\n"
                            if 'description' in example_data:
                                md_content += f"{example_data['description']}\n\n"
                            
                            md_content += "```json\n"
                            md_content += json.dumps(example_data.get('value', {}), indent=2)
                            md_content += "\n```\n\n"
            
            # Add response examples
            if 'responses' in details:
                md_content += "**Responses:**\n\n"
                for status_code, response_details in details['responses'].items():
                    md_content += f"**{status_code}** - {response_details.get('description', 'No description')}\n\n"
                    
                    # Add response schema examples if available
                    content = response_details.get('content', {})
                    for content_type, content_details in content.items():
                        if 'schema' in content_details:
                            schema_ref = content_details['schema']
                            if '$ref' in schema_ref:
                                schema_name = schema_ref['$ref'].split('/')[-1]
                                if schema_name in schema.get('components', {}).get('schemas', {}):
                                    schema_def = schema['components']['schemas'][schema_name]
                                    if 'example' in schema_def:
                                        md_content += f"```json\n"
                                        md_content += json.dumps(schema_def['example'], indent=2)
                                        md_content += "\n```\n\n"
            
            md_content += "---\n\n"
    
    # Add schemas section
    if 'components' in schema and 'schemas' in schema['components']:
        md_content += "## Data Models\n\n"
        
        for schema_name, schema_def in schema['components']['schemas'].items():
            md_content += f"### {schema_name}\n\n"
            
            if 'description' in schema_def:
                md_content += f"{schema_def['description']}\n\n"
            
            # Add properties
            if 'properties' in schema_def:
                md_content += "**Properties:**\n\n"
                for prop_name, prop_def in schema_def['properties'].items():
                    required = "✅" if prop_name in schema_def.get('required', []) else "❌"
                    prop_type = prop_def.get('type', 'unknown')
                    prop_desc = prop_def.get('description', 'No description')
                    md_content += f"- `{prop_name}` ({prop_type}) {required} - {prop_desc}\n"
                md_content += "\n"
            
            # Add example
            if 'example' in schema_def:
                md_content += "**Example:**\n\n"
                md_content += "```json\n"
                md_content += json.dumps(schema_def['example'], indent=2)
                md_content += "\n```\n\n"
            
            md_content += "---\n\n"
    
    # Write the markdown file
    with open(docs_dir / "README.md", "w") as f:
        f.write(md_content)


def generate_documentation() -> None:
    """Generate comprehensive API documentation."""
    
    print("🚀 Starting API documentation generation...")
    
    try:
        # Import the app
        from faultmaven.main import app
        print("✅ FastAPI app imported successfully")
        
        # Generate enhanced schema
        print("📝 Enhancing OpenAPI schema with examples...")
        schema = enhance_openapi_schema(app)
        
        # Create docs directory
        docs_dir = Path("docs/api")
        docs_dir.mkdir(parents=True, exist_ok=True)
        print(f"📁 Created documentation directory: {docs_dir}")
        
        # Save as JSON
        json_file = docs_dir / "openapi.json"
        with open(json_file, "w") as f:
            json.dump(schema, f, indent=2)
        print(f"💾 Saved OpenAPI JSON: {json_file}")
        
        # Save as YAML
        yaml_file = docs_dir / "openapi.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(schema, f, default_flow_style=False, sort_keys=False)
        print(f"💾 Saved OpenAPI YAML: {yaml_file}")
        
        # Generate markdown documentation
        print("📖 Generating markdown documentation...")
        generate_markdown_docs(schema, docs_dir)
        print(f"💾 Saved Markdown docs: {docs_dir / 'README.md'}")
        
        # Generate summary
        endpoint_count = len(schema.get("paths", {}))
        method_count = sum(len([m for m in methods.keys() if not m.startswith('x-')]) 
                          for methods in schema.get("paths", {}).values())
        schema_count = len(schema.get("components", {}).get("schemas", {}))
        
        print(f"""
✅ API documentation generated successfully!

📊 Documentation Statistics:
   • Endpoints: {endpoint_count}
   • HTTP Methods: {method_count}
   • Data Models: {schema_count}
   • Output Formats: JSON, YAML, Markdown

📂 Generated Files:
   • {json_file}
   • {yaml_file}
   • {docs_dir / 'README.md'}

🌐 View documentation:
   • OpenAPI UI: http://localhost:8000/docs
   • ReDoc: http://localhost:8000/redoc
   • Markdown: {docs_dir / 'README.md'}
""")
        
    except Exception as e:
        print(f"❌ Error generating API documentation: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    generate_documentation()