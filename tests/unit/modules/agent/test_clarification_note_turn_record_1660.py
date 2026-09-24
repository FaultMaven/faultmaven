"""A turn's record and its row agree on whether the model answered (#1660).

When an upload fails classification, the service appends a clarification note
to the reply — AFTER the turn has been recorded, by the engine's Step 6 or by
the service's own backfill, and both record a blank answer as unanswered. The
note makes the reply non-blank, so the persistence backstop never fires and the
row goes out unflagged. The next prompt then said two things about one turn:
EARLIER TURNS rendered "(no answer …)" from the record while the RECENT window
quoted the note from the row.

Settled towards the row, the way the engine settles its own composition: prose
composed onto a missing answer is a real reply. The note is the question the
user was shown, and the next turn needs it.

Every combination is driven through the real ``process_turn``, and the check is
the invariant itself — the record's flag equals the row's, and the two history
renderings agree — rather than one hand-picked outcome.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import RESPONSE_WITHHELD_TEXT
from faultmaven.core.investigation.prompts import context_builder as cb
from faultmaven.core.investigation.prompts.fence import mint_token
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.models.api import DataType
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
    _record_composed_reply,
)
from faultmaven.modules.case.contracts import MESSAGE_METADATA_AGENT_SYNTHESIZED
from faultmaven.modules.case.domain.models import TurnOutcome, TurnProgress

from .conftest import MockCaseRepository, create_sample_case

pytestmark = pytest.mark.unit

NOTE_OPENING = "One more thing — I couldn't confidently classify"

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
    writes it — the path the engine's terminal short-circuit takes."""

    def __init__(self, text: str, flagged: bool, records: bool) -> None:
        self.llm_provider = MagicMock()
        self._text, self._flagged, self._records = text, flagged, records
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
        return {
            "case_updated": case,
            "agent_response": self._text,
            "metadata": metadata,
        }


async def _run(answer: str, *, note: bool, records: bool):
    text, flagged = ANSWERS[answer]
    repo = MockCaseRepository()
    case = create_sample_case(user_id="user_owner")
    case.uploaded_files = []
    case.evidence = []
    repo._storage[case.case_id] = case

    preprocessing = MagicMock()
    preprocessing.classify_and_extract = AsyncMock(
        return_value=_failed_classification()
    )
    storage = MagicMock()
    storage.store_file = AsyncMock(return_value={"file_path": "evidence/x/blob.txt"})
    storage.mark_linked = AsyncMock(return_value=True)
    service = InvestigationService(
        milestone_engine=_Engine(text, flagged, records),
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
    response = await service.process_turn(
        case_id=case.case_id,
        user_id="user_owner",
        payload=TurnPayload(query="here is the dump", attachments=attachments),
    )
    saved = await repo.get(case.case_id)
    row = [m for m in saved.messages if m["role"] == "assistant"][-1]
    return response, saved, row


@pytest.mark.parametrize("records", [False, True], ids=["backfill", "engine_step6"])
@pytest.mark.parametrize("note", [False, True], ids=["no_note", "note"])
@pytest.mark.parametrize("answer", list(ANSWERS))
async def test_the_record_and_the_row_agree(answer, note, records):
    response, saved, row = await _run(answer, note=note, records=records)
    assert (NOTE_OPENING in response.agent_response) is note, "harness: note"

    row_flagged = bool(
        (row.get("metadata") or {}).get(MESSAGE_METADATA_AGENT_SYNTHESIZED)
    )
    record = saved.turn_history[-1]
    assert record.turn_number == saved.current_turn
    assert record.agent_response_synthesized is row_flagged

    # And therefore the two fidelities of the next prompt say the same thing.
    earlier = cb._build_turn_summary(record)
    recent = cb._build_verbatim_history(saved.messages, cb.PromptFence(mint_token()))
    assert (cb.NO_ANSWER_LINE in earlier) is (cb.NO_ANSWER_LINE in recent)


@pytest.mark.parametrize("records", [False, True], ids=["backfill", "engine_step6"])
async def test_a_note_on_a_blank_answer_is_the_reply_everywhere(records):
    """The case #1660 named: the note is quoted in both fidelities, as the
    reply it is, and neither calls the turn unanswered."""
    response, saved, row = await _run("blank", note=True, records=records)

    record = saved.turn_history[-1]
    assert row["content"] == response.agent_response
    assert record.agent_response_synthesized is False
    assert record.agent_response_summary.startswith(NOTE_OPENING)

    earlier = cb._build_turn_summary(record)
    recent = cb._build_verbatim_history(saved.messages, cb.PromptFence(mint_token()))
    assert f"| Agent: {NOTE_OPENING}" in earlier
    assert NOTE_OPENING in recent
    assert cb.NO_ANSWER_LINE not in earlier + recent


def test_only_this_turns_record_is_ever_rewritten():
    """Defensive, and unreachable through ``process_turn``: the backfill runs
    first and guarantees the newest record is this turn's. Pinned directly,
    because the alternative failure is silent — the previous turn's summary
    replaced by this turn's note."""
    case = create_sample_case(current_turn=2)
    earlier = TurnProgress(
        turn_number=1,
        progress_made=False,
        outcome=TurnOutcome.CONVERSATION,
        agent_response_summary="Could you share free -m from db-01?",
    )
    case.turn_history = [earlier]

    _record_composed_reply(case, "One more thing — how should I treat it?")

    assert case.turn_history == [earlier]
