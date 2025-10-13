# Tool Catalog

Complete catalog of available tools for troubleshooting sessions.

## Implemented Tools ✅

### Knowledge Base Search Tool
**Status**: ✅ Production  
**File**: `faultmaven/tools/knowledge_base.py`  
**Type**: Direct Implementation

**Purpose**: Search team's internal runbooks, documentation, and historical incident resolutions.

**When Used**:
- All investigation phases when internal knowledge needed
- Preferred first before external web search
- Context-aware with phase-specific filtering

**Capabilities**:
- Semantic search using BGE-M3 embeddings
- Metadata filtering (service, environment, technology, tags)
- Query expansion based on context (error codes, symptoms, phase)
- Relevance scoring with confidence indicators
- Document type filtering (troubleshooting guides, runbooks, error catalogs)

**Example Invocations**:
```
Phase 1: "Search KB for Redis connection timeout impacts"
Phase 3: "Find runbooks related to database deadlocks"
Phase 5: "Show documented solutions for OOM kills"
```

**Configuration**:
```env
CHROMADB_URL=http://chromadb.faultmaven.local:30080
EMBEDDING_MODEL=BAAI/bge-m3
MAX_SEARCH_RESULTS=10
SIMILARITY_THRESHOLD=0.7
```

**See**: [Knowledge Base Tool Documentation](./implemented/knowledge-base-tool.md)

---

### Web Search Tool
**Status**: ✅ Production  
**File**: `faultmaven/tools/web_search.py`  
**Type**: External API Integration

**Purpose**: Search public internet for technical documentation and solutions when internal knowledge base lacks information.

**When Used**:
- Fallback when knowledge base returns insufficient results
- Phase 3 (Hypothesis) for external error documentation
- Phase 5 (Solution) for implementation guides

**Capabilities**:
- Trusted domain filtering (Stack Overflow, GitHub, official docs, etc.)
- Context-enhanced queries (adds phase-specific search terms)
- Privacy-first (all queries PII-sanitized before external call)
- Limited to 3 results by default for focused answers

**Trusted Domains**:
- stackoverflow.com
- github.com
- docs.microsoft.com, learn.microsoft.com
- docs.aws.amazon.com
- kubernetes.io
- docs.docker.com
- redis.io, mongodb.com
- nginx.org, apache.org
- python.org, nodejs.org

**Example Invocations**:
```
"Search web for Kubernetes CrashLoopBackOff solutions"
"Find documentation for Redis MISCONF error"
"Look up PostgreSQL deadlock resolution strategies"
```

**Configuration**:
```env
WEB_SEARCH_API_KEY=your_google_api_key
WEB_SEARCH_ENGINE_ID=your_search_engine_id
WEB_SEARCH_API_ENDPOINT=https://www.googleapis.com/customsearch/v1
WEB_SEARCH_MAX_RESULTS=3
```

**Safety Features**:
- Double PII sanitization (before and after external call)
- Domain whitelist enforcement
- Result disclaimer added to responses
- 10-second timeout protection

**See**: [Web Search Tool Documentation](./implemented/web-search-tool.md)

---

### Log Analyzer Tool
**Status**: ✅ Production  
**File**: `faultmaven/core/processing/log_analyzer.py`  
**Type**: Direct Implementation

**Purpose**: Parse, analyze, and extract insights from uploaded log files.

**When Used**:
- Phase 1 (Blast Radius) for impact assessment
- Phase 2 (Timeline) for event reconstruction
- Phase 4 (Validation) for hypothesis confirmation

**Capabilities**:
- **Pattern Detection**: Timestamps, log levels, HTTP status codes, error codes, durations
- **Anomaly Detection**: IsolationForest ML for unusual patterns
- **Security Scanning**: PII detection in logs (emails, SSN, phone, credit cards, IPs)
- **Context-Aware Processing**: Uses memory service for historical pattern learning
- **Technology-Specific Patterns**:
  - **Kubernetes**: pod/namespace/deployment patterns
  - **Database**: connection issues, deadlocks, transactions
  - **HTTP**: Status codes, response times, endpoints
  
**Analysis Types**:
- Error frequency and clustering
- Performance degradation detection
- Correlation analysis
- Timeline reconstruction
- Security flag identification

**Example Usage**:
```
Phase 2: "Analyze nginx access logs for timeline of 500 errors"
Phase 4: "Check application logs for database connection patterns"
```

**Performance**:
- Processing speed: ~1s per MB
- Memory usage: Efficient streaming for large files
- Max file size: 100 MB (configurable)

**Security Features**:
- Automatic PII redaction in log content
- Security pattern detection
- Sensitive data flagging

**See**: [Log Analyzer Tool Documentation](./implemented/log-analyzer-tool.md)

---

### Data Classifier Tool
**Status**: ✅ Production  
**File**: `faultmaven/core/processing/data_classifier.py`  
**Type**: Direct Implementation

**Purpose**: Automatically identify uploaded data types for appropriate processing.

**When Used**:
- Automatically invoked when user uploads data via API
- Before routing to specialized analyzers

**Capabilities**:
- **File Type Detection**: Extension, magic bytes, content analysis
- **Data Type Classification**:
  - `log_file` - Application/system logs
  - `metrics_data` - Time-series metrics (Prometheus, CSV)
  - `trace_data` - Distributed traces (JSON, OpenTelemetry)
  - `config_file` - YAML, JSON, TOML configurations
  - `error_report` - Stack traces, error dumps
  - `database_dump` - SQL dumps, query logs
  - `network_capture` - Packet captures, network logs

**Classification Method**:
1. File extension analysis
2. Content sampling and pattern matching
3. Structure validation
4. Confidence scoring

**Example Classifications**:
```
nginx.log → log_file (confidence: 0.95)
metrics.json → metrics_data (confidence: 0.88)
k8s-deployment.yaml → config_file (confidence: 1.0)
```

**See**: [Data Classifier Tool Documentation](./implemented/data-classifier-tool.md)

---

## Partially Implemented Tools 🟡

### Document Generator Tool
**Status**: 🟡 Prompts configured, storage layer incomplete  
**Planned File**: `faultmaven/tools/document_generator.py`  
**Type**: Direct Implementation (LLM-based)

**Purpose**: Generate structured runbooks, post-mortems, and session summaries from resolved cases.

**Current Status**:
- ✅ LLM prompts configured for runbook generation
- ✅ Template structure defined
- 🔲 Storage API not implemented
- 🔲 Retrieval system not implemented

**When Used**:
- Phase 6 (Documentation) after case resolution
- User requests summary or runbook creation
- Automatic suggestion after successful resolution

**Planned Capabilities**:
- Runbook generation with structured format
- Post-mortem report creation
- Session summary generation
- Evidence-based documentation
- Template customization

**See**: [Document Generator Tool Documentation](./planned/document-generator-tool.md)

**Note**: Previously documented in `docs/planned-features/RUNBOOK_CREATION.md`, now consolidated here.

---

## Planned Tools 🔲

### Metrics Analyzer Tool
**Status**: 🔲 Designed, not implemented  
**Planned File**: `faultmaven/tools/metrics_analyzer.py`  
**Type**: Direct Implementation

**Purpose**: Analyze time-series metrics data from Prometheus, Grafana, CloudWatch, etc.

**Planned Capabilities**:
- Trend analysis and anomaly detection
- Correlation with events and deployments
- Performance degradation identification
- Alert threshold recommendations

**See**: [Metrics Analyzer Tool Documentation](./planned/metrics-analyzer-tool.md)

---

### Trace Analyzer Tool
**Status**: 🔲 Concept  
**Planned File**: `faultmaven/tools/trace_analyzer.py`  
**Type**: Direct Implementation

**Purpose**: Analyze distributed traces to identify latency issues and service dependencies.

**Planned Capabilities**:
- Trace span analysis
- Critical path identification
- Service dependency mapping
- Latency hotspot detection

**See**: [Trace Analyzer Tool Documentation](./planned/trace-analyzer-tool.md)

---

### System Commands Tool
**Status**: 🔲 Designed, security concerns  
**Planned File**: `faultmaven/tools/system_commands.py`  
**Type**: System Integration (requires sandboxing)

**Purpose**: Execute diagnostic commands like kubectl, curl, grep for validation.

**Security Requirements**:
- Command whitelist enforcement
- User confirmation for execution
- Output sanitization
- Execution timeout protection
- Sandboxed environment

**Planned Commands**:
- `kubectl get pods` - Kubernetes resource inspection
- `curl` - HTTP endpoint testing
- `nslookup` - DNS resolution testing
- `traceroute` - Network path analysis

**See**: [System Commands Tool Documentation](./planned/system-commands-tool.md)

---

### Configuration Validator Tool
**Status**: 🔲 Concept  
**Planned File**: `faultmaven/tools/config_validator.py`  
**Type**: Direct Implementation

**Purpose**: Validate YAML/JSON configuration files for syntax and best practices.

**Planned Capabilities**:
- Syntax validation
- Schema validation against known formats
- Best practice checks
- Security vulnerability scanning

**See**: [Config Validator Tool Documentation](./planned/config-validator-tool.md)

---

## Integration Tools 🔌

### MCP Server Tools
**Status**: 🔲 Integration pattern defined  
**Type**: External Protocol Integration

**Purpose**: Connect to external Model Context Protocol servers for extended capabilities.

**Integration Patterns**:
1. **Consuming MCP Servers**: FaultMaven as MCP client
2. **Exposing as MCP Server**: FaultMaven tools accessible via MCP

**Potential Use Cases**:
- Database query tools (via MCP server)
- Cloud provider APIs (via MCP server)
- Custom organizational tools (via MCP server)

**See**: [MCP Integration Guide](./integrations/mcp-integration.md)

---

### Custom API Tools
**Status**: 🔲 Pattern documented  
**Type**: External API Integration

**Purpose**: Wrap custom REST/GraphQL APIs as tools.

**Integration Pattern**:
```python
@register_tool("custom_api")
class CustomAPITool(BaseTool):
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url
        self.api_key = api_key
    
    async def execute(self, params: Dict) -> ToolResult:
        # Make API call with sanitized params
        pass
```

**See**: [Custom API Tools Guide](./integrations/custom-api-tools.md)

---

## Tool Usage Matrix

| Tool | Phase 0 | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Phase 5 | Phase 6 |
|------|---------|---------|---------|---------|---------|---------|---------|
| **Knowledge Base** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Web Search** | - | - | - | ✅ | ✅ | ✅ | - |
| **Log Analyzer** | - | ✅ | ✅ | - | ✅ | - | - |
| **Data Classifier** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | - |
| **Metrics Analyzer** | - | ✅ | ✅ | - | ✅ | - | - |
| **Trace Analyzer** | - | - | ✅ | - | ✅ | - | - |
| **System Commands** | - | - | - | - | ✅ | ✅ | - |
| **Config Validator** | - | - | - | - | ✅ | ✅ | - |
| **Document Generator** | - | - | - | - | - | - | ✅ |

**Phases**:
- Phase 0: Intake
- Phase 1: Blast Radius
- Phase 2: Timeline
- Phase 3: Hypothesis
- Phase 4: Validation
- Phase 5: Solution
- Phase 6: Documentation

---

## Tool Performance Metrics

| Tool | Avg Latency | Success Rate | Cache Hit Rate |
|------|-------------|--------------|----------------|
| Knowledge Base Search | 200-500ms | 98% | 60% |
| Web Search | 1-2s | 95% | 40% |
| Log Analyzer | 800ms/MB | 99% | N/A |
| Data Classifier | 50-200ms | 99.5% | N/A |

---

**Last Updated**: 2025-10-12  
**Maintained By**: Architecture Team

