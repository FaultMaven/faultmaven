"""A really-initialized container gives the model a wired ``reclassify_evidence``
when FAULTMAVEN_RECLASSIFY_ENABLED is on, and nothing when it is off (#1941).

The unit counterpart (``tests/unit/modules/agent/test_reclassify_evidence_wired_1941.py``)
drives the builder and the registry directly. This drives ``container.initialize()``,
so it also pins the composition order: the tool is built in ``register_tools``,
before ``register_services`` builds the milestone engine from the registry, and
it must reach that engine's registry holding the deps the infrastructure layer
registered, not ``None``.
"""

import pytest

from faultmaven.modules.agent.domain.services.investigation_service.reclassification import (  # noqa: E501
    EvidenceReclassifier,
)


@pytest.fixture
def reclassify_enabled(monkeypatch):
    """Compose under the flag, whatever the ambient value; the settings
    singleton is rebuilt from the patched environment and again afterwards."""
    from faultmaven.config.settings import reset_settings

    def _set(value: bool) -> None:
        monkeypatch.setenv("FAULTMAVEN_RECLASSIFY_ENABLED", str(value).lower())
        reset_settings()

    yield _set
    monkeypatch.undo()
    reset_settings()


def _engine_tools(container):
    engine = container.milestone_engine
    assert engine is not None, "the container composed no milestone engine"
    registry = engine.deps.investigation_tools
    assert registry is not None, "the milestone engine was given no tool registry"
    return registry


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flag_on_the_engine_is_given_a_wired_tool(
    reclassify_enabled, fresh_container
):
    reclassify_enabled(True)
    await fresh_container.initialize()

    tool = _engine_tools(fresh_container).get("reclassify_evidence")
    assert tool is not None, "flag on, yet the engine's tool registry lacks it"
    reclassifier = tool.reclassifier
    assert isinstance(reclassifier, EvidenceReclassifier)
    # One instance: the tool holds the reclassifier the container registers,
    # which is the one the composition root publishes to the PATCH route.
    assert reclassifier is fresh_container.get_service("evidence_reclassifier")
    assert reclassifier.repository is fresh_container.get_service("case_repository")
    assert reclassifier.file_storage_service is fresh_container.get_service(
        "file_storage_service"
    )
    assert reclassifier.preprocessing_service is fresh_container.get_service(
        "preprocessing_service"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flag_off_the_engine_is_not_given_it(reclassify_enabled, fresh_container):
    reclassify_enabled(False)
    await fresh_container.initialize()

    assert _engine_tools(fresh_container).get("reclassify_evidence") is None
    # Built regardless of the flag: the PATCH route answers 404 off its own
    # flag check, not 503 for a missing reclassifier.
    assert isinstance(
        fresh_container.get_service("evidence_reclassifier"), EvidenceReclassifier
    )
