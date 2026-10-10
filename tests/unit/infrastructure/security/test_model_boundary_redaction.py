"""#1901 — one decision says whether what a model is sent is redacted.

``should_redact`` (a sanitizer is configured AND ``SANITIZE_PII`` is on) used to
exist twice: once in the milestone engine, and once re-stated inline by
case→runbook extraction (since removed, #1897), with a test pinning the copy to
the original. It now lives once, in ``infrastructure/security/case_redaction.py``,
beside ``model_boundary_redaction`` — the context the knowledge-authoring paths
(case and document conversion) build from it.

Pinned here, once, for every path that uses it:

1. the truth table — sanitizer present/absent x ``SANITIZE_PII`` on/off;
2. ``model_boundary_redaction`` is that decision over the injected sanitizer,
   with no registry (nothing loads, saves or reverses);
3. ``asanitize_messages`` redacts every message of a chat-shaped call;
4. structurally, every ``CaseRedactionContext`` the code builds is enabled by
   ``should_redact`` — so no path can re-state the rule and drift.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

import pytest

import faultmaven
from faultmaven.infrastructure.security import case_redaction
from faultmaven.infrastructure.security.case_redaction import (
    MODEL_BOUNDARY_SCOPE,
    CaseRedactionContext,
    model_boundary_redaction,
    should_redact,
)
from faultmaven.infrastructure.security.redaction import DataSanitizer
from tests.utils import sanitize_pii_pinned

pytestmark = [pytest.mark.unit, pytest.mark.security]

PII_IP = "10.20.30.40"


@pytest.fixture(params=[False, True], ids=["sanitize_pii-off", "sanitize_pii-on"])
def redaction_arm(request):
    """Both values of the flag, pinned whatever the CI job exports."""
    with sanitize_pii_pinned(request.param):
        yield request.param


@pytest.fixture(scope="module")
def sanitizer():
    return DataSanitizer()


@pytest.mark.parametrize("has_sanitizer", [True, False])
def test_should_redact_truth_table(redaction_arm, has_sanitizer, sanitizer):
    assert should_redact(sanitizer if has_sanitizer else None) is (
        redaction_arm and has_sanitizer
    )


@pytest.mark.parametrize("has_sanitizer", [True, False])
def test_model_boundary_redaction_is_that_decision_over_that_sanitizer(
    redaction_arm, has_sanitizer, sanitizer
):
    handed = sanitizer if has_sanitizer else None

    ctx = model_boundary_redaction(handed)

    assert isinstance(ctx, CaseRedactionContext)
    assert ctx.enabled is should_redact(handed)
    assert ctx.enabled is (redaction_arm and has_sanitizer)
    assert ctx.sanitizer is handed
    # No registry: nothing is loaded from or saved to Redis, and so there is
    # nothing a caller could reverse placeholders from but this call's own —
    # which is why the context takes no case or conversion id to key one by.
    assert ctx.redis_client is None
    assert ctx.case_id == MODEL_BOUNDARY_SCOPE


async def test_asanitize_messages_redacts_every_message(redaction_arm, sanitizer):
    ctx = model_boundary_redaction(sanitizer)
    messages = [
        {"role": "system", "content": f"system mentions {PII_IP}"},
        {"role": "user", "content": f"the replica at {PII_IP} refuses"},
    ]

    sent = await ctx.asanitize_messages(messages)

    assert [m["role"] for m in sent] == ["system", "user"]
    # The input is not mutated: it is the caller's record of what it built.
    assert messages[1]["content"] == f"the replica at {PII_IP} refuses"
    placeholder = await ctx.asanitize(PII_IP)
    if redaction_arm:
        assert placeholder != PII_IP, "control: the sanitizer redacts an IP"
        for message in sent:
            assert PII_IP not in message["content"]
            assert placeholder in message["content"]
    else:
        assert sent == messages


@pytest.mark.parametrize(
    "content", [[{"type": "text", "text": f"at {PII_IP}"}], {"text": PII_IP}, None]
)
async def test_structured_content_is_refused_not_sent_in_clear(
    redaction_arm, sanitizer, content
):
    """``asanitize`` passes non-text through unchanged; the router's own pass
    walks lists and dicts. So an enabled context refuses a message it cannot
    redact rather than send it in clear. A disabled one redacts nothing and
    checks nothing."""
    ctx = model_boundary_redaction(sanitizer)
    messages = [
        {"role": "system", "content": "fixed"},
        {"role": "user", "content": content},
    ]

    if redaction_arm:
        with pytest.raises(TypeError, match=type(content).__name__):
            await ctx.asanitize_messages(messages)
    else:
        assert await ctx.asanitize_messages(messages) == messages


def _redaction_context_constructions():
    """Every ``CaseRedactionContext(...)`` call in the package, with the
    function it sits in."""
    root = Path(faultmaven.__file__).parent
    for path in sorted(root.rglob("*.py")):
        with warnings.catch_warnings():
            # A source file's own invalid escapes are not this test's finding.
            warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
            tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "CaseRedactionContext"
                ):
                    yield path.relative_to(root.parent).as_posix(), func.name, node


def test_every_redaction_context_is_enabled_by_should_redact():
    """Structural, because the behavioural tests can only exercise the paths
    that exist: a new path that builds its own context and re-states the rule
    (or forgets it, and so redacts under ``enabled=True`` regardless of the
    flag) fails here with its location."""
    sites = []
    for where, func_name, call in _redaction_context_constructions():
        enabled = next((k.value for k in call.keywords if k.arg == "enabled"), None)
        decided_by_should_redact = (
            isinstance(enabled, ast.Call)
            and getattr(enabled.func, "id", None) == "should_redact"
        )
        sites.append((where, func_name, decided_by_should_redact))

    # Positive control: the scan sees the three constructions that exist — the
    # investigation turn, the terminal Q&A turn, and the shared helper every
    # knowledge-authoring path builds through.
    assert {(w, f) for w, f, _ in sites} == {
        (
            "faultmaven/core/investigation/milestone_engine/turn_generation.py",
            "_generate_turn_response",
        ),
        (
            "faultmaven/core/investigation/milestone_engine/terminal_turns.py",
            "_process_terminal_qa",
        ),
        (
            "faultmaven/infrastructure/security/case_redaction.py",
            "model_boundary_redaction",
        ),
    }, sites
    assert all(decided for _, _, decided in sites), sites


def test_should_redact_is_defined_once():
    """The rule's one copy. The module it used to live in is gone."""
    assert case_redaction.should_redact is should_redact
    engine_dir = (
        Path(faultmaven.__file__).parent / "core" / "investigation" / "milestone_engine"
    )
    assert not (engine_dir / "redaction.py").exists()
