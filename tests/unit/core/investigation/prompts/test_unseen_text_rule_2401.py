"""#2401: a cut, elided or omitted item carries a rule against describing
what the model was not shown.

#3031's turn 10: the user pasted 7,311 characters, the prompt showed the
first 4,000 (the per-item cap), and the model said the paste "is truncated
before" a key that sat at character 6,385. The cap stays (ruled); what changes
is what the prompt says about the part it cuts. INQUIRY_TEMPLATE and
INVESTIGATION_BASE, the prompts that render <evidence_collected>, state the
unseen-text rule. It names every marker that shortens or leaves out an item
there, the budget squeeze's included, and keeps apart a tool result's
output-limit notice. It names ``search_file`` only when the turn gives it
(#1939), and then only for a ``file_backed="true"`` item (#2375). The cut
marker, the fallback stub's included, says FaultMaven holds the whole item
when it does.
"""

from __future__ import annotations

import json
import logging
import re

import pytest

from faultmaven.config.settings import get_settings
from faultmaven.core.investigation.prompts.context_builder.budget import (
    EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM,
)
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
from faultmaven.core.investigation.prompts.templates import assembly
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.blocks import unseen_text_rule
from faultmaven.core.investigation.prompts.templates.fallback import (
    _fallback_stub_block,
)
from faultmaven.core.investigation.prompts.templates.tool_text import NO_TOOLS
from faultmaven.infrastructure.llm.truncation import TRUNCATION_NOTICE
from faultmaven.modules.case.contracts import (
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    UploadedFile,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

pytestmark = pytest.mark.unit

_FILE_ID = "file_3031aaaaaaaa"
_EV_ID = "ev_3031aaaaaaaa"
_TAIL_KEY = "FragmentPath="


def _paste_text() -> str:
    """#3031's shape: ~7,300 characters whose last section starts past
    character 4,000 — a `systemctl show`-style dump with the key the model
    said the paste was cut before near character 6,400."""

    def lines(prefix: str, n: int) -> str:
        return "".join(f"{prefix}{i:03d}=value-{i:03d}-{'x' * 24}\n" for i in range(n))

    return (
        "[Service]\n"
        + lines("Property", 135)
        + "\n[Unit]\n"
        + _TAIL_KEY
        + "/etc/systemd/system/decoder.service\n"
        + lines("UnitProp", 19)
    )


_PASTE = _paste_text()
_RULE_SENTENCE = (
    "Never state what the part you were not shown contains, defines, returns or "
    "lacks, and never call the user's data missing, incomplete or truncated "
    "because this prompt does not show it."
)
_HELD = "FaultMaven holds the whole item."
_LEFT_OUT = "was shortened or left out of this prompt by FaultMaven."
_READ_STORED = (
    'To read the rest of an item marked file_backed="true", call search_file '
    "with its id; an item without it cannot be read further, so say which part "
    "you could not see."
)
_SAY_WHICH = "If you need the rest, say which part you could not see."


def _paste(turn: int, file_id: str = _FILE_ID) -> UploadedFile:
    return UploadedFile(
        file_id=file_id,
        filename="pasted_text_10.txt",
        size_bytes=len(_PASTE),
        content_type="text/plain",
        uploaded_at_turn=turn,
        upload_source="text_paste",
        storage_ref=f"evidence/case_3031/{file_id}.txt",
        data_type="config",
        summary="systemd unit properties.",
        structural_index=_PASTE,
    )


def _row(evidence_id: str = _EV_ID, file_id: str = _FILE_ID) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        source_file_id=file_id,
        summary="decoder unit properties",
        extract=None,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.CONFIGURATION,
        primary_purpose="Test",
        collected_by="user_3031",
        collected_at_turn=10,
    )


def _inference() -> Evidence:
    """An engine inference: a case with an evidence row reaches the tiers, so
    an uncited paste renders as a Tier-D orphan, not through the no-evidence
    path."""
    return Evidence(
        evidence_id="ev_3031bbbbbbbb",
        source_file_id=None,
        summary="restarts follow each config reload",
        extract=None,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.ENGINE_INFERENCE,
        primary_purpose="Test",
        collected_by="engine",
        collected_at_turn=9,
    )


def _case(turn: int, cited: bool = False, state=CaseState.INVESTIGATING):
    case = investigating_case(
        case_id="case_3031aaaaaaaa",
        title="Decoder service restarts",
        description="decoder.service restarts every few minutes",
        user_id="user_3031",
        enterprise_id="org_3031",
        statement="decoder.service restarts every few minutes",
        uploaded_files=[_paste(10)],
        evidence=[_row() if cited else _inference()],
        current_turn=turn,
    )
    if state == CaseState.INQUIRY:
        case = case.model_copy(
            update={"state": state, "inquiry": InquiryData(), "evidence": []}
        )
    return case


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_fixture_has_3031s_shape():
    assert 7_200 < len(_PASTE) < 7_400
    assert 6_000 < _PASTE.index("[Unit]") < _PASTE.index(_TAIL_KEY) < 6_500


_CUT = len(_PASTE) - EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM
_ORPHAN = "file_id above for specific lookups"
_TIER_A = "evidence id above to search"
#: (turn, cited, processing mode) → the tool sentence the paste's cut marker
#: ends with. The arrival turn renders through the current-turn floor, which
#: never elides; the turn after renders the cited paste as Tier A and the
#: uncited one as a Tier-D orphan; directed analysis elides a stored item's
#: extract there (``None``: the elided marker).
_CELLS = {
    "arrival_triage": (10, False, "triage", _ORPHAN),
    "arrival_directed": (10, False, "directed_analysis", _ORPHAN),
    "after_uncited_triage": (11, False, "triage", _ORPHAN),
    "after_cited_triage": (11, True, "triage", _TIER_A),
    "after_uncited_directed": (11, False, "directed_analysis", None),
    "after_cited_directed": (11, True, "directed_analysis", None),
}


@pytest.mark.parametrize("cell", sorted(_CELLS))
def test_a_cut_paste_renders_with_the_rule_and_a_held_marker(cell):
    turn, cited, mode, lookup = _CELLS[cell]

    prompt = get_prompt_for_case(
        _case(turn, cited),
        "why does it keep restarting?",
        processing_mode=mode,
        given_tools=DEFAULT_GIVEN,
    )

    flat = _flat(prompt)
    assert "UNSEEN TEXT:" in flat
    assert _LEFT_OUT in flat
    assert _RULE_SENTENCE in flat
    assert _READ_STORED in flat
    # The part past the cap is really not shown.
    assert _TAIL_KEY not in prompt
    if lookup is None:
        assert "[Structural index elided in directed-analysis mode" in prompt
    else:
        assert (
            f"[TRUNCATED: {_CUT:,} more characters not shown. {_HELD} "
            f"Use search_file with the {lookup}"
        ) in prompt


def test_inquiry_renders_the_rule_on_the_arrival_turn():
    prompt = get_prompt_for_case(
        _case(10, state=CaseState.INQUIRY),
        "here is the unit",
        given_tools=DEFAULT_GIVEN,
    )

    assert _RULE_SENTENCE in _flat(prompt)
    assert _HELD in prompt
    assert _TAIL_KEY not in prompt


@pytest.mark.parametrize("state", [CaseState.INQUIRY, CaseState.INVESTIGATING])
def test_a_toolless_turn_names_no_tool_in_the_rule_or_the_marker(state):
    prompt = get_prompt_for_case(_case(10, state=state), "why?", given_tools=NO_TOOLS)

    flat = _flat(prompt)
    assert _RULE_SENTENCE in flat
    assert _SAY_WHICH in flat
    assert f"[TRUNCATED: {_CUT:,} more characters not shown. {_HELD}]" in prompt
    assert "search_file" not in prompt
    assert "tool result" not in flat


def test_an_unstored_cut_item_is_neither_held_nor_sent_to_search_file():
    """With search_file given, the rule sends the model to it only for an item
    marked file_backed="true"; an unstored item carries no such mark, and its
    marker names no tool (#2375)."""
    case = _case(10)
    case.uploaded_files[0] = case.uploaded_files[0].model_copy(
        update={"storage_ref": None}
    )

    prompt = get_prompt_for_case(case, "why?", given_tools=DEFAULT_GIVEN)

    assert f"[TRUNCATED: {_CUT:,} more characters not shown.]" in prompt
    assert _HELD not in prompt
    tag = re.search(rf'<uploaded_file file_id="{_FILE_ID}"[^>]*>', prompt).group(0)
    assert 'file_backed="true"' not in tag
    flat = _flat(prompt)
    assert _RULE_SENTENCE in flat and _READ_STORED in flat
    # The rule's only reading sentence is the file_backed-scoped one.
    assert "call search_file with the item's id" not in flat
    assert "Use search_file with the file_id above" not in prompt


#: How far past the starvation boundary the squeeze test's target sits: well
#: inside the window in which the budget squeeze cuts mid-item (measured at
#: about 850 tokens wide on #2811 S2c-3b), and well clear of the few tokens a
#: fence token's length moves the template.
_PAST_STARVATION = 400


def _squeeze_prompt(case, target: int) -> str:
    """*case*'s prompt at a *target*-token fill target."""
    get_settings().model_context.prompt_target_tokens = target
    return get_prompt_for_case(
        case,
        "why?",
        provider_name="openai",
        model_name="gpt-4o",
        given_tools=DEFAULT_GIVEN,
    )


def _starvation_boundary(case, caplog) -> int:
    """The smallest target at which the budget backstop does not starve
    *case*: the template overhead and reserve it measures (logged when a
    small target starves), plus its margin and ``min_viable``."""
    with caplog.at_level(logging.WARNING, logger=assembly.__name__):
        _squeeze_prompt(case, 2_000)
    (starved,) = [
        r for r in caplog.records if r.getMessage() == "prompt_starvation_fallback"
    ]
    budget = get_settings().prompt_budget
    return (
        starved.template_overhead
        + starved.reserve_tokens
        + budget.overhead_margin_tokens
        + budget.min_viable_tokens
    )


def test_a_budget_squeeze_cut_renders_beside_the_rule(monkeypatch, caplog):
    """Six cited pastes at a target just past the starvation boundary: the
    budget squeeze cuts <evidence_collected> mid-item, with its own marker and
    no per-item one. The target is derived from the template's measured
    overhead, so the template's growth moves it (the #2811 rule line moved it
    by about 37 tokens)."""
    ids = [f"file_3031{i:08x}" for i in range(6)]
    case = _case(11).model_copy(
        update={
            "uploaded_files": [_paste(10, f) for f in ids],
            "evidence": [_row(f"ev_3031{i:08x}", f) for i, f in enumerate(ids)],
        }
    )
    context = get_settings().model_context
    monkeypatch.setattr(context, "prompt_target_tokens", context.prompt_target_tokens)

    prompt = _squeeze_prompt(
        case, _starvation_boundary(case, caplog) + _PAST_STARVATION
    )

    block = prompt[re.search(r'<evidence_collected fence="[0-9a-f]+">', prompt).end() :]
    assert "[... Content truncated due to context limit ...]" in block
    flat_rule = _flat(unseen_text_rule(DEFAULT_GIVEN))
    for marker in (
        "[... Content truncated due to context limit ...]",
        "[...truncated...]",
    ):
        assert f"`{marker}`" in flat_rule
    # A bare [...] replaces a whole section, never part of an item, and users
    # type it in their own pastes: the rule does not name it.
    assert "`[...]`" not in flat_rule
    assert flat_rule in _flat(prompt)


def test_the_rule_is_scoped_apart_from_the_output_limit_notice():
    """A tool result can carry TRUNCATION_NOTICE, whose answer really is
    incomplete: the rule names the evidence cut marker by a form the notice
    does not match, and says the notice keeps its meaning."""
    rule = _flat(unseen_text_rule(DEFAULT_GIVEN))
    assert "An item in <evidence_collected> marked `[TRUNCATED: … more" in rule
    assert "more characters not shown" not in TRUNCATION_NOTICE
    notice_head = "[TRUNCATED: the answer below hit the model's output limit"
    assert TRUNCATION_NOTICE.startswith(notice_head)
    assert (
        f"a tool result's own `{notice_head} …]` notice keeps its meaning, and "
        "that answer is incomplete." in rule
    )


@pytest.mark.parametrize("stored", [True, False], ids=["stored", "unstored"])
@pytest.mark.parametrize("given", [DEFAULT_GIVEN, NO_TOOLS], ids=["six", "none"])
def test_the_fallback_stub_marks_a_cut_head_and_not_a_whole_one(given, stored):
    def case_with(index: str):
        case = _case(10)
        case.uploaded_files[0] = case.uploaded_files[0].model_copy(
            update={
                "structural_index": index,
                "storage_ref": case.uploaded_files[0].storage_ref if stored else None,
            }
        )
        return case

    def stub(case):
        return _fallback_stub_block(
            case, PromptFence(mint_token()), given, head_chars=200
        )

    held = f" {_HELD}" if stored else ""
    marker = f" [TRUNCATED: {len(_PASTE) - 200:,} more characters not shown.{held}]"
    assert marker in stub(case_with(_PASTE))
    # The count is of the extract the full render shows, not of the whole
    # structural index with its search map and metadata.
    indexed = json.dumps(
        {"v": 1, "file_extract": _PASTE, "search_map": "s" * 900, "file_meta": {}}
    )
    rendered = stub(case_with(indexed))
    assert marker in rendered
    assert _PASTE[:40].replace("\n", " ") in rendered
    short = stub(case_with("ActiveState=failed"))
    assert "ActiveState=failed" in short
    assert "[TRUNCATED:" not in short
