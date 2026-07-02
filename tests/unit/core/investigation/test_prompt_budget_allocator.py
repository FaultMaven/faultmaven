"""Invariant tests for the prompt token-budget allocator.

Covers the acceptance matrix from
docs/architecture/investigation-engine/prompt-token-budget-allocation.md §14:
fits-budget, reserve present, INV-1 (current-turn upload survives — including the
fallback path), journal preserved in fallback, bounded reserve, starvation
fallback, and the runtime context-length-error classifier.
"""

import sys
from pathlib import Path

import pytest

# Reuse the Case/evidence builders from the sliding-window suite.
sys.path.insert(0, str(Path(__file__).parent))
import test_context_sliding_window as t  # noqa: E402

from faultmaven.core.investigation.milestone_engine import (  # noqa: E402
    _is_context_length_error,
)
from faultmaven.core.investigation.prompts.context_builder import (  # noqa: E402
    TokenBudget,
    build_investigation_context,
)
from faultmaven.core.investigation.prompts.templates import (  # noqa: E402
    get_fallback_prompt_for_case,
    get_prompt_for_case,
)
from faultmaven.modules.case.domain.models import JournalEntry  # noqa: E402

PROVIDER, MODEL = "openai", "gpt-4"
FILE_ID = "file_aabb12345678"


def _count(text: str) -> int:
    return TokenBudget(10**9, provider_name=PROVIDER, model_name=MODEL).count(text)


def _case_with_current_turn_upload():
    ev = [
        t._make_evidence(
            summary="db pool exhausted",
            extract="ERROR pool timeout " * 80,
            source_file_id=FILE_ID,
        )
    ]
    case = t._make_case_with_evidence(ev)
    # Mark the backing file as uploaded THIS turn.
    case.uploaded_files[0].uploaded_at_turn = case.current_turn
    return case


# ---------------------------------------------------------------------------
# Allocator: fits budget + reserve + INV-1 (normal path)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("budget", [400, 2000, 8000, 24000])
def test_allocator_fits_budget_and_keeps_reserve_and_current_turn(budget):
    case = _case_with_current_turn_upload()
    ctx = build_investigation_context(
        case,
        "why is the db slow?",
        max_tokens=budget,
        provider_name=PROVIDER,
        model_name=MODEL,
    )
    total = sum(_count(v) for v in ctx.values() if v)
    # Fits within budget (+ small tolerance for the truncation marker / estimate)
    assert total <= budget + 60, f"sections {total} exceed budget {budget}"
    # Reserve always present
    assert ctx["identity"] and ctx["core_context"] and ctx["user_message"]
    # INV-1: the current-turn upload is present and addressable, except at the
    # most starved budget where the allocator alone can't (→ fallback handles it,
    # tested separately).
    if budget >= 2000:
        assert FILE_ID in ctx["evidence"]


def test_allocator_bounds_huge_user_message(monkeypatch):
    """A pasted mega-message cannot blow the reserve / negative section budget."""
    case = _case_with_current_turn_upload()
    huge = "PASTED LOG LINE " * 5000  # ~tens of thousands of tokens
    ctx = build_investigation_context(
        case,
        huge,
        max_tokens=24000,
        provider_name=PROVIDER,
        model_name=MODEL,
    )
    # user_message present but capped well below its raw size.
    assert ctx["user_message"]
    assert _count(ctx["user_message"]) <= 4000 + 50  # PROMPT_USER_MESSAGE_MAX_TOKENS
    # And the assembled sections still fit.
    assert sum(_count(v) for v in ctx.values() if v) <= 24000 + 200


def test_allocator_keeps_journal_above_kb_under_pressure():
    case = _case_with_current_turn_upload()
    case.investigation_journal = [
        JournalEntry(turn=1, entry_type="ruled_out", content="not a network issue"),
        JournalEntry(turn=2, entry_type="decision", content="focus on connection pool"),
    ]
    case.kb_context = [
        {"title": "Runbook", "summary": "x" * 500, "solution": "y" * 800}
        for _ in range(5)
    ]
    ctx = build_investigation_context(
        case,
        "continue",
        max_tokens=1600,
        provider_name=PROVIDER,
        model_name=MODEL,
    )
    # Journal (priority #3) survives; KB (lower) is the one squeezed.
    assert ctx["investigation_journal"], "journal must survive under pressure"
    assert "connection pool" in ctx["investigation_journal"]


# ---------------------------------------------------------------------------
# INV-1 + journal survive the FALLBACK path (the tightest-budget case)
# ---------------------------------------------------------------------------
def test_fallback_preserves_current_turn_upload():
    case = _case_with_current_turn_upload()
    fb = get_fallback_prompt_for_case(case, "why slow?")
    assert FILE_ID in fb, "INV-1: current-turn upload must survive the fallback"


def test_fallback_preserves_journal():
    case = _case_with_current_turn_upload()
    case.investigation_journal = [
        JournalEntry(turn=1, entry_type="ruled_out", content="not DNS"),
        JournalEntry(turn=2, entry_type="decision", content="check pool limits"),
    ]
    fb = get_fallback_prompt_for_case(case, "continue")
    assert "check pool limits" in fb or "not DNS" in fb


def test_fallback_journal_digest_keeps_newest_not_oldest():
    """With more high-signal entries than the cap, the NEWEST must survive."""
    from faultmaven.core.investigation.prompts.templates import (
        _fallback_journal_digest,
    )

    case = _case_with_current_turn_upload()
    case.investigation_journal = [
        JournalEntry(turn=i, entry_type="finding", content=f"finding number {i}")
        for i in range(1, 30)
    ]
    digest = _fallback_journal_digest(case, max_entries=5)
    assert "finding number 29" in digest  # newest kept
    assert "finding number 1" not in digest  # oldest dropped


def test_fallback_formats_without_current_turn_upload():
    """Empty current-turn slot must still render (no KeyError / stray text)."""
    ev = [t._make_evidence(source_file_id=FILE_ID)]
    case = t._make_case_with_evidence(ev)
    case.uploaded_files[0].uploaded_at_turn = 0  # not this turn
    fb = get_fallback_prompt_for_case(case, "hi")
    assert "STATE: INVESTIGATING" in fb


# ---------------------------------------------------------------------------
# Starvation backstop: tiny budget → minimal fallback (not a near-empty prompt)
# ---------------------------------------------------------------------------
def test_starvation_routes_to_fallback(monkeypatch):
    # Set a target below the (~14K-token) template overhead by mutating the
    # cached settings singleton (auto-restored).
    from faultmaven.config.settings import get_settings

    s = get_settings()
    monkeypatch.setattr(s.model_context, "prompt_target_tokens", 2500)

    case = _case_with_current_turn_upload()
    prompt = get_prompt_for_case(
        case, "why slow?", provider_name=PROVIDER, model_name=MODEL
    )
    # The starvation fallback fires: minimal template, but INV-1 still holds.
    assert FILE_ID in prompt
    assert _count(prompt) < 12000  # far smaller than the full ~14K-token template


# ---------------------------------------------------------------------------
# Runtime context-length error classifier
# ---------------------------------------------------------------------------
class _Err(Exception):
    def __init__(self, msg, status_code=None):
        self.message = msg
        self.status_code = status_code
        super().__init__(msg)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_Err("This model's maximum context length is 8192 tokens"), True),
        (_Err("Please reduce the length of the messages"), True),
        (_Err("prompt is too long: 200000 tokens"), True),
        (_Err("bad request: too many tokens", 400), True),
        (_Err("invalid api key", 401), False),
        (_Err("connection reset"), False),
        # Tightened: these must NOT be misclassified as context overflow.
        (_Err("string too long", 400), False),  # generic Pydantic validation
        (_Err("invalid token parameter", 400), False),  # bare 400 + 'token'
        (_Err("max_tokens must be <= 4096", 400), False),
    ],
)
def test_context_length_classifier(exc, expected):
    assert _is_context_length_error(exc) is expected


# ---------------------------------------------------------------------------
# Continuity: truncation keeps the TAIL (recent turns), allocator prefers the
# fidelity that fits rather than front-cutting graduated history.
# ---------------------------------------------------------------------------
def test_truncate_to_tail_keeps_end():
    tb = TokenBudget(10**9, provider_name=PROVIDER, model_name=MODEL)
    text = "OLD_START " + ("filler " * 200) + "RECENT_END"
    head = tb._truncate_to(text, 40, keep="head")
    tail = tb._truncate_to(text, 40, keep="tail")
    assert "OLD_START" in head and "RECENT_END" not in head
    assert "RECENT_END" in tail and "OLD_START" not in tail


def test_truncate_to_never_silently_drops():
    """INV-4: non-empty input above the 2-token floor always leaves a marker."""
    tb = TokenBudget(10**9, provider_name=PROVIDER, model_name=MODEL)
    assert tb._truncate_to("some content here", 6) != ""  # tiny but not silent
    assert tb._truncate_to("x", 2) == ""  # below floor → empty is acceptable


def test_allocator_conversation_keeps_latest_turn_under_pressure():
    """When graduated history can't be afforded, the compact history (which
    carries the latest turn) is used instead of a front-truncated graduated."""
    case = _case_with_current_turn_upload()
    # Multi-turn history: older turns first, a distinctive RECENT marker last.
    case.messages = []
    for i in range(1, 7):
        case.messages.append(
            {"turn_number": i, "role": "user", "content": f"user turn {i}"}
        )
        marker = "RECENT_MARKER_XYZ" if i == 6 else f"agent reply {i}"
        case.messages.append({"turn_number": i, "role": "assistant", "content": marker})
    case.current_turn = 7
    ctx = build_investigation_context(
        case,
        "latest question",
        max_tokens=1500,  # tight: evidence will pressure conversation
        provider_name=PROVIDER,
        model_name=MODEL,
    )
    conv = ctx["conversation_history"]
    # The latest content must survive — never front-truncated away.
    assert "latest question" in conv or "RECENT_MARKER_XYZ" in conv


def test_allocator_journal_truncation_keeps_newest_not_oldest():
    """A journal too large for its cap is truncated keeping the TAIL (newest),
    not the head — dropping the newest anti-amnesia entries is exactly wrong."""
    case = _case_with_current_turn_upload()
    # All high-signal so the entry-level middle-out keeps every entry; enough of
    # them (with long content) that the rendered journal still exceeds its cap,
    # forcing the allocator's section-level truncation to fire.
    # content is schema-capped at 200 chars; ~60 high-signal entries render to
    # ~2100 tokens, comfortably over the ~1500-token journal cap so the
    # allocator's section-level truncation fires.
    case.investigation_journal = [
        JournalEntry(
            turn=i,
            entry_type="finding",
            content=f"finding number {i} " + ("detail " * 25),
        )
        for i in range(1, 61)
    ]
    ctx = build_investigation_context(
        case,
        "continue the investigation",
        max_tokens=24000,
        provider_name=PROVIDER,
        model_name=MODEL,
    )
    journal = ctx["investigation_journal"]
    assert journal, "journal must survive"
    assert "truncated" in journal.lower(), "expected the section to be truncated"
    # The newest entry (60) survives (keep='tail'); the oldest (1) is dropped —
    # the OLD keep='head' bug would invert this (keep 1, drop 60).
    assert "finding number 60" in journal
    assert "finding number 1 " not in journal


def test_allocator_conversation_cap_does_not_starve_journal():
    """Verbose conversation (priority #2) must not consume the whole section
    budget and starve the journal (#3): conversation is bounded by its cap."""
    from faultmaven.config.settings import get_settings

    conv_cap = get_settings().prompt_budget.conversation_history_max_tokens
    case = _case_with_current_turn_upload()
    case.investigation_journal = [
        JournalEntry(turn=1, entry_type="decision", content="focus on connection pool"),
    ]

    # Verbose history: the recent (verbatim) user turns carry large, token-dense
    # pastes so the rendered conversation far exceeds the cap. (Older user turns
    # are summarized; recent user content renders verbatim and uncapped — that is
    # the section the cap must bound.)
    def _dense(prefix, n):
        return prefix + " " + " ".join(f"tok{prefix}{k}" for k in range(n))

    case.messages = []
    for i in range(1, 25):
        case.messages.append(
            {"turn_number": i, "role": "user", "content": _dense(f"u{i}", 2000)}
        )
        case.messages.append(
            {"turn_number": i, "role": "assistant", "content": f"agent reply {i}"}
        )
    case.current_turn = 25
    # Sanity: the raw conversation genuinely exceeds the cap, so the assertion
    # below actually exercises the cap (guards against a no-op test).
    from faultmaven.core.investigation.prompts.context_builder import (
        _build_graduated_history,
    )

    assert _count(_build_graduated_history(case)) > conv_cap
    ctx = build_investigation_context(
        case,
        "continue",
        max_tokens=24000,
        provider_name=PROVIDER,
        model_name=MODEL,
    )
    # Conversation is bounded by its cap (+ small marker/estimate tolerance)...
    assert _count(ctx["conversation_history"]) <= conv_cap + 80
    # ...and the journal below it survives rather than being starved to empty.
    assert ctx["investigation_journal"], "journal must not be starved by history"
    assert "connection pool" in ctx["investigation_journal"]


# ---------------------------------------------------------------------------
# The allocator is the only assembly path — get_prompt_for_case always uses it.
# ---------------------------------------------------------------------------
def test_get_prompt_for_case_assembles_via_allocator():
    """A normal turn renders the full template + current-turn upload through the
    (only) allocator path."""
    case = _case_with_current_turn_upload()
    prompt = get_prompt_for_case(
        case, "why slow?", provider_name=PROVIDER, model_name=MODEL
    )
    assert FILE_ID in prompt
    assert "STATE: INVESTIGATING" in prompt or "INVESTIGATING" in prompt


# ---------------------------------------------------------------------------
# Tool-loop per-call size enforcement (no oversized prompt may be sent)
# ---------------------------------------------------------------------------
def test_tool_loop_messages_bounded_elides_oldest_keeps_recent():
    """Every tool-loop call is bounded: accumulated tool exchanges compact
    (oldest-first, with a marker) so the sent prompt never exceeds the budget,
    while the system+base task and the newest observations are preserved and
    assistant/tool pairing stays valid."""
    from types import SimpleNamespace

    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    fake = SimpleNamespace(da_model=None)
    msgs = [
        {"role": "system", "content": "SYS " + "s" * 200},
        {"role": "user", "content": "BASE " + "x" * 400},
    ]
    for i in range(6):
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "function": {"name": "search_file", "arguments": "{}"},
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"c{i}",
                "name": "search_file",
                "content": f"RESULT_{i} " + "y" * 400,
            }
        )
    budget = 400
    out = MilestoneEngine._bound_tool_loop_messages(fake, msgs, budget, "openai")

    total = sum(
        _count(str(m.get("content") or "") + str(m.get("tool_calls") or ""))
        for m in out
    )
    joined = " ".join(str(m.get("content")) for m in out)
    assert total <= budget, "bounded: sent prompt must fit the budget"
    assert out[0]["role"] == "system" and out[1]["content"].startswith("BASE")
    assert any(
        "elided to stay within" in (m.get("content") or "") for m in out
    ), "INV-4 marker"
    assert "RESULT_5" in joined, "newest observation kept"
    assert "RESULT_0" not in joined, "oldest observation elided"
    # Pairing valid: every tool result is immediately preceded by an assistant.
    for idx, m in enumerate(out):
        if m.get("role") == "tool":
            assert out[idx - 1].get("role") == "assistant"

    # No-op (returns the SAME list object) when already under budget.
    under = msgs[:4]
    assert (
        MilestoneEngine._bound_tool_loop_messages(fake, under, 10**6, "openai") is under
    )


# ---------------------------------------------------------------------------
# Per-turn spend tracking + tool-capability gate (code-review coverage)
# ---------------------------------------------------------------------------
def test_spend_weighted_tokens_downweights_cache_reads():
    """The cost-weighted total down-weights cache-read tokens (0.25x) so a
    heavily-cached turn isn't charged full byte count against the spend guards."""
    from faultmaven.infrastructure.llm.metering import TurnTokenTracker

    tr = TurnTokenTracker()
    tr.input_tokens = 1000
    tr.output_tokens = 200
    tr.cache_write_tokens = 400
    tr.cache_read_tokens = 10000
    assert tr.total_tokens == 11600
    # 1000 + 200 + 400 + 0.25*10000 = 4100
    assert tr.spend_weighted_tokens == 4100
    assert tr.spend_weighted_tokens < tr.total_tokens


def test_tools_effectively_available_gates_on_capability():
    from types import SimpleNamespace

    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    def eng(tools, provider):
        ns = SimpleNamespace(
            investigation_tools=tools,
            da_provider=None,
            llm_provider=provider,
            da_model=None,
        )
        # _tools_effectively_available delegates to this method on self.
        ns._da_provider_supports_tools = (
            lambda: MilestoneEngine._da_provider_supports_tools(ns)
        )
        return ns

    capable = SimpleNamespace(supports_tool_calling=lambda m: True)
    incapable = SimpleNamespace(supports_tool_calling=lambda m: False)
    no_attr = SimpleNamespace()  # missing capability info → assume capable

    f = MilestoneEngine._tools_effectively_available
    assert f(eng(object(), capable)) is True
    assert f(eng(None, capable)) is False  # no tools registered
    assert f(eng(object(), incapable)) is False  # tools present but incapable
    assert f(eng(object(), no_attr)) is True  # unknown capability → capable
    # The shared helper agrees with the gate's capability half.
    g = MilestoneEngine._da_provider_supports_tools
    assert g(eng(object(), incapable)) is False
    assert g(eng(object(), capable)) is True


def test_resolve_tool_loop_budget_is_bounded():
    from types import SimpleNamespace

    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    b = MilestoneEngine._resolve_tool_loop_budget(
        SimpleNamespace(da_model=MODEL), PROVIDER
    )
    # prompt_target (32K default) + observation allowance (16K default), clamped
    # down to the model ceiling. Always a positive int, never above target+obs.
    assert isinstance(b, int) and b >= 2000
    assert b <= 32000 + 16000
