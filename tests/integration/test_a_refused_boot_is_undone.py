"""A boot refused after composition is undone, and the next one composes afresh (#2242).

The lifespan undid composition only in the shutdown that follows serving, so a
startup refused after ``compose_application`` (here the route-policy gate,
planted to report a problem) left the container initialized with its Redis
client open, the usage ledger installed and the composed services on
``app.state``. The next lifespan on the same app found ``initialize()``
returning early and published the same objects again, so its identity diff
recorded none of them, and its shutdown closed the client while those names
still held it.

The real lifespan, composition root included, on a scratch
``FastAPI(lifespan=lifespan)``: refused once, then booted clean. It is entered
through the app's own lifespan context rather than a ``TestClient``, and takes
``unshared_app_boot`` because a refused boot runs the whole shutdown, which
resets the container singleton and disposes the database engine.
"""

import pytest
from fastapi import FastAPI

from faultmaven.bootstrap.lifespan import lifespan
from faultmaven.infrastructure.llm import usage_ledger

pytestmark = pytest.mark.integration

#: What the composition root publishes that holds the container's Redis client,
#: directly or through a service built over it.
_HOLD_THE_REDIS_CLIENT = ("redis_client", "job_service", "case_service")


async def test_a_refused_boot_is_undone_and_the_next_boot_composes_afresh(
    monkeypatch, unshared_app_boot
):
    from faultmaven.api.middleware import route_policy
    from faultmaven.container import container

    # The shutdown uninstalls the process-global ledger; put back what was there.
    monkeypatch.setattr(
        usage_ledger, "_installed_ledger", usage_ledger.get_installed_usage_ledger()
    )
    app = FastAPI(lifespan=lifespan)
    refused_composition = {}

    def _refuse_after_reading_what_was_composed(app):
        refused_composition["session_store"] = container.session_store
        refused_composition["redis_client"] = app.state.redis_client
        return "planted incoherent policy"

    with monkeypatch.context() as refusing:
        refusing.setattr(
            route_policy,
            "assert_policy_coherent",
            _refuse_after_reading_what_was_composed,
        )
        with pytest.raises(RuntimeError, match="planted incoherent policy"):
            async with app.router.lifespan_context(app):
                pytest.fail("the lifespan must not yield after a refusal")

    assert refused_composition["redis_client"] is not None, "composition never ran"
    assert container.is_initialized is False, "the refused boot left it initialized"
    left = [name for name in _HOLD_THE_REDIS_CLIENT if name in app.state]
    assert left == [], f"the refused boot left {left} published"
    assert usage_ledger.get_installed_usage_ledger() is None

    async with app.router.lifespan_context(app):
        assert container.is_initialized is True
        assert container.session_store is not refused_composition["session_store"], (
            "the second boot was handed the refused boot's composition: the "
            "container was still initialized"
        )
        assert app.state.redis_client is not None
        assert container.session_store.redis_client is app.state.redis_client

    left = [name for name in _HOLD_THE_REDIS_CLIENT if name in app.state]
    assert left == []
