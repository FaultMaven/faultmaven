"""Fixtures for real-LLM integration tests.

The provider fixture is the load-bearing piece: it constructs a real LLM
client gated on an API key in env. Tests that depend on it are
automatically skipped when no key is present, so a developer without
provider access can still run the rest of the suite via `pytest` (the
default config also excludes the `real_llm` marker).

To run real-LLM tests:

    export ANTHROPIC_API_KEY=sk-ant-...
    export FM_TEST_ALLOW_LIVE_NETWORK=1   # the session's network guard refuses otherwise
    pytest -m real_llm

To override the default model:

    export REAL_LLM_TEST_MODEL=claude-sonnet-4-6
    pytest -m real_llm
"""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderRegistry,
    ProviderState,
)
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.infrastructure.security.case_redaction import (
    CaseRedactionContext,
    should_redact,
)
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.modules.case.contracts import Case, CaseState, InquiryData

# Default model: Haiku 4.5 — cheapest current Anthropic model. Tests use
# temperature=0 where the engine surface allows; flakiness policy is
# documented in docs/development/testing/standards.md.
DEFAULT_REAL_LLM_MODEL = "claude-haiku-4-5-20251001"


@pytest.fixture
def anthropic_api_key() -> str:
    """Return the Anthropic API key from env, or skip the test if absent.

    Real-LLM tests are opt-in. Tests requiring a real provider depend on
    this fixture (transitively via ``real_llm_provider``) and will skip
    cleanly when the key is missing rather than failing the suite.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip(
            "ANTHROPIC_API_KEY not set — real-LLM tests skipped. "
            "Set the key in env and run `pytest -m real_llm` to execute."
        )
    return key


@pytest.fixture
def real_llm_provider(anthropic_api_key: str) -> AnthropicProvider:
    """Construct a real Anthropic provider for integration testing.

    Default model is Haiku (cheapest); override via REAL_LLM_TEST_MODEL.
    The provider hits ``https://api.anthropic.com/v1``; the engine reaches it
    through :func:`real_llm_router`, never directly.
    """
    model = os.environ.get("REAL_LLM_TEST_MODEL", DEFAULT_REAL_LLM_MODEL)
    config = ProviderConfig(
        name="anthropic",
        api_key=anthropic_api_key,
        base_url="https://api.anthropic.com/v1",
        models=[model],
        default_model=model,
        timeout=30,
    )
    return AnthropicProvider(config)


@pytest.fixture
def real_llm_router(real_llm_provider: AnthropicProvider, monkeypatch) -> LLMRouter:
    """A real ``LLMRouter`` whose registry holds only the real provider.

    The engine sends as production does (#1998): through
    ``dispatch.route_model_call`` to a router, whose registry hands the call to
    ``dispatch.send_to_model``. A bare provider has no ``route``, and the seam
    refuses content the redaction module did not mint. No fallback chain: the
    one provider answers or the call fails.
    """
    registry = ProviderRegistry(settings=None)
    registry._providers = {"anthropic": real_llm_provider}
    registry._fallback_chain = ["anthropic"]
    registry._provider_states = {"anthropic": ProviderState(name="anthropic")}
    registry._initialized = True
    monkeypatch.setattr(
        "faultmaven.infrastructure.llm.router.get_registry", lambda: registry
    )
    return LLMRouter()


@pytest.fixture
def stub_repo():
    """Minimal in-memory case repo for engine construction.

    ``save`` returns the case unchanged so the engine's persistence
    points are exercised without a real database. ``get`` is a MagicMock
    that tests can override if needed.
    """
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock()
    return repo


@pytest.fixture
def fresh_inquiry_case() -> Case:
    """Fresh case in INQUIRY status with no proposed_problem_statement.

    Use this as the starting state for tests that exercise a full
    INQUIRY flow (problem statement composition, handshake, transition).
    """
    return Case(
        case_id="case_realllm00001",
        title="Real-LLM Integration Test",
        state=CaseState.INQUIRY,
        user_id="user_realllm",
        enterprise_id="org_realllm",
        description="",
        inquiry=InquiryData(thread_id="thread_realllm"),
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def case_redaction(fresh_inquiry_case: Case) -> CaseRedactionContext:
    """The turn's case redaction context, built as the engine builds it
    (``turn_generation._generate_turn_response``): the minting helpers take
    no other (#1998), and never ``None``."""
    sanitizer = DataSanitizer()
    return CaseRedactionContext(
        case_id=fresh_inquiry_case.case_id,
        sanitizer=sanitizer,
        enabled=should_redact(sanitizer),
    )
