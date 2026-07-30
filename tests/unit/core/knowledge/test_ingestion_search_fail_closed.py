"""An unavailable embedder is not an empty knowledge base (#868).

``KnowledgeIngester.search`` embeds the query before it can search. When the
embedding model cannot be loaded there is nothing to search *with* — but the
old code logged and returned ``[]``, which the KB tool renders as "No relevant
information found in the knowledge base." The investigation engine then reasons
from an affirmative negative it was never entitled to: absence of evidence
presented as evidence of absence, the incorrect conclusion the engine exists to
avoid.

This became reachable when the model stopped loading during construction: the
ingester used to be dropped entirely when BGE-M3 was unavailable, taking both
KB tools with it, so the agent had no KB tool rather than a lying one.

Availability failures must therefore surface as failures, all the way to the
tool boundary.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.settings import DeploymentMode, FaultMavenSettings
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.agent.tools.knowledge_base import KnowledgeBaseTool
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester

pytestmark = [pytest.mark.unit]


def _ingester(tmp_path) -> KnowledgeIngester:
    settings = FaultMavenSettings(_env_file=None)
    settings.deployment_mode = DeploymentMode.STANDALONE
    settings.database.chromadb_url = ""
    settings.database.chromadb_host = "localhost"
    settings.database.vector_storage_type = "chromadb"
    settings.database.chromadb_kb_persist_dir = str(tmp_path / "chroma-kb")
    settings.server.skip_service_checks = False
    return KnowledgeIngester(settings=settings)


@pytest.mark.asyncio
async def test_search_raises_when_the_embedder_is_unavailable(tmp_path):
    ingester = _ingester(tmp_path)

    with patch(
        "faultmaven.modules.knowledge.domain.services.ingestion.model_cache"
    ) as mc:
        mc.aembed_query = AsyncMock(return_value=None)

        with pytest.raises(KnowledgeBaseError, match="embedding model"):
            await ingester.search(query="disk pressure on node-3")


@pytest.mark.asyncio
async def test_the_blanket_handler_does_not_swallow_it(tmp_path):
    """``search`` ends in ``except Exception: return []``. The typed
    availability error has to survive that, or the fix is inert — the raise
    would be caught three lines later and become the same silent []."""
    ingester = _ingester(tmp_path)

    with patch(
        "faultmaven.modules.knowledge.domain.services.ingestion.model_cache"
    ) as mc:
        mc.aembed_query = AsyncMock(return_value=None)
        result = None
        try:
            result = await ingester.search(query="anything")
        except KnowledgeBaseError:
            pass

    assert result is None, "the availability error was swallowed into a result"


@pytest.mark.asyncio
async def test_tool_reports_unavailability_not_an_empty_knowledge_base():
    """The property that actually matters, at the boundary the LLM reads."""
    ingester = MagicMock()
    ingester.search = AsyncMock(
        side_effect=KnowledgeBaseError(
            "Knowledge base search unavailable: the embedding model could not "
            "be loaded",
            error_code="KNOWLEDGE_EMBEDDER_UNAVAILABLE",
        )
    )
    tool = KnowledgeBaseTool(knowledge_ingester=ingester)

    answer = await tool._arun(query="disk pressure on node-3")

    assert "No relevant information found" not in answer
    assert "unavailable" in answer.lower() or "error" in answer.lower()


@pytest.mark.asyncio
async def test_a_genuinely_empty_knowledge_base_still_reads_as_empty():
    """The gate must be able to pass: a real search returning nothing is still
    an honest 'nothing found', not an error."""
    ingester = MagicMock()
    ingester.search = AsyncMock(return_value=[])
    tool = KnowledgeBaseTool(knowledge_ingester=ingester)

    answer = await tool._arun(query="disk pressure on node-3")

    assert "No relevant information found" in answer
