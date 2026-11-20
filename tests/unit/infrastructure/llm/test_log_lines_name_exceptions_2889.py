"""The llm layer's log lines name an exception by its class and code, and a
subprocess's output by its exit code and length, never the text (#2889 S3a).

Principles §7, "What a log line may hold", rule 1: "A message that interpolates
an exception's text, a payload, or a user-supplied value is not metadata. A
first-party exception contributes its class and its ``error_code`` ..., not its
message." Stage S3 of #2886.

Each case drives one site with :data:`MARKER` in the exception's message, the
subprocess's output or the response body, captures every record at DEBUG, and
renders each through the real chain's formatter (``build_log_formatter``), so
the message and the ``extra`` fields are both on the line. The marker is on no
rendered line, and the class name (with its code, or the length) is on the
site's line.
"""

import io
import json
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm import pricing
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.local_llm_manager import LocalLLMServiceManager
from faultmaven.infrastructure.llm.metering import record_provider_call
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    create_strategy_for_capability,
)
from faultmaven.infrastructure.logging.config import build_log_formatter
from faultmaven.utils.schema_converter import StrictSchemaUnsupported
from tests.model_content import vetted
from tests.unit.infrastructure.llm.providers.test_chain_failure_order_2833 import (
    _registry,
    _Stub,
)
from tests.unit.infrastructure.llm.providers.test_local_tool_calls import _mock_session
from tests.unit.infrastructure.llm.providers.test_openai_prompt_cache_breakpoint import (
    _azure,
    _loop_messages,
    _ok,
    _refuses_breakpoint,
    _send,
)

pytestmark = [pytest.mark.unit, pytest.mark.llm, pytest.mark.security]

#: The text every case plants. Plain letters and digits, so the redactor cannot
#: be what keeps it off the line.
MARKER = "zqxmarker2889"


@pytest.fixture
def lines(caplog):
    """Every record the test emits, at DEBUG, as the chain's JSON lines."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="faultmaven")
    formatter = build_log_formatter("json", False, io.StringIO())

    def render() -> list[str]:
        return [formatter.format(record) for record in caplog.records]

    return render


def _named(rendered: list[str], event: str, *present: str) -> dict:
    """The marker is on no line; the one line whose event holds ``event``
    carries every ``present`` text. Returns that line's fields."""
    assert rendered, "nothing was logged"
    for line in rendered:
        assert MARKER not in line, line
    hits = [line for line in rendered if event in json.loads(line)["event"]]
    assert hits, f"no line holds {event!r}: {rendered}"
    for text in present:
        assert any(text in line for line in hits), (text, hits)
    return json.loads(hits[0])


@pytest.fixture
def _no_metering():
    with patch(
        "faultmaven.infrastructure.llm.providers.registry.record_provider_call",
        lambda *a, **k: None,
    ):
        yield


def _auth_failure():
    return LLMException(f"401 invalid key {MARKER}", status_code=401)


# ── providers/registry.py ─────────────────────────────────────────────────


@pytest.mark.usefixtures("_no_metering")
async def test_a_provider_failure_is_named_by_class_and_code(lines):
    registry = _registry(_Stub(_auth_failure), _Stub(_auth_failure))
    with pytest.raises(LLMException):
        await registry.route_request(prompt=vetted("why is node-3 down?"))
    rendered = lines()
    failed = _named(rendered, "primary failed", "LLMException[PROVIDER_AUTH_FAILED]")
    assert failed["category"] == "request_rejected"
    _named(rendered, "All providers failed", "LLMException[PROVIDER_AUTH_FAILED]")


# ── router.py ─────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("_no_metering")
async def test_the_router_names_the_chain_failure_by_class_and_code(lines):
    registry = _registry(_Stub(_auth_failure), _Stub(_auth_failure))
    with patch(
        "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
    ):
        with pytest.raises(LLMException):
            await LLMRouter().route(prompt=vetted("node-3"), bypass_cache=True)
    _named(
        lines(),
        "LLM Router: All providers failed",
        "LLMException[PROVIDER_AUTH_FAILED]",
    )


# ── local_llm_manager.py ──────────────────────────────────────────────────


def _script(tmp_path):
    script = tmp_path / "llm-service.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    return str(script)


def _ran(returncode):
    return subprocess.CompletedProcess(
        ["x"], returncode, stdout=f"out {MARKER}", stderr=f"err {MARKER}!"
    )


def test_a_failed_script_is_named_by_exit_and_lengths(lines, tmp_path):
    manager = LocalLLMServiceManager(script_path=_script(tmp_path))
    with patch(
        "faultmaven.infrastructure.llm.local_llm_manager.subprocess.run",
        return_value=_ran(3),
    ):
        assert manager.stop_service() is False
    rendered = lines()
    length = f"stderr_len={len(f'err {MARKER}!')}"
    _named(rendered, "Script command failed", "exit=3", length)
    _named(rendered, "Failed to stop local LLM service", "exit=3", length)


async def test_a_script_output_is_named_by_exit_and_length(lines, tmp_path):
    manager = LocalLLMServiceManager(script_path=_script(tmp_path))
    manager.is_service_running = AsyncMock(side_effect=[False, True])
    with patch(
        "faultmaven.infrastructure.llm.local_llm_manager.subprocess.run",
        return_value=_ran(0),
    ):
        assert await manager.start_service("model-a") is True
    _named(
        lines(),
        "Service start script output",
        "exit=0",
        f"stdout_len={len(f'out {MARKER}')}",
    )


# ── providers/openai_provider.py ──────────────────────────────────────────


async def test_a_refused_cache_breakpoint_is_named_by_class_status_and_category(
    lines,
):
    refusal = _refuses_breakpoint()
    body = json.loads(refusal["text"])
    body["error"]["message"] += f" {MARKER}"
    refusal["text"] = json.dumps(body)
    result, _ = await _send(
        _azure(), [refusal, _ok()], messages=_loop_messages(), cache_prompt=True
    )
    assert result.content == "ok"
    refused = _named(lines(), "refused 'prompt_cache_breakpoint'", "(LLMException)")
    assert refused["status"] == 400
    assert refused["category"] == "request_rejected"


# ── pricing.py ────────────────────────────────────────────────────────────


def test_an_unparseable_override_is_named_by_class(lines, monkeypatch):
    monkeypatch.setenv(pricing._OVERRIDES_ENV, "{}")
    loads = MagicMock(side_effect=ValueError(f"bad JSON near {MARKER}"))
    with patch.object(pricing.json, "loads", loads):
        assert pricing._load_overrides() == {}
    _named(lines(), "not JSON", "ValueError")


# ── metering.py ───────────────────────────────────────────────────────────


def test_a_metering_failure_is_named_by_class(lines):
    with patch(
        "faultmaven.infrastructure.llm.metering.estimate_cost_usd",
        side_effect=RuntimeError(f"pricing {MARKER}"),
    ):
        record_provider_call("openai", "gpt-x", SimpleNamespace(), 1.0)
    _named(lines(), "record_provider_call failed", "RuntimeError")


# ── structured_output_capability.py ───────────────────────────────────────


def test_an_unstrict_schema_is_named_by_class(lines):
    with patch(
        "faultmaven.utils.schema_converter.to_strict_schema",
        side_effect=StrictSchemaUnsupported(f"properties.{MARKER} is free-form"),
    ):
        create_strategy_for_capability(
            StructuredOutputCapability.STRICT, {"title": "Answer", "type": "object"}
        )
    _named(lines(), "strict_schema_unavailable", "StrictSchemaUnsupported")


# ── providers/local_provider.py ───────────────────────────────────────────


def _local() -> LocalProvider:
    return LocalProvider(
        ProviderConfig(
            name="local",
            api_key=None,
            base_url="http://localhost:5000",
            models=["functionary-7b-v2"],
            default_model="functionary-7b-v2",
            timeout=30,
        )
    )


async def test_a_refused_reply_is_named_by_class(lines):
    provider = _local()
    provider._validate_response_content = MagicMock(
        side_effect=ValueError(f"reply {MARKER}")
    )
    data = {"choices": [{"message": {"role": "assistant", "content": "x"}}]}
    with patch("aiohttp.ClientSession", return_value=_mock_session(data)):
        with pytest.raises(ValueError):
            await send_to_model(provider, prompt=vetted("node-3"))
    rendered = lines()
    _named(rendered, "Content validation failed", "ValueError")
    _named(rendered, "Request failed after", "ValueError")


async def test_an_error_body_is_named_by_status_and_length(lines):
    body = f"model {MARKER} not loaded"
    session = _mock_session({})
    response = session.post.return_value
    response.status = 400
    response.text = AsyncMock(return_value=body)
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(LLMException):
            await send_to_model(_local(), prompt=vetted("node-3"))
    _named(lines(), "HTTP Error", "status=400", f"body_len={len(body)}")
