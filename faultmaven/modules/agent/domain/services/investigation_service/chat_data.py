"""The user's answer to an offer of what they told FaultMaven in a chat message (#1930).

What the user writes in a chat message is a claim; only submitted data is
evidence. When a message carries system output, or reports the state of the
user's system with no data showing it, the engine records a ``ChatDataOffer``
(``core/investigation/chat_data.py``) and the turn's reply carries its choices:

* **Yes, save it as data** — a ``save_chat_data`` intent. The offered blocks
  become ONE pasted file (the turn's attachment), and the turn then runs like
  any paste turn, so the model can record evidence from the file in the same
  turn. Admitted in ``admit_chat_data_save`` BEFORE the turn is consumed, so a
  click naming no offer costs nothing.
* **Not exactly, let me re-submit** — the client's own action: it opens its data
  box holding ``prefill`` (the blocks) for the user to correct and send. Never a
  request to the server.
* **Submit the output** — the one choice for a message that only REPORTED a
  state ("it's stable"): the client opens its data box, empty, the requested
  output as its hint. There is nothing to save: the message holds no output,
  and saving the user's own sentence would make a claim evidence.

There is no "no" choice. Ignoring the offer is the user's no: the message stays
a claim, as it would have without the offer. A save is final; a click on an
offer already saved is answered with that.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from faultmaven.core.investigation.chat_data import find_offer
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.investigation.turn_uploads import report_turn_uploads
from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.observability.evidence_metrics import (
    CHAT_DATA_OFFER_EVENTS_TOTAL,
)
from faultmaven.models.api_models import IntentType, SuggestedActionResponse
from faultmaven.modules.case.contracts import (
    Case,
    ChatDataOffer,
    ChatDataOfferState,
)

logger = logging.getLogger(__name__)

SAVE_LABEL = "Yes, save it as data"
RESUBMIT_LABEL = "Not exactly, let me re-submit"
SUBMIT_LABEL = "Submit the output"

#: Characters of the first block quoted in the note that introduces the choices.
_NOTE_PREVIEW_CHARS = 80


def admit_chat_data_save(case: Case, payload: TurnPayload) -> Optional[ChatDataOffer]:
    """Turn a "save it as data" click into the turn's attachment.

    Runs before the turn is consumed. A click naming no offer of this case is
    refused here (422) and costs nothing. A save of an offer still ``OFFERED``
    that holds output gains ONE pasted attachment holding the blocks, exactly as
    the user typed them, and the offer is returned for ``record_chat_data_save``
    to settle once the turn is admitted. A click on an offer already saved, or
    one holding no output, returns None and is answered by
    ``_handle_save_chat_data``.
    """
    intent = payload.intent
    if intent is None or intent.type != IntentType.SAVE_CHAT_DATA:
        return None
    offer = find_offer(case, intent.offer_id)
    if offer is None:
        raise ValidationException(
            f"No data offer {intent.offer_id!r} on this case.",
            {"offer_id": intent.offer_id},
        )
    if offer.state != ChatDataOfferState.OFFERED or not offer.blocks:
        return None
    payload.attachments.append(
        Attachment(
            content="\n\n".join(offer.blocks).encode("utf-8"),
            filename=f"chat-turn-{offer.turn}.txt",
            content_type="text/plain",
            source_metadata={"source_type": "text_paste", "chat_turn": offer.turn},
        )
    )
    return offer


def record_chat_data_save(
    offer: ChatDataOffer, *, turn: int, preprocess_results: List[Any]
) -> None:
    """Settle a save once its turn is admitted and its file is recorded.

    The file id is the one the paste produced or, when the case already held the
    same content, the existing file it was matched to. The paste is the turn's
    last attachment (``admit_chat_data_save`` appends it after anything the
    request carried).
    """
    offer.state = ChatDataOfferState.SAVED
    offer.decided_at_turn = turn
    offer.file_ids = [
        res.duplicate_of or res.uploaded_file.file_id
        for res in preprocess_results
        if getattr(res, "uploaded_file", None) is not None
    ][-1:]
    CHAT_DATA_OFFER_EVENTS_TOTAL.labels(event="saved").inc()


async def _handle_save_chat_data(
    case: Case,
    *,
    offer_id: Optional[str],
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Answer a "save it as data" click that saves nothing: the offer was
    already saved, or it holds no output (it only asked for some). No model
    call."""
    offer = find_offer(case, offer_id)
    if offer is None:
        # admit_chat_data_save refused this before the turn was consumed.
        raise ValidationException(
            f"No data offer {offer_id!r} on this case.", {"offer_id": offer_id}
        )
    if offer.state == ChatDataOfferState.SAVED:
        reply = (
            f"That output from your message (turn {offer.turn}) is already saved "
            "as data."
        )
    elif not offer.blocks:
        reply = (
            "There's no output in that message to save. Submit it as data "
            f"(pasted or attached): {offer.request}."
        )
    else:
        # A save reaching here was not admitted for a reason the route checks
        # first (a closed case) or that changed under the request.
        reply = "That output can't be saved as data on this case now."
    return {
        "agent_response": reply,
        "suggested_follow_ups": [],
        "case_updated": case,
        "metadata": {
            "progress_made": False,
            "milestones_completed": [],
            **report_turn_uploads(case.case_id, case.current_turn, attachments),
        },
    }


def chat_data_choices(
    case: Case, offer_id: Optional[str]
) -> Tuple[List[SuggestedActionResponse], Optional[str]]:
    """The choices for the offer this turn made, and the note introducing them."""
    offer = find_offer(case, offer_id)
    if offer is None or offer.state != ChatDataOfferState.OFFERED:
        return [], None
    if not offer.blocks:
        submit = SuggestedActionResponse(
            label=SUBMIT_LABEL,
            type="EVIDENCE",
            body=f"Send it as data (pasted or attached): {offer.request}.",
            prefill="",
        )
        note = (
            "\n\n---\nTo verify what you report and to record it, I need to see "
            f"it: {offer.request}. Submit it as data (pasted or attached), not "
            "typed into your message."
        )
        return [submit], note
    choices = [
        SuggestedActionResponse(
            label=SAVE_LABEL,
            type="DECIDE",
            payload="Save that output as data.",
            body="It is output copied from the system, unchanged.",
            intent={
                "type": IntentType.SAVE_CHAT_DATA.value,
                "offer_id": offer.offer_id,
            },
        ),
        SuggestedActionResponse(
            label=RESUBMIT_LABEL,
            type="EVIDENCE",
            body="Correct it, then send it as data (pasted or attached).",
            prefill="\n\n".join(offer.blocks),
        ),
    ]

    def _preview(line: str) -> str:
        line = line.strip()
        if len(line) > _NOTE_PREVIEW_CHARS:
            return line[: _NOTE_PREVIEW_CHARS - 1] + "…"
        return line

    block_lines = [line for block in offer.blocks for line in block.splitlines()]
    span = f"starting `{_preview(block_lines[0])}`"
    if len(block_lines) > 1:
        # Both ends, so a block that took in a line the user wrote shows it.
        span += f", ending `{_preview(block_lines[-1])}`"
    lines = len(block_lines)
    note = (
        f"\n\n---\nYour message includes output from a system ({lines} "
        f"line{'s' if lines != 1 else ''}, {span}). It isn't evidence yet: it "
        "counts as evidence once it is submitted as data."
    )
    if offer.request:
        note += f" To verify what you report, I also need: {offer.request}."
    return choices, note
