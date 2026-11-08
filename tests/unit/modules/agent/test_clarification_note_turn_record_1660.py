"""A note the service composes onto an answer, in the row, the record and every
prompt reader (#1660, #1665).

When an upload fails classification, the service appends a clarification note
to the reply, and #1930 appends one on system output in the message. Both land
AFTER the turn has been recorded, by the engine's Step 6 or by the service's
own backfill. #1660 made the record agree with the row.

#1665 (ruled 2026-09-28, option (a)) settled what both say when the answer
under the note is unusable — blank, or a placeholder the engine flagged: the
row and the record are flagged, the note is stored apart from the answer
(``server_note``), and every prompt reader renders the no-answer marker
followed by the note as server text the user was shown. One rule for both
arms. The text the user saw does not change, and a note on a usable answer is
part of the reply, as before.

Every combination is driven through the real ``process_turn``, on a case with
three turns behind it, so the graduated history has a RECENT window.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.chat_data import (
    TURN_METADATA_CHAT_DATA_OFFER,
    mint_chat_data_offer,
)
from faultmaven.core.investigation.milestone_engine.response_synthesis import (
    RESPONSE_WITHHELD_TEXT,
)
from faultmaven.core.investigation.prompts.context_builder import history as cb
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.models.api import DataType
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_results import (
    _record_server_note,
)
from faultmaven.modules.agent.domain.services.orientation import (
    last_investigation_message,
)
from faultmaven.modules.agent.domain.services.out_of_band import OutOfBandTriage
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    MESSAGE_METADATA_SERVER_NOTE,
    is_server_written_assistant_row,
    server_note_of,
)
from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress

from .conftest import MockCaseRepository, create_sample_case

pytestmark = pytest.mark.unit

NOTE_OPENING = "One more thing — I couldn't confidently classify"
CHAT_NOTE_OPENING = "Your message includes output from a system"
#: How the note line opens, whatever the note.
NOTE_LINE = cb.SERVER_NOTE_LINE.split("{note}")[0]
#: The seeded case's last real answer.
EARLIER_ANSWER = "answer 3"
NEXT_MESSAGE = "treat it as a config file"
TRIAGE_NOTE = (
    "The assistant gave no usable answer on its last turn; the server then "
    "showed the user this note:\n<<<\n"
)

_OUTPUT = "NAME          READY   STATUS\napi-0         0/1     CrashLoopBackOff"

#: (reply text, whether the engine flagged it as its own placeholder)
ANSWERS = {
    "answered": ("The pool was exhausted by open transactions.", False),
    "blank": ("", False),
    "engine_placeholder": (RESPONSE_WITHHELD_TEXT, True),
    # Contradictory, and no current writer produces it — kept so "the record
    # follows the row" is checked where the two inputs disagree.
    "blank_but_flagged": ("", True),
}


def _failed_classification():
    result = MagicMock()
    result.summary = "preview summary"
    result.structural_index = "index"
    result.data_type = UnifiedDataType.TEXT
    result.detailed_data_type = DataType.UNSTRUCTURED_TEXT
    result.content_hash = "c" * 64
    result.extraction_method = "classification_failed"
    result.extraction_metadata = {"suggested_types": ["documentation"]}
    result.coverage_start_ts = None
    result.coverage_end_ts = None
    return result


class _Engine:
    """Answers exactly what the test asks. With ``records=True`` it writes the
    turn record the way the real Step 6 does; without, the service's backfill
    writes it — the path the engine's terminal short-circuit takes. With
    ``chat_data=True`` it offers the system output in the message, as the real
    engine does when the model names it (#1930)."""

    def __init__(self, text: str, flagged: bool, records: bool, chat_data: bool):
        self.deps = SimpleNamespace(
            llm_provider=MagicMock(), sanitizer=None, redis_client=None
        )
        self._text, self._flagged, self._records = text, flagged, records
        self._chat_data = chat_data
        self.process_turn = AsyncMock(side_effect=self._turn)

    async def _turn(self, *, case, user_message, **_kw):
        case.updated_at = datetime.now(timezone.utc)
        if self._records:
            case.turn_history.append(
                TurnProgress(
                    turn_number=case.current_turn,
                    progress_made=False,
                    outcome=TurnOutcome.CONVERSATION,
                    agent_response_summary=self._text,
                    agent_response_synthesized=(
                        self._flagged or not self._text.strip()
                    ),
                )
            )
        metadata = {"milestones_completed": [], "progress_made": False}
        if self._flagged:
            metadata[MESSAGE_METADATA_AGENT_SYNTHESIZED] = True
        if self._chat_data:
            lines = _OUTPUT.splitlines()
            offer = mint_chat_data_offer(
                case,
                user_message=user_message,
                named_blocks=[
                    SimpleNamespace(first_line=lines[0], last_line=lines[-1])
                ],
            )
            metadata[TURN_METADATA_CHAT_DATA_OFFER] = offer.offer_id
        return {
            "case_updated": case,
            "agent_response": self._text,
            "metadata": metadata,
        }


def _seeded_case():
    case = create_sample_case(user_id="user_owner", current_turn=3)
    case.uploaded_files = []
    case.evidence = []
    for n in (1, 2, 3):
        for role, text in (("user", f"user turn {n}"), ("assistant", f"answer {n}")):
            case.messages.append(
                {"turn_number": n, "role": role, "content": text, "metadata": {}}
            )
        case.turn_history.append(
            TurnProgress(
                turn_number=n,
                progress_made=False,
                outcome=TurnOutcome.CONVERSATION,
                user_message_summary=f"user turn {n}",
                agent_response_summary=f"answer {n}",
            )
        )
    return case


async def _run(answer: str, *, note: bool, records: bool, chat_data: bool = False):
    text, flagged = ANSWERS[answer]
    repo = MockCaseRepository()
    case = _seeded_case()
    repo._storage[case.case_id] = case

    preprocessing = MagicMock()
    preprocessing.classify_and_extract = AsyncMock(
        return_value=_failed_classification()
    )
    storage = MagicMock()
    storage.store_file = AsyncMock(return_value={"file_path": "evidence/x/blob.txt"})
    storage.mark_linked = AsyncMock(return_value=True)
    service = InvestigationService(
        milestone_engine=_Engine(text, flagged, records, chat_data),
        case_repository=repo,
        preprocessing_service=preprocessing,
        file_storage_service=storage,
    )
    attachments = (
        [
            Attachment(
                content=b"ambiguous bytes",
                filename="mystery.txt",
                content_type="text/plain",
                source_metadata={"source_type": "file_upload"},
            )
        ]
        if note
        else []
    )
    query = f"The pods won't start:\n\n{_OUTPUT}" if chat_data else "here is the dump"
    response = await service.process_turn(
        case_id=case.case_id,
        user_id="user_owner",
        payload=TurnPayload(query=query, attachments=attachments),
    )
    saved = await repo.get(case.case_id)
    row = [m for m in saved.messages if m["role"] == "assistant"][-1]
    return response, saved, row


def _renderings(saved) -> dict[str, str]:
    """The four prompt readers, over the turn just taken."""
    fence = PromptFence(mint_token())
    return {
        "EARLIER": cb._build_turn_summary(saved.turn_history[-1]),
        "RECENT": cb._build_graduated_history(saved, fence),
        "verbatim": cb._build_verbatim_history(saved.messages, fence),
        "previous_turn": cb._build_compact_history(saved, NEXT_MESSAGE, fence),
    }


def _note_follows_marker(out: str, opening: str) -> bool:
    """The marker, then on the next line (or, in the one-line summary, the
    next segment) the note line, opening with *opening*."""
    return bool(
        re.search(
            re.escape(cb.NO_ANSWER_LINE)
            + r"(\n| \| )"
            + re.escape(NOTE_LINE + opening),
            out,
        )
    )


@pytest.mark.parametrize("records", [False, True], ids=["backfill", "engine_step6"])
@pytest.mark.parametrize("note", [False, True], ids=["no_note", "note"])
@pytest.mark.parametrize("answer", list(ANSWERS))
async def test_the_record_and_the_row_agree(answer, note, records):
    response, saved, row = await _run(answer, note=note, records=records)
    assert (NOTE_OPENING in response.agent_response) is note, "harness: note"

    row_flagged = is_server_written_assistant_row(row)
    record = saved.turn_history[-1]
    assert record.turn_number == saved.current_turn
    assert record.agent_response_synthesized is row_flagged
    # #1665: and on the note the server showed after an unusable answer.
    row_note = server_note_of(row)
    assert record.server_note == (row_note.strip() if row_note else None)

    # And therefore the two fidelities of the next prompt say the same thing.
    earlier = cb._build_turn_summary(record)
    recent = cb._build_verbatim_history(saved.messages, PromptFence(mint_token()))
    assert (cb.NO_ANSWER_LINE in earlier) is (cb.NO_ANSWER_LINE in recent)
    assert (NOTE_LINE in earlier) is (NOTE_LINE in recent)


@pytest.mark.parametrize("records", [False, True], ids=["backfill", "engine_step6"])
@pytest.mark.parametrize("answer", ["engine_placeholder", "blank"])
async def test_a_note_on_an_unusable_answer_is_server_text_in_every_reader(
    answer, records
):
    """The ruling, on both arms: the marker, then the note as server text the
    user was shown — never the placeholder, and never the note as the
    assistant's words. The blank arm replaces #1660's pin, which quoted it."""
    response, saved, row = await _run(answer, note=True, records=records)
    note = row["metadata"][MESSAGE_METADATA_SERVER_NOTE]

    # What the user saw is unchanged: the unusable answer, then the note.
    assert row["content"] == ANSWERS[answer][0] + note == response.agent_response
    assert note.strip().startswith(NOTE_OPENING)
    assert is_server_written_assistant_row(row) and server_note_of(row) == note
    assert saved.turn_history[-1].agent_response_synthesized is True
    assert saved.turn_history[-1].server_note == note.strip()

    whole = cb.SERVER_NOTE_LINE.format(note=note.strip())
    one_line = cb.SERVER_NOTE_LINE.format(note=" ".join(note.split())[:200])
    expected = {
        "EARLIER": f" | {cb.NO_ANSWER_LINE} | {one_line}",
        "RECENT": f"\n{cb.NO_ANSWER_LINE}\n{whole}\n",
        "verbatim": f"\n{cb.NO_ANSWER_LINE}\n{whole}\n",
        "previous_turn": f"\n{cb.NO_ANSWER_LINE}\n{one_line}\n</previous_turn>",
    }
    renderings = _renderings(saved)
    assert renderings["EARLIER"].endswith(expected["EARLIER"])
    assert "\n" not in renderings["EARLIER"]
    for reader, out in renderings.items():
        assert expected[reader] in out, reader
        assert out.count(NOTE_OPENING) == 1, reader
        assert RESPONSE_WITHHELD_TEXT not in out, reader
        assert "Agent: One more thing" not in out, reader


async def test_the_chat_data_note_is_recorded_by_the_same_rule():
    """#1930's note on system output in the message is appended by the same
    code, so the one writer covers it too."""
    response, saved, row = await _run(
        "engine_placeholder", note=False, records=True, chat_data=True
    )
    note = server_note_of(row)
    assert note is not None and CHAT_NOTE_OPENING in note
    assert row["content"] == RESPONSE_WITHHELD_TEXT + note == response.agent_response
    assert CHAT_NOTE_OPENING in saved.turn_history[-1].server_note

    renderings = _renderings(saved)
    assert "\n" not in renderings["EARLIER"]
    for reader, out in renderings.items():
        # The note opens with a ``---`` rule; every form keeps it in the note.
        assert _note_follows_marker(out, "---"), reader
        assert out.count(CHAT_NOTE_OPENING) == 1, reader
        assert RESPONSE_WITHHELD_TEXT not in out, reader


@pytest.mark.parametrize("answer", ["engine_placeholder", "blank"])
async def test_triage_reads_the_note_and_the_greeting_does_not(answer):
    """The triage classifier is shown the question the user was asked, beside
    the last real answer, whose block keeps its meaning. The greeting's "Where
    we left off" still passes the flagged row over — on the blank arm that
    means it no longer quotes the note."""
    _, saved, _ = await _run(answer, note=True, records=True)
    prompt = OutOfBandTriage._build_prompt(saved, NEXT_MESSAGE)

    last_answer = f"The assistant's last answer began:\n<<<\n{EARLIER_ANSWER}\n>>>"
    assert last_answer in prompt
    assert TRIAGE_NOTE + NOTE_OPENING in prompt
    assert prompt.index(last_answer) < prompt.index(TRIAGE_NOTE)
    assert RESPONSE_WITHHELD_TEXT not in prompt
    assert last_investigation_message(saved) == EARLIER_ANSWER


async def test_a_note_on_a_usable_answer_is_still_the_reply():
    response, saved, row = await _run("answered", note=True, records=True)

    assert not is_server_written_assistant_row(row)
    assert MESSAGE_METADATA_SERVER_NOTE not in row["metadata"]
    assert saved.turn_history[-1].server_note is None
    verbatim = cb._build_verbatim_history(saved.messages, PromptFence(mint_token()))
    assert f"ASSISTANT: {response.agent_response}\n" in verbatim
    for reader, out in _renderings(saved).items():
        assert cb.NO_ANSWER_LINE not in out and NOTE_LINE not in out, reader
    assert TRIAGE_NOTE not in OutOfBandTriage._build_prompt(saved, NEXT_MESSAGE)


@pytest.mark.parametrize("records", [False, True], ids=["backfill", "engine_step6"])
async def test_a_placeholder_with_no_note_renders_the_marker_alone(records):
    _, saved, row = await _run("engine_placeholder", note=False, records=records)

    assert server_note_of(row) is None
    assert saved.turn_history[-1].server_note is None
    for reader, out in _renderings(saved).items():
        assert cb.NO_ANSWER_LINE in out and NOTE_LINE not in out, reader
    assert TRIAGE_NOTE not in OutOfBandTriage._build_prompt(saved, NEXT_MESSAGE)


def _placeholder_record(**update) -> TurnProgress:
    return TurnProgress(
        turn_number=1,
        progress_made=False,
        outcome=TurnOutcome.CONVERSATION,
        user_message_summary="disk full on /var",
        agent_response_summary=RESPONSE_WITHHELD_TEXT,
        agent_response_synthesized=True,
    ).model_copy(update=update)


def test_a_record_stored_before_the_field_loads_and_renders_as_before():
    stored = _placeholder_record().model_dump(mode="json")
    del stored["server_note"]

    record = TurnProgress(**stored)

    assert record.server_note is None
    assert cb._build_turn_summary(record) == (
        f"TURN 1: disk full on /var | {cb.NO_ANSWER_LINE}"
    )


def test_a_long_note_reloads_and_the_one_line_forms_stay_one_line():
    """No length cap on the record's copy: its writer replaces the record with
    ``model_copy``, which skips validation, and the repositories reload with
    ``TurnProgress(**t)``, which would not."""
    note = "\n\n" + "a line of the note\n" * 55
    assert len(note) > 1000

    reloaded = TurnProgress(
        **_placeholder_record(server_note=note).model_dump(mode="json")
    )

    assert reloaded.server_note == note
    summary = cb._build_turn_summary(reloaded)
    assert "\n" not in summary
    assert summary.endswith(
        cb.SERVER_NOTE_LINE.format(note=" ".join(note.split())[:200])
    )


def test_only_this_turns_record_is_ever_rewritten():
    """Defensive, and unreachable through ``process_turn``: the backfill runs
    first and guarantees the newest record is this turn's. Pinned directly,
    because the alternative failure is silent — the previous turn's record
    flagged and given this turn's note."""
    case = create_sample_case(current_turn=2)
    earlier = TurnProgress(
        turn_number=1,
        progress_made=False,
        outcome=TurnOutcome.CONVERSATION,
        agent_response_summary="Could you share free -m from db-01?",
    )
    case.turn_history = [earlier]
    turn_meta: dict = {}

    _record_server_note(case, turn_meta, "\n\nOne more thing — how should I treat it?")

    assert case.turn_history == [earlier]
    assert turn_meta[MESSAGE_METADATA_AGENT_SYNTHESIZED] is True
