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
from faultmaven.exceptions import LLMException  # noqa: E402
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
    from faultmaven.core.investigation.prompts.templates import _fallback_journal_digest

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
# An overflow is something a PROVIDER reported, so these are built the way a
# provider builds one — ``LLMException`` with the status the API answered,
# which is what makes ``LLMErrorCategory`` authoritative (#509). Before that,
# any exception at all was classified by matching its sentence.
def _Err(msg, status_code=400):
    return LLMException(msg, status_code=status_code)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_Err("This model's maximum context length is 8192 tokens"), True),
        (_Err("Please reduce the length of the messages"), True),
        (_Err("prompt is too long: 200000 tokens"), True),
        (_Err("bad request: too many tokens", 400), True),
        # Gemini's overflow body, whose only code is the undifferentiated
        # INVALID_ARGUMENT. Unrecognised before #509 — the shipped default
        # provider's own overflow hard-failed the turn instead of degrading.
        (
            _Err(
                "The input token count (1200000) exceeds the maximum number of "
                "tokens allowed (1048576).",
                400,
            ),
            True,
        ),
        (_Err("invalid api key", 401), False),
        (_Err("connection reset", 502), False),
        # Tightened: these must NOT be misclassified as context overflow.
        (_Err("string too long", 400), False),  # generic Pydantic validation
        (_Err("invalid token parameter", 400), False),  # bare 400 + 'token'
        (_Err("max_tokens must be <= 4096", 400), False),
        # An exception nobody classified is not an overflow, whatever it says.
        (Exception("This model's maximum context length is 8192 tokens"), False),
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
    from faultmaven.core.investigation.prompts.fence import PromptFence

    # The history is rendered inside the assembly's fence (#1256), so it needs
    # one here too; the token is irrelevant to what this measures.
    assert _count(_build_graduated_history(case, PromptFence("deadbeef"))) > conv_cap
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
# INV-4 at the boundary the code has: a section allotted <= 2 tokens (#610)
# ---------------------------------------------------------------------------
_VARIABLE_KEYS = (
    "evidence",
    "conversation_history",
    "investigation_journal",
    "working_conclusion",
    "kb_results",
    "hypotheses",
    "candidate_solutions",
    "evidence_needs",
    "entity_highlights",
)


def _allocate(budget, **variable):
    from faultmaven.core.investigation.prompts.context_builder import (
        _allocate_sections,
    )

    reserve = dict(
        identity="IDENTITY block",
        core_context="CORE context",
        milestones_str="MILESTONES",
        inquiry_state_str="",
        pending_action_str="",
        user_message_block="the user asks something",
        feedback_str="",
    )
    sections = dict(
        evidence_str="",
        graduated_history="",
        compact_history="",
        journal_str="",
        conclusion_str="",
        kb_str="",
        hypothesis_str="",
        evidence_needs_str="",
        entity_highlights_str="",
        candidate_solutions_str="",
    )
    sections.update(variable)
    case = t._make_case_with_evidence([])
    return (
        _allocate_sections(
            budget=budget,
            case=case,
            provider_name=PROVIDER,
            model_name=MODEL,
            **reserve,
            **sections,
        ),
        reserve,
    )


@pytest.mark.parametrize("room", [0, 1, 2])
def test_a_non_empty_section_allotted_two_tokens_or_fewer_is_marked(room):
    """INV-4 (#610). ``_truncate_to`` returns "" below 3 tokens, so a section
    allotted 0, 1 or 2 used to vanish unmarked — and absent engine state such
    as ``hypotheses`` reads to the model as "none exist". It now carries the
    same bare ``[...]`` ``_truncate_to`` emits, charged to the margin."""
    from faultmaven.core.investigation.prompts.context_builder import (
        _SECTION_DROPPED_MARKER,
    )

    probe = TokenBudget(10**9, provider_name=PROVIDER, model_name=MODEL)
    _, reserve = _allocate(probe)
    reserve_tokens = probe.used_tokens

    budget = TokenBudget(
        reserve_tokens + room, provider_name=PROVIDER, model_name=MODEL
    )
    ctx, _ = _allocate(
        budget,
        evidence_str="EVIDENCE " * 50,
        graduated_history="HISTORY " * 80,
        compact_history="LATEST TURN " * 20,
        journal_str="JOURNAL " * 50,
        conclusion_str="CONCLUSION " * 50,
        kb_str="KB " * 50,
        hypothesis_str="HYPOTHESIS " * 50,
        candidate_solutions_str="SOLUTION " * 50,
        entity_highlights_str="ENTITY " * 50,
        # evidence_needs left empty: nothing existed, so nothing is marked.
    )
    marked = [k for k in _VARIABLE_KEYS if k != "evidence_needs"]
    for key in marked:
        assert ctx[key] == _SECTION_DROPPED_MARKER, (key, ctx[key])
    assert ctx["evidence_needs"] == ""
    # Charged honestly: every marker is counted, even past the budget.
    assert budget.used_tokens == reserve_tokens + len(marked) * _count(
        _SECTION_DROPPED_MARKER
    )


def test_a_section_that_fits_a_tiny_allotment_renders_as_itself():
    """The marker replaces content that did NOT fit, never content that did: a
    one-token section granted its one token renders verbatim, and only the
    section behind it — which does not fit — is marked."""
    from faultmaven.core.investigation.prompts.context_builder import (
        _SECTION_DROPPED_MARKER,
    )

    probe = TokenBudget(10**9, provider_name=PROVIDER, model_name=MODEL)
    _allocate(probe)
    tiny = "ok"
    assert _count(tiny) == 1
    budget = TokenBudget(
        probe.used_tokens + 2, provider_name=PROVIDER, model_name=MODEL
    )
    ctx, _ = _allocate(budget, conclusion_str=tiny, kb_str="KB " * 50)
    assert ctx["working_conclusion"] == tiny
    assert ctx["kb_results"] == _SECTION_DROPPED_MARKER


def test_the_allocator_marks_at_the_boundary_truncation_leaves_empty():
    """The two boundaries are one constant, so they cannot drift apart: the
    largest allotment ``_truncate_to`` still answers "" for is exactly where
    the allocator takes over."""
    from faultmaven.core.investigation.prompts.context_builder import (
        _SECTION_DROPPED_MARKER,
        _SILENT_DROP_MAX_TOKENS,
    )

    tb = TokenBudget(10**9, provider_name=PROVIDER, model_name=MODEL)
    text = "some content here " * 10
    assert tb._truncate_to(text, _SILENT_DROP_MAX_TOKENS) == ""
    assert tb._truncate_to(text, _SILENT_DROP_MAX_TOKENS + 1) == (
        _SECTION_DROPPED_MARKER
    )


def _pressure_case():
    """A realistic case whose variable sections all compete: four large logs,
    a twelve-turn history, a journal and KB runbooks."""
    evs = [
        t._make_evidence(
            summary=f"ev {i}",
            extract=f"LOGLINE {i} " * 600,
            source_file_id=f"file_{i:012x}",
            collected_at_turn=i + 1,
        )
        for i in range(4)
    ]
    case = t._make_case_with_evidence(evs)
    case.messages = []
    for i in range(1, 13):
        case.messages.append(
            {"turn_number": i, "role": "user", "content": f"u{i} " + "detail " * 60}
        )
        case.messages.append(
            {"turn_number": i, "role": "assistant", "content": f"a{i} " + "why " * 60}
        )
    case.current_turn = 13
    case.investigation_journal = [
        JournalEntry(turn=i, entry_type="finding", content=f"finding {i} " + "x " * 40)
        for i in range(1, 15)
    ]
    case.kb_context = [
        {"title": f"Runbook {k}", "summary": "s" * 300, "solution": "y" * 400}
        for k in range(3)
    ]
    return case


def test_no_section_vanishes_unmarked_on_the_assembled_prompt(monkeypatch):
    """INV-4 driven through the path that runs it (#610): sweep the prompt
    target across the band just above the starvation fallback — where the
    lower-priority sections are squeezed to nothing — and require every
    non-empty variable section of every MAIN-template prompt to render as
    content or as the marker, never as "".

    The same sweep pins the answer to #610's starvation-trigger edge, which is
    a documented non-goal rather than a subtraction (see
    prompt-token-budget-allocation.md §7): the trigger does not reserve room
    for the sections below the conversation, but it does guarantee evidence and
    continuity content on every main-template prompt at the shipped settings.
    """
    from faultmaven.config.settings import get_settings
    from faultmaven.core.investigation.prompts import context_builder as cb
    from faultmaven.core.investigation.prompts import templates as tp

    settings = get_settings()
    real_allocate = cb._allocate_sections
    real_fallback = tp.get_fallback_prompt_for_case
    seen: dict = {}

    def spy_allocate(**kw):
        ctx = real_allocate(**kw)
        seen["inputs"] = {
            "evidence": kw["evidence_str"],
            "conversation_history": kw["graduated_history"] or kw["compact_history"],
            "investigation_journal": kw["journal_str"],
            "working_conclusion": kw["conclusion_str"],
            "kb_results": kw["kb_str"],
            "hypotheses": kw["hypothesis_str"],
            "candidate_solutions": kw["candidate_solutions_str"],
            "evidence_needs": kw["evidence_needs_str"],
            "entity_highlights": kw["entity_highlights_str"],
        }
        seen["ctx"] = ctx
        return ctx

    def spy_fallback(*a, **k):
        seen["fallback"] = True
        return real_fallback(*a, **k)

    monkeypatch.setattr(cb, "_allocate_sections", spy_allocate)
    monkeypatch.setattr(tp, "get_fallback_prompt_for_case", spy_fallback)

    def assemble(target: int) -> bool:
        """True when the main template was emitted at ``target``."""
        monkeypatch.setattr(settings.model_context, "prompt_target_tokens", target)
        seen.clear()
        get_prompt_for_case(
            _pressure_case(),
            "why is it slow?",
            provider_name=PROVIDER,
            model_name=MODEL,
        )
        return not seen.get("fallback")

    # Find the starvation boundary rather than hardcode it: it moves with the
    # template's size, and a fixed window would go quietly vacuous.
    lo, hi = 2_000, 200_000
    assert not assemble(lo) and assemble(hi)
    while hi - lo > 10:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if assemble(mid) else (mid, hi)

    main_runs = 0
    squeezed_to_marker = 0
    for target in range(hi, hi + 600, 10):
        if not assemble(target):
            continue
        main_runs += 1
        ctx, inputs = seen["ctx"], seen["inputs"]
        for key, text in inputs.items():
            if text:
                assert ctx[key], f"{key} vanished unmarked at target={target}"
        squeezed_to_marker += sum(
            1 for key, text in inputs.items() if text and ctx[key] == "[...]"
        )
        # The starvation edge's documented bound: evidence and continuity keep
        # real content whenever the main template is used.
        for key in ("evidence", "conversation_history"):
            assert ctx[key] not in ("", "[...]"), f"{key} starved at {target}"

    assert main_runs >= 40
    # Positive control: the sweep did reach the regime where sections are
    # squeezed to nothing. Without it, a band in which nothing is ever
    # squeezed would pass the loop above for free.
    assert squeezed_to_marker > 0


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


def test_tool_loop_bound_counts_reasoning_artifacts(monkeypatch):
    """Echoed reasoning artifacts are WIRE payload and must be counted (#1116).

    For a thinking-carrying assistant turn the provider serializes
    ``provider_metadata["assistant_content"]`` (Anthropic thinking blocks) or
    ``["assistant_parts"]`` (Gemini parts) INSTEAD OF ``content``. Estimating
    from ``content`` + ``tool_calls`` alone under-counts those turns by the
    whole size of their reasoning, so the bound would green-light a request
    that blows the provider's context limit — the failure this function exists
    to prevent.

    Fails before the fix: the reasoning-heavy history estimates as tiny, the
    early-exit returns the input list unchanged, and nothing is elided.
    """
    from types import SimpleNamespace

    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    fake = SimpleNamespace(da_model=None)
    reasoning = "step " * 2000  # thousands of tokens of hidden reasoning

    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "BASE"},
    ]
    for i in range(4):
        msgs.append(
            {
                "role": "assistant",
                # Short visible content — the whole weight is in the artifact.
                "content": "ok",
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "function": {"name": "search_file", "arguments": "{}"},
                    }
                ],
                "provider_metadata": {
                    "assistant_content": [
                        {
                            "type": "thinking",
                            "thinking": f"{reasoning}{i}",
                            "signature": "sig==",
                        },
                        {
                            "type": "tool_use",
                            "id": f"c{i}",
                            "name": "search_file",
                            "input": {},
                        },
                    ]
                },
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"c{i}",
                "name": "search_file",
                "content": f"RESULT_{i}",
            }
        )

    # Sized so ONE reasoning-carrying group fits and four do not: the elision
    # policy is unchanged, only the estimate that drives it.
    budget = 4000
    out = MilestoneEngine._bound_tool_loop_messages(fake, msgs, budget, "openai")

    # The reasoning is visible to the estimator, so the history is over budget
    # and gets bounded rather than passed through untouched.
    assert out is not msgs, "reasoning-heavy history must be recognised as over budget"
    assert any(
        "elided to stay within" in (m.get("content") or "") for m in out
    ), "INV-4 marker"
    # Newest observation survives, oldest is elided (unchanged elision policy).
    joined = " ".join(str(m.get("content")) for m in out)
    assert "RESULT_3" in joined
    assert "RESULT_0" not in joined

    # Gemini's artifact key is counted by the same code path.
    gemini_msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "BASE"},
        {
            "role": "assistant",
            "content": "ok",
            "provider_metadata": {
                "assistant_parts": [{"text": reasoning, "thoughtSignature": "sig=="}]
            },
        },
    ]
    assert (
        MilestoneEngine._bound_tool_loop_messages(fake, gemini_msgs, 1000, "openai")
        is not gemini_msgs
    )
