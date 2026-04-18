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
**File**: `faultmaven/modules/agent/tools/web_search.py`
**Type**: External API Integration (AgentTool interface)

**Purpose**: Search public internet for technical documentation and solutions when case evidence and knowledge bases lack information. Available as an investigation tool in the DA Tool Loop.

**When Used**:
- Within the DA Tool Loop during Directed Analysis turns (Type B and Type C questions)
- Last resort after `search_file`, `deep_analysis`, and KB tools have been tried
- Stage-aware query enrichment (DIAGNOSIS → root cause terms, MITIGATION → workaround terms, TREATMENT → fix/solution terms)

**Provider Abstraction**:
Two search providers, auto-selected based on configured API keys:
- **TavilyProvider** (preferred): Uses Tavily AI search API. Cleaner content extraction than Google CSE snippets. Requires `TAVILY_API_KEY`.
- **GoogleCSEProvider** (fallback): Google Custom Search Engine with trusted domain filtering via `site:` operators. Requires `WEB_SEARCH_API_KEY` + `WEB_SEARCH_ENGINE_ID`.

If neither provider is configured, the tool is omitted from the DA tool registry.

**Trusted Domains** (Google CSE `site:` filtering):
- stackoverflow.com, serverfault.com
- github.com
- docs.microsoft.com, learn.microsoft.com
- docs.aws.amazon.com
- kubernetes.io, docs.docker.com
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
# Tavily provider (preferred)
TAVILY_API_KEY=tvly-xxx

# Google CSE provider (fallback)
WEB_SEARCH_API_KEY=your_google_api_key
WEB_SEARCH_ENGINE_ID=your_search_engine_id
WEB_SEARCH_API_ENDPOINT=https://www.googleapis.com/customsearch/v1
WEB_SEARCH_MAX_RESULTS=3
```

**Safety Features**:
- Domain whitelist enforcement (Google CSE)
- Result verification disclaimer added to responses
- Stage-aware query enrichment via `ToolContext.metadata["stage"]`

**See**: [Web Search Tool Documentation](./implemented/web-search-tool.md)

---

### Global KB QA Tool
**Status**: ✅ Production
**File**: `faultmaven/modules/agent/tools/kb_tool_adapter.py`
**Type**: Adapter (wraps `AnswerFromGlobalKB` → AgentTool interface)

**Purpose**: Query the system-wide knowledge base for documented solutions, best practices, and historical incident resolutions. Available as an investigation tool in the DA Tool Loop.

**When Used**:
- Within the DA Tool Loop during Directed Analysis turns
- After case evidence tools (`search_file`, `deep_analysis`) when evidence alone doesn't explain the issue
- Before `web_search` — prefer internal knowledge over external search

**Capabilities**:
- Semantic search using BGE-M3 embeddings over global knowledge base (ChromaDB)
- LLM synthesis of retrieved chunks into a focused answer with source citations
- No user scoping — searches the entire global knowledge base

**Cost**: ~$0.01 per call (vector search + LLM synthesis)

---

### User KB QA Tool
**Status**: ✅ Production
**File**: `faultmaven/modules/agent/tools/kb_tool_adapter.py`
**Type**: Adapter (wraps `AnswerFromUserKB` → AgentTool interface)

**Purpose**: Query the user's personal knowledge base for their runbooks, procedures, and custom documentation. Available as an investigation tool in the DA Tool Loop.

**When Used**:
- Within the DA Tool Loop during Directed Analysis turns
- When the user's personal runbooks or procedures may contain relevant troubleshooting steps
- User-scoped: passes `context.user_id` to restrict search to the requesting user's documents

**Capabilities**:
- Semantic search scoped to a specific user's uploaded knowledge documents
- LLM synthesis with source citations
- User isolation enforced via `user_id` parameter

**Cost**: ~$0.01 per call (vector search + LLM synthesis)

---

### Log Analyzer Tool
**Status**: ✅ Production
**File**: `faultmaven/core/processing/log_analyzer.py`
**Type**: Direct Implementation

**Purpose**: Parse, analyze, and extract insights from uploaded log files (Step 2 of data processing pipeline).

**Note**: This tool extracts insights but does NOT create LLM-ready summaries. A preprocessing step (Step 3) is needed to format insights for LLM analysis.

**See Also**: [Complete Data Processing Pipeline](../architecture/data-processing-complete-pipeline.md)

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
**File**: `faultmaven/core/processing/classifier.py`
**Type**: Direct Implementation

**Purpose**: Automatically identify uploaded data types for appropriate processing (Step 1 of data processing pipeline).

**See Also**: [Complete Data Processing Pipeline](../architecture/data-processing-complete-pipeline.md)

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

### Search File Tool (Tier 2)
**Status**: ✅ Production
**File**: `faultmaven/modules/agent/tools/search_file_tool.py`
**Type**: Direct Implementation

**Purpose**: Tier 2 mechanical search over raw evidence file content. Zero LLM cost, on-demand.

**When Used**:
- Tier 1 structural index lacks detail for the user's question
- Agent needs specific lines, values, or patterns from raw file
- Re-running domain extractors with different parameters
- Within the DA Tool Loop (`_tool_augmented_generate()`) during Directed Analysis turns — the LLM iterates the investigation tools (`search_file`, `deep_analysis`, `web_search`, `answer_from_kb` [unified — serves all scopes], `case_evidence_search`) + `schema_tool` for up to 4 iterations

**Search Modes**:
- **keyword** (default): Two-pass strategy. Pass 1 requires ALL keywords on same line (high relevance). Pass 2 falls back to individual keywords with `partial_match: True` (capped at 5 results).
- **regex**: Treats query as regex pattern. Useful for timestamps, error codes, IP addresses.
- **extractor**: Re-runs domain-specific extractor with overridden parameters (e.g., `min_severity`, `z_score_threshold`).

**Evidence Resolution** (dual-path):

- **Path 1 (standalone)**: Query `evidence_artifacts` table by evidence ID → read raw content from `content_ref`
- **Path 2 (case-embedded)**: Load case via `case_repo.get()` → find matching `Evidence` object → read content from `content_ref`

The `Evidence.original_filename` field (set during `_preprocess_attachment()`) provides the display filename in search results instead of the opaque evidence ID.

**Zero-Result Recovery**:
When any search mode returns 0 results, vocabulary extraction runs on the file content (first 100KB):
- Known patterns: HTTP errors, exception names, host:port, IPs, file paths
- Frequent tokens: statistical analysis of token frequency (2-10 occurrences)
- Suggestion string with top 10 discovered terms

**Configuration**:
```env
SEARCH_FILE_MAX_RESULTS=10
SEARCH_FILE_CONTEXT_LINES=20
```

**Performance**:
- Keyword/regex search: <2s on typical files
- Vocabulary extraction: <500ms on ~1MB content

---

### Interpreted Search Tool (deep_analysis)
**Status**: 🟢 Implemented (enabled by default)
**File**: `faultmaven/modules/agent/tools/deep_analysis_tool.py`
**Type**: LLM-Powered Analysis

**Purpose**: Interpreted search — keyword search + a dedicated LLM call to reason over the matched sections in isolation with investigation context. Uses the configured CHAT_PROVIDER, no additional setup needed.

**When Used**:
- Agent needs interpreted analysis ("What caused the error spike at 14:23?")
- Raw keyword matches need cross-line reasoning (pattern recognition, correlation)
- Hypothesis validation requires interpretation beyond exact matching

**How it differs from `search_file`**:
- `search_file`: returns raw matched lines to the main conversation context
- `deep_analysis`: makes a separate, focused LLM call with only the relevant file sections + case context, returns a pre-interpreted answer

**Future**: Will be merged into `search_file` as an `interpret: true` option.

**Backends**:
- `local` (default): Uses configured CHAT_PROVIDER — no extra config needed
- `external`: HTTP call to cloud microservice (enterprise only)

**Configuration**:
```env
# Enabled by default (local). No config needed for standard use.
# DEEP_ANALYSIS_BACKEND=local       # local | external | disabled
# DEEP_ANALYSIS_URL=                # URL for external backend (enterprise only)
# DEEP_ANALYSIS_API_KEY=            # API key for external backend (enterprise only)
```

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
| Global KB QA | 1-3s | 95% | N/A |
| User KB QA | 1-3s | 95% | N/A |
| Log Analyzer | 800ms/MB | 99% | N/A |
| Data Classifier | 50-200ms | 99.5% | N/A |
| Search File (Tier 2) | 0.5-2s | 99% | N/A |
| Interpreted Search (deep_analysis) | 3-15s | 95% | N/A |

---

**Last Updated**: 2026-03-13
**Maintained By**: Architecture Team
