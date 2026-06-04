"""Fixtures for real-LLM integration tests.

The provider fixture is the load-bearing piece: it constructs a real LLM
client gated on an API key in env. Tests that depend on it are
automatically skipped when no key is present, so a developer without
provider access can still run the rest of the suite via `pytest` (the
default config also excludes the `real_llm` marker).

To run real-LLM tests:

    export ANTHROPIC_API_KEY=sk-ant-...
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
    The provider hits ``https://api.anthropic.com/v1`` directly — no
    router, no fallback chain, no DI container. The point is to exercise
    the engine's interaction with a real LLM, not the provider-routing
    machinery (which has its own tests).
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
def stub_repo():
    """Minimal in-memory case repo for engine construction.

    ``save`` returns the case unchanged so the engine's persistence
    points are exercised without a real database. ``get`` is a MagicMock
    that tests can override if needed.
    """
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
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
        organization_id="org_realllm",
        description="",
        inquiry=InquiryData(thread_id="thread_realllm"),
        created_at=datetime.now(timezone.utc),
    )
