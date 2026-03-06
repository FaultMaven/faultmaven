"""Tools layer providers.

This module contains factory functions for tool creation:
- Core tools via tool registry
- Document Q&A tools (case evidence, user KB, global KB)
- Knowledge ingester
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List

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
        from faultmaven.core.knowledge.ingestion import KnowledgeIngester

        return KnowledgeIngester(settings=settings)
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

    # Web search tool is optional (may be disabled if API key is missing).
    try:
        from faultmaven.modules.agent.tools.web_search import WebSearchTool

        tools.append(WebSearchTool(settings=settings))
    except Exception as e:
        logger.warning(f"Web search tool creation failed: {e}")

    return tools


def create_document_qa_tools(
    case_vector_store: Any | None,
    user_kb_vector_store: Any | None,
    llm_provider: Any,
    settings: FaultMavenSettings,
) -> dict[str, Any | None]:
    """Create document Q&A tools using strategy pattern.

    Three tool instances from one base class, configured differently:
    - Case Evidence: case-scoped forensic analysis
    - User KB: user-scoped personal runbooks
    - Global KB: system-wide best practices

    Args:
        case_vector_store: Vector store for case evidence
        user_kb_vector_store: Vector store for user knowledge base
        llm_provider: LLM provider for Q&A
        settings: Application settings

    Returns:
        Dict with tool instances (may be None if unavailable)
    """
    result = {
        "case_evidence_qa_tool": None,
        "user_kb_qa_tool": None,
        "global_kb_qa_tool": None,
    }

    if settings.server.skip_service_checks or not case_vector_store:
        logger.debug(
            "Document Q&A tools skipped (no case_vector_store or SKIP_SERVICE_CHECKS=True)"
        )
        return result

    try:
        from faultmaven.modules.agent.tools.case_evidence_qa import (
            AnswerFromCaseEvidence,
        )
        from faultmaven.modules.agent.tools.global_kb_qa import AnswerFromGlobalKB
        from faultmaven.modules.agent.tools.user_kb_qa import AnswerFromUserKB

        # Tool 1: Case Evidence (case-scoped forensic analysis)
        result["case_evidence_qa_tool"] = AnswerFromCaseEvidence(
            vector_store=case_vector_store,
            llm_router=llm_provider,
        )

        # Tool 2: User KB (user-scoped personal runbooks)
        if user_kb_vector_store:
            result["user_kb_qa_tool"] = AnswerFromUserKB(
                vector_store=user_kb_vector_store,
                llm_router=llm_provider,
            )
        else:
            logger.warning(
                "User KB QA tool skipped (user_kb_vector_store not available)"
            )

        # Tool 3: Global KB (system-wide best practices)
        result["global_kb_qa_tool"] = AnswerFromGlobalKB(
            vector_store=case_vector_store,
            llm_router=llm_provider,
        )

        logger.info("✅ Created document Q&A tools (case evidence, user KB, global KB)")

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

    # Create registry tools
    tools = create_registry_tools(ingester, settings)
    container.tools = tools

    # Create document Q&A tools
    llm_provider = container.get_service("llm_provider")
    case_vector_store = getattr(container, "case_vector_store", None)
    user_kb_vector_store = getattr(container, "user_kb_vector_store", None)

    qa_tools = create_document_qa_tools(
        case_vector_store,
        user_kb_vector_store,
        llm_provider,
        settings,
    )

    # Set tool instances on container
    container.case_evidence_qa_tool = qa_tools["case_evidence_qa_tool"]
    container.user_kb_qa_tool = qa_tools["user_kb_qa_tool"]
    container.global_kb_qa_tool = qa_tools["global_kb_qa_tool"]

    # Add Q&A tools to tools list
    tools_to_add = [
        qa_tools["case_evidence_qa_tool"],
        qa_tools["global_kb_qa_tool"],
    ]
    if qa_tools["user_kb_qa_tool"]:
        tools_to_add.insert(1, qa_tools["user_kb_qa_tool"])

    container.tools.extend([t for t in tools_to_add if t is not None])

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
    preprocessing_service = (
        container.get_service("preprocessing_service")
        if hasattr(container, "get_service")
        else None
    )
    storage_service = (
        container.get_service("storage_service")
        if hasattr(container, "get_service")
        else None
    )
    try:
        from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool

        search_file_tool = SearchFileTool(
            storage_service=storage_service,
            preprocessing_service=preprocessing_service,
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

    logger.info(f"✅ Tools layer registered: {len(container.tools)} tools")
