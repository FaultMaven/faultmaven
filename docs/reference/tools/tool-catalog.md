# Tool Catalog

The tools the model can be given during an investigation, from `_create_investigation_tools` (`faultmaven/container/providers/services.py`). All live in `faultmaven/modules/agent/tools/` and subclass `AgentTool`. Which ones a turn actually offers depends on configuration and on the turn (terminal Q&A turns offer only `TERMINAL_QA_TOOLS`, `core/investigation/milestone_engine/terminal_turns.py`). The tool descriptions the model reads are in each tool's `description` property.

| Tool (model-facing name) | File | Given when |
|---|---|---|
| `search_file` | `search_file_tool.py` | always, when constructed |
| `deep_analysis` | `deep_analysis_tool.py` | unless `DEEP_ANALYSIS_BACKEND=disabled` |
| `web_search` | `web_search.py` | `ENABLE_WEB_SEARCH` on and a provider key configured |
| `kb_qa` | `kb_tool_adapter.py` (`KBToolAdapter`, wraps `kb_qa.py`) | always, when the KB tool is constructed |
| `case_evidence_search` | `kb_tool_adapter.py` (`CaseEvidenceQAAdapter`, wraps `case_evidence_qa.py`) | with a case vector store |
| `vectorize_file` | `vectorize_file_tool.py` | only beside `case_evidence_search` |
| `find_entity`, `list_top_entities` | `find_entity_tool.py`, `list_top_entities_tool.py` | `FAULTMAVEN_ENTITY_REGISTRY` on |
| `reclassify_evidence` | `reclassify_evidence_tool.py` | `FAULTMAVEN_RECLASSIFY_ENABLED` on |

There is one `kb_qa`, scoped by the case's retrieval filter (`ToolContext.kb_scope_filter`); there is no separate global or per-user KB tool. Its only parameter is `question`.

## Tools

### `kb_qa`
Searches the knowledge base (global, personal and team runbooks the case driver can access) and answers from the retrieved chunks. Parameter: `question` (required). The earlier `KnowledgeBaseTool` was deleted, not wired (#943).

### `case_evidence_search`
Semantic search over this case's vectorized evidence. Parameter: `question` (required). Given together with `vectorize_file`, or neither.

### `vectorize_file`
Chunks and embeds an uploaded evidence file so `case_evidence_search` can read it. Parameter: `evidence_id` (required).

### `search_file`
Searches one uploaded file of the case for specific information. Parameters: `evidence_id` (an evidence row or an upload's `file_id`) and `query`, both required. An id that is not on the case returns the list of files the tool can read.

### `deep_analysis`
Runs a focused model call over the raw file to answer a question the structural summary cannot. Parameters: `evidence_id` and `query`, both required. Uses the configured `CHAT_PROVIDER`. `DEEP_ANALYSIS_BACKEND` is `local` (default) or `disabled`; any other value fails startup.

### `web_search`
Searches trusted technical sites (`TRUSTED_DOMAINS` in `web_search.py`) for error messages and documentation. Parameter: `query` (required). Providers are chosen by configured keys: Tavily (`TAVILY_API_KEY`, preferred) or Google Custom Search (`WEB_SEARCH_API_KEY` plus `WEB_SEARCH_ENGINE_ID`). With neither, or with `ENABLE_WEB_SEARCH=false`, the tool is not registered.

### `find_entity` and `list_top_entities`
Look up one entity value across the case's evidence (`find_entity`, parameter `entity_value`), or tally the most-mentioned entities of a type (`list_top_entities`, parameter `entity_type`).

### `reclassify_evidence`
Re-extracts an evidence file under a different data type when the user corrects the classifier. Parameters: `evidence_id` and `data_type`, both required. It writes to the live case aggregate, so terminal Q&A turns do not offer it.

## Not built

These appear in older designs and have no code: a document generator, a metrics analyzer, a trace analyzer, a system-commands tool, a configuration validator, an MCP client or server (see [MCP Integration](./integrations/mcp-integration.md), design only), and a custom-API tool. To add one, follow the [Developer Guide](./developer-guide.md).
