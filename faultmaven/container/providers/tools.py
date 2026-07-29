"""Tools layer providers.

This module contains factory functions for tool creation:
- Core tools via tool registry
- Document Q&A tools (case evidence, user KB, global KB)
- Knowledge ingester
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List

from faultmaven.config.deployment_coherence import DeploymentCoherenceError

if TYPE_CHECKING:
    from faultmaven.config.settings import FaultMavenSettings
    from faultmaven.container.base import BaseDIContainer

logger = logging.getLogger(__name__)


def create_knowledge_ingester(settings: FaultMavenSettings) -> Any | None:
    """Create knowledge ingester for tools that need it."""
    if settings.server.skip_service_checks:
        logger.debug("KnowledgeIngester skipped (SKIP_SERVICE_CHECKS=True)")
        return None

    try:
        from faultmaven.modules.knowledge.domain.services.ingestion import (
            KnowledgeIngester,
        )

        return KnowledgeIngester(settings=settings)
    except DeploymentCoherenceError:
        # A boot-refusal (e.g. ChromaUnavailableError under cloud, #901) must
        # propagate — swallowing it here would re-open the fail-open path this
        # gate exists to close.
        raise
    except Exception as e:
        logger.warning(f"KnowledgeIngester creation failed: {e}")
        return None


def create_registry_tools(
    ingester: Any | None,
    settings: FaultMavenSettings,
) -> List[Any]:
    """Create tools (no decorator/registry side effects).

    We intentionally avoid any self-registration decorators or global registries.
    Tools should be constructed explicitly with their required dependencies.
    """
    tools: List[Any] = []

    # Knowledge base tools require the ingester. If it is unavailable, skip.
    if ingester is not None:
        try:
            from faultmaven.modules.agent.tools.knowledge_base import (
                KnowledgeBaseFilteredTool,
                KnowledgeBaseTool,
            )

            tools.append(KnowledgeBaseTool(knowledge_ingester=ingester))
            tools.append(KnowledgeBaseFilteredTool(knowledge_ingester=ingester))
        except Exception as e:
            logger.warning(f"Knowledge base tool creation failed: {e}")

    # Web search tool is optional (may be disabled if no API key configured).
    try:
        from faultmaven.modules.agent.tools.web_search import WebSearchTool

        web_search = WebSearchTool(settings=settings)
        if web_search.is_available():
            tools.append(web_search)
        else:
            web_search = None
            logger.debug("Web search tool skipped (no search provider configured)")
    except Exception as e:
        web_search = None
        logger.warning(f"Web search tool creation failed: {e}")

    return tools


def create_document_qa_tools(
    case_vector_store: Any | None,
    knowledge_vector_store: Any | None,
    llm_provider: Any,
    settings: FaultMavenSettings,
) -> dict[str, Any | None]:
    """Create document Q&A tools.

    Two tools:
    - KB Q&A: unified search across all KB scopes (global + personal + team)
    - Case Evidence Q&A: case-scoped forensic analysis

    Args:
        case_vector_store: CaseVectorStore for case evidence (case_{case_id} collections)
        knowledge_vector_store: KnowledgeVectorStore for KB (faultmaven_kb collection)
        llm_provider: LLM provider for Q&A synthesis
        settings: Application settings

    Returns:
        Dict with tool instances (may be None if unavailable)
    """
    result = {
        "case_evidence_qa_tool": None,
        "kb_qa_tool": None,
    }

    if settings.server.skip_service_checks:
        logger.debug("Document Q&A tools skipped (SKIP_SERVICE_CHECKS=True)")
        return result

    try:
        # Case Evidence Q&A (case-scoped, separate collections)
        if case_vector_store:
            from faultmaven.modules.agent.tools.case_evidence_qa import (
                AnswerFromCaseEvidence,
            )

            result["case_evidence_qa_tool"] = AnswerFromCaseEvidence(
                vector_store=case_vector_store,
                llm_router=llm_provider,
            )

        # Unified KB Q&A (all scopes, metadata-filtered on faultmaven_kb)
        if knowledge_vector_store:
            from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB

            result["kb_qa_tool"] = AnswerFromKB(
                vector_store=knowledge_vector_store,
                llm_router=llm_provider,
            )

        logger.info("✅ Created document Q&A tools (case evidence, unified KB)")

    except Exception as e:
        logger.warning(f"Document Q&A tools creation failed: {e}")

    return result


def register_tools(container: BaseDIContainer) -> None:
    """Register all tools with the container.

    Args:
        container: The DI container to register tools with
    """
    settings = container.settings

    logger.info("🔍 Tools: Registering tools...")

    # Create knowledge ingester
    ingester = create_knowledge_ingester(settings)
    if ingester:
        container._register_service("knowledge_ingester", ingester)
    container.knowledge_ingester = ingester

    # Create registry tools (includes web search if configured)
    tools = create_registry_tools(ingester, settings)
    container.tools = tools

    # Extract web_search_tool from registry tools for DA loop registration
    from faultmaven.modules.agent.tools.web_search import WebSearchTool

    container.web_search_tool = next(
        (t for t in tools if isinstance(t, WebSearchTool)), None
    )

    # Create document Q&A tools
    llm_provider = container.get_service("llm_provider")
    case_vector_store = getattr(container, "case_vector_store", None)

    # Create knowledge vector store for KB collections
    from faultmaven.container.providers.infrastructure import (
        create_knowledge_vector_store,
    )

    kb_chromadb_client = getattr(container, "kb_chromadb_client", None)
    knowledge_vector_store = create_knowledge_vector_store(settings, kb_chromadb_client)
    container.knowledge_vector_store = knowledge_vector_store

    qa_tools = create_document_qa_tools(
        case_vector_store,
        knowledge_vector_store,
        llm_provider,
        settings,
    )

    # Set tool instances on container
    container.case_evidence_qa_tool = qa_tools["case_evidence_qa_tool"]
    container.kb_qa_tool = qa_tools["kb_qa_tool"]

    # Add Q&A tools to tools list
    container.tools.extend([t for t in qa_tools.values() if t is not None])

    # Create KB adapters (AgentTool wrappers for the DA loop)
    from faultmaven.modules.agent.tools.kb_tool_adapter import (
        CaseEvidenceQAAdapter,
        KBToolAdapter,
    )

    container.kb_adapter = (
        KBToolAdapter(wrapped_tool=qa_tools["kb_qa_tool"])
        if qa_tools["kb_qa_tool"]
        else None
    )
    container.case_evidence_qa_adapter = (
        CaseEvidenceQAAdapter(wrapped_tool=qa_tools["case_evidence_qa_tool"])
        if qa_tools["case_evidence_qa_tool"]
        else None
    )

    # Deep analysis tool (LLM-interpreted analysis)
    deep_analysis_service = (
        container.get_service("deep_analysis_service")
        if hasattr(container, "get_service")
        else None
    )
    if deep_analysis_service:
        from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool

        deep_analysis_tool = DeepAnalysisTool(tier2_service=deep_analysis_service)
        container.deep_analysis_tool = deep_analysis_tool
        container.tools.append(deep_analysis_tool)
        logger.info("Deep analysis tool registered (DA backend active)")

    # Search file tool (mechanical search)
    # Must match the registered name in services.py (`file_storage_service`).
    # Previously keyed as "storage_service", which silently returned None
    # because that name was never registered.
    storage_service = (
        container.get_service("file_storage_service")
        if hasattr(container, "get_service")
        else None
    )
    try:
        from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool

        search_file_tool = SearchFileTool(
            storage_service=storage_service,
        )
        container.search_file_tool = search_file_tool
        container.tools.append(search_file_tool)
        logger.info("Search file tool registered (mechanical search)")
    except Exception as e:
        logger.warning(f"Search file tool registration failed: {e}")

    # Vectorize file tool (on-demand vectorization)
    case_vector_store = (
        container.get_service("case_vector_store")
        if hasattr(container, "get_service")
        else None
    )
    try:
        from faultmaven.modules.agent.tools.vectorize_file_tool import VectorizeFileTool

        vectorize_file_tool = VectorizeFileTool(
            case_vector_store=case_vector_store,
            storage_service=storage_service,
        )
        container.vectorize_file_tool = vectorize_file_tool
        container.tools.append(vectorize_file_tool)
        logger.info("Vectorize file tool registered (auto-triggered on DA failure)")
    except Exception as e:
        logger.warning(f"Vectorize file tool registration failed: {e}")

    # Phase 1.5 — Reclassify evidence tool. Registered conditionally on
    # the feature flag so the LLM sees the tool in its function-calling
    # menu only when the operator has opted in.
    try:
        from faultmaven.config.settings import get_settings

        if get_settings().preprocessing.reclassify_enabled:
            from faultmaven.modules.agent.tools.reclassify_evidence_tool import (
                ReclassifyEvidenceTool,
            )

            investigation_service = (
                container.get_service("investigation_service")
                if hasattr(container, "get_service")
                else None
            )
            reclassify_evidence_tool = ReclassifyEvidenceTool(
                investigation_service=investigation_service,
            )
            container.reclassify_evidence_tool = reclassify_evidence_tool
            container.tools.append(reclassify_evidence_tool)
            logger.info("Reclassify evidence tool registered (Phase 1.5 escape hatch)")
        else:
            logger.info(
                "Reclassify evidence tool NOT registered "
                "(FAULTMAVEN_RECLASSIFY_ENABLED=false)"
            )
    except Exception as e:
        logger.warning(f"Reclassify evidence tool registration failed: {e}")

    # Phase 3b — list_evidence_by_time tool. Case-level timeline
    # queries across evidence coverage_*_ts columns. Always registered
    # when the case repository is available — behaviour-neutral for
    # existing cases (their coverage columns are NULL and the query
    # naturally excludes them).
    try:
        from faultmaven.modules.agent.tools.list_evidence_by_time_tool import (
            ListEvidenceByTimeTool,
        )

        case_repository = (
            container.get_service("case_repository")
            if hasattr(container, "get_service")
            else None
        )
        if case_repository is not None:
            list_evidence_by_time_tool = ListEvidenceByTimeTool(
                case_repository=case_repository,
            )
            container.list_evidence_by_time_tool = list_evidence_by_time_tool
            container.tools.append(list_evidence_by_time_tool)
            logger.info("List-evidence-by-time tool registered (Phase 3b)")
        else:
            logger.info(
                "List-evidence-by-time tool NOT registered "
                "(case_repository unavailable)"
            )
    except Exception as e:
        logger.warning(f"List-evidence-by-time tool registration failed: {e}")

    # Phase 4c — entity registry tools. ``find_entity`` and
    # ``list_top_entities`` expose the ``case_entities`` table built by
    # Phase 4b. Gated on the same feature flag that controls the
    # producer side — the tools would always return empty results with
    # the flag off, so offering them to the LLM would just invite
    # misleading queries.
    try:
        from faultmaven.config.settings import get_settings

        if get_settings().preprocessing.entity_registry_enabled:
            from faultmaven.modules.agent.tools.find_entity_tool import FindEntityTool
            from faultmaven.modules.agent.tools.list_top_entities_tool import (
                ListTopEntitiesTool,
            )

            case_repository = (
                container.get_service("case_repository")
                if hasattr(container, "get_service")
                else None
            )
            if case_repository is not None:
                find_entity_tool = FindEntityTool(case_repository=case_repository)
                list_top_entities_tool = ListTopEntitiesTool(
                    case_repository=case_repository,
                )
                container.find_entity_tool = find_entity_tool
                container.list_top_entities_tool = list_top_entities_tool
                container.tools.append(find_entity_tool)
                container.tools.append(list_top_entities_tool)
                logger.info(
                    "Entity registry tools registered (Phase 4c): "
                    "find_entity, list_top_entities"
                )
            else:
                logger.info(
                    "Entity registry tools NOT registered "
                    "(case_repository unavailable)"
                )
        else:
            logger.info(
                "Entity registry tools NOT registered "
                "(FAULTMAVEN_ENTITY_REGISTRY=false)"
            )
    except Exception as e:
        logger.warning(f"Entity registry tool registration failed: {e}")

    logger.info(f"✅ Tools layer registered: {len(container.tools)} tools")
