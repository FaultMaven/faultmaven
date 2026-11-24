"""#2401: a cut, elided or omitted item carries a rule against describing
what the model was not shown.

#3031's turn 10: the user pasted 7,311 characters, the prompt showed the
first 4,000 (the per-item cap), and the model said the paste "is truncated
before" a key that sat at character 6,385. The cap stays (ruled); what changes
is what the prompt says about the part it cuts. Every prompt that can render a
cut, elided or omitted item states the unseen-text rule, naming
``search_file`` only when the turn gives it (#1939), and the cut marker says
FaultMaven holds the whole item when it does.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts.context_builder.budget import (
    EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM,
)
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.fallback import (
    _fallback_stub_block,
)
from faultmaven.core.investigation.prompts.templates.tool_text import NO_TOOLS
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


def _paste(turn: int) -> UploadedFile:
    return UploadedFile(
        file_id=_FILE_ID,
        filename="pasted_text_10.txt",
        size_bytes=len(_PASTE),
        content_type="text/plain",
        uploaded_at_turn=turn,
        upload_source="text_paste",
        storage_ref=f"evidence/case_3031/{_FILE_ID}.txt",
        data_type="config",
        summary="systemd unit properties.",
        structural_index=_PASTE,
    )


def _row() -> Evidence:
    return Evidence(
        evidence_id=_EV_ID,
        source_file_id=_FILE_ID,
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
    assert _RULE_SENTENCE in flat
    assert "To read the rest, call search_file with the item's id." in flat
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
    assert "If you need the rest, say which part you could not see." in flat
    assert f"[TRUNCATED: {_CUT:,} more characters not shown. {_HELD}]" in prompt
    assert "search_file" not in prompt


def test_an_unstored_item_is_not_said_to_be_held():
    case = _case(10)
    case.uploaded_files[0] = case.uploaded_files[0].model_copy(
        update={"storage_ref": None}
    )

    prompt = get_prompt_for_case(case, "why?", given_tools=DEFAULT_GIVEN)

    assert f"[TRUNCATED: {_CUT:,} more characters not shown.]" in prompt
    assert _HELD not in prompt
    assert _RULE_SENTENCE in _flat(prompt)


@pytest.mark.parametrize("given", [DEFAULT_GIVEN, NO_TOOLS], ids=["six", "none"])
def test_the_fallback_stub_marks_a_cut_head_and_not_a_whole_one(given):
    long_case = _case(10)
    short_case = _case(10)
    short_case.uploaded_files[0] = short_case.uploaded_files[0].model_copy(
        update={"structural_index": "ActiveState=failed"}
    )

    def stub(case):
        return _fallback_stub_block(
            case, PromptFence(mint_token()), given, head_chars=200
        )

    assert f" [TRUNCATED: {len(_PASTE) - 200:,} more characters not shown.]" in stub(
        long_case
    )
    short = stub(short_case)
    assert "ActiveState=failed" in short
    assert "[TRUNCATED:" not in short
