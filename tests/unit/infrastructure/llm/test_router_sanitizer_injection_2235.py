"""The router redacts with the sanitizer it is given, never a silent own (#2235).

``LLMRouter.__init__`` used to build a ``DataSanitizer`` of its own, so every
composition built a second one beside the container's and health-checked
Presidio twice more. The composition root now assigns the container's instance
after construction (a ``LLM_ROUTER_CLASS`` keeps its zero-argument
constructor). A router nothing was injected into still redacts: it builds one
on first read, and logs that it did.
"""

import logging

import pytest

from faultmaven.config.settings import reset_settings
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.infrastructure.security.redaction import DataSanitizer
from tests.utils import sanitizer_constructions

pytestmark = [pytest.mark.unit, pytest.mark.security]

SELF_BUILT = "redacting with a sanitizer it built itself"


@pytest.fixture
def constructions(monkeypatch):
    """Constructions counted, with service checks pinned on so the fallback's
    own sanitizer never health-checks Presidio."""
    monkeypatch.setenv("SKIP_SERVICE_CHECKS", "true")
    reset_settings()
    yield sanitizer_constructions(monkeypatch)
    monkeypatch.undo()
    reset_settings()


def _self_built_warnings(caplog):
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and SELF_BUILT in record.getMessage()
    ]


def test_an_injected_sanitizer_is_the_one_the_router_redacts_with(
    constructions, caplog
):
    """The composition's path: nothing built, nothing logged."""
    router = LLMRouter()
    injected = object()
    with caplog.at_level(logging.WARNING):
        router.sanitizer = injected

        assert router.sanitizer is injected
    assert constructions == [], "the router built a sanitizer of its own"
    assert _self_built_warnings(caplog) == []


def test_a_router_given_none_builds_one_on_first_read_and_says_so(
    constructions, caplog
):
    """Built outside the composition root, the router still redacts, with one
    sanitizer it builds once, and the second construction is not silent."""
    router = LLMRouter()
    assert constructions == [], "the router built its sanitizer before any read"

    with caplog.at_level(logging.WARNING):
        first = router.sanitizer
        second = router.sanitizer

    assert isinstance(first, DataSanitizer)
    assert second is first
    assert constructions == [first]
    assert len(_self_built_warnings(caplog)) == 1


def test_the_router_refuses_a_none_sanitizer(constructions):
    """``should_redact(None)`` is false: a None here would send unredacted."""
    router = LLMRouter()
    injected = object()
    router.sanitizer = injected

    with pytest.raises(TypeError, match="cannot be None"):
        router.sanitizer = None
    assert router.sanitizer is injected
